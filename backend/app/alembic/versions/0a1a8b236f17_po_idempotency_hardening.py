"""phase 3 hardening: PO idempotency + outbox dedupe + crosswalk uniqueness

- integration_outbox.entity_ref: first-class dedupe key (the PO id) + index, so
  the re-enqueue check is an indexed lookup rather than a scan+json-parse.
- partial UNIQUE index on integration_outbox(target, action, entity_ref) WHERE
  status != 'FAILED': at most one LIVE posting job per PO; FAILED rows excluded so
  a fresh attempt can be enqueued after a dead row.
- UNIQUE constraint on external_refs(entity_kind, entity_id, system, external_type):
  the DB-level idempotency anchor that makes a concurrent double-post impossible.
- Backfill: entity_ref from each create_purchase_order row's request_json po_id, and
  re-key existing BC PO crosswalk rows from entity_kind 'PURCHASE_ORDER' -> 'PO'
  (the documented canonical value).

Revision ID: 0a1a8b236f17
Revises: 8b1f2a4c7d3e
Create Date: 2026-06-25 07:00:00.000000
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0a1a8b236f17'
down_revision: Union[str, None] = '8b1f2a4c7d3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1) integration_outbox.entity_ref + plain index for fast dedupe lookups.
    op.add_column(
        'integration_outbox',
        sa.Column('entity_ref', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.create_index(
        op.f('ix_integration_outbox_entity_ref'),
        'integration_outbox',
        ['entity_ref'],
        unique=False,
    )

    # Backfill entity_ref from request_json po_id for existing rows.
    rows = bind.execute(sa.text(
        "SELECT id, request_json FROM integration_outbox "
        "WHERE action = 'create_purchase_order'"
    )).fetchall()
    for row_id, request_json in rows:
        po_id = None
        try:
            po_id = (json.loads(request_json) or {}).get('po_id')
        except (ValueError, TypeError):
            po_id = None
        if po_id:
            bind.execute(
                sa.text("UPDATE integration_outbox SET entity_ref = :ref WHERE id = :id"),
                {"ref": po_id, "id": row_id},
            )

    # 2) Re-key existing BC PO crosswalk rows to the canonical entity_kind 'PO'.
    bind.execute(sa.text(
        "UPDATE external_refs SET entity_kind = 'PO' "
        "WHERE entity_kind = 'PURCHASE_ORDER' AND system = 'BC' "
        "AND external_type = 'PURCHASE_ORDER'"
    ))

    # 3) Partial UNIQUE index: one LIVE outbox job per (target, action, entity_ref).
    op.create_index(
        'uq_integration_outbox_live_ref',
        'integration_outbox',
        ['target', 'action', 'entity_ref'],
        unique=True,
        postgresql_where=sa.text("status != 'FAILED' AND entity_ref IS NOT NULL"),
        sqlite_where=sa.text("status != 'FAILED' AND entity_ref IS NOT NULL"),
    )

    # 4) UNIQUE constraint on the external_refs crosswalk. SQLite cannot add a
    #    constraint in place, so use batch mode (table copy) there; Postgres adds
    #    it directly.
    with op.batch_alter_table('external_refs', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_external_refs_entity_system_type',
            ['entity_kind', 'entity_id', 'system', 'external_type'],
        )


def downgrade() -> None:
    with op.batch_alter_table('external_refs', schema=None) as batch_op:
        batch_op.drop_constraint('uq_external_refs_entity_system_type', type_='unique')

    op.drop_index('uq_integration_outbox_live_ref', table_name='integration_outbox')

    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE external_refs SET entity_kind = 'PURCHASE_ORDER' "
        "WHERE entity_kind = 'PO' AND system = 'BC' "
        "AND external_type = 'PURCHASE_ORDER'"
    ))

    op.drop_index(op.f('ix_integration_outbox_entity_ref'), table_name='integration_outbox')
    op.drop_column('integration_outbox', 'entity_ref')
