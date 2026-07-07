"""customer master (mirrored from BC)

BC owns the customer master; the app syncs it read-only so a forecast's customer
can be picked from the canonical BC list instead of free-typed.

Revision ID: e2b9c4a7d0f3
Revises: d1a4e8c2f6b7
Create Date: 2026-07-03 02:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'e2b9c4a7d0f3'
down_revision: Union[str, None] = 'd1a4e8c2f6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'customers',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('bc_customer_no', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('email', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_customers_bc_customer_no'), 'customers',
                    ['bc_customer_no'], unique=False)
    op.create_index(op.f('ix_customers_name'), 'customers', ['name'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_customers_name'), table_name='customers')
    op.drop_index(op.f('ix_customers_bc_customer_no'), table_name='customers')
    op.drop_table('customers')
