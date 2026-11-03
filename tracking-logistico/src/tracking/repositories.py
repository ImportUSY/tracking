"""Repositorios con aislamiento por inquilino + proveedores de inyección de dependencias (FastAPI).

Los endpoints de Remesas, Facturas, Importaciones y Conductores no construyen ``select(...)`` a mano:
piden un repositorio con ``Depends(get_facturas)``. El repositorio nace ligado al ``TenantScope``
del usuario autenticado y **toda** consulta parte de ``consulta()``, que ya trae el
``WHERE empresa_id = :empresa`` para TRANSPORTISTA y CONDUCTOR. Así el filtro se escribe una vez
(DRY), es imposible de omitir por descuido y se puede probar de forma aislada.

* SRP: el repositorio solo sabe *leer con alcance*; la política (quién es restringido) está en
  ``tracking.tenancy`` y las reglas de negocio en ``tracking.services``.
* OCP/LSP: una entidad aislada nueva = una subclase de ``RepositorioAislado`` de 3 líneas.
* DIP: los endpoints dependen de la abstracción inyectada, no de la sesión ni del rol.

Un recurso de otra empresa se comporta como inexistente (``None`` → HTTP 404): no se revela que existe.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from fastapi import Depends
from sqlalchemy import Select, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.base import ExecutableOption

from tracking.models import CapturaFactura, Conductor, Factura, Importacion, PerteneceAEmpresa, Remesa, Usuario
from tracking.security import get_current_user, get_db
from tracking.tenancy import TenantScope

M = TypeVar("M", bound=PerteneceAEmpresa)


class RepositorioAislado(Generic[M]):
    """Acceso de lectura acotado al inquilino para una entidad ``PerteneceAEmpresa``."""

    def __init__(self, db: Session, scope: TenantScope, modelo: type[M], pk: InstrumentedAttribute[Any]) -> None:
        self.db = db
        self.scope = scope
        self._modelo = modelo
        self._pk = pk

    def consulta(self) -> Select[tuple[M]]:
        """Punto de partida de TODA consulta de la entidad (ya filtrado por empresa)."""
        return self.scope.restringir(select(self._modelo), self._modelo)

    def acotar(self, stmt: Select[Any]) -> Select[Any]:
        """Aplica el filtro de empresa a una consulta ad hoc (agregados, columnas sueltas)."""
        return self.scope.restringir(stmt, self._modelo)

    def obtener(
        self, ident: Any, *, opciones: Sequence[ExecutableOption] = (), bloquear: bool = False
    ) -> M | None:
        stmt = self.consulta().where(self._pk == ident).options(*opciones)
        if bloquear:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def contar(self, stmt: Select[Any]) -> int:
        return int(self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)

    def exigir(self, obj: M) -> None:
        """Verificación explícita antes de escribir (lanza ``ViolacionAislamiento``)."""
        self.scope.exigir(obj)


class RemesaRepositorio(RepositorioAislado[Remesa]):
    def __init__(self, db: Session, scope: TenantScope) -> None:
        super().__init__(db, scope, Remesa, Remesa.id)

    def de_conductor(self, conductor_id: int) -> Select[tuple[Remesa]]:
        return self.consulta().where(Remesa.conductor_id == conductor_id)


class FacturaRepositorio(RepositorioAislado[Factura]):
    def __init__(self, db: Session, scope: TenantScope) -> None:
        super().__init__(db, scope, Factura, Factura.id)

    def de_conductor(self, conductor_id: int) -> Select[tuple[Factura]]:
        return self.consulta().where(Factura.conductor_id == conductor_id)


class ImportacionRepositorio(RepositorioAislado[Importacion]):
    def __init__(self, db: Session, scope: TenantScope) -> None:
        super().__init__(db, scope, Importacion, Importacion.id)


class CapturaRepositorio(RepositorioAislado[CapturaFactura]):
    def __init__(self, db: Session, scope: TenantScope) -> None:
        super().__init__(db, scope, CapturaFactura, CapturaFactura.id)


class ConductorRepositorio:
    """Conductores visibles para el actor: los de su empresa (vía ``usuarios.empresa_id``)."""

    def __init__(self, db: Session, scope: TenantScope) -> None:
        self.db = db
        self.scope = scope

    def consulta(self) -> Select[tuple[Conductor]]:
        stmt = select(Conductor)
        if self.scope.restringido:
            stmt = stmt.where(Conductor.usuario.has(self.scope.criterio(Usuario.empresa_id)))
        return stmt

    def obtener(self, conductor_id: int) -> Conductor | None:
        return self.db.scalar(self.consulta().where(Conductor.id == conductor_id))

    @staticmethod
    def empresa_de(conductor: Conductor) -> int | None:
        return conductor.usuario.empresa_id if conductor.usuario is not None else None


# ------------------------------------------------------------ inyección FastAPI
def get_tenant_scope(user: Usuario = Depends(get_current_user)) -> TenantScope:
    return TenantScope.de_usuario(user)


def get_remesas(db: Session = Depends(get_db), scope: TenantScope = Depends(get_tenant_scope)) -> RemesaRepositorio:
    return RemesaRepositorio(db, scope)


def get_facturas(db: Session = Depends(get_db), scope: TenantScope = Depends(get_tenant_scope)) -> FacturaRepositorio:
    return FacturaRepositorio(db, scope)


def get_importaciones(
    db: Session = Depends(get_db), scope: TenantScope = Depends(get_tenant_scope)
) -> ImportacionRepositorio:
    return ImportacionRepositorio(db, scope)


def get_capturas(db: Session = Depends(get_db), scope: TenantScope = Depends(get_tenant_scope)) -> CapturaRepositorio:
    return CapturaRepositorio(db, scope)


def get_conductores(
    db: Session = Depends(get_db), scope: TenantScope = Depends(get_tenant_scope)
) -> ConductorRepositorio:
    return ConductorRepositorio(db, scope)
