"""Pairing credentials (PLAN.md §1.4).

`authorize(username, local_secret)` over TLS + a knob press returns a
`local_code`. From then on `reauthorize(username, local_secret, local_code)`
authenticates any new TLS connection with no knob press. The one_time_token
it also returns works exactly once, for a plaintext :9999 connection.
"""

import json
import os
import secrets
import socket
import sys
from dataclasses import asdict, dataclass

from .trust import default_data_dir

KEYRING_SERVICE = "OpenBot printer pairing"


@dataclass
class Credentials:
    username: str
    local_secret: str
    local_code: str

    def reauthorize_params(self):
        return {"username": self.username, "local_secret": self.local_secret,
                "local_code": self.local_code}


def default_username():
    host = socket.gethostname().split(".")[0] or "mac"
    return f"OpenBot@{host}"


def new_local_secret():
    return secrets.token_hex(32)


class FileCredentialStore:
    """JSON file with mode 0600. Used on Linux/Raspberry Pi and as a fallback."""

    def __init__(self, directory=None):
        self.path = os.path.join(directory or default_data_dir(), "credentials.json")

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self, data):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd = os.open(self.path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(self.path + ".tmp", self.path)

    def get(self, serial):
        d = self._load().get(serial)
        return Credentials(**d) if d else None

    def put(self, serial, creds):
        data = self._load()
        data[serial] = asdict(creds)
        self._save(data)

    def delete(self, serial):
        data = self._load()
        if data.pop(serial, None) is not None:
            self._save(data)


class KeychainCredentialStore:
    """macOS Keychain via `keyring`; one generic-password item per printer serial."""

    def __init__(self):
        import keyring  # imported lazily so Linux servers don't need it
        self._kr = keyring

    def get(self, serial):
        raw = self._kr.get_password(KEYRING_SERVICE, serial)
        return Credentials(**json.loads(raw)) if raw else None

    def put(self, serial, creds):
        self._kr.set_password(KEYRING_SERVICE, serial, json.dumps(asdict(creds)))

    def delete(self, serial):
        try:
            self._kr.delete_password(KEYRING_SERVICE, serial)
        except self._kr.errors.PasswordDeleteError:
            pass


def default_credential_store(directory=None):
    if sys.platform == "darwin" and not os.environ.get("OPENBOT_NO_KEYCHAIN"):
        try:
            return KeychainCredentialStore()
        except ImportError:
            pass
    return FileCredentialStore(directory)
