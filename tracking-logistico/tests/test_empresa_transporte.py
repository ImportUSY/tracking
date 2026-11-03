"""Cambio de empresa de transporte de una remesa (solo SUPERVISOR) con actualización en cascada de sus facturas."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import crear_empresa, login, upload_remesa
from tests.test_capturas import _usuario
from tests.test_filtro_fecha_fac import _libro
from tracking.models import Factura, Remesa
from tracking.security import MENSAJE_SOLO_CONSULTA


def _conductor(client: TestClient, sup: dict[str, str], cedula: str, nombre: str, empresa: int) -> int:
    r = client.post(
        "/api/admin/conductores",
        headers=sup,
        json={"cedula": cedula, "nombre": nombre, "empresa_id": empresa, "placa": f"P{cedula[-4:]}"},
    )
    assert r.status_code == 201, r.text
    return int(r.json()["conductor"]["id"])


@pytest.fixture()
def escenario(client: TestClient, sup: dict[str, str]) -> dict[str, Any]:
    """Empresa A con el conductor Luis (dueño de las 3 remesas del libro), empresa B con dos conductores."""
    a = crear_empresa(client, sup, "TRANSPORTES A")
    b = crear_empresa(client, sup, "TRANSPORTES B")
    luis = _conductor(client, sup, "15111222", "Luis Herrera", a)
    pedro = _conductor(client, sup, "21000111", "Pedro Silva", b)
    up = upload_remesa(client, sup, _libro())
    assert up.status_code == 201, up.text
    assert client.post(f"/api/importaciones/{up.json()['id']}/confirmar", headers=sup, json={}).status_code == 200
    remesas = {m["numero"]: m for m in client.get("/api/remesas", headers=sup).json()["items"]}
    return {"a": a, "b": b, "luis": luis, "pedro": pedro, "remesas": remesas}


def _facturas_bd(client: TestClient, remesa_id: int) -> list[Factura]:
    with client.app.state.db.session() as db:  # type: ignore[attr-defined]
        return list(db.scalars(select(Factura).where(Factura.remesa_id == remesa_id)))


def test_supervisor_cambia_empresa_en_cascada(
    client: TestClient, sup: dict[str, str], escenario: dict[str, Any]
) -> None:
    rem = escenario["remesas"]["2026-01001"]
    otra = escenario["remesas"]["2026-01002"]
    assert rem["empresa_transporte"] == "TRANSPORTES A" and rem["total_facturas"] == 4
    r = client.put(
        f"/api/remesas/{rem['id']}/empresa-transporte",
        headers=sup,
        json={"empresa_id": escenario["b"], "conductor_id": escenario["pedro"]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["facturas_actualizadas"] == 4 and out["empresa_anterior"] == "TRANSPORTES A"
    assert out["empresa_nueva"] == "TRANSPORTES B" and out["conductor_nombre"] == "Pedro Silva"
    assert out["remesa"]["empresa_transporte"] == "TRANSPORTES B" and out["remesa"]["chofer_nombre"] == "Pedro Silva"

    # Cascada en la base: todas las facturas de la remesa, ninguna de las demás remesas
    assert {(f.empresa_id, f.conductor_id) for f in _facturas_bd(client, rem["id"])} == {
        (escenario["b"], escenario["pedro"])
    }
    assert {f.empresa_id for f in _facturas_bd(client, otra["id"])} == {escenario["a"]}

    # Se ve en los listados y quedó en Auditoría
    facts = client.get("/api/facturas", headers=sup, params={"limit": 100}).json()["items"]
    assert {f["empresa_transporte"] for f in facts if f["remesa_id"] == rem["id"]} == {"TRANSPORTES B"}
    aud = client.get("/api/admin/auditoria", headers=sup).json()[0]
    assert aud["accion"] == "empresa_transporte_cambiada"
    assert aud["detalle"]["empresa_nueva"] == "TRANSPORTES B" and aud["detalle"]["facturas"] == 4

    # Aislamiento: la empresa A ya no la ve; la B sí, y su conductor la recibe en la app
    ta = _usuario(client, sup, "transp_a", "transportista", escenario["a"])
    tb = _usuario(client, sup, "transp_b", "transportista", escenario["b"])
    assert client.get(f"/api/remesas/{rem['id']}", headers=ta).status_code == 404
    det = client.get(f"/api/remesas/{rem['id']}", headers=tb).json()
    assert det["empresa_transporte"] == "TRANSPORTES B" and len(det["facturas"]) == 4
    pedro = login(client, "21000111", "qwerty")
    assert len(client.get("/api/conductor/facturas", headers=pedro).json()["facturas"]) == 4
    luis = login(client, "15111222", "qwerty")
    numeros_luis = {f["numero"] for f in client.get("/api/conductor/facturas", headers=luis).json()["facturas"]}
    assert not numeros_luis & {f["numero"] for f in det["facturas"]}


def test_desde_la_factura_mueve_toda_la_remesa_sin_conductor(
    client: TestClient, sup: dict[str, str], escenario: dict[str, Any]
) -> None:
    rem = escenario["remesas"]["2026-01002"]
    una = client.get(f"/api/remesas/{rem['id']}", headers=sup).json()["facturas"][0]
    r = client.put(f"/api/facturas/{una['id']}/empresa-transporte", headers=sup, json={"empresa_id": escenario["b"]})
    assert r.status_code == 200, r.text
    assert r.json()["facturas_actualizadas"] == 2 and r.json()["conductor_nombre"] is None
    with client.app.state.db.session() as db:  # type: ignore[attr-defined]
        remesa = db.get(Remesa, rem["id"])
        assert remesa is not None and remesa.empresa_id == escenario["b"] and remesa.conductor_id is None
        assert remesa.chofer_nombre is None
    assert {(f.empresa_id, f.conductor_id) for f in _facturas_bd(client, rem["id"])} == {(escenario["b"], None)}
    # La empresa B la asigna a su conductor con «Reasignar»
    tb = _usuario(client, sup, "transp_b2", "transportista", escenario["b"])
    r = client.post(f"/api/remesas/{rem['id']}/reasignar", headers=tb, json={"conductor_id": escenario["pedro"]})
    assert r.status_code == 200 and r.json()["chofer_nombre"] == "Pedro Silva"


def test_solo_el_supervisor(client: TestClient, sup: dict[str, str], escenario: dict[str, Any]) -> None:
    rem = escenario["remesas"]["2026-01001"]
    fac = client.get(f"/api/remesas/{rem['id']}", headers=sup).json()["facturas"][0]["id"]
    cuerpo = {"empresa_id": escenario["b"]}
    otros = {
        "transportista": _usuario(client, sup, "transp_x", "transportista", escenario["a"]),
        "cobranzas": _usuario(client, sup, "cobra_x", "cobranzas", None),
        "conductor": login(client, "15111222", "qwerty"),
    }
    for rol, h in otros.items():
        assert client.put(f"/api/remesas/{rem['id']}/empresa-transporte", headers=h, json=cuerpo).status_code in (
            403,
            404,
        ), rol
        assert client.put(f"/api/facturas/{fac}/empresa-transporte", headers=h, json=cuerpo).status_code in (
            403,
            404,
        ), rol
    co = _usuario(client, sup, "consul_x", "consultor", None)
    r = client.put(f"/api/remesas/{rem['id']}/empresa-transporte", headers=co, json=cuerpo)
    assert r.status_code == 403 and r.json()["detail"] == MENSAJE_SOLO_CONSULTA
    # Transportista: no puede sacar una remesa de su empresa reasignándola a un conductor de otra
    r = client.post(
        f"/api/remesas/{rem['id']}/reasignar", headers=otros["transportista"], json={"conductor_id": escenario["pedro"]}
    )
    assert r.status_code == 404
    # Nada cambió; el permiso solo lo tiene el Supervisor (el Consultor solo ve el botón)
    assert {f.empresa_id for f in _facturas_bd(client, rem["id"])} == {escenario["a"]}
    perm = "remesas.empresa_transporte.cambiar"
    assert perm in client.get("/api/me/permisos", headers=sup).json()["permisos"]
    assert perm not in client.get("/api/me/permisos", headers=otros["transportista"]).json()["permisos_interfaz"]
    assert perm not in client.get("/api/me/permisos", headers=otros["cobranzas"]).json()["permisos_interfaz"]
    pco = client.get("/api/me/permisos", headers=co).json()
    assert perm not in pco["permisos"] and perm in pco["permisos_interfaz"]


def test_validaciones(client: TestClient, sup: dict[str, str], escenario: dict[str, Any]) -> None:
    rem = escenario["remesas"]["2026-01001"]
    url = f"/api/remesas/{rem['id']}/empresa-transporte"
    r = client.put(url, headers=sup, json={"empresa_id": escenario["b"], "conductor_id": escenario["luis"]})
    assert r.status_code == 422 and "no pertenece a TRANSPORTES B" in r.json()["detail"]
    r = client.put(url, headers=sup, json={"empresa_id": escenario["a"]})
    assert r.status_code == 409 and "ya pertenece" in r.json()["detail"]
    assert client.put(url, headers=sup, json={"empresa_id": 999}).status_code == 404
    assert client.put(url, headers=sup, json={"empresa_id": escenario["b"], "conductor_id": 999}).status_code == 404
    c = crear_empresa(client, sup, "TRANSPORTES C")
    client.patch(f"/api/admin/empresas/{c}", headers=sup, json={"activo": False})
    r = client.put(url, headers=sup, json={"empresa_id": c})
    assert r.status_code == 422 and "inactiva" in r.json()["detail"]
    assert {f.empresa_id for f in _facturas_bd(client, rem["id"])} == {escenario["a"]}


def test_reasignar_a_conductor_de_otra_empresa_es_cascada(
    client: TestClient, sup: dict[str, str], escenario: dict[str, Any]
) -> None:
    rem = escenario["remesas"]["2026-01001"]
    r = client.post(f"/api/remesas/{rem['id']}/reasignar", headers=sup, json={"conductor_id": escenario["pedro"]})
    assert r.status_code == 200 and r.json()["empresa_transporte"] == "TRANSPORTES B"
    assert {f.empresa_id for f in _facturas_bd(client, rem["id"])} == {escenario["b"]}
    assert client.get("/api/admin/auditoria", headers=sup).json()[0]["accion"] == "empresa_transporte_cambiada"


def test_si_algo_falla_no_cambia_nada(
    client: TestClient, sup: dict[str, str], escenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tracking.models import Usuario
    from tracking.services import empresa_transporte as et

    rem = escenario["remesas"]["2026-01001"]

    def falla(*_: Any, **__: Any) -> None:
        raise RuntimeError("fallo simulado después de los UPDATE")

    monkeypatch.setattr(et.audit, "registrar", falla)
    with client.app.state.db.session() as db:  # type: ignore[attr-defined]
        remesa = db.get(Remesa, rem["id"])
        supervisor = db.scalar(select(Usuario).where(Usuario.username == "supervisor"))
        assert remesa is not None and supervisor is not None
        with pytest.raises(RuntimeError):
            et.cambiar_empresa_remesa(db, remesa, escenario["b"], None, supervisor)
        db.rollback()
    assert {(f.empresa_id, f.conductor_id) for f in _facturas_bd(client, rem["id"])} == {
        (escenario["a"], escenario["luis"])
    }
