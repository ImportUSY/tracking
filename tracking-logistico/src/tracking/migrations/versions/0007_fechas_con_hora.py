"""Fecha Alm, Fecha Rec, Fecha Car y Fecha Tra guardan fecha y hora (Fecha Fac sigue siendo solo día)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20 09:00:00

DDL equivalente (PostgreSQL):

    ALTER TABLE facturas ALTER COLUMN fecha_alm TYPE TIMESTAMPTZ;
    ALTER TABLE facturas ALTER COLUMN fecha_rec TYPE TIMESTAMPTZ;
    ALTER TABLE facturas ALTER COLUMN fecha_car TYPE TIMESTAMPTZ;
    ALTER TABLE facturas ALTER COLUMN fecha_tra TYPE TIMESTAMPTZ;

Datos existentes:
* Fecha Alm / Fecha Rec (vienen del Excel, solo día): 00:00 de ese día en la hora local de la operación.
* Fecha Car / Fecha Tra: el momento exacto del primer evento aplicado de esa etapa (historial); si la
  etapa se saltó, el de la etapa siguiente alcanzada; sin historial, 00:00 del día que tenían.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNAS = ("fecha_alm", "fecha_rec", "fecha_car", "fecha_tra")
_ORDEN = {"recibida": 0, "cargado": 1, "en_transito": 2, "entregada": 3, "devolucion": 3}


def _zona() -> ZoneInfo:
    try:
        from tracking.config import get_settings

        return ZoneInfo(get_settings().timezone)
    except Exception:
        return ZoneInfo("America/Caracas")


def _tabla(tipo: Any) -> sa.TableClause:
    return sa.table("facturas", sa.column("id", sa.Integer()), sa.column("estatus", sa.String()),
                    *(sa.column(c, tipo) for c in COLUMNAS))


def _utc(valor: object) -> datetime | None:
    if isinstance(valor, str):
        valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if not isinstance(valor, datetime):
        return None
    return (valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _cambiar_tipo(tipo: Any) -> None:
    with op.batch_alter_table("facturas", schema=None) as b:
        for c in COLUMNAS:
            # Los valores se reescriben a continuación desde Python (igual en PostgreSQL y SQLite)
            b.alter_column(c, type_=tipo, existing_nullable=True, postgresql_using=f"NULL::{_nombre_pg(tipo)}")


def _nombre_pg(tipo: Any) -> str:
    return "timestamptz" if isinstance(tipo, sa.DateTime) else "date"


def _escribir(filas: list[dict[str, Any]], tipo: Any) -> None:
    t = _tabla(tipo)
    stmt = t.update().where(t.c.id == sa.bindparam("_id")).values(
        **{c: sa.bindparam(f"_{c}") for c in COLUMNAS})
    bind = op.get_bind()
    for i in range(0, len(filas), 500):
        bind.execute(stmt, [{"_id": f["id"], **{f"_{c}": f[c] for c in COLUMNAS}} for f in filas[i:i + 500]])


def upgrade() -> None:
    bind = op.get_bind()
    zona = _zona()
    t = _tabla(sa.Date())
    antes = [dict(r._mapping) for r in bind.execute(
        sa.select(t).where(sa.or_(*(t.c[c].isnot(None) for c in COLUMNAS))))]
    momentos = _momentos_de_etapa()
    _cambiar_tipo(sa.DateTime(timezone=True))

    def medianoche(d: date | None) -> datetime | None:
        return datetime.combine(d, time.min, tzinfo=zona).astimezone(timezone.utc) if d else None

    filas = []
    for f in antes:
        etapa = momentos.get(f["id"], {})
        filas.append({
            "id": f["id"],
            "fecha_alm": medianoche(f["fecha_alm"]),
            "fecha_rec": medianoche(f["fecha_rec"]),
            "fecha_car": (etapa.get("car") or medianoche(f["fecha_car"])) if f["fecha_car"] else None,
            "fecha_tra": (etapa.get("tra") or medianoche(f["fecha_tra"])) if f["fecha_tra"] else None,
        })
    _escribir(filas, sa.DateTime(timezone=True))


def _momentos_de_etapa() -> dict[int, dict[str, datetime]]:
    """Primer momento aplicado de cada etapa por factura (con la regla de etapas saltadas)."""
    filas = op.get_bind().execute(sa.text(
        """
        SELECT f.id, f.estatus, e.estatus, MIN(e.ocurrido_at)
        FROM facturas f JOIN eventos_estatus e ON e.factura_id = f.id
        WHERE f.estatus <> 'recibida' AND e.aplicado = :si AND e.estatus <> 'recibida'
        GROUP BY f.id, f.estatus, e.estatus
        """
    ), {"si": True}).all()
    por_factura: dict[int, tuple[str, dict[str, datetime]]] = {}
    for fid, estatus, etapa, cuando in filas:
        momento = _utc(cuando)
        if momento is not None:
            por_factura.setdefault(fid, (estatus, {}))[1][etapa] = momento
    out: dict[int, dict[str, datetime]] = {}
    for fid, (estatus, m) in por_factura.items():
        alcanzada = _ORDEN.get(estatus, 0)
        final = min((d for k, d in m.items() if _ORDEN.get(k, 0) == 3), default=None)
        tra = (m.get("en_transito") or final) if alcanzada >= 2 else None
        car = (m.get("cargado") or tra or final) if alcanzada >= 1 else None
        out[fid] = {k: v for k, v in (("car", car), ("tra", tra)) if v is not None}
    return out


def downgrade() -> None:
    bind = op.get_bind()
    zona = _zona()
    t = _tabla(sa.DateTime(timezone=True))
    antes = [dict(r._mapping) for r in bind.execute(
        sa.select(t).where(sa.or_(*(t.c[c].isnot(None) for c in COLUMNAS))))]
    _cambiar_tipo(sa.Date())
    filas = []
    for f in antes:
        fila: dict[str, Any] = {"id": f["id"]}
        for c in COLUMNAS:
            momento = _utc(f[c])
            fila[c] = momento.astimezone(zona).date() if momento else None
        filas.append(fila)
    _escribir(filas, sa.Date())
