import importlib.metadata
import json
import logging
import os
import pkgutil
import shutil
import sys
from pathlib import Path

import markdown
from bs4 import BeautifulSoup

from selfai_ui.constants import ERROR_MESSAGES

####################################
# Load .env file
####################################

SELFAI_UI_DIR = Path(__file__).parent  # the path containing this file
print(SELFAI_UI_DIR)

BACKEND_DIR = SELFAI_UI_DIR.parent  # the path containing this file
BASE_DIR = BACKEND_DIR.parent  # the path containing the backend/

print(BACKEND_DIR)
print(BASE_DIR)

try:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(str(BASE_DIR / ".env")))
except ImportError:
    print("dotenv not installed, skipping...")

DOCKER = os.environ.get("DOCKER", "False").lower() == "true"

# device type embedding models - "cpu" (default), "cuda" (nvidia gpu required)
# or "mps" (apple silicon) - choosing this right can lead to better performance
USE_CUDA = os.environ.get("USE_CUDA_DOCKER", "false")

if USE_CUDA.lower() == "true":
    try:
        import torch

        assert torch.cuda.is_available(), "CUDA not available"
        DEVICE_TYPE = "cuda"
    except Exception as e:
        cuda_error = "Error when testing CUDA but USE_CUDA_DOCKER is true. " f"Resetting USE_CUDA_DOCKER to false: {e}"
        os.environ["USE_CUDA_DOCKER"] = "false"
        USE_CUDA = "false"
        DEVICE_TYPE = "cpu"
else:
    DEVICE_TYPE = "cpu"

try:
    if torch.backends.mps.is_available() and torch.backends.mps.is_built():
        DEVICE_TYPE = "mps"
except Exception:
    pass

####################################
# LOGGING
####################################

log_levels = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]

GLOBAL_LOG_LEVEL = os.environ.get("GLOBAL_LOG_LEVEL", "").upper()
if GLOBAL_LOG_LEVEL in log_levels:
    logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL, force=True)
else:
    GLOBAL_LOG_LEVEL = "INFO"

log = logging.getLogger(__name__)
log.info(f"GLOBAL_LOG_LEVEL: {GLOBAL_LOG_LEVEL}")

if "cuda_error" in locals():
    log.exception(cuda_error)

log_sources = [
    "ANTHROPIC",
    "AUDIO",
    "COMFYUI",
    "CONFIG",
    "CODE_EVAL",
    "CURATOR",
    "DB",
    "ICEBERG",
    "IMAGES",
    "MAIN",
    "MODELS",
    "LLAMOLOTL",
    "LANGUAGE_EVAL",
    "OLLAMA",
    "OPENAI",
    "RAG",
    "WEBHOOK",
    "SOCKET",
]

SRC_LOG_LEVELS = {}

for source in log_sources:
    log_env_var = source + "_LOG_LEVEL"
    SRC_LOG_LEVELS[source] = os.environ.get(log_env_var, "").upper()
    if SRC_LOG_LEVELS[source] not in log_levels:
        SRC_LOG_LEVELS[source] = GLOBAL_LOG_LEVEL
    log.info(f"{log_env_var}: {SRC_LOG_LEVELS[source]}")

log.setLevel(SRC_LOG_LEVELS["CONFIG"])


WEBUI_NAME = os.environ.get("WEBUI_NAME", "Self.AI")
# if WEBUI_NAME != "Self.AI UI":
#     WEBUI_NAME += " (Self.AI UI)"

# get the favicon from the container
WEBUI_FAVICON_URL = os.environ.get(
    "WEBUI_FAVICON_URL",
    f"{os.environ.get('WEBUI_URL', 'http://localhost:3000')}/static/favicon.png",
)


####################################
# ENV (dev,test,prod)
####################################

ENV = os.environ.get("ENV", "dev")

FROM_INIT_PY = os.environ.get("FROM_INIT_PY", "False").lower() == "true"

if FROM_INIT_PY:
    PACKAGE_DATA = {"version": importlib.metadata.version("selfai-ui")}
else:
    # Read the version from api/package.json, which is BACKEND_DIR in the image
    # (the Dockerfile COPYs the api/ build context into /app/backend). The old
    # BASE_DIR path was the repo root -- an Open-WebUI monolith holdover that is
    # OUTSIDE the split API image's build context, so it never resolved and
    # VERSION silently fell to 0.0.0 in every deployment. That made every mod's
    # min_core_version (the contract examples use 0.5.0) fail the version gate.
    # BASE_DIR is kept as a fallback for a monolith/combined layout.
    for candidate in (BACKEND_DIR / "package.json", BASE_DIR / "package.json"):
        try:
            PACKAGE_DATA = json.loads(candidate.read_text())
            break
        except Exception:
            continue
    else:
        PACKAGE_DATA = {"version": "0.0.0"}


VERSION = PACKAGE_DATA["version"]


# Function to parse each section
def parse_section(section):
    items = []
    for li in section.find_all("li"):
        # Extract raw HTML string
        raw_html = str(li)

        # Extract text without HTML tags
        text = li.get_text(separator=" ", strip=True)

        # Split into title and content
        parts = text.split(": ", 1)
        title = parts[0].strip() if len(parts) > 1 else ""
        content = parts[1].strip() if len(parts) > 1 else text

        items.append({"title": title, "content": content, "raw": raw_html})
    return items


try:
    changelog_path = BASE_DIR / "CHANGELOG.md"
    with open(str(changelog_path.absolute()), "r", encoding="utf8") as file:
        changelog_content = file.read()

except Exception:
    try:
        changelog_content = (pkgutil.get_data("selfai_ui", "CHANGELOG.md") or b"").decode()
    except Exception:
        # CHANGELOG is non-essential (it only feeds the version modal). The slim
        # API image doesn't bundle it as package data, and a missing changelog
        # must never crash boot — degrade to empty.
        changelog_content = ""


# Convert markdown content to HTML
html_content = markdown.markdown(changelog_content)

# Parse the HTML content
soup = BeautifulSoup(html_content, "html.parser")

# Initialize JSON structure
changelog_json = {}

# Iterate over each version
for version in soup.find_all("h2"):
    # Upstream expected every h2 to be "[<version>] - <date>" and crashed at
    # import on anything else (IndexError on the split) — which took out test
    # collection and any deploy that bundles CHANGELOG.md the moment the weekly
    # "## self.ai — <date>" headings landed. This feeds the version modal only;
    # tolerate any heading shape.
    heading = version.get_text().strip()
    parts = heading.split(" - ", 1)
    version_number = parts[0].strip("[]")
    date = parts[1] if len(parts) > 1 else ""

    version_data = {"date": date}

    # Find the next sibling that is a h3 tag (section title)
    current = version.find_next_sibling()

    while current and current.name != "h2":
        if current.name == "h3":
            section_title = current.get_text().lower()  # e.g., "added", "fixed"
            section_items = parse_section(current.find_next_sibling("ul"))
            version_data[section_title] = section_items

        # Move to the next element
        current = current.find_next_sibling()

    changelog_json[version_number] = version_data


CHANGELOG = changelog_json

####################################
# SAFE_MODE
####################################

SAFE_MODE = os.environ.get("SAFE_MODE", "false").lower() == "true"

####################################
# ENABLE_FORWARD_USER_INFO_HEADERS
####################################

ENABLE_FORWARD_USER_INFO_HEADERS = os.environ.get("ENABLE_FORWARD_USER_INFO_HEADERS", "False").lower() == "true"


####################################
# WEBUI_BUILD_HASH
####################################

WEBUI_BUILD_HASH = os.environ.get("WEBUI_BUILD_HASH", "dev-build")

####################################
# DATA/FRONTEND BUILD DIR
####################################

DATA_DIR = Path(os.getenv("DATA_DIR", BACKEND_DIR / "data")).resolve()

if FROM_INIT_PY:
    NEW_DATA_DIR = Path(os.getenv("DATA_DIR", SELFAI_UI_DIR / "data")).resolve()
    NEW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if the data directory exists in the package directory
    if DATA_DIR.exists() and DATA_DIR != NEW_DATA_DIR:
        log.info(f"Moving {DATA_DIR} to {NEW_DATA_DIR}")
        for item in DATA_DIR.iterdir():
            dest = NEW_DATA_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        # Zip the data directory
        shutil.make_archive(DATA_DIR.parent / "selfai_ui_data", "zip", DATA_DIR)

        # Remove the old data directory
        shutil.rmtree(DATA_DIR)

    DATA_DIR = Path(os.getenv("DATA_DIR", SELFAI_UI_DIR / "data"))


# Create DATA_DIR, and prove it is writable, before anything tries to use it
# (self.ai#120).
#
# Without this, pointing DATA_DIR at a path that does not exist fails several
# frames from the cause: DATABASE_URL defaults to sqlite under DATA_DIR, sqlite
# will not create a missing parent, and the operator sees
# `sqlite3.OperationalError: unable to open database file` wrapped in a
# MigrationError. Nothing in that stack says DATA_DIR, so it reads as a
# permissions fault. The default /app/backend/data ships in the image, so this
# stays invisible until someone follows the advice we give for the combined
# image -- mount a volume, point DATA_DIR at it -- which is the on-ramp new
# users take.
#
# DELIBERATELY placed here rather than at the DATA_DIR assignment above, which
# is where it looks like it belongs. The FROM_INIT_PY block migrates the old
# package-relative data directory only `if DATA_DIR.exists()`; creating the
# directory before it would make that branch fire on every CLI run, archive an
# empty directory to selfai_ui_data.zip and delete it. By this point
# FROM_INIT_PY has already created NEW_DATA_DIR and repointed DATA_DIR at it,
# so the mkdir below is a no-op on that path.
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError as e:
    raise RuntimeError(
        f"DATA_DIR could not be created: {DATA_DIR} ({e.strerror}). "
        "self.ai stores uploads, caches and -- unless DATABASE_URL points "
        "elsewhere -- its SQLite database here. Set DATA_DIR to a writable "
        "path, or mount a writable volume at this one."
    ) from e

# os.access() is not enough: it consults the real uid and lies under a
# read-only mount or when running as root over a read-only filesystem. Actually
# writing is the only answer that matches what the app will do a moment later.
_probe = DATA_DIR / ".selfai-write-probe"
try:
    _probe.touch()
    _probe.unlink()
except OSError as e:
    raise RuntimeError(
        f"DATA_DIR exists but is not writable: {DATA_DIR} ({e.strerror}). "
        "self.ai stores uploads, caches and -- unless DATABASE_URL points "
        "elsewhere -- its SQLite database here. A non-root or read-only-rootfs "
        "deployment needs a writable volume mounted at DATA_DIR."
    ) from e
finally:
    del _probe


STATIC_DIR = Path(os.getenv("STATIC_DIR", SELFAI_UI_DIR / "static"))

FONTS_DIR = Path(os.getenv("FONTS_DIR", SELFAI_UI_DIR / "static" / "fonts"))

FRONTEND_BUILD_DIR = Path(os.getenv("FRONTEND_BUILD_DIR", BASE_DIR / "build")).resolve()

if FROM_INIT_PY:
    FRONTEND_BUILD_DIR = Path(os.getenv("FRONTEND_BUILD_DIR", SELFAI_UI_DIR / "frontend")).resolve()


####################################
# Database
####################################

# Check if the file exists
if os.path.exists(f"{DATA_DIR}/ollama.db"):
    # Rename the file
    os.rename(f"{DATA_DIR}/ollama.db", f"{DATA_DIR}/webui.db")
    log.info("Database migrated from Ollama-WebUI successfully.")
else:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/webui.db")

# Replace the postgres:// with postgresql://
if "postgres://" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

DATABASE_POOL_SIZE = os.environ.get("DATABASE_POOL_SIZE", 0)

if DATABASE_POOL_SIZE == "":
    DATABASE_POOL_SIZE = 0
else:
    try:
        DATABASE_POOL_SIZE = int(DATABASE_POOL_SIZE)
    except Exception:
        DATABASE_POOL_SIZE = 0

DATABASE_POOL_MAX_OVERFLOW = os.environ.get("DATABASE_POOL_MAX_OVERFLOW", 0)

if DATABASE_POOL_MAX_OVERFLOW == "":
    DATABASE_POOL_MAX_OVERFLOW = 0
else:
    try:
        DATABASE_POOL_MAX_OVERFLOW = int(DATABASE_POOL_MAX_OVERFLOW)
    except Exception:
        DATABASE_POOL_MAX_OVERFLOW = 0

DATABASE_POOL_TIMEOUT = os.environ.get("DATABASE_POOL_TIMEOUT", 30)

if DATABASE_POOL_TIMEOUT == "":
    DATABASE_POOL_TIMEOUT = 30
else:
    try:
        DATABASE_POOL_TIMEOUT = int(DATABASE_POOL_TIMEOUT)
    except Exception:
        DATABASE_POOL_TIMEOUT = 30

DATABASE_POOL_RECYCLE = os.environ.get("DATABASE_POOL_RECYCLE", 3600)

if DATABASE_POOL_RECYCLE == "":
    DATABASE_POOL_RECYCLE = 3600
else:
    try:
        DATABASE_POOL_RECYCLE = int(DATABASE_POOL_RECYCLE)
    except Exception:
        DATABASE_POOL_RECYCLE = 3600

RESET_CONFIG_ON_START = os.environ.get("RESET_CONFIG_ON_START", "False").lower() == "true"


ENABLE_REALTIME_CHAT_SAVE = os.environ.get("ENABLE_REALTIME_CHAT_SAVE", "False").lower() == "true"

####################################
# REDIS
####################################

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

####################################
# WEBUI_AUTH (Required for security)
####################################

WEBUI_AUTH = os.environ.get("WEBUI_AUTH", "True").lower() == "true"
WEBUI_AUTH_TRUSTED_EMAIL_HEADER = os.environ.get("WEBUI_AUTH_TRUSTED_EMAIL_HEADER", None)
WEBUI_AUTH_TRUSTED_NAME_HEADER = os.environ.get("WEBUI_AUTH_TRUSTED_NAME_HEADER", None)

BYPASS_MODEL_ACCESS_CONTROL = os.environ.get("BYPASS_MODEL_ACCESS_CONTROL", "False").lower() == "true"

####################################
# WEBUI_SECRET_KEY
####################################

WEBUI_SECRET_KEY = os.environ.get(
    "WEBUI_SECRET_KEY",
    os.environ.get("WEBUI_JWT_SECRET_KEY", "t0p-s3cr3t"),  # DEPRECATED: remove at next major version
)

WEBUI_SESSION_COOKIE_SAME_SITE = os.environ.get(
    "WEBUI_SESSION_COOKIE_SAME_SITE",
    os.environ.get("WEBUI_SESSION_COOKIE_SAME_SITE", "lax"),
)

_cookie_secure_env = os.environ.get("WEBUI_SESSION_COOKIE_SECURE", "")
if _cookie_secure_env:
    WEBUI_SESSION_COOKIE_SECURE = _cookie_secure_env.lower() == "true"
else:
    # Default to secure cookies in non-dev mode
    WEBUI_SESSION_COOKIE_SECURE = ENV != "dev"

if WEBUI_AUTH and WEBUI_SECRET_KEY == "":
    raise ValueError(ERROR_MESSAGES.ENV_VAR_NOT_FOUND)

if WEBUI_AUTH and WEBUI_SECRET_KEY == "t0p-s3cr3t":
    raise ValueError(
        "WEBUI_SECRET_KEY is set to the well-known default 't0p-s3cr3t'. "
        "This is a critical security risk — anyone can forge admin tokens. "
        "Set WEBUI_SECRET_KEY to a unique, random value in your environment."
    )

####################################
# SERVICE_AUTH (internal ticket-granting mesh — self.ai#25)
####################################

# Shared HMAC secret self.ai signs internal service tickets with. Distinct
# from WEBUI_SECRET_KEY (that one signs user session tokens; this one signs
# short-lived, scoped tickets self.ai mints for itself right before calling
# a backend like self.llamolotl). Empty by default: minting simply isn't
# attempted until wiring is deployed, so an unset secret does not block
# self.ai booting for deployments that don't have a backend needing tickets
# yet — see selfai_ui/utils/service_auth.py, which raises at call time
# (not import time) if this is unset when a mint is actually requested.
SERVICE_AUTH_SECRET = os.environ.get("SERVICE_AUTH_SECRET", "")
# The `iss` claim on every ticket self.ai mints. Backends can use this to
# confirm the ticket came from self.ai specifically (defense alongside the
# signature check, not instead of it).
SERVICE_AUTH_ISSUER = os.environ.get("SERVICE_AUTH_ISSUER", "self.ai")
# self.ai's own audience identity for tickets minted *toward* it by a peer
# service (the inbound direction). Every other mesh backend validates tickets
# against its own SERVICE_AUTH_AUDIENCE; when self.ai is itself the callee
# (e.g. a consumer like self.llamolotl calling the VRAM lease broker), it
# validates the `aud` claim against this. Defaults to "self.ai" — the same
# identity string SERVICE_AUTH_ISSUER uses, since self.ai is both the mesh's
# ticket-granting service and, for the broker, a ticket-consuming callee.
SERVICE_AUTH_AUDIENCE = os.environ.get("SERVICE_AUTH_AUDIENCE", "self.ai")

# --- MCP front door: who may register a backend, and where it may point ---
# self.ai#25. Registration decides where /mcp/{name} points, and the proxy
# forwards the caller's OWN backend credential there (a GitLab PAT, an IPA
# password). So whoever controls a name can harvest the credentials of everyone
# who uses it, and both of these are security controls rather than convenience
# config.
#
# MCP_REGISTRARS: comma-separated self.ai user ids and/or emails allowed to
# register. Empty (the default) means registration is CLOSED — the API returns
# 403 for everyone, and the front door behaves exactly as it did in phase 1 with
# only the GitOps entries in MCP_PROXY_BACKENDS serving. Fail-closed is the right
# default for a surface that can redirect other people's credentials.
#
# Deliberately not a service ticket: self.ai#79 established that
# require_service_ticket proves possession of one shared SERVICE_AUTH_SECRET and
# nothing more (`iss` is unpinned), so every mesh service can act as every other.
# A user id is a real identity, revocable by rotating one API key.
MCP_REGISTRARS = {
    entry.strip()
    for entry in os.environ.get("MCP_REGISTRARS", "").split(",")
    if entry.strip()
}

# MCP_REGISTRY_ALLOWED_NAMESPACES: the k8s namespaces a registered backend URL
# may live in. This is the control that survives a leaked registrar credential —
# a stolen key still cannot point a backend at an attacker-controlled host
# off-cluster, so credentials can only ever be forwarded somewhere the platform
# already governs. Defaults to the two namespaces the front door exists to
# serve: our own, and the crew's MCP servers.
MCP_REGISTRY_ALLOWED_NAMESPACES = {
    entry.strip()
    for entry in os.environ.get(
        "MCP_REGISTRY_ALLOWED_NAMESPACES", "self-ai,crew-system"
    ).split(",")
    if entry.strip()
}

# --- GPU VRAM lease broker: config-driven self.llamolotl registration (R4) ---
# self.llamolotl is the single known VRAM consumer this phase registers at
# startup (cavekit-gpu-lease-broker R4/AC1 — manual/config-driven is acceptable
# for one known instance; not required to be self-service/dynamic yet). Left
# empty by default: an unset capacity means "unconfigured", so startup skips the
# registration and logs it rather than crashing (self.ai must boot fine on
# deployments with no llamolotl / no lease broker wired). Kept as the raw string
# here and parsed defensively at use so a malformed value degrades to "skip",
# never an import-time crash.
LLAMOLOTL_VRAM_CAPACITY_BYTES = os.environ.get("LLAMOLOTL_VRAM_CAPACITY_BYTES", "")
# Reclamation priority for self.llamolotl's lease (lower = asked to release
# first when a later grant needs to reclaim). Integer, defaults to 0.
LLAMOLOTL_VRAM_LEASE_PRIORITY = int(os.environ.get("LLAMOLOTL_VRAM_LEASE_PRIORITY", "0"))
# R5 force-reap pod identity for self.llamolotl: the k8s namespace and a pod
# selector (label selector like "app=self-llamolotl", or a Deployment name the
# reaper resolves to pods) core uses to delete the pod when it goes stale and
# won't cooperate with a release. Both blank by default = unconfigured = the
# consumer is registered but never force-reap eligible (opt-in via config,
# cavekit-gpu-lease-broker R5/AC7). Parsed defensively at registration
# (blank -> None), mirroring the capacity/priority block above.
LLAMOLOTL_K8S_NAMESPACE = os.environ.get("LLAMOLOTL_K8S_NAMESPACE", "")
LLAMOLOTL_K8S_POD_SELECTOR = os.environ.get("LLAMOLOTL_K8S_POD_SELECTOR", "")

# --- GPU VRAM lease broker: config-driven self.speak registration ---
# self.speak is the broker's SECOND config-driven VRAM consumer (mirrors the
# llamolotl block above; cavekit-vram-speak-consumer R1). Same discipline: kept
# as the raw string and parsed defensively at use so a malformed value degrades
# to "skip" rather than crashing at import; unset default "" means
# "unconfigured" → startup skips the self.speak registration and logs it.
SPEAK_VRAM_CAPACITY_BYTES = os.environ.get("SPEAK_VRAM_CAPACITY_BYTES", "")
# Reclamation priority for self.speak's lease (lower = asked to release first).
# Integer, defaults to 0; the manifest supplies 5 (below llamolotl's 10) so the
# audio path yields before the inference brain.
SPEAK_VRAM_LEASE_PRIORITY = int(os.environ.get("SPEAK_VRAM_LEASE_PRIORITY", "0"))

# --- GPU VRAM lease broker: config-driven self.sketch registration ---
# self.sketch (ComfyUI image generation) is the broker's THIRD config-driven VRAM
# consumer (Color epic Phase 2b; same shape as the speak block above). Same
# discipline: kept as the raw string and parsed defensively at use so a malformed
# value degrades to "skip" rather than crashing at import; unset default "" means
# "unconfigured" → startup skips the self.sketch registration and logs it.
SKETCH_VRAM_CAPACITY_BYTES = os.environ.get("SKETCH_VRAM_CAPACITY_BYTES", "")
# Reclamation priority for self.sketch's lease (lower = asked to release first).
# Integer, defaults to 0; the manifest supplies 3 (below self.speak's 5, itself
# below llamolotl's 10) so image generation — the most interruptible, bursty
# workload — yields VRAM before both audio and the inference brain.
SKETCH_VRAM_LEASE_PRIORITY = int(os.environ.get("SKETCH_VRAM_LEASE_PRIORITY", "0"))

# --- GPU VRAM lease broker: config-driven self.curator registration ---
# self.curator (NeMo Curator data curation) is the broker's FOURTH config-driven
# VRAM consumer (self.ai#88; same shape as the sketch block above). It was the
# last GPU pod on the shared 4090 that the broker could not see — an invisible
# holder core would over-grant against. Same discipline: raw string parsed
# defensively at use, unset "" means "unconfigured" → startup skips the
# registration and logs it rather than crashing.
CURATOR_VRAM_CAPACITY_BYTES = os.environ.get("CURATOR_VRAM_CAPACITY_BYTES", "")
# Reclamation priority for self.curator's lease (lower = asked to release
# first). Integer, defaults to 0; the manifest supplies 8 — below llamolotl's 10
# so curation never pre-empts the inference brain, above self.speak (5) and
# self.sketch (3) so a curator window can actually clear voices and image
# generation off the card, which is what makes the window's promise real.
CURATOR_VRAM_LEASE_PRIORITY = int(os.environ.get("CURATOR_VRAM_LEASE_PRIORITY", "0"))
# Force-reap pod identity for self.curator (R5 opt-in, same contract as the
# llamolotl block above): both blank = registered but never force-reap eligible.
CURATOR_K8S_NAMESPACE = os.environ.get("CURATOR_K8S_NAMESPACE", "")
CURATOR_K8S_POD_SELECTOR = os.environ.get("CURATOR_K8S_POD_SELECTOR", "")

# A PUBLISH (self.ai#136) is the broker's FIFTH config-driven consumer, and the
# odd one out: the VRAM is not held by a pod of its own. A publish merges
# adapters into a base through self.llamolotl's pipeline, which loads the base
# at fp16 with device_map="auto" (api/merge_lora.py:51-52), so the hold lands
# in a pipeline SUBPROCESS inside the llamolotl pod — not in llama-server, and
# not anywhere the llamolotl row accounts for.
#
# It registers under its own consumer id precisely because of that: acquiring
# as `self.llamolotl` would mark the serving process the exclusive holder while
# the serving process is exactly what has to give its VRAM up first.
#
# Same discipline as the four blocks above: unset "" means unconfigured, so
# startup skips the registration and logs it rather than crashing, and
# gpu_queue._publish_is_lease_consumer() then reports False and publishes
# dispatch without a lease exactly as they did before this existed.
PUBLISH_VRAM_CAPACITY_BYTES = os.environ.get("PUBLISH_VRAM_CAPACITY_BYTES", "")
# Reclamation priority for a publish (lower = asked to release first). Integer,
# defaults to 0; the manifest supplies 8 — deliberately the SAME tier as
# self.curator, because a publish is the same kind of act: a scheduled,
# window-gated job that needs the whole card and must be able to clear
# self.speak (5) and self.sketch (3) off it, while never casually pre-empting
# the inference brain at llamolotl's 10. A publish and a curator run cannot
# both hold the card regardless of this number: both take the card through
# acquire_exclusive, and the first one holding it denies the second.
PUBLISH_VRAM_LEASE_PRIORITY = int(os.environ.get("PUBLISH_VRAM_LEASE_PRIORITY", "0"))
# No force-reap identity, deliberately: the merge runs inside self.llamolotl's
# pod, so reaping "the publish" would mean killing llamolotl — which is
# llamolotl's own row's business, under its own namespace/selector. A publish is
# registered but never force-reap eligible.

ENABLE_WEBSOCKET_SUPPORT = os.environ.get("ENABLE_WEBSOCKET_SUPPORT", "True").lower() == "true"

WEBSOCKET_MANAGER = os.environ.get("WEBSOCKET_MANAGER", "")

WEBSOCKET_REDIS_URL = os.environ.get("WEBSOCKET_REDIS_URL", REDIS_URL)

# Prefix for the Redis keys this app owns (the socket session/user/usage pools
# and the cleanup lock). Lets a shared/ACL-scoped Valkey restrict us to one
# keyspace (e.g. `~selfai:*`). Defaults to the upstream "open-webui".
REDIS_KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "open-webui")

AIOHTTP_CLIENT_TIMEOUT = os.environ.get("AIOHTTP_CLIENT_TIMEOUT", "")

if AIOHTTP_CLIENT_TIMEOUT == "":
    AIOHTTP_CLIENT_TIMEOUT = None
else:
    try:
        AIOHTTP_CLIENT_TIMEOUT = int(AIOHTTP_CLIENT_TIMEOUT)
    except Exception:
        AIOHTTP_CLIENT_TIMEOUT = 300

AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST = os.environ.get("AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST", "")

if AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST == "":
    AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST = None
else:
    try:
        AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST = int(AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    except Exception:
        AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST = 5

####################################
# OFFLINE_MODE
####################################

OFFLINE_MODE = os.environ.get("OFFLINE_MODE", "false").lower() == "true"

if OFFLINE_MODE:
    os.environ["HF_HUB_OFFLINE"] = "1"
