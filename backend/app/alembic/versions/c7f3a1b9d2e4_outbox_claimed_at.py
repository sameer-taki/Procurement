"""outbox: claimed_at for stale-SENDING recovery

A worker claims an outbox row by flipping PENDING -> SENDING; if it then crashes
(or a redeploy kills it) mid-post, the row is stranded SENDING forever with no
recovery path. Recording the claim time lets the processor reclaim rows that
have been SENDING longer than any real post could take, back to PENDING. This
is safe because the ExternalRef idempotency anchor already makes a re-post a
no-op — the reaper only ensures the work resumes.

Revision ID: c7f3a1b9d2e4
Revises: b6e2d9a4f8c1
Create Date: 2026-07-03 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c7f3a1b9d2e4'
down_revision: Union[str, None] = 'b6e2d9a4f8c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'integration_outbox',
        sa.Column('claimed_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('integration_outbox', 'claimed_at')
