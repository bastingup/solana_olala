"""Encrypted private-key storage.

Keys are encrypted at rest with Fernet under a key derived from the
operator's passphrase (scrypt). The keystore is unlocked once per process
via the REST API; decrypted material lives only in this object's memory and
is never serialized back out or returned to any client.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from solders.keypair import Keypair

KEYSTORE_PATH = Path(__file__).resolve().parent.parent.parent / "keystore.enc"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


class KeystoreError(RuntimeError):
    pass


class KeystoreLocked(KeystoreError):
    pass


class EncryptedKeystore:
    def __init__(self, path: Path = KEYSTORE_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._fernet: Fernet | None = None
        self._entries: dict[str, dict[str, str]] = {}
        self._salt: bytes = b""

    @property
    def is_locked(self) -> bool:
        with self._lock:
            return self._fernet is None

    @property
    def exists(self) -> bool:
        return self._path.exists()

    def unlock(self, passphrase: str) -> None:
        """Unlock an existing keystore, or initialize a new one."""
        if not passphrase:
            raise KeystoreError("passphrase must not be empty")
        with self._lock:
            if self._path.exists():
                raw = self._path.read_bytes()
                salt, token = raw[:16], raw[16:]
                fernet = Fernet(self._derive_key(passphrase, salt))
                try:
                    payload = fernet.decrypt(token)
                except InvalidToken as exc:
                    raise KeystoreError("wrong passphrase") from exc
                self._entries = json.loads(payload)
                self._salt = salt
            else:
                self._salt = os.urandom(16)
                self._entries = {}
                fernet = Fernet(self._derive_key(passphrase, self._salt))
            self._fernet = fernet
            self._save()

    def add_key(self, label: str, secret: str) -> str:
        """Store a private key; returns the derived public address."""
        with self._lock:
            self._require_unlocked()
            keypair = self._parse_secret(secret)
            address = str(keypair.pubkey())
            self._entries[address] = {
                "label": label,
                "secret": base64.b64encode(bytes(keypair)).decode(),
            }
            self._save()
            return address

    def get_signer(self, address: str) -> Keypair:
        with self._lock:
            self._require_unlocked()
            entry = self._entries.get(address)
            if not entry:
                raise KeystoreError(f"no key stored for {address}")
            return Keypair.from_bytes(base64.b64decode(entry["secret"]))

    def addresses(self) -> list[dict[str, str]]:
        with self._lock:
            self._require_unlocked()
            return [{"address": address, "label": entry["label"]}
                    for address, entry in self._entries.items()]

    # -- internals ---------------------------------------------------------

    def _require_unlocked(self) -> None:
        if self._fernet is None:
            raise KeystoreLocked("keystore is locked")

    def _save(self) -> None:
        assert self._fernet is not None
        token = self._fernet.encrypt(
            json.dumps(self._entries).encode())
        self._path.write_bytes(self._salt + token)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass  # Windows: chmod is best-effort.

    @staticmethod
    def _derive_key(passphrase: str, salt: bytes) -> bytes:
        kdf = Scrypt(salt=salt, length=32,
                     n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))

    @staticmethod
    def _parse_secret(secret: str) -> Keypair:
        """Accept base58 secret keys and solana-keygen JSON byte arrays."""
        secret = secret.strip()
        if secret.startswith("["):
            try:
                return Keypair.from_bytes(bytes(json.loads(secret)))
            except (ValueError, TypeError) as exc:
                raise KeystoreError(f"invalid JSON keypair: {exc}") from exc
        try:
            return Keypair.from_base58_string(secret)
        except Exception as exc:
            raise KeystoreError(f"invalid base58 secret key: {exc}") from exc
