"""Estatus «No Entregado», ubicación GPS de los eventos y consumo de datos móviles del conductor

El estatus es texto (``facturas.estatus``): «no_entregado» no requiere cambios de esquema.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-05 15:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("eventos_estatus") as t:
        t.add_column(sa.Column("latitud", sa.Float()))
        t.add_column(sa.Column("longitud", sa.Float()))
        t.add_column(sa.Column("precision_m", sa.Float()))
    op.create_table(
        "consumo_datos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dia", sa.Date(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("usuario", sa.String(64)),
        sa.Column("usuario_nombre", sa.String(160)),
        sa.Column("empresa_id", sa.Integer()),
        sa.Column("empresa_nombre", sa.String(160)),
        sa.Column("conexion_id", sa.String(36), nullable=False),
        sa.Column("tipo_red", sa.String(12), nullable=False),
        sa.Column("tipo_efectivo", sa.String(8)),
        sa.Column("metodo", sa.String(8), nullable=False),
        sa.Column("ruta", sa.String(200), nullable=False),
        sa.Column("solicitudes", sa.Integer(), nullable=False),
        sa.Column("bytes_subida", sa.BigInteger(), nullable=False),
        sa.Column("bytes_bajada", sa.BigInteger(), nullable=False),
        sa.Column("desde", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hasta", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("usuario_id", "conexion_id", "metodo", "ruta", "dia", name="uq_consumo_datos_clave"),
    )
    op.create_index("ix_consumo_datos_dia_usuario", "consumo_datos", ["dia", "usuario_id"])
    op.create_index("ix_consumo_datos_empresa_id", "consumo_datos", ["empresa_id"])


def downgrade() -> None:
    op.drop_index("ix_consumo_datos_empresa_id", table_name="consumo_datos")
    op.drop_index("ix_consumo_datos_dia_usuario", table_name="consumo_datos")
    op.drop_table("consumo_datos")
    with op.batch_alter_table("eventos_estatus") as t:
        t.drop_column("precision_m")
        t.drop_column("longitud")
        t.drop_column("latitud")
