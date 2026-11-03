"""Lector determinista de la hoja REMESA.

Formato (repetido N veces en la misma hoja; puede empezar en la columna A o B):

    Chofer:   <cédula>        <nombre>
    Placa:    <placa>         Telf.: <teléfono>
    Destino:  <destino>       ...   <N° remesa, p.ej. 2025-00742>
    <EMPRESA> | Factura | N de Pedido | Cod. Vendedor | Fecha Fac | ... | COMENTARIO
    <cliente> | FAC-...  | PED-...     | LA-13001      | 2026-08-21| ... | DPP ...
    TOTALES:

La lectura es por streaming (openpyxl read_only) para no cargar el libro completo en memoria.
Nunca inventa datos: lo que falta queda vacío y se reporta como aviso.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import IO, Any

from openpyxl import load_workbook

from tracking.services.normalize import (
    clean_cedula,
    clean_code,
    clean_phone,
    clean_text,
    is_blank,
    is_excel_error,
    norm_key,
    to_date,
    to_decimal,
    to_int,
)

MAX_COLS = 30
SCAN_ROWS_FOR_DETECTION = 400
REMESA_NUM_RE = re.compile(r"^\d{4}-\d{3,}$")


class Nivel(str, Enum):
    ERROR = "error"
    ADVERTENCIA = "advertencia"
    INFO = "info"


# Campos canónicos de cada fila de factura -> alias aceptados en el encabezado (normalizados)
CAMPOS: dict[str, tuple[str, ...]] = {
    "numero": ("factura", "n factura", "nro factura", "numero de factura", "no factura", "numero factura"),
    "pedido": ("n de pedido", "no de pedido", "nro de pedido", "numero de pedido", "pedido", "n pedido"),
    "cod_vendedor": ("cod vendedor", "codigo vendedor", "cod de vendedor", "id de vendedor", "vendedor"),
    "fecha_fac": ("fecha fac", "fecha factura", "fecha de factura", "fecha del documento"),
    "fecha_alm": ("fecha alm", "fecha almacen", "fecha de almacen", "fecha preparado", "fecha prep", "preparado"),
    "fecha_rec": ("fecha rec", "fecha recepcion", "fecha recibido", "fecha de recepcion"),
    "bultos": ("bult", "bultos", "# bultos", "bulto"),
    "total_fact": ("total fact", "total factura", "monto factura"),
    "porcentaje": ("%",),
    "total_neto": ("total neto", "neto"),
    "flete_pct": ("f", "% flete"),
    "total_flete": ("total flete", "flete"),
    "comentario": ("comentario", "comentarios", "observacion", "observaciones"),
}
CAMPOS_IMPORTANTES = ("numero", "pedido", "cod_vendedor", "fecha_fac")
NOMBRES_CAMPO = {
    "numero": "Factura",
    "pedido": "N de Pedido",
    "cod_vendedor": "Cod. Vendedor",
    "fecha_fac": "Fecha Fac",
    "fecha_alm": "Fecha Preparado (Fecha Alm)",
    "fecha_rec": "Fecha Rec",
    "bultos": "Bult",
    "total_fact": "Total Fact",
    "porcentaje": "%",
    "total_neto": "Total Neto",
    "flete_pct": "F",
    "total_flete": "Total Flete",
    "comentario": "COMENTARIO",
}


@dataclass
class Aviso:
    codigo: str
    nivel: Nivel
    mensaje: str
    fila: int | None = None
    remesa: str | None = None
    factura: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["nivel"] = self.nivel.value
        return d


@dataclass
class FilaFactura:
    fila: int
    numero: str
    cliente: str | None = None
    pedido: str | None = None
    cod_vendedor: str | None = None
    fecha_fac: date | None = None
    fecha_alm: date | None = None
    fecha_rec: date | None = None
    bultos: int | None = None
    total_fact: Decimal | None = None
    porcentaje: Decimal | None = None
    total_neto: Decimal | None = None
    flete_pct: Decimal | None = None
    total_flete: Decimal | None = None
    comentario: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if isinstance(v, date):
                out[k] = v.isoformat()
            elif isinstance(v, Decimal):
                out[k] = str(v)
            else:
                out[k] = v
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FilaFactura:
        data = dict(d)
        for k in ("fecha_fac", "fecha_alm", "fecha_rec"):
            data[k] = date.fromisoformat(data[k]) if data.get(k) else None
        for k in ("total_fact", "porcentaje", "total_neto", "flete_pct", "total_flete"):
            data[k] = Decimal(data[k]) if data.get(k) is not None else None
        return cls(**data)


@dataclass
class BloqueRemesa:
    fila: int
    numero: str | None = None
    numero_generado: bool = False
    empresa: str | None = None
    destino: str | None = None
    chofer_cedula: str | None = None
    chofer_nombre: str | None = None
    placa: str | None = None
    telefono: str | None = None
    facturas: list[FilaFactura] = field(default_factory=list)
    columnas_faltantes: list[str] = field(default_factory=list)
    excluida: bool = False

    @property
    def fecha_referencia(self) -> date | None:
        fechas = [d for f in self.facturas if (d := f.fecha_alm or f.fecha_fac) is not None]
        return max(fechas) if fechas else None

    def to_dict(self) -> dict[str, Any]:
        ref = self.fecha_referencia
        return {
            "fila": self.fila,
            "numero": self.numero,
            "numero_generado": self.numero_generado,
            "empresa": self.empresa,
            "destino": self.destino,
            "chofer_cedula": self.chofer_cedula,
            "chofer_nombre": self.chofer_nombre,
            "placa": self.placa,
            "telefono": self.telefono,
            "columnas_faltantes": self.columnas_faltantes,
            "excluida": self.excluida,
            "fecha_referencia": ref.isoformat() if ref else None,
            "facturas": [f.to_dict() for f in self.facturas],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BloqueRemesa:
        data = {k: v for k, v in d.items() if k not in ("facturas", "fecha_referencia")}
        b = cls(**data)
        b.facturas = [FilaFactura.from_dict(f) for f in d.get("facturas", [])]
        return b


@dataclass
class ResultadoParseo:
    hoja: str
    hojas_disponibles: list[str]
    bloques: list[BloqueRemesa]
    avisos: list[Aviso]
    filas_leidas: int
    encabezados_desconocidos: list[str] = field(default_factory=list)
    # Filtro por «Fecha Fac» aplicado antes de validar (None = sin filtro, se analiza toda la hoja)
    filtro: dict[str, Any] | None = None

    @property
    def bloques_validos(self) -> list[BloqueRemesa]:
        return [b for b in self.bloques if not b.excluida]


class FormatoNoReconocido(ValueError):
    """La hoja no contiene bloques Chofer/Placa/Destino + tabla de facturas."""


def _fmt(d: date) -> str:
    return d.strftime("%d/%m/%Y")


@dataclass(frozen=True)
class FiltroFechaFac:
    """Rango [desde, hasta] (ambos incluidos) sobre la «Fecha Fac» de cada factura de la hoja.

    Solo se analizan y cargan las facturas cuya Fecha Fac está dentro del rango; las demás se ignoran en
    esta carga (no generan avisos ni se guardan en la vista previa). Una factura sin Fecha Fac no se
    puede ubicar en el rango, así que también se ignora. Sin fechas = sin filtro (toda la hoja).
    """

    desde: date | None = None
    hasta: date | None = None

    def __post_init__(self) -> None:
        if self.desde and self.hasta and self.desde > self.hasta:
            raise ValueError("La fecha «Desde» no puede ser posterior a «Hasta»")

    @property
    def activo(self) -> bool:
        return self.desde is not None or self.hasta is not None

    def incluye(self, fecha: date | None) -> bool:
        if fecha is None:
            return False
        return (self.desde is None or fecha >= self.desde) and (self.hasta is None or fecha <= self.hasta)

    @property
    def texto(self) -> str:
        if self.desde and self.hasta:
            return f"del {_fmt(self.desde)} al {_fmt(self.hasta)}"
        if self.desde:
            return f"desde el {_fmt(self.desde)}"
        return f"hasta el {_fmt(self.hasta)}" if self.hasta else "sin filtro"


class SinFacturasEnRango(FormatoNoReconocido):
    """Ninguna factura de la hoja tiene la Fecha Fac dentro del rango pedido."""



# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------


def _label(row: tuple[Any, ...]) -> tuple[int, str] | None:
    """Primera celda no vacía de la fila y su texto normalizado."""
    for idx, v in enumerate(row):
        if not is_blank(v):
            return idx, norm_key(v)
    return None


def _non_empty_after(row: tuple[Any, ...], start: int) -> list[tuple[int, Any]]:
    return [(i, v) for i, v in enumerate(row) if i > start and not is_blank(v)]


def _build_alias_index(extra: dict[str, str] | None) -> dict[str, str]:
    idx: dict[str, str] = {}
    for campo, aliases in CAMPOS.items():
        for a in aliases:
            idx[norm_key(a)] = campo
    for raw, campo in (extra or {}).items():
        if campo in CAMPOS:
            idx[norm_key(raw)] = campo
    return idx


def _is_header(row: tuple[Any, ...]) -> bool:
    return any(norm_key(v) == "factura" for v in row if isinstance(v, str))


def clave_factura(empresa: str | None, numero: str) -> str:
    """Identidad de una factura entre remesas.

    Los números con prefijo (FAC-U..., FAC-D...) ya son únicos entre empresas; los puramente
    numéricos (761, 10360) pueden repetirse entre empresas, así que se combinan con la empresa.
    """
    if re.match(r"^[A-Z]", numero):
        return numero
    return f"{norm_key(empresa)}|{numero}"


def generar_numero_remesa(b: BloqueRemesa) -> str:
    """Número estable para remesas sin número: mismo contenido => mismo número (idempotente)."""
    facts = ",".join(sorted(f.numero for f in b.facturas[:50]))
    base = f"{b.chofer_cedula}|{b.placa}|{b.destino}|{b.empresa}|{facts}"
    return "SN-" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:10].upper()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class RemesaParser:
    def __init__(self, extra_aliases: dict[str, str] | None = None, filtro: FiltroFechaFac | None = None) -> None:
        self.alias_index = _build_alias_index(extra_aliases)
        self.filtro = filtro if filtro is not None and filtro.activo else None

    # -- API pública ---------------------------------------------------------

    def parse_file(self, source: str | Path | IO[bytes], hoja: str | None = None) -> ResultadoParseo:
        wb = load_workbook(source, read_only=True, data_only=True)
        try:
            nombres = list(wb.sheetnames)
            if hoja:
                if hoja not in nombres:
                    raise FormatoNoReconocido(f"La hoja '{hoja}' no existe. Hojas: {', '.join(nombres)}")
                elegida = hoja
            else:
                elegida = self._detectar_hoja(wb, nombres)
            ws = wb[elegida]
            rows = ws.iter_rows(max_col=MAX_COLS, values_only=True)
            resultado = self.parse_rows(rows, hoja=elegida, hojas=nombres)
        finally:
            wb.close()
        return resultado

    def parse_rows(
        self, rows: Iterable[Sequence[Any]], hoja: str = "", hojas: list[str] | None = None
    ) -> ResultadoParseo:
        avisos: list[Aviso] = []
        bloques: list[BloqueRemesa] = []
        desconocidos: set[str] = set()
        actual: BloqueRemesa | None = None
        colmap: dict[str, int] | None = None
        cliente_col = 0
        en_tabla = False
        filas = 0

        for n, raw in enumerate(rows, start=1):
            filas = n
            row = tuple(raw[:MAX_COLS]) if raw else ()
            lab = _label(row)
            if lab is None:
                continue
            col, key = lab

            if key.startswith("chofer"):
                actual = BloqueRemesa(fila=n)
                bloques.append(actual)
                en_tabla, colmap = False, None
                vals = _non_empty_after(row, col)
                if vals:
                    actual.chofer_cedula = clean_cedula(vals[0][1])
                    nombre = next((v for _, v in vals[1:] if isinstance(v, str)), None)
                    if nombre is None and isinstance(vals[0][1], str) and not clean_cedula(vals[0][1]):
                        nombre = vals[0][1]
                    actual.chofer_nombre = clean_text(nombre, 160)
                continue

            if actual is not None and not en_tabla and key.startswith("placa"):
                vals = _non_empty_after(row, col)
                tel_idx = next(
                    (i for i, v in vals if isinstance(v, str) and norm_key(v).startswith("tel")), None
                )
                if vals and (tel_idx is None or vals[0][0] < tel_idx):
                    actual.placa = clean_code(vals[0][1], 20)
                if tel_idx is not None:
                    tel = next((v for i, v in vals if i > tel_idx), None)
                    actual.telefono = clean_phone(tel)
                continue

            if actual is not None and not en_tabla and key.startswith("destino"):
                vals = _non_empty_after(row, col)
                if vals:
                    actual.destino = clean_text(vals[0][1], 160)
                for _, v in vals[1:]:
                    txt = clean_text(v)
                    if txt and REMESA_NUM_RE.match(txt):
                        actual.numero = txt
                        break
                if actual.numero is None and len(vals) > 1:
                    actual.numero = clean_text(vals[-1][1], 40)
                continue

            if _is_header(row):
                if actual is None:
                    actual = BloqueRemesa(fila=n)
                    bloques.append(actual)
                colmap, cliente_col, faltantes, unk = self._map_header(row)
                desconocidos.update(unk)
                actual.empresa = clean_text(row[cliente_col], 120) if cliente_col < len(row) else None
                actual.columnas_faltantes = [NOMBRES_CAMPO[c] for c in faltantes]
                en_tabla = True
                continue

            if en_tabla and actual is not None and colmap is not None:
                if key.startswith("total"):
                    en_tabla = False
                    continue
                fila = self._parse_factura(row, n, colmap, cliente_col, actual, avisos)
                if fila is not None:
                    actual.facturas.append(fila)

        if not bloques:
            raise FormatoNoReconocido(
                "No se encontraron bloques 'Chofer: / Placa: / Destino:' con tabla de facturas en la hoja "
                f"'{hoja}'. Verifique que sea la hoja de remesas."
            )

        # Filtro por Fecha Fac ANTES de validar: las validaciones (duplicados, datos faltantes, remesas
        # vacías) y todo el análisis posterior trabajan solo con las facturas del rango.
        info = self._aplicar_filtro(bloques, avisos, self.filtro, filas) if self.filtro else None
        self._validar(bloques, avisos)
        return ResultadoParseo(
            hoja=hoja,
            hojas_disponibles=hojas or [hoja],
            bloques=bloques,
            avisos=avisos,
            filas_leidas=filas,
            encabezados_desconocidos=sorted(desconocidos),
            filtro=info,
        )

    @staticmethod
    def _aplicar_filtro(
        bloques: list[BloqueRemesa], avisos: list[Aviso], filtro: FiltroFechaFac, filas_leidas: int
    ) -> dict[str, Any]:
        """Deja en cada bloque solo las facturas con Fecha Fac en el rango y quita los bloques que se
        quedan sin facturas por el filtro (y los avisos de las filas que se ignoraron)."""
        total = en_rango = sin_fecha = 0
        fechas = [f.fecha_fac for b in bloques for f in b.facturas if f.fecha_fac is not None]
        filas_ignoradas: set[int] = set()
        rangos_ignorados: list[tuple[int, int]] = []
        conservados: list[BloqueRemesa] = []
        for i, b in enumerate(bloques):
            fin = bloques[i + 1].fila if i + 1 < len(bloques) else filas_leidas + 1
            total += len(b.facturas)
            dentro = [f for f in b.facturas if filtro.incluye(f.fecha_fac)]
            sin_fecha += sum(1 for f in b.facturas if f.fecha_fac is None)
            filas_ignoradas.update(f.fila for f in b.facturas if not filtro.incluye(f.fecha_fac))
            en_rango += len(dentro)
            if b.facturas and not dentro:
                rangos_ignorados.append((b.fila, fin))  # remesa sin ninguna factura en el rango
                continue
            b.facturas = dentro
            conservados.append(b)
        remesas_fuera = len(bloques) - len(conservados)
        bloques[:] = conservados
        avisos[:] = [a for a in avisos if a.fila is None or (
            a.fila not in filas_ignoradas and not any(ini <= a.fila < fin for ini, fin in rangos_ignorados))]
        info: dict[str, Any] = {
            "desde": filtro.desde.isoformat() if filtro.desde else None,
            "hasta": filtro.hasta.isoformat() if filtro.hasta else None,
            "texto": filtro.texto,
            "facturas_archivo": total,
            "facturas_en_rango": en_rango,
            "facturas_fuera_de_rango": total - en_rango - sin_fecha,
            "facturas_sin_fecha": sin_fecha,
            "remesas_fuera_de_rango": remesas_fuera,
            "fecha_fac_min": min(fechas).isoformat() if fechas else None,
            "fecha_fac_max": max(fechas).isoformat() if fechas else None,
        }
        if not en_rango:
            rango = (f" Las facturas de la hoja tienen Fecha Fac del {_fmt(min(fechas))} al {_fmt(max(fechas))}."
                     if fechas else " Ninguna factura de la hoja tiene Fecha Fac.")
            raise SinFacturasEnRango(
                f"No hay facturas con Fecha Fac {filtro.texto} en la hoja ({total} factura(s) leídas).{rango}")
        ignoradas = total - en_rango
        avisos.insert(0, Aviso(
            "FILTRO_FECHA_FAC", Nivel.INFO,
            f"Filtro por Fecha Fac {filtro.texto}: se analizan {en_rango} de {total} factura(s). "
            f"Se ignoran {ignoradas} en esta carga"
            + (f" ({sin_fecha} sin Fecha Fac)" if sin_fecha else "")
            + (f" y {remesas_fuera} remesa(s) sin facturas en el rango" if remesas_fuera else "") + "."))
        return info

    # -- Internos ------------------------------------------------------------

    def _detectar_hoja(self, wb: Any, nombres: list[str]) -> str:
        preferidas = sorted(nombres, key=lambda s: (0 if "remesa" in norm_key(s) else 1, nombres.index(s)))
        for nombre in preferidas:
            ws = wb[nombre]
            chofer = header = False
            for i, row in enumerate(ws.iter_rows(max_col=MAX_COLS, values_only=True)):
                if i >= SCAN_ROWS_FOR_DETECTION:
                    break
                lab = _label(row)
                if lab and lab[1].startswith("chofer") and _non_empty_after(row, lab[0]):
                    chofer = True
                if _is_header(row):
                    header = True
                if chofer and header:
                    return str(nombre)
        raise FormatoNoReconocido(
            "Ninguna hoja del archivo tiene el formato de REMESA (Chofer/Placa/Destino + Factura). "
            f"Hojas encontradas: {', '.join(nombres)}"
        )

    def _map_header(self, row: tuple[Any, ...]) -> tuple[dict[str, int], int, list[str], set[str]]:
        colmap: dict[str, int] = {}
        unknown: set[str] = set()
        first_idx: int | None = None
        for idx, v in enumerate(row):
            if is_blank(v):
                continue
            if first_idx is None:
                first_idx = idx
                continue  # la primera celda es el nombre de la empresa (columna de clientes)
            if not isinstance(v, str):
                continue
            campo = self.alias_index.get(norm_key(v))
            if campo and campo not in colmap:
                colmap[campo] = idx
            elif not campo:
                unknown.add(str(v).strip())
        faltantes = [c for c in CAMPOS_IMPORTANTES if c not in colmap]
        return colmap, first_idx or 0, faltantes, unknown

    @staticmethod
    def _parse_factura(
        row: tuple[Any, ...],
        n: int,
        colmap: dict[str, int],
        cliente_col: int,
        bloque: BloqueRemesa,
        avisos: list[Aviso],
    ) -> FilaFactura | None:
        def get(campo: str) -> Any:
            idx = colmap.get(campo)
            return row[idx] if idx is not None and idx < len(row) else None

        numero = clean_code(get("numero"))
        if numero is None:
            datos = [v for i, v in enumerate(row) if not is_blank(v) and i != cliente_col]
            if datos:
                avisos.append(
                    Aviso("FACTURA_SIN_NUMERO", Nivel.ADVERTENCIA,
                          "Fila con datos pero sin número de factura: se omite.", fila=n,
                          remesa=bloque.numero)
                )
            return None
        errores_excel = [NOMBRES_CAMPO[c] for c, i in colmap.items() if i < len(row) and is_excel_error(row[i])]
        if errores_excel:
            avisos.append(
                Aviso("VALOR_ERROR_EXCEL", Nivel.INFO,
                      f"Celdas con error de fórmula (#N/A, #REF!...) en: {', '.join(errores_excel)}. "
                      "Se dejan vacías.", fila=n, remesa=bloque.numero, factura=numero)
            )
        return FilaFactura(
            fila=n,
            numero=numero,
            cliente=clean_text(row[cliente_col] if cliente_col < len(row) else None, 255),
            pedido=clean_code(get("pedido")),
            cod_vendedor=clean_code(get("cod_vendedor"), 20),
            fecha_fac=to_date(get("fecha_fac")),
            fecha_alm=to_date(get("fecha_alm")),
            fecha_rec=to_date(get("fecha_rec")),
            bultos=to_int(get("bultos")),
            total_fact=to_decimal(get("total_fact")),
            porcentaje=to_decimal(get("porcentaje")),
            total_neto=to_decimal(get("total_neto")),
            flete_pct=to_decimal(get("flete_pct")),
            total_flete=to_decimal(get("total_flete")),
            comentario=clean_text(get("comentario")),
        )

    @staticmethod
    def _validar(bloques: list[BloqueRemesa], avisos: list[Aviso]) -> None:
        vistos_remesa: dict[str, BloqueRemesa] = {}
        vistas_factura: dict[str, str] = {}

        for b in bloques:
            if b.chofer_cedula is None:
                b.excluida = True
                avisos.append(Aviso("CHOFER_SIN_CEDULA", Nivel.ERROR,
                                    "Bloque sin cédula de chofer: no se puede asignar. Se excluye.",
                                    fila=b.fila, remesa=b.numero))
            if b.columnas_faltantes:
                avisos.append(Aviso("COLUMNAS_FALTANTES", Nivel.ADVERTENCIA,
                                    f"Columnas no encontradas: {', '.join(b.columnas_faltantes)}. "
                                    "Esos datos quedarán vacíos.", fila=b.fila, remesa=b.numero))
            if not b.facturas:
                b.excluida = True
                avisos.append(Aviso("REMESA_SIN_FACTURAS", Nivel.ADVERTENCIA,
                                    "Remesa sin facturas: se omite.", fila=b.fila, remesa=b.numero))
                continue
            if b.numero is None:
                b.numero = generar_numero_remesa(b)
                b.numero_generado = True
                avisos.append(Aviso("REMESA_SIN_NUMERO", Nivel.ADVERTENCIA,
                                    f"Remesa sin número en la fila Destino. Se asigna el código {b.numero}.",
                                    fila=b.fila, remesa=b.numero))
            if b.placa is None:
                avisos.append(Aviso("PLACA_VACIA", Nivel.ADVERTENCIA, "Remesa sin placa.",
                                    fila=b.fila, remesa=b.numero))

            previo = vistos_remesa.get(b.numero)
            if previo is not None and not previo.excluida and not b.excluida:
                if previo.chofer_cedula == b.chofer_cedula:
                    avisos.append(Aviso("REMESA_DUPLICADA_UNIDA", Nivel.INFO,
                                        f"La remesa {b.numero} aparece dos veces con el mismo chofer: "
                                        "se unen sus facturas.", fila=b.fila, remesa=b.numero))
                    previo.facturas.extend(b.facturas)
                    b.facturas = []
                    b.excluida = True
                else:
                    b.excluida = True
                    avisos.append(Aviso("REMESA_NUMERO_REPETIDO", Nivel.ERROR,
                                        f"El número de remesa {b.numero} ya se usó en la fila {previo.fila} "
                                        "con otro chofer. Se excluye este bloque.", fila=b.fila,
                                        remesa=b.numero))
                    continue
            elif not b.excluida:
                vistos_remesa[b.numero] = b

        # Facturas repetidas dentro del archivo (después de unir remesas)
        for b in bloques:
            if b.excluida:
                continue
            unicas: list[FilaFactura] = []
            for f in b.facturas:
                clave = clave_factura(b.empresa, f.numero)
                if clave in vistas_factura:
                    avisos.append(Aviso("FACTURA_REPETIDA_ARCHIVO", Nivel.ADVERTENCIA,
                                        f"Factura {f.numero} repetida en el archivo (ya está en la remesa "
                                        f"{vistas_factura[clave]}). Se conserva la primera.",
                                        fila=f.fila, remesa=b.numero, factura=f.numero))
                    continue
                vistas_factura[clave] = b.numero or "?"
                unicas.append(f)
            if b.facturas and not unicas:
                b.excluida = True
                avisos.append(Aviso("REMESA_SOLO_REPETIDAS", Nivel.ADVERTENCIA,
                                    "Todas las facturas de esta remesa ya aparecen en otra remesa del archivo. "
                                    "Se omite.", fila=b.fila, remesa=b.numero))
            b.facturas = unicas

        # Datos faltantes agregados (un aviso por tipo, no uno por fila)
        faltas: dict[str, list[str]] = {"cod_vendedor": [], "cliente": [], "fecha_fac": [], "pedido": []}
        for b in bloques:
            if b.excluida:
                continue
            for f in b.facturas:
                for campo in faltas:
                    if getattr(f, campo) is None and NOMBRES_CAMPO.get(campo, "") not in b.columnas_faltantes:
                        faltas[campo].append(f.numero)
        etiquetas = {"cod_vendedor": "Cod. Vendedor", "cliente": "cliente", "fecha_fac": "Fecha Fac",
                     "pedido": "N de Pedido"}
        for campo, lista in faltas.items():
            if lista:
                muestra = ", ".join(lista[:15]) + ("…" if len(lista) > 15 else "")
                avisos.append(Aviso("DATO_FALTANTE", Nivel.ADVERTENCIA,
                                    f"{len(lista)} factura(s) sin {etiquetas[campo]}: {muestra}"))


def iter_blocks_json(resultado: ResultadoParseo) -> Iterator[dict[str, Any]]:
    for b in resultado.bloques:
        yield b.to_dict()
