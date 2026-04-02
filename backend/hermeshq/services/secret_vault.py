import base64
import hashlib

from cryptography.fernet import Fernet


class SecretVault:
    def __init__(self, seed: str) -> None:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value_enc: bytes) -> str:
        return self._fernet.decrypt(value_enc).decode("utf-8")

