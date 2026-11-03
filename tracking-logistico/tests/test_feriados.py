"""v1.21: días feriados (no hábiles) y selección del Indicador de Gestión con clic en la gráfica."""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.test_consultor import _consultor
from tests.test_indicador import RANGO, _escenario, _ind
from tests.test_multitenant import _usuario
from tracking.services.feriados import nacionales, pascua
from tracking.services.tiempos import minutos_habiles, texto_jornada


def _vet(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=timezone.utc) + timedelta(hours=4)  # hora de Caracas → UTC


def test_feriados_nacionales_de_venezuela() -> None:
    assert pascua(2026) == date(2026, 4, 5) and pascua(2027) == date(2027, 3, 28)
    f = dict(nacionales(2026))
    assert f[date(2026, 2, 16)] == "Lunes de Carnaval" and f[date(2026, 2, 17)] == "Martes de Carnaval"
    assert f[date(2026, 4, 2)] == "Jueves Santo" and f[date(2026, 4, 3)] == "Viernes Santo"
    assert f[date(2026, 10, 12)] == "Día de la Resistencia Indígena" and len(f) == 14
    # 2030: Viernes Santo cae el 19 de abril → una sola fecha con los dos nombres
    assert dict(nacionales(2030))[date(2030, 4, 19)] == "Declaración de la Independencia / Viernes Santo"


def test_el_feriado_no_cuenta_como_habil() -> None:
    fer = (date(2026, 10, 12),)  # lunes
    # Viernes 9-oct 15:00 → martes 13-oct 9:00: 1 h del viernes + (lunes feriado) + 2 h del martes
    assert minutos_habiles(_vet(2026, 10, 9, 15), _vet(2026, 10, 13, 9), fer) == 180
    assert minutos_habiles(_vet(2026, 10, 9, 15), _vet(2026, 10, 13, 9)) == 180 + 540
    # Todo dentro del feriado: 0; desde el feriado hasta el martes 10:00 = 3 h
    assert minutos_habiles(_vet(2026, 10, 12, 8), _vet(2026, 10, 12, 15), fer) == 0
    assert minutos_habiles(_vet(2026, 10, 12, 10), _vet(2026, 10, 13, 10), fer) == 180
    assert texto_jornada() == "Días hábiles: de lunes a viernes de 7:00 a 16:00"


def test_administrar_feriados(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    lista = client.get("/api/feriados", headers=sup, params={"anio": 2026}).json()
    assert len(lista) == 14 and all(x["nacional"] for x in lista)  # la migración carga los nacionales
    r = client.post("/api/feriados", headers=sup, json={"fecha": "2026-11-02", "nombre": "  Decreto   presidencial "})
    assert r.status_code == 201 and r.json()["nombre"] == "Decreto presidencial" and not r.json()["nacional"]
    assert client.post("/api/feriados", headers=sup, json={"fecha": "2026-11-02", "nombre": "Otra"}).status_code == 409
    assert client.post("/api/feriados/nacionales", headers=sup, json={"anio": 2026}).json()["agregados"] == 0
    assert client.post("/api/feriados/nacionales", headers=sup, json={"anio": 2031}).json()["agregados"] == 14
    assert client.delete(f"/api/feriados/{r.json()['id']}", headers=sup).status_code == 204
    # Transportista y Consultor ven la lista pero no la cambian
    ta = _usuario(client, sup, "transp_f", "transportista", empresa)
    assert client.get("/api/feriados", headers=ta).status_code == 200
    assert client.post("/api/feriados", headers=ta, json={"fecha": "2026-11-03", "nombre": "X"}).status_code == 403
    co = _consultor(client, sup)
    assert client.post("/api/feriados/nacionales", headers=co, json={"anio": 2032}).status_code == 403


def test_clic_en_la_grafica_filtra_la_tabla(client: TestClient, sup: dict[str, str]) -> None:
    _escenario(client, sup)
    todo = _ind(client, sup)
    # Clic en «Entregado» (leyenda): la tabla muestra Entregadas + Devoluciones; gráfica y KPIs no cambian
    r = _ind(client, sup, serie="entregada")
    assert sorted(x["estatus"] for x in r["items"]) == ["Devolución", "Entregada"] and r["total"] == 2
    assert r["grafica"] == todo["grafica"] and r["kpis"] == todo["kpis"] and r["kpis"]["facturas"] == 5
    assert r["seleccion"] == {"serie": "entregada", "transporte": None,
                              "texto": "Entregado (incluye devoluciones) · todas las empresas de transporte"}
    # Clic en una barra: serie + empresa de transporte
    r = _ind(client, sup, serie="recibida", transporte="TRANSPORTES DEMO")
    assert r["total"] == 3 and {x["estatus"] for x in r["items"]} == {"Preparado"}
    assert _ind(client, sup, serie="recibida", transporte="OTRA EMPRESA")["total"] == 0
    assert client.get("/api/reportes/indicador", headers=sup, params={**RANGO, "serie": "x"}).status_code == 422
    # El Excel descarga lo mismo que muestra la tabla
    x = client.get("/api/reportes/indicador.xlsx", headers=sup, params={**RANGO, "serie": "entregada"})
    ws = load_workbook(io.BytesIO(x.content)).active
    assert ws is not None and ws.max_row == 3
