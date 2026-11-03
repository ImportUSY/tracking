"""Fecha de carga (Fecha Car) y fecha de tránsito (Fecha Tra) de cada factura

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19 13:00:00

DDL equivalente (PostgreSQL):

    ALTER TABLE facturas ADD COLUMN fecha_car DATE;   -- día en que pasó a «Cargado»
    ALTER TABLE facturas ADD COLUMN fecha_tra DATE;   -- día en que pasó a «En Tránsito»

Relleno: para las facturas que ya avanzaron, la fecha sale del primer evento aplicado de esa etapa
(día local de la operación); si la etapa se saltó, toma la fecha de la etapa siguiente alcanzada.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORDEN = {"recibida": 0, "cargado": 1, "en_transito": 2, "entregada": 3, "devolucion": 3}


def _zona() -> ZoneInfo:
    try:
        from tracking.config import get_settings

        return ZoneInfo(get_settings().timezone)
    except Exception:
        return ZoneInfo("America/Caracas")


def _dia(valor: object, zona: ZoneInfo) -> date | None:
    if isinstance(valor, str):
        valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if not isinstance(valor, datetime):
        return None
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(zona).date()


def upgrade() -> None:
    with op.batch_alter_table("facturas", schema=None) as b:
        b.add_column(sa.Column("fecha_car", sa.Date(), nullable=True))
        b.add_column(sa.Column("fecha_tra", sa.Date(), nullable=True))
    _rellenar()


def _rellenar() -> None:
    bind = op.get_bind()
    zona = _zona()
    filas = bind.execute(sa.text(
        """
        SELECT f.id, f.estatus, e.estatus, MIN(e.ocurrido_at)
        FROM facturas f JOIN eventos_estatus e ON e.factura_id = f.id
        WHERE f.estatus <> 'recibida' AND e.aplicado = :si AND e.estatus <> 'recibida'
        GROUP BY f.id, f.estatus, e.estatus
        """
    ), {"si": True}).all()
    por_factura: dict[int, tuple[str, dict[str, date]]] = {}
    for fid, estatus, etapa, cuando in filas:
        dia = _dia(cuando, zona)
        if dia is not None:
            por_factura.setdefault(fid, (estatus, {}))[1][etapa] = dia
    cambios = []
    for fid, (estatus, dias) in por_factura.items():
        alcanzada = _ORDEN.get(estatus, 0)
        final = min((d for k, d in dias.items() if _ORDEN.get(k, 0) == 3), default=None)
        tra = (dias.get("en_transito") or final) if alcanzada >= 2 else None
        car = (dias.get("cargado") or tra or final) if alcanzada >= 1 else None
        if car or tra:
            cambios.append({"i": fid, "c": car, "t": tra})
    for i in range(0, len(cambios), 500):
        bind.execute(sa.text("UPDATE facturas SET fecha_car = :c, fecha_tra = :t WHERE id = :i"), cambios[i:i + 500])


def downgrade() -> None:
    with op.batch_alter_table("facturas", schema=None) as b:
        b.drop_column("fecha_tra")
        b.drop_column("fecha_car")
