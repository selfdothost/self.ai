"""Config export must not hand out live secrets (self.ai#95).

The bug had two halves sharing one flag. `ENABLE_PERSISTENT_CONFIG=False` reads
as though it keeps secrets out of the config table; it gated only the **boot**
read (`config.py:300`), while `save()` wrote unconditionally and `update()`
adopted from the database unconditionally. So every save copied a live
env/vault value into Postgres, `GET /configs/export` returned the lot
unredacted, and `POST /configs/import` could override env-sourced config at
runtime until the next restart silently reverted it.
"""

import pytest

from selfai_ui.utils.config_redaction import (
    ALWAYS_SENSITIVE_ENV_NAMES,
    NOT_SENSITIVE_ENV_NAMES,
    REDACTED,
    is_sensitive_env_name,
    redact_config,
    sensitive_config_paths,
)

pytestmark = pytest.mark.tier0


####################
# Classification
####################


@pytest.mark.parametrize(
    "env_name",
    [
        "OPENAI_API_KEYS",
        "BRAVE_SEARCH_API_KEY",
        "SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY",
        "SELF_CORPUS_LAKEFS_ACCESS_KEY_ID",
        "GOOGLE_CLIENT_SECRET",
        "MICROSOFT_CLIENT_SECRET",
        "OAUTH_CLIENT_SECRET",
        "LDAP_APP_PASSWORD",
        "JINA_API_KEY",
        "TAVILY_API_KEY",
        "FIRECRAWL_API_KEY",
        "COMFYUI_API_KEY",
        "AUDIO_TTS_OPENAI_API_KEY",
        "BING_SEARCH_V7_SUBSCRIPTION_KEY",
    ],
)
def test_credential_bearing_names_are_sensitive(env_name):
    assert is_sensitive_env_name(env_name)


@pytest.mark.parametrize("env_name", sorted(NOT_SENSITIVE_ENV_NAMES))
def test_named_exceptions_are_not_treated_as_secrets(env_name):
    """These match the pattern but hold a boolean or an allowlist. Redacting
    them would hide useful operational state for no security gain."""
    assert not is_sensitive_env_name(env_name)


@pytest.mark.parametrize("env_name", sorted(ALWAYS_SENSITIVE_ENV_NAMES))
def test_unnamed_credentials_are_still_caught(env_name):
    """Slack/Discord webhook URLs embed the secret in the path — the URL is
    the token, despite the name saying nothing about it."""
    assert is_sensitive_env_name(env_name)


@pytest.mark.parametrize(
    "env_name",
    ["ENABLE_SELF_CORPUS", "DEFAULT_MODELS", "WEBUI_NAME", "TASK_MODEL", "RAG_TOP_K"],
)
def test_ordinary_settings_are_not_redacted(env_name):
    assert not is_sensitive_env_name(env_name)


def test_classification_is_derived_from_the_live_registry():
    """A hand-maintained list of paths is the thing that rots. This must come
    off PERSISTENT_CONFIG_REGISTRY so a newly added *_API_KEY is covered the
    day it lands."""
    paths = sensitive_config_paths()
    assert paths, "expected the registry to yield at least one sensitive path"
    # The self.corpus credentials are the ones that guard the backup store.
    assert "self_corpus.lakefs_secret_access_key" in paths
    assert "rag.web.search.brave_search_api_key" in paths


####################
# Redaction
####################


def test_secret_values_are_replaced():
    config = {"self_corpus": {"lakefs_secret_access_key": "AKIAEXAMPLEKEY000000"}}
    out = redact_config(config)
    assert out["self_corpus"]["lakefs_secret_access_key"] == REDACTED


def test_redaction_does_not_mutate_the_input():
    """The caller holds the live blob; redacting must not scrub it in place."""
    config = {"self_corpus": {"lakefs_secret_access_key": "live-secret"}}
    redact_config(config)
    assert config["self_corpus"]["lakefs_secret_access_key"] == "live-secret"


def test_empty_values_are_left_visible():
    """An empty string is not a secret, and showing it is how an admin tells
    'not configured' from 'configured and hidden'."""
    config = {"rag": {"web": {"search": {"brave_search_api_key": ""}}}}
    out = redact_config(config)
    assert out["rag"]["web"]["search"]["brave_search_api_key"] == ""


def test_lists_of_keys_redact_element_wise_preserving_count():
    config = {"openai": {"api_keys": ["sk-one", "sk-two", "sk-three"]}}
    out = redact_config(config)
    assert out["openai"]["api_keys"] == [REDACTED, REDACTED, REDACTED]


def test_non_secret_values_survive_untouched():
    config = {"ui": {"default_models": "gpt-4"}, "enable_self_corpus": True}
    out = redact_config(config)
    assert out == config


def test_unknown_secret_shaped_keys_are_caught_by_name():
    """Values that reached the table via /configs/import or an older build are
    not in today's registry, so name matching is the second pass."""
    config = {"some_mod": {"vendor_api_key": "live", "vendor_endpoint": "https://x"}}
    out = redact_config(config)
    assert out["some_mod"]["vendor_api_key"] == REDACTED
    assert out["some_mod"]["vendor_endpoint"] == "https://x"


def test_secrets_nested_inside_lists_of_dicts_are_caught():
    config = {"connections": [{"name": "a", "api_key": "live-1"}, {"name": "b", "api_key": "live-2"}]}
    out = redact_config(config)
    assert [c["api_key"] for c in out["connections"]] == [REDACTED, REDACTED]
    assert [c["name"] for c in out["connections"]] == ["a", "b"]


def test_booleans_under_sensitive_names_are_not_mangled():
    config = {"auth": {"api_key": {"enable": True}}}
    out = redact_config(config)
    assert out["auth"]["api_key"]["enable"] is True


def test_basic_auth_credentials_are_redacted():
    """AUTOMATIC1111_API_AUTH holds "user:password" and its name says nothing
    about being a secret — found by sweeping the registry, not by reading the
    pattern."""
    config = {"image_generation": {"automatic1111": {"api_auth": "admin:hunter2"}}}
    out = redact_config(config)
    assert out["image_generation"]["automatic1111"]["api_auth"] == REDACTED


def test_secrets_nested_in_connection_config_blobs_are_caught():
    """AUDIO_CONNECTION_CONFIGS is not itself a secret — it carries URLs and
    engine names worth seeing — but credentials live inside it."""
    config = {
        "audio": {
            "connection_configs": [
                {"name": "tts", "url": "http://self-speak:8880", "api_key": "live-key"},
                {"name": "stt", "url": "http://self-transcribe:8890", "key": "other-key"},
            ]
        }
    }
    out = redact_config(config)
    assert out["audio"]["connection_configs"][0]["api_key"] == REDACTED
    assert out["audio"]["connection_configs"][1]["key"] == REDACTED
    assert out["audio"]["connection_configs"][0]["url"] == "http://self-speak:8880"
    assert out["audio"]["connection_configs"][1]["name"] == "stt"


####################
# Write gating
####################


def test_save_is_a_noop_when_persistent_config_is_disabled(monkeypatch):
    """With the flag off, self.value IS the env value and the database copy is
    ignored on read — so writing it is pure leak for zero benefit."""
    import selfai_ui.config as config_module

    called = []
    monkeypatch.setattr(config_module, "ENABLE_PERSISTENT_CONFIG", False)
    monkeypatch.setattr(config_module, "save_to_db", lambda data: called.append(data))

    entry = config_module.PersistentConfig("TEST_SECRET_KEY", "test.secret_key", "env-sourced-secret")
    entry.value = "env-sourced-secret"
    entry.save()

    assert called == [], "save() wrote to the config table with persistent config disabled"


def test_update_does_not_adopt_db_value_when_persistent_config_is_disabled(monkeypatch):
    """save_config() calls update() on every registered entry, and that path is
    reachable from POST /configs/import. Ungated, it overrides env/vault at
    runtime until a restart silently reverts it."""
    import selfai_ui.config as config_module

    monkeypatch.setattr(config_module, "ENABLE_PERSISTENT_CONFIG", False)
    monkeypatch.setattr(config_module, "get_config_value", lambda path: "value-from-uploaded-json")

    entry = config_module.PersistentConfig("TEST_SETTING", "test.setting", "value-from-env")
    entry.value = "value-from-env"
    entry.update()

    assert entry.value == "value-from-env", "update() adopted the database value despite env being authoritative"


def test_save_still_writes_when_persistent_config_is_enabled(monkeypatch):
    """The gate must not break deployments that persist config by design."""
    import selfai_ui.config as config_module

    called = []
    monkeypatch.setattr(config_module, "ENABLE_PERSISTENT_CONFIG", True)
    monkeypatch.setattr(config_module, "save_to_db", lambda data: called.append(data))
    monkeypatch.setattr(config_module, "CONFIG_DATA", {})

    entry = config_module.PersistentConfig("TEST_PLAIN_SETTING", "test.plain_setting", "a")
    entry.value = "b"
    entry.save()

    assert len(called) == 1
    assert entry.config_value == "b"
