"""Los archivos de la PWA se revalidan por contenido, no por fecha + tamaño (en Vercel la fecha es fija)."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from tracking.main import EstaticosWeb, _etag_contenido

FECHA_FIJA = 1540000000  # la que Vercel pone a todos los archivos del despliegue


def _cliente(carpeta: Path) -> TestClient:
    return TestClient(Starlette(routes=[Mount("/static", EstaticosWeb(directory=carpeta))]))


def test_version_nueva_del_mismo_tamano_no_queda_en_cache(tmp_path: Path) -> None:
    js = tmp_path / "panel.js"
    js.write_text('const VERSION_PANEL = "1.26.0";')
    os.utime(js, (FECHA_FIJA, FECHA_FIJA))
    vieja = _cliente(tmp_path).get("/static/panel.js")
    assert vieja.status_code == 200 and "last-modified" not in vieja.headers

    _etag_contenido.cache_clear()  # despliegue nuevo = proceso nuevo
    js.write_text('const VERSION_PANEL = "1.26.1";')
    os.utime(js, (FECHA_FIJA, FECHA_FIJA))
    nueva = _cliente(tmp_path).get("/static/panel.js", headers={"If-None-Match": vieja.headers["etag"]})
    assert nueva.status_code == 200 and "1.26.1" in nueva.text
    assert nueva.headers["etag"] != vieja.headers["etag"]

    igual = _cliente(tmp_path).get("/static/panel.js", headers={"If-None-Match": nueva.headers["etag"]})
    assert igual.status_code == 304


def test_paginas_con_etag_por_contenido(client: TestClient) -> None:
    for ruta in ("/", "/panel", "/conductor", "/sw.js"):
        r = client.get(ruta)
        assert r.status_code == 200 and r.headers["etag"] and "last-modified" not in r.headers
