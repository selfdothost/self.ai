"""Per-mod operator configuration, on core's existing config mechanism.

A mod declares the keys it needs in its manifest; core surfaces and persists
them. Deliberately no new storage, no new admin surface, no parallel mechanism:
values become `PersistentConfig` entries on `AppConfig`, which is what every
first-party setting already is, so they persist, export, and import exactly the
way core settings do.

Two rules that are not negotiable:

**Namespacing.** Every key is stored under `mods.<id>.config.<key>`. Two mods
declaring `api_key` must neither read nor overwrite each other, and the only
way to guarantee that structurally is to root each mod's keys at its own id --
the same reasoning that roots its scopes and its tables.

**Secrets never come from the manifest.** A key marked secret takes its value
from the deployment's existing secret path (the environment), never from a
value written in `mod.yaml`. A manifest is a file in an image; it is read by
anyone who can read the image, and it lands in version control.

Cavekit: cavekit-mods-loader.md R8 -- T-038
"""

import logging
import os

from selfai_ui.config import PersistentConfig
from selfai_ui.mods.manifest import ModManifest

log = logging.getLogger(__name__)


def config_path_for(mod_id: str, key: str) -> str:
    """Where a mod's configuration value is persisted.

    Rooted at the mod's own id, so collision between two mods is structurally
    impossible rather than merely unlikely.
    """
    return f"mods.{mod_id}.config.{key}"


def env_var_for(mod_id: str, key: str) -> str:
    """The environment variable an operator sets for this key.

    Upper-cased and namespaced the same way, so a secret for one mod cannot be
    picked up by another that happens to want the same key name.
    """
    return f"MOD_{mod_id}_{key}".upper().replace("-", "_")


def build_mod_config(manifest: ModManifest) -> dict[str, PersistentConfig]:
    """Build the `PersistentConfig` entries for one mod's declared keys.

    Returns a mapping of key name to config object. A mod declaring no config
    yields an empty mapping rather than a special case.
    """
    built: dict[str, PersistentConfig] = {}

    for entry in manifest.config:
        env_name = env_var_for(manifest.id, entry.key)
        from_env = os.environ.get(env_name)

        if entry.secret:
            # Never the manifest. `validate_semantics` already refuses a secret
            # carrying an inline value, so the only source left is the
            # deployment's own secret path.
            default = from_env
        else:
            default = from_env if from_env is not None else entry.default

        built[entry.key] = PersistentConfig(
            env_name,
            config_path_for(manifest.id, entry.key),
            default,
        )

    return built


def attach_mod_config(app_config, manifest: ModManifest) -> dict[str, PersistentConfig]:
    """Attach a mod's config to `AppConfig` under namespaced attribute names.

    `AppConfig.__setattr__` accepts only `PersistentConfig` instances on first
    assignment, which is precisely why these are built as `PersistentConfig`
    rather than plain values -- there is no other supported way onto that
    object, and inventing one would be the parallel mechanism this requirement
    exists to prevent.
    """
    built = build_mod_config(manifest)

    for key, config in built.items():
        setattr(app_config, env_var_for(manifest.id, key), config)

    if built:
        log.info("mods: %r registered %d configuration key(s)", manifest.id, len(built))

    return built


def redacted(manifest: ModManifest, values: dict) -> dict:
    """A mod's config with secret values masked, for display.

    The admin surface has to show that a secret key *exists* and whether it is
    set, without showing what it is.
    """
    secrets = {entry.key for entry in manifest.config if entry.secret}
    return {key: ("********" if key in secrets and value else value) for key, value in values.items()}
