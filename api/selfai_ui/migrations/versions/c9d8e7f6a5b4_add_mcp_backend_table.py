"""Create mcp_backend table (dynamically-registered MCP front-door backends)

Phase 1 of the front door read its name -> URL map from MCP_PROXY_BACKENDS at
import time, so adding a backend meant a manifest edit and a pod restart. This
table is the durable half: a registrar adds a backend over the API and it serves
on the next request.

GitOps entries stay in the env and are NOT migrated in here — they are the
reserved, immutable set that a registration can never shadow, and keeping them
in two places would make "which one wins" a question rather than a rule.

Revision ID: c9d8e7f6a5b4
Revises: d2e3f4a5b6c7
Create Date: 2026-07-30 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c9d8e7f6a5b4"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    print("Creating mcp_backend table")
    op.create_table(
        "mcp_backend",
        # The path segment it serves at, /mcp/{name}.
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        # The self.ai user who registered it; only they may update or delete it.
        # Not nullable: an unowned row would be editable by anyone, which is the
        # hijack path this column exists to close (self.ai#79's lesson).
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("owner_name", sa.Text()),
        sa.Column("created_at", sa.BigInteger()),
        sa.Column("updated_at", sa.BigInteger()),
    )


def downgrade():
    op.drop_table("mcp_backend")
