"""Alta de usuarios y conductores (lógica compartida por API, importación y CLI)."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tracking.enums import Rol
from tracking.models import Conductor, Usuario
from tracking.security import clave_temporal_inicial, hash_password
from tracking.services.normalize import clean_cedula, clean_code, clean_phone
from tracking.services.timeutil import now_utc

log = logging.getLogger(__name__)


class ConflictoDatos(Exception):
    pass


def crear_usuario(
    db: Session,
    *,
    username: str,
    nombre: str,
    rol: Rol,
    clave: str | None = None,
    email: str | None = None,
    empresa_id: int | None = None,
) -> tuple[Usuario, str | None]:
    """Crea un usuario. Si no se da clave, genera una temporal y la devuelve (para mostrarla una vez)."""
    username = username.strip()
    if db.scalar(select(Usuario.id).where(func.lower(Usuario.username) == username.lower())):
        raise ConflictoDatos(f"Ya existe el usuario '{username}'")
    temporal = None
    if not clave:
        temporal = clave = clave_temporal_inicial()
    user = Usuario(
        username=username,
        nombre=nombre.strip(),
        rol=rol.value,
        empresa_id=empresa_id,
        email=(email or "").strip() or None,
        password_hash=hash_password(clave),
        activo=True,
        debe_cambiar_clave=temporal is not None,
        accesos_clave_temporal=0,
        clave_actualizada_at=now_utc(),
    )
    db.add(user)
    db.flush()
    return user, temporal


def crear_usuario_conductor(
    db: Session,
    *,
    cedula: str,
    nombre: str,
    telefono: str | None = None,
    placa: str | None = None,
    clave: str | None = None,
    empresa_id: int | None = None,
) -> tuple[Conductor, str | None]:
    """Crea el conductor y su usuario. ``empresa_id`` es el inquilino del conductor (ver tracking.tenancy)."""
    ced = clean_cedula(cedula)
    if not ced:
        raise ConflictoDatos("Cédula inválida")
    if db.scalar(select(Conductor.id).where(Conductor.cedula == ced)):
        raise ConflictoDatos(f"Ya existe un conductor con cédula {ced}")
    user, temporal = crear_usuario(
        db, username=ced, nombre=nombre, rol=Rol.CONDUCTOR, clave=clave, empresa_id=empresa_id
    )
    conductor = Conductor(
        cedula=ced,
        nombre=nombre.strip(),
        telefono=clean_phone(telefono),
        placa=clean_code(placa, 20),
        usuario_id=user.id,
        activo=True,
    )
    db.add(conductor)
    db.flush()
    log.info("conductor_creado", extra={"conductor_id": conductor.id})
    return conductor, temporal


def asegurar_supervisor_inicial(db: Session, username: str, password: str) -> bool:
    """Crea el primer Supervisor si la base no tiene usuarios. Devuelve True si lo creó."""
    if not password:
        return False
    if db.scalar(select(func.count(Usuario.id))):
        return False
    try:
        crear_usuario(db, username=username, nombre="Supervisor", rol=Rol.SUPERVISOR, clave=password)
        db.commit()
    except (IntegrityError, ConflictoDatos):
        db.rollback()  # otro proceso (worker) lo creó al mismo tiempo
        return False
    log.warning("supervisor_inicial_creado", extra={"username": username})
    return True
