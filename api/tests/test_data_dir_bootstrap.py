"""DATA_DIR is created and proven writable at import (self.ai#120).

These run `import selfai_ui.env` in a subprocess, following
tests/security/test_config_security.py: env.py does its work at import time, so
a fixture that sets os.environ after the module is already loaded proves
nothing.

Why this is worth testing at all: the default /app/backend/data ships inside the
image, so `docker run` with no DATA_DIR works and the bug stays invisible. It
only surfaces when someone follows the advice we give for the combined image --
mount a volume and point DATA_DIR at it -- with a path that is not pre-created.
That is the on-ramp we recommend publicly.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

IMPORT_ENV = "import selfai_ui.env"


def run_import(tmp_path, data_dir, extra=None):
    env = os.environ.copy()
    env["DATA_DIR"] = str(data_dir)
    env["WEBUI_SECRET_KEY"] = "test-secret-key-not-for-production"
    # Keep the import off the real database; this suite is about the directory.
    env["DATABASE_URL"] = f"sqlite:///{tmp_path}/probe.db"
    env.update(extra or {})
    return subprocess.run(
        [sys.executable, "-c", IMPORT_ENV],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.tier0
def test_missing_data_dir_is_created(tmp_path):
    """The reported bug: a DATA_DIR that does not exist yet must just work."""
    target = tmp_path / "does-not-exist-yet"
    assert not target.exists()

    result = run_import(tmp_path, target)

    assert result.returncode == 0, f"import failed:\n{result.stderr}"
    assert target.is_dir(), "DATA_DIR should have been created"


@pytest.mark.tier0
def test_nested_missing_data_dir_is_created(tmp_path):
    """`mkdir -p` semantics: a volume mounted several levels deep is normal."""
    target = tmp_path / "a" / "b" / "c"

    result = run_import(tmp_path, target)

    assert result.returncode == 0, f"import failed:\n{result.stderr}"
    assert target.is_dir()


@pytest.mark.tier0
def test_existing_data_dir_is_left_alone(tmp_path):
    """Creation must be idempotent and must not disturb existing contents."""
    target = tmp_path / "existing"
    target.mkdir()
    keeper = target / "webui.db"
    keeper.write_text("pretend database")

    result = run_import(tmp_path, target)

    assert result.returncode == 0, f"import failed:\n{result.stderr}"
    assert keeper.read_text() == "pretend database"


@pytest.mark.tier0
def test_no_write_probe_is_left_behind(tmp_path):
    """The writability check must clean up after itself."""
    target = tmp_path / "probe-cleanup"

    result = run_import(tmp_path, target)

    assert result.returncode == 0, f"import failed:\n{result.stderr}"
    assert list(target.iterdir()) == [], f"probe left files behind: {list(target.iterdir())}"


@pytest.mark.tier0
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_data_dir_fails_naming_data_dir(tmp_path):
    """An existing-but-unwritable DATA_DIR must say so, not fail as sqlite.

    This is the non-root / read-only-rootfs case. The whole point of the fix is
    that the operator reads the words DATA_DIR rather than
    "unable to open database file" several frames away.
    """
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o500)
    try:
        result = run_import(tmp_path, target)

        assert result.returncode != 0, "import should refuse an unwritable DATA_DIR"
        output = result.stderr + result.stdout
        assert "DATA_DIR" in output, f"error must name DATA_DIR, got:\n{output}"
        assert "not writable" in output
        assert str(target) in output, "error must name the offending path"
    finally:
        target.chmod(0o700)


@pytest.mark.tier0
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_uncreatable_data_dir_fails_naming_data_dir(tmp_path):
    """A DATA_DIR under an unwritable parent cannot be created -- say which."""
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    target = parent / "child"
    try:
        result = run_import(tmp_path, target)

        assert result.returncode != 0, "import should refuse an uncreatable DATA_DIR"
        output = result.stderr + result.stdout
        assert "DATA_DIR" in output, f"error must name DATA_DIR, got:\n{output}"
        assert "could not be created" in output
    finally:
        parent.chmod(0o700)


@pytest.mark.tier0
def test_sqlite_database_actually_opens_under_a_fresh_data_dir(tmp_path):
    """End-to-end on the real failure: DATABASE_URL defaulting to DATA_DIR.

    The other tests would still pass if DATA_DIR were created but the sqlite
    default were computed from something else. This one reproduces the exact
    reported stack -- default DATABASE_URL, DATA_DIR that does not exist -- and
    asserts the connection now opens.
    """
    target = tmp_path / "fresh"
    env = os.environ.copy()
    env["DATA_DIR"] = str(target)
    env["WEBUI_SECRET_KEY"] = "test-secret-key-not-for-production"
    env.pop("DATABASE_URL", None)  # let it default to sqlite under DATA_DIR

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sqlite3;from selfai_ui.env import DATABASE_URL;"
            "assert DATABASE_URL.startswith('sqlite:///'), DATABASE_URL;"
            "p=DATABASE_URL.removeprefix('sqlite:///');"
            "sqlite3.connect(p).execute('create table t (x int)');"
            "print('OPENED', p)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, f"sqlite still cannot open under a fresh DATA_DIR:\n{result.stderr}"
    assert "OPENED" in result.stdout
    assert Path(target / "webui.db").exists()
