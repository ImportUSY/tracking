"""Configuración central de la aplicación (12-factor: todo por variables de entorno)."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- General ---
    app_name: str = "Tracking Logístico"
    app_env: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    log_json: bool = True
    timezone: str = "America/Caracas"
    # Jornada laboral para los tiempos entre procesos (días hábiles de lunes a viernes, hora local):
    # por defecto de 7:00 a 16:00 → un día hábil = 9 horas. JORNADA_INICIO / JORNADA_FIN en horas enteras.
    jornada_inicio: int = Field(default=7, ge=0, le=23)
    jornada_fin: int = Field(default=16, ge=1, le=24)
    # Clave temporal de usuarios y conductores nuevos (o al restablecer la clave): CLAVE_TEMPORAL.
    # Vacía = una clave aleatoria distinta para cada uno. Con ella se puede entrar ACCESOS_CLAVE_TEMPORAL
    # veces (con aviso); después el sistema exige crear una clave personal.
    clave_temporal: str = "qwerty"
    accesos_clave_temporal: int = Field(default=3, ge=0, le=20)
    cors_origins: str = ""  # lista separada por comas
    # Pie de la pantalla de inicio: «Versión X.X.X · Octubre 2026». La versión sale de tracking.__version__
    # (pyproject.toml); VERSION_PUBLICA permite mostrar otra (p. ej. «2.0.0-beta») sin tocar el código.
    version_publica: str | None = None
    fecha_creacion: str = "Octubre 2026"

    # --- Plataforma ---
    # Vercel define VERCEL=1 en sus funciones: activa conexiones "serverless" y fotos en la base.
    vercel: bool = False

    # --- Base de datos ---
    # Acepta DATABASE_URL o POSTGRES_URL (la que crea la integración Neon de Vercel).
    database_url: str = Field(
        default="sqlite:///./data/tracking.db",
        validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL", "database_url"),
    )
    # Conexiones para funciones serverless (pool mínimo, sin sentencias preparadas: compatible con PgBouncer)
    db_serverless: bool | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_connect_retries: int = 6
    # Aplica las migraciones Alembic pendientes al arrancar (seguro: alembic usa una tabla de versión)
    db_auto_migrate: bool = True

    # --- Seguridad ---
    secret_key: str = ""
    jwt_algorithm: str = "HS256"
    staff_token_minutes: int = 12 * 60
    # Los conductores trabajan sin señal: el token debe sobrevivir varios días offline.
    driver_token_days: int = 14
    bootstrap_admin_user: str = "supervisor"
    bootstrap_admin_password: str = ""
    # Tomar la IP del cliente de X-Forwarded-For (detrás de Vercel, Caddy o nginx). Por defecto: sí en Vercel.
    trust_proxy_headers: bool | None = None
    # Roles que pueden crear usuarios CONDUCTOR (lista separada por comas).
    # Valores permitidos: supervisor, transportista. Ej.: CONDUCTOR_CREATOR_ROLES=transportista
    conductor_creator_roles: str = "supervisor,transportista"
    login_max_attempts: int = 8
    login_window_seconds: int = 300
    # Ingreso con la huella o el rostro del teléfono (WebAuthn / llaves de acceso). Vacíos = se toman del
    # dominio con que se abre la app (lo normal). WEBAUTHN_RP_ID: dominio sin https:// ni puerto;
    # WEBAUTHN_ORIGENES: orígenes completos permitidos, separados por coma (https://dominio).
    biometria_habilitada: bool = True
    webauthn_rp_id: str = ""
    webauthn_origenes: str = ""

    # --- Archivos ---
    storage_dir: Path = Path("./data/fotos")
    # disk = carpeta STORAGE_DIR (servidor propio/Docker); db = dentro de PostgreSQL (Vercel, sin disco)
    storage_backend: Literal["disk", "db"] | None = None
    max_excel_mb: int = 30
    max_photo_mb: int = 10

    # --- App Android (APK tipo TWA): verificación de dominio /.well-known/assetlinks.json ---
    # Opción simple: pegar aquí el contenido completo de assetlinks.json que entrega PWABuilder
    android_assetlinks_json: str = ""
    android_package_name: str = ""
    android_sha256_fingerprints: str = ""  # huellas SHA-256 del certificado, separadas por coma

    # --- Consulta pública ---
    public_rate_limit_per_minute: int = 30
    # --- Proof of Delivery en la consulta pública (vendedores / clientes) ---
    # Expone un enlace firmado y temporal a la foto de Entregada/Devolución. False = nunca se muestran fotos.
    public_evidence_enabled: bool = True
    evidence_url_ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)

    # --- IA (opcional) ---
    ai_review_enabled: bool = False
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    ai_timeout_seconds: float = 60.0
    # --- Captura de facturas en papel (visión/OCR con Claude) ---
    # Usa ANTHROPIC_API_KEY. Independiente de AI_REVIEW_ENABLED: se puede tener OCR sin la revisión de cargas.
    ocr_enabled: bool = True
    ocr_model: str = ""  # vacío = ANTHROPIC_MODEL

    # --- Correo (segunda entrega, apagado por defecto) ---
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_from: str = ""
    cobranzas_emails: str = ""  # lista separada por comas

    @field_validator("*", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        # Valores pegados en paneles o enviados por consola suelen traer espacios o saltos de línea
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check_secrets(self) -> Settings:
        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql://" + self.database_url[len("postgres://"):]
        if self.db_serverless is None:
            self.db_serverless = self.vercel
        roles = set(self._csv(self.conductor_creator_roles.lower()))
        invalidos = roles - {"supervisor", "transportista"}
        if invalidos or not roles:
            raise ValueError(
                "CONDUCTOR_CREATOR_ROLES admite solo 'supervisor' y/o 'transportista' "
                f"(recibido: {self.conductor_creator_roles!r})"
            )
        if self.jornada_fin <= self.jornada_inicio:
            raise ValueError("JORNADA_FIN debe ser mayor que JORNADA_INICIO (horas del día, p. ej. 7 y 16)")
        if self.trust_proxy_headers is None:
            self.trust_proxy_headers = self.vercel
        if self.storage_backend is None:
            self.storage_backend = "db" if self.vercel else "disk"
        if self.vercel:
            # Vercel limita cada petición a 4,5 MB
            self.max_photo_mb = min(self.max_photo_mb, 4)
        if self.vercel and self.is_sqlite:
            raise ValueError(
                "En Vercel se necesita PostgreSQL: conecte la base Neon (Storage) o defina DATABASE_URL"
            )
        if not self.secret_key:
            if self.app_env == "production" or self.vercel:
                raise ValueError("SECRET_KEY es obligatorio en producción")
            # En desarrollo/test se genera una clave efímera (los tokens no sobreviven reinicios).
            self.secret_key = secrets.token_urlsafe(48)
        if self.app_env == "production" and len(self.secret_key) < 32:
            raise ValueError("SECRET_KEY debe tener al menos 32 caracteres")
        return self

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [p.strip() for p in value.split(",") if p.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return self._csv(self.cors_origins)

    @property
    def cobranzas_email_list(self) -> list[str]:
        return self._csv(self.cobranzas_emails)

    @property
    def webauthn_origen_list(self) -> list[str]:
        return [o.rstrip("/") for o in self._csv(self.webauthn_origenes)]

    @property
    def conductor_creator_role_set(self) -> frozenset[str]:
        return frozenset(self._csv(self.conductor_creator_roles.lower()))

    @property
    def android_fingerprint_list(self) -> list[str]:
        return [f.upper() for f in self._csv(self.android_sha256_fingerprints)]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def ai_available(self) -> bool:
        return self.ai_review_enabled and bool(self.anthropic_api_key)

    @property
    def ocr_available(self) -> bool:
        return self.ocr_enabled and bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
