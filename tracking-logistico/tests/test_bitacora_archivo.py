"""Archivo de la bitácora por mes o por año: sale de la tabla activa, queda comprimido, íntegro e inmutable."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import crear_empresa, login
from tests.test_capturas import _usuario
from tracking.cli import main as cli
from tracking.models import Bitacora

# Septiembre 2025 (3 registros, uno el 30-sep 23:30 de Caracas = 1-oct 03:30 UTC) y enero 2026 (1)
VIEJOS = [
    datetime(2025, 9, 1, 12, 0, tzinfo=timezone.utc),
    datetime(2025, 9, 15, 12, 0, tzinfo=timezone.utc),
    datetime(2025, 10, 1, 3, 30, tzinfo=timezone.utc),
    datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc),
]


def _sembrar(client: TestClient) -> None:
    with client.app.state.db.session() as db:  # type: ignore[attr-defined]
        for i, momento in enumerate(VIEJOS):
            db.add(
                Bitacora(
                    ocurrido_at=momento,
                    usuario_id=1,
                    usuario="supervisor",
                    usuario_nombre="Supervisor",
                    rol="supervisor",
                    accion="CONSULTAR",
                    modulo="remesas",
                    metodo="GET",
                    ruta=f"/api/remesas/{i}",
                    estado_http=200,
                    exito=True,
                    detalle={"n": i},
                )
            )
        db.commit()


def _activos(client: TestClient, desde: datetime, hasta: datetime) -> int:
    with client.app.state.db.session() as db:  # type: ignore[attr-defined]
        q = select(func.count(Bitacora.id)).where(Bitacora.ocurrido_at >= desde, Bitacora.ocurrido_at < hasta)
        return int(db.scalar(q) or 0)


def test_archivar_un_mes(client: TestClient, sup: dict[str, str]) -> None:
    _sembrar(client)
    est = client.get("/api/admin/bitacora/archivos", headers=sup).json()
    pend = {p["periodo"]: p["filas"] for p in est["pendientes"]}
    assert pend["2025-09"] == 3 and pend["2026-01"] == 1 and pend["2025"] == 3  # hora local (Caracas)
    assert not any(p["periodo"] == "2026" for p in est["pendientes"])  # el año en curso no está cerrado

    r = client.post("/api/admin/bitacora/archivar", headers=sup, json={"tipo": "mes", "periodo": "2025-09"})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["filas"] == 3 and a["periodo"] == "2025-09" and len(a["sha256"]) == 64
    assert _activos(client, datetime(2025, 9, 1, tzinfo=timezone.utc), datetime(2025, 10, 2, tzinfo=timezone.utc)) == 0

    # Original: JSON Lines + gzip con la firma; CSV para Excel con hora local
    orig = client.get(f"/api/admin/bitacora/archivos/{a['id']}/descargar", headers=sup, params={"formato": "jsonl.gz"})
    assert hashlib.sha256(orig.content).hexdigest() == a["sha256"] == orig.headers["x-content-sha256"]
    filas = [json.loads(x) for x in gzip.decompress(orig.content).decode().splitlines()]
    assert [f["ruta"] for f in filas] == ["/api/remesas/0", "/api/remesas/1", "/api/remesas/2"]
    assert filas[0]["detalle"] == {"n": 0}
    c = client.get(f"/api/admin/bitacora/archivos/{a['id']}/descargar", headers=sup)
    assert c.headers["content-type"].startswith("text/csv")
    tabla = list(csv.reader(io.StringIO(c.content.decode("utf-8-sig")), delimiter=";"))
    assert tabla[0][:3] == ["id", "Fecha y hora", "usuario_id"] and len(tabla) == 4
    assert tabla[3][1] == "30/09/2025 23:30:00"  # hora de Caracas

    # La consulta avisa que el período está archivado; reglas de duplicado y de año
    r = client.get("/api/admin/bitacora", headers=sup, params={"desde": "2025-09-01", "hasta": "2025-09-30"}).json()
    assert r["total"] == 0 and r["archivados"] == ["2025-09"]
    r = client.post("/api/admin/bitacora/archivar", headers=sup, json={"tipo": "mes", "periodo": "2025-09"})
    assert r.status_code == 409 and "ya está archivado" in r.json()["detail"]
    r = client.post("/api/admin/bitacora/archivar", headers=sup, json={"tipo": "anio", "periodo": "2025"})
    assert r.status_code == 409 and "meses" in r.json()["detail"]
    r = client.post("/api/admin/bitacora/archivar", headers=sup, json={"tipo": "mes", "periodo": "2025-08"})
    assert r.status_code == 422 and "No hay registros" in r.json()["detail"]
    hoy = datetime.now(timezone.utc)
    r = client.post("/api/admin/bitacora/archivar", headers=sup, json={"tipo": "mes", "periodo": f"{hoy:%Y-%m}"})
    assert r.status_code == 422 and "no ha terminado" in r.json()["detail"]
    r = client.post("/api/admin/bitacora/archivar", headers=sup, json={"tipo": "mes", "periodo": "2025/09"})
    assert r.status_code == 422
    auditoria = client.get("/api/admin/auditoria", headers=sup).json()
    assert auditoria[0]["accion"] == "bitacora_archivada" and auditoria[0]["detalle"]["filas"] == 3


def test_archivar_un_anio_e_inmutabilidad(client: TestClient, sup: dict[str, str]) -> None:
    _sembrar(client)
    r = client.post("/api/admin/bitacora/archivar", headers=sup, json={"tipo": "anio", "periodo": "2025"})
    assert r.status_code == 201 and r.json()["filas"] == 3
    r = client.post("/api/admin/bitacora/archivar", headers=sup, json={"tipo": "mes", "periodo": "2025-09"})
    assert r.status_code == 409 and "año 2025 completo" in r.json()["detail"]
    with client.app.state.db.session() as db:  # type: ignore[attr-defined]
        # Fuera de un archivado, la bitácora y sus archivos siguen sin admitir cambios ni borrados
        for sql in (
            "DELETE FROM bitacora",
            "UPDATE bitacora SET accion = 'X'",
            "DELETE FROM bitacora_archivos",
            "UPDATE bitacora_archivos SET filas = 0",
        ):
            with pytest.raises(DBAPIError):
                db.execute(text(sql))
            db.rollback()
        assert db.scalar(select(func.count(Bitacora.id))) > 0


def test_permisos_del_archivo(client: TestClient, sup: dict[str, str]) -> None:
    _sembrar(client)
    tb = _usuario(client, sup, "transp", "transportista", crear_empresa(client, sup))
    assert client.get("/api/admin/bitacora/archivos", headers=tb).status_code == 403
    assert (
        client.post("/api/admin/bitacora/archivar", headers=tb, json={"tipo": "mes", "periodo": "2025-09"}).status_code
        == 403
    )
    client.post(
        "/api/admin/usuarios",
        headers=sup,
        json={"username": "consulta1", "nombre": "Consultora", "rol": "consultor", "clave": "Consulta-1"},
    )
    co = login(client, "consulta1", "Consulta-1")
    assert client.get("/api/admin/bitacora/archivos", headers=co).status_code == 200  # ve y descarga
    r = client.post("/api/admin/bitacora/archivar", headers=co, json={"tipo": "mes", "periodo": "2025-09"})
    assert r.status_code == 403 and "solo puede consultar" in r.json()["detail"]


def test_cli_archiva_automaticamente(
    client: TestClient,
    sup: dict[str, str],
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _sembrar(client)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    from tracking.config import get_settings

    get_settings.cache_clear()
    try:
        assert cli(["archivar-bitacora", "--automatico", "1"]) == 0  # deja el último mes cerrado
    finally:
        get_settings.cache_clear()
    salida = capsys.readouterr().out
    assert "Archivado 2025-09: 3 registros" in salida and "2026-01" not in salida
    est = client.get("/api/admin/bitacora/archivos", headers=sup).json()
    assert [a["periodo"] for a in est["archivos"]] == ["2025-09"]
