"""Ingreso con la huella o el rostro del teléfono (WebAuthn). Ver ``tracking.services.biometria``."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from tracking.api.auth import usuario_out
from tracking.config import Settings, get_settings
from tracking.enums import AccionAuditoria, AccionBitacora
from tracking.models import Usuario
from tracking.permissions import Permiso, require_permission
from tracking.schemas import BiometriaActivarIn, BiometriaOut, BiometriaRespuestaIn, TokenOut
from tracking.security import (
    MENSAJE_INACTIVO,
    client_ip,
    create_access_token,
    cuenta_habilitada,
    get_db,
    verify_password,
)
from tracking.services import audit
from tracking.services.biometria import BiometriaInvalida, ContextoRP, ServicioBiometria
from tracking.services.bitacora import Actor
from tracking.services.timeutil import now_utc

router = APIRouter(prefix="/api", tags=["auth"])
log = logging.getLogger(__name__)

MENSAJE_APAGADA = "El ingreso con huella está desactivado en este servidor"


def contexto_rp(request: Request, settings: Settings) -> ContextoRP:
    """Dominio y origen con que se abrió la app (WEBAUTHN_RP_ID / WEBAUTHN_ORIGENES los fijan a mano)."""
    esquema = request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    if settings.trust_proxy_headers:
        esquema = (request.headers.get("x-forwarded-proto") or esquema).split(",")[0].strip()
        host = (request.headers.get("x-forwarded-host") or host).split(",")[0].strip()
    dominio = host.split(":")[0].lower()
    return ContextoRP(
        rp_id=settings.webauthn_rp_id or dominio,
        origenes=settings.webauthn_origen_list or [f"{esquema}://{host}"],
        nombre=settings.app_name,
    )


def _exigir_habilitada(settings: Settings) -> None:
    if not settings.biometria_habilitada:
        raise HTTPException(status.HTTP_404_NOT_FOUND, MENSAJE_APAGADA)


def _nombre_dispositivo(user_agent: str) -> str:
    ua = user_agent.lower()
    for clave, nombre in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Teléfono Android"),
                          ("windows", "Computadora Windows"), ("mac os", "Mac"), ("linux", "Linux")):
        if clave in ua:
            return nombre
    return "Dispositivo"


# ------------------------------------------------------------------------------------------- ingreso
@router.post("/auth/biometria/opciones")
def opciones_ingreso(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    """Reto para entrar con la huella (anónimo: aún no se sabe quién es)."""
    _exigir_habilitada(settings)
    if not request.app.state.public_limiter.hit(f"bio|{client_ip(request, settings)}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demasiados intentos. Espere unos minutos.")
    out = ServicioBiometria(db).opciones_ingreso(contexto_rp(request, settings))
    db.commit()
    return out


@router.post("/auth/biometria/ingreso", response_model=TokenOut)
def ingreso(
    body: BiometriaRespuestaIn,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenOut:
    _exigir_habilitada(settings)
    ip = client_ip(request, settings)
    limiter = request.app.state.login_limiter
    key = f"biometria|{ip}"
    if not limiter.hit(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demasiados intentos. Espere unos minutos.")
    request.state.bitacora_detalle = {"metodo": "biometria"}
    try:
        user = ServicioBiometria(db).verificar_ingreso(contexto_rp(request, settings), body.reto_id, body.credencial)
    except BiometriaInvalida as exc:
        db.commit()  # el reto queda consumido
        log.warning("login_biometria_fallido", extra={"ip": ip})
        request.state.bitacora_accion = AccionBitacora.LOGIN_FALLIDO
        request.state.bitacora_detalle = {"metodo": "biometria", "motivo": "huella"}
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    request.state.bitacora_actor = Actor.de(user)
    if not cuenta_habilitada(user):
        db.commit()
        request.state.bitacora_accion = AccionBitacora.LOGIN_FALLIDO
        request.state.bitacora_detalle = {"metodo": "biometria",
                                          "motivo": "usuario_inactivo" if not user.activo else "empresa_inactiva"}
        raise HTTPException(status.HTTP_403_FORBIDDEN, MENSAJE_INACTIVO)
    if user.debe_cambiar_clave:
        # Su clave fue restablecida: primero debe entrar con la clave temporal y crear la suya
        db.commit()
        request.state.bitacora_accion = AccionBitacora.LOGIN_FALLIDO
        request.state.bitacora_detalle = {"metodo": "biometria", "motivo": "clave_temporal"}
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Entre con su usuario y clave para continuar.")
    limiter.reset(key)
    user.ultimo_acceso_at = now_utc()
    db.commit()
    token, exp = create_access_token(user, settings)
    log.info("login_ok", extra={"usuario_id": user.id, "rol": user.rol, "metodo": "biometria"})
    request.state.bitacora_accion = AccionBitacora.LOGIN
    return TokenOut(access_token=token, expira=exp, usuario=usuario_out(user))


# ------------------------------------------------------------------------------------ cuenta propia
@router.post("/me/biometria/opciones")
def opciones_registro(
    body: BiometriaActivarIn,
    request: Request,
    user: Usuario = Depends(require_permission(Permiso.CUENTA_PROPIA)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Reto para activar la huella en este teléfono. Exige la clave actual (por si dejó la sesión abierta)."""
    _exigir_habilitada(settings)
    if user.debe_cambiar_clave:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Primero cree su clave personal")
    limiter = request.app.state.login_limiter
    key = f"{user.username.lower()}|{client_ip(request, settings)}"
    if not limiter.hit(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demasiados intentos. Espere unos minutos.")
    if not verify_password(body.clave.get_secret_value(), user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "La clave actual no es correcta")
    limiter.reset(key)
    try:
        out = ServicioBiometria(db).opciones_registro(user, contexto_rp(request, settings))
    except BiometriaInvalida as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return out


@router.post("/me/biometria", response_model=BiometriaOut, status_code=status.HTTP_201_CREATED)
def activar(
    body: BiometriaRespuestaIn,
    request: Request,
    user: Usuario = Depends(require_permission(Permiso.CUENTA_PROPIA)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BiometriaOut:
    _exigir_habilitada(settings)
    try:
        cred = ServicioBiometria(db).registrar(
            user, contexto_rp(request, settings), body.reto_id, body.credencial,
            _nombre_dispositivo(request.headers.get("user-agent", "")),
        )
    except BiometriaInvalida as exc:
        db.commit()  # el reto queda consumido
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.registrar(db, user, AccionAuditoria.BIOMETRIA_ACTIVADA, user, {"dispositivo": cred.dispositivo})
    db.commit()
    return BiometriaOut.model_validate(cred)


@router.get("/me/biometria", response_model=list[BiometriaOut])
def mis_huellas(
    user: Usuario = Depends(require_permission(Permiso.CUENTA_PROPIA)), db: Session = Depends(get_db)
) -> list[BiometriaOut]:
    return [BiometriaOut.model_validate(c) for c in ServicioBiometria(db).listar(user)]


@router.delete("/me/biometria/{credencial_id}", status_code=status.HTTP_204_NO_CONTENT)
def quitar(
    credencial_id: int,
    user: Usuario = Depends(require_permission(Permiso.CUENTA_PROPIA)),
    db: Session = Depends(get_db),
) -> None:
    try:
        cred = ServicioBiometria(db).eliminar(user, credencial_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe") from exc
    audit.registrar(db, user, AccionAuditoria.BIOMETRIA_ELIMINADA, user,
                    {"motivo": "usuario", "dispositivo": cred.dispositivo})
    db.commit()
