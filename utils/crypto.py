from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from os import getenv
from umsgpack import packb, unpackb

KEY = bytes.fromhex(getenv("AES_KEY", ""))
IV = bytes.fromhex(getenv("AES_IV", ""))


def encrypt(plaintext: bytes) -> bytes:
    cipher = AES.new(KEY, AES.MODE_CBC, IV)

    return cipher.encrypt(pad(plaintext, AES.block_size))


def decrypt(ciphertext: bytes) -> bytes:
    cipher = AES.new(KEY, AES.MODE_CBC, IV)

    return unpad(cipher.decrypt(ciphertext), AES.block_size)


def encrypt_msgpack(plaindict: dict) -> bytes:
    cipher = AES.new(KEY, AES.MODE_CBC, IV)

    return cipher.encrypt(pad(packb(plaindict), AES.block_size))


def decrypt_msgpack(ciphertext: bytes) -> dict:
    cipher = AES.new(KEY, AES.MODE_CBC, IV)

    return unpackb(unpad(cipher.decrypt(ciphertext), AES.block_size))
