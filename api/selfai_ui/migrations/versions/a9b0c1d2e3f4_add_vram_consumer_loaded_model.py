"""Add loaded-model columns to vram_consumer (Decision 6 / R4 eval-coexist datum)

Adds loaded_model_id + loaded_model_reported_at to vram_consumer. The R6
VRAM-state poller (T-012) relays self.llamolotl's currently-loaded model into
these so core's chat admission checkpoint (T-014+) can route an eval-window
request to the already-loaded model instead of triggering a competing load.
Both nullable, no default: NULL = not-yet-reported (never fabricated), exactly
like the pre-poll state the checkpoint must distinguish from "reported, none
loaded".

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-07-25 00:00:01.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a9b0c1d2e3f4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade():
    print("Adding loaded-model columns to vram_consumer")
    op.add_column("vram_consumer", sa.Column("loaded_model_id", sa.Text(), nullable=True))
    op.add_column(
        "vram_consumer", sa.Column("loaded_model_reported_at", sa.BigInteger(), nullable=True)
    )


def downgrade():
    op.drop_column("vram_consumer", "loaded_model_reported_at")
    op.drop_column("vram_consumer", "loaded_model_id")
