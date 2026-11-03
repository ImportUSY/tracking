"""Multi-tenant: módulo Empresas (solo SUPERVISOR), aislamiento de Remesas/Facturas y evidencia PoD."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from tests.conftest import crear_empresa, login, upload_remesa
from tests.factories import JPEG_MINIMO, libro_remesa
from tracking.config import Settings
from tracking.migrate import alembic_config
from tracking.models import Factura, Remesa
from tracking.services.evidencias import FirmadorHmac
from tracking.tenancy import TODAS_LAS_EMPRESAS, TenantScope, ViolacionAislamiento, vincular_sesion


def _usuario(client: TestClient, sup: dict[str, str], username: str, rol: str, empresa: int | None) -> dict[str, str]:
    clave = f"clave-{username}-1"
    r = client.post("/api/admin/usuarios", headers=sup,
                    json={"username": username, "nombre": username.title(), "rol": rol, "clave": clave,
                          "empresa_id": empresa})
    assert r.status_code == 201, r.text
    return login(client, username, clave)


def _conductor(client: TestClient, h: dict[str, str], cedula: str, nombre: str) -> tuple[dict[str, str], int, int]:
    clave = f"clave-{cedula}"
    r = client.post("/api/admin/conductores", headers=h, json={"cedula": cedula, "nombre": nombre, "clave": clave})
    assert r.status_code == 201, r.text
    c = r.json()["conductor"]
    return login(client, cedula, clave), c["id"], c["usuario_id"]


def _dt(valor: str) -> datetime:
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))  # Python 3.10 no acepta el sufijo Z


def _ev(factura_id: int, estatus: str, **kw: Any) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "factura_id": factura_id, "estatus": estatus, **kw}


def _dos_empresas(client: TestClient, sup: dict[str, str]) -> dict[str, Any]:
    """ALFA (transportista ta + Ana) y BETA (transportista tb + Beto). El Supervisor carga la REMESA."""
    emp_a = crear_empresa(client, sup, "TRANSPORTES ALFA", rif="J-11111111-1")
    emp_b = crear_empresa(client, sup, "TRANSPORTES BETA")
    ta = _usuario(client, sup, "transp_a", "transportista", emp_a)
    tb = _usuario(client, sup, "transp_b", "transportista", emp_b)
    ana, ana_cid, ana_uid = _conductor(client, ta, "11111111", "Ana Chofer")
    beto, beto_cid, beto_uid = _conductor(client, tb, "22222222", "Beto Chofer")
    prev = upload_remesa(client, sup, libro_remesa()).json()
    r = client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=sup, json={})
    assert r.status_code == 200 and r.json()["facturas_creadas"] == 5, r.text
    remesas = {x["chofer_nombre"]: x for x in client.get("/api/remesas", headers=sup).json()["items"]}
    return {
        "emp_a": emp_a, "emp_b": emp_b, "ta": ta, "tb": tb, "ana": ana, "beto": beto,
        "ana_cid": ana_cid, "beto_cid": beto_cid, "ana_uid": ana_uid, "beto_uid": beto_uid,
        "rem_a": remesas["Ana Chofer"], "rem_b": remesas["Beto Chofer"], "imp_sup": prev["id"],
        "fa": client.get("/api/conductor/facturas", headers=ana).json()["facturas"],
        "fb": client.get("/api/conductor/facturas", headers=beto).json()["facturas"],
    }


# ------------------------------------------------------------------ 1. Empresas
def test_crud_empresas_supervisor(client: TestClient, sup: dict[str, str]) -> None:
    r = client.post("/api/admin/empresas", headers=sup,
                    json={"nombre": "  Transportes   Andes ", "rif": "j-12.345.678-9", "email": "ops@andes.com"})
    assert r.status_code == 201, r.text
    emp = r.json()
    assert emp["nombre"] == "Transportes Andes" and emp["rif"] == "J-12345678-9" and emp["activo"] is True
    assert client.post("/api/admin/empresas", headers=sup, json={"nombre": "TRANSPORTES ANDES"}).status_code == 409
    assert client.post("/api/admin/empresas", headers=sup,
                       json={"nombre": "Otra", "rif": "J-12345678-9"}).status_code == 409
    assert client.post("/api/admin/empresas", headers=sup, json={"nombre": "Mala", "rif": "XYZ-1"}).status_code == 422

    r = client.patch(f"/api/admin/empresas/{emp['id']}", headers=sup, json={"telefono": "0212-5550000"})
    assert r.status_code == 200 and r.json()["telefono"] == "0212-5550000"
    assert client.get(f"/api/admin/empresas/{emp['id']}", headers=sup).json()["usuarios"] == 0
    assert client.get("/api/admin/empresas/9999", headers=sup).status_code == 404

    # Sin referencias: se elimina. Con usuarios: se desactiva y sus usuarios pierden el acceso.
    vacia = crear_empresa(client, sup, "VACIA")
    assert client.delete(f"/api/admin/empresas/{vacia}", headers=sup).json()["eliminada"] is True
    t = _usuario(client, sup, "transp", "transportista", emp["id"])
    assert client.get("/api/me", headers=t).json()["empresa_nombre"] == "Transportes Andes"
    r = client.delete(f"/api/admin/empresas/{emp['id']}", headers=sup).json()
    assert r["eliminada"] is False and r["desactivada"] is True
    assert client.get("/api/me", headers=t).status_code == 401
    r = client.post("/api/auth/login", json={"username": "transp", "password": "clave-transp-1"})
    assert r.status_code == 403 and "inactivo" in r.json()["detail"]  # clave correcta, empresa desactivada
    lista = client.get("/api/admin/empresas", headers=sup).json()
    assert [e["nombre"] for e in lista] == ["Transportes Andes"] and lista[0]["usuarios"] == 1
    assert client.get("/api/admin/empresas?incluir_inactivas=false", headers=sup).json() == []
    acciones = {a["accion"] for a in client.get("/api/admin/auditoria", headers=sup).json()}
    assert {"empresa_creada", "empresa_editada", "empresa_eliminada", "empresa_desactivada"} <= acciones


def test_empresas_solo_supervisor(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    ta = _usuario(client, sup, "transp_a", "transportista", empresa)
    cob = _usuario(client, sup, "cobra", "cobranzas", None)
    cond, _, _ = _conductor(client, ta, "30000001", "Cond")
    for h in (ta, cob, cond):
        assert client.get("/api/admin/empresas", headers=h).status_code == 403
        assert client.get(f"/api/admin/empresas/{empresa}", headers=h).status_code == 403
        assert client.post("/api/admin/empresas", headers=h, json={"nombre": "Hack"}).status_code == 403
        assert client.patch(f"/api/admin/empresas/{empresa}", headers=h, json={"activo": False}).status_code == 403
        assert client.delete(f"/api/admin/empresas/{empresa}", headers=h).status_code == 403
    assert client.get("/api/admin/empresas").status_code == 401


def test_usuarios_aislados_requieren_empresa(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    r = client.post("/api/admin/usuarios", headers=sup, json={"username": "t", "nombre": "T", "rol": "transportista"})
    assert r.status_code == 422
    r = client.post("/api/admin/usuarios", headers=sup,
                    json={"username": "t", "nombre": "T", "rol": "transportista", "empresa_id": 999})
    assert r.status_code == 422
    otra = crear_empresa(client, sup, "OTRA")
    ta = _usuario(client, sup, "transp_a", "transportista", empresa)
    # El Transportista crea conductores solo en su empresa (hereda la suya; pedir otra es 403)
    r = client.post("/api/admin/conductores", headers=ta,
                    json={"cedula": "30000002", "nombre": "Dos", "empresa_id": otra})
    assert r.status_code == 403
    r = client.post("/api/admin/conductores", headers=ta, json={"cedula": "30000002", "nombre": "Dos"})
    assert r.status_code == 201 and r.json()["conductor"]["empresa_id"] == empresa
    # Supervisor reasigna la empresa de un usuario: cierra su sesión; no puede dejarlo sin empresa
    uid = client.get("/api/me", headers=ta).json()["id"]
    assert client.patch(f"/api/admin/usuarios/{uid}", headers=sup, json={"empresa_id": None}).status_code == 422
    r = client.patch(f"/api/admin/usuarios/{uid}", headers=sup, json={"empresa_id": otra})
    assert r.status_code == 200 and r.json()["empresa_id"] == otra and r.json()["empresa_nombre"] == "OTRA"
    assert client.get("/api/me", headers=ta).status_code == 401
    # Cambiar a un rol aislado exige empresa
    cob = client.post("/api/admin/usuarios", headers=sup,
                      json={"username": "cob", "nombre": "Cob", "rol": "cobranzas", "clave": "clave-cob-1"}).json()
    cid = cob["usuario"]["id"]
    assert client.patch(f"/api/admin/usuarios/{cid}", headers=sup, json={"rol": "transportista"}).status_code == 422
    r = client.patch(f"/api/admin/usuarios/{cid}", headers=sup, json={"rol": "transportista", "empresa_id": empresa})
    assert r.status_code == 200 and r.json()["rol"] == "transportista"


# ------------------------------------------------------- 2. Aislamiento de datos
def test_transportista_solo_ve_su_empresa(client: TestClient, sup: dict[str, str]) -> None:
    e = _dos_empresas(client, sup)
    ta, rem_a, rem_b = e["ta"], e["rem_a"], e["rem_b"]
    assert rem_a["empresa_id"] == e["emp_a"] and rem_b["empresa_id"] == e["emp_b"]
    fb = e["fb"][0]["id"]

    # Listados acotados (Supervisor ve todo)
    assert client.get("/api/remesas", headers=sup).json()["total"] == 2
    r = client.get("/api/remesas", headers=ta).json()
    assert r["total"] == 1 and r["items"][0]["id"] == rem_a["id"]
    assert client.get("/api/remesas", headers=e["tb"]).json()["items"][0]["id"] == rem_b["id"]
    facts = client.get("/api/facturas", headers=ta).json()
    assert facts["total"] == 3 and {f["empresa_id"] for f in facts["items"]} == {e["emp_a"]}
    assert client.get("/api/facturas", headers=ta, params={"q": e["fb"][0]["numero"]}).json()["total"] == 0
    dash = client.get("/api/dashboard", headers=ta).json()
    assert sum(dash["por_estatus"].values()) == 3 and dash["remesas_activas"] == 1
    assert client.get("/api/reportes/metricas", headers=ta).status_code == 403  # métrica estrella: solo Supervisor

    # Recursos de otra empresa: 404 (como inexistentes) para leer, alterar o reasignar
    assert client.get(f"/api/remesas/{rem_b['id']}", headers=ta).status_code == 404
    assert client.get(f"/api/facturas/{fb}", headers=ta).status_code == 404
    r = client.post(f"/api/facturas/{fb}/estatus", headers=ta, json={"estatus": "entregada"})
    assert r.status_code == 404
    r = client.post(f"/api/remesas/{rem_b['id']}/reasignar", headers=ta, json={"conductor_id": e["ana_cid"]})
    assert r.status_code == 404
    r = client.post(f"/api/remesas/{rem_a['id']}/reasignar", headers=ta, json={"conductor_id": e["beto_cid"]})
    assert r.status_code == 404  # no puede sacar su remesa hacia un conductor ajeno
    r = client.get("/api/reportes/cobranzas.xlsx", headers=ta, params={"remesa_ids": [rem_b["id"]]})
    assert r.status_code == 404

    # Catálogo de conductores y credenciales de otra empresa
    assert [c["nombre"] for c in client.get("/api/admin/conductores", headers=ta).json()] == ["Ana Chofer"]
    assert client.patch(f"/api/admin/conductores/{e['beto_cid']}", headers=ta, json={"placa": "X"}).status_code == 404
    assert client.post(f"/api/admin/conductores/{e['beto_cid']}/clave", headers=ta).status_code == 404
    assert client.post(f"/api/admin/usuarios/{e['beto_uid']}/clave", headers=ta, json={}).status_code == 404
    assert client.delete(f"/api/admin/conductores/{e['beto_cid']}", headers=ta).status_code == 404

    # Fotos de otra empresa
    foto_b = str(uuid.uuid4())
    r = client.put(f"/api/fotos/{foto_b}", headers=e["beto"], data={"factura_id": fb},
                   files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    assert r.status_code == 201
    assert client.get(f"/api/fotos/{foto_b}", headers=ta).status_code == 404
    assert client.get(f"/api/fotos/{foto_b}", headers=sup).status_code == 200
    r = client.put(f"/api/fotos/{uuid.uuid4()}", headers=ta, data={"factura_id": fb},
                   files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    assert r.status_code == 404

    # Supervisor sí reasigna entre empresas: remesa y facturas pasan a la empresa del conductor
    r = client.post(f"/api/remesas/{rem_b['id']}/reasignar", headers=sup, json={"conductor_id": e["ana_cid"]})
    assert r.status_code == 200 and r.json()["empresa_id"] == e["emp_a"]
    assert client.get("/api/facturas", headers=ta).json()["total"] == 5
    assert client.get("/api/remesas", headers=e["tb"]).json()["total"] == 0


def test_conductor_aislado(client: TestClient, sup: dict[str, str]) -> None:
    e = _dos_empresas(client, sup)
    fa, fb = e["fa"], e["fb"]
    assert len(fa) == 3 and len(fb) == 2
    out = client.post("/api/conductor/eventos", headers=e["ana"],
                      json={"eventos": [_ev(fb[0]["id"], "en_transito")]}).json()["resultados"][0]
    assert out["ok"] is False and out["codigo_error"] == "no_encontrada"
    r = client.post(f"/api/conductor/facturas/{fb[0]['id']}/salida", headers=e["ana"], json={"id": str(uuid.uuid4())})
    assert r.status_code == 404
    assert client.get(f"/api/conductor/remesas/{e['rem_b']['id']}", headers=e["ana"]).status_code == 404
    # Un conductor reasignado a otra empresa deja de ver las remesas de la anterior
    r = client.patch(f"/api/admin/usuarios/{e['ana_uid']}", headers=sup, json={"empresa_id": e["emp_b"]})
    assert r.status_code == 200
    ana = login(client, "11111111", "clave-11111111")
    assert client.get("/api/conductor/facturas", headers=ana).json()["facturas"] == []


def test_cargas_aisladas_por_empresa(client: TestClient, sup: dict[str, str]) -> None:
    e = _dos_empresas(client, sup)
    ta = e["ta"]
    # Historial de importaciones: el del Supervisor (sin empresa) no es visible para el Transportista
    assert client.get("/api/importaciones", headers=ta).json() == []
    assert client.get(f"/api/importaciones/{e['imp_sup']}", headers=ta).status_code == 404

    prev = upload_remesa(client, ta, libro_remesa()).json()
    remesas = {r["chofer_cedula"]: r for r in prev["resumen"]["remesas"]}
    assert remesas["22222222"]["estado"] == "otra_empresa" and remesas["22222222"]["chofer_otra_empresa"]
    assert remesas["22222222"]["seleccion_sugerida"] is False
    assert any(a["codigo"] == "REMESA_OTRA_EMPRESA" for a in prev["resumen"]["avisos"])
    assert "Beto" not in str([c["nombre"] for c in prev["resumen"]["choferes"]])
    assert client.get(f"/api/importaciones/{prev['id']}", headers=e["tb"]).status_code == 404

    url = f"/api/importaciones/{prev['id']}/confirmar"
    sel = {"remesas": [remesas["22222222"]["numero"], "2025-00003"], "registrar_choferes": True}
    assert client.post(url, headers=ta, json={**sel, "empresa_id": e["emp_b"]}).status_code == 403
    r = client.post(url, headers=ta, json=sel)
    assert r.status_code == 200, r.text
    assert r.json()["remesas_excluidas"] == [remesas["22222222"]["numero"]]  # la remesa de Beto no se toca
    assert [c["usuario"] for c in r.json()["choferes_registrados"]] == ["33333333"]
    nueva = client.get("/api/remesas", headers=ta, params={"q": "2025-00003"}).json()["items"][0]
    assert nueva["empresa_id"] == e["emp_a"]
    carlos = next(u for u in client.get("/api/admin/usuarios?incluir_conductores=true", headers=sup).json()
                  if u["username"] == "33333333")
    assert carlos["empresa_id"] == e["emp_a"]
    assert client.get("/api/remesas", headers=e["tb"]).json()["total"] == 1


def test_red_de_seguridad_en_la_sesion_orm(client: TestClient, sup: dict[str, str]) -> None:
    """Aunque un endpoint olvidara el repositorio, la sesión de un usuario aislado sale filtrada."""
    e = _dos_empresas(client, sup)
    db = client.app.state.db.session()  # type: ignore[attr-defined]
    try:
        vincular_sesion(db, TenantScope(restringido=True, empresa_id=e["emp_a"]))
        assert db.get(Factura, e["fb"][0]["id"]) is None
        assert {f.empresa_id for f in db.scalars(select(Factura))} == {e["emp_a"]}
        assert db.get(Remesa, e["rem_a"]["id"]).facturas  # lazy load filtrado pero propio
        todas = db.scalars(select(Remesa).execution_options(**TODAS_LAS_EMPRESAS)).all()
        assert len(todas) == 2  # bypass explícito (comprobaciones de sistema)
        # Escrituras: sello automático del inquilino y bloqueo de filas ajenas
        nueva = Remesa(numero="AUTO-1", numero_generado=True)
        db.add(nueva)
        db.flush()
        assert nueva.empresa_id == e["emp_a"]
        db.add(Remesa(numero="HACK-1", numero_generado=True, empresa_id=e["emp_b"]))
        with pytest.raises(ViolacionAislamiento):
            db.flush()
        db.rollback()
        vincular_sesion(db, TenantScope(restringido=True, empresa_id=None))  # aislado sin empresa: nada
        assert db.scalars(select(Factura)).all() == []
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------- 3. Proof of Delivery
def test_tracking_publico_con_evidencia(client: TestClient, sup: dict[str, str], settings: Settings,
                                        empresa: int) -> None:
    ta = _usuario(client, sup, "transp_a", "transportista", empresa)
    ana, _, _ = _conductor(client, ta, "11111111", "Ana Chofer")
    prev = upload_remesa(client, sup, libro_remesa()).json()
    client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=sup, json={"remesas": ["2025-00001"]})
    f0, f1, _ = client.get("/api/conductor/facturas", headers=ana).json()["facturas"]
    for est in ("en_transito",):
        client.post("/api/conductor/eventos", headers=ana, json={"eventos": [_ev(f0["id"], est), _ev(f1["id"], est)]})

    def consulta(numero: str) -> dict[str, Any]:
        body = client.get("/api/publico/tracking", params={"q": numero}).json()
        assert body["encontrado"]
        return dict(body["resultados"][0])

    assert consulta(f0["numero"])["evidencia"] is None  # en tránsito: sin evidencia

    # Entrega con foto (hora de captura del teléfono)
    foto = str(uuid.uuid4())
    hace_una_hora = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
    capturada = hace_una_hora.astimezone(timezone(timedelta(hours=-4))).isoformat()  # hora local del teléfono
    r = client.put(f"/api/fotos/{foto}", headers=ana, data={"factura_id": f0["id"], "capturada_at": capturada},
                   files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    assert r.status_code == 201
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(f0["id"], "entregada", foto_id=foto)]}).json()["resultados"][0]
    assert out["ok"] and out["aplicado"]

    res = consulta(f0["numero"])
    ev = res["evidencia"]
    assert ev["estatus"] == "Entregada" and ev["content_type"] == "image/jpeg"
    assert ev["url"].startswith(f"/api/publico/evidencias/{foto}?exp=") and "&sig=" in ev["url"]
    assert _dt(ev["capturada_at"]) == hace_una_hora
    assert _dt(ev["expira_at"]) > datetime.now(timezone.utc)
    assert res["historial"][-1]["con_evidencia"] is True
    # La foto va adjunta al hito Entregada (como en el panel), no a los demás
    assert res["historial"][-1]["estatus"] == "Entregada" and res["historial"][-1]["evidencia_url"] == ev["url"]
    assert all(h["evidencia_url"] is None for h in res["historial"][:-1])
    assert client.get(f"/api/facturas/{f0['id']}", headers=sup).json()["evidencia_foto_id"] == foto

    img = client.get(ev["url"])
    assert img.status_code == 200 and img.content == JPEG_MINIMO
    assert img.headers["content-type"] == "image/jpeg" and img.headers["cache-control"].startswith("private")
    # Firma alterada, foto distinta o sin firma: 404
    malo = ev["url"][:-1] + ("0" if ev["url"][-1] != "0" else "1")
    assert client.get(malo).status_code == 404
    assert client.get(ev["url"].replace(foto, str(uuid.uuid4()))).status_code == 404
    assert client.get(f"/api/publico/evidencias/{foto}").status_code == 422

    # Devolución con foto: la evidencia respalda el nuevo cierre
    foto2 = str(uuid.uuid4())
    client.put(f"/api/fotos/{foto2}", headers=ana, data={"factura_id": f1["id"]},
               files={"archivo": ("d.jpg", JPEG_MINIMO, "image/jpeg")})
    client.post("/api/conductor/eventos", headers=ana,
                json={"eventos": [_ev(f1["id"], "devolucion", foto_id=foto2, motivo="Local cerrado")]})
    assert consulta(f1["numero"])["evidencia"]["estatus"] == "Devolución"

    # Revocación: si Transporte corrige el estatus, el enlace deja de servir aunque no haya caducado
    r = client.post(f"/api/facturas/{f0['id']}/estatus", headers=ta,
                    json={"estatus": "en_transito", "motivo": "Entregada por error"})
    assert r.status_code == 200 and r.json()["evidencia_foto_id"] is None
    assert consulta(f0["numero"])["evidencia"] is None
    assert client.get(ev["url"]).status_code == 404

    # Interruptor de privacidad
    settings.public_evidence_enabled = False
    assert consulta(f1["numero"])["evidencia"] is None

    # Borrar una remesa con evidencias (FK circular facturas <-> fotos) no falla
    assert client.delete(f"/api/remesas/{f0['remesa_id']}", headers=sup).status_code == 204
    assert client.get("/api/remesas", headers=sup).json()["total"] == 0


def test_firmador_hmac_caducidad() -> None:
    class _Foto:
        id = "abc-123"

    firmador = FirmadorHmac("s" * 40, timedelta(minutes=10))
    ahora = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    enlace = firmador.firmar(_Foto(), ahora)  # type: ignore[arg-type]
    exp = int(enlace.url.split("exp=")[1].split("&")[0])
    sig = enlace.url.split("sig=")[1]
    assert firmador.verificar("abc-123", exp, sig, ahora)
    assert not firmador.verificar("abc-123", exp, sig, enlace.expira_at + timedelta(seconds=1))
    assert not firmador.verificar("otra", exp, sig, ahora)
    assert not FirmadorHmac("t" * 40, timedelta(minutes=10)).verificar("abc-123", exp, sig, ahora)


# ------------------------------------------------------------- 4. Migración 0004
def test_migracion_asigna_empresa_principal_y_evidencias(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    cfg = alembic_config()
    t = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0003")
        usuario = ("INSERT INTO usuarios (id, username, nombre, rol, password_hash, activo, debe_cambiar_clave, "
                   "token_version, created_at, updated_at) VALUES (:id, :u, :u, :r, 'x', 1, 0, 0, :t, :t)")
        conn.execute(text(usuario), {"id": 1, "u": "sup", "r": "supervisor", "t": t})
        conn.execute(text(usuario), {"id": 2, "u": "transp", "r": "transportista", "t": t})
        conn.execute(text("INSERT INTO remesas (id, numero, numero_generado, created_at, updated_at) "
                          "VALUES (1, 'R-1', 0, :t, :t)"), {"t": t})
        conn.execute(text("INSERT INTO facturas (id, remesa_id, numero, estatus, estatus_at, version, created_at, "
                          "updated_at) VALUES (1, 1, 'F-1', 'entregada', :t, 3, :t, :t)"), {"t": t})
        conn.execute(text("INSERT INTO fotos (id, factura_id, ruta, content_type, bytes, sha256, created_at) "
                          "VALUES ('foto-1', 1, 'db', 'image/jpeg', 10, 'x', :t)"), {"t": t})
        conn.execute(text("INSERT INTO eventos_estatus (id, factura_id, estatus, ocurrido_at, recibido_at, origen, "
                          "aplicado, foto_id) VALUES ('ev-1', 1, 'entregada', :t, :t, 'online', 1, 'foto-1')"),
                     {"t": t})
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        emp = conn.execute(text("SELECT id, nombre FROM empresas")).all()
        assert [n for _, n in emp] == ["EMPRESA PRINCIPAL"]
        eid = emp[0][0]
        assert dict(conn.execute(text("SELECT username, empresa_id FROM usuarios")).all()) == {
            "sup": None, "transp": eid}
        assert conn.execute(text("SELECT empresa_id FROM remesas")).scalar() == eid
        fila = conn.execute(text("SELECT empresa_id, evidencia_foto_id FROM facturas")).one()
        assert tuple(fila) == (eid, "foto-1")
    with engine.begin() as conn:  # y vuelve atrás sin errores
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0003")
        ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'facturas'")).scalar()
        assert "empresa_id" not in str(ddl)
    engine.dispose()
