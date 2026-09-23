from os import getenv
from typing import Any, Protocol, cast

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from umsgpack import packb, unpackb


class _Cipher(Protocol):
    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, data: bytes) -> bytes: ...


def _load_key_material_env(name: str, sizes: tuple[int, ...], sizes_text: str) -> bytes:
    """Read AES key material configured as hexadecimal or as plain text.

    Hexadecimal wins whenever it decodes to an accepted size, so existing hex
    settings keep their meaning. A plain-text value that is itself hex of an
    accepted size (for example 32 hex digits) is therefore read as hex; such a
    value must be configured in hex instead.
    """
    value = getenv(name, "")
    if not value:
        raise RuntimeError(f"{name} is not configured")
    try:
        material = bytes.fromhex(value)
    except ValueError:
        material = b""
    if len(material) not in sizes:
        material = value.encode("utf-8")
    if len(material) not in sizes:
        raise RuntimeError(
            f"{name} must be {sizes_text} bytes long as hexadecimal or plain "
            f"text (got {len(value)} characters)"
        )
    return material


def _build_cipher() -> _Cipher:
    key = _load_key_material_env("AES_KEY", (16, 24, 32), "16, 24, or 32")
    iv = _load_key_material_env("AES_IV", (AES.block_size,), str(AES.block_size))

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
