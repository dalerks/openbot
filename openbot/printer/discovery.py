"""Find Replicator+ printers on the local network.

Phase 0 findings drive the order here:
* UDP broadcast to :12307 returns full info including `ip`, but gets no reply
  while MakerBot Print's `conveyor-svc` is running (it takes the replies).
* Bonjour (`_makerbot-jsonrpc._tcp`) always showed the printer, but its TXT
  record has `ip=None` and `<name>.local` does not resolve.
* The last 12 hex digits of `iserial` are the MAC address, so the ARP table
  maps a Bonjour-only result to an IP.
* Last resort: a handshake sweep of the local /24 on :9999.
"""

import asyncio
import ipaddress
import json
import re
import socket
import subprocess
import sys

from .models import PrinterInfo, mac_from_serial
from .rpc import RpcConnection

DISCOVERY_PORT = 12307
DISCOVERY_SRC_PORT = 12309
BONJOUR_TYPE = "_makerbot-jsonrpc._tcp.local."


# ---------------------------------------------------------------- UDP

class _UdpCollector(asyncio.DatagramProtocol):
    def __init__(self):
        self.found = {}

    def datagram_received(self, data, addr):
        try:
            info = json.loads(data)
        except ValueError:
            return
        if isinstance(info, dict) and "machine_type" in info:
            p = PrinterInfo.from_handshake(info, ip=addr[0], source="udp")
            self.found[p.serial or addr[0]] = p


async def udp_discover(seconds=3.0):
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", DISCOVERY_SRC_PORT))
    except OSError:
        sock.close()
        return []  # port held (usually by MakerBot Print's conveyor-svc)
    transport, proto = await loop.create_datagram_endpoint(_UdpCollector, sock=sock)
    try:
        req = json.dumps({"command": "broadcast"}).encode()
        end = loop.time() + seconds
        while loop.time() < end:
            transport.sendto(req, ("255.255.255.255", DISCOVERY_PORT))
            await asyncio.sleep(min(1.0, max(end - loop.time(), 0)))
    finally:
        transport.close()
    return list(proto.found.values())


# ---------------------------------------------------------------- Bonjour

async def bonjour_discover(seconds=3.0):
    """Browse _makerbot-jsonrpc._tcp and parse TXT records.

    macOS: go through the system mDNSResponder (`dns-sd`). A process's own
    multicast socket can be silently blocked by Local Network privacy or a
    sandbox; the system daemon is what Apple intends apps to use.
    Elsewhere (Raspberry Pi): python-zeroconf.
    """
    if sys.platform == "darwin":
        return await _dnssd_discover(seconds)
    return await _zeroconf_discover(seconds)


async def _run_for(args, seconds):
    """Run a long-lived command for `seconds` and return what it printed."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), seconds)
    except asyncio.TimeoutError:
        proc.terminate()
    out = await proc.stdout.read()
    try:
        await asyncio.wait_for(proc.wait(), 2)
    except asyncio.TimeoutError:
        proc.kill()
    return out.decode(errors="replace")


_TXT_PAIR = re.compile(r"((?:[^\s\\]|\\.)+)=((?:[^\s\\]|\\.)*)")


def parse_dnssd_txt(line):
    """Parse a `dns-sd -L` TXT line: key=value pairs, spaces escaped as '\\ '."""
    unescape = lambda s: re.sub(r"\\(.)", r"\1", s)  # noqa: E731
    return {unescape(k): unescape(v) for k, v in _TXT_PAIR.findall(line)}


async def _dnssd_discover(seconds):
    browse = await _run_for(["dns-sd", "-B", BONJOUR_TYPE[:-7], "local."], seconds)
    names = []
    for line in browse.splitlines():
        if " Add " in line and "_makerbot-jsonrpc._tcp." in line:
            name = line.split("_makerbot-jsonrpc._tcp.")[-1].strip()
            if name and name not in names:
                names.append(name)
    found = []
    for name in names:
        out = await _run_for(["dns-sd", "-L", name, BONJOUR_TYPE[:-7], "local."], 2.5)
        lines = out.splitlines()
        for i, line in enumerate(lines):
            if "can be reached at" in line and i + 1 < len(lines):
                txt = parse_dnssd_txt(lines[i + 1])
                info = PrinterInfo.from_handshake(txt, source="bonjour")
                info.name = info.name or name
                found.append(info)
                break
    return found


async def _zeroconf_discover(seconds):
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    names = set()

    def handler(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            names.add(name)

    azc = AsyncZeroconf()
    browser = AsyncServiceBrowser(azc.zeroconf, [BONJOUR_TYPE], handlers=[handler])
    try:
        await asyncio.sleep(seconds)
        found = []
        for name in names:
            si = AsyncServiceInfo(BONJOUR_TYPE, name)
            await si.async_request(azc.zeroconf, 3000)
            txt = {k.decode(): (v.decode() if v is not None else None)
                   for k, v in (si.properties or {}).items()}
            addrs = si.parsed_addresses() if si else []
            info = PrinterInfo.from_handshake(txt, ip=addrs[0] if addrs else None,
                                              source="bonjour")
            if not info.name:
                info.name = name.split(".")[0]
            found.append(info)
        return found
    finally:
        await browser.async_cancel()
        await azc.async_close()


# ---------------------------------------------------------------- ARP / sweep

_ARP_LINE = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f:]+)", re.I)


def _normalize_mac(mac):
    return ":".join(part.zfill(2) for part in mac.lower().split(":"))


def arp_table():
    try:
        out = subprocess.run(["arp", "-an"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table = {}
    for ip, mac in _ARP_LINE.findall(out):
        if mac != "ff:ff:ff:ff:ff:ff":
            table[_normalize_mac(mac)] = ip
    return table


def ip_for_serial(iserial, table=None):
    mac = mac_from_serial(iserial)
    if not mac:
        return None
    return (table if table is not None else arp_table()).get(mac)


def local_ipv4_networks():
    """Best-effort list of this machine's /24 networks."""
    nets = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent
        nets.add(ipaddress.ip_network(f"{s.getsockname()[0]}/24", strict=False))
        s.close()
    except OSError:
        pass
    return sorted(nets)


async def probe(ip, timeout=1.5):
    """Handshake one address; returns PrinterInfo or None."""
    try:
        conn = await asyncio.wait_for(RpcConnection.open(ip, 9999, timeout=timeout), timeout)
    except Exception:  # noqa: BLE001
        return None
    try:
        result = await conn.call("handshake", timeout=timeout)
        return PrinterInfo.from_handshake(result, ip=ip, source="sweep")
    except Exception:  # noqa: BLE001
        return None
    finally:
        await conn.close()


async def sweep(networks=None, concurrency=64):
    networks = networks or local_ipv4_networks()
    sem = asyncio.Semaphore(concurrency)

    async def one(ip):
        async with sem:
            return await probe(str(ip))

    hosts = [ip for net in networks for ip in net.hosts()]
    results = await asyncio.gather(*(one(ip) for ip in hosts))
    return [r for r in results if r]


# ---------------------------------------------------------------- combined

async def discover(seconds=3.0, allow_sweep=True):
    """UDP + Bonjour in parallel, ARP to fill in missing IPs, sweep if still unresolved."""
    udp, bonjour = await asyncio.gather(udp_discover(seconds), _safe_bonjour(seconds))
    found = {p.serial: p for p in udp if p.serial}
    table = None
    for p in bonjour:
        if p.serial in found:
            continue
        if not p.ip:
            table = table if table is not None else arp_table()
            p.ip = ip_for_serial(p.serial, table)
            if p.ip:
                p.source = "bonjour+arp"
        found[p.serial or p.name] = p
    unresolved = [p for p in found.values() if not p.ip]
    if allow_sweep and (not found or unresolved):
        for p in await sweep():
            if p.serial in found and found[p.serial].ip:
                continue
            found[p.serial] = p
    # Confirm each IP with a real handshake so the info is complete and current.
    confirmed = await asyncio.gather(*(probe(p.ip) if p.ip else _none() for p in found.values()))
    out = []
    for p, c in zip(found.values(), confirmed):
        if c:
            c.source = p.source
            out.append(c)
        else:
            out.append(p)
    return sorted(out, key=lambda p: p.name)


async def _safe_bonjour(seconds):
    try:
        return await bonjour_discover(seconds)
    except Exception:  # noqa: BLE001 - zeroconf can fail on odd interface setups
        return []


async def _none():
    return None


def conveyor_running():
    """True if MakerBot's background service is running (it blocks UDP discovery)."""
    try:
        out = subprocess.run(["pgrep", "-f", "conveyor-svc"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(out.strip())
