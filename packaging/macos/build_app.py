#!/usr/bin/env python3
"""Build OpenBot.app (and a .dmg) for macOS 12+.

    <python> packaging/macos/build_app.py [--arch universal2|arm64] [--no-dmg] [--no-orca]

Use a python.org Python (it's universal2) for a universal build that runs natively
on both Apple Silicon and Intel Macs; Homebrew Python can only build arm64.

Steps:
 1. make a clean build venv from that Python;
 2. install dependencies; for universal2, any compiled package that PyPI only
    ships per-architecture is downloaded for both arm64 and x86_64 and merged
    with delocate-merge;
 3. PyInstaller bundles the app; Info.plist gets the Local Network / Bonjour keys;
 4. OrcaSlicer.app (vendor/) is copied into Contents/Resources;
 5. ad-hoc code signing (a Developer ID signature + notarization is the
    release step: see PLAN.md Phase 4), then a compressed .dmg.
"""

import argparse
import glob
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.path.join(ROOT, "build", "macos")
DIST = os.path.join(ROOT, "dist")
sys.path.insert(0, ROOT)
from openbot import __version__  # noqa: E402

DEPS = ["PySide6-Essentials>=6.8,<6.9", "qasync>=0.27", "cryptography>=42",
        "zeroconf>=0.130", "keyring>=25", "pyserial>=3.5", "aiohttp>=3.9",
        "pyinstaller>=6.5", "delocate>=0.11"]
BUNDLE_ID = "org.openbot.OpenBot"
BUILD_ONLY = {"delocate", "pyinstaller", "PyInstaller", "macholib"}   # never bundled


def run(*cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def archs(path):
    out = subprocess.run(["lipo", "-archs", path], capture_output=True, text=True)
    return set(out.stdout.split()) if out.returncode == 0 else set()


def site_packages(venv):
    return glob.glob(os.path.join(venv, "lib", "python3.*", "site-packages"))[0]


def thin_packages(venv):
    """Installed distributions whose compiled files aren't universal2."""
    sp = site_packages(venv)
    bad = set()
    for f in glob.glob(os.path.join(sp, "**", "*.so"), recursive=True) + \
            glob.glob(os.path.join(sp, "**", "*.dylib"), recursive=True):
        if archs(f) and not {"arm64", "x86_64"} <= archs(f):
            top = os.path.relpath(f, sp).split(os.sep)[0]
            bad.add(top)
    # map top-level module dirs back to distribution names
    dists = {}
    for info in glob.glob(os.path.join(sp, "*.dist-info")):
        name = os.path.basename(info).split("-")[0]
        rec = os.path.join(info, "RECORD")
        if os.path.exists(rec):
            tops = {line.split("/")[0].split(",")[0] for line in open(rec)}
            for t in tops & bad:
                dists[t] = name
    found = set(dists.values()) | (bad - set(dists))
    return sorted(found - BUILD_ONLY)


def fuse_universal(venv, pkgs, work):
    py = os.path.join(venv, "bin", "python")
    pyver = subprocess.run([py, "-c", "import sys;print(f'{sys.version_info[0]}"
                            "{sys.version_info[1]}')"], capture_output=True, text=True,
                           check=True).stdout.strip()
    for name in pkgs:
        ver = subprocess.run([py, "-m", "pip", "show", name], capture_output=True,
                             text=True).stdout
        ver = next((l.split(":", 1)[1].strip() for l in ver.splitlines()
                    if l.startswith("Version:")), None)
        if not ver:
            continue
        # 1) a ready-made universal2 wheel for this version?
        d = os.path.join(work, "u2")
        os.makedirs(d, exist_ok=True)
        cmd = [py, "-m", "pip", "download", "-q", "--no-deps", "--only-binary=:all:",
               "--python-version", pyver, "--implementation", "cp", "--abi", f"cp{pyver}",
               "--abi", "abi3", "-d", d, f"{name}=={ver}"]
        for m in ("10_9", "10_12", "10_13", "10_15", "11_0", "12_0"):
            cmd += ["--platform", f"macosx_{m}_universal2"]
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            norm = name.replace("-", "_").lower()
            whl = [w for w in glob.glob(os.path.join(d, "*.whl"))
                   if os.path.basename(w).lower().startswith(f"{norm}-{ver}-")]
            if whl:
                run(py, "-m", "pip", "install", "-q", "--force-reinstall", "--no-deps", whl[0])
                continue
        # 2) otherwise merge the arm64 and x86_64 wheels
        got = {}
        for arch, plats in (("arm64", ["macosx_11_0_arm64"]),
                            ("x86_64", [f"macosx_10_{m}_x86_64" for m in (9, 12, 13, 15)]
                             + ["macosx_11_0_x86_64", "macosx_12_0_x86_64"])):
            d = os.path.join(work, arch)
            os.makedirs(d, exist_ok=True)
            cmd = [py, "-m", "pip", "download", "-q", "--no-deps", "--only-binary=:all:",
                   "--python-version", pyver, "--implementation", "cp",
                   "--abi", f"cp{pyver}", "--abi", "abi3", "-d", d, f"{name}=={ver}"]
            for p in plats:
                cmd += ["--platform", p]
            run(*cmd)
            norm = name.replace("-", "_").lower()
            whl = [w for w in glob.glob(os.path.join(d, "*.whl"))
                   if os.path.basename(w).lower().startswith(f"{norm}-{ver}-")]
            got[arch] = whl[0]
        out = os.path.join(work, "fused")
        run(os.path.join(venv, "bin", "delocate-merge"), got["arm64"], got["x86_64"],
            "-w", out)
        fused = [w for w in glob.glob(os.path.join(out, "*.whl"))
                 if os.path.basename(w).lower().startswith(name.replace("-", "_").lower())]
        run(py, "-m", "pip", "install", "-q", "--force-reinstall", "--no-deps", fused[0])


def make_icon(venv, dest):
    """A simple app icon drawn with Qt (nozzle over a build plate)."""
    script = r'''
import os, sys, subprocess, tempfile
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QColor, QLinearGradient, QPolygonF
from PySide6.QtCore import Qt, QRectF, QPointF
app = QGuiApplication([])
iconset = tempfile.mkdtemp(suffix=".iconset")
for size in (16, 32, 64, 128, 256, 512, 1024):
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 1024
    g = QLinearGradient(0, 0, 0, size)
    g.setColorAt(0, QColor(46, 125, 226)); g.setColorAt(1, QColor(20, 60, 140))
    p.setBrush(g); p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(90*s, 90*s, 844*s, 844*s), 190*s, 190*s)
    p.setBrush(QColor(255, 255, 255, 235))
    p.drawRect(QRectF(210*s, 700*s, 604*s, 70*s))               # plate
    p.setBrush(QColor(255, 150, 40))
    p.drawRect(QRectF(430*s, 560*s, 164*s, 140*s))              # printed part
    p.setBrush(QColor(255, 255, 255))
    p.drawRect(QRectF(390*s, 250*s, 244*s, 170*s))              # hotend
    p.drawPolygon(QPolygonF([QPointF(470*s, 420*s), QPointF(554*s, 420*s),
                             QPointF(512*s, 500*s)]))            # nozzle
    p.end()
    for scale, name in ((1, f"icon_{size}x{size}.png"), (2, f"icon_{size//2}x{size//2}@2x.png")):
        if scale == 1 and size <= 512 or scale == 2 and size >= 32:
            img.save(os.path.join(iconset, name))
subprocess.run(["iconutil", "-c", "icns", iconset, "-o", sys.argv[1]], check=True)
'''
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    run(os.path.join(venv, "bin", "python"), "-c", script, dest, env=env)


def build(args):
    venv = os.path.join(BUILD, f"venv-{args.arch}")
    if os.path.exists(venv) and args.clean:
        shutil.rmtree(venv)
    if not os.path.exists(venv):
        run(sys.executable, "-m", "venv", venv)
    pip = [os.path.join(venv, "bin", "python"), "-m", "pip"]
    run(*pip, "install", "-q", "--upgrade", "pip")
    run(*pip, "install", "-q", *DEPS)
    run(*pip, "install", "-q", "--no-deps", "--force-reinstall", ROOT)
    if args.arch == "universal2":
        if not {"arm64", "x86_64"} <= archs(os.path.realpath(sys.executable)):
            sys.exit("universal2 needs a universal Python (python.org installer), not "
                     f"{sys.executable}")
        # As of 2026 these no longer publish Intel Mac wheels for their newest releases:
        # cryptography 48.0.1 is the last universal2 release; zeroconf runs fine in its
        # pure-Python mode (no compiled extension, so nothing to be universal about).
        run(*pip, "install", "-q", "cryptography>=42,<49")
        zc = subprocess.run([*pip, "show", "zeroconf"], capture_output=True, text=True).stdout
        zc_ver = next(l.split(":", 1)[1].strip() for l in zc.splitlines()
                      if l.startswith("Version:"))
        run(*pip, "install", "-q", "--force-reinstall", "--no-deps", "--no-binary", "zeroconf",
            f"zeroconf=={zc_ver}", env=dict(os.environ, SKIP_CYTHON="1"))
        thin = thin_packages(venv)
        print("fusing to universal2:", ", ".join(thin) or "nothing needed")
        with tempfile.TemporaryDirectory() as work:
            fuse_universal(venv, thin, work)
        still = thin_packages(venv)
        if still:
            sys.exit(f"still not universal2: {still}")

    icon = os.path.join(BUILD, "OpenBot.icns")
    make_icon(venv, icon)
    entry = os.path.join(BUILD, "openbot_main.py")
    with open(entry, "w") as f:
        f.write("from openbot.app.main import main\nmain()\n")
    work = os.path.join(BUILD, "pyinstaller")
    run(os.path.join(venv, "bin", "pyinstaller"), "--noconfirm", "--clean", "--windowed",
        "--name", "OpenBot", "--icon", icon, "--target-arch", args.arch,
        "--osx-bundle-identifier", BUNDLE_ID,
        "--collect-data", "openbot.slicing", "--collect-submodules", "openbot",
        "--collect-submodules", "keyring.backends", "--collect-submodules", "zeroconf",
        "--hidden-import", "qasync", "--hidden-import", "serial.tools.list_ports",
        "--exclude-module", "tkinter", "--exclude-module", "PySide6.QtWebEngineCore",
        "--distpath", os.path.join(BUILD, "dist"), "--workpath", work, "--specpath", work,
        entry)
    app = os.path.join(BUILD, "dist", "OpenBot.app")

    plist_path = os.path.join(app, "Contents", "Info.plist")
    with open(plist_path, "rb") as f:
        plist = plistlib.load(f)
    plist.update({
        "CFBundleName": "OpenBot", "CFBundleDisplayName": "OpenBot",
        "CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSLocalNetworkUsageDescription": "OpenBot finds and talks to your 3D printers and "
                                          "OpenBot servers on the local network.",
        "NSBonjourServices": ["_makerbot-jsonrpc._tcp", "_openbot._tcp"],
        "NSHumanReadableCopyright": "OpenBot, GPL-3.0-or-later",
    })
    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    if not args.no_orca:
        orca = os.path.join(ROOT, "vendor", "OrcaSlicer.app")
        if not os.path.isdir(orca):
            sys.exit("vendor/OrcaSlicer.app missing (see PLAN.md Phase 2), or pass --no-orca")
        dest = os.path.join(app, "Contents", "Resources", "OrcaSlicer.app")
        if os.path.exists(dest):
            shutil.rmtree(dest)
        run("ditto", orca, dest)

    # Ad-hoc signature so macOS will launch it locally. Release builds re-sign with a
    # Developer ID and notarize.
    run("codesign", "--force", "--sign", "-", "--timestamp=none",
        os.path.join(app, "Contents", "MacOS", "OpenBot"))
    run("codesign", "--force", "--sign", "-", "--timestamp=none", app)

    os.makedirs(DIST, exist_ok=True)
    final = os.path.join(DIST, "OpenBot.app")
    if os.path.exists(final):
        shutil.rmtree(final)
    run("ditto", app, final)
    print("built", final)
    if not args.no_dmg:
        dmg = os.path.join(DIST, f"OpenBot-{__version__}-{args.arch}.dmg")
        if os.path.exists(dmg):
            os.remove(dmg)
        stage = tempfile.mkdtemp()
        run("ditto", final, os.path.join(stage, "OpenBot.app"))
        os.symlink("/Applications", os.path.join(stage, "Applications"))
        run("hdiutil", "create", "-volname", "OpenBot", "-srcfolder", stage, "-ov",
            "-format", "UDZO", dmg)
        shutil.rmtree(stage)
        print("built", dmg)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", choices=["universal2", "arm64", "x86_64"],
                    default="universal2")
    ap.add_argument("--no-dmg", action="store_true")
    ap.add_argument("--no-orca", action="store_true", help="skip bundling OrcaSlicer")
    ap.add_argument("--clean", action="store_true", help="recreate the build venv")
    build(ap.parse_args())
