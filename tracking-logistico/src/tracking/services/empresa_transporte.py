"""Cambio de EMPRESA DE TRANSPORTE de una remesa con actualización masiva en cascada (solo SUPERVISOR).

Invariantes del modelo multi-tenant que esta operación mantiene:

* ``factura.empresa_id == remesa.empresa_id`` para todas las facturas de la remesa (y sus capturas por foto),
  porque el aislamiento por empresa filtra cada tabla por su propia columna ``empresa_id``.
* ``remesa.empresa_id == empresa del conductor``: si la remesa cambia de empresa, su conductor (de la empresa
  anterior) ya no puede seguir asignado. O se elige un conductor de la empresa nueva, o la remesa queda sin
  conductor hasta que esa empresa la reasigne.

Las tres tablas se actualizan con sentencias ``UPDATE`` masivas dentro de la misma transacción del endpoint:
o cambian todas o no cambia ninguna.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.orm import Session

from tracking.enums import AccionAuditoria, Rol
from tracking.models import CapturaFactura, Conductor, EmpresaTransporte, Factura, Remesa, Usuario
from tracking.repositories import ConductorRepositorio
from tracking.services import audit


class CambioEmpresaInvalido(Exception):
    def __init__(self, mensaje: str, status_code: int = 422) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.status_code = status_code


@dataclass
class ResultadoCambioEmpresa:
    facturas: int
    capturas: int
    empresa_anterior: str | None
    empresa_nueva: str
    conductor: Conductor | None


def _filas(resultado: Any) -> int:
    return int(cast(CursorResult[Any], resultado).rowcount or 0)


def validar_destino(db: Session, remesa: Remesa, empresa_id: int, conductor: Conductor | None) -> EmpresaTransporte:
    empresa = db.get(EmpresaTransporte, empresa_id)
    if empresa is None:
        raise CambioEmpresaInvalido("La empresa de transporte no existe", 404)
    if not empresa.activo:
        raise CambioEmpresaInvalido(f"La empresa {empresa.nombre} está inactiva: actívela antes de asignarle remesas")
    if conductor is not None:
        if not conductor.activo:
            raise CambioEmpresaInvalido(f"El conductor {conductor.nombre} está inactivo")
        if ConductorRepositorio.empresa_de(conductor) != empresa.id:
            raise CambioEmpresaInvalido(f"El conductor {conductor.nombre} no pertenece a {empresa.nombre}")
    misma_empresa = remesa.empresa_id == empresa.id
    if misma_empresa and (conductor is None or conductor.id == remesa.conductor_id):
        raise CambioEmpresaInvalido(f"La remesa {remesa.numero} ya pertenece a {empresa.nombre}", 409)
    return empresa


def cambiar_empresa_remesa(
    db: Session, remesa: Remesa, empresa_id: int, conductor: Conductor | None, actor: Usuario
) -> ResultadoCambioEmpresa:
    """Mueve la remesa y TODAS sus facturas (y capturas por foto) a otra empresa de transporte.

    No confirma la transacción: el endpoint hace ``commit`` (o ``rollback`` si algo falla)."""
    if actor.rol != Rol.SUPERVISOR.value:  # defensa en profundidad: el endpoint ya exige el permiso
        raise CambioEmpresaInvalido("Solo el Supervisor puede cambiar la empresa de transporte", 403)
    empresa = validar_destino(db, remesa, empresa_id, conductor)
    anterior = db.get(EmpresaTransporte, remesa.empresa_id) if remesa.empresa_id else None
    chofer_anterior = remesa.chofer_nombre
    conductor_id = conductor.id if conductor else None

    # 1) Remesa: empresa y conductor (si no se eligió conductor, queda sin conductor ni datos de chofer)
    datos_chofer: dict[str, Any] = (
        {
            "chofer_cedula": conductor.cedula,
            "chofer_nombre": conductor.nombre,
            "chofer_telefono": conductor.telefono,
            "placa": conductor.placa or remesa.placa,
        }
        if conductor
        else {"chofer_cedula": None, "chofer_nombre": None, "chofer_telefono": None}
    )
    db.execute(
        update(Remesa)
        .where(Remesa.id == remesa.id)
        .values(empresa_id=empresa.id, conductor_id=conductor_id, **datos_chofer)
        .execution_options(synchronize_session=False)
    )
    # 2) Cascada: todas las facturas de la remesa
    n_facturas = _filas(
        db.execute(
            update(Factura)
            .where(Factura.remesa_id == remesa.id)
            .values(empresa_id=empresa.id, conductor_id=conductor_id)
            .execution_options(synchronize_session=False)
        )
    )
    # 3) Cascada: capturas por foto ligadas a la remesa o a sus facturas (la foto original se sigue viendo)
    ids_facturas = select(Factura.id).where(Factura.remesa_id == remesa.id).scalar_subquery()
    n_capturas = _filas(
        db.execute(
            update(CapturaFactura)
            .where(or_(CapturaFactura.remesa_id == remesa.id, CapturaFactura.factura_id.in_(ids_facturas)))
            .values(empresa_id=empresa.id)
            .execution_options(synchronize_session=False)
        )
    )
    audit.registrar(
        db,
        actor,
        AccionAuditoria.EMPRESA_TRANSPORTE_CAMBIADA,
        None,
        {
            "remesa": remesa.numero,
            "remesa_id": remesa.id,
            "empresa_anterior": anterior.nombre if anterior else None,
            "empresa_nueva": empresa.nombre,
            "conductor_anterior": chofer_anterior,
            "conductor_nuevo": conductor.nombre if conductor else None,
            "facturas": n_facturas,
        },
    )
    db.expire(remesa)  # las sentencias masivas no sincronizan los objetos ya cargados
    return ResultadoCambioEmpresa(
        n_facturas, n_capturas, anterior.nombre if anterior else None, empresa.nombre, conductor
    )
