"""Ingreso con huella o rostro del teléfono (WebAuthn): llaves de acceso y retos de un solo uso

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-02 16:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "credenciales_biometricas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("usuario_id", sa.Integer(), sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credencial_id", sa.String(512), nullable=False, unique=True),
        sa.Column("llave_publica", sa.LargeBinary(), nullable=False),
        sa.Column("contador", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rp_id", sa.String(253), nullable=False),
        sa.Column("dispositivo", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ultimo_uso_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_credenciales_biometricas_usuario_id", "credenciales_biometricas", ["usuario_id"])
    op.create_table(
        "retos_biometricos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("reto", sa.LargeBinary(), nullable=False),
        sa.Column("proposito", sa.String(10), nullable=False),
        sa.Column("usuario_id", sa.Integer()),
        sa.Column("expira_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_retos_biometricos_expira_at", "retos_biometricos", ["expira_at"])


def downgrade() -> None:
    op.drop_index("ix_retos_biometricos_expira_at", table_name="retos_biometricos")
    op.drop_table("retos_biometricos")
    op.drop_index("ix_credenciales_biometricas_usuario_id", table_name="credenciales_biometricas")
    op.drop_table("credenciales_biometricas")
