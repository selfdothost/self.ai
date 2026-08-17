"""
Password hashing: bcrypt directly, replacing passlib.

Covers: backward compatibility with hashes written by the passlib era,
round-tripping, the 72-byte bcrypt boundary, and malformed stored hashes.

These matter more than usual because getting them wrong locks every existing
user out of production. passlib stored standard modular-crypt bcrypt strings,
so `bcrypt.checkpw` reads them unchanged -- the fixtures below are real
`$2b$12$` hashes (passlib's own default: bcrypt ident 2b, 12 rounds) and are
deliberately hard-coded rather than generated, so they keep asserting
compatibility with the OLD format even as the implementation changes.
"""

import pytest

from selfai_ui.utils.auth import (
    BCRYPT_MAX_BYTES,
    get_password_hash,
    verify_password,
)

# Generated under the passlib-era settings (bcrypt 2b, cost 12).
PASSLIB_ERA_HASHES = {
    "hunter2": "$2b$12$f8lBJL.gifT0n1VrXqyAk.ENMzxcsOvCttpepga9mC1JGmNWr33yC",
    "pässwörd-ünicode": "$2b$12$nEnt07kErnVqkrxlmj/bnuDu4.po5Cb5SXG83Fai9Q6kPjdGT7N7K",
}


@pytest.mark.tier0
@pytest.mark.parametrize("password,stored", PASSLIB_ERA_HASHES.items())
def test_passlib_era_hashes_still_verify(password, stored):
    """The whole point of the migration: no existing user is locked out."""
    assert verify_password(password, stored) is True


@pytest.mark.tier0
@pytest.mark.parametrize("password,stored", PASSLIB_ERA_HASHES.items())
def test_passlib_era_hashes_reject_wrong_password(password, stored):
    assert verify_password(password + "-wrong", stored) is False


@pytest.mark.tier0
def test_hash_roundtrip():
    hashed = get_password_hash("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("Correct Horse Battery Staple", hashed) is False


@pytest.mark.tier0
def test_hash_is_modular_crypt_bcrypt():
    """Format must stay readable by anything else that reads these rows."""
    hashed = get_password_hash("whatever")
    assert hashed.startswith("$2b$")
    assert len(hashed) == 60
    assert isinstance(hashed, str)


@pytest.mark.tier0
def test_hash_is_salted():
    """Same password, two hashes -> different digests."""
    assert get_password_hash("same") != get_password_hash("same")


@pytest.mark.tier0
def test_password_longer_than_72_bytes_does_not_raise():
    """bcrypt >= 5 raises on >72 bytes; passlib truncated. We truncate.

    This must not become an exception, because it would turn a long password
    into a 500 on the login and registration paths.
    """
    long_password = "a" * 100
    hashed = get_password_hash(long_password)
    assert verify_password(long_password, hashed) is True


@pytest.mark.tier0
def test_truncation_boundary_matches_bcrypt_semantics():
    """Anything past 72 bytes is ignored -- same as the pre-migration behaviour.

    Asserted explicitly rather than left implicit: it is a real (inherited)
    property of bcrypt that two distinct long passwords sharing a 72-byte
    prefix are the same credential.
    """
    hashed = get_password_hash("a" * BCRYPT_MAX_BYTES)
    assert verify_password("a" * (BCRYPT_MAX_BYTES + 40), hashed) is True


@pytest.mark.tier0
def test_multibyte_password_truncates_on_bytes_not_characters():
    """Truncating the str instead of the bytes would change the prefix."""
    password = "é" * 40  # 80 bytes, 40 characters
    hashed = get_password_hash(password)
    assert verify_password(password, hashed) is True


@pytest.mark.tier0
def test_empty_stored_hash_returns_none():
    """Pre-existing contract: no stored hash is not a failed comparison."""
    assert verify_password("anything", "") is None
    assert verify_password("anything", None) is None


@pytest.mark.tier0
@pytest.mark.parametrize(
    "bad_hash",
    [
        "not-a-hash",
        "$2b$12$tooshort",
        "$9z$12$" + "x" * 53,
        "plaintext-password",
        "$2b$12$" + "!" * 53,
        "$2b$12$" + "x" * 52,
    ],
)
def test_malformed_stored_hash_is_rejected_not_raised(bad_hash):
    """A corrupt row must not take down the login path.

    This is stronger than "passlib raised UnknownHashError". bcrypt.checkpw
    does not reject a malformed hash cleanly -- it PANICS inside its Rust
    extension ("range end index N out of range for slice of length M"), and
    pyo3 surfaces that as PanicException, which derives from BaseException.
    A bare `except Exception` does not catch it. Found by running exactly this
    case; the implementation therefore shape-checks before calling into bcrypt.
    """
    assert verify_password("anything", bad_hash) is False
