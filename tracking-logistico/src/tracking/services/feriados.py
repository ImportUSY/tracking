"""Días feriados: no cuentan como días hábiles en los tiempos entre procesos.

La tabla ``feriados`` es la fuente: el Supervisor agrega o quita días desde el panel (Feriados). Para no
cargarlos a mano cada año, ``cargar_nacionales`` agrega los feriados nacionales de Venezuela de un año
(los fijos y los que dependen de la Semana Santa). Revise la lista: puede haber decretos adicionales.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracking.models import Feriado

# Fijos: Ley Orgánica del Trabajo (art. 184) y Ley de Fiestas Nacionales
FIJOS: tuple[tuple[int, int, str], ...] = (
    (1, 1, "Año Nuevo"),
    (4, 19, "Declaración de la Independencia"),
    (5, 1, "Día del Trabajador"),
    (6, 24, "Batalla de Carabobo"),
    (7, 5, "Día de la Independencia"),
    (7, 24, "Natalicio del Libertador"),
    (10, 12, "Día de la Resistencia Indígena"),
    (12, 24, "Nochebuena"),
    (12, 25, "Navidad"),
    (12, 31, "Fin de Año"),
)


def pascua(anio: int) -> date:
    """Domingo de Resurrección (algoritmo gregoriano anónimo)."""
    a, b, c = anio % 19, anio // 100, anio % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    mes = (h + m - 7 * n + 114) // 31
    dia = (h + m - 7 * n + 114) % 31 + 1
    return date(anio, mes, dia)


def nacionales(anio: int) -> list[tuple[date, str]]:
    p = pascua(anio)
    moviles = [
        (p - timedelta(days=48), "Lunes de Carnaval"),
        (p - timedelta(days=47), "Martes de Carnaval"),
        (p - timedelta(days=3), "Jueves Santo"),
        (p - timedelta(days=2), "Viernes Santo"),
    ]
    # Si dos feriados caen el mismo día (p. ej. Viernes Santo y 19 de abril en 2030) quedan en una sola fecha
    por_fecha: dict[date, list[str]] = {}
    for f, n in [(date(anio, m, d), n) for m, d, n in FIJOS] + moviles:
        por_fecha.setdefault(f, []).append(n)
    return sorted((f, " / ".join(ns)) for f, ns in por_fecha.items())


def cargar_nacionales(db: Session, anio: int) -> int:
    """Agrega los feriados nacionales del año que falten. Devuelve cuántos agregó (no hace commit)."""
    del_anio = Feriado.fecha.between(date(anio, 1, 1), date(anio, 12, 31))
    existentes = set(db.scalars(select(Feriado.fecha).where(del_anio)))
    nuevos = [Feriado(fecha=f, nombre=n, nacional=True) for f, n in nacionales(anio) if f not in existentes]
    db.add_all(nuevos)
    return len(nuevos)


def fechas(db: Session) -> tuple[date, ...]:
    """Feriados que caen de lunes a viernes, ordenados (los de fin de semana ya no son hábiles)."""
    return tuple(sorted(f for f in db.scalars(select(Feriado.fecha)) if f.weekday() < 5))
