"""v1.26: ingreso con la huella o el rostro del teléfono (WebAuthn), con un autenticador simulado."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import bytes_to_base64url

from tests.conftest import SUP_PASS, SUP_USER, login
from tracking.config import Settings
from tracking.main import create_app

ORIGEN = "http://testserver"
RP_ID = "testserver"


class Telefono:
    """Autenticador de plataforma simulado: llave P-256 propia, firma como lo haría el teléfono."""

    def __init__(self) -> None:
        self.llave = ec.generate_private_key(ec.SECP256R1())
        self.cid = os.urandom(16)
        self.contador = 0

    def _cliente(self, tipo: str, reto: str, origen: str = ORIGEN) -> bytes:
        return json.dumps({"type": tipo, "challenge": reto, "origin": origen, "crossOrigin": False}).encode()

    def _cose(self) -> bytes:
        n = self.llave.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def crear(self, opciones: dict[str, Any]) -> dict[str, Any]:
        assert opciones["rp"]["id"] == RP_ID
        assert opciones["authenticatorSelection"]["userVerification"] == "required"
        cd = self._cliente("webauthn.create", opciones["challenge"])
        auth = (hashlib.sha256(RP_ID.encode()).digest() + bytes([0x01 | 0x04 | 0x40]) + struct.pack(">I", 0)
                + b"\x00" * 16 + struct.pack(">H", len(self.cid)) + self.cid + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        cid = bytes_to_base64url(self.cid)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cd), "attestationObject": bytes_to_base64url(att),
                             "transports": ["internal"]}}

    def firmar(self, opciones: dict[str, Any], *, verificado: bool = True, origen: str = ORIGEN) -> dict[str, Any]:
        self.contador += 1
        cd = self._cliente("webauthn.get", opciones["challenge"], origen)
        flags = 0x01 | (0x04 if verificado else 0)
        auth = hashlib.sha256(RP_ID.encode()).digest() + bytes([flags]) + struct.pack(">I", self.contador)
        firma = self.llave.sign(auth + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.cid)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cd), "authenticatorData": bytes_to_base64url(auth),
                             "signature": bytes_to_base64url(firma)}}


def activar(client: TestClient, h: dict[str, str], clave: str, tel: Telefono) -> int:
    r = client.post("/api/me/biometria/opciones", headers=h, json={"clave": clave})
    assert r.status_code == 200, r.text
    o = r.json()
    cuerpo = {"reto_id": o["reto_id"], "credencial": tel.crear(o["opciones"])}
    r = client.post("/api/me/biometria", headers=h, json=cuerpo)
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def entrar(client: TestClient, tel: Telefono, **kw: Any) -> Any:
    o = client.post("/api/auth/biometria/opciones").json()
    return client.post("/api/auth/biometria/ingreso", json={"reto_id": o["reto_id"],
                                                             "credencial": tel.firmar(o["opciones"], **kw)})


def _conductor_con_clave(client: TestClient, sup: dict[str, str], empresa: int) -> tuple[int, dict[str, str]]:
    r = client.post("/api/admin/conductores", headers=sup,
                    json={"cedula": "15111222", "nombre": "Chofer Huella", "empresa_id": empresa})
    assert r.status_code == 201, r.text
    h = login(client, "15111222", "qwerty")
    r = client.post("/api/me/clave", headers=h, json={"clave_actual": "qwerty", "clave_nueva": "Chofer-2026"})
    assert r.status_code == 200, r.text
    return int(r.json()["usuario"]["id"]), {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_activar_y_entrar_con_huella(client: TestClient, sup: dict[str, str]) -> None:
    tel = Telefono()
    # Activarla exige la clave actual
    r = client.post("/api/me/biometria/opciones", headers=sup, json={"clave": "otra-clave"})
    assert r.status_code == 400 and "clave actual" in r.text
    activar(client, sup, SUP_PASS, tel)
    lista = client.get("/api/me/biometria", headers=sup).json()
    assert len(lista) == 1 and lista[0]["ultimo_uso_at"] is None

    r = entrar(client, tel)
    assert r.status_code == 200, r.text
    assert r.json()["usuario"]["username"] == SUP_USER
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/me", headers=h).status_code == 200
    assert client.get("/api/me/biometria", headers=sup).json()[0]["ultimo_uso_at"] is not None
    bit = client.get("/api/admin/bitacora", headers=sup, params={"accion": "LOGIN"})
    assert bit.status_code == 200, bit.text
    assert any((f.get("detalle") or {}).get("metodo") == "biometria" for f in bit.json()["items"])


def test_respuestas_invalidas_se_rechazan(client: TestClient, sup: dict[str, str]) -> None:
    tel = Telefono()
    activar(client, sup, SUP_PASS, tel)
    # El mismo reto no sirve dos veces
    o = client.post("/api/auth/biometria/opciones").json()
    cuerpo = {"reto_id": o["reto_id"], "credencial": tel.firmar(o["opciones"])}
    assert client.post("/api/auth/biometria/ingreso", json=cuerpo).status_code == 200
    assert client.post("/api/auth/biometria/ingreso", json=cuerpo).status_code == 401
    # Sin verificar la huella (solo presencia), desde otro sitio o con la firma alterada
    assert entrar(client, tel, verificado=False).status_code == 401
    assert entrar(client, tel, origen="https://sitio-falso.example").status_code == 401
    o = client.post("/api/auth/biometria/opciones").json()
    cred = tel.firmar(o["opciones"])
    cred["response"]["signature"] = bytes_to_base64url(b"\x30\x06\x02\x01\x01\x02\x01\x01")
    cuerpo = {"reto_id": o["reto_id"], "credencial": cred}
    assert client.post("/api/auth/biometria/ingreso", json=cuerpo).status_code == 401
    # Otro teléfono (llave no registrada)
    r = entrar(client, Telefono())
    assert r.status_code == 401 and "ya no está activada" in r.text
    # Con todo eso, la llave buena sigue funcionando
    assert entrar(client, tel).status_code == 200


def test_restablecer_la_clave_revoca_la_huella(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    uid, h = _conductor_con_clave(client, sup, empresa)
    tel = Telefono()
    activar(client, h, "Chofer-2026", tel)
    assert entrar(client, tel).status_code == 200
    # Teléfono perdido: la oficina le restablece la clave y la huella deja de servir
    r = client.post(f"/api/admin/usuarios/{uid}/clave", headers=sup)
    assert r.status_code == 200, r.text
    r = entrar(client, tel)
    assert r.status_code == 401 and "ya no está activada" in r.text


def test_usuario_inactivo_y_clave_temporal(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    uid, h = _conductor_con_clave(client, sup, empresa)
    tel = Telefono()
    activar(client, h, "Chofer-2026", tel)
    assert client.post(f"/api/admin/usuarios/{uid}/activo", headers=sup, json={"activo": False}).status_code == 200
    assert entrar(client, tel).status_code == 403
    assert client.post(f"/api/admin/usuarios/{uid}/activo", headers=sup, json={"activo": True}).status_code == 200
    assert entrar(client, tel).status_code == 200
    # Con clave temporal no se puede activar la huella
    r = client.post("/api/admin/conductores", headers=sup,
                    json={"cedula": "15999888", "nombre": "Chofer Nuevo", "empresa_id": empresa})
    h2 = login(client, "15999888", "qwerty")
    r = client.post("/api/me/biometria/opciones", headers=h2, json={"clave": "qwerty"})
    assert r.status_code == 400 and "clave personal" in r.text


def test_otro_telefono_entra_con_usuario_y_clave(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    _uid, h = _conductor_con_clave(client, sup, empresa)
    tel_a, tel_b = Telefono(), Telefono()
    activar(client, h, "Chofer-2026", tel_a)
    # En otro teléfono (sin huella) entra con su cédula y clave, como siempre
    r = client.post("/api/auth/login", json={"username": "15111222", "password": "Chofer-2026"})
    assert r.status_code == 200, r.text
    h_b = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/conductor/facturas", headers=h_b).status_code == 200
    # Y puede activar la huella también en ese teléfono: ambos sirven
    activar(client, h_b, "Chofer-2026", tel_b)
    assert len(client.get("/api/me/biometria", headers=h_b).json()) == 2
    assert entrar(client, tel_a).status_code == 200
    assert entrar(client, tel_b).status_code == 200
    # La sesión abierta con la huella no cierra la que se abrió con la clave
    assert client.get("/api/conductor/facturas", headers=h_b).status_code == 200


def test_quitar_la_huella(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    _uid, h = _conductor_con_clave(client, sup, empresa)
    tel = Telefono()
    cred_id = activar(client, h, "Chofer-2026", tel)
    # Nadie más puede quitarla
    assert client.delete(f"/api/me/biometria/{cred_id}", headers=sup).status_code == 404
    assert client.delete(f"/api/me/biometria/{cred_id}", headers=h).status_code == 204
    assert client.get("/api/me/biometria", headers=h).json() == []
    assert entrar(client, tel).status_code == 401


def test_consultor_puede_activar_su_huella(client: TestClient, sup: dict[str, str]) -> None:
    r = client.post("/api/admin/usuarios", headers=sup,
                    json={"username": "consulta1", "nombre": "Consultor Uno", "rol": "consultor"})
    assert r.status_code == 201, r.text
    h = login(client, "consulta1", "qwerty")
    r = client.post("/api/me/clave", headers=h, json={"clave_actual": "qwerty", "clave_nueva": "Consulta-2026"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    cred_id = activar(client, h, "Consulta-2026", Telefono())
    assert client.delete(f"/api/me/biometria/{cred_id}", headers=h).status_code == 204


def test_biometria_desactivada(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"biometria_habilitada": False}))
    with TestClient(app) as c:
        assert c.post("/api/auth/biometria/opciones").status_code == 404
        assert c.get("/api/config").json()["biometria"] is False
