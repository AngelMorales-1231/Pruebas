"""Consola Interna de Administración (/admin/ops) — solo rol admin.

Implementa los 20 endpoints de la Especificación de API — Consola Interna v1.0:
§5 usuarios y clientes, §6 sistemas/procesos/incidentes, §7 indicadores.

Decisiones heredadas del resto del backend:

* El router entero exige `require_admin`; no se usa X-Company-Id porque el
  alcance es cross-tenant (el contexto RLS queda con is_admin=true y las
  tablas ops_* solo son visibles así, ver 07 §9).
* Los agregados pesados viven en PL/pgSQL (08_funciones_consola.sql); aquí
  solo se orquesta, filtra y serializa.
* Escrituras auditadas en `actividad` con modulo='ops' y company_id NULL.
* GET agregados con Cache-Control: private, max-age=60. El estado en tiempo
  real (§6.1) va siempre fresco (max-age=0).
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, joinedload

from ..config_ops import ops_settings
from ..database import get_db
from ..deps import (Page, err, get_current_user, paginacion, require_admin,
                    sobre)
from ..models import Company, Plan, Suscripcion, User
from ..models_ops import (MetricasMensuales, MetricasMensualesPlan,
                          OpsComponente, OpsDisponibilidadDiaria, OpsIncidente,
                          OpsJob, OpsJobRun)
from ..services import ops
from ..services.jobs import enqueue

router = APIRouter(prefix="/admin/ops", tags=["admin-ops"],
                   dependencies=[Depends(require_admin)])

INDUSTRIAS = ("mineria", "construccion", "energia", "industrial", "otras")
ESTADOS_CUENTA = ("al_dia", "en_riesgo", "moroso")
_MES_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ============================================================================
# Helpers
# ============================================================================
def _cache(resp: Response, segundos: int = 60) -> None:
    resp.headers["Cache-Control"] = f"private, max-age={segundos}"


def _mes_param(mes: str | None) -> date:
    """'YYYY-MM' -> date del día 1. Default: mes actual. Futuro -> 400."""
    if mes is None:
        hoy = date.today()
        return hoy.replace(day=1)
    if not _MES_RE.match(mes):
        raise err(400, "RANGO_INVALIDO", "El mes debe tener formato YYYY-MM")
    d = date(int(mes[:4]), int(mes[5:7]), 1)
    if d > date.today():
        raise err(400, "RANGO_INVALIDO", "No hay métricas de meses futuros")
    return d


def _es_mes_actual(d: date) -> bool:
    return d == date.today().replace(day=1)


def _meses_atras(n: int) -> list[date]:
    """Los últimos n meses (día 1), ascendente, incluido el actual."""
    actual = date.today().replace(day=1)
    salida = []
    for i in range(n - 1, -1, -1):
        y, m = actual.year, actual.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        salida.append(date(y, m, 1))
    return salida


def _num(x) -> float | None:
    return float(x) if x is not None else None


def _validar_rango(desde: date | None, hasta: date | None) -> None:
    if desde and hasta and desde > hasta:
        raise err(400, "RANGO_INVALIDO", "'desde' no puede ser posterior a 'hasta'")


def _snapshot(db: Session, mes: date) -> MetricasMensuales | None:
    return db.get(MetricasMensuales, mes)


def _metricas_de(db: Session, mes: date) -> tuple[MetricasMensuales | None, bool]:
    """(fila, parcial). Mes en curso: cálculo en vivo vía ops_calc_metricas;
    histórico: snapshot (None si no existe)."""
    if _es_mes_actual(mes):
        fila = db.execute(text("SELECT * FROM ops_calc_metricas(:m, :uf)"),
                          {"m": mes, "uf": ops_settings.ops_valor_uf}).one()
        return fila, True
    return _snapshot(db, mes), False


def _auditar_confidencial(db: Session, user: User) -> None:
    """Deja constancia del acceso a cifras financieras (una vez por día y
    usuario, para no inundar el feed)."""
    from ..models import Actividad
    hoy = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
    ya = db.scalar(select(func.count()).select_from(Actividad).where(
        Actividad.modulo == "ops", Actividad.user_id == user.id,
        Actividad.tipo == "visualizacion", Actividad.created_at >= hoy))
    if not ya:
        ops.auditar(db, "visualizacion", "Consulta de indicadores confidenciales",
                    user_id=user.id)
        db.commit()


# ============================================================================
# §5 — Seguimiento de usuarios
# ============================================================================
@router.get("/usuarios/resumen")
def usuarios_resumen(resp: Response,
                     ventana_dias: int = Query(30, ge=7, le=90),
                     db: Session = Depends(get_db)):
    filas = db.execute(text(
        "SELECT * FROM ops_usuarios_por_empresa(:v)"), {"v": ventana_dias}).all()
    totales = sum(f.usuarios for f in filas)
    activos = sum(f.activos for f in filas)
    sin_90 = sum(f.sin_acceso_90d for f in filas)

    mes_actual = date.today().replace(day=1)
    prev = _snapshot(db, (mes_actual - timedelta(days=1)).replace(day=1))

    variacion = {"abs": None, "pct": None}
    delta_pts = None
    if prev:
        variacion = {
            "abs": totales - prev.usuarios_totales,
            "pct": round(100.0 * (totales - prev.usuarios_totales)
                         / prev.usuarios_totales, 1) if prev.usuarios_totales else None,
        }
        if prev.usuarios_totales:
            delta_pts = round(100.0 * activos / totales
                              - 100.0 * prev.usuarios_activos / prev.usuarios_totales, 1) \
                if totales else None

    # Altas/bajas del mes y clientes activos, en vivo.
    ini_mes = mes_actual
    altas = db.scalar(select(func.count()).select_from(Suscripcion)
                      .where(Suscripcion.created_at >= ini_mes)) or 0
    bajas = db.scalar(select(func.count()).select_from(Suscripcion)
                      .where(Suscripcion.estado == "cancelada",
                             Suscripcion.updated_at >= ini_mes)) or 0
    clientes = db.scalar(
        select(func.count()).select_from(Company)
        .join(Suscripcion, Suscripcion.company_id == Company.id)
        .where(Company.status == "approved",
               Suscripcion.estado.in_(("activa", "trial")))) or 0

    serie = [{"mes": s.mes.strftime("%Y-%m"), "usuarios_totales": s.usuarios_totales}
             for s in db.scalars(
                 select(MetricasMensuales)
                 .where(MetricasMensuales.mes >= _meses_atras(12)[0],
                        MetricasMensuales.mes < mes_actual)
                 .order_by(MetricasMensuales.mes))]
    serie.append({"mes": mes_actual.strftime("%Y-%m"), "usuarios_totales": totales})

    _cache(resp)
    return {
        "generado_at": datetime.now(timezone.utc).isoformat(),
        "ventana_dias": ventana_dias,
        "usuarios": {
            "totales": totales,
            "variacion_mes": variacion,
            "activos": {"n": activos,
                        "pct": round(100.0 * activos / totales, 1) if totales else None,
                        "delta_pts_vs_mes_anterior": delta_pts},
            "inactivos": {"n": totales - activos,
                          "pct": round(100.0 * (totales - activos) / totales, 1)
                                 if totales else None,
                          "sin_acceso_90d": sin_90},
        },
        "clientes": {"activos": clientes, "altas_mes": altas, "bajas_mes": bajas},
        "serie_12m": serie,
    }


@router.get("/usuarios/por-plan")
def usuarios_por_plan(resp: Response,
                      ventana_dias: int = Query(30, ge=7, le=90),
                      db: Session = Depends(get_db)):
    filas = db.execute(text("""
        SELECT p.id AS plan_id, p.nombre AS plan,
               count(DISTINCT s.company_id)          AS clientes,
               COALESCE(sum(f.usuarios), 0)::int     AS usuarios,
               COALESCE(sum(f.activos), 0)::int      AS activos
          FROM planes p
          JOIN suscripciones s ON s.plan_id = p.id AND s.estado IN ('activa','trial')
          LEFT JOIN ops_usuarios_por_empresa(:v) f ON f.company_id = s.company_id
         GROUP BY p.id, p.nombre, p.precio
         ORDER BY p.precio
    """), {"v": ventana_dias}).all()

    sin_plan = db.execute(text("""
        SELECT count(DISTINCT c.id)              AS clientes,
               COALESCE(sum(f.usuarios), 0)::int AS usuarios
          FROM companies c
          LEFT JOIN suscripciones s ON s.company_id = c.id
                                   AND s.estado IN ('activa','trial')
          LEFT JOIN ops_usuarios_por_empresa(:v) f ON f.company_id = c.id
         WHERE c.status = 'approved' AND s.id IS NULL
    """), {"v": ventana_dias}).one()

    _cache(resp)
    return {
        "ventana_dias": ventana_dias,
        "items": [{
            "plan_id": str(f.plan_id), "plan": f.plan, "clientes": f.clientes,
            "usuarios": f.usuarios, "activos": f.activos,
            "inactivos": f.usuarios - f.activos,
            "pct_activos": round(100.0 * f.activos / f.usuarios, 1)
                           if f.usuarios else None,
        } for f in filas],
        "sin_plan": {"clientes": sin_plan.clientes, "usuarios": sin_plan.usuarios},
    }


@router.get("/usuarios/por-industria")
def usuarios_por_industria(resp: Response, db: Session = Depends(get_db)):
    filas = dict(db.execute(text("""
        SELECT c.industria::text, count(u.id)
          FROM companies c
          LEFT JOIN users u ON u.company_id = c.id AND u.role <> 'admin'
                           AND u.status = 'approved' AND u.activo
         WHERE c.status = 'approved'
         GROUP BY c.industria
    """)).all())
    total = sum(filas.values())
    items = sorted(
        ({"industria": ind, "usuarios": filas.get(ind, 0),
          "pct": round(100.0 * filas.get(ind, 0) / total, 1) if total else 0.0}
         for ind in INDUSTRIAS),
        key=lambda x: -x["usuarios"])
    # Ajuste para que los pct sumen 100,0 exacto (se corrige el mayor).
    if total and items:
        diff = round(100.0 - sum(x["pct"] for x in items), 1)
        items[0]["pct"] = round(items[0]["pct"] + diff, 1)
    _cache(resp)
    return {"total": total, "items": items}


@router.get("/usuarios/actividad-mensual")
def actividad_mensual(resp: Response, meses: int = Query(12, ge=1, le=36),
                      ventana_dias: int = Query(30, ge=7, le=90),
                      db: Session = Depends(get_db)):
    mes_actual = date.today().replace(day=1)
    historicos = {s.mes: s for s in db.scalars(
        select(MetricasMensuales)
        .where(MetricasMensuales.mes >= _meses_atras(meses)[0],
               MetricasMensuales.mes < mes_actual))}
    items = [{"mes": m.strftime("%Y-%m"),
              "usuarios_activos": historicos[m].usuarios_activos,
              "usuarios_totales": historicos[m].usuarios_totales,
              "parcial": False}
             for m in _meses_atras(meses) if m in historicos]

    vivo = db.execute(text(
        "SELECT COALESCE(sum(usuarios),0)::int AS u, COALESCE(sum(activos),0)::int AS a "
        "FROM ops_usuarios_por_empresa(:v)"), {"v": ventana_dias}).one()
    items.append({"mes": mes_actual.strftime("%Y-%m"), "usuarios_activos": vivo.a,
                  "usuarios_totales": vivo.u, "parcial": True})
    _cache(resp)
    return {"items": items}


_SORT_CLIENTES = {"nombre": "c.nombre", "usuarios": "usuarios",
                  "activos_30d": "activos", "pct_actividad": "pct_actividad",
                  "ultima_actividad_at": "ultima_actividad_at"}


@router.get("/clientes")
def clientes(resp: Response, p: Page = Depends(paginacion),
             plan_id: str | None = Query(None),
             industria: str | None = Query(None),
             estado_cuenta: str | None = Query(None),
             ventana_dias: int = Query(30, ge=7, le=90),
             incluir_no_aprobadas: bool = Query(False),
             db: Session = Depends(get_db)):
    if industria and industria not in INDUSTRIAS:
        raise err(400, "RANGO_INVALIDO",
                  f"industria debe ser una de: {', '.join(INDUSTRIAS)}")
    if estado_cuenta and estado_cuenta not in ESTADOS_CUENTA:
        raise err(400, "RANGO_INVALIDO",
                  f"estado_cuenta debe ser uno de: {', '.join(ESTADOS_CUENTA)}")

    filtros, params = [], {"v": ventana_dias}
    if not incluir_no_aprobadas:
        filtros.append("c.status = 'approved'")
    if p.search:
        filtros.append("(c.nombre ILIKE :q OR c.rut ILIKE :q)")
        params["q"] = f"%{p.search}%"
    if plan_id == "sin_plan":
        filtros.append("s.id IS NULL")
    elif plan_id:
        try:
            params["plan_id"] = str(uuid.UUID(plan_id))
        except ValueError:
            raise err(400, "RANGO_INVALIDO", "plan_id no es un UUID ni 'sin_plan'")
        filtros.append("s.plan_id = :plan_id")
    if industria:
        filtros.append("c.industria = :industria")
        params["industria"] = industria
    if estado_cuenta:
        filtros.append("ops_estado_cuenta(c.id) = :ec")
        params["ec"] = estado_cuenta

    where = ("WHERE " + " AND ".join(filtros)) if filtros else ""
    base = f"""
        FROM companies c
        LEFT JOIN suscripciones s ON s.company_id = c.id
        LEFT JOIN planes pl ON pl.id = s.plan_id
        LEFT JOIN ops_usuarios_por_empresa(:v) f ON f.company_id = c.id
        {where}
    """
    total = db.execute(text("SELECT count(*) " + base), params).scalar() or 0

    campo = (p.sort or "-usuarios").lstrip("-")
    desc = (p.sort or "-usuarios").startswith("-")
    orden = _SORT_CLIENTES.get(campo, "usuarios")
    filas = db.execute(text(f"""
        SELECT c.id, c.nombre, c.rut, c.industria::text, c.status,
               pl.id AS plan_id, pl.nombre AS plan, s.estado AS susc_estado,
               COALESCE(f.usuarios, 0)  AS usuarios,
               COALESCE(f.activos, 0)   AS activos,
               CASE WHEN COALESCE(f.usuarios, 0) > 0
                    THEN round(100.0 * f.activos / f.usuarios, 1) END AS pct_actividad,
               f.ultima_actividad_at,
               CASE WHEN c.status = 'approved'
                    THEN ops_estado_cuenta(c.id) END AS estado_cuenta
        {base}
        ORDER BY {orden} {'DESC NULLS LAST' if desc else 'ASC NULLS LAST'}, c.nombre
        LIMIT :lim OFFSET :off
    """), {**params, "lim": p.page_size, "off": p.offset}).all()

    items = [{
        "company_id": str(f.id), "nombre": f.nombre, "rut": f.rut,
        "industria": f.industria, "plan": f.plan,
        "plan_id": str(f.plan_id) if f.plan_id else None,
        "usuarios": f.usuarios, "activos_30d": f.activos,
        "pct_actividad": _num(f.pct_actividad),
        "estado_cuenta": f.estado_cuenta, "suscripcion_estado": f.susc_estado,
        "ultima_actividad_at": f.ultima_actividad_at.isoformat()
                               if f.ultima_actividad_at else None,
    } for f in filas]
    _cache(resp)
    return sobre(items, total, p)


@router.get("/clientes/{company_id}")
def cliente_detalle(company_id: uuid.UUID, resp: Response,
                    db: Session = Depends(get_db)):
    c = db.get(Company, company_id)
    if c is None:
        raise err(404, "NO_ENCONTRADO", "Cliente inexistente")

    susc = db.scalars(select(Suscripcion).options(joinedload(Suscripcion.plan))
                      .where(Suscripcion.company_id == c.id)).first()
    mrr = None
    if susc and susc.estado == "activa":
        mrr = db.execute(text(
            "SELECT ops_precio_a_mrr_clp(:p, :m, :per, :uf)"),
            {"p": susc.plan.precio, "m": susc.plan.moneda,
             "per": susc.plan.periodo, "uf": ops_settings.ops_valor_uf}).scalar()

    f = db.execute(text(
        "SELECT * FROM ops_usuarios_por_empresa(30) WHERE company_id = :cid"),
        {"cid": str(c.id)}).first()

    fact = db.execute(text("""
        SELECT count(*) FILTER (WHERE estado = 'pendiente'
                                AND emitida_at < now() - interval '14 days') AS impagas,
               COALESCE(sum(monto) FILTER (WHERE estado = 'pendiente'
                                AND emitida_at < now() - interval '14 days'), 0) AS vencido,
               max(pagada_at) AS ultima_pagada
          FROM facturas WHERE company_id = :cid
    """), {"cid": str(c.id)}).one()

    actividad_6m = db.execute(text("""
        SELECT to_char(date_trunc('month', created_at), 'YYYY-MM') AS mes,
               count(DISTINCT user_id) AS usuarios_activos
          FROM actividad
         WHERE company_id = :cid AND user_id IS NOT NULL
           AND created_at >= date_trunc('month', now()) - interval '5 months'
         GROUP BY 1 ORDER BY 1
    """), {"cid": str(c.id)}).all()

    contratos = db.execute(text(
        "SELECT count(*) FROM contratos WHERE company_id = :cid AND estado = 'vigente'"),
        {"cid": str(c.id)}).scalar() or 0
    docs = db.execute(text(
        "SELECT count(*) FROM documentos WHERE company_id = :cid AND estado_calc = 'ok'"),
        {"cid": str(c.id)}).scalar() or 0

    _cache(resp)
    return {
        "company_id": str(c.id), "nombre": c.nombre, "rut": c.rut,
        "industria": c.industria, "status": c.status,
        "creado_at": c.created_at.isoformat(),
        "suscripcion": {
            "plan": susc.plan.nombre, "estado": susc.estado,
            "periodo_actual_hasta": susc.periodo_actual_hasta.isoformat()
                                    if susc.periodo_actual_hasta else None,
            "mrr_clp": _num(mrr),
        } if susc else None,
        "usuarios": {"totales": f.usuarios if f else 0,
                     "activos_30d": f.activos if f else 0,
                     "sin_acceso_90d": f.sin_acceso_90d if f else 0},
        "actividad_6m": [{"mes": a.mes, "usuarios_activos": a.usuarios_activos}
                         for a in actividad_6m],
        "facturacion": {
            "estado_cuenta": db.execute(text("SELECT ops_estado_cuenta(:cid)"),
                                        {"cid": str(c.id)}).scalar()
                             if c.status == "approved" else None,
            "facturas_impagas": fact.impagas,
            "monto_vencido_clp": _num(fact.vencido),
            "ultima_pagada_at": fact.ultima_pagada.isoformat()
                                if fact.ultima_pagada else None,
        },
        "contratos_activos": contratos, "documentos_vigentes": docs,
    }


# ============================================================================
# §6 — Sistemas, procesos e incidentes
# ============================================================================
@router.get("/sistemas/estado")
def sistemas_estado(resp: Response, db: Session = Depends(get_db)):
    resp.headers["Cache-Control"] = "private, max-age=0"
    componentes = list(db.scalars(select(OpsComponente)
                                  .where(OpsComponente.activo)
                                  .order_by(OpsComponente.orden)))
    abiertos = {i.componente: i for i in db.scalars(
        select(OpsIncidente).where(OpsIncidente.estado != "resuelto"))}

    uptime90 = dict(db.execute(
        select(OpsDisponibilidadDiaria.componente,
               func.round(func.avg(OpsDisponibilidadDiaria.uptime_pct), 2))
        .where(OpsDisponibilidadDiaria.fecha
               >= date.today() - timedelta(days=90))
        .group_by(OpsDisponibilidadDiaria.componente)).all())

    salida, peor = [], "operativo"
    for comp in componentes:
        estado, metricas, desde = ops.estado_componente(comp.clave)
        if metricas is not None and comp.clave in uptime90:
            metricas = {"uptime_90d_pct": float(uptime90[comp.clave]), **metricas}
        inc = abiertos.get(comp.clave)
        salida.append({
            "clave": comp.clave, "nombre": comp.nombre,
            "descripcion": comp.descripcion, "estado": estado,
            "desde": desde.isoformat() if desde else None,
            "metricas": metricas,
            "incidente_abierto_id": str(inc.id) if inc else None,
        })
        if ops.PESO_ESTADO[estado] > ops.PESO_ESTADO[peor]:
            peor = estado
    return {"estado_global": peor,
            "evaluado_at": datetime.now(timezone.utc).isoformat(),
            "componentes": salida}


@router.get("/sistemas/{componente}/disponibilidad")
def disponibilidad(componente: str, resp: Response,
                   dias: int = Query(90, ge=7, le=365),
                   db: Session = Depends(get_db)):
    comp = db.get(OpsComponente, componente)
    if comp is None:
        raise err(404, "COMPONENTE_DESCONOCIDO",
                  f"No existe el componente '{componente}'")

    desde = date.today() - timedelta(days=dias - 1)
    historia = {d.fecha: d for d in db.scalars(
        select(OpsDisponibilidadDiaria)
        .where(OpsDisponibilidadDiaria.componente == componente,
               OpsDisponibilidadDiaria.fecha >= desde,
               OpsDisponibilidadDiaria.fecha < date.today()))}

    # Incidente más severo que tocó cada día del rango.
    incs = db.scalars(select(OpsIncidente).where(
        OpsIncidente.componente == componente,
        OpsIncidente.abierto_at >= datetime.combine(
            desde, datetime.min.time(), tzinfo=timezone.utc))).all()

    def _inc_de(f: date) -> str | None:
        candidatos = [i for i in incs
                      if i.abierto_at.date() <= f
                      and (i.resuelto_at is None or i.resuelto_at.date() >= f)]
        if not candidatos:
            return None
        peor = max(candidatos, key=lambda i: ops.PESO_ESTADO[i.severidad])
        return str(peor.id)

    dias_out, pesos = [], []
    f = desde
    while f < date.today():
        d = historia.get(f)
        if d:
            dias_out.append({"fecha": f.isoformat(), "estado": d.estado,
                             "uptime_pct": float(d.uptime_pct),
                             "incidente_id": _inc_de(f)})
            pesos.append(float(d.uptime_pct))
        f += timedelta(days=1)

    parcial = ops.parcial_del_dia(componente)
    dias_out.append({"fecha": date.today().isoformat(),
                     "estado": parcial["estado"],
                     "uptime_pct": parcial["uptime_pct"],
                     "incidente_id": _inc_de(date.today()), "parcial": True})
    pesos.append(parcial["uptime_pct"])

    _cache(resp)
    return {"componente": componente,
            "uptime_pct": round(sum(pesos) / len(pesos), 2) if pesos else None,
            "dias": dias_out}


@router.get("/procesos")
def procesos(resp: Response, componente: str | None = Query(None),
             solo_con_error: bool = Query(False),
             db: Session = Depends(get_db)):
    q = select(OpsJob).order_by(OpsJob.clave)
    if componente:
        q = q.where(OpsJob.componente == componente)
    items = []
    for job in db.scalars(q):
        ultimo = db.scalars(select(OpsJobRun)
                            .where(OpsJobRun.job_clave == job.clave,
                                   OpsJobRun.status.notin_(("queued",)))
                            .order_by(OpsJobRun.created_at.desc())).first()
        if solo_con_error and (ultimo is None
                               or ultimo.status not in ("error", "timeout")):
            continue
        prox = ops.proxima_ejecucion(job)
        items.append({
            "clave": job.clave, "nombre": job.nombre, "tipo": job.tipo,
            "cron_expr": job.cron_expr, "activo": job.activo,
            "componente": job.componente,
            "pausado_motivo": job.pausado_motivo,
            "proxima_ejecucion_at": prox.isoformat() if prox else None,
            "tasa_exito_7d_pct": ops.tasa_exito_7d(db, job.clave),
            "ultima_ejecucion": {
                "run_id": ultimo.id, "status": ultimo.status,
                "disparo": ultimo.disparo,
                "started_at": ultimo.started_at.isoformat()
                              if ultimo.started_at else None,
                "duracion_s": int((ultimo.finished_at - ultimo.started_at)
                                  .total_seconds())
                              if ultimo.started_at and ultimo.finished_at else None,
                "items_procesados": ultimo.items_procesados,
                "mensaje": ultimo.mensaje,
            } if ultimo else None,
        })
    _cache(resp)
    return {"items": items}


@router.get("/procesos/{clave}/ejecuciones")
def ejecuciones(clave: str, p: Page = Depends(paginacion),
                status: str | None = Query(None),
                desde: date | None = Query(None),
                hasta: date | None = Query(None),
                db: Session = Depends(get_db)):
    if db.get(OpsJob, clave) is None:
        raise err(404, "JOB_DESCONOCIDO", f"No existe el proceso '{clave}'")
    _validar_rango(desde, hasta)

    q = select(OpsJobRun).where(OpsJobRun.job_clave == clave)
    if status:
        q = q.where(OpsJobRun.status == status)
    if desde:
        q = q.where(OpsJobRun.started_at >= datetime.combine(
            desde, datetime.min.time(), tzinfo=timezone.utc))
    if hasta:
        q = q.where(OpsJobRun.started_at < datetime.combine(
            hasta + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    filas = db.scalars(q.options(joinedload(OpsJobRun.actor))
                       .order_by(OpsJobRun.started_at.desc().nulls_last(),
                                 OpsJobRun.id.desc())
                       .offset(p.offset).limit(p.page_size)).all()
    items = [{
        "run_id": r.id, "status": r.status, "disparo": r.disparo,
        "actor": {"user_id": str(r.actor.id), "email": r.actor.email}
                 if r.actor else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "duracion_s": int((r.finished_at - r.started_at).total_seconds())
                      if r.started_at and r.finished_at else None,
        "items_procesados": r.items_procesados,
        "mensaje": r.mensaje, "error_detalle": r.error_detalle,
    } for r in filas]
    return sobre(items, total, p)


class EjecutarBody(BaseModel):
    motivo: str | None = None
    params: dict = Field(default_factory=dict)


@router.post("/procesos/{clave}/ejecutar", status_code=202)
def ejecutar(clave: str, body: EjecutarBody | None = Body(None),
             db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    job = db.get(OpsJob, clave)
    if job is None:
        raise err(404, "JOB_DESCONOCIDO", f"No existe el proceso '{clave}'")

    en_curso = db.scalars(select(OpsJobRun).where(
        OpsJobRun.job_clave == clave,
        OpsJobRun.status.in_(("queued", "running")))).first()
    if en_curso:
        raise err(409, "JOB_EN_EJECUCION",
                  "Ya hay una ejecución en curso de este proceso",
                  details=[{"run_id": en_curso.id}])

    ultimo = db.scalars(select(OpsJobRun)
                        .where(OpsJobRun.job_clave == clave)
                        .order_by(OpsJobRun.created_at.desc())).first()
    disparo = ("reintento" if ultimo and ultimo.status in ("error", "timeout")
               else "manual")
    body = body or EjecutarBody()
    run = ops.crear_run(db, job, disparo=disparo, actor_user_id=user.id,
                        params=body.params)
    ops.auditar(db, "actualizacion",
                f"Ejecución {disparo} del proceso '{clave}'"
                + (f" — {body.motivo}" if body.motivo else ""),
                user_id=user.id, entidad_tipo="ops_job_run")
    db.commit()

    enqueue("ops_ejecutar_run", run_id=run.id)
    return {"run_id": run.id, "job_clave": clave, "status": "queued",
            "disparo": disparo,
            "encolado_at": datetime.now(timezone.utc).isoformat()}


class PausaBody(BaseModel):
    activo: bool
    motivo: str | None = None


@router.patch("/procesos/{clave}")
def pausar(clave: str, body: PausaBody, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    job = db.get(OpsJob, clave)
    if job is None:
        raise err(404, "JOB_DESCONOCIDO", f"No existe el proceso '{clave}'")
    if job.tipo != "programado":
        raise err(409, "JOB_NO_PAUSABLE",
                  "Los procesos continuos no se pausan desde la consola")

    if job.activo != body.activo:               # idempotente si no cambia
        if not body.activo:
            if not (body.motivo or "").strip():
                raise err(400, "MOTIVO_REQUERIDO",
                          "Indique el motivo de la pausa")
            job.activo = False
            job.pausado_por = user.id
            job.pausado_motivo = body.motivo.strip()
            ops.auditar(db, "actualizacion",
                        f"Proceso '{clave}' pausado — {job.pausado_motivo}",
                        user_id=user.id, entidad_tipo="ops_job")
        else:
            job.activo = True
            job.pausado_por = None
            job.pausado_motivo = None
            ops.auditar(db, "actualizacion", f"Proceso '{clave}' reanudado",
                        user_id=user.id, entidad_tipo="ops_job")
        db.commit()

    prox = ops.proxima_ejecucion(job)
    return {"clave": job.clave, "activo": job.activo,
            "pausado_por": {"user_id": str(user.id), "email": user.email}
                           if not job.activo else None,
            "pausado_motivo": job.pausado_motivo,
            "proxima_ejecucion_at": prox.isoformat() if prox else None}


# ------------------------------------------------------------- incidentes
def _inc_out(i: OpsIncidente) -> dict:
    fin = i.resuelto_at or datetime.now(timezone.utc)
    return {
        "id": str(i.id), "componente": i.componente, "severidad": i.severidad,
        "estado": i.estado, "origen": i.origen, "titulo": i.titulo,
        "descripcion": i.descripcion, "resolucion": i.resolucion,
        "abierto_at": i.abierto_at.isoformat(),
        "resuelto_at": i.resuelto_at.isoformat() if i.resuelto_at else None,
        "duracion_min": int((fin - i.abierto_at).total_seconds() // 60),
        "creado_por": {"user_id": str(i.creador.id), "email": i.creador.email}
                      if i.creador else None,
    }


@router.get("/incidentes")
def incidentes(p: Page = Depends(paginacion),
               estado: str | None = Query(None),
               componente: str | None = Query(None),
               desde: date | None = Query(None),
               hasta: date | None = Query(None),
               db: Session = Depends(get_db)):
    _validar_rango(desde, hasta)
    q = select(OpsIncidente)
    if estado:
        q = q.where(OpsIncidente.estado == estado)
    if componente:
        q = q.where(OpsIncidente.componente == componente)
    if desde:
        q = q.where(OpsIncidente.abierto_at >= datetime.combine(
            desde, datetime.min.time(), tzinfo=timezone.utc))
    if hasta:
        q = q.where(OpsIncidente.abierto_at < datetime.combine(
            hasta + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    filas = db.scalars(
        q.options(joinedload(OpsIncidente.creador))
        .order_by((OpsIncidente.estado == "resuelto").asc(),
                  OpsIncidente.abierto_at.desc())
        .offset(p.offset).limit(p.page_size)).all()
    return sobre([_inc_out(i) for i in filas], total, p)


class IncidenteNuevo(BaseModel):
    componente: str
    severidad: str
    titulo: str = Field(min_length=1, max_length=140)
    descripcion: str | None = None
    abierto_at: datetime | None = None


@router.post("/incidentes", status_code=201)
def crear_incidente(body: IncidenteNuevo, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    if db.get(OpsComponente, body.componente) is None:
        raise err(404, "COMPONENTE_DESCONOCIDO",
                  f"No existe el componente '{body.componente}'")
    if body.severidad not in ("degradado", "caido"):
        raise err(422, "VALIDACION",
                  "severidad debe ser 'degradado' o 'caido'")
    ahora = datetime.now(timezone.utc)
    abierto = body.abierto_at or ahora
    if abierto.tzinfo is None:
        abierto = abierto.replace(tzinfo=timezone.utc)
    if abierto > ahora or abierto < ahora - timedelta(days=7):
        raise err(400, "RANGO_INVALIDO",
                  "abierto_at no puede ser futuro ni anterior a 7 días")

    inc = OpsIncidente(componente=body.componente, severidad=body.severidad,
                       origen="manual", titulo=body.titulo,
                       descripcion=body.descripcion, abierto_at=abierto,
                       creado_por=user.id)
    db.add(inc)
    ops.auditar(db, "creacion", f"Incidente manual creado: {body.titulo}",
                user_id=user.id, entidad_tipo="ops_incidente")
    db.commit()
    db.refresh(inc)
    return _inc_out(inc)


TRANSICIONES = {("abierto", "monitoreando"), ("abierto", "resuelto"),
                ("monitoreando", "resuelto"), ("resuelto", "abierto")}


class IncidentePatch(BaseModel):
    estado: str | None = None
    severidad: str | None = None
    titulo: str | None = Field(None, min_length=1, max_length=140)
    descripcion: str | None = None
    resolucion: str | None = None


@router.patch("/incidentes/{incidente_id}")
def editar_incidente(incidente_id: uuid.UUID, body: IncidentePatch,
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    inc = db.get(OpsIncidente, incidente_id)
    if inc is None:
        raise err(404, "NO_ENCONTRADO", "Incidente inexistente")

    cambia_estado = body.estado is not None and body.estado != inc.estado
    if inc.estado == "resuelto" and not (cambia_estado and body.estado == "abierto"):
        raise err(409, "INCIDENTE_RESUELTO",
                  "Un incidente resuelto solo admite reapertura (estado='abierto')")

    if cambia_estado:
        if (inc.estado, body.estado) not in TRANSICIONES:
            raise err(400, "TRANSICION_INVALIDA",
                      f"No se puede pasar de '{inc.estado}' a '{body.estado}'")
        if body.estado == "resuelto":
            resolucion = (body.resolucion or inc.resolucion or "").strip()
            if not resolucion:
                raise err(422, "RESOLUCION_REQUERIDA",
                          "Indique la resolución para cerrar el incidente")
            inc.resolucion = resolucion
            inc.resuelto_at = datetime.now(timezone.utc)
        if body.estado == "abierto":            # reapertura
            inc.resuelto_at = None
            ops.auditar(db, "actualizacion",
                        f"Incidente reabierto: {inc.titulo}", user_id=user.id,
                        entidad_tipo="ops_incidente", entidad_id=inc.id)
        inc.estado = body.estado

    for campo in ("severidad", "titulo", "descripcion"):
        valor = getattr(body, campo)
        if valor is not None:
            if campo == "severidad" and valor not in ("degradado", "caido"):
                raise err(422, "VALIDACION",
                          "severidad debe ser 'degradado' o 'caido'")
            setattr(inc, campo, valor)
    if body.resolucion is not None and not cambia_estado:
        inc.resolucion = body.resolucion

    ops.auditar(db, "actualizacion", f"Incidente actualizado: {inc.titulo}",
                user_id=user.id, entidad_tipo="ops_incidente", entidad_id=inc.id)
    db.commit()
    db.refresh(inc)
    return _inc_out(inc)


# ============================================================================
# §7 — Indicadores de negocio (confidencial)
# ============================================================================
@router.get("/indicadores/resumen")
def indicadores_resumen(resp: Response, mes: str | None = Query(None),
                        db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    m = _mes_param(mes)
    fila, parcial = _metricas_de(db, m)
    if fila is None:
        raise err(404, "NO_ENCONTRADO", f"No hay snapshot del mes {mes}")
    _auditar_confidencial(db, user)

    prev = _snapshot(db, (m - timedelta(days=1)).replace(day=1))
    mrr = float(fila.mrr_clp)
    variacion = (round(100.0 * (mrr - float(prev.mrr_clp)) / float(prev.mrr_clp), 1)
                 if prev and float(prev.mrr_clp) else None)

    trim = _snapshot(db, _meses_atras(4)[0]) if _es_mes_actual(m) else None
    delta_trim = None
    if (trim and fila.churn_clientes_pct is not None
            and trim.churn_clientes_pct is not None):
        delta_trim = round(float(fila.churn_clientes_pct)
                           - float(trim.churn_clientes_pct), 1)

    ltv_cac = None
    payback = None
    if ops_settings.ops_cac_clp > 0 and fila.clientes_activos:
        arpa = mrr / fila.clientes_activos
        margen = ops_settings.ops_margen_bruto_pct / 100.0
        churn_ing = _num(fila.churn_ingresos_pct)
        if churn_ing:
            ltv = arpa * margen / (churn_ing / 100.0)
            ltv_cac = round(ltv / ops_settings.ops_cac_clp, 1)
        if arpa * margen > 0:
            payback = round(ops_settings.ops_cac_clp / (arpa * margen), 1)

    _cache(resp)
    return {
        "mes": m.strftime("%Y-%m"), "parcial": parcial, "moneda": "CLP",
        "valor_uf": float(fila.valor_uf),
        "mrr": {"valor": mrr, "variacion_mom_pct": variacion},
        "arr": round(mrr * 12, 2),
        "churn": {"clientes_pct": _num(fila.churn_clientes_pct),
                  "ingresos_pct": _num(fila.churn_ingresos_pct),
                  "delta_trimestre_pts": delta_trim},
        "nrr_pct": _num(fila.nrr_pct),
        "ltv_cac": {"valor": ltv_cac, "payback_meses": payback}
                   if ltv_cac is not None else None,
        "clientes_activos": fila.clientes_activos,
        "objetivo_churn_pct": ops_settings.ops_objetivo_churn_pct,
    }


def _serie_mensual(db: Session, meses: int):
    """(mes, fila, parcial) para los últimos `meses`, con el actual en vivo."""
    mes_actual = date.today().replace(day=1)
    historicos = {s.mes: s for s in db.scalars(
        select(MetricasMensuales)
        .where(MetricasMensuales.mes >= _meses_atras(meses)[0]))}
    for m in _meses_atras(meses):
        if m == mes_actual:
            fila, _ = _metricas_de(db, m)
            yield m, fila, True
        elif m in historicos:
            yield m, historicos[m], False


@router.get("/indicadores/mrr")
def indicadores_mrr(resp: Response, meses: int = Query(12, ge=1, le=36),
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    _auditar_confidencial(db, user)
    items = [{
        "mes": m.strftime("%Y-%m"), "mrr": float(f.mrr_clp),
        "nuevos": float(f.mrr_nuevos_clp), "expansion": float(f.mrr_expansion_clp),
        "contraccion": float(f.mrr_contraccion_clp),
        "perdido": float(f.mrr_perdido_clp), "parcial": parcial,
    } for m, f, parcial in _serie_mensual(db, meses)]
    _cache(resp)
    return {"moneda": "CLP", "items": items}


@router.get("/indicadores/clientes-flujo")
def clientes_flujo(resp: Response, meses: int = Query(12, ge=1, le=36),
                   db: Session = Depends(get_db)):
    items = [{
        "mes": m.strftime("%Y-%m"), "altas": f.altas, "bajas": f.bajas,
        "neto": f.altas - f.bajas, "clientes_fin": f.clientes_activos,
        "parcial": parcial,
    } for m, f, parcial in _serie_mensual(db, meses)]
    _cache(resp)
    return {"items": items}


@router.get("/indicadores/churn")
def indicadores_churn(resp: Response, meses: int = Query(12, ge=1, le=36),
                      db: Session = Depends(get_db)):
    items = [{
        "mes": m.strftime("%Y-%m"),
        "churn_clientes_pct": _num(f.churn_clientes_pct),
        "churn_ingresos_pct": _num(f.churn_ingresos_pct), "parcial": parcial,
    } for m, f, parcial in _serie_mensual(db, meses)]
    _cache(resp)
    return {"objetivo_pct": ops_settings.ops_objetivo_churn_pct, "items": items}


@router.get("/indicadores/por-plan")
def indicadores_por_plan(resp: Response, mes: str | None = Query(None),
                         db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    m = _mes_param(mes)
    _auditar_confidencial(db, user)

    if _es_mes_actual(m):
        filas = db.execute(text(
            "SELECT * FROM ops_calc_metricas_plan(:m, :uf)"),
            {"m": m, "uf": ops_settings.ops_valor_uf}).all()
        parcial = True
    else:
        filas = db.execute(
            select(MetricasMensualesPlan)
            .where(MetricasMensualesPlan.mes == m)).scalars().all()
        if not filas:
            raise err(404, "NO_ENCONTRADO", f"No hay snapshot del mes {mes}")
        parcial = False

    nombres = dict(db.execute(select(Plan.id, Plan.nombre)).all())
    total_mrr = sum(float(f.mrr_clp) for f in filas) or None
    items = [{
        "plan_id": str(f.plan_id), "plan": nombres.get(f.plan_id, "—"),
        "clientes": f.clientes, "usuarios": f.usuarios,
        "mrr": float(f.mrr_clp),
        "pct_mrr": round(100.0 * float(f.mrr_clp) / total_mrr, 1)
                   if total_mrr else None,
        "churn_clientes_pct": _num(f.churn_clientes_pct),
        "nrr_pct": _num(f.nrr_pct),
    } for f in filas]
    _cache(resp)
    return {"mes": m.strftime("%Y-%m"), "parcial": parcial, "moneda": "CLP",
            "items": items}
