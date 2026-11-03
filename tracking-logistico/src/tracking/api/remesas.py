"""Consulta y gestión de remesas y facturas (Transportista, Supervisor, Cobranzas solo lectura).

Aislamiento multi-tenant: ningún endpoint construye ``select(Remesa)`` / ``select(Factura)`` a mano.
Todos parten de un repositorio inyectado (``get_remesas`` / ``get_facturas``) que ya aplica
``WHERE empresa_id = <empresa del usuario>`` a TRANSPORTISTA y CONDUCTOR. Un id de otra empresa
responde 404, igual que uno inexistente.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Exists, case, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from tracking.config import Settings, get_settings
from tracking.enums import Estatus, OrigenEvento
from tracking.models import CapturaFactura, EventoEstatus, Factura, Remesa, Usuario
from tracking.permissions import (
    Permiso,
    puede_ver_datos_personales,
    puede_ver_montos,
    require_permission,
    tiene_permiso,
)
from tracking.repositories import (
    ConductorRepositorio,
    FacturaRepositorio,
    RemesaRepositorio,
    get_conductores,
    get_facturas,
    get_remesas,
)
from tracking.schemas import (
    CambiarEmpresaTransporteIn,
    CambioEmpresaTransporteOut,
    CambioEstatusAdminIn,
    CicloFacturaOut,
    EventoOut,
    FacturaDetalleOut,
    FacturaOut,
    Pagina,
    ReasignarRemesaIn,
    RemesaDetalleOut,
    RemesaOut,
    TiemposProcesoOut,
)
from tracking.services.consultas import conteos_por_remesa
from tracking.services.empresa_transporte import CambioEmpresaInvalido, cambiar_empresa_remesa
from tracking.services.feriados import fechas as fechas_feriados
from tracking.services.status import EventoRechazado, aplicar_evento
from tracking.services.tiempos import dias_entre_procesos
from tracking.services.timeutil import local_day_bounds
from tracking.services.tracking_codes import normalize_query

router = APIRouter(prefix="/api", tags=["remesas"])

FINALES = [Estatus.ENTREGADA.value, Estatus.DEVOLUCION.value]
_F2 = aliased(Factura)


def _remesa_abierta() -> Exists:
    """EXISTS: la remesa tiene al menos una factura sin estatus final (alias explícito para correlación)."""
    return exists(select(_F2.id).where(_F2.remesa_id == Remesa.id, _F2.estatus.not_in(FINALES)).correlate(Remesa))


def _conteos(db: Session, remesa_ids: list[int]) -> dict[int, dict[str, int]]:
    return conteos_por_remesa(db, remesa_ids)


def _remesa_out(r: Remesa, conteo: dict[str, int], user: Usuario, cls: type[RemesaOut] = RemesaOut) -> RemesaOut:
    out = cls.model_validate(r)
    total = sum(conteo.values())
    finales = sum(v for k, v in conteo.items() if k in FINALES)
    out.total_facturas = total
    out.por_estatus = conteo
    out.progreso = round(finales / total, 3) if total else 0.0
    out.empresa_transporte = r.empresa_transporte.nombre if r.empresa_transporte else None
    if puede_ver_datos_personales(user):
        out.chofer_cedula = r.chofer_cedula
        out.chofer_telefono = r.chofer_telefono
    else:
        out.chofer_cedula = None
        out.chofer_telefono = None
    return out


MONTOS = ("total_fact", "porcentaje", "total_neto", "flete_pct", "total_flete")


def _factura_out(f: Factura, user: Usuario, cls: type[FacturaOut] = FacturaOut) -> FacturaOut:
    out = cls.model_validate(f)
    out.remesa_numero = f.remesa.numero if f.remesa else None
    out.conductor_nombre = f.remesa.chofer_nombre if f.remesa else None
    transporte = f.remesa.empresa_transporte if f.remesa else None
    out.empresa_transporte = transporte.nombre if transporte else None
    if not puede_ver_montos(user):
        # Total factura / neto y montos asociados: solo SUPERVISOR y COBRANZAS (no viajan a nadie más)
        for campo in MONTOS:
            setattr(out, campo, None)
        out.montos_visibles = False
    return out


@router.get("/remesas", response_model=Pagina)
def listar_remesas(
    q: str | None = None,
    estado: str = Query("todas", pattern="^(activas|completas|todas)$"),
    desde: date | None = None,
    hasta: date | None = None,
    conductor_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: Usuario = Depends(require_permission(Permiso.REMESAS_VER)),
    remesas_repo: RemesaRepositorio = Depends(get_remesas),
) -> Pagina:
    db = remesas_repo.db
    stmt = remesas_repo.consulta()
    if q:
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Remesa.numero.ilike(term),
                Remesa.chofer_nombre.ilike(term),
                Remesa.destino.ilike(term),
                Remesa.placa.ilike(term),
            )
        )
    if conductor_id:
        stmt = stmt.where(Remesa.conductor_id == conductor_id)
    ini, fin = local_day_bounds(desde, hasta)
    if ini:
        stmt = stmt.where(Remesa.created_at >= ini)
    if fin:
        stmt = stmt.where(Remesa.created_at < fin)
    abiertas = _remesa_abierta()
    if estado == "activas":
        stmt = stmt.where(abiertas)
    elif estado == "completas":
        stmt = stmt.where(~abiertas)
    total = remesas_repo.contar(stmt)
    stmt = stmt.options(selectinload(Remesa.empresa_transporte))
    remesas = list(db.scalars(stmt.order_by(Remesa.id.desc()).limit(limit).offset(offset)))
    conteos = _conteos(db, [r.id for r in remesas])
    return Pagina(total=total, items=[_remesa_out(r, conteos.get(r.id, {}), user) for r in remesas])


@router.get("/remesas/{remesa_id}", response_model=RemesaDetalleOut)
def detalle_remesa(
    remesa_id: int,
    user: Usuario = Depends(require_permission(Permiso.REMESAS_VER)),
    remesas: RemesaRepositorio = Depends(get_remesas),
) -> RemesaOut:
    db = remesas.db
    r = remesas.obtener(remesa_id, opciones=[selectinload(Remesa.facturas)])
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Remesa no encontrada")
    out = _remesa_out(r, _conteos(db, [r.id]).get(r.id, {}), user, RemesaDetalleOut)
    assert isinstance(out, RemesaDetalleOut)
    out.facturas = [_factura_out(f, user) for f in r.facturas]
    return out


@router.post("/remesas/{remesa_id}/reasignar", response_model=RemesaOut)
def reasignar_remesa(
    remesa_id: int,
    body: ReasignarRemesaIn,
    user: Usuario = Depends(require_permission(Permiso.REMESAS_REASIGNAR)),
    remesas: RemesaRepositorio = Depends(get_remesas),
    conductores: ConductorRepositorio = Depends(get_conductores),
    settings: Settings = Depends(get_settings),
) -> RemesaOut:
    """Cambia el conductor. La remesa y sus facturas pasan a la empresa del nuevo conductor
    (invariante: ``remesa.empresa_id == empresa del conductor``, si no el conductor no la vería).
    Un Transportista solo ve remesas y conductores de su empresa, así que no puede sacarlas de ella."""
    db = remesas.db
    r = remesas.obtener(remesa_id)
    c = conductores.obtener(body.conductor_id)
    if r is None or c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Remesa o conductor no encontrado")
    empresa = ConductorRepositorio.empresa_de(c)
    if remesas.scope.restringido and empresa != r.empresa_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Remesa o conductor no encontrado")
    if empresa is not None and empresa != r.empresa_id:
        # Conductor de OTRA empresa: es un cambio de empresa de transporte (solo Supervisor), en cascada
        if not tiene_permiso(user, Permiso.REMESAS_EMPRESA_CAMBIAR, settings):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo el Supervisor puede cambiar la empresa de transporte")
        try:
            cambiar_empresa_remesa(db, r, empresa, c, user)
        except CambioEmpresaInvalido as exc:
            db.rollback()
            raise HTTPException(exc.status_code, exc.mensaje) from exc
        db.commit()
        db.refresh(r)
        return _remesa_out(r, _conteos(db, [r.id]).get(r.id, {}), user)
    r.empresa_id = empresa
    r.conductor_id = c.id
    r.chofer_cedula, r.chofer_nombre, r.chofer_telefono = c.cedula, c.nombre, c.telefono
    r.placa = c.placa or r.placa
    for f in r.facturas:
        f.conductor_id = c.id
        f.empresa_id = empresa
    db.commit()
    return _remesa_out(r, _conteos(db, [r.id]).get(r.id, {}), user)


def _cambiar_empresa(
    r: Remesa, body: CambiarEmpresaTransporteIn, user: Usuario, remesas: RemesaRepositorio,
    conductores: ConductorRepositorio,
) -> CambioEmpresaTransporteOut:
    db = remesas.db
    conductor = None
    if body.conductor_id is not None:
        conductor = conductores.obtener(body.conductor_id)
        if conductor is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Conductor no encontrado")
    try:
        res = cambiar_empresa_remesa(db, r, body.empresa_id, conductor, user)
    except CambioEmpresaInvalido as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.mensaje) from exc
    db.commit()
    db.refresh(r)
    return CambioEmpresaTransporteOut(
        remesa=_remesa_out(r, _conteos(db, [r.id]).get(r.id, {}), user),
        facturas_actualizadas=res.facturas,
        capturas_actualizadas=res.capturas,
        empresa_anterior=res.empresa_anterior,
        empresa_nueva=res.empresa_nueva,
        conductor_nombre=res.conductor.nombre if res.conductor else None,
    )


@router.put("/remesas/{remesa_id}/empresa-transporte", response_model=CambioEmpresaTransporteOut)
def cambiar_empresa_transporte_remesa(
    remesa_id: int,
    body: CambiarEmpresaTransporteIn,
    user: Usuario = Depends(require_permission(Permiso.REMESAS_EMPRESA_CAMBIAR)),
    remesas: RemesaRepositorio = Depends(get_remesas),
    conductores: ConductorRepositorio = Depends(get_conductores),
) -> CambioEmpresaTransporteOut:
    """Solo SUPERVISOR. Cambia la empresa de transporte de la remesa y, en cascada (UPDATE masivo en una
    transacción), la de TODAS sus facturas y capturas. Opcional: conductor de la empresa nueva."""
    r = remesas.obtener(remesa_id, bloquear=True)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Remesa no encontrada")
    return _cambiar_empresa(r, body, user, remesas, conductores)


@router.put("/facturas/{factura_id}/empresa-transporte", response_model=CambioEmpresaTransporteOut)
def cambiar_empresa_transporte_factura(
    factura_id: int,
    body: CambiarEmpresaTransporteIn,
    user: Usuario = Depends(require_permission(Permiso.REMESAS_EMPRESA_CAMBIAR)),
    facturas: FacturaRepositorio = Depends(get_facturas),
    remesas: RemesaRepositorio = Depends(get_remesas),
    conductores: ConductorRepositorio = Depends(get_conductores),
) -> CambioEmpresaTransporteOut:
    """Igual que desde la remesa: el cambio se aplica a la remesa de la factura y a TODAS sus facturas
    (una factura no puede ir en otra empresa que su remesa)."""
    f = facturas.obtener(factura_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Factura no encontrada")
    r = remesas.obtener(f.remesa_id, bloquear=True)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Remesa no encontrada")
    return _cambiar_empresa(r, body, user, remesas, conductores)


@router.delete("/remesas/{remesa_id}", status_code=status.HTTP_204_NO_CONTENT)
def eliminar_remesa(
    remesa_id: int,
    _: Usuario = Depends(require_permission(Permiso.REMESAS_ELIMINAR)),
    remesas: RemesaRepositorio = Depends(get_remesas),
) -> None:
    """Solo Supervisor: borra una remesa cargada por error (con sus facturas e historial)."""
    r = remesas.obtener(remesa_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Remesa no encontrada")
    remesas.db.delete(r)
    remesas.db.commit()


@router.get("/facturas", response_model=Pagina)
def buscar_facturas(
    q: str | None = None,
    estatus: Estatus | None = None,
    conductor_id: int | None = None,
    cod_vendedor: str | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: Usuario = Depends(require_permission(Permiso.FACTURAS_VER)),
    facturas: FacturaRepositorio = Depends(get_facturas),
) -> Pagina:
    db = facturas.db
    stmt = facturas.consulta().options(selectinload(Factura.remesa).selectinload(Remesa.empresa_transporte))
    if q:
        term = normalize_query(q)
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Factura.numero == term,
                Factura.pedido == term,
                Factura.tracking == term,
                Factura.numero.ilike(like),
                Factura.cliente.ilike(like),
            )
        )
    if estatus:
        stmt = stmt.where(Factura.estatus == estatus.value)
    if conductor_id:
        stmt = stmt.where(Factura.conductor_id == conductor_id)
    if cod_vendedor:
        stmt = stmt.where(Factura.cod_vendedor == cod_vendedor.strip().upper())
    ini, fin = local_day_bounds(desde, hasta)
    if ini:
        stmt = stmt.where(Factura.estatus_at >= ini)
    if fin:
        stmt = stmt.where(Factura.estatus_at < fin)
    total = facturas.contar(stmt)
    rows = db.scalars(stmt.order_by(Factura.estatus_at.desc()).limit(limit).offset(offset))
    return Pagina(total=total, items=[_factura_out(f, user) for f in rows])


@router.get("/facturas/{factura_id}", response_model=FacturaDetalleOut)
def detalle_factura(
    factura_id: int,
    user: Usuario = Depends(require_permission(Permiso.FACTURAS_VER)),
    facturas: FacturaRepositorio = Depends(get_facturas),
) -> FacturaOut:
    f = facturas.obtener(
        factura_id,
        opciones=[selectinload(Factura.remesa), selectinload(Factura.eventos).selectinload(EventoEstatus.usuario)],
    )
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Factura no encontrada")
    out = _factura_out(f, user, FacturaDetalleOut)
    assert isinstance(out, FacturaDetalleOut)
    # DÍAS TRANSCURRIDOS ENTRE PROCESOS (Factura → Preparado → Por Entregar → Entregado)
    tiempos = dias_entre_procesos(f, feriados=fechas_feriados(facturas.db))
    out.tiempos = TiemposProcesoOut.model_validate(tiempos, from_attributes=True)
    eventos = []
    for e in sorted(f.eventos, key=lambda e: (e.ocurrido_at, e.recibido_at)):
        eo = EventoOut.model_validate(e)
        eo.usuario_nombre = e.usuario.nombre if e.usuario else None
        eventos.append(eo)
    out.eventos = eventos
    out.captura_id = facturas.db.scalar(
        select(CapturaFactura.id).where(CapturaFactura.factura_id == f.id).limit(1)
    )
    # Ciclos de envío (reingresos tras devolución): el historial de cada ciclo queda en su factura
    def ciclo(x: Factura) -> CicloFacturaOut:
        return CicloFacturaOut(id=x.id, remesa_numero=x.remesa.numero if x.remesa else None,
                               estatus=x.estatus, estatus_at=x.estatus_at)

    db = facturas.db
    if f.reingreso_de_id:
        anterior = db.get(Factura, f.reingreso_de_id)  # respeta el aislamiento por empresa
        out.reingreso_de = ciclo(anterior) if anterior else None
    posteriores = db.scalars(
        select(Factura).options(selectinload(Factura.remesa))
        .where(Factura.reingreso_de_id == f.id).order_by(Factura.id)
    )
    out.reingresos = [ciclo(x) for x in posteriores]
    out.habilitada_reingreso = f.estatus == Estatus.DEVOLUCION.value and not out.reingresos
    return out


@router.post("/facturas/{factura_id}/estatus", response_model=FacturaOut)
def cambiar_estatus_admin(
    factura_id: int,
    body: CambioEstatusAdminIn,
    user: Usuario = Depends(require_permission(Permiso.FACTURAS_CORREGIR_ESTATUS)),
    facturas: FacturaRepositorio = Depends(get_facturas),
) -> FacturaOut:
    """Corrección manual de estatus (queda en el historial como origen 'admin')."""
    db = facturas.db
    f = facturas.obtener(factura_id, bloquear=True)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Factura no encontrada")
    try:
        aplicar_evento(
            db,
            evento_id=str(uuid.uuid4()),
            factura=f,
            nuevo=body.estatus,
            usuario=user,
            origen=OrigenEvento.ADMIN,
            motivo=body.motivo,
        )
    except EventoRechazado as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, exc.mensaje) from exc
    db.commit()
    db.refresh(f)
    return _factura_out(f, user)


@router.get("/dashboard")
def dashboard(
    _: Usuario = Depends(require_permission(Permiso.REMESAS_VER)),
    remesas: RemesaRepositorio = Depends(get_remesas),
    facturas: FacturaRepositorio = Depends(get_facturas),
) -> dict[str, object]:
    db = remesas.db
    filas = db.execute(
        facturas.acotar(select(Factura.estatus, func.count(Factura.id)).group_by(Factura.estatus))
    ).all()
    por_estatus: dict[str, int] = {str(est): int(n) for est, n in filas}
    abiertas = _remesa_abierta()
    remesas_activas = db.scalar(remesas.acotar(select(func.count(Remesa.id)).where(abiertas))) or 0
    por_chofer = db.execute(
        remesas.acotar(
            select(
                Remesa.chofer_nombre,
                func.count(Factura.id),
                func.sum(case((Factura.estatus.in_(FINALES), 1), else_=0)),
            )
            .join(Factura, Factura.remesa_id == Remesa.id)
            .where(abiertas)
            .group_by(Remesa.chofer_nombre)
            .order_by(Remesa.chofer_nombre)
        )
    ).all()
    return {
        "por_estatus": {Estatus(k).label: int(v) for k, v in por_estatus.items()},
        "remesas_activas": int(remesas_activas),
        "choferes_en_ruta": [
            {"chofer": n or "-", "facturas": int(t), "cerradas": int(c or 0)} for n, t, c in por_chofer
        ],
    }
