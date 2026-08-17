"""Re-seed benchmark_config with the harnesses' real task names

Revision ID: e1f2a3b4c5d6
Revises: c9d8e7f6a5b4
Create Date: 2026-07-31 00:00:00.000000

The original seed (d5e6f7a8b9c0) invented names that neither harness can
schedule, so `BenchmarkConfigs.get_by_benchmark()` — called from
`utils/gpu_queue.py` — never found a row for them and every affected benchmark
silently ran on the default timeout. The per-benchmark duration control was
therefore inert for most of what the picker offers (self.ai#90).

Seven of the twenty-two seeded rows named nothing runnable. They fall into two
kinds:

* **Three misspellings** with a clean 1:1 real counterpart — renamed in place so
  any admin-set duration/notes survive.
* **Four family prefixes** (`apps`, `ds1000`, `multiple_e`, `humanevalpack`)
  that are not tasks at all: the harness *generates* per-member task names from
  a parameter list. Each is expanded to its real members, carrying the family
  row's duration/notes onto every member so an admin's intent is preserved
  rather than dropped.

The expansion lists below mirror the harness generators exactly; each cites its
source. They are deliberately limited to *bounded, parameter-generated* families
— this migration does not attempt to enumerate the full catalog. That is the
point: the durable model is a sparse override table plus live task discovery
(self.ai#89 / #91), not an ever-staler seed list.
"""

import time
import uuid

import sqlalchemy as sa
from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


# ─── 1:1 renames ────────────────────────────────────────────────────────
#
# left = seeded name that matches no task, right = the real one.
#   truthfulqa_mc  — no such task; language_eval/tasks/truthfulqa/ ships
#                    truthfulqa_mc1 / truthfulqa_mc2 / truthfulqa_gen. The
#                    picker offers mc2, so that is the counterpart.
#   mbpp_plus      — real registry key is "mbppplus" (code_eval/tasks/__init__)
#   humaneval_plus — real key is "humanevalplus" (tasks/humanevalplus.py)
RENAMES = [
    ("truthfulqa_mc", "truthfulqa_mc2", "language-eval"),
    ("mbpp_plus", "mbppplus", "code-eval"),
    ("humaneval_plus", "humanevalplus", "code-eval"),
]


def _multiple_tasks() -> list[str]:
    """code_eval/tasks/multiple.py: LANGUAGES + create_all_tasks()."""
    languages = [
        "sh", "clj", "cpp", "cs", "d", "dart", "elixir", "go", "hs", "java",
        "js", "jl", "lua", "ml", "pl", "php", "r", "rkt", "rb", "rs",
        "scala", "swift", "ts",
    ]
    return [f"multiple-{lang}" for lang in languages]


def _ds1000_tasks() -> list[str]:
    """code_eval/tasks/ds1000.py: create_all_tasks() lowercases key and mode."""
    keys = ["all", "numpy", "pandas", "scipy", "matplotlib", "sklearn", "tensorflow", "pytorch"]
    modes = ["completion", "insertion"]
    return [f"ds1000-{key}-{mode}" for key in keys for mode in modes]


def _apps_tasks() -> list[str]:
    """code_eval/tasks/apps.py: LEVELS."""
    return [f"apps-{level}" for level in ("introductory", "interview", "competition")]


def _humanevalpack_tasks() -> list[str]:
    """code_eval/tasks/humanevalpack.py: create_all_tasks() — fix/explain/synthesize."""
    languages = ["python", "cpp", "js", "java", "go", "rust"]
    names = []
    for mode in ("tests", "docs"):
        names += [f"humanevalfix{mode}-{lang}" for lang in languages]
    for mode in ("describe", "synthesize"):
        names += [f"humanevalexplain{mode}-{lang}" for lang in languages]
    names += [f"humanevalsynthesize-{lang}" for lang in languages]
    return names


# family name -> (real member names, eval_type)
EXPANSIONS = {
    "multiple_e": (_multiple_tasks(), "code-eval"),
    "ds1000": (_ds1000_tasks(), "code-eval"),
    "apps": (_apps_tasks(), "code-eval"),
    "humanevalpack": (_humanevalpack_tasks(), "code-eval"),
}


def _existing_keys(conn) -> set:
    return set(conn.execute(sa.text("SELECT benchmark || '|' || eval_type FROM benchmark_config")).scalars())


def upgrade():
    conn = op.get_bind()
    now = int(time.time())

    # ── 1:1 renames ──
    for old, new, eval_type in RENAMES:
        existing = _existing_keys(conn)
        if f"{old}|{eval_type}" not in existing:
            continue
        if f"{new}|{eval_type}" in existing:
            # Target already present (re-run, or added by hand) — drop the dead
            # row rather than creating a duplicate benchmark.
            conn.execute(
                sa.text("DELETE FROM benchmark_config WHERE benchmark = :old AND eval_type = :t"),
                {"old": old, "t": eval_type},
            )
            print(f"benchmark_config: {new} already present, removed dead row {old}")
            continue
        conn.execute(
            sa.text(
                "UPDATE benchmark_config SET benchmark = :new, updated_at = :now "
                "WHERE benchmark = :old AND eval_type = :t"
            ),
            {"new": new, "old": old, "t": eval_type, "now": now},
        )
        print(f"benchmark_config: renamed {old} -> {new}")

    # ── family expansions ──
    for family, (members, eval_type) in EXPANSIONS.items():
        row = conn.execute(
            sa.text(
                "SELECT max_duration_minutes, notes FROM benchmark_config "
                "WHERE benchmark = :b AND eval_type = :t"
            ),
            {"b": family, "t": eval_type},
        ).first()
        if row is None:
            continue
        duration, notes = row[0], row[1]

        existing = _existing_keys(conn)
        new_rows = [
            {
                "id": str(uuid.uuid4()),
                "benchmark": name,
                "eval_type": eval_type,
                "max_duration_minutes": duration,
                "notes": notes,
                "created_at": now,
                "updated_at": now,
            }
            for name in members
            if f"{name}|{eval_type}" not in existing
        ]
        if new_rows:
            conn.execute(
                sa.text(
                    "INSERT INTO benchmark_config "
                    "(id, benchmark, eval_type, max_duration_minutes, notes, created_at, updated_at) "
                    "VALUES (:id, :benchmark, :eval_type, :max_duration_minutes, :notes, :created_at, :updated_at)"
                ),
                new_rows,
            )
        conn.execute(
            sa.text("DELETE FROM benchmark_config WHERE benchmark = :b AND eval_type = :t"),
            {"b": family, "t": eval_type},
        )
        print(
            f"benchmark_config: expanded {family} -> {len(new_rows)} real task "
            f"names (carrying max_duration_minutes={duration})"
        )


def downgrade():
    conn = op.get_bind()
    now = int(time.time())

    # Collapse each expansion back to its family name, keeping the duration the
    # members shared (they were all seeded from the family row).
    for family, (members, eval_type) in EXPANSIONS.items():
        row = conn.execute(
            sa.text(
                "SELECT max_duration_minutes, notes FROM benchmark_config "
                "WHERE benchmark = :b AND eval_type = :t"
            ),
            {"b": members[0], "t": eval_type},
        ).first()
        if row is None:
            continue
        conn.execute(
            sa.text("DELETE FROM benchmark_config WHERE benchmark IN :names AND eval_type = :t").bindparams(
                sa.bindparam("names", expanding=True)
            ),
            {"names": members, "t": eval_type},
        )
        conn.execute(
            sa.text(
                "INSERT INTO benchmark_config "
                "(id, benchmark, eval_type, max_duration_minutes, notes, created_at, updated_at) "
                "VALUES (:id, :b, :t, :d, :n, :now, :now)"
            ),
            {"id": str(uuid.uuid4()), "b": family, "t": eval_type, "d": row[0], "n": row[1], "now": now},
        )

    for old, new, eval_type in RENAMES:
        conn.execute(
            sa.text(
                "UPDATE benchmark_config SET benchmark = :old, updated_at = :now "
                "WHERE benchmark = :new AND eval_type = :t"
            ),
            {"old": old, "new": new, "t": eval_type, "now": now},
        )
