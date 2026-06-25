"""phase 5 receiving: link receipt rows to a PO line + item

One GRN = several Receipt rows that share grn_no, one per received PO line. Adding
po_line_id + item_id (both nullable, indexed) makes each receipt row traceable to
the ordered line and the item it fulfilled, so the app can compute cumulative
received-vs-ordered per line and feed analytics (spend, on-time-delivery).

Revision ID: 3f7c9d12e5ab
Revises: 0a1a8b236f17
Create Date: 2026-06-25 09:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3f7c9d12e5ab'
down_revision: Union[str, None] = '0a1a8b236f17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'receipts',
        sa.Column('po_line_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column(
        'receipts',
        sa.Column('item_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.create_index(
        op.f('ix_receipts_po_line_id'), 'receipts', ['po_line_id'], unique=False,
    )
    op.create_index(
        op.f('ix_receipts_item_id'), 'receipts', ['item_id'], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_receipts_item_id'), table_name='receipts')
    op.drop_index(op.f('ix_receipts_po_line_id'), table_name='receipts')
    op.drop_column('receipts', 'item_id')
    op.drop_column('receipts', 'po_line_id')
