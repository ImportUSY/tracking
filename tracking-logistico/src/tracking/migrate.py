"""Ejecución programática de migraciones Alembic (usada al arrancar y por la CLI)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Clave arbitraria del candado: varias instancias (workers, funciones serverless) pueden arrancar a la vez.
_ADVISORY_LOCK_KEY = 7_420_261


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade_head(engine: Engine) -> None:
    cfg = alembic_config()
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # Serializa migraciones concurrentes; se libera solo al terminar la transacción.
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
