"""Add device-occupancy columns to vram_consumer (self.ai#74, held-unit fix)

`held_bytes` is a PER-CONSUMER, self-measured figure, and `total_held()` sums it
across consumers as if the holdings were disjoint. That is only sound if every
consumer measures the same thing about itself — which self.ai#74 found was not
true, and cannot be made true for everything on the card: CUDA contexts and any
process that is not a registered lease consumer are real VRAM that no consumer
can attribute to itself.

These columns carry the CARD-level reading (`nvidia-smi memory.used` /
`torch.cuda.mem_get_info`), which is a different quantity from any consumer's
held and must never be summed with it. `device_unattributed_bytes` is the
derived gap — card-used minus the ledger's total held at the moment of the
reading — recorded at relay time so `free_capacity()` can subtract real overhead
the ledger cannot see, WITHOUT losing the immediate feedback the R3 reclamation
loop depends on (a confirmed release must raise free capacity at once, not one
poll cycle later).

All nullable, no default: NULL = never reported by that consumer, which is the
honest pre-poll state and the permanent state for a consumer that does not
implement the field. `free_capacity()` falls back to the pure ledger arithmetic
when no fresh reading exists, so old consumers keep working unchanged.

Revision ID: b1c2d3e4f5a6
Revises: a9b0c1d2e3f4
Create Date: 2026-07-28 00:00:01.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade():
    print("Adding device-occupancy columns to vram_consumer")
    op.add_column("vram_consumer", sa.Column("device_used_bytes", sa.BigInteger(), nullable=True))
    op.add_column("vram_consumer", sa.Column("device_total_bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "vram_consumer", sa.Column("device_unattributed_bytes", sa.BigInteger(), nullable=True)
    )
    op.add_column("vram_consumer", sa.Column("device_reported_at", sa.BigInteger(), nullable=True))


def downgrade():
    op.drop_column("vram_consumer", "device_reported_at")
    op.drop_column("vram_consumer", "device_unattributed_bytes")
    op.drop_column("vram_consumer", "device_total_bytes")
    op.drop_column("vram_consumer", "device_used_bytes")
