"""Rol CONSULTOR: ve todo el sistema (todas las empresas) y todos los botones, pero no agrega, modifica ni
elimina nada: al intentarlo recibe el aviso de «solo consulta»."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import login
from tests.test_flujo_completo import _cargar
from tracking.enums import Rol
from tracking.permissions import MATRIZ, Permiso
from tracking.security import MENSAJE_SOLO_CONSULTA

ESCRITURA = {
    Permiso.REMESAS_REASIGNAR, Permiso.REMESAS_ELIMINAR, Permiso.FACTURAS_CORREGIR_ESTATUS, Permiso.IMPORTAR_REMESA,
    Permiso.NOTIFICACIONES_ENVIAR, Permiso.CATALOGOS_GESTIONAR, Permiso.CONDUCTORES_CREAR,
    Permiso.CLAVES_CONDUCTORES_GESTIONAR, Permiso.USUARIOS_GESTIONAR, Permiso.CLAVES_USUARIOS_GESTIONAR,
    Permiso.EMPRESAS_GESTIONAR, Permiso.PERFIL_EDITAR, Permiso.ESTATUS_OPERATIVO_REGISTRAR,
}


def _consultor(client: TestClient, sup: dict[str, str]) -> dict[str, str]:
    r = client.post("/api/admin/usuarios", headers=sup,
                    json={"username": "consulta1", "nombre": "Ana Consultora", "rol": "consultor"})
    assert r.status_code == 201, r.text
    assert r.json()["usuario"]["rol"] == "consultor" and r.json()["usuario"]["empresa_id"] is None
    return login(client, "consulta1", r.json()["credencial"]["clave_temporal"])


def test_matriz_del_consultor_es_solo_lectura() -> None:
    permisos = MATRIZ[Rol.CONSULTOR]
    assert not permisos & ESCRITURA
    assert {Permiso.METRICAS_VER, Permiso.IMPORTACIONES_VER, Permiso.NOTIFICACIONES_VISTA,
            Permiso.REMESAS_VER, Permiso.FACTURAS_VER, Permiso.REPORTES_VER,
            Permiso.EMPRESAS_VER, Permiso.USUARIOS_VER, Permiso.AUDITORIA_VER, Permiso.CATALOGOS_VER} <= permisos


def test_consultor_ve_todo_y_no_cambia_nada(client: TestClient, sup: dict[str, str]) -> None:
    ctx = _cargar(client, sup)
    client.post(f"/api/importaciones/{ctx['preview']['id']}/confirmar", headers=sup, json={})
    co = _consultor(client, sup)

    # Lectura: todo lo que ve el Supervisor, de todas las empresas
    rem_sup = client.get("/api/remesas", headers=sup).json()
    rem_co = client.get("/api/remesas", headers=co).json()
    assert rem_co["total"] == rem_sup["total"] > 0
    rem_id = rem_co["items"][0]["id"]
    fac = client.get("/api/facturas", headers=co).json()["items"][0]
    usuarios = client.get("/api/admin/usuarios", headers=co).json()
    for url in ("/api/dashboard", "/api/reportes/metricas", "/api/reportes/cobranzas", "/api/importaciones",
                "/api/capturas", f"/api/reportes/cobranzas.xlsx?remesa_ids={rem_id}",
                "/api/reportes/indicador?desde=2026-01-01&hasta=2026-12-31", "/api/reportes/indicador/opciones",
                f"/api/remesas/{rem_id}", f"/api/facturas/{fac['id']}", "/api/admin/conductores",
                "/api/admin/camiones", "/api/admin/vendedores", "/api/admin/empresas", "/api/admin/auditoria",
                f"/api/admin/usuarios/{usuarios[0]['id']}/credencial", "/api/me", "/api/me/permisos"):
        assert client.get(url, headers=co).status_code == 200, url
    assert client.get("/api/admin/conductores", headers=co).json()[0]["cedula"]  # ve datos personales

    # Escritura: siempre 403, con el mensaje del modo consulta
    cid = client.get("/api/admin/conductores", headers=co).json()[0]["id"]
    intentos = [
        ("post", "/api/admin/conductores", {"cedula": "40000000", "nombre": "Nuevo Chofer"}),
        ("patch", f"/api/admin/conductores/{cid}", {"placa": "X"}),
        ("delete", f"/api/admin/conductores/{cid}", None),
        ("post", "/api/admin/usuarios", {"username": "x1", "nombre": "X", "rol": "cobranzas"}),
        ("post", "/api/admin/empresas", {"nombre": "OTRA"}),
        ("post", "/api/admin/vendedores", {"codigo": "ZZ-00001", "nombre": "Z"}),
        ("post", f"/api/facturas/{fac['id']}/estatus", {"estatus": "entregada"}),
        ("post", f"/api/remesas/{rem_id}/reasignar", {"conductor_id": cid}),
        ("delete", f"/api/remesas/{rem_id}", None),
        ("post", "/api/reportes/notificar/vendedores", {"modo": "final", "simular": False}),
        ("post", "/api/reportes/notificar/cobranzas", {}),
        ("post", f"/api/capturas/{'0' * 8}/confirmar", {}),
        ("patch", "/api/me/perfil", {"nombre": "Otro"}),
    ]
    for metodo, url, body in intentos:
        r = client.request(metodo.upper(), url, headers=co, json=body)
        assert r.status_code == 403 and r.json()["detail"] == MENSAJE_SOLO_CONSULTA, (metodo, url, r.text)
    # La vista previa de los correos (no envía nada) sí la puede ver
    r = client.post("/api/reportes/notificar/vendedores", headers=co, json={"modo": "final", "simular": True})
    assert r.status_code == 200 and "desglose" in r.json()
    # La interfaz le muestra los botones del Supervisor; sus permisos reales siguen siendo de lectura
    p_co = client.get("/api/me/permisos", headers=co).json()
    p_sup = client.get("/api/me/permisos", headers=sup).json()
    # ...salvo los montos de las facturas: solo SUPERVISOR y COBRANZAS
    assert p_co["solo_consulta"] is True
    assert sorted(p_co["permisos_interfaz"]) == sorted(set(p_sup["permisos"]) - {"facturas.montos.ver"})
    assert "remesas.eliminar" in p_co["permisos_interfaz"] and "remesas.eliminar" not in p_co["permisos"]
    assert p_sup["solo_consulta"] is False and p_sup["permisos_interfaz"] == p_sup["permisos"]
    assert client.get("/api/remesas", headers=sup).json()["total"] == rem_sup["total"]  # nada cambió

    # Sí puede cambiar su propia clave (la temporal se cambia al primer ingreso)
    r = client.post("/api/admin/usuarios/{}/clave".format(
        next(u["id"] for u in client.get("/api/admin/usuarios", headers=sup).json() if u["username"] == "consulta1")),
        headers=sup, json={"clave_nueva": "Consulta-123", "debe_cambiar_clave": False})
    assert r.status_code == 200, r.text
    co = login(client, "consulta1", "Consulta-123")
    r = client.post("/api/me/clave", headers=co, json={"clave_actual": "Consulta-123", "clave_nueva": "Consulta-456"})
    assert r.status_code == 200


def test_solo_el_supervisor_crea_consultores(client: TestClient, sup: dict[str, str]) -> None:
    from tests.conftest import crear_empresa
    from tests.test_capturas import _usuario

    tb = _usuario(client, sup, "transp", "transportista", crear_empresa(client, sup))
    nuevo = {"username": "consul2", "nombre": "Consul Dos", "rol": "consultor"}
    r = client.post("/api/admin/usuarios", headers=tb, json=nuevo)
    assert r.status_code == 403, r.text
    # Un usuario existente se puede pasar a Consultor (y deja de poder escribir)
    uid = client.post("/api/admin/usuarios", headers=sup,
                      json={"username": "cob1", "nombre": "Cob", "rol": "cobranzas"}).json()["usuario"]["id"]
    r = client.patch(f"/api/admin/usuarios/{uid}", headers=sup, json={"rol": "consultor"})
    assert r.status_code == 200 and r.json()["rol"] == "consultor"


def test_consultor_extrae_la_foto_y_analiza_la_hoja_sin_registrar(client: TestClient, sup: dict[str, str]) -> None:
    import gzip
    import json

    from tests.factories import libro_remesa
    from tests.test_capturas import CAJAS_USY, _texto
    from tests.test_vercel import _filas

    co = _consultor(client, sup)

    # 1) Foto: toda la extracción (campos y avisos), sin guardar foto ni captura
    cajas = [dict(zip(("texto", "confianza", "x", "y", "w", "h"), c, strict=True)) for c in CAJAS_USY]
    lectura = {"texto": _texto(CAJAS_USY), "confianza": 0.9, "cajas": cajas}
    r = client.post("/api/capturas/analizar", headers=co, json=lectura)
    assert r.status_code == 200, r.text
    leida = r.json()
    assert leida["id"] == "" and leida["estado"] == "analisis"
    valores = {k: v["valor"] for k, v in leida["campos"].items()}
    assert valores["numero"] == "340239" and valores["total_neto"] == "940.5000" and valores["bultos"] == 3
    assert any("vendedor" in a for a in leida["avisos"])  # mismos avisos que el flujo normal
    assert client.get("/api/capturas", headers=sup).json() == []  # nada guardado
    r = client.post("/api/capturas//confirmar", headers=co, json={})
    assert r.status_code in (403, 404, 405)

    # 2) Hoja de remesas: el mismo análisis que la vista previa, sin crear la importación
    data = libro_remesa()
    filas = _filas(data, "REMESA 2026,")
    cuerpo = gzip.compress(json.dumps({"archivo": "REMESA.xlsx", "hoja": "REMESA 2026,",
                                       "hojas": ["PLACAS", "REMESA 2026,"], "filas": filas}).encode())
    r = client.post("/api/importaciones/analizar", headers={**co, "Content-Type": "application/json"}, content=cuerpo)
    assert r.status_code == 200, r.text
    imp = r.json()
    assert imp["id"] == 0 and imp["estado"] == "analisis" and imp["resumen"]["totales"]["facturas_nuevas"] == 6
    files = {"archivo": ("REMESA.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = client.post("/api/importaciones/analizar-archivo", headers=co, files=files)
    assert r.status_code == 200 and r.json()["resumen"]["choferes"], r.text
    assert client.get("/api/importaciones", headers=sup).json() == []  # nada guardado
    assert client.get("/api/remesas", headers=sup).json()["total"] == 0
    # Confirmar o subir para cargar de verdad: solo consulta
    r = client.post("/api/importaciones/0/confirmar", headers=co, json={})
    assert r.status_code == 403 and r.json()["detail"] == MENSAJE_SOLO_CONSULTA
    r = client.post("/api/importaciones", headers=co, files=files)
    assert r.status_code == 403 and r.json()["detail"] == MENSAJE_SOLO_CONSULTA
