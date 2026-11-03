"""Consultas de lectura compartidas por varios endpoints."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracking.enums import Estatus
from tracking.models import Factura

FINALES = (Estatus.ENTREGADA.value, Estatus.DEVOLUCION.value)


def conteos_por_remesa(db: Session, remesa_ids: list[int]) -> dict[int, dict[str, int]]:
    """{remesa_id: {estatus: cantidad}} en una sola consulta agregada."""
    out: dict[int, dict[str, int]] = defaultdict(dict)
    if not remesa_ids:
        return out
    stmt = (
        select(Factura.remesa_id, Factura.estatus, func.count(Factura.id))
        .where(Factura.remesa_id.in_(remesa_ids))
        .group_by(Factura.remesa_id, Factura.estatus)
    )
    for rid, est, n in db.execute(stmt):
        out[rid][est] = int(n)
    return out


def progreso(conteo: dict[str, int]) -> tuple[int, float]:
    total = sum(conteo.values())
    finales = sum(v for k, v in conteo.items() if k in FINALES)
    return total, (round(finales / total, 3) if total else 0.0)
