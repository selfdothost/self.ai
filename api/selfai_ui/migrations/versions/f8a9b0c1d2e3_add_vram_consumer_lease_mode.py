"""Add lease_mode column to vram_consumer (Decision 6 / R1 exclusive-lease posture)

Adds the per-consumer lease mode ("shared" | "exclusive") the GPU lease broker
uses to represent a training/pipeline/curator window that has seized the whole
card. While an "exclusive" lease is active the broker denies every other
consumer's grant (T-002). NOT NULL with server_default "shared" so pre-existing
rows (the R4 self.llamolotl registration, any R5/R6 rows) backfill to the
ordinary shared posture — none of them is exclusive until a window explicitly
acquires one.

Revision ID: f8a9b0c1d2e3
Revises: f7c3b1a2d4e5
Create Date: 2026-07-25 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "f8a9b0c1d2e3"
down_revision = "f7c3b1a2d4e5"
branch_labels = None
depends_on = None


def upgrade():
    print("Adding lease_mode column to vram_consumer")
    op.add_column(
        "vram_consumer",
        sa.Column("lease_mode", sa.Text(), nullable=False, server_default="shared"),
    )


def downgrade():
    op.drop_column("vram_consumer", "lease_mode")
