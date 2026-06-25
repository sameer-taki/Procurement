"""add purchase_orders.requisition_id

Phase 3: trace a PurchaseOrder back to the approved requisition it was created
from. Nullable + indexed; no FK enforcement change beyond the column itself.

Revision ID: 8b1f2a4c7d3e
Revises: 2c4acce36085
Create Date: 2026-06-25 06:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '8b1f2a4c7d3e'
down_revision: Union[str, None] = '2c4acce36085'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'purchase_orders',
        sa.Column('requisition_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.create_index(
        op.f('ix_purchase_orders_requisition_id'),
        'purchase_orders',
        ['requisition_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_purchase_orders_requisition_id'), table_name='purchase_orders')
    op.drop_column('purchase_orders', 'requisition_id')
