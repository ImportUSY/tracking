"""v1.30.0: fecha y hora del estatus Por Entregar (conductor la indica; Supervisor/Transportista la modifican)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.test_capturas import _usuario
from tests.test_reglas_y_bitacora import _escenario, _ev


def _dt(valor: str) -> datetime:
    d = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat()


def _facturas(client: TestClient, ana: dict[str, str]) -> list[int]:
    return [f["id"] for f in client.get("/api/conductor/facturas", headers=ana).json()["facturas"]]


def test_conductor_indica_fecha_por_entregar(client: TestClient, sup: dict[str, str]) -> None:
    esc = _escenario(client, sup)
    ana, tb = esc["ana"], esc["tb"]
    fid = _facturas(client, ana)[0]
    det = client.get(f"/api/facturas/{fid}", headers=tb).json()
    ahora = datetime.now(timezone.utc)
    elegida = (_dt(det["fecha_alm"]) + (ahora - _dt(det["fecha_alm"])) / 2).replace(microsecond=0)
    r = client.post("/api/conductor/eventos", headers=ana,
                    json={"eventos": [_ev(fid, "en_transito", ocurrido_at=_iso(elegida))]})
    assert r.json()["resultados"][0]["ok"], r.text
    det = client.get(f"/api/facturas/{fid}", headers=tb).json()
    assert det["estatus"] == "en_transito"
    assert _dt(det["fecha_tra"]) == elegida and _dt(det["estatus_at"]) == elegida


def test_transportista_modifica_fecha_por_entregar(client: TestClient, sup: dict[str, str]) -> None:
    esc = _escenario(client, sup)
    ana, tb = esc["ana"], esc["tb"]
    fid = _facturas(client, ana)[0]
    url = f"/api/facturas/{fid}/fecha-por-entregar"
    ahora = datetime.now(timezone.utc)

    # Preparado: todavía no hay fecha Por Entregar que modificar
    r = client.put(url, headers=tb, json={"fecha": _iso(ahora)})
    assert r.status_code == 409

    client.post("/api/conductor/eventos", headers=ana, json={"eventos": [_ev(fid, "en_transito")]})
    det = client.get(f"/api/facturas/{fid}", headers=tb).json()
    alm = _dt(det["fecha_alm"])
    nueva = (alm + (ahora - alm) / 2).replace(microsecond=0)

    assert client.put(url, headers=tb, json={"fecha": _iso(ahora + timedelta(hours=2))}).status_code == 422
    r = client.put(url, headers=tb, json={"fecha": _iso(alm - timedelta(days=1))})
    assert r.status_code == 422 and "Fecha Preparado" in r.json()["detail"]

    r = client.put(url, headers=tb, json={"fecha": _iso(nueva), "motivo": "Salió más temprano"})
    assert r.status_code == 200, r.text
    det = client.get(f"/api/facturas/{fid}", headers=tb).json()
    assert _dt(det["fecha_tra"]) == nueva and _dt(det["estatus_at"]) == nueva
    ev = next(e for e in det["eventos"] if e["estatus"] == "en_transito")
    assert _dt(ev["ocurrido_at"]) == nueva

    aud = client.get("/api/admin/auditoria?limite=50", headers=sup).json()
    reg = next(a for a in aud if a["accion"] == "fecha_por_entregar_modificada")
    assert reg["detalle"]["factura_id"] == fid and reg["detalle"]["motivo"] == "Salió más temprano"


def test_fecha_por_entregar_no_pasa_del_estatus_siguiente(client: TestClient, sup: dict[str, str]) -> None:
    ana = _escenario(client, sup)["ana"]
    fid = _facturas(client, ana)[0]
    client.post("/api/conductor/eventos", headers=ana, json={"eventos": [_ev(fid, "en_transito")]})
    client.post("/api/conductor/eventos", headers=ana, json={"eventos": [_ev(fid, "entregada")]})
    det = client.get(f"/api/facturas/{fid}", headers=sup).json()
    rec = _dt(det["fecha_rec"])
    url = f"/api/facturas/{fid}/fecha-por-entregar"
    r = client.put(url, headers=sup, json={"fecha": _iso(rec + timedelta(seconds=30))})
    assert r.status_code == 422 and "estatus siguiente" in r.json()["detail"]
    antes = rec - timedelta(minutes=1)
    if antes > _dt(det["fecha_alm"]):
        r = client.put(url, headers=sup, json={"fecha": _iso(antes)})
        assert r.status_code == 200, r.text
        det = client.get(f"/api/facturas/{fid}", headers=sup).json()
        assert _dt(det["fecha_tra"]) == antes
        assert det["estatus"] == "entregada" and _dt(det["estatus_at"]) == rec


def test_panel_pasa_a_por_entregar_con_fecha(client: TestClient, sup: dict[str, str]) -> None:
    esc = _escenario(client, sup)
    ana, tb = esc["ana"], esc["tb"]
    fid = _facturas(client, ana)[0]
    det = client.get(f"/api/facturas/{fid}", headers=tb).json()
    alm = _dt(det["fecha_alm"])
    url = f"/api/facturas/{fid}/estatus"
    r = client.post(url, headers=tb, json={"estatus": "en_transito", "ocurrido_at": _iso(alm - timedelta(hours=1))})
    assert r.status_code == 422 and "Fecha Preparado" in r.json()["detail"]
    nueva = (alm + (datetime.now(timezone.utc) - alm) / 2).replace(microsecond=0)
    r = client.post(url, headers=tb, json={"estatus": "en_transito", "ocurrido_at": _iso(nueva)})
    assert r.status_code == 200, r.text
    det = client.get(f"/api/facturas/{fid}", headers=tb).json()
    assert det["estatus"] == "en_transito" and _dt(det["fecha_tra"]) == nueva


def test_cobranzas_no_modifica_fecha_por_entregar(client: TestClient, sup: dict[str, str]) -> None:
    esc = _escenario(client, sup)
    fid = _facturas(client, esc["ana"])[0]
    client.post("/api/conductor/eventos", headers=esc["ana"], json={"eventos": [_ev(fid, "en_transito")]})
    cob = _usuario(client, sup, "cobra", "cobranzas", None)
    r = client.put(f"/api/facturas/{fid}/fecha-por-entregar", headers=cob,
                   json={"fecha": _iso(datetime.now(timezone.utc))})
    assert r.status_code == 403
