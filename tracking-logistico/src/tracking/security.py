"""Hash de claves, tokens JWT y dependencias de autorización por rol."""

from __future__ import annotations

import secrets
import string
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from tracking.config import Settings, get_settings
from tracking.enums import Rol
from tracking.models import Usuario
from tracking.services.bitacora import Actor
from tracking.tenancy import TenantScope, vincular_sesion

_hasher = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def clave_temporal_inicial() -> str:
    """Clave temporal de usuarios y conductores nuevos: CLAVE_TEMPORAL (por defecto «qwerty»), o una
    aleatoria si se configuró vacía. Solo sirve ACCESOS_CLAVE_TEMPORAL veces (ver ``estado_clave_temporal``)."""
    return get_settings().clave_temporal or generate_temp_password()


def estado_clave_temporal(user: Usuario, settings: Settings) -> tuple[int | None, bool]:
    """(accesos que le quedan con la clave temporal, ¿cambio obligatorio ya?). (None, False) si no aplica."""
    if not user.debe_cambiar_clave:
        return None, False
    usados = user.accesos_clave_temporal or 0
    return max(0, settings.accesos_clave_temporal - usados), usados > settings.accesos_clave_temporal


def generate_temp_password(length: int = 8) -> str:
    # Sin caracteres ambiguos (0/O, 1/l/I) para dictarla por teléfono.
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_access_token(user: Usuario, settings: Settings) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    if user.rol == Rol.CONDUCTOR.value:
        exp = now + timedelta(days=settings.driver_token_days)
    else:
        exp = now + timedelta(minutes=settings.staff_token_minutes)
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "rol": user.rol,
        "tv": user.token_version,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, exp


def get_db(request: Request) -> Iterator[Session]:
    db: Session = request.app.state.db.session()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


MENSAJE_INACTIVO = "Su usuario está inactivo: no puede acceder. Comuníquese con su supervisor o transportista."
MENSAJE_SOLO_CONSULTA = (
    "Este usuario es Consultor: solo puede consultar. No puede agregar, modificar ni eliminar."
)
# Lo único que un CONSULTOR puede enviar (además de leer): su propia clave y las operaciones de análisis
# que no guardan nada (vista previa de correos, extracción de la foto, análisis de la hoja de remesas)
ESCRITURAS_CONSULTOR: frozenset[str] = frozenset({
    "/api/me/clave",
    "/api/auth/logout",
    "/api/bitacora/vista",
    "/api/reportes/notificar/vendedores",  # vista previa (el envío real se rechaza en el endpoint)
    "/api/capturas/analizar",  # extracción de la foto sin guardar
    "/api/importaciones/analizar",  # análisis de la hoja de remesas sin guardar
    "/api/importaciones/analizar-archivo",
})
# Su propia huella (activar y quitar): rutas con id, por eso van por prefijo
PREFIJOS_ESCRITURA_CONSULTOR: tuple[str, ...] = ("/api/me/biometria",)
METODOS_LECTURA: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})
MENSAJE_CAMBIO_OBLIGATORIO = "Ya usó todos los accesos con la clave temporal: cree su clave personal para continuar."
# Lo único que puede hacer quien agotó los accesos con la clave temporal
RUTAS_CAMBIO_OBLIGATORIO: frozenset[str] = frozenset({
    "/api/me", "/api/me/clave", "/api/me/permisos", "/api/auth/logout", "/api/bitacora/vista",
})


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Usuario:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Sesión inválida o vencida",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise unauthorized
    try:
        payload = jwt.decode(creds.credentials, settings.secret_key, algorithms=[settings.jwt_algorithm])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized from None
    user = db.get(Usuario, user_id)
    if user is None or user.token_version != payload.get("tv"):
        raise unauthorized
    # Bitácora: quién hace esta petición (el middleware la registra al terminar, incluso si se rechaza)
    request.state.bitacora_actor = Actor.de(user)
    if not cuenta_habilitada(user):
        # Activo = No (o su empresa desactivada): sin acceso desde la siguiente petición
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, MENSAJE_INACTIVO, headers={"WWW-Authenticate": "Bearer"})
    if estado_clave_temporal(user, settings)[1] and request.url.path not in RUTAS_CAMBIO_OBLIGATORIO:
        # Agotó los accesos con la clave temporal: solo puede crear su clave personal
        raise HTTPException(status.HTTP_403_FORBIDDEN, MENSAJE_CAMBIO_OBLIGATORIO)
    if (user.rol == Rol.CONSULTOR.value and request.method not in METODOS_LECTURA
            and request.url.path not in ESCRITURAS_CONSULTOR
            and not request.url.path.startswith(PREFIJOS_ESCRITURA_CONSULTOR)):
        # Red de seguridad del modo consulta: aunque un endpoint se equivoque de permiso, no escribe
        raise HTTPException(status.HTTP_403_FORBIDDEN, MENSAJE_SOLO_CONSULTA)
    # Aislamiento por inquilino: desde aquí, toda consulta ORM de esta petición queda acotada a la
    # empresa del usuario si su rol es TRANSPORTISTA o CONDUCTOR (ver tracking.tenancy).
    vincular_sesion(db, TenantScope.de_usuario(user))
    return user


def cuenta_habilitada(user: Usuario) -> bool:
    """Usuario activo y, si pertenece a una empresa, que esa empresa siga activa."""
    return user.activo and (user.empresa is None or user.empresa.activo)


def client_ip(request: Request, settings: Settings) -> str:
    """IP real del cliente (para límites de intentos/consultas)."""
    if settings.trust_proxy_headers:
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"
