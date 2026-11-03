"""Pipeline OCR de facturas en papel: imagen → extracción (IA de visión) → normalización → validación.

Etapas (cada una pura y probable por separado):

1. **Extracción** (``ExtractorFacturas``), dos motores intercambiables:

   * ``ExtractorLocal`` (por defecto, **gratis**): el OCR lo hace el propio navegador con PaddleOCR
     (modelos PP-OCRv3 en ONNX Runtime Web, ``web/ocr-local.js``), sin enviar la imagen a terceros ni
     pagar nada. El teléfono manda cada frase leída con su posición (*cajas*) y el texto por renglones.
     Aquí ``parsear_cajas`` busca cada dato junto a su etiqueta («FACTURA» → número debajo,
     «Subtotal» → monto a la derecha…) y ``parsear_texto_factura`` completa con expresiones regulares
     tolerantes a errores de OCR (letras por números, coma decimal perdida, 6 leído como 8 en el año).
   * ``ExtractorClaudeVision`` (opcional, de pago): un modelo de visión lee la foto con salida
     estructurada. Solo se usa si hay ``ANTHROPIC_API_KEY``; si falla, cae al texto local.
2. **Normalización** (``normalizar``): convierte el texto a tipos del dominio con reglas
   deterministas: montos venezolanos ("$ 1.090,9800" → 1090.98), fechas DD-MM-AAAA, códigos en
   mayúsculas, porcentaje como fracción (5,00 % → 0.05), razón social sin sufijo legal.
3. **Coherencia** (``revisar_coherencia``): reglas de negocio que marcan campos dudosos (neto ≠ total
   × (1 − descuento), fecha futura, faltan campos clave). No corrige: avisa.

La IA nunca escribe en la base: el operador revisa y confirma (``tracking.services.capturas``).
"""

from __future__ import annotations

import base64
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from tracking.services.ai import ClaudeClient
from tracking.services.normalize import clean_code, clean_text
from tracking.services.timeutil import today_local

log = logging.getLogger(__name__)

# Campos que se extraen (clave interna → descripción para el modelo y para la interfaz)
CAMPOS: Final[dict[str, str]] = {
    "numero": "Número de factura",
    "pedido": "Número de pedido",
    "cliente": "Cliente",
    "fecha_fac": "Fecha de la factura",
    "cod_vendedor": "Código de vendedor",
    "bultos": "Número de bultos",
    "total_fact": "Total de la factura (Subtotal)",
    "porcentaje": "Descuento (%)",
    "total_neto": "Total neto (Base imponible)",
    "empresa_emisora": "Empresa emisora",
}
UMBRAL_DUDOSO: Final = 0.8

_PROMPT_SISTEMA = """Eres un sistema de captura de datos de facturas fiscales venezolanas (formato "FORMA LIBRE").
Lee la foto y devuelve el TEXTO EXACTO impreso en cada campo, sin reformatear ni calcular nada.
Si un campo no existe o no se lee con claridad, devuelve null y confianza baja. Nunca inventes valores.

Ubicación de cada campo en este modelo de factura:
- empresa_emisora: razón social del encabezado, junto al logotipo (ej. "IMPORTADORA USY, C.A.").
- cliente: razón social escrita a continuación de la etiqueta "CLIENTE:" en el bloque del cliente
  (ej. "COMERCIAL SUPER EXCELENTE, C.A"). No es el código de cliente ni el RIF.
- numero_factura: número bajo la etiqueta "FACTURA" en la columna derecha (debajo de "Serie").
  NO es el "Nº CONTROL" (ej. 00-308057) ni el código de cliente.
- pedido: valor tras "Nro. Orden de Pedido" (formato PED-…).
- fecha: valor tras "Fecha:" en la columna derecha (formato DD-MM-AAAA). Devuélvelo tal cual.
- cod_vendedor: código tras "VENDEDOR:" (formato LETRAS-NÚMEROS, ej. "BO-07001"); no el nombre del cobrador.
- bultos: número tras "# Bultos" al pie de la factura.
- subtotal: monto de la línea "Subtotal" del cuadro de totales (antes de descuento e IVA).
- descuento_pct: porcentaje de la línea "Descuento" (ej. "5,00%").
- base_imponible: monto de la línea "Base Imponible".
Los montos usan coma decimal y punto de miles (ej. "$ 1.090,9800"): cópialos con sus símbolos."""

_CAMPO_TEXTO = {"type": ["string", "null"]}
_TOOL: Final[dict[str, Any]] = {
    "name": "registrar_factura",
    "description": "Registra los campos leídos de la foto de una factura, con la confianza de cada lectura.",
    "input_schema": {
        "type": "object",
        "properties": {
            "es_factura": {"type": "boolean", "description": "false si la imagen no es una factura legible"},
            "empresa_emisora": _CAMPO_TEXTO,
            "cliente": _CAMPO_TEXTO,
            "numero_factura": _CAMPO_TEXTO,
            "pedido": _CAMPO_TEXTO,
            "fecha": _CAMPO_TEXTO,
            "cod_vendedor": _CAMPO_TEXTO,
            "bultos": _CAMPO_TEXTO,
            "subtotal": _CAMPO_TEXTO,
            "descuento_pct": _CAMPO_TEXTO,
            "base_imponible": _CAMPO_TEXTO,
            "confianza": {
                "type": "object",
                "description": "Confianza 0..1 de cada campo (mismas claves que arriba)",
                "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "observaciones": {"type": ["string", "null"], "description": "Tachones, sellos, partes cortadas…"},
        },
        "required": ["es_factura", "numero_factura", "cliente", "confianza"],
    },
}
# Clave del modelo → clave interna
_MAPA_MODELO: Final[dict[str, str]] = {
    "numero_factura": "numero",
    "pedido": "pedido",
    "cliente": "cliente",
    "fecha": "fecha_fac",
    "cod_vendedor": "cod_vendedor",
    "bultos": "bultos",
    "subtotal": "total_fact",
    "descuento_pct": "porcentaje",
    "base_imponible": "total_neto",
    "empresa_emisora": "empresa_emisora",
}


@dataclass(frozen=True)
class CampoExtraido:
    valor: Any  # ya normalizado y serializable a JSON (str / int / número en texto / fecha ISO)
    texto: str | None  # como lo leyó el modelo
    confianza: float | None

    @property
    def dudoso(self) -> bool:
        return self.valor is None or (self.confianza is not None and self.confianza < UMBRAL_DUDOSO)

    def to_dict(self) -> dict[str, Any]:
        return {"valor": self.valor, "texto": self.texto, "confianza": self.confianza, "dudoso": self.dudoso}


@dataclass
class ResultadoExtraccion:
    motor: str
    campos: dict[str, CampoExtraido] = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)
    error: str | None = None

    def datos(self) -> dict[str, Any]:
        return {k: c.to_dict() for k, c in self.campos.items()}


@dataclass(frozen=True)
class LecturaLocal:
    """Resultado del OCR hecho en el navegador (PaddleOCR con ONNX Runtime Web)."""

    texto: str
    confianza: float | None = None  # 0..1, media de la página
    zonas: dict[str, tuple[str, float | None]] = field(default_factory=dict)  # campo → (texto, confianza 0..1)
    cajas: tuple[CajaOCR, ...] = ()  # frases con su posición en la foto (para buscar cada dato junto a su etiqueta)


class ExtractorFacturas(Protocol):
    @property
    def nombre(self) -> str: ...

    def extraer(
        self, imagen: bytes, content_type: str, lectura: LecturaLocal | None = None
    ) -> ResultadoExtraccion: ...


# ------------------------------------------------------------------ normalización
def parse_monto(texto: str | None) -> Decimal | None:
    """'$ 1.090,9800' → 1090.98 · '940,5000' → 940.5 · '990.00' → 990.00 · '1,090.98' → 1090.98."""
    if not texto:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(texto))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1:  # '1.090.980' → miles con punto · '1.077.6000' → 4 decimales (punto mal leído)
        ultimo = s.rfind(".")
        s = s[:ultimo].replace(".", "") + ("." if len(s) - ultimo - 1 in (2, 4) else "") + s[ultimo + 1:]
    try:
        valor = Decimal(s)
    except InvalidOperation:
        return None
    return valor.quantize(Decimal("0.0001"))


def parse_porcentaje(texto: str | None) -> Decimal | None:
    """'5,00%' → 0.05 (como en la columna % de la REMESA). Acepta '0,05' como fracción."""
    v = parse_monto(texto)
    if v is None:
        return None
    frac = v / 100 if v > 1 or "%" in str(texto) else v
    return frac.quantize(Decimal("0.0001")) if Decimal(0) <= frac <= 1 else None


def parse_fecha(texto: str | None) -> date | None:
    """DD-MM-AAAA, DD/MM/AAAA, DD.MM.AA o AAAA-MM-DD."""
    if not texto:
        return None
    s = str(texto).strip()
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
        if m:
            anio = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(anio, int(m[2]), int(m[1]))
    except ValueError:
        return None
    return None


def parse_entero(texto: str | None) -> int | None:
    m = re.search(r"\d+", str(texto or ""))
    return int(m.group(0)) if m else None


_SUFIJO_LEGAL = re.compile(r"[\s,]*\b(C\.?\s?A\.?|S\.?\s?A\.?|S\.?R\.?L\.?)\s*$", re.I)


def normalizar_empresa(texto: str | None) -> str | None:
    """'IMPORTADORA USY, C.A.' → 'IMPORTADORA USY' (así viene en la columna Empresa de la REMESA)."""
    t = clean_text(texto, 120)
    if not t:
        return None
    return _SUFIJO_LEGAL.sub("", t.upper()).strip(" ,.") or None


def normalizar(crudo: dict[str, Any]) -> tuple[dict[str, CampoExtraido], list[str]]:
    """Respuesta del modelo → campos tipados (JSON-safe) + avisos de lectura."""
    conf_cruda = crudo.get("confianza")
    confianzas: dict[str, Any] = conf_cruda if isinstance(conf_cruda, dict) else {}
    avisos: list[str] = []
    campos: dict[str, CampoExtraido] = {}
    for clave_modelo, clave in _MAPA_MODELO.items():
        texto = crudo.get(clave_modelo)
        texto = str(texto).strip() if texto not in (None, "") else None
        valor: Any
        if clave == "total_fact" or clave == "total_neto":
            d = parse_monto(texto)
            valor = str(d) if d is not None else None
        elif clave == "porcentaje":
            d = parse_porcentaje(texto)
            valor = str(d) if d is not None else None
        elif clave == "fecha_fac":
            f = parse_fecha(texto)
            valor = f.isoformat() if f else None
        elif clave == "bultos":
            valor = parse_entero(texto)
        elif clave == "empresa_emisora":
            valor = normalizar_empresa(texto)
        elif clave == "cliente":
            valor = clean_text(texto, 255)
        else:  # numero, pedido, cod_vendedor
            valor = clean_code(texto, 40)
        conf = confianzas.get(clave_modelo)
        confianza = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else None
        if texto and valor is None:
            avisos.append(f"{CAMPOS[clave]}: no se pudo interpretar «{texto}»")
        campos[clave] = CampoExtraido(valor=valor, texto=texto, confianza=confianza)
    if crudo.get("es_factura") is False:
        avisos.insert(0, "La imagen no parece una factura legible: revise la foto o transcriba los datos.")
    if crudo.get("observaciones"):
        avisos.append(f"Observación de la lectura: {crudo['observaciones']}")
    return campos, avisos


def revisar_coherencia(campos: dict[str, CampoExtraido], hoy: date | None = None) -> list[str]:
    """Reglas de negocio sobre los valores (no modifican nada; marcan para revisión)."""
    hoy = hoy or today_local()
    avisos: list[str] = []
    for clave in ("numero", "cliente"):
        if campos.get(clave) is None or campos[clave].valor is None:
            avisos.append(f"Falta {CAMPOS[clave].lower()}: es obligatorio.")
    total = _dec(campos, "total_fact")
    neto = _dec(campos, "total_neto")
    pct = _dec(campos, "porcentaje")
    if total is not None and neto is not None:
        esperado = total * (1 - (pct or Decimal(0)))
        if abs(esperado - neto) > Decimal("0.05"):
            avisos.append(
                f"El neto ({neto}) no coincide con total × (1 − descuento) = {esperado.quantize(Decimal('0.01'))}. "
                "Revise los montos."
            )
        if neto > total:
            avisos.append("El total neto es mayor que el total de la factura.")
    fecha = campos.get("fecha_fac")
    if fecha and fecha.valor:
        f = date.fromisoformat(fecha.valor)
        if f > hoy + timedelta(days=1):
            avisos.append(f"La fecha de la factura ({f:%d-%m-%Y}) es futura: puede ser un error de lectura.")
        elif f < hoy - timedelta(days=365):
            avisos.append(f"La fecha de la factura ({f:%d-%m-%Y}) tiene más de un año.")
    return avisos


def _dec(campos: dict[str, CampoExtraido], clave: str) -> Decimal | None:
    c = campos.get(clave)
    return Decimal(str(c.valor)) if c is not None and c.valor is not None else None


# --------------------------------------------------------- texto OCR → campos
def _sin_acentos(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")


_RE_FECHA = re.compile(r"(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})")
_RE_MONTO = r"\$?\s*(\d{1,3}(?:[.,\s]?\d{3})*[.,]\d{2,4})"
_RE_VENDEDOR = re.compile(r"\b([A-Z]{2})\s?-\s?(\d{5})\b")
_RE_PEDIDO = re.compile(r"\b[PF][EF]D\s*[-\u2013]?\s*(\d{8,16})")
_RE_EMISORA = re.compile(
    r"\b((?:I[MN]PORTADORA|DISTRIBUIDORA|COMERCIALIZADORA|CORPORACION|CORP|INVERSIONES)\s+[A-Z0-9 &]{2,40}?)"
    r"\s*,?\s*C\s*\.?\s*A\b"
)
_PATRON_ZONA: Final[dict[str, re.Pattern[str]]] = {
    "numero_factura": re.compile(r"^\d{5,10}$"),
    "pedido": re.compile(r"^PED-\d{8,16}$"),
    "fecha": re.compile(r"^\d{2}-\d{2}-\d{4}$"),
    "cod_vendedor": re.compile(r"^[A-Z]{2}-\d{5}$"),
    "bultos": re.compile(r"^\d{1,4}$"),
    "subtotal": re.compile(r"^\d[\d.,]*[.,]\d{2,4}$"),
    "base_imponible": re.compile(r"^\d[\d.,]*[.,]\d{2,4}$"),
    "descuento_pct": re.compile(r"^\d{1,2}[.,]?\d{0,2}%?$"),
}
CONFIANZA_REGEX: Final = 0.55  # hallado por patrón en el texto general: siempre se marca para revisar
CONFIANZA_ZONA_MINIMA: Final = 0.6  # por debajo, la zona solo se usa si el texto general no trae el dato


def _limpiar_zona(campo: str, texto: str) -> str | None:
    """Texto de una zona (whitelist) → valor con el formato del campo, o None si no encaja."""
    t = _sin_acentos(texto).upper()
    if campo == "pedido":
        m = _RE_PEDIDO.search(t.replace(" ", ""))
        return f"PED-{m.group(1)}" if m else None
    if campo == "fecha":
        m = _RE_FECHA.search(t)
        return f"{int(m[1]):02d}-{int(m[2]):02d}-{m[3]}" if m else None
    if campo == "cod_vendedor":
        m = _RE_VENDEDOR.search(t)
        return f"{m[1]}-{m[2]}" if m else None
    if campo in ("subtotal", "base_imponible"):
        m = re.search(_RE_MONTO, t)
        return m.group(1).replace(" ", "") if m else None
    if campo == "descuento_pct":
        m = re.search(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%?", t)
        return f"{m.group(1)}%" if m else None
    m = re.search(r"\d+", t.replace(" ", ""))
    return m.group(0) if m else None


def _tras_etiqueta(texto: str, etiqueta: str, patron: str, alcance: int = 60) -> str | None:
    for m in re.finditer(etiqueta, texto):
        hallado = re.search(patron, texto[m.end(): m.end() + alcance])
        if hallado:
            return hallado.group(1)
    return None


def parsear_texto_factura(lectura: LecturaLocal) -> dict[str, Any]:
    """Texto OCR (página + zonas) → mismo formato crudo que el extractor con IA (``normalizar`` lo tipa).

    Prioridad por campo: 1) zona leída junto a su etiqueta, si encaja con el formato esperado y
    Tesseract la leyó con confianza; 2) patrón en el texto completo (cerca de la etiqueta y luego en
    todo el documento); 3) zona de baja confianza, como último recurso.
    """
    crudo: dict[str, Any] = {"es_factura": True}
    confianza: dict[str, float] = {}
    pagina = lectura.confianza if lectura.confianza is not None else 0.6
    respaldo: dict[str, tuple[str, float]] = {}

    for campo, (texto_zona, conf_zona) in lectura.zonas.items():
        if campo not in _PATRON_ZONA or not texto_zona:
            continue
        valor = _limpiar_zona(campo, texto_zona)
        if not valor or not _PATRON_ZONA[campo].match(valor):
            continue
        conf = conf_zona if conf_zona is not None else pagina
        if conf >= CONFIANZA_ZONA_MINIMA:
            crudo[campo] = valor
            confianza[campo] = conf
        else:
            respaldo[campo] = (valor, conf)

    for campo, (valor, conf) in parsear_cajas(list(lectura.cajas)).items():
        if campo not in crudo:
            crudo[campo] = valor
            confianza[campo] = conf

    texto = _sin_acentos(lectura.texto or "").upper()
    # Tesseract conserva los espacios de alineación (preserve_interword_spaces): se reducen a uno
    lineas = [re.sub(r"[ \t]+", " ", ln).strip() for ln in texto.splitlines() if ln.strip()]
    plano = " ".join(lineas)

    def poner(campo: str, valor: str | None) -> None:
        if valor and campo not in crudo:
            crudo[campo] = valor
            confianza[campo] = min(CONFIANZA_REGEX, pagina)

    m = _RE_PEDIDO.search(plano.replace(" -", "-").replace("- ", "-"))
    poner("pedido", f"PED-{m.group(1)}" if m else None)
    m = _RE_VENDEDOR.search(plano)
    poner("cod_vendedor", f"{m[1]}-{m[2]}" if m else None)
    f = _tras_etiqueta(plano, r"FECHA\W{0,3}", r"(\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{4})", 25)
    if f is None:
        hallada = _RE_FECHA.search(plano)
        f = hallada.group(0) if hallada else None
    if f:
        mf = _RE_FECHA.search(f)
        poner("fecha", f"{int(mf[1]):02d}-{int(mf[2]):02d}-{mf[3]}" if mf else None)
    poner("bultos", _tras_etiqueta(plano, r"BULTOS", r"^\W{0,4}(\d{1,4})\b", 12))
    poner("subtotal", _tras_etiqueta(plano, r"SUBTOTAL", r"^\W{0,6}" + _RE_MONTO, 30))
    poner("base_imponible", _tras_etiqueta(plano, r"IMPONIBLE", r"^\W{0,6}" + _RE_MONTO, 30))
    d = _tras_etiqueta(plano, r"DESCUENTO", r"^\W{0,4}(\d{1,2}[.,]\d{1,2})\s*%", 15)
    poner("descuento_pct", f"{d}%" if d else None)
    poner("numero_factura", _numero_factura(lineas))
    poner("cliente", _cliente(lineas))
    m = _RE_EMISORA.search(plano)
    poner("empresa_emisora", re.sub(r"^INPORTADORA", "IMPORTADORA", m.group(1).strip()) + ", C.A." if m else None)

    for campo, (valor, conf) in respaldo.items():
        if campo not in crudo:
            crudo[campo] = valor
            confianza[campo] = min(conf, CONFIANZA_REGEX)

    crudo["confianza"] = confianza
    crudo["es_factura"] = len(confianza) >= 3 or bool(re.search(r"FACTURA|RIF|SUBTOTAL", texto))
    return crudo


# ------------------------------------------------- cajas OCR (posición) → campos
# PaddleOCR devuelve una caja por frase con su posición. En esta factura cada dato está a la derecha
# de su etiqueta («Subtotal  $ 990,0000») o justo debajo («FACTURA» / «340239»), así que se busca la
# etiqueta y se toma la caja vecina: mucho más fiable que adivinar sobre el texto corrido.
_CONFUSIONES_DIGITO = str.maketrans({"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "|": "1",
                                     "T": "7", "Z": "2", "S": "5", "B": "8", "G": "6", "g": "9", "q": "9"})
CONFIANZA_CORREGIDA: Final = 0.6  # valor reparado (letra por número, año, decimales): se marca para revisar


def _digitos(texto: str) -> str:
    """Letras que el OCR confunde con números → números ('DDD1904' → '0001904')."""
    return texto.translate(_CONFUSIONES_DIGITO)


def _ascii(texto: str) -> str:
    """Mayúsculas sin acentos ni símbolos ajenos (el OCR a veces lee '$' como un ideograma)."""
    return re.sub(r"[^\x20-\x7e]", "", _sin_acentos(texto)).upper()


@dataclass(frozen=True)
class CajaOCR:
    texto: str
    confianza: float
    x: float
    y: float
    w: float
    h: float

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def limpio(self) -> str:
        return _ascii(self.texto)


@dataclass(frozen=True)
class _Hallazgo:
    valor: str
    confianza: float


def _misma_fila(a: CajaOCR, b: CajaOCR) -> bool:
    return abs(a.cy - b.cy) < 0.6 * min(a.h, b.h) or (b.y < a.cy < b.y + b.h and a.y < b.cy < a.y + a.h)


def _vecinas(cajas: list[CajaOCR], etiqueta: CajaOCR, abajo: bool) -> list[CajaOCR]:
    """Caja inmediatamente a la derecha en el mismo renglón y, si ``abajo``, las de debajo (más cerca primero)."""
    derecha = sorted((c for c in cajas if c is not etiqueta and _misma_fila(etiqueta, c)
                      and c.x >= etiqueta.x + etiqueta.w * 0.6), key=lambda c: c.x)
    out = derecha[:1]
    if abajo:
        debajo = [c for c in cajas if c is not etiqueta and etiqueta.cy < c.y + c.h * 0.2
                  and c.y - (etiqueta.y + etiqueta.h) < 2.5 * etiqueta.h
                  and c.x < etiqueta.x + etiqueta.w and c.x + c.w > etiqueta.x]
        out += sorted(debajo, key=lambda c: c.y)[:2]
    return out


def _buscar(
    cajas: list[CajaOCR], etiqueta: str, extraer: Any, abajo: bool = False
) -> list[_Hallazgo]:
    """Por cada caja con la etiqueta: lo que sigue a la etiqueta en la misma caja o la caja vecina."""
    hallados: list[_Hallazgo] = []
    for c in cajas:
        m = re.search(etiqueta, c.limpio)
        if not m:
            continue
        resto = c.limpio[m.end():].strip()
        if resto:
            v = extraer(resto)
            if v:
                hallados.append(_Hallazgo(v[0], min(c.confianza, v[1])))
                continue
        for vecina in _vecinas(cajas, c, abajo):
            v = extraer(vecina.limpio)
            if v:
                hallados.append(_Hallazgo(v[0], min(vecina.confianza, v[1])))
                break
    return hallados


def _monto_ocr(texto: str) -> tuple[str, float] | None:
    """'$1.077.6000' → '1077.6000' · '$9405000' (sin coma) → '940.5000' (corregido). En Bs → None."""
    t = texto.replace(" ", "")
    if re.search(r"\d\s*B[S5]\b|BS\.?$", t):
        return None  # montos en bolívares (columna izquierda): no son los de la factura en $
    m = re.search(r"\d[\d.,]*\d", t)
    if not m:
        return None
    s, conf = m.group(0), 1.0
    seps = [i for i, ch in enumerate(s) if ch in ".,"]
    if seps:
        decimales = len(s) - seps[-1] - 1
        entero = re.sub(r"[.,]", "", s[: seps[-1]])
        if decimales in (1, 2, 4) or (decimales == 3 and len(seps) == 1 and s[seps[-1]] == ","):
            s = f"{entero}.{s[seps[-1] + 1:]}"
        else:
            s = entero + s[seps[-1] + 1:]
    elif len(s) >= 6:  # se perdió la coma: los montos de esta factura llevan 4 decimales
        s, conf = f"{s[:-4]}.{s[-4:]}", CONFIANZA_CORREGIDA
    elif "$" not in t and len(s) < 3:
        return None
    return s, conf


def _fecha_ocr(texto: str, hoy: date) -> tuple[str, float] | None:
    m = re.search(r"([0-9OoDIlSBgZ]{1,2})\s*[-/.]\s*([0-9OoDIlSBgZ]{1,2})\s*[-/.]\s*([0-9OoDIlSBgZ]{4})", texto)
    if not m:
        return None
    d, me, a = (_digitos(g) for g in m.groups())
    conf = 1.0 if all(g.isdigit() for g in m.groups()) else CONFIANZA_CORREGIDA
    try:
        f = date(int(a), int(me), int(d))
    except ValueError:
        return None
    if f > hoy + timedelta(days=1):
        # El 6 y el 8 se confunden ('2028'): si cambiando un dígito del año queda una fecha pasada, se usa
        for anio in (hoy.year, hoy.year - 1):
            if sum(x != y for x, y in zip(str(anio), a, strict=True)) == 1:
                try:
                    candidata = f.replace(year=anio)
                except ValueError:
                    continue
                if candidata <= hoy + timedelta(days=1):
                    f, conf = candidata, CONFIANZA_CORREGIDA
                    break
    return f"{f.day:02d}-{f.month:02d}-{f.year}", conf


def _pedido_ocr(texto: str) -> tuple[str, float] | None:
    t = re.sub(r"PEDID[O0]\W*", " ", texto.replace(" ", ""))
    m = re.search(r"P[EF3R][DO0]\W{0,2}([A-Z0-9]{5,18})", t)
    if not m:
        return None
    codigo = m.group(1)
    letras = re.match(r"[A-Z]*", codigo).group(0)  # type: ignore[union-attr]
    prefijo = letras[:3] if len(codigo) - len(letras) >= 3 or len(letras) > 3 else ""
    numero = _digitos(codigo[len(prefijo):])
    if not numero.isdigit() or len(numero) < 5:
        return None
    conf = 1.0 if numero == codigo[len(prefijo):] and m.group(0).startswith("PED") else CONFIANZA_CORREGIDA
    return f"PED-{prefijo}{numero}", conf


def _vendedor_ocr(texto: str) -> tuple[str, float] | None:
    m = re.search(r"\b([A-Z]{2})\s?[-.\s]\s?([0-9OD]{5})\b", texto)
    if not m:
        return None
    num = _digitos(m[2])
    return f"{m[1]}-{num}", (1.0 if num == m[2] else CONFIANZA_CORREGIDA)


def _numero_ocr(texto: str) -> tuple[str, float] | None:
    m = re.fullmatch(r"\W*(?:N[O0]?\W*)?([0-9OD]{5,10})\W*", texto.replace(" ", ""))
    if not m:
        return None
    num = _digitos(m.group(1))
    return num, (1.0 if num == m.group(1) else CONFIANZA_CORREGIDA)


def _porcentaje_ocr(texto: str) -> tuple[str, float] | None:
    t = texto.replace(" ", "")
    m = re.search(r"(\d{1,2}[.,]\d{1,2})%?", t) or re.search(r"(\d{1,2})%", t)
    if m:
        return f"{m.group(1)}%", 1.0
    if re.fullmatch(r"%?[0O]{1,3}[.,]?[0O]{0,2}%?", t):  # '%000', '0,00' mal leídos
        return "0%", CONFIANZA_CORREGIDA
    return None


def _entero_ocr(texto: str) -> tuple[str, float] | None:
    m = re.match(r"\W{0,3}(\d{1,4})\b", texto.replace(" ", ""))
    return (m.group(1), 1.0) if m else None


def _cliente_ocr(texto: str) -> tuple[str, float] | None:
    c = re.sub(r"\bSERIE\s*\w?\b.*$", "", texto).strip(" :;.")
    c = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ,.&-]", "", c)).strip(" ,.-")
    if len(re.findall(r"[A-Z]{3,}", c)) < 1 or len(c) < 4:
        return None
    return _con_sufijo_legal(c), 1.0


def _con_sufijo_legal(c: str) -> str:
    """'MATVAL,C.A' / 'MATVAL CA' → 'MATVAL, C.A' (como lo escribe la REMESA)."""
    legal = re.search(r"^(.*?\S)[\s.,]*\b(C\s*\.?\s*A|S\s*\.?\s*A|S\.?R\.?L)\b\.?$", c)
    if not legal:
        legal = re.search(r"^(.*?[A-Z]{3})[.,]+\s*(C\.?A|S\.?A)\.?$", c)
    if legal:
        siglas = re.sub(r"[^A-Z]", "", legal.group(2))
        return f"{legal.group(1).rstrip(' ,.')}, {siglas[:1]}.{siglas[1:]}"
    return c


_RE_EMISORA_PEGADA = re.compile(
    r"([IU1][MN]PORTADORA|DISTRIBUIDORA|COMERCIALIZADORA|CORPORACION|INVERSIONES)\s*([A-Z0-9][A-Z0-9 &]{1,40}?)"
    r"\s*[,.]?\s*C\s*\.?\s*A\b"
)


def parsear_cajas(cajas: list[CajaOCR], hoy: date | None = None) -> dict[str, tuple[str, float]]:
    """Cajas OCR con posición → {campo: (texto, confianza)} buscando cada dato junto a su etiqueta."""
    hoy = hoy or today_local()
    if not cajas:
        return {}
    out: dict[str, tuple[str, float]] = {}

    def mejor(campo: str, hallados: list[_Hallazgo]) -> None:
        if hallados:
            out[campo] = (hallados[0].valor, round(hallados[0].confianza, 2))

    mejor("numero_factura", _buscar(cajas, r"^\W*F[AE4][CGO]?T[UIVL]{1,2}R[A4](?!D)\W*", _numero_ocr, abajo=True))
    mejor("fecha", _buscar(cajas, r"F[EC]CH[A4]\W*", lambda t: _fecha_ocr(t, hoy)))
    mejor("cod_vendedor", _buscar(cajas, r"(?:VENDE)?DOR\s*[:.]\W*", _vendedor_ocr))
    mejor("bultos", _buscar(cajas, r"BULT[O0]S\W*", _entero_ocr))
    mejor("descuento_pct", _buscar(cajas, r"DESCUENT[O0]\W*", _porcentaje_ocr))
    for campo, etiqueta in (("subtotal", r"SUB\s*TOTAL\W*"), ("base_imponible", r"IMP[OA]NIBLE\W*")):
        mejor(campo, _buscar(cajas, etiqueta, _monto_ocr))
    # Pedido y vendedor suelen venir pegados a su etiqueta en la misma caja: se buscan en todas
    for c in cajas:
        pedido = _pedido_ocr(c.limpio)
        if pedido:
            out["pedido"] = (pedido[0], round(min(c.confianza, pedido[1]), 2))
            break
    mejor("cliente", _buscar(cajas, r"(?<!COD)(?<!COD\.)(?<!COD )\bCLIENTE\s*[:;]\W*", _cliente_ocr))
    for c in sorted(cajas, key=lambda c: c.y):
        m = _RE_EMISORA_PEGADA.search(c.limpio)
        if m:
            tipo = re.sub(r"^[IU1][MN]", "IM", m.group(1))
            out["empresa_emisora"] = (f"{tipo} {m.group(2).strip()}, C.A.", round(c.confianza, 2))
            break
    return out


def _numero_factura(lineas: list[str]) -> str | None:
    """Número bajo la etiqueta FACTURA (columna derecha): suele quedar al final de la línea siguiente."""
    for i, ln in enumerate(lineas):
        if re.search(r"[EF]?ACTURA\b", ln) and "PROFORMA" not in ln:
            mismo = re.search(r"ACTURA\W{0,3}(\d{5,10})\b", ln)
            if mismo:
                return mismo.group(1)
            for sig in lineas[i + 1: i + 3]:
                fin = re.search(r"(?<![\d-])(\d{5,10})\W{0,3}$", sig)
                if fin:
                    return fin.group(1)
    return None


def _cliente(lineas: list[str]) -> str | None:
    """Razón social del cliente: tras «CLIENTE:» o en la línea anterior a «COD. CLIENTE»."""
    candidatas: list[str] = []
    for i, ln in enumerate(lineas):
        m = re.search(r"(?<!COD\. )(?<!COD )\bCLIENTE\s*[:;]\s*(.+)", ln)
        if m and not re.search(r"COD\W{0,2}CLIENTE", ln):
            candidatas.append(m.group(1))
        if re.search(r"COD\W{0,2}CLIENTE", ln):
            candidatas.extend(reversed(lineas[max(0, i - 3): i]))  # la más cercana primero (salta ruido)
    for c in candidatas:
        c = re.sub(r"\bSERIE\s+\w\b.*$", "", c)  # «Serie F» de la columna derecha
        c = re.sub(r"^.*?[:|\[]\s*", "", c) if re.search(r"[:|\[]", c[:12]) else c
        c = re.sub(r"^(?:[^A-Z0-9]*\b[A-Z]{1,2}\b\W+)+", "", c)  # basura corta del borde izquierdo
        c = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ,.&-]", "", c)).strip(" ,.-")
        legal = re.search(r"^(.*?\S)[\s.,]*\b(C\s*\.?\s*A|S\s*\.?\s*A|S\.?R\.?L)\b\.?", c)
        if legal:
            siglas = re.sub(r"[^A-Z]", "", legal.group(2))  # CA → C.A · SRL → S.RL
            c = f"{legal.group(1).rstrip(' ,.')}, {siglas[:1]}.{siglas[1:]}"
        if len(re.findall(r"[A-Z]{3,}", c)) >= 2:
            return c
    return None


# --------------------------------------------------------------------- motores
class ExtractorManual:
    """Sin lectura automática: el formulario llega vacío y el operador transcribe."""

    nombre = "manual"

    def extraer(
        self, imagen: bytes, content_type: str, lectura: LecturaLocal | None = None
    ) -> ResultadoExtraccion:
        campos = {k: CampoExtraido(None, None, None) for k in CAMPOS}
        return ResultadoExtraccion(
            self.nombre, campos,
            ["No se pudo leer el texto de la foto en este dispositivo: complete los datos a mano."],
        )


class ExtractorLocal:
    """Gratis: interpreta lo que el navegador leyó con PaddleOCR (la imagen no sale hacia terceros)."""

    nombre = "paddleocr (navegador)"

    def extraer(
        self, imagen: bytes, content_type: str, lectura: LecturaLocal | None = None
    ) -> ResultadoExtraccion:
        if lectura is None or not (lectura.texto or "").strip():
            return ExtractorManual().extraer(imagen, content_type)
        campos, avisos = normalizar(parsear_texto_factura(lectura))
        avisos += completar_montos(campos)
        marcar_montos_incoherentes(campos)
        faltan = [CAMPOS[k] for k, c in campos.items() if c.valor is None]
        if faltan:
            avisos.append("No se pudieron leer: " + ", ".join(faltan).lower() + ". Complételos mirando la foto.")
        return ResultadoExtraccion(self.nombre, campos, avisos)


def completar_montos(campos: dict[str, CampoExtraido]) -> list[str]:
    """Si falta el total o el neto pero hay descuento, lo calcula (y lo marca como dudoso para revisar)."""
    total, neto, pct = _dec(campos, "total_fact"), _dec(campos, "total_neto"), _dec(campos, "porcentaje")
    if pct is None or pct >= 1:
        return []
    if total is None and neto is not None:
        valor = (neto / (1 - pct)).quantize(Decimal("0.0001"))
        campos["total_fact"] = CampoExtraido(str(valor), None, 0.5)
        return [f"Total de la factura calculado a partir del neto y el descuento ({valor}): verifíquelo."]
    if neto is None and total is not None:
        valor = (total * (1 - pct)).quantize(Decimal("0.0001"))
        campos["total_neto"] = CampoExtraido(str(valor), None, 0.5)
        return [f"Total neto calculado a partir del total y el descuento ({valor}): verifíquelo."]
    return []


def marcar_montos_incoherentes(campos: dict[str, CampoExtraido]) -> None:
    """Si total × (1 − descuento) ≠ neto, alguno de los dos se leyó mal (p. ej. 6 por 8): ambos a revisar."""
    total, neto, pct = _dec(campos, "total_fact"), _dec(campos, "total_neto"), _dec(campos, "porcentaje")
    if total is None or neto is None or abs(total * (1 - (pct or Decimal(0))) - neto) <= Decimal("0.05"):
        return
    for clave in ("total_fact", "total_neto"):
        c = campos[clave]
        campos[clave] = CampoExtraido(c.valor, c.texto, min(c.confianza if c.confianza is not None else 1.0, 0.5))


class ExtractorClaudeVision:
    """Lectura con un modelo de visión de Anthropic usando salida estructurada (tool use)."""

    def __init__(self, cliente: ClaudeClient, modelo: str, max_tokens: int = 1024) -> None:
        self.cliente = cliente
        self.modelo = modelo
        self.max_tokens = max_tokens

    @property
    def nombre(self) -> str:
        return f"anthropic:{self.modelo}"

    def extraer(
        self, imagen: bytes, content_type: str, lectura: LecturaLocal | None = None
    ) -> ResultadoExtraccion:
        contenido = [
            {"type": "image", "source": {"type": "base64", "media_type": content_type,
                                         "data": base64.b64encode(imagen).decode("ascii")}},
            {"type": "text", "text": "Extrae los campos de esta factura con la herramienta registrar_factura."},
        ]
        try:
            crudo = self.cliente.herramienta(
                system=_PROMPT_SISTEMA, contenido=contenido, tool=_TOOL, modelo=self.modelo,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # la captura no se pierde: se usa el OCR local o se transcribe a mano
            log.exception("ocr_fallo")
            respaldo = ExtractorLocal().extraer(imagen, content_type, lectura)
            respaldo.error = f"{self.nombre}: {type(exc).__name__}: {str(exc)[:300]}"
            respaldo.avisos.insert(0, "La lectura con IA falló; se usó la lectura del dispositivo.")
            return respaldo
        campos, avisos = normalizar(crudo)
        return ResultadoExtraccion(self.nombre, campos, avisos)


def crear_extractor(cliente: ClaudeClient, modelo: str, disponible: bool) -> ExtractorFacturas:
    """Con ANTHROPIC_API_KEY: visión con IA (de pago). Sin clave: OCR gratuito en el navegador."""
    return ExtractorClaudeVision(cliente, modelo) if disponible else ExtractorLocal()
