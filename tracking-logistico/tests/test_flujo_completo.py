"""Flujo de punta a punta: carga REMESA -> conductor cambia estatus -> consulta pública -> Cobranzas."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.conftest import crear_empresa, login, upload_remesa
from tests.factories import JPEG_MINIMO, libro_remesa, vendedores_xlsx


def _ev(factura_id: int, estatus: str, **kw: Any) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "factura_id": factura_id, "estatus": estatus, **kw}


def _cargar(client: TestClient, sup: dict[str, str]) -> dict[str, Any]:
    # Catálogos: choferes y camiones desde la hoja PLACAS + vendedores (choferes en la empresa de transporte)
    empresa = crear_empresa(client, sup)
    r = client.post("/api/admin/catalogos/placas", headers=sup, data={"empresa_id": str(empresa)},
                    files={"archivo": ("REMESA.xlsx", libro_remesa(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    creds = {c["usuario"]: c["clave_temporal"] for c in r.json()["credenciales"]}
    assert set(creds) == {"11111111", "22222222"}
    r = client.post("/api/admin/catalogos/vendedores", headers=sup,
                    files={"archivo": ("v.xlsx", vendedores_xlsx(), "application/octet-stream")})
    assert r.json()["creados"] == 2

    data = libro_remesa()  # mismos bytes en ambas cargas (openpyxl incluye la hora en el archivo)
    prev = upload_remesa(client, sup, data)
    assert prev.status_code == 201, prev.text
    resumen = prev.json()["resumen"]
    t = resumen["totales"]
    assert t["remesas"] == 3 and t["facturas_nuevas"] == 6 and t["choferes_no_registrados"] == 1
    assert any(a["codigo"] == "CHOFER_NO_REGISTRADO" for a in resumen["avisos"])
    return {"preview": prev.json(), "creds": creds, "data": data, "empresa": empresa}


def test_flujo_completo(client: TestClient, sup: dict[str, str]) -> None:
    ctx = _cargar(client, sup)
    imp_id = ctx["preview"]["id"]

    # Sin registrar choferes nuevos: la remesa del chofer desconocido se excluye
    r = client.post(f"/api/importaciones/{imp_id}/confirmar", headers=sup, json={})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["facturas_creadas"] == 5 and res["remesas_creadas"] == 2
    assert res["remesas_excluidas"] == ["2025-00003"]

    # Confirmar dos veces -> 409
    assert client.post(f"/api/importaciones/{imp_id}/confirmar", headers=sup, json={}).status_code == 409

    # Volver a cargar el mismo archivo: idempotente, solo ofrece lo que falta
    prev2 = upload_remesa(client, sup, ctx["data"]).json()
    assert prev2["resumen"]["archivo_repetido"] is True
    assert prev2["resumen"]["totales"]["facturas_ya_cargadas"] == 5
    r = client.post(f"/api/importaciones/{prev2['id']}/confirmar", headers=sup,
                    json={"remesas": ["2025-00003"], "registrar_choferes": True})
    assert r.status_code == 422  # el Supervisor debe indicar la empresa de los choferes nuevos
    r = client.post(f"/api/importaciones/{prev2['id']}/confirmar", headers=sup,
                    json={"remesas": ["2025-00003"], "registrar_choferes": True, "empresa_id": ctx["empresa"]})
    assert r.status_code == 200, r.text
    assert r.json()["facturas_creadas"] == 1
    nuevo = r.json()["choferes_registrados"][0]
    assert nuevo["usuario"] == "33333333" and nuevo["clave_temporal"] == "qwerty"  # CLAVE_TEMPORAL

    # ---------------- Conductor
    ana = login(client, "V-11.111.111", ctx["creds"]["11111111"])  # tolera formato de cédula
    me = client.get("/api/me", headers=ana).json()
    assert me["rol"] == "conductor" and me["debe_cambiar_clave"] is True
    r = client.post("/api/me/clave", headers=ana,
                    json={"clave_actual": ctx["creds"]["11111111"], "clave_nueva": "nueva-clave-ana"})
    assert r.status_code == 200
    ana = {"Authorization": f"Bearer {r.json()['access_token']}"}

    facts = client.get("/api/conductor/facturas", headers=ana).json()["facturas"]
    assert len(facts) == 3 and all(f["estatus"] == "recibida" for f in facts)
    assert "total_fact" not in facts[0]  # el conductor no ve montos
    ids = [f["id"] for f in facts]

    # Lote enviado por un teléfono con la versión anterior: «cargado» (estatus retirado) entra como Por Entregar
    lote = [_ev(i, "cargado") for i in ids]
    out = client.post("/api/conductor/eventos", headers=ana, json={"eventos": lote}).json()["resultados"]
    assert all(o["ok"] and o["aplicado"] and o["estatus_actual"] == "en_transito" for o in out)
    assert all(o["tracking"] and o["tracking"].startswith("TR-") for o in out)
    tracking0 = out[0]["tracking"]

    # Reenvío del mismo lote (cola offline reintenta): duplicado, sin efecto
    out = client.post("/api/conductor/eventos", headers=ana, json={"eventos": lote}).json()["resultados"]
    assert all(o["ok"] and o["duplicado"] for o in out)

    # En tránsito + entregada con foto opcional
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(i, "en_transito") for i in ids]}).json()["resultados"]
    assert all(o["estatus_actual"] == "en_transito" for o in out)

    foto_id = str(uuid.uuid4())
    r = client.put(f"/api/fotos/{foto_id}", headers=ana, data={"factura_id": ids[0]},
                   files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    assert r.status_code == 201, r.text
    r = client.put(f"/api/fotos/{foto_id}", headers=ana, data={"factura_id": ids[0]},
                   files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    assert r.json()["duplicado"] is True
    # archivo que no es imagen
    r = client.put(f"/api/fotos/{uuid.uuid4()}", headers=ana, data={"factura_id": ids[0]},
                   files={"archivo": ("x.jpg", b"no soy imagen", "image/jpeg")})
    assert r.status_code == 422

    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(ids[0], "entregada", foto_id=foto_id)]}).json()["resultados"][0]
    assert out["ok"] and out["estatus_actual"] == "entregada"

    # Devolución sin foto -> rechazada; con foto aún no subida -> foto_pendiente (reintentar)
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(ids[1], "devolucion")]}).json()["resultados"][0]
    assert not out["ok"] and out["codigo_error"] == "invalido"
    foto2 = str(uuid.uuid4())
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(ids[1], "devolucion", foto_id=foto2)]}).json()["resultados"][0]
    assert out["codigo_error"] == "foto_pendiente"
    client.put(f"/api/fotos/{foto2}", headers=ana, data={"factura_id": ids[1]},
               files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(ids[1], "devolucion", foto_id=foto2, motivo="Cerrado")]}
                      ).json()["resultados"][0]
    assert out["ok"] and out["estatus_actual"] == "devolucion"

    # Evento offline atrasado (ocurrió antes) -> queda en historial sin aplicarse
    viejo = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(ids[0], "en_transito", ocurrido_at=viejo, offline=True)]}
                      ).json()["resultados"][0]
    assert out["ok"] and not out["aplicado"] and out["estatus_actual"] == "entregada"

    # Retroceso reciente -> conflicto (solo Transporte corrige)
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(ids[0], "cargado")]}).json()["resultados"][0]
    assert out["codigo_error"] == "conflicto"

    # Entregada -> Devolución más reciente: gana el último cambio
    foto3 = str(uuid.uuid4())
    client.put(f"/api/fotos/{foto3}", headers=ana, data={"factura_id": ids[0]},
               files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
    out = client.post("/api/conductor/eventos", headers=ana,
                      json={"eventos": [_ev(ids[0], "devolucion", foto_id=foto3)]}).json()["resultados"][0]
    assert out["ok"] and out["estatus_actual"] == "devolucion"

    # Otro conductor no puede tocar facturas ajenas
    beto = login(client, "22222222", ctx["creds"]["22222222"])
    out = client.post("/api/conductor/eventos", headers=beto,
                      json={"eventos": [_ev(ids[2], "entregada")]}).json()["resultados"][0]
    assert out["codigo_error"] == "sin_permiso"
    assert client.get(f"/api/fotos/{foto_id}", headers=beto).status_code == 403
    assert client.get(f"/api/fotos/{foto_id}", headers=sup).status_code == 200

    # Corrección administrativa hacia atrás exige motivo
    r = client.post(f"/api/facturas/{ids[0]}/estatus", headers=sup, json={"estatus": "en_transito"})
    assert r.status_code == 409
    r = client.post(f"/api/facturas/{ids[0]}/estatus", headers=sup,
                    json={"estatus": "en_transito", "motivo": "Se marcó por error"})
    assert r.status_code == 200 and r.json()["estatus"] == "en_transito"
    det = client.get(f"/api/facturas/{ids[0]}", headers=sup).json()
    assert [e["estatus"] for e in det["eventos"] if e["aplicado"]][-1] == "en_transito"
    assert any(not e["aplicado"] for e in det["eventos"])

    # ---------------- Consulta pública (sin clave)
    for q in (facts[0]["numero"], facts[0]["numero"].replace("FAC-", ""), tracking0, facts[0]["pedido"]):
        r = client.get("/api/publico/tracking", params={"q": q.lower()})
        body = r.json()
        assert body["encontrado"], q
        res0 = body["resultados"][0]
        assert res0["numero"] == facts[0]["numero"]
        assert "total_fact" not in res0 and "cedula" not in str(body).lower()
        assert len(res0["historial"]) >= 3
    r = client.get("/api/publico/tracking", params={"q": "FAC-NOEXISTE"}).json()
    assert r == {"encontrado": False, "mensaje": "No encontramos esa factura, pedido o tracking.",
                 "resultados": []}

    # ---------------- Remesas / dashboard / métricas
    rem = client.get("/api/remesas", headers=sup).json()
    assert rem["total"] == 3
    assert rem["items"][0]["chofer_cedula"]  # supervisor ve datos personales
    dash = client.get("/api/dashboard", headers=sup).json()
    assert dash["remesas_activas"] >= 1
    met = client.get("/api/reportes/metricas", headers=sup).json()
    assert met["cambios_de_estatus"] > 0 and met["pct_mismo_dia"] is not None
    assert met["cambios_sin_senal_sincronizados"] == 1

    # ---------------- Cobranzas
    cob_user = client.post("/api/admin/usuarios", headers=sup,
                           json={"username": "cobranzas1", "nombre": "Cobranzas", "rol": "cobranzas"}).json()
    cob = login(client, "cobranzas1", cob_user["credencial"]["clave_temporal"])
    rem_cob = client.get("/api/remesas", headers=cob).json()
    assert rem_cob["items"][0]["chofer_cedula"] is None  # Cobranzas no ve cédula
    assert upload_remesa(client, cob, libro_remesa()).status_code == 403  # solo lectura
    assert client.get("/api/conductor/facturas", headers=cob).status_code == 403

    r = client.get("/api/reportes/cobranzas.xlsx", headers=cob)
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb.active
    assert ws is not None
    valores = [[c.value for c in row] for row in ws.iter_rows()]
    planos = [v for row in valores for v in row if v is not None]
    assert "Cod. Vendedor" in planos and "Estatus" in planos and "Devolución" in planos
    assert "Chofer:" in planos and any(isinstance(v, str) and v.startswith("=SUM(") for v in planos)
    hdr = next(row for row in valores if "Factura" in row)
    assert hdr[2] == "Factura" and hdr[1] == "IMPORTADORA USY"  # mismo layout que Libro2 (columna B)

    # Simulación de correos (no requiere SMTP)
    r = client.post("/api/reportes/notificar/vendedores", headers=sup, json={"modo": "final", "simular": True})
    assert r.status_code == 200 and r.json()["vendedores"] >= 1
    r = client.post("/api/reportes/notificar/cobranzas", headers=sup, json={"simular": True})
    assert r.status_code == 200
    r = client.post("/api/reportes/notificar/cobranzas", headers=sup, json={})
    assert r.status_code == 409  # correo desactivado por defecto


def test_permisos_y_login(client: TestClient, sup: dict[str, str], empresa: int) -> None:
    assert client.get("/api/remesas").status_code == 401
    r = client.post("/api/auth/login", json={"username": "supervisor", "password": "mala"})
    assert r.status_code == 401
    assert client.get("/api/remesas", headers={"Authorization": "Bearer basura"}).status_code == 401
    # Transportista no crea usuarios de oficina (sí ve a los conductores de su empresa, ver test_usuarios_activo)
    t = client.post("/api/admin/usuarios", headers=sup,
                    json={"username": "transp", "nombre": "Transporte", "rol": "transportista",
                          "clave": "clave-transp", "empresa_id": empresa}).json()
    assert t["credencial"] is None
    tr = login(client, "transp", "clave-transp")
    assert client.get("/api/admin/usuarios", headers=tr).json() == []  # aún sin conductores en su empresa
    nuevo = {"username": "otro", "nombre": "Otro Usuario", "rol": "cobranzas"}
    assert client.post("/api/admin/usuarios", headers=tr, json=nuevo).status_code == 403
    # ...pero sí conductores y camiones
    r = client.post("/api/admin/conductores", headers=tr,
                    json={"cedula": "44.444.444", "nombre": "Dora", "telefono": "4144444444"})
    assert r.status_code == 201 and r.json()["credencial"]["usuario"] == "44444444"
    cid = r.json()["conductor"]["id"]
    assert client.post("/api/admin/conductores", headers=tr,
                       json={"cedula": "44444444", "nombre": "Dup"}).status_code == 409
    r = client.post("/api/admin/camiones", headers=tr, json={"placa": "a 1b 2c"})
    assert r.status_code == 201 and r.json()["placa"] == "A1B2C"
    assert client.delete(f"/api/admin/conductores/{cid}", headers=tr).json()["eliminado"] is True
    # Cambiar clave invalida el token anterior
    r = client.post("/api/me/clave", headers=tr, json={"clave_actual": "clave-transp", "clave_nueva": "otra-clave"})
    assert r.status_code == 200
    assert client.get("/api/me", headers=tr).status_code == 401


def test_descartar_y_formato_invalido(client: TestClient, sup: dict[str, str]) -> None:
    r = upload_remesa(client, sup, b"no es excel", name="x.xlsx")
    assert r.status_code == 415
    r = upload_remesa(client, sup, b"PK", name="x.csv")
    assert r.status_code == 415
    prev = upload_remesa(client, sup, libro_remesa()).json()
    assert client.post(f"/api/importaciones/{prev['id']}/descartar", headers=sup).status_code == 204
    assert client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=sup, json={}).status_code == 409
    lista = client.get("/api/importaciones", headers=sup).json()
    assert lista[0]["estado"] == "descartada"


def test_health_y_rate_limit_publico(settings: Any) -> None:
    from tracking.main import create_app

    settings.public_rate_limit_per_minute = 2
    with TestClient(create_app(settings)) as c:
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/api/publico/tracking", params={"q": "abc"}).status_code == 200
        assert c.get("/api/publico/tracking", params={"q": "abc"}).status_code == 200
        assert c.get("/api/publico/tracking", params={"q": "abc"}).status_code == 429
