"""Script de inicialización de la base: vacía todo y conserva los feriados y los usuarios Supervisor, Cobranzas
y Consultor."""

from __future__ import annotations

import gzip
import io
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import text

from tests.conftest import SUP_PASS, SUP_USER, crear_empresa, login
from tests.factories import bloque
from tests.test_bitacora_archivo import _sembrar
from tests.test_capturas import JPEG_MINIMO as JPEG
from tests.test_capturas import _foto, _usuario
from tracking import inicializar_bd as ini
from tracking.config import Settings


def _fac(n: int, cliente: str) -> dict[str, Any]:
    return {
        "numero": f"FAC-U{n:08d}",
        "pedido": f"PED-{n:013d}",
        "vendedor": "LA-13001",
        "fecha": datetime(2026, 10, 19),
        "bultos": "2",
        "total": 100.0,
        "pct": 0.1,
        "neto": 90.0,
        "f": 0.05,
        "flete": 4.5,
        "comentario": "DPP",
        "cliente": cliente,
    }


def _libro() -> bytes:
    wb = Workbook()
    ws0 = wb.active
    ws0.title = "PLACAS"
    ws0.append(["Cedula", "Nombre", "Placa", "Telefono", None, "PLACA ", "COLOR ", "SERIAL N,I,V", "CLASE"])
    ws0.append([15111222, "Luis Herrera", "A12BC3D", "0412 555 1122", None, "A12BC3D", "BLANCO", "S1", "CAMION"])
    ws0.append([18333444, "Carla Méndez", "B45EF6G", "0414 555 3344", None, "B45EF6G", "GRIS", "S2", "CAMION"])
    ws = wb.create_sheet("REMESA 2026,")
    fila = bloque(
        ws,
        7,
        cedula=15111222,
        nombre="Luis Herrera",
        placa="A12BC3D",
        telefono="0412 555 1122",
        destino="LARA",
        numero="2026-01001",
        empresa="IMPORTADORA DEMO",
        facturas=[_fac(1201, "FERRETERÍA EL MARTILLO, C.A."), _fac(1202, "MATERIALES LA ESQUINA, C.A.")],
    )
    bloque(
        ws,
        fila,
        cedula=18333444,
        nombre="Carla Méndez",
        placa="B45EF6G",
        telefono="0414 555 3344",
        destino="ORIENTE",
        numero="2026-01002",
        empresa="CORPORACION FERMETAL",
        facturas=[_fac(1301, "TODO OBRA, C.A.")],
    )
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def _vendedores() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Salesperson ID", "Salespersons Last Name", "Sales Territory", "Email"])
    ws.append(["LA-13001", "MARÍA GONZÁLEZ", "R. OCCIDENTAL", "maria@ejemplo.com"])
    ws.append(["DC-01005", "PEDRO RAMÍREZ", "R. CAPITAL", "pedro@ejemplo.com"])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def _sembrar_todo(client: TestClient, sup: dict[str, str]) -> None:
    """Datos de todas las tablas: empresas, vendedores, conductores, camiones, remesas, facturas, fotos, etc."""
    emp = crear_empresa(client, sup)
    crear_empresa(client, sup, "SERGAMA, C.A.")
    xlsx = "application/octet-stream"
    assert (
        client.post(
            "/api/admin/catalogos/vendedores", headers=sup, files={"archivo": ("v.xlsx", _vendedores(), xlsx)}
        ).status_code
        < 300
    )
    r = client.post(
        "/api/admin/catalogos/placas",
        headers=sup,
        data={"empresa_id": str(emp)},
        files={"archivo": ("R.xlsx", _libro(), xlsx)},
    )
    assert r.status_code < 300, r.text
    claves = {c["usuario"]: c["clave_temporal"] for c in r.json()["credenciales"]}
    _usuario(client, sup, "transporte1", "transportista", emp)
    _usuario(client, sup, "cobranzas1", "cobranzas", emp)  # se conserva, pero sin empresa
    _usuario(client, sup, "consultor1", "consultor", None)
    up = client.post("/api/importaciones", headers=sup, files={"archivo": ("R.xlsx", _libro(), xlsx)})
    assert up.status_code == 201, up.text
    assert client.post(f"/api/importaciones/{up.json()['id']}/confirmar", headers=sup, json={}).status_code == 200
    luis = login(client, "15111222", claves["15111222"])
    facts = client.get("/api/conductor/facturas", headers=luis).json()["facturas"]
    a = facts[0]["id"]
    ahora = datetime.now(timezone.utc).isoformat()
    ev = {"id": str(uuid.uuid4()), "factura_id": a, "estatus": "en_transito", "ocurrido_at": ahora}
    client.post("/api/conductor/eventos", headers=luis, json={"eventos": [ev]})
    fid = str(uuid.uuid4())
    r = client.put(
        f"/api/fotos/{fid}", headers=luis, data={"factura_id": a}, files={"archivo": ("f.jpg", JPEG, "image/jpeg")}
    )
    assert r.status_code < 300, r.text
    ev2 = {"id": str(uuid.uuid4()), "factura_id": a, "estatus": "entregada", "ocurrido_at": ahora, "foto_id": fid}
    assert client.post("/api/conductor/eventos", headers=luis, json={"eventos": [ev2]}).json()["resultados"][0][
        "aplicado"
    ]
    reg = {"dia": ahora[:10], "conexion_id": "abcd1234-0000", "metodo": "GET", "ruta": "/api/conductor/facturas",
           "solicitudes": 1, "bytes_subida": 10, "bytes_bajada": 20, "desde": ahora, "hasta": ahora}
    assert client.post("/api/conductor/consumo", headers=luis, json={"registros": [reg]}).status_code == 200
    _foto(client, sup)  # captura de factura por foto
    client.get("/api/publico/tracking", params={"q": "FAC-U00001201"})  # consulta pública
    _sembrar(client)  # bitácora vieja y su archivo (inmutable)
    assert (
        client.post("/api/admin/bitacora/archivar", headers=sup, json={"tipo": "mes", "periodo": "2025-09"}).status_code
        == 201
    )
    if not client.get("/api/feriados", headers=sup, params={"anio": 2026}).json():
        client.post("/api/feriados/nacionales", headers=sup, json={"anio": 2026})


def _proyecto(tmp_path: Path, settings: Settings) -> Path:
    """Carpeta tipo «tracking-logistico» con su .env apuntando a la base de la prueba."""
    p = tmp_path / "Tracking - Octubre" / "tracking-logistico"
    p.mkdir(parents=True)
    (p / ".env").write_text(
        f"DATABASE_URL={settings.database_url}\nSTORAGE_DIR={settings.storage_dir}\n", encoding="utf-8"
    )
    return p


def _conteos(settings: Settings) -> dict[str, int]:
    motor = ini.crear_motor(settings.database_url)
    with motor.connect() as conn:
        c = ini.conteos(conn)
    motor.dispose()
    return c


def _huellas(settings: Settings) -> dict[str, str]:
    motor = ini.crear_motor(settings.database_url)
    with motor.connect() as conn:
        h = ini.huellas_conservadas(conn)
    motor.dispose()
    return h


@pytest.fixture()
def sembrado(client: TestClient, sup: dict[str, str], settings: Settings, tmp_path: Path) -> Path:
    _sembrar_todo(client, sup)
    return _proyecto(tmp_path, settings)


def test_limpieza_vacia_lo_operativo_y_conserva_catalogos(
    client: TestClient, sup: dict[str, str], settings: Settings, sembrado: Path, tmp_path: Path
) -> None:
    antes = _conteos(settings)
    huellas = _huellas(settings)
    for t in (
        "remesas",
        "facturas",
        "fotos",
        "eventos_estatus",
        "capturas_factura",
        "importaciones",
        "conductores",
        "camiones",
        "auditoria",
        "bitacora",
        "bitacora_archivos",
        "consultas_publicas",
        "consumo_datos",
        "usuarios_conductor",
        "usuarios_transportista",
        "empresas",
        "vendedores",
        "feriados",
    ):
        assert antes[t] > 0, t  # la prueba cubre todas las tablas
    assert antes["usuarios_conservados"] == 3  # supervisor, cobranzas y consultor
    fotos_disco = [p for p in Path(settings.storage_dir).rglob("*") if p.is_file()]
    assert fotos_disco

    respaldos = tmp_path / "respaldos"
    assert ini.main(["--destino", "local", "--proyecto", str(sembrado), "--si", "--respaldos", str(respaldos)]) == 0

    despues = _conteos(settings)
    for t in ini.TABLAS_A_VACIAR:
        if t == "auditoria":
            assert despues[t] == 1  # solo el registro de la propia limpieza
        elif t in despues:
            assert despues[t] == 0, t
    assert despues["usuarios_conductor"] == 0 and despues["usuarios_transportista"] == 0
    assert despues["usuarios_borrar"] == 0
    for t in ("feriados", "usuarios_conservados", "usuarios_supervisor", "usuarios_cobranzas", "usuarios_consultor"):
        assert despues[t] == antes[t], t
    assert _huellas(settings) == huellas  # contenido idéntico, fila por fila

    # Respaldo completo antes de borrar y fotos movidas (no borradas)
    archivos = sorted(p.name for p in respaldos.iterdir())
    assert any(n.startswith("inicializacion_local_") and n.endswith(".log") for n in archivos)
    if settings.is_sqlite:
        copia = next(respaldos.glob("local_*.db"))
        con = sqlite3.connect(copia)
        assert con.execute("SELECT COUNT(*) FROM facturas").fetchone()[0] == antes["facturas"]
        con.close()
    else:
        copia = next(respaldos.glob("local_*.jsonl.gz"))
        with gzip.open(copia, "rt", encoding="utf-8") as gz:
            lineas = [json.loads(x) for x in gz]
        assert {"tabla": "facturas", "filas": antes["facturas"], "fin": True} in lineas
    movidas = [p for p in next(respaldos.glob("local_*_fotos")).rglob("*") if p.is_file()]
    assert len(movidas) == len(fotos_disco)
    assert not [p for p in Path(settings.storage_dir).rglob("*") if p.is_file()]

    # El sistema sigue funcionando: la oficina entra, los conductores ya no, y se puede cargar de nuevo
    assert client.post("/api/auth/login", json={"username": "15111222", "password": "qwerty"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "transporte1", "password": "clave-transporte1"})
    assert r.status_code == 401
    sup2 = login(client, SUP_USER, SUP_PASS)
    login(client, "cobranzas1", "clave-cobranzas1")
    login(client, "consultor1", "clave-consultor1")
    aud = client.get("/api/admin/auditoria", headers=sup2).json()
    assert aud[0]["accion"] == "base_inicializada" and aud[0]["detalle"]["borrados"]["facturas"] == antes["facturas"]
    assert client.get("/api/admin/empresas", headers=sup2).json() == []
    usuarios = client.get("/api/admin/usuarios", headers=sup2).json()
    usuarios = usuarios["items"] if isinstance(usuarios, dict) else usuarios
    assert sorted(u["rol"] for u in usuarios) == ["cobranzas", "consultor", "supervisor"]
    assert all(u.get("empresa_id") is None for u in usuarios)
    emp = crear_empresa(client, sup2)
    assert emp == 1  # contador reiniciado
    r = client.post(
        "/api/admin/catalogos/placas",
        headers=sup2,
        data={"empresa_id": str(emp)},
        files={"archivo": ("R.xlsx", _libro(), "application/octet-stream")},
    )
    assert r.status_code < 300, r.text
    up = client.post(
        "/api/importaciones", headers=sup2, files={"archivo": ("R.xlsx", _libro(), "application/octet-stream")}
    )
    assert up.status_code == 201 and up.json()["id"] == 1  # contadores reiniciados
    assert client.post(f"/api/importaciones/{up.json()['id']}/confirmar", headers=sup2, json={}).status_code == 200
    remesas = client.get("/api/remesas", headers=sup2).json()
    lista = remesas["items"] if isinstance(remesas, dict) else remesas
    assert sorted(x["id"] for x in lista) == [1, 2]

    # La bitácora archivada sigue siendo inmutable (los disparadores quedaron repuestos)
    motor = ini.crear_motor(settings.database_url)
    with motor.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO bitacora_archivos (tipo, periodo, desde, hasta, filas, bytes, sha256, formato, "
                "contenido, created_at) VALUES ('mes', '2020-01', :d, :d, 0, 0, 'x', 'jsonl.gz', :c, :d)"
            ),
            {"d": datetime(2020, 1, 1, tzinfo=timezone.utc), "c": b"x"},
        )
        conn.commit()
        with pytest.raises(Exception, match="inmutable"):
            conn.execute(text("DELETE FROM bitacora_archivos"))
        conn.rollback()
    motor.dispose()


def test_simulacion_no_cambia_nada(settings: Settings, sembrado: Path, tmp_path: Path) -> None:
    antes, huellas = _conteos(settings), _huellas(settings)
    assert (
        ini.main(["--destino", "local", "--proyecto", str(sembrado), "--simular", "--respaldos", str(tmp_path / "r")])
        == 0
    )
    assert _conteos(settings) == antes and _huellas(settings) == huellas
    assert not list((tmp_path / "r").glob("local_*"))  # la simulación no respalda ni mueve fotos


def test_validacion_fallida_deshace_todo(
    settings: Settings, sembrado: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    antes = _conteos(settings)
    monkeypatch.setattr(ini, "validar", lambda *a: ["Vendedores: prueba de error"])
    assert (
        ini.main(["--destino", "local", "--proyecto", str(sembrado), "--si", "--respaldos", str(tmp_path / "r")]) == 2
    )
    assert _conteos(settings) == antes
    registro = next((tmp_path / "r").glob("inicializacion_local_*.log")).read_text(encoding="utf-8")
    assert "VALIDACIÓN FALLIDA" in registro and "prueba de error" in registro


def test_sin_confirmacion_no_borra(
    settings: Settings, sembrado: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    antes = _conteos(settings)
    monkeypatch.setattr("builtins.input", lambda: "no")
    assert ini.main(["--destino", "local", "--proyecto", str(sembrado), "--respaldos", str(tmp_path / "r")]) == 1
    assert _conteos(settings) == antes


def test_urls_de_destino(tmp_path: Path) -> None:
    p = tmp_path / "proy"
    p.mkdir()
    (p / ".env").write_text("DATABASE_URL=sqlite:///./data/tracking.db\n", encoding="utf-8")
    assert ini.url_de_destino("local", p) == "sqlite:///" + str((p / "data" / "tracking.db").resolve())
    (p / ".env.local").write_text(
        'DATABASE_URL="postgres://u:secreta@ep-x.neon.tech/neondb?sslmode=require"\n'
        'DATABASE_URL_UNPOOLED="postgresql://u:secreta@ep-y.neon.tech/neondb"\n',
        encoding="utf-8",
    )
    url = ini.url_de_destino("produccion", p)
    assert "ep-y" in url  # conexión directa (sin el pooler)
    assert ini.describir(url) == "PostgreSQL ep-y.neon.tech/neondb" and "secreta" not in ini.describir(url)
    (p / ".env.local").unlink()
    with pytest.raises(SystemExit, match=r"env\.local"):
        ini.url_de_destino("produccion", p)
