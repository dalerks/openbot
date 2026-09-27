"""Server-side client accounts, pairing requests and the TLS certificate.

* A client asks to join (POST /api/pair) and gets a 6-digit code to compare with
  the one shown on the server (or on any Admin client for a headless server).
* An Admin approves it with a role; the client receives a long random token once.
* Only a SHA-256 of each token is stored.
* A headless server with no Admin yet prints a one-time setup code; entering it
  makes that first client an Admin (PLAN.md §3.2).
"""

import datetime
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import time
from dataclasses import asdict, dataclass

from ..backend import Role

PAIRING_TTL = 300          # seconds a pairing request stays open
MAX_PENDING = 20           # rate limit: open requests at once


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Client:
    id: str
    name: str
    role: int
    token_hash: str
    created: str
    last_seen: str | None = None
    console: bool = False          # the server's own shell/UI, not a paired device

    def public(self):
        d = asdict(self)
        d.pop("token_hash")
        d["role_name"] = Role(self.role).name.lower()
        return d


@dataclass
class PairingRequest:
    id: str
    name: str
    code: str
    created: float
    status: str = "pending"        # pending | approved | denied | expired
    token: str | None = None       # handed out once, on the first poll after approval
    role: int | None = None

    def public(self):
        return {"id": self.id, "name": self.name, "code": self.code, "status": self.status,
                "age_s": round(time.time() - self.created)}


class ClientStore:
    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, "clients.json")
        self.clients: dict[str, Client] = {}
        self.pending: dict[str, PairingRequest] = {}
        self._listeners = []
        self._load()
        self.setup_code = None if self.has_admin() else self._new_setup_code()

    # ------------------------------------------------------------ persistence

    def _load(self):
        try:
            with open(self.path) as f:
                for d in json.load(f):
                    c = Client(**d)
                    self.clients[c.id] = c
        except FileNotFoundError:
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump([asdict(c) for c in self.clients.values()], f, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def _new_setup_code():
        """e.g. 'K7QM-4XPT'. No 0/O/1/I, so it's easy to read off a terminal."""
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(chars) for _ in range(8))
        return f"{raw[:4]}-{raw[4:]}"

    @staticmethod
    def _normalise_code(code):
        return (code or "").replace("-", "").replace(" ", "").upper()

    def on_change(self, callback):
        self._listeners.append(callback)

    def _changed(self):
        for cb in list(self._listeners):
            cb()

    # ------------------------------------------------------------ accounts

    def has_admin(self):
        """A paired Admin device exists (the server's own console doesn't count)."""
        return any(c.role >= Role.ADMIN and not c.console for c in self.clients.values())

    def authenticate(self, token):
        if not token:
            return None
        h = _hash(token)
        for c in self.clients.values():
            if secrets.compare_digest(c.token_hash, h):
                now = datetime.datetime.now(datetime.UTC)
                stale = (c.last_seen is None or now - datetime.datetime.fromisoformat(
                    c.last_seen) > datetime.timedelta(minutes=1))
                c.last_seen = now.isoformat(timespec="seconds")
                if stale:        # persist at most once a minute (SD-card friendly on a Pi)
                    self._save()
                return c
        return None

    def _create(self, name, role, console=False):
        token = secrets.token_urlsafe(32)
        c = Client(id=secrets.token_hex(8), name=name[:64], role=int(role),
                   token_hash=_hash(token), console=console,
                   created=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"))
        self.clients[c.id] = c
        self._save()
        return c, token

    def add_local_admin(self, name):
        """The server's own UI/CLI: an Admin that never needs pairing. It doesn't use up
        the setup code, which is for the first paired device."""
        c, token = self._create(name, Role.ADMIN, console=True)
        self._changed()
        return c, token

    def revoke(self, client_id):
        if self.clients.pop(client_id, None):
            self._save()
            self._changed()
            return True
        return False

    def set_role(self, client_id, role):
        c = self.clients[client_id]
        c.role = int(role)
        self._save()
        self._changed()

    # ------------------------------------------------------------ pairing

    def request(self, name, setup_code=None):
        self._expire()
        if setup_code:
            if not self.setup_code or not secrets.compare_digest(
                    self._normalise_code(setup_code), self._normalise_code(self.setup_code)):
                raise PermissionError("wrong setup code")
            c, token = self._create(name, Role.ADMIN)
            self.setup_code = None
            req = PairingRequest(secrets.token_hex(8), name, "", time.time(), "approved",
                                 token, Role.ADMIN)
            self.pending[req.id] = req
            self._changed()
            return req
        if sum(r.status == "pending" for r in self.pending.values()) >= MAX_PENDING:
            raise PermissionError("too many pairing requests; try again later")
        req = PairingRequest(secrets.token_hex(8), name[:64],
                             f"{secrets.randbelow(10**6):06d}", time.time())
        self.pending[req.id] = req
        self._changed()
        return req

    def poll(self, request_id):
        self._expire()
        req = self.pending.get(request_id)
        if req is None:
            return {"status": "unknown"}
        out = {"status": req.status}
        if req.status == "approved" and req.token:
            out.update(token=req.token, role=req.role)
            req.token = None                # hand the token out exactly once
            self.pending.pop(request_id, None)
        return out

    def approve(self, request_id, role=Role.OPERATOR):
        req = self.pending.get(request_id)
        if req is None or req.status != "pending":
            raise KeyError("no such pending request")
        _, token = self._create(req.name, role)
        req.status, req.token, req.role = "approved", token, int(role)
        self._changed()

    def deny(self, request_id):
        req = self.pending.get(request_id)
        if req is not None and req.status == "pending":
            req.status = "denied"
            self._changed()

    def pending_requests(self):
        self._expire()
        return [r.public() for r in self.pending.values() if r.status == "pending"]

    def _expire(self):
        now = time.time()
        for rid, r in list(self.pending.items()):
            if now - r.created > PAIRING_TTL:
                if r.status == "pending":
                    r.status = "expired"
                if now - r.created > PAIRING_TTL * 2:
                    self.pending.pop(rid, None)


# ---------------------------------------------------------------- TLS certificate

def ensure_certificate(directory, server_name):
    """Self-signed cert + key for this server (created once). Returns (cert_path, key_path)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    cert_path = os.path.join(directory, "server.crt")
    key_path = os.path.join(directory, "server.key")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    os.makedirs(directory, mode=0o700, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"OpenBot {server_name}"[:64]),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OpenBot")])
    host = socket.gethostname()
    alt = [x509.DNSName(host), x509.DNSName(host.split(".")[0] + ".local"),
           x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path
