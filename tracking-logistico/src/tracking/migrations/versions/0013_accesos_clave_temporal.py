"""Accesos con la clave temporal (por defecto 3 con «qwerty» antes de exigir la clave personal)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-29 15:00:00

    ALTER TABLE usuarios ADD COLUMN accesos_clave_temporal INTEGER NOT NULL DEFAULT 0;
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("usuarios", schema=None) as b:
        b.add_column(sa.Column("accesos_clave_temporal", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("usuarios", schema=None) as b:
        b.drop_column("accesos_clave_temporal")
