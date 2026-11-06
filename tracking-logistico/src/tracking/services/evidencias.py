"""Proof of Delivery (PoD): enlaces temporales firmados a la foto de evidencia de una factura.

El almacenamiento actual es el disco del servidor o la propia base (Vercel), así que no hay un
bucket que firme URLs. Se implementa el mismo contrato que una URL pre-firmada de S3:

    GET /api/publico/evidencias/{foto_id}?exp=<epoch>&sig=<HMAC-SHA256(foto_id.exp)>

* Sin sesión: lo abre un vendedor o cliente desde la consulta pública.
* Caduca (``EVIDENCE_URL_TTL_MINUTES``) y no se puede alterar sin invalidar la firma.
* Revocable: al servirla se comprueba que la foto siga siendo pública (evidencia vigente de un cierre o
  foto de un hito del historial que no quedó por delante del estatus actual); si Transporte retrocede el
  estatus, el enlace de las fotos de ese hito deja de funcionar aunque no haya caducado.

``FirmadorEvidencias`` es la abstracción (DIP/OCP): para migrar las fotos a S3/GCS basta otra
implementación que devuelva la URL pre-firmada del proveedor a partir de ``Foto.url_externa``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracking.enums import Estatus
from tracking.models import Factura, Foto
from tracking.schemas import EvidenciaPublicaOut

RUTA_PUBLICA = "/api/publico/evidencias"


@dataclass(frozen=True)
class EnlaceFirmado:
    url: str
    expira_at: datetime


class FirmadorEvidencias(Protocol):
    def firmar(self, foto: Foto, ahora: datetime) -> EnlaceFirmado: ...

    def verificar(self, foto_id: str, exp: int, firma: str, ahora: datetime) -> bool: ...


class FirmadorHmac:
    """URLs pre-firmadas con HMAC-SHA256 sobre ``foto_id.exp`` (clave derivada de SECRET_KEY)."""

    def __init__(self, secret_key: str, ttl: timedelta, ruta: str = RUTA_PUBLICA) -> None:
        if not secret_key:
            raise ValueError("Se requiere SECRET_KEY para firmar enlaces de evidencia")
        # Clave separada de la del JWT: una firma de evidencia nunca sirve como token ni al revés.
        self._clave = hashlib.sha256(b"tracking:evidencias:v1|" + secret_key.encode()).digest()
        self._ttl = ttl
        self._ruta = ruta.rstrip("/")

    def _firma(self, foto_id: str, exp: int) -> str:
        return hmac.new(self._clave, f"{foto_id}.{exp}".encode(), hashlib.sha256).hexdigest()

    def firmar(self, foto: Foto, ahora: datetime) -> EnlaceFirmado:
        # Ventanas alineadas: el mismo enlace dentro de la ventana (el navegador lo cachea)
        paso = max(60, int(self._ttl.total_seconds()) // 2)
        exp = (int(ahora.timestamp()) // paso) * paso + int(self._ttl.total_seconds())
        url = f"{self._ruta}/{foto.id}?exp={exp}&sig={self._firma(foto.id, exp)}"
        return EnlaceFirmado(url=url, expira_at=datetime.fromtimestamp(exp, tz=timezone.utc))

    def verificar(self, foto_id: str, exp: int, firma: str, ahora: datetime) -> bool:
        if exp < int(ahora.timestamp()):
            return False
        return hmac.compare_digest(self._firma(foto_id, exp), firma)


FINALES = (Estatus.ENTREGADA.value, Estatus.DEVOLUCION.value)


class ServicioEvidencias:
    """Decide qué evidencia es pública y la resuelve para servirla."""

    def __init__(self, db: Session, firmador: FirmadorEvidencias) -> None:
        self.db = db
        self.firmador = firmador

    @staticmethod
    def es_publicable(factura: Factura) -> bool:
        return factura.estatus in FINALES and factura.evidencia_foto_id is not None

    def para_factura(self, factura: Factura, ahora: datetime) -> EvidenciaPublicaOut | None:
        if not self.es_publicable(factura) or factura.evidencia is None:
            return None
        foto = factura.evidencia
        enlace = self.firmador.firmar(foto, ahora)
        return EvidenciaPublicaOut(
            estatus=Estatus(factura.estatus).label,
            url=enlace.url,
            expira_at=enlace.expira_at,
            content_type=foto.content_type,
            capturada_at=foto.capturada_at or foto.created_at,
        )

    def fotos_por_evento(self, factura: Factura) -> dict[str, list[Foto]]:
        """Fotos públicas de cada hito del historial (id del evento → fotos, en orden de captura).

        Solo de eventos aplicados cuyo estatus no quedó por delante del actual: si Transporte retrocede
        el estatus (p. ej. Entregada por error → Por Entregar), las fotos de ese cierre dejan de publicarse.
        """
        actual = Estatus(factura.estatus).orden
        eventos = {e.id: e for e in factura.eventos if e.aplicado and Estatus(e.estatus).orden <= actual}
        principal = {e.foto_id: e.id for e in eventos.values() if e.foto_id}
        fotos = self.db.scalars(
            select(Foto).where(Foto.factura_id == factura.id)
            .order_by(func.coalesce(Foto.capturada_at, Foto.created_at), Foto.id))
        salida: dict[str, list[Foto]] = {}
        for foto in fotos:
            evento_id = foto.evento_id if foto.evento_id in eventos else principal.get(foto.id)
            if evento_id is not None:
                salida.setdefault(evento_id, []).append(foto)
        return salida

    def enlace(self, foto: Foto, ahora: datetime) -> EnlaceFirmado:
        return self.firmador.firmar(foto, ahora)

    def resolver(self, foto_id: str, exp: int, firma: str, ahora: datetime) -> Foto | None:
        """Foto a servir si la firma es válida y sigue siendo pública (evidencia vigente o foto de un hito)."""
        if not self.firmador.verificar(foto_id, exp, firma, ahora):
            return None
        foto = self.db.get(Foto, foto_id)
        if foto is None:
            return None
        factura = self.db.get(Factura, foto.factura_id)
        if factura is None:
            return None
        if self.es_publicable(factura) and factura.evidencia_foto_id == foto_id:
            return foto
        publicas = {x.id for fotos in self.fotos_por_evento(factura).values() for x in fotos}
        return foto if foto_id in publicas else None
