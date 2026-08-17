"""Add custom_eval table — the registry for admin-added evaluations

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-01 00:00:00.000000

self.ai#91. No existing table can represent an added evaluation:
`benchmark_config` is a timeout side-table with no insert path, and it cannot
say what an added eval points at, who added it, or whether it is built-in.

Note `sync_status` is NOT NULL with a server default of 'pending'. That is the
honest initial state — the row exists and the harness has not been told yet.
Every PVC in this tenant is ReadWriteOnce on local-path, so the API pod cannot
write into the harness's storage; definitions travel over the harness control
API, which means registration and delivery are separate facts that can drift.
Defaulting to 'synced' would encode an assumption that is false at the moment of
insert.
"""

import sqlalchemy as sa
from alembic import op

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "custom_eval",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("eval_type", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sync_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.BigInteger(), nullable=True),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("owner_name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
        # A name means one thing per harness. The two harnesses have separate
        # task namespaces, so the same name may legitimately exist in both.
        sa.UniqueConstraint("name", "eval_type", name="uq_custom_eval_name_eval_type"),
    )


def downgrade():
    op.drop_table("custom_eval")
