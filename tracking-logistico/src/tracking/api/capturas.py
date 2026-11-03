"""Captura de facturas en papel desde «Cargar REMESA»: foto → OCR con IA → revisión → alta.

    POST /api/capturas                     imagen (multipart) → captura pendiente con los campos leídos
    GET  /api/capturas?estado=pendiente    capturas recientes (para retomar una revisión)
    GET  /api/capturas/{id}                detalle
    GET  /api/capturas/{id}/imagen         la foto original (con sesión)
    POST /api/capturas/{id}/confirmar      datos validados + conductor + remesa → factura en «Preparado»
    POST /api/capturas/{id}/descartar

Mismo permiso que importar la REMESA (Supervisor y Transportista). Aislamiento por empresa vía
``CapturaRepositorio``; conductor y remesa se buscan con sus repositorios aislados (otra empresa → 404).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from tracking.config import Settings, get_settings
from tracking.enums import EstadoCaptura
from tracking.models import CapturaFactura, Usuario
from tracking.permissions import Permiso, require_permission
from tracking.repositories import (
    CapturaRepositorio,
    ConductorRepositorio,
    RemesaRepositorio,
    get_capturas,
    get_conductores,
    get_remesas,
)
from tracking.schemas import CampoOCROut, CapturaConfirmarIn, CapturaOut, LecturaOCRIn, ResultadoCapturaOut
from tracking.services.capturas import CapturaError, DatosFactura, DestinoCaptura, ServicioCapturas
from tracking.services.ocr_facturas import CAMPOS, CajaOCR, LecturaLocal
from tracking.services.timeutil import now_utc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/capturas", tags=["capturas"])
puede_capturar = require_permission(Permiso.IMPORTAR_REMESA)
puede_ver = require_permission(Permiso.IMPORTAR_REMESA, Permiso.IMPORTACIONES_VER, cualquiera=True)


def _servicio(request: Request, capturas: CapturaRepositorio = Depends(get_capturas)) -> ServicioCapturas:
    return ServicioCapturas(capturas.db, request.app.state.extractor_facturas, request.app.state.photos)


def _out(c: CapturaFactura) -> CapturaOut:
    out = CapturaOut.model_validate(c)
    extraidos = c.datos_extraidos or {}
    out.campos = {k: CampoOCROut.model_validate(extraidos.get(k) or {}) for k in CAMPOS}
    out.imagen_url = f"/api/capturas/{c.id}/imagen"
    return out


def _obtener(capturas: CapturaRepositorio, captura_id: str, bloquear: bool = False) -> CapturaFactura:
    c = capturas.obtener(captura_id.lower(), bloquear=bloquear)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Captura no encontrada")
    return c


_MAX_ZONAS = 20


def _lectura_local(texto: str | None, confianza: float | None, zonas: str | None) -> LecturaLocal | None:
    """Valida lo que mandó el OCR del navegador (es entrada del cliente: tamaños y tipos acotados)."""
    if not texto or not texto.strip():
        return None
    leidas: dict[str, tuple[str, float | None]] = {}
    if zonas:
        try:
            crudo = json.loads(zonas)
        except ValueError as exc:
            raise HTTPException(422, "ocr_zonas no es JSON válido") from exc
        if not isinstance(crudo, dict) or len(crudo) > _MAX_ZONAS:
            raise HTTPException(422, "ocr_zonas debe ser un objeto {campo: {texto, confianza}}")
        for campo, z in crudo.items():
            if isinstance(z, dict) and isinstance(z.get("texto"), str):
                conf = z.get("confianza")
                conf_ok = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else None
                leidas[str(campo)[:40]] = (z["texto"][:200], conf_ok)
    conf_pagina = max(0.0, min(1.0, confianza)) if confianza is not None else None
    return LecturaLocal(texto=texto[:20000], confianza=conf_pagina, zonas=leidas)


@router.post("", response_model=CapturaOut, status_code=status.HTTP_201_CREATED)
async def capturar(
    archivo: UploadFile = File(..., description="Foto de la factura (JPEG, PNG o WEBP)"),
    ocr_texto: str | None = Form(default=None, max_length=20000, description="Texto leído en el navegador"),
    ocr_confianza: float | None = Form(default=None, ge=0, le=1),
    ocr_zonas: str | None = Form(default=None, max_length=10000, description="JSON {campo: {texto, confianza}}"),
    ocr_en_curso: bool = Form(default=False, description="El navegador mandará el texto luego (POST /lectura)"),
    tomada_at: datetime | None = Form(default=None, description="Hora en que se tomó la foto (Fecha Preparado)"),
    user: Usuario = Depends(puede_capturar),
    svc: ServicioCapturas = Depends(_servicio),
    settings: Settings = Depends(get_settings),
) -> CapturaOut:
    limite = settings.max_photo_mb * 1024 * 1024
    data = await archivo.read(limite + 1)
    if len(data) > limite:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"La foto supera {settings.max_photo_mb} MB")
    lectura = _lectura_local(ocr_texto, ocr_confianza, ocr_zonas)
    try:
        # La lectura con IA es una llamada HTTP de varios segundos: fuera del event loop
        captura = await run_in_threadpool(
            svc.registrar, data, archivo.filename or "factura.jpg", user, lectura, ocr_en_curso, tomada_at
        )
    except CapturaError as exc:
        raise HTTPException(exc.status_code, exc.mensaje) from exc
    return _out(captura)


@router.get("", response_model=list[CapturaOut])
def listar(
    estado: EstadoCaptura | None = EstadoCaptura.PENDIENTE,
    limite: int = Query(20, ge=1, le=100),
    _: Usuario = Depends(puede_ver),
    capturas: CapturaRepositorio = Depends(get_capturas),
) -> list[CapturaOut]:
    stmt = capturas.consulta().order_by(CapturaFactura.created_at.desc()).limit(limite)
    if estado is not None:
        stmt = stmt.where(CapturaFactura.estado == estado.value)
    return [_out(c) for c in capturas.db.scalars(stmt)]


@router.get("/{captura_id}", response_model=CapturaOut)
def detalle(
    captura_id: str, _: Usuario = Depends(puede_ver), capturas: CapturaRepositorio = Depends(get_capturas)
) -> CapturaOut:
    return _out(_obtener(capturas, captura_id))


@router.get("/{captura_id}/imagen")
def imagen(
    captura_id: str,
    request: Request,
    _: Usuario = Depends(require_permission(Permiso.IMPORTAR_REMESA, Permiso.FACTURAS_VER, cualquiera=True)),
    capturas: CapturaRepositorio = Depends(get_capturas),
) -> Response:
    c = _obtener(capturas, captura_id)
    headers = {"Cache-Control": "private, max-age=86400"}
    if c.url_externa:
        return RedirectResponse(c.url_externa, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    if c.ruta == "db":
        if c.datos is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Imagen no disponible")
        return Response(c.datos, media_type=c.content_type, headers=headers)
    path = request.app.state.photos.ruta_absoluta(c.ruta)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Imagen no disponible")
    return FileResponse(path, media_type=c.content_type, headers=headers)


@router.post("/{captura_id}/confirmar", response_model=ResultadoCapturaOut)
def confirmar(
    captura_id: str,
    body: CapturaConfirmarIn,
    user: Usuario = Depends(puede_capturar),
    capturas: CapturaRepositorio = Depends(get_capturas),
    conductores: ConductorRepositorio = Depends(get_conductores),
    remesas: RemesaRepositorio = Depends(get_remesas),
    svc: ServicioCapturas = Depends(_servicio),
) -> ResultadoCapturaOut:
    captura = _obtener(capturas, captura_id, bloquear=True)
    conductor = conductores.obtener(body.conductor_id)
    if conductor is None or not conductor.activo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conductor no encontrado o inactivo")
    remesa = None
    if body.remesa_id is not None:
        remesa = remesas.obtener(body.remesa_id)
        if remesa is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Remesa no encontrada")
    datos = DatosFactura(**body.model_dump(include=set(DatosFactura.__dataclass_fields__)))
    try:
        r = svc.confirmar(
            captura, datos, DestinoCaptura(conductor, remesa, body.remesa_numero, body.destino), user
        )
        svc.db.commit()
    except CapturaError as exc:
        svc.db.rollback()
        raise HTTPException(exc.status_code, exc.mensaje) from exc
    except IntegrityError as exc:  # carrera con otra carga simultánea: conflicto explicado, no «error interno»
        svc.db.rollback()
        log.warning("captura_conflicto", extra={"captura_id": captura_id, "error": str(exc.orig)[:200]})
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No se pudo registrar: ya existe un dato igual (misma factura o mismo número de remesa). "
            "Actualice la página y revise.",
        ) from exc
    return ResultadoCapturaOut(
        captura_id=r.captura.id, factura_id=r.factura.id, factura_numero=r.factura.numero,
        remesa_id=r.remesa.id, remesa_numero=r.remesa.numero, remesa_nueva=r.remesa_nueva,
        conductor=conductor.nombre,
    )


@router.post("/analizar", response_model=CapturaOut)
def analizar_lectura(
    body: LecturaOCRIn,
    _: Usuario = Depends(puede_ver),
    svc: ServicioCapturas = Depends(_servicio),
) -> CapturaOut:
    """Interpreta lo que el navegador leyó de la foto (campos + avisos) **sin guardar** la foto ni la
    captura. Lo usa el rol Consultor: ve toda la extracción, pero no puede registrar la factura."""
    try:
        resultado = svc.analizar(_lectura_de(body))
    finally:
        svc.db.rollback()  # garantía: nada de este análisis se escribe
    return CapturaOut(
        id="", estado="analisis", archivo_nombre="", content_type="", bytes=0, motor=resultado.motor,
        created_at=now_utc(), error=resultado.error, avisos=resultado.avisos,
        campos={k: CampoOCROut.model_validate(resultado.datos().get(k) or {}) for k in CAMPOS},
    )


def _lectura_de(body: LecturaOCRIn) -> LecturaLocal:
    return LecturaLocal(
        texto=body.texto, confianza=body.confianza,
        zonas={k[:40]: (z.texto, z.confianza) for k, z in body.zonas.items()},
        cajas=tuple(CajaOCR(c.texto, c.confianza, c.x, c.y, c.w, c.h) for c in body.cajas),
    )


@router.post("/{captura_id}/lectura", response_model=CapturaOut)
def aplicar_lectura(
    captura_id: str,
    body: LecturaOCRIn,
    _: Usuario = Depends(puede_capturar),
    capturas: CapturaRepositorio = Depends(get_capturas),
    svc: ServicioCapturas = Depends(_servicio),
) -> CapturaOut:
    """Resultado del OCR hecho en segundo plano en el dispositivo (flujo instantáneo)."""
    lectura = _lectura_de(body)
    try:
        captura = svc.aplicar_lectura(_obtener(capturas, captura_id, bloquear=True), lectura)
    except CapturaError as exc:
        svc.db.rollback()
        raise HTTPException(exc.status_code, exc.mensaje) from exc
    svc.db.commit()
    return _out(captura)


@router.post("/{captura_id}/descartar", status_code=status.HTTP_204_NO_CONTENT)
def descartar(
    captura_id: str,
    _: Usuario = Depends(puede_capturar),
    capturas: CapturaRepositorio = Depends(get_capturas),
    svc: ServicioCapturas = Depends(_servicio),
) -> None:
    try:
        svc.descartar(_obtener(capturas, captura_id))
    except CapturaError as exc:
        raise HTTPException(exc.status_code, exc.mensaje) from exc
    svc.db.commit()
