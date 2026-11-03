"""Archivo de la bitácora por mes o por año

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22 15:00:00

DDL equivalente (PostgreSQL):

    CREATE TABLE bitacora_archivos (
        id SERIAL PRIMARY KEY,
        tipo VARCHAR(4) NOT NULL,              -- mes | anio
        periodo VARCHAR(7) NOT NULL UNIQUE,    -- 2026-09 | 2026
        desde TIMESTAMPTZ NOT NULL, hasta TIMESTAMPTZ NOT NULL,
        filas INTEGER NOT NULL, primer_id INTEGER, ultimo_id INTEGER,
        bytes INTEGER NOT NULL, sha256 VARCHAR(64) NOT NULL, formato VARCHAR(20) NOT NULL,
        contenido BYTEA NOT NULL,              -- filas originales, JSON Lines + gzip
        creado_por_id INTEGER, creado_por VARCHAR(160), created_at TIMESTAMPTZ NOT NULL
    );
    -- Los archivos tampoco se modifican ni se borran
    CREATE TRIGGER tr_bitacora_archivos_inmutable BEFORE UPDATE OR DELETE ON bitacora_archivos ...
    -- La bitácora activa sigue sin admitir UPDATE; DELETE solo dentro de un archivado
    -- (la transacción que archiva hace SET LOCAL tracking.archivando = 'si').

En SQLite (desarrollo/pruebas) el permiso de borrado es una fila temporal en bitacora_archivando dentro de la
misma transacción del archivado.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _es_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.create_table(
        "bitacora_archivos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.String(length=4), nullable=False),
        sa.Column("periodo", sa.String(length=7), nullable=False),
        sa.Column("desde", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hasta", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filas", sa.Integer(), nullable=False),
        sa.Column("primer_id", sa.Integer(), nullable=True),
        sa.Column("ultimo_id", sa.Integer(), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("formato", sa.String(length=20), nullable=False),
        sa.Column("contenido", sa.LargeBinary(), nullable=False),
        sa.Column("creado_por_id", sa.Integer(), nullable=True),
        sa.Column("creado_por", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("periodo", name="uq_bitacora_archivos_periodo"),
    )
    op.create_index("ix_bitacora_archivos_created_at", "bitacora_archivos", ["created_at"], unique=False)

    if _es_sqlite():
        op.create_table("bitacora_archivando", sa.Column("id", sa.Integer(), primary_key=True))
        op.execute("DROP TRIGGER IF EXISTS tr_bitacora_inmutable_delete")
        op.execute(
            "CREATE TRIGGER tr_bitacora_inmutable_delete BEFORE DELETE ON bitacora "
            "WHEN NOT EXISTS (SELECT 1 FROM bitacora_archivando) "
            "BEGIN SELECT RAISE(ABORT, 'La bitacora es inmutable'); END"
        )
        for evento in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER tr_bitacora_archivos_inmutable_{evento.lower()} BEFORE {evento} ON bitacora_archivos "
                "BEGIN SELECT RAISE(ABORT, 'El archivo de la bitacora es inmutable'); END"
            )
    else:
        op.execute(
            "CREATE OR REPLACE FUNCTION bitacora_inmutable() RETURNS trigger AS $$ BEGIN "
            "IF TG_OP = 'DELETE' AND current_setting('tracking.archivando', true) = 'si' THEN RETURN OLD; END IF; "
            "RAISE EXCEPTION 'La bitacora es inmutable'; END $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION bitacora_archivos_inmutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'El archivo de la bitacora es inmutable'; END $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER tr_bitacora_archivos_inmutable BEFORE UPDATE OR DELETE ON bitacora_archivos "
            "FOR EACH ROW EXECUTE FUNCTION bitacora_archivos_inmutable()"
        )


def downgrade() -> None:
    if _es_sqlite():
        op.execute("DROP TRIGGER IF EXISTS tr_bitacora_archivos_inmutable_update")
        op.execute("DROP TRIGGER IF EXISTS tr_bitacora_archivos_inmutable_delete")
        op.execute("DROP TRIGGER IF EXISTS tr_bitacora_inmutable_delete")
        op.execute(
            "CREATE TRIGGER tr_bitacora_inmutable_delete BEFORE DELETE ON bitacora "
            "BEGIN SELECT RAISE(ABORT, 'La bitacora es inmutable'); END"
        )
        op.drop_table("bitacora_archivando")
    else:
        op.execute("DROP TRIGGER IF EXISTS tr_bitacora_archivos_inmutable ON bitacora_archivos")
        op.execute("DROP FUNCTION IF EXISTS bitacora_archivos_inmutable()")
        op.execute(
            "CREATE OR REPLACE FUNCTION bitacora_inmutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'La bitacora es inmutable'; END $$ LANGUAGE plpgsql"
        )
    op.drop_index("ix_bitacora_archivos_created_at", table_name="bitacora_archivos")
    op.drop_table("bitacora_archivos")
