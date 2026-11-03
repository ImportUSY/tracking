"""Servicio de cuentas: altas y credenciales con reglas de autorización y auditoría.

Separación de responsabilidades:
* ``tracking.permissions`` decide *si* el actor puede (políticas puras, sin base de datos).
* ``tracking.services.users`` crea las filas (sin autorización; lo usan también CLI e importación).
* Este módulo orquesta: valida la política, ejecuta, invalida sesiones y deja rastro en la auditoría.

Ninguna función escribe claves en logs ni en la auditoría; la clave temporal solo se devuelve al
llamador para mostrarla una vez.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from tracking.config import Settings
from tracking.enums import AccionAuditoria, Rol
from tracking.models import Conductor, Usuario
from tracking.permissions import (
    Permiso,
    PermisoDenegado,
    tiene_permiso,
    validar_creacion,
    validar_gestion_clave,
)
from tracking.security import clave_temporal_inicial, hash_password, verify_password
from tracking.services import audit, biometria
from tracking.services.empresas import EmpresaInvalida, ServicioEmpresas
from tracking.services.timeutil import now_utc
from tracking.services.users import crear_usuario, crear_usuario_conductor
from tracking.tenancy import ROLES_AISLADOS, TenantScope

MIN_CLAVE = 6
MAX_CLAVE = 200
ALGORITMO = "argon2id"


class ClaveInvalida(ValueError):
    pass


class DatosInvalidos(ValueError):
    """Datos de alta/edición incompletos o incoherentes (HTTP 422)."""


@dataclass(frozen=True)
class ResultadoClave:
    usuario: Usuario
    clave_temporal: str | None  # solo si la generó el sistema


def validar_clave(clave: str, usuario: str | None = None) -> None:
    if len(clave) < MIN_CLAVE:
        raise ClaveInvalida(f"La clave debe tener al menos {MIN_CLAVE} caracteres")
    if len(clave) > MAX_CLAVE:
        raise ClaveInvalida("La clave es demasiado larga")
    if usuario and clave.strip().lower() == usuario.strip().lower():
        raise ClaveInvalida("La clave no puede ser igual al usuario o a la cédula")


class ServicioCuentas:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------- inquilino
    def resolver_empresa(self, actor: Usuario, rol: Rol, empresa_id: int | None) -> int | None:
        """Empresa del usuario que se va a crear (o reasignar), según quién lo hace.

        * Actor restringido (TRANSPORTISTA): siempre su propia empresa; pedir otra es 403.
        * Actor global (SUPERVISOR): la indicada; obligatoria si el rol objetivo es TRANSPORTISTA o
          CONDUCTOR (un usuario aislado sin empresa no vería nada). Debe existir y estar activa.
        """
        scope = TenantScope.de_usuario(actor)
        if scope.restringido:
            if scope.empresa_id is None:
                raise PermisoDenegado("Su usuario no está asociado a una empresa de transporte")
            if empresa_id is not None and empresa_id != scope.empresa_id:
                raise PermisoDenegado("Solo puede registrar usuarios de su propia empresa")
            return scope.empresa_id
        if empresa_id is None:
            if rol in ROLES_AISLADOS:
                raise DatosInvalidos(f"Indique la empresa de transporte del usuario {rol.value.upper()}")
            return None
        try:
            ServicioEmpresas(self.db).asignable(empresa_id)
        except (LookupError, EmpresaInvalida) as exc:
            raise DatosInvalidos(str(exc)) from exc
        return empresa_id

    def cambiar_empresa(self, actor: Usuario, objetivo: Usuario, empresa_id: int | None) -> bool:
        """Reasigna la empresa de un usuario (solo SUPERVISOR). Cierra sus sesiones si cambia."""
        if not tiene_permiso(actor, Permiso.USUARIOS_GESTIONAR, self.settings):
            raise PermisoDenegado("Solo SUPERVISOR puede cambiar la empresa de un usuario")
        nueva = self.resolver_empresa(actor, Rol(objetivo.rol), empresa_id)
        if nueva == objetivo.empresa_id:
            return False
        objetivo.empresa_id = nueva
        objetivo.token_version += 1
        return True

    # ------------------------------------------------------------------ altas
    def crear_conductor(
        self,
        actor: Usuario,
        *,
        cedula: str,
        nombre: str,
        telefono: str | None = None,
        placa: str | None = None,
        clave: str | None = None,
        empresa_id: int | None = None,
    ) -> tuple[Conductor, str | None]:
        validar_creacion(actor, Rol.CONDUCTOR, self.settings)
        empresa = self.resolver_empresa(actor, Rol.CONDUCTOR, empresa_id)
        if clave:
            validar_clave(clave, cedula)
        conductor, temporal = crear_usuario_conductor(
            self.db, cedula=cedula, nombre=nombre, telefono=telefono, placa=placa, clave=clave, empresa_id=empresa
        )
        if conductor.usuario is not None:
            conductor.usuario.clave_actualizada_por_id = actor.id
        audit.registrar(
            self.db,
            actor,
            AccionAuditoria.CONDUCTOR_CREADO,
            conductor.usuario,
            {"cedula": conductor.cedula, "clave_generada": temporal is not None, "empresa_id": empresa},
        )
        return conductor, temporal

    def crear_usuario(
        self,
        actor: Usuario,
        *,
        rol: Rol,
        nombre: str,
        username: str | None = None,
        email: str | None = None,
        clave: str | None = None,
        cedula: str | None = None,
        telefono: str | None = None,
        placa: str | None = None,
        empresa_id: int | None = None,
    ) -> tuple[Usuario, str | None]:
        if rol == Rol.CONDUCTOR:
            conductor, temporal = self.crear_conductor(
                actor, cedula=cedula or "", nombre=nombre, telefono=telefono, placa=placa, clave=clave,
                empresa_id=empresa_id,
            )
            assert conductor.usuario is not None
            return conductor.usuario, temporal
        validar_creacion(actor, rol, self.settings)
        empresa = self.resolver_empresa(actor, rol, empresa_id)
        if not username:
            raise ClaveInvalida("Indique el nombre de usuario")
        if clave:
            validar_clave(clave, username)
        user, temporal = crear_usuario(
            self.db, username=username, nombre=nombre, rol=rol, clave=clave, email=email, empresa_id=empresa
        )
        user.clave_actualizada_por_id = actor.id
        audit.registrar(
            self.db,
            actor,
            AccionAuditoria.USUARIO_CREADO,
            user,
            {"rol": rol.value, "clave_generada": temporal is not None, "empresa_id": empresa},
        )
        return user, temporal

    def registrar_conductores_importados(self, actor: Usuario, usernames: list[str]) -> None:
        """Auditoría de conductores creados al confirmar una carga de REMESA o PLACAS."""
        for u in usernames:
            audit.registrar(
                self.db,
                actor,
                AccionAuditoria.CONDUCTOR_CREADO,
                None,
                {"cedula": u, "origen": "carga_excel", "clave_generada": True},
            )

    # ------------------------------------------------------------- credenciales
    def establecer_clave(
        self, actor: Usuario, objetivo: Usuario, nueva: str | None, debe_cambiar: bool = True
    ) -> ResultadoClave:
        """Fija una clave concreta o genera una temporal. Cierra todas las sesiones del usuario."""
        validar_gestion_clave(actor, objetivo, self.settings)
        temporal: str | None = None
        if nueva:
            validar_clave(nueva, objetivo.username)
            accion = AccionAuditoria.CLAVE_ESTABLECIDA
        else:
            nueva = temporal = clave_temporal_inicial()
            debe_cambiar = True
            accion = AccionAuditoria.CLAVE_RESTABLECIDA
        objetivo.password_hash = hash_password(nueva)
        objetivo.debe_cambiar_clave = debe_cambiar
        objetivo.accesos_clave_temporal = 0  # vuelve a tener sus accesos con la clave temporal
        objetivo.token_version += 1
        objetivo.clave_actualizada_at = now_utc()
        objetivo.clave_actualizada_por_id = actor.id
        audit.registrar(self.db, actor, accion, objetivo, {"debe_cambiar_clave": debe_cambiar})
        # Teléfono perdido o en otras manos: la huella activada en él deja de servir
        revocadas = biometria.revocar(self.db, objetivo)
        if revocadas:
            audit.registrar(self.db, actor, AccionAuditoria.BIOMETRIA_ELIMINADA, objetivo,
                            {"motivo": "clave_restablecida", "cantidad": revocadas})
        return ResultadoClave(objetivo, temporal)

    def cambiar_clave_propia(self, usuario: Usuario, actual: str, nueva: str) -> None:
        if not verify_password(actual, usuario.password_hash):
            raise PermisoDenegado("La clave actual no es correcta")
        if actual == nueva:
            raise ClaveInvalida("La clave nueva debe ser distinta")
        temporal = self.settings.clave_temporal
        if temporal and nueva.strip().lower() == temporal.strip().lower():
            raise ClaveInvalida("No puede usar la clave temporal como su clave personal")
        validar_clave(nueva, usuario.username)
        usuario.password_hash = hash_password(nueva)
        usuario.debe_cambiar_clave = False
        usuario.accesos_clave_temporal = 0
        usuario.token_version += 1
        usuario.clave_actualizada_at = now_utc()
        usuario.clave_actualizada_por_id = usuario.id
        audit.registrar(self.db, usuario, AccionAuditoria.CLAVE_CAMBIADA, usuario)

    def estado_credencial(self, actor: Usuario, objetivo: Usuario) -> dict[str, object]:
        if not tiene_permiso(actor, Permiso.CREDENCIALES_CONSULTAR, self.settings):
            raise PermisoDenegado("Solo SUPERVISOR puede consultar credenciales")
        audit.registrar(self.db, actor, AccionAuditoria.CREDENCIAL_CONSULTADA, objetivo)
        por = objetivo.clave_actualizada_por
        return {
            "usuario_id": objetivo.id,
            "usuario": objetivo.username,
            "nombre": objetivo.nombre,
            "rol": objetivo.rol,
            "activo": objetivo.activo,
            "tiene_clave": bool(objetivo.password_hash),
            "algoritmo": ALGORITMO if objetivo.password_hash.startswith("$argon2id$") else "desconocido",
            "debe_cambiar_clave": objetivo.debe_cambiar_clave,
            "accesos_clave_temporal": objetivo.accesos_clave_temporal if objetivo.debe_cambiar_clave else None,
            "clave_actualizada_at": objetivo.clave_actualizada_at,
            "clave_actualizada_por": por.nombre if por else None,
            "ultimo_acceso_at": objetivo.ultimo_acceso_at,
        }
