"""Add knowledge_file join table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-26

"""

import time
import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column
from sqlalchemy import String, Text, JSON, BigInteger

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)


def upgrade():
    # 1. Create the knowledge_file join table
    op.create_table(
        "knowledge_file",
        sa.Column("knowledge_id", sa.Text(), sa.ForeignKey("knowledge.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("file_id", sa.String(), sa.ForeignKey("file.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.BigInteger()),
    )

    # 2. Add index on file_id for reverse lookups
    op.create_index("ix_knowledge_file_file_id", "knowledge_file", ["file_id"])

    # 3. Migrate existing file_ids from knowledge.data JSON column
    knowledge_table = table(
        "knowledge",
        column("id", Text),
        column("data", JSON),
    )
    knowledge_file_table = table(
        "knowledge_file",
        column("knowledge_id", Text),
        column("file_id", String),
        column("created_at", BigInteger),
    )

    connection = op.get_bind()
    now = int(time.time())

    kb_rows = connection.execute(
        sa.select(knowledge_table.c.id, knowledge_table.c.data).where(
            knowledge_table.c.data.isnot(None)
        )
    ).fetchall()

    for row in kb_rows:
        data = row.data if isinstance(row.data, dict) else {}
        file_ids = data.get("file_ids", [])
        for file_id in file_ids:
            try:
                connection.execute(
                    knowledge_file_table.insert().values(
                        knowledge_id=row.id,
                        file_id=file_id,
                        created_at=now,
                    )
                )
            except Exception as e:
                log.warning(f"Skipping duplicate or invalid entry ({row.id}, {file_id}): {e}")


def downgrade():
    op.drop_index("ix_knowledge_file_file_id", table_name="knowledge_file")
    op.drop_table("knowledge_file")
