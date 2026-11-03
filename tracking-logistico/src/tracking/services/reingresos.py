"""Reingreso de facturas devueltas.

Una factura en **Devolución** queda habilitada para volver a cargarse en una remesa posterior (por Excel
o por foto). El reingreso crea una factura nueva —un ciclo de envío nuevo, con su propio tracking e
historial— enlazada con la anterior por ``reingreso_de_id``; la devuelta conserva intacto su historial.

Regla de identidad: la misma que la carga de REMESA (``clave_factura``: número + empresa emisora).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracking.enums import Estatus
from tracking.models import Factura
from tracking.services.remesa_parser import clave_factura


def factura_devuelta_previa(
    db: Session, numero: str, empresa: str | None, excluir_id: int | None = None
) -> Factura | None:
    """Último ciclo de esta factura, si está en Devolución y todavía no se reingresó (o ``None``).

    La consulta respeta el aislamiento por empresa de la sesión (un Transportista solo enlaza con
    facturas de su empresa)."""
    clave = clave_factura(empresa, numero)
    candidatas = [
        f for f in db.scalars(select(Factura).where(Factura.numero == numero).order_by(Factura.id))
        if f.id != excluir_id and clave_factura(f.empresa, f.numero) == clave
    ]
    if not candidatas or candidatas[-1].estatus != Estatus.DEVOLUCION.value:
        return None
    ultima = candidatas[-1]
    ya = db.scalar(select(Factura.id).where(Factura.reingreso_de_id == ultima.id, Factura.id != (excluir_id or 0)))
    return None if ya is not None else ultima


def habilitada_para_reingreso(db: Session, factura: Factura) -> bool:
    """Devuelta y sin un ciclo posterior: puede cargarse otra vez en una remesa nueva."""
    if factura.estatus != Estatus.DEVOLUCION.value:
        return False
    return db.scalar(select(Factura.id).where(Factura.reingreso_de_id == factura.id).limit(1)) is None
