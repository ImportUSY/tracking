"""Bitácora de actividad (inmutable), observación de entrega y reingreso de facturas devueltas

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-22 09:00:00

DDL equivalente (PostgreSQL):

    ALTER TABLE facturas ADD COLUMN observacion_entrega TEXT;          -- nota del conductor al entregar
    ALTER TABLE facturas ADD COLUMN reingreso_de_id INTEGER
        REFERENCES facturas(id) ON DELETE SET NULL;                    -- ciclo anterior (devuelto)
    CREATE INDEX ix_facturas_reingreso_de_id ON facturas (reingreso_de_id);

    CREATE TABLE bitacora (
        id             SERIAL PRIMARY KEY,
        ocurrido_at    TIMESTAMPTZ NOT NULL,
        usuario_id     INTEGER,             -- sin FK: fotografía que sobrevive al usuario
        usuario        VARCHAR(64),
        usuario_nombre VARCHAR(160),
        rol            VARCHAR(20),
        empresa_id     INTEGER,
        empresa_nombre VARCHAR(160),
        accion         VARCHAR(20) NOT NULL, -- LOGIN, LOGOUT, VISTA, CONSULTAR, CREAR, MODIFICAR, ELIMINAR…
        modulo         VARCHAR(40),
        metodo         VARCHAR(8),
        ruta           VARCHAR(300) NOT NULL,
        recurso_id     VARCHAR(64),
        estado_http    INTEGER,
        exito          BOOLEAN NOT NULL,
        detalle        JSON NOT NULL,       -- payload saneado (sin claves ni tokens)
        ip             VARCHAR(64),
        user_agent     VARCHAR(300),
        request_id     VARCHAR(32),
        duracion_ms    INTEGER
    );
    -- Inmutable: la base de datos rechaza UPDATE y DELETE
    CREATE FUNCTION bitacora_inmutable() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'La bitácora es inmutable'; END $$ LANGUAGE plpgsql;
    CREATE TRIGGER tr_bitacora_inmutable BEFORE UPDATE OR DELETE ON bitacora
        FOR EACH ROW EXECUTE FUNCTION bitacora_inmutable();

Relleno: las facturas que ya se reingresaron (misma factura, una devuelta en una remesa anterior y otra
cargada después) quedan enlazadas con su ciclo anterior.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _es_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    with op.batch_alter_table("facturas", schema=None) as b:
        b.add_column(sa.Column("observacion_entrega", sa.Text(), nullable=True))
        b.add_column(sa.Column("reingreso_de_id", sa.Integer(), nullable=True))
        b.create_index("ix_facturas_reingreso_de_id", ["reingreso_de_id"], unique=False)
        b.create_foreign_key("fk_facturas_reingreso_de", "facturas", ["reingreso_de_id"], ["id"], ondelete="SET NULL")

    op.create_table(
        "bitacora",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ocurrido_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=True),
        sa.Column("usuario", sa.String(length=64), nullable=True),
        sa.Column("usuario_nombre", sa.String(length=160), nullable=True),
        sa.Column("rol", sa.String(length=20), nullable=True),
        sa.Column("empresa_id", sa.Integer(), nullable=True),
        sa.Column("empresa_nombre", sa.String(length=160), nullable=True),
        sa.Column("accion", sa.String(length=20), nullable=False),
        sa.Column("modulo", sa.String(length=40), nullable=True),
        sa.Column("metodo", sa.String(length=8), nullable=True),
        sa.Column("ruta", sa.String(length=300), nullable=False),
        sa.Column("recurso_id", sa.String(length=64), nullable=True),
        sa.Column("estado_http", sa.Integer(), nullable=True),
        sa.Column("exito", sa.Boolean(), nullable=False),
        sa.Column("detalle", sa.JSON(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("request_id", sa.String(length=32), nullable=True),
        sa.Column("duracion_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in ("ocurrido_at", "usuario_id", "rol", "empresa_id", "accion", "modulo"):
        op.create_index(f"ix_bitacora_{col}", "bitacora", [col], unique=False)

    if _es_sqlite():
        for evento in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER tr_bitacora_inmutable_{evento.lower()} BEFORE {evento} ON bitacora "
                "BEGIN SELECT RAISE(ABORT, 'La bitacora es inmutable'); END"
            )
    else:
        op.execute(
            "CREATE OR REPLACE FUNCTION bitacora_inmutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'La bitacora es inmutable'; END $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER tr_bitacora_inmutable BEFORE UPDATE OR DELETE ON bitacora "
            "FOR EACH ROW EXECUTE FUNCTION bitacora_inmutable()"
        )
    _enlazar_reingresos()


def _enlazar_reingresos() -> None:
    """Cada factura que repite número (misma empresa emisora) tras una devolución → su ciclo anterior."""
    from tracking.services.remesa_parser import clave_factura

    bind = op.get_bind()
    filas = bind.execute(sa.text("SELECT id, numero, empresa, estatus FROM facturas ORDER BY id")).all()
    ultima: dict[str, tuple[int, str]] = {}
    enlaces = []
    for fid, numero, empresa, estatus in filas:
        clave = clave_factura(empresa, numero)
        previa = ultima.get(clave)
        if previa is not None and previa[1] == "devolucion":
            enlaces.append({"i": fid, "p": previa[0]})
        ultima[clave] = (fid, estatus)
    for i in range(0, len(enlaces), 500):
        bind.execute(sa.text("UPDATE facturas SET reingreso_de_id = :p WHERE id = :i"), enlaces[i:i + 500])


def downgrade() -> None:
    if _es_sqlite():
        op.execute("DROP TRIGGER IF EXISTS tr_bitacora_inmutable_update")
        op.execute("DROP TRIGGER IF EXISTS tr_bitacora_inmutable_delete")
    else:
        op.execute("DROP TRIGGER IF EXISTS tr_bitacora_inmutable ON bitacora")
        op.execute("DROP FUNCTION IF EXISTS bitacora_inmutable()")
    op.drop_table("bitacora")
    with op.batch_alter_table("facturas", schema=None) as b:
        if not _es_sqlite():
            b.drop_constraint("fk_facturas_reingreso_de", type_="foreignkey")
        b.drop_index("ix_facturas_reingreso_de_id")
        b.drop_column("reingreso_de_id")
        b.drop_column("observacion_entrega")
