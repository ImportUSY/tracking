"""Reingreso de facturas devueltas, observación de entrega, Usuarios para el Transportista (Activo Sí/No),
bitácora de actividad inmutable y versión en el pie de la pantalla de inicio."""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import crear_empresa, login, upload_remesa
from tests.factories import bloque, factura, libro_remesa
from tests.test_capturas import CRUDO_EJEMPLO, ExtractorFalso, _confirmar, _foto, _usuario
from tracking import __version__
from tracking.models import Bitacora


def _ev(fid: int, estatus: str, **kw: Any) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "factura_id": fid, "estatus": estatus, **kw}


def _escenario(client: TestClient, sup: dict[str, str]) -> dict[str, Any]:
    """Empresa con Transportista, Ana (conductora) y la REMESA 2025-00001 cargada."""
    emp = crear_empresa(client, sup, "TRANSPORTE YANEIBER")
    tb = _usuario(client, sup, "transp", "transportista", emp)
    r = client.post(
        "/api/admin/conductores",
        headers=tb,
        json={"cedula": "11111111", "nombre": "Ana Chofer", "clave": "clave-ana-1"},
    )
    ana_uid = next(
        u["id"]
        for u in client.get("/api/admin/usuarios?incluir_conductores=true", headers=sup).json()
        if u["username"] == "11111111"
    )
    client.post("/api/admin/conductores", headers=tb, json={"cedula": "22222222", "nombre": "Beto Chofer"})
    prev = upload_remesa(client, tb, libro_remesa()).json()
    client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=tb, json={})
    return {
        "emp": emp,
        "tb": tb,
        "ana": login(client, "11111111", "clave-ana-1"),
        "ana_uid": ana_uid,
        "ana_cid": r.json()["conductor"]["id"],
    }


def _libro_reenvio() -> bytes:
    """La factura FAC-U00000001 (devuelta) vuelve en una remesa nueva."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "REMESA 2026,"
    bloque(
        ws,
        7,
        cedula=11111111,
        nombre="Ana Chofer",
        placa="AAA111",
        telefono="0412 111 1111",
        destino="LARA",
        numero="2025-00077",
        empresa="IMPORTADORA USY",
        facturas=[factura(1)],
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _bitacora(client: TestClient) -> list[Bitacora]:
    with client.app.state.db.session() as db:  # type: ignore[attr-defined]
        return list(db.scalars(select(Bitacora).order_by(Bitacora.id)))


# --------------------------------------------------------- 1. Reingreso por devolución
def test_factura_devuelta_se_reingresa_en_una_remesa_nueva(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    vieja = client.get("/api/facturas", headers=sup, params={"q": "FAC-U00000001"}).json()["items"][0]
    assert client.get(f"/api/facturas/{vieja['id']}", headers=sup).json()["habilitada_reingreso"] is False
    r = client.post(f"/api/facturas/{vieja['id']}/estatus", headers=sup, json={"estatus": "devolucion"})
    assert r.status_code == 200
    assert client.get(f"/api/facturas/{vieja['id']}", headers=sup).json()["habilitada_reingreso"] is True

    # Excel con la misma factura en una remesa nueva: se carga como reingreso y queda enlazada
    prev = upload_remesa(client, e["tb"], _libro_reenvio()).json()
    assert prev["resumen"]["totales"]["facturas_nuevas"] == 1
    assert any(a["codigo"] == "FACTURA_REENVIO" for a in prev["resumen"]["avisos"])
    assert (
        client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=e["tb"], json={}).json()["facturas_creadas"]
        == 1
    )
    nuevas = client.get("/api/facturas", headers=sup, params={"q": "FAC-U00000001"}).json()["items"]
    nueva = next(f for f in nuevas if f["id"] != vieja["id"])
    det = client.get(f"/api/facturas/{nueva['id']}", headers=sup).json()
    assert det["estatus"] == "recibida" and det["reingreso_de"]["id"] == vieja["id"]
    assert det["reingreso_de"]["remesa_numero"] == "2025-00001" and det["remesa_numero"] == "2025-00077"
    assert "Reingreso tras devolución" in det["eventos"][0]["motivo"]
    det_vieja = client.get(f"/api/facturas/{vieja['id']}", headers=sup).json()
    assert det_vieja["estatus"] == "devolucion" and det_vieja["habilitada_reingreso"] is False  # historial intacto
    assert [x["id"] for x in det_vieja["reingresos"]] == [nueva["id"]]
    assert [ev["estatus"] for ev in det_vieja["eventos"]] == ["recibida", "devolucion"]


def test_reingreso_por_foto_de_factura(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    client.app.state.extractor_facturas = ExtractorFalso()  # type: ignore[attr-defined]
    cap = _foto(client, e["tb"])
    res = client.post(
        f"/api/capturas/{cap['id']}/confirmar", headers=e["tb"], json=_confirmar(cap, conductor_id=e["ana_cid"])
    ).json()
    client.post(f"/api/facturas/{res['factura_id']}/estatus", headers=sup, json={"estatus": "devolucion"})
    otra = _foto(client, e["tb"])  # misma factura otra vez: ya no es duplicado, es reingreso
    assert not any("ya está en el sistema" in a for a in otra["avisos"])
    r = client.post(
        f"/api/capturas/{otra['id']}/confirmar", headers=e["tb"], json=_confirmar(otra, conductor_id=e["ana_cid"])
    )
    assert r.status_code == 200, r.text
    assert (
        client.get(f"/api/facturas/{r.json()['factura_id']}", headers=sup).json()["reingreso_de"]["id"]
        == res["factura_id"]
    )
    # Una vez reingresada, una tercera foto sí es duplicado (la factura está en curso)
    tercera = _foto(client, e["tb"])
    assert any("ya está en el sistema" in a for a in tercera["avisos"])
    assert CRUDO_EJEMPLO["numero_factura"] == "340239"


# --------------------------------------------------------- 2. Observación de la entrega
def test_observacion_al_entregar(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    fid = client.get("/api/conductor/facturas", headers=e["ana"]).json()["facturas"][0]["id"]
    obs = "Recibido por Juan Pérez, una caja golpeada"
    for est, extra in (("en_transito", {}), ("entregada", {"motivo": obs})):
        r = client.post("/api/conductor/eventos", headers=e["ana"], json={"eventos": [_ev(fid, est, **extra)]})
        assert r.json()["resultados"][0]["aplicado"], r.text
    det = client.get(f"/api/facturas/{fid}", headers=sup).json()
    assert det["observacion_entrega"] == obs
    entrega = det["eventos"][-1]
    assert entrega["motivo"] == obs and entrega["usuario_nombre"] == "Ana Chofer" and entrega["ocurrido_at"]
    assert client.get("/api/conductor/facturas", headers=e["ana"]).json()["facturas"][0]["observacion_entrega"] == obs
    pub = client.get("/api/publico/tracking", params={"q": det["numero"]}).json()["resultados"][0]
    assert [h.get("observacion") for h in pub["historial"]][-1] == obs
    # Si la factura deja de estar Entregada, ya no hay observación vigente (el historial la conserva)
    client.post(f"/api/facturas/{fid}/estatus", headers=sup, json={"estatus": "devolucion"})
    det = client.get(f"/api/facturas/{fid}", headers=sup).json()
    assert det["observacion_entrega"] is None and any(ev["motivo"] == obs for ev in det["eventos"])


# --------------------------------------------------------- 3. Usuarios para el Transportista · Activo
def test_transportista_ve_y_gestiona_solo_sus_conductores(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    otra = crear_empresa(client, sup, "OTRA")
    ajeno = client.post(
        "/api/admin/conductores", headers=sup, json={"cedula": "99999999", "nombre": "Chofer Ajeno", "empresa_id": otra}
    ).json()
    ajeno_uid = next(
        u["id"]
        for u in client.get("/api/admin/usuarios?incluir_conductores=true", headers=sup).json()
        if u["username"] == "99999999"
    )
    cob = _usuario(client, sup, "cobra", "cobranzas", None)
    assert cob
    lista = client.get("/api/admin/usuarios?incluir_conductores=true", headers=e["tb"]).json()
    assert {u["username"] for u in lista} == {"11111111", "22222222"} and {u["rol"] for u in lista} == {"conductor"}
    assert ajeno["conductor"]["id"]

    # Clave de su conductor: sí. De otra empresa u otro rol: no
    assert client.post(f"/api/admin/usuarios/{e['ana_uid']}/clave", headers=e["tb"], json={}).status_code == 200
    assert client.post(f"/api/admin/usuarios/{ajeno_uid}/clave", headers=e["tb"], json={}).status_code == 404

    # Activo = No: sin acceso inmediato (su token deja de servir y el login se rechaza con aviso)
    r = client.post(f"/api/admin/usuarios/{e['ana_uid']}/activo", headers=e["tb"], json={"activo": False})
    assert r.status_code == 200 and r.json()["activo"] is False
    assert client.get("/api/conductor/facturas", headers=e["ana"]).status_code == 401
    clave = client.post(
        f"/api/admin/usuarios/{e['ana_uid']}/clave",
        headers=sup,
        json={"clave_nueva": "clave-ana-2", "debe_cambiar_clave": False},
    ).json()
    assert clave
    r = client.post("/api/auth/login", json={"username": "11111111", "password": "clave-ana-2"})
    assert r.status_code == 403 and "inactivo" in r.json()["detail"]
    fichas = {
        c["cedula"]: c for c in client.get("/api/admin/conductores?incluir_inactivos=true", headers=e["tb"]).json()
    }
    assert fichas["11111111"]["activo"] is False  # la ficha del conductor sigue al usuario
    # Vuelve a Sí: puede entrar
    assert (
        client.post(f"/api/admin/usuarios/{e['ana_uid']}/activo", headers=e["tb"], json={"activo": True}).status_code
        == 200
    )
    assert client.post("/api/auth/login", json={"username": "11111111", "password": "clave-ana-2"}).status_code == 200

    # Fuera de su alcance
    cob_id = next(u["id"] for u in client.get("/api/admin/usuarios", headers=sup).json() if u["username"] == "cobra")
    assert (
        client.post(f"/api/admin/usuarios/{ajeno_uid}/activo", headers=e["tb"], json={"activo": False}).status_code
        == 404
    )
    r = client.post(f"/api/admin/usuarios/{cob_id}/activo", headers=e["tb"], json={"activo": False})
    assert r.status_code == 403 and "Conductor" in r.json()["detail"]
    # Supervisor: a cualquiera, menos a sí mismo; Cobranzas: a nadie
    assert (
        client.post(f"/api/admin/usuarios/{cob_id}/activo", headers=sup, json={"activo": False}).json()["activo"]
        is False
    )
    sup_id = client.get("/api/me", headers=sup).json()["id"]
    assert client.post(f"/api/admin/usuarios/{sup_id}/activo", headers=sup, json={"activo": False}).status_code == 403


# --------------------------------------------------------- 4. Bitácora
def test_bitacora_registra_todo_y_es_inmutable(client: TestClient, sup: dict[str, str]) -> None:
    client.get("/api/remesas", headers=sup, params={"estado": "activas"})
    client.post("/api/auth/login", json={"username": "supervisor", "password": "mala"})
    emp = crear_empresa(client, sup, "BITACORA SA")
    r = client.post(
        "/api/admin/conductores",
        headers=sup,
        json={"cedula": "55555555", "nombre": "Chofer Bita", "clave": "secreto-123", "empresa_id": emp},
    )
    cid = r.json()["conductor"]["id"]
    client.patch(f"/api/admin/conductores/{cid}", headers=sup, json={"placa": "BIT001"})
    client.delete(f"/api/admin/conductores/{cid}", headers=sup)
    client.post("/api/bitacora/vista", headers=sup, json={"vista": "remesas", "titulo": "Remesas"})
    client.get("/api/config")
    client.get("/api/conductor/facturas", headers={**sup, "X-Sincronizacion": "1"})  # sincronización automática
    client.post("/api/auth/logout", headers=sup)

    filas = _bitacora(client)
    acciones = [(b.accion, b.modulo, b.metodo) for b in filas]
    assert ("LOGIN", "sesion", "POST") in acciones  # el login del fixture
    assert ("CONSULTAR", "remesas", "GET") in acciones and ("VISTA", "remesas", "POST") in acciones
    assert ("CREAR", "conductores", "POST") in acciones and ("MODIFICAR", "conductores", "PATCH") in acciones
    assert ("ELIMINAR", "conductores", "DELETE") in acciones and acciones[-1][0] == "LOGOUT"
    assert not any(b.ruta in ("/api/config", "/api/conductor/facturas") for b in filas)
    fallido = next(b for b in filas if b.accion == "LOGIN_FALLIDO")
    assert fallido.usuario_id is None and fallido.detalle["usuario_intentado"] == "supervisor" and not fallido.exito
    alta = next(b for b in filas if b.accion == "CREAR" and b.modulo == "conductores")
    assert alta.usuario == "supervisor" and alta.rol == "supervisor" and alta.estado_http == 201
    assert alta.detalle["payload"]["clave"] == "[oculto]" and alta.detalle["payload"]["nombre"] == "Chofer Bita"
    consulta = next(b for b in filas if b.ruta == "/api/remesas")
    assert consulta.detalle["consulta"] == {"estado": "activas"} and consulta.ocurrido_at and consulta.ip

    # Consulta con filtros (Supervisor / Consultor); el Transportista no la ve
    r = client.get("/api/admin/bitacora", headers=sup, params={"accion": "ELIMINAR"}).json()
    assert r["total"] == 1 and r["items"][0]["recurso_id"] == str(cid)
    assert client.get("/api/admin/bitacora", headers=sup, params={"exito": False}).json()["total"] >= 1
    tb = _usuario(client, sup, "transp", "transportista", emp)
    assert client.get("/api/admin/bitacora", headers=tb).status_code == 403
    r = client.get("/api/admin/bitacora", headers=sup, params={"q": "transp", "accion": "LOGIN"}).json()
    assert r["items"][0]["empresa_nombre"] == "BITACORA SA" and r["items"][0]["rol"] == "transportista"

    # Inmutable: la base de datos rechaza cambios y borrados
    with client.app.state.db.session() as db:  # type: ignore[attr-defined]
        for sql in ("UPDATE bitacora SET accion = 'X'", "DELETE FROM bitacora"):
            with pytest.raises(DBAPIError):
                db.execute(text(sql))
            db.rollback()
    assert len(_bitacora(client)) >= len(filas)


def test_bitacora_de_un_consultor_y_rechazos(client: TestClient, sup: dict[str, str]) -> None:
    r = client.post(
        "/api/admin/usuarios",
        headers=sup,
        json={"username": "consulta1", "nombre": "Ana Consultora", "rol": "consultor", "clave": "Consulta-1"},
    )
    assert r.status_code == 201
    co = login(client, "consulta1", "Consulta-1")
    client.post("/api/admin/empresas", headers=co, json={"nombre": "NO"})  # rechazado: solo consulta
    filas = [b for b in _bitacora(client) if b.usuario == "consulta1"]
    rechazo = next(b for b in filas if b.ruta == "/api/admin/empresas")
    assert rechazo.accion == "CREAR" and rechazo.estado_http == 403 and rechazo.exito is False
    assert client.get("/api/admin/bitacora", headers=co).status_code == 200


# --------------------------------------------------------- 5. Pie: versión y fecha
def test_config_trae_version_y_fecha_de_creacion(client: TestClient) -> None:
    c = client.get("/api/config").json()
    assert c["version"] == __version__ and c["version_publica"] == __version__
    assert c["fecha_creacion"] == "Octubre 2026"
    html = client.get("/").text
    assert "Creado por" not in html and "data-version" in html and "data-fecha-creacion" in html
    panel = client.get("/panel").text
    assert "data-version" in panel and "data-fecha-creacion" in panel


def test_migracion_0008_enlaza_reingresos_y_vuelve_atras(tmp_path: Any) -> None:
    from datetime import datetime, timezone

    from alembic import command
    from sqlalchemy import create_engine

    from tracking.migrate import alembic_config

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    cfg = alembic_config()
    t = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0007")
        for rid in (1, 2):
            conn.execute(
                text(
                    "INSERT INTO remesas (id, numero, numero_generado, created_at, updated_at) "
                    "VALUES (:i, :n, 0, :t, :t)"
                ),
                {"i": rid, "n": f"R-{rid}", "t": t},
            )
        for fid, rid, estatus in ((1, 1, "devolucion"), (2, 2, "recibida"), (3, 1, "entregada")):
            conn.execute(
                text(
                    "INSERT INTO facturas (id, remesa_id, numero, empresa, estatus, estatus_at, version, created_at, "
                    "updated_at) VALUES (:i, :r, :n, 'IMPORTADORA USY', :e, :t, 1, :t, :t)"
                ),
                {"i": fid, "r": rid, "n": "777" if fid < 3 else "888", "e": estatus, "t": t},
            )
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        enlaces = dict(conn.execute(text("SELECT id, reingreso_de_id FROM facturas")).all())
        assert enlaces == {1: None, 2: 1, 3: None}
        command.downgrade(cfg, "0007")
        ddl = str(conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'facturas'")).scalar())
        assert "reingreso_de_id" not in ddl and "observacion_entrega" not in ddl
        assert conn.execute(text("SELECT count(*) FROM sqlite_master WHERE name = 'bitacora'")).scalar() == 0
    engine.dispose()
