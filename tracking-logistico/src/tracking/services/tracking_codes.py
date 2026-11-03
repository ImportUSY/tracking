"""Generación de números de tracking legibles y únicos."""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracking.models import Factura
from tracking.tenancy import TODAS_LAS_EMPRESAS

# Alfabeto Crockford sin I, L, O, U (evita confusiones al dictarlo por teléfono)
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PREFIX = "TR-"
LENGTH = 8


def new_code() -> str:
    return PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(LENGTH))


def ensure_tracking(db: Session, factura: Factura, max_attempts: int = 8) -> str:
    """Asigna un tracking si la factura aún no tiene. 32^8 ≈ 1e12 combinaciones: colisión improbable."""
    if factura.tracking:
        return factura.tracking
    for _ in range(max_attempts):
        code = new_code()
        # La unicidad del tracking es global: se comprueba sobre todas las empresas, no solo la visible
        exists = db.scalar(select(Factura.id).where(Factura.tracking == code).execution_options(**TODAS_LAS_EMPRESAS))
        if exists is None:
            factura.tracking = code
            return code
    raise RuntimeError("No se pudo generar un número de tracking único")


def normalize_query(term: str) -> str:
    return term.strip().upper().replace(" ", "")
