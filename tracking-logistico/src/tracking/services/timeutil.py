"""Utilidades de fecha/hora con la zona horaria de la operación."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from tracking.config import get_settings


@lru_cache
def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz())


def local_day_bounds(desde: date | None, hasta: date | None) -> tuple[datetime | None, datetime | None]:
    """Convierte un rango de días locales [desde, hasta] (inclusive) en instantes UTC."""
    tz = local_tz()
    ini = datetime.combine(desde, time.min, tzinfo=tz).astimezone(timezone.utc) if desde else None
    fin = (
        datetime.combine(hasta + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
        if hasta
        else None
    )
    return ini, fin


def inicio_dia_local(d: date | None) -> datetime | None:
    """Día sin hora (p. ej. del Excel) → 00:00 de ese día en la zona de la operación, en UTC."""
    if d is None:
        return None
    return datetime.combine(d, time.min, tzinfo=local_tz()).astimezone(timezone.utc)


def today_local() -> date:
    return to_local(now_utc()).date()
