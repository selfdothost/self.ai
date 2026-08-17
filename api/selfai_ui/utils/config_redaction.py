"""Redaction for the config blob returned by `GET /configs/export` (self.ai#95).

Gating `PersistentConfig.save()` stops the config table *accumulating* new
secrets. It does not clean what is already in there, and it does nothing for a
deployment that legitimately runs with `ENABLE_PERSISTENT_CONFIG=True`, where
persisting values is the point. Export therefore redacts regardless of the flag.

**The classification is derived from the registry, not from a hand-maintained
list of paths.** `PERSISTENT_CONFIG_REGISTRY` already knows every config path
and the env name behind it, so classifying by env name and reading the paths
off the registry means a newly added `PersistentConfig` named `*_API_KEY` is
redacted the day it lands, with nobody remembering to update a list here. A
hand-written path list is exactly the thing that silently rots.

Name matching alone would be wrong in both directions, so there are two
explicit escape hatches below: `NOT_SENSITIVE_ENV_NAMES` for entries whose name
matches but which hold no secret (booleans, allowlists), and
`ALWAYS_SENSITIVE_ENV_NAMES` for entries that hold one without saying so.
"""

import logging
import re

from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("MAIN", logging.INFO))


REDACTED = "[redacted]"

# Substrings that mark a config entry as holding a credential.
#
# The trailing `_key`/`_keys` alternative is load-bearing and was added after a
# test caught `BING_SEARCH_V7_SUBSCRIPTION_KEY` slipping through a list of
# specific tokens — vendors name their credentials whatever they like, so match
# the shape ("something ending in KEY") and carve out the exceptions by name.
_SENSITIVE_PATTERN = re.compile(
    r"(api_?keys?|secret|password|passwd|token|credential|access_key|private_key|client_secret|_keys?$|^keys?$)",
    re.IGNORECASE,
)

# Match the pattern but hold no secret. Each of these is a real entry in
# config.py — a boolean switch or an allowlist — and redacting them would hide
# operationally useful state for no security gain.
NOT_SENSITIVE_ENV_NAMES = frozenset(
    {
        "ENABLE_API_KEY",
        "ENABLE_API_KEY_ENDPOINT_RESTRICTIONS",
        "API_KEY_ALLOWED_ENDPOINTS",
        # "TIKTOKEN" contains "token"; this is an encoding name like "cl100k_base".
        "TIKTOKEN_ENCODING_NAME",
    }
)

# Hold a credential without a matching name. Both of these were found by
# sweeping the registry for credential-adjacent words the pattern does not
# cover, rather than by reading the pattern and hoping.
ALWAYS_SENSITIVE_ENV_NAMES = frozenset(
    {
        # Slack and Discord embed the secret in the path, so the URL *is* the token.
        "WEBHOOK_URL",
        # HTTP basic credentials ("user:password") for the AUTOMATIC1111 API.
        # Nothing in the name says "secret", and it is one.
        "AUTOMATIC1111_API_AUTH",
    }
)


def is_sensitive_env_name(env_name: str) -> bool:
    if env_name in NOT_SENSITIVE_ENV_NAMES:
        return False
    if env_name in ALWAYS_SENSITIVE_ENV_NAMES:
        return True
    return bool(_SENSITIVE_PATTERN.search(env_name))


def sensitive_config_paths() -> set:
    """Dotted config paths that must never leave the process in the clear.

    Read off the live registry, so it reflects this build's actual config
    surface rather than a snapshot someone took once.
    """
    from selfai_ui.config import PERSISTENT_CONFIG_REGISTRY

    return {entry.config_path for entry in PERSISTENT_CONFIG_REGISTRY if is_sensitive_env_name(entry.env_name)}


def _redact_value(value):
    """Redact a leaf, preserving shape.

    Empty values are left alone on purpose: an empty string is not a secret,
    and showing it is how an admin can tell "not configured" apart from
    "configured and hidden". Lists redact element-wise — `OPENAI_API_KEYS` is a
    list of live keys, and collapsing it to a single marker would lose the
    count, which is meaningful.
    """
    if value is None or value == "" or value == [] or value == {}:
        return value
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, bool) or isinstance(value, (int, float)):
        # A numeric or boolean setting under a sensitive-looking name is a
        # toggle or a count, not a credential.
        return value
    return REDACTED


def redact_config(config: dict) -> dict:
    """Return a copy of the config blob with credentials replaced.

    Two passes, deliberately belt-and-braces:

    1. Every path the registry says is sensitive.
    2. Any remaining leaf whose own key looks sensitive — this catches values
       that reached the table through `POST /configs/import` or a previous
       build's config surface, which the current registry knows nothing about.
    """
    known_sensitive = sensitive_config_paths()
    redacted_count = 0

    def walk(node, path=""):
        nonlocal redacted_count
        if isinstance(node, dict):
            return {key: walk(value, f"{path}.{key}" if path else key) for key, value in node.items()}
        if isinstance(node, list):
            # A list under a non-sensitive path may still contain dicts with
            # sensitive keys inside (connection configs, for example).
            return [walk(item, path) for item in node]

        leaf_key = path.rsplit(".", maxsplit=1)[-1]
        if path in known_sensitive or is_sensitive_env_name(leaf_key):
            new_value = _redact_value(node)
            if new_value != node:
                redacted_count += 1
            return new_value
        return node

    result = walk(config or {})
    if redacted_count:
        log.info(f"config export: redacted {redacted_count} sensitive value(s)")
    return result
