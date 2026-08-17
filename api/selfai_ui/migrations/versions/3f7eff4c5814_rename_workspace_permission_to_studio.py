"""Rekey stored group permissions from `workspace` to `studio` (self.ai#134)

Phase 0 of the Tokenization Studio programme renames the `workspace` permission
group to `studio`. The default blob in `config.py` moves with the code; STORED
GROUP BLOBS DO NOT, and that is the whole reason this revision exists.

`has_permission` denies on any missing level of the dotted key hierarchy. The
moment the call sites ask for `studio.models`, a group whose stored blob still
says `workspace` misses at the first level and falls through to the defaults --
every one of which is `False`, with no `USER_PERMISSIONS_*` env under
`manifests/` to override them. The result is that every non-admin loses all
Studio access at once, silently: no exception, no log line, the navigation
simply stops rendering.

There is a transitional dual-read in `utils/access_control.py` that catches
exactly this case, so a group missed by this migration still works. The two are
belt and braces on purpose: the fallback is what makes the rename safe to land
in either order, and this revision is what lets the fallback eventually be
removed.

THE BOTH-KEYS RULE, stated because it is a real decision and not an edge case.
A blob may hold BOTH `workspace` and `studio` -- most plausibly one written by
migrated code and then restored from an older backup, or a group edited during a
partial rollout. In that case `studio` WINS and `workspace` is dropped: the
`studio` half was written by code that knew about the rename, so it is the newer
intent.

The two dicts are deliberately NOT merged per-child. A union would resurrect a
permission an admin had explicitly turned off -- `workspace.tools: true` from
before, `studio.tools: false` set deliberately after, merged back to true. A
silent re-grant is worse than dropping a stale key, so the stale key is dropped.

Child keys and their boolean values are preserved exactly. No absent child is
added -- notably NOT `evaluations`, which is enforced at
`routers/evaluations.py` but missing from the default blob (self.ai#133). That
is a pre-existing defect and fixing it here would be this revision quietly
granting a permission nobody asked it to grant. Unrecognised children are kept
rather than filtered, since an admin may hold keys this code does not know.

`chat`, `features` and any `mods.*` sibling are untouched.

The blob is read/modified/written in Python over the SQLAlchemy connection
rather than with a JSON operator, because PostgreSQL and SQLite disagree on JSON
function names and this must run on both (SQLite is what the test suite and
`test_sqlite_migration_chain.py` use).

Idempotent: re-running finds no top-level `workspace` key and does nothing.
The down-path performs the exact inverse and is real, not a `pass`.

Revision ID: 3f7eff4c5814
Revises: d7e8f9a0b1c2
Create Date: 2026-08-12

NOTE ON THE PARENT REVISION: `d7e8f9a0b1c2` was verified as the SINGLE head by
walking `down_revision` across all 39 revisions on main at the time of writing.
self.ai#131's `b4c5d6e7f8a9` (self.ai!437) targets the same parent and is not
yet merged. WHICHEVER OF THE TWO LANDS SECOND MUST RE-POINT ITS `down_revision`
at the other -- two heads is a CrashLoop on the next roll under the strict boot
migration check (self.ai#82/#102), and CI does not catch it.
"""

import json
import logging

import sqlalchemy as sa
from alembic import op

revision = "3f7eff4c5814"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_OLD_KEY = "workspace"
_NEW_KEY = "studio"


def _rekey(blob: dict, source: str, target: str) -> "dict | None":
    """Move `blob[source]` to `blob[target]`, or return None if nothing to do.

    Returns a NEW dict so the caller can tell "changed" from "unchanged" without
    comparing deeply. `target` wins if both are present; see the both-keys rule
    in the module docstring.
    """
    if not isinstance(blob, dict) or source not in blob:
        return None

    updated = dict(blob)
    stale = updated.pop(source)
    if target not in updated:
        # the ordinary case: carry the value across untouched
        updated[target] = stale
    # else: `target` already present -- keep it, drop `stale`. Not merged.
    return updated


def _migrate(source: str, target: str) -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text('SELECT id, permissions FROM "group"')).fetchall()

    moved = 0
    both = 0
    for row in rows:
        raw = row[1]
        if raw is None:
            continue

        # PostgreSQL JSON columns hand back a parsed object; SQLite hands back
        # text. Accept either rather than assuming the dialect.
        if isinstance(raw, (str, bytes)):
            try:
                blob = json.loads(raw)
            except (ValueError, TypeError):
                log.warning(
                    "group %s has permissions that are not valid JSON; left untouched",
                    row[0],
                )
                continue
        else:
            blob = raw

        if not isinstance(blob, dict):
            continue
        if isinstance(blob.get(source), dict) and isinstance(blob.get(target), dict):
            both += 1

        updated = _rekey(blob, source, target)
        if updated is None:
            continue

        conn.execute(
            sa.text('UPDATE "group" SET permissions = :p WHERE id = :i'),
            {"p": json.dumps(updated), "i": row[0]},
        )
        moved += 1

    log.info(
        "studio rekey %s -> %s: %s group blob(s) updated, %s held both keys (kept %s, dropped %s)",
        source,
        target,
        moved,
        both,
        target,
        source,
    )


def upgrade() -> None:
    _migrate(_OLD_KEY, _NEW_KEY)


def downgrade() -> None:
    _migrate(_NEW_KEY, _OLD_KEY)
