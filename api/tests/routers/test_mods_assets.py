"""Per-mod static asset serving: traversal safety, cross-mod isolation, headers.

The pure resolver (`mods.assets.resolve_mod_asset`) is tested directly against a
real on-disk tree -- including a real symlink escape it must refuse. The HTTP
surface (`routers.mod_assets`) is tested through a mounted router reading a
synthetic `app.state.MODS`, mirroring `test_mods_registry.py::_client_with`, so
the tests need no real boot.

Every fact is read through the surface under test (the resolver's return/raise,
the endpoint's status/body/headers), never by re-implementing the guard.

Cavekit: cavekit-mods-frontend-api.md R3 -- T-A03 (AC 1,2,3,4,6; AC5 is T-A04)
"""

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from selfai_ui.mods.assets import AssetResolutionError, resolve_mod_asset
from selfai_ui.routers import mod_assets


# --- fixtures: two mods on disk, plus an out-of-tree secret ------------------
@pytest.fixture
def two_mods(tmp_path):
    """Two mod directories side by side, plus a core-ish secret file outside both.

    Returns (mod_a_dir, mod_b_dir, secret_path). `mod_a` ships a real asset;
    `mod_b` ships a distinct one; the secret sits outside every mod directory --
    the thing a traversal must never reach.
    """
    mod_a = tmp_path / "mods" / "alpha"
    mod_b = tmp_path / "mods" / "beta"
    (mod_a / "assets").mkdir(parents=True)
    (mod_b / "assets").mkdir(parents=True)

    (mod_a / "assets" / "app.a1b2c3.js").write_bytes(b"export const A = 1;\n")
    (mod_b / "assets" / "app.d4e5f6.js").write_bytes(b"export const B = 2;\n")

    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET -- outside every mod directory\n")

    return mod_a, mod_b, secret


def _client(loaded):
    """A TestClient over just the asset router, with a synthetic MODS state.

    `loaded` maps mod id -> directory (a Path or None). Mirrors the
    LoadResult-shaped stub `test_mods_registry.py` uses: the router reads
    `app.state.MODS.loaded[<id>].directory`.
    """
    app = FastAPI()
    app.include_router(mod_assets.router)
    app.state.MODS = types.SimpleNamespace(
        loaded={mid: types.SimpleNamespace(manifest=None, directory=d) for mid, d in loaded.items()}
    )
    return TestClient(app)


# =============================================================================
# R3 AC1 -- a real in-directory asset returns its bytes
# =============================================================================
@pytest.mark.tier1
def test_an_existing_asset_returns_its_bytes(two_mods):
    mod_a, _, _ = two_mods
    client = _client({"alpha": mod_a})

    resp = client.get("/static/mods/alpha/assets/app.a1b2c3.js")

    assert resp.status_code == 200
    assert resp.content == b"export const A = 1;\n"


def test_resolver_returns_the_real_file_for_an_in_directory_request(two_mods):
    """The pure guard, directly: a legitimate path resolves to the real file."""
    mod_a, _, _ = two_mods
    resolved = resolve_mod_asset(mod_a, "assets/app.a1b2c3.js")
    assert resolved == (mod_a / "assets" / "app.a1b2c3.js").resolve()
    assert resolved.read_bytes() == b"export const A = 1;\n"


# =============================================================================
# R3 AC2 -- escape via `..`, absolute path, or symlink is refused, no bytes
# =============================================================================
def _encoded(path: str) -> str:
    """Percent-encode `.` and `/` so an httpx client delivers the raw traversal
    to the server instead of normalising `../` away client-side. This is the
    realistic attack shape -- a browser never sends a literal `../`, but an
    attacker crafts an encoded request -- and it is the only way to exercise the
    SERVER-side guard through the HTTP surface rather than httpx's normaliser."""
    return path.replace(".", "%2e").replace("/", "%2f")


@pytest.mark.tier1
@pytest.mark.parametrize(
    "asset_path",
    [
        "../../secret.txt",  # .. traversal up out of the mod dir
        "assets/../../../secret.txt",  # .. traversal through a real subdir
        "/etc/hostname",  # absolute path injection
        "assets/../../secret.txt",
    ],
)
def test_a_traversal_escape_is_refused_without_returning_outside_bytes(two_mods, asset_path):
    mod_a, _, secret = two_mods
    client = _client({"alpha": mod_a})

    # Encoded so the raw traversal reaches the server's guard, not httpx's normaliser.
    resp = client.get(f"/static/mods/alpha/{_encoded(asset_path)}")

    assert resp.status_code == 404
    assert secret.read_bytes() not in resp.content


@pytest.mark.parametrize("asset_path", ["../../secret.txt", "assets/../../../secret.txt", "/etc/hostname"])
def test_resolver_raises_on_dotdot_and_absolute_escape(two_mods, asset_path):
    """The guard rejects `..` and absolute escapes -- via resolve()+is_relative_to,
    not a string-prefix check."""
    mod_a, _, _ = two_mods
    with pytest.raises(AssetResolutionError):
        resolve_mod_asset(mod_a, asset_path)


def test_a_symlink_pointing_outside_the_mod_directory_is_refused(two_mods):
    """A real symlink inside the mod dir aimed at the out-of-tree secret must not
    be followed out. `.resolve()` follows the link; `is_relative_to` refuses it."""
    mod_a, _, secret = two_mods
    (mod_a / "escape.txt").symlink_to(secret)

    # Pure guard.
    with pytest.raises(AssetResolutionError):
        resolve_mod_asset(mod_a, "escape.txt")

    # And through the HTTP surface.
    client = _client({"alpha": mod_a})
    resp = client.get("/static/mods/alpha/escape.txt")
    assert resp.status_code == 404
    assert b"TOP SECRET" not in resp.content


def test_a_sibling_directory_that_string_prefixes_the_mod_dir_cannot_be_reached(tmp_path):
    """`/mods/foo` string-prefixes `/mods/foobar`: a string-prefix guard would let
    `foobar` masquerade as inside `foo`. is_relative_to compares components, so a
    path resolving into the sibling is refused."""
    foo = tmp_path / "mods" / "foo"
    foobar = tmp_path / "mods" / "foobar"
    foo.mkdir(parents=True)
    foobar.mkdir(parents=True)
    (foobar / "leak.js").write_text("sibling asset\n")

    with pytest.raises(AssetResolutionError):
        resolve_mod_asset(foo, "../foobar/leak.js")


# =============================================================================
# R3 AC3 -- one mod's prefix cannot read another mod's directory
# =============================================================================
@pytest.mark.tier1
def test_mod_a_prefix_cannot_read_mod_b_directory(two_mods):
    mod_a, mod_b, _ = two_mods
    client = _client({"alpha": mod_a, "beta": mod_b})

    # Mod A's own asset is fine.
    assert client.get("/static/mods/alpha/assets/app.a1b2c3.js").status_code == 200

    # Mod A's prefix reaching sideways into mod B's directory is refused, and B's
    # bytes never appear in the response. Encoded so the raw `../beta/...` reaches
    # the server's guard rather than being normalised into a plain mod-B request.
    resp = client.get(f"/static/mods/alpha/{_encoded('../beta/assets/app.d4e5f6.js')}")
    assert resp.status_code == 404
    assert b"export const B" not in resp.content


def test_resolver_refuses_a_cross_mod_path(two_mods):
    """Directly: mod A's directory cannot resolve a path into mod B's."""
    mod_a, mod_b, _ = two_mods
    with pytest.raises(AssetResolutionError):
        resolve_mod_asset(mod_a, "../beta/assets/app.d4e5f6.js")


# =============================================================================
# R3 AC4 -- content-hashed assets carry long-lived immutable cache headers
# =============================================================================
@pytest.mark.tier1
def test_a_served_asset_carries_immutable_cache_headers(two_mods):
    mod_a, _, _ = two_mods
    client = _client({"alpha": mod_a})

    resp = client.get("/static/mods/alpha/assets/app.a1b2c3.js")

    assert resp.status_code == 200
    cache_control = resp.headers["cache-control"]
    assert "immutable" in cache_control
    assert "max-age=31536000" in cache_control
    assert "public" in cache_control


# =============================================================================
# R3 AC6 -- serves ONLY from the R2-retained directory; no other source
# =============================================================================
def test_the_resolver_serves_only_from_its_directory_argument(two_mods):
    """The resolver's sole source of where a mod's files live is the `directory`
    argument. Given mod B's directory, the very same asset_path that resolved mod
    A's file now resolves B's -- there is no id map, no config, no second source."""
    mod_a, mod_b, _ = two_mods
    (mod_a / "assets" / "shared.js").write_bytes(b"A-copy\n")
    (mod_b / "assets" / "shared.js").write_bytes(b"B-copy\n")

    assert resolve_mod_asset(mod_a, "assets/shared.js").read_bytes() == b"A-copy\n"
    assert resolve_mod_asset(mod_b, "assets/shared.js").read_bytes() == b"B-copy\n"


@pytest.mark.tier1
def test_the_endpoint_reads_the_directory_off_the_loaded_record(two_mods):
    """Repointing the loaded record's directory repoints what the same URL serves:
    the surface has no directory source other than the loaded record (R3 AC6). A
    loaded mod carrying no directory serves nothing."""
    mod_a, mod_b, _ = two_mods
    (mod_a / "assets" / "shared.js").write_bytes(b"A-copy\n")
    (mod_b / "assets" / "shared.js").write_bytes(b"B-copy\n")

    # Same request path, directory taken from mod A's record.
    client_a = _client({"m": mod_a})
    assert client_a.get("/static/mods/m/assets/shared.js").content == b"A-copy\n"

    # Same request path, record now points at mod B's directory.
    client_b = _client({"m": mod_b})
    assert client_b.get("/static/mods/m/assets/shared.js").content == b"B-copy\n"

    # A loaded record with no retained directory serves nothing.
    client_none = _client({"m": None})
    assert client_none.get("/static/mods/m/assets/shared.js").status_code == 404


@pytest.mark.tier1
def test_an_unknown_mod_id_is_a_plain_not_found(two_mods):
    """A mod id absent from the loaded set is a not-found -- the surface exposes
    no directory for a mod it did not load. (The disabled-mod hardening is T-A04;
    this pins the not-loaded case the endpoint already refuses.)"""
    mod_a, _, _ = two_mods
    client = _client({"alpha": mod_a})
    assert client.get("/static/mods/ghost/assets/app.js").status_code == 404


@pytest.mark.tier1
def test_a_missing_file_inside_the_directory_is_a_not_found(two_mods):
    mod_a, _, _ = two_mods
    client = _client({"alpha": mod_a})
    assert client.get("/static/mods/alpha/assets/does-not-exist.js").status_code == 404
