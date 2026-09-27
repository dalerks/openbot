"""Exceptions raised by openbot.printer."""


class PrinterError(Exception):
    """Base class for everything that can go wrong talking to a printer."""


class RpcError(PrinterError):
    """The printer answered a JSON-RPC call with an error object."""

    def __init__(self, code, message, data=None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"{message} (code {code})")


class SecureChannelRequired(RpcError):
    """Method is only allowed on the TLS port (kaiten `require_secure`)."""


class AuthError(PrinterError):
    """Pairing was rejected/timed out, or stored credentials are no longer valid."""


class NotTrusted(PrinterError):
    """The printer's certificate has not been trusted yet.

    `cert` carries the certificate details so a UI can show the trust prompt.
    """

    def __init__(self, cert):
        self.cert = cert
        super().__init__(f"printer certificate not trusted yet ({cert.fingerprint_display})")


class CertificateChanged(PrinterError):
    """The printer presented a different certificate than the one we trusted."""

    def __init__(self, trusted, presented):
        self.trusted = trusted
        self.presented = presented
        super().__init__(
            "printer certificate changed: trusted "
            f"{trusted.fingerprint_display}, got {presented.fingerprint_display}")


# kaiten error codes seen in firmware source and on real hardware.
UNSECURE_CHANNEL = -32604
AUTH_REJECTED = 25
AUTH_TIMED_OUT = 26
INVALID_CREDENTIALS = 27
SERVICE_PATH_DISAPPEARED = 50
WIFI_WHILE_TETHERED = 56
BAD_STATIC_IP = 78


def rpc_error_from(error):
    code = error.get("code")
    message = error.get("message", "unknown error")
    cls = SecureChannelRequired if code == UNSECURE_CHANNEL else RpcError
    return cls(code, message, error.get("data"))
