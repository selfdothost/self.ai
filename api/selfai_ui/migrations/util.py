import os
import sys

from alembic import op
from sqlalchemy import inspect


def enabled_mods() -> list[str]:
    """The enabled-mod list, read the way the running app reads it.

    P0 of `context/treasuremaps/2026-07-30-mod-owned-alembic-revisions.md` in
    Data (self.ai#85). This body previously lived, byte-identical, inside each
    mod-owned revision -- `b7e1c0ffee42` and `d2e3f4a5b6c7` -- because there was
    nowhere shared to put it. Two mods in, the duplication was a live hazard:
    the third copy would either be another hand-copy or, worse, a naive
    `os.environ` read that rediscovers both failure modes below in production.
    It lives here now, in the module revisions already import from.

    Primary source is `selfai_ui.config.ENABLED_MODS.value` -- the same
    `PersistentConfig` the boot path consults, so a DB-persisted enablement
    override is honoured. But we consult it ONLY when `selfai_ui.config` is
    already fully imported in this process; we never trigger a *fresh* import of
    it from inside a running Alembic env. Importing that heavy module mid-upgrade
    has two failure modes, both observed while building the first of these
    migrations:

      * a circular import when config is imported for the first time during the
        upgrade (the test harness runs `upgrade head` *before* importing config,
        precisely to build the schema the config table lives in --
        `tests/conftest.py`); and
      * corruption of Alembic's shared proxy bookkeeping
        (`alembic.util.langhelpers` module-class proxies), which surfaced as a
        `KeyError` in `EnvironmentContext.__exit__._remove_proxy`.

    When config is not yet loaded there is no persisted override in play (the DB
    is fresh in that window), so the env-derived value equals what
    `ENABLED_MODS.value` would hold; we parse the same `ENABLED_MODS` env var
    config itself derives from. The two paths agree in that window by
    construction.

    Note this is also why production always takes the env branch: `config.py`
    calls `run_migrations()` at import, *above* where `ENABLED_MODS` is defined
    further down the same module. Correct today because
    `ENABLE_PERSISTENT_CONFIG=False` makes env the live value -- correct by
    coincidence of two settings, not by design (self.ai#82).
    """
    config_mod = sys.modules.get("selfai_ui.config")
    enabled = getattr(config_mod, "ENABLED_MODS", None)
    if enabled is not None:
        return list(enabled.value)
    return [m.strip() for m in os.environ.get("ENABLED_MODS", "").split(",") if m.strip()]


def mod_enabled(mod_id: str) -> bool:
    """True when `mod_id` is in the enabled list.

    The form a mod-owned revision should use, so no revision restates the
    membership test either.
    """
    return mod_id in enabled_mods()


def get_existing_tables():
    con = op.get_bind()
    inspector = inspect(con)
    tables = set(inspector.get_table_names())
    return tables


def get_revision_id():
    import uuid

    return str(uuid.uuid4()).replace("-", "")[:12]
