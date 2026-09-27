"""Announce OpenBot servers on Bonjour (_openbot._tcp) and find them.

macOS goes through the system mDNSResponder (`dns-sd`), which works even where
an app's own multicast socket is blocked (Phase 0 finding); Linux uses zeroconf.
"""

import asyncio
import contextlib
import socket
import subprocess
import sys

from ..printer.discovery import _run_for, parse_dnssd_txt

SERVICE = "_openbot._tcp"


class Advertiser:
    def __init__(self, name, port, txt):
        self.name, self.port, self.txt = name, port, txt
        self._proc = None
        self._zc = None

    async def start(self):
        if sys.platform == "darwin":
            args = ["dns-sd", "-R", f"OpenBot {self.name}", SERVICE, "local", str(self.port)]
            args += [f"{k}={v}" for k, v in self.txt.items()]
            self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
            return
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
        self._zc = AsyncZeroconf()
        host = socket.gethostname().split(".")[0]
        addr = socket.inet_aton(_local_ip())
        info = ServiceInfo(f"{SERVICE}.local.", f"OpenBot {self.name}.{SERVICE}.local.",
                           addresses=[addr], port=self.port,
                           properties={k: str(v) for k, v in self.txt.items()},
                           server=f"{host}.local.")
        await self._zc.async_register_service(info)

    async def stop(self):
        if self._proc:
            self._proc.terminate()
        if self._zc:
            with contextlib.suppress(Exception):
                await self._zc.async_unregister_all_services()
                await self._zc.async_close()


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def browse(seconds=3.0):
    """[{name, host, port, server_id, fingerprint}] for OpenBot servers on the network."""
    if sys.platform != "darwin":
        return await _browse_zeroconf(seconds)
    out = await _run_for(["dns-sd", "-B", SERVICE, "local."], seconds)
    names = []
    for line in out.splitlines():
        if " Add " in line and f"{SERVICE}." in line:
            name = line.split(f"{SERVICE}.")[-1].strip()
            if name not in names:
                names.append(name)
    found = []
    for name in names:
        detail = await _run_for(["dns-sd", "-L", name, SERVICE, "local."], 2.5)
        lines = detail.splitlines()
        for i, line in enumerate(lines):
            if "can be reached at" in line:
                target = line.split("can be reached at", 1)[1].split("(")[0].strip()
                host, _, port = target.rpartition(":")
                txt = parse_dnssd_txt(lines[i + 1]) if i + 1 < len(lines) else {}
                found.append({"name": name, "host": host.rstrip("."), "port": int(port),
                              "server_id": txt.get("id", ""),
                              "fingerprint": txt.get("fp", "")})
                break
    return found


async def _browse_zeroconf(seconds):
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    names = set()

    def handler(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            names.add(name)
    azc = AsyncZeroconf()
    browser = AsyncServiceBrowser(azc.zeroconf, [f"{SERVICE}.local."], handlers=[handler])
    try:
        await asyncio.sleep(seconds)
        found = []
        for name in names:
            si = AsyncServiceInfo(f"{SERVICE}.local.", name)
            await si.async_request(azc.zeroconf, 3000)
            txt = {k.decode(): (v or b"").decode() for k, v in (si.properties or {}).items()}
            addrs = si.parsed_addresses()
            found.append({"name": name.split(".")[0], "host": addrs[0] if addrs else "",
                          "port": si.port, "server_id": txt.get("id", ""),
                          "fingerprint": txt.get("fp", "")})
        return found
    finally:
        await browser.async_cancel()
        await azc.async_close()
