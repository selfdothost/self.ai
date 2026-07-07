"""Move files to KB subdirectories

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-26

"""

import os
import shutil
import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column
from sqlalchemy import String, Text, JSON

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)


def upgrade():
    # Determine UPLOAD_DIR from environment or default
    data_dir = os.environ.get("DATA_DIR", "/app/backend/data")
    upload_dir = f"{data_dir}/uploads"

    if not os.path.exists(upload_dir):
        return

    # Define table references
    knowledge_table = table(
        "knowledge",
        column("id", Text),
        column("data", JSON),
    )
    file_table = table(
        "file",
        column("id", String),
        column("path", Text),
    )

    connection = op.get_bind()

    # Build knowledge_id -> [file_ids] map
    kb_rows = connection.execute(
        sa.select(knowledge_table.c.id, knowledge_table.c.data).where(
            knowledge_table.c.data.isnot(None)
        )
    ).fetchall()

    # Build reverse map: file_id -> first knowledge_id
    file_to_kb = {}
    for row in kb_rows:
        data = row.data if isinstance(row.data, dict) else {}
        file_ids = data.get("file_ids", [])
        for file_id in file_ids:
            if file_id not in file_to_kb:
                file_to_kb[file_id] = row.id
            else:
                log.warning(
                    f"File {file_id} appears in multiple KBs: "
                    f"{file_to_kb[file_id]} and {row.id}. "
                    f"Assigning to first KB: {file_to_kb[file_id]}"
                )

    if not file_to_kb:
        return

    # Get file paths for all files that need moving
    file_rows = connection.execute(
        sa.select(file_table.c.id, file_table.c.path).where(
            file_table.c.id.in_(list(file_to_kb.keys()))
        )
    ).fetchall()

    for file_row in file_rows:
        file_id = file_row.id
        current_path = file_row.path

        if not current_path:
            continue

        kb_id = file_to_kb[file_id]
        target_dir = f"{upload_dir}/{kb_id}"
        basename = os.path.basename(current_path)
        new_path = f"{target_dir}/{basename}"

        # Skip if already in the correct subdirectory
        if current_path == new_path or f"/{kb_id}/" in current_path:
            continue

        # Skip if already in some other subdirectory (shouldn't happen, but be safe)
        parent = os.path.dirname(current_path)
        if parent != upload_dir:
            log.warning(
                f"File {file_id} at {current_path} is already in a subdirectory, skipping"
            )
            continue

        # Move the file on disk
        if not os.path.isfile(current_path):
            log.warning(f"File {file_id} not found at {current_path}, skipping move")
            continue

        os.makedirs(target_dir, exist_ok=True)
        try:
            shutil.move(current_path, new_path)
        except Exception as e:
            log.error(f"Failed to move file {file_id}: {e}")
            continue

        # Update path in database
        connection.execute(
            file_table.update()
            .where(file_table.c.id == file_id)
            .values({"path": new_path})
        )
        log.info(f"Moved file {file_id} to {new_path}")


def downgrade():
    # Move files back to flat directory
    data_dir = os.environ.get("DATA_DIR", "/app/backend/data")
    upload_dir = f"{data_dir}/uploads"

    if not os.path.exists(upload_dir):
        return

    file_table = table(
        "file",
        column("id", String),
        column("path", Text),
    )

    connection = op.get_bind()

    file_rows = connection.execute(
        sa.select(file_table.c.id, file_table.c.path).where(
            file_table.c.path.isnot(None)
        )
    ).fetchall()

    for file_row in file_rows:
        current_path = file_row.path
        if not current_path:
            continue

        parent = os.path.dirname(current_path)
        if parent == upload_dir:
            continue  # Already in flat directory

        basename = os.path.basename(current_path)
        new_path = f"{upload_dir}/{basename}"

        if os.path.isfile(current_path):
            try:
                shutil.move(current_path, new_path)
                # Clean up empty subdirectory
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            except Exception as e:
                log.error(f"Failed to move file {file_row.id} back: {e}")
                continue

        connection.execute(
            file_table.update()
            .where(file_table.c.id == file_row.id)
            .values({"path": new_path})
        )
