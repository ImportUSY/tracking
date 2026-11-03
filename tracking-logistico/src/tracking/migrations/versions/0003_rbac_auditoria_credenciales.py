"""RBAC: bitácora de auditoría y trazabilidad de credenciales

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09 09:51:30.309033
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('auditoria',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('actor_rol', sa.String(length=20), nullable=True),
    sa.Column('accion', sa.String(length=40), nullable=False),
    sa.Column('objetivo_usuario_id', sa.Integer(), nullable=True),
    sa.Column('detalle', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['usuarios.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['objetivo_usuario_id'], ['usuarios.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('auditoria', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_auditoria_accion'), ['accion'], unique=False)
        batch_op.create_index(batch_op.f('ix_auditoria_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_auditoria_objetivo_usuario_id'), ['objetivo_usuario_id'], unique=False)

    # ADD COLUMN nativo: en SQLite NO se reconstruye la tabla usuarios. Reconstruirla con las claves
    # foráneas activas dispararía ON DELETE SET NULL en conductores/eventos y perdería vínculos.
    op.add_column('usuarios', sa.Column('clave_actualizada_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('usuarios', sa.Column('clave_actualizada_por_id', sa.Integer(), nullable=True))
    if op.get_bind().dialect.name != 'sqlite':
        op.create_foreign_key('fk_usuarios_clave_actualizada_por', 'usuarios', 'usuarios',
                              ['clave_actualizada_por_id'], ['id'], ondelete='SET NULL')



def downgrade() -> None:
    if op.get_bind().dialect.name != 'sqlite':
        op.drop_constraint('fk_usuarios_clave_actualizada_por', 'usuarios', type_='foreignkey')
    op.drop_column('usuarios', 'clave_actualizada_por_id')
    op.drop_column('usuarios', 'clave_actualizada_at')

    with op.batch_alter_table('auditoria', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_auditoria_objetivo_usuario_id'))
        batch_op.drop_index(batch_op.f('ix_auditoria_created_at'))
        batch_op.drop_index(batch_op.f('ix_auditoria_accion'))

    op.drop_table('auditoria')
