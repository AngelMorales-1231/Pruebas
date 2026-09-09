"""Dominio de la consola interna: heartbeats, incidentes, runs y auditoría.

Tres piezas:

* **HeartbeatStore** — último latido por componente. Redis en producción
  (TTL = ops_heartbeat_ttl_s) y un diccionario en proceso para desarrollo y
  tests, elegido con la misma regla que la cola de trabajos (QUEUE_BACKEND).
  El estado "en tiempo real" NUNCA se persiste en PostgreSQL fila a fila; a la
  BD solo llega el consolidado diario (ops_upsert_disponibilidad).

* **Ciclo de vida de un run** — `crear_run()` + `ejecutar_run()`. Todo lo que
  la consola muestra en §6.3/§6.4 sale de `ops_job_runs`; el decorador
  `@con_run` envuelve funciones de negocio existentes para que sus corridas
  queden registradas sin tocarlas.

* **Detector de incidentes** — `evaluar_incidentes()` aplica las reglas de
  §8.1 sobre la historia reciente de latidos (que el store también guarda,
  acotada, para contar rachas).

La auditoría escribe en `actividad` con modulo='ops' y company_id NULL
(cross-tenant); ver el ALTER de 07_consola_interna.sql.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..config_ops import COMPONENTES, ops_settings
from ..models import Actividad
from ..models_ops import OpsIncidente, OpsJob, OpsJobRun

log = logging.getLogger("acredittia.ops")

# clave de job -> callable(db, **params) -> (items_procesados, mensaje).
# Lo puebla worker/ops_tasks.py al importarse (mismo patrón que TAREAS).
OPS_RUNNERS: dict[str, callable] = {}


def runner(clave: str):
    """Registra la función que ejecuta un job de ops_jobs."""
    def wrap(fn):
        OPS_RUNNERS[clave] = fn
        return fn
    return wrap


# ============================================================================
# Auditoría (modulo='ops', sin tenant)
# ============================================================================
def auditar(db: Session, tipo: str, descripcion: str,
            user_id: uuid.UUID | None = None,
            entidad_tipo: str | None = None,
            entidad_id: uuid.UUID | None = None) -> None:
    db.add(Actividad(company_id=None, user_id=user_id, tipo=tipo, modulo="ops",
                     descripcion=descripcion, entidad_tipo=entidad_tipo,
                     entidad_id=entidad_id))


# ============================================================================
# Heartbeats
# ============================================================================
class _MemStore:
    """Desarrollo y tests. Mismo contrato que Redis, sin TTL real: la
    antigüedad se evalúa con el timestamp guardado en el propio latido."""

    def __init__(self):
        self._kv: dict[str, str] = {}
        self._hist: dict[str, list[str]] = {}

    def set(self, k, v, ex=None):
        self._kv[k] = v

    def get(self, k):
        return self._kv.get(k)

    def lpush(self, k, v):
        self._hist.setdefault(k, []).insert(0, v)

    def ltrim(self, k, a, b):
        self._hist[k] = self._hist.get(k, [])[a:b + 1]

    def lrange(self, k, a, b):
        return self._hist.get(k, [])[a:b + 1]


_store = None


def get_store():
    """Redis con QUEUE_BACKEND=celery; diccionario en proceso en el resto."""
    global _store
    if _store is None:
        if settings.queue_backend == "celery":
            import redis
            _store = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        else:
            _store = _MemStore()
    return _store


def reset_store() -> None:
    global _store
    _store = None


def _k(componente: str) -> str:
    return f"ops:hb:{componente}"


def guardar_latido(componente: str, estado: str, metricas: dict) -> None:
    """Último latido + historia acotada (60 latidos ≈ 1 hora) para las rachas
    del detector y el parcial del día en curso."""
    doc = json.dumps({"estado": estado, "metricas": metricas,
                      "ts": datetime.now(timezone.utc).isoformat()})
    s = get_store()
    s.set(_k(componente), doc, ex=ops_settings.ops_heartbeat_ttl_s)
    s.lpush(_k(componente) + ":hist", doc)
    s.ltrim(_k(componente) + ":hist", 0, 1439)      # 24 h a un latido/minuto


def leer_latido(componente: str) -> dict | None:
    """Último latido, o None si no hay o está vencido (monitor ciego)."""
    raw = get_store().get(_k(componente))
    if not raw:
        return None
    doc = json.loads(raw)
    edad = datetime.now(timezone.utc) - datetime.fromisoformat(doc["ts"])
    if edad.total_seconds() > ops_settings.ops_heartbeat_ttl_s:
        return None
    return doc


def historia_latidos(componente: str, n: int = 60) -> list[dict]:
    return [json.loads(x) for x in get_store().lrange(_k(componente) + ":hist", 0, n - 1)]


PESO_ESTADO = {"operativo": 0, "degradado": 1, "caido": 2}


def estado_componente(componente: str) -> tuple[str, dict | None, datetime | None]:
    """(estado, metricas, desde). Sin latido fresco => 'caido' con métricas
    None: si el monitor está ciego se reporta el peor caso, no el último feliz."""
    doc = leer_latido(componente)
    if doc is None:
        return "caido", None, None
    # 'desde': primer latido de la racha actual con el mismo estado.
    desde = datetime.fromisoformat(doc["ts"])
    for h in historia_latidos(componente, 1440):
        if h["estado"] != doc["estado"]:
            break
        desde = datetime.fromisoformat(h["ts"])
    return doc["estado"], doc["metricas"], desde


def parcial_del_dia(componente: str) -> dict:
    """Agrega los latidos de HOY (zona del servidor) para el punto parcial de
    la serie de disponibilidad."""
    hoy = datetime.now(timezone.utc).date()
    latidos = [h for h in historia_latidos(componente, 1440)
               if datetime.fromisoformat(h["ts"]).date() == hoy]
    if not latidos:
        return {"estado": "caido", "uptime_pct": 0.0, "checks_total": 0,
                "checks_fallidos": 0, "latencia_p95_ms": None}
    total = len(latidos)
    caidos = sum(1 for x in latidos if x["estado"] == "caido")
    peor = max(latidos, key=lambda x: PESO_ESTADO[x["estado"]])["estado"]
    p95s = sorted(x["metricas"].get("latencia_p95_ms")
                  for x in latidos if x["metricas"].get("latencia_p95_ms") is not None)
    return {
        "estado": peor,
        "uptime_pct": round(100.0 * (total - caidos) / total, 2),
        "checks_total": total,
        "checks_fallidos": caidos,
        "latencia_p95_ms": p95s[int(len(p95s) * 0.95) - 1] if p95s else None,
    }


# ============================================================================
# Detector de incidentes (§8.1)
# ============================================================================
def evaluar_incidentes(db: Session, componente: str) -> None:
    """Se llama tras guardar cada latido. Reglas:

    * `ops_fallos_para_caida` latidos 'caido' seguidos  -> incidente caido.
    * un latido 'degradado' (umbral ya aplicado en la sonda) -> incidente degradado.
    * `ops_sanos_para_monitoreo` sanos seguidos -> auto pasa a monitoreando.
    * `ops_min_para_resolver` minutos sanos     -> auto se resuelve solo.
    * anti-rebote: no se abre uno nuevo hasta `ops_min_antirebote` minutos
      después del último resuelto del componente.

    Los incidentes manuales nunca se cierran solos.
    """
    hist = historia_latidos(componente, max(ops_settings.ops_fallos_para_caida,
                                            ops_settings.ops_sanos_para_monitoreo,
                                            ops_settings.ops_min_para_resolver))
    if not hist:
        return
    ahora = datetime.now(timezone.utc)

    abierto = db.scalars(select(OpsIncidente).where(
        OpsIncidente.componente == componente,
        OpsIncidente.estado != "resuelto")).first()

    n_caidos = 0
    for h in hist:
        if h["estado"] == "caido":
            n_caidos += 1
        else:
            break
    n_sanos = 0
    for h in hist:
        if h["estado"] == "operativo":
            n_sanos += 1
        else:
            break
    minutos_sanos = 0.0
    if n_sanos and len(hist) > 0:
        primero_sano = datetime.fromisoformat(hist[min(n_sanos, len(hist)) - 1]["ts"])
        minutos_sanos = (ahora - primero_sano).total_seconds() / 60

    if abierto is None:
        ultimo_resuelto = db.scalars(
            select(OpsIncidente).where(
                OpsIncidente.componente == componente,
                OpsIncidente.estado == "resuelto")
            .order_by(OpsIncidente.resuelto_at.desc())).first()
        if (ultimo_resuelto and ultimo_resuelto.resuelto_at and
                (ahora - ultimo_resuelto.resuelto_at).total_seconds()
                < ops_settings.ops_min_antirebote * 60):
            return                                            # anti-rebote
        actual = hist[0]["estado"]
        if n_caidos >= ops_settings.ops_fallos_para_caida:
            db.add(OpsIncidente(
                componente=componente, severidad="caido", origen="auto",
                titulo=f"{componente}: sin respuesta "
                       f"({n_caidos} sondeos consecutivos fallidos)"))
        elif actual == "degradado":
            detalle = hist[0]["metricas"].get("detalle", "umbral superado")
            db.add(OpsIncidente(
                componente=componente, severidad="degradado", origen="auto",
                titulo=f"{componente}: degradado — {detalle}"[:140]))
        return

    if abierto.origen != "auto":
        return
    if abierto.estado == "abierto" and n_sanos >= ops_settings.ops_sanos_para_monitoreo:
        abierto.estado = "monitoreando"
    if (abierto.estado == "monitoreando"
            and minutos_sanos >= ops_settings.ops_min_para_resolver):
        abierto.estado = "resuelto"
        abierto.resuelto_at = ahora
        abierto.resolucion = "Recuperación automática verificada"


# ============================================================================
# Ciclo de vida de runs
# ============================================================================
def crear_run(db: Session, job: OpsJob, disparo: str = "cron",
              actor_user_id: uuid.UUID | None = None,
              params: dict | None = None) -> OpsJobRun:
    run = OpsJobRun(job_clave=job.clave, status="queued", disparo=disparo,
                    actor_user_id=actor_user_id, params=params or {})
    db.add(run)
    db.flush()          # asigna el id sin cerrar la transacción
    return run


def ejecutar_run(run_id: int) -> None:
    """Ejecuta el runner del job de un run 'queued' y cierra el run.

    Corre en el worker (o en proceso con inproc). Abre su propia sesión
    is_admin: las tablas ops_* exigen contexto admin y no hay request.
    """
    from ..database import worker_session

    with worker_session(is_admin=True) as db:
        run = db.get(OpsJobRun, run_id)
        if run is None or run.status != "queued":
            return                       # idempotencia: reintento de Celery
        fn = OPS_RUNNERS.get(run.job_clave)
        if fn is None:
            run.status = "error"
            run.finished_at = datetime.now(timezone.utc)
            run.error_detalle = f"Sin runner registrado para '{run.job_clave}'"
            db.commit()
            return
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        db.commit()

        try:
            items, mensaje = fn(db, **(run.params or {}))
            run.status = "ok"
            run.items_procesados = items
            run.mensaje = mensaje
        except Exception:
            db.rollback()
            run = db.get(OpsJobRun, run_id)     # la sesión pudo invalidarse
            run.status = "error"
            run.error_detalle = traceback.format_exc()[:8192]
        run.finished_at = datetime.now(timezone.utc)
        db.commit()


def disparar_por_cron(clave: str) -> int | None:
    """Entrada de celery beat: respeta la pausa y evita corridas solapadas.
    Devuelve el run_id creado o None si no correspondía ejecutar."""
    from ..database import worker_session

    with worker_session(is_admin=True) as db:
        job = db.get(OpsJob, clave)
        if job is None or not job.activo:
            return None
        en_curso = db.scalars(select(OpsJobRun).where(
            OpsJobRun.job_clave == clave,
            OpsJobRun.status.in_(("queued", "running")))).first()
        if en_curso:
            log.warning("Job %s aún en ejecución (run %s); se omite el tick",
                        clave, en_curso.id)
            return None
        run = crear_run(db, job, disparo="cron")
        db.commit()
        rid = run.id
    ejecutar_run(rid)
    return rid


def con_run(clave: str):
    """Decorador para funciones de negocio EXISTENTES (§8.2): registra la
    corrida en ops_job_runs sin cambiar la firma ni el resultado.

    La función decorada debe devolver algo convertible a str; si devuelve un
    int se interpreta como items_procesados.
    """
    def wrap(fn):
        def dentro(*args, **kwargs):
            from ..database import worker_session

            t0 = time.monotonic()
            with worker_session(is_admin=True) as db:
                job = db.get(OpsJob, clave)
                run = crear_run(db, job, disparo="cron") if job else None
                if run:
                    run.status = "running"
                    run.started_at = datetime.now(timezone.utc)
                    db.commit()
                    rid = run.id
            try:
                resultado = fn(*args, **kwargs)
            except Exception:
                if run:
                    with worker_session(is_admin=True) as db:
                        r = db.get(OpsJobRun, rid)
                        r.status = "error"
                        r.finished_at = datetime.now(timezone.utc)
                        r.error_detalle = traceback.format_exc()[:8192]
                        db.commit()
                raise
            if run:
                with worker_session(is_admin=True) as db:
                    r = db.get(OpsJobRun, rid)
                    r.status = "ok"
                    r.finished_at = datetime.now(timezone.utc)
                    if isinstance(resultado, int):
                        r.items_procesados = resultado
                        r.mensaje = f"{resultado} elementos procesados"
                    else:
                        r.mensaje = str(resultado)[:500]
                    db.commit()
            log.info("Job %s: ok en %.1fs", clave, time.monotonic() - t0)
            return resultado
        dentro.__name__ = getattr(fn, "__name__", clave)
        return dentro
    return wrap


# ============================================================================
# Utilidades varias
# ============================================================================
def proxima_ejecucion(job: OpsJob) -> datetime | None:
    """Próximo tick del cron en America/Santiago; None si pausado/continuo o
    si croniter no está instalado (dependencia opcional, ver README)."""
    if not job.activo or not job.cron_expr:
        return None
    try:
        from zoneinfo import ZoneInfo

        from croniter import croniter
        tz = ZoneInfo("America/Santiago")
        return croniter(job.cron_expr, datetime.now(tz)).get_next(datetime)
    except ImportError:
        return None
    except Exception:
        log.warning("cron_expr inválida en %s: %s", job.clave, job.cron_expr)
        return None


def tasa_exito_7d(db: Session, clave: str) -> float | None:
    corte = datetime.now(timezone.utc) - timedelta(days=7)
    filas = db.execute(select(OpsJobRun.status).where(
        OpsJobRun.job_clave == clave,
        OpsJobRun.created_at >= corte,
        OpsJobRun.status.in_(("ok", "error", "timeout")))).all()
    if not filas:
        return None
    ok = sum(1 for (s,) in filas if s == "ok")
    return round(100.0 * ok / len(filas), 1)
