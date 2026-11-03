"""Bitácora de auditoría de acciones sensibles (no hace commit: forma parte de la transacción del llamador)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from tracking.enums import AccionAuditoria
from tracking.models import Auditoria, Usuario

log = logging.getLogger(__name__)

# Claves que jamás deben llegar a la bitácora aunque un llamador las pase por error
_PROHIBIDAS = ("clave", "password", "hash", "token", "secret")


def _limpiar(detalle: dict[str, Any] | None) -> dict[str, Any]:
    if not detalle:
        return {}
    return {k: v for k, v in detalle.items() if not any(p in k.lower() for p in _PROHIBIDAS)}


def registrar(
    db: Session,
    actor: Usuario | None,
    accion: AccionAuditoria,
    objetivo: Usuario | None = None,
    detalle: dict[str, Any] | None = None,
) -> Auditoria:
    entrada = Auditoria(
        actor_id=actor.id if actor else None,
        actor_rol=actor.rol if actor else None,
        accion=accion.value,
        objetivo_usuario_id=objetivo.id if objetivo else None,
        detalle=_limpiar(detalle),
    )
    db.add(entrada)
    log.info(
        "auditoria",
        extra={"accion": accion.value, "actor_id": entrada.actor_id, "objetivo_id": entrada.objetivo_usuario_id},
    )
    return entrada
