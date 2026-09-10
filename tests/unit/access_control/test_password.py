"""Scenario 1: password hashes are Argon2id and passwords are never stored/returned."""

from __future__ import annotations

from app.modules.access_control.password import (
    hash_password,
    is_argon2id_hash,
    verify_password,
)


def test_hash_password_produces_an_argon2id_hash() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    assert is_argon2id_hash(hashed)
    assert "correct-horse-battery-staple" not in hashed


def test_verify_password_accepts_the_correct_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", hashed) is True


def test_verify_password_rejects_the_wrong_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("wrong-password-entirely", hashed) is False


def test_two_hashes_of_the_same_password_differ() -> None:
    # Argon2id salts every hash independently -- equal plaintext must never
    # produce equal stored hashes.
    first = hash_password("correct-horse-battery-staple")
    second = hash_password("correct-horse-battery-staple")
    assert first != second
    assert verify_password("correct-horse-battery-staple", first)
    assert verify_password("correct-horse-battery-staple", second)
