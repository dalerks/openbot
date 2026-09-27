"""`openbot serve`: run a headless OpenBot server, and `openbot server ...` to administer it.

The running server writes a local-admin token (mode 0600) into its data folder so
`openbot server pending/approve/clients/revoke` on the same machine can manage it
through the normal API, without pairing (PLAN.md §3.2: approve from the Pi's shell).
"""

import asyncio
import json
import logging
import os
import signal
import sys

from ..printer.trust import default_data_dir
from .app import DEFAULT_PORT, OpenBotServer

log = logging.getLogger(__name__)


def _server_dir(data_dir=None):
    return os.path.join(data_dir or default_data_dir(), "server")


def _write_local_admin(server):
    path = os.path.join(server.dir, "local-admin.json")
    existing = None
    try:
        with open(path) as f:
            existing = json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    if existing and existing.get("client_id") in server.clients.clients:
        existing["port"] = server.port
        data = existing
    else:
        client, token = server.clients.add_local_admin("server console")
        data = {"client_id": client.id, "token": token, "port": server.port}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def load_config(path, args):
    """Fill unset CLI options from a TOML file (the Pi service uses /etc/openbot/server.toml).

    [server]  name, port, bind, bonjour, data
    [printer] type = "replicator_plus" | "ender3_pro" | ...,  address = "IP" (MakerBot)
              or usb_port = "/dev/ttyUSB0" (Marlin)
    """
    import tomllib
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    srv, prn = cfg.get("server", {}), cfg.get("printer", {})
    args.name = args.name or srv.get("name") or None
    if args.port == DEFAULT_PORT and srv.get("port"):
        args.port = int(srv["port"])
    if args.bind == "0.0.0.0" and srv.get("bind"):
        args.bind = srv["bind"]
    if srv.get("bonjour") is False:
        args.no_bonjour = True
    args.data = args.data or srv.get("data")
    ptype = prn.get("type", "")
    if ptype == "replicator_plus" or prn.get("address"):
        args.printer = args.printer or prn.get("address")
    else:
        args.usb = args.usb or prn.get("usb_port")
        if ptype:
            args.printer_type = ptype
    return args


async def serve(args):
    from .advertise import Advertiser
    from .host import PrinterHost
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.config:
        load_config(args.config, args)
    if not args.printer and not args.usb:
        sys.exit("give the printer: --printer IP (MakerBot) or --usb PORT (Ender), or set "
                 "it in the config file ([printer] address / usb_port)")
    host = PrinterHost(host=args.printer, usb_port=args.usb, machine_id=args.printer_type)
    server = OpenBotServer(args.data or default_data_dir(), host, name=args.name)
    had_admin = server.clients.has_admin()
    setup_code = server.clients.setup_code
    await host.start()
    port = await server.start(args.bind, args.port)
    _write_local_admin(server)
    adv = None
    if not args.no_bonjour:
        adv = Advertiser(server.name, port, {"id": server.server_id, "fp":
                                             server.fingerprint[:16], "ver": 1})
        await adv.start()
    print(f"OpenBot server '{server.name}' listening on port {port}")
    print(f"  certificate SHA-256: {server.fingerprint}")
    print(f"  web page: {server.web_url()}  (your browser will ask you to accept the "
          "server's own certificate)")
    if not had_admin and setup_code:
        print(f"\n  >>> First client setup code: {setup_code} <<<")
        print("  Enter it in OpenBot on your Mac to become this server's Admin.\n")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    print("stopping…")
    if adv:
        await adv.stop()
    await server.stop()
    await host.stop()


# ---------------------------------------------------------------- admin commands

async def admin(args):
    """openbot server pending | approve CODE [--role] | deny CODE | clients | revoke ID"""
    from ..backend import Role
    from ..remote.client import RemoteBackend
    import ssl
    path = os.path.join(_server_dir(args.data), "local-admin.json")
    try:
        with open(path) as f:
            local = json.load(f)
    except FileNotFoundError:
        sys.exit("no local server found; start it with `openbot serve` first")
    cert = os.path.join(_server_dir(args.data), "server.crt")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.load_verify_locations(cert)
    conn = await RemoteBackend.connect("127.0.0.1", local["port"], local["token"], ctx)
    try:
        if args.action == "pending":
            pending = await conn.pending_pairings()
            for p in pending:
                print(f"{p['code']}  {p['name']}  ({p['age_s']} s ago)")
            if not pending:
                print("no pending requests")
        elif args.action in ("approve", "deny"):
            match = [p for p in await conn.pending_pairings() if p["code"] == args.code]
            if not match:
                sys.exit(f"no pending request with code {args.code}")
            if args.action == "approve":
                await conn.approve_pairing(match[0]["id"], Role[args.role.upper()])
                print(f"approved {match[0]['name']} as {args.role}")
            else:
                await conn.deny_pairing(match[0]["id"])
                print(f"denied {match[0]['name']}")
        elif args.action == "clients":
            for c in await conn.list_clients():
                print(f"{c['id']}  {c['role_name']:8}  {c['name']}  last seen {c['last_seen']}")
        elif args.action == "revoke":
            ok = await conn.revoke_client(args.client_id)
            print("revoked" if ok else "no such client")
    finally:
        await conn.close()


def add_commands(sub):
    from .. import machines
    sv = sub.add_parser("serve", help="share a printer with other OpenBot apps (headless)")
    sv.add_argument("--printer", help="MakerBot printer IP (trust + pair it first)")
    sv.add_argument("--usb", help="USB serial port of a Marlin printer (Ender-3)")
    sv.add_argument("--printer-type", default="ender3_pro",
                    choices=[m for m in machines.MACHINES if m != "replicator_plus"],
                    help="for --usb (default ender3_pro)")
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
    sv.add_argument("--bind", default="0.0.0.0")
    sv.add_argument("--name", help="server name shown to clients (default: host name)")
    sv.add_argument("--data", help="data folder (default: OpenBot's)")
    sv.add_argument("--no-bonjour", action="store_true")
    sv.add_argument("--config", help="TOML settings file, e.g. /etc/openbot/server.toml")
    sv.set_defaults(func=serve)

    ad = sub.add_parser("server", help="administer the local `openbot serve`")
    ad.add_argument("--data")
    asub = ad.add_subparsers(dest="action", required=True)
    asub.add_parser("pending", help="pairing requests waiting for approval")
    ap = asub.add_parser("approve")
    ap.add_argument("code")
    ap.add_argument("--role", choices=["viewer", "operator", "admin"], default="operator")
    asub.add_parser("deny").add_argument("code")
    asub.add_parser("clients")
    asub.add_parser("revoke").add_argument("client_id")
    ad.set_defaults(func=admin)
