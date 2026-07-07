"""Create benchmark_config table and seed known benchmarks

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-04-05 00:00:00.000000

"""

import time
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column
from sqlalchemy import Text, Integer, BigInteger

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None

# Seed: all known benchmarks with a conservative 120-minute default.
# Admins can adjust max_duration_minutes per benchmark after running real evals.
SEED_BENCHMARKS = [
    # language-eval benchmarks
    ("hellaswag",       "language-eval"),
    ("mmlu",            "language-eval"),
    ("arc_easy",        "language-eval"),
    ("arc_challenge",   "language-eval"),
    ("winogrande",      "language-eval"),
    ("truthfulqa_mc",   "language-eval"),
    ("gsm8k",           "language-eval"),
    ("boolq",           "language-eval"),
    ("piqa",            "language-eval"),
    ("openbookqa",      "language-eval"),
    ("sciq",            "language-eval"),
    ("logiqa",          "language-eval"),
    ("mathqa",          "language-eval"),
    ("copa",            "language-eval"),
    # code-eval benchmarks
    ("humaneval",       "code-eval"),
    ("mbpp",            "code-eval"),
    ("apps",            "code-eval"),
    ("multiple_e",      "code-eval"),
    ("ds1000",          "code-eval"),
    ("humanevalpack",   "code-eval"),
    ("mbpp_plus",       "code-eval"),
    ("humaneval_plus",  "code-eval"),
]


def upgrade():
    print("Creating benchmark_config table")
    op.create_table(
        "benchmark_config",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("benchmark", sa.Text(), nullable=False),
        sa.Column("eval_type", sa.Text(), nullable=False),
        sa.Column("max_duration_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )

    print("Seeding benchmark_config with default benchmarks")
    bc = table(
        "benchmark_config",
        column("id", Text),
        column("benchmark", Text),
        column("eval_type", Text),
        column("max_duration_minutes", Integer),
        column("notes", Text),
        column("created_at", BigInteger),
        column("updated_at", BigInteger),
    )

    # Check which (benchmark, eval_type) pairs already exist (idempotent)
    conn = op.get_bind()
    existing = set(
        conn.execute(
            sa.text("SELECT benchmark || '|' || eval_type FROM benchmark_config")
        ).scalars()
    )

    now = int(time.time())
    rows = []
    for benchmark, eval_type in SEED_BENCHMARKS:
        key = f"{benchmark}|{eval_type}"
        if key not in existing:
            rows.append({
                "id": str(uuid.uuid4()),
                "benchmark": benchmark,
                "eval_type": eval_type,
                "max_duration_minutes": 120,
                "notes": None,
                "created_at": now,
                "updated_at": now,
            })

    if rows:
        op.bulk_insert(bc, rows)


def downgrade():
    op.drop_table("benchmark_config")
