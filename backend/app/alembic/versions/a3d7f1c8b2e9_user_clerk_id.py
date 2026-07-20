"""add users.clerk_user_id (Clerk auth)

Clerk becomes the primary sign-in path in the cloud setup; users are matched by
their Clerk user id (the token `sub`) with email as the fallback key. Nullable so
existing Entra/break-glass rows are untouched and the first Clerk login backfills
it. Batch ALTER keeps the up/down chain green on SQLite (CI migration check) and
Postgres alike.

Revision ID: a3d7f1c8b2e9
Revises: e2b9c4a7d0f3
Create Date: 2026-07-20 01:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'a3d7f1c8b2e9'
down_revision: Union[str, None] = 'e2b9c4a7d0f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(
            sa.Column('clerk_user_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True)
        )
    op.create_index(op.f('ix_users_clerk_user_id'), 'users', ['clerk_user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_users_clerk_user_id'), table_name='users')
    with op.batch_alter_table('users') as batch:
        batch.drop_column('clerk_user_id')
