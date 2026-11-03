"""Máquina de estados de la factura y aplicación idempotente de eventos (online/offline).

Reglas (decisiones documentadas para los TBD del PRD):

* Flujo: Preparado -> Por Entregar -> Entregada | Devolución (valores internos ``recibida`` y
  ``en_transito``). Se permite saltar pasos hacia adelante (p.ej. Preparado -> Entregada).
  El estatus «Cargado» ya no existe.
* Todo evento queda en el historial con la hora en que ocurrió (dispositivo) y la hora en que llegó.
* Un evento que *avanza* la factura siempre se aplica, aunque llegue tarde desde el modo sin señal.
* Un evento que no avanza:
    - si es más antiguo que el estatus vigente -> se guarda en el historial sin aplicarse;
    - si es más reciente: entre Entregada y Devolución gana el último cambio (regla del PRD);
      un retroceso (p.ej. Entregada -> Por Entregar) solo lo puede hacer Supervisor/Transportista.
* Devolución exige una foto previamente subida para esa factura.
* El número de tracking nace al salir de Preparado.
* El id del evento lo genera el dispositivo (UUID): reenviarlo no duplica nada.
* Proof of Delivery: al aplicarse un estatus de cierre (Entregada / Devolución) con foto, la foto
  queda como ``factura.evidencia_foto_id``; si el estatus vuelve a uno no final, la evidencia se retira.
* Multi-tenant: nadie restringido (Transportista, Conductor) opera facturas de otra empresa.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from tracking.enums import ESTATUS_OPERATIVOS, Estatus, OrigenEvento, Rol
from tracking.models import EventoEstatus, Factura, Foto, Usuario
from tracking.permissions import MATRIZ, Permiso
from tracking.services.timeutil import now_utc
from tracking.services.tracking_codes import ensure_tracking
from tracking.tenancy import TenantScope

log = logging.getLogger(__name__)

MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_OFFLINE_AGE = timedelta(days=30)


class EventoRechazado(Exception):
    def __init__(self, codigo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


@dataclass
class ResultadoEvento:
    evento: EventoEstatus
    aplicado: bool
    duplicado: bool


def _normalizar_ocurrido(ocurrido_at: datetime | None, ahora: datetime) -> datetime:
    if ocurrido_at is None:
        return ahora
    if ocurrido_at.tzinfo is None:
        # El dispositivo envía ISO con zona; si no la trae asumimos UTC.
        ocurrido_at = ocurrido_at.replace(tzinfo=timezone.utc)
    if ocurrido_at > ahora + MAX_CLOCK_SKEW:
        return ahora  # reloj del teléfono adelantado
    if ocurrido_at < ahora - MAX_OFFLINE_AGE:
        raise EventoRechazado("invalido", "El cambio es demasiado antiguo para sincronizarse")
    return ocurrido_at


def puede_operar_factura(user: Usuario, factura: Factura) -> bool:
    """Oficina con permiso de corrección: cualquier factura de su alcance. Conductor: solo las suyas."""
    if not TenantScope.de_usuario(user).permite(factura.empresa_id):
        return False
    permisos = MATRIZ.get(Rol(user.rol), frozenset())
    if Permiso.FACTURAS_CORREGIR_ESTATUS in permisos:
        return True
    if Permiso.ESTATUS_OPERATIVO_REGISTRAR in permisos:
        return user.conductor is not None and factura.conductor_id == user.conductor.id
    return False


def _actualizar_evidencia(factura: Factura, nuevo: Estatus, foto_id: str | None, cuando: datetime) -> None:
    """Mantiene el puntero PoD coherente con el estatus que se acaba de aplicar.

    Solo se llama cuando el estatus cambia, así que un cierre sin foto (p. ej. Entregada sin
    comprobante, o una corrección Devolución → Entregada) nunca arrastra la foto de otro estatus.
    """
    if nuevo.es_final and foto_id is not None:
        factura.evidencia_foto_id, factura.evidencia_at = foto_id, cuando
    else:
        factura.evidencia_foto_id, factura.evidencia_at = None, None


def _actualizar_observacion(factura: Factura, nuevo: Estatus, motivo: str | None) -> None:
    """Observación de la entrega vigente: la del evento que la dejó Entregada (en el historial quedan todas)."""
    factura.observacion_entrega = motivo if nuevo == Estatus.ENTREGADA and motivo else None


def _actualizar_fechas_etapa(factura: Factura, nuevo: Estatus, cuando: datetime, aplicado: bool) -> None:
    """Fecha Por Entregar (``fecha_tra``) y Fecha Entregado (``fecha_rec``): fecha y hora del evento.

    * Al pasar a Por Entregar se guarda el momento de ese evento (el que marcó el conductor, aunque
      llegue después sin señal).
    * Al pasar a Entregada se guarda la Fecha Entregado. Si se saltó Por Entregar (Preparado →
      Entregada), esa etapa toma el mismo momento: la mercancía tuvo que salir para entregarse.
    * Devolución: la mercancía salió (Fecha Por Entregar) pero no se entregó: sin Fecha Entregado.
    * Si Transporte retrocede el estatus, se borran las fechas de las etapas que quedan por delante.
    * Un «Por Entregar» que llega tarde (sin conexión) y no cambia el estatus corrige la Fecha Por
      Entregar cuando esa etapa ya quedó atrás (la fecha real reemplaza la deducida).
    """
    if not aplicado:
        ya_paso = nuevo == Estatus.EN_TRANSITO and Estatus(factura.estatus).orden > nuevo.orden
        if ya_paso and (factura.fecha_tra is None or cuando < factura.fecha_tra):
            factura.fecha_tra = cuando
        return
    if nuevo == Estatus.RECIBIDA:
        factura.fecha_tra = None
    elif nuevo == Estatus.EN_TRANSITO:
        factura.fecha_tra = cuando
    else:  # Entregada / Devolución
        factura.fecha_tra = factura.fecha_tra or cuando
    factura.fecha_rec = cuando if nuevo == Estatus.ENTREGADA else None


def aplicar_evento(
    db: Session,
    *,
    evento_id: str,
    factura: Factura,
    nuevo: Estatus,
    usuario: Usuario,
    origen: OrigenEvento,
    ocurrido_at: datetime | None = None,
    motivo: str | None = None,
    foto_id: str | None = None,
) -> ResultadoEvento:
    """Aplica un cambio de estatus. No hace commit (el llamador controla la transacción)."""
    existente = db.get(EventoEstatus, evento_id)
    if existente is not None:
        if existente.factura_id != factura.id:
            raise EventoRechazado("invalido", "El id del evento ya fue usado para otra factura")
        return ResultadoEvento(existente, existente.aplicado, duplicado=True)

    if not puede_operar_factura(usuario, factura):
        raise EventoRechazado("sin_permiso", "Esta factura no está asignada a usted")

    ahora = now_utc()
    cuando = _normalizar_ocurrido(ocurrido_at, ahora)
    actual = Estatus(factura.estatus)
    es_admin = origen == OrigenEvento.ADMIN

    if not es_admin and nuevo not in ESTATUS_OPERATIVOS:
        # El conductor solo registra eventos operativos: Por Entregar, entrega, devolución
        raise EventoRechazado("invalido", f"El conductor no puede asignar el estatus «{nuevo.label}»")

    if foto_id is not None:
        foto = db.get(Foto, foto_id)
        if foto is None:
            raise EventoRechazado("foto_pendiente", "La foto aún no ha llegado al servidor")
        if foto.factura_id != factura.id:
            raise EventoRechazado("invalido", "La foto pertenece a otra factura")
    if nuevo == Estatus.DEVOLUCION and foto_id is None and not es_admin:
        raise EventoRechazado("invalido", "La devolución requiere foto de la factura con el motivo")

    if es_admin:
        if nuevo.orden < actual.orden and not (motivo and motivo.strip()):
            raise EventoRechazado("invalido", "Para retroceder un estatus indique el motivo")
        aplicar = nuevo != actual
    elif nuevo.orden > actual.orden:
        aplicar = True
    elif cuando < factura.estatus_at:
        aplicar = False  # llegó tarde: solo historial
    elif nuevo == actual:
        aplicar = False  # repetido (selección en lote): no cambia nada
    elif actual.es_final and nuevo.es_final:
        aplicar = True  # Entregada <-> Devolución: gana el último cambio
    else:
        raise EventoRechazado(
            "conflicto",
            f"La factura ya está en «{actual.label}» y no puede volver a «{nuevo.label}». "
            "Pida la corrección a Transporte.",
        )

    evento = EventoEstatus(
        id=evento_id,
        factura_id=factura.id,
        estatus=nuevo.value,
        estatus_anterior=actual.value,
        ocurrido_at=cuando,
        recibido_at=ahora,
        usuario_id=usuario.id,
        origen=origen.value,
        motivo=(motivo or "").strip() or None,
        foto_id=foto_id,
        aplicado=aplicar,
    )
    db.add(evento)

    _actualizar_fechas_etapa(factura, nuevo, cuando if not es_admin else ahora, aplicar)
    if aplicar:
        factura.estatus = nuevo.value
        factura.estatus_at = cuando if not es_admin else ahora
        factura.version = (factura.version or 0) + 1
        _actualizar_evidencia(factura, nuevo, foto_id, cuando)
        _actualizar_observacion(factura, nuevo, (motivo or "").strip() or None)
        if nuevo != Estatus.RECIBIDA:
            ensure_tracking(db, factura)
    db.flush()
    log.info(
        "evento_estatus",
        extra={
            "evento_id": evento_id,
            "factura_id": factura.id,
            "de": actual.value,
            "a": nuevo.value,
            "aplicado": aplicar,
            "origen": origen.value,
            "usuario_id": usuario.id,
        },
    )
    return ResultadoEvento(evento, aplicar, duplicado=False)
