"""Capturas de facturas en papel (OCR / visión + validación humana)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19 09:00:00

DDL equivalente (PostgreSQL):

    CREATE TABLE capturas_factura (
        id                VARCHAR(36) PRIMARY KEY,          -- UUID
        empresa_id        INTEGER REFERENCES empresas(id) ON DELETE RESTRICT,   -- inquilino
        estado            VARCHAR(20) NOT NULL,             -- pendiente | confirmada | descartada
        archivo_nombre    VARCHAR(255) NOT NULL,
        content_type      VARCHAR(60)  NOT NULL,
        bytes             INTEGER      NOT NULL,
        sha256            VARCHAR(64)  NOT NULL,
        ruta              VARCHAR(500) NOT NULL,            -- ruta en disco o 'db'
        url_externa       VARCHAR(1000),                    -- object storage (futuro)
        datos             BYTEA,                            -- imagen cuando STORAGE_BACKEND=db (Vercel)
        motor             VARCHAR(80)  NOT NULL,            -- 'anthropic:<modelo>' | 'manual'
        datos_extraidos   JSON NOT NULL,                    -- {campo: {valor, texto, confianza}}
        avisos            JSON NOT NULL,
        error             TEXT,
        datos_validados   JSON,                             -- lo que aprobó el operador
        factura_id        INTEGER REFERENCES facturas(id) ON DELETE SET NULL,
        remesa_id         INTEGER REFERENCES remesas(id)  ON DELETE SET NULL,
        creado_por_id     INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        confirmado_por_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        created_at        TIMESTAMPTZ NOT NULL,
        confirmado_at     TIMESTAMPTZ
    );
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capturas_factura",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("empresa_id", sa.Integer(), nullable=True),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("archivo_nombre", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=60), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("ruta", sa.String(length=500), nullable=False),
        sa.Column("url_externa", sa.String(length=1000), nullable=True),
        sa.Column("datos", sa.LargeBinary(), nullable=True),
        sa.Column("motor", sa.String(length=80), nullable=False),
        sa.Column("datos_extraidos", sa.JSON(), nullable=False),
        sa.Column("avisos", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("datos_validados", sa.JSON(), nullable=True),
        sa.Column("factura_id", sa.Integer(), nullable=True),
        sa.Column("remesa_id", sa.Integer(), nullable=True),
        sa.Column("creado_por_id", sa.Integer(), nullable=True),
        sa.Column("confirmado_por_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmado_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["factura_id"], ["facturas.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["remesa_id"], ["remesas.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["creado_por_id"], ["usuarios.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["confirmado_por_id"], ["usuarios.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("capturas_factura", schema=None) as b:
        b.create_index("ix_capturas_factura_empresa_id", ["empresa_id"], unique=False)
        b.create_index("ix_capturas_factura_estado", ["estado"], unique=False)
        b.create_index("ix_capturas_factura_sha256", ["sha256"], unique=False)
        b.create_index("ix_capturas_factura_factura_id", ["factura_id"], unique=False)
        b.create_index("ix_capturas_factura_created_at", ["created_at"], unique=False)
        b.create_index("ix_capturas_empresa_estado", ["empresa_id", "estado"], unique=False)


def downgrade() -> None:
    op.drop_table("capturas_factura")
