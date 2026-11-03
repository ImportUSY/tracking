"""v1.18–1.19: Indicador de Gestión (tabla, gráfica y Excel), Fecha Preparado en el historial,
devolución y consulta pública."""

from __future__ import annotations

import io
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alembic import command
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine, text

from tests.conftest import login
from tests.factories import JPEG_MINIMO
from tests.test_capturas import ExtractorFalso, _confirmar
from tests.test_capturas import _escenario as _escenario_foto
from tests.test_flujo_completo import _cargar, _ev
from tests.test_multitenant import _usuario
from tracking.migrate import alembic_config
from tracking.services.timeutil import today_local

RANGO = {"desde": "2026-09-01", "hasta": today_local().isoformat()}


def _dt(texto: str) -> datetime:
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


def _escenario(client: TestClient, sup: dict[str, str]) -> dict[str, Any]:
    ctx = _cargar(client, sup)
    client.post(f"/api/importaciones/{ctx['preview']['id']}/confirmar", headers=sup, json={})
    ana = login(client, "11111111", ctx["creds"]["11111111"])
    facts = {f["numero"]: f["id"] for f in client.get("/api/conductor/facturas", headers=ana).json()["facturas"]}
    u1, u2 = facts["FAC-U00000001"], facts["FAC-U00000002"]
    client.post("/api/conductor/eventos", headers=ana,
                json={"eventos": [_ev(u1, "en_transito"), _ev(u2, "en_transito")]})
    client.post("/api/conductor/eventos", headers=ana, json={"eventos": [_ev(u1, "entregada")]})
    foto = str(uuid.uuid4())
    client.put(f"/api/fotos/{foto}", headers=ana, data={"factura_id": u2},
               files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    r = client.post("/api/conductor/eventos", headers=ana,
                    json={"eventos": [_ev(u2, "devolucion", foto_id=foto, motivo="Cerrado")]})
    assert r.json()["resultados"][0]["aplicado"], r.text
    return {**ctx, "u1": u1, "u2": u2}


def _ind(client: TestClient, h: dict[str, str], **params: Any) -> dict[str, Any]:
    r = client.get("/api/reportes/indicador", headers=h, params={**RANGO, "limit": 500, **params})
    assert r.status_code == 200, r.text
    return dict(r.json())


def test_indicador_de_gestion(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    r = _ind(client, sup)
    assert r["total"] == 5  # la remesa del chofer no registrado no se carga
    # En pantalla, las mismas 13 columnas del Excel y en el mismo orden (sin montos)
    claves = [c["clave"] for c in r["columnas"]]
    assert claves == ["numero", "cliente", "estatus", "fecha_fac", "estatus_at", "t_preparado", "t_por_entregar",
                      "t_entrega", "observacion_tiempo", "cod_vendedor", "destino", "chofer_nombre",
                      "empresa_transporte"]
    s = r["resumen"]
    assert (s["facturas"], s["entregadas"], s["devoluciones"], s["preparadas"]) == (5, 1, 1, 3)
    filas = {x["numero"]: x for x in r["items"]}
    entregada, devuelta = filas["FAC-U00000001"], filas["FAC-U00000002"]
    assert entregada["t_total"] and entregada["t_total_min"] is not None and entregada["estatus"] == "Entregada"
    # Devolución: los mismos tramos que una entrega; el último termina en la devolución
    assert devuelta["t_preparado"] and devuelta["t_por_entregar"] and devuelta["t_entrega"]
    assert devuelta["t_total_min"] == sum(devuelta[f"{k}_min"] for k in ("t_preparado", "t_por_entregar", "t_entrega"))
    assert devuelta["fecha_rec"] == devuelta["estatus_at"]
    assert "montos" not in s  # SUMA y PROMEDIO solo de tiempos, nunca de montos
    assert devuelta["observacion_tiempo"] == "No se entregó - Devolución"
    # Sumatoria: suma de los minutos hábiles de las filas
    total = sum(x["t_total_min"] for x in r["items"] if x["t_total_min"] is not None)
    assert s["duraciones"]["t_total"]["suma_min"] == total

    # Filtros: estatus (uno o varios), cliente, vendedor, zona (destino), empresa y campo de fecha
    assert {x["numero"] for x in _ind(client, sup, estatus="entregada")["items"]} == {"FAC-U00000001"}
    assert _ind(client, sup, estatus=["entregada", "devolucion"])["total"] == 2
    assert [x["numero"] for x in _ind(client, sup, cliente="cliente 3")["items"]] == ["FAC-U00000003"]
    assert {x["numero"] for x in _ind(client, sup, cod_vendedor="dc-01005")["items"]} == {"FAC-U00000003"}
    assert {x["destino"] for x in _ind(client, sup, zona="oriente")["items"]} == {"ORIENTE"}
    assert _ind(client, sup, empresa_id=e["empresa"])["total"] == 5
    assert _ind(client, sup, empresa_id=e["empresa"] + 99)["total"] == 0
    assert _ind(client, sup, campo_fecha="fecha_rec", desde=today_local().isoformat())["total"] == 1
    assert _ind(client, sup, campo_fecha="fecha_fac", desde="2026-09-15", hasta="2026-09-30")["total"] == 0
    assert _ind(client, sup, campo_fecha="fecha_fac", desde="2026-09-14", hasta="2026-09-14")["total"] == 5
    assert client.get("/api/reportes/indicador", headers=sup, params={**RANGO, "campo_fecha": "x"}).status_code == 422
    assert client.get("/api/reportes/indicador", headers=sup, params={**RANGO, "estatus": "cargado"}).status_code == 422
    # Orden por duración (minutos), vacíos al final
    orden = _ind(client, sup, orden="t_total", desc=True)["items"]
    mins = [x["t_total_min"] for x in orden if x["t_total_min"] is not None]
    assert mins == sorted(mins, reverse=True)

    op = client.get("/api/reportes/indicador/opciones", headers=sup).json()
    assert {"LARA", "ORIENTE"} <= set(op["zonas"]) and op["vendedores"] and op["empresas"]

    # Gráfica: barras por empresa de transporte (cantidad y %) y desglose por empresa emisora.
    # Las devoluciones se suman a Entregado.
    g = r["grafica"]
    assert g["titulo"] == "Indicador de Gestión"
    assert [(x["clave"], x["titulo"]) for x in g["estatus"]] == [
        ("recibida", "Preparado"), ("en_transito", "Por Entregar"), ("entregada", "Entregado")]
    assert [x["nombre"] for x in g["empresas"]] == ["TRANSPORTES DEMO"]
    barra = g["empresas"][0]
    assert barra["total"] == 5 and barra["devoluciones"] == 1
    assert barra["cantidades"] == {"recibida": 3, "en_transito": 0, "entregada": 2}
    assert barra["porcentajes"]["recibida"] == 60.0 and barra["porcentajes"]["entregada"] == 40.0
    assert {x["empresa"]: x["facturas"] for x in g["desglose"]["entregada"]["*"]} == {"IMPORTADORA USY": 2}
    assert "devolucion" not in g["desglose"] and r["jornada"] == "Días hábiles: de lunes a viernes de 7:00 a 16:00"
    prep = g["desglose"]["recibida"]["*"]
    assert {x["empresa"]: x["facturas"] for x in prep} == {"CORP DIJONAS": 2, "IMPORTADORA USY": 1}
    assert prep[0] == {"empresa": "CORP DIJONAS", "facturas": 2, "porcentaje": 66.7}
    assert g["desglose"]["recibida"]["TRANSPORTES DEMO"] == prep and g["desglose"]["en_transito"] == {}
    # La gráfica sigue a los filtros y cubre todas las filas, no solo la página
    assert _ind(client, sup, limit=1)["grafica"] == g
    solo = _ind(client, sup, estatus="entregada")["grafica"]["empresas"][0]
    assert solo["total"] == 1 and solo["porcentajes"]["entregada"] == 100.0

    # Excel: exactamente 13 columnas, en este orden, sin títulos, totales ni montos
    x = client.get("/api/reportes/indicador.xlsx", headers=sup, params=RANGO)
    assert x.status_code == 200
    ws = load_workbook(io.BytesIO(x.content)).active
    assert ws is not None
    assert [c.value for c in ws[1]] == [
        "Factura", "Cliente", "Estatus", "Fecha de factura", "Fecha último estatus",
        "Tiempo de Facturado a Preparado", "Tiempo de Preparado a Por Entregar",
        "Tiempo de Por Entregar a Entregado o Devolución", "Observación", "Cod. Vendedor", "Zona", "Chofer",
        "Empresa de transporte"]
    assert ws.max_column == 13 and ws.max_row == 6  # encabezado + 5 facturas
    filas_x = {f[0].value: [c.value for c in f] for f in ws.iter_rows(min_row=2)}
    dev = filas_x["FAC-U00000002"]
    assert dev[2] == "Devolución" and dev[8] == "No se entregó - Devolución" and dev[10] == "LARA"
    assert dev[11] == "Ana Chofer" and dev[12] == "TRANSPORTES DEMO" and dev[5] and dev[6] and dev[7]
    assert isinstance(dev[3], datetime) and isinstance(dev[4], datetime)
    vacio = client.get("/api/reportes/indicador.xlsx", headers=sup, params={**RANGO, "cliente": "nadie"})
    assert vacio.status_code == 404

    # Transportista: solo su empresa y sin montos; Cobranzas sí ve montos
    otra = client.post("/api/admin/empresas", headers=sup, json={"nombre": "OTRA"}).json()["id"]
    tb = _usuario(client, sup, "transp_b", "transportista", otra)
    assert _ind(client, tb)["total"] == 0
    ta = _usuario(client, sup, "transp_a", "transportista", e["empresa"])
    ra = _ind(client, ta)
    assert ra["total"] == 5 and "total_fact" not in {c["clave"] for c in ra["columnas"]}
    xa = client.get("/api/reportes/indicador.xlsx", headers=ta, params=RANGO)
    assert load_workbook(io.BytesIO(xa.content)).active.max_column == 13  # type: ignore[union-attr]
    assert len(client.get("/api/reportes/indicador/opciones", headers=ta).json()["empresas"]) == 1
    cob = _usuario(client, sup, "cobra2", "cobranzas", None)
    assert [c["clave"] for c in _ind(client, cob)["columnas"]] == claves  # nadie ve montos en el indicador


def test_devolucion_y_consulta_publica_con_tiempos(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    det = client.get(f"/api/facturas/{e['u2']}", headers=sup).json()
    t = det["tiempos"]
    assert det["estatus"] == "devolucion" and det["fecha_rec"] is None
    assert t["devolucion"] and t["mensaje"] == "No se entregó - Devolución"
    assert len(t["tramos"]) == 3 and t["tramos"][2]["hasta"] == "Fecha Devolución"
    assert _dt(t["tramos"][2]["fin"]) == _dt(det["estatus_at"]) and all(x["estado"] == "completo" for x in t["tramos"])
    # Consulta pública: mismo desglose de tiempos (sin montos)
    pub = client.get("/api/publico/tracking", params={"q": "FAC-U00000001"}).json()["resultados"][0]
    assert pub["tiempos"]["entregada"] and len(pub["tiempos"]["tramos"]) == 3 and pub["tiempos"]["total_texto"]
    assert "total_fact" not in str(pub)
    pub2 = client.get("/api/publico/tracking", params={"q": "FAC-U00000002"}).json()["resultados"][0]
    assert pub2["tiempos"]["mensaje"] == "No se entregó - Devolución"


def test_preparado_con_la_hora_de_la_foto(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario_foto(client, sup)
    client.app.state.extractor_facturas = ExtractorFalso()  # type: ignore[attr-defined]
    tomada = (datetime.now(timezone.utc) - timedelta(hours=5)).replace(microsecond=0)
    r = client.post("/api/capturas", headers=sup, data={"tomada_at": tomada.isoformat()},
                    files={"archivo": ("factura.jpg", JPEG_MINIMO, "image/jpeg")})
    assert r.status_code == 201, r.text
    cap = r.json()
    res = client.post(f"/api/capturas/{cap['id']}/confirmar", headers=sup,
                      json=_confirmar(cap, conductor_id=e["cid"])).json()
    f = client.get(f"/api/facturas/{res['factura_id']}", headers=sup).json()
    assert _dt(f["fecha_alm"]) == tomada == _dt(f["estatus_at"])
    assert [_dt(x["ocurrido_at"]) for x in f["eventos"] if x["estatus"] == "recibida"] == [tomada]
    # Una hora absurda del teléfono (futuro) se ignora: vale el momento de la subida
    r = client.post("/api/capturas", headers=sup, data={"tomada_at": "2099-01-01T00:00:00Z"},
                    files={"archivo": ("otra.jpg", JPEG_MINIMO + b"x", "image/jpeg")})
    assert r.status_code == 201, r.text


def test_migracion_0011_fecha_preparado(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    cfg = alembic_config()
    carga, excel, foto = "2026-10-20 15:00:00.000000", "2026-10-01 04:00:00.000000", "2026-10-19 13:30:00.000000"
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0010")
        conn.execute(text("INSERT INTO remesas (id, numero, numero_generado, created_at, updated_at) "
                          "VALUES (1, 'R-1', 0, :t, :t)"), {"t": carga})
        # 1 = Excel con Fecha Alm (sigue en Preparado) · 2 = Excel sin Fecha Alm · 3 = foto · 4 = Excel, ya avanzó
        for fid, alm, est in ((1, excel, "recibida"), (2, None, "recibida"), (3, carga, "recibida"),
                              (4, excel, "en_transito")):
            conn.execute(text("INSERT INTO facturas (id, remesa_id, numero, estatus, estatus_at, version, fecha_alm, "
                              "created_at, updated_at) VALUES (:i, 1, :n, :e, :t, 1, :a, :t, :t)"),
                         {"i": fid, "n": f"F-{fid}", "e": est, "a": alm, "t": carga})
            conn.execute(text("INSERT INTO eventos_estatus (id, factura_id, estatus, ocurrido_at, recibido_at, origen, "
                              "aplicado) VALUES (:id, :f, 'recibida', :t, :t, :o, 1)"),
                         {"id": f"p{fid}", "f": fid, "t": carga, "o": "captura" if fid == 3 else "importacion"})
        conn.execute(text("INSERT INTO eventos_estatus (id, factura_id, estatus, estatus_anterior, ocurrido_at, "
                          "recibido_at, origen, aplicado) VALUES ('t4', 4, 'en_transito', 'recibida', :t, :t, "
                          "'online', 1)"), {"t": "2026-10-21 12:00:00.000000"})
        conn.execute(text("INSERT INTO capturas_factura (id, estado, archivo_nombre, content_type, bytes, sha256, "
                          "ruta, motor, datos_extraidos, avisos, factura_id, created_at) VALUES ('c3', 'confirmada', "
                          "'f.jpg', 'image/jpeg', 1, 'x', 'db', 'manual', '{}', '[]', 3, :t)"), {"t": foto})
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        f = {i: (a, at) for i, a, at in conn.execute(text("SELECT id, fecha_alm, estatus_at FROM facturas"))}
        assert f[1] == (excel, excel) and f[2] == (carga, carga) and f[3] == (foto, foto)
        assert f[4] == (excel, carga)  # ya avanzó: su Fecha Estatus no cambia
        ev = dict(conn.execute(text("SELECT id, ocurrido_at FROM eventos_estatus WHERE estatus = 'recibida'")).all())
        assert ev == {"p1": excel, "p2": carga, "p3": foto, "p4": excel}
        command.downgrade(cfg, "0010")
    engine.dispose()


def test_fecha_fac_es_dia() -> None:
    assert date.fromisoformat(RANGO["desde"]) < today_local()
