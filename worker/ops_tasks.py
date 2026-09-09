"""Tareas de fondo de la consola interna (§8) y registro de runners.

Importar este módulo (lo hacen `main.py` y `worker/celery_app.py`):

1. registra en `TAREAS` la entrada 'ops_ejecutar_run' que usa el endpoint
   POST /admin/ops/procesos/{clave}/ejecutar, con lo que funciona igual con
   Celery y con QUEUE_BACKEND=inproc;
2. puebla `OPS_RUNNERS` con la función de cada clave de ops_jobs, de modo que
   una ejecución manual y una por cron corren exactamente el mismo código.

Para Celery, `registrar_ops(celery)` añade las tareas y su beat schedule; en
`worker/celery_app.py` basta con:

    from worker.ops_tasks import registrar_ops
    registrar_ops(celery)
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app.config_ops import COMPONENTES, ops_settings
from app.database import worker_session
from app.services import ops
from app.services.jobs import tarea
from app.services.ops_probes import SONDAS

log = logging.getLogger("acredittia.ops.tasks")


# ============================================================================
# Entrada de la cola para ejecuciones manuales (§6.5)
# ============================================================================
@tarea("ops_ejecutar_run")
def ops_ejecutar_run(run_id: int) -> None:
    ops.ejecutar_run(int(run_id))


# ============================================================================
# Runners de los jobs de ops_jobs (§8). Cada uno recibe la sesión is_admin
# del ciclo de vida del run y devuelve (items_procesados, mensaje).
# ============================================================================
@ops.runner("ops_heartbeat")
def run_heartbeat(db, **_):
    """Sondea los componentes, guarda los latidos y evalúa incidentes."""
    for componente in COMPONENTES:
        estado, metricas = SONDAS[componente](db)
        ops.guardar_latido(componente, estado, metricas)
        ops.evaluar_incidentes(db, componente)
    db.commit()
    return len(COMPONENTES), f"{len(COMPONENTES)} componentes sondeados"


@ops.runner("ops_disponibilidad")
def run_disponibilidad(db, fecha: str | None = None, **_):
    """Consolida los latidos del día ANTERIOR en ops_disponibilidad_diaria.

    Con params {"fecha": "YYYY-MM-DD"} consolida ese día (re-cálculo manual).
    Los latidos viven 24 h en la historia del store, así que el job debe
    correr a primera hora (beat: 00:05).
    """
    objetivo = (date.fromisoformat(fecha) if fecha
                else date.today() - timedelta(days=1))
    n = 0
    for componente in COMPONENTES:
        latidos = [h for h in ops.historia_latidos(componente, 1440)
                   if datetime.fromisoformat(h["ts"]).date() == objetivo]
        if not latidos:
            continue        # sin telemetría de ese día: no se inventa una fila
        total = len(latidos)
        caidos = sum(1 for x in latidos if x["estado"] == "caido")
        peor = max(latidos, key=lambda x: ops.PESO_ESTADO[x["estado"]])["estado"]
        p95s = sorted(x["metricas"].get("latencia_p95_ms") for x in latidos
                      if x["metricas"].get("latencia_p95_ms") is not None)
        db.execute(text(
            "SELECT ops_upsert_disponibilidad(:c, :f, :e, :u, :p, :t, :x)"), {
            "c": componente, "f": objetivo, "e": peor,
            "u": round(100.0 * (total - caidos) / total, 2),
            "p": p95s[int(len(p95s) * 0.95) - 1] if p95s else None,
            "t": total, "x": caidos,
        })
        n += 1
    db.commit()
    return n, f"Disponibilidad de {objetivo.isoformat()} consolidada ({n} componentes)"


@ops.runner("metricas_snapshot")
def run_metricas_snapshot(db, mes: str | None = None, forzar: bool = False, **_):
    """Snapshot del mes en curso; el día 1 consolida además el mes anterior.

    Con params {"mes": "YYYY-MM"} recalcula ese mes (forzar=true si estaba
    cerrado; queda auditado porque la ejecución manual registra actor).
    """
    uf = ops_settings.ops_valor_uf
    if mes:
        objetivo = date(int(mes[:4]), int(mes[5:7]), 1)
        db.execute(text(
            "SELECT ops_snapshot_metricas(:m, :uf, true, :fz)"),
            {"m": objetivo, "uf": uf, "fz": forzar})
        db.commit()
        return 1, f"Snapshot de {mes} recalculado"

    hoy = date.today()
    n = 0
    if hoy.day == 1:
        anterior = (hoy - timedelta(days=1)).replace(day=1)
        db.execute(text("SELECT ops_snapshot_metricas(:m, :uf, true, false)"),
                   {"m": anterior, "uf": uf})
        n += 1
    db.execute(text("SELECT ops_snapshot_metricas(:m, :uf, false, false)"),
               {"m": hoy.replace(day=1), "uf": uf})
    db.commit()
    return n + 1, f"Snapshot del mes en curso actualizado (UF {uf})"


@ops.runner("ops_retencion")
def run_retencion(db, **_):
    fila = db.execute(text("SELECT * FROM ops_podar_telemetria()")).one()
    db.commit()
    return int(fila.runs_podados + fila.incidentes_podados), (
        f"{fila.runs_podados} runs y {fila.incidentes_podados} incidentes "
        f"podados; {fila.runs_timeout} runs marcados timeout")


# --- Jobs de negocio existentes, expuestos en la consola --------------------
@ops.runner("vencimientos")
def run_vencimientos(db, **_):
    """Mismo trabajo que acredittia.cron_diario, expuesto y reintetable."""
    from app.services.vencimientos import (escribir_snapshots,
                                           expirar_credenciales,
                                           recalcular_documentos)
    docs = recalcular_documentos(db)
    snaps = escribir_snapshots(db)
    creds = expirar_credenciales(db)
    db.commit()
    return docs, (f"{docs} documentos recalculados, {snaps} snapshots, "
                  f"{creds} credenciales expiradas")


@ops.runner("reportes_programados")
def run_reportes_programados(db, **_):
    from app.services.programados import disparar_pendientes
    n = disparar_pendientes(db)
    db.commit()
    return n, f"{n} reportes programados disparados"


@ops.runner("purga_temporales")
def run_purga_temporales(db, **_):
    from app.services.tasks import purgar_temporales
    n = purgar_temporales()
    return n, f"{n} blobs temporales purgados"


# ============================================================================
# Celery: tareas + beat (§8). Los ticks pasan por disparar_por_cron(), que
# respeta la pausa (ops_jobs.activo) y evita corridas solapadas.
# ============================================================================
def registrar_ops(celery) -> None:
    from celery.schedules import crontab

    @celery.task(name="acredittia.ops_tick")
    def ops_tick(clave: str):
        rid = ops.disparar_por_cron(clave)
        return {"job": clave, "run_id": rid}

    celery.conf.beat_schedule.update({
        "ops-heartbeat": {
            "task": "acredittia.ops_tick",
            "schedule": 60.0,                       # cada minuto
            "args": ("ops_heartbeat",),
        },
        "ops-disponibilidad": {
            "task": "acredittia.ops_tick",
            "schedule": crontab(hour=0, minute=5),
            "args": ("ops_disponibilidad",),
        },
        "ops-metricas-snapshot": {
            "task": "acredittia.ops_tick",
            "schedule": crontab(hour=2, minute=10),
            "args": ("metricas_snapshot",),
        },
        "ops-retencion": {
            "task": "acredittia.ops_tick",
            "schedule": crontab(hour=3, minute=30, day_of_week=0),
            "args": ("ops_retencion",),
        },
    })
    log.info("Tareas de la consola interna registradas en Celery")
