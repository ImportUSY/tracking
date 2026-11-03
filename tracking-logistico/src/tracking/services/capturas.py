"""Captura de facturas en papel: guardar imagen → OCR → revisión humana → alta de factura en una remesa.

Orquesta el pipeline (``tracking.services.ocr_facturas``) con la persistencia. Reglas:

* La imagen se guarda siempre (aunque la IA falle) como respaldo de la factura.
* Nada se da de alta sin la confirmación del operador; lo confirmado queda en ``datos_validados`` y
  lo leído por la IA en ``datos_extraidos`` (trazabilidad: se puede medir la precisión del OCR).
* La factura entra en estatus *Preparado* (``recibida``), asignada al conductor elegido, en una remesa abierta de ese
  conductor o en una remesa nueva, y con el mismo control de duplicados que la carga de REMESA.
* Multi-tenant: la captura pertenece a la empresa de quien la toma (``PerteneceAEmpresa``) y al
  confirmarse pasa a la empresa del conductor, igual que la remesa.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracking.enums import EstadoCaptura, Estatus, OrigenEvento
from tracking.models import CapturaFactura, Conductor, EventoEstatus, Factura, Remesa, Usuario, Vendedor
from tracking.services.normalize import clean_code, clean_text, norm_key
from tracking.services.ocr_facturas import (
    CAMPOS,
    CampoExtraido,
    ExtractorFacturas,
    ExtractorLocal,
    LecturaLocal,
    ResultadoExtraccion,
    revisar_coherencia,
)
from tracking.services.photos import detectar_tipo
from tracking.services.reingresos import factura_devuelta_previa
from tracking.services.remesa_parser import clave_factura
from tracking.services.timeutil import now_utc
from tracking.tenancy import TODAS_LAS_EMPRESAS, scope_de_sesion

log = logging.getLogger(__name__)

MOTOR_PENDIENTE = "lectura en curso"


def _clave_nombre(nombre: str) -> str:
    """Nombre comparable: solo letras y números ('EXCELENTE, C.A' ≈ 'EXCELENTECA')."""
    return re.sub(r"[^A-Z0-9]", "", nombre.upper())


class CapturaError(Exception):
    def __init__(self, mensaje: str, status_code: int = 422) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.status_code = status_code


class AlmacenImagenes(Protocol):
    def guardar(self, foto_id: str, factura_id: int | str, data: bytes) -> tuple[str, str, str]: ...


@dataclass(frozen=True)
class DatosFactura:
    """Datos validados por el operador (ya tipados por el DTO de entrada)."""

    numero: str
    cliente: str
    pedido: str | None = None
    empresa_emisora: str | None = None
    fecha_fac: date | None = None
    cod_vendedor: str | None = None
    bultos: int | None = None
    total_fact: Decimal | None = None
    porcentaje: Decimal | None = None
    total_neto: Decimal | None = None
    comentario: str | None = None


@dataclass(frozen=True)
class DestinoCaptura:
    conductor: Conductor
    remesa: Remesa | None  # None = crear una remesa nueva
    remesa_numero: str | None = None
    destino: str | None = None


@dataclass(frozen=True)
class ResultadoCaptura:
    captura: CapturaFactura
    factura: Factura
    remesa: Remesa
    remesa_nueva: bool


class ServicioCapturas:
    def __init__(self, db: Session, extractor: ExtractorFacturas, almacen: AlmacenImagenes | None) -> None:
        self.db = db
        self.extractor = extractor
        self.almacen = almacen  # None = imágenes en la base (STORAGE_BACKEND=db)

    # ------------------------------------------------------------ 1. capturar
    def registrar(
        self,
        imagen: bytes,
        archivo: str,
        usuario: Usuario,
        lectura: LecturaLocal | None = None,
        lectura_en_curso: bool = False,
        tomada_at: datetime | None = None,
    ) -> CapturaFactura:
        """Guarda la imagen, interpreta el OCR y deja la captura *pendiente* de revisión. Hace commit.

        ``lectura``: texto que el navegador leyó con Tesseract.js (modo gratuito).
        ``lectura_en_curso``: el navegador sigue leyendo en segundo plano y mandará el texto con
        ``aplicar_lectura`` (flujo instantáneo: la foto y el formulario no esperan al OCR).
        ``tomada_at``: hora en que el teléfono tomó la foto (será la Fecha Preparado de la factura)."""
        try:
            ctype = detectar_tipo(imagen)
        except ValueError as exc:
            raise CapturaError(str(exc)) from exc
        captura_id = str(uuid.uuid4())
        sha = hashlib.sha256(imagen).hexdigest()
        if self.almacen is None:
            ruta, datos = "db", imagen
        else:
            ruta, ctype, sha = self.almacen.guardar(captura_id, "capturas", imagen)
            datos = None

        if lectura_en_curso and lectura is None and isinstance(self.extractor, ExtractorLocal):
            resultado = ResultadoExtraccion(MOTOR_PENDIENTE, {k: CampoExtraido(None, None, None) for k in CAMPOS})
            avisos: list[str] = []
        else:
            resultado = self.extractor.extraer(imagen, ctype, lectura)
            self._ajustar_con_bd(resultado)
            avisos = self._avisos(resultado)
        scope = scope_de_sesion(self.db)
        captura = CapturaFactura(
            id=captura_id,
            empresa_id=scope.empresa_id if scope.restringido else None,
            estado=EstadoCaptura.PENDIENTE.value,
            archivo_nombre=(archivo or "factura.jpg")[:255],
            content_type=ctype,
            bytes=len(imagen),
            sha256=sha,
            ruta=ruta,
            datos=datos,
            motor=resultado.motor[:80],
            datos_extraidos=resultado.datos(),
            avisos=avisos,
            error=resultado.error,
            creado_por_id=usuario.id,
            tomada_at=momento_de_la_foto(tomada_at, now_utc()),
        )
        self.db.add(captura)
        self.db.commit()
        log.info("captura_registrada", extra={"captura_id": captura_id, "motor": resultado.motor,
                                              "avisos": len(avisos), "error": bool(resultado.error)})
        return captura

    def aplicar_lectura(self, captura: CapturaFactura, lectura: LecturaLocal) -> CapturaFactura:
        """Texto OCR que llega cuando el navegador termina de leer (puede llegar dos veces: página y
        luego zonas afinadas). Reemplaza lo extraído; lo validado por el operador vive en el formulario."""
        if captura.estado != EstadoCaptura.PENDIENTE.value:
            raise CapturaError("Esta captura ya fue confirmada o descartada", 409)
        resultado = ExtractorLocal().extraer(b"", captura.content_type, lectura)
        self._ajustar_con_bd(resultado)
        captura.motor = resultado.motor[:80]
        captura.datos_extraidos = resultado.datos()
        captura.avisos = self._avisos(resultado)
        captura.error = resultado.error
        return captura

    def analizar(self, lectura: LecturaLocal) -> ResultadoExtraccion:
        """Misma interpretación que ``aplicar_lectura`` (campos, cliente registrado, avisos de
        coherencia y duplicados) pero sin captura ni imagen: no escribe nada en la base."""
        resultado = ExtractorLocal().extraer(b"", "image/jpeg", lectura)
        self._ajustar_con_bd(resultado)
        resultado.avisos = self._avisos(resultado)
        return resultado

    def _ajustar_con_bd(self, resultado: ResultadoExtraccion) -> None:
        """Si el cliente leído se parece mucho a uno ya registrado (p. ej. 'EXCELENTECA' por
        'EXCELENTE, C.A'), se propone el nombre registrado: así no se crean clientes duplicados."""
        cliente = resultado.campos.get("cliente")
        if cliente is None or not cliente.valor:
            return
        clave = _clave_nombre(str(cliente.valor))
        conocidos: dict[str, str] = {}
        for (nombre,) in self.db.execute(select(Factura.cliente).distinct().limit(5000)):
            if nombre:
                conocidos.setdefault(_clave_nombre(nombre), nombre)
        if clave in conocidos:
            parecido: str | None = conocidos[clave]
        else:
            similares = difflib.get_close_matches(clave, list(conocidos), n=1, cutoff=0.88)
            parecido = conocidos[similares[0]] if similares else None
        if parecido and parecido != cliente.valor:
            resultado.campos["cliente"] = CampoExtraido(parecido, cliente.texto, cliente.confianza)
            resultado.avisos.append(f"Cliente leído «{cliente.valor}»: se propone el ya registrado «{parecido}».")

    def _avisos(self, resultado: ResultadoExtraccion) -> list[str]:
        return [*resultado.avisos, *revisar_coherencia(resultado.campos), *self._avisos_bd(resultado.campos)]

    def _avisos_bd(self, campos: dict[str, CampoExtraido]) -> list[str]:
        avisos: list[str] = []
        vend = campos.get("cod_vendedor")
        if vend and vend.valor and self.db.get(Vendedor, vend.valor) is None:
            avisos.append(f"El código de vendedor {vend.valor} no está en el listado de vendedores.")
        num = campos.get("numero")
        if num and num.valor:
            empresa = campos.get("empresa_emisora")
            dup = self._duplicado(str(num.valor), empresa.valor if empresa else None)
            if dup:
                avisos.append(dup)
        return avisos

    # ----------------------------------------------------------- 2. confirmar
    def confirmar(
        self, captura: CapturaFactura, datos: DatosFactura, destino: DestinoCaptura, usuario: Usuario
    ) -> ResultadoCaptura:
        """Alta de la factura (estatus Preparado) con los datos validados. No hace commit."""
        if captura.estado != EstadoCaptura.PENDIENTE.value:
            raise CapturaError("Esta captura ya fue confirmada o descartada", 409)
        numero = clean_code(datos.numero, 40)
        cliente = clean_text(datos.cliente, 255)
        if not numero or not cliente:
            raise CapturaError("El número de factura y el cliente son obligatorios")
        empresa_emisora = clean_text(datos.empresa_emisora, 120)
        dup = self._duplicado(numero, empresa_emisora)
        if dup:
            raise CapturaError(dup, 409)

        conductor = destino.conductor
        empresa_id = conductor.usuario.empresa_id if conductor.usuario else None
        remesa, nueva = destino.remesa, destino.remesa is None
        if remesa is not None:
            if remesa.conductor_id != conductor.id:
                raise CapturaError("La remesa elegida pertenece a otro conductor")
            # Una remesa no puede tener dos facturas con el mismo número (aunque sean de otra empresa emisora)
            previa = self.db.execute(
                select(Factura.id, Factura.empresa).where(Factura.remesa_id == remesa.id, Factura.numero == numero)
            ).first()
            if previa is not None:
                de = f" ({previa.empresa})" if previa.empresa else ""
                raise CapturaError(
                    f"La remesa {remesa.numero} ya tiene la factura {numero}{de}. Si es la misma factura no hace "
                    "falta registrarla otra vez; si es otra con el mismo número, revise el número leído o use "
                    "otra remesa.", 409)
        else:
            remesa = self._nueva_remesa(captura, conductor, empresa_id, empresa_emisora, destino)

        ahora = now_utc()
        # Fecha Preparado (y fecha del evento «Preparado» del historial): el momento en que se tomó la foto
        preparado = captura.tomada_at or captura.created_at or ahora
        factura = Factura(
            remesa_id=remesa.id, conductor_id=conductor.id, empresa_id=remesa.empresa_id,
            empresa=empresa_emisora or remesa.empresa, numero=numero, pedido=clean_code(datos.pedido, 40),
            cod_vendedor=clean_code(datos.cod_vendedor, 20), cliente=cliente, fecha_fac=datos.fecha_fac,
            fecha_alm=preparado,  # Fecha Entregado llega cuando el conductor marca Entregada
            bultos=datos.bultos, total_fact=datos.total_fact, porcentaje=datos.porcentaje,
            total_neto=datos.total_neto, comentario=clean_text(datos.comentario, 2000),
            estatus=Estatus.RECIBIDA.value, estatus_at=preparado,
        )
        devuelta = factura_devuelta_previa(self.db, numero, factura.empresa)  # reingreso tras devolución
        factura.reingreso_de_id = devuelta.id if devuelta else None
        self.db.add(factura)
        self.db.flush()
        nota = "Alta desde foto de factura"
        if devuelta is not None:
            nota += f" · reingreso tras devolución (remesa {devuelta.remesa.numero if devuelta.remesa else 'anterior'})"
        self.db.add(EventoEstatus(
            id=str(uuid.uuid4()), factura_id=factura.id, estatus=Estatus.RECIBIDA.value, estatus_anterior=None,
            ocurrido_at=preparado, recibido_at=ahora, usuario_id=usuario.id, origen=OrigenEvento.CAPTURA.value,
            motivo=nota, aplicado=True,
        ))
        captura.estado = EstadoCaptura.CONFIRMADA.value
        captura.empresa_id = remesa.empresa_id
        captura.datos_validados = _json_validado(datos, numero, cliente, empresa_emisora, conductor, remesa, preparado)
        captura.factura_id, captura.remesa_id = factura.id, remesa.id
        captura.confirmado_por_id, captura.confirmado_at = usuario.id, ahora
        self.db.flush()
        log.info("captura_confirmada", extra={"captura_id": captura.id, "factura_id": factura.id,
                                              "remesa_id": remesa.id, "remesa_nueva": nueva})
        return ResultadoCaptura(captura, factura, remesa, nueva)

    def descartar(self, captura: CapturaFactura) -> None:
        if captura.estado != EstadoCaptura.PENDIENTE.value:
            raise CapturaError("Solo se puede descartar una captura pendiente", 409)
        captura.estado = EstadoCaptura.DESCARTADA.value

    # ------------------------------------------------------------- internos
    def _nueva_remesa(
        self, captura: CapturaFactura, conductor: Conductor, empresa_id: int | None,
        empresa_emisora: str | None, destino: DestinoCaptura,
    ) -> Remesa:
        numero = clean_code(destino.remesa_numero, 40) or f"CF-{captura.id[:8].upper()}"
        existe = self.db.scalar(
            select(Remesa.id).where(Remesa.numero == numero).execution_options(**TODAS_LAS_EMPRESAS)
        )
        if existe is not None:
            raise CapturaError(f"Ya existe una remesa con el número {numero}: elíjala en la lista o use otro", 409)
        remesa = Remesa(
            empresa_id=empresa_id, numero=numero, numero_generado=destino.remesa_numero is None,
            empresa=empresa_emisora, destino=clean_text(destino.destino, 160), conductor_id=conductor.id,
            chofer_cedula=conductor.cedula, chofer_nombre=conductor.nombre, chofer_telefono=conductor.telefono,
            placa=conductor.placa,
        )
        self.db.add(remesa)
        self.db.flush()
        return remesa

    def _duplicado(self, numero: str, empresa_emisora: str | None) -> str | None:
        """Mismo criterio que la carga de REMESA: la factura ya está en curso (no devuelta) → no se duplica."""
        numero = clean_code(numero, 40) or numero
        filas = self.db.execute(
            select(Factura.empresa, Factura.estatus, Factura.empresa_id, Remesa.numero)
            .join(Remesa, Remesa.id == Factura.remesa_id)
            .where(Factura.numero == numero)
            .order_by(Factura.id)
            .execution_options(**TODAS_LAS_EMPRESAS)
        ).all()
        clave = clave_factura(empresa_emisora, numero)
        previas = [f for f in filas if clave_factura(f[0], numero) == clave or _misma_empresa(f[0], empresa_emisora)]
        if not previas or previas[-1][1] == Estatus.DEVOLUCION.value:
            return None
        _, estatus, empresa_id, remesa_numero = previas[-1]
        if not scope_de_sesion(self.db).permite(empresa_id):
            return f"La factura {numero} ya está asignada a otra empresa de transporte."
        return (f"La factura {numero} ya está en el sistema (remesa {remesa_numero}, "
                f"estatus {Estatus(estatus).label}).")


_PALABRAS_GENERICAS = frozenset({
    "CORP", "CORPORACION", "IMPORTADORA", "DISTRIBUIDORA", "COMERCIALIZADORA", "COMERCIAL", "INVERSIONES",
    "GRUPO", "INDUSTRIAS", "SUMINISTROS", "FERRETERIA", "CA", "SA", "SRL", "DE", "LA", "LOS", "LAS", "EL", "Y",
})


def _misma_empresa(a: str | None, b: str | None) -> bool:
    """'CORP DIJONAS' y 'CORPORACION DIJONAS, C.A.' son la misma empresa: comparten el nombre propio."""
    def propias(x: str | None) -> set[str]:
        return {w for w in re.findall(r"[A-Z0-9]{3,}", norm_key(x).upper()) if w not in _PALABRAS_GENERICAS}

    pa, pb = propias(a), propias(b)
    return bool(pa and pb and pa & pb)


def momento_de_la_foto(tomada_at: datetime | None, ahora: datetime) -> datetime:
    """Hora de la foto que informa el teléfono, acotada: nunca en el futuro ni más de 30 días atrás."""
    if tomada_at is None:
        return ahora
    if tomada_at.tzinfo is None:
        tomada_at = tomada_at.replace(tzinfo=timezone.utc)
    if tomada_at > ahora or tomada_at < ahora - timedelta(days=30):
        return ahora
    return tomada_at


def _json_validado(
    datos: DatosFactura, numero: str, cliente: str, empresa: str | None, conductor: Conductor, remesa: Remesa,
    momento: datetime,
) -> dict[str, Any]:
    def _s(v: object) -> Any:
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v) if isinstance(v, (Decimal, date)) else v

    return {
        "numero": numero, "cliente": cliente, "empresa_emisora": empresa, "pedido": clean_code(datos.pedido, 40),
        "fecha_fac": _s(datos.fecha_fac), "cod_vendedor": clean_code(datos.cod_vendedor, 20),
        "bultos": datos.bultos, "total_fact": _s(datos.total_fact), "porcentaje": _s(datos.porcentaje),
        "total_neto": _s(datos.total_neto), "comentario": datos.comentario,
        "fecha_alm": _s(momento),
        "conductor_id": conductor.id, "conductor": conductor.nombre, "remesa_id": remesa.id,
        "remesa_numero": remesa.numero,
    }
