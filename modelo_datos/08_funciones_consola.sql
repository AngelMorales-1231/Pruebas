-- ============================================================================
-- 08_funciones_consola.sql — Procedimientos almacenados de la consola interna
-- Especificación: Acredittia_Especificacion_API_Consola_Interna.docx §3.1, §4
--
-- La lógica de agregación vive aquí (decisión de diseño): la API solo orquesta
-- y serializa. Todas las funciones son STABLE salvo las que escriben.
-- Convención de exclusión: role='admin' (personal de Acredittia) nunca cuenta.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Normalización de precio a MRR en CLP
--    'UF' se convierte con el valor recibido; 'CLP' pasa tal cual; periodo
--    'anual' se prorratea a mes. Cualquier otro periodo se trata como mensual.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops_precio_a_mrr_clp(
  p_precio numeric, p_moneda text, p_periodo text, p_valor_uf numeric
) RETURNS numeric
LANGUAGE sql IMMUTABLE AS $$
  SELECT round(
    (CASE WHEN p_moneda = 'UF' THEN p_precio * p_valor_uf ELSE p_precio END)
    / (CASE WHEN p_periodo = 'anual' THEN 12 ELSE 1 END), 2);
$$;

-- ----------------------------------------------------------------------------
-- 2. Estado de cuenta derivado (§3.1)
--    al_dia    sin facturas vencidas impagas
--    en_riesgo 1 factura vencida impaga, o trial vencido hace < 15 días
--    moroso    >= 2 facturas vencidas impagas, o > 30 días de atraso
--    "Vencida" = pendiente y emitida hace más de 14 días (las facturas no
--    llevan fecha de vencimiento propia; si se agrega, cambiar solo aquí).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops_estado_cuenta(p_company uuid)
RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_vencidas   integer;
  v_max_atraso integer;   -- días de atraso de la factura más antigua
  v_trial_vto  date;
BEGIN
  SELECT count(*),
         COALESCE(max(extract(day FROM now() - emitida_at)::integer - 14), 0)
    INTO v_vencidas, v_max_atraso
    FROM facturas
   WHERE company_id = p_company
     AND estado = 'pendiente'
     AND emitida_at < now() - interval '14 days';

  IF v_vencidas >= 2 OR v_max_atraso > 30 THEN
    RETURN 'moroso';
  END IF;

  SELECT trial_hasta INTO v_trial_vto
    FROM suscripciones
   WHERE company_id = p_company AND estado = 'trial';

  IF v_vencidas = 1
     OR (v_trial_vto IS NOT NULL AND v_trial_vto < current_date
         AND v_trial_vto >= current_date - 15) THEN
    RETURN 'en_riesgo';
  END IF;
  RETURN 'al_dia';
END $$;

-- ----------------------------------------------------------------------------
-- 3. Usuarios y actividad por empresa (§3.1)
--    activo = last_login_at dentro de la ventana O una fila en actividad.
--    Devuelve una fila por empresa aprobada, incluidas las que tienen 0 usuarios.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops_usuarios_por_empresa(p_ventana_dias integer DEFAULT 30)
RETURNS TABLE (
  company_id     uuid,
  usuarios       integer,
  activos        integer,
  sin_acceso_90d integer,
  ultima_actividad_at timestamptz
)
LANGUAGE sql STABLE AS $$
  WITH corte AS (
    SELECT now() - make_interval(days => p_ventana_dias) AS ventana,
           now() - interval '90 days'                     AS n90
  ),
  act AS (                       -- usuarios con actividad en la ventana
    SELECT DISTINCT a.user_id
      FROM actividad a, corte
     WHERE a.user_id IS NOT NULL AND a.created_at >= corte.ventana
  ),
  ult AS (                       -- última actividad registrada por empresa
    SELECT a.company_id, max(a.created_at) AS ultima
      FROM actividad a GROUP BY a.company_id
  )
  SELECT c.id,
         count(u.id)::integer,
         count(u.id) FILTER (
           WHERE u.last_login_at >= corte.ventana
              OR u.id IN (SELECT user_id FROM act))::integer,
         count(u.id) FILTER (
           WHERE COALESCE(u.last_login_at, u.created_at) < corte.n90
             AND u.id NOT IN (SELECT user_id FROM act))::integer,
         ult.ultima
    FROM companies c
    CROSS JOIN corte
    LEFT JOIN users u ON u.company_id = c.id
                     AND u.role <> 'admin'
                     AND u.status = 'approved'
                     AND u.activo
    LEFT JOIN ult ON ult.company_id = c.id
   WHERE c.status = 'approved'
   GROUP BY c.id, ult.ultima, corte.ventana, corte.n90;
$$;

-- ----------------------------------------------------------------------------
-- 4. MRR vigente en CLP (suscripciones activas + trial NO cuentan: solo activa)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops_mrr_actual(p_valor_uf numeric)
RETURNS numeric
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(sum(ops_precio_a_mrr_clp(p.precio, p.moneda, p.periodo, p_valor_uf)), 0)
    FROM suscripciones s
    JOIN planes p ON p.id = s.plan_id
   WHERE s.estado = 'activa';
$$;

-- ----------------------------------------------------------------------------
-- 5. Cálculo de métricas de un mes (composite, sin escribir)
--    Para el mes en curso los valores son "a la fecha" (parcial). Para meses
--    pasados solo es exacto si se ejecuta el día 1 siguiente (así lo hace el
--    job de cierre): las tablas transaccionales no guardan historia de estado.
--    El puente de MRR se calcula contra el snapshot del mes anterior:
--      nuevos    = MRR de suscripciones creadas en el mes (estado activa)
--      perdido   = -MRR de suscripciones canceladas en el mes (a su plan)
--      expansion / contraccion = 0 por ahora: exigen historial de cambios de
--        plan (tabla suscripcion_eventos, pendiente de producto). El NRR
--        degenera entonces en (prev + perdido) / prev.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops_calc_metricas(p_mes date, p_valor_uf numeric)
RETURNS metricas_mensuales
LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_ini  date := date_trunc('month', p_mes)::date;
  v_fin  date := (date_trunc('month', p_mes) + interval '1 month')::date;
  r      metricas_mensuales;
  prev   metricas_mensuales;
BEGIN
  IF v_ini > current_date THEN
    RAISE EXCEPTION 'MES_FUTURO: no se pueden calcular métricas de %', v_ini;
  END IF;

  SELECT * INTO prev FROM metricas_mensuales
   WHERE mes = (v_ini - interval '1 month')::date;

  r.mes      := v_ini;
  r.valor_uf := p_valor_uf;
  r.mrr_clp  := ops_mrr_actual(p_valor_uf);

  SELECT count(*) INTO r.clientes_activos
    FROM companies c
    JOIN suscripciones s ON s.company_id = c.id
   WHERE c.status = 'approved'
     AND (s.estado = 'activa'
          OR (s.estado = 'trial' AND COALESCE(s.trial_hasta, current_date) >= current_date));

  SELECT count(*) INTO r.altas
    FROM suscripciones WHERE created_at >= v_ini AND created_at < v_fin;

  SELECT count(*) INTO r.bajas
    FROM suscripciones
   WHERE estado = 'cancelada' AND updated_at >= v_ini AND updated_at < v_fin;

  SELECT COALESCE(sum(ops_precio_a_mrr_clp(p.precio, p.moneda, p.periodo, p_valor_uf)), 0)
    INTO r.mrr_nuevos_clp
    FROM suscripciones s JOIN planes p ON p.id = s.plan_id
   WHERE s.estado = 'activa' AND s.created_at >= v_ini AND s.created_at < v_fin;

  SELECT -COALESCE(sum(ops_precio_a_mrr_clp(p.precio, p.moneda, p.periodo, p_valor_uf)), 0)
    INTO r.mrr_perdido_clp
    FROM suscripciones s JOIN planes p ON p.id = s.plan_id
   WHERE s.estado = 'cancelada' AND s.updated_at >= v_ini AND s.updated_at < v_fin;

  r.mrr_expansion_clp   := 0;   -- pendiente: suscripcion_eventos
  r.mrr_contraccion_clp := 0;

  SELECT COALESCE(sum(f.usuarios), 0), COALESCE(sum(f.activos), 0)
    INTO r.usuarios_totales, r.usuarios_activos
    FROM ops_usuarios_por_empresa(30) f;

  IF prev.mes IS NOT NULL AND prev.clientes_activos > 0 THEN
    r.churn_clientes_pct := round(100.0 * r.bajas / prev.clientes_activos, 2);
  END IF;
  IF prev.mes IS NOT NULL AND prev.mrr_clp > 0 THEN
    r.churn_ingresos_pct := round(-100.0 * (r.mrr_perdido_clp + r.mrr_contraccion_clp)
                                  / prev.mrr_clp, 2);
    r.nrr_pct := round(100.0 * (prev.mrr_clp + r.mrr_expansion_clp
                                + r.mrr_contraccion_clp + r.mrr_perdido_clp)
                       / prev.mrr_clp, 2);
  END IF;

  r.cerrado      := false;
  r.calculado_at := now();
  RETURN r;
END $$;

-- ----------------------------------------------------------------------------
-- 6. Desglose por plan del mes (composite set, sin escribir)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops_calc_metricas_plan(p_mes date, p_valor_uf numeric)
RETURNS SETOF metricas_mensuales_plan
LANGUAGE sql STABLE AS $$
  WITH usuarios AS (
    SELECT s.plan_id, count(u.id) AS usuarios
      FROM suscripciones s
      LEFT JOIN users u ON u.company_id = s.company_id
                       AND u.role <> 'admin' AND u.status = 'approved' AND u.activo
     WHERE s.estado IN ('activa', 'trial')
     GROUP BY s.plan_id
  ),
  bajas AS (
    SELECT s.plan_id, count(*) AS bajas
      FROM suscripciones s
     WHERE s.estado = 'cancelada'
       AND s.updated_at >= date_trunc('month', p_mes)
       AND s.updated_at <  date_trunc('month', p_mes) + interval '1 month'
     GROUP BY s.plan_id
  )
  SELECT date_trunc('month', p_mes)::date,
         p.id,
         count(s.id) FILTER (WHERE s.estado IN ('activa','trial'))::integer,
         COALESCE(max(u.usuarios), 0)::integer,
         COALESCE(sum(ops_precio_a_mrr_clp(p.precio, p.moneda, p.periodo, p_valor_uf))
                  FILTER (WHERE s.estado = 'activa'), 0),
         CASE WHEN count(s.id) FILTER (WHERE s.estado IN ('activa','trial')) > 0
              THEN round(100.0 * COALESCE(max(b.bajas), 0)
                         / count(s.id) FILTER (WHERE s.estado IN ('activa','trial')), 2)
         END,
         NULL::numeric(6,2)          -- NRR por plan: pendiente de suscripcion_eventos
    FROM planes p
    LEFT JOIN suscripciones s ON s.plan_id = p.id
    LEFT JOIN usuarios u ON u.plan_id = p.id
    LEFT JOIN bajas b ON b.plan_id = p.id
   GROUP BY p.id, p.precio
  HAVING count(s.id) > 0
   ORDER BY max(p.precio);
$$;

-- ----------------------------------------------------------------------------
-- 7. Snapshot: calcula y persiste el mes (upsert). Un mes cerrado es inmutable
--    salvo p_forzar (re-cálculo manual auditado desde la consola).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops_snapshot_metricas(
  p_mes date, p_valor_uf numeric,
  p_cerrar boolean DEFAULT false, p_forzar boolean DEFAULT false
) RETURNS metricas_mensuales
LANGUAGE plpgsql AS $$
DECLARE
  r metricas_mensuales;
  v_cerrado boolean;
BEGIN
  SELECT cerrado INTO v_cerrado FROM metricas_mensuales
   WHERE mes = date_trunc('month', p_mes)::date;
  IF COALESCE(v_cerrado, false) AND NOT p_forzar THEN
    RAISE EXCEPTION 'MES_CERRADO: % ya está consolidado', p_mes;
  END IF;

  r := ops_calc_metricas(p_mes, p_valor_uf);
  r.cerrado := p_cerrar;

  INSERT INTO metricas_mensuales AS m
  VALUES (r.*)
  ON CONFLICT (mes) DO UPDATE SET
    valor_uf = EXCLUDED.valor_uf,           mrr_clp = EXCLUDED.mrr_clp,
    mrr_nuevos_clp = EXCLUDED.mrr_nuevos_clp,
    mrr_expansion_clp = EXCLUDED.mrr_expansion_clp,
    mrr_contraccion_clp = EXCLUDED.mrr_contraccion_clp,
    mrr_perdido_clp = EXCLUDED.mrr_perdido_clp,
    clientes_activos = EXCLUDED.clientes_activos,
    altas = EXCLUDED.altas,                 bajas = EXCLUDED.bajas,
    churn_clientes_pct = EXCLUDED.churn_clientes_pct,
    churn_ingresos_pct = EXCLUDED.churn_ingresos_pct,
    nrr_pct = EXCLUDED.nrr_pct,
    usuarios_totales = EXCLUDED.usuarios_totales,
    usuarios_activos = EXCLUDED.usuarios_activos,
    cerrado = EXCLUDED.cerrado,             calculado_at = EXCLUDED.calculado_at;

  DELETE FROM metricas_mensuales_plan WHERE mes = r.mes;
  INSERT INTO metricas_mensuales_plan
    SELECT * FROM ops_calc_metricas_plan(p_mes, p_valor_uf);

  RETURN r;
END $$;

-- ----------------------------------------------------------------------------
-- 8. Consolidación diaria de disponibilidad (upsert idempotente).
--    Los datos crudos viven en Redis; el worker los agrega y llama aquí.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops_upsert_disponibilidad(
  p_componente text, p_fecha date, p_estado ops_comp_estado,
  p_uptime numeric, p_p95 integer, p_total integer, p_fallidos integer
) RETURNS void
LANGUAGE sql AS $$
  INSERT INTO ops_disponibilidad_diaria AS d
    (componente, fecha, estado, uptime_pct, latencia_p95_ms, checks_total, checks_fallidos)
  VALUES (p_componente, p_fecha, p_estado, p_uptime, p_p95, p_total, p_fallidos)
  ON CONFLICT (componente, fecha) DO UPDATE SET
    estado = EXCLUDED.estado, uptime_pct = EXCLUDED.uptime_pct,
    latencia_p95_ms = EXCLUDED.latencia_p95_ms,
    checks_total = EXCLUDED.checks_total, checks_fallidos = EXCLUDED.checks_fallidos;
$$;

-- ----------------------------------------------------------------------------
-- 9. Poda de telemetría (job ops_retencion). Devuelve filas eliminadas.
--    También marca 'timeout' los runs colgados en running más allá del
--    timeout del job (el worker murió sin cerrar el run).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops_podar_telemetria()
RETURNS TABLE (runs_podados bigint, incidentes_podados bigint, runs_timeout bigint)
LANGUAGE plpgsql AS $$
DECLARE
  v_runs bigint; v_inc bigint; v_to bigint;
BEGIN
  UPDATE ops_job_runs r SET status = 'timeout',
         finished_at = now(),
         mensaje = COALESCE(r.mensaje, 'Marcado timeout por retención: el worker no cerró el run')
    FROM ops_jobs j
   WHERE j.clave = r.job_clave AND r.status = 'running'
     AND r.started_at < now() - make_interval(secs => j.timeout_s * 2);
  GET DIAGNOSTICS v_to = ROW_COUNT;

  DELETE FROM ops_job_runs WHERE created_at < now() - interval '180 days';
  GET DIAGNOSTICS v_runs = ROW_COUNT;

  DELETE FROM ops_incidentes
   WHERE estado = 'resuelto' AND resuelto_at < now() - interval '2 years';
  GET DIAGNOSTICS v_inc = ROW_COUNT;

  RETURN QUERY SELECT v_runs, v_inc, v_to;
END $$;
