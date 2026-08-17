"""Add last_observed_at to vram_consumer (self.ai#105, never-polled reap guard)

`last_reported_at` cannot answer "have we ever actually heard from this
consumer?", because ``register()`` writes it. Registration is CORE asserting a
consumer exists from its own configuration — not the consumer reporting
anything. So a consumer that is registered at boot and never successfully polled
looks, to every staleness reader, exactly like one that reported healthily and
then went quiet: both have a `last_reported_at` that ages past
STALE_THRESHOLD_SECONDS.

Those two are not the same, and conflating them arms force-reap on a deployment
gap. The concrete case is self.ai#105: rolling core before self.curator serves
`/api/system/vram-state` registers curator, fails every poll, and about two
minutes later leaves it stale AND force-reap eligible — so a grant under
pressure can delete a pod that was never even reachable, potentially mid-run.

`last_observed_at` records only GENUINE observations — a heartbeat (which is
what the R6 poller drives) or a consumer-confirmed release. Never registration,
never a core-side grant/reservation write. NULL therefore means precisely "no
consumer-originated evidence has ever arrived", which is the condition the R5
force-reap gate now refuses to act on.

Nullable with no default and no backfill, deliberately: existing rows correctly
read as never-observed until their next real heartbeat, which the poller
delivers within one cycle for any consumer that is actually alive. Backfilling
from `last_reported_at` would manufacture exactly the evidence this column
exists to distinguish.

Revision ID: d7e8f9a0b1c2
Revises: f3a7c2d1e8b9
Create Date: 2026-08-03 00:00:01.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "f3a7c2d1e8b9"
branch_labels = None
depends_on = None


def upgrade():
    print("Adding last_observed_at to vram_consumer")
    op.add_column(
        "vram_consumer", sa.Column("last_observed_at", sa.BigInteger(), nullable=True)
    )


def downgrade():
    op.drop_column("vram_consumer", "last_observed_at")
