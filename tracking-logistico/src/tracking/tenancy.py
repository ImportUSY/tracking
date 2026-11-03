"""Aislamiento de datos por inquilino (empresa de transporte).

Estrategia en dos capas, para que el filtro no dependa de que cada endpoint "se acuerde" de él:

1. **Explícita (repositorios)**: ``tracking.repositories`` construye toda consulta de Remesas y
   Facturas a partir de un ``TenantScope`` inyectado por FastAPI. Es la vía normal y la que se lee
   en el código de cada endpoint.
2. **Implícita (red de seguridad en la sesión ORM)**: al autenticar, ``get_current_user`` vincula el
   ``TenantScope`` del usuario a la sesión SQLAlchemy de la petición. Dos eventos de la sesión hacen
   el resto:

   * ``do_orm_execute`` añade ``with_loader_criteria(PerteneceAEmpresa, empresa_id = :empresa)`` a
     todo SELECT/UPDATE/DELETE ORM (también lazy loads, ``Session.get``, subconsultas y alias). Una
     consulta "olvidada" en un reporte o un ``db.get(Factura, id)`` ya sale filtrada.
   * ``before_flush`` impide insertar, modificar o borrar filas de otra empresa y sella
     automáticamente ``empresa_id`` en las filas nuevas.

Solo TRANSPORTISTA y CONDUCTOR quedan restringidos (``ROLES_AISLADOS``). Un usuario restringido sin
empresa no ve nada (*fail-closed*). Las consultas de sistema que deben cruzar empresas (unicidad
global de tracking, número de remesa) lo piden de forma explícita y auditable con
``.execution_options(**TODAS_LAS_EMPRESAS)``.

Este módulo no depende de FastAPI (SRP): las dependencias HTTP viven en ``tracking.repositories``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, TypeVar

from sqlalchemy import ColumnElement, Select, event, false, true
from sqlalchemy.orm import ORMExecuteState, Session, attributes, sessionmaker, with_loader_criteria

from tracking.enums import Rol
from tracking.models import PerteneceAEmpresa, Usuario

ROLES_AISLADOS: Final[frozenset[Rol]] = frozenset({Rol.TRANSPORTISTA, Rol.CONDUCTOR})

_CLAVE_SESION: Final = "tracking.tenant_scope"
_OPCION_SIN_FILTRO: Final = "tracking_todas_las_empresas"
# Uso: select(...).execution_options(**TODAS_LAS_EMPRESAS)  — solo para comprobaciones de sistema.
TODAS_LAS_EMPRESAS: Final[dict[str, Any]] = {_OPCION_SIN_FILTRO: True}

S = TypeVar("S", bound=Select[Any])


class ViolacionAislamiento(Exception):
    """Intento de leer o escribir datos de otra empresa (se traduce a HTTP 404/403)."""


@dataclass(frozen=True, slots=True)
class TenantScope:
    """Alcance de datos de quien consulta. Inmutable: se calcula una vez por petición."""

    restringido: bool
    empresa_id: int | None = None

    @classmethod
    def global_(cls) -> TenantScope:
        return cls(restringido=False)

    @classmethod
    def de_usuario(cls, user: Usuario) -> TenantScope:
        if Rol(user.rol) in ROLES_AISLADOS:
            return cls(restringido=True, empresa_id=user.empresa_id)
        return cls.global_()

    # ----------------------------------------------------------------- consultas
    def permite(self, empresa_id: int | None) -> bool:
        if not self.restringido:
            return True
        return self.empresa_id is not None and empresa_id == self.empresa_id

    def criterio(self, columna: Any) -> ColumnElement[bool]:
        """``WHERE`` a inyectar sobre una columna ``empresa_id`` (true() si el alcance es global)."""
        if not self.restringido:
            return true()
        if self.empresa_id is None:
            return false()
        return columna == self.empresa_id  # type: ignore[no-any-return]

    def restringir(self, stmt: S, modelo: type[PerteneceAEmpresa]) -> S:
        if not self.restringido:
            return stmt
        return stmt.where(self.criterio(modelo.empresa_id))

    def exigir(self, obj: PerteneceAEmpresa) -> None:
        if not self.permite(obj.empresa_id):
            raise ViolacionAislamiento(f"{type(obj).__name__} fuera del alcance de la empresa")


# ------------------------------------------------------------- sesión ORM
def vincular_sesion(db: Session, scope: TenantScope) -> None:
    """Asocia el alcance a la sesión de la petición (lo usan los eventos de abajo)."""
    db.info[_CLAVE_SESION] = scope


def scope_de_sesion(db: Session) -> TenantScope:
    scope = db.info.get(_CLAVE_SESION)
    return scope if isinstance(scope, TenantScope) else TenantScope.global_()


def _filtrar_lecturas(state: ORMExecuteState) -> None:
    scope = state.session.info.get(_CLAVE_SESION)
    if not isinstance(scope, TenantScope) or not scope.restringido:
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.is_column_load or state.execution_options.get(_OPCION_SIN_FILTRO, False):
        return  # recarga de atributos de un objeto ya autorizado / comprobación de sistema explícita
    empresa_id = scope.empresa_id
    if empresa_id is None:
        opcion = with_loader_criteria(PerteneceAEmpresa, lambda cls: false(), include_aliases=True)
    else:
        opcion = with_loader_criteria(
            PerteneceAEmpresa, lambda cls: cls.empresa_id == empresa_id, include_aliases=True
        )
    state.statement = state.statement.options(opcion)


def _validar_escrituras(session: Session, _ctx: object, _instances: object) -> None:
    scope = session.info.get(_CLAVE_SESION)
    if not isinstance(scope, TenantScope) or not scope.restringido:
        return
    for obj in session.new:
        if isinstance(obj, PerteneceAEmpresa):
            if obj.empresa_id is None:
                obj.empresa_id = scope.empresa_id  # sello automático del inquilino
            scope.exigir(obj)
    for obj in (*session.dirty, *session.deleted):
        if isinstance(obj, PerteneceAEmpresa):
            scope.exigir(obj)
            anteriores = attributes.get_history(obj, "empresa_id").deleted or ()
            if any(not scope.permite(v) for v in anteriores):
                raise ViolacionAislamiento("No se puede mover un registro desde otra empresa")


def instalar_aislamiento(fabrica: sessionmaker[Session]) -> None:
    """Registra los eventos en la fábrica de sesiones (idempotente)."""
    if not event.contains(fabrica, "do_orm_execute", _filtrar_lecturas):
        event.listen(fabrica, "do_orm_execute", _filtrar_lecturas)
    if not event.contains(fabrica, "before_flush", _validar_escrituras):
        event.listen(fabrica, "before_flush", _validar_escrituras)
