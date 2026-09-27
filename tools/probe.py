#!/usr/bin/env python3
"""OpenBot Phase 0 — verify the MakerBot Replicator+ network protocol on real hardware.

Stdlib only. Every step appends to probe_out/report.json and a raw transcript,
so the results can be turned into docs/protocol.md and test fixtures.

Steps (run in order; each is safe unless marked):
  discover              UDP broadcast + Bonjour browse (read-only)
  hello   --ip IP       plaintext :9999 handshake + TLS :12309 handshake (read-only)
  pair    --ip IP       authorize over TLS  ** someone must press the printer's knob **
  reauth  --ip IP       reauthorize on a fresh TLS connection, test one_time_token on :9999
  read    --ip IP       status, network_state, wifi_scan, static IP, notifications (read-only)
  camera  --ip IP       grab a few camera frames (read-only)
  secure  --ip IP       confirm wifi_connect is refused on plaintext :9999 (changes nothing)
  print   --ip IP FILE  upload and START A PRINT  ** heats and moves the printer **
"""

import argparse
import datetime
import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import time
import zlib

PLAIN_PORT = 9999
TLS_PORT = 12309
DISCOVERY_PORT = 12307
DISCOVERY_SRC_PORT = 12309
USERNAME = "OpenBot-probe@" + socket.gethostname().split(".")[0]

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "probe_out")
OUT = os.path.normpath(OUT)
REPORT = os.path.join(OUT, "report.json")
TRANSCRIPT = os.path.join(OUT, "transcript.log")
AUTH = os.path.join(OUT, "auth.json")

SECRET_KEYS = {"local_secret", "local_code", "one_time_token", "access_token", "password"}


# ---------------------------------------------------------------- reporting

def _redact(obj):
    if isinstance(obj, dict):
        # wifi_scan's "password" is a status (none/stored/required), not a secret.
        return {k: ("<redacted>" if k in SECRET_KEYS
                    and v not in ("none", "stored", "required") else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def log(direction, payload):
    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.datetime.now().isoformat(timespec="milliseconds")
    with open(TRANSCRIPT, "a") as f:
        f.write(f"{stamp} {direction} {json.dumps(_redact(payload))}\n")


def record(step, data):
    os.makedirs(OUT, exist_ok=True)
    report = {}
    if os.path.exists(REPORT):
        with open(REPORT) as f:
            report = json.load(f)
    report[step] = {"at": datetime.datetime.now().isoformat(timespec="seconds"),
                    **_redact(data)}
    with open(REPORT, "w") as f:
        json.dump(report, f, indent=2)


def ok(msg):
    print(f"  ✓ {msg}")


def bad(msg):
    print(f"  ✗ {msg}")


# ---------------------------------------------------------------- transport

class RpcError(Exception):
    def __init__(self, error):
        self.error = error
        super().__init__(f"{error.get('code')}: {error.get('message')}")


class Conn:
    """JSON-RPC over a byte stream of back-to-back JSON objects.

    A 'camera_frame' notification is followed by a 16-byte header and raw
    frame bytes; those are consumed here so they never corrupt JSON parsing.
    """

    def __init__(self, ip, port, use_tls, timeout=10):
        raw = socket.create_connection((ip, port), timeout=timeout)
        self.tls_info = None
        if use_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            # Verified on fw 2.6.2: TLSv1.2 + ECDHE-RSA-AES256-GCM-SHA384, so no
            # legacy cipher/protocol overrides are needed.
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self.sock = ctx.wrap_socket(raw)
            der = self.sock.getpeercert(binary_form=True)
            self.tls_info = {
                "version": self.sock.version(),
                "cipher": self.sock.cipher()[0],
                "cert_sha256": hashlib.sha256(der).hexdigest() if der else None,
            }
        else:
            self.sock = raw
        self.buf = b""
        self.next_id = 1
        self.notifications = []
        self.frames = []

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _fill(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.sock.settimeout(remaining)
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("printer closed the connection")
        self.buf += chunk

    def _raw(self, n, deadline):
        while len(self.buf) < n:
            self._fill(deadline)
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _split_object(self):
        """Return one complete JSON object from the buffer, or None."""
        start = None
        depth = 0
        in_str = False
        esc = False
        for i, b in enumerate(self.buf):
            c = chr(b)
            if start is None:
                if c in "{[":
                    start, depth = i, 1
                continue
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    obj = self.buf[start:i + 1]
                    self.buf = self.buf[i + 1:]
                    return json.loads(obj)
        return None

    def read_message(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            msg = self._split_object()
            if msg is not None:
                break
            self._fill(deadline)
        log("<<", msg)
        if isinstance(msg, dict) and msg.get("method") == "camera_frame":
            header = self._raw(16, deadline + 10)
            total = int.from_bytes(header[0:4], "big")
            meta = {
                "total_len": total,
                "width": int.from_bytes(header[4:8], "big"),
                "height": int.from_bytes(header[8:12], "big"),
                "format": int.from_bytes(header[12:16], "big"),
            }
            data = self._raw(max(total - 16, 0), deadline + 10)
            self.frames.append((meta, data))
            log("<<", {"camera_frame_header": meta})
        return msg

    def send(self, method, params=None, notify=False):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            payload["id"] = self.next_id
            self.next_id += 1
        log(">>", payload)
        self.sock.sendall(json.dumps(payload).encode())
        return payload.get("id")

    def call(self, method, params=None, timeout=15):
        req_id = self.send(method, params)
        deadline = time.monotonic() + timeout
        while True:
            msg = self.read_message(max(deadline - time.monotonic(), 0.01))
            if isinstance(msg, dict) and msg.get("id") == req_id:
                if "error" in msg and msg["error"] is not None:
                    raise RpcError(msg["error"])
                return msg.get("result")
            if isinstance(msg, dict) and "method" in msg:
                self.notifications.append(msg)

    def listen(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                msg = self.read_message(end - time.monotonic())
            except TimeoutError:
                break
            if isinstance(msg, dict) and "method" in msg:
                self.notifications.append(msg)


def load_auth():
    if not os.path.exists(AUTH):
        sys.exit("No probe_out/auth.json yet: run `pair` first.")
    with open(AUTH) as f:
        return json.load(f)


def secure_session(ip):
    """TLS connection authenticated with the stored local_secret/local_code."""
    auth = load_auth()
    c = Conn(ip, TLS_PORT, use_tls=True)
    c.call("handshake")
    res = c.call("reauthorize", {"username": auth["username"],
                                 "local_secret": auth["local_secret"],
                                 "local_code": auth["local_code"]})
    return c, res


# ---------------------------------------------------------------- steps

def step_discover(args):
    print("Discovery")
    found = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", DISCOVERY_SRC_PORT))
        s.settimeout(0.5)
        req = json.dumps({"command": "broadcast"}).encode()
        end = time.monotonic() + args.seconds
        last_send = 0
        while time.monotonic() < end:
            if time.monotonic() - last_send > 1:
                s.sendto(req, ("255.255.255.255", DISCOVERY_PORT))
                last_send = time.monotonic()
            try:
                data, addr = s.recvfrom(65536)
            except socket.timeout:
                continue
            try:
                info = json.loads(data)
            except ValueError:
                continue
            if isinstance(info, dict) and "machine_type" in info:
                found[addr[0]] = info
        s.close()
    except OSError as e:
        bad(f"UDP broadcast failed: {e}")

    for ip, info in found.items():
        ok(f"UDP: {info.get('machine_name')} at {ip} "
           f"(type={info.get('machine_type')}, fw={info.get('firmware_version')}, "
           f"serial={info.get('iserial')})")
    if not found:
        bad("no UDP replies (MakerBot Print's background service can hold port 12309)")

    bonjour = []
    try:
        p = subprocess.run(["dns-sd", "-B", "_makerbot-jsonrpc._tcp", "local."],
                           capture_output=True, text=True, timeout=4)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    except FileNotFoundError:
        out = ""
    for line in out.splitlines():
        if "Add" in line and "_makerbot-jsonrpc" in line:
            bonjour.append(line.split("_makerbot-jsonrpc._tcp.")[-1].strip())
    if bonjour:
        ok(f"Bonjour: {', '.join(bonjour)}")
    else:
        bad("no Bonjour adverts seen")
    record("discover", {"udp": found, "bonjour": bonjour})


def step_hello(args):
    print(f"Handshake with {args.ip}")
    result = {}
    try:
        c = Conn(args.ip, PLAIN_PORT, use_tls=False)
        hs = c.call("handshake")
        c.close()
        ok(f":9999 handshake: {hs.get('machine_name')} type={hs.get('machine_type')} "
           f"fw={hs.get('firmware_version')}")
        result["plain_handshake"] = hs
    except Exception as e:
        bad(f":9999 handshake failed: {e!r}")
        result["plain_error"] = repr(e)
    try:
        c = Conn(args.ip, TLS_PORT, use_tls=True)
        ok(f":12309 TLS up: {c.tls_info}")
        result["tls"] = c.tls_info
        hs = c.call("handshake")
        c.close()
        ok(":12309 handshake answered over TLS")
        result["tls_handshake"] = hs
    except Exception as e:
        bad(f":12309 TLS failed: {e!r}")
        result["tls_error"] = repr(e)
    record("hello", result)


def step_pair(args):
    print(f"Pairing with {args.ip} as '{USERNAME}'")
    if os.path.exists(AUTH) and not args.force:
        sys.exit("Already paired (probe_out/auth.json). Use --force to pair again.")
    local_secret = secrets.token_hex(32)
    c = Conn(args.ip, TLS_PORT, use_tls=True, timeout=180)
    c.call("handshake")
    print("  >>> Go to the printer now and PRESS THE KNOB to accept (2 min timeout) <<<")
    try:
        res = c.call("authorize", {"username": USERNAME, "local_secret": local_secret},
                     timeout=180)
    except RpcError as e:
        bad(f"authorize refused: {e}")
        record("pair", {"ok": False, "error": e.error})
        return
    finally:
        c.close()
    if not res or "local_code" not in res:
        bad(f"unexpected authorize result: {res!r}")
        record("pair", {"ok": False, "result_keys": list((res or {}).keys())})
        return
    os.makedirs(OUT, exist_ok=True)
    fd = os.open(AUTH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"username": USERNAME, "local_secret": local_secret,
                   "local_code": res["local_code"], "ip": args.ip,
                   "cert_sha256": c.tls_info["cert_sha256"]}, f, indent=2)
    ok("authorized: got local_code (saved to probe_out/auth.json, mode 0600)")
    record("pair", {"ok": True, "result_keys": sorted(res.keys())})


def step_reauth(args):
    print("Persistent pairing check")
    result = {}
    try:
        c, res = secure_session(args.ip)
        ok("reauthorize on a new TLS connection worked with no knob press")
        result["reauthorize"] = True
        info = c.call("get_system_information")
        ok("authenticated call over TLS works (get_system_information)")
        c.close()
        token = res.get("one_time_token") if res else None
    except Exception as e:
        bad(f"reauthorize failed: {e!r}")
        record("reauth", {"reauthorize": False, "error": repr(e)})
        return
    if token:
        p = Conn(args.ip, PLAIN_PORT, use_tls=False)
        p.call("handshake")
        try:
            p.call("authenticate", {"access_token": token})
            ok("one_time_token authenticates a :9999 connection")
            result["token_on_9999"] = True
        except RpcError as e:
            bad(f"one_time_token rejected on :9999: {e}")
            result["token_on_9999"] = False
        p.close()
        p = Conn(args.ip, PLAIN_PORT, use_tls=False)
        p.call("handshake")
        try:
            p.call("authenticate", {"access_token": token})
            result["token_reusable"] = True
            bad("one_time_token worked twice (expected single-use)")
        except (RpcError, ConnectionError):
            result["token_reusable"] = False
            ok("one_time_token is single-use, as the firmware source says")
        p.close()
    record("reauth", result)


def step_read(args):
    print("Read-only status and network")
    c, _ = secure_session(args.ip)
    result = {}
    for method, params in [("get_system_information", None),
                           ("network_state", None),
                           ("wifi_scan", {"force_rescan": False}),
                           ("get_static_ipv4", {"service_path": "ethernet"}),
                           ("get_authorized", None),
                           ("get_tool_usage_stats", None)]:
        try:
            result[method] = c.call(method, params, timeout=30)
            ok(method)
        except Exception as e:
            result[method] = {"error": repr(e)}
            bad(f"{method}: {e!r}")
    sysinfo = result.get("get_system_information") or {}
    th = (sysinfo.get("machine") or {}).get("toolhead_0_status") or {}
    if "tool_id" in th:
        ok(f"attached extruder tool_id = {th['tool_id']}")
    net = result.get("network_state") or {}
    if isinstance(net, dict) and "state" in net:
        ok(f"network: {net.get('state')} {net.get('ip', '')} {net.get('name', '')}")
    aps = result.get("wifi_scan")
    if isinstance(aps, list):
        ok(f"wifi_scan saw {len(aps)} networks")
    print(f"  listening for notifications for {args.seconds}s ...")
    c.listen(args.seconds)
    kinds = sorted({n.get("method") for n in c.notifications})
    ok(f"{len(c.notifications)} notifications: {kinds}")
    result["notifications_sample"] = c.notifications[:20]
    c.close()
    with open(os.path.join(OUT, "fixture_read.json"), "w") as f:
        json.dump(_redact(result), f, indent=2)
    record("read", {"methods_ok": [m for m, v in result.items()
                                   if not (isinstance(v, dict) and "error" in v)],
                    "notification_kinds": kinds,
                    "tool_id": th.get("tool_id"),
                    "machine_type": sysinfo.get("machine_type")})


def step_camera(args):
    print("Camera")
    c, _ = secure_session(args.ip)
    result = {}
    try:
        c.call("request_camera_stream")
        ok("request_camera_stream accepted")
    except Exception as e:
        bad(f"request_camera_stream: {e!r}")
        result["stream_error"] = repr(e)
    end = time.monotonic() + 20
    while len(c.frames) < args.frames and time.monotonic() < end:
        try:
            c.read_message(end - time.monotonic())
        except TimeoutError:
            break
    try:
        c.send("end_camera_stream")
    except OSError:
        pass
    c.listen(3)  # drain late frames
    fmt_names = {1: "yuyv", 2: "jpg"}
    for i, (meta, data) in enumerate(c.frames[:args.frames]):
        ext = fmt_names.get(meta["format"], "bin")
        with open(os.path.join(OUT, f"frame_{i}.{ext}"), "wb") as f:
            f.write(data)
    if c.frames:
        m = c.frames[0][0]
        ok(f"got {len(c.frames)} frames, {m['width']}x{m['height']} "
           f"format={fmt_names.get(m['format'], m['format'])} (saved in probe_out/)")
    else:
        bad("no frames received")
    result["frames"] = len(c.frames)
    result["first_frame"] = c.frames[0][0] if c.frames else None
    try:
        c.call("request_camera_frame")
        result["request_camera_frame"] = "supported"
    except RpcError as e:
        result["request_camera_frame"] = f"error {e.error.get('code')}"
    except Exception as e:
        result["request_camera_frame"] = repr(e)
    ok(f"request_camera_frame: {result['request_camera_frame']}")
    c.close()
    record("camera", result)


def step_secure(args):
    print("Secure-channel enforcement (wifi_connect on plaintext :9999)")
    s, res = secure_session(args.ip)
    s.close()
    p = Conn(args.ip, PLAIN_PORT, use_tls=False)
    p.call("handshake")
    p.call("authenticate", {"access_token": res["one_time_token"]})
    try:
        # Deliberately bogus path; the secure check runs before anything executes.
        p.call("wifi_connect", {"path": "/net/connman/service/openbot_probe_nonexistent"})
        bad("wifi_connect was NOT refused on :9999")
        record("secure", {"refused_on_9999": False})
    except RpcError as e:
        ok(f"refused on :9999 as expected ({e})")
        record("secure", {"refused_on_9999": True, "error": e.error})
    p.close()
    ok("authorize/reauthorize succeeding on :12309 already proves TLS counts as secure")


def step_print(args):
    path = args.file
    data = open(path, "rb").read()
    name = os.path.basename(path)
    print(f"PRINT {name} ({len(data)} bytes) on {args.ip}")
    if not args.yes:
        ans = input("  This will HEAT and MOVE the printer. Plate clear and you are present? [y/N] ")
        if ans.strip().lower() != "y":
            sys.exit("aborted")
    c, _ = secure_session(args.ip)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    block = 50000
    c.call("print", {"filepath": name, "transfer_wait": True})
    try:
        c.call("process_method", {"method": "build_plate_cleared"})
    except RpcError as e:
        print(f"  (build_plate_cleared: {e})")
    c.call("put_init", {"block_size": block, "file_id": "1",
                        "file_path": f"/current_thing/{name}", "length": len(data)})
    t0 = time.monotonic()
    for off in range(0, len(data), block):
        chunk = data[off:off + block]
        c.call("put_raw", {"file_id": "1", "length": len(chunk)})
        c.sock.sendall(chunk)
        print(f"  sent {off + len(chunk)}/{len(data)}", end="\r")
    print()
    c.call("put_term", {"crc": crc, "file_id": "1", "length": len(data)}, timeout=60)
    secs = time.monotonic() - t0
    ok(f"upload done in {secs:.1f}s ({len(data) / secs / 1024:.0f} KB/s over TLS)")
    c.listen(20)
    steps = [n.get("params", {}).get("info", {}).get("current_process", {})
             for n in c.notifications if n.get("method") == "state_notification"]
    record("print", {"bytes": len(data), "seconds": round(secs, 1),
                     "state_after": steps[-1:] if steps else None})
    c.close()
    ok("print started - watch the printer; cancel from its screen if anything looks wrong")


def step_watch(args):
    """Follow the current process until it finishes; save notifications + a final photo."""
    print("Watching current process")
    c, _ = secure_session(args.ip)
    seen = []
    last = None
    end = time.monotonic() + args.max_minutes * 60
    done = False
    while time.monotonic() < end and not done:
        try:
            msg = c.read_message(30)
        except TimeoutError:
            continue
        if not (isinstance(msg, dict) and msg.get("method") in
                ("state_notification", "system_notification")):
            continue
        seen.append(msg)
        info = msg.get("params", {}).get("info", {})
        proc = info.get("current_process") or {}
        ext = (info.get("toolheads", {}).get("extruder") or [{}])[0]
        line = (proc.get("name"), proc.get("step"), proc.get("progress"),
                ext.get("current_temperature"), ext.get("target_temperature"))
        if line != last:
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"  {stamp} {line[0]} step={line[1]} progress={line[2]} "
                  f"temp={line[3]}/{line[4]}", flush=True)
            last = line
        if proc.get("complete") or (last and last[0] and not proc):
            done = True
    steps = []
    for m in seen:
        s = ((m.get("params", {}).get("info", {}).get("current_process") or {}).get("step"))
        if s and (not steps or steps[-1] != s):
            steps.append(s)
    with open(os.path.join(OUT, "fixture_print_notifications.json"), "w") as f:
        json.dump(_redact(seen), f)
    c.close()
    try:
        c2, _ = secure_session(args.ip)
        c2.call("request_camera_frame")
        c2.listen(5)
        if c2.frames:
            with open(os.path.join(OUT, "print_result.jpg"), "wb") as f:
                f.write(c2.frames[0][1])
            ok("saved end-of-print photo to probe_out/print_result.jpg")
        c2.close()
    except Exception as e:
        bad(f"end photo failed: {e!r}")
    final = ((seen[-1].get("params", {}).get("info", {}).get("current_process") or {})
             if seen else {})
    record("watch", {"steps": steps, "notifications": len(seen), "done": done,
                     "final": {k: final.get(k) for k in
                               ("step", "progress", "complete", "cancelled", "error")}})
    ok(f"steps seen: {steps}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="step", required=True)
    d = sub.add_parser("discover"); d.add_argument("--seconds", type=float, default=4)
    for name in ("hello", "reauth", "secure"):
        sub.add_parser(name).add_argument("--ip", required=True)
    p = sub.add_parser("pair"); p.add_argument("--ip", required=True)
    p.add_argument("--force", action="store_true")
    r = sub.add_parser("read"); r.add_argument("--ip", required=True)
    r.add_argument("--seconds", type=float, default=60)
    cm = sub.add_parser("camera"); cm.add_argument("--ip", required=True)
    cm.add_argument("--frames", type=int, default=5)
    w = sub.add_parser("watch"); w.add_argument("--ip", required=True)
    w.add_argument("--max-minutes", type=float, default=25)
    pr = sub.add_parser("print"); pr.add_argument("--ip", required=True)
    pr.add_argument("file"); pr.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    globals()["step_" + args.step](args)


if __name__ == "__main__":
    main()
