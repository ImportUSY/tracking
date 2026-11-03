"""Cargar REMESA: filtro previo por rango de «Fecha Fac» (Desde / Hasta) al analizar la hoja."""

from __future__ import annotations

import gzip
import io
import json
from datetime import date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from tests.conftest import crear_empresa, upload_remesa
from tests.factories import bloque
from tests.test_vercel import _filas
from tracking.services.remesa_parser import FiltroFechaFac, RemesaParser, SinFacturasEnRango

HOJA = "REMESA 2026,"


def _f(n: int, fecha: date | None, cliente: str = "CLIENTE") -> dict[str, Any]:
    d: dict[str, Any] = {"numero": f"FAC-U{n:08d}", "pedido": f"PED-{n:013d}", "vendedor": "LA-13001", "bultos": "2",
                         "total": 100.0, "pct": 0.1, "neto": 90.0, "f": 0.05, "flete": 4.5, "comentario": "DPP",
                         "cliente": f"{cliente} {n}, C.A."}
    if fecha is not None:
        d["fecha"] = datetime(fecha.year, fecha.month, fecha.day)
    return d


def _libro() -> bytes:
    """3 remesas: una con facturas del 1, 10 y 20 de octubre (y una sin fecha), una solo del 10 y una
    solo del 25. La factura 1110 está repetida en dos remesas: la copia del 20 queda fuera del rango."""
    wb = Workbook()
    ws = wb.active
    ws.title = HOJA
    fila = bloque(ws, 7, cedula=15111222, nombre="Luis Herrera", placa="A12BC3D", telefono="0412", destino="LARA",
                  numero="2026-01001", empresa="IMPORTADORA DEMO",
                  facturas=[_f(1101, date(2026, 10, 1)), _f(1102, date(2026, 10, 10)), _f(1103, date(2026, 10, 20)),
                            _f(1104, None)])
    fila = bloque(ws, fila, cedula=15111222, nombre="Luis Herrera", placa="A12BC3D", telefono="0412", destino="LARA",
                  numero="2026-01002", empresa="IMPORTADORA DEMO",
                  facturas=[_f(1110, date(2026, 10, 10)), _f(1111, date(2026, 10, 12))])
    fila = bloque(ws, fila, cedula=15111222, nombre="Luis Herrera", placa="A12BC3D", telefono="0412", destino="ZULIA",
                  numero="2026-01003", empresa="IMPORTADORA DEMO",
                  facturas=[_f(1120, date(2026, 10, 25)), _f(1110, date(2026, 10, 20))])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def _parse(filtro: FiltroFechaFac | None = None) -> Any:
    return RemesaParser(filtro=filtro).parse_file(io.BytesIO(_libro()), HOJA)


# ------------------------------------------------------------------------------------------ parser
def test_filtro_deja_solo_las_facturas_del_rango() -> None:
    p = _parse(FiltroFechaFac(date(2026, 10, 5), date(2026, 10, 15)))
    facturas = {b.numero: [f.numero for f in b.facturas] for b in p.bloques}
    assert facturas == {"2026-01001": ["FAC-U00001102"], "2026-01002": ["FAC-U00001110", "FAC-U00001111"]}
    assert p.filtro == {
        "desde": "2026-10-05", "hasta": "2026-10-15", "texto": "del 05/10/2026 al 15/10/2026",
        "facturas_archivo": 8, "facturas_en_rango": 3, "facturas_fuera_de_rango": 4, "facturas_sin_fecha": 1,
        "remesas_fuera_de_rango": 1, "fecha_fac_min": "2026-10-01", "fecha_fac_max": "2026-10-25",
    }
    codigos = [a.codigo for a in p.avisos]
    assert codigos[0] == "FILTRO_FECHA_FAC"
    # La copia repetida de 1110 estaba fuera del rango: no hay aviso de factura repetida ni de datos faltantes
    assert "FACTURA_REPETIDA_ARCHIVO" not in codigos
    assert not any(a.codigo == "DATO_FALTANTE" and "Fecha Fac" in a.mensaje for a in p.avisos)


def test_limites_incluidos_y_rangos_abiertos() -> None:
    def numeros(f: FiltroFechaFac) -> list[str]:
        return sorted(x.numero for b in _parse(f).bloques for x in b.facturas)

    assert numeros(FiltroFechaFac(date(2026, 10, 10), date(2026, 10, 10))) == ["FAC-U00001102", "FAC-U00001110"]
    assert numeros(FiltroFechaFac(desde=date(2026, 10, 20))) == ["FAC-U00001103", "FAC-U00001110", "FAC-U00001120"]
    assert numeros(FiltroFechaFac(hasta=date(2026, 10, 1))) == ["FAC-U00001101"]
    assert FiltroFechaFac(desde=date(2026, 10, 20)).texto == "desde el 20/10/2026"
    with pytest.raises(ValueError, match="Desde"):
        FiltroFechaFac(date(2026, 10, 20), date(2026, 10, 1))


def test_sin_fechas_el_analisis_no_cambia() -> None:
    sin = _parse()
    vacio = _parse(FiltroFechaFac())
    assert sin.filtro is None and vacio.filtro is None
    assert [b.to_dict() for b in sin.bloques] == [b.to_dict() for b in vacio.bloques]
    assert [a.to_dict() for a in sin.avisos] == [a.to_dict() for a in vacio.avisos]
    assert "FACTURA_REPETIDA_ARCHIVO" in [a.codigo for a in sin.avisos]  # comportamiento de siempre


def test_ninguna_factura_en_el_rango() -> None:
    with pytest.raises(SinFacturasEnRango, match=r"del 01/10/2026 al 25/10/2026"):
        _parse(FiltroFechaFac(date(2026, 11, 1), date(2026, 11, 30)))


# ------------------------------------------------------------------------------------------ API
@pytest.fixture()
def preparado(client: TestClient, sup: dict[str, str]) -> dict[str, str]:
    emp = crear_empresa(client, sup)
    r = client.post("/api/admin/conductores", headers=sup,
                    json={"cedula": "15111222", "nombre": "Luis Herrera", "empresa_id": emp})
    assert r.status_code == 201, r.text
    return sup


def test_analizar_y_confirmar_con_rango(client: TestClient, preparado: dict[str, str]) -> None:
    sup = preparado
    r = upload_remesa(client, sup, _libro(), fecha_fac_desde="2026-10-05", fecha_fac_hasta="2026-10-15")
    assert r.status_code == 201, r.text
    res = r.json()["resumen"]
    assert res["filtro_fecha_fac"]["facturas_en_rango"] == 3
    assert res["totales"]["facturas"] == 3 and res["totales"]["facturas_nuevas"] == 3
    assert [m["numero"] for m in res["remesas"]] == ["2026-01001", "2026-01002"]
    assert [(d["factura"], d["fecha_fac"], d["clase"]) for d in res["facturas_detalle"]] == [
        ("FAC-U00001102", "2026-10-10", "nueva"), ("FAC-U00001110", "2026-10-10", "nueva"),
        ("FAC-U00001111", "2026-10-12", "nueva")]
    assert "total" not in json.dumps(res["facturas_detalle"])  # sin montos en el detalle
    assert res["avisos"][0]["codigo"] == "FILTRO_FECHA_FAC"

    out = client.post(f"/api/importaciones/{r.json()['id']}/confirmar", headers=sup, json={}).json()
    assert out["facturas_creadas"] == 3 and out["remesas_creadas"] == 2
    cargadas = client.get("/api/facturas", headers=sup, params={"limit": 100}).json()
    lista = cargadas["items"] if isinstance(cargadas, dict) else cargadas
    assert sorted(f["numero"] for f in lista) == ["FAC-U00001102", "FAC-U00001110", "FAC-U00001111"]
    hist = client.get("/api/importaciones", headers=sup).json()
    assert hist[0]["filtro_fecha_fac"] == "del 05/10/2026 al 15/10/2026"

    # Otra carga del mismo archivo con otro rango: solo entran las facturas de ese rango
    r = upload_remesa(client, sup, _libro(), fecha_fac_desde="2026-10-16")
    res = r.json()["resumen"]
    clases = {d["factura"]: d["clase"] for d in res["facturas_detalle"]}
    assert clases == {"FAC-U00001103": "nueva", "FAC-U00001120": "nueva", "FAC-U00001110": "en_otra_remesa"}
    out = client.post(f"/api/importaciones/{r.json()['id']}/confirmar", headers=sup,
                      json={"remesas": ["2026-01001", "2026-01003"]}).json()
    assert out["facturas_creadas"] == 2


def test_sin_rango_igual_que_antes(client: TestClient, preparado: dict[str, str]) -> None:
    res = upload_remesa(client, preparado, _libro()).json()["resumen"]
    assert res["filtro_fecha_fac"] is None and res["facturas_detalle"] is None
    assert res["totales"]["facturas"] == 7  # 8 en la hoja, una repetida en el archivo
    assert len(res["remesas"]) == 3


def test_filas_del_navegador_con_rango(client: TestClient, preparado: dict[str, str]) -> None:
    datos = {"archivo": "R.xlsx", "hoja": HOJA, "hojas": [HOJA], "filas": _filas(_libro(), HOJA),
             "fecha_fac_desde": "2026-10-20", "fecha_fac_hasta": "2026-10-31"}
    cuerpo = gzip.compress(json.dumps(datos).encode())
    h = {**preparado, "Content-Type": "application/json"}
    r = client.post("/api/importaciones/filas", headers=h, content=cuerpo)
    assert r.status_code == 201, r.text
    res = r.json()["resumen"]
    assert sorted(d["factura"] for d in res["facturas_detalle"]) == ["FAC-U00001103", "FAC-U00001110", "FAC-U00001120"]
    assert res["filtro_fecha_fac"]["facturas_en_rango"] == 3


def test_rango_invalido_o_vacio(client: TestClient, preparado: dict[str, str]) -> None:
    r = upload_remesa(client, preparado, _libro(), fecha_fac_desde="2026-10-20", fecha_fac_hasta="2026-10-01")
    assert r.status_code == 422 and "Desde" in r.json()["detail"]
    r = upload_remesa(client, preparado, _libro(), fecha_fac_desde="2027-01-01")
    assert r.status_code == 422 and "No hay facturas con Fecha Fac desde el 01/01/2027" in r.json()["detail"]


def test_consultor_analiza_con_rango(client: TestClient, sup: dict[str, str]) -> None:
    from tests.test_capturas import _usuario

    co = _usuario(client, sup, "consultor9", "consultor", None)
    files = {"archivo": ("R.xlsx", _libro(), "application/octet-stream")}
    r = client.post("/api/importaciones/analizar-archivo", headers=co, files=files,
                    data={"fecha_fac_hasta": "2026-10-01"})
    assert r.status_code == 200, r.text
    assert [d["factura"] for d in r.json()["resumen"]["facturas_detalle"]] == ["FAC-U00001101"]
