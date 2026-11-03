"""Excel para Cobranzas con el mismo formato de bloque que 'Libro2' + Cod. Vendedor y estatus.

Columnas B..N idénticas a Libro2 (para que fórmulas o macros existentes sigan funcionando) y
al final: O = Cod. Vendedor, P = Estatus, Q = Fecha/hora del estatus (hora local).

``COLUMNAS_FACTURA`` / ``valores_factura`` y ``COLUMNAS_REMESA`` / ``valores_remesa`` son la fuente
única de los datos del reporte: las usan este Excel y la vista en pantalla (``/api/reportes/cobranzas``),
así ambos muestran siempre la misma información.

Montos (Total Fact, %, Total Neto, F, Total Flete): solo para SUPERVISOR y COBRANZAS. Para los demás
roles el reporte se arma con ``columnas_factura(con_montos=False)`` y esas columnas no existen.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import datetime, time
from decimal import Decimal
from typing import Final

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from tracking.enums import Estatus
from tracking.models import Factura, Remesa
from tracking.services.timeutil import to_local

THIN = Side(style="thin")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
GRIS = PatternFill("solid", fgColor="D9D9D9")
AZUL = PatternFill("solid", fgColor="DDEBF7")
FILL_ESTATUS = {
    Estatus.ENTREGADA.value: PatternFill("solid", fgColor="C6EFCE"),
    Estatus.DEVOLUCION.value: PatternFill("solid", fgColor="FFC7CE"),
}
FILL_EN_CURSO = PatternFill("solid", fgColor="FFEB9C")

FMT_FECHAHORA: Final = "mm-dd-yy hh:mm"

# (clave, título, tipo). La primera (cliente) va en la columna B, cuyo encabezado es la empresa emisora.
COLUMNAS_FACTURA: Final[tuple[tuple[str, str, str], ...]] = (
    ("cliente", "Cliente", "texto"),
    ("numero", "Factura", "texto"),
    ("pedido", "N de Pedido", "texto"),
    ("fecha_fac", "Fecha Fac", "fecha"),
    ("fecha_alm", "Fecha Preparado", "fechahora"),
    ("fecha_tra", "Fecha Por Entregar", "fechahora"),
    ("fecha_rec", "Fecha Entregado", "fechahora"),
    ("bultos", "Bult", "entero"),
    ("total_fact", "Total Fact", "monto"),
    ("porcentaje", "%", "pct"),
    ("total_neto", "Total Neto", "monto"),
    ("flete_pct", "F", "pct"),
    ("total_flete", "Total Flete", "monto"),
    ("comentario", "COMENTARIO", "texto"),
    ("cod_vendedor", "Cod. Vendedor", "texto"),
    ("estatus", "Estatus", "texto"),
    ("estatus_at", "Fecha Estatus", "fechahora"),
)
# Datos del encabezado de cada bloque del Excel (Chofer, Placa, Telf., Destino, número, empresa)
COLUMNAS_REMESA: Final[tuple[tuple[str, str, str], ...]] = (
    ("remesa_numero", "Remesa", "texto"),
    ("empresa", "Empresa", "texto"),
    ("chofer_nombre", "Chofer", "texto"),
    ("chofer_cedula", "Cédula", "texto"),
    ("placa", "Placa", "texto"),
    ("chofer_telefono", "Telf.", "texto"),
    ("destino", "Destino", "texto"),
)
MONTOS: Final = frozenset({"total_fact", "porcentaje", "total_neto", "flete_pct", "total_flete"})


def columnas_factura(con_montos: bool = True) -> tuple[tuple[str, str, str], ...]:
    """Columnas del reporte según el rol: sin montos para quien no es SUPERVISOR ni COBRANZAS."""
    return COLUMNAS_FACTURA if con_montos else tuple(c for c in COLUMNAS_FACTURA if c[0] not in MONTOS)


HEADERS = [titulo for _, titulo, _ in COLUMNAS_FACTURA[1:]]
_ESTILO: Final[dict[str, tuple[float, str | None, str]]] = {
    "cliente": (6, None, "left"), "numero": (8, None, "center"), "pedido": (8, None, "center"),
    "fecha_fac": (9, "mm-dd-yy", "center"), "fecha_alm": (9, FMT_FECHAHORA, "center"),
    "fecha_rec": (9, FMT_FECHAHORA, "center"),
    "fecha_tra": (9, FMT_FECHAHORA, "center"), "bultos": (9, None, "center"),
    "total_fact": (9, "#,##0.00", "center"), "porcentaje": (8, "0%", "center"),
    "total_neto": (9, "#,##0.00", "center"), "flete_pct": (8, "0%", "center"),
    "total_flete": (9, "#,##0.00", "center"), "comentario": (6, None, "left"),
    "cod_vendedor": (8, None, "center"), "estatus": (8, None, "center"),
    "estatus_at": (8, "dd/mm/yyyy hh:mm", "center"),
}


def valores_factura(f: Factura) -> dict[str, object]:
    """Valores de una fila del reporte (tipos nativos: Decimal, date, datetime local)."""
    return {
        "cliente": f.cliente, "numero": f.numero, "pedido": f.pedido, "fecha_fac": f.fecha_fac,
        "fecha_alm": _local(f.fecha_alm), "fecha_rec": _local(f.fecha_rec), "fecha_tra": _local(f.fecha_tra),
        "bultos": f.bultos, "total_fact": f.total_fact,
        "porcentaje": f.porcentaje, "total_neto": f.total_neto, "flete_pct": f.flete_pct,
        "total_flete": f.total_flete, "comentario": f.comentario, "cod_vendedor": f.cod_vendedor,
        "estatus": Estatus(f.estatus).label, "estatus_at": _local(f.estatus_at),
    }


def _local(momento: datetime | None) -> datetime | None:
    return to_local(momento) if momento else None


def valores_remesa(r: Remesa) -> dict[str, object]:
    return {
        "remesa_numero": r.numero, "empresa": r.empresa, "chofer_nombre": r.chofer_nombre,
        "chofer_cedula": r.chofer_cedula, "placa": r.placa, "chofer_telefono": r.chofer_telefono,
        "destino": r.destino,
    }
_ANCHO: Final[dict[str, float]] = {
    "cliente": 34, "numero": 15, "pedido": 19, "fecha_fac": 10, "fecha_alm": 15, "fecha_rec": 15,
    "fecha_tra": 15, "bultos": 6, "total_fact": 12, "porcentaje": 6, "total_neto": 12,
    "flete_pct": 6, "total_flete": 11, "comentario": 26, "cod_vendedor": 13, "estatus": 13, "estatus_at": 17,
}
# Columna de Excel (1 = A) de cada dato: la A queda libre y la tabla empieza en la B
def posiciones(columnas: Sequence[tuple[str, str, str]]) -> dict[str, int]:
    return {clave: i for i, (clave, _, _) in enumerate(columnas, start=2)}


def anchos(columnas: Sequence[tuple[str, str, str]]) -> dict[str, float]:
    col = posiciones(columnas)
    return {"A": 5.86, **{get_column_letter(col[k]): _ANCHO[k] for k in col}}


COL: Final[dict[str, int]] = posiciones(COLUMNAS_FACTURA)
WIDTHS = anchos(COLUMNAS_FACTURA)
SEPARACION = 6
FMT_FECHA = "mm-dd-yy"
FMT_MONTO = "#,##0.00"
FMT_PCT = "0%"


def _font(size: float, bold: bool = False) -> Font:
    return Font(name="Arial", size=size, bold=bold)


def _escribir_bloque(
    ws: Worksheet,
    fila: int,
    remesa: Remesa,
    facturas: Sequence[Factura],
    columnas: Sequence[tuple[str, str, str]] = COLUMNAS_FACTURA,
) -> int:
    col_de = posiciones(columnas)
    # Encabezado del bloque (B=2)
    c = ws.cell(fila, 2, "Chofer:")
    c.font, c.fill, c.alignment = _font(11, True), GRIS, Alignment(horizontal="right")
    cedula = remesa.chofer_cedula
    ced = ws.cell(fila, 3, int(cedula) if cedula and cedula.isdigit() else cedula)
    ced.font, ced.number_format = _font(10), "00,000,000"
    nom = ws.cell(fila, 5, remesa.chofer_nombre)
    nom.font, nom.alignment = _font(12), Alignment(horizontal="left")

    c = ws.cell(fila + 1, 2, "Placa:")
    c.font, c.fill, c.alignment = _font(11, True), GRIS, Alignment(horizontal="right")
    p = ws.cell(fila + 1, 3, remesa.placa)
    p.font, p.alignment = _font(11, True), Alignment(horizontal="center")
    ws.cell(fila + 1, 5, "Telf.:").font = _font(11, True)
    t = ws.cell(fila + 1, 6, remesa.chofer_telefono)
    t.font, t.alignment = _font(11, True), Alignment(horizontal="left")

    c = ws.cell(fila + 2, 2, "Destino:")
    c.font, c.fill, c.alignment = _font(11, True), GRIS, Alignment(horizontal="right")
    d = ws.cell(fila + 2, 3, remesa.destino)
    d.font, d.fill, d.alignment = _font(11, True), AZUL, Alignment(horizontal="center")
    for col in range(3, 10):
        ws.cell(fila + 2, col).border = Border(bottom=THIN)
    ws.merge_cells(start_row=fila + 2, start_column=3, end_row=fila + 2, end_column=9)
    n = ws.cell(fila + 2, 12, remesa.numero)
    n.font, n.alignment = _font(8, True), Alignment(horizontal="center")

    # Encabezados de tabla
    h = fila + 3
    ws.row_dimensions[h].height = 30
    cab = ws.cell(h, 2, remesa.empresa)
    cab.font, cab.fill, cab.border = _font(11, True), GRIS, BORDER
    cab.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, (_, titulo, _) in enumerate(columnas[1:], start=3):
        cell = ws.cell(h, i, titulo)
        cell.font, cell.fill, cell.border = _font(11, True), GRIS, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Filas
    r = h + 1
    primera = r
    for f in facturas:
        fila_valores = valores_factura(f)
        for i, (clave, _, _) in enumerate(columnas, start=2):
            size, fmt, align = _ESTILO[clave]
            valor = fila_valores[clave]
            if isinstance(valor, Decimal):
                valor = float(valor)
            elif isinstance(valor, datetime):
                if fmt == FMT_FECHAHORA and valor.time() == time.min:
                    fmt = "mm-dd-yy"  # del Excel solo llegó el día: sin hora inventada
                valor = valor.replace(tzinfo=None)  # Excel no admite zona horaria
            cell = ws.cell(r, i, valor)
            cell.font, cell.border = _font(size), BORDER
            cell.alignment = Alignment(horizontal=align, vertical="center")
            if fmt:
                cell.number_format = fmt
        ws.cell(r, col_de["estatus"]).fill = FILL_ESTATUS.get(f.estatus, FILL_EN_CURSO)
        r += 1
    ultima = r - 1

    # Totales (fórmulas, como en Libro2)
    tot = ws.cell(r, 2, "TOTALES:")
    tot.font, tot.alignment = _font(9, True), Alignment(horizontal="right")
    if facturas and "total_fact" in col_de:
        for col in (col_de["total_fact"], col_de["total_neto"]):
            letra = get_column_letter(col)
            cell = ws.cell(r, col, f"=SUM({letra}{primera}:{letra}{ultima})")
            cell.font, cell.number_format = _font(8, True), FMT_MONTO
            cell.alignment = Alignment(horizontal="center")
        letra = get_column_letter(col_de["total_flete"])
        flete = ws.cell(r + 1, col_de["total_flete"], f"=SUM({letra}{primera}:{letra}{ultima})")
        flete.font, flete.number_format, flete.border = _font(8, True), FMT_MONTO, BORDER
        flete.alignment = Alignment(horizontal="center")
    return r + 2 + SEPARACION


def generar_excel_cobranzas(
    remesas: Sequence[tuple[Remesa, Sequence[Factura]]], titulo: str | None = None, con_montos: bool = True
) -> bytes:
    columnas = columnas_factura(con_montos)
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Hoja1"
    for col, w in anchos(columnas).items():
        ws.column_dimensions[col].width = w
    fila = 2
    if titulo:
        t = ws.cell(fila, 2, titulo)
        t.font = _font(12, True)
        ws.cell(fila + 1, 2, f"Generado: {datetime.now():%d/%m/%Y %H:%M}").font = _font(8)
    fila = 6
    for remesa, facturas in remesas:
        fila = _escribir_bloque(ws, fila, remesa, facturas, columnas)
    ws.sheet_view.zoomScale = 90
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
