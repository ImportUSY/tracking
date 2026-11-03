"""Fecha Preparado en el historial (Excel o foto) y hora de la foto de la captura

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-27 09:00:00

DDL / datos equivalentes (PostgreSQL y SQLite):

    ALTER TABLE capturas_factura ADD COLUMN tomada_at TIMESTAMPTZ;   -- hora en que se tomó la foto
    -- Facturas dadas de alta por foto: Fecha Preparado = momento de la foto (antes: momento de confirmar)
    UPDATE facturas SET fecha_alm = (SELECT c.created_at FROM capturas_factura c ...) WHERE <tiene captura>;
    -- Del Excel sin «Fecha Alm»: el momento de la carga
    UPDATE facturas SET fecha_alm = <fecha del evento Preparado> WHERE fecha_alm IS NULL;
    -- El evento «Preparado» del historial lleva la Fecha Preparado
    UPDATE eventos_estatus SET ocurrido_at = <fecha_alm de su factura>
     WHERE estatus = 'recibida' AND estatus_anterior IS NULL AND origen IN ('importacion', 'captura');
    -- Facturas que siguen en Preparado sin cambios: su Fecha Estatus es la Fecha Preparado
    UPDATE facturas SET estatus_at = fecha_alm WHERE estatus = 'recibida' AND <sin cambios de estatus>;
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("capturas_factura", schema=None) as b:
        b.add_column(sa.Column("tomada_at", sa.DateTime(timezone=True), nullable=True))
    bind = op.get_bind()
    bind.execute(sa.text(
        """
        UPDATE facturas SET fecha_alm = (
            SELECT MIN(c.created_at) FROM capturas_factura c WHERE c.factura_id = facturas.id)
        WHERE EXISTS (SELECT 1 FROM capturas_factura c WHERE c.factura_id = facturas.id AND c.estado = 'confirmada')
        """
    ))
    bind.execute(sa.text(
        """
        UPDATE facturas SET fecha_alm = (
            SELECT MIN(e.ocurrido_at) FROM eventos_estatus e
            WHERE e.factura_id = facturas.id AND e.estatus = 'recibida' AND e.estatus_anterior IS NULL)
        WHERE fecha_alm IS NULL
        """
    ))
    bind.execute(sa.text(
        """
        UPDATE eventos_estatus SET ocurrido_at = (
            SELECT f.fecha_alm FROM facturas f WHERE f.id = eventos_estatus.factura_id)
        WHERE estatus = 'recibida' AND estatus_anterior IS NULL AND origen IN ('importacion', 'captura')
          AND EXISTS (SELECT 1 FROM facturas f WHERE f.id = eventos_estatus.factura_id AND f.fecha_alm IS NOT NULL)
        """
    ))
    bind.execute(sa.text(
        """
        UPDATE facturas SET estatus_at = fecha_alm
        WHERE estatus = 'recibida' AND fecha_alm IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM eventos_estatus e
                          WHERE e.factura_id = facturas.id AND e.estatus_anterior IS NOT NULL AND e.aplicado = :si)
        """
    ), {"si": True})


def downgrade() -> None:
    with op.batch_alter_table("capturas_factura", schema=None) as b:
        b.drop_column("tomada_at")
