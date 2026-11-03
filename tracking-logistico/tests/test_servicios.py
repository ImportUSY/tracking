from __future__ import annotations

import json
import smtplib
from typing import Any

import httpx
import pytest

from tracking.config import Settings
from tracking.services.ai import ClaudeClient
from tracking.services.notifications import Correo, EmailSender, _smtp_transitorio
from tracking.services.photos import FotoInvalida, PhotoStorage, detectar_tipo
from tracking.services.status import MAX_CLOCK_SKEW
from tracking.services.tracking_codes import new_code


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test", "secret_key": "s" * 40}
    base.update(kw)
    return Settings(**base)


def test_ia_desactivada_no_llama() -> None:
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - no debe ejecutarse
        raise AssertionError("no debía llamar a la API")

    c = ClaudeClient(_settings(), transport=httpx.MockTransport(handler))
    assert c.mapear_encabezados(["X"], ["pedido"]) == {}
    assert c.revisar_importacion({"totales": {}}) is None


def test_ia_mapeo_filtra_campos_no_permitidos() -> None:
    llamadas: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        llamadas.append(json.loads(req.content))
        assert req.headers["x-api-key"] == "k"
        texto = '{"Nro. Ped": "pedido", "Otra": "monto_inventado", "Vend": "cod_vendedor"}'
        return httpx.Response(200, json={"content": [{"type": "text", "text": texto}]})

    c = ClaudeClient(_settings(ai_review_enabled=True, anthropic_api_key="k"),
                     transport=httpx.MockTransport(handler))
    # "Vend" no estaba entre los faltantes -> se descarta; "monto_inventado" no es un campo -> se descarta
    assert c.mapear_encabezados(["Nro. Ped", "Otra", "Vend"], ["pedido"]) == {"Nro. Ped": "pedido"}
    assert llamadas[0]["model"]


def test_ia_reintenta_y_luego_degrada() -> None:
    intentos = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        intentos["n"] += 1
        return httpx.Response(529, json={"error": "overloaded"})

    c = ClaudeClient(_settings(ai_review_enabled=True, anthropic_api_key="k"),
                     transport=httpx.MockTransport(handler))
    c._complete.retry.wait = lambda *_: 0  # type: ignore[attr-defined]
    assert c.revisar_importacion({"totales": {}}) is None  # no rompe el flujo
    assert intentos["n"] == 4


def test_smtp_clasificacion_errores() -> None:
    assert _smtp_transitorio(smtplib.SMTPResponseException(451, b"try later"))
    assert not _smtp_transitorio(smtplib.SMTPAuthenticationError(535, b"bad auth"))
    assert _smtp_transitorio(ConnectionRefusedError())
    assert not _smtp_transitorio(smtplib.SMTPRecipientsRefused({}))


def test_correo_desactivado() -> None:
    from tracking.services.notifications import CorreoNoConfigurado

    with pytest.raises(CorreoNoConfigurado):
        EmailSender(_settings()).enviar(Correo(["a@b.c"], "x", "y", "<p>y</p>"))


def test_fotos(tmp_path: Any) -> None:
    st = PhotoStorage(tmp_path)
    rel, ctype, sha = st.guardar("0f8fad5b-d9cb-469f-a165-70867728950e", 7, b"\x89PNG\r\n\x1a\n" + b"0" * 10)
    assert ctype == "image/png" and len(sha) == 64 and st.ruta_absoluta(rel).exists()
    with pytest.raises(FotoInvalida):
        st.guardar("../../etc/passwd", 7, b"\xff\xd8\xff")
    with pytest.raises(FotoInvalida):
        detectar_tipo(b"GIF89a")
    with pytest.raises(FotoInvalida):
        st.ruta_absoluta("../../fuera.jpg")


def test_tracking_codes() -> None:
    codes = {new_code() for _ in range(2000)}
    assert len(codes) == 2000
    assert all(c.startswith("TR-") and len(c) == 11 and not set(c[3:]) & set("ILOU") for c in codes)
    assert MAX_CLOCK_SKEW.total_seconds() == 300
