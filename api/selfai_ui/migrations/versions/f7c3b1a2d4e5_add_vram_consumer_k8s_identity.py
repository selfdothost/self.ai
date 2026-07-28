"""Add k8s pod identity columns to vram_consumer (R5 force-reap)

Adds the per-consumer Kubernetes pod identity carried alongside the existing
capacity/priority registration config (cavekit-gpu-lease-broker R5/AC3). Both
columns are nullable with no default so pre-existing rows (the R4 config-driven
self.llamolotl registration) simply carry NULL = not-yet-configured =
force-reap ineligible until a re-register (T-003) populates them from env. The
both-None state is the AC7 opt-in switch; nothing here enforces eligibility —
that lands with the escalation in T-005.

Revision ID: f7c3b1a2d4e5
Revises: e7d2a9c1f3b0
Create Date: 2026-07-24 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "f7c3b1a2d4e5"
down_revision = "e7d2a9c1f3b0"
branch_labels = None
depends_on = None


def upgrade():
    print("Adding k8s pod identity columns to vram_consumer")
    op.add_column("vram_consumer", sa.Column("k8s_namespace", sa.Text(), nullable=True))
    op.add_column("vram_consumer", sa.Column("k8s_pod_selector", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("vram_consumer", "k8s_pod_selector")
    op.drop_column("vram_consumer", "k8s_namespace")
