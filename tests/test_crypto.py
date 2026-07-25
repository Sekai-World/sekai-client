import pytest

from utils import crypto


def set_aes_env(monkeypatch, *, key: bytes, iv: bytes = b"i" * 16) -> None:
    monkeypatch.setenv("AES_KEY", key.hex())
    monkeypatch.setenv("AES_IV", iv.hex())


@pytest.mark.parametrize("value", ["not-hex", "abc"])
def test_cipher_rejects_malformed_hex_lazily(monkeypatch, value):
    monkeypatch.setenv("AES_KEY", value)
    monkeypatch.setenv("AES_IV", (b"i" * 16).hex())

    with pytest.raises(RuntimeError, match="AES_KEY.*valid hexadecimal"):
        crypto._build_cipher()


@pytest.mark.parametrize("key_length", [15, 17, 31, 33])
def test_cipher_rejects_invalid_key_size_at_construction(monkeypatch, key_length):
    set_aes_env(monkeypatch, key=b"k" * key_length)

    with pytest.raises(RuntimeError, match="AES_KEY must be 16, 24, or 32 bytes"):
        crypto._build_cipher()


@pytest.mark.parametrize("iv_length", [15, 17])
def test_cipher_rejects_invalid_iv_size_at_construction(monkeypatch, iv_length):
    set_aes_env(monkeypatch, key=b"k" * 16, iv=b"i" * iv_length)

    with pytest.raises(RuntimeError, match="AES_IV must be 16 bytes"):
        crypto._build_cipher()


def test_crypto_wire_framing_is_preserved(monkeypatch):
    set_aes_env(monkeypatch, key=b"k" * 16)

    plaintext = b"payload"
    ciphertext = crypto.encrypt(plaintext)
    assert len(ciphertext) % 16 == 0
    assert crypto.decrypt(ciphertext) == plaintext

    message = {"value": "payload"}
    encrypted_message = crypto.encrypt_msgpack(message)
    assert len(encrypted_message) % 16 == 0
    assert crypto.decrypt_msgpack(encrypted_message) == message
