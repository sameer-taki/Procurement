"""paper planning: grade/deckle on items + usage_history, forecasts, shipments

The procurement SOP (paper inventory) plans by grade AND deckle with a rolling
3-month cover target. This adds the two paper attributes to the item master
mirror, monthly usage imported from BC (trailing movement for non-forecast
items), customer forecasts in cartons, and per-PO shipment tracking (the
in-transit record the reorder formula nets off).

Revision ID: b6e2d9a4f8c1
Revises: 3f7c9d12e5ab
Create Date: 2026-07-02 09:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b6e2d9a4f8c1'
down_revision: Union[str, None] = '3f7c9d12e5ab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'items',
        sa.Column('grade', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.add_column('items', sa.Column('deckle_mm', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_items_grade'), 'items', ['grade'], unique=False)

    op.create_table(
        'usage_history',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('item_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('period', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('item_id', 'period',
                            name='uq_usage_history_item_period'),
    )
    op.create_index(op.f('ix_usage_history_item_id'), 'usage_history',
                    ['item_id'], unique=False)
    op.create_index(op.f('ix_usage_history_period'), 'usage_history',
                    ['period'], unique=False)

    op.create_table(
        'forecasts',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('customer', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('item_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('period', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('qty_cartons', sa.Float(), nullable=False),
        sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('customer', 'item_id', 'period',
                            name='uq_forecasts_customer_item_period'),
    )
    op.create_index(op.f('ix_forecasts_customer'), 'forecasts',
                    ['customer'], unique=False)
    op.create_index(op.f('ix_forecasts_item_id'), 'forecasts',
                    ['item_id'], unique=False)
    op.create_index(op.f('ix_forecasts_period'), 'forecasts',
                    ['period'], unique=False)

    op.create_table(
        'shipments',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('po_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('vessel', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('etd', sa.Date(), nullable=True),
        sa.Column('eta', sa.Date(), nullable=True),
        sa.Column('rolls', sa.Integer(), nullable=True),
        sa.Column('weight_kg', sa.Float(), nullable=True),
        sa.Column('fcl_count', sa.Integer(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('notes', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['po_id'], ['purchase_orders.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_shipments_po_id'), 'shipments', ['po_id'],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_shipments_po_id'), table_name='shipments')
    op.drop_table('shipments')
    op.drop_index(op.f('ix_forecasts_period'), table_name='forecasts')
    op.drop_index(op.f('ix_forecasts_item_id'), table_name='forecasts')
    op.drop_index(op.f('ix_forecasts_customer'), table_name='forecasts')
    op.drop_table('forecasts')
    op.drop_index(op.f('ix_usage_history_period'), table_name='usage_history')
    op.drop_index(op.f('ix_usage_history_item_id'), table_name='usage_history')
    op.drop_table('usage_history')
    op.drop_index(op.f('ix_items_grade'), table_name='items')
    op.drop_column('items', 'deckle_mm')
    op.drop_column('items', 'grade')
