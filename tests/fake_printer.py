"""An in-process fake of the Replicator+'s kaiten JSON-RPC service, for tests.

Behaviour mirrors what Phase 0 observed on real hardware (fw 2.6.2):
plaintext :9999 + TLS :12309, authorize/reauthorize secure-only, single-use
one_time_token, wifi_connect refused off TLS with -32604, camera frames as a
notification followed by a 16-byte header + JPEG, put_raw raw bytes after reply.
"""

import asyncio
import datetime
import ipaddress
import json
import os
import secrets
import ssl
import tempfile
import zlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from openbot.printer.jsonstream import split_value

SERIAL = "23C1000B3C7059AABBCC"
FAKE_JPEG = open(os.path.join(os.path.dirname(__file__), "fixtures", "test_camera_frame.jpg"), "rb").read()


def make_cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "MakerBot Replicator"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MakerBot Industries"),
    ])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()))


class RpcFail(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class FakePrinter:
    def __init__(self, knob="accept", chunked_frames=False):
        self.knob = knob                  # "accept" | "reject" | "timeout"
        self.chunked_frames = chunked_frames
        self.local_auth = {}              # username -> (secret, code)
        self.tokens = {}                  # one_time_token -> username
        self.files = {}
        self.process = None
        self.name = "MakerBot Replicator+"
        self.wifi = [
            {"path": "/net/connman/service/wifi_home", "name": "HomeNet",
             "strength": 80, "password": "required", "_pw": "hunter22"},
            {"path": "/net/connman/service/wifi_cafe", "name": "Cafe",
             "strength": 40, "password": "none"},
            {"path": "/net/connman/service/wifi_hidden", "name": "",
             "strength": 55, "password": "required", "_pw": "secret"},
        ]
        self.net = {"state": "ethernet", "ip": "127.0.0.1", "netmask": "255.255.255.0",
                    "gateway": "127.0.0.1", "dns": ["127.0.0.1"], "static": False,
                    "wifi": "enabled", "tethering": False, "service_hash": "eth"}
        self.static = {"use_static": False}
        self.calls = []
        self._servers = []
        self._conns = set()
        self._authed = []
        self.cert_pem, self.key_pem = make_cert()

    # ------------------------------------------------------------ lifecycle

    async def start(self):
        self._tmp = tempfile.TemporaryDirectory()
        cert_path = os.path.join(self._tmp.name, "server.crt")
        key_path = os.path.join(self._tmp.name, "server.key")
        with open(cert_path, "wb") as f:
            f.write(self.cert_pem)
        with open(key_path, "wb") as f:
            f.write(self.key_pem)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)
        plain = await asyncio.start_server(lambda r, w: self._serve(r, w, False),
                                           "127.0.0.1", 0)
        tls = await asyncio.start_server(lambda r, w: self._serve(r, w, True),
                                         "127.0.0.1", 0, ssl=ctx)
        self._servers = [plain, tls]
        self.port = plain.sockets[0].getsockname()[1]
        self.ssl_port = tls.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        for s in self._servers:
            s.close()
        for w in list(self._conns):
            w.close()
        for s in self._servers:
            await s.wait_closed()
        self._tmp.cleanup()

    def swap_certificate(self):
        """Simulate a factory reset / board swap: new cert on the TLS port."""
        self.cert_pem, self.key_pem = make_cert()
        # restart TLS listener with the new cert
        return self._restart_tls()

    async def _restart_tls(self):
        old = self._servers.pop()
        old.close()
        await old.wait_closed()
        cert_path = os.path.join(self._tmp.name, "server.crt")
        key_path = os.path.join(self._tmp.name, "server.key")
        with open(cert_path, "wb") as f:
            f.write(self.cert_pem)
        with open(key_path, "wb") as f:
            f.write(self.key_pem)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)
        tls = await asyncio.start_server(lambda r, w: self._serve(r, w, True),
                                         "127.0.0.1", self.ssl_port, ssl=ctx)
        self._servers.append(tls)

    # ------------------------------------------------------------ data

    def handshake(self):
        return {"machine_type": "horseshoe", "vid": 9153, "ip": "127.0.0.1", "pid": 11,
                "api_version": "1.9.0", "iserial": SERIAL,
                "firmware_version": {"major": 2, "minor": 6, "bugfix": 2, "build": 734},
                "ssl_port": str(self.ssl_port), "machine_name": self.name,
                "motor_driver_version": "4.6", "bot_type": "replicator_b",
                "port": str(self.port)}

    def system_info(self):
        return {"machine_type": "horseshoe",
                "toolheads": {"extruder": [{"current_temperature": 29, "tool_id": 8,
                                            "tool_present": True, "index": 0, "error": 0,
                                            "target_temperature": 0,
                                            "filament_presence": True,
                                            "preheating": False}]},
                "ip": "127.0.0.1", "api_version": "1.9.0",
                "firmware_version": {"major": 2, "minor": 6, "bugfix": 2, "build": 734},
                "current_process": self.process, "machine_name": self.name,
                "sound": True, "bot_type": "replicator_b", "disabled_errors": [],
                "auto_unload": "off"}

    # ------------------------------------------------------------ connection

    async def _serve(self, reader, writer, secure):
        self._conns.add(writer)
        conn = {"secure": secure, "authed": False, "streaming": None, "writer": writer}
        buf = bytearray()
        raw_needed = 0
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    if raw_needed:
                        if len(buf) < raw_needed:
                            break
                        self._pending_upload += bytes(buf[:raw_needed])
                        del buf[:raw_needed]
                        raw_needed = 0
                        continue
                    found = split_value(buf)
                    if not found:
                        break
                    msg, end = found
                    del buf[:end]
                    raw_needed = await self._handle(conn, msg)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if conn["streaming"]:
                conn["streaming"].cancel()
            self._conns.discard(writer)
            if conn in self._authed:
                self._authed.remove(conn)
            writer.close()

    async def broadcast_state(self):
        """Push a state_notification to every authenticated client, like the real printer."""
        msg = {"jsonrpc": "2.0", "method": "state_notification",
               "params": {"info": self.system_info()}}
        for conn in list(self._authed):
            try:
                await self._send(conn["writer"], msg)
            except (ConnectionError, RuntimeError):
                self._authed.remove(conn)

    async def _send(self, writer, obj):
        writer.write(json.dumps(obj).encode())
        await writer.drain()

    async def _send_frame(self, writer):
        await self._send(writer, {"jsonrpc": "2.0", "method": "camera_frame", "params": {}})
        header = ((16 + len(FAKE_JPEG)).to_bytes(4, "big") + (640).to_bytes(4, "big")
                  + (480).to_bytes(4, "big") + (2).to_bytes(4, "big"))
        blob = header + FAKE_JPEG
        if self.chunked_frames:           # exercise partial-header/partial-body reads
            for i in range(0, len(blob), 7):
                writer.write(blob[i:i + 7])
                await writer.drain()
                await asyncio.sleep(0)
        else:
            writer.write(blob)
            await writer.drain()

    async def _handle(self, conn, msg):
        method = msg.get("method")
        params = msg.get("params") or {}
        req_id = msg.get("id")
        self.calls.append((method, params, conn["secure"]))
        writer = conn["writer"]
        raw_after = 0
        try:
            result = await self._dispatch(conn, method, params)
            if isinstance(result, tuple) and result[0] == "RAW":
                raw_after, result = result[1], True
            if req_id is not None:
                await self._send(writer, {"jsonrpc": "2.0", "id": req_id, "result": result})
            if conn["authed"] and conn not in self._authed:
                self._authed.append(conn)
            if method in self.STATE_CHANGING:
                await self.broadcast_state()
            if method == "request_camera_frame":
                await self._send_frame(writer)
            elif method == "request_camera_stream" and not conn["streaming"]:
                conn["streaming"] = asyncio.create_task(self._stream(writer))
        except RpcFail as e:
            if req_id is not None:
                await self._send(writer, {"jsonrpc": "2.0", "id": req_id,
                                          "error": {"code": e.code, "message": e.message}})
        return raw_after

    async def _stream(self, writer):
        try:
            while True:
                await self._send_frame(writer)
                await asyncio.sleep(0.02)
        except (asyncio.CancelledError, ConnectionError):
            pass

    SECURE_ONLY = {"authorize", "reauthorize", "wifi_connect"}
    STATE_CHANGING = {"put_term", "cancel", "acknowledged", "process_method", "load_filament",
                      "unload_filament", "change_machine_name"}
    NO_AUTH = {"handshake", "ping", "authenticate", "authorize", "reauthorize"}

    async def _dispatch(self, conn, method, p):
        if method in self.SECURE_ONLY and not conn["secure"]:
            raise RpcFail(-32604, "privileged information on unsecure channel")
        if method not in self.NO_AUTH and not conn["authed"]:
            raise RpcFail(-32000, "authentication required")
        handler = getattr(self, f"rpc_{method}", None)
        if handler is None:
            raise RpcFail(-32601, "method not found")
        return await handler(conn, **p)

    # ------------------------------------------------------------ auth

    async def rpc_handshake(self, conn):
        return self.handshake()

    async def rpc_authorize(self, conn, username, local_secret=None, makerbot_token=None,
                            chamber_blink=False):
        if self.knob == "reject":
            raise RpcFail(25, "authorization rejected")
        if self.knob == "timeout":
            raise RpcFail(26, "authorization timed out")
        code = secrets.token_hex(16)
        self.local_auth[username] = (local_secret, code)
        conn["authed"] = True
        token = secrets.token_hex(16)
        self.tokens[token] = username
        return {"one_time_token": token, "local_code": code}

    async def rpc_reauthorize(self, conn, username, local_secret=None, local_code=None,
                              makerbot_token=None):
        if self.local_auth.get(username) != (local_secret, local_code):
            raise RpcFail(27, "invalid credentials")
        conn["authed"] = True
        token = secrets.token_hex(16)
        self.tokens[token] = username
        return {"one_time_token": token}

    async def rpc_authenticate(self, conn, access_token):
        if self.tokens.pop(access_token, None) is None:
            raise RpcFail(-32000, "authentication failed")
        conn["authed"] = True
        return None

    async def rpc_deauthorize(self, conn, username):
        self.local_auth.pop(username, None)

    # ------------------------------------------------------------ status / control

    async def rpc_get_system_information(self, conn):
        return self.system_info()

    async def rpc_print(self, conn, filepath, transfer_wait=False,
                        ensure_build_plate_clear=None):
        self._pending_name = filepath
        return None

    async def rpc_process_method(self, conn, method, params=None):
        if method == "build_plate_cleared":
            self.plate_cleared = True
        elif method in ("suspend", "resume") and self.process:
            self.process["step"] = "suspended" if method == "suspend" else "printing"
        return None

    async def rpc_put_init(self, conn, file_path, file_id, block_size, length):
        self._pending_upload = b""
        self._pending_len = length
        self._pending_path = file_path
        return None

    async def rpc_put_raw(self, conn, file_id, length):
        return ("RAW", length)

    async def rpc_put_term(self, conn, file_id, length, crc):
        data = self._pending_upload
        if len(data) != length or (zlib.crc32(data) & 0xFFFFFFFF) != crc:
            raise RpcFail(-32000, "file transfer checksum mismatch")
        self.files[self._pending_path] = data
        self.process = {"name": "PrintProcess", "step": "initial_heating", "progress": 0,
                        "cancellable": True, "complete": False, "cancelled": False,
                        "error": None, "filename": "/home" + self._pending_path,
                        "username": "test", "methods": [], "elapsed_time": 0}
        return None

    async def rpc_cancel(self, conn, force=None):
        self.process = None

    async def rpc_acknowledged(self, conn):
        self.process = None

    async def rpc_load_filament(self, conn, tool_index, temperature_settings=None):
        self.process = {"name": "LoadFilamentProcess", "step": "heating", "progress": 0}

    async def rpc_unload_filament(self, conn, tool_index, temperature_settings=None):
        self.process = {"name": "UnloadFilamentProcess", "step": "heating", "progress": 0}

    async def rpc_preheat(self, conn, temperature_settings=None):
        return None

    async def rpc_cool(self, conn, ignore_tool_errors=None):
        return None

    async def rpc_change_machine_name(self, conn, machine_name):
        self.name = machine_name

    # ------------------------------------------------------------ camera

    async def rpc_request_camera_frame(self, conn):
        return True

    async def rpc_request_camera_stream(self, conn):
        return None

    async def rpc_end_camera_stream(self, conn):
        if conn["streaming"]:
            conn["streaming"].cancel()
            conn["streaming"] = None

    # ------------------------------------------------------------ network

    async def rpc_network_state(self, conn):
        return dict(self.net)

    async def rpc_wifi_scan(self, conn, force_rescan=False):
        return [{k: v for k, v in n.items() if not k.startswith("_")} for n in self.wifi]

    async def rpc_wifi_connect(self, conn, path, password=None, name=None):
        match = [n for n in self.wifi if n["path"] == path]
        if not match:
            raise RpcFail(50, "Service path disappeared")
        n = match[0]
        if n["password"] == "required" and password != n.get("_pw"):
            raise RpcFail(54, "invalid password")
        n["password"] = "stored" if n["password"] != "none" else "none"
        self.net.update({"state": "wifi", "name": name or n["name"], "ip": "127.0.0.2"})
        return dict(self.net)

    async def rpc_wifi_disconnect(self, conn, path=None):
        self.net.update({"state": "ethernet", "name": None})

    async def rpc_wifi_forget(self, conn, path=None):
        for n in self.wifi:
            if n["path"] == path and n["password"] == "stored":
                n["password"] = "required"

    async def rpc_wifi_enable(self, conn):
        self.net["wifi"] = "enabled"

    async def rpc_wifi_disable(self, conn):
        self.net["wifi"] = "disabled"

    async def rpc_get_static_ipv4(self, conn, service_path):
        return dict(self.static)

    async def rpc_set_static_ipv4(self, conn, service_path, ip=None, netmask=None,
                                  gateway=None, dns=None, use_static=True):
        for v in (ip, netmask, gateway):
            if v is not None:
                try:
                    ipaddress.IPv4Address(v)
                except ValueError:
                    raise RpcFail(78, "bad static ip params") from None
        self.static = {"use_static": use_static}
        if use_static:
            self.static.update({k: v for k, v in
                                (("ip", ip), ("netmask", netmask), ("gateway", gateway))
                                if v})
        return None
