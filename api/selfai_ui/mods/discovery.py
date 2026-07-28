"""Which mods exist on disk, and which of them are switched on.

Discovery is deliberately boring and deliberately unable to raise. One broken
mod must never stop the instance booting, so every failure here is recorded and
returned rather than thrown -- the caller gets the set that loaded plus the
reasons the others did not, and decides what to do with both.

Two states are distinct and must stay distinct:

  * **not enabled** -- present on disk, deliberately switched off. Contributes
    nothing and reports nothing. Not an error; somebody meant it.
  * **enabled but absent** -- an operator switched on a mod that is not
    installed. Always an error, because it is always a mistake, but never fatal.

Cavekit: cavekit-mods-discovery.md R1, R3 -- T-020, T-022
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from selfai_ui.mods.manifest import ManifestError, ModManifest
from selfai_ui.mods.validate import load_manifest

log = logging.getLogger(__name__)

MANIFEST_FILENAME = "mod.yaml"


@dataclass
class DiscoveryResult:
    """What discovery found. Never an exception -- always a report."""

    #: Mods that are enabled, installed, and passed full validation.
    loaded: dict[str, ModManifest] = field(default_factory=dict)
    #: Human-readable reasons, one per mod that could not be loaded. These are
    #: for an operator reading a boot log, so each names the mod and the cause.
    errors: list[str] = field(default_factory=list)
    #: Non-fatal oddities -- a configured install location that is not there.
    warnings: list[str] = field(default_factory=list)

    @property
    def loaded_ids(self) -> list[str]:
        return sorted(self.loaded)


def _candidate_dirs(install_dirs) -> list[Path]:
    return [Path(d) for d in install_dirs]


def scan(install_dirs) -> tuple[dict[str, list[Path]], list[str]]:
    """Find every `mod.yaml` on disk, grouped by directory name.

    Grouping is by the *directory* name rather than the manifest's declared
    `id`, because reading the id requires parsing, and a manifest too broken to
    parse still occupies a slot that must not silently collide with another.
    The declared id is checked against the directory later.
    """
    found: dict[str, list[Path]] = {}
    warnings: list[str] = []

    for directory in _candidate_dirs(install_dirs):
        if not directory.is_dir():
            warnings.append(f"mods: install location {directory} does not exist or is not a directory")
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = entry / MANIFEST_FILENAME
            if manifest_path.is_file():
                found.setdefault(entry.name, []).append(manifest_path)

    return found, warnings


def discover(install_dirs, enabled_ids, *, core_version: str) -> DiscoveryResult:
    """Resolve installed mods against the enabled list.

    Never raises. A mod that fails for any reason is absent from `loaded` and
    accounted for in `errors`.
    """
    result = DiscoveryResult()
    found, warnings = scan(install_dirs)
    result.warnings.extend(warnings)

    enabled = list(dict.fromkeys(enabled_ids))  # de-duplicate, keep order

    for mod_id in enabled:
        paths = found.get(mod_id)

        if not paths:
            # Always an error: somebody switched on something that is not here.
            result.errors.append(
                f"mods: {mod_id!r} is enabled but no {MANIFEST_FILENAME} was found in any install location"
            )
            continue

        if len(paths) > 1:
            # A hard error, not last-wins. Two mods claiming one id is
            # ambiguous, and resolving it silently would mean the instance
            # runs code the operator did not choose. Neither is loaded.
            listed = ", ".join(str(p) for p in paths)
            result.errors.append(
                f"mods: {mod_id!r} is installed in more than one location "
                f"and will not be loaded: {listed}"
            )
            continue

        path = paths[0]
        try:
            manifest = load_manifest(path, core_version=core_version)
        except ManifestError as exc:
            result.errors.append(f"mods: {mod_id!r} failed to load: {exc}")
            continue

        if manifest.id != mod_id:
            result.errors.append(
                f"mods: {path} declares id {manifest.id!r} but is installed as {mod_id!r}; "
                f"the directory name and the declared id must match"
            )
            continue

        # Retain the on-disk directory this mod loaded from, so the per-mod
        # asset and manifest surfaces (frontend-api R3, R4) can serve off it.
        # `path` is the single install location `scan()` already resolved (its
        # multi-location refusal above is unchanged); its parent is the mod
        # directory. `.resolve()` canonicalises to an absolute path -- it walks
        # no directory (no `iterdir`), so retention adds no filesystem scan.
        manifest._directory = path.parent.resolve()
        result.loaded[manifest.id] = manifest

    for message in result.warnings:
        log.warning(message)
    for message in result.errors:
        log.error(message)

    return result
