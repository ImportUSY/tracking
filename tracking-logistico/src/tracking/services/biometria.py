"""Ingreso con la huella o el rostro del teléfono (WebAuthn / llaves de acceso).

Cómo funciona:
* **Activar** (con sesión abierta y la clave actual): el teléfono crea un par de llaves para este sitio,
  protegido con su huella/rostro/PIN, y nos entrega solo la llave pública.
* **Entrar**: el servidor da un reto aleatorio, el teléfono lo firma tras pedir la huella y aquí se
  verifica la firma con la llave pública guardada. La huella nunca sale del teléfono.

Los retos se guardan en la base (no en memoria) porque en Vercel cada petición puede atenderla otra
instancia; se usan una sola vez y vencen en ``VIGENCIA_RETO``. No hace commit: lo hace la ruta.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.options_to_json_dict import options_to_json_dict
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from tracking.models import CredencialBiometrica, RetoBiometrico, Usuario
from tracking.services.timeutil import now_utc

VIGENCIA_RETO = timedelta(minutes=3)
MAX_CREDENCIALES = 10  # teléfonos con huella activada por usuario
REGISTRO = "registro"
INGRESO = "ingreso"
# Errores de una respuesta mal formada o alterada (la librería no los envuelve todos en WebAuthnException)
_ERRORES_RESPUESTA = (WebAuthnException, ValueError, KeyError, TypeError)


class BiometriaInvalida(ValueError):
    """Reto vencido o ya usado, firma inválida o llave no registrada (HTTP 400/401)."""


@dataclass(frozen=True)
class ContextoRP:
    """Sitio que pide la huella (Relying Party): dominio y orígenes aceptados en la firma."""

    rp_id: str
    origenes: list[str]
    nombre: str


def _id_normalizado(valor: str) -> str:
    return bytes_to_base64url(base64url_to_bytes(valor))


def revocar(db: Session, usuario: Usuario) -> int:
    """Quita todas las llaves de acceso del usuario (al restablecer su clave). Devuelve cuántas había."""
    filas = db.scalars(select(CredencialBiometrica.id).where(CredencialBiometrica.usuario_id == usuario.id)).all()
    if filas:
        db.execute(delete(CredencialBiometrica).where(CredencialBiometrica.usuario_id == usuario.id))
    return len(filas)


class ServicioBiometria:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------ retos
    def _nuevo_reto(self, proposito: str, usuario_id: int | None) -> RetoBiometrico:
        ahora = now_utc()
        self.db.execute(delete(RetoBiometrico).where(RetoBiometrico.expira_at < ahora))
        reto = RetoBiometrico(
            id=str(uuid.uuid4()), reto=secrets.token_bytes(32), proposito=proposito, usuario_id=usuario_id,
            expira_at=ahora + VIGENCIA_RETO,
        )
        self.db.add(reto)
        return reto

    def _consumir_reto(self, reto_id: str, proposito: str, usuario_id: int | None) -> bytes:
        reto = self.db.get(RetoBiometrico, reto_id)
        if reto is None:
            raise BiometriaInvalida("La solicitud venció. Intente de nuevo.")
        self.db.delete(reto)  # un solo uso, salga bien o mal
        self.db.flush()
        if reto.proposito != proposito or reto.usuario_id != usuario_id or reto.expira_at < now_utc():
            raise BiometriaInvalida("La solicitud venció. Intente de nuevo.")
        return reto.reto

    # ------------------------------------------------------------- activación
    def opciones_registro(self, usuario: Usuario, rp: ContextoRP) -> dict[str, Any]:
        if self.cuantas(usuario) >= MAX_CREDENCIALES:
            raise BiometriaInvalida(
                f"Ya tiene {MAX_CREDENCIALES} teléfonos con huella. Quite alguno en «Mis datos» antes de agregar otro."
            )
        reto = self._nuevo_reto(REGISTRO, usuario.id)
        opciones = generate_registration_options(
            rp_id=rp.rp_id,
            rp_name=rp.nombre,
            user_id=f"tl-{usuario.id}".encode(),
            user_name=usuario.username,
            user_display_name=usuario.nombre,
            challenge=reto.reto,
            timeout=120_000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credencial_id)) for c in self.listar(usuario)
            ],
        )
        return {"reto_id": reto.id, "opciones": options_to_json_dict(opciones)}

    def registrar(
        self, usuario: Usuario, rp: ContextoRP, reto_id: str, credencial: dict[str, Any], dispositivo: str | None
    ) -> CredencialBiometrica:
        reto = self._consumir_reto(reto_id, REGISTRO, usuario.id)
        try:
            v = verify_registration_response(
                credential=credencial, expected_challenge=reto, expected_rp_id=rp.rp_id,
                expected_origin=rp.origenes, require_user_verification=True,
            )
        except _ERRORES_RESPUESTA as exc:
            raise BiometriaInvalida("El teléfono no pudo confirmar la huella. Intente de nuevo.") from exc
        cid = bytes_to_base64url(v.credential_id)
        if self.db.scalar(select(CredencialBiometrica.id).where(CredencialBiometrica.credencial_id == cid)):
            raise BiometriaInvalida("Este teléfono ya tiene la huella activada.")
        cred = CredencialBiometrica(
            usuario_id=usuario.id, credencial_id=cid, llave_publica=v.credential_public_key,
            contador=v.sign_count, rp_id=rp.rp_id, dispositivo=dispositivo[:80] if dispositivo else None,
        )
        self.db.add(cred)
        self.db.flush()
        return cred

    # ---------------------------------------------------------------- ingreso
    def opciones_ingreso(self, rp: ContextoRP) -> dict[str, Any]:
        """Reto anónimo: el teléfono sabe qué llave usar (la guarda la app al activar la huella)."""
        reto = self._nuevo_reto(INGRESO, None)
        opciones = generate_authentication_options(
            rp_id=rp.rp_id, challenge=reto.reto, timeout=120_000,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return {"reto_id": reto.id, "opciones": options_to_json_dict(opciones)}

    def verificar_ingreso(self, rp: ContextoRP, reto_id: str, credencial: dict[str, Any]) -> Usuario:
        reto = self._consumir_reto(reto_id, INGRESO, None)
        try:
            cid = _id_normalizado(str(credencial.get("rawId") or credencial.get("id") or ""))
        except _ERRORES_RESPUESTA as exc:
            raise BiometriaInvalida("Huella no reconocida") from exc
        cred = self.db.scalar(select(CredencialBiometrica).where(CredencialBiometrica.credencial_id == cid))
        if cred is None or not cid:
            raise BiometriaInvalida("Esta huella ya no está activada en el sistema. Entre con su clave.")
        try:
            v = verify_authentication_response(
                credential=credencial, expected_challenge=reto, expected_rp_id=cred.rp_id,
                expected_origin=rp.origenes, credential_public_key=cred.llave_publica,
                credential_current_sign_count=cred.contador, require_user_verification=True,
            )
        except _ERRORES_RESPUESTA as exc:
            raise BiometriaInvalida("Huella no reconocida") from exc
        cred.contador = v.new_sign_count
        cred.ultimo_uso_at = now_utc()
        usuario = self.db.get(Usuario, cred.usuario_id)
        if usuario is None:
            raise BiometriaInvalida("Huella no reconocida")
        return usuario

    # --------------------------------------------------------------- gestión
    def listar(self, usuario: Usuario) -> list[CredencialBiometrica]:
        return list(self.db.scalars(
            select(CredencialBiometrica).where(CredencialBiometrica.usuario_id == usuario.id)
            .order_by(CredencialBiometrica.created_at)
        ))

    def cuantas(self, usuario: Usuario) -> int:
        return len(self.listar(usuario))

    def eliminar(self, usuario: Usuario, credencial_id: int) -> CredencialBiometrica:
        cred = self.db.get(CredencialBiometrica, credencial_id)
        if cred is None or cred.usuario_id != usuario.id:
            raise LookupError("No existe")
        self.db.delete(cred)
        return cred
