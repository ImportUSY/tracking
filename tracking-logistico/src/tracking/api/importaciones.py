"""Carga de la hoja REMESA: vista previa -> confirmación (o descarte)."""

from __future__ import annotations

import json
import zlib
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from tracking.config import Settings, get_settings
from tracking.enums import Rol
from tracking.models import Importacion, Usuario
from tracking.permissions import Permiso, PermisoDenegado, require_permission, tiene_permiso
from tracking.repositories import ImportacionRepositorio, get_importaciones
from tracking.schemas import (
    ConfirmarImportacionIn,
    CredencialTemporal,
    FilasExcelIn,
    ImportacionOut,
    ResultadoConfirmacionOut,
)
from tracking.security import get_db
from tracking.services.accounts import DatosInvalidos, ServicioCuentas
from tracking.services.importer import ImportacionError, Importador
from tracking.services.remesa_parser import FiltroFechaFac, FormatoNoReconocido

router = APIRouter(prefix="/api/importaciones", tags=["importaciones"])

EXCEL_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroEnabled.12",
    "application/octet-stream",
    "",
}


async def leer_excel(archivo: UploadFile, max_mb: int) -> bytes:
    nombre = (archivo.filename or "").lower()
    if not nombre.endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "El archivo debe ser Excel (.xlsx o .xlsm)")
    limite = max_mb * 1024 * 1024
    data = await archivo.read(limite + 1)
    if len(data) > limite:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"El archivo supera {max_mb} MB")
    if not data.startswith(b"PK"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "El archivo no es un Excel válido")
    return data


def filtro_fecha_fac(desde: date | None, hasta: date | None) -> FiltroFechaFac | None:
    """Rango de «Fecha Fac» elegido en Cargar REMESA (ambas fechas incluidas). Sin fechas = toda la hoja."""
    try:
        filtro = FiltroFechaFac(desde, hasta)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return filtro if filtro.activo else None


MAX_DESCOMPRIMIDO = 120 * 1024 * 1024  # protección contra "bombas" gzip


async def leer_json(request: Request, max_mb: int) -> Any:
    """Lee un cuerpo JSON, opcionalmente comprimido con gzip por el navegador (CompressionStream)."""
    limite = max_mb * 1024 * 1024
    cuerpo = bytearray()
    async for chunk in request.stream():
        cuerpo.extend(chunk)
        if len(cuerpo) > limite:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Los datos superan {max_mb} MB")
    data = bytes(cuerpo)
    if data[:2] == b"\x1f\x8b":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = d.decompress(data, MAX_DESCOMPRIMIDO)
        if d.unconsumed_tail:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Datos demasiado grandes")
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "Datos inválidos") from exc


@router.post("/filas", response_model=ImportacionOut, status_code=status.HTTP_201_CREATED)
async def vista_previa_filas(
    request: Request,
    user: Usuario = Depends(require_permission(Permiso.IMPORTAR_REMESA)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Importacion:
    """Igual que subir el Excel, pero con las filas ya extraídas en el navegador (ver xlsx-lite.js)."""
    try:
        body = FilasExcelIn.model_validate(await leer_json(request, settings.max_excel_mb))
    except ValidationError as exc:
        raise HTTPException(422, "Formato de filas inválido") from exc
    importador = Importador(db, request.app.state.ai)
    try:
        filtro = filtro_fecha_fac(body.fecha_fac_desde, body.fecha_fac_hasta)
        return await run_in_threadpool(
            importador.vista_previa_filas, body.filas, body.hoja, body.hojas, body.archivo, user, body.sha256,
            True, filtro,
        )
    except FormatoNoReconocido as exc:
        raise HTTPException(422, str(exc)) from exc


# --- Solo análisis (rol Consultor): mismo resumen que la vista previa, sin guardar nada ------------
puede_analizar = require_permission(Permiso.IMPORTAR_REMESA, Permiso.IMPORTACIONES_VER, cualquiera=True)


@router.post("/analizar", response_model=ImportacionOut)
async def analizar_filas(
    request: Request,
    user: Usuario = Depends(puede_analizar),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Importacion:
    """Analiza la hoja de remesas (filas extraídas en el navegador) sin registrar nada."""
    try:
        body = FilasExcelIn.model_validate(await leer_json(request, settings.max_excel_mb))
    except ValidationError as exc:
        raise HTTPException(422, "Formato de filas inválido") from exc
    importador = Importador(db, request.app.state.ai)
    try:
        filtro = filtro_fecha_fac(body.fecha_fac_desde, body.fecha_fac_hasta)
        return await run_in_threadpool(
            importador.vista_previa_filas, body.filas, body.hoja, body.hojas, body.archivo, user, body.sha256, False,
            filtro,
        )
    except FormatoNoReconocido as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        db.rollback()  # garantía: nada de este análisis se escribe


@router.post("/analizar-archivo", response_model=ImportacionOut)
async def analizar_archivo(
    request: Request,
    archivo: UploadFile = File(...),
    hoja: str | None = Form(default=None),
    fecha_fac_desde: date | None = Form(default=None),
    fecha_fac_hasta: date | None = Form(default=None),
    user: Usuario = Depends(puede_analizar),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Importacion:
    """Igual que ``/analizar`` pero con el archivo completo (navegadores que no leen el Excel)."""
    filtro = filtro_fecha_fac(fecha_fac_desde, fecha_fac_hasta)
    data = await leer_excel(archivo, settings.max_excel_mb)
    importador = Importador(db, request.app.state.ai)
    try:
        return await run_in_threadpool(
            importador.vista_previa, data, archivo.filename or "remesa.xlsx", user, hoja or None, False, filtro
        )
    except FormatoNoReconocido as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        db.rollback()


@router.post("", response_model=ImportacionOut, status_code=status.HTTP_201_CREATED)
async def vista_previa(
    request: Request,
    archivo: UploadFile = File(...),
    hoja: str | None = Form(default=None),
    fecha_fac_desde: date | None = Form(default=None),
    fecha_fac_hasta: date | None = Form(default=None),
    user: Usuario = Depends(require_permission(Permiso.IMPORTAR_REMESA)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Importacion:
    filtro = filtro_fecha_fac(fecha_fac_desde, fecha_fac_hasta)
    data = await leer_excel(archivo, settings.max_excel_mb)
    importador = Importador(db, request.app.state.ai)
    try:
        # El parseo es CPU/IO intensivo: fuera del event loop
        return await run_in_threadpool(
            importador.vista_previa, data, archivo.filename or "remesa.xlsx", user, hoja or None, True, filtro
        )
    except FormatoNoReconocido as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
def listar(
    limite: int = 20,
    _: Usuario = Depends(require_permission(Permiso.IMPORTAR_REMESA, Permiso.IMPORTACIONES_VER, cualquiera=True)),
    importaciones: ImportacionRepositorio = Depends(get_importaciones),
) -> list[dict[str, Any]]:
    """Historial liviano (sin el detalle completo del resumen). Transportista: solo el de su empresa."""
    db = importaciones.db
    stmt = importaciones.consulta().order_by(Importacion.id.desc()).limit(min(max(limite, 1), 100))
    return [
        {
            "id": i.id,
            "archivo_nombre": i.archivo_nombre,
            "hoja": i.hoja,
            "estado": i.estado,
            "created_at": i.created_at,
            "confirmado_at": i.confirmado_at,
            "totales": (i.resumen or {}).get("totales", {}),
            "filtro_fecha_fac": ((i.resumen or {}).get("filtro_fecha_fac") or {}).get("texto"),
            "resultado": i.resultado,
        }
        for i in db.scalars(stmt)
    ]


@router.get("/{importacion_id}", response_model=ImportacionOut)
def obtener(
    importacion_id: int,
    _: Usuario = Depends(require_permission(Permiso.IMPORTAR_REMESA, Permiso.IMPORTACIONES_VER, cualquiera=True)),
    importaciones: ImportacionRepositorio = Depends(get_importaciones),
) -> Importacion:
    imp = importaciones.obtener(importacion_id)
    if imp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Importación no encontrada")
    return imp


@router.post("/{importacion_id}/confirmar", response_model=ResultadoConfirmacionOut)
def confirmar(
    importacion_id: int,
    body: ConfirmarImportacionIn,
    user: Usuario = Depends(require_permission(Permiso.IMPORTAR_REMESA)),
    importaciones: ImportacionRepositorio = Depends(get_importaciones),
    settings: Settings = Depends(get_settings),
) -> ResultadoConfirmacionOut:
    db = importaciones.db
    empresa_choferes: int | None = None
    if body.registrar_choferes:
        if not tiene_permiso(user, Permiso.CONDUCTORES_CREAR, settings):
            # Registrar choferes nuevos = crear usuarios CONDUCTOR: misma jerarquía de creación
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Su rol no puede crear usuarios CONDUCTOR")
        try:
            empresa_choferes = ServicioCuentas(db, settings).resolver_empresa(user, Rol.CONDUCTOR, body.empresa_id)
        except PermisoDenegado as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, exc.mensaje) from exc
        except DatosInvalidos as exc:
            raise HTTPException(422, str(exc)) from exc
    imp = importaciones.obtener(importacion_id, bloquear=True)
    if imp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Importación no encontrada")
    try:
        r = Importador(db).confirmar(
            imp, user, body.remesas, body.fecha_desde, body.registrar_choferes, empresa_choferes
        )
    except ImportacionError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.mensaje) from exc
    if r["credenciales"]:
        ServicioCuentas(db, settings).registrar_conductores_importados(user, [c["usuario"] for c in r["credenciales"]])
        db.commit()
    return ResultadoConfirmacionOut(
        importacion_id=r["importacion_id"],
        remesas_creadas=r["remesas_creadas"],
        remesas_actualizadas=r["remesas_actualizadas"],
        facturas_creadas=r["facturas_creadas"],
        facturas_omitidas=r["facturas_omitidas"],
        remesas_excluidas=r["remesas_excluidas"],
        choferes_registrados=[CredencialTemporal(**c) for c in r["credenciales"]],
    )


@router.post("/{importacion_id}/descartar", status_code=status.HTTP_204_NO_CONTENT)
def descartar(
    importacion_id: int,
    _: Usuario = Depends(require_permission(Permiso.IMPORTAR_REMESA)),
    importaciones: ImportacionRepositorio = Depends(get_importaciones),
) -> None:
    imp = importaciones.obtener(importacion_id)
    if imp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Importación no encontrada")
    try:
        Importador(importaciones.db).descartar(imp)
    except ImportacionError as exc:
        raise HTTPException(exc.status_code, exc.mensaje) from exc
