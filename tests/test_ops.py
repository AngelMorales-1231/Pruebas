"""Consola interna /admin/ops (Especificación Consola Interna v1.0).

Requiere que 07_consola_interna.sql y 08_funciones_consola.sql estén en la
lista SCRIPTS de tests/conftest.py (ver README de codigo_administracion).

Con QUEUE_BACKEND=inproc las ejecuciones manuales corren en el acto, así que
el run ya está cerrado ('ok'/'error') en la aserción siguiente al 202 — el
mismo patrón que usa test_reportes.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import API

ENVOLTURA = {"items", "page", "page_size", "total", "total_pages"}
COMPONENTES = {"api", "db", "workers", "storage", "ia", "integraciones"}


# ============================================================================
# Fixtures propias
# ============================================================================
@pytest.fixture
def plan_pro(motor_admin):
    """Plan comercial + retorno del id. Directo a BD: el CRUD de planes
    (spec v1.1 §6) no es prerrequisito de esta suite."""
    from sqlalchemy import text
    with motor_admin.connect() as c:
        pid = c.execute(text(
            "INSERT INTO planes (nombre, precio, moneda, periodo) "
            "VALUES ('Pro', 12.5, 'UF', 'mensual') "
            "ON CONFLICT (nombre) DO UPDATE SET precio = EXCLUDED.precio "
            "RETURNING id")).scalar()
        c.commit()
    return str(pid)


@pytest.fixture
def suscripcion_a(motor_admin, empresa_a, plan_pro):
    """empresa_a con suscripción activa al plan Pro y su industria fijada."""
    from sqlalchemy import text
    with motor_admin.connect() as c:
        c.execute(text(
            "UPDATE companies SET industria = 'mineria' WHERE id = :cid"),
            {"cid": empresa_a["company_id"]})
        sid = c.execute(text(
            "INSERT INTO suscripciones (company_id, plan_id, estado) "
            "VALUES (:cid, :pid, 'activa') RETURNING id"),
            {"cid": empresa_a["company_id"], "pid": plan_pro}).scalar()
        c.commit()
    return str(sid)


def _run_ok(cliente, admin, clave: str, params: dict | None = None) -> dict:
    """Ejecuta un proceso por la API y asevera que terminó ok (inproc)."""
    r = cliente.post(f"{API}/admin/ops/procesos/{clave}/ejecutar",
                     headers=admin["headers"],
                     json={"params": params or {}})
    assert r.status_code == 202, r.text
    rid = r.json()["run_id"]
    r = cliente.get(f"{API}/admin/ops/procesos/{clave}/ejecuciones",
                    headers=admin["headers"])
    run = next(x for x in r.json()["items"] if x["run_id"] == rid)
    assert run["status"] == "ok", run
    return run


# ============================================================================
# Autorización
# ============================================================================
def test_todo_el_router_exige_admin(app_cliente, empresa_a):
    for ruta in ("usuarios/resumen", "sistemas/estado", "procesos",
                 "indicadores/resumen"):
        r = app_cliente.get(f"{API}/admin/ops/{ruta}",
                            headers=empresa_a["headers"])
        assert r.status_code == 403, ruta
        assert r.json()["error"]["code"] == "SOLO_ADMIN"


def test_sin_token(app_cliente):
    r = app_cliente.get(f"{API}/admin/ops/usuarios/resumen")
    assert r.status_code == 401


# ============================================================================
# §5 — Usuarios
# ============================================================================
def test_usuarios_resumen(app_cliente, admin, empresa_a, empresa_b):
    r = app_cliente.get(f"{API}/admin/ops/usuarios/resumen",
                        headers=admin["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    u = d["usuarios"]
    # Los fixtures crean un usuario por empresa y ambos hicieron login.
    assert u["totales"] >= 2
    assert u["activos"]["n"] + u["inactivos"]["n"] == u["totales"]
    # El admin de plataforma no cuenta como usuario.
    assert d["ventana_dias"] == 30
    assert d["serie_12m"][-1]["usuarios_totales"] == u["totales"]


def test_usuarios_resumen_ventana_invalida(app_cliente, admin):
    r = app_cliente.get(f"{API}/admin/ops/usuarios/resumen?ventana_dias=5",
                        headers=admin["headers"])
    assert r.status_code == 422        # validación FastAPI (ge=7)


def test_por_plan_y_sin_plan(app_cliente, admin, empresa_a, empresa_b,
                             suscripcion_a):
    r = app_cliente.get(f"{API}/admin/ops/usuarios/por-plan",
                        headers=admin["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    pro = next(x for x in d["items"] if x["plan"] == "Pro")
    assert pro["clientes"] == 1
    assert pro["usuarios"] >= 1
    assert pro["activos"] + pro["inactivos"] == pro["usuarios"]
    # empresa_b no tiene suscripción: cae en sin_plan.
    assert d["sin_plan"]["clientes"] >= 1


def test_por_industria_categorias_estables(app_cliente, admin, empresa_a,
                                           suscripcion_a):
    r = app_cliente.get(f"{API}/admin/ops/usuarios/por-industria",
                        headers=admin["headers"])
    assert r.status_code == 200
    d = r.json()
    assert {x["industria"] for x in d["items"]} == {
        "mineria", "construccion", "energia", "industrial", "otras"}
    mineria = next(x for x in d["items"] if x["industria"] == "mineria")
    assert mineria["usuarios"] >= 1
    if d["total"]:
        assert round(sum(x["pct"] for x in d["items"]), 1) == 100.0


def test_clientes_tabla_y_filtros(app_cliente, admin, empresa_a, empresa_b,
                                  suscripcion_a):
    r = app_cliente.get(f"{API}/admin/ops/clientes", headers=admin["headers"])
    assert r.status_code == 200 and set(r.json()) == ENVOLTURA
    assert r.json()["total"] >= 2

    r = app_cliente.get(f"{API}/admin/ops/clientes?industria=mineria",
                        headers=admin["headers"])
    assert {x["industria"] for x in r.json()["items"]} == {"mineria"}

    r = app_cliente.get(f"{API}/admin/ops/clientes?plan_id=sin_plan",
                        headers=admin["headers"])
    assert all(x["plan"] is None for x in r.json()["items"])

    nombre = None
    r = app_cliente.get(f"{API}/admin/ops/clientes", headers=admin["headers"])
    nombre = r.json()["items"][0]["nombre"]
    r = app_cliente.get(f"{API}/admin/ops/clientes?search={nombre[:8]}",
                        headers=admin["headers"])
    assert any(x["nombre"] == nombre for x in r.json()["items"])

    # Sin facturas vencidas: todos al día.
    assert all(x["estado_cuenta"] == "al_dia" for x in r.json()["items"])

    r = app_cliente.get(f"{API}/admin/ops/clientes?industria=agro",
                        headers=admin["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "RANGO_INVALIDO"


def test_cliente_detalle_y_404(app_cliente, admin, empresa_a, suscripcion_a):
    r = app_cliente.get(f"{API}/admin/ops/clientes/{empresa_a['company_id']}",
                        headers=admin["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["suscripcion"]["plan"] == "Pro"
    assert d["suscripcion"]["mrr_clp"] > 0          # 12,5 UF convertidas a CLP
    assert d["facturacion"]["estado_cuenta"] == "al_dia"

    r = app_cliente.get(f"{API}/admin/ops/clientes/{uuid.uuid4()}",
                        headers=admin["headers"])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_ENCONTRADO"


# ============================================================================
# §6 — Sistemas y procesos
# ============================================================================
def test_estado_ciego_luego_operativo(app_cliente, admin):
    from app.services import ops as svc_ops
    svc_ops.reset_store()          # monitor sin latidos: peor caso

    r = app_cliente.get(f"{API}/admin/ops/sistemas/estado",
                        headers=admin["headers"])
    assert r.status_code == 200
    d = r.json()
    assert {c["clave"] for c in d["componentes"]} == COMPONENTES
    assert d["estado_global"] == "caido"           # ciego = caído

    _run_ok(app_cliente, admin, "ops_heartbeat")
    r = app_cliente.get(f"{API}/admin/ops/sistemas/estado",
                        headers=admin["headers"])
    d = r.json()
    assert d["estado_global"] == "operativo", d
    api = next(c for c in d["componentes"] if c["clave"] == "db")
    assert api["metricas"]["conexiones"] >= 1


def test_disponibilidad_404_y_parcial(app_cliente, admin):
    r = app_cliente.get(f"{API}/admin/ops/sistemas/nube/disponibilidad",
                        headers=admin["headers"])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "COMPONENTE_DESCONOCIDO"

    _run_ok(app_cliente, admin, "ops_heartbeat")
    r = app_cliente.get(f"{API}/admin/ops/sistemas/db/disponibilidad?dias=7",
                        headers=admin["headers"])
    assert r.status_code == 200
    d = r.json()
    assert d["dias"][-1]["parcial"] is True
    assert d["dias"][-1]["estado"] == "operativo"


def test_procesos_listado(app_cliente, admin):
    r = app_cliente.get(f"{API}/admin/ops/procesos", headers=admin["headers"])
    assert r.status_code == 200
    claves = {x["clave"] for x in r.json()["items"]}
    assert {"vencimientos", "ops_heartbeat", "metricas_snapshot",
            "ia_revision"} <= claves
    continuo = next(x for x in r.json()["items"] if x["clave"] == "ia_revision")
    assert continuo["tipo"] == "continuo" and continuo["cron_expr"] is None


def test_ejecutar_manual_y_409_en_ejecucion(app_cliente, admin, motor_admin):
    run = _run_ok(app_cliente, admin, "ops_heartbeat")
    assert run["disparo"] == "manual"
    assert run["actor"]["email"] == admin["email"]

    # Con un run 'running' colgado, el siguiente POST debe chocar.
    from sqlalchemy import text
    with motor_admin.connect() as c:
        c.execute(text(
            "INSERT INTO ops_job_runs (job_clave, status, disparo, started_at) "
            "VALUES ('ops_heartbeat', 'running', 'cron', now())"))
        c.commit()
    r = app_cliente.post(f"{API}/admin/ops/procesos/ops_heartbeat/ejecutar",
                         headers=admin["headers"], json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_EN_EJECUCION"
    with motor_admin.connect() as c:
        c.execute(text("UPDATE ops_job_runs SET status='ok' "
                       "WHERE status='running'"))
        c.commit()


def test_ejecutar_job_desconocido(app_cliente, admin):
    r = app_cliente.post(f"{API}/admin/ops/procesos/nada/ejecutar",
                         headers=admin["headers"], json={})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "JOB_DESCONOCIDO"


def test_pausar_reanudar_y_reglas(app_cliente, admin):
    base = f"{API}/admin/ops/procesos/purga_temporales"

    r = app_cliente.patch(base, headers=admin["headers"],
                          json={"activo": False})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "MOTIVO_REQUERIDO"

    r = app_cliente.patch(base, headers=admin["headers"],
                          json={"activo": False, "motivo": "Mantención"})
    assert r.status_code == 200
    assert r.json()["activo"] is False
    assert r.json()["proxima_ejecucion_at"] is None

    # Pausado sigue aceptando ejecución manual (la pausa es solo del cron).
    _run_ok(app_cliente, admin, "purga_temporales")

    r = app_cliente.patch(base, headers=admin["headers"], json={"activo": True})
    assert r.status_code == 200 and r.json()["activo"] is True

    r = app_cliente.patch(f"{API}/admin/ops/procesos/ia_revision",
                          headers=admin["headers"],
                          json={"activo": False, "motivo": "x"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_NO_PAUSABLE"


def test_incidentes_ciclo_completo(app_cliente, admin):
    # Crear manual.
    r = app_cliente.post(f"{API}/admin/ops/incidentes", headers=admin["headers"],
                         json={"componente": "storage", "severidad": "degradado",
                               "titulo": "Subida lenta reportada por cliente"})
    assert r.status_code == 201, r.text
    inc = r.json()
    assert inc["origen"] == "manual" and inc["estado"] == "abierto"
    iid = inc["id"]

    # Componente inexistente y severidad inválida.
    r = app_cliente.post(f"{API}/admin/ops/incidentes", headers=admin["headers"],
                         json={"componente": "nube", "severidad": "caido",
                               "titulo": "x"})
    assert r.status_code == 404
    r = app_cliente.post(f"{API}/admin/ops/incidentes", headers=admin["headers"],
                         json={"componente": "api", "severidad": "operativo",
                               "titulo": "x"})
    assert r.status_code == 422

    # Cerrar sin resolución -> 422; con resolución -> resuelto.
    r = app_cliente.patch(f"{API}/admin/ops/incidentes/{iid}",
                          headers=admin["headers"], json={"estado": "resuelto"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RESOLUCION_REQUERIDA"

    r = app_cliente.patch(f"{API}/admin/ops/incidentes/{iid}",
                          headers=admin["headers"],
                          json={"estado": "resuelto",
                                "resolucion": "Reinicio del pool"})
    assert r.status_code == 200 and r.json()["estado"] == "resuelto"

    # Resuelto: editar -> 409; reabrir -> ok; transición inválida -> 400.
    r = app_cliente.patch(f"{API}/admin/ops/incidentes/{iid}",
                          headers=admin["headers"], json={"titulo": "otro"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INCIDENTE_RESUELTO"

    r = app_cliente.patch(f"{API}/admin/ops/incidentes/{iid}",
                          headers=admin["headers"], json={"estado": "abierto"})
    assert r.status_code == 200 and r.json()["resuelto_at"] is None

    r = app_cliente.patch(f"{API}/admin/ops/incidentes/{iid}",
                          headers=admin["headers"],
                          json={"estado": "abierto"})   # abierto -> abierto
    # sin cambio de estado no es transición: se acepta como edición vacía
    assert r.status_code == 200

    r = app_cliente.get(f"{API}/admin/ops/incidentes?estado=abierto",
                        headers=admin["headers"])
    assert any(x["id"] == iid for x in r.json()["items"])


# ============================================================================
# §7 — Indicadores
# ============================================================================
def test_indicadores_resumen_en_vivo(app_cliente, admin, suscripcion_a):
    r = app_cliente.get(f"{API}/admin/ops/indicadores/resumen",
                        headers=admin["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["parcial"] is True and d["moneda"] == "CLP"
    assert d["mrr"]["valor"] > 0                    # 12,5 UF activas
    assert d["arr"] == round(d["mrr"]["valor"] * 12, 2)
    assert d["clientes_activos"] >= 1
    assert d["objetivo_churn_pct"] == 2.0
    # Sin CAC configurado no se inventa un LTV/CAC.
    assert d["ltv_cac"] is None


def test_indicadores_mes_futuro(app_cliente, admin):
    r = app_cliente.get(f"{API}/admin/ops/indicadores/resumen?mes=2999-01",
                        headers=admin["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "RANGO_INVALIDO"


def test_snapshot_y_series(app_cliente, admin, suscripcion_a):
    _run_ok(app_cliente, admin, "metricas_snapshot")

    r = app_cliente.get(f"{API}/admin/ops/indicadores/mrr?meses=3",
                        headers=admin["headers"])
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[-1]["parcial"] is True
    assert items[-1]["mrr"] > 0

    r = app_cliente.get(f"{API}/admin/ops/indicadores/clientes-flujo",
                        headers=admin["headers"])
    fin = r.json()["items"][-1]
    assert fin["neto"] == fin["altas"] - fin["bajas"]

    r = app_cliente.get(f"{API}/admin/ops/indicadores/churn",
                        headers=admin["headers"])
    assert r.json()["objetivo_pct"] == 2.0

    r = app_cliente.get(f"{API}/admin/ops/indicadores/por-plan",
                        headers=admin["headers"])
    d = r.json()
    pro = next(x for x in d["items"] if x["plan"] == "Pro")
    assert pro["mrr"] > 0
    if len(d["items"]) == 1:
        assert pro["pct_mrr"] == 100.0


# ============================================================================
# Auditoría
# ============================================================================
def test_acciones_auditadas_sin_tenant(app_cliente, admin, motor_admin):
    app_cliente.patch(f"{API}/admin/ops/procesos/purga_temporales",
                      headers=admin["headers"],
                      json={"activo": False, "motivo": "Prueba de auditoría"})
    from sqlalchemy import text
    with motor_admin.connect() as c:
        filas = c.execute(text(
            "SELECT descripcion FROM actividad "
            "WHERE modulo = 'ops' AND company_id IS NULL")).all()
    assert any("Prueba de auditoría" in f.descripcion for f in filas)
    app_cliente.patch(f"{API}/admin/ops/procesos/purga_temporales",
                      headers=admin["headers"], json={"activo": True})
