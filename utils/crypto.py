from os import getenv
from typing import Any, Protocol, cast

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from umsgpack import packb, unpackb


class _Cipher(Protocol):
    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, data: bytes) -> bytes: ...


def _load_hex_env(name: str) -> bytes:
    value = getenv(name, "")
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return bytes.fromhex(value)


def _build_cipher() -> _Cipher:
    key = _load_hex_env("AES_KEY")
    iv = _load_hex_env("AES_IV")
    return cast(_Cipher, AES.new(key, AES.MODE_CBC, iv))


def encrypt(plaintext: bytes) -> bytes:
    cipher = _build_cipher()

    return cipher.encrypt(pad(plaintext, AES.block_size))


def decrypt(ciphertext: bytes) -> bytes:
    cipher = _build_cipher()

    return unpad(cipher.decrypt(ciphertext), AES.block_size)


def encrypt_msgpack(plaindict: dict[str, Any]) -> bytes:
    cipher = _build_cipher()

    return cipher.encrypt(pad(packb(plaindict), AES.block_size))


def decrypt_msgpack(ciphertext: bytes) -> dict[str, Any]:
    cipher = _build_cipher()

    return cast(
        dict[str, Any], unpackb(unpad(cipher.decrypt(ciphertext), AES.block_size))
    )
