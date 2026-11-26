from datetime import datetime
from uuid import uuid4

from sqlalchemy import DDL, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, utcnow

TRIGGER_INMUTABLES = """
CREATE OR REPLACE FUNCTION approvals_inmutables() RETURNS trigger AS $$
BEGIN
    IF NEW.huella IS DISTINCT FROM OLD.huella
       OR NEW.contenido_json IS DISTINCT FROM OLD.contenido_json
       OR NEW.vista_previa IS DISTINCT FROM OLD.vista_previa
       OR NEW.propietario_user_id IS DISTINCT FROM OLD.propietario_user_id
       OR NEW.conversacion_id IS DISTINCT FROM OLD.conversacion_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.tipo_accion IS DISTINCT FROM OLD.tipo_accion
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION 'approvals: la versión aprobable es inmutable (%).', OLD.id;
    END IF;
    IF OLD.decidido_por_user_id IS NOT NULL
       AND NEW.decidido_por_user_id IS DISTINCT FROM OLD.decidido_por_user_id THEN
        RAISE EXCEPTION 'approvals: la decisión de % no se puede reescribir.', OLD.id;
    END IF;
    IF OLD.estado IN ('rechazada', 'expirada', 'ejecutada', 'fallida') AND NEW.estado IS DISTINCT FROM OLD.estado THEN
        RAISE EXCEPTION 'approvals: % está en estado final %.', OLD.id, OLD.estado;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tr_approvals_inmutables ON approvals;
CREATE TRIGGER tr_approvals_inmutables BEFORE UPDATE ON approvals
    FOR EACH ROW EXECUTE FUNCTION approvals_inmutables();
"""

TRIGGER_EVENTOS = """
CREATE OR REPLACE FUNCTION approval_events_solo_insertar() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'approval_events es un registro de auditoría de solo inserción.';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tr_approval_events_solo_insertar ON approval_events;
CREATE TRIGGER tr_approval_events_solo_insertar BEFORE UPDATE OR DELETE ON approval_events
    FOR EACH ROW EXECUTE FUNCTION approval_events_solo_insertar();
"""


class Aprobacion(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_propietario_estado", "propietario_user_id", "estado"),
        Index("ix_approvals_conversacion_estado", "agent_id", "conversacion_id", "estado"),
        CheckConstraint(
            "estado IN ('pendiente', 'aprobada', 'rechazada', 'expirada', 'ejecutando', 'ejecutada', 'fallida')",
            name="ck_approvals_estado",
        ),
        CheckConstraint("huella ~ '^[0-9a-f]{64}$'", name="ck_approvals_huella"),
        CheckConstraint("btrim(conversacion_id) <> ''", name="ck_approvals_conversacion"),
        CheckConstraint(
            "estado IN ('pendiente', 'expirada') OR decidido_por_user_id IS NOT NULL", name="ck_approvals_decision"
        ),
        CheckConstraint(
            "decidido_por_user_id IS NULL OR decidido_por_user_id = propietario_user_id",
            name="ck_approvals_decisor_es_propietario",
        ),
        CheckConstraint(
            "estado NOT IN ('ejecutando', 'ejecutada', 'fallida') OR ejecucion_iniciada_en IS NOT NULL",
            name="ck_approvals_ejecucion",
        ),
        CheckConstraint(
            "envio_estado IN ('sin_envio', 'enviando', 'aceptada_proveedor', 'confirmada', 'entregada', 'fallida', "
            "'desconocida')",
            name="ck_approvals_envio_estado",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tipo_accion: Mapped[str] = mapped_column(String(64), nullable=False)
    estado: Mapped[str] = mapped_column(String(16), nullable=False, default="pendiente")
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False)
    solicitante_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    propietario_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    conversacion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    canal_origen: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    task_id_origen: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_id_origen: Mapped[str | None] = mapped_column(String(36), nullable=True)
    huella: Mapped[str] = mapped_column(String(64), nullable=False)
    version_canonica: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    contenido_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    parametros_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    vista_previa: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decidido_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decidido_por_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    canal_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ejecucion_iniciada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ejecutado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    task_id_ejecucion: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_id_ejecucion: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_generation_ejecucion: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resultado_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reemplazada_por: Mapped[str | None] = mapped_column(String(36), nullable=True)
    envio_estado: Mapped[str] = mapped_column(
        String(24), nullable=False, default="sin_envio", server_default=text("'sin_envio'")
    )
    envio_proveedor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    envio_intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    envio_actualizado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventoAprobacion(Base):
    __tablename__ = "approval_events"
    __table_args__ = (Index("ix_approval_events_approval_id", "approval_id", "creado_en"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=False)
    evento: Mapped[str] = mapped_column(String(48), nullable=False)
    estado_anterior: Mapped[str | None] = mapped_column(String(16), nullable=True)
    estado_nuevo: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actor_tipo: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    canal: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detalle_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


def sentencias(script: str) -> list[str]:
    funcion, _, resto = script.partition("$$ LANGUAGE plpgsql;")
    return [funcion.strip() + "$$ LANGUAGE plpgsql"] + [p.strip() for p in resto.split(";") if p.strip()]


for _tabla, _script in ((Aprobacion.__table__, TRIGGER_INMUTABLES), (EventoAprobacion.__table__, TRIGGER_EVENTOS)):
    for _sentencia in sentencias(_script):
        event.listen(_tabla, "after_create", DDL(_sentencia.replace("%", "%%")).execute_if(dialect="postgresql"))
