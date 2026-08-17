import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Generic, Optional, TypeVar
from urllib.parse import urlparse

import requests
from pydantic import BaseModel
from sqlalchemy import JSON, Column, DateTime, Integer, func

from selfai_ui.env import (
    BACKEND_DIR,
    DATA_DIR,
    DATABASE_URL,
    ENV,
    FRONTEND_BUILD_DIR,
    OFFLINE_MODE,
    SELFAI_UI_DIR,
    WEBUI_AUTH,
    WEBUI_FAVICON_URL,
    WEBUI_NAME,
    log,
)
from selfai_ui.internal.db import Base, get_db


class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find("/health") == -1


# Filter out /endpoint
logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

####################################
# Config helpers
####################################


class MigrationError(RuntimeError):
    """The schema is not at head, so this process must not serve.

    Its own type so the boot failure is greppable and cannot be confused with
    an unrelated import error in the same module.
    """


def _alembic_config():
    """The runner config, built the one way this codebase builds it."""
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(SELFAI_UI_DIR / "alembic.ini")
    # Set the script location dynamically
    cfg.set_main_option("script_location", str(SELFAI_UI_DIR / "migrations"))
    return cfg


def _stamped_revisions(engine) -> list[str]:
    """Every revision id currently recorded in `alembic_version`.

    A list, not a scalar: once mods own branch-labelled lineages the table holds
    one row per branch. Returns empty for a database that has never been
    migrated -- the table simply does not exist yet.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    with engine.connect() as connection:
        if not sa_inspect(connection).has_table("alembic_version"):
            return []
        return [row[0] for row in connection.execute(text("select version_num from alembic_version"))]


def _prune_orphaned_mod_stamps(alembic_cfg) -> list[str]:
    """Drop `alembic_version` rows naming revisions nothing can resolve.

    P1 slice 1 of the mod-owned-revisions work (self.ai#85), and the prerequisite
    for the rest of it. **Inert until mod locations are actually configured** --
    with only core's directory in play every stamp resolves, so this finds
    nothing and changes nothing.

    WHY IT HAS TO EXIST BEFORE MODS OWN REVISIONS. Verified on self.ai#86 against
    the pinned Alembic: a stamped revision whose file is gone makes
    `upgrade heads` raise `Can't locate revision identified by '<rev>'`, and

      * branch targeting does NOT contain it -- `upgrade <branch>@head` fails
        identically, because Alembic resolves the entire revision map before
        doing anything; and
      * core's own migrations are blocked too, so the whole tenant's schema
        freezes, not just the removed mod's.

    Under the strict boot from self.ai#82 that is a refusal to serve. So without
    this, `rm -rf` on a mod directory would be a tenant outage recoverable only
    by hand-editing `alembic_version` in production.

    WHY PRUNING IS SAFE, which is the part worth checking rather than assuming.
    An unresolvable stamp cannot be a core revision:

      * core's revisions ship inside the image, so its directory is whole by
        construction;
      * a missing revision in the MIDDLE of core's chain leaves a dangling
        `down_revision`, and `ScriptDirectory.from_config` raises before this
        function is reached; and
      * a missing revision at the END of core's chain would not dangle -- but a
        core revision file disappearing fails CI outright, via the `removed`
        assertion in `tests/test_toolspec_roundtrip.py`
        (`test_no_alembic_revision_was_introduced_for_toolspec`).

    So a stamp that resolves to nothing came from a mod location that is no
    longer installed. Note this is ownership by *inference*, not by bookkeeping:
    `Script.path` can tell us which location a revision came from only while its
    file still exists, which is exactly not the case for an orphan. The
    inference above is what makes a `(revision -> mod)` tracking table
    unnecessary.

    Returns the pruned revision ids. Logs at ERROR, not INFO: deleting a row
    from `alembic_version` is not routine, and the operator should see it even
    though the boot succeeds.
    """
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    engine = create_engine(DATABASE_URL)
    try:
        stamped = _stamped_revisions(engine)
        if not stamped:
            return []

        # Building the ScriptDirectory is itself the dangling-chain check.
        script = ScriptDirectory.from_config(alembic_cfg)
        known = {revision.revision for revision in script.walk_revisions()}

        orphans = [rev for rev in stamped if rev not in known]
        if not orphans:
            return []

        log.error(
            "migrations: %d stamped revision(s) resolve to nothing and will be pruned "
            "from alembic_version: %s. This is expected when a mod has been uninstalled; "
            "if it is not, a revision file has gone missing and the image is wrong.",
            len(orphans),
            sorted(orphans),
        )
        with engine.begin() as connection:
            for revision in orphans:
                connection.execute(
                    text("delete from alembic_version where version_num = :rev"),
                    {"rev": revision},
                )
        return sorted(orphans)
    finally:
        engine.dispose()


def _assert_at_head(alembic_cfg) -> None:
    """Confirm the database really is at head after the upgrade returned.

    `command.upgrade` returning is not the same fact as "the schema is at
    head": a revision that no-ops on its own gate, an interrupted run, or a
    database pointed somewhere other than the one just migrated all leave a
    process that believes it migrated. This reads the answer back out of the
    database instead of inferring it from control flow -- the same posture the
    mods work takes about verifying through the surface rather than the code
    path that produced it.
    """
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    heads = set(ScriptDirectory.from_config(alembic_cfg).get_heads())

    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

    if current not in heads:
        raise MigrationError(
            f"database is at revision {current!r}, expected one of {sorted(heads)!r}. "
            f"Refusing to serve on a schema that is not at head."
        )

    log.info("migrations: database at head (%s)", current)


def run_migrations():
    """Upgrade to head at boot, and fail the boot if that does not happen.

    self.ai#82. This used to swallow every exception into
    `print(f"Error: {e}")` and continue, so a failed migration produced a pod
    that was Running, Ready, and serving against an absent or half-built
    schema. Nothing downstream checked, and stdout is not a signal anyone
    monitors -- the symptom surfaced instead as whatever degraded path the
    consumer happened to have.

    That is not hypothetical. `benchmark_config` never got created in
    production, so `BenchmarkConfigs.get_by_benchmark()` returned None for
    every benchmark, so `_fits_in_window()` took its `if not cfg: return True`
    branch, and **every GPU-window fit check passed unconditionally** on a
    single shared 4090 -- a safety gate silently degraded to always-yes, with a
    green pod and no alert anywhere (self.chat#26).

    So a failure here is now a boot failure. A CrashLoopBackOff is a signal an
    operator already watches; a Ready pod serving a broken schema is not.

    **There is deliberately no opt-out flag.** A `MIGRATIONS_STRICT=false` knob
    would be set once during an incident and never unset, which recreates
    exactly the silent degradation this replaces. A genuine need for one should
    arrive as its own reviewed change with a stated reason.

    Consequence worth knowing: a database that is unreachable at boot now
    crash-loops the pod instead of starting it broken. That is the intended
    trade -- Kubernetes retries, and a pod that cannot reach its database has
    nothing useful to serve.

    Orphaned stamps are pruned first (self.ai#85 P1 slice 1). That step is inert
    today and stays inert until mod locations are configured; it exists before
    the change that creates the hazard rather than after it. See
    `_prune_orphaned_mod_stamps`.
    """
    log.info("migrations: upgrading to head")
    try:
        from alembic import command

        alembic_cfg = _alembic_config()
        _prune_orphaned_mod_stamps(alembic_cfg)
        command.upgrade(alembic_cfg, "head")
    except Exception as exc:
        log.exception("migrations: upgrade to head FAILED -- refusing to serve")
        raise MigrationError(f"alembic upgrade to head failed: {exc}") from exc

    _assert_at_head(alembic_cfg)


run_migrations()


class Config(Base):
    __tablename__ = "config"

    id = Column(Integer, primary_key=True)
    data = Column(JSON, nullable=False)
    version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=True, onupdate=func.now())


def load_json_config():
    with open(f"{DATA_DIR}/config.json", "r") as file:
        return json.load(file)


def save_to_db(data):
    with get_db() as db:
        existing_config = db.query(Config).first()
        if not existing_config:
            new_config = Config(data=data, version=0)
            db.add(new_config)
        else:
            existing_config.data = data
            existing_config.updated_at = datetime.now()
            db.add(existing_config)
        db.commit()


def reset_config():
    with get_db() as db:
        db.query(Config).delete()
        db.commit()


# When initializing, check if config.json exists and migrate it to the database
if os.path.exists(f"{DATA_DIR}/config.json"):
    data = load_json_config()
    save_to_db(data)
    os.rename(f"{DATA_DIR}/config.json", f"{DATA_DIR}/old_config.json")

DEFAULT_CONFIG = {
    "version": 0,
    "ui": {
        "default_locale": "",
        "prompt_suggestions": [
            {
                "title": [
                    "Help me study",
                    "vocabulary for a college entrance exam",
                ],
                "content": (
                    "Help me study vocabulary: write a sentence for me to fill in "
                    "the blank, and I'll try to pick the correct option."
                ),
            },
            {
                "title": [
                    "Give me ideas",
                    "for what to do with my kids' art",
                ],
                "content": (
                    "What are 5 creative things I could do with my kids' art? "
                    "I don't want to throw them away, but it's also so much clutter."
                ),
            },
            {
                "title": ["Tell me a fun fact", "about the Roman Empire"],
                "content": "Tell me a random fun fact about the Roman Empire",
            },
            {
                "title": [
                    "Show me a code snippet",
                    "of a website's sticky header",
                ],
                "content": "Show me a code snippet of a website's sticky header in CSS and JavaScript.",
            },
            {
                "title": [
                    "Explain options trading",
                    "if I'm familiar with buying and selling stocks",
                ],
                "content": "Explain options trading in simple terms if I'm familiar with buying and selling stocks.",
            },
            {
                "title": ["Overcome procrastination", "give me tips"],
                "content": (
                    "Could you start by asking me about instances when I "
                    "procrastinate the most and then give me some suggestions "
                    "to overcome it?"
                ),
            },
            {
                "title": [
                    "Grammar check",
                    "rewrite it for better readability ",
                ],
                "content": (
                    "Check the following sentence for grammar and clarity: "
                    '"[sentence]". Rewrite it for better readability while '
                    "maintaining its original meaning."
                ),
            },
        ],
    },
}


def get_config():
    with get_db() as db:
        config_entry = db.query(Config).order_by(Config.id.desc()).first()
        return config_entry.data if config_entry else DEFAULT_CONFIG


CONFIG_DATA = get_config()


def get_config_value(config_path: str):
    path_parts = config_path.split(".")
    cur_config = CONFIG_DATA
    for key in path_parts:
        if key in cur_config:
            cur_config = cur_config[key]
        else:
            return None
    return cur_config


PERSISTENT_CONFIG_REGISTRY = []


def save_config(config):
    global CONFIG_DATA
    global PERSISTENT_CONFIG_REGISTRY
    try:
        save_to_db(config)
        CONFIG_DATA = config

        # Trigger updates on all registered PersistentConfig entries
        for config_item in PERSISTENT_CONFIG_REGISTRY:
            config_item.update()
    except Exception as e:
        log.exception(e)
        return False
    return True


T = TypeVar("T")

# When False, env is authoritative: PersistentConfig ignores the DB-saved value
# and always uses the env value (GitOps — config lives in manifests/vault, not
# the DB). Backported to this fork; default True preserves upstream behaviour.
ENABLE_PERSISTENT_CONFIG = os.environ.get("ENABLE_PERSISTENT_CONFIG", "True").lower() == "true"


class PersistentConfig(Generic[T]):
    def __init__(self, env_name: str, config_path: str, env_value: T):
        self.env_name = env_name
        self.config_path = config_path
        self.env_value = env_value
        self.config_value = get_config_value(config_path) if ENABLE_PERSISTENT_CONFIG else None
        if self.config_value is not None:
            log.info(f"'{env_name}' loaded from the latest database entry")
            self.value = self.config_value
        else:
            self.value = env_value

        PERSISTENT_CONFIG_REGISTRY.append(self)

    def __str__(self):
        return str(self.value)

    @property
    def __dict__(self):
        raise TypeError("PersistentConfig object cannot be converted to dict, use config_get or .value instead.")

    def __getattribute__(self, item):
        if item == "__dict__":
            raise TypeError("PersistentConfig object cannot be converted to dict, use config_get or .value instead.")
        return super().__getattribute__(item)

    def update(self):
        """Adopt the database's value for this entry.

        Gated on ENABLE_PERSISTENT_CONFIG (self.ai#95). The gate in __init__
        only covers **boot**; this method is called by `save_config()` for every
        registered entry, which is reachable from `POST /configs/import`. Left
        ungated, an admin uploading a JSON file could override env- and
        vault-sourced configuration at runtime, and the override would then
        silently disappear on the next pod restart when boot-gating took back
        over. With the flag off, env is authoritative — including here.
        """
        if not ENABLE_PERSISTENT_CONFIG:
            return
        new_value = get_config_value(self.config_path)
        if new_value is not None:
            self.value = new_value
            log.info(f"Updated {self.env_name} to new value {self.value}")

    def save(self):
        """Persist this entry's value to the config table.

        Gated on ENABLE_PERSISTENT_CONFIG (self.ai#95). With the flag off,
        `self.value` *is* the env/vault value and the database copy is ignored
        on read — so writing it is pure leak for zero benefit. Every save()
        used to widen a secret's blast radius from "OpenBao + pod env" to
        "OpenBao + pod env + a Postgres table + every JSON any admin ever
        exported".
        """
        if not ENABLE_PERSISTENT_CONFIG:
            log.debug(f"Not saving '{self.env_name}': ENABLE_PERSISTENT_CONFIG is False, env is authoritative")
            return
        log.info(f"Saving '{self.env_name}' to the database")
        path_parts = self.config_path.split(".")
        sub_config = CONFIG_DATA
        for key in path_parts[:-1]:
            if key not in sub_config:
                sub_config[key] = {}
            sub_config = sub_config[key]
        sub_config[path_parts[-1]] = self.value
        save_to_db(CONFIG_DATA)
        self.config_value = self.value


class AppConfig:
    _state: dict[str, PersistentConfig]

    def __init__(self):
        super().__setattr__("_state", {})

    def __setattr__(self, key, value):
        if isinstance(value, PersistentConfig):
            self._state[key] = value
        else:
            self._state[key].value = value
            self._state[key].save()

    def __getattr__(self, key):
        return self._state[key].value


####################################
# WEBUI_AUTH (Required for security)
####################################

ENABLE_API_KEY = PersistentConfig(
    "ENABLE_API_KEY",
    "auth.api_key.enable",
    os.environ.get("ENABLE_API_KEY", "True").lower() == "true",
)

ENABLE_API_KEY_ENDPOINT_RESTRICTIONS = PersistentConfig(
    "ENABLE_API_KEY_ENDPOINT_RESTRICTIONS",
    "auth.api_key.endpoint_restrictions",
    os.environ.get("ENABLE_API_KEY_ENDPOINT_RESTRICTIONS", "False").lower() == "true",
)

API_KEY_ALLOWED_ENDPOINTS = PersistentConfig(
    "API_KEY_ALLOWED_ENDPOINTS",
    "auth.api_key.allowed_endpoints",
    os.environ.get("API_KEY_ALLOWED_ENDPOINTS", ""),
)


JWT_EXPIRES_IN = PersistentConfig("JWT_EXPIRES_IN", "auth.jwt_expiry", os.environ.get("JWT_EXPIRES_IN", "-1"))

####################################
# OAuth config
####################################

ENABLE_OAUTH_SIGNUP = PersistentConfig(
    "ENABLE_OAUTH_SIGNUP",
    "oauth.enable_signup",
    os.environ.get("ENABLE_OAUTH_SIGNUP", "False").lower() == "true",
)

OAUTH_MERGE_ACCOUNTS_BY_EMAIL = PersistentConfig(
    "OAUTH_MERGE_ACCOUNTS_BY_EMAIL",
    "oauth.merge_accounts_by_email",
    os.environ.get("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "False").lower() == "true",
)

OAUTH_PROVIDERS = {}

GOOGLE_CLIENT_ID = PersistentConfig(
    "GOOGLE_CLIENT_ID",
    "oauth.google.client_id",
    os.environ.get("GOOGLE_CLIENT_ID", ""),
)

GOOGLE_CLIENT_SECRET = PersistentConfig(
    "GOOGLE_CLIENT_SECRET",
    "oauth.google.client_secret",
    os.environ.get("GOOGLE_CLIENT_SECRET", ""),
)


GOOGLE_OAUTH_SCOPE = PersistentConfig(
    "GOOGLE_OAUTH_SCOPE",
    "oauth.google.scope",
    os.environ.get("GOOGLE_OAUTH_SCOPE", "openid email profile"),
)

GOOGLE_REDIRECT_URI = PersistentConfig(
    "GOOGLE_REDIRECT_URI",
    "oauth.google.redirect_uri",
    os.environ.get("GOOGLE_REDIRECT_URI", ""),
)

MICROSOFT_CLIENT_ID = PersistentConfig(
    "MICROSOFT_CLIENT_ID",
    "oauth.microsoft.client_id",
    os.environ.get("MICROSOFT_CLIENT_ID", ""),
)

MICROSOFT_CLIENT_SECRET = PersistentConfig(
    "MICROSOFT_CLIENT_SECRET",
    "oauth.microsoft.client_secret",
    os.environ.get("MICROSOFT_CLIENT_SECRET", ""),
)

MICROSOFT_CLIENT_TENANT_ID = PersistentConfig(
    "MICROSOFT_CLIENT_TENANT_ID",
    "oauth.microsoft.tenant_id",
    os.environ.get("MICROSOFT_CLIENT_TENANT_ID", ""),
)

MICROSOFT_OAUTH_SCOPE = PersistentConfig(
    "MICROSOFT_OAUTH_SCOPE",
    "oauth.microsoft.scope",
    os.environ.get("MICROSOFT_OAUTH_SCOPE", "openid email profile"),
)

MICROSOFT_REDIRECT_URI = PersistentConfig(
    "MICROSOFT_REDIRECT_URI",
    "oauth.microsoft.redirect_uri",
    os.environ.get("MICROSOFT_REDIRECT_URI", ""),
)

OAUTH_CLIENT_ID = PersistentConfig(
    "OAUTH_CLIENT_ID",
    "oauth.oidc.client_id",
    os.environ.get("OAUTH_CLIENT_ID", ""),
)

OAUTH_CLIENT_SECRET = PersistentConfig(
    "OAUTH_CLIENT_SECRET",
    "oauth.oidc.client_secret",
    os.environ.get("OAUTH_CLIENT_SECRET", ""),
)

OPENID_PROVIDER_URL = PersistentConfig(
    "OPENID_PROVIDER_URL",
    "oauth.oidc.provider_url",
    os.environ.get("OPENID_PROVIDER_URL", ""),
)

OPENID_REDIRECT_URI = PersistentConfig(
    "OPENID_REDIRECT_URI",
    "oauth.oidc.redirect_uri",
    os.environ.get("OPENID_REDIRECT_URI", ""),
)

OAUTH_SCOPES = PersistentConfig(
    "OAUTH_SCOPES",
    "oauth.oidc.scopes",
    os.environ.get("OAUTH_SCOPES", "openid email profile"),
)

OAUTH_PROVIDER_NAME = PersistentConfig(
    "OAUTH_PROVIDER_NAME",
    "oauth.oidc.provider_name",
    os.environ.get("OAUTH_PROVIDER_NAME", "SSO"),
)

OAUTH_USERNAME_CLAIM = PersistentConfig(
    "OAUTH_USERNAME_CLAIM",
    "oauth.oidc.username_claim",
    os.environ.get("OAUTH_USERNAME_CLAIM", "name"),
)

OAUTH_PICTURE_CLAIM = PersistentConfig(
    "OAUTH_PICTURE_CLAIM",
    "oauth.oidc.avatar_claim",
    os.environ.get("OAUTH_PICTURE_CLAIM", "picture"),
)

OAUTH_EMAIL_CLAIM = PersistentConfig(
    "OAUTH_EMAIL_CLAIM",
    "oauth.oidc.email_claim",
    os.environ.get("OAUTH_EMAIL_CLAIM", "email"),
)

OAUTH_GROUPS_CLAIM = PersistentConfig(
    "OAUTH_GROUPS_CLAIM",
    "oauth.oidc.group_claim",
    os.environ.get("OAUTH_GROUP_CLAIM", "groups"),
)

ENABLE_OAUTH_ROLE_MANAGEMENT = PersistentConfig(
    "ENABLE_OAUTH_ROLE_MANAGEMENT",
    "oauth.enable_role_mapping",
    os.environ.get("ENABLE_OAUTH_ROLE_MANAGEMENT", "False").lower() == "true",
)

ENABLE_OAUTH_GROUP_MANAGEMENT = PersistentConfig(
    "ENABLE_OAUTH_GROUP_MANAGEMENT",
    "oauth.enable_group_mapping",
    os.environ.get("ENABLE_OAUTH_GROUP_MANAGEMENT", "False").lower() == "true",
)

OAUTH_ROLES_CLAIM = PersistentConfig(
    "OAUTH_ROLES_CLAIM",
    "oauth.roles_claim",
    os.environ.get("OAUTH_ROLES_CLAIM", "roles"),
)

OAUTH_ALLOWED_ROLES = PersistentConfig(
    "OAUTH_ALLOWED_ROLES",
    "oauth.allowed_roles",
    [role.strip() for role in os.environ.get("OAUTH_ALLOWED_ROLES", "user,admin").split(",")],
)

OAUTH_ADMIN_ROLES = PersistentConfig(
    "OAUTH_ADMIN_ROLES",
    "oauth.admin_roles",
    [role.strip() for role in os.environ.get("OAUTH_ADMIN_ROLES", "admin").split(",")],
)

OAUTH_ALLOWED_DOMAINS = PersistentConfig(
    "OAUTH_ALLOWED_DOMAINS",
    "oauth.allowed_domains",
    [domain.strip() for domain in os.environ.get("OAUTH_ALLOWED_DOMAINS", "*").split(",")],
)


def load_oauth_providers():
    OAUTH_PROVIDERS.clear()
    if GOOGLE_CLIENT_ID.value and GOOGLE_CLIENT_SECRET.value:
        OAUTH_PROVIDERS["google"] = {
            "client_id": GOOGLE_CLIENT_ID.value,
            "client_secret": GOOGLE_CLIENT_SECRET.value,
            "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration",
            "scope": GOOGLE_OAUTH_SCOPE.value,
            "redirect_uri": GOOGLE_REDIRECT_URI.value,
        }

    if MICROSOFT_CLIENT_ID.value and MICROSOFT_CLIENT_SECRET.value and MICROSOFT_CLIENT_TENANT_ID.value:
        OAUTH_PROVIDERS["microsoft"] = {
            "client_id": MICROSOFT_CLIENT_ID.value,
            "client_secret": MICROSOFT_CLIENT_SECRET.value,
            "server_metadata_url": f"https://login.microsoftonline.com/{MICROSOFT_CLIENT_TENANT_ID.value}/v2.0/.well-known/openid-configuration",
            "scope": MICROSOFT_OAUTH_SCOPE.value,
            "redirect_uri": MICROSOFT_REDIRECT_URI.value,
        }

    if OAUTH_CLIENT_ID.value and OAUTH_CLIENT_SECRET.value and OPENID_PROVIDER_URL.value:
        OAUTH_PROVIDERS["oidc"] = {
            "client_id": OAUTH_CLIENT_ID.value,
            "client_secret": OAUTH_CLIENT_SECRET.value,
            "server_metadata_url": OPENID_PROVIDER_URL.value,
            "scope": OAUTH_SCOPES.value,
            "name": OAUTH_PROVIDER_NAME.value,
            "redirect_uri": OPENID_REDIRECT_URI.value,
        }


load_oauth_providers()

####################################
# Static DIR
####################################

STATIC_DIR = Path(os.getenv("STATIC_DIR", SELFAI_UI_DIR / "static")).resolve()

frontend_favicon = FRONTEND_BUILD_DIR / "static" / "favicon.png"

if frontend_favicon.exists():
    try:
        shutil.copyfile(frontend_favicon, STATIC_DIR / "favicon.png")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
else:
    logging.warning(f"Frontend favicon not found at {frontend_favicon}")

frontend_splash = FRONTEND_BUILD_DIR / "static" / "splash.png"

if frontend_splash.exists():
    try:
        shutil.copyfile(frontend_splash, STATIC_DIR / "splash.png")
    except Exception as e:
        logging.error(f"An error occurred: {e}")
else:
    logging.warning(f"Frontend splash not found at {frontend_splash}")


####################################
# CUSTOM_NAME
####################################

CUSTOM_NAME = os.environ.get("CUSTOM_NAME", "")

if CUSTOM_NAME:
    try:
        r = requests.get(f"https://api.selfdotai.com/api/v1/custom/{CUSTOM_NAME}")
        data = r.json()
        if r.ok:
            if "logo" in data:
                # WEBUI_URL (the PersistentConfig) isn't defined until later
                # in this module -- use the same env lookup it wraps, since
                # referencing the name here raised NameError (F821) whenever
                # CUSTOM_NAME was actually set.
                _webui_url = os.environ.get("WEBUI_URL", "http://localhost:3000")
                WEBUI_FAVICON_URL = url = f"{_webui_url}/static/favicon.ico" if data["logo"][0] == "/" else data["logo"]

                r = requests.get(url, stream=True)
                if r.status_code == 200:
                    with open(f"{STATIC_DIR}/favicon.png", "wb") as f:
                        r.raw.decode_content = True
                        shutil.copyfileobj(r.raw, f)

            if "splash" in data:
                url = "" if data["splash"][0] == "/" else data["splash"]

                r = requests.get(url, stream=True)
                if r.status_code == 200:
                    with open(f"{STATIC_DIR}/splash.png", "wb") as f:
                        r.raw.decode_content = True
                        shutil.copyfileobj(r.raw, f)

            WEBUI_NAME = data["name"]
    except Exception as e:
        log.exception(e)
        pass


####################################
# STORAGE PROVIDER
####################################

STORAGE_PROVIDER = os.environ.get("STORAGE_PROVIDER", "")  # defaults to local, s3

S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", None)
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", None)
S3_REGION_NAME = os.environ.get("S3_REGION_NAME", None)
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", None)
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", None)

####################################
# File Upload DIR
####################################

UPLOAD_DIR = f"{DATA_DIR}/uploads"
Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)


####################################
# Cache DIR
####################################

CACHE_DIR = f"{DATA_DIR}/cache"
Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

####################################
# OLLAMA_BASE_URL
####################################

ENABLE_OLLAMA_API = PersistentConfig(
    "ENABLE_OLLAMA_API",
    "ollama.enable",
    os.environ.get("ENABLE_OLLAMA_API", "True").lower() == "true",
)

OLLAMA_API_BASE_URL = os.environ.get("OLLAMA_API_BASE_URL", "http://localhost:11434/api")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "")
if OLLAMA_BASE_URL:
    # Remove trailing slash
    OLLAMA_BASE_URL = OLLAMA_BASE_URL[:-1] if OLLAMA_BASE_URL.endswith("/") else OLLAMA_BASE_URL


K8S_FLAG = os.environ.get("K8S_FLAG", "")
USE_OLLAMA_DOCKER = os.environ.get("USE_OLLAMA_DOCKER", "false")

if OLLAMA_BASE_URL == "" and OLLAMA_API_BASE_URL != "":
    OLLAMA_BASE_URL = OLLAMA_API_BASE_URL[:-4] if OLLAMA_API_BASE_URL.endswith("/api") else OLLAMA_API_BASE_URL

if ENV == "prod":
    if OLLAMA_BASE_URL == "/ollama" and not K8S_FLAG:
        if USE_OLLAMA_DOCKER.lower() == "true":
            # if you use all-in-one docker container (Self.AI UI + Ollama)
            # with the docker build arg USE_OLLAMA=true (--build-arg="USE_OLLAMA=true") this only works with http://localhost:11434
            OLLAMA_BASE_URL = "http://localhost:11434"
        else:
            OLLAMA_BASE_URL = "http://host.docker.internal:11434"
    elif K8S_FLAG:
        OLLAMA_BASE_URL = "http://ollama-service.self-ai.svc.cluster.local:11434"


OLLAMA_BASE_URLS = os.environ.get("OLLAMA_BASE_URLS", "")
OLLAMA_BASE_URLS = OLLAMA_BASE_URLS if OLLAMA_BASE_URLS != "" else OLLAMA_BASE_URL

OLLAMA_BASE_URLS = [url.strip() for url in OLLAMA_BASE_URLS.split(";")]
OLLAMA_BASE_URLS = PersistentConfig("OLLAMA_BASE_URLS", "ollama.base_urls", OLLAMA_BASE_URLS)

OLLAMA_API_CONFIGS = PersistentConfig(
    "OLLAMA_API_CONFIGS",
    "ollama.api_configs",
    {},
)

####################################
# LLAMOLOTL
####################################

ENABLE_LLAMOLOTL_API = PersistentConfig(
    "ENABLE_LLAMOLOTL_API",
    "llamolotl.enable",
    os.environ.get("ENABLE_LLAMOLOTL_API", "True").lower() == "true",
)

LLAMOLOTL_BASE_URLS = os.environ.get("LLAMOLOTL_BASE_URLS", "")
if LLAMOLOTL_BASE_URLS == "":
    LLAMOLOTL_BASE_URLS = os.environ.get("LLAMOLOTL_BASE_URL", "http://self-llamolotl:8080")

LLAMOLOTL_BASE_URLS = [url.strip() for url in LLAMOLOTL_BASE_URLS.split(";")]
LLAMOLOTL_BASE_URLS = PersistentConfig("LLAMOLOTL_BASE_URLS", "llamolotl.base_urls", LLAMOLOTL_BASE_URLS)

LLAMOLOTL_CONTROL_BASE_URLS = os.environ.get("LLAMOLOTL_CONTROL_BASE_URLS", "")
if LLAMOLOTL_CONTROL_BASE_URLS == "":
    LLAMOLOTL_CONTROL_BASE_URLS = os.environ.get("LLAMOLOTL_CONTROL_BASE_URL", "http://self-llamolotl:8093")

LLAMOLOTL_CONTROL_BASE_URLS = [url.strip() for url in LLAMOLOTL_CONTROL_BASE_URLS.split(";")]
LLAMOLOTL_CONTROL_BASE_URLS = PersistentConfig(
    "LLAMOLOTL_CONTROL_BASE_URLS",
    "llamolotl.control_base_urls",
    LLAMOLOTL_CONTROL_BASE_URLS,
)

LLAMOLOTL_API_CONFIGS = PersistentConfig(
    "LLAMOLOTL_API_CONFIGS",
    "llamolotl.api_configs",
    {},
)

####################################
# CURATOR
####################################

ENABLE_CURATOR_API = PersistentConfig(
    "ENABLE_CURATOR_API",
    "curator.enable",
    os.environ.get("ENABLE_CURATOR_API", "True").lower() == "true",
)

CURATOR_BASE_URLS = os.environ.get("CURATOR_BASE_URLS", "")
if CURATOR_BASE_URLS == "":
    CURATOR_BASE_URLS = os.environ.get("CURATOR_BASE_URL", "http://self-curator:8094")

CURATOR_BASE_URLS = [url.strip() for url in CURATOR_BASE_URLS.split(";")]
CURATOR_BASE_URLS = PersistentConfig("CURATOR_BASE_URLS", "curator.base_urls", CURATOR_BASE_URLS)

CURATOR_API_CONFIGS = PersistentConfig(
    "CURATOR_API_CONFIGS",
    "curator.api_configs",
    {},
)

# self.curator VRAM-lease CONTROL base — the pod the GPU-lease broker GETs
# /api/system/vram-state from and POSTs /api/system/vram-release to (self.ai#88).
# Its own config key, mirroring SKETCH_CONTROL_BASE_URL, rather than reusing
# CURATOR_BASE_URLS: that is a LIST of job-API endpoints an admin can repoint at
# will, and aiming a VRAM e-stop (which kills a running pipeline) at whatever
# happens to be first in an admin-editable list is not a decision to inherit.
# self.curator serves job API and control on the same port today, so the default
# matches CURATOR_BASE_URL's default — but the two can diverge without the
# e-stop silently following.
CURATOR_CONTROL_BASE_URL = PersistentConfig(
    "CURATOR_CONTROL_BASE_URL",
    "curator.control_base_url",
    os.environ.get("CURATOR_CONTROL_BASE_URL", "http://self-curator:8094"),
)

####################################
# SELF.CORPUS
####################################

# Public-tier only for now (self.ai/self.ai#32's first slice): gates whether
# creating a public Knowledge Base also creates a matching LakeFS repo in
# self.corpus. Per-user Private connections are future work — see
# selfshipyard/selfai/gitlab-profile:context/treasuremaps/2026-07-11-selfai-corpus-connections.md

ENABLE_SELF_CORPUS = PersistentConfig(
    "ENABLE_SELF_CORPUS",
    "self_corpus.enable",
    os.environ.get("ENABLE_SELF_CORPUS", "False").lower() == "true",
)

SELF_CORPUS_LAKEFS_ENDPOINT = PersistentConfig(
    "SELF_CORPUS_LAKEFS_ENDPOINT",
    "self_corpus.lakefs_endpoint",
    os.environ.get("SELF_CORPUS_LAKEFS_ENDPOINT", ""),
)

SELF_CORPUS_LAKEFS_ACCESS_KEY_ID = PersistentConfig(
    "SELF_CORPUS_LAKEFS_ACCESS_KEY_ID",
    "self_corpus.lakefs_access_key_id",
    os.environ.get("SELF_CORPUS_LAKEFS_ACCESS_KEY_ID", ""),
)

SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY = PersistentConfig(
    "SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY",
    "self_corpus.lakefs_secret_access_key",
    os.environ.get("SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY", ""),
)

####################################
# LANGUAGE-EVAL
####################################

ENABLE_LANGUAGE_EVAL_API = PersistentConfig(
    "ENABLE_LANGUAGE_EVAL_API",
    "language_eval.enable",
    os.environ.get("ENABLE_LANGUAGE_EVAL_API", "False").lower() == "true",
)

LANGUAGE_EVAL_BASE_URLS = os.environ.get("LANGUAGE_EVAL_BASE_URLS", "")
if LANGUAGE_EVAL_BASE_URLS == "":
    LANGUAGE_EVAL_BASE_URLS = os.environ.get("LANGUAGE_EVAL_BASE_URL", "http://self-language-eval:8096")
LANGUAGE_EVAL_BASE_URLS = [url.strip() for url in LANGUAGE_EVAL_BASE_URLS.split(";")]
LANGUAGE_EVAL_BASE_URLS = PersistentConfig(
    "LANGUAGE_EVAL_BASE_URLS", "language_eval.base_urls", LANGUAGE_EVAL_BASE_URLS
)

####################################
# CODE-EVAL
####################################

ENABLE_CODE_EVAL_API = PersistentConfig(
    "ENABLE_CODE_EVAL_API",
    "code_eval.enable",
    os.environ.get("ENABLE_CODE_EVAL_API", "False").lower() == "true",
)

CODE_EVAL_BASE_URLS = os.environ.get("CODE_EVAL_BASE_URLS", "")
if CODE_EVAL_BASE_URLS == "":
    CODE_EVAL_BASE_URLS = os.environ.get("CODE_EVAL_BASE_URL", "http://self-code-eval:8094")
CODE_EVAL_BASE_URLS = [url.strip() for url in CODE_EVAL_BASE_URLS.split(";")]
CODE_EVAL_BASE_URLS = PersistentConfig("CODE_EVAL_BASE_URLS", "code_eval.base_urls", CODE_EVAL_BASE_URLS)

####################################
# PISTON
####################################
# Sandboxed execution backend for Tools/Functions. Off by default: until this
# is enabled, Tools/Functions still run via in-process exec() with no
# isolation (see context/treasuremaps/2026-07-20-tools-piston-sandboxing.md).

ENABLE_PISTON_EXECUTION = PersistentConfig(
    "ENABLE_PISTON_EXECUTION",
    "piston.enable",
    os.environ.get("ENABLE_PISTON_EXECUTION", "False").lower() == "true",
)

PISTON_BASE_URL = PersistentConfig(
    "PISTON_BASE_URL",
    "piston.base_url",
    os.environ.get("PISTON_BASE_URL", "http://self-piston:2000"),
)

####################################
# MODS
####################################
# Operator-installed extensions. A mod is trusted code running in this process
# with this process's privileges -- enabling one is a decision of the same
# weight as deploying any other service into your stack. Scopes bound what
# *users* reach through a mod; they do not bound the mod itself.
#
# Discovery is the intersection of installed and enabled. Present-but-not-
# enabled contributes nothing and reports nothing (somebody meant it);
# enabled-but-not-installed is always an error (it is always a mistake) but
# never fatal. Enablement takes effect at boot -- there is no hot reload.

MODS_DIR = Path(os.environ.get("MODS_DIR", BACKEND_DIR / "mods"))

# Extra install locations beyond MODS_DIR. Semicolon-separated, matching the
# convention already used by OPENAI_API_BASE_URLS and CODE_EVAL_BASE_URLS.
MODS_EXTRA_DIRS = [d.strip() for d in os.environ.get("MODS_EXTRA_DIRS", "").split(";") if d.strip()]

MODS_INSTALL_DIRS = [MODS_DIR, *[Path(d) for d in MODS_EXTRA_DIRS]]

# The enabled list. Empty by default -- installing a mod does not enable it,
# and an instance nobody has configured runs no mod code at all.
ENABLED_MODS = PersistentConfig(
    "ENABLED_MODS",
    "mods.enabled",
    [m.strip() for m in os.environ.get("ENABLED_MODS", "").split(",") if m.strip()],
)

####################################
# ICEBERG
####################################

ICEBERG_BASE_URL = PersistentConfig(
    "ICEBERG_BASE_URL",
    "iceberg.base_url",
    os.environ.get("ICEBERG_BASE_URL", ""),
)

####################################
# OPENAI_API
####################################


ENABLE_OPENAI_API = PersistentConfig(
    "ENABLE_OPENAI_API",
    "openai.enable",
    os.environ.get("ENABLE_OPENAI_API", "True").lower() == "true",
)


OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_API_BASE_URL = os.environ.get("OPENAI_API_BASE_URL", "")


if OPENAI_API_BASE_URL == "":
    OPENAI_API_BASE_URL = "https://api.openai.com/v1"

OPENAI_API_KEYS = os.environ.get("OPENAI_API_KEYS", "")
OPENAI_API_KEYS = OPENAI_API_KEYS if OPENAI_API_KEYS != "" else OPENAI_API_KEY

OPENAI_API_KEYS = [url.strip() for url in OPENAI_API_KEYS.split(";")]
OPENAI_API_KEYS = PersistentConfig("OPENAI_API_KEYS", "openai.api_keys", OPENAI_API_KEYS)

OPENAI_API_BASE_URLS = os.environ.get("OPENAI_API_BASE_URLS", "")
OPENAI_API_BASE_URLS = OPENAI_API_BASE_URLS if OPENAI_API_BASE_URLS != "" else OPENAI_API_BASE_URL

OPENAI_API_BASE_URLS = [
    url.strip() if url != "" else "https://api.openai.com/v1" for url in OPENAI_API_BASE_URLS.split(";")
]
OPENAI_API_BASE_URLS = PersistentConfig("OPENAI_API_BASE_URLS", "openai.api_base_urls", OPENAI_API_BASE_URLS)

try:
    OPENAI_API_CONFIGS_ENV = json.loads(os.environ.get("OPENAI_API_CONFIGS", "{}"))
except Exception as e:
    print(f"Error loading OPENAI_API_CONFIGS: {e}")
    OPENAI_API_CONFIGS_ENV = {}

OPENAI_API_CONFIGS = PersistentConfig(
    "OPENAI_API_CONFIGS",
    "openai.api_configs",
    OPENAI_API_CONFIGS_ENV,
)

# Get the actual OpenAI API key based on the base URL
OPENAI_API_KEY = ""
try:
    OPENAI_API_KEY = OPENAI_API_KEYS.value[OPENAI_API_BASE_URLS.value.index("https://api.openai.com/v1")]
except Exception:
    pass
OPENAI_API_BASE_URL = "https://api.openai.com/v1"

####################################
# ANTHROPIC
####################################

ENABLE_ANTHROPIC_API = PersistentConfig(
    "ENABLE_ANTHROPIC_API",
    "anthropic.enable",
    os.environ.get("ENABLE_ANTHROPIC_API", "False").lower() == "true",
)

ANTHROPIC_BASE_URLS = os.environ.get("ANTHROPIC_BASE_URLS", "")
if ANTHROPIC_BASE_URLS == "":
    ANTHROPIC_BASE_URLS = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

ANTHROPIC_BASE_URLS = [url.strip() for url in ANTHROPIC_BASE_URLS.split(";")]
ANTHROPIC_BASE_URLS = PersistentConfig("ANTHROPIC_BASE_URLS", "anthropic.base_urls", ANTHROPIC_BASE_URLS)

# Unlike OPENAI_API_KEYS, the Anthropic key lives *inside* the per-URL config bag
# (as ollama/llamolotl already do). Anthropic authenticates with x-api-key, and the
# parallel URLs/keys arrays on the OpenAI side force a pad/truncate dance in both
# the router and the admin UI that we are deliberately not reproducing.
try:
    ANTHROPIC_API_CONFIGS_ENV = json.loads(os.environ.get("ANTHROPIC_API_CONFIGS", "{}"))
except Exception as e:
    print(f"Error loading ANTHROPIC_API_CONFIGS: {e}")
    ANTHROPIC_API_CONFIGS_ENV = {}

ANTHROPIC_API_CONFIGS = PersistentConfig(
    "ANTHROPIC_API_CONFIGS",
    "anthropic.api_configs",
    ANTHROPIC_API_CONFIGS_ENV,
)

####################################
# WEBUI
####################################


WEBUI_URL = PersistentConfig("WEBUI_URL", "webui.url", os.environ.get("WEBUI_URL", "http://localhost:3000"))


ENABLE_SIGNUP = PersistentConfig(
    "ENABLE_SIGNUP",
    "ui.enable_signup",
    (False if not WEBUI_AUTH else os.environ.get("ENABLE_SIGNUP", "True").lower() == "true"),
)

ENABLE_LOGIN_FORM = PersistentConfig(
    "ENABLE_LOGIN_FORM",
    "ui.ENABLE_LOGIN_FORM",
    os.environ.get("ENABLE_LOGIN_FORM", "True").lower() == "true",
)


DEFAULT_LOCALE = PersistentConfig(
    "DEFAULT_LOCALE",
    "ui.default_locale",
    os.environ.get("DEFAULT_LOCALE", ""),
)

DEFAULT_MODELS = PersistentConfig("DEFAULT_MODELS", "ui.default_models", os.environ.get("DEFAULT_MODELS", None))

DEFAULT_PROMPT_SUGGESTIONS = PersistentConfig(
    "DEFAULT_PROMPT_SUGGESTIONS",
    "ui.prompt_suggestions",
    [
        {
            "title": ["Help me study", "vocabulary for a college entrance exam"],
            "content": (
                "Help me study vocabulary: write a sentence for me to fill in "
                "the blank, and I'll try to pick the correct option."
            ),
        },
        {
            "title": ["Give me ideas", "for what to do with my kids' art"],
            "content": (
                "What are 5 creative things I could do with my kids' art? "
                "I don't want to throw them away, but it's also so much clutter."
            ),
        },
        {
            "title": ["Tell me a fun fact", "about the Roman Empire"],
            "content": "Tell me a random fun fact about the Roman Empire",
        },
        {
            "title": ["Show me a code snippet", "of a website's sticky header"],
            "content": "Show me a code snippet of a website's sticky header in CSS and JavaScript.",
        },
        {
            "title": [
                "Explain options trading",
                "if I'm familiar with buying and selling stocks",
            ],
            "content": "Explain options trading in simple terms if I'm familiar with buying and selling stocks.",
        },
        {
            "title": ["Overcome procrastination", "give me tips"],
            "content": (
                "Could you start by asking me about instances when I "
                "procrastinate the most and then give me some suggestions "
                "to overcome it?"
            ),
        },
    ],
)

MODEL_ORDER_LIST = PersistentConfig(
    "MODEL_ORDER_LIST",
    "ui.model_order_list",
    [],
)

DEFAULT_USER_ROLE = PersistentConfig(
    "DEFAULT_USER_ROLE",
    "ui.default_user_role",
    os.getenv("DEFAULT_USER_ROLE", "pending"),
)

# Studio (formerly Workspace) — Phase 0 of the Tokenization Studio programme.
# See context/plans/build-site-studio-rename-permissions.md and the treasuremap
# selfai/gitlab-profile:context/treasuremaps/2026-08-11-tokenization-studio.md
# (Decision 1).
#
# NO MANIFEST CHANGE ACCOMPANIES THIS RENAME, and that is deliberate rather than
# an oversight: no `WORKSPACE` string appears anywhere under `manifests/`, so
# nothing was overriding these and every default below stays `False` exactly as
# it was. Renaming the env keys therefore orphans no configured value. If a
# `USER_PERMISSIONS_STUDIO_*_ACCESS` env is ever added to a manifest, it is a
# new grant, not a restoration.
USER_PERMISSIONS_STUDIO_MODELS_ACCESS = (
    os.environ.get("USER_PERMISSIONS_STUDIO_MODELS_ACCESS", "False").lower() == "true"
)

USER_PERMISSIONS_STUDIO_KNOWLEDGE_ACCESS = (
    os.environ.get("USER_PERMISSIONS_STUDIO_KNOWLEDGE_ACCESS", "False").lower() == "true"
)

# Sound Studio: gates the Voices Studio tab + voice creation (studio.voices).
USER_PERMISSIONS_STUDIO_VOICES_ACCESS = (
    os.environ.get("USER_PERMISSIONS_STUDIO_VOICES_ACCESS", "False").lower() == "true"
)

USER_PERMISSIONS_STUDIO_PROMPTS_ACCESS = (
    os.environ.get("USER_PERMISSIONS_STUDIO_PROMPTS_ACCESS", "False").lower() == "true"
)

USER_PERMISSIONS_STUDIO_TRAINING_ACCESS = (
    os.environ.get("USER_PERMISSIONS_STUDIO_TRAINING_ACCESS", "False").lower() == "true"
)

USER_PERMISSIONS_STUDIO_TOOLS_ACCESS = (
    os.environ.get("USER_PERMISSIONS_STUDIO_TOOLS_ACCESS", "False").lower() == "true"
)

# self.ai#131: gates publishing a model line -- merging its accumulated
# adapters into the base and producing a new base GGUF. Deliberately separate
# from studio.training, which gates fitting an adapter: a bake costs an adapter
# and a publish costs several GB and a GPU window, so holding one must not imply
# the other. Defaults False like every sibling; this is a new grant.
USER_PERMISSIONS_STUDIO_PUBLISH_ACCESS = (
    os.environ.get("USER_PERMISSIONS_STUDIO_PUBLISH_ACCESS", "False").lower() == "true"
)

# Tokenization Studio (Phase 2). Admits an artist to /studio/tokenization AND to
# queueing tokenization jobs from it -- queueing is deliberately NOT separately
# gated (treasuremap Decision 11). What stays separate is defining job WINDOWS
# (an operator act) and creating/queueing the existing training courses. The
# boundary is who may change the rules, not who may consume capacity under them;
# neither implies the other in either direction. Independent of studio.publish
# above for the same reason: neither implies the other.
USER_PERMISSIONS_STUDIO_TOKENIZATION_ACCESS = (
    os.environ.get("USER_PERMISSIONS_STUDIO_TOKENIZATION_ACCESS", "False").lower() == "true"
)

USER_PERMISSIONS_CHAT_FILE_UPLOAD = os.environ.get("USER_PERMISSIONS_CHAT_FILE_UPLOAD", "True").lower() == "true"

USER_PERMISSIONS_CHAT_DELETE = os.environ.get("USER_PERMISSIONS_CHAT_DELETE", "True").lower() == "true"

USER_PERMISSIONS_CHAT_EDIT = os.environ.get("USER_PERMISSIONS_CHAT_EDIT", "True").lower() == "true"

USER_PERMISSIONS_CHAT_TEMPORARY = os.environ.get("USER_PERMISSIONS_CHAT_TEMPORARY", "True").lower() == "true"

# Gates any tool backed by the core browse (Playwright) connection — see
# api/selfai_ui/browse/access_control.py (cavekit-browse-access-control.md R1).
# Decoupled from the browse connection's own auth to the Playwright service
# (cavekit-browse-connection.md R1): granting/revoking this never touches that
# service.
USER_PERMISSIONS_FEATURES_WEB_BROWSING_ACCESS = (
    os.environ.get("USER_PERMISSIONS_FEATURES_WEB_BROWSING_ACCESS", "False").lower() == "true"
)

USER_PERMISSIONS = PersistentConfig(
    "USER_PERMISSIONS",
    "user.permissions",
    {
        # Renamed from "workspace" (Phase 0). Stored group blobs are rekeyed by
        # the Alembic revision that accompanies this, and has_permission carries
        # a transitional fallback for any group not yet migrated -- without both,
        # every non-admin silently loses all Studio access on deploy, because
        # has_permission denies on a missing level and every default here is
        # False. The six children and their defaults are unchanged; this renames
        # a key and loosens nothing.
        "studio": {
            "models": USER_PERMISSIONS_STUDIO_MODELS_ACCESS,
            "knowledge": USER_PERMISSIONS_STUDIO_KNOWLEDGE_ACCESS,
            "voices": USER_PERMISSIONS_STUDIO_VOICES_ACCESS,
            "prompts": USER_PERMISSIONS_STUDIO_PROMPTS_ACCESS,
            "training": USER_PERMISSIONS_STUDIO_TRAINING_ACCESS,
            "tools": USER_PERMISSIONS_STUDIO_TOOLS_ACCESS,
            "publish": USER_PERMISSIONS_STUDIO_PUBLISH_ACCESS,
            # Must be declared HERE, not only enforced at the call site. The
            # default blob feeds `get_permissions` as well as `has_permission`
            # (see build-site-studio-rename-permissions.md T-003 as corrected),
            # and `get_permissions` builds the object the client gates its
            # navigation on -- so a key missing from this dict is invisible to
            # the client even for a group that has been granted it. That is the
            # same silent failure `evaluations` still has today (self.ai#133).
            "tokenization": USER_PERMISSIONS_STUDIO_TOKENIZATION_ACCESS,
        },
        "chat": {
            "file_upload": USER_PERMISSIONS_CHAT_FILE_UPLOAD,
            "delete": USER_PERMISSIONS_CHAT_DELETE,
            "edit": USER_PERMISSIONS_CHAT_EDIT,
            "temporary": USER_PERMISSIONS_CHAT_TEMPORARY,
        },
        "features": {
            "web_browsing": USER_PERMISSIONS_FEATURES_WEB_BROWSING_ACCESS,
        },
    },
)

ENABLE_CHANNELS = PersistentConfig(
    "ENABLE_CHANNELS",
    "channels.enable",
    os.environ.get("ENABLE_CHANNELS", "False").lower() == "true",
)


ENABLE_EVALUATION_ARENA_MODELS = PersistentConfig(
    "ENABLE_EVALUATION_ARENA_MODELS",
    "evaluation.arena.enable",
    os.environ.get("ENABLE_EVALUATION_ARENA_MODELS", "True").lower() == "true",
)
EVALUATION_ARENA_MODELS = PersistentConfig(
    "EVALUATION_ARENA_MODELS",
    "evaluation.arena.models",
    [],
)

DEFAULT_ARENA_MODEL = {
    "id": "arena-model",
    "name": "Arena Model",
    "meta": {
        "profile_image_url": "/favicon.png",
        "description": "Submit your questions to anonymous AI chatbots and vote on the best response.",
        "model_ids": None,
    },
}

WEBHOOK_URL = PersistentConfig("WEBHOOK_URL", "webhook_url", os.environ.get("WEBHOOK_URL", ""))

ENABLE_ADMIN_EXPORT = os.environ.get("ENABLE_ADMIN_EXPORT", "True").lower() == "true"

ENABLE_ADMIN_CHAT_ACCESS = os.environ.get("ENABLE_ADMIN_CHAT_ACCESS", "True").lower() == "true"

ENABLE_COMMUNITY_SHARING = PersistentConfig(
    "ENABLE_COMMUNITY_SHARING",
    "ui.enable_community_sharing",
    os.environ.get("ENABLE_COMMUNITY_SHARING", "True").lower() == "true",
)

ENABLE_MESSAGE_RATING = PersistentConfig(
    "ENABLE_MESSAGE_RATING",
    "ui.enable_message_rating",
    os.environ.get("ENABLE_MESSAGE_RATING", "True").lower() == "true",
)


def validate_cors_origins(origins):
    for origin in origins:
        if origin != "*":
            validate_cors_origin(origin)


def validate_cors_origin(origin):
    parsed_url = urlparse(origin)

    # Check if the scheme is either http or https
    if parsed_url.scheme not in ["http", "https"]:
        raise ValueError(f"Invalid scheme in CORS_ALLOW_ORIGIN: '{origin}'. Only 'http' and 'https' are allowed.")

    # Ensure that the netloc (domain + port) is present, indicating it's a valid URL
    if not parsed_url.netloc:
        raise ValueError(f"Invalid URL structure in CORS_ALLOW_ORIGIN: '{origin}'.")


# For production, you should only need one host as
# fastapi serves the svelte-kit built frontend and backend from the same host and port.
# To test CORS_ALLOW_ORIGIN locally, you can set something like
# CORS_ALLOW_ORIGIN=http://localhost:5173;http://localhost:8080
# in your .env file depending on your frontend port, 5173 in this case.
_cors_env = os.environ.get("CORS_ALLOW_ORIGIN", "")
CORS_ALLOW_ORIGIN = _cors_env.split(";") if _cors_env else []

if "*" in CORS_ALLOW_ORIGIN:
    log.warning(
        "\n\nWARNING: CORS_ALLOW_ORIGIN IS SET TO '*' - NOT RECOMMENDED.\n"
        "Wildcard origins with allow_credentials=True is a security misconfiguration.\n"
        "Set CORS_ALLOW_ORIGIN to your specific domain(s) instead.\n"
    )

validate_cors_origins(CORS_ALLOW_ORIGIN)


class BannerModel(BaseModel):
    id: str
    type: str
    title: Optional[str] = None
    content: str
    dismissible: bool
    timestamp: int


try:
    banners = json.loads(os.environ.get("WEBUI_BANNERS", "[]"))
    banners = [BannerModel(**banner) for banner in banners]
except Exception as e:
    print(f"Error loading WEBUI_BANNERS: {e}")
    banners = []

WEBUI_BANNERS = PersistentConfig("WEBUI_BANNERS", "ui.banners", banners)


SHOW_ADMIN_DETAILS = PersistentConfig(
    "SHOW_ADMIN_DETAILS",
    "auth.admin.show",
    os.environ.get("SHOW_ADMIN_DETAILS", "true").lower() == "true",
)

ADMIN_EMAIL = PersistentConfig(
    "ADMIN_EMAIL",
    "auth.admin.email",
    os.environ.get("ADMIN_EMAIL", None),
)


####################################
# TASKS
####################################


TASK_MODEL = PersistentConfig(
    "TASK_MODEL",
    "task.model.default",
    os.environ.get("TASK_MODEL", ""),
)

TASK_MODEL_EXTERNAL = PersistentConfig(
    "TASK_MODEL_EXTERNAL",
    "task.model.external",
    os.environ.get("TASK_MODEL_EXTERNAL", ""),
)

TITLE_GENERATION_PROMPT_TEMPLATE = PersistentConfig(
    "TITLE_GENERATION_PROMPT_TEMPLATE",
    "task.title.prompt_template",
    os.environ.get("TITLE_GENERATION_PROMPT_TEMPLATE", ""),
)

DEFAULT_TITLE_GENERATION_PROMPT_TEMPLATE = """Create a concise, 3-5 word title with an emoji as a title for the chat history, in the given language. Suitable Emojis for the summary can be used to enhance understanding but avoid quotation marks or special formatting. RESPOND ONLY WITH THE TITLE TEXT.

Examples of titles:
📉 Stock Market Trends
🍪 Perfect Chocolate Chip Recipe
Evolution of Music Streaming
Remote Work Productivity Tips
Artificial Intelligence in Healthcare
🎮 Video Game Development Insights

<chat_history>
{{MESSAGES:END:2}}
</chat_history>"""


TAGS_GENERATION_PROMPT_TEMPLATE = PersistentConfig(
    "TAGS_GENERATION_PROMPT_TEMPLATE",
    "task.tags.prompt_template",
    os.environ.get("TAGS_GENERATION_PROMPT_TEMPLATE", ""),
)

DEFAULT_TAGS_GENERATION_PROMPT_TEMPLATE = """### Task:
Generate 1-3 broad tags categorizing the main themes of the chat history, along with 1-3 more specific subtopic tags.

### Guidelines:
- Start with high-level domains (e.g. Science, Technology, Philosophy, Arts, Politics, Business, Health, Sports, Entertainment, Education)
- Consider including relevant subfields/subdomains if they are strongly represented throughout the conversation
- If content is too short (less than 3 messages) or too diverse, use only ["General"]
- Use the chat's primary language; default to English if multilingual
- Prioritize accuracy over specificity

### Output:
JSON format: { "tags": ["tag1", "tag2", "tag3"] }

### Chat History:
<chat_history>
{{MESSAGES:END:6}}
</chat_history>"""

ENABLE_TAGS_GENERATION = PersistentConfig(
    "ENABLE_TAGS_GENERATION",
    "task.tags.enable",
    os.environ.get("ENABLE_TAGS_GENERATION", "True").lower() == "true",
)


ENABLE_SEARCH_QUERY_GENERATION = PersistentConfig(
    "ENABLE_SEARCH_QUERY_GENERATION",
    "task.query.search.enable",
    os.environ.get("ENABLE_SEARCH_QUERY_GENERATION", "True").lower() == "true",
)

ENABLE_RETRIEVAL_QUERY_GENERATION = PersistentConfig(
    "ENABLE_RETRIEVAL_QUERY_GENERATION",
    "task.query.retrieval.enable",
    os.environ.get("ENABLE_RETRIEVAL_QUERY_GENERATION", "True").lower() == "true",
)


QUERY_GENERATION_PROMPT_TEMPLATE = PersistentConfig(
    "QUERY_GENERATION_PROMPT_TEMPLATE",
    "task.query.prompt_template",
    os.environ.get("QUERY_GENERATION_PROMPT_TEMPLATE", ""),
)

DEFAULT_QUERY_GENERATION_PROMPT_TEMPLATE = """### Task:
Analyze the chat history to determine the necessity of generating search queries, in the given language. By default, **prioritize generating 1-3 broad and relevant search queries** unless it is absolutely certain that no additional information is required. The aim is to retrieve comprehensive, updated, and valuable information even with minimal uncertainty. If no search is unequivocally needed, return an empty list.

### Guidelines:
- Respond **EXCLUSIVELY** with a JSON object. Any form of extra commentary, explanation, or additional text is strictly prohibited.
- When generating search queries, respond in the format: { "queries": ["query1", "query2"] }, ensuring each query is distinct, concise, and relevant to the topic.
- If and only if it is entirely certain that no useful results can be retrieved by a search, return: { "queries": [] }.
- Err on the side of suggesting search queries if there is **any chance** they might provide useful or updated information.
- Be concise and focused on composing high-quality search queries, avoiding unnecessary elaboration, commentary, or assumptions.
- Today's date is: {{CURRENT_DATE}}.
- Always prioritize providing actionable and broad queries that maximize informational coverage.

### Output:
Strictly return in JSON format: 
{
  "queries": ["query1", "query2"]
}

### Chat History:
<chat_history>
{{MESSAGES:END:6}}
</chat_history>
"""

ENABLE_AUTOCOMPLETE_GENERATION = PersistentConfig(
    "ENABLE_AUTOCOMPLETE_GENERATION",
    "task.autocomplete.enable",
    os.environ.get("ENABLE_AUTOCOMPLETE_GENERATION", "True").lower() == "true",
)

AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH = PersistentConfig(
    "AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH",
    "task.autocomplete.input_max_length",
    int(os.environ.get("AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH", "-1")),
)

AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE = PersistentConfig(
    "AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE",
    "task.autocomplete.prompt_template",
    os.environ.get("AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE", ""),
)


DEFAULT_AUTOCOMPLETE_GENERATION_PROMPT_TEMPLATE = """### Task:
You are an autocompletion system. Continue the text in `<text>` based on the **completion type** in `<type>` and the given language.  

### **Instructions**:
1. Analyze `<text>` for context and meaning.  
2. Use `<type>` to guide your output:  
   - **General**: Provide a natural, concise continuation.  
   - **Search Query**: Complete as if generating a realistic search query.  
3. Start as if you are directly continuing `<text>`. Do **not** repeat, paraphrase, or respond as a model. Simply complete the text.  
4. Ensure the continuation:
   - Flows naturally from `<text>`.  
   - Avoids repetition, overexplaining, or unrelated ideas.  
5. If unsure, return: `{ "text": "" }`.  

### **Output Rules**:
- Respond only in JSON format: `{ "text": "<your_completion>" }`.

### **Examples**:
#### Example 1:  
Input:  
<type>General</type>  
<text>The sun was setting over the horizon, painting the sky</text>  
Output:  
{ "text": "with vibrant shades of orange and pink." }

#### Example 2:  
Input:  
<type>Search Query</type>  
<text>Top-rated restaurants in</text>  
Output:  
{ "text": "New York City for Italian cuisine." }  

---
### Context:
<chat_history>
{{MESSAGES:END:6}}
</chat_history>
<type>{{TYPE}}</type>  
<text>{{PROMPT}}</text>  
#### Output:
"""

TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE = PersistentConfig(
    "TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE",
    "task.tools.prompt_template",
    os.environ.get("TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE", ""),
)


DEFAULT_TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE = """Available Tools: {{TOOLS}}\nReturn an empty string if no tools match the query. If a function tool matches, construct and return a JSON object in the format {\"name\": \"functionName\", \"parameters\": {\"requiredFunctionParamKey\": \"requiredFunctionParamValue\"}} using the appropriate tool and its parameters. Only return the object and limit the response to the JSON object without additional text."""


DEFAULT_EMOJI_GENERATION_PROMPT_TEMPLATE = """Your task is to reflect the speaker's likely facial expression through a fitting emoji. Interpret emotions from the message and reflect their facial expression using fitting, diverse emojis (e.g., 😊, 😢, 😡, 😱).

Message: ```{{prompt}}```"""

DEFAULT_MOA_GENERATION_PROMPT_TEMPLATE = """You have been provided with a set of responses from various models to the latest user query: "{{prompt}}"

Your task is to synthesize these responses into a single, high-quality response. It is crucial to critically evaluate the information provided in these responses, recognizing that some of it may be biased or incorrect. Your response should not simply replicate the given answers but should offer a refined, accurate, and comprehensive reply to the instruction. Ensure your response is well-structured, coherent, and adheres to the highest standards of accuracy and reliability.

Responses from models: {{responses}}"""

####################################
# Vector Database
####################################

VECTOR_DB = os.environ.get("VECTOR_DB", "sqlite-vec")

# sqlite-vec — the zero-configuration default, replacing ChromaDB.
#
# Chroma was the inherited default and cost 41 transitive packages (~98MB:
# onnxruntime, tokenizers, the OpenTelemetry stack, a Kubernetes client) plus a
# from-source chroma-hnswlib build, to serve single-user local installs. It was
# also dead weight in the yard, which runs VECTOR_DB=pgvector and shipped all of
# it anyway. sqlite-vec (MIT/Apache-2.0) is a loadable extension for the SQLite
# Python already links: no extra service for the single-container quickstart.
#
# The store lives beside the old chroma directory rather than inside it -- there
# is no in-place migration between the two formats, so an existing install
# re-indexes rather than silently reading a store that isn't there.
SQLITE_VEC_PATH = os.environ.get("SQLITE_VEC_PATH", f"{DATA_DIR}/vector_db/sqlite_vec.db")
# Fixed for the life of the vec0 table. Shorter embeddings are zero-padded
# (cosine-safe); anything longer is a hard error rather than a silent truncation.
SQLITE_VEC_VECTOR_LENGTH = int(os.environ.get("SQLITE_VEC_VECTOR_LENGTH", "1536"))

# Milvus

MILVUS_URI = os.environ.get("MILVUS_URI", f"{DATA_DIR}/vector_db/milvus.db")

# Qdrant
QDRANT_URI = os.environ.get("QDRANT_URI", None)
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", None)

# OpenSearch
OPENSEARCH_URI = os.environ.get("OPENSEARCH_URI", "https://localhost:9200")
OPENSEARCH_SSL = os.environ.get("OPENSEARCH_SSL", True)
OPENSEARCH_CERT_VERIFY = os.environ.get("OPENSEARCH_CERT_VERIFY", False)
OPENSEARCH_USERNAME = os.environ.get("OPENSEARCH_USERNAME", None)
OPENSEARCH_PASSWORD = os.environ.get("OPENSEARCH_PASSWORD", None)

# Pgvector
PGVECTOR_DB_URL = os.environ.get("PGVECTOR_DB_URL", DATABASE_URL)
if VECTOR_DB == "pgvector" and not PGVECTOR_DB_URL.startswith("postgres"):
    raise ValueError(
        "Pgvector requires setting PGVECTOR_DB_URL or using Postgres with vector extension as the primary database."
    )
PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH = int(os.environ.get("PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH", "1536"))

# Vector index on document_chunk: OPT-IN, default OFF (self.ai#62).
#
# This used to be created unconditionally with a hardcoded lists = 100. Measured
# on yard-pg 2026-07-23: document_chunk held 174 rows, the planner chose a seq
# scan every time, and the index was 4792 kB -- roughly 72% of the table's total
# footprint -- for a structure nothing read, re-maintained on every insert.
# Top-10 results were byte-identical against a forced exact scan, so this was
# dead weight rather than a correctness problem.
#
# lists = 100 is also simply wrong at that cardinality: pgvector's guidance is
# ~rows/1000, i.e. lists = 1 for 174 rows. At this size the right answer is no
# vector index at all, so it now has to be asked for.
#
# When document_chunk does outgrow a seq scan (~10k rows), prefer HNSW over
# ivfflat: better recall/latency, and it does not need the table populated
# before it is built. That is a bigger change than re-enabling this flag.
PGVECTOR_CREATE_VECTOR_INDEX = (
    os.environ.get("PGVECTOR_CREATE_VECTOR_INDEX", "false").lower() == "true"
)
# Only consulted when the index is enabled. Size it from the row count
# (~rows/1000) rather than leaving it at the inherited default.
PGVECTOR_IVFFLAT_LISTS = int(os.environ.get("PGVECTOR_IVFFLAT_LISTS", "100"))

####################################
# Information Retrieval (RAG)
####################################


# If configured, Google Drive will be available as an upload option.
ENABLE_GOOGLE_DRIVE_INTEGRATION = PersistentConfig(
    "ENABLE_GOOGLE_DRIVE_INTEGRATION",
    "google_drive.enable",
    os.getenv("ENABLE_GOOGLE_DRIVE_INTEGRATION", "False").lower() == "true",
)

GOOGLE_DRIVE_CLIENT_ID = PersistentConfig(
    "GOOGLE_DRIVE_CLIENT_ID",
    "google_drive.client_id",
    os.environ.get("GOOGLE_DRIVE_CLIENT_ID", ""),
)

GOOGLE_DRIVE_API_KEY = PersistentConfig(
    "GOOGLE_DRIVE_API_KEY",
    "google_drive.api_key",
    os.environ.get("GOOGLE_DRIVE_API_KEY", ""),
)

# RAG Content Extraction
CONTENT_EXTRACTION_ENGINE = PersistentConfig(
    "CONTENT_EXTRACTION_ENGINE",
    "rag.CONTENT_EXTRACTION_ENGINE",
    os.environ.get("CONTENT_EXTRACTION_ENGINE", "").lower(),
)

TIKA_SERVER_URL = PersistentConfig(
    "TIKA_SERVER_URL",
    "rag.tika_server_url",
    os.getenv("TIKA_SERVER_URL", "http://tika:9998"),  # Default for sidecar deployment
)

RAG_TOP_K = PersistentConfig("RAG_TOP_K", "rag.top_k", int(os.environ.get("RAG_TOP_K", "3")))
RAG_RELEVANCE_THRESHOLD = PersistentConfig(
    "RAG_RELEVANCE_THRESHOLD",
    "rag.relevance_threshold",
    float(os.environ.get("RAG_RELEVANCE_THRESHOLD", "0.0")),
)

ENABLE_RAG_HYBRID_SEARCH = PersistentConfig(
    "ENABLE_RAG_HYBRID_SEARCH",
    "rag.enable_hybrid_search",
    os.environ.get("ENABLE_RAG_HYBRID_SEARCH", "").lower() == "true",
)

RAG_FILE_MAX_COUNT = PersistentConfig(
    "RAG_FILE_MAX_COUNT",
    "rag.file.max_count",
    (int(os.environ.get("RAG_FILE_MAX_COUNT")) if os.environ.get("RAG_FILE_MAX_COUNT") else None),
)

RAG_FILE_MAX_SIZE = PersistentConfig(
    "RAG_FILE_MAX_SIZE",
    "rag.file.max_size",
    (int(os.environ.get("RAG_FILE_MAX_SIZE")) if os.environ.get("RAG_FILE_MAX_SIZE") else None),
)

FILE_UPLOAD_MIME_ALLOWLIST = PersistentConfig(
    "FILE_UPLOAD_MIME_ALLOWLIST",
    "rag.file.mime_allowlist",
    (
        os.environ.get("FILE_UPLOAD_MIME_ALLOWLIST", "").split(",")
        if os.environ.get("FILE_UPLOAD_MIME_ALLOWLIST")
        else []
    ),
)

# Default allowed MIME categories when allowlist is empty (permissive default)
# If FILE_UPLOAD_MIME_ALLOWLIST is set, only those exact types are allowed.
# Executable types are always blocked regardless of allowlist.
FILE_UPLOAD_BLOCKED_MIME_PREFIXES = [
    "application/x-executable",
    "application/x-sharedlib",
    "application/x-msdos-program",
    "application/x-msdownload",
]

ENABLE_RAG_WEB_LOADER_SSL_VERIFICATION = PersistentConfig(
    "ENABLE_RAG_WEB_LOADER_SSL_VERIFICATION",
    "rag.enable_web_loader_ssl_verification",
    os.environ.get("ENABLE_RAG_WEB_LOADER_SSL_VERIFICATION", "True").lower() == "true",
)

RAG_WEB_LOADER_ENGINE = PersistentConfig(
    "RAG_WEB_LOADER_ENGINE",
    "rag.web.loader.engine",
    os.getenv("RAG_WEB_LOADER_ENGINE", ""),
)

RAG_EMBEDDING_ENGINE = PersistentConfig(
    "RAG_EMBEDDING_ENGINE",
    "rag.embedding_engine",
    os.environ.get("RAG_EMBEDDING_ENGINE", ""),
)

PDF_EXTRACT_IMAGES = PersistentConfig(
    "PDF_EXTRACT_IMAGES",
    "rag.pdf_extract_images",
    os.environ.get("PDF_EXTRACT_IMAGES", "False").lower() == "true",
)

RAG_EMBEDDING_MODEL = PersistentConfig(
    "RAG_EMBEDDING_MODEL",
    "rag.embedding_model",
    os.environ.get("RAG_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
)
log.info(f"Embedding model set: {RAG_EMBEDDING_MODEL.value}")

RAG_EMBEDDING_MODEL_AUTO_UPDATE = (
    not OFFLINE_MODE and os.environ.get("RAG_EMBEDDING_MODEL_AUTO_UPDATE", "True").lower() == "true"
)

RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE = (
    os.environ.get("RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE", "True").lower() == "true"
)

RAG_EMBEDDING_BATCH_SIZE = PersistentConfig(
    "RAG_EMBEDDING_BATCH_SIZE",
    "rag.embedding_batch_size",
    int(os.environ.get("RAG_EMBEDDING_BATCH_SIZE") or os.environ.get("RAG_EMBEDDING_OPENAI_BATCH_SIZE", "1")),
)

RAG_RERANKING_MODEL = PersistentConfig(
    "RAG_RERANKING_MODEL",
    "rag.reranking_model",
    os.environ.get("RAG_RERANKING_MODEL", ""),
)
if RAG_RERANKING_MODEL.value != "":
    log.info(f"Reranking model set: {RAG_RERANKING_MODEL.value}")

RAG_RERANKING_MODEL_AUTO_UPDATE = (
    not OFFLINE_MODE and os.environ.get("RAG_RERANKING_MODEL_AUTO_UPDATE", "True").lower() == "true"
)

RAG_RERANKING_MODEL_TRUST_REMOTE_CODE = (
    os.environ.get("RAG_RERANKING_MODEL_TRUST_REMOTE_CODE", "True").lower() == "true"
)


RAG_TEXT_SPLITTER = PersistentConfig(
    "RAG_TEXT_SPLITTER",
    "rag.text_splitter",
    os.environ.get("RAG_TEXT_SPLITTER", ""),
)


TIKTOKEN_CACHE_DIR = os.environ.get("TIKTOKEN_CACHE_DIR", f"{CACHE_DIR}/tiktoken")
TIKTOKEN_ENCODING_NAME = PersistentConfig(
    "TIKTOKEN_ENCODING_NAME",
    "rag.tiktoken_encoding_name",
    os.environ.get("TIKTOKEN_ENCODING_NAME", "cl100k_base"),
)


CHUNK_SIZE = PersistentConfig("CHUNK_SIZE", "rag.chunk_size", int(os.environ.get("CHUNK_SIZE", "1000")))
CHUNK_OVERLAP = PersistentConfig(
    "CHUNK_OVERLAP",
    "rag.chunk_overlap",
    int(os.environ.get("CHUNK_OVERLAP", "100")),
)

DEFAULT_RAG_TEMPLATE = """### Task:
Respond to the user query using the provided context, incorporating inline citations in the format [source_id] **only when the <source_id> tag is explicitly provided** in the context.

### Guidelines:
- If you don't know the answer, clearly state that.
- If uncertain, ask the user for clarification.
- Respond in the same language as the user's query.
- If the context is unreadable or of poor quality, inform the user and provide the best possible answer.
- If the answer isn't present in the context but you possess the knowledge, explain this to the user and provide the answer using your own understanding.
- **Only include inline citations using [source_id] when a <source_id> tag is explicitly provided in the context.**  
- Do not cite if the <source_id> tag is not provided in the context.  
- Do not use XML tags in your response.
- Ensure citations are concise and directly related to the information provided.

### Example of Citation:
If the user asks about a specific topic and the information is found in "whitepaper.pdf" with a provided <source_id>, the response should include the citation like so:  
* "According to the study, the proposed method increases efficiency by 20% [whitepaper.pdf]."
If no <source_id> is present, the response should omit the citation.

### Output:
Provide a clear and direct response to the user's query, including inline citations in the format [source_id] only when the <source_id> tag is present in the context.

<context>
{{CONTEXT}}
</context>

<user_query>
{{QUERY}}
</user_query>
"""

RAG_TEMPLATE = PersistentConfig(
    "RAG_TEMPLATE",
    "rag.template",
    os.environ.get("RAG_TEMPLATE", DEFAULT_RAG_TEMPLATE),
)

RAG_OPENAI_API_BASE_URL = PersistentConfig(
    "RAG_OPENAI_API_BASE_URL",
    "rag.openai_api_base_url",
    os.getenv("RAG_OPENAI_API_BASE_URL", OPENAI_API_BASE_URL),
)
RAG_OPENAI_API_KEY = PersistentConfig(
    "RAG_OPENAI_API_KEY",
    "rag.openai_api_key",
    os.getenv("RAG_OPENAI_API_KEY", OPENAI_API_KEY),
)

RAG_OLLAMA_BASE_URL = PersistentConfig(
    "RAG_OLLAMA_BASE_URL",
    "rag.ollama.url",
    os.getenv("RAG_OLLAMA_BASE_URL", OLLAMA_BASE_URL),
)

RAG_OLLAMA_API_KEY = PersistentConfig(
    "RAG_OLLAMA_API_KEY",
    "rag.ollama.key",
    os.getenv("RAG_OLLAMA_API_KEY", ""),
)


ENABLE_RAG_LOCAL_WEB_FETCH = os.getenv("ENABLE_RAG_LOCAL_WEB_FETCH", "False").lower() == "true"

YOUTUBE_LOADER_LANGUAGE = PersistentConfig(
    "YOUTUBE_LOADER_LANGUAGE",
    "rag.youtube_loader_language",
    os.getenv("YOUTUBE_LOADER_LANGUAGE", "en").split(","),
)

YOUTUBE_LOADER_PROXY_URL = PersistentConfig(
    "YOUTUBE_LOADER_PROXY_URL",
    "rag.youtube_loader_proxy_url",
    os.getenv("YOUTUBE_LOADER_PROXY_URL", ""),
)


ENABLE_RAG_WEB_SEARCH = PersistentConfig(
    "ENABLE_RAG_WEB_SEARCH",
    "rag.web.search.enable",
    os.getenv("ENABLE_RAG_WEB_SEARCH", "False").lower() == "true",
)

# Deep research (cavekit-browse-web-access.md R4) is gated separately from web
# search, not folded into it. Searching and reading a named page are one
# capability -- ask the web something, get a page back. Crawling is a different
# one: it reads a dozen pages, follows links between them, and takes a minute
# and a half doing it. A user who turned on "Web Search" did not ask for that.
#
# Defaults off. An admin turns it on deliberately, per instance.
ENABLE_DEEP_RESEARCH = PersistentConfig(
    "ENABLE_DEEP_RESEARCH",
    "browse.research.enable",
    os.getenv("ENABLE_DEEP_RESEARCH", "False").lower() == "true",
)

RAG_WEB_SEARCH_ENGINE = PersistentConfig(
    "RAG_WEB_SEARCH_ENGINE",
    "rag.web.search.engine",
    os.getenv("RAG_WEB_SEARCH_ENGINE", ""),
)

# You can provide a list of your own websites to filter after performing a web search.
# This ensures the highest level of safety and reliability of the information sources.
RAG_WEB_SEARCH_DOMAIN_FILTER_LIST = PersistentConfig(
    "RAG_WEB_SEARCH_DOMAIN_FILTER_LIST",
    "rag.rag.web.search.domain.filter_list",
    [
        # "wikipedia.com",
        # "wikimedia.org",
        # "wikidata.org",
    ],
)

FIRECRAWL_API_BASE_URL = PersistentConfig(
    "FIRECRAWL_API_BASE_URL",
    "rag.web.firecrawl.base_url",
    os.getenv("FIRECRAWL_API_BASE_URL", ""),
)

FIRECRAWL_API_KEY = PersistentConfig(
    "FIRECRAWL_API_KEY",
    "rag.web.firecrawl.api_key",
    os.getenv("FIRECRAWL_API_KEY", ""),
)

# Knowledge-Base domain crawls (Firecrawl) are crawler behavior, so they honor
# a site's robots.txt Crawl-delay: before a crawl, the target's robots.txt is
# read and Firecrawl's inter-request delay is raised to at least what the site
# asks. This layers ON TOP of the user's static delay (which still applies as
# the per-page render wait) — the effective inter-request delay becomes
# max(static delay, robots Crawl-delay). Default on.
#
# The robots.txt itself is read through the browse connection (the Playwright
# tunnel), so honoring only happens when that connection is configured; without
# it the crawl proceeds on the static delay alone.
KB_CRAWL_RESPECT_ROBOTS_DELAY = PersistentConfig(
    "KB_CRAWL_RESPECT_ROBOTS_DELAY",
    "rag.web.firecrawl.respect_robots_delay",
    os.getenv("KB_CRAWL_RESPECT_ROBOTS_DELAY", "True").lower() == "true",
)

# Web Crawl (chat) — the Knowledge-Base domain crawl, driven by a model instead
# of the KB UI. Same crawl endpoint and pipeline; what differs is who starts it.
# Defaults off: it writes into a knowledge base, so an admin opts in.
ENABLE_WEB_CRAWL = PersistentConfig(
    "ENABLE_WEB_CRAWL",
    "rag.web.crawl.enable",
    os.getenv("ENABLE_WEB_CRAWL", "False").lower() == "true",
)

# Crawl budget for model-driven crawls. Admin-set, never a tool argument: a
# model that could choose these would DoS Firecrawl and bloat the target KB,
# and unlike a transient answer the output is persisted storage.
WEB_CRAWL_MAX_PAGES = PersistentConfig(
    "WEB_CRAWL_MAX_PAGES",
    "rag.web.crawl.max_pages",
    int(os.getenv("WEB_CRAWL_MAX_PAGES", "25")),
)

WEB_CRAWL_MAX_DEPTH = PersistentConfig(
    "WEB_CRAWL_MAX_DEPTH",
    "rag.web.crawl.max_depth",
    int(os.getenv("WEB_CRAWL_MAX_DEPTH", "2")),
)

# self.ai's own, first-class connection to a Playwright browser-automation
# service — independent of the domain-crawl backend (self.crawl/Firecrawl)
# above. See api/selfai_ui/browse/connection.py (cavekit-browse-connection.md).
BROWSE_PLAYWRIGHT_SERVICE_URL = PersistentConfig(
    "BROWSE_PLAYWRIGHT_SERVICE_URL",
    "browse.playwright.base_url",
    os.getenv("BROWSE_PLAYWRIGHT_SERVICE_URL", ""),
)

BROWSE_PLAYWRIGHT_API_KEY = PersistentConfig(
    "BROWSE_PLAYWRIGHT_API_KEY",
    "browse.playwright.api_key",
    os.getenv("BROWSE_PLAYWRIGHT_API_KEY", ""),
)

# Web-access tool tuning (cavekit-browse-web-access.md).
#
# R2: how much of a directly-read page the model may receive. Content past this
# point is truncated, with the truncation stated in the returned text — a
# truncated page is never indistinguishable from a complete one.
BROWSE_FETCH_MAX_CHARS = PersistentConfig(
    "BROWSE_FETCH_MAX_CHARS",
    "browse.fetch.max_chars",
    int(os.getenv("BROWSE_FETCH_MAX_CHARS", "50000")),
)

# R3: cap on how many outbound links a single page may contribute. Bounds the
# model-facing link list, and the research tool's frontier.
BROWSE_MAX_LINKS_PER_PAGE = PersistentConfig(
    "BROWSE_MAX_LINKS_PER_PAGE",
    "browse.fetch.max_links_per_page",
    int(os.getenv("BROWSE_MAX_LINKS_PER_PAGE", "50")),
)

# R5: the deep_research traversal budget. These bound a single tool call, and
# they are deliberately admin-side only — the model cannot read, set, or
# negotiate them. A model that could set its own depth would set it wrong, and
# it would turn an in-chat tool into a load generator aimed at a Playwright
# service shared with every other yard consumer.
#
# Whichever of these binds first ends the traversal; the pages gathered so far
# are returned and the reason is stated, rather than truncating silently.
DEEP_RESEARCH_MAX_DEPTH = PersistentConfig(
    "DEEP_RESEARCH_MAX_DEPTH",
    "browse.research.max_depth",
    int(os.getenv("DEEP_RESEARCH_MAX_DEPTH", "2")),
)

DEEP_RESEARCH_MAX_PAGES = PersistentConfig(
    "DEEP_RESEARCH_MAX_PAGES",
    "browse.research.max_pages",
    int(os.getenv("DEEP_RESEARCH_MAX_PAGES", "10")),
)

# Applies to the WHOLE traversal, not to each fetch. A dozen pages at the
# link-follow profile's own timeout would otherwise be a multi-minute tool call.
DEEP_RESEARCH_MAX_SECONDS = PersistentConfig(
    "DEEP_RESEARCH_MAX_SECONDS",
    "browse.research.max_seconds",
    int(os.getenv("DEEP_RESEARCH_MAX_SECONDS", "90")),
)

DEEP_RESEARCH_CONCURRENCY = PersistentConfig(
    "DEEP_RESEARCH_CONCURRENCY",
    "browse.research.concurrency",
    int(os.getenv("DEEP_RESEARCH_CONCURRENCY", "5")),
)

# Per page, not per call: a dozen pages at web_fetch's whole-page budget would
# bury the conversation. Reading one page in full is web_fetch's job.
DEEP_RESEARCH_MAX_CHARS_PER_PAGE = PersistentConfig(
    "DEEP_RESEARCH_MAX_CHARS_PER_PAGE",
    "browse.research.max_chars_per_page",
    int(os.getenv("DEEP_RESEARCH_MAX_CHARS_PER_PAGE", "6000")),
)

# robots.txt honoring for deep_research (crawler etiquette). Default on:
# link-following across a site is crawler behavior and should respect the file.
DEEP_RESEARCH_RESPECT_ROBOTS = PersistentConfig(
    "DEEP_RESEARCH_RESPECT_ROBOTS",
    "browse.research.respect_robots",
    os.getenv("DEEP_RESEARCH_RESPECT_ROBOTS", "True").lower() == "true",
)

# The User-Agent our browse connection identifies as. robots.txt rules target a
# User-agent, and Crawl-delay/allow decisions are resolved against it, so it
# must be a stable, honest token — not a spoofed browser string.
BROWSE_USER_AGENT = PersistentConfig(
    "BROWSE_USER_AGENT",
    "browse.user_agent",
    os.getenv("BROWSE_USER_AGENT", "self.ai-research"),
)

# A ceiling on how long a single robots.txt Crawl-delay may pause the
# traversal. Some sites declare very large delays; honoring an unbounded one
# would let one origin stall the whole (already wall-clock-bounded) research
# call. At the cap we stop visiting that origin rather than sleep past it.
DEEP_RESEARCH_MAX_CRAWL_DELAY_SECONDS = PersistentConfig(
    "DEEP_RESEARCH_MAX_CRAWL_DELAY_SECONDS",
    "browse.research.max_crawl_delay_seconds",
    float(os.getenv("DEEP_RESEARCH_MAX_CRAWL_DELAY_SECONDS", "10")),
)

SEARXNG_QUERY_URL = PersistentConfig(
    "SEARXNG_QUERY_URL",
    "rag.web.search.searxng_query_url",
    os.getenv("SEARXNG_QUERY_URL", ""),
)

GOOGLE_PSE_API_KEY = PersistentConfig(
    "GOOGLE_PSE_API_KEY",
    "rag.web.search.google_pse_api_key",
    os.getenv("GOOGLE_PSE_API_KEY", ""),
)

GOOGLE_PSE_ENGINE_ID = PersistentConfig(
    "GOOGLE_PSE_ENGINE_ID",
    "rag.web.search.google_pse_engine_id",
    os.getenv("GOOGLE_PSE_ENGINE_ID", ""),
)

BRAVE_SEARCH_API_KEY = PersistentConfig(
    "BRAVE_SEARCH_API_KEY",
    "rag.web.search.brave_search_api_key",
    os.getenv("BRAVE_SEARCH_API_KEY", ""),
)

KAGI_SEARCH_API_KEY = PersistentConfig(
    "KAGI_SEARCH_API_KEY",
    "rag.web.search.kagi_search_api_key",
    os.getenv("KAGI_SEARCH_API_KEY", ""),
)

MOJEEK_SEARCH_API_KEY = PersistentConfig(
    "MOJEEK_SEARCH_API_KEY",
    "rag.web.search.mojeek_search_api_key",
    os.getenv("MOJEEK_SEARCH_API_KEY", ""),
)

SERPSTACK_API_KEY = PersistentConfig(
    "SERPSTACK_API_KEY",
    "rag.web.search.serpstack_api_key",
    os.getenv("SERPSTACK_API_KEY", ""),
)

SERPSTACK_HTTPS = PersistentConfig(
    "SERPSTACK_HTTPS",
    "rag.web.search.serpstack_https",
    os.getenv("SERPSTACK_HTTPS", "True").lower() == "true",
)

SERPER_API_KEY = PersistentConfig(
    "SERPER_API_KEY",
    "rag.web.search.serper_api_key",
    os.getenv("SERPER_API_KEY", ""),
)

SERPLY_API_KEY = PersistentConfig(
    "SERPLY_API_KEY",
    "rag.web.search.serply_api_key",
    os.getenv("SERPLY_API_KEY", ""),
)

TAVILY_API_KEY = PersistentConfig(
    "TAVILY_API_KEY",
    "rag.web.search.tavily_api_key",
    os.getenv("TAVILY_API_KEY", ""),
)

JINA_API_KEY = PersistentConfig(
    "JINA_API_KEY",
    "rag.web.search.jina_api_key",
    os.getenv("JINA_API_KEY", ""),
)

SEARCHAPI_API_KEY = PersistentConfig(
    "SEARCHAPI_API_KEY",
    "rag.web.search.searchapi_api_key",
    os.getenv("SEARCHAPI_API_KEY", ""),
)

SEARCHAPI_ENGINE = PersistentConfig(
    "SEARCHAPI_ENGINE",
    "rag.web.search.searchapi_engine",
    os.getenv("SEARCHAPI_ENGINE", ""),
)

BING_SEARCH_V7_ENDPOINT = PersistentConfig(
    "BING_SEARCH_V7_ENDPOINT",
    "rag.web.search.bing_search_v7_endpoint",
    os.environ.get("BING_SEARCH_V7_ENDPOINT", "https://api.bing.microsoft.com/v7.0/search"),
)

BING_SEARCH_V7_SUBSCRIPTION_KEY = PersistentConfig(
    "BING_SEARCH_V7_SUBSCRIPTION_KEY",
    "rag.web.search.bing_search_v7_subscription_key",
    os.environ.get("BING_SEARCH_V7_SUBSCRIPTION_KEY", ""),
)


RAG_WEB_SEARCH_RESULT_COUNT = PersistentConfig(
    "RAG_WEB_SEARCH_RESULT_COUNT",
    "rag.web.search.result_count",
    int(os.getenv("RAG_WEB_SEARCH_RESULT_COUNT", "3")),
)

RAG_WEB_SEARCH_CONCURRENT_REQUESTS = PersistentConfig(
    "RAG_WEB_SEARCH_CONCURRENT_REQUESTS",
    "rag.web.search.concurrent_requests",
    int(os.getenv("RAG_WEB_SEARCH_CONCURRENT_REQUESTS", "10")),
)


####################################
# Images
####################################

IMAGE_GENERATION_ENGINE = PersistentConfig(
    "IMAGE_GENERATION_ENGINE",
    "image_generation.engine",
    os.getenv("IMAGE_GENERATION_ENGINE", "openai"),
)

ENABLE_IMAGE_GENERATION = PersistentConfig(
    "ENABLE_IMAGE_GENERATION",
    "image_generation.enable",
    os.environ.get("ENABLE_IMAGE_GENERATION", "").lower() == "true",
)
AUTOMATIC1111_BASE_URL = PersistentConfig(
    "AUTOMATIC1111_BASE_URL",
    "image_generation.automatic1111.base_url",
    os.getenv("AUTOMATIC1111_BASE_URL", ""),
)
AUTOMATIC1111_API_AUTH = PersistentConfig(
    "AUTOMATIC1111_API_AUTH",
    "image_generation.automatic1111.api_auth",
    os.getenv("AUTOMATIC1111_API_AUTH", ""),
)

AUTOMATIC1111_CFG_SCALE = PersistentConfig(
    "AUTOMATIC1111_CFG_SCALE",
    "image_generation.automatic1111.cfg_scale",
    (float(os.environ.get("AUTOMATIC1111_CFG_SCALE")) if os.environ.get("AUTOMATIC1111_CFG_SCALE") else None),
)


AUTOMATIC1111_SAMPLER = PersistentConfig(
    "AUTOMATIC1111_SAMPLER",
    "image_generation.automatic1111.sampler",
    (os.environ.get("AUTOMATIC1111_SAMPLER") if os.environ.get("AUTOMATIC1111_SAMPLER") else None),
)

AUTOMATIC1111_SCHEDULER = PersistentConfig(
    "AUTOMATIC1111_SCHEDULER",
    "image_generation.automatic1111.scheduler",
    (os.environ.get("AUTOMATIC1111_SCHEDULER") if os.environ.get("AUTOMATIC1111_SCHEDULER") else None),
)

COMFYUI_BASE_URL = PersistentConfig(
    "COMFYUI_BASE_URL",
    "image_generation.comfyui.base_url",
    os.getenv("COMFYUI_BASE_URL", ""),
)

COMFYUI_API_KEY = PersistentConfig(
    "COMFYUI_API_KEY",
    "image_generation.comfyui.api_key",
    os.getenv("COMFYUI_API_KEY", ""),
)

COMFYUI_DEFAULT_WORKFLOW = """
{
  "3": {
    "inputs": {
      "seed": 0,
      "steps": 20,
      "cfg": 8,
      "sampler_name": "euler",
      "scheduler": "normal",
      "denoise": 1,
      "model": [
        "4",
        0
      ],
      "positive": [
        "6",
        0
      ],
      "negative": [
        "7",
        0
      ],
      "latent_image": [
        "5",
        0
      ]
    },
    "class_type": "KSampler",
    "_meta": {
      "title": "KSampler"
    }
  },
  "4": {
    "inputs": {
      "ckpt_name": "model.safetensors"
    },
    "class_type": "CheckpointLoaderSimple",
    "_meta": {
      "title": "Load Checkpoint"
    }
  },
  "5": {
    "inputs": {
      "width": 512,
      "height": 512,
      "batch_size": 1
    },
    "class_type": "EmptyLatentImage",
    "_meta": {
      "title": "Empty Latent Image"
    }
  },
  "6": {
    "inputs": {
      "text": "Prompt",
      "clip": [
        "4",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "CLIP Text Encode (Prompt)"
    }
  },
  "7": {
    "inputs": {
      "text": "",
      "clip": [
        "4",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "CLIP Text Encode (Prompt)"
    }
  },
  "8": {
    "inputs": {
      "samples": [
        "3",
        0
      ],
      "vae": [
        "4",
        2
      ]
    },
    "class_type": "VAEDecode",
    "_meta": {
      "title": "VAE Decode"
    }
  },
  "9": {
    "inputs": {
      "filename_prefix": "ComfyUI",
      "images": [
        "8",
        0
      ]
    },
    "class_type": "SaveImage",
    "_meta": {
      "title": "Save Image"
    }
  }
}
"""


COMFYUI_WORKFLOW = PersistentConfig(
    "COMFYUI_WORKFLOW",
    "image_generation.comfyui.workflow",
    os.getenv("COMFYUI_WORKFLOW", COMFYUI_DEFAULT_WORKFLOW),
)

# Node-map that binds the abstract knobs (prompt/model/width/height/steps/seed)
# to concrete node ids + input keys in COMFYUI_WORKFLOW. MUST be env-settable:
# with ENABLE_PERSISTENT_CONFIG=False an admin-UI value never survives a pod
# restart, so an unset default of [] silently reverts image-gen to "prompt never
# injected" on every restart. Parsed as a JSON list of
# {type, node_ids, key, value} dicts. (The env-var NAME arg was previously a
# copy-paste "COMFYUI_WORKFLOW" — fixed to its own key.)
COMFYUI_WORKFLOW_NODES = PersistentConfig(
    "COMFYUI_WORKFLOW_NODES",
    "image_generation.comfyui.nodes",
    json.loads(os.getenv("COMFYUI_WORKFLOW_NODES", "[]")),
)

# self.sketch (ComfyUI) VRAM-lease CONTROL base — the pod the GPU-lease broker
# POSTs /api/system/vram-* to when it needs self.sketch to yield VRAM (Color
# epic Phase 2b). Deliberately separate from COMFYUI_BASE_URL (the Phase-4
# image-generation *serving* client, which may carry an API key / path prefix):
# serving vs control, mirroring self.llamolotl's and self.transcribe's split. A
# single string (like TTS_CONTROL_BASE_URL, not llamolotl's list); the transport
# treats blank as "unconfigured" and resolves a clean timeout, never a silent
# success. Defaults to the in-cluster Service so a bare deploy is wired.
SKETCH_CONTROL_BASE_URL = PersistentConfig(
    "SKETCH_CONTROL_BASE_URL",
    "image_generation.sketch.control_base_url",
    os.getenv("SKETCH_CONTROL_BASE_URL", "http://self-sketch:8188"),
)

IMAGES_OPENAI_API_BASE_URL = PersistentConfig(
    "IMAGES_OPENAI_API_BASE_URL",
    "image_generation.openai.api_base_url",
    os.getenv("IMAGES_OPENAI_API_BASE_URL", OPENAI_API_BASE_URL),
)
IMAGES_OPENAI_API_KEY = PersistentConfig(
    "IMAGES_OPENAI_API_KEY",
    "image_generation.openai.api_key",
    os.getenv("IMAGES_OPENAI_API_KEY", OPENAI_API_KEY),
)

IMAGE_SIZE = PersistentConfig("IMAGE_SIZE", "image_generation.size", os.getenv("IMAGE_SIZE", "512x512"))

IMAGE_STEPS = PersistentConfig("IMAGE_STEPS", "image_generation.steps", int(os.getenv("IMAGE_STEPS", 50)))

IMAGE_GENERATION_MODEL = PersistentConfig(
    "IMAGE_GENERATION_MODEL",
    "image_generation.model",
    os.getenv("IMAGE_GENERATION_MODEL", ""),
)

####################################
# Audio
####################################

# Transcription
WHISPER_MODEL = PersistentConfig(
    "WHISPER_MODEL",
    "audio.stt.whisper_model",
    os.getenv("WHISPER_MODEL", "base"),
)

WHISPER_MODEL_DIR = os.getenv("WHISPER_MODEL_DIR", f"{CACHE_DIR}/whisper/models")
WHISPER_MODEL_AUTO_UPDATE = not OFFLINE_MODE and os.environ.get("WHISPER_MODEL_AUTO_UPDATE", "").lower() == "true"


AUDIO_STT_OPENAI_API_BASE_URL = PersistentConfig(
    "AUDIO_STT_OPENAI_API_BASE_URL",
    "audio.stt.openai.api_base_url",
    os.getenv("AUDIO_STT_OPENAI_API_BASE_URL", OPENAI_API_BASE_URL),
)

AUDIO_STT_OPENAI_API_KEY = PersistentConfig(
    "AUDIO_STT_OPENAI_API_KEY",
    "audio.stt.openai.api_key",
    os.getenv("AUDIO_STT_OPENAI_API_KEY", OPENAI_API_KEY),
)

AUDIO_STT_ENGINE = PersistentConfig(
    "AUDIO_STT_ENGINE",
    "audio.stt.engine",
    os.getenv("AUDIO_STT_ENGINE", ""),
)

AUDIO_STT_MODEL = PersistentConfig(
    "AUDIO_STT_MODEL",
    "audio.stt.model",
    os.getenv("AUDIO_STT_MODEL", ""),
)

# Control port for the self-hosted STT backend (self.transcribe). This is the
# management endpoint the transcribe router talks to for model listing (and, in
# later tasks, pull/swap/delete) — the STT analog of LLAMOLOTL_CONTROL_BASE_URLS.
# It is deliberately separate from AUDIO_STT_OPENAI_API_BASE_URL (the OpenAI-
# compatible *serving* endpoint used for transcription requests): serving vs
# control mirror self.llamolotl's split. Empty when no self-hosted STT backend
# exposes a router control port (e.g. plain OpenAI-compatible STT).
AUDIO_STT_CONTROL_BASE_URL = PersistentConfig(
    "AUDIO_STT_CONTROL_BASE_URL",
    "audio.stt.control_base_url",
    os.getenv("AUDIO_STT_CONTROL_BASE_URL", ""),
)

# The persisted set of typed audio connections (cavekit-audio-connections.md R2).
# A dict keyed by each connection's stable id — the audio analog of the text-model
# OLLAMA_API_CONFIGS / OPENAI_API_CONFIGS multi-connection stores. Keying by a
# generated id (not by type or url) is what lets more than one connection of the
# same type coexist, each independently addressable, without one collapsing into a
# sibling. Serialized form: {id: {"type": <AudioConnectionType value>, "fields":
# {...}}}, produced/consumed by selfai_ui.audio.connections.AudioConnectionStore.
#
# Seedable from the AUDIO_CONNECTION_CONFIGS env var (a JSON blob of that same
# shape), mirroring AUDIO_TTS_ENABLED_VOICES / AUDIO_STT_ENABLED_MODELS below.
# This matters under ENABLE_PERSISTENT_CONFIG=False (this yard's GitOps posture),
# where the DB-saved value is ignored every boot and only the env value is
# authoritative — so a self-hosted connection created at runtime via the CRUD
# router does NOT survive a restart. Declaring the connection set here lets it be
# pinned in the manifest and stay durable across reboots. A non-empty value also
# suppresses the legacy-migration mint (main.py's "if not existing" guard), which
# is intended: the declared set is the source of truth.
try:
    AUDIO_CONNECTION_CONFIGS_ENV = json.loads(os.environ.get("AUDIO_CONNECTION_CONFIGS", "{}"))
    if not isinstance(AUDIO_CONNECTION_CONFIGS_ENV, dict):
        AUDIO_CONNECTION_CONFIGS_ENV = {}
except Exception as e:
    print(f"Error loading AUDIO_CONNECTION_CONFIGS: {e}")
    AUDIO_CONNECTION_CONFIGS_ENV = {}

AUDIO_CONNECTION_CONFIGS = PersistentConfig(
    "AUDIO_CONNECTION_CONFIGS",
    "audio.connection_configs",
    AUDIO_CONNECTION_CONFIGS_ENV,
)

# Admin curation of transcribe-router models (cavekit-audio-transcribe-picker R1).
# A map of model-id -> bool recording whether an admin has enabled that model for
# end-user selection. It is deliberately independent of whether a model is
# downloaded or merely pullable: an admin may enable a model that is only
# pullable. Absence of an id means "not enabled" (the conservative default —
# admins curate models in; the picker's R5 fallback covers the none-enabled
# state). Persisted like every other PersistentConfig so a toggle survives reload.
try:
    AUDIO_STT_ENABLED_MODELS_ENV = json.loads(os.environ.get("AUDIO_STT_ENABLED_MODELS", "{}"))
    if not isinstance(AUDIO_STT_ENABLED_MODELS_ENV, dict):
        AUDIO_STT_ENABLED_MODELS_ENV = {}
except Exception as e:
    print(f"Error loading AUDIO_STT_ENABLED_MODELS: {e}")
    AUDIO_STT_ENABLED_MODELS_ENV = {}

AUDIO_STT_ENABLED_MODELS = PersistentConfig(
    "AUDIO_STT_ENABLED_MODELS",
    "audio.stt.enabled_models",
    AUDIO_STT_ENABLED_MODELS_ENV,
)

# Admin curation of TTS voices (cavekit-audio-voice-catalog R5). A map of
# voice-id -> bool recording whether an admin has enabled that voice for
# end-user selection. Mirrors AUDIO_STT_ENABLED_MODELS exactly for consistency:
# absence of an id means "not enabled" (the conservative default — admins curate
# voices in), and it is persisted like every other PersistentConfig so a toggle
# survives reload. The admin catalog view shows all voices regardless; this map
# only narrows the end-user-facing catalog.
try:
    AUDIO_TTS_ENABLED_VOICES_ENV = json.loads(os.environ.get("AUDIO_TTS_ENABLED_VOICES", "{}"))
    if not isinstance(AUDIO_TTS_ENABLED_VOICES_ENV, dict):
        AUDIO_TTS_ENABLED_VOICES_ENV = {}
except Exception as e:
    print(f"Error loading AUDIO_TTS_ENABLED_VOICES: {e}")
    AUDIO_TTS_ENABLED_VOICES_ENV = {}

AUDIO_TTS_ENABLED_VOICES = PersistentConfig(
    "AUDIO_TTS_ENABLED_VOICES",
    "audio.tts.enabled_voices",
    AUDIO_TTS_ENABLED_VOICES_ENV,
)

AUDIO_TTS_OPENAI_API_BASE_URL = PersistentConfig(
    "AUDIO_TTS_OPENAI_API_BASE_URL",
    "audio.tts.openai.api_base_url",
    os.getenv("AUDIO_TTS_OPENAI_API_BASE_URL", OPENAI_API_BASE_URL),
)
AUDIO_TTS_OPENAI_API_KEY = PersistentConfig(
    "AUDIO_TTS_OPENAI_API_KEY",
    "audio.tts.openai.api_key",
    os.getenv("AUDIO_TTS_OPENAI_API_KEY", OPENAI_API_KEY),
)

AUDIO_TTS_API_KEY = PersistentConfig(
    "AUDIO_TTS_API_KEY",
    "audio.tts.api_key",
    os.getenv("AUDIO_TTS_API_KEY", ""),
)

AUDIO_TTS_ENGINE = PersistentConfig(
    "AUDIO_TTS_ENGINE",
    "audio.tts.engine",
    os.getenv("AUDIO_TTS_ENGINE", ""),
)


AUDIO_TTS_MODEL = PersistentConfig(
    "AUDIO_TTS_MODEL",
    "audio.tts.model",
    os.getenv("AUDIO_TTS_MODEL", "tts-1"),  # OpenAI default model
)

AUDIO_TTS_VOICE = PersistentConfig(
    "AUDIO_TTS_VOICE",
    "audio.tts.voice",
    os.getenv("AUDIO_TTS_VOICE", "alloy"),  # OpenAI default voice
)

AUDIO_TTS_SPLIT_ON = PersistentConfig(
    "AUDIO_TTS_SPLIT_ON",
    "audio.tts.split_on",
    os.getenv("AUDIO_TTS_SPLIT_ON", "punctuation"),
)

AUDIO_TTS_AZURE_SPEECH_REGION = PersistentConfig(
    "AUDIO_TTS_AZURE_SPEECH_REGION",
    "audio.tts.azure.speech_region",
    os.getenv("AUDIO_TTS_AZURE_SPEECH_REGION", "eastus"),
)

AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT = PersistentConfig(
    "AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT",
    "audio.tts.azure.speech_output_format",
    os.getenv("AUDIO_TTS_AZURE_SPEECH_OUTPUT_FORMAT", "audio-24khz-160kbitrate-mono-mp3"),
)

# Control port for the self-hosted TTS backend (self.speak). This is the
# endpoint the voice catalog talks to for the connection's *real* voice list —
# the TTS analog of AUDIO_STT_CONTROL_BASE_URL. It is deliberately separate from
# AUDIO_TTS_OPENAI_API_BASE_URL (the OpenAI-compatible *serving* endpoint used
# for synthesis requests): serving vs control mirror self.llamolotl's split.
# Empty when no self-hosted TTS backend exposes a voice-listing control port
# (e.g. a plain OpenAI-compatible or hosted-provider TTS).
AUDIO_TTS_CONTROL_BASE_URL = PersistentConfig(
    "AUDIO_TTS_CONTROL_BASE_URL",
    "audio.tts.control_base_url",
    os.getenv("AUDIO_TTS_CONTROL_BASE_URL", ""),
)


####################################
# LDAP
####################################

ENABLE_LDAP = PersistentConfig(
    "ENABLE_LDAP",
    "ldap.enable",
    os.environ.get("ENABLE_LDAP", "false").lower() == "true",
)

LDAP_SERVER_LABEL = PersistentConfig(
    "LDAP_SERVER_LABEL",
    "ldap.server.label",
    os.environ.get("LDAP_SERVER_LABEL", "LDAP Server"),
)

LDAP_SERVER_HOST = PersistentConfig(
    "LDAP_SERVER_HOST",
    "ldap.server.host",
    os.environ.get("LDAP_SERVER_HOST", "localhost"),
)

LDAP_SERVER_PORT = PersistentConfig(
    "LDAP_SERVER_PORT",
    "ldap.server.port",
    int(os.environ.get("LDAP_SERVER_PORT", "389")),
)

LDAP_ATTRIBUTE_FOR_USERNAME = PersistentConfig(
    "LDAP_ATTRIBUTE_FOR_USERNAME",
    "ldap.server.attribute_for_username",
    os.environ.get("LDAP_ATTRIBUTE_FOR_USERNAME", "uid"),
)

LDAP_APP_DN = PersistentConfig("LDAP_APP_DN", "ldap.server.app_dn", os.environ.get("LDAP_APP_DN", ""))

LDAP_APP_PASSWORD = PersistentConfig(
    "LDAP_APP_PASSWORD",
    "ldap.server.app_password",
    os.environ.get("LDAP_APP_PASSWORD", ""),
)

LDAP_SEARCH_BASE = PersistentConfig("LDAP_SEARCH_BASE", "ldap.server.users_dn", os.environ.get("LDAP_SEARCH_BASE", ""))

LDAP_SEARCH_FILTERS = PersistentConfig(
    "LDAP_SEARCH_FILTER",
    "ldap.server.search_filter",
    os.environ.get("LDAP_SEARCH_FILTER", ""),
)

LDAP_USE_TLS = PersistentConfig(
    "LDAP_USE_TLS",
    "ldap.server.use_tls",
    os.environ.get("LDAP_USE_TLS", "True").lower() == "true",
)

LDAP_CA_CERT_FILE = PersistentConfig(
    "LDAP_CA_CERT_FILE",
    "ldap.server.ca_cert_file",
    os.environ.get("LDAP_CA_CERT_FILE", ""),
)

LDAP_CIPHERS = PersistentConfig("LDAP_CIPHERS", "ldap.server.ciphers", os.environ.get("LDAP_CIPHERS", "ALL"))
