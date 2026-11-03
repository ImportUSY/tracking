"""v1.22: clave temporal «qwerty» con 3 accesos (con aviso) antes de exigir la clave personal."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tracking.security import MENSAJE_CAMBIO_OBLIGATORIO


def _entrar(client: TestClient, usuario: str, clave: str) -> tuple[dict[str, str], dict[str, Any]]:
    r = client.post("/api/auth/login", json={"username": usuario, "password": clave})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["usuario"]


def test_tres_accesos_con_la_clave_temporal(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    r = client.post("/api/admin/usuarios", headers=sup,
                    json={"username": "nuevo1", "nombre": "Nuevo Uno", "rol": "transportista", "empresa_id": empresa})
    assert r.status_code == 201 and r.json()["credencial"]["clave_temporal"] == "qwerty"
    # Accesos 1, 2 y 3: entra, con aviso de cuántos le quedan
    for quedan in (2, 1, 0):
        h, u = _entrar(client, "nuevo1", "qwerty")
        assert u["debe_cambiar_clave"] and u["accesos_temporales_restantes"] == quedan
        assert not u["cambio_clave_obligatorio"]
        assert client.get("/api/remesas", headers=h).status_code == 200
    # Cuarto acceso: solo puede crear su clave personal
    h, u = _entrar(client, "nuevo1", "qwerty")
    assert u["cambio_clave_obligatorio"] and u["accesos_temporales_restantes"] == 0
    r = client.get("/api/remesas", headers=h)
    assert r.status_code == 403 and r.json()["detail"] == MENSAJE_CAMBIO_OBLIGATORIO
    assert client.get("/api/me", headers=h).json()["cambio_clave_obligatorio"] is True
    # La clave temporal no sirve como clave personal
    r = client.post("/api/me/clave", headers=h, json={"clave_actual": "qwerty", "clave_nueva": "QWERTY"})
    assert r.status_code in (400, 422) and "temporal" in r.text
    r = client.post("/api/me/clave", headers=h, json={"clave_actual": "qwerty", "clave_nueva": "MiClave2026"})
    assert r.status_code == 200
    h, u = _entrar(client, "nuevo1", "MiClave2026")
    assert not u["debe_cambiar_clave"] and u["accesos_temporales_restantes"] is None
    assert client.get("/api/remesas", headers=h).status_code == 200


def test_conductor_y_restablecer_reinician_los_accesos(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    r = client.post("/api/admin/conductores", headers=sup, json={"cedula": "12345678", "nombre": "Chofer Temporal",
                                                                 "empresa_id": empresa})
    assert r.status_code == 201, r.text
    assert r.json()["credencial"]["clave_temporal"] == "qwerty"
    for _ in range(4):
        _h, u = _entrar(client, "12345678", "qwerty")
    assert u["cambio_clave_obligatorio"]
    # El supervisor le restablece la clave: otra vez «qwerty» y 3 accesos
    uid = r.json()["conductor"]["usuario_id"]
    r = client.post(f"/api/admin/usuarios/{uid}/clave", headers=sup)
    assert r.json()["clave_temporal"] == "qwerty"
    _h, u = _entrar(client, "12345678", "qwerty")
    assert u["accesos_temporales_restantes"] == 2 and not u["cambio_clave_obligatorio"]
    est = client.get(f"/api/admin/usuarios/{uid}/credencial", headers=sup).json()
    assert est["debe_cambiar_clave"] and est["accesos_clave_temporal"] == 1
