"""Multi-tenant (empresas de transporte) y metadatos de evidencia fotográfica (PoD)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14 10:00:00

DDL equivalente (PostgreSQL):

    CREATE TABLE empresas (
        id          SERIAL PRIMARY KEY,
        nombre      VARCHAR(160) NOT NULL UNIQUE,
        rif         VARCHAR(20) UNIQUE,
        telefono    VARCHAR(40),
        email       VARCHAR(200),
        direccion   TEXT,
        activo      BOOLEAN NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL,
        updated_at  TIMESTAMPTZ NOT NULL
    );
    ALTER TABLE usuarios      ADD COLUMN empresa_id INTEGER REFERENCES empresas(id) ON DELETE RESTRICT;
    ALTER TABLE remesas       ADD COLUMN empresa_id INTEGER REFERENCES empresas(id) ON DELETE RESTRICT;
    ALTER TABLE facturas      ADD COLUMN empresa_id INTEGER REFERENCES empresas(id) ON DELETE RESTRICT;
    ALTER TABLE importaciones ADD COLUMN empresa_id INTEGER REFERENCES empresas(id) ON DELETE RESTRICT;
    CREATE INDEX ix_facturas_empresa_estatus ON facturas (empresa_id, estatus);   -- + ix_<tabla>_empresa_id

    -- Proof of Delivery
    ALTER TABLE facturas ADD COLUMN evidencia_foto_id VARCHAR(36) REFERENCES fotos(id) ON DELETE SET NULL;
    ALTER TABLE facturas ADD COLUMN evidencia_at TIMESTAMPTZ;
    ALTER TABLE fotos    ADD COLUMN capturada_at TIMESTAMPTZ;
    ALTER TABLE fotos    ADD COLUMN url_externa VARCHAR(1000);

Relación lógica: usuarios.empresa_id define el inquilino (obligatorio para TRANSPORTISTA/CONDUCTOR,
validado en la aplicación); remesas.empresa_id = empresa del conductor asignado; facturas.empresa_id
se desnormaliza desde su remesa para filtrar sin JOIN. NULL = "sin empresa" (solo roles globales).

Datos existentes: hasta hoy el sistema era de una sola empresa. Si hay datos, se crea
"EMPRESA PRINCIPAL" y se le asignan transportistas, conductores, remesas, facturas e importaciones,
de modo que nadie pierde acceso al actualizar. Luego el Supervisor crea las demás empresas y mueve
usuarios. También se rellena ``facturas.evidencia_foto_id`` desde el historial de eventos.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLAS_INQUILINO = ("usuarios", "remesas", "facturas", "importaciones")
EMPRESA_INICIAL = "EMPRESA PRINCIPAL"


def _es_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.create_table(
        "empresas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nombre", sa.String(length=160), nullable=False),
        sa.Column("rif", sa.String(length=20), nullable=True),
        sa.Column("telefono", sa.String(length=40), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("direccion", sa.Text(), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nombre", name="uq_empresas_nombre"),
        sa.UniqueConstraint("rif", name="uq_empresas_rif"),
    )

    # ADD COLUMN nativo (igual que 0003): en SQLite no se reconstruyen tablas con FKs activas.
    # En SQLite la FK la declara el modelo para bases nuevas; en PostgreSQL se crea aquí.
    for tabla in TABLAS_INQUILINO:
        op.add_column(tabla, sa.Column("empresa_id", sa.Integer(), nullable=True))
        op.create_index(f"ix_{tabla}_empresa_id", tabla, ["empresa_id"], unique=False)
        if not _es_sqlite():
            nombre_fk = "fk_usuarios_empresa" if tabla == "usuarios" else f"{tabla}_empresa_id_fkey"
            op.create_foreign_key(nombre_fk, tabla, "empresas", ["empresa_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_facturas_empresa_estatus", "facturas", ["empresa_id", "estatus"], unique=False)

    # Proof of Delivery
    op.add_column("facturas", sa.Column("evidencia_foto_id", sa.String(length=36), nullable=True))
    op.add_column("facturas", sa.Column("evidencia_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("fotos", sa.Column("capturada_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("fotos", sa.Column("url_externa", sa.String(length=1000), nullable=True))
    if not _es_sqlite():
        op.create_foreign_key("fk_facturas_evidencia_foto", "facturas", "fotos",
                              ["evidencia_foto_id"], ["id"], ondelete="SET NULL")

    _asignar_empresa_inicial()
    _rellenar_evidencias()


def _asignar_empresa_inicial() -> None:
    bind = op.get_bind()
    hay_datos = bind.execute(sa.text(
        "SELECT 1 FROM usuarios WHERE rol IN ('transportista', 'conductor') "
        "UNION ALL SELECT 1 FROM remesas UNION ALL SELECT 1 FROM importaciones LIMIT 1"
    )).first()
    if hay_datos is None:
        return  # instalación nueva: el Supervisor crea las empresas desde el panel
    ahora = datetime.now(timezone.utc)
    bind.execute(
        sa.text("INSERT INTO empresas (nombre, activo, created_at, updated_at) VALUES (:n, :a, :t, :t)"),
        {"n": EMPRESA_INICIAL, "a": True, "t": ahora},
    )
    empresa_id = bind.execute(sa.text("SELECT id FROM empresas WHERE nombre = :n"), {"n": EMPRESA_INICIAL}).scalar()
    bind.execute(
        sa.text("UPDATE usuarios SET empresa_id = :e WHERE rol IN ('transportista', 'conductor')"),
        {"e": empresa_id},
    )
    for tabla in ("remesas", "facturas", "importaciones"):
        bind.execute(sa.text(f"UPDATE {tabla} SET empresa_id = :e"), {"e": empresa_id})


def _rellenar_evidencias() -> None:
    """Foto del último evento aplicado que llevó la factura a su estatus de cierre vigente."""
    op.get_bind().execute(
        sa.text(
            """
            UPDATE facturas SET
                evidencia_foto_id = (
                    SELECT e.foto_id FROM eventos_estatus e
                    WHERE e.factura_id = facturas.id AND e.aplicado = :si AND e.estatus = facturas.estatus
                      AND e.foto_id IS NOT NULL
                    ORDER BY e.ocurrido_at DESC, e.recibido_at DESC LIMIT 1),
                evidencia_at = (
                    SELECT e.ocurrido_at FROM eventos_estatus e
                    WHERE e.factura_id = facturas.id AND e.aplicado = :si AND e.estatus = facturas.estatus
                      AND e.foto_id IS NOT NULL
                    ORDER BY e.ocurrido_at DESC, e.recibido_at DESC LIMIT 1)
            WHERE estatus IN ('entregada', 'devolucion')
            """
        ),
        {"si": True},
    )


def downgrade() -> None:
    sqlite = _es_sqlite()
    if not sqlite:
        op.drop_constraint("fk_facturas_evidencia_foto", "facturas", type_="foreignkey")
    op.drop_column("fotos", "url_externa")
    op.drop_column("fotos", "capturada_at")
    op.drop_column("facturas", "evidencia_at")
    op.drop_column("facturas", "evidencia_foto_id")

    op.drop_index("ix_facturas_empresa_estatus", table_name="facturas")
    for tabla in TABLAS_INQUILINO:
        if not sqlite:
            nombre_fk = "fk_usuarios_empresa" if tabla == "usuarios" else f"{tabla}_empresa_id_fkey"
            op.drop_constraint(nombre_fk, tabla, type_="foreignkey")
        op.drop_index(f"ix_{tabla}_empresa_id", table_name=tabla)
        op.drop_column(tabla, "empresa_id")
    op.drop_table("empresas")
