"""Cobranzas por Fecha Estatus (con validación de Desde/Hasta), desglose de «Avisar a vendedores» y
métrica estrella solo para el Supervisor."""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.conftest import crear_empresa, login
from tests.test_flujo_completo import _cargar, _ev
from tracking.services.timeutil import now_utc, to_local, today_local


def _escenario(client: TestClient, sup: dict[str, str]) -> dict[str, Any]:
    ctx = _cargar(client, sup)
    client.post(f"/api/importaciones/{ctx['preview']['id']}/confirmar", headers=sup, json={})
    ana = login(client, "11111111", ctx["creds"]["11111111"])
    facts = client.get("/api/conductor/facturas", headers=ana).json()["facturas"]
    ids = [f["id"] for f in facts]
    antes = now_utc() - timedelta(days=2)
    # Factura 0: entregada hace dos días (sin señal) · factura 1: entregada hoy · factura 2: sigue recibida
    for i, estatus in enumerate(("en_transito", "entregada")):
        cuando = (antes + timedelta(minutes=i)).isoformat()
        lote = {"eventos": [_ev(ids[0], estatus, ocurrido_at=cuando)]}
        r = client.post("/api/conductor/eventos", headers=ana, json=lote)
        assert r.json()["resultados"][0]["aplicado"], r.text
    for estatus in ("en_transito", "entregada"):
        client.post("/api/conductor/eventos", headers=ana, json={"eventos": [_ev(ids[1], estatus)]})
    return {"ids": ids, "numeros": [f["numero"] for f in facts], "dia_antes": to_local(antes).date()}


def _numeros(client: TestClient, h: dict[str, str], **params: Any) -> list[str]:
    r = client.get("/api/reportes/cobranzas", headers=h, params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return sorted(x["numero"] for x in r.json()["items"])


def test_cobranzas_filtra_por_fecha_estatus(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    hoy, antes = today_local().isoformat(), e["dia_antes"].isoformat()
    # Hace dos días: solo la factura entregada ese día (no el resto de su remesa)
    assert _numeros(client, sup, desde=antes, hasta=antes) == [e["numeros"][0]]
    # Hoy: la entregada hoy, no la de hace dos días. La que sigue en Preparado tiene como Fecha Estatus su
    # Fecha Preparado (la «Fecha Alm» del Excel), no el día de la carga
    de_hoy = _numeros(client, sup, desde=hoy, hasta=hoy)
    assert e["numeros"][1] in de_hoy and e["numeros"][2] not in de_hoy and e["numeros"][0] not in de_hoy
    assert _numeros(client, sup, desde=hoy, hasta=hoy, solo_finales=True) == [e["numeros"][1]]
    # El rango abarca ambos días
    assert {e["numeros"][0], e["numeros"][1]} <= set(_numeros(client, sup, desde=antes, hasta=hoy))

    # El Excel trae lo mismo y el rango en el título
    xls = client.get("/api/reportes/cobranzas.xlsx", headers=sup, params={"desde": antes, "hasta": antes})
    assert xls.status_code == 200
    ws = load_workbook(io.BytesIO(xls.content)).active
    assert ws is not None
    valores = [c.value for fila in ws.iter_rows() for c in fila if c.value is not None]
    assert e["numeros"][0] in valores and e["numeros"][1] not in valores
    assert any(isinstance(v, str) and v.startswith("Entregas para Cobranzas · Fecha Estatus") for v in valores)
    lejos = (today_local() + timedelta(days=30)).isoformat()
    r = client.get("/api/reportes/cobranzas.xlsx", headers=sup, params={"desde": lejos, "hasta": lejos})
    assert r.status_code == 404 and "Fecha Estatus" in r.json()["detail"]


def test_rango_de_fechas_invalido(client: TestClient, sup: dict[str, str]) -> None:
    hoy = today_local()
    al_reves = {"desde": hoy.isoformat(), "hasta": (hoy - timedelta(days=1)).isoformat()}
    muy_largo = {"desde": (hoy - timedelta(days=400)).isoformat(), "hasta": hoy.isoformat()}
    for params, texto in ((al_reves, "no puede ser posterior"), (muy_largo, "366 días")):
        for url in ("/api/reportes/cobranzas", "/api/reportes/cobranzas.xlsx", "/api/reportes/metricas"):
            r = client.get(url, headers=sup, params=params)
            assert r.status_code == 422 and texto in r.json()["detail"], (url, r.text)
        for url, body in (("/api/reportes/notificar/vendedores", {"modo": "final", "simular": True}),
                          ("/api/reportes/notificar/cobranzas", {"simular": True})):
            r = client.post(url, headers=sup, json={**body, **params})
            assert r.status_code == 422 and texto in r.json()["detail"], (url, r.text)


def test_vista_previa_de_vendedores_desglosada(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    hoy, antes = today_local().isoformat(), e["dia_antes"].isoformat()
    r = client.post("/api/reportes/notificar/vendedores", headers=sup,
                    json={"modo": "final", "desde": antes, "hasta": hoy, "simular": True}).json()
    assert r["enviados"] == [] and r["vendedores"] == len(r["desglose"]) >= 1
    lineas = [(v["codigo"], f["numero"], f["estatus"]) for v in r["desglose"] for f in v["facturas"]]
    assert len(lineas) == r["facturas"] == 2
    assert {n for _, n, _ in lineas} == {e["numeros"][0], e["numeros"][1]}
    assert all(est == "entregada" for _, _, est in lineas)
    v = r["desglose"][0]
    assert v["nombre"] and v["correo"] is None  # en el catálogo, sin correo (aparece igual en el desglose)
    assert {"pedido", "cliente", "estatus_at", "remesa"} <= set(v["facturas"][0])


def test_metrica_estrella_solo_supervisor(client: TestClient, sup: dict[str, str]) -> None:
    assert client.get("/api/reportes/metricas", headers=sup).status_code == 200
    assert "metricas.ver" in client.get("/api/me/permisos", headers=sup).json()["permisos"]
    for rol in ("transportista", "cobranzas"):
        datos = {"username": f"u_{rol}", "nombre": rol.title(), "rol": rol}
        if rol == "transportista":
            datos["empresa_id"] = crear_empresa(client, sup, "EMP M")
        cred = client.post("/api/admin/usuarios", headers=sup, json=datos).json()["credencial"]["clave_temporal"]
        h = login(client, f"u_{rol}", cred)
        assert client.get("/api/reportes/metricas", headers=h).status_code == 403
        assert "metricas.ver" not in client.get("/api/me/permisos", headers=h).json()["permisos"]
        assert client.get("/api/reportes/cobranzas", headers=h).status_code == 200  # el resto de reportes sí
