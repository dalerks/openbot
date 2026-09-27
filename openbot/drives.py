"""Removable drives (SD cards, USB sticks): list, save print files, eject.

Ender-3 printers read G-code from an SD card; the Replicator+ reads .makerbot
files from a USB stick. Marlin's SD support needs FAT16/FAT32 (not exFAT) and
copes best with short, plain filenames.
"""

import os
import plistlib
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass


class DriveError(RuntimeError):
    pass


@dataclass
class Drive:
    mount: str
    name: str
    filesystem: str
    total_bytes: int
    free_bytes: int

    @property
    def fat(self):
        return "fat" in self.filesystem.lower() or "ms-dos" in self.filesystem.lower()

    @property
    def exfat(self):
        return "exfat" in self.filesystem.lower()

    def warnings_for(self, output):
        w = []
        if output == "gcode" and self.exfat:
            w.append(f"{self.name} is formatted exFAT; most Ender-3 firmware only reads "
                     "FAT32. Reformat it as MS-DOS (FAT32) in Disk Utility if the printer "
                     "doesn't see the file.")
        return w

    def label(self):
        gb = self.total_bytes / 1e9
        return f"{self.name} ({gb:.1f} GB {self.filesystem}, {self.free_bytes / 1e9:.1f} GB free)"


def _diskutil_info(target):
    out = subprocess.run(["diskutil", "info", "-plist", target], capture_output=True,
                         timeout=10)
    if out.returncode != 0:
        return None
    return plistlib.loads(out.stdout)


def list_removable():
    """Mounted removable/external physical drives."""
    if sys.platform == "darwin":
        return _list_macos()
    return _list_linux()


def _list_macos():
    drives = []
    for entry in sorted(os.listdir("/Volumes")):
        mount = os.path.join("/Volumes", entry)
        if os.path.islink(mount) or not os.path.ismount(mount):
            continue
        info = _diskutil_info(mount)
        if not info:
            continue
        if info.get("VirtualOrPhysical") == "Virtual":      # disk images etc.
            continue
        removable = (info.get("RemovableMedia") or info.get("Ejectable")
                     or info.get("Removable") or not info.get("Internal", True))
        if not removable:
            continue
        st = shutil.disk_usage(mount)
        drives.append(Drive(mount=mount, name=info.get("VolumeName") or entry,
                            filesystem=info.get("FilesystemName") or
                            info.get("FilesystemType", "?"),
                            total_bytes=st.total, free_bytes=st.free))
    return drives


def _list_linux():
    drives = []
    user = os.environ.get("USER", "")
    for base in (f"/media/{user}", "/media", f"/run/media/{user}"):
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            mount = os.path.join(base, entry)
            if not os.path.ismount(mount):
                continue
            fs = "?"
            try:
                with open("/proc/mounts") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) > 2 and parts[1] == mount:
                            fs = parts[2]
            except OSError:
                pass
            st = shutil.disk_usage(mount)
            drives.append(Drive(mount, entry, fs, st.total, st.free))
    return drives


def safe_filename(name, extension, max_stem=24):
    """Plain ASCII, no spaces: what printer SD menus handle reliably."""
    stem = os.path.splitext(os.path.basename(name))[0]
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_-") or "print"
    return stem[:max_stem] + extension


def save_to_drive(src, drive, filename=None):
    """Copy a print file onto the drive (atomically), flush it, and return the path."""
    filename = filename or os.path.basename(src)
    size = os.path.getsize(src)
    if size > drive.free_bytes:
        raise DriveError(f"not enough space on {drive.name}")
    dest = os.path.join(drive.mount, filename)
    tmp = dest + ".tmp"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
    except OSError as e:
        raise DriveError(f"couldn't write to {drive.name}: {e}") from e
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    os.sync()
    return dest


def eject(drive):
    cmd = (["diskutil", "eject", drive.mount] if sys.platform == "darwin"
           else ["udisksctl", "unmount", "-b", drive.mount])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise DriveError(f"couldn't eject {drive.name}: {out.stderr.strip() or out.stdout}")
