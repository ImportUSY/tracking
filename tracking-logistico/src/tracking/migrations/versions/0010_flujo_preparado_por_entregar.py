"""Flujo Preparado → Por Entregar → Entregada: se elimina «Cargado» y la Fecha Car

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-26 10:00:00

Cambios de datos (PostgreSQL y SQLite):

    -- 1) Facturas que hoy están en «Cargado» pasan a «Por Entregar» (en_transito) con su fecha de carga;
    --    su último evento «Cargado» aplicado queda en el historial como «Por Entregar».
    UPDATE eventos_estatus SET estatus = 'en_transito' WHERE id IN (<último Cargado aplicado de cada una>);
    UPDATE facturas SET estatus = 'en_transito',
                        fecha_tra = COALESCE(fecha_tra, fecha_car, estatus_at)
     WHERE estatus = 'cargado';
    -- 2) El resto de eventos «Cargado» desaparece del historial
    DELETE FROM eventos_estatus WHERE estatus = 'cargado';
    UPDATE eventos_estatus SET estatus_anterior = 'recibida' WHERE estatus_anterior = 'cargado';
    -- 3) Fecha Entregado (fecha_rec): el momento en que la factura quedó Entregada; si no, vacía (Por Entregar)
    UPDATE facturas SET fecha_rec = CASE WHEN estatus = 'entregada' THEN estatus_at END;
    -- 4) Sin Fecha Car
    ALTER TABLE facturas DROP COLUMN fecha_car;

Los valores internos ``recibida`` y ``en_transito`` se conservan (solo cambia su nombre visible:
«Preparado» y «Por Entregar»). La bajada vuelve a crear ``fecha_car`` vacía; los eventos «Cargado»
borrados y las Fecha Rec anteriores (del Excel) no se recuperan.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        """
        UPDATE eventos_estatus SET estatus = 'en_transito'
        WHERE id IN (
            SELECT (SELECT e.id FROM eventos_estatus e
                    WHERE e.factura_id = f.id AND e.estatus = 'cargado' AND e.aplicado = :si
                    ORDER BY e.ocurrido_at DESC, e.recibido_at DESC LIMIT 1)
            FROM facturas f WHERE f.estatus = 'cargado'
        )
        """
    ), {"si": True})
    bind.execute(sa.text(
        """
        UPDATE facturas SET estatus = 'en_transito',
               fecha_tra = COALESCE(fecha_tra, fecha_car, estatus_at),
               version = COALESCE(version, 0) + 1
        WHERE estatus = 'cargado'
        """
    ))
    bind.execute(sa.text("DELETE FROM eventos_estatus WHERE estatus = 'cargado'"))
    bind.execute(sa.text("UPDATE eventos_estatus SET estatus_anterior = 'recibida' WHERE estatus_anterior = 'cargado'"))
    bind.execute(sa.text("UPDATE facturas SET fecha_rec = CASE WHEN estatus = 'entregada' THEN estatus_at END"))
    with op.batch_alter_table("facturas", schema=None) as b:
        b.drop_column("fecha_car")


def downgrade() -> None:
    with op.batch_alter_table("facturas", schema=None) as b:
        b.add_column(sa.Column("fecha_car", sa.DateTime(timezone=True), nullable=True))
