"""Path-traversal-safe resolution of a per-mod asset path.

The API's first traversal-safe subtree resolver. No such thing existed anywhere
in the codebase before this (research brief §4); the three core static mounts
(`main.py:1718` `/static`, `:1719` `/cache`, `:1736` `SPAStaticFiles` at `/`) do
not qualify -- `SPAStaticFiles` does index.html fallback, not subtree containment.

The single load-bearing guard is `resolve()` + `is_relative_to()`:

    resolved = (base / asset_path).resolve()
    if not resolved.is_relative_to(base): refuse

`Path.resolve()` normalises `..` segments AND follows symlinks, so one check
refuses all three escape shapes at once:

* **`..` traversal** -- `base/../../etc/passwd` normalises to `/etc/passwd`,
  which is not relative to `base`.
* **absolute-path injection** -- `base / "/etc/passwd"` is `/etc/passwd`
  (pathlib discards the left operand when the right is absolute), not relative
  to `base`.
* **symlink escape** -- a symlink inside `base` pointing at `/etc` resolves
  through to `/etc/...`, not relative to `base`.

A bare string-prefix check (`str(resolved).startswith(str(base))`) does NOT
satisfy this -- `/mods/foobar` string-prefixes `/mods/foo`, so a sibling
directory whose name extends the mod's would falsely pass. `is_relative_to`
compares path *components*, not string prefixes, and is immune to that.

`base` is itself resolved once, so a mod directory reached through a symlink is
canonicalised before any comparison. The resolver's ONLY source of truth for
where a mod's files live is the `directory` argument -- there is no other path
input, no config lookup, no id-to-directory map (frontend-api R3 AC6). The
caller passes the directory the loaded-mod record retained (R2); this function
cannot be pointed at anything else.

Cavekit: cavekit-mods-frontend-api.md R3 -- T-A03; R4 bundle discovery -- T-A05
"""

from pathlib import Path

#: The filename convention a mod's build MUST emit for its current frontend
#: bundle entry: `entry.<contenthash>.js`, at the ROOT of the mod's served
#: directory (frontend-api R4 -- T-A05). The `<contenthash>` segment changes
#: whenever the bundle content changes, so an update yields a NEW filename (a new
#: URL) rather than overwriting bytes in place -- the property the always-fresh
#: manifest endpoint relies on to point at a content-hashed, immutable asset.
#: A clean build ships exactly one such file; discovery below tolerates stale
#: leftovers by resolving to the most recently written one.
#:
#: T-C08 (which compiles the reference mod's Svelte 5 custom element) must emit
#: its entry as `entry.<hash>.js` at the root of `api/mods/reference/`.
BUNDLE_ENTRY_GLOB = "entry.*.js"

#: File types this surface will serve. Anything else is refused, whatever a mod
#: happens to have in its install directory.
#:
#: The traversal guard below answers "is this file inside the mod's directory".
#: It does NOT answer "should this file be public", and for a while nothing did:
#: the surface served every regular file under the mod's directory, so a mod's
#: Python source, its `mod.yaml`, its tests and its build inputs were all
#: anonymously readable (#70). The kit's wording is "a mod's **built frontend
#: assets**" -- this is the missing half of that sentence.
#:
#: An allowlist rather than a blocklist of `.py`/`.yaml`/`.env`: operators are
#: told to install a mod by dropping a directory onto a volume, and a blocklist
#: only protects against the file types somebody thought of. The failure mode of
#: an allowlist is a mod author asking for an extension to be added; the failure
#: mode of a blocklist is publishing a credential.
#:
#: Restricting to a subdirectory (serve only `dist/`) was considered and
#: rejected: bundlers legitimately emit chunks beside the entry, and both real
#: mods keep their entry and icon at the directory root, so it would break them
#: now and future mods later without adding safety this does not already give.
SERVABLE_ASSET_SUFFIXES = frozenset(
    {
        # scripts and styles, plus the source maps that accompany them
        ".js",
        ".mjs",
        ".css",
        ".map",
        # markup a mod's surface may load
        ".html",
        # data a bundle fetches at runtime
        ".json",
        # images, including the nav icon a manifest may point at
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".ico",
        # fonts
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
    }
)


def is_servable_asset(path) -> bool:
    """True when this filename's type may be served from a mod's directory.

    Case-insensitive: a file is no less source code for being named `.PY`.
    """
    return Path(path).suffix.lower() in SERVABLE_ASSET_SUFFIXES


class AssetResolutionError(Exception):
    """The requested asset path could not be served from the mod's directory.

    Raised both when the path escapes the mod's directory (a refusal) and when
    it resolves inside the directory but names no real file (a miss). The caller
    maps both to the same not-found response so the surface discloses nothing
    about which case occurred or about what lies outside the directory.
    """


def resolve_mod_asset(directory: Path, asset_path: str) -> Path:
    """Resolve `asset_path` to a real file inside `directory`, or raise.

    `directory` is the absolute on-disk directory the loaded mod was loaded from
    (frontend-api R2). `asset_path` is the caller-supplied remainder of the URL.
    Returns the resolved, canonical path of an existing regular file that is
    provably within `directory`. Raises `AssetResolutionError` for anything that
    escapes the directory (`..`, absolute path, or symlink pointing outside) or
    that names no regular file inside it.
    """
    base = Path(directory).resolve()

    # The one guard. `.resolve()` normalises `..` and follows symlinks; the
    # `is_relative_to` comparison is component-wise, not a string prefix.
    resolved = (base / asset_path).resolve()
    if not resolved.is_relative_to(base):
        raise AssetResolutionError(f"resolved asset path {resolved!s} escapes the mod directory {base!s}")

    # Inside the directory is necessary but not sufficient: only web-asset file
    # types are public (#70). Checked on the RESOLVED path, not the requested
    # one, so a symlink named `logo.svg` pointing at `secrets.yaml` is refused on
    # what it actually is rather than on what it is called.
    if not is_servable_asset(resolved):
        raise AssetResolutionError(
            f"asset {resolved.name!r} is not a servable file type; a mod's directory is not a web root"
        )

    # Inside the directory but not a real file: a miss, not an escape. Reported
    # as the same not-found so a probe learns nothing about directory contents.
    if not resolved.is_file():
        raise AssetResolutionError(f"no asset file at {resolved!s}")

    return resolved


def discover_bundle_entry(directory: Path) -> str | None:
    """The filename of the mod's CURRENT frontend bundle entry, or `None`.

    Scans `directory` (the R2-retained mod directory) for a file matching
    `BUNDLE_ENTRY_GLOB` (`entry.<contenthash>.js`) at its root -- the mod's own
    build convention. The directory is the ONLY source of truth (frontend-api
    R4): the current entry is discovered by reading the directory fresh on every
    call, never cached, so a build that swaps the hashed filename is observed on
    the next request. Returns just the filename (the caller composes the R3 URL).

    Returns `None` when the directory holds no built bundle -- the distinguishable
    "declares a frontend but ships no built assets yet" condition (R4 AC6), which
    the caller reports as a NAMED state, never as a URL to a missing file.

    A clean build ships exactly one `entry.*.js`; if more than one is present
    (a stale artifact left beside a fresh one) the most recently modified wins,
    so discovery tracks the current build rather than an abandoned one.
    """
    base = Path(directory).resolve()
    if not base.is_dir():
        return None
    matches = [p for p in base.glob(BUNDLE_ENTRY_GLOB) if p.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime).name
