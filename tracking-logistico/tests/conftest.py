from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracking.config import Settings
from tracking.main import create_app

SUP_USER = "supervisor"
SUP_PASS = "Clave-Segura-1"


def _database_url(tmp_path: Path) -> str:
    """SQLite por defecto; TEST_DATABASE_URL=postgresql://... para probar contra PostgreSQL."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return f"sqlite:///{tmp_path / 'test.db'}"
    from sqlalchemy import create_engine, text

    eng = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    return url


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url=_database_url(tmp_path),
        storage_dir=tmp_path / "fotos",
        secret_key="x" * 48,
        bootstrap_admin_user=SUP_USER,
        bootstrap_admin_password=SUP_PASS,
        log_json=False,
        log_level="WARNING",
        public_rate_limit_per_minute=1000,
        login_max_attempts=50,
        db_connect_retries=1,
    )


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def login(client: TestClient, user: str, password: str) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def sup(client: TestClient) -> dict[str, str]:
    return login(client, SUP_USER, SUP_PASS)


def crear_empresa(client: TestClient, sup: dict[str, str], nombre: str = "TRANSPORTES DEMO", **extra: Any) -> int:
    r = client.post("/api/admin/empresas", headers=sup, json={"nombre": nombre, **extra})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


@pytest.fixture()
def empresa(client: TestClient, sup: dict[str, str]) -> int:
    """Empresa de transporte por defecto de los tests (inquilino de transportistas y conductores)."""
    return crear_empresa(client, sup)


def upload_remesa(client: TestClient, headers: dict[str, str], data: bytes, name: str = "REMESA.xlsx",
                  **form: Any) -> Any:
    files = {"archivo": (name, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    return client.post("/api/importaciones", headers=headers, files=files, data=form)
