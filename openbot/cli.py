"""`openbot` command line.

    openbot discover
    openbot pair 192.168.1.154          # trust prompt, then press the knob
    openbot status [--watch]
    openbot print cube.makerbot
    openbot camera --snapshot plate.jpg
    openbot wifi scan | connect SSID | forget SSID | disconnect | on | off
    openbot ip show | dhcp | static IP NETMASK GATEWAY [--dns 1.1.1.1 ...]
    openbot cert show | trust | list | export FILE | import FILE | remove | keychain

The printer defaults to the last one used (-p/--printer to choose another).
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
import time

from .printer import (AuthError, CertificateChanged, NotTrusted, Printer, PrinterError)
from .printer import discovery
from .printer.client import check_print_file, inspect_print_file
from .printer.trust import TrustStore, add_to_keychain, default_data_dir

CONFIG = os.path.join(default_data_dir(), "cli.json")


# ---------------------------------------------------------------- config / output

def _load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


def remember(printer):
    cfg = _load_config()
    cfg["last_printer"] = printer.host
    if printer.info:
        cfg.setdefault("printers", {})[printer.info.serial] = {
            "ip": printer.host, "name": printer.info.name}
    _save_config(cfg)


def ok(msg):
    print(f"✓ {msg}")


def warn(msg):
    print(f"! {msg}")


def fail(msg, code=1):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


def confirm(question, assume_yes=False):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def show_cert(cert, title="Printer certificate"):
    print(title)
    for k, v in cert.summary().items():
        print(f"  {k:12} {v}")


# ---------------------------------------------------------------- connecting

def _host(args):
    host = args.printer or _load_config().get("last_printer")
    if not host:
        fail("no printer given; run `openbot discover`, then use -p IP")
    return host


async def ensure_trusted(printer, assume_yes=False):
    """Run the trust prompt if needed. Returns True if the cert is trusted afterwards."""
    if printer.info is None:
        await printer.hello()
    if printer.trust.get(printer.info.serial):
        return True
    cert = await printer.fetch_certificate()
    print(f"{printer.info.name} ({printer.info.serial}) at {printer.host}")
    show_cert(cert)
    print("  This is a self-signed certificate. Trusting it means OpenBot will only talk to")
    print("  a printer presenting exactly this certificate at this serial number.")
    if not confirm("Trust this printer's certificate?", assume_yes):
        return False
    printer.trust.trust(printer.info.serial, cert)
    ok("certificate trusted")
    return True


async def connected(args, need_auth=True):
    printer = Printer(_host(args))
    try:
        if not await ensure_trusted(printer, getattr(args, "yes", False)):
            fail("certificate not trusted; nothing was sent to the printer")
        if need_auth:
            await printer.connect()
    except CertificateChanged as e:
        show_cert(e.trusted, "Trusted certificate")
        show_cert(e.presented, "Certificate the printer presents NOW")
        fail("the printer's identity changed (factory reset, board swap, or impersonation).\n"
             "  If you expect this, run: openbot cert trust --replace")
    except AuthError as e:
        fail(f"{e}. Run `openbot pair` (you'll need to press the knob on the printer).")
    except PrinterError as e:
        fail(str(e))
    remember(printer)
    return printer


# ---------------------------------------------------------------- commands

async def cmd_discover(args):
    if discovery.conveyor_running():
        warn("MakerBot Print's background service (conveyor-svc) is running; "
             "UDP discovery won't get replies while it runs. Using Bonjour/ARP instead.")
    found = await discovery.discover(args.seconds, allow_sweep=not args.no_sweep)
    if not found:
        fail("no printers found. Try `openbot -p <ip> status`.")
    for p in found:
        print(f"{p.name:24} {p.ip or '?':16} serial={p.serial} fw={p.firmware} "
              f"type={p.bot_type or p.machine_type} via {p.source}")


async def cmd_pair(args):
    printer = await connected(args, need_auth=False)
    if printer.credentials.get(printer.info.serial) and not args.force:
        try:
            await printer.connect()
            ok("already paired; reconnect works (use --force to pair again)")
            await printer.close()
            return
        except AuthError:
            warn("stored pairing no longer works; pairing again")
    print(">>> Go to the printer and PRESS THE KNOB to accept OpenBot (3 minutes) <<<")
    try:
        await printer.pair()
    except AuthError as e:
        fail(str(e))
    ok(f"paired with {printer.info.name}; credentials stored")
    await printer.close()


def _fmt_status(s):
    parts = [f"{s.name}"]
    e = s.extruder
    if e:
        parts.append(f"{e.display_name} {e.current_temperature:.0f}/{e.target_temperature:.0f}°C")
        parts.append("filament " + ("loaded" if e.filament_present else "NOT detected"))
    if s.process:
        p = s.process
        prog = f" {p.progress}%" if p.progress is not None else ""
        parts.append(f"{p.name}: {p.step}{prog}")
    else:
        parts.append("idle")
    return " | ".join(parts)


async def cmd_status(args):
    printer = await connected(args)
    s = await printer.status()
    if args.json:
        print(json.dumps(s.raw, indent=2))
    else:
        print(_fmt_status(s))
    if args.watch:
        last = [None]

        def show(st):
            line = _fmt_status(st)
            if line != last[0]:
                print(time.strftime("%H:%M:%S"), line, flush=True)
                last[0] = line
        printer.on_status(show)
        done = asyncio.get_running_loop().create_future()
        printer.on_disconnect(lambda err: done.done() or done.set_result(err))
        try:
            await done
        finally:
            await printer.close()
        fail("printer connection closed")
    await printer.close()


def _settings_from_args(args, default_extruder=None):
    from .slicing import PrintSettings, SettingsError
    machine = getattr(args, "printer_type", None) or "replicator_plus"
    extruder = args.extruder or default_extruder or ""
    try:
        return PrintSettings(machine=machine, extruder=extruder, material=args.material,
                             quality=args.quality, infill_percent=args.infill,
                             walls=args.walls, supports=args.supports,
                             support_style=args.support_style, adhesion=args.adhesion,
                             temperature=args.temp,
                             bed_temperature=getattr(args, "bed_temp", None)).validate()
    except SettingsError as e:
        fail(str(e))


def _run_slice(models, out, settings):
    from .slicing import MeshError, SlicerError, ToolpathError, slice_models
    if isinstance(models, str):
        models = [models]
    names = ", ".join(os.path.basename(m) for m in models)
    print(f"Slicing {names} for {settings.extruder_name}, "
          f"{settings.material_name} {settings.nozzle_temperature}°C, {settings.quality} "
          f"quality, {settings.infill_percent}% infill"
          f"{', supports (' + settings.support_style + ')' if settings.supports else ''}...")
    try:
        r = slice_models(models, out, settings)
    except (MeshError, SlicerError, ToolpathError) as e:
        fail(str(e))
    for w in r.warnings:
        warn(w)
    h, m = divmod(round(r.duration_s / 60), 60)
    ok(f"{out}: {r.layers} layers, about {f'{h} h ' if h else ''}{m} min, "
       f"{r.filament_g:.1f} g of filament")
    return r


async def cmd_slice(args):
    settings = _settings_from_args(args)
    out = args.output or os.path.splitext(args.models[0])[0] + \
        settings.machine_def.file_extension
    _run_slice(args.models, out, settings)


async def cmd_print(args):
    if args.file.lower().endswith((".stl", ".obj")):
        printer = await connected(args)
        status = await printer.status()
        attached = status.extruder.tool_type if status.extruder else None
        if args.extruder is None and attached:
            print(f"Using the attached extruder: {status.extruder.display_name}")
        settings = _settings_from_args(args, attached)
        await printer.close()
        out = os.path.splitext(args.file)[0] + ".makerbot"
        _run_slice(args.file, out, settings)
        args.file = out
    try:
        meta = inspect_print_file(args.file)
    except Exception as e:  # noqa: BLE001
        fail(f"cannot read {args.file}: {e}")
    printer = await connected(args)
    status = await printer.status()
    problems = check_print_file(args.file, status)
    mins = (meta.get("duration_s") or 0) / 60
    print(f"{os.path.basename(args.file)}: {meta.get('material')} "
          f"{meta.get('extruder_temperature')}°C, about {mins:.0f} min, "
          f"extruder {meta.get('tool_type')}")
    for p in problems:
        warn(p)
    if problems and not args.force:
        fail("not printing (use --force to override the checks above)")
    if not confirm("The printer will HEAT and MOVE. Build plate clear and someone present?",
                   args.yes):
        fail("cancelled")

    def progress(sent, total):
        print(f"\r  uploading {sent * 100 // total}%", end="", flush=True)
    await printer.start_print(args.file, progress=progress)
    print()
    ok("print started")
    await printer.close()


async def cmd_simple(args):
    printer = await connected(args)
    method = {"cancel": printer.cancel, "pause": printer.pause, "resume": printer.resume,
              "ack": printer.acknowledge, "preheat": printer.preheat, "cool": printer.cool}
    await method[args.command]()
    ok(f"{args.command} sent")
    await printer.close()


async def cmd_filament(args):
    printer = await connected(args)
    action = {"load": printer.load_filament, "unload": printer.unload_filament,
              "stop": printer.stop_filament}[args.action]
    await (action() if args.action == "stop" else action(0))
    ok(f"filament {args.action} started; follow the printer's screen")
    await printer.close()


async def cmd_camera(args):
    printer = await connected(args)
    if args.snapshot:
        frame = await printer.snapshot()
        with open(args.snapshot, "wb") as f:
            f.write(frame.data)
        ok(f"saved {frame.width}x{frame.height} {'JPEG' if frame.is_jpeg else 'YUYV'} "
           f"to {args.snapshot}")
    else:
        os.makedirs(args.dir, exist_ok=True)
        n = 0
        async for frame in printer.camera_stream():
            path = os.path.join(args.dir, f"frame_{n:04d}.jpg")
            with open(path, "wb") as f:
                f.write(frame.data)
            n += 1
            print(f"\r  {n} frames", end="", flush=True)
            if n >= args.frames:
                break
        print()
        ok(f"saved {n} frames to {args.dir}")
    await printer.close()


def _bars(strength):
    return "▂▄▆█"[:max(1, min(4, strength // 25 + 1))].ljust(4)


async def cmd_wifi(args):
    printer = await connected(args)
    serial = printer.info.serial
    try:
        if args.action == "scan":
            state = await printer.network_state()
            for n in await printer.wifi_scan(force_rescan=True):
                mark = "*" if state.state == "wifi" and n.name == state.name else " "
                lock = "open" if not n.secured else ("saved" if n.saved else "secured")
                print(f" {mark} {_bars(n.strength)} {n.strength:3d}  "
                      f"{n.name or '<hidden network>':32} {lock}")
            return
        if args.action in ("on", "off"):
            await (printer.wifi_enable() if args.action == "on" else printer.wifi_disable())
            ok(f"Wi-Fi radio {args.action}")
            return
        if args.action == "disconnect":
            await printer.wifi_disconnect()
            ok("disconnected from Wi-Fi")
            return
        nets = await printer.wifi_scan(force_rescan=True)
        if args.action == "forget":
            match = [n for n in nets if n.name == args.ssid]
            if not match:
                fail(f"'{args.ssid}' not in range; the printer can only forget visible networks")
            await printer.wifi_forget(match[0].path)
            ok(f"forgot {args.ssid}")
            return
        # connect
        if args.hidden:
            match = [n for n in nets if n.hidden]
            if not match:
                fail("no hidden networks visible to the printer")
        else:
            match = [n for n in nets if n.name == args.ssid]
            if not match:
                fail(f"'{args.ssid}' not found. Visible: "
                     + ", ".join(sorted({n.name for n in nets if n.name})))
        net = match[0]
        password = args.password
        if (net.secured and not net.saved or args.hidden) and password is None:
            password = getpass.getpass(f"Password for {args.ssid}: ")
        state = await printer.network_state()
        if state.state == "wifi":
            warn("the printer is on Wi-Fi now; this connection will drop while it switches")
        try:
            new = await printer.wifi_connect(net.path, password,
                                             name=args.ssid if args.hidden else None)
            ok(f"connected to {args.ssid}: {new.ip}")
        except PrinterError as e:
            if "closed" not in str(e):
                raise
            warn("connection dropped as expected; looking for the printer again...")
            new_ip = await _rediscover(serial)
            if not new_ip:
                fail("couldn't find the printer again yet. Check its screen, then run "
                     "`openbot discover`.")
            cfg = _load_config()
            cfg["last_printer"] = new_ip
            _save_config(cfg)
            ok(f"printer is back at {new_ip}")
    finally:
        await printer.close()


async def _rediscover(serial, attempts=6):
    for _ in range(attempts):
        await asyncio.sleep(10)
        for p in await discovery.discover(3, allow_sweep=True):
            if p.serial == serial and p.ip:
                return p.ip
    return None


async def cmd_ip(args):
    printer = await connected(args)
    try:
        state = await printer.network_state()
        service = "ethernet"
        if state.state == "wifi":
            nets = await printer.wifi_scan()
            match = [n for n in nets if n.name == state.name]
            service = match[0].path if match else "ethernet"
        if args.action == "show":
            print(f"connection: {state.state} {state.name or ''}")
            print(f"address:    {state.ip}/{state.netmask} gateway {state.gateway}")
            print(f"dns:        {', '.join(state.dns) or '-'}")
            print(f"mode:       {'static' if state.static else 'DHCP'}")
            print(f"Wi-Fi radio {state.wifi_radio}")
            return
        warn(f"changing the {state.state} address can move the printer away from "
             f"{printer.host}")
        if not confirm("Continue?", args.yes):
            fail("cancelled")
        if args.action == "dhcp":
            await printer.use_dhcp(service)
            ok("switched to DHCP")
        else:
            await printer.set_static_ipv4(service, ip=args.ip, netmask=args.netmask,
                                          gateway=args.gateway, dns=args.dns)
            cfg = _load_config()
            cfg["last_printer"] = args.ip
            _save_config(cfg)
            ok(f"static address {args.ip} set")
    finally:
        await printer.close()


async def cmd_rename(args):
    printer = await connected(args)
    await printer.rename(args.name)
    ok(f"renamed to {args.name}")
    await printer.close()


async def cmd_cert(args):
    store = TrustStore()
    if args.action == "list":
        for serial, cert in store.all().items():
            print(f"{serial}  {cert.fingerprint_display}")
        return
    printer = Printer(_host(args), trust=store)
    try:
        await printer.hello()
    except PrinterError as e:
        fail(str(e))
    serial = printer.info.serial
    if args.action == "show":
        trusted = store.get(serial)
        presented = await printer.fetch_certificate()
        show_cert(presented, "Presented by the printer")
        if trusted is None:
            warn("not trusted yet (openbot cert trust)")
        elif trusted.sha256 == presented.sha256:
            ok("matches the trusted certificate")
        else:
            warn("DIFFERENT from the trusted certificate")
    elif args.action == "trust":
        if store.get(serial) and not args.replace:
            ok("already trusted (use --replace after a factory reset)")
            return
        cert = await printer.fetch_certificate()
        show_cert(cert)
        if confirm("Trust this certificate?", args.yes):
            store.trust(serial, cert)
            ok("trusted")
    elif args.action == "remove":
        store.remove(serial)
        ok("trust removed (also from Keychain if it was added)")
    elif args.action == "export":
        store.export_file(serial, args.file)
        ok(f"exported to {args.file}")
    elif args.action == "import":
        cert = store.import_file(serial, args.file)
        show_cert(cert, "Imported and trusted")
    elif args.action == "keychain":
        cert = store.get(serial)
        if not cert:
            fail("trust the certificate in OpenBot first (openbot cert trust)")
        print(f"Adding to your login keychain, trusted for SSL to {printer.host} only.")
        add_to_keychain(cert, printer.host)
        store.mark_keychain(serial, printer.host)
        ok("added to Keychain")


# ---------------------------------------------------------------- argparse

def _add_slice_options(p):
    from . import machines
    from .slicing.profiles import SUPPORT_STYLES
    p.add_argument("--printer-type", choices=list(machines.MACHINES), default="replicator_plus",
                   help="printer to slice for (default: replicator_plus)")
    p.add_argument("--extruder", help="Replicator+: mk13 = Smart Extruder+, mk13_impla = "
                                      "Tough, mk13_experimental = Experimental "
                                      "(default: attached / mk13); Ender: stock")
    p.add_argument("--material", default="pla",
                   help="Replicator+: pla, im-pla (Tough PLA), petg; Ender: pla, petg, abs, tpu")
    p.add_argument("--quality", default="standard",
                   help="fine, optimal, standard, draft, superdraft (what the printer offers)")
    p.add_argument("--bed-temp", type=int, help="heated bed °C (Ender)")
    p.add_argument("--infill", type=int, default=15, help="infill percent")
    p.add_argument("--walls", type=int, default=2)
    p.add_argument("--supports", action="store_true")
    p.add_argument("--support-style", choices=list(SUPPORT_STYLES), default="organic")
    p.add_argument("--adhesion", choices=["none", "brim", "raft"], default="none")
    p.add_argument("--temp", type=int, help="override nozzle temperature (°C)")


def build_parser():
    ap = argparse.ArgumentParser(prog="openbot", description="MakerBot Replicator+ host")
    ap.add_argument("-p", "--printer", help="printer IP/hostname (default: last used)")
    ap.add_argument("-y", "--yes", action="store_true", help="answer yes to prompts")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="find printers on the network")
    d.add_argument("--seconds", type=float, default=3)
    d.add_argument("--no-sweep", action="store_true")
    d.set_defaults(func=cmd_discover)

    p = sub.add_parser("pair", help="pair with a printer (press its knob)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_pair)

    s = sub.add_parser("status")
    s.add_argument("--watch", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    sl = sub.add_parser("slice", help="slice one or more STL/OBJ files into one .makerbot "
                                      "(several files are arranged on the plate together)")
    sl.add_argument("models", nargs="+")
    sl.add_argument("-o", "--output")
    _add_slice_options(sl)
    sl.set_defaults(func=cmd_slice)

    pr = sub.add_parser("print", help="print a .makerbot, or slice and print an STL/OBJ")
    pr.add_argument("file")
    pr.add_argument("--force", action="store_true", help="ignore file/extruder checks")
    _add_slice_options(pr)
    pr.set_defaults(func=cmd_print)

    for name in ("cancel", "pause", "resume", "ack", "preheat", "cool"):
        sub.add_parser(name).set_defaults(func=cmd_simple)

    f = sub.add_parser("filament")
    f.add_argument("action", choices=["load", "unload", "stop"])
    f.set_defaults(func=cmd_filament)

    c = sub.add_parser("camera")
    c.add_argument("--snapshot", metavar="FILE")
    c.add_argument("--frames", type=int, default=30)
    c.add_argument("--dir", default="camera_frames")
    c.set_defaults(func=cmd_camera)

    w = sub.add_parser("wifi", help="printer Wi-Fi settings")
    wsub = w.add_subparsers(dest="action", required=True)
    wsub.add_parser("scan")
    wc = wsub.add_parser("connect")
    wc.add_argument("ssid")
    wc.add_argument("--password")
    wc.add_argument("--hidden", action="store_true")
    wsub.add_parser("forget").add_argument("ssid")
    for a in ("disconnect", "on", "off"):
        wsub.add_parser(a)
    w.set_defaults(func=cmd_wifi)

    i = sub.add_parser("ip", help="printer IP settings")
    isub = i.add_subparsers(dest="action", required=True)
    isub.add_parser("show")
    isub.add_parser("dhcp")
    st = isub.add_parser("static")
    st.add_argument("ip")
    st.add_argument("netmask")
    st.add_argument("gateway")
    st.add_argument("--dns", nargs="*")
    i.set_defaults(func=cmd_ip)

    r = sub.add_parser("rename")
    r.add_argument("name")
    r.set_defaults(func=cmd_rename)

    ce = sub.add_parser("cert", help="printer certificate trust")
    csub = ce.add_subparsers(dest="action", required=True)
    csub.add_parser("show")
    csub.add_parser("trust").add_argument("--replace", action="store_true")
    csub.add_parser("list")
    csub.add_parser("remove")
    csub.add_parser("export").add_argument("file")
    csub.add_parser("import").add_argument("file")
    csub.add_parser("keychain")
    ce.set_defaults(func=cmd_cert)

    from .server.service import add_commands
    add_commands(sub)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print()
    except PrinterError as e:
        fail(str(e))


if __name__ == "__main__":
    main()
