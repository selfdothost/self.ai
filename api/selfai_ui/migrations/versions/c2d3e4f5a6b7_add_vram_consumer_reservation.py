"""Split the VRAM grant reservation out of held_bytes (self.ai#76)

`held_bytes` carried two irreconcilable meanings. `record_grant()` wrote a
RESERVATION into it — VRAM core has promised a consumer, before that consumer has
allocated anything — while the R6 poller's `heartbeat()` overwrote the same
column with a MEASUREMENT of what the consumer actually holds. A grant was
therefore erased by the next poll cycle (<=30s) if the consumer had not finished
allocating yet, and the following grant saw the same VRAM as free: the exact
over-grant the broker exists to prevent.

`reserved_bytes` + `reserved_at` give the promise its own column, so measurement
can overwrite measurement without touching it. A consumer's effective holding
becomes max(measured, live reservation), which is the conservative reading during
the window where the two disagree, and collapses back to the measurement once the
allocation lands and overtakes the promise.

`reserved_at` exists so a reservation cannot outlive its usefulness: a consumer
that is granted VRAM and then dies before allocating would otherwise hold
capacity out of the pool forever. It ages out via
VRAM_RESERVATION_TTL_SECONDS.

Both nullable, no default: NULL = no outstanding reservation, which is the state
of every existing row and of any consumer that has never been granted a lease.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-07-29 00:00:01.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    print("Adding grant-reservation columns to vram_consumer")
    op.add_column("vram_consumer", sa.Column("reserved_bytes", sa.BigInteger(), nullable=True))
    op.add_column("vram_consumer", sa.Column("reserved_at", sa.BigInteger(), nullable=True))


def downgrade():
    op.drop_column("vram_consumer", "reserved_at")
    op.drop_column("vram_consumer", "reserved_bytes")
