"""Per-printer TLS certificate trust (PLAN.md §3.3).

The printer's certificate is self-signed (CN=MakerBot Replicator, CA:TRUE, no
subjectAltName), so it can never pass normal verification. We trust *this
exact certificate for this exact printer serial*: the stored PEM becomes the
only trust anchor of a CERT_REQUIRED SSL context (real TLS verification, no
hostname check because the certificate names no host).
"""

import asyncio
import datetime
import hashlib
import json
import os
import ssl
import subprocess
import sys
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from .errors import CertificateChanged, NotTrusted, PrinterError


@dataclass(frozen=True)
class CertInfo:
    pem: str
    sha256: str          # lowercase hex, no separators
    sha1: str            # needed by `security delete-certificate -Z`
    subject: str
    issuer: str
    not_before: datetime.datetime
    not_after: datetime.datetime
    is_ca: bool
    self_signed: bool

    @classmethod
    def from_pem(cls, pem):
        cert = x509.load_pem_x509_certificate(pem.encode() if isinstance(pem, str) else pem)
        try:
            is_ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        except x509.ExtensionNotFound:
            is_ca = False
        pem_text = cert.public_bytes(serialization.Encoding.PEM).decode()
        return cls(
            pem=pem_text,
            sha256=cert.fingerprint(hashes.SHA256()).hex(),
            sha1=cert.fingerprint(hashes.SHA1()).hex(),  # noqa: S303 - Keychain lookup key only
            subject=cert.subject.rfc4514_string(),
            issuer=cert.issuer.rfc4514_string(),
            not_before=cert.not_valid_before_utc,
            not_after=cert.not_valid_after_utc,
            is_ca=is_ca,
            self_signed=cert.subject == cert.issuer,
        )

    @classmethod
    def from_der(cls, der):
        return cls.from_pem(ssl.DER_cert_to_PEM_cert(der))

    @property
    def fingerprint_display(self):
        """SHA-256 as colon-separated upper-case pairs, like Keychain Access shows."""
        return ":".join(self.sha256[i:i + 2] for i in range(0, 64, 2)).upper()

    def summary(self):
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "valid_from": self.not_before.isoformat(),
            "valid_until": self.not_after.isoformat(),
            "ca": self.is_ca,
            "self_signed": self.self_signed,
            "sha256": self.fingerprint_display,
        }


def default_data_dir():
    if env := os.environ.get("OPENBOT_DATA"):
        return env
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/OpenBot")
    return os.path.expanduser("~/.local/share/openbot")


class TrustStore:
    """Trusted printer certificates, one PEM per printer serial."""

    def __init__(self, directory=None):
        self.dir = os.path.join(directory or default_data_dir(), "trust")

    def _path(self, serial):
        safe = "".join(c for c in serial if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError("printer serial is required")
        return os.path.join(self.dir, f"{safe}.pem")

    def _meta_path(self, serial):
        return self._path(serial)[:-4] + ".json"

    def get(self, serial):
        try:
            with open(self._path(serial)) as f:
                return CertInfo.from_pem(f.read())
        except FileNotFoundError:
            return None

    def trust(self, serial, cert):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        with open(self._path(serial), "w") as f:
            f.write(cert.pem)
        meta = {"trusted_at": datetime.datetime.now(datetime.UTC).isoformat(),
                "keychain_host": None}
        old = self._meta()
        if serial in old:
            meta["keychain_host"] = old[serial].get("keychain_host")
        self._write_meta(serial, meta)

    def remove(self, serial):
        cert = self.get(serial)
        meta = self._meta().get(serial, {})
        if cert and meta.get("keychain_host"):
            remove_from_keychain(cert)
        for p in (self._path(serial), self._meta_path(serial)):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def all(self):
        if not os.path.isdir(self.dir):
            return {}
        out = {}
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".pem"):
                serial = name[:-4]
                out[serial] = self.get(serial)
        return out

    def import_file(self, serial, path):
        with open(path, "rb") as f:
            data = f.read()
        if b"-----BEGIN CERTIFICATE-----" in data:
            cert = CertInfo.from_pem(data.decode())
        else:
            cert = CertInfo.from_der(data)
        self.trust(serial, cert)
        return cert

    def export_file(self, serial, path):
        cert = self.get(serial)
        if not cert:
            raise PrinterError(f"no trusted certificate for {serial}")
        with open(path, "w") as f:
            f.write(cert.pem)
        return cert

    def mark_keychain(self, serial, host):
        meta = self._meta().get(serial, {})
        meta["keychain_host"] = host
        self._write_meta(serial, meta)

    def _meta(self):
        out = {}
        if os.path.isdir(self.dir):
            for name in os.listdir(self.dir):
                if name.endswith(".json"):
                    with open(os.path.join(self.dir, name)) as f:
                        out[name[:-5]] = json.load(f)
        return out

    def _write_meta(self, serial, meta):
        with open(self._meta_path(serial), "w") as f:
            json.dump(meta, f, indent=2)

    # ------------------------------------------------------------ contexts

    def context_for(self, serial, presented=None):
        """Verified SSL context for `serial`.

        Raises NotTrusted if nothing is stored, or CertificateChanged if
        `presented` (the cert the printer just offered) differs from the stored one.
        """
        trusted = self.get(serial)
        if trusted is None:
            if presented is None:
                raise PrinterError("certificate unknown; fetch it first")
            raise NotTrusted(presented)
        if presented is not None and presented.sha256 != trusted.sha256:
            raise CertificateChanged(trusted, presented)
        return verified_context(trusted.pem)


def verified_context(pem):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False          # the certificate names no host
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=pem)
    return ctx


def unverified_context():
    """Only for fetching a certificate to show the user. Never used for commands."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def fetch_certificate(host, port=12309, timeout=10.0):
    """Connect without verification just to read the certificate the printer presents."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=unverified_context(),
                                    server_hostname=host), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise PrinterError(f"cannot reach {host}:{port}: {e!r}") from e
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        return CertInfo.from_der(der)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- macOS Keychain

def _login_keychain():
    return os.path.expanduser("~/Library/Keychains/login.keychain-db")


def add_to_keychain(cert, host):
    """Trust `cert` in the login keychain for SSL to `host` only.

    Never adds it as an unrestricted root: the cert is CA:TRUE, so unrestricted
    trust would let whoever holds its key impersonate any site to this Mac.
    macOS shows its own password prompt.
    """
    if sys.platform != "darwin":
        raise PrinterError("Keychain trust is only available on macOS")
    tmp = os.path.join(default_data_dir(), "trust", f".keychain-{cert.sha256[:16]}.pem")
    os.makedirs(os.path.dirname(tmp), mode=0o700, exist_ok=True)
    with open(tmp, "w") as f:
        f.write(cert.pem)
    try:
        subprocess.run(["security", "add-trusted-cert", "-r", "trustRoot",
                        "-p", "ssl", "-s", host, "-k", _login_keychain(), tmp],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise PrinterError(f"Keychain refused the certificate: {e.stderr.strip()}") from e
    finally:
        os.remove(tmp)


def remove_from_keychain(cert):
    if sys.platform != "darwin":
        return
    tmp = os.path.join(default_data_dir(), "trust", f".keychain-{cert.sha256[:16]}.pem")
    with open(tmp, "w") as f:
        f.write(cert.pem)
    try:
        subprocess.run(["security", "remove-trusted-cert", tmp], capture_output=True)
        subprocess.run(["security", "delete-certificate", "-Z", cert.sha1.upper(),
                        _login_keychain()], capture_output=True)
    finally:
        os.remove(tmp)
