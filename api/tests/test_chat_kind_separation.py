"""Phase 2 prerequisite X-3: `chat.kind` separates surfaces' histories.

A tokenization session must not appear in the chat sidebar, and an ordinary
chat must not appear in a surface's history. Plan: self.chat
`context/plans/build-site-tokenization-shell.md`, finding A.

WHY A COLUMN AND NOT `meta`. Both chat lists are paginated server-side, so the
filter has to be part of the query. Filtering on a JSON key needs a
dialect-specific expression (PostgreSQL and SQLite differ — the same reason
`3f7eff4c5814` rewrites blobs in Python); filtering in Python after the query
breaks `skip`/`limit` **silently**, returning short pages rather than raising.
`test_pagination_is_not_broken_by_the_filter` is the guard for that second
failure, because it is the one that looks fine in a screenshot.

The migration is driven through the REAL Alembic runner against a throwaway
SQLite DB, observing the resulting table rather than reading the revision's
source — the convention from `test_crew_mod_migration.py`.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

import selfai_ui.env

_API_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _API_ROOT / "selfai_ui" / "alembic.ini"
_MIGRATIONS = _API_ROOT / "selfai_ui" / "migrations"

_REVISION = "d6e7f8a9b0c1"
_REVISION_PARENT = "c5d6e7f8a9b0"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "kind.db"
    monkeypatch.setattr(selfai_ui.env, "DATABASE_URL", f"sqlite:///{db_path}")
    command.upgrade(_cfg(db_path), "head")
    return db_path


def _columns(db_path: Path, table: str = "chat") -> dict:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return {c["name"]: c for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


class TestMigration:
    def test_upgrade_adds_a_nullable_kind_column(self, migrated_db):
        cols = _columns(migrated_db)
        assert "kind" in cols, "the chat table should carry `kind` after upgrade"
        assert cols["kind"]["nullable"] is True, (
            "NULL is the ordinary-chat value; a NOT NULL column would need a "
            "backfill rewriting every chat row on a live database"
        )

    def test_existing_rows_are_valid_without_a_backfill(self, migrated_db):
        """The reason the column is nullable with no server default: a row
        written before it existed is already correct."""
        engine = create_engine(f"sqlite:///{migrated_db}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO chat (id, user_id, title, chat, created_at, updated_at) "
                        "VALUES ('c1', 'u1', 't', '{}', 1, 1)"
                    )
                )
                kind = conn.execute(sa.text("SELECT kind FROM chat WHERE id='c1'")).scalar()
            assert kind is None
        finally:
            engine.dispose()

    def test_downgrade_is_a_real_inverse(self, migrated_db):
        command.downgrade(_cfg(migrated_db), _REVISION_PARENT)
        assert "kind" not in _columns(migrated_db)
        command.upgrade(_cfg(migrated_db), _REVISION)
        assert "kind" in _columns(migrated_db)

    def test_the_chain_has_exactly_one_head(self):
        """A CYCLE fails loudly in CI; a FORK into two heads passes every job
        and only surfaces as a CrashLoop at boot, because `_assert_at_head`
        checks membership of `get_heads()` (self.ai#82, #102). Asserted here so
        a green pipeline is not mistaken for a linear chain."""
        from alembic.script import ScriptDirectory

        heads = ScriptDirectory.from_config(_cfg(Path("/tmp/unused.db"))).get_heads()
        assert len(heads) == 1, f"expected a single head, got {heads}"


class TestListSeparation:
    """`kind IS NULL` is ordinary chat; anything else belongs to another
    surface and must not leak into the chat lists."""

    def test_chat_lists_exclude_non_null_kinds(self, migrated_db):
        from selfai_ui.models.chats import Chat

        engine = create_engine(f"sqlite:///{migrated_db}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO chat (id, user_id, title, chat, created_at, updated_at, kind) "
                        "VALUES ('chat-1', 'u1', 'a', '{}', 1, 1, NULL),"
                        "       ('tok-1',  'u1', 'b', '{}', 2, 2, 'tokenization')"
                    )
                )
                session = sa.orm.Session(conn)
                chat_ids = [
                    r.id
                    for r in session.query(Chat)
                    .filter_by(user_id="u1")
                    .filter(Chat.kind.is_(None))
                    .all()
                ]
                tok_ids = [
                    r.id
                    for r in session.query(Chat)
                    .filter_by(user_id="u1")
                    .filter(Chat.kind == "tokenization")
                    .all()
                ]
            assert chat_ids == ["chat-1"], "a tokenization session leaked into the chat list"
            assert tok_ids == ["tok-1"], "an ordinary chat leaked into the tokenization history"
        finally:
            engine.dispose()

    def test_a_future_surface_is_excluded_by_default(self, migrated_db):
        """The filter is `kind IS NULL`, not `kind != 'tokenization'`. A surface
        added later must be invisible to the chat list without anyone having to
        remember to extend the filter."""
        from selfai_ui.models.chats import Chat

        engine = create_engine(f"sqlite:///{migrated_db}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO chat (id, user_id, title, chat, created_at, updated_at, kind) "
                        "VALUES ('future-1', 'u1', 'c', '{}', 3, 3, 'some-later-surface')"
                    )
                )
                session = sa.orm.Session(conn)
                ids = [
                    r.id
                    for r in session.query(Chat)
                    .filter_by(user_id="u1")
                    .filter(Chat.kind.is_(None))
                    .all()
                ]
            assert "future-1" not in ids
        finally:
            engine.dispose()

    def test_pagination_is_not_broken_by_the_filter(self, migrated_db):
        """THE SILENT FAILURE this column exists to avoid. Filtering after the
        query would return a SHORT page rather than an error: ask for 3 and get
        2, with no exception and nothing in a log. Filtering inside the query
        means limit counts only matching rows."""
        from selfai_ui.models.chats import Chat

        engine = create_engine(f"sqlite:///{migrated_db}")
        try:
            with engine.begin() as conn:
                rows = []
                for i in range(6):
                    kind = "NULL" if i % 2 == 0 else "'tokenization'"
                    rows.append(f"('p{i}', 'u1', 't', '{{}}', {i}, {i}, {kind})")
                conn.execute(
                    sa.text(
                        "INSERT INTO chat (id, user_id, title, chat, created_at, updated_at, kind) "
                        "VALUES " + ",".join(rows)
                    )
                )
                session = sa.orm.Session(conn)
                page = (
                    session.query(Chat)
                    .filter_by(user_id="u1")
                    .filter(Chat.kind.is_(None))
                    .order_by(Chat.updated_at.desc())
                    .limit(3)
                    .all()
                )
            assert len(page) == 3, (
                "a full page of ordinary chats was expected; a short page here means the "
                "filter is being applied after the limit"
            )
            assert all(r.kind is None for r in page)
        finally:
            engine.dispose()


# ── The wire: a client can declare a kind, but only a known one ─────────────
#
# X-3 added the column and the model parameter; without this the client had
# nothing to send to -- ChatForm carried only `chat`, so `insert_new_chat` was
# always called with the default kind and every session persisted as ordinary
# chat. The separation existed in the schema and nowhere on the wire.


class TestChatFormKind:
    def test_omitting_kind_is_ordinary_chat(self):
        from selfai_ui.models.chats import ChatForm

        assert ChatForm(chat={}).kind is None

    def test_a_known_kind_is_accepted(self):
        from selfai_ui.models.chats import ChatForm

        assert ChatForm(chat={}, kind="tokenization").kind == "tokenization"

    def test_an_unknown_kind_is_refused(self):
        """`kind` is a FILTER column. An unvalidated value would let a client
        write a kind that matches no surface's query, making the chat invisible
        in its own list with no way to recover it from the UI. Scoped to the
        caller's own rows, so this is robustness rather than a cross-user
        concern -- but a typo'd kind would orphan a conversation silently."""
        import pytest as _pytest
        from pydantic import ValidationError

        from selfai_ui.models.chats import ChatForm

        with _pytest.raises(ValidationError):
            ChatForm(chat={}, kind="tokenizaton")  # typo, deliberately
        with _pytest.raises(ValidationError):
            ChatForm(chat={}, kind="../../etc")

    def test_the_allowlist_is_the_single_source(self):
        from selfai_ui.models.chats import KNOWN_CHAT_KINDS, ChatForm

        for kind in KNOWN_CHAT_KINDS:
            assert ChatForm(chat={}, kind=kind).kind == kind
