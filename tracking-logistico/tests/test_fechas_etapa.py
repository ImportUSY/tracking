"""Fechas de etapa (v1.17.0): Fecha Preparado (fecha_alm), Por Entregar (fecha_tra) y Fecha Entregado
(fecha_rec, solo al entregar). «Cargado» y la Fecha Car ya no existen. Fecha Fac sigue siendo solo día."""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alembic import command
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine, inspect, text

from tests.conftest import login
from tests.test_capturas import ExtractorFalso, _confirmar, _escenario, _foto
from tests.test_flujo_completo import _cargar, _ev
from tracking.enums import Estatus
from tracking.migrate import alembic_config
from tracking.models import Factura
from tracking.services.cobranzas_excel import COL, HEADERS
from tracking.services.feriados import nacionales
from tracking.services.status import _actualizar_fechas_etapa
from tracking.services.tiempos import minutos_habiles
from tracking.services.timeutil import local_tz, now_utc, to_local

# 2026-10-15 01:00 UTC = 14-oct 21:00 en Caracas (UTC-4)
NOCHE = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)
DIA = timedelta(days=1)


def _dt(texto: str) -> datetime:
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))  # Python 3.10 no acepta «Z»


def _factura(estatus: Estatus, **kw: Any) -> Factura:
    return Factura(numero="1", estatus=estatus.value, **kw)


def test_regla_de_fechas_por_etapa() -> None:
    f = _factura(Estatus.RECIBIDA)
    _actualizar_fechas_etapa(f, Estatus.EN_TRANSITO, NOCHE, aplicado=True)
    assert (f.fecha_tra, f.fecha_rec) == (NOCHE, None)  # el momento exacto; sin entregar = «Por Entregar»
    f.estatus = Estatus.EN_TRANSITO.value
    entrega = NOCHE + 2 * DIA + timedelta(hours=3, minutes=17)
    _actualizar_fechas_etapa(f, Estatus.ENTREGADA, entrega, aplicado=True)
    assert (f.fecha_tra, f.fecha_rec) == (NOCHE, entrega)

    # Etapa saltada: Preparado → Entregada llena Por Entregar con el mismo momento
    g = _factura(Estatus.RECIBIDA)
    _actualizar_fechas_etapa(g, Estatus.ENTREGADA, NOCHE, aplicado=True)
    assert g.fecha_tra == g.fecha_rec == NOCHE
    # ...y si luego llega tarde (sin señal) el «Por Entregar» real de antes, corrige el momento deducido
    g.estatus = Estatus.ENTREGADA.value
    _actualizar_fechas_etapa(g, Estatus.EN_TRANSITO, NOCHE - DIA, aplicado=False)
    assert (g.fecha_tra, g.fecha_rec) == (NOCHE - DIA, NOCHE)

    # Devolución: salió pero no se entregó → sin Fecha Entregado
    _actualizar_fechas_etapa(g, Estatus.DEVOLUCION, NOCHE + DIA, aplicado=True)
    assert (g.fecha_tra, g.fecha_rec) == (NOCHE - DIA, None)
    # Retroceso administrativo: se borran las fechas de las etapas que quedan por delante
    _actualizar_fechas_etapa(g, Estatus.EN_TRANSITO, NOCHE + 3 * DIA, aplicado=True)
    assert (g.fecha_tra, g.fecha_rec) == (NOCHE + 3 * DIA, None)
    _actualizar_fechas_etapa(g, Estatus.RECIBIDA, NOCHE, aplicado=True)
    assert (g.fecha_tra, g.fecha_rec) == (None, None)


def test_estatus_cargado_eliminado() -> None:
    assert [e.label for e in Estatus] == ["Preparado", "Por Entregar", "No Entregado", "Entregada", "Devolución"]
    assert "cargado" not in {e.value for e in Estatus}


def test_captura_por_foto_solo_fecha_preparado(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    client.app.state.extractor_facturas = ExtractorFalso()  # type: ignore[attr-defined]
    cap = _foto(client, sup)
    res = client.post(f"/api/capturas/{cap['id']}/confirmar", headers=sup,
                      json=_confirmar(cap, conductor_id=e["cid"])).json()
    f = client.get(f"/api/facturas/{res['factura_id']}", headers=sup).json()
    assert abs(now_utc() - _dt(f["fecha_alm"])) < timedelta(minutes=1)  # Fecha Preparado: el momento del registro
    assert (f["fecha_tra"], f["fecha_rec"], f["fecha_fac"]) == (None, None, "2026-07-03")  # aún «Por Entregar»
    assert "fecha_car" not in f


def test_conductor_por_entregar_y_entrega_guardan_fechas(client: TestClient, sup: dict[str, str]) -> None:
    ctx = _cargar(client, sup)
    client.post(f"/api/importaciones/{ctx['preview']['id']}/confirmar", headers=sup, json={})
    ana = login(client, "11111111", ctx["creds"]["11111111"])
    ids = [f["id"] for f in client.get("/api/conductor/facturas", headers=ana).json()["facturas"]]
    salida, entrega = NOCHE, NOCHE + timedelta(days=1, hours=12)  # 14-oct 21:00 y 16-oct 09:00 (Caracas)
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(i, "en_transito", ocurrido_at=salida.isoformat()) for i in ids[:2]]}).json()
    assert all(o["ok"] and o["aplicado"] for o in out["resultados"]), out
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(ids[0], "entregada", ocurrido_at=entrega.isoformat())]}).json()
    assert out["resultados"][0]["aplicado"], out

    f = client.get(f"/api/facturas/{ids[0]}", headers=sup).json()
    assert _dt(f["fecha_tra"]) == salida and _dt(f["fecha_rec"]) == entrega
    # Fecha Preparado viene del Excel (solo día → 00:00 de Caracas); la Fecha Rec del Excel ya no se usa
    alm = _dt(f["fecha_alm"])
    assert to_local(alm).hour == 0
    por_entregar = client.get(f"/api/facturas/{ids[1]}", headers=sup).json()
    assert por_entregar["fecha_rec"] is None and _dt(por_entregar["fecha_tra"]) == salida
    sin_mover = client.get(f"/api/facturas/{ids[2]}", headers=sup).json()
    assert (sin_mover["fecha_tra"], sin_mover["fecha_rec"]) == (None, None)

    # El historial: «Preparado» con la Fecha Preparado del Excel (no el momento de la carga)
    prep = [e for e in f["eventos"] if e["estatus"] == "recibida"]
    assert len(prep) == 1 and _dt(prep[0]["ocurrido_at"]) == alm
    assert _dt(sin_mover["estatus_at"]) == _dt(sin_mover["fecha_alm"])  # sigue en Preparado: Fecha Estatus = Preparado

    # DÍAS TRANSCURRIDOS ENTRE PROCESOS: jornada laboral (lunes a viernes, 7:00–16:00; día = 9 h)
    t = f["tiempos"]
    fac = datetime.combine(date.fromisoformat(f["fecha_fac"]), datetime.min.time(), tzinfo=local_tz())
    # Sin feriados: del 14-sep al 14-oct de 2026 está el lunes 12 de octubre (Día de la Resistencia Indígena)
    fer = tuple(sorted(d for d, _ in nacionales(2026) if d.weekday() < 5))
    esperado = [minutos_habiles(fac, alm, fer), minutos_habiles(alm, salida, fer),
                minutos_habiles(salida, entrega, fer)]
    assert esperado[1] == minutos_habiles(alm, salida) - 540  # el feriado resta una jornada completa
    assert [x["minutos"] for x in t["tramos"]] == esperado and all(x["estado"] == "completo" for x in t["tramos"])
    assert t["tramos"][2]["texto"] == "1 día 2 h 00 min"  # mié 21:00 → vie 09:00 = jueves (9 h) + vie 7–9 (2 h)
    assert t["nota"] == "Días hábiles: de lunes a viernes de 7:00 a 16:00"
    assert t["total_minutos"] == sum(esperado) and t["entregada"] and not t["devolucion"]
    t1 = por_entregar["tiempos"]
    assert [x["estado"] for x in t1["tramos"]] == ["completo", "completo", "en_curso"] and t1["en_curso"]

    # Cobranzas (pantalla y Excel): Fecha Preparado → Fecha Por Entregar → Fecha Entregado, sin Fecha Car
    remesas = [r["id"] for r in client.get("/api/remesas", headers=sup).json()["items"]]
    tabla = client.get("/api/reportes/cobranzas", headers=sup, params={"remesa_ids": remesas}).json()
    claves = [c["clave"] for c in tabla["columnas"]]
    assert "fecha_car" not in claves
    assert claves.index("fecha_alm") + 1 == claves.index("fecha_tra") == claves.index("fecha_rec") - 1
    assert [HEADERS[COL[k] - 3] for k in ("fecha_alm", "fecha_tra", "fecha_rec")] == [
        "Fecha Preparado", "Fecha Por Entregar", "Fecha Entregado"]
    xlsx = client.get("/api/reportes/cobranzas.xlsx", headers=sup, params={"remesa_ids": remesas})
    assert xlsx.status_code == 200
    ws = load_workbook(io.BytesIO(xlsx.content)).active
    assert ws is not None
    valores = [c.value for fila_ in ws.iter_rows() for c in fila_]
    assert {"Fecha Preparado", "Fecha Por Entregar", "Fecha Entregado"} <= set(valores)
    assert "Fecha Car" not in valores and "Fecha Alm" not in valores
    assert datetime(2026, 10, 16, 9, 0) in valores  # Fecha Entregado en hora local


def test_migracion_0010_elimina_cargado(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    cfg = alembic_config()
    t0, t1, t2 = "2026-10-14 13:00:00.000000", "2026-10-15 01:00:00.000000", "2026-10-17 01:00:00.000000"
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0009")
        conn.execute(text("INSERT INTO remesas (id, numero, numero_generado, created_at, updated_at) "
                          "VALUES (1, 'R-1', 0, :t, :t)"), {"t": t0})
        # 1 = Cargado ahora · 2 = Entregada (Fecha Rec del Excel) · 3 = Preparado (Fecha Rec del Excel)
        # 4 = Por Entregar pasando antes por Cargado
        for fid, estatus, car, rec, at in ((1, "cargado", t1, None, t1), (2, "entregada", None, t0, t2),
                                           (3, "recibida", None, t0, t0), (4, "en_transito", t1, None, t2)):
            conn.execute(text("INSERT INTO facturas (id, remesa_id, numero, estatus, estatus_at, version, fecha_car, "
                              "fecha_rec, created_at, updated_at) VALUES (:i, 1, :n, :e, :a, 1, :c, :r, :t, :t)"),
                         {"i": fid, "n": f"F-{fid}", "e": estatus, "a": at, "c": car, "r": rec, "t": t0})
        eventos = [("a1", 1, "recibida", None, t0, 1), ("b1", 1, "cargado", "recibida", t1, 1),
                   ("c1", 1, "cargado", "cargado", t0, 0),  # repetido sin señal: solo historial
                   ("a4", 4, "cargado", "recibida", t1, 1), ("b4", 4, "en_transito", "cargado", t2, 1)]
        for eid, fid, estatus, ant, cuando, apl in eventos:
            conn.execute(text("INSERT INTO eventos_estatus (id, factura_id, estatus, estatus_anterior, ocurrido_at, "
                              "recibido_at, origen, aplicado) VALUES (:id, :f, :e, :ant, :t, :t, 'online', :ap)"),
                         {"id": eid, "f": fid, "e": estatus, "ant": ant, "t": cuando, "ap": apl})
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        facts = {i: (e, tra, rec) for i, e, tra, rec in conn.execute(
            text("SELECT id, estatus, fecha_tra, fecha_rec FROM facturas ORDER BY id"))}
        assert facts == {1: ("en_transito", t1, None), 2: ("entregada", None, t2),
                         3: ("recibida", None, None), 4: ("en_transito", None, None)}
        evs = {i: (e, ant) for i, e, ant in conn.execute(
            text("SELECT id, estatus, estatus_anterior FROM eventos_estatus"))}
        assert evs == {"a1": ("recibida", None), "b1": ("en_transito", "recibida"), "b4": ("en_transito", "recibida")}
        assert "fecha_car" not in {c["name"] for c in inspect(conn).get_columns("facturas")}
        command.downgrade(cfg, "0009")
        assert "fecha_car" in {c["name"] for c in inspect(conn).get_columns("facturas")}
    engine.dispose()
