"""Todas las fotos de cada estatus (fotos.evento_id) y hora en el consumo de datos móviles

* ``fotos.evento_id``: un evento del historial puede tener varias fotos. Se rellena con el evento que
  ya apuntaba a cada foto (``eventos_estatus.foto_id``).
* ``consumo_datos.hora``: la medición se agrupa por hora local (además del día); las filas existentes
  toman la hora local de su primera petición.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-06 15:00:00
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hora_local(valor: datetime | str | None) -> int:
    from tracking.services.timeutil import to_local

    if valor is None:
        return 0
    if isinstance(valor, str):
        valor = datetime.fromisoformat(valor)
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return to_local(valor).hour


def upgrade() -> None:
    with op.batch_alter_table("fotos") as t:
        t.add_column(sa.Column("evento_id", sa.String(36)))
    op.create_index("ix_fotos_evento_id", "fotos", ["evento_id"])
    op.execute(
        "UPDATE fotos SET evento_id = (SELECT e.id FROM eventos_estatus e WHERE e.foto_id = fotos.id "
        "ORDER BY e.recibido_at LIMIT 1) WHERE evento_id IS NULL"
    )

    with op.batch_alter_table("consumo_datos") as t:
        t.add_column(sa.Column("hora", sa.SmallInteger(), nullable=False, server_default="0"))
    bind = op.get_bind()
    for id_, desde in bind.execute(sa.text("SELECT id, desde FROM consumo_datos")).all():
        bind.execute(sa.text("UPDATE consumo_datos SET hora = :h WHERE id = :i"), {"h": _hora_local(desde), "i": id_})
    with op.batch_alter_table("consumo_datos") as t:
        t.drop_constraint("uq_consumo_datos_clave", type_="unique")
        t.create_unique_constraint(
            "uq_consumo_datos_hora", ["usuario_id", "conexion_id", "metodo", "ruta", "dia", "hora"])


def downgrade() -> None:
    with op.batch_alter_table("consumo_datos") as t:
        t.drop_constraint("uq_consumo_datos_hora", type_="unique")
    # Sin la hora quedarían varias filas con la misma clave diaria: se conserva solo la primera
    op.execute(
        "DELETE FROM consumo_datos WHERE id NOT IN (SELECT MIN(id) FROM consumo_datos "
        "GROUP BY usuario_id, conexion_id, metodo, ruta, dia)"
    )
    with op.batch_alter_table("consumo_datos") as t:
        t.drop_column("hora")
        t.create_unique_constraint("uq_consumo_datos_clave", ["usuario_id", "conexion_id", "metodo", "ruta", "dia"])
    op.drop_index("ix_fotos_evento_id", table_name="fotos")
    with op.batch_alter_table("fotos") as t:
        t.drop_column("evento_id")
