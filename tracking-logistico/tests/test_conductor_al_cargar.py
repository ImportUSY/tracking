"""Alta del conductor desde «Cargar REMESA» cuando no está registrado (foto de factura o Excel)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import crear_empresa, upload_remesa
from tests.factories import libro_remesa
from tests.test_capturas import ExtractorFalso, _confirmar, _foto, _usuario


def test_transportista_crea_chofer_del_excel_y_la_remesa_ya_no_se_excluye(
    client: TestClient, sup: dict[str, str]
) -> None:
    emp = crear_empresa(client, sup, "TRANSPORTE YANEIBER")
    for ced, nom in (("11111111", "Ana Chofer"), ("22222222", "Beto Chofer")):
        client.post("/api/admin/conductores", headers=sup, json={"cedula": ced, "nombre": nom, "empresa_id": emp})
    tb = _usuario(client, sup, "transp", "transportista", emp)
    prev = upload_remesa(client, tb, libro_remesa()).json()
    chofer = next(c for c in prev["resumen"]["choferes"] if not c["registrado"])
    assert (chofer["cedula"], chofer["nombre"], chofer["telefono"]) == ("33333333", "Carlos Nuevo", "04243333333")

    # El Transportista lo crea con los datos del archivo (sin indicar empresa: queda en la suya)
    r = client.post("/api/admin/conductores", headers=tb,
                    json={"cedula": chofer["cedula"], "nombre": chofer["nombre"], "placa": chofer["placa"]})
    assert r.status_code == 201, r.text
    assert r.json()["credencial"]["usuario"] == "33333333" and r.json()["credencial"]["clave_temporal"]

    # Al confirmar (sin «registrar automáticamente») su remesa ya entra
    out = client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=tb,
                      json={"remesas": ["2025-00001", "2025-00003"]}).json()
    assert out["remesas_excluidas"] == [] and out["remesas_creadas"] == 2 and out["choferes_registrados"] == []
    rem = next(x for x in client.get("/api/remesas", headers=tb).json()["items"] if x["numero"] == "2025-00003")
    assert rem["chofer_nombre"] == "Carlos Nuevo"


def test_transportista_crea_conductor_al_registrar_la_foto(client: TestClient, sup: dict[str, str]) -> None:
    emp = crear_empresa(client, sup, "TRANSPORTE YANEIBER")
    tb = _usuario(client, sup, "transp", "transportista", emp)
    client.app.state.extractor_facturas = ExtractorFalso()  # type: ignore[attr-defined]
    cap = _foto(client, tb)
    assert client.get("/api/admin/conductores", headers=tb).json() == []  # no hay ninguno: lo crea ahí mismo
    nuevo = client.post("/api/admin/conductores", headers=tb,
                        json={"cedula": "V-12.345.678", "nombre": "Pedro Pérez", "placa": "AB123CD"}).json()
    cid = nuevo["conductor"]["id"]
    assert nuevo["conductor"]["empresa_id"] == emp
    r = client.post(f"/api/capturas/{cap['id']}/confirmar", headers=tb, json=_confirmar(cap, conductor_id=cid))
    assert r.status_code == 200 and r.json()["conductor"] == "Pedro Pérez"
    # Cobranzas (sin permiso de crear conductores) no puede
    cob = _usuario(client, sup, "cobra", "cobranzas", None)
    r = client.post("/api/admin/conductores", headers=cob, json={"cedula": "1234567", "nombre": "X Y"})
    assert r.status_code == 403
