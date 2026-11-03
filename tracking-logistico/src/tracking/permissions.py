"""Control de acceso basado en roles (RBAC): matriz de permisos y políticas de negocio.

Única fuente de verdad sobre *quién puede hacer qué*. Los endpoints declaran el permiso que
necesitan (``require_permission``) y los servicios aplican las reglas que dependen de los datos
(p. ej. qué rol puede crear a qué rol, o sobre qué usuario se puede restablecer una clave).

Alcance del CONDUCTOR (requisito RBAC #3): solo sus propias Remesas y Facturas, y en ellas solo
puede registrar eventos operativos (Por Entregar, entrega, devolución). No edita datos maestros,
ni su perfil, ni ve información de otros conductores.

Multi-tenant: este módulo responde *qué* puede hacer un rol; *sobre qué datos* lo resuelve
``tracking.tenancy`` (TRANSPORTISTA y CONDUCTOR solo ven y operan los de su empresa). El módulo de
Empresas (``Permiso.EMPRESAS_GESTIONAR``) es exclusivo del SUPERVISOR.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum

from fastapi import Depends, HTTPException, status

from tracking.config import Settings, get_settings
from tracking.enums import Rol
from tracking.models import Usuario
from tracking.security import get_current_user
from tracking.tenancy import TenantScope

log = logging.getLogger(__name__)


class Permiso(str, Enum):
    # --- Cuenta propia (todos los roles)
    CUENTA_PROPIA = "cuenta.propia"  # ver su sesión y cambiar su propia clave
    PERFIL_EDITAR = "perfil.editar"  # editar datos de su ficha (desactivado para conductores)

    # --- Alcance del conductor: solo lo propio
    REMESAS_PROPIAS_VER = "remesas.propias.ver"
    FACTURAS_PROPIAS_VER = "facturas.propias.ver"
    ESTATUS_OPERATIVO_REGISTRAR = "facturas.estatus_operativo.registrar"
    FOTOS_PROPIAS = "fotos.propias"

    # --- Operación (oficina)
    REMESAS_VER = "remesas.ver"
    REMESAS_REASIGNAR = "remesas.reasignar"
    # Cambiar la EMPRESA DE TRANSPORTE de una remesa y, en cascada, de todas sus facturas (solo SUPERVISOR)
    REMESAS_EMPRESA_CAMBIAR = "remesas.empresa_transporte.cambiar"
    REMESAS_ELIMINAR = "remesas.eliminar"
    FACTURAS_VER = "facturas.ver"
    FACTURAS_CORREGIR_ESTATUS = "facturas.estatus.corregir"
    FACTURAS_MONTOS_VER = "facturas.montos.ver"  # Total factura / neto, % y flete (solo SUPERVISOR y COBRANZAS)
    FOTOS_VER = "fotos.ver"
    IMPORTAR_REMESA = "remesas.importar"
    REPORTES_VER = "reportes.ver"
    EMPRESAS_VER = "empresas.ver"  # catálogo de empresas en modo lectura
    USUARIOS_VER = "usuarios.ver"  # listado de usuarios (Transportista: solo conductores de su empresa)
    USUARIOS_ACTIVAR = "usuarios.activar"  # Activo Sí/No (Transportista: solo conductores de su empresa)
    METRICAS_VER = "metricas.ver"  # métrica estrella y métricas de operación (solo SUPERVISOR)
    NOTIFICACIONES_ENVIAR = "notificaciones.enviar"
    NOTIFICACIONES_VISTA = "notificaciones.vista_previa"  # armar los correos sin enviarlos
    IMPORTACIONES_VER = "remesas.importaciones.ver"  # historial de cargas y fotos de facturas (lectura)

    # --- Catálogos
    CATALOGOS_VER = "catalogos.ver"
    CATALOGOS_GESTIONAR = "catalogos.gestionar"  # camiones, vendedores, editar/desactivar conductores
    FERIADOS_GESTIONAR = "feriados.gestionar"  # días feriados (no hábiles): solo el SUPERVISOR
    DATOS_PERSONALES_VER = "conductores.datos_personales.ver"  # cédula y teléfono

    # --- Identidades y credenciales
    CONDUCTORES_CREAR = "conductores.crear"  # se asigna según CONDUCTOR_CREATOR_ROLES
    CLAVES_CONDUCTORES_GESTIONAR = "conductores.claves.gestionar"
    USUARIOS_GESTIONAR = "usuarios.gestionar"
    CLAVES_USUARIOS_GESTIONAR = "usuarios.claves.gestionar"  # restablecer/fijar clave de cualquier usuario
    CREDENCIALES_CONSULTAR = "usuarios.credenciales.consultar"
    AUDITORIA_VER = "auditoria.ver"
    BITACORA_ARCHIVAR = "bitacora.archivar"  # sacar meses/años cerrados de la bitácora activa (solo SUPERVISOR)

    # --- Multi-tenant: catálogo de empresas de transporte (exclusivo del SUPERVISOR)
    EMPRESAS_GESTIONAR = "empresas.gestionar"


_P = Permiso
_OFICINA_LECTURA = frozenset(
    {
        _P.CUENTA_PROPIA,
        _P.REMESAS_VER,
        _P.FACTURAS_VER,
        _P.FOTOS_VER,
        _P.REPORTES_VER,
        _P.CATALOGOS_VER,
    }
)
_OPERACION = _OFICINA_LECTURA | frozenset(
    {
        _P.REMESAS_REASIGNAR,
        _P.FACTURAS_CORREGIR_ESTATUS,
        _P.IMPORTAR_REMESA,
        _P.NOTIFICACIONES_ENVIAR,
        _P.NOTIFICACIONES_VISTA,
        _P.IMPORTACIONES_VER,
        _P.CATALOGOS_GESTIONAR,
        _P.DATOS_PERSONALES_VER,
        _P.CLAVES_CONDUCTORES_GESTIONAR,
    }
)

# Matriz base (sin CONDUCTORES_CREAR, que depende de la configuración)
MATRIZ: dict[Rol, frozenset[Permiso]] = {
    Rol.SUPERVISOR: _OPERACION
    | frozenset(
        {
            _P.REMESAS_ELIMINAR,
            _P.REMESAS_EMPRESA_CAMBIAR,
            _P.USUARIOS_GESTIONAR,
            _P.CLAVES_USUARIOS_GESTIONAR,
            _P.CREDENCIALES_CONSULTAR,
            _P.AUDITORIA_VER,
            _P.EMPRESAS_GESTIONAR,
            _P.METRICAS_VER,
            _P.EMPRESAS_VER,
            _P.USUARIOS_VER,
            _P.USUARIOS_ACTIVAR,
            _P.BITACORA_ARCHIVAR,
            _P.FACTURAS_MONTOS_VER,
            _P.FERIADOS_GESTIONAR,
        }
    ),
    # Transportista: menú Usuarios acotado a los CONDUCTORES de su empresa (clave y Activo)
    Rol.TRANSPORTISTA: _OPERACION | frozenset({_P.USUARIOS_VER, _P.USUARIOS_ACTIVAR}),
    Rol.COBRANZAS: _OFICINA_LECTURA | frozenset({_P.FACTURAS_MONTOS_VER}),
    # CONSULTOR: todo el sistema, solo lectura. Ningún permiso de alta, cambio o baja; además
    # ``security.get_current_user`` rechaza cualquier escritura suya (red de seguridad).
    # La interfaz le muestra los mismos botones que al SUPERVISOR (``permisos_interfaz``): al usarlos,
    # el servidor responde MENSAJE_SOLO_CONSULTA.
    Rol.CONSULTOR: _OFICINA_LECTURA
    | frozenset(
        {
            _P.METRICAS_VER,
            _P.NOTIFICACIONES_VISTA,
            _P.IMPORTACIONES_VER,
            _P.DATOS_PERSONALES_VER,
            _P.AUDITORIA_VER,
            _P.EMPRESAS_VER,
            _P.USUARIOS_VER,
            _P.CREDENCIALES_CONSULTAR,  # estado de la clave (nunca la clave)
        }
    ),
    Rol.CONDUCTOR: frozenset(
        {
            _P.CUENTA_PROPIA,
            _P.REMESAS_PROPIAS_VER,
            _P.FACTURAS_PROPIAS_VER,
            _P.ESTATUS_OPERATIVO_REGISTRAR,
            _P.FOTOS_PROPIAS,
        }
    ),
}


class PermisoDenegado(Exception):
    """Regla de negocio de autorización incumplida (se traduce a HTTP 403)."""

    def __init__(self, mensaje: str) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje


def _rol(user_or_rol: Usuario | Rol | str) -> Rol:
    if isinstance(user_or_rol, Usuario):
        return Rol(user_or_rol.rol)
    return Rol(user_or_rol)


def permisos_de(user_or_rol: Usuario | Rol | str, settings: Settings) -> frozenset[Permiso]:
    rol = _rol(user_or_rol)
    base = MATRIZ.get(rol, frozenset())
    if rol.value in settings.conductor_creator_role_set:
        base = base | {Permiso.CONDUCTORES_CREAR}
    return base


def permisos_interfaz(user: Usuario, settings: Settings) -> frozenset[Permiso]:
    """Lo que la interfaz muestra. Igual a los permisos reales, salvo el CONSULTOR: ve todos los botones
    del SUPERVISOR (así conoce el sistema completo), pero el servidor rechaza cualquier cambio suyo.
    Los montos de las facturas no: esos solo los ven SUPERVISOR y COBRANZAS."""
    if _rol(user) == Rol.CONSULTOR:
        return permisos_de(Rol.SUPERVISOR, settings) - {Permiso.FACTURAS_MONTOS_VER}
    return permisos_de(user, settings)


def tiene_permiso(user: Usuario, permiso: Permiso, settings: Settings) -> bool:
    return permiso in permisos_de(user, settings)


def require_permission(*requeridos: Permiso, cualquiera: bool = False) -> Callable[..., Usuario]:
    """Dependencia FastAPI: exige todos los permisos (o al menos uno con ``cualquiera=True``)."""
    if not requeridos:
        raise ValueError("Debe indicar al menos un permiso")

    def _dep(user: Usuario = Depends(get_current_user), settings: Settings = Depends(get_settings)) -> Usuario:
        otorgados = permisos_de(user, settings)
        ok = any(p in otorgados for p in requeridos) if cualquiera else all(p in otorgados for p in requeridos)
        if not ok:
            log.warning(
                "acceso_denegado",
                extra={"usuario_id": user.id, "rol": user.rol, "requeridos": [p.value for p in requeridos]},
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No tiene permiso para esta acción")
        return user

    return _dep


def puede_ver_datos_personales(user: Usuario) -> bool:
    """Cédula y teléfono de conductores (permiso estático, no depende de la configuración)."""
    return Permiso.DATOS_PERSONALES_VER in MATRIZ.get(_rol(user), frozenset())


def puede_ver_montos(user: Usuario) -> bool:
    """Total factura / neto y montos asociados (%, flete): exclusivo de SUPERVISOR y COBRANZAS."""
    return Permiso.FACTURAS_MONTOS_VER in MATRIZ.get(_rol(user), frozenset())


# --------------------------------------------------------------------- políticas
def validar_creacion(actor: Usuario, rol_objetivo: Rol, settings: Settings) -> None:
    """Jerarquía de creación (requisito RBAC #1).

    * CONDUCTOR: solo los roles listados en CONDUCTOR_CREATOR_ROLES
      (por defecto SUPERVISOR y TRANSPORTISTA).
    * SUPERVISOR, TRANSPORTISTA, COBRANZAS, CONSULTOR: solo el SUPERVISOR.
    """
    if rol_objetivo == Rol.CONDUCTOR:
        if not tiene_permiso(actor, Permiso.CONDUCTORES_CREAR, settings):
            permitidos = " o ".join(sorted(r.upper() for r in settings.conductor_creator_role_set))
            raise PermisoDenegado(f"Solo {permitidos} puede crear usuarios con rol CONDUCTOR")
        return
    if not tiene_permiso(actor, Permiso.USUARIOS_GESTIONAR, settings):
        raise PermisoDenegado(f"Solo SUPERVISOR puede crear usuarios con rol {rol_objetivo.value.upper()}")


def validar_cambio_activo(actor: Usuario, objetivo: Usuario, settings: Settings) -> None:
    """Quién puede cambiar el campo Activo (Sí/No) de quién.

    * SUPERVISOR: el de cualquier usuario (menos el suyo: no puede bloquearse a sí mismo).
    * TRANSPORTISTA: solo el de usuarios CONDUCTOR de su misma empresa.
    """
    if actor.id == objetivo.id:
        raise PermisoDenegado("No puede cambiar su propio estado Activo")
    if not tiene_permiso(actor, Permiso.USUARIOS_ACTIVAR, settings):
        raise PermisoDenegado("No tiene permiso para activar o desactivar usuarios")
    if tiene_permiso(actor, Permiso.USUARIOS_GESTIONAR, settings):
        return
    if objetivo.rol != Rol.CONDUCTOR.value:
        raise PermisoDenegado("Solo puede activar o desactivar usuarios con rol Conductor")
    if not TenantScope.de_usuario(actor).permite(objetivo.empresa_id) or objetivo.empresa_id is None:
        raise PermisoDenegado("Solo puede activar o desactivar conductores de su empresa")


def validar_gestion_clave(actor: Usuario, objetivo: Usuario, settings: Settings) -> None:
    """Quién puede fijar o restablecer la clave de quién (requisito RBAC #2).

    * SUPERVISOR: la de cualquier otro usuario.
    * TRANSPORTISTA: solo la de usuarios CONDUCTOR.
    * La clave propia se cambia en /api/me/clave (exige la clave actual), nunca por la vía administrativa.
    """
    if actor.id == objetivo.id:
        raise PermisoDenegado("Para cambiar su propia clave use 'Mi clave' (se pide la clave actual)")
    if tiene_permiso(actor, Permiso.CLAVES_USUARIOS_GESTIONAR, settings):
        return
    if objetivo.rol == Rol.CONDUCTOR.value and tiene_permiso(actor, Permiso.CLAVES_CONDUCTORES_GESTIONAR, settings):
        if not TenantScope.de_usuario(actor).permite(objetivo.empresa_id):
            # Multi-tenant: un Transportista nunca toca credenciales de conductores de otra empresa
            raise PermisoDenegado("No puede gestionar la clave de un conductor de otra empresa")
        return
    raise PermisoDenegado("No puede gestionar la clave de este usuario")
