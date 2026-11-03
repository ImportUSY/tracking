"""CRUD de Empresas de Transporte. Exclusivo del rol SUPERVISOR (``Permiso.EMPRESAS_GESTIONAR``).

Cualquier otro rol (Transportista, Cobranzas, Conductor) recibe 403 en los cinco endpoints, incluso
en la lectura: la lista de inquilinos es información de la plataforma, no de una empresa.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from tracking.models import Usuario
from tracking.permissions import Permiso, require_permission
from tracking.schemas import EliminacionEmpresaOut, EmpresaIn, EmpresaOut, EmpresaUpdate
from tracking.security import get_db
from tracking.services.empresas import EmpresaConConteos, EmpresaInvalida, EmpresaNoEncontrada, ServicioEmpresas
from tracking.services.users import ConflictoDatos

router = APIRouter(prefix="/api/admin/empresas", tags=["empresas"])

solo_supervisor = require_permission(Permiso.EMPRESAS_GESTIONAR)
puede_ver = require_permission(Permiso.EMPRESAS_VER)  # Supervisor y Consultor (lectura)


def _servicio(db: Session = Depends(get_db)) -> ServicioEmpresas:
    return ServicioEmpresas(db)


def _out(e: EmpresaConConteos) -> EmpresaOut:
    out = EmpresaOut.model_validate(e.empresa)
    out.usuarios, out.remesas = e.usuarios, e.remesas
    return out


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, EmpresaNoEncontrada):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ConflictoDatos):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, EmpresaInvalida):
        return HTTPException(422, str(exc))
    raise exc


@router.get("", response_model=list[EmpresaOut])
def listar_empresas(
    q: str | None = Query(default=None, max_length=80),
    incluir_inactivas: bool = True,
    _: Usuario = Depends(puede_ver),
    svc: ServicioEmpresas = Depends(_servicio),
) -> list[EmpresaOut]:
    return [_out(e) for e in svc.listar(q=q, incluir_inactivas=incluir_inactivas)]


@router.get("/{empresa_id}", response_model=EmpresaOut)
def obtener_empresa(
    empresa_id: int, _: Usuario = Depends(puede_ver), svc: ServicioEmpresas = Depends(_servicio)
) -> EmpresaOut:
    try:
        return _out(svc.con_conteos(svc.obtener(empresa_id)))
    except EmpresaNoEncontrada as exc:
        raise _http(exc) from exc


@router.post("", response_model=EmpresaOut, status_code=status.HTTP_201_CREATED)
def crear_empresa(
    body: EmpresaIn,
    actor: Usuario = Depends(solo_supervisor),
    svc: ServicioEmpresas = Depends(_servicio),
) -> EmpresaOut:
    try:
        empresa = svc.crear(actor, body.model_dump())
    except (ConflictoDatos, EmpresaInvalida) as exc:
        svc.db.rollback()
        raise _http(exc) from exc
    svc.db.commit()
    return _out(EmpresaConConteos(empresa, 0, 0))


@router.patch("/{empresa_id}", response_model=EmpresaOut)
def editar_empresa(
    empresa_id: int,
    body: EmpresaUpdate,
    actor: Usuario = Depends(solo_supervisor),
    svc: ServicioEmpresas = Depends(_servicio),
) -> EmpresaOut:
    try:
        empresa = svc.actualizar(actor, empresa_id, body.model_dump(exclude_unset=True))
    except (EmpresaNoEncontrada, ConflictoDatos, EmpresaInvalida) as exc:
        svc.db.rollback()
        raise _http(exc) from exc
    svc.db.commit()
    return _out(svc.con_conteos(empresa))


@router.delete("/{empresa_id}", response_model=EliminacionEmpresaOut)
def eliminar_empresa(
    empresa_id: int,
    actor: Usuario = Depends(solo_supervisor),
    svc: ServicioEmpresas = Depends(_servicio),
) -> EliminacionEmpresaOut:
    """Si la empresa tiene usuarios, remesas o facturas se desactiva (sus usuarios pierden el acceso)."""
    try:
        eliminada = svc.eliminar(actor, empresa_id)
    except EmpresaNoEncontrada as exc:
        raise _http(exc) from exc
    svc.db.commit()
    if eliminada:
        return EliminacionEmpresaOut(eliminada=True, desactivada=False)
    return EliminacionEmpresaOut(
        eliminada=False, desactivada=True, motivo="Tiene usuarios o historial: se desactivó para conservarlo"
    )
