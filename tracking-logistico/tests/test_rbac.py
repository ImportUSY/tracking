"""Control de acceso (RBAC): jerarquía de creación, gestión de claves y alcance del conductor."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import SUP_PASS, SUP_USER, crear_empresa, login, upload_remesa
from tests.factories import JPEG_MINIMO, libro_remesa
from tracking.config import Settings
from tracking.enums import Rol
from tracking.main import create_app
from tracking.permissions import MATRIZ, Permiso, permisos_de

CLAVE_T = "clave-transp-1"
CLAVE_C = "clave-cobra-1"


# --------------------------------------------------------------------------- utilidades
def _staff(client: TestClient, sup: dict[str, str]) -> dict[str, dict[str, str]]:
    empresa = crear_empresa(client, sup)
    for user, rol, clave in (("transp", "transportista", CLAVE_T), ("cobra", "cobranzas", CLAVE_C)):
        r = client.post("/api/admin/usuarios", headers=sup,
                        json={"username": user, "nombre": user.title(), "rol": rol, "clave": clave,
                              "empresa_id": empresa if rol == "transportista" else None})
        assert r.status_code == 201, r.text
    return {"sup": sup, "transp": login(client, "transp", CLAVE_T), "cobra": login(client, "cobra", CLAVE_C)}


def _conductor(client: TestClient, h: dict[str, str], cedula: str, nombre: str, clave: str) -> dict[str, str]:
    r = client.post("/api/admin/conductores", headers=h, json={"cedula": cedula, "nombre": nombre, "clave": clave})
    assert r.status_code == 201, r.text
    return login(client, cedula, clave)


def _escenario(client: TestClient, sup: dict[str, str]) -> dict[str, Any]:
    """Transportista crea a Ana y Beto; se carga una REMESA con facturas para ambos."""
    hs = _staff(client, sup)
    ana = _conductor(client, hs["transp"], "11111111", "Ana Chofer", "clave-ana-1")
    beto = _conductor(client, hs["transp"], "22222222", "Beto Chofer", "clave-beto-1")
    prev = upload_remesa(client, sup, libro_remesa()).json()
    r = client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=sup, json={})
    assert r.status_code == 200 and r.json()["facturas_creadas"] == 5, r.text
    fa = client.get("/api/conductor/facturas", headers=ana).json()["facturas"]
    fb = client.get("/api/conductor/facturas", headers=beto).json()["facturas"]
    return {**hs, "ana": ana, "beto": beto, "fa": fa, "fb": fb}


def _ev() -> dict[str, str]:
    return {"id": str(uuid.uuid4())}


# --------------------------------------------------------------------------- 1. matriz
def test_matriz_estatica(settings: Settings) -> None:
    cond = permisos_de(Rol.CONDUCTOR, settings)
    assert cond == {Permiso.CUENTA_PROPIA, Permiso.REMESAS_PROPIAS_VER, Permiso.FACTURAS_PROPIAS_VER,
                    Permiso.ESTATUS_OPERATIVO_REGISTRAR, Permiso.FOTOS_PROPIAS}
    assert Permiso.PERFIL_EDITAR not in cond
    assert Permiso.CONDUCTORES_CREAR in permisos_de(Rol.SUPERVISOR, settings)
    assert Permiso.CONDUCTORES_CREAR in permisos_de(Rol.TRANSPORTISTA, settings)
    assert Permiso.CONDUCTORES_CREAR not in permisos_de(Rol.COBRANZAS, settings)
    assert Permiso.CLAVES_USUARIOS_GESTIONAR in MATRIZ[Rol.SUPERVISOR]
    assert Permiso.CLAVES_USUARIOS_GESTIONAR not in MATRIZ[Rol.TRANSPORTISTA]
    settings.conductor_creator_roles = "transportista"
    assert Permiso.CONDUCTORES_CREAR not in permisos_de(Rol.SUPERVISOR, settings)
    assert Permiso.CONDUCTORES_CREAR in permisos_de(Rol.TRANSPORTISTA, settings)


def test_configuracion_invalida_de_creadores() -> None:
    with pytest.raises(ValueError, match="CONDUCTOR_CREATOR_ROLES"):
        Settings(_env_file=None, app_env="test", secret_key="k" * 40, conductor_creator_roles="cobranzas")


# --------------------------------------------------------------------------- 2. creación
def test_jerarquia_de_creacion_por_defecto(client: TestClient, sup: dict[str, str]) -> None:
    hs = _staff(client, sup)
    empresa = client.get("/api/me", headers=hs["transp"]).json()["empresa_id"]
    # Supervisor y Transportista crean conductores (por /conductores y por /usuarios)
    r = client.post("/api/admin/conductores", headers=sup, json={"cedula": "30000001", "nombre": "Uno"})
    assert r.status_code == 422 and "empresa" in r.json()["detail"]  # el Supervisor debe indicar la empresa
    assert client.post("/api/admin/conductores", headers=sup,
                       json={"cedula": "30000001", "nombre": "Uno", "empresa_id": empresa}).status_code == 201
    r = client.post("/api/admin/usuarios", headers=hs["transp"],
                    json={"rol": "conductor", "cedula": "V-30.000.002", "nombre": "Dos", "telefono": "4140000000"})
    assert r.status_code == 201, r.text
    assert r.json()["usuario"]["username"] == "30000002" and r.json()["usuario"]["rol"] == "conductor"
    assert r.json()["credencial"]["clave_temporal"]
    # Cobranzas y Conductor: denegado
    assert client.post("/api/admin/conductores", headers=hs["cobra"],
                       json={"cedula": "30000003", "nombre": "Tres"}).status_code == 403
    cond = login(client, "30000002", r.json()["credencial"]["clave_temporal"])
    assert client.post("/api/admin/conductores", headers=cond,
                       json={"cedula": "30000004", "nombre": "Cuatro"}).status_code == 403
    assert client.post("/api/admin/usuarios", headers=cond,
                       json={"rol": "conductor", "cedula": "30000005", "nombre": "Cinco"}).status_code == 403
    # El Transportista NO crea otros roles
    r = client.post("/api/admin/usuarios", headers=hs["transp"],
                    json={"username": "otro", "nombre": "Otro", "rol": "transportista"})
    assert r.status_code == 403 and "SUPERVISOR" in r.json()["detail"]
    # Conductor sin cédula -> 422
    assert client.post("/api/admin/usuarios", headers=sup,
                       json={"rol": "conductor", "nombre": "Sin Cedula"}).status_code == 422


def test_politica_solo_transportista_crea_conductores(settings: Settings) -> None:
    settings.conductor_creator_roles = "transportista"
    with TestClient(create_app(settings)) as client:
        sup = login(client, SUP_USER, SUP_PASS)
        hs = _staff(client, sup)
        r = client.post("/api/admin/conductores", headers=sup, json={"cedula": "40000001", "nombre": "Uno"})
        assert r.status_code == 403
        r = client.post("/api/admin/usuarios", headers=sup,
                        json={"rol": "conductor", "cedula": "40000001", "nombre": "Uno"})
        assert r.status_code == 403 and "TRANSPORTISTA" in r.json()["detail"]
        assert client.post("/api/admin/conductores", headers=hs["transp"],
                           json={"cedula": "40000001", "nombre": "Uno"}).status_code == 201
        # Registrar choferes nuevos al cargar la REMESA también es crear conductores
        prev = upload_remesa(client, sup, libro_remesa()).json()
        r = client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=sup, json={"registrar_choferes": True})
        assert r.status_code == 403
        r = client.post("/api/admin/catalogos/placas", headers=sup,
                        files={"archivo": ("R.xlsx", libro_remesa(), "application/octet-stream")})
        assert r.status_code == 403
        assert "conductores.crear" not in client.get("/api/me/permisos", headers=sup).json()["permisos"]
        assert "conductores.crear" in client.get("/api/me/permisos", headers=hs["transp"]).json()["permisos"]


# --------------------------------------------------------------------------- 3. claves
def test_gestion_de_claves(client: TestClient, sup: dict[str, str], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    hs = _staff(client, sup)
    lista = client.get("/api/admin/usuarios?incluir_conductores=true", headers=sup).json()
    usuarios = {u["username"]: u for u in lista}
    t_id, c_id, s_id = usuarios["transp"]["id"], usuarios["cobra"]["id"], usuarios[SUP_USER]["id"]
    ana_h = _conductor(client, hs["transp"], "11111111", "Ana", "clave-ana-1")
    ana_id = client.get("/api/me", headers=ana_h).json()["id"]

    # Supervisor FIJA una clave concreta a un transportista
    nueva = "Nueva-Clave-Transp-9"
    r = client.post(f"/api/admin/usuarios/{t_id}/clave", headers=sup, json={"clave_nueva": nueva})
    assert r.status_code == 200, r.text
    assert r.json()["clave_temporal"] is None and r.json()["debe_cambiar_clave"] is True
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/me", headers=hs["transp"]).status_code == 401  # sesiones cerradas
    assert client.post("/api/auth/login", json={"username": "transp", "password": CLAVE_T}).status_code == 401
    transp = login(client, "transp", nueva)

    # Supervisor RESTABLECE (clave temporal generada, se muestra una vez)
    r = client.post(f"/api/admin/usuarios/{c_id}/clave", headers=sup)
    temporal = r.json()["clave_temporal"]
    assert r.status_code == 200 and temporal == "qwerty"  # CLAVE_TEMPORAL
    login(client, "cobra", temporal)

    # Transportista: solo claves de conductores
    assert client.post(f"/api/admin/usuarios/{ana_id}/clave", headers=transp,
                       json={"clave_nueva": "ana-nueva-22", "debe_cambiar_clave": False}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "11111111", "password": "ana-nueva-22"}).status_code == 200
    assert client.post(f"/api/admin/usuarios/{s_id}/clave", headers=transp, json={}).status_code == 403
    assert client.post(f"/api/admin/usuarios/{c_id}/clave", headers=transp, json={}).status_code == 403
    # Nadie usa la vía administrativa para su propia clave; Cobranzas no gestiona claves
    assert client.post(f"/api/admin/usuarios/{s_id}/clave", headers=sup, json={}).status_code == 403
    cobra = login(client, "cobra", temporal)
    assert client.post(f"/api/admin/usuarios/{ana_id}/clave", headers=cobra, json={}).status_code == 403
    # Validaciones de la clave
    assert client.post(f"/api/admin/usuarios/{t_id}/clave", headers=sup, json={"clave_nueva": "123"}).status_code == 422
    assert client.post(f"/api/admin/usuarios/{t_id}/clave", headers=sup,
                       json={"clave_nueva": "transp"}).status_code == 422

    # Consulta de credencial: solo Supervisor, nunca expone la clave ni el hash
    r = client.get(f"/api/admin/usuarios/{t_id}/credencial", headers=sup)
    assert r.status_code == 200
    cred = r.json()
    assert cred["algoritmo"] == "argon2id" and cred["clave_actualizada_por"] == "Supervisor"
    assert cred["debe_cambiar_clave"] is True and cred["tiene_clave"] is True
    texto = json.dumps(cred)
    assert "$argon2" not in texto and nueva not in texto and "password" not in texto
    assert client.get(f"/api/admin/usuarios/{t_id}/credencial", headers=transp).status_code == 403

    # Cambio de clave propia (exige la actual)
    assert client.post("/api/me/clave", headers=transp,
                       json={"clave_actual": "mala", "clave_nueva": "otra-clave-77"}).status_code == 400
    r = client.post("/api/me/clave", headers=transp, json={"clave_actual": nueva, "clave_nueva": "otra-clave-77"})
    assert r.status_code == 200
    assert client.get("/api/me", headers=transp).status_code == 401  # el token anterior queda inválido
    transp = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # Auditoría: registra las acciones y no contiene ninguna clave
    aud = client.get("/api/admin/auditoria", headers=sup).json()
    acciones = {a["accion"] for a in aud}
    assert {"usuario_creado", "conductor_creado", "clave_establecida", "clave_restablecida",
            "clave_cambiada", "credencial_consultada"} <= acciones
    volcado = json.dumps(aud)
    for secreto in (nueva, temporal, "ana-nueva-22", "otra-clave-77", CLAVE_T):
        assert secreto not in volcado
    assert client.get("/api/admin/auditoria", headers=transp).status_code == 403
    # Logs: ninguna clave en texto plano
    for secreto in (nueva, temporal, "ana-nueva-22", "otra-clave-77"):
        assert secreto not in caplog.text


# --------------------------------------------------------------------------- 4. conductor
def test_alcance_del_conductor(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    ana, beto, fa, fb = e["ana"], e["beto"], e["fa"], e["fb"]

    # Solo ve SUS remesas
    rem_a = client.get("/api/conductor/remesas?estado=todas", headers=ana).json()
    rem_b = client.get("/api/conductor/remesas?estado=todas", headers=beto).json()
    assert [r["numero"] for r in rem_a] == ["2025-00001"]
    assert len(rem_b) == 1 and rem_b[0]["numero"].startswith("SN-")
    sensibles = ('"total_fact"', '"total_neto"', '"chofer_cedula"', '"chofer_telefono"', '"cod_vendedor"')
    det = client.get(f"/api/conductor/remesas/{rem_a[0]['id']}", headers=ana).json()
    assert len(det["facturas"]) == 3
    assert not any(k in json.dumps([rem_a, det]) for k in sensibles)
    # Recursos ajenos: 404 (no se revela su existencia)
    assert client.get(f"/api/conductor/remesas/{rem_b[0]['id']}", headers=ana).status_code == 404
    assert client.get(f"/api/conductor/facturas/{fb[0]['id']}", headers=ana).status_code == 404
    assert client.post(f"/api/conductor/facturas/{fb[0]['id']}/salida", headers=ana, json=_ev()).status_code == 404

    # Flujo operativo: salida (Por Entregar) -> entrega. «carga» (Cargado) ya no existe
    fid = fa[0]["id"]
    assert client.post(f"/api/conductor/facturas/{fid}/carga", headers=ana, json=_ev()).status_code == 422
    for tipo, estatus in (("salida", "en_transito"), ("entrega", "entregada")):
        r = client.post(f"/api/conductor/facturas/{fid}/{tipo}", headers=ana, json=_ev())
        assert r.status_code == 200 and r.json()["ok"] and r.json()["estatus_actual"] == estatus, r.text
    # Devolución: exige foto
    fid2 = fa[1]["id"]
    r = client.post(f"/api/conductor/facturas/{fid2}/devolucion", headers=ana, json=_ev()).json()
    assert not r["ok"] and r["codigo_error"] == "invalido"
    foto = str(uuid.uuid4())
    assert client.put(f"/api/fotos/{foto}", headers=ana, data={"factura_id": fid2},
                      files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")}).status_code == 201
    r = client.post(f"/api/conductor/facturas/{fid2}/devolucion", headers=ana,
                    json={**_ev(), "foto_id": foto, "motivo": "Cerrado"}).json()
    assert r["ok"] and r["estatus_actual"] == "devolucion"
    hist = client.get(f"/api/conductor/facturas/{fid}", headers=ana).json()["eventos"]
    assert [h["estatus"] for h in hist] == ["recibida", "en_transito", "entregada"]

    # Solo los 4 eventos operativos: otros tipos o estatus se rechazan
    assert client.post(f"/api/conductor/facturas/{fid}/recibida", headers=ana, json=_ev()).status_code == 422
    lote = {"eventos": [{**_ev(), "factura_id": fa[2]["id"], "estatus": "recibida"}]}
    res = client.post("/api/conductor/eventos", headers=ana, json=lote).json()
    assert res["resultados"][0]["codigo_error"] == "invalido"
    # No edita su perfil ni otros datos
    assert client.patch("/api/me/perfil", headers=ana, json={"nombre": "Otro Nombre"}).status_code == 403
    correccion = {"estatus": "recibida", "motivo": "x"}
    assert client.post(f"/api/facturas/{fid}/estatus", headers=ana, json=correccion).status_code == 403
    perms = client.get("/api/me/permisos", headers=ana).json()
    assert perms["rol"] == "conductor" and len(perms["permisos"]) == 5


ALLOW_CONDUCTOR = re.compile(
    r"^/api/(conductor/|fotos/|me$|me/clave$|me/permisos$|publico/|config$|auth/login$|auth/logout$|bitacora/vista$"
    r"|auth/biometria/|me/biometria|docs|openapi)"
)


def test_conductor_no_accede_a_ninguna_otra_ruta(client: TestClient, sup: dict[str, str]) -> None:
    """Recorre TODAS las rutas de la API: fuera de su alcance, el conductor recibe 403."""
    hs = _staff(client, sup)
    ana = _conductor(client, hs["transp"], "11111111", "Ana", "clave-ana-1")
    revisadas = 0
    rutas = client.get("/api/openapi.json").json()["paths"]
    for ruta, metodos in rutas.items():
        if not ruta.startswith("/api/") or (ALLOW_CONDUCTOR.match(ruta) and ruta != "/api/me/perfil"):
            continue
        path = re.sub(r"\{(\w+)\}", lambda m: "1" if m.group(1).endswith("id") else "X", ruta)
        for method in metodos:
            r = client.request(method.upper(), path, headers=ana, json={})
            if ruta == "/api/me/perfil" and method == "get":
                assert r.status_code == 200  # su propia sesión/ficha, solo lectura
                continue
            assert r.status_code == 403, f"{method.upper()} {ruta} -> {r.status_code}"
            revisadas += 1
    assert revisadas >= 40
