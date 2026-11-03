"""Excel para Cobranzas, correos a vendedores/Cobranzas, Indicador de Gestión y métricas."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from tracking.config import Settings, get_settings
from tracking.enums import Rol
from tracking.models import Usuario, Vendedor
from tracking.permissions import Permiso, puede_ver_montos, require_permission, tiene_permiso
from tracking.schemas import NotificarCobranzasIn, NotificarVendedoresIn, TablaCobranzasOut
from tracking.security import MENSAJE_SOLO_CONSULTA, get_db
from tracking.services.cobranzas_excel import generar_excel_cobranzas
from tracking.services.indicador import (
    FiltroInvalido,
    FiltrosIndicador,
    filas_indicador,
)
from tracking.services.indicador import generar_excel as generar_excel_indicador
from tracking.services.indicador import opciones as opciones_de_indicador
from tracking.services.indicador import seleccion as seleccion_indicador
from tracking.services.indicador import tabla as tabla_indicador
from tracking.services.notifications import (
    CorreoNoConfigurado,
    EmailSender,
    correo_cobranzas,
    correo_vendedor,
)
from tracking.services.reportes import (
    facturas_por_vendedor,
    filtrar_cobranzas,
    metricas,
    seleccionar_para_cobranzas,
    tabla_cobranzas,
    validar_rango,
)

router = APIRouter(prefix="/api/reportes", tags=["reportes"])
log = logging.getLogger(__name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _nombre_archivo(desde: date | None, hasta: date | None) -> str:
    d, h = validar_rango(desde, hasta)
    return f"Entregas_Cobranzas_{d:%Y%m%d}" + (f"_{h:%Y%m%d}" if h != d else "") + ".xlsx"


def _titulo(desde: date | None, hasta: date | None, remesa_ids: list[int] | None) -> str:
    if remesa_ids:
        return "Entregas para Cobranzas · remesas seleccionadas"
    d, h = validar_rango(desde, hasta)
    rango = f"{d:%d/%m/%Y}" if d == h else f"del {d:%d/%m/%Y} al {h:%d/%m/%Y}"
    return f"Entregas para Cobranzas · Fecha Estatus {rango}"


@router.get("/cobranzas.xlsx")
def descargar_excel_cobranzas(
    desde: date | None = None,
    hasta: date | None = None,
    remesa_ids: list[int] | None = Query(default=None),
    solo_finales: bool = False,
    cliente: str | None = Query(default=None, max_length=120),
    conductor_id: int | None = None,
    user: Usuario = Depends(require_permission(Permiso.REPORTES_VER)),
    db: Session = Depends(get_db),
) -> Response:
    """Excel para Cobranzas. Acepta los mismos filtros que la vista en pantalla (cliente, conductor)."""
    seleccion = filtrar_cobranzas(
        seleccionar_para_cobranzas(db, remesa_ids, desde, hasta, solo_finales),
        cliente=cliente, conductor_id=conductor_id,
    )
    if not seleccion:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay facturas con Fecha Estatus en el rango indicado")
    data = generar_excel_cobranzas(seleccion, _titulo(desde, hasta, remesa_ids), con_montos=puede_ver_montos(user))
    nombre = _nombre_archivo(desde, hasta)
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@router.get("/cobranzas", response_model=TablaCobranzasOut)
def ver_cobranzas(
    desde: date | None = None,
    hasta: date | None = None,
    remesa_ids: list[int] | None = Query(default=None),
    solo_finales: bool = False,
    cliente: str | None = Query(default=None, max_length=120),
    conductor_id: int | None = None,
    orden: str | None = Query(default=None, max_length=40, description="Clave de columna (ver `columnas`)"),
    desc: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: Usuario = Depends(require_permission(Permiso.REPORTES_VER)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Vista en pantalla del Excel de Cobranzas: misma selección y mismas columnas, paginada y ordenable."""
    seleccion = filtrar_cobranzas(
        seleccionar_para_cobranzas(db, remesa_ids, desde, hasta, solo_finales),
        cliente=cliente, conductor_id=conductor_id,
    )
    return tabla_cobranzas(
        seleccion, orden=orden, desc=desc, limit=limit, offset=offset, con_montos=puede_ver_montos(user)
    )


@router.post("/notificar/vendedores")
async def notificar_vendedores(
    body: NotificarVendedoresIn,
    request: Request,
    user: Usuario = Depends(
        require_permission(Permiso.NOTIFICACIONES_ENVIAR, Permiso.NOTIFICACIONES_VISTA, cualquiera=True)
    ),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not body.simular and not tiene_permiso(user, Permiso.NOTIFICACIONES_ENVIAR, settings):
        # Vista previa sí; enviar no (p. ej. el Consultor)
        mensaje = MENSAJE_SOLO_CONSULTA if user.rol == Rol.CONSULTOR.value else "No tiene permiso para enviar correos"
        raise HTTPException(status.HTTP_403_FORBIDDEN, mensaje)
    grupos = facturas_por_vendedor(db, body.modo, body.desde, body.hasta, body.remesa_ids)
    vendedores = {v.codigo: v for v in db.scalars(select(Vendedor).where(Vendedor.codigo.in_(list(grupos))))}
    sender = EmailSender(settings)
    if not body.simular:
        try:
            sender.verificar()
        except CorreoNoConfigurado as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    enviados, sin_correo, errores, vista = [], [], [], []
    # Desglose por vendedor (todos, tengan o no correo): qué facturas recibiría cada uno
    desglose = [
        {
            "codigo": codigo,
            "nombre": vendedores[codigo].nombre if codigo in vendedores else None,
            "correo": vendedores[codigo].email if codigo in vendedores else None,
            "facturas": [
                {"numero": f.numero, "pedido": f.pedido, "cliente": f.cliente, "estatus": f.estatus,
                 "estatus_at": f.estatus_at.isoformat() if f.estatus_at else None,
                 "remesa": f.remesa.numero if f.remesa else None}
                for f in facturas
            ],
        }
        for codigo, facturas in grupos.items()
    ]
    for codigo, facturas in grupos.items():
        correo = correo_vendedor(vendedores.get(codigo), codigo, facturas, body.modo, request.app.state.ai)
        if correo is None:
            sin_correo.append(codigo)
            continue
        if body.simular:
            vista.append({"codigo": codigo, "para": correo.para, "asunto": correo.asunto, "texto": correo.texto})
            continue
        try:
            await run_in_threadpool(sender.enviar, correo)
            enviados.append(codigo)
        except Exception as exc:
            log.exception("correo_vendedor_fallo", extra={"codigo": codigo})
            errores.append({"codigo": codigo, "error": str(exc)[:200]})
    return {
        "enviados": enviados,
        "sin_correo": sin_correo,
        "errores": errores,
        "vista_previa": vista,
        "desglose": desglose,
        "vendedores": len(grupos),
        "facturas": sum(len(f) for f in grupos.values()),
    }


@router.post("/notificar/cobranzas")
async def notificar_cobranzas(
    body: NotificarCobranzasIn,
    _: Usuario = Depends(require_permission(Permiso.NOTIFICACIONES_ENVIAR)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    seleccion = seleccionar_para_cobranzas(db, body.remesa_ids, body.desde, body.hasta, body.solo_finales)
    if not seleccion:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay facturas con Fecha Estatus en el rango indicado")
    n_fact = sum(len(f) for _, f in seleccion)
    if body.simular:
        return {"enviado": False, "remesas": len(seleccion), "facturas": n_fact, "para": settings.cobranzas_email_list}
    if not settings.cobranzas_email_list:
        raise HTTPException(status.HTTP_409_CONFLICT, "Configure COBRANZAS_EMAILS")
    sender = EmailSender(settings)
    try:
        sender.verificar()
    except CorreoNoConfigurado as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    nombre = _nombre_archivo(body.desde, body.hasta)
    excel = generar_excel_cobranzas(seleccion, _titulo(body.desde, body.hasta, body.remesa_ids))
    correo = correo_cobranzas(settings.cobranzas_email_list, excel, nombre, len(seleccion), n_fact)
    try:
        await run_in_threadpool(sender.enviar, correo)
    except Exception as exc:
        log.exception("correo_cobranzas_fallo")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"No se pudo enviar el correo: {exc}") from exc
    return {"enviado": True, "remesas": len(seleccion), "facturas": n_fact, "para": settings.cobranzas_email_list}


@router.get("/metricas")
def ver_metricas(
    desde: date | None = None,
    hasta: date | None = None,
    _: Usuario = Depends(require_permission(Permiso.METRICAS_VER)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Métrica estrella y de operación: solo el Supervisor."""
    return metricas(db, desde, hasta)


# ------------------------------------------------------------------ Indicador de Gestión
def _filtros_indicador(
    desde: date | None = None,
    hasta: date | None = None,
    campo_fecha: str = Query("fecha_fac", description="fecha_fac | fecha_alm | fecha_rec | estatus_at"),
    estatus: list[str] | None = Query(default=None, description="Uno o varios: recibida, en_transito, …"),
    cliente: str | None = Query(default=None, max_length=120),
    cod_vendedor: str | None = Query(default=None, max_length=20),
    zona: str | None = Query(default=None, max_length=160, description="Destino de la remesa"),
    empresa_id: int | None = None,
    serie: str | None = Query(default=None, max_length=20,
                              description="Clic en la gráfica: recibida | en_transito | entregada (con devoluciones)"),
    transporte: str | None = Query(default=None, max_length=160,
                                   description="Clic en una barra: empresa de transporte"),
) -> FiltrosIndicador:
    return FiltrosIndicador(desde=desde, hasta=hasta, campo_fecha=campo_fecha, estatus=list(estatus or []),
                            cliente=cliente, cod_vendedor=cod_vendedor, zona=zona, empresa_id=empresa_id,
                            serie=serie or None, transporte=transporte or None)


def _filas(db: Session, filtros: FiltrosIndicador, con_montos: bool) -> list[dict[str, Any]]:
    try:
        return filas_indicador(db, filtros, con_montos)
    except FiltroInvalido as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/indicador/opciones")
def opciones_indicador(
    _: Usuario = Depends(require_permission(Permiso.REPORTES_VER)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Listas para los filtros: campos de fecha, estatus, zonas (destinos), vendedores y empresas."""
    return opciones_de_indicador(db)


@router.get("/indicador")
def ver_indicador(
    filtros: FiltrosIndicador = Depends(_filtros_indicador),
    orden: str | None = Query(default=None, max_length=40),
    desc: bool = False,
    limit: int = Query(50, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    user: Usuario = Depends(require_permission(Permiso.REPORTES_VER)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Indicador de Gestión en pantalla: tabla paginada y ordenable + sumatorias y promedios (tiempo hábil)."""
    con_montos = puede_ver_montos(user)
    try:
        out = tabla_indicador(_filas(db, filtros, con_montos), con_montos, orden, desc, limit, offset, filtros)
    except FiltroInvalido as exc:
        raise HTTPException(422, str(exc)) from exc
    out["filtro"] = filtros.descripcion()
    return out


@router.get("/indicador.xlsx")
def descargar_indicador(
    filtros: FiltrosIndicador = Depends(_filtros_indicador),
    user: Usuario = Depends(require_permission(Permiso.REPORTES_VER)),
    db: Session = Depends(get_db),
) -> Response:
    """Excel limpio con 13 columnas fijas: factura, tiempos entre procesos, vendedor, zona, chofer y empresa."""
    try:
        filas = seleccion_indicador(_filas(db, filtros, puede_ver_montos(user)), filtros)
    except FiltroInvalido as exc:
        raise HTTPException(422, str(exc)) from exc
    if not filas:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay facturas con esos filtros")
    data = generar_excel_indicador(filas)
    d, h = validar_rango(filtros.desde, filtros.hasta)
    nombre = f"Indicador_Gestion_{d:%Y%m%d}" + (f"_{h:%Y%m%d}" if h != d else "") + ".xlsx"
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{nombre}"'})
