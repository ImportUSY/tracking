"""App del conductor: alcance restringido a SUS Remesas y Facturas.

Matriz de acceso del rol CONDUCTOR (ver ``tracking.permissions``):

| Recurso                                   | Acción                                  |
|-------------------------------------------|-----------------------------------------|
| Remesas propias                           | ver (sin montos)                        |
| Facturas propias                          | ver (sin montos) e historial            |
| Estatus de facturas propias               | registrar Por Entregar, No Entregado, entrega, devolución |
| Fotos de facturas propias                 | subir y ver (comprobante)               |
| Cualquier otro recurso                    | denegado (403) o inexistente (404)      |

Un recurso ajeno responde 404 (no se revela que existe).

Multi-tenant: además del filtro por conductor, toda lectura pasa por los repositorios aislados
(``get_facturas`` / ``get_remesas``), que acotan a la empresa del usuario: aunque una factura quedara
mal asignada a su conductor_id, si es de otra empresa el conductor no la ve ni la opera."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tracking.config import Settings, get_settings
from tracking.enums import Estatus, OrigenEvento, TipoEventoOperativo
from tracking.models import EventoEstatus, Factura, Foto, Remesa, Usuario
from tracking.permissions import Permiso, permisos_de, require_permission
from tracking.repositories import FacturaRepositorio, RemesaRepositorio, get_facturas, get_remesas
from tracking.schemas import (
    EventoConductorOut,
    EventoIn,
    EventoOperativoIn,
    EventoResultado,
    FacturaConductorDetalleOut,
    FacturaConductorOut,
    LoteConsumoIn,
    LoteEventosIn,
    LoteEventosOut,
    RemesaConductorDetalleOut,
    RemesaConductorOut,
)
from tracking.security import get_db
from tracking.services import consumo_datos
from tracking.services.consultas import FINALES, conteos_por_remesa, progreso
from tracking.services.photos import FotoInvalida, detectar_tipo, validar_id
from tracking.services.status import EventoRechazado, Ubicacion, aplicar_evento, puede_operar_factura
from tracking.services.timeutil import now_utc

router = APIRouter(prefix="/api", tags=["conductor"])
log = logging.getLogger(__name__)


def _conductor_id(user: Usuario) -> int:
    if user.conductor is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Su usuario no está asociado a un conductor")
    return user.conductor.id


@router.get("/conductor/facturas")
def mis_facturas(
    dias_finales: int = 3,
    user: Usuario = Depends(require_permission(Permiso.FACTURAS_PROPIAS_VER)),
    facturas: FacturaRepositorio = Depends(get_facturas),
) -> dict[str, object]:
    """Facturas abiertas del conductor + las cerradas en los últimos `dias_finales` días."""
    cid = _conductor_id(user)
    limite = now_utc() - timedelta(days=max(0, min(dias_finales, 30)))
    tiene_foto = exists().where(Foto.factura_id == Factura.id)
    stmt = facturas.acotar(
        select(Factura, Remesa.numero, Remesa.destino, tiene_foto.label("tiene_foto"))
        .join(Remesa, Remesa.id == Factura.remesa_id)
        .where(Factura.conductor_id == cid)
        .where(
            or_(
                Factura.estatus.not_in([Estatus.ENTREGADA.value, Estatus.DEVOLUCION.value]),
                Factura.estatus_at >= limite,
            )
        )
        .order_by(Remesa.numero, Factura.id)
    )
    items = [
        _factura_dto(f, rem_num, destino, bool(foto)).model_dump(mode="json")
        for f, rem_num, destino, foto in facturas.db.execute(stmt)
    ]
    return {"servidor_at": now_utc().isoformat(), "facturas": items}


def _factura_dto(
    f: Factura,
    remesa_numero: str,
    destino: str | None,
    tiene_foto: bool,
    cls: type[FacturaConductorOut] = FacturaConductorOut,
) -> FacturaConductorOut:
    return cls(
        id=f.id,
        numero=f.numero,
        pedido=f.pedido,
        cliente=f.cliente,
        empresa=f.empresa,
        bultos=f.bultos,
        comentario=f.comentario,
        observacion_entrega=f.observacion_entrega,
        estatus=f.estatus,
        estatus_at=f.estatus_at,
        tracking=f.tracking,
        remesa_id=f.remesa_id,
        remesa_numero=remesa_numero,
        destino=destino,
        tiene_foto=tiene_foto,
    )


def _remesa_dto(
    r: Remesa, conteo: dict[str, int], cls: type[RemesaConductorOut] = RemesaConductorOut
) -> RemesaConductorOut:
    total, avance = progreso(conteo)
    return cls(
        id=r.id,
        numero=r.numero,
        empresa=r.empresa,
        destino=r.destino,
        placa=r.placa,
        created_at=r.created_at,
        total_facturas=total,
        por_estatus=conteo,
        progreso=avance,
    )


def _mi_remesa(remesas: RemesaRepositorio, user: Usuario, remesa_id: int) -> Remesa:
    r = remesas.obtener(remesa_id)
    if r is None or r.conductor_id != _conductor_id(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Remesa no encontrada")
    return r


def _mi_factura(facturas: FacturaRepositorio, user: Usuario, factura_id: int) -> Factura:
    f = facturas.obtener(factura_id)
    if f is None or f.conductor_id != _conductor_id(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Factura no encontrada")
    return f


@router.get("/conductor/remesas", response_model=list[RemesaConductorOut])
def mis_remesas(
    estado: str = Query("activas", pattern="^(activas|todas)$"),
    user: Usuario = Depends(require_permission(Permiso.REMESAS_PROPIAS_VER)),
    remesas_repo: RemesaRepositorio = Depends(get_remesas),
) -> list[RemesaConductorOut]:
    """Remesas asignadas al conductor (por defecto, las que tienen facturas sin cerrar)."""
    cid = _conductor_id(user)
    db = remesas_repo.db
    stmt = remesas_repo.de_conductor(cid).order_by(Remesa.id.desc()).limit(200)
    if estado == "activas":
        abiertas = exists().where(Factura.remesa_id == Remesa.id, Factura.estatus.not_in(FINALES))
        stmt = stmt.where(abiertas)
    remesas = list(db.scalars(stmt))
    conteos = conteos_por_remesa(db, [r.id for r in remesas])
    return [_remesa_dto(r, conteos.get(r.id, {})) for r in remesas]


@router.get("/conductor/remesas/{remesa_id}", response_model=RemesaConductorDetalleOut)
def mi_remesa(
    remesa_id: int,
    user: Usuario = Depends(require_permission(Permiso.REMESAS_PROPIAS_VER, Permiso.FACTURAS_PROPIAS_VER)),
    remesas: RemesaRepositorio = Depends(get_remesas),
) -> RemesaConductorOut:
    db = remesas.db
    r = _mi_remesa(remesas, user, remesa_id)
    out = _remesa_dto(r, conteos_por_remesa(db, [r.id]).get(r.id, {}), RemesaConductorDetalleOut)
    assert isinstance(out, RemesaConductorDetalleOut)
    con_foto = set(db.scalars(select(Foto.factura_id).where(Foto.factura_id.in_([f.id for f in r.facturas]))))
    out.facturas = [_factura_dto(f, r.numero, r.destino, f.id in con_foto) for f in r.facturas]
    return out


@router.get("/conductor/facturas/{factura_id}", response_model=FacturaConductorDetalleOut)
def mi_factura(
    factura_id: int,
    user: Usuario = Depends(require_permission(Permiso.FACTURAS_PROPIAS_VER)),
    facturas: FacturaRepositorio = Depends(get_facturas),
) -> FacturaConductorOut:
    db = facturas.db
    f = _mi_factura(facturas, user, factura_id)
    tiene_foto = db.scalar(select(exists().where(Foto.factura_id == f.id))) or False
    out = _factura_dto(f, f.remesa.numero, f.remesa.destino, bool(tiene_foto), FacturaConductorDetalleOut)
    assert isinstance(out, FacturaConductorDetalleOut)
    out.eventos = [EventoConductorOut.model_validate(e) for e in sorted(f.eventos, key=lambda e: e.ocurrido_at)]
    return out


@router.post("/conductor/facturas/{factura_id}/{tipo}", response_model=EventoResultado)
def registrar_evento_operativo(
    factura_id: int,
    tipo: TipoEventoOperativo,
    body: EventoOperativoIn,
    user: Usuario = Depends(require_permission(Permiso.ESTATUS_OPERATIVO_REGISTRAR)),
    facturas: FacturaRepositorio = Depends(get_facturas),
) -> EventoResultado:
    """Registra un evento del ciclo de transporte: ``salida``, ``no_entregado``, ``entrega`` o ``devolucion``.

    El conductor no envía el estatus: lo determina el tipo de evento (Salida→Por Entregar, No
    entregado→No Entregado, Entrega→Entregada, Devolución→Devolución). Ningún otro campo se modifica.
    """
    _mi_factura(facturas, user, factura_id)
    ev = EventoIn(
        id=body.id,
        factura_id=factura_id,
        estatus=tipo.estatus,
        ocurrido_at=body.ocurrido_at,
        motivo=body.motivo,
        foto_id=body.foto_id,
        offline=body.offline,
        latitud=body.latitud,
        longitud=body.longitud,
        precision_m=body.precision_m,
    )
    return _procesar_evento(facturas, user, ev)


def _ubicacion(ev: EventoIn) -> Ubicacion | None:
    if ev.latitud is None or ev.longitud is None:
        return None
    return Ubicacion(ev.latitud, ev.longitud, ev.precision_m)


def _procesar_evento(facturas: FacturaRepositorio, user: Usuario, ev: EventoIn) -> EventoResultado:
    db = facturas.db
    factura = facturas.obtener(ev.factura_id, bloquear=True)
    if factura is None:
        return EventoResultado(
            id=ev.id, ok=False, factura_id=ev.factura_id, error="Factura no encontrada", codigo_error="no_encontrada"
        )
    try:
        r = aplicar_evento(
            db,
            evento_id=ev.id,
            factura=factura,
            nuevo=ev.estatus,
            usuario=user,
            origen=OrigenEvento.OFFLINE if ev.offline else OrigenEvento.ONLINE,
            ocurrido_at=ev.ocurrido_at,
            motivo=ev.motivo,
            foto_id=ev.foto_id.lower() if ev.foto_id else None,
            ubicacion=_ubicacion(ev),
        )
        db.commit()
    except EventoRechazado as exc:
        db.rollback()
        db.refresh(factura)
        return EventoResultado(
            id=ev.id,
            ok=False,
            factura_id=factura.id,
            estatus_actual=factura.estatus,
            estatus_at=factura.estatus_at,
            tracking=factura.tracking,
            error=exc.mensaje,
            codigo_error=exc.codigo,
        )
    except IntegrityError:
        # Mismo evento enviado dos veces en paralelo: el otro ya lo guardó.
        db.rollback()
        previo = db.get(EventoEstatus, ev.id)
        factura = facturas.obtener(ev.factura_id)
        if previo is None or factura is None:
            raise
        return EventoResultado(
            id=ev.id,
            ok=True,
            aplicado=previo.aplicado,
            duplicado=True,
            factura_id=factura.id,
            estatus_actual=factura.estatus,
            estatus_at=factura.estatus_at,
            tracking=factura.tracking,
        )
    return EventoResultado(
        id=ev.id,
        ok=True,
        aplicado=r.aplicado,
        duplicado=r.duplicado,
        factura_id=factura.id,
        estatus_actual=factura.estatus,
        estatus_at=factura.estatus_at,
        tracking=factura.tracking,
    )


@router.post("/conductor/eventos", response_model=LoteEventosOut)
def registrar_eventos(
    body: LoteEventosIn,
    user: Usuario = Depends(require_permission(Permiso.ESTATUS_OPERATIVO_REGISTRAR)),
    facturas: FacturaRepositorio = Depends(get_facturas),
) -> LoteEventosOut:
    """Aplica cambios de estatus en lote. Cada evento es independiente e idempotente por su `id`."""
    _conductor_id(user)
    resultados = [_procesar_evento(facturas, user, ev) for ev in body.eventos]
    return LoteEventosOut(resultados=resultados, servidor_at=now_utc())


@router.post("/conductor/consumo")
def registrar_consumo(
    body: LoteConsumoIn,
    user: Usuario = Depends(require_permission(Permiso.ESTATUS_OPERATIVO_REGISTRAR)),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Datos móviles (solo red celular) que midió la app del conductor. No se anota en la bitácora."""
    _conductor_id(user)
    registros = [consumo_datos.RegistroConsumo(**r.model_dump()) for r in body.registros]
    return {"registros": consumo_datos.registrar(db, user, registros)}


@router.put("/fotos/{foto_id}", status_code=status.HTTP_201_CREATED)
async def subir_foto(
    foto_id: str,
    request: Request,
    factura_id: int = Form(...),
    archivo: UploadFile = File(...),
    capturada_at: datetime | None = Form(default=None, description="Momento de la captura en el dispositivo"),
    user: Usuario = Depends(
        require_permission(Permiso.FOTOS_PROPIAS, Permiso.FACTURAS_CORREGIR_ESTATUS, cualquiera=True)
    ),
    facturas: FacturaRepositorio = Depends(get_facturas),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Sube la foto de una factura (evidencia PoD). Idempotente: reenviar el mismo id no duplica."""
    db = facturas.db
    try:
        foto_id = validar_id(foto_id)
    except FotoInvalida as exc:
        raise HTTPException(422, str(exc)) from exc
    factura = facturas.obtener(factura_id)
    if factura is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Factura no encontrada")
    if not puede_operar_factura(user, factura):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Esta factura no está asignada a usted")
    previa = db.get(Foto, foto_id)
    if previa is not None:
        if previa.factura_id != factura.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "El id de foto ya pertenece a otra factura")
        return {"id": previa.id, "duplicado": True}

    limite = settings.max_photo_mb * 1024 * 1024
    data = await archivo.read(limite + 1)
    if len(data) > limite:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"La foto supera {settings.max_photo_mb} MB")
    datos: bytes | None = None
    try:
        if settings.storage_backend == "db":
            ctype, sha, ruta, datos = detectar_tipo(data), hashlib.sha256(data).hexdigest(), "db", data
        else:
            ruta, ctype, sha = request.app.state.photos.guardar(foto_id, factura.id, data)
    except FotoInvalida as exc:
        raise HTTPException(422, str(exc)) from exc
    db.add(
        Foto(
            id=foto_id,
            factura_id=factura.id,
            ruta=ruta,
            content_type=ctype,
            bytes=len(data),
            sha256=sha,
            subido_por_id=user.id,
            datos=datos,
            capturada_at=_normalizar_captura(capturada_at),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"id": foto_id, "duplicado": True}
    log.info("foto_subida", extra={"foto_id": foto_id, "factura_id": factura.id, "bytes": len(data)})
    return {"id": foto_id, "duplicado": False}


def _normalizar_captura(capturada_at: datetime | None) -> datetime | None:
    """Hora de captura del teléfono: con zona (UTC si no trae) y nunca en el futuro."""
    if capturada_at is None:
        return None
    if capturada_at.tzinfo is None:
        capturada_at = capturada_at.replace(tzinfo=timezone.utc)
    return min(capturada_at, now_utc())


@router.get("/fotos/{foto_id}")
def ver_foto(
    foto_id: str,
    request: Request,
    user: Usuario = Depends(require_permission(Permiso.FOTOS_VER, Permiso.FOTOS_PROPIAS, cualquiera=True)),
    facturas: FacturaRepositorio = Depends(get_facturas),
    settings: Settings = Depends(get_settings),
) -> Response:
    db = facturas.db
    foto = db.get(Foto, foto_id.lower())
    # La foto hereda el inquilino de su factura: si la factura no es visible, la foto "no existe"
    factura = facturas.obtener(foto.factura_id) if foto is not None else None
    if foto is None or factura is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Foto no encontrada")
    permitido = Permiso.FOTOS_VER in permisos_de(user, settings) or puede_operar_factura(user, factura)
    if not permitido:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No tiene permiso para ver esta foto")
    cache = {"Cache-Control": "private, max-age=86400"}
    if foto.ruta == "db":
        if foto.datos is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "El archivo de la foto no está disponible")
        return Response(foto.datos, media_type=foto.content_type, headers=cache)
    path = request.app.state.photos.ruta_absoluta(foto.ruta)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "El archivo de la foto no está disponible")
    return FileResponse(path, media_type=foto.content_type, headers=cache)
