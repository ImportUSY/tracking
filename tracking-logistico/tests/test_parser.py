from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from tests.factories import libro2, libro_remesa
from tracking.services.normalize import clean_cedula, clean_phone, to_date, to_decimal, to_int
from tracking.services.remesa_parser import FormatoNoReconocido, RemesaParser, clave_factura


def test_normalizacion_basica() -> None:
    assert clean_cedula(23806960) == "23806960"
    assert clean_cedula("V-12.345.678") == "12345678"
    assert clean_phone(4127889338) == "04127889338"
    assert clean_phone("0424 371 6985") == "04243716985"
    assert to_decimal(6309.539999999999) == Decimal("6309.5400")
    assert to_decimal("1.234,50") == Decimal("1234.5000")
    assert to_decimal("#N/A") is None
    assert to_int("3") == 3
    assert to_int("2.5") is None
    assert to_date("21/08/2026") == date(2026, 8, 21)
    assert to_date(46255) == date(2026, 8, 21)


def test_clave_factura() -> None:
    assert clave_factura("IMPORTADORA USY", "FAC-U1") == "FAC-U1"
    assert clave_factura("CORP FERMETAL", "761") != clave_factura("AJH IMPORT", "761")


def test_parser_remesa_sintetica() -> None:
    r = RemesaParser().parse_file(io.BytesIO(libro_remesa()))
    assert r.hoja == "REMESA 2026,"  # detecta la hoja aunque haya otras
    assert len(r.bloques) == 3
    b1, b2, b3 = r.bloques
    assert b1.numero == "2025-00001"
    assert b1.chofer_cedula == "11111111" and b1.chofer_nombre == "Ana Chofer"
    assert b1.placa == "AAA111" and b1.telefono == "04121111111"
    assert b1.empresa == "IMPORTADORA USY"
    assert [f.numero for f in b1.facturas] == ["FAC-U00000001", "FAC-U00000002", "FAC-U00000003"]
    f = b1.facturas[0]
    assert f.bultos == 1 and f.total_fact == Decimal("100.5000") and f.fecha_fac == date(2026, 9, 14)
    # Remesa sin número -> código estable
    assert b2.numero_generado and b2.numero and b2.numero.startswith("SN-")
    again = RemesaParser().parse_file(io.BytesIO(libro_remesa()))
    assert again.bloques[1].numero == b2.numero
    # Número en otra columna, factura numérica y #N/A
    assert b3.numero == "2025-00003"
    assert b3.facturas[0].numero == "761" and b3.facturas[0].cod_vendedor is None
    codigos = {a.codigo for a in r.avisos}
    assert {"FACTURA_REPETIDA_ARCHIVO", "REMESA_SIN_NUMERO", "VALOR_ERROR_EXCEL"} <= codigos


def test_parser_formato_libro2() -> None:
    r = RemesaParser().parse_file(io.BytesIO(libro2()))
    assert len(r.bloques) == 1
    b = r.bloques[0]
    assert b.numero == "2025-00832" and b.chofer_cedula == "11111111"
    assert len(b.facturas) == 2 and b.facturas[0].cod_vendedor is None
    assert "Cod. Vendedor" in b.columnas_faltantes


def test_parser_formato_no_reconocido() -> None:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["hola", "mundo"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(FormatoNoReconocido):
        RemesaParser().parse_file(io.BytesIO(buf.getvalue()))


def test_alias_extra_para_columnas_movidas() -> None:
    rows = [
        ("Chofer:", 123456, None, None, "X"),
        ("Placa:", "P1"),
        ("Destino:", "D", None, "2025-1000"),
        ("EMP", "Factura", "Nro. Ped. Cliente"),
        ("cli", "FAC-1", "PED-9"),
        ("TOTALES:",),
    ]
    sin = RemesaParser().parse_rows(rows)
    assert sin.bloques[0].facturas[0].pedido is None
    assert "Nro. Ped. Cliente" in sin.encabezados_desconocidos
    con = RemesaParser(extra_aliases={"Nro. Ped. Cliente": "pedido"}).parse_rows(rows)
    assert con.bloques[0].facturas[0].pedido == "PED-9"
