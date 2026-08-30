"""Encryption of the provider secrets held in the database."""

from __future__ import annotations

import pytest

from app.crypto import SecretDecryptionError, decrypt, encrypt, is_encrypted

KEY = "k" * 48
OTHER_KEY = "j" * 48


class TestRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            "simple",
            "with spaces and punctuation!",
            "quotes \" ' and backslash \\",
            "ampersand & dollar $ backtick `",
            "unicode: Grüße 日本語 🔐",
            "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----",
            "x" * 5000,
        ],
    )
    def test_values_survive_a_round_trip(self, value):
        assert decrypt(encrypt(value, KEY), KEY) == value

    def test_an_empty_value_stays_empty(self):
        """ "Unset" is not a secret, and must not become ciphertext."""
        assert encrypt("", KEY) == ""
        assert decrypt("", KEY) == ""

    def test_ciphertext_does_not_contain_the_plaintext(self):
        assert "hunter2" not in encrypt("hunter2", KEY)

    def test_encryption_is_not_deterministic(self):
        """Equal secrets must not produce equal ciphertext."""
        assert encrypt("same", KEY) != encrypt("same", KEY)

    def test_ciphertext_is_marked(self):
        assert is_encrypted(encrypt("x", KEY))
        assert not is_encrypted("plaintext")


class TestKeyHandling:
    def test_a_different_key_cannot_decrypt(self):
        with pytest.raises(SecretDecryptionError):
            decrypt(encrypt("secret", KEY), OTHER_KEY)

    def test_the_error_explains_the_likely_cause(self):
        with pytest.raises(SecretDecryptionError, match="SECRET_KEY changed"):
            decrypt(encrypt("secret", KEY), OTHER_KEY)

    def test_tampered_ciphertext_is_rejected(self):
        """Fernet authenticates, so a flipped byte fails rather than decoding to garbage."""
        token = encrypt("secret", KEY)
        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with pytest.raises(SecretDecryptionError):
            decrypt(tampered, KEY)


class TestBackwardsCompatibility:
    def test_an_unmarked_value_is_passed_through(self):
        """A hand-edited row holds plaintext; return it rather than mangling it."""
        assert decrypt("plain-value", KEY) == "plain-value"
