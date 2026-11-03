"""Inicio de sesión, perfil propio y cambio de clave."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracking.config import Settings, get_settings
from tracking.enums import AccionBitacora, Rol
from tracking.models import Usuario
from tracking.permissions import Permiso, PermisoDenegado, permisos_de, permisos_interfaz, require_permission
from tracking.schemas import CambioClaveIn, LoginIn, PerfilConductorIn, PermisosOut, TokenOut, UsuarioOut, VistaIn
from tracking.security import (
    MENSAJE_INACTIVO,
    client_ip,
    create_access_token,
    cuenta_habilitada,
    estado_clave_temporal,
    get_current_user,
    get_db,
    hash_password,
    needs_rehash,
    verify_password,
)
from tracking.services.accounts import ClaveInvalida, ServicioCuentas
from tracking.services.bitacora import Actor
from tracking.services.normalize import clean_cedula, clean_code, clean_phone
from tracking.services.timeutil import now_utc

router = APIRouter(prefix="/api", tags=["auth"])
log = logging.getLogger(__name__)

# Hash ficticio para igualar tiempos cuando el usuario no existe (evita enumeración por tiempo).
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def usuario_out(user: Usuario) -> UsuarioOut:
    out = UsuarioOut.model_validate(user)
    out.conductor_id = user.conductor.id if user.conductor else None
    out.empresa_nombre = user.empresa.nombre if user.empresa else None
    out.accesos_temporales_restantes, out.cambio_clave_obligatorio = estado_clave_temporal(user, get_settings())
    return out


@router.post("/auth/login", response_model=TokenOut)
def login(
    body: LoginIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenOut:
    ip = client_ip(request, settings)
    limiter = request.app.state.login_limiter
    # Conductores entran con la cédula: tolerar "V-12.345.678"
    username = body.username
    if not db.scalar(select(Usuario.id).where(func.lower(Usuario.username) == username.lower())):
        ced = clean_cedula(username)
        if ced:
            username = ced
    key = f"{username.lower()}|{ip}"
    if not limiter.hit(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demasiados intentos. Espere unos minutos.")

    user = db.scalar(select(Usuario).where(func.lower(Usuario.username) == username.lower()))
    clave = body.password.get_secret_value()
    valido = verify_password(clave, user.password_hash if user else _DUMMY_HASH)
    if user is None or not valido:
        log.warning("login_fallido", extra={"username": username, "ip": ip})
        request.state.bitacora_accion = AccionBitacora.LOGIN_FALLIDO
        request.state.bitacora_detalle = {"usuario_intentado": username[:64], "motivo": "credenciales"}
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario o clave incorrectos")
    if not cuenta_habilitada(user):
        # Clave correcta pero Activo = No: se informa (solo a quien conoce la clave) y no se da acceso
        request.state.bitacora_actor = Actor.de(user)
        request.state.bitacora_accion = AccionBitacora.LOGIN_FALLIDO
        request.state.bitacora_detalle = {"motivo": "usuario_inactivo" if not user.activo else "empresa_inactiva"}
        raise HTTPException(status.HTTP_403_FORBIDDEN, MENSAJE_INACTIVO)

    limiter.reset(key)
    if user.debe_cambiar_clave:
        # Clave temporal: hasta ACCESOS_CLAVE_TEMPORAL ingresos (con aviso); el siguiente exige cambiarla
        usados = user.accesos_clave_temporal or 0
        user.accesos_clave_temporal = min(usados + 1, settings.accesos_clave_temporal + 1)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(clave)
    user.ultimo_acceso_at = now_utc()
    db.commit()
    token, exp = create_access_token(user, settings)
    log.info("login_ok", extra={"usuario_id": user.id, "rol": user.rol})
    request.state.bitacora_actor = Actor.de(user)
    request.state.bitacora_accion = AccionBitacora.LOGIN
    return TokenOut(access_token=token, expira=exp, usuario=usuario_out(user))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, user: Usuario = Depends(get_current_user)) -> None:
    """Cierre de sesión (queda en la bitácora). El token se borra en el dispositivo."""
    request.state.bitacora_accion = AccionBitacora.LOGOUT


@router.post("/bitacora/vista", status_code=status.HTTP_204_NO_CONTENT)
def registrar_vista(body: VistaIn, request: Request, user: Usuario = Depends(get_current_user)) -> None:
    """La interfaz informa qué pantalla/módulo abrió el usuario (acción VISTA de la bitácora)."""
    request.state.bitacora_accion = AccionBitacora.VISTA
    request.state.bitacora_detalle = {"vista": body.vista, "titulo": body.titulo}


@router.get("/me", response_model=UsuarioOut)
def me(user: Usuario = Depends(require_permission(Permiso.CUENTA_PROPIA))) -> UsuarioOut:
    return usuario_out(user)


@router.get("/me/permisos", response_model=PermisosOut)
def mis_permisos(
    user: Usuario = Depends(require_permission(Permiso.CUENTA_PROPIA)),
    settings: Settings = Depends(get_settings),
) -> PermisosOut:
    """Permisos efectivos del usuario (la interfaz muestra u oculta acciones con esto)."""
    return PermisosOut(
        rol=user.rol,
        permisos=sorted(p.value for p in permisos_de(user, settings)),
        permisos_interfaz=sorted(p.value for p in permisos_interfaz(user, settings)),
        solo_consulta=user.rol == Rol.CONSULTOR.value,
    )


@router.post("/me/clave", response_model=TokenOut)
def cambiar_clave(
    body: CambioClaveIn,
    user: Usuario = Depends(require_permission(Permiso.CUENTA_PROPIA)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenOut:
    """Cambio de la clave propia: exige la clave actual. Cierra las demás sesiones."""
    try:
        ServicioCuentas(db, settings).cambiar_clave_propia(
            user, body.clave_actual.get_secret_value(), body.clave_nueva.get_secret_value()
        )
    except PermisoDenegado as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.mensaje) from exc
    except ClaveInvalida as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    token, exp = create_access_token(user, settings)
    return TokenOut(access_token=token, expira=exp, usuario=usuario_out(user))


@router.get("/me/perfil")
def ver_perfil(user: Usuario = Depends(require_permission(Permiso.CUENTA_PROPIA))) -> dict[str, object]:
    c = user.conductor
    return {
        "usuario": usuario_out(user).model_dump(),
        "conductor": None
        if c is None
        else {
            "id": c.id,
            "cedula": c.cedula,
            "nombre": c.nombre,
            "telefono": c.telefono,
            "placa": c.placa,
        },
    }


@router.patch("/me/perfil")
def editar_perfil(
    body: PerfilConductorIn,
    user: Usuario = Depends(require_permission(Permiso.PERFIL_EDITAR)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Edición de la ficha propia del conductor.

    Desactivado por la matriz RBAC (el CONDUCTOR solo opera Remesas y Facturas). Para reactivarlo,
    agregue ``Permiso.PERFIL_EDITAR`` al rol CONDUCTOR en ``tracking/permissions.py``.
    """
    if user.conductor is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo disponible para conductores")
    c = user.conductor
    if body.nombre is not None:
        c.nombre = body.nombre.strip()
        user.nombre = c.nombre
    if body.placa is not None:
        c.placa = clean_code(body.placa, 20)
    if body.telefono is not None:
        c.telefono = clean_phone(body.telefono)
    db.commit()
    return ver_perfil(user)
