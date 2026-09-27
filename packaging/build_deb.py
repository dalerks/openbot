#!/usr/bin/env python3
"""Build openbot-server_<version>_all.deb for Raspberry Pi OS / Debian (no dpkg needed).

    python3 packaging/build_deb.py            ->  dist/openbot-server_<ver>_all.deb

The package ships OpenBot as a wheel; its postinst makes a private venv in
/opt/openbot and installs the Python dependencies from PyPI there.
"""

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINUX = os.path.join(ROOT, "packaging", "linux")
sys.path.insert(0, ROOT)
from openbot import __version__  # noqa: E402

CONTROL = f"""Package: openbot-server
Version: {__version__}
Architecture: all
Maintainer: OpenBot <openbot@localhost>
Depends: python3 (>= 3.11), python3-venv, adduser
Section: misc
Priority: optional
Homepage: https://example.invalid/openbot
Description: OpenBot printer server
 Shares a MakerBot Replicator+ (network) or Creality Ender-3 (USB) with
 OpenBot apps on the local network: print queue, live camera, pairing with
 roles. Runs as the "openbot" systemd service; configure it in
 /etc/openbot/server.toml.
"""


def _add(tar, arcname, data=None, path=None, mode=0o644):
    info = tarfile.TarInfo(arcname)
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = int(time.time())
    if data is None and path is None:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        return
    raw = data if data is not None else open(path, "rb").read()
    info.size = len(raw)
    info.mode = mode
    tar.addfile(info, io.BytesIO(raw))


def _tar(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        dirs = set()
        for name, kw in entries:
            parts = name.strip("./").split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "./" + "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    _add(tar, d)
            _add(tar, name, **kw)
    return buf.getvalue()


def _ar(members):
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data in members:
        header = (f"{name:<16}{int(time.time()):<12}{0:<6}{0:<6}{0o100644:<8o}"
                  f"{len(data):<10}`\n").encode()
        assert len(header) == 60
        out.write(header)
        out.write(data)
        if len(data) % 2:
            out.write(b"\n")
    return out.getvalue()


def build():
    wheel_dir = tempfile.mkdtemp(prefix="openbot-wheel-")
    subprocess.run([sys.executable, "-m", "pip", "wheel", ROOT, "--no-deps", "--quiet",
                    "-w", wheel_dir], check=True)
    wheel = [f for f in os.listdir(wheel_dir) if f.endswith(".whl")][0]
    data = _tar([
        (f"./opt/openbot/wheels/{wheel}", {"path": os.path.join(wheel_dir, wheel)}),
        ("./lib/systemd/system/openbot.service",
         {"path": os.path.join(LINUX, "openbot.service")}),
        ("./etc/openbot/server.toml", {"path": os.path.join(LINUX, "server.toml")}),
        ("./usr/bin/openbot-server",
         {"path": os.path.join(LINUX, "openbot-server"), "mode": 0o755}),
    ])
    control = _tar([
        ("./control", {"data": CONTROL.encode()}),
        ("./conffiles", {"data": b"/etc/openbot/server.toml\n"}),
        ("./postinst", {"path": os.path.join(LINUX, "postinst"), "mode": 0o755}),
        ("./prerm", {"path": os.path.join(LINUX, "prerm"), "mode": 0o755}),
        ("./postrm", {"path": os.path.join(LINUX, "postrm"), "mode": 0o755}),
    ])
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", f"openbot-server_{__version__}_all.deb")
    with open(out, "wb") as f:
        f.write(_ar([("debian-binary", b"2.0\n"), ("control.tar.gz", control),
                     ("data.tar.gz", data)]))
    print(out)
    return out


if __name__ == "__main__":
    build()
