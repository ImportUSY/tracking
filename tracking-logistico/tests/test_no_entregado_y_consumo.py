"""v1.27.0/v1.28.0: estatus «No Entregado» (foto + GPS), fotos por estatus desde el panel y Datos de Navegación."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.conftest import login
from tests.factories import JPEG_MINIMO
from tests.test_flujo_completo import _cargar, _ev
from tracking.enums import MENSAJE_NO_ENTREGADO, Estatus
from tracking.services.consumo_datos import normalizar_ruta
from tracking.services.indicador import _serie
from tracking.services.timeutil import today_local


def _preparar(client: TestClient, sup: dict[str, str]) -> tuple[dict[str, str], list[int]]:
    ctx = _cargar(client, sup)
    r = client.post(f"/api/importaciones/{ctx['preview']['id']}/confirmar", headers=sup, json={})
    assert r.status_code == 200, r.text
    ana = login(client, "11111111", ctx["creds"]["11111111"])
    r = client.post("/api/me/clave", headers=ana,
                    json={"clave_actual": ctx["creds"]["11111111"], "clave_nueva": "nueva-clave-ana"})
    ana = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ids = [f["id"] for f in client.get("/api/conductor/facturas", headers=ana).json()["facturas"]]
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(i, "en_transito") for i in ids]}).json()["resultados"]
    assert all(o["estatus_actual"] == "en_transito" for o in out)
    return ana, ids


def _foto(client: TestClient, h: dict[str, str], factura_id: int) -> str:
    foto_id = str(uuid.uuid4())
    r = client.put(f"/api/fotos/{foto_id}", headers=h, data={"factura_id": factura_id},
                   files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    assert r.status_code == 201, r.text
    return foto_id


def _post(client: TestClient, h: dict[str, str], ev: dict[str, Any]) -> dict[str, Any]:
    r = client.post("/api/conductor/eventos", headers=h, json={"eventos": [ev]})
    assert r.status_code == 200, r.text
    return r.json()["resultados"][0]  # type: ignore[no-any-return]


def test_orden_y_series() -> None:
    assert Estatus.EN_TRANSITO.orden < Estatus.NO_ENTREGADO.orden < Estatus.ENTREGADA.orden
    assert not Estatus.NO_ENTREGADO.es_final and Estatus.NO_ENTREGADO.label == "No Entregado"
    assert _serie("no_entregado") == "no_entregado" and _serie("devolucion") == "entregada"
    assert normalizar_ruta("/api/fotos/3f2a9c1e-1111-2222-3333-444455556666?x=1") == "/api/fotos/:id"
    assert normalizar_ruta("/api/conductor/facturas/12/entrega") == "/api/conductor/facturas/:id/entrega"


def test_no_entregado_con_foto_y_gps(client: TestClient, sup: dict[str, str]) -> None:
    ana, ids = _preparar(client, sup)
    out = _post(client, ana, _ev(ids[0], "no_entregado"))
    assert not out["ok"] and out["codigo_error"] == "invalido"  # sin foto

    foto = _foto(client, ana, ids[0])
    out = _post(client, ana, _ev(ids[0], "no_entregado", foto_id=foto, motivo="Local cerrado",
                                 latitud=10.4806, longitud=-66.9036, precision_m=12.5))
    assert out["ok"] and out["aplicado"] and out["estatus_actual"] == "no_entregado"

    # Sigue en Pendientes del conductor (no es final) y se puede entregar después
    facts = {f["id"]: f for f in client.get("/api/conductor/facturas", headers=ana).json()["facturas"]}
    assert facts[ids[0]]["estatus"] == "no_entregado"
    # No vuelve a Por Entregar desde la app
    assert _post(client, ana, _ev(ids[0], "en_transito"))["codigo_error"] == "conflicto"

    det = client.get(f"/api/facturas/{ids[0]}", headers=sup).json()
    ev = [e for e in det["eventos"] if e["estatus"] == "no_entregado"][-1]
    assert ev["latitud"] == 10.4806 and ev["longitud"] == -66.9036 and ev["precision_m"] == 12.5
    assert ev["foto_id"] == foto
    assert det["tiempos"]["mensaje"] == MENSAJE_NO_ENTREGADO
    assert det["fecha_rec"] is None and det["fecha_tra"] is not None

    # Reportes: aparece en el dashboard y en el indicador con su conteo
    assert client.get("/api/dashboard", headers=sup).json()["por_estatus"]["No Entregado"] == 1
    # Indicador de Gestión: barra propia, con desglose y clic que filtra la tabla
    hoy = today_local().isoformat()
    rango = {"desde": hoy, "hasta": hoy, "campo_fecha": "estatus_at"}
    ind = client.get("/api/reportes/indicador", headers=sup, params=rango)
    assert ind.status_code == 200, ind.text
    ind = ind.json()
    barra = ind["grafica"]["empresas"][0]
    assert barra["cantidades"]["no_entregado"] == 1 and barra["cantidades"]["en_transito"] == len(ids) - 1
    assert sum(x["facturas"] for x in ind["grafica"]["desglose"]["no_entregado"]["*"]) == 1
    sel = client.get("/api/reportes/indicador", headers=sup, params={**rango, "serie": "no_entregado"}).json()
    assert sel["total"] == 1 and sel["items"][0]["factura_id"] == ids[0]
    assert sel["seleccion"]["texto"].startswith("No Entregado")

    out = _post(client, ana, _ev(ids[0], "entregada"))
    assert out["ok"] and out["estatus_actual"] == "entregada"

    # Latitud fuera de rango: 422
    r = client.post("/api/conductor/eventos", headers=ana,
                    json={"eventos": [_ev(ids[1], "entregada", latitud=95, longitud=0)]})
    assert r.status_code == 422


def test_panel_entregada_con_fecha_y_foto(client: TestClient, sup: dict[str, str]) -> None:
    _, ids = _preparar(client, sup)
    url = f"/api/facturas/{ids[1]}/estatus"
    assert client.post(url, headers=sup, json={"estatus": "entregada"}).status_code == 422  # falta la fecha
    recepcion = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
    r = client.post(url, headers=sup, json={"estatus": "entregada", "ocurrido_at": recepcion.isoformat()})
    assert r.status_code == 422 and "foto" in r.json()["detail"]
    futuro = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    foto = _foto(client, sup, ids[1])
    r = client.post(url, headers=sup, json={"estatus": "entregada", "ocurrido_at": futuro, "foto_id": foto})
    assert r.status_code == 409
    antes = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()  # antes de salir a entregar
    r = client.post(url, headers=sup, json={"estatus": "entregada", "ocurrido_at": antes, "foto_id": foto})
    assert r.status_code == 422 and "Fecha Por Entregar" in r.json()["detail"]

    r = client.post(url, headers=sup, json={"estatus": "entregada", "ocurrido_at": recepcion.isoformat(),
                                            "foto_id": foto})
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["estatus"] == "entregada" and f["evidencia_foto_id"] == foto
    assert datetime.fromisoformat(f["fecha_rec"]) == recepcion
    assert datetime.fromisoformat(f["estatus_at"]) == recepcion
    det = client.get(f"/api/facturas/{ids[1]}", headers=sup).json()
    ultimo = [e for e in det["eventos"] if e["aplicado"]][-1]
    assert ultimo["estatus"] == "entregada" and ultimo["foto_id"] == foto and ultimo["origen"] == "admin"
    assert det["tiene_foto"] and det["fotos_sueltas"] == []


def test_adjuntar_foto_desde_el_panel(client: TestClient, sup: dict[str, str]) -> None:
    _, ids = _preparar(client, sup)
    det = client.get(f"/api/facturas/{ids[2]}", headers=sup).json()
    assert det["tiene_foto"] is False

    foto = _foto(client, sup, ids[2])
    det = client.get(f"/api/facturas/{ids[2]}", headers=sup).json()
    assert det["fotos_sueltas"] == [foto]  # subida pero aún no ligada: se ve en el historial
    r = client.post(f"/api/facturas/{ids[2]}/foto", headers=sup, json={"foto_id": foto})
    assert r.status_code == 200, r.text
    det = client.get(f"/api/facturas/{ids[2]}", headers=sup).json()
    assert det["fotos_sueltas"] == [] and [e for e in det["eventos"] if e["aplicado"]][-1]["foto_id"] == foto

    otra = _foto(client, sup, ids[2])
    r = client.post(f"/api/facturas/{ids[2]}/foto", headers=sup, json={"foto_id": otra})
    assert r.status_code == 409  # ya tiene foto
    assert client.post(f"/api/facturas/{ids[2]}/foto", headers=sup, json={"foto_ids": []}).status_code == 422


def _foto_tomada(client: TestClient, h: dict[str, str], factura_id: int, hace: timedelta) -> tuple[str, datetime]:
    foto_id = str(uuid.uuid4())
    tomada = (datetime.now(timezone.utc) - hace).replace(microsecond=0)
    r = client.put(f"/api/fotos/{foto_id}", headers=h, data={"factura_id": factura_id,
                                                             "capturada_at": tomada.isoformat()},
                   files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    assert r.status_code == 201, r.text
    return foto_id, tomada


def test_panel_no_entregado_motivo_y_varias_fotos(client: TestClient, sup: dict[str, str], settings: Any) -> None:
    settings.public_evidence_enabled = True
    _, ids = _preparar(client, sup)
    url = f"/api/facturas/{ids[0]}/estatus"
    r = client.post(url, headers=sup, json={"estatus": "no_entregado"})
    assert r.status_code == 422 and "motivo" in r.json()["detail"]
    r = client.post(url, headers=sup, json={"estatus": "no_entregado", "motivo": "Cliente no disponible"})
    assert r.status_code == 422 and "foto" in r.json()["detail"]

    (f1, t1), (f2, t2) = (_foto_tomada(client, sup, ids[0], timedelta(minutes=m)) for m in (5, 3))
    r = client.post(url, headers=sup, json={"estatus": "no_entregado", "motivo": "Cliente no disponible",
                                            "foto_ids": [f1, f2]})
    assert r.status_code == 200, r.text
    det = client.get(f"/api/facturas/{ids[0]}", headers=sup).json()
    ev = [e for e in det["eventos"] if e["aplicado"]][-1]
    assert ev["estatus"] == "no_entregado" and ev["motivo"] == "Cliente no disponible"
    assert [(x["id"], _dt(x["capturada_at"])) for x in ev["fotos"]] == [(f1, t1), (f2, t2)]
    assert det["fotos_sueltas"] == [] and det["adjuntar_foto"] is False

    # Una foto de otra factura no se puede ligar
    ajena = _foto(client, sup, ids[1])
    r = client.post(url, headers=sup, json={"estatus": "entregada", "foto_ids": [ajena],
                                            "ocurrido_at": datetime.now(timezone.utc).isoformat()})
    assert r.status_code == 404

    # Entregada sin foto: se puede adjuntar después (varias) y el historial guarda todas
    r = client.post(url, headers=sup, json={"estatus": "entregada", "foto_id": _foto(client, sup, ids[0]),
                                            "ocurrido_at": datetime.now(timezone.utc).isoformat()})
    assert r.status_code == 200, r.text
    det = client.get(f"/api/facturas/{ids[0]}", headers=sup).json()
    entregada = [e for e in det["eventos"] if e["aplicado"]][-1]
    assert entregada["estatus"] == "entregada" and len(entregada["fotos"]) == 1

    # Seguimiento público («¿Dónde está mi pedido?»): cada hito trae sus fotos con hora de captura
    pub = client.get("/api/publico/tracking", params={"q": det["numero"]}).json()["resultados"][0]
    hitos = {h["estatus"]: h for h in pub["historial"]}
    no_ent = hitos["No Entregado"]["fotos"]
    assert len(no_ent) == 2 and all(x["url"].startswith("/api/publico/evidencias/") for x in no_ent)
    assert _dt(no_ent[0]["capturada_at"]) == t1
    assert len(hitos["Entregada"]["fotos"]) == 1
    assert client.get(no_ent[1]["url"]).status_code == 200

    # Si se corrige hacia atrás, las fotos de los estatus posteriores dejan de ser públicas
    r = client.post(url, headers=sup, json={"estatus": "en_transito", "motivo": "Error de carga"})
    assert r.status_code == 200, r.text
    assert client.get(no_ent[1]["url"]).status_code == 404


def test_adjuntar_foto_a_entregada_sin_foto(client: TestClient, sup: dict[str, str]) -> None:
    ana, ids = _preparar(client, sup)
    assert _post(client, ana, _ev(ids[1], "entregada"))["ok"]
    det = client.get(f"/api/facturas/{ids[1]}", headers=sup).json()
    assert det["adjuntar_foto"] is True and det["tiene_foto"] is False
    fotos = [_foto(client, sup, ids[1]) for _ in range(2)]
    r = client.post(f"/api/facturas/{ids[1]}/foto", headers=sup, json={"foto_ids": fotos})
    assert r.status_code == 200, r.text
    assert r.json()["evidencia_foto_id"] == fotos[0]
    det = client.get(f"/api/facturas/{ids[1]}", headers=sup).json()
    assert [x["id"] for x in det["eventos"][-1]["fotos"]] == fotos and det["adjuntar_foto"] is False


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def test_datos_de_navegacion(client: TestClient, sup: dict[str, str]) -> None:
    ana, _ = _preparar(client, sup)
    hoy = today_local().isoformat()
    ahora = datetime.now(timezone.utc).isoformat()
    reg = {"dia": hoy, "conexion_id": "abcd1234-0000", "metodo": "get", "ruta": "/api/conductor/facturas?x=1",
           "tipo_efectivo": "4g", "solicitudes": 2, "bytes_subida": 900, "bytes_bajada": 15000,
           "desde": ahora, "hasta": ahora}
    for _ in range(2):
        r = client.post("/api/conductor/consumo", headers=ana, json={"registros": [reg]})
        assert r.status_code == 200 and r.json() == {"registros": 1}
    assert client.post("/api/conductor/consumo", headers=sup, json={"registros": [reg]}).status_code == 403
    assert client.get("/api/admin/consumo-datos", headers=ana).status_code == 403

    r = client.get("/api/admin/consumo-datos", headers=sup, params={"desde": hoy, "hasta": hoy})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    fila = body["items"][0]
    assert fila["ruta"] == "/api/conductor/facturas" and fila["metodo"] == "GET"
    assert fila["bytes_subida"] == 1800 and fila["bytes_bajada"] == 30000 and fila["solicitudes"] == 4
    assert body["totales"]["bytes_total"] == 31800 and body["totales"]["conductores"] == 1
    for agrupar in ("conductor", "dia", "ruta", "conexion"):
        r = client.get("/api/admin/consumo-datos", headers=sup, params={"desde": hoy, "agrupar": agrupar})
        assert r.status_code == 200 and r.json()["items"][0]["bytes_total"] == 31800, agrupar
    assert client.get("/api/admin/consumo-datos", headers=sup, params={"agrupar": "otra"}).status_code == 422
    r = client.get("/api/admin/consumo-datos", headers=sup, params={"desde": hoy, "ruta": "/api/fotos"})
    assert r.json()["total"] == 0

    # Cada hora es un registro aparte, con su fecha y hora exactas
    assert fila["hora"] is not None and _dt(fila["desde"]) == _dt(ahora)
    otra_hora = dict(reg, hora=(fila["hora"] + 1) % 24)
    assert client.post("/api/conductor/consumo", headers=ana, json={"registros": [otra_hora]}).status_code == 200
    body = client.get("/api/admin/consumo-datos", headers=sup, params={"desde": hoy, "hasta": hoy}).json()
    assert body["total"] == 2 and sorted(x["hora"] for x in body["items"]) == sorted({fila["hora"], otra_hora["hora"]})
    r = client.get("/api/admin/consumo-datos", headers=sup, params={"desde": hoy, "agrupar": "dia"})
    assert r.json()["total"] == 1  # las métricas por día no cambian

    # Gráficas: Enviado / Recibido por conductor y consumo total por conexión (todas las filas del filtro)
    g = r.json()["grafica"]
    assert len(g["conductores"]) == 1 and g["conductores"][0]["bytes_subida"] == 2700
    assert g["conductores"][0]["bytes_bajada"] == 45000 and g["conductores"][0]["conexiones"] == 1
    con = g["conexiones"][0]
    assert con["conexion_id"] == "abcd1234-0000" and con["bytes_total"] == 47700 and con["desde"]

    # Excel: agrupación elegida + por conductor + por conexión, con gráficas
    x = client.get("/api/admin/consumo-datos.xlsx", headers=sup, params={"desde": hoy, "hasta": hoy})
    assert x.status_code == 200, x.text
    assert x.headers["content-disposition"].endswith('.xlsx"')
    wb = load_workbook(io.BytesIO(x.content))
    assert wb.sheetnames == ["Datos de Navegación", "Por conductor", "Por conexión"]
    assert wb["Datos de Navegación"]["A1"].value == "Fecha y hora" and wb["Datos de Navegación"].max_row == 3
    assert len(wb["Por conductor"]._charts) == 1 and len(wb["Por conexión"]._charts) == 1
    assert client.get("/api/admin/consumo-datos.xlsx", headers=ana).status_code == 403
    vacio = client.get("/api/admin/consumo-datos.xlsx", headers=sup, params={"desde": "2020-01-01"})
    assert vacio.status_code == 404

    # El envío automático de la medición no llena la bitácora
    bit = client.get("/api/admin/bitacora", headers=sup, params={"q": "/api/conductor/consumo"}).json()
    assert bit["total"] == 0
