"""RFC 6238 TOTP generation/verification and the `otpauth://` provisioning URI."""

from __future__ import annotations

from app.modules.access_control.totp import (
    generate_totp_secret,
    totp_provisioning_uri,
    verify_totp,
)


def _real_code(secret: str, *, at_time: float) -> str:
    """Compute the expected code independently of `verify_totp`'s own internals."""
    import base64
    import hashlib
    import hmac
    import struct

    counter = int(at_time // 30)
    padded = secret + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % 1_000_000).zfill(6)


def test_generate_totp_secret_is_random_and_base32() -> None:
    a = generate_totp_secret()
    b = generate_totp_secret()
    assert a != b
    assert a == a.upper()
    assert "=" not in a


def test_verify_totp_accepts_the_correct_code() -> None:
    secret = generate_totp_secret()
    at_time = 1_800_000_000.0
    code = _real_code(secret, at_time=at_time)
    assert verify_totp(secret, code, at_time=at_time) is True


def test_verify_totp_rejects_a_wrong_code() -> None:
    secret = generate_totp_secret()
    at_time = 1_800_000_000.0
    correct = _real_code(secret, at_time=at_time)
    wrong = "000000" if correct != "000000" else "111111"
    assert verify_totp(secret, wrong, at_time=at_time) is False


def test_verify_totp_tolerates_one_step_of_clock_drift() -> None:
    secret = generate_totp_secret()
    at_time = 1_800_000_000.0
    code = _real_code(secret, at_time=at_time)
    # 20 seconds later is still inside the +/-1 step (30s) drift window.
    assert verify_totp(secret, code, at_time=at_time + 20) is True


def test_verify_totp_rejects_a_code_two_steps_stale() -> None:
    secret = generate_totp_secret()
    at_time = 1_800_000_000.0
    code = _real_code(secret, at_time=at_time)
    assert verify_totp(secret, code, at_time=at_time + 90) is False


def test_verify_totp_rejects_malformed_code_shapes() -> None:
    secret = generate_totp_secret()
    assert verify_totp(secret, "12345", at_time=1_800_000_000.0) is False  # too short
    assert verify_totp(secret, "1234567", at_time=1_800_000_000.0) is False  # too long
    assert verify_totp(secret, "abcdef", at_time=1_800_000_000.0) is False  # non-digit


def test_totp_provisioning_uri_is_a_standard_otpauth_uri() -> None:
    uri = totp_provisioning_uri(secret="ABCD1234", account_name="analyst@example.test")
    assert uri.startswith("otpauth://totp/TraceX%3Aanalyst%40example.test?")
    assert "secret=ABCD1234" in uri
    assert "issuer=TraceX" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_totp_provisioning_uri_respects_custom_issuer() -> None:
    uri = totp_provisioning_uri(secret="ABCD1234", account_name="a@b.test", issuer="MyOrg")
    assert "MyOrg%3Aa%40b.test" in uri
    assert "issuer=MyOrg" in uri
