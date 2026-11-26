from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from hermeshq.models.aprobacion import TRIGGER_EVENTOS, TRIGGER_INMUTABLES, sentencias

revision: str = "e1a2b3c4d5f6"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tipo_accion", sa.String(64), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("solicitante_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("propietario_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("conversacion_id", sa.String(255), nullable=False),
        sa.Column("canal_origen", sa.String(32), nullable=False),
        sa.Column("task_id_origen", sa.String(36), nullable=True),
        sa.Column("attempt_id_origen", sa.String(36), nullable=True),
        sa.Column("huella", sa.String(64), nullable=False),
        sa.Column("version_canonica", sa.Integer(), nullable=False),
        sa.Column("contenido_json", JSONB(), nullable=False),
        sa.Column("parametros_json", JSONB(), nullable=True),
        sa.Column("vista_previa", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expira_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decidido_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decidido_por_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("canal_decision", sa.String(32), nullable=True),
        sa.Column("ejecucion_iniciada_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ejecutado_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_id_ejecucion", sa.String(36), nullable=True),
        sa.Column("attempt_id_ejecucion", sa.String(36), nullable=True),
        sa.Column("lease_generation_ejecucion", sa.BigInteger(), nullable=True),
        sa.Column("resultado_json", JSONB(), nullable=True),
        sa.Column("reemplazada_por", sa.String(36), nullable=True),
        sa.Column("envio_estado", sa.String(24), nullable=False, server_default=sa.text("'sin_envio'")),
        sa.Column("envio_proveedor_id", sa.String(255), nullable=True),
        sa.Column("envio_intentos", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("envio_actualizado_en", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "estado IN ('pendiente', 'aprobada', 'rechazada', 'expirada', 'ejecutando', 'ejecutada', 'fallida')",
            name="ck_approvals_estado",
        ),
        sa.CheckConstraint("huella ~ '^[0-9a-f]{64}$'", name="ck_approvals_huella"),
        sa.CheckConstraint("btrim(conversacion_id) <> ''", name="ck_approvals_conversacion"),
        sa.CheckConstraint(
            "estado IN ('pendiente', 'expirada') OR decidido_por_user_id IS NOT NULL", name="ck_approvals_decision"
        ),
        sa.CheckConstraint(
            "decidido_por_user_id IS NULL OR decidido_por_user_id = propietario_user_id",
            name="ck_approvals_decisor_es_propietario",
        ),
        sa.CheckConstraint(
            "estado NOT IN ('ejecutando', 'ejecutada', 'fallida') OR ejecucion_iniciada_en IS NOT NULL",
            name="ck_approvals_ejecucion",
        ),
        sa.CheckConstraint(
            "envio_estado IN ('sin_envio', 'enviando', 'aceptada_proveedor', 'confirmada', 'entregada', 'fallida', "
            "'desconocida')",
            name="ck_approvals_envio_estado",
        ),
    )
    op.create_index("ix_approvals_propietario_estado", "approvals", ["propietario_user_id", "estado"])
    op.create_index("ix_approvals_conversacion_estado", "approvals", ["agent_id", "conversacion_id", "estado"])
    op.create_table(
        "approval_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("approval_id", sa.String(36), sa.ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("evento", sa.String(48), nullable=False),
        sa.Column("estado_anterior", sa.String(16), nullable=True),
        sa.Column("estado_nuevo", sa.String(16), nullable=True),
        sa.Column("actor_tipo", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=True),
        sa.Column("canal", sa.String(32), nullable=True),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("detalle_json", JSONB(), nullable=True),
        sa.Column("creado_en", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_approval_events_approval_id", "approval_events", ["approval_id", "creado_en"])
    for script in (TRIGGER_INMUTABLES, TRIGGER_EVENTOS):
        for sentencia in sentencias(script):
            op.execute(sentencia)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tr_approval_events_solo_insertar ON approval_events")
    op.execute("DROP TRIGGER IF EXISTS tr_approvals_inmutables ON approvals")
    op.execute("DROP FUNCTION IF EXISTS approval_events_solo_insertar()")
    op.execute("DROP FUNCTION IF EXISTS approvals_inmutables()")
    op.drop_table("approval_events")
    op.drop_table("approvals")
