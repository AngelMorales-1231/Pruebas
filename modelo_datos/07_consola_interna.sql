-- ============================================================================
-- 07_consola_interna.sql — Consola Interna de Administración (/admin/ops)
-- Especificación: Acredittia_Especificacion_API_Consola_Interna.docx §4
--
-- Requiere: 01..04 + 06 aplicados (versión de esquema 6). Este script sube la
-- versión a 7 (la detección usa la existencia de ops_jobs, ver database.py).
-- Ejecutar en autocommit, como el resto de los scripts del modelo.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Tipos enumerados (§4.1)
-- ----------------------------------------------------------------------------
CREATE TYPE company_industria AS ENUM ('mineria','construccion','energia','industrial','otras');
CREATE TYPE ops_job_tipo      AS ENUM ('programado','continuo');
CREATE TYPE ops_run_status    AS ENUM ('queued','running','ok','error','timeout');
CREATE TYPE ops_run_trigger   AS ENUM ('cron','manual','reintento');
CREATE TYPE ops_comp_estado   AS ENUM ('operativo','degradado','caido');
CREATE TYPE ops_inc_estado    AS ENUM ('abierto','monitoreando','resuelto');

-- ----------------------------------------------------------------------------
-- 2. Cambios en tablas existentes (§4.2)
-- ----------------------------------------------------------------------------
ALTER TABLE companies ADD COLUMN industria company_industria NOT NULL DEFAULT 'otras';

-- La auditoría de la consola escribe en `actividad` con modulo='ops' y SIN
-- tenant (las acciones son cross-tenant). Para el resto de la aplicación la
-- columna sigue comportándose como obligatoria: solo el código de ops inserta
-- NULL, y las políticas RLS por company_id dejan esas filas invisibles para
-- los roles de empresa (NULL nunca iguala a app.company_id).
ALTER TABLE actividad ALTER COLUMN company_id DROP NOT NULL;

-- ----------------------------------------------------------------------------
-- 3. Componentes monitoreados y disponibilidad diaria (§4.3)
-- ----------------------------------------------------------------------------
CREATE TABLE ops_componentes (
  clave       text PRIMARY KEY,             -- 'api' | 'db' | 'workers' | 'storage' | 'ia' | 'integraciones'
  nombre      text NOT NULL,
  descripcion text,
  orden       smallint NOT NULL DEFAULT 0,
  activo      boolean NOT NULL DEFAULT true
);

CREATE TABLE ops_disponibilidad_diaria (
  componente      text NOT NULL REFERENCES ops_componentes(clave) ON DELETE CASCADE,
  fecha           date NOT NULL,
  estado          ops_comp_estado NOT NULL,   -- el peor estado observado en el día
  uptime_pct      numeric(5,2) NOT NULL CHECK (uptime_pct BETWEEN 0 AND 100),
  latencia_p95_ms integer,
  checks_total    integer NOT NULL DEFAULT 0,
  checks_fallidos integer NOT NULL DEFAULT 0,
  PRIMARY KEY (componente, fecha)
);

-- ----------------------------------------------------------------------------
-- 4. Catálogo de procesos críticos y sus ejecuciones (§4.4)
-- ----------------------------------------------------------------------------
CREATE TABLE ops_jobs (
  clave          text PRIMARY KEY,           -- 'vencimientos', 'metricas_snapshot', ...
  nombre         text NOT NULL,
  tipo           ops_job_tipo NOT NULL,
  cron_expr      text,                       -- NULL si tipo='continuo'
  componente     text REFERENCES ops_componentes(clave),
  activo         boolean NOT NULL DEFAULT true,   -- false = pausado (solo afecta el cron)
  pausado_por    uuid REFERENCES users(id),
  pausado_motivo text,
  timeout_s      integer NOT NULL DEFAULT 3600,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_ops_jobs_cron CHECK ((tipo = 'programado') = (cron_expr IS NOT NULL))
);

CREATE TABLE ops_job_runs (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_clave        text NOT NULL REFERENCES ops_jobs(clave) ON DELETE CASCADE,
  status           ops_run_status NOT NULL DEFAULT 'queued',
  disparo          ops_run_trigger NOT NULL DEFAULT 'cron',
  actor_user_id    uuid REFERENCES users(id),      -- NULL si disparo='cron'
  celery_task_id   text,
  params           jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at       timestamptz,
  finished_at      timestamptz,
  items_procesados integer,
  mensaje          text,                            -- resumen legible
  error_detalle    text,                            -- traza truncada a 8 KB
  created_at       timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 5. Incidentes (§4.5)
-- ----------------------------------------------------------------------------
CREATE TABLE ops_incidentes (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  componente   text NOT NULL REFERENCES ops_componentes(clave),
  severidad    ops_comp_estado NOT NULL CHECK (severidad <> 'operativo'),
  estado       ops_inc_estado NOT NULL DEFAULT 'abierto',
  origen       text NOT NULL CHECK (origen IN ('auto','manual')),
  titulo       text NOT NULL CHECK (length(titulo) BETWEEN 1 AND 140),
  descripcion  text,
  resolucion   text,
  abierto_at   timestamptz NOT NULL DEFAULT now(),
  resuelto_at  timestamptz,
  creado_por   uuid REFERENCES users(id),  -- NULL si origen='auto'
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_inc_resuelto CHECK (
    estado <> 'resuelto' OR (resuelto_at IS NOT NULL AND resolucion IS NOT NULL))
);

-- ----------------------------------------------------------------------------
-- 6. Snapshots mensuales del negocio (§4.6)
-- ----------------------------------------------------------------------------
CREATE TABLE metricas_mensuales (
  mes                  date PRIMARY KEY CHECK (mes = date_trunc('month', mes)::date),
  valor_uf             numeric(10,2) NOT NULL,   -- UF usada en la conversión (reproducibilidad)
  mrr_clp              numeric(14,2) NOT NULL,
  mrr_nuevos_clp       numeric(14,2) NOT NULL DEFAULT 0,
  mrr_expansion_clp    numeric(14,2) NOT NULL DEFAULT 0,
  mrr_contraccion_clp  numeric(14,2) NOT NULL DEFAULT 0,   -- <= 0
  mrr_perdido_clp      numeric(14,2) NOT NULL DEFAULT 0,   -- <= 0
  clientes_activos     integer NOT NULL,
  altas                integer NOT NULL DEFAULT 0,
  bajas                integer NOT NULL DEFAULT 0,
  churn_clientes_pct   numeric(5,2),
  churn_ingresos_pct   numeric(5,2),
  nrr_pct              numeric(6,2),
  usuarios_totales     integer NOT NULL,
  usuarios_activos     integer NOT NULL,
  cerrado              boolean NOT NULL DEFAULT false,
  calculado_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE metricas_mensuales_plan (
  mes                date NOT NULL REFERENCES metricas_mensuales(mes) ON DELETE CASCADE,
  plan_id            uuid NOT NULL REFERENCES planes(id),
  clientes           integer NOT NULL,
  usuarios           integer NOT NULL,
  mrr_clp            numeric(14,2) NOT NULL,
  churn_clientes_pct numeric(5,2),
  nrr_pct            numeric(6,2),
  PRIMARY KEY (mes, plan_id)
);

-- ----------------------------------------------------------------------------
-- 7. Índices (los de las tablas nuevas + apoyo a los cálculos de la consola)
-- ----------------------------------------------------------------------------
CREATE INDEX ix_runs_job_fecha   ON ops_job_runs (job_clave, created_at DESC);
CREATE INDEX ix_runs_status      ON ops_job_runs (status) WHERE status IN ('queued','running');
CREATE INDEX ix_inc_abiertos     ON ops_incidentes (componente) WHERE estado <> 'resuelto';
CREATE INDEX ix_inc_abierto_at   ON ops_incidentes (abierto_at DESC);
CREATE INDEX ix_disp_fecha       ON ops_disponibilidad_diaria (fecha);

-- Actividad por usuario en ventana: alimenta la definición de "usuario activo"
-- (§3.1). El índice existente de actividad es por company, no por user.
CREATE INDEX ix_actividad_user_fecha ON actividad (user_id, created_at DESC)
  WHERE user_id IS NOT NULL;

-- Último acceso: clasifica activos/inactivos sin recorrer toda la tabla.
CREATE INDEX ix_users_last_login ON users (last_login_at)
  WHERE role <> 'admin' AND status = 'approved';

-- Facturas pendientes por empresa: base del estado de cuenta derivado.
CREATE INDEX ix_facturas_pendientes ON facturas (company_id, emitida_at)
  WHERE estado = 'pendiente';

-- Filtros de la tabla de clientes de la consola.
CREATE INDEX ix_companies_industria ON companies (industria);
CREATE INDEX ix_susc_estado         ON suscripciones (estado);

-- ----------------------------------------------------------------------------
-- 8. Triggers de updated_at (mismo helper fn_touch_updated_at de 03_triggers.sql)
-- ----------------------------------------------------------------------------
CREATE TRIGGER trg_ops_jobs_touch BEFORE UPDATE ON ops_jobs
  FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
CREATE TRIGGER trg_ops_incidentes_touch BEFORE UPDATE ON ops_incidentes
  FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

-- ----------------------------------------------------------------------------
-- 9. RLS: sin company_id, visibles solo con contexto admin (§4.7)
-- ----------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'ops_componentes','ops_disponibilidad_diaria','ops_jobs','ops_job_runs',
    'ops_incidentes','metricas_mensuales','metricas_mensuales_plan']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    -- app_is_admin() es el helper que ya definió 04_rls.sql.
    EXECUTE format(
      'CREATE POLICY p_ops_solo_admin ON %I '
      'USING (app_is_admin()) WITH CHECK (app_is_admin())', t);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- 10. Seeds: componentes y catálogo inicial de procesos
--     Las claves de ops_jobs deben coincidir con OPS_RUNNERS (services/ops.py).
-- ----------------------------------------------------------------------------
INSERT INTO ops_componentes (clave, nombre, descripcion, orden) VALUES
  ('api',           'API Backend',            'FastAPI en Container Apps',           1),
  ('db',            'Base de datos',          'PostgreSQL 16 · primario + réplica',  2),
  ('workers',       'Workers · Celery',       'Colas: documentos, alertas, sync',    3),
  ('storage',       'Storage de documentos',  'Azure Blob Storage',                  4),
  ('ia',            'Verificación IA',        'Lectura y validación de documentos',  5),
  ('integraciones', 'Integraciones externas', 'SIGA · Workmate · Metacontratas · WebControl', 6)
ON CONFLICT (clave) DO NOTHING;

INSERT INTO ops_jobs (clave, nombre, tipo, cron_expr, componente, timeout_s) VALUES
  ('vencimientos',          'Cálculo de vencimientos y snapshots', 'programado', '30 0 * * *',  'workers', 3600),
  ('reportes_programados',  'Reportes programados',                'programado', '5 * * * *',   'workers', 1800),
  ('purga_temporales',      'Purga de blobs temporales IA',        'programado', '0 4 * * *',   'storage',  900),
  ('ops_heartbeat',         'Sondeo de componentes',               'programado', '* * * * *',   'api',       55),
  ('ops_disponibilidad',    'Consolidación de disponibilidad',     'programado', '5 0 * * *',   'api',      600),
  ('metricas_snapshot',     'Snapshot de métricas de negocio',     'programado', '10 2 * * *',  'api',     1800),
  ('ops_retencion',         'Retención y poda de telemetría',      'programado', '30 3 * * 0',  'db',      1800),
  ('ia_revision',           'Verificación IA de documentos',       'continuo',   NULL,          'ia',      3600),
  ('webhooks_clientes',     'Webhooks a clientes',                 'continuo',   NULL,          'api',     3600)
ON CONFLICT (clave) DO NOTHING;
