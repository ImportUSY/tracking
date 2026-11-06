"""Fábrica de la aplicación FastAPI: API + PWA estática."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse

from tracking import __version__
from tracking.api import (
    admin,
    auth,
    biometria,
    capturas,
    conductor,
    empresas,
    feriados,
    importaciones,
    publico,
    remesas,
    reportes,
)
from tracking.config import Settings, get_settings
from tracking.db import Database
from tracking.enums import AccionBitacora
from tracking.logging_config import configure_logging, request_id_var
from tracking.ratelimit import SlidingWindowLimiter
from tracking.security import client_ip
from tracking.services import bitacora
from tracking.services.ai import ClaudeClient
from tracking.services.evidencias import FirmadorHmac
from tracking.services.ocr_facturas import ExtractorFacturas, crear_extractor
from tracking.services.photos import PhotoStorage
from tracking.services.reportes import RangoInvalido
from tracking.services.users import asegurar_supervisor_inicial
from tracking.tenancy import ViolacionAislamiento

log = logging.getLogger("tracking")
WEB_DIR = Path(__file__).parent / "web"


@lru_cache(maxsize=256)
def _etag_contenido(ruta: str, mtime: float, tamano: int) -> str:
    return f'"{hashlib.md5(Path(ruta).read_bytes(), usedforsecurity=False).hexdigest()}"'


def archivo_web(ruta: Path | str, stat_result: os.stat_result | None = None, **kwargs: Any) -> FileResponse:
    """FileResponse con ETag por contenido y sin Last-Modified.

    En Vercel todos los archivos tienen la misma fecha: el ETag de Starlette (fecha + tamaño) no cambia
    cuando una versión nueva pesa lo mismo, y el navegador recibe 304 y se queda con el archivo viejo.
    """
    st = stat_result or os.stat(ruta)
    resp = FileResponse(ruta, stat_result=st, **kwargs)
    resp.headers["etag"] = _etag_contenido(str(ruta), st.st_mtime, st.st_size)
    del resp.headers["last-modified"]
    return resp


class EstaticosWeb(StaticFiles):
    def file_response(self, full_path: Any, stat_result: os.stat_result, scope: Any,
                      status_code: int = 200) -> Response:
        resp = archivo_web(full_path, stat_result, status_code=status_code)
        if self.is_not_modified(resp.headers, Headers(scope=scope)):
            return NotModifiedResponse(resp.headers)
        return resp

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; worker-src 'self'; manifest-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db: Database = app.state.db
        db.wait_until_ready()
        if settings.db_auto_migrate:
            from tracking.migrate import upgrade_head

            upgrade_head(db.engine)
        with db.session() as s:
            asegurar_supervisor_inicial(s, settings.bootstrap_admin_user, settings.bootstrap_admin_password)
        log.info("app_iniciada", extra={"env": settings.app_env, "version": __version__,
                                        "ia": settings.ai_available, "correo": settings.email_enabled})
        yield
        app.state.ai.close()
        db.engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    # Las rutas reciben exactamente esta configuración (clave JWT, límites, etc.)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.db = database or Database(settings)
    # En Vercel el disco es de solo lectura: las fotos van a la base (STORAGE_BACKEND=db)
    app.state.photos = PhotoStorage(settings.storage_dir) if settings.storage_backend == "disk" else None
    app.state.ai = ClaudeClient(settings)
    # OCR de facturas en papel (inyectable: los tests lo reemplazan por un extractor falso)
    extractor: ExtractorFacturas = crear_extractor(
        app.state.ai, settings.ocr_model or settings.anthropic_model, settings.ocr_available
    )
    app.state.extractor_facturas = extractor
    app.state.login_limiter = SlidingWindowLimiter(settings.login_max_attempts, settings.login_window_seconds)
    app.state.public_limiter = SlidingWindowLimiter(settings.public_rate_limit_per_minute, 60)
    app.state.firmador_evidencias = FirmadorHmac(
        settings.secret_key, timedelta(minutes=settings.evidence_url_ttl_minutes)
    )

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        ruta = request.url.path
        en_bitacora = ruta.startswith("/api/") and ruta not in bitacora.RUTAS_OMITIDAS and not (
            request.method == "GET" and request.headers.get(bitacora.CABECERA_AUTOMATICA)
        )
        cuerpo: bytes | None = None
        largo = int(request.headers.get("content-length") or 0) or None
        if en_bitacora and request.method != "GET" and (largo or 0) <= 64 * 1024 \
                and (request.headers.get("content-type") or "").startswith("application/json"):
            cuerpo = await request.body()  # Starlette la guarda: el endpoint la vuelve a leer igual
        try:
            response = await call_next(request)
        except Exception:
            log.exception("error_no_controlado", extra={"path": request.url.path})
            response = JSONResponse({"detail": "Error interno. Código: " + rid}, status_code=500)
        finally:
            request_id_var.reset(token)
        ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Aislamiento de origen (COOP/COEP): permite que el lector de facturas use varios hilos
        # (SharedArrayBuffer). Todo se sirve desde el mismo origen, así que no bloquea nada propio.
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Permissions-Policy"] = "camera=(self), geolocation=(self)"
        if request.url.path.startswith("/static/vendor/") and response.status_code == 200:
            # Lector de facturas (rutas con versión): una semana en la caché del navegador
            response.headers["Cache-Control"] = "public, max-age=604800"
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            # Respuestas de la API (pueden incluir claves temporales o datos personales): nunca en caché
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/api/") and request.url.path != "/api/docs":
            log.info("http", extra={"method": request.method, "path": request.url.path,
                                    "status": response.status_code, "ms": ms, "request_id": rid})
        if en_bitacora:
            await _bitacora(request, response.status_code, cuerpo, largo, rid, int(ms))
        return response

    async def _bitacora(request: Request, estado: int, cuerpo: bytes | None, largo: int | None,
                        rid: str, ms: int) -> None:
        """Una fila de bitácora por petición de un usuario identificado (o por intento de login)."""
        actor: bitacora.Actor | None = getattr(request.state, "bitacora_actor", None)
        forzada: AccionBitacora | None = getattr(request.state, "bitacora_accion", None)
        if actor is None and forzada is None:
            return  # peticiones anónimas (consulta pública, etc.): tienen su propio registro
        detalle_cuerpo = bitacora.detalle_de_cuerpo(request.headers.get("content-type", ""), cuerpo, largo)
        accion, modulo, recurso = bitacora.clasificar(request.method, request.url.path, detalle_cuerpo.get("payload"))
        detalle: dict[str, Any] = dict(getattr(request.state, "bitacora_detalle", {}) or {})
        if request.query_params:
            detalle["consulta"] = dict(request.query_params)
        detalle.update(detalle_cuerpo)
        if forzada == AccionBitacora.VISTA:
            modulo = str(detalle.get("vista") or modulo)  # la pantalla abierta, no la ruta técnica
        elif forzada in (AccionBitacora.LOGIN, AccionBitacora.LOGIN_FALLIDO, AccionBitacora.LOGOUT):
            modulo = "sesion"
        await run_in_threadpool(
            bitacora.registrar, app.state.db.session,
            accion=forzada or accion, ruta=request.url.path, actor=actor, metodo=request.method, modulo=modulo,
            recurso_id=recurso, estado_http=estado, detalle=detalle, ip=client_ip(request, settings),
            user_agent=request.headers.get("user-agent"), request_id=rid, duracion_ms=ms,
        )

    for r in (auth.router, biometria.router, importaciones.router, capturas.router, conductor.router, remesas.router,
              admin.router, empresas.router, publico.router, reportes.router, feriados.router):
        app.include_router(r)

    @app.exception_handler(ViolacionAislamiento)
    async def _fuera_de_alcance(request: Request, exc: ViolacionAislamiento) -> JSONResponse:
        # Red de seguridad multi-tenant: se responde como inexistente para no revelar datos ajenos.
        log.warning("aislamiento_bloqueado", extra={"path": request.url.path, "detalle": str(exc)})
        return JSONResponse({"detail": "Recurso no encontrado"}, status_code=404)

    @app.exception_handler(RangoInvalido)
    async def _rango_invalido(request: Request, exc: RangoInvalido) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        with app.state.db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    @app.get("/api/config", tags=["publico"])
    def public_config() -> dict[str, object]:
        return {"app": settings.app_name, "version": __version__,
                "version_publica": settings.version_publica or __version__, "fecha_creacion": settings.fecha_creacion,
                "correo": settings.email_enabled, "biometria": settings.biometria_habilitada,
                "ia": settings.ai_available, "max_foto_mb": settings.max_photo_mb,
                # Lectura de facturas por foto: "ia" (Anthropic, de pago) o "local" (Tesseract.js, gratis)
                "ocr_modo": "ia" if settings.ocr_available else "local"}

    # ---------------- PWA estática ----------------
    def page(name: str) -> Callable[[], FileResponse]:
        def _serve() -> FileResponse:
            return archivo_web(WEB_DIR / name, headers={"Cache-Control": "no-cache"})

        return _serve

    for ruta, archivo in (("/", "index.html"), ("/conductor", "conductor.html"), ("/panel", "panel.html"),
                          ("/tracking", "tracking.html")):
        app.add_api_route(ruta, page(archivo), include_in_schema=False, methods=["GET"])

    @app.get("/.well-known/assetlinks.json", include_in_schema=False)
    def assetlinks() -> JSONResponse:
        """Digital Asset Links: permite que el APK (TWA) abra la app a pantalla completa, sin barra."""
        if settings.android_assetlinks_json:
            try:
                return JSONResponse(json.loads(settings.android_assetlinks_json))
            except ValueError:
                log.error("ANDROID_ASSETLINKS_JSON no es un JSON válido")
                return JSONResponse([], status_code=500)
        if not settings.android_package_name or not settings.android_fingerprint_list:
            return JSONResponse([], status_code=404)
        return JSONResponse([{
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": settings.android_package_name,
                "sha256_cert_fingerprints": settings.android_fingerprint_list,
            },
        }])

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return archivo_web(WEB_DIR / "sw.js", media_type="application/javascript",
                           headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return archivo_web(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    app.mount("/static", EstaticosWeb(directory=WEB_DIR), name="static")
    return app


def app_factory() -> FastAPI:
    """Punto de entrada para uvicorn: `uvicorn tracking.main:app_factory --factory`."""
    return create_app()
