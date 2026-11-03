"""Bitácora de actividad: quién hizo qué, cuándo, desde dónde y con qué datos.

* Se escribe sola desde el middleware HTTP (``registrar_peticion``) para cada petición a ``/api/`` de un
  usuario identificado: consultas (GET), altas (POST), cambios (PATCH/PUT y acciones POST) y bajas
  (DELETE), incluidas las rechazadas (``exito=False`` con su código HTTP).
* Inicio y cierre de sesión (``LOGIN`` / ``LOGOUT`` / ``LOGIN_FALLIDO``) y las vistas del panel que el
  usuario abre (``VISTA``, informadas por la interfaz) completan el recorrido.
* El detalle (payload) se sanea: nunca guarda claves, tokens ni hashes; se acota de tamaño y los
  archivos se resumen (nombre/tipo/tamaño, nunca su contenido).
* Inmutable: no hay endpoints para editar ni borrar y la base de datos lo impide con un trigger.
* Escribir la bitácora nunca rompe la petición del usuario: un fallo aquí solo se anota en el log.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.orm import Session

from tracking.enums import AccionBitacora
from tracking.models import Bitacora, Usuario

log = logging.getLogger(__name__)

PROHIBIDAS: Final = ("clave", "password", "pass", "token", "secret", "hash", "authorization")
MAX_DETALLE: Final = 4000  # caracteres del JSON guardado
MAX_TEXTO: Final = 300
MAX_LISTA: Final = 25
# Lecturas de soporte que no son actividad del usuario (se piden solas al abrir cualquier pantalla)
RUTAS_OMITIDAS: Final = frozenset({"/api/config", "/api/me/permisos", "/api/me", "/health"})
CABECERA_AUTOMATICA: Final = "x-sincronizacion"  # el teléfono del conductor sincroniza solo cada minuto

# Acciones POST que modifican algo existente (no crean)
_POST_MODIFICA: Final = re.compile(
    r"/(estatus|reasignar|clave|descartar|lectura|activo|confirmar)$|^/api/conductor/eventos$"
)
_POST_CONSULTA: Final = re.compile(r"/(analizar|analizar-archivo)$")
_ID: Final = re.compile(r"^(\d+|[0-9a-f-]{8,36}|[A-Z]{2,4}-[\w-]+)$", re.I)


@dataclass(frozen=True)
class Actor:
    id: int | None
    usuario: str | None
    nombre: str | None
    rol: str | None
    empresa_id: int | None
    empresa_nombre: str | None

    @classmethod
    def de(cls, u: Usuario) -> Actor:
        return cls(u.id, u.username, u.nombre, u.rol, u.empresa_id, u.empresa.nombre if u.empresa else None)


def clasificar(metodo: str, ruta: str, cuerpo: Any = None) -> tuple[AccionBitacora, str | None, str | None]:
    """(acción, módulo, id del recurso) a partir del método y la ruta."""
    partes = [p for p in ruta.split("/") if p][1:]  # sin "api"
    if partes[:1] == ["admin"]:
        partes = partes[1:]
    modulo = partes[0] if partes else None
    if modulo == "me":
        modulo = "cuenta"
    recurso = next((p for p in partes[1:] if _ID.match(p)), None)
    metodo = metodo.upper()
    if metodo in ("GET", "HEAD"):
        accion = AccionBitacora.CONSULTAR
    elif metodo == "DELETE":
        accion = AccionBitacora.ELIMINAR
    elif metodo in ("PATCH", "PUT"):
        accion = AccionBitacora.MODIFICAR
    elif _POST_CONSULTA.search(ruta) or (ruta.endswith("/notificar/vendedores") and _simular(cuerpo)):
        accion = AccionBitacora.CONSULTAR
    elif _POST_MODIFICA.search(ruta) or ruta == "/api/me/clave":
        accion = AccionBitacora.MODIFICAR
    else:
        accion = AccionBitacora.CREAR
    return accion, modulo, recurso


def _simular(cuerpo: Any) -> bool:
    return isinstance(cuerpo, dict) and cuerpo.get("simular") is True


def sanear(valor: Any, profundidad: int = 0) -> Any:
    """Copia segura para la bitácora: sin secretos, textos y listas acotados."""
    if profundidad > 4:
        return "…"
    if isinstance(valor, dict):
        return {
            str(k): ("[oculto]" if any(p in str(k).lower() for p in PROHIBIDAS) else sanear(v, profundidad + 1))
            for k, v in list(valor.items())[:60]
        }
    if isinstance(valor, list):
        out = [sanear(v, profundidad + 1) for v in valor[:MAX_LISTA]]
        if len(valor) > MAX_LISTA:
            out.append(f"… y {len(valor) - MAX_LISTA} más")
        return out
    if isinstance(valor, str) and len(valor) > MAX_TEXTO:
        return valor[:MAX_TEXTO] + "…"
    return valor


def acotar(detalle: dict[str, Any]) -> dict[str, Any]:
    texto = json.dumps(detalle, ensure_ascii=False, default=str)
    if len(texto) <= MAX_DETALLE:
        return detalle
    return {"_resumen": texto[:MAX_DETALLE] + "…", "_truncado": True}


def detalle_de_cuerpo(content_type: str, cuerpo: bytes | None, largo: int | None) -> dict[str, Any]:
    """Payload de la petición para la bitácora (JSON saneado; archivos solo resumidos)."""
    if not cuerpo and not largo:
        return {}
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "application/json" and cuerpo and cuerpo[:2] != b"\x1f\x8b":
        try:
            return {"payload": sanear(json.loads(cuerpo))}
        except ValueError:
            return {"payload": "(JSON inválido)"}
    tamano = largo if largo is not None else len(cuerpo or b"")
    return {"archivo_o_datos": {"tipo": ct or "desconocido", "bytes": tamano}}


def registrar(
    sesiones: Callable[[], Session],
    *,
    accion: AccionBitacora,
    ruta: str,
    actor: Actor | None = None,
    metodo: str | None = None,
    modulo: str | None = None,
    recurso_id: str | None = None,
    estado_http: int | None = None,
    detalle: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    duracion_ms: int | None = None,
) -> None:
    """Escribe una entrada en su propia transacción (independiente de la petición). Nunca lanza."""
    try:
        with sesiones() as db:
            db.add(Bitacora(
                usuario_id=actor.id if actor else None,
                usuario=actor.usuario if actor else None,
                usuario_nombre=actor.nombre if actor else None,
                rol=actor.rol if actor else None,
                empresa_id=actor.empresa_id if actor else None,
                empresa_nombre=actor.empresa_nombre if actor else None,
                accion=accion.value,
                modulo=(modulo or "")[:40] or None,
                metodo=metodo,
                ruta=ruta[:300],
                recurso_id=(recurso_id or "")[:64] or None,
                estado_http=estado_http,
                exito=estado_http is None or estado_http < 400,
                detalle=acotar(sanear(detalle or {})),
                ip=(ip or "")[:64] or None,
                user_agent=(user_agent or "")[:300] or None,
                request_id=request_id,
                duracion_ms=duracion_ms,
            ))
            db.commit()
    except Exception:  # la bitácora jamás debe tumbar la operación del usuario
        log.exception("bitacora_no_registrada", extra={"ruta": ruta, "accion": accion.value})
