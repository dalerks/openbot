"""Find and run the bundled OrcaSlicer CLI."""

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class SlicerError(RuntimeError):
    pass


def _candidates():
    if env := os.environ.get("OPENBOT_ORCASLICER"):
        yield env
    if sys.platform == "darwin":
        # Inside a packaged OpenBot.app: Contents/Resources/OrcaSlicer.app
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        yield os.path.join(exe_dir, "..", "Resources", "OrcaSlicer.app",
                           "Contents", "MacOS", "OrcaSlicer")
        yield os.path.join(REPO_ROOT, "vendor", "OrcaSlicer.app", "Contents", "MacOS",
                           "OrcaSlicer")
        yield "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
    else:
        yield os.path.join(REPO_ROOT, "vendor", "squashfs-root", "AppRun")
        yield "/opt/openbot/orca/AppRun"


def find_orcaslicer():
    for path in _candidates():
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return os.path.normpath(path)
    raise SlicerError("OrcaSlicer not found. Put OrcaSlicer.app in vendor/ or set "
                      "OPENBOT_ORCASLICER to its executable.")


def slice_project(project_3mf, out_dir, timeout=900):
    """Slice a 3MF project; returns the path of the produced G-code."""
    exe = find_orcaslicer()
    os.makedirs(out_dir, exist_ok=True)
    cmd = [exe, "--outputdir", out_dir, "--arrange", "0", "--orient", "0",
           "--slice", "0", project_3mf]
    try:
        # cwd=out_dir: OrcaSlicer writes error logs (e.g. 00000.log) into its working
        # directory; keep them in our temp folder, not wherever the app was started.
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout, text=True,
                              errors="replace", cwd=out_dir)
    except subprocess.TimeoutExpired:
        raise SlicerError(f"slicing took longer than {timeout // 60} minutes") from None
    gcode = os.path.join(out_dir, "plate_1.gcode")
    if proc.returncode != 0 or not os.path.exists(gcode):
        tail = "\n".join(proc.stdout.strip().splitlines()[-15:])
        raise SlicerError(f"OrcaSlicer failed (exit {proc.returncode}):\n{tail}")
    return gcode
