"""Modo Vercel: filas extraídas en el navegador (gzip), fotos en la base y assetlinks del APK."""

from __future__ import annotations

import gzip
import io
import json
import uuid
from typing import Any

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.conftest import SUP_PASS, SUP_USER, login
from tests.factories import JPEG_MINIMO, libro_remesa
from tracking.config import Settings
from tracking.main import create_app


def _filas(data: bytes, hoja: str, max_col: int = 30) -> list[list[Any]]:
    """Simula lo que hace xlsx-lite.js: valores por fila, fechas como serial de Excel."""
    from datetime import datetime

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for row in wb[hoja].iter_rows(max_col=max_col, values_only=True):
        vals = [((v - datetime(1899, 12, 30)).days if isinstance(v, datetime) else v) for v in row]
        while vals and vals[-1] is None:
            vals.pop()
        out.append(vals)
    return out


def test_modo_vercel(settings: Settings) -> None:
    settings.storage_backend = "db"
    settings.android_package_name = "com.egci.tracking"
    settings.android_sha256_fingerprints = "aa:bb:cc"
    with TestClient(create_app(settings)) as client:
        sup = login(client, SUP_USER, SUP_PASS)
        data = libro_remesa()
        # Catálogo PLACAS por filas comprimidas (el Supervisor indica la empresa de los choferes)
        empresa = client.post("/api/admin/empresas", headers=sup, json={"nombre": "TRANSPORTE VERCEL"}).json()["id"]
        cuerpo = gzip.compress(json.dumps({"hoja": "PLACAS", "filas": _filas(data, "PLACAS", 12),
                                           "empresa_id": empresa}).encode())
        r = client.post("/api/admin/catalogos/placas/filas", headers={**sup, "Content-Type": "application/json"},
                        content=cuerpo)
        assert r.status_code == 200, r.text
        creds = {c["usuario"]: c["clave_temporal"] for c in r.json()["credenciales"]}

        filas = _filas(data, "REMESA 2026,")
        cuerpo = gzip.compress(json.dumps({"archivo": "REMESA.xlsx", "hoja": "REMESA 2026,",
                                           "hojas": ["PLACAS", "REMESA 2026,"], "filas": filas}).encode())
        r = client.post("/api/importaciones/filas", headers={**sup, "Content-Type": "application/json"}, content=cuerpo)
        assert r.status_code == 201, r.text
        prev = r.json()
        assert prev["resumen"]["totales"]["facturas_nuevas"] == 6
        r = client.post(f"/api/importaciones/{prev['id']}/confirmar", headers=sup, json={})
        assert r.json()["facturas_creadas"] == 5

        # Datos inválidos / bomba gzip
        assert client.post("/api/importaciones/filas", headers=sup, content=b"{no json").status_code == 422
        bomba = gzip.compress(b"[" + b"0," * 70_000_000 + b"0]")
        assert client.post("/api/importaciones/filas", headers=sup, content=bomba).status_code == 413

        # Foto guardada en la base de datos (sin disco)
        ana = login(client, "11111111", creds["11111111"])
        fid = client.get("/api/conductor/facturas", headers=ana).json()["facturas"][0]["id"]
        foto = str(uuid.uuid4())
        r = client.put(f"/api/fotos/{foto}", headers=ana, data={"factura_id": fid},
                       files={"archivo": ("f.jpg", JPEG_MINIMO, "image/jpeg")})
        assert r.status_code == 201
        assert not any(settings.storage_dir.rglob("*.jpg"))
        r = client.get(f"/api/fotos/{foto}", headers=sup)
        assert r.status_code == 200 and r.content == JPEG_MINIMO

        al = client.get("/.well-known/assetlinks.json").json()
        assert al[0]["target"]["package_name"] == "com.egci.tracking"
        assert al[0]["target"]["sha256_cert_fingerprints"] == ["AA:BB:CC"]
    settings.android_assetlinks_json = '[{"relation": ["x"], "target": {"package_name": "p"}}]'
    with TestClient(create_app(settings)) as client:
        assert client.get("/.well-known/assetlinks.json").json()[0]["target"]["package_name"] == "p"


def test_config_vercel(monkeypatch: Any) -> None:
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("POSTGRES_URL", "postgres://u:p@host/db")
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    s = Settings(_env_file=None)
    assert s.database_url.startswith("postgresql://") and s.storage_backend == "db" and s.db_serverless
    assert s.max_photo_mb <= 4
    monkeypatch.delenv("SECRET_KEY")
    try:
        Settings(_env_file=None)
    except ValueError as exc:
        assert "SECRET_KEY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("debía exigir SECRET_KEY en Vercel")
