"""Consulta pública de tracking (vendedores y clientes, sin usuario ni clave).

Expone estatus, fechas, tiempos entre procesos y datos generales; nunca montos, cédulas ni teléfonos. Cada hito
del historial trae **sus fotos** (Por Entregar, No Entregado, Entregada, Devolución…) y, cuando el estatus vigente
es de cierre, la **evidencia fotográfica (PoD)**, siempre como enlaces pre-firmados y temporales
(``tracking.services.evidencias``): no se publica el id interno de la foto ni un enlace permanente.
``PUBLIC_EVIDENCE_ENABLED=false`` vuelve al comportamiento sin fotos.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from tracking.config import Settings, get_settings
from tracking.enums import Estatus, OrigenEvento
from tracking.models import ConsultaPublica, EventoEstatus, Factura
from tracking.schemas import (
    ConsultaPublicaOut,
    EvidenciaPublicaOut,
    FotoPublicaOut,
    HitoPublico,
    TiemposProcesoOut,
    TrackingPublicoOut,
)
from tracking.security import client_ip, get_db
from tracking.services.evidencias import ServicioEvidencias
from tracking.services.feriados import fechas as fechas_feriados
from tracking.services.tiempos import dias_entre_procesos
from tracking.services.timeutil import now_utc
from tracking.services.tracking_codes import normalize_query

router = APIRouter(prefix="/api/publico", tags=["publico"])

NO_ENCONTRADO = "No encontramos esa factura, pedido o tracking."


def _evidencias(request: Request, db: Session = Depends(get_db)) -> ServicioEvidencias:
    return ServicioEvidencias(db, request.app.state.firmador_evidencias)


def _limitar(request: Request, settings: Settings) -> str:
    ip = client_ip(request, settings)
    if not request.app.state.public_limiter.hit(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demasiadas consultas. Intente en un minuto.")
    return ip


def _variantes(term: str) -> list[str]:
    """Tolera que el vendedor escriba sin prefijo: 'U00099434' -> 'FAC-U00099434'."""
    out = {term}
    if not term.startswith(("FAC-", "PED-", "TR-")):
        out.update({f"FAC-{term}", f"PED-{term}", f"TR-{term}"})
    return sorted(out)


@router.get("/tracking", response_model=ConsultaPublicaOut)
def consultar(
    request: Request,
    q: str = Query(..., min_length=3, max_length=40),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    evidencias: ServicioEvidencias = Depends(_evidencias),
) -> ConsultaPublicaOut:
    ip = _limitar(request, settings)
    term = normalize_query(q)
    variantes = _variantes(term)
    stmt = (
        select(Factura)
        .options(selectinload(Factura.remesa), selectinload(Factura.eventos), selectinload(Factura.evidencia))
        .where(or_(Factura.numero.in_(variantes), Factura.pedido.in_(variantes), Factura.tracking.in_(variantes)))
        .order_by(Factura.id.desc())
        .limit(10)
    )
    facturas = list(db.scalars(stmt))
    ip_hash = hashlib.sha256(f"{settings.secret_key[:8]}|{ip}".encode()).hexdigest()
    db.add(ConsultaPublica(termino=term[:60], encontrado=bool(facturas), ip_hash=ip_hash))
    db.commit()
    if not facturas:
        return ConsultaPublicaOut(encontrado=False, mensaje=NO_ENCONTRADO)
    resultados = []
    ahora = now_utc()
    dias_feriados = fechas_feriados(db)
    for f in facturas:
        evidencia = evidencias.para_factura(f, ahora) if settings.public_evidence_enabled else None
        historial = _historial(f, evidencia)
        if settings.public_evidence_enabled:
            _fotos_de_hitos(f, historial, evidencias, ahora)
        resultados.append(
            TrackingPublicoOut(
                numero=f.numero,
                pedido=f.pedido,
                tracking=f.tracking,
                empresa=f.empresa,
                cliente=f.cliente,
                destino=f.remesa.destino if f.remesa else None,
                estatus=Estatus(f.estatus).label,
                estatus_at=f.estatus_at,
                historial=historial,
                evidencia=evidencia,
                tiempos=TiemposProcesoOut.model_validate(
                    dias_entre_procesos(f, ahora, dias_feriados), from_attributes=True),
            )
        )
    return ConsultaPublicaOut(encontrado=True, resultados=resultados)


_ORIGEN_CONDUCTOR = (OrigenEvento.ONLINE.value, OrigenEvento.OFFLINE.value)


def _eventos_aplicados(f: Factura) -> list[EventoEstatus]:
    return [e for e in sorted(f.eventos, key=lambda e: (e.ocurrido_at, e.recibido_at)) if e.aplicado]


def _fotos_de_hitos(f: Factura, historial: list[HitoPublico], evidencias: ServicioEvidencias,
                    ahora: datetime) -> None:
    """Cada hito lleva todas sus fotos (enlaces firmados y temporales, como la evidencia PoD)."""
    por_evento = evidencias.fotos_por_evento(f)
    for e, hito in zip(_eventos_aplicados(f), historial, strict=True):
        for foto in por_evento.get(e.id, []):
            enlace = evidencias.enlace(foto, ahora)
            hito.fotos.append(FotoPublicaOut(url=enlace.url, expira_at=enlace.expira_at,
                                             capturada_at=foto.capturada_at or foto.created_at))


def _historial(f: Factura, evidencia: EvidenciaPublicaOut | None) -> list[HitoPublico]:
    """Hitos aplicados en orden; la foto PoD vigente se adjunta al hito que la registró (como en el panel)."""
    hitos: list[HitoPublico] = []
    hito_evidencia: HitoPublico | None = None
    for e in _eventos_aplicados(f):
        hito = HitoPublico(estatus=Estatus(e.estatus).label, fecha=e.ocurrido_at, con_evidencia=e.foto_id is not None)
        if e.estatus == Estatus.ENTREGADA.value and e.motivo and e.origen in _ORIGEN_CONDUCTOR:
            hito.observacion = e.motivo  # la nota de la entrega del conductor (no las correcciones internas)
        if evidencia is not None and e.foto_id is not None and e.foto_id == f.evidencia_foto_id:
            hito_evidencia = hito  # si hubiera varios con la misma foto, gana el último
        hitos.append(hito)
    if evidencia is not None and hito_evidencia is not None:
        hito_evidencia.evidencia_url = evidencia.url
        hito_evidencia.evidencia_expira_at = evidencia.expira_at
    return hitos


@router.get("/evidencias/{foto_id}", include_in_schema=True)
def ver_evidencia(
    foto_id: str,
    request: Request,
    exp: int = Query(..., ge=0),
    sig: str = Query(..., min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"),
    settings: Settings = Depends(get_settings),
    evidencias: ServicioEvidencias = Depends(_evidencias),
) -> Response:
    """Sirve la foto PoD de un enlace pre-firmado vigente. Firma inválida, vencida o revocada → 404."""
    if not settings.public_evidence_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidencia no disponible")
    _limitar(request, settings)
    ahora = now_utc()
    foto = evidencias.resolver(foto_id.lower(), exp, sig, ahora)
    if foto is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidencia no disponible o enlace vencido")
    restante = max(0, exp - int(ahora.timestamp()))
    headers = {"Cache-Control": f"private, max-age={restante}", "X-Robots-Tag": "noindex"}
    if foto.url_externa:  # object storage externo: el firmador del proveedor reemplazaría este redirect
        return RedirectResponse(foto.url_externa, status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers=headers)
    if foto.ruta == "db":
        if foto.datos is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidencia no disponible")
        return Response(foto.datos, media_type=foto.content_type, headers=headers)
    path = request.app.state.photos.ruta_absoluta(foto.ruta)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidencia no disponible")
    return FileResponse(path, media_type=foto.content_type, headers=headers)
