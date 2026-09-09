"""Modelos ORM de la consola interna (/admin/ops).

Mapea las tablas de 07_consola_interna.sql sobre el MISMO `Base` de
`app.models`, de modo que comparten metadata y sesiones. Importar este módulo
es suficiente para que las clases existan; lo hace `routers/admin_ops.py`.

También añade `Company.industria` sin tocar `models.py`: SQLAlchemy permite
agregar columnas mapeadas a una clase declarativa después de definirla, y la
columna ya existe en la BD (07 §2). Si se prefiere, mover esa línea a la clase
Company de `models.py` y borrarla de aquí — son equivalentes.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Column, Date, DateTime,
                        ForeignKey, Identity, Integer, Numeric, SmallInteger,
                        Text)
from sqlalchemy.dialects.postgresql import ENUM as PGENUM
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base, Company, User, _uuid

# ENUMs creados por 07_consola_interna.sql (create_type=False: ya existen).
_OPS_ENUMS = {
    "company_industria": ("mineria", "construccion", "energia", "industrial", "otras"),
    "ops_job_tipo": ("programado", "continuo"),
    "ops_run_status": ("queued", "running", "ok", "error", "timeout"),
    "ops_run_trigger": ("cron", "manual", "reintento"),
    "ops_comp_estado": ("operativo", "degradado", "caido"),
    "ops_inc_estado": ("abierto", "monitoreando", "resuelto"),
}


def ops_enum(name: str) -> PGENUM:
    return PGENUM(*_OPS_ENUMS[name], name=name, create_type=False)


# --- Columna nueva sobre la tabla existente (§4.2) --------------------------
# Column clásico a propósito: la adición de atributos a una clase declarativa
# ya mapeada está soportada para Column; mapped_column solo se resuelve dentro
# del cuerpo de la clase.
Company.industria = Column(ops_enum("company_industria"), default="otras")


class OpsComponente(Base):
    __tablename__ = "ops_componentes"
    clave: Mapped[str] = mapped_column(Text, primary_key=True)
    nombre: Mapped[str] = mapped_column(Text)
    descripcion: Mapped[str | None] = mapped_column(Text)
    orden: Mapped[int] = mapped_column(SmallInteger, default=0)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)


class OpsDisponibilidadDiaria(Base):
    __tablename__ = "ops_disponibilidad_diaria"
    componente: Mapped[str] = mapped_column(
        ForeignKey("ops_componentes.clave"), primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, primary_key=True)
    estado: Mapped[str] = mapped_column(ops_enum("ops_comp_estado"))
    uptime_pct: Mapped[float] = mapped_column(Numeric(5, 2))
    latencia_p95_ms: Mapped[int | None] = mapped_column(Integer)
    checks_total: Mapped[int] = mapped_column(Integer, default=0)
    checks_fallidos: Mapped[int] = mapped_column(Integer, default=0)


class OpsJob(Base):
    __tablename__ = "ops_jobs"
    clave: Mapped[str] = mapped_column(Text, primary_key=True)
    nombre: Mapped[str] = mapped_column(Text)
    tipo: Mapped[str] = mapped_column(ops_enum("ops_job_tipo"))
    cron_expr: Mapped[str | None] = mapped_column(Text)
    componente: Mapped[str | None] = mapped_column(ForeignKey("ops_componentes.clave"))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    pausado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    pausado_motivo: Mapped[str | None] = mapped_column(Text)
    timeout_s: Mapped[int] = mapped_column(Integer, default=3600)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow)

    pausado_por_user: Mapped[User | None] = relationship()


class OpsJobRun(Base):
    __tablename__ = "ops_job_runs"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_clave: Mapped[str] = mapped_column(ForeignKey("ops_jobs.clave"))
    status: Mapped[str] = mapped_column(ops_enum("ops_run_status"), default="queued")
    disparo: Mapped[str] = mapped_column(ops_enum("ops_run_trigger"), default="cron")
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    celery_task_id: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_procesados: Mapped[int | None] = mapped_column(Integer)
    mensaje: Mapped[str | None] = mapped_column(Text)
    error_detalle: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow)

    actor: Mapped[User | None] = relationship()
    job: Mapped[OpsJob] = relationship()


class OpsIncidente(Base):
    __tablename__ = "ops_incidentes"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid)
    componente: Mapped[str] = mapped_column(ForeignKey("ops_componentes.clave"))
    severidad: Mapped[str] = mapped_column(ops_enum("ops_comp_estado"))
    estado: Mapped[str] = mapped_column(ops_enum("ops_inc_estado"), default="abierto")
    origen: Mapped[str] = mapped_column(Text)  # 'auto' | 'manual' (CHECK en BD)
    titulo: Mapped[str] = mapped_column(Text)
    descripcion: Mapped[str | None] = mapped_column(Text)
    resolucion: Mapped[str | None] = mapped_column(Text)
    abierto_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow)
    resuelto_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow)

    creador: Mapped[User | None] = relationship()


class MetricasMensuales(Base):
    __tablename__ = "metricas_mensuales"
    mes: Mapped[date] = mapped_column(Date, primary_key=True)
    valor_uf: Mapped[float] = mapped_column(Numeric(10, 2))
    mrr_clp: Mapped[float] = mapped_column(Numeric(14, 2))
    mrr_nuevos_clp: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    mrr_expansion_clp: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    mrr_contraccion_clp: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    mrr_perdido_clp: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    clientes_activos: Mapped[int] = mapped_column(Integer)
    altas: Mapped[int] = mapped_column(Integer, default=0)
    bajas: Mapped[int] = mapped_column(Integer, default=0)
    churn_clientes_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    churn_ingresos_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    nrr_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    usuarios_totales: Mapped[int] = mapped_column(Integer)
    usuarios_activos: Mapped[int] = mapped_column(Integer)
    cerrado: Mapped[bool] = mapped_column(Boolean, default=False)
    calculado_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow)


class MetricasMensualesPlan(Base):
    __tablename__ = "metricas_mensuales_plan"
    mes: Mapped[date] = mapped_column(
        ForeignKey("metricas_mensuales.mes"), primary_key=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("planes.id"), primary_key=True)
    clientes: Mapped[int] = mapped_column(Integer)
    usuarios: Mapped[int] = mapped_column(Integer)
    mrr_clp: Mapped[float] = mapped_column(Numeric(14, 2))
    churn_clientes_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    nrr_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
