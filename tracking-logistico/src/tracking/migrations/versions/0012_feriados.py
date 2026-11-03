"""Días feriados (no cuentan como hábiles en los tiempos entre procesos)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-29 09:00:00

    CREATE TABLE feriados (
        id SERIAL PRIMARY KEY,
        fecha DATE NOT NULL UNIQUE,
        nombre VARCHAR(120) NOT NULL,
        nacional BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL
    );

Se cargan los feriados nacionales de Venezuela de 2024 a 2030 (fijos + Carnaval y Semana Santa).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIJOS = ((1, 1, "Año Nuevo"), (4, 19, "Declaración de la Independencia"), (5, 1, "Día del Trabajador"),
          (6, 24, "Batalla de Carabobo"), (7, 5, "Día de la Independencia"), (7, 24, "Natalicio del Libertador"),
          (10, 12, "Día de la Resistencia Indígena"), (12, 24, "Nochebuena"), (12, 25, "Navidad"),
          (12, 31, "Fin de Año"))


def _pascua(anio: int) -> date:
    a, b, c = anio % 19, anio // 100, anio % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    return date(anio, (h + m - 7 * n + 114) // 31, (h + m - 7 * n + 114) % 31 + 1)


def upgrade() -> None:
    tabla = op.create_table(
        "feriados",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("nombre", sa.String(length=120), nullable=False),
        sa.Column("nacional", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_feriados_fecha", "feriados", ["fecha"], unique=True)
    ahora = datetime.now(timezone.utc)
    filas = []
    for anio in range(2024, 2031):
        p = _pascua(anio)
        dias = [(date(anio, m, d), n) for m, d, n in _FIJOS] + [
            (p - timedelta(days=48), "Lunes de Carnaval"), (p - timedelta(days=47), "Martes de Carnaval"),
            (p - timedelta(days=3), "Jueves Santo"), (p - timedelta(days=2), "Viernes Santo")]
        por_fecha: dict[date, list[str]] = {}
        for f, n in dias:  # dos feriados el mismo día (p. ej. Viernes Santo y 19 de abril en 2030): una fila
            por_fecha.setdefault(f, []).append(n)
        filas += [{"fecha": f, "nombre": " / ".join(ns), "nacional": True, "created_at": ahora}
                  for f, ns in sorted(por_fecha.items())]
    op.bulk_insert(tabla, filas)


def downgrade() -> None:
    op.drop_index("ix_feriados_fecha", table_name="feriados")
    op.drop_table("feriados")
