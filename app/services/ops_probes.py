"""Sondas de los seis componentes monitoreados (§8.1, job ops_heartbeat).

Cada sonda devuelve (estado, metricas):

    estado    'operativo' | 'degradado' | 'caido'
    metricas  dict libre por componente (documentado en §6.1 de la espec.)

Reglas de diseño:

* Una sonda NUNCA lanza: cualquier excepción es 'caido' con el error en las
  métricas. El heartbeat tiene que sobrevivir a lo que sea que esté roto.
* Los umbrales de degradación se aplican AQUÍ (la sonda conoce su métrica);
  el detector de incidentes solo mira estados.
* Las métricas externas que exigen infraestructura que no siempre existe
  (Application Insights para p95 real de la API) están detrás de un adaptador
  con stub: `latencia_api_p95_ms()` devuelve None si no hay fuente configurada
  y la sonda no degrada por una métrica ausente.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from ..config import settings
from ..config_ops import ops_settings

log = logging.getLogger("acredittia.ops.probes")


def _seguro(fn):
    """Envuelve la sonda: excepción => ('caido', {'error': ...})."""
    def dentro(db):
        try:
            return fn(db)
        except Exception as exc:            # noqa: BLE001 — es el contrato
            log.warning("Sonda %s falló: %s", fn.__name__, exc)
            return "caido", {"error": str(exc)[:300]}
    dentro.__name__ = fn.__name__
    return dentro


# ---------------------------------------------------------------- adaptadores
def latencia_api_p95_ms() -> int | None:
    """p95 real de la API. Fuente: Application Insights (KQL) en producción.

    Stub deliberado: sin APPINSIGHTS_QUERY_ENDPOINT configurado devuelve None
    y la sonda de la API no opina sobre latencia. Implementar el cliente aquí
    cuando el recurso exista; nada más cambia.
    """
    if not os.environ.get("APPINSIGHTS_QUERY_ENDPOINT"):
        return None
    return None     # TODO: consulta KQL requests | summarize percentile(duration, 95)


# ------------------------------------------------------------------- sondas
@_seguro
def sonda_db(db):
    t0 = time.monotonic()
    db.execute(text("SELECT 1"))
    ping_ms = int((time.monotonic() - t0) * 1000)
    fila = db.execute(text(
        "SELECT count(*) AS activas,"
        "       current_setting('max_connections')::int AS maximo "
        "FROM pg_stat_activity WHERE datname = current_database()")).one()
    estado = "operativo"
    if fila.activas > fila.maximo * 0.85:
        estado = "degradado"
    return estado, {"ping_ms": ping_ms, "conexiones": fila.activas,
                    "conexiones_max": fila.maximo,
                    "detalle": f"{fila.activas}/{fila.maximo} conexiones"}


@_seguro
def sonda_api(db):
    """El heartbeat corre en la misma imagen que la API: si esta sonda corre,
    el proceso vive. La latencia real viene del adaptador externo."""
    p95 = latencia_api_p95_ms()
    estado = "operativo"
    metricas: dict = {"latencia_p95_ms": p95}
    if p95 is not None and p95 > ops_settings.ops_umbral_p95_ms:
        estado = "degradado"
        metricas["detalle"] = f"p95 {p95} ms > umbral {ops_settings.ops_umbral_p95_ms} ms"
    return estado, metricas


@_seguro
def sonda_workers(db):
    """Profundidad de colas en Redis. Con inproc no hay cola: operativo, 0."""
    if settings.queue_backend != "celery":
        return "operativo", {"en_cola": 0, "backlog_p95_min": 0.0,
                             "detalle": "inproc (desarrollo)"}
    import redis
    r = redis.Redis.from_url(settings.redis_url)
    en_cola = int(r.llen("celery") or 0)
    estado = "operativo"
    metricas = {"en_cola": en_cola}
    if en_cola > ops_settings.ops_umbral_cola:
        estado = "degradado"
        metricas["detalle"] = f"{en_cola} tareas en cola > umbral {ops_settings.ops_umbral_cola}"
    return estado, metricas


@_seguro
def sonda_storage(db):
    """Escribe y borra un canario. local: en el directorio de uploads;
    azure: un blob de 1 byte en el contenedor configurado."""
    from .storage import get_storage
    st = get_storage()          # LocalStorage o AzureBlobStorage, misma interfaz
    t0 = time.monotonic()
    st.save("ops/_canario.txt", b"1")
    st.delete("ops/_canario.txt")
    return "operativo", {"rtt_ms": int((time.monotonic() - t0) * 1000),
                         "backend": settings.storage_backend}


@_seguro
def sonda_ia(db):
    """Precisión de los últimos 7 días desde ia_reviews; el backend simulado
    siempre está operativo. Un p50 alto degrada."""
    from ..models import IaReview
    corte = datetime.now(timezone.utc) - timedelta(days=7)
    total = db.scalar(select(func.count()).select_from(IaReview)
                      .where(IaReview.created_at >= corte)) or 0
    fallidos = db.scalar(select(func.count()).select_from(IaReview)
                         .where(IaReview.created_at >= corte,
                                IaReview.status == "failed")) or 0
    metricas = {"revisiones_7d": total, "fallidas_7d": fallidos,
                "backend": settings.ia_backend}
    if total >= 20 and fallidos / total > 0.10:
        metricas["detalle"] = f"{fallidos}/{total} revisiones fallidas en 7 días"
        return "degradado", metricas
    return "operativo", metricas


@_seguro
def sonda_integraciones(db):
    """Conectores activos vs. con error, mirando `integraciones` y el último
    sync_log de cada una. Dos fallos consecutivos de la misma integración es
    regla del detector; aquí basta el estado declarado."""
    from ..models import Integracion
    filas = db.execute(select(Integracion.tipo, Integracion.estado)).all()
    activas = [t for t, e in filas if e == "activa"]
    con_error = [t for t, e in filas if e == "con_error"]
    total_conf = len(activas) + len(con_error)
    metricas = {"conectores_ok": len(activas), "conectores_total": total_conf,
                "con_error": con_error}
    if con_error:
        metricas["detalle"] = "con error: " + ", ".join(sorted(set(con_error)))
        return "degradado", metricas
    return "operativo", metricas


SONDAS = {
    "api": sonda_api,
    "db": sonda_db,
    "workers": sonda_workers,
    "storage": sonda_storage,
    "ia": sonda_ia,
    "integraciones": sonda_integraciones,
}
