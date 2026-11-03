"""Integración opcional con Claude (API de Anthropic).

Principio: la IA *asiste* pero nunca es fuente de datos.
* `mapear_encabezados`: sugiere a qué campo corresponde un encabezado desconocido (columnas movidas o
  renombradas). El resultado se valida contra la lista cerrada de campos y se muestra en el resumen.
* `revisar_importacion`: redacta en lenguaje claro los problemas que el parser ya detectó.
* `redactar_intro_correo`: un párrafo de saludo; la tabla de facturas la arma siempre el programa.

Si la IA no está configurada o falla, todo sigue funcionando sin ella.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from tracking.config import Settings
from tracking.services.remesa_parser import CAMPOS, NOMBRES_CAMPO

log = logging.getLogger(__name__)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 409, 429, 500, 502, 503, 504, 529)
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


class ClaudeClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.anthropic_base_url,
            timeout=settings.ai_timeout_seconds,
            transport=transport,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    @property
    def enabled(self) -> bool:
        return self.settings.ai_available

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1, max=20),
        reraise=True,
    )
    def _complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        resp = self._client.post(
            "/v1/messages",
            json={
                "model": self.settings.anthropic_model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts).strip()

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=8),
        reraise=True,
    )
    def herramienta(
        self,
        *,
        system: str,
        contenido: list[dict[str, Any]],
        tool: dict[str, Any],
        modelo: str | None = None,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Llamada con salida estructurada: obliga al modelo a responder invocando ``tool``.

        Devuelve el ``input`` de la herramienta (ya es JSON validado contra ``input_schema`` por la API).
        Admite contenido multimodal (bloques ``image`` en base64). No comprueba ``enabled``: lo decide
        quien la usa (p. ej. el OCR tiene su propio interruptor).
        """
        resp = self._client.post(
            "/v1/messages",
            json={
                "model": modelo or self.settings.anthropic_model,
                "max_tokens": max_tokens,
                "system": system,
                "tools": [tool],
                "tool_choice": {"type": "tool", "name": tool["name"]},
                "messages": [{"role": "user", "content": contenido}],
            },
        )
        resp.raise_for_status()
        for bloque in resp.json().get("content", []):
            if bloque.get("type") == "tool_use" and bloque.get("name") == tool["name"]:
                entrada = bloque.get("input")
                if isinstance(entrada, dict):
                    return entrada
        raise ValueError("La respuesta del modelo no trae la salida estructurada esperada")

    def safe_complete(self, system: str, prompt: str, max_tokens: int = 1024) -> str | None:
        if not self.enabled:
            return None
        try:
            return self._complete(system, prompt, max_tokens)
        except Exception:
            log.exception("ia_fallo")
            return None

    # ------------------------------------------------------------------ tareas
    def mapear_encabezados(self, desconocidos: list[str], faltantes: list[str]) -> dict[str, str]:
        if not desconocidos or not faltantes:
            return {}
        campos = {c: NOMBRES_CAMPO[c] for c in CAMPOS}
        system = (
            "Eres un asistente que asocia encabezados de columnas de una hoja Excel de remesas de transporte "
            "con campos conocidos. Responde SOLO con un objeto JSON {\"encabezado\": \"campo\"}. "
            "Usa únicamente los campos permitidos. Si no estás seguro, no incluyas el encabezado."
        )
        prompt = (
            f"Campos permitidos (clave: nombre habitual): {json.dumps(campos, ensure_ascii=False)}\n"
            f"Campos que faltan en la hoja: {json.dumps(faltantes, ensure_ascii=False)}\n"
            f"Encabezados no reconocidos: {json.dumps(desconocidos, ensure_ascii=False)}"
        )
        raw = self.safe_complete(system, prompt, max_tokens=400)
        if not raw:
            return {}
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
        return {
            str(k): str(v)
            for k, v in parsed.items()
            if str(k) in desconocidos and str(v) in CAMPOS and str(v) in faltantes
        }

    def revisar_importacion(self, resumen: dict[str, Any]) -> str | None:
        compacto = {
            "totales": resumen.get("totales"),
            "choferes_no_registrados": [c for c in resumen.get("choferes", []) if not c.get("registrado")],
            "avisos": resumen.get("avisos", [])[:60],
        }
        system = (
            "Eres analista de logística. Recibes el resumen que un programa generó al leer una hoja REMESA. "
            "Explica en español, en máximo 8 viñetas cortas, qué debe revisar el Transportista antes de "
            "confirmar. No inventes datos ni cifras que no estén en el resumen. No prometas plazos."
        )
        return self.safe_complete(system, json.dumps(compacto, ensure_ascii=False, default=str), 700)

    def redactar_intro_correo(self, vendedor: str, modo: str, cantidad: int) -> str | None:
        system = (
            "Redactas un saludo breve (2 frases) en español para un correo a un vendedor con el estatus de "
            "sus facturas. No agregues datos, montos, fechas ni promesas de entrega."
        )
        prompt = (
            f"Vendedor: {vendedor}. Tipo de aviso: "
            f"{'facturas por entregar' if modo == 'en_curso' else 'facturas con estatus final'}. "
            f"Cantidad de facturas: {cantidad}."
        )
        return self.safe_complete(system, prompt, max_tokens=200)

    def close(self) -> None:
        self._client.close()
