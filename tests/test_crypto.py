import pytest

from tunebox import crypto

RFC_SECRET = b"12345678901234567890"


def test_a_password_hash_verifies_and_describes_itself() -> None:
    stored = crypto.hash_password("correct horse battery")
    assert stored.startswith("scrypt$32768$8$1$67108864$")
    assert crypto.verify_password(stored, "correct horse battery")
    assert not crypto.verify_password(stored, "correct horse batterY")


@pytest.mark.parametrize("stored", ["", "md5$x", "scrypt$1$2$3", "scrypt$a$8$1$1$AA$AA"])
def test_a_damaged_hash_never_verifies(stored: str) -> None:
    assert not crypto.verify_password(stored, "anything at all")


@pytest.mark.parametrize(
    ("seconds", "code"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
    ],
)
def test_totp_matches_rfc_6238(seconds: int, code: str) -> None:
    assert crypto.totp(RFC_SECRET, seconds // 30, digits=8) == code


def test_matching_step_accepts_one_step_either_side_and_only_later_steps() -> None:
    now = 1_000_000.0
    step = int(now // 30)
    for offset in (-1, 0, 1):
        found = crypto.matching_step(RFC_SECRET, crypto.totp(RFC_SECRET, step + offset), now, 0)
        assert found == step + offset
    assert crypto.matching_step(RFC_SECRET, crypto.totp(RFC_SECRET, step + 2), now, 0) is None
    assert crypto.matching_step(RFC_SECRET, crypto.totp(RFC_SECRET, step), now, step) is None
    assert crypto.matching_step(RFC_SECRET, "12a456", now, 0) is None
    assert crypto.matching_step(RFC_SECRET, "١٢٣٤٥٦", now, 0) is None


def test_otpauth_uri_carries_the_secret_and_the_issuer() -> None:
    secret = crypto.new_totp_secret()
    uri = crypto.otpauth_uri(secret)
    assert uri.startswith("otpauth://totp/tunebox%3Aadmin?")
    assert f"secret={crypto.secret_text(secret)}" in uri
    assert "issuer=tunebox" in uri


def test_recovery_codes_are_distinct_and_recognised() -> None:
    codes = crypto.new_recovery_codes()
    assert len(set(codes)) == 10
    assert all(crypto.is_recovery_code(code) for code in codes)
    assert not crypto.is_recovery_code("123456")
    assert crypto.code_hash(codes[0].upper()) == crypto.code_hash(f" {codes[0]} ")


def test_signed_values_round_trip_and_reject_tampering() -> None:
    key = b"k" * 32
    signed = crypto.sign(key, "k1.3.1999999999")
    assert crypto.unsign(key, signed) == "k1.3.1999999999"
    assert crypto.unsign(key, signed.replace("k1.3", "k1.4")) is None
    assert crypto.unsign(b"x" * 32, signed) is None
    assert crypto.unsign(key, "no-signature") is None
    assert crypto.unsign(key, "payload.!!!") is None


def test_tokens_are_long_and_hashed() -> None:
    token = crypto.new_token()
    assert len(token) >= 43
    assert crypto.token_hash(token) != token
    assert len(crypto.token_hash(token)) == 64


def test_the_dummy_hash_is_made_once() -> None:
    assert crypto.dummy_hash() is crypto.dummy_hash()
    assert crypto.dummy_hash().startswith("scrypt$")
