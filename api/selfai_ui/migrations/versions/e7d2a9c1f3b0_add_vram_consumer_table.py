"""Create vram_consumer table (VRAM lease registry)

Persisting the lease registry is what makes it survive a core process restart
(R1-AC8) while consumers keep running independently — deliberately unlike the
in-memory `_processes`/`_app_state` dict pattern `gpu_queue.py` uses elsewhere.

Revision ID: e7d2a9c1f3b0
Revises: b7e1c0ffee42
Create Date: 2026-07-24 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "e7d2a9c1f3b0"
down_revision = "b7e1c0ffee42"
branch_labels = None
depends_on = None


def upgrade():
    print("Creating vram_consumer table")
    op.create_table(
        "vram_consumer",
        sa.Column("consumer_id", sa.Text(), primary_key=True),
        sa.Column("total_capacity_bytes", sa.BigInteger(), nullable=False),
        sa.Column("held_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_state", sa.Text(), nullable=False, server_default="steady"),
        sa.Column("last_reported_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )


def downgrade():
    op.drop_table("vram_consumer")
