"""Motor de base de datos, sesiones y verificación de conexión con reintentos."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_exponential

from tracking.config import Settings, get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def build_engine(settings: Settings) -> Engine:
    url = settings.database_url
    if settings.is_sqlite:
        db_path = url.split("sqlite:///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
        event.listen(engine, "connect", _sqlite_pragmas)
        return engine
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if settings.db_serverless:
        # Funciones serverless (Vercel): pocas conexiones por instancia, reciclaje corto y sin sentencias
        # preparadas del lado del servidor (compatibles con el pooler PgBouncer de Neon).
        return create_engine(
            url,
            pool_size=1,
            max_overflow=2,
            pool_pre_ping=True,
            pool_recycle=240,
            connect_args={"prepare_threshold": None, "connect_timeout": 10},
            future=True,
        )
    return create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )


class Database:
    """Contenedor del engine + fábrica de sesiones (inyectable en tests)."""

    def __init__(self, settings: Settings | None = None, engine: Engine | None = None) -> None:
        self.settings = settings or get_settings()
        self.engine = engine or build_engine(self.settings)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        from tracking.tenancy import instalar_aislamiento  # import diferido: tenancy depende de models

        instalar_aislamiento(self.session_factory)

    def wait_until_ready(self) -> None:
        attempts = max(1, self.settings.db_connect_retries)

        @retry(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=15),
            before_sleep=before_sleep_log(log, logging.WARNING),
            reraise=True,
        )
        def _ping() -> None:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))

        _ping()

    def create_all(self) -> None:
        from tracking import models  # noqa: F401  (registra tablas)

        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.session_factory()

    def session_scope(self) -> Iterator[Session]:
        db = self.session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
