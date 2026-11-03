"""Captura de facturas por foto (OCR + validación humana) y reporte de Cobranzas en pantalla."""

from __future__ import annotations

import io
import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.conftest import crear_empresa, login, upload_remesa
from tests.factories import JPEG_MINIMO, libro_remesa
from tracking.config import Settings
from tracking.services.ai import ClaudeClient
from tracking.services.ocr_facturas import (
    CONFIANZA_REGEX,
    CajaOCR,
    CampoExtraido,
    ExtractorClaudeVision,
    ExtractorLocal,
    LecturaLocal,
    ResultadoExtraccion,
    normalizar,
    parse_fecha,
    parse_monto,
    parse_porcentaje,
    parsear_cajas,
    parsear_texto_factura,
    revisar_coherencia,
)

# Lo que un modelo de visión devuelve para la factura de ejemplo (IMPORTADORA USY, factura 340239)
CRUDO_EJEMPLO: dict[str, Any] = {
    "es_factura": True,
    "empresa_emisora": "IMPORTADORA USY, C.A.",
    "cliente": "COMERCIAL SUPER EXCELENTE, C.A",
    "numero_factura": "340239",
    "pedido": "PED-1000000267517",
    "fecha": "03-07-2026",
    "cod_vendedor": "BO-07001",
    "bultos": "3",
    "subtotal": "$ 990,0000",
    "descuento_pct": "5,00%",
    "base_imponible": "$ 940,5000",
    "confianza": {"numero_factura": 0.97, "cliente": 0.95, "fecha": 0.9, "bultos": 0.6},
}


class ExtractorFalso:
    nombre = "falso"

    def __init__(self, crudo: dict[str, Any] | None = None) -> None:
        self.crudo = crudo or CRUDO_EJEMPLO
        self.llamadas = 0

    def extraer(self, imagen: bytes, content_type: str, lectura: LecturaLocal | None = None) -> ResultadoExtraccion:
        self.llamadas += 1
        campos, avisos = normalizar(self.crudo)
        return ResultadoExtraccion(self.nombre, campos, avisos)


# ------------------------------------------------------------------ pipeline (unidad)
def test_normalizacion_de_la_factura_de_ejemplo() -> None:
    assert parse_monto("$ 990,0000") == Decimal("990.0000")
    assert parse_monto("$ 1.090,9800") == Decimal("1090.9800")
    assert parse_monto("1,090.98") == Decimal("1090.9800")
    assert parse_monto("sin monto") is None
    assert parse_porcentaje("5,00%") == Decimal("0.0500")
    assert parse_fecha("03-07-2026") == date(2026, 7, 3) and parse_fecha("2026/07/03") == date(2026, 7, 3)
    assert parse_fecha("31-02-2026") is None

    campos, avisos = normalizar(CRUDO_EJEMPLO)
    valores = {k: c.valor for k, c in campos.items()}
    assert valores == {
        "numero": "340239", "pedido": "PED-1000000267517", "cliente": "COMERCIAL SUPER EXCELENTE, C.A",
        "fecha_fac": "2026-07-03", "cod_vendedor": "BO-07001", "bultos": 3, "total_fact": "990.0000",
        "porcentaje": "0.0500", "total_neto": "940.5000", "empresa_emisora": "IMPORTADORA USY",
    }
    assert avisos == []
    assert campos["bultos"].dudoso and not campos["numero"].dudoso  # confianza 0.6 < 0.8
    assert revisar_coherencia(campos, hoy=date(2026, 7, 10)) == []  # 990 × 0,95 = 940,50


def test_coherencia_marca_montos_y_fechas_raras() -> None:
    campos, _ = normalizar({**CRUDO_EJEMPLO, "base_imponible": "$ 904,5000", "fecha": "03-07-2027",
                            "numero_factura": None})
    avisos = " ".join(revisar_coherencia(campos, hoy=date(2026, 10, 19)))
    assert "no coincide" in avisos and "futura" in avisos and "Falta número de factura" in avisos


def test_extractor_claude_usa_vision_y_salida_estructurada(settings: Settings) -> None:
    peticiones: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        peticiones.append(json.loads(req.content))
        return httpx.Response(200, json={"content": [
            {"type": "tool_use", "id": "t1", "name": "registrar_factura", "input": CRUDO_EJEMPLO}]})

    settings.anthropic_api_key = "k"
    cliente = ClaudeClient(settings, transport=httpx.MockTransport(handler))
    r = ExtractorClaudeVision(cliente, "modelo-vision").extraer(JPEG_MINIMO, "image/jpeg")
    assert r.error is None and r.motor == "anthropic:modelo-vision" and r.campos["numero"].valor == "340239"
    body = peticiones[0]
    assert body["model"] == "modelo-vision"
    assert body["tool_choice"] == {"type": "tool", "name": "registrar_factura"}
    imagen = body["messages"][0]["content"][0]
    assert imagen["type"] == "image" and imagen["source"]["media_type"] == "image/jpeg"

    # Error de la API (no reintentable): la captura no se pierde, queda para transcribir a mano
    malo = ClaudeClient(settings, transport=httpx.MockTransport(lambda _: httpx.Response(400, json={})))
    r = ExtractorClaudeVision(malo, "m").extraer(JPEG_MINIMO, "image/jpeg")
    assert r.error and all(c.valor is None for c in r.campos.values())


# --------------------------------------------------------------------- API (flujo)
def _escenario(client: TestClient, sup: dict[str, str]) -> dict[str, Any]:
    client.app.state.extractor_facturas = ExtractorFalso()  # type: ignore[attr-defined]
    emp = crear_empresa(client, sup)
    r = client.post("/api/admin/conductores", headers=sup,
                    json={"cedula": "11111111", "nombre": "Leyder Barreto", "placa": "AB123CD", "empresa_id": emp})
    return {"emp": emp, "cid": r.json()["conductor"]["id"]}


def _usuario(client: TestClient, sup: dict[str, str], username: str, rol: str, empresa: int | None) -> dict[str, str]:
    datos = {"username": username, "nombre": username.title(), "rol": rol, "clave": f"clave-{username}",
             "empresa_id": empresa}
    r = client.post("/api/admin/usuarios", headers=sup, json=datos)
    assert r.status_code == 201, r.text
    return login(client, username, f"clave-{username}")


def _foto(client: TestClient, h: dict[str, str]) -> dict[str, Any]:
    r = client.post("/api/capturas", headers=h, files={"archivo": ("factura.jpg", JPEG_MINIMO, "image/jpeg")})
    assert r.status_code == 201, r.text
    return dict(r.json())


def _confirmar(c: dict[str, Any], **extra: Any) -> dict[str, Any]:
    datos = {k: v["valor"] for k, v in c["campos"].items()}
    return {**datos, **extra}


def test_captura_ocr_revision_y_alta(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    cap = _foto(client, sup)
    assert cap["estado"] == "pendiente" and cap["motor"] == "falso"
    assert cap["campos"]["pedido"]["valor"] == "PED-1000000267517" and cap["campos"]["bultos"]["dudoso"]
    assert any("BO-07001" in a for a in cap["avisos"])  # vendedor fuera del listado
    img = client.get(cap["imagen_url"], headers=sup)
    assert img.status_code == 200 and img.content == JPEG_MINIMO
    assert [x["id"] for x in client.get("/api/capturas", headers=sup).json()] == [cap["id"]]

    # El operador corrige un valor (bultos) y asigna conductor; remesa nueva con destino
    body = _confirmar(cap, bultos=4, conductor_id=e["cid"], destino="GUAYANA")
    r = client.post(f"/api/capturas/{cap['id']}/confirmar", headers=sup, json=body)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["remesa_nueva"] and res["remesa_numero"].startswith("CF-") and res["conductor"] == "Leyder Barreto"

    f = client.get(f"/api/facturas/{res['factura_id']}", headers=sup).json()
    assert (f["numero"], f["estatus"], f["bultos"], f["cod_vendedor"]) == ("340239", "recibida", 4, "BO-07001")
    assert Decimal(f["total_fact"]) == Decimal("990") and Decimal(f["porcentaje"]) == Decimal("0.05")
    assert f["fecha_fac"] == "2026-07-03" and f["empresa"] == "IMPORTADORA USY" and f["empresa_id"] == e["emp"]
    assert f["captura_id"] == cap["id"] and f["eventos"][0]["origen"] == "captura"
    rem = client.get(f"/api/remesas/{res['remesa_id']}", headers=sup).json()
    assert rem["destino"] == "GUAYANA" and rem["placa"] == "AB123CD" and rem["conductor_id"] == e["cid"]

    hecha = client.get(f"/api/capturas/{cap['id']}", headers=sup).json()
    assert hecha["estado"] == "confirmada" and hecha["datos_validados"]["bultos"] == 4
    assert hecha["campos"]["bultos"]["valor"] == 3  # se conserva lo que leyó la IA (medir precisión)
    assert client.post(f"/api/capturas/{cap['id']}/confirmar", headers=sup, json=body).status_code == 409

    # Misma factura otra vez: duplicado (aviso al capturar y 409 al confirmar)
    cap2 = _foto(client, sup)
    assert any("ya está en el sistema" in a for a in cap2["avisos"])
    r = client.post(f"/api/capturas/{cap2['id']}/confirmar", headers=sup,
                    json=_confirmar(cap2, conductor_id=e["cid"], remesa_id=res["remesa_id"]))
    assert r.status_code == 409
    assert client.post(f"/api/capturas/{cap2['id']}/descartar", headers=sup).status_code == 204
    assert client.get("/api/capturas", headers=sup).json() == []

    # Otra factura a la remesa abierta del conductor
    client.app.state.extractor_facturas = ExtractorFalso({**CRUDO_EJEMPLO, "numero_factura": "340240"})  # type: ignore[attr-defined]
    cap3 = _foto(client, sup)
    r = client.post(f"/api/capturas/{cap3['id']}/confirmar", headers=sup,
                    json=_confirmar(cap3, conductor_id=e["cid"], remesa_id=res["remesa_id"]))
    assert r.status_code == 200 and r.json()["remesa_nueva"] is False
    assert client.get(f"/api/remesas/{res['remesa_id']}", headers=sup).json()["total_facturas"] == 2


def test_captura_aislada_por_empresa(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    otra = crear_empresa(client, sup, "OTRA")
    tb = _usuario(client, sup, "transp_b", "transportista", otra)
    cap = _foto(client, tb)
    # No puede asignar un conductor de otra empresa
    r = client.post(f"/api/capturas/{cap['id']}/confirmar", headers=tb, json=_confirmar(cap, conductor_id=e["cid"]))
    assert r.status_code == 404
    # La captura del transportista de otra empresa no es visible para otro transportista
    ta = _usuario(client, sup, "transp_a", "transportista", e["emp"])
    assert client.get(f"/api/capturas/{cap['id']}", headers=ta).status_code == 404
    assert client.get(f"/api/capturas/{cap['id']}/imagen", headers=ta).status_code == 404
    assert client.get("/api/capturas", headers=ta).json() == []
    # Cobranzas (sin permiso de carga) no captura
    cob = _usuario(client, sup, "cobra", "cobranzas", None)
    assert client.post("/api/capturas", headers=cob,
                       files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")}).status_code == 403


def test_captura_sin_texto_y_archivo_invalido(client: TestClient, sup: dict[str, str]) -> None:
    assert client.get("/api/config").json()["ocr_modo"] == "local"  # sin ANTHROPIC_API_KEY: OCR gratis
    cap = _foto(client, sup)  # el navegador no pudo leer texto: formulario vacío para transcribir
    assert cap["motor"] == "manual" and all(c["valor"] is None for c in cap["campos"].values())
    assert any("a mano" in a for a in cap["avisos"])
    r = client.post("/api/capturas", headers=sup, files={"archivo": ("x.jpg", b"no soy imagen", "image/jpeg")})
    assert r.status_code == 422


# ------------------------------------------------- OCR gratuito (Tesseract.js en el navegador)
# Texto real que Tesseract.js leyó de la foto de ejemplo (con sus errores típicos de OCR)
TEXTO_OCR = """-» e : INPORTADORA USY, C.A. RIF: J-30030804-9 | h
a qee ve 100771101 Maxim miajoas- N*CONTROL: 00-308057
E IECOMÉRCIAL SUPER EXCELENTE.CA Serie F
ie mea COD. CLIENTE: [500004702 Y EACTURA
CIÓN: CALLE CEDENO LOCAL NRO 18 SECTOR UD-TUT CIUDAD GUAYANA 340239
NOS: (412) 026-6308/ (256) 071-0428 Fax: (208) 971-0426 Fechal| 007-2020 | |
DOR: | BO-07001 Nro, Orden de Compra: Y
DoR: DisfaFeña — Nro. Orden de Pedido: FED-1000000267517 :
8 eL 0 Descuento        5.00% DE NT
2 egun Art. 25 Ley de IVA y Art. Base imponible $940.50
É a POT Li LE ut  # Bultos 3
e E Importe Total $ 1.090,0800"""


def test_parser_de_texto_ocr_tolera_errores() -> None:
    crudo = parsear_texto_factura(LecturaLocal(TEXTO_OCR, 0.7))
    assert crudo["numero_factura"] == "340239"  # bajo «EACTURA» (la F leída como E), no el Nº de control
    assert crudo["pedido"] == "PED-1000000267517"  # «FED-» corregido
    assert crudo["cod_vendedor"] == "BO-07001" and crudo["bultos"] == "3"
    assert crudo["base_imponible"] == "940.50" and crudo["descuento_pct"] == "5.00%"
    assert crudo["cliente"] == "IECOMERCIAL SUPER EXCELENTE, C.A"  # el operador corrige el ruido del borde
    assert crudo["empresa_emisora"] == "IMPORTADORA USY, C.A." and "fecha" not in crudo
    assert all(c <= CONFIANZA_REGEX for c in crudo["confianza"].values())  # todo lo hallado así, a revisar

    # Las zonas (releídas junto a su etiqueta) mandan sobre el texto general; y se calcula lo que falta
    zonas = {"fecha": ("03-07-2026", 0.91), "numero_factura": ("340239", 0.95), "pedido": ("basura", 0.2),
             "descuento_pct": ("50", 0.31), "cod_vendedor": ("BO-07001", 0.4)}
    r = ExtractorLocal().extraer(b"", "image/jpeg", LecturaLocal(TEXTO_OCR, 0.7, zonas))
    assert r.motor == ExtractorLocal.nombre
    assert r.campos["fecha_fac"].valor == "2026-07-03" and not r.campos["fecha_fac"].dudoso
    assert r.campos["numero"].confianza == 0.95 and r.campos["pedido"].valor == "PED-1000000267517"
    assert r.campos["porcentaje"].valor == "0.0500"  # zona dudosa («50») pierde frente al texto («5.00%»)
    assert r.campos["total_fact"].valor == "990.0000" and r.campos["total_fact"].dudoso  # 940,50 / 0,95
    assert any("calculado" in a for a in r.avisos)


def test_captura_con_ocr_del_navegador(client: TestClient, sup: dict[str, str]) -> None:
    zonas = {"fecha": {"texto": "03-07-2026", "confianza": 0.9}, "subtotal": {"texto": "$ 990,0000", "confianza": 0.88}}
    r = client.post("/api/capturas", headers=sup, files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")},
                    data={"ocr_texto": TEXTO_OCR, "ocr_confianza": "0.7", "ocr_zonas": json.dumps(zonas)})
    assert r.status_code == 201, r.text
    cap = r.json()
    assert cap["motor"] == ExtractorLocal.nombre
    valores = {k: v["valor"] for k, v in cap["campos"].items()}
    assert valores["numero"] == "340239" and valores["fecha_fac"] == "2026-07-03"
    assert valores["total_fact"] == "990.0000" and valores["total_neto"] == "940.5000"
    assert cap["campos"]["cliente"]["dudoso"] is True
    malo = client.post("/api/capturas", headers=sup, files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")},
                       data={"ocr_texto": "x", "ocr_zonas": "{no json"})
    assert malo.status_code == 422


def test_flujo_instantaneo_lectura_en_segundo_plano(client: TestClient, sup: dict[str, str]) -> None:
    # 1) La foto se sube sin esperar al OCR: captura vacía, sin avisos de «no se pudo leer»
    r = client.post("/api/capturas", headers=sup, files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")},
                    data={"ocr_en_curso": "true"})
    cap = r.json()
    assert r.status_code == 201 and cap["motor"] == "lectura en curso" and cap["avisos"] == []
    # 2) Primera entrega (texto de la página) y 3) entrega final con las zonas afinadas
    url = f"/api/capturas/{cap['id']}/lectura"
    p1 = client.post(url, headers=sup, json={"texto": TEXTO_OCR, "confianza": 0.7}).json()
    assert p1["motor"] == ExtractorLocal.nombre and p1["campos"]["numero"]["valor"] == "340239"
    assert p1["campos"]["fecha_fac"]["valor"] is None
    zonas = {"fecha": {"texto": "03-07-2026", "confianza": 0.9}}
    p2 = client.post(url, headers=sup, json={"texto": TEXTO_OCR, "confianza": 0.7, "zonas": zonas}).json()
    assert p2["campos"]["fecha_fac"]["valor"] == "2026-07-03"
    assert client.get(f"/api/capturas/{cap['id']}", headers=sup).json()["campos"]["fecha_fac"]["valor"] == "2026-07-03"
    # Validación de la entrada y estado
    assert client.post(url, headers=sup, json={"texto": "x" * 20001}).status_code == 422
    client.post(f"/api/capturas/{cap['id']}/descartar", headers=sup)
    assert client.post(url, headers=sup, json={"texto": TEXTO_OCR}).status_code == 409


# ------------------------------------------------------- Cobranzas en pantalla
def test_reporte_cobranzas_en_pantalla_igual_al_excel(client: TestClient, sup: dict[str, str]) -> None:
    emp = crear_empresa(client, sup)
    for ced, nom in (("11111111", "Ana Chofer"), ("22222222", "Beto Chofer")):
        client.post("/api/admin/conductores", headers=sup, json={"cedula": ced, "nombre": nom, "empresa_id": emp})
    prev = upload_remesa(client, sup, libro_remesa()).json()
    client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=sup, json={})
    rem = client.get("/api/remesas", headers=sup).json()["items"]
    ids = [r["id"] for r in rem]
    params: dict[str, Any] = {"remesa_ids": ids}

    r = client.get("/api/reportes/cobranzas", headers=sup, params=params).json()
    assert r["total"] == 5 and r["totales"] == {
        "facturas": 5, "remesas": 2, "total_fact": "502.5000", "total_neto": "452.2500", "total_flete": "22.6125"}
    claves = [c["clave"] for c in r["columnas"]]
    assert claves[:3] == ["remesa_numero", "empresa", "chofer_nombre"] and claves[-2:] == ["estatus", "estatus_at"]
    fila = r["items"][0]
    assert fila["estatus"] == "Preparado" and fila["chofer_cedula"] and fila["cod_vendedor"]

    # Mismos datos que el Excel (todas las facturas y sus valores)
    xls = client.get("/api/reportes/cobranzas.xlsx", headers=sup, params=params)
    ws = load_workbook(io.BytesIO(xls.content)).active
    assert ws is not None
    numeros_xls = {c.value for row in ws.iter_rows() for c in row
                   if isinstance(c.value, str) and c.value.startswith("FAC-")}
    assert numeros_xls == {f["numero"] for f in r["items"]}

    # Orden, paginación y filtros (cliente, conductor) — el Excel acepta los mismos filtros
    orden = client.get("/api/reportes/cobranzas", headers=sup,
                       params={**params, "orden": "numero", "desc": True, "limit": 2, "offset": 1}).json()
    todos = sorted((f["numero"] for f in r["items"]), reverse=True)
    assert [f["numero"] for f in orden["items"]] == todos[1:3] and orden["total"] == 5
    cli = client.get("/api/reportes/cobranzas", headers=sup, params={**params, "cliente": "cliente 1"}).json()
    assert {f["cliente"] for f in cli["items"]} == {"CLIENTE 1, C.A.", "CLIENTE 10, C.A.", "CLIENTE 11, C.A."}
    beto = next(m["conductor_id"] for m in rem if m["chofer_nombre"] == "Beto Chofer")
    cond = client.get("/api/reportes/cobranzas", headers=sup, params={**params, "conductor_id": beto}).json()
    assert cond["total"] == 2 and {f["chofer_nombre"] for f in cond["items"]} == {"Beto Chofer"}
    xls_beto = client.get("/api/reportes/cobranzas.xlsx", headers=sup, params={**params, "conductor_id": beto})
    planos = [c.value for row in load_workbook(io.BytesIO(xls_beto.content)).active.iter_rows() for c in row]  # type: ignore[union-attr]
    assert "Beto Chofer" in planos and "Ana Chofer" not in planos
    vacio = client.get("/api/reportes/cobranzas", headers=sup, params={**params, "cliente": "NO EXISTE"}).json()
    assert vacio["total"] == 0 and vacio["items"] == []


def test_campo_extraido_dudoso() -> None:
    assert CampoExtraido(None, None, None).dudoso
    assert CampoExtraido("x", "x", 0.79).dudoso and not CampoExtraido("x", "x", 0.8).dudoso


# --------------------------------------------- lectura real de PaddleOCR (cajas con posición)
# Cajas que devolvió el lector del navegador con las fotos de prueba (texto, confianza, x, y, ancho, alto)
CAJAS_DIJONAS = [
    ("CORPORACIONDIJONAS,C.A", 0.87, 334, 107, 179, 20),
    ("CORPORACION", 0.96, 116, 120, 190, 28),
    ("CLIENTE:", 0.98, 41, 222, 76, 22),
    ("CONSTRUCCIONES Y MANTENIMIENTOS MATVAL,C.A", 0.88, 143, 221, 402, 26),
    ("FACTURA:", 0.99, 717, 235, 88, 28),
    ("COD.CLIENTE:J501010510", 0.9, 318, 240, 209, 28),
    ("002133", 0.99, 712, 255, 81, 29),
    ("Foma de pago:", 0.94, 724, 276, 149, 29),
    ("Fecha:09-07-2028", 0.92, 720, 330, 163, 35),
    ("VENDEDOR:DC-01017", 0.96, 42, 367, 181, 24),
    ("Nro.Orden de Pedido: PED-DiJDDD1904", 0.88, 331, 378, 319, 35),
    ("FACTURADOR:Diana Fenia", 0.9, 41, 391, 213, 23),
    ("$1.077.6000", 0.85, 825, 791, 94, 27),
    ("Subtotal", 0.98, 601, 793, 64, 23),
    ("754.557.07Bs", 0.89, 215, 813, 92, 22),
    ("0,00%", 0.81, 707, 814, 50, 23),
    ("Descuento", 0.95, 602, 815, 79, 22),
    ("$0.0000", 0.91, 854, 815, 64, 21),
    ("Base impanible", 0.9, 9, 816, 114, 29),
    ("Base Imponible", 0.94, 602, 836, 114, 25),
    ("$1.077.8000", 0.9, 828, 838, 92, 22),
    ("Importe Total", 0.86, 603, 882, 97, 22),
    ("$1.250.0180", 0.96, 817, 885, 106, 25),
    ("#Bultos4", 0.94, 375, 939, 99, 29),
]
CAJAS_USY = [
    ("IMPORTADORAUSY,C.A.", 0.93, 227, 106, 250, 37),
    ("COMERCIAL SUPER EXCELENTECA", 0.94, 62, 255, 320, 37),
    ("COD CLIENTE", 0.88, 263, 288, 127, 25),
    ("FAGTIIRA", 0.77, 716, 299, 83, 17),
    ("340239", 0.98, 707, 318, 86, 24),
    ("Fecha", 0.97, 719, 408, 68, 26),
    ("03-07-2028", 0.82, 798, 404, 97, 24),
    ("BO-07001", 0.93, 49, 418, 96, 28),
    ("DOR:", 0.93, 0, 422, 47, 22),
    ("Nro.Orden de Pedido", 0.9, 275, 449, 216, 27),
    ("PEO-1000000267517", 0.9, 497, 449, 167, 24),
    ("\u7696\u00b79900000", 0.66, 848, 921, 102, 29),  # «$ 990,0000» con el $ leído como ideograma
    ("Subtotal", 0.98, 583, 923, 74, 26),
    ("5.00%", 0.9, 704, 946, 54, 27),
    ("814.118.28B5", 0.77, 119, 947, 110, 24),
    ("Descuento", 0.99, 583, 948, 90, 24),
    ("$\u00b79405000", 0.86, 851, 972, 97, 29),
    ("BaseImponible", 0.93, 583, 973, 129, 28),
    ("$1.090.9800", 0.83, 831, 1032, 116, 25),
    ("#Bultos", 0.95, 310, 1104, 94, 29),
    ("3", 0.96, 407, 1107, 21, 25),
]


def _cajas(datos: list[tuple[str, float, int, int, int, int]]) -> list[CajaOCR]:
    return [CajaOCR(*c) for c in datos]


def _texto(datos: list[tuple[str, float, int, int, int, int]]) -> str:
    return "\n".join(c[0] for c in datos)


def test_parser_por_posicion_factura_dijonas() -> None:
    hallado = parsear_cajas(_cajas(CAJAS_DIJONAS), hoy=date(2026, 10, 19))
    valores = {k: v for k, (v, _) in hallado.items()}
    assert valores == {
        "numero_factura": "002133",  # debajo de «FACTURA:» (no el Nº de control 00-003106)
        "fecha": "09-07-2026",  # «2028»: el 6 leído como 8; con ese año la fecha sería futura
        "cod_vendedor": "DC-01017",
        "bultos": "4",
        "descuento_pct": "0,00%",
        "subtotal": "1077.6000",  # '$1.077.6000': el último punto es el decimal (4 decimales)
        "base_imponible": "1077.8000",  # la de $ (la de la izquierda está en Bs y se descarta)
        "pedido": "PED-DIJ0001904",  # «DDD» → «000»
        "cliente": "CONSTRUCCIONES Y MANTENIMIENTOS MATVAL, C.A",
        "empresa_emisora": "CORPORACION DIJONAS, C.A.",
    }
    assert hallado["fecha"][1] <= 0.6 and hallado["pedido"][1] <= 0.6  # reparados: a revisar
    assert hallado["numero_factura"][1] == 0.99

    lectura = LecturaLocal(_texto(CAJAS_DIJONAS), 0.9, cajas=tuple(_cajas(CAJAS_DIJONAS)))
    r = ExtractorLocal().extraer(b"", "image/jpeg", lectura)
    assert r.campos["numero"].valor == "002133" and not r.campos["numero"].dudoso
    assert r.campos["empresa_emisora"].valor == "CORPORACION DIJONAS" and r.campos["bultos"].valor == 4
    assert r.campos["total_fact"].valor == "1077.6000" and r.campos["porcentaje"].valor == "0.0000"
    # Subtotal y base no cuadran con 0 % de descuento: los dos quedan marcados para revisar
    assert r.campos["total_fact"].dudoso and r.campos["total_neto"].dudoso


def test_parser_por_posicion_factura_usy() -> None:
    hallado = parsear_cajas(_cajas(CAJAS_USY), hoy=date(2026, 10, 19))
    valores = {k: v for k, (v, _) in hallado.items()}
    assert valores["numero_factura"] == "340239"  # «FAGTIIRA» se reconoce como FACTURA
    assert valores["fecha"] == "03-07-2026" and valores["cod_vendedor"] == "BO-07001"
    assert valores["pedido"] == "PED-1000000267517" and valores["bultos"] == "3"
    assert valores["subtotal"] == "990.0000" and valores["base_imponible"] == "940.5000"  # coma perdida
    assert valores["descuento_pct"] == "5.00%" and valores["empresa_emisora"] == "IMPORTADORA USY, C.A."
    lectura = LecturaLocal(_texto(CAJAS_USY), 0.9, cajas=tuple(_cajas(CAJAS_USY)))
    r = ExtractorLocal().extraer(b"", "image/jpeg", lectura)
    assert r.campos["total_neto"].valor == "940.5000" and r.campos["porcentaje"].valor == "0.0500"
    assert r.campos["cliente"].valor == "COMERCIAL SUPER EXCELENTECA"  # sin etiqueta visible: por texto


def test_montos_y_fechas_mal_leidos() -> None:
    assert parse_monto("$1.077.6000") == Decimal("1077.6") and parse_monto("1.090.980") == Decimal("1090980")
    hoy = date(2026, 10, 19)
    assert parsear_cajas(_cajas([("Fecha:", 0.9, 0, 0, 60, 20), ("09-07-2026", 0.9, 70, 0, 90, 20)]), hoy)["fecha"] \
        == ("09-07-2026", 0.9)
    # Año futuro que no se explica por un dígito confundido: se deja (y la coherencia avisa)
    assert parsear_cajas(_cajas([("Fecha:09-07-2031", 0.9, 0, 0, 120, 20)]), hoy)["fecha"][0] == "09-07-2031"
    bs = _cajas([("Base Imponible", 0.9, 0, 0, 100, 20), ("754.557,07 Bs", 0.9, 110, 0, 100, 20)])
    assert "base_imponible" not in parsear_cajas(bs, hoy)


def test_lectura_con_cajas_y_cliente_ya_registrado(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    primera = _foto(client, sup)  # «COMERCIAL SUPER EXCELENTE, C.A» queda registrado al confirmar
    body = _confirmar(primera, conductor_id=e["cid"])
    r = client.post(f"/api/capturas/{primera['id']}/confirmar", headers=sup, json=body)
    assert r.status_code == 200, r.text

    cap = client.post("/api/capturas", headers=sup, files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")},
                      data={"ocr_en_curso": "true"}).json()
    cajas = [dict(zip(("texto", "confianza", "x", "y", "w", "h"), c, strict=True)) for c in CAJAS_USY]
    r = client.post(f"/api/capturas/{cap['id']}/lectura", headers=sup,
                    json={"texto": _texto(CAJAS_USY), "confianza": 0.9, "cajas": cajas})
    assert r.status_code == 200, r.text
    leida = r.json()
    assert leida["motor"] == ExtractorLocal.nombre
    valores = {k: v["valor"] for k, v in leida["campos"].items()}
    assert valores["numero"] == "340239" and valores["total_neto"] == "940.5000"
    assert valores["cliente"] == "COMERCIAL SUPER EXCELENTE, C.A"  # el registrado, no «EXCELENTECA»
    assert any("se propone el ya registrado" in a for a in leida["avisos"])
    assert any("ya" in a.lower() and "340239" in a for a in leida["avisos"])  # duplicado de la primera
    demasiadas = {"texto": "x", "cajas": cajas * 40}
    assert client.post(f"/api/capturas/{cap['id']}/lectura", headers=sup, json=demasiadas).status_code == 422


def test_misma_factura_en_la_remesa_da_409_y_no_error_interno(client: TestClient, sup: dict[str, str]) -> None:
    e = _escenario(client, sup)
    tb = _usuario(client, sup, "transp", "transportista", e["emp"])
    primera = _foto(client, tb)
    res = client.post(f"/api/capturas/{primera['id']}/confirmar", headers=tb,
                      json=_confirmar(primera, conductor_id=e["cid"])).json()
    # Mismo número, la empresa emisora leída de otra forma ('CORP USY'): es la misma factura
    client.app.state.extractor_facturas = ExtractorFalso({**CRUDO_EJEMPLO, "empresa_emisora": "CORP USY"})  # type: ignore[attr-defined]
    otra = _foto(client, tb)
    assert any("ya está en el sistema" in a for a in otra["avisos"])
    # Mismo número y una empresa sin relación: tampoco cabe en la misma remesa (antes: error interno 500)
    client.app.state.extractor_facturas = ExtractorFalso({**CRUDO_EJEMPLO, "empresa_emisora": "CORPORACION DIJONAS"})  # type: ignore[attr-defined]
    cap = _foto(client, tb)
    r = client.post(f"/api/capturas/{cap['id']}/confirmar", headers=tb,
                    json=_confirmar(cap, conductor_id=e["cid"], remesa_id=res["remesa_id"]))
    assert r.status_code == 409 and "ya tiene la factura 340239" in r.json()["detail"]
    assert client.get(f"/api/capturas/{cap['id']}", headers=tb).json()["estado"] == "pendiente"
    # En una remesa nueva sí se puede (es otra empresa emisora con el mismo número)
    r = client.post(f"/api/capturas/{cap['id']}/confirmar", headers=tb, json=_confirmar(cap, conductor_id=e["cid"]))
    assert r.status_code == 200, r.text
