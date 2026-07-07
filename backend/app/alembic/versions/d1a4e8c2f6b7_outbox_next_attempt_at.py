"""outbox: next_attempt_at for exponential backoff

A fixed 60s retry with a 5-attempt budget turned every in-flight BC post
terminally FAILED after ~5 minutes of BC downtime. Recording the earliest
permitted retry time lets failures back off (1m, 5m, 30m, 2h, 12h), so the
budget spans hours instead of minutes and a transient outage self-heals.

Revision ID: d1a4e8c2f6b7
Revises: c7f3a1b9d2e4
Create Date: 2026-07-03 01:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd1a4e8c2f6b7'
down_revision: Union[str, None] = 'c7f3a1b9d2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'integration_outbox',
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('integration_outbox', 'next_attempt_at')
