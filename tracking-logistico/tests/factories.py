"""Generadores de libros Excel sintéticos con el mismo formato que REMESA/Libro2 (sin datos reales)."""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from openpyxl import Workbook

HEADER_REMESA = ["Factura", "N de Pedido", "Cod. Vendedor", "Fecha Fac", "Fecha Alm", "Fecha Rec", "Bult",
                 "Total Fact", "%", "Total Neto", "F", "Total Flete", "COMENTARIO"]


def bloque(
    ws: Any,
    fila: int,
    *,
    cedula: Any,
    nombre: str,
    placa: str,
    telefono: Any,
    destino: str,
    numero: str | None,
    empresa: str,
    facturas: list[dict[str, Any]],
    col0: int = 1,
    header: list[str] | None = None,
    num_col_offset: int = 10,
) -> int:
    c = col0
    ws.cell(fila, c, "Chofer:")
    ws.cell(fila, c + 1, cedula)
    ws.cell(fila, c + 4, nombre)
    ws.cell(fila + 1, c, "Placa:")
    ws.cell(fila + 1, c + 1, placa)
    ws.cell(fila + 1, c + 4, "Telf.:")
    ws.cell(fila + 1, c + 5, telefono)
    ws.cell(fila + 2, c, "Destino:")
    ws.cell(fila + 2, c + 1, destino)
    if numero:
        ws.cell(fila + 2, c + num_col_offset, numero)
    h = header or HEADER_REMESA
    ws.cell(fila + 3, c, empresa)
    for i, t in enumerate(h, start=1):
        ws.cell(fila + 3, c + i, t)
    r = fila + 4
    for f in facturas:
        ws.cell(r, c, f.get("cliente", "CLIENTE DEMO, C.A."))
        for i, t in enumerate(h, start=1):
            key = {
                "Factura": "numero", "N de Pedido": "pedido", "Cod. Vendedor": "vendedor", "Fecha Fac": "fecha",
                "Fecha Alm": "fecha", "Fecha Rec": "fecha_rec", "Bult": "bultos", "Total Fact": "total",
                "%": "pct", "Total Neto": "neto", "F": "f", "Total Flete": "flete", "COMENTARIO": "comentario",
            }.get(t)
            if key and key in f:
                ws.cell(r, c + i, f[key])
        r += 1
    ws.cell(r, c, "TOTALES:")
    return r + 6


def factura(n: int, prefijo: str = "FAC-U", vendedor: str = "LA-13001", **kw: Any) -> dict[str, Any]:
    base = {
        "numero": f"{prefijo}{n:08d}",
        "pedido": f"PED-U{n:012d}",
        "vendedor": vendedor,
        "fecha": datetime(2026, 9, 14),
        "fecha_rec": datetime(2026, 9, 21),
        "bultos": "1",
        "total": 100.5,
        "pct": 0.1,
        "neto": 90.45,
        "f": 0.05,
        "flete": 4.5225,
        "comentario": "DPP (10% A 15 DIAS)",
        "cliente": f"CLIENTE {n}, C.A.",
    }
    base.update(kw)
    return base


def libro_remesa() -> bytes:
    """Libro con hoja PLACAS + hoja 'REMESA 2026,' con 3 remesas y casos raros."""
    wb = Workbook()
    ws0 = wb.active
    assert ws0 is not None
    ws0.title = "PLACAS"
    ws0.append(["Cedula", "Nombre", "Placa", "Telefono", None, "PLACA ", "COLOR ", "SERIAL N,I,V", "CLASE"])
    ws0.append([11111111, "Ana Chofer", "AAA111", 4121111111, None, "AAA111", "BLANCO", "SER1", "CAMION"])
    ws0.append([22222222, "Beto Chofer", "BBB222", "0414 2222222", None, "BBB222", "GRIS", "SER2", "CAMION"])
    ws0.append([22222222, "Beto Chofer", "BBB222", "0414 2222222", None, None, None, None, None])

    ws = wb.create_sheet("REMESA 2026,")
    fila = 7
    fila = bloque(ws, fila, cedula=11111111, nombre="Ana Chofer", placa="AAA111", telefono="0412 111 1111",
                  destino="LARA", numero="2025-00001", empresa="IMPORTADORA USY",
                  facturas=[factura(1), factura(2), factura(3, vendedor="DC-01005"),
                            factura(3)])  # repetida en el archivo
    fila = bloque(ws, fila, cedula=22222222, nombre="Beto Chofer", placa="BBB222", telefono=4142222222,
                  destino="ORIENTE", numero=None, empresa="CORP DIJONAS",
                  facturas=[factura(10, prefijo="FAC-D"), factura(11, prefijo="FAC-D")])
    # Chofer no registrado + número en otra columna + valor #N/A
    fila = bloque(ws, fila, cedula=33333333, nombre="Carlos Nuevo", placa="CCC333", telefono=4243333333,
                  destino="TACHIRA", numero="2025-00003", empresa="CORP FERMETAL", num_col_offset=11,
                  facturas=[{"numero": 761, "pedido": "PED-0000000000782", "vendedor": "#N/A",
                             "fecha": datetime(2026, 9, 17), "bultos": 6, "total": 256.8, "cliente": "X"}])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def libro2() -> bytes:
    """Formato Libro2: empieza en la columna B y no tiene Cod. Vendedor."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Hoja1"
    header = [h for h in HEADER_REMESA if h != "Cod. Vendedor"]
    bloque(ws, 6, cedula=11111111, nombre="Ana Chofer", placa="AAA111", telefono=4121111111,
           destino="ARAGUA", numero="2025-00832", empresa="IMPORTADORA USY", facturas=[factura(50), factura(51)],
           col0=2, header=header)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def vendedores_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["Salesperson ID", "Salespersons Last Name", "Sales Territory"])
    ws.append(["LA-13001", "VENDEDOR UNO", "R. OCCIDENTAL"])
    ws.append(["DC-01005", "VENDEDOR DOS", "R. CAPITAL"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


JPEG_MINIMO = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
