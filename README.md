<p align="center">
  <img src="openbot/server/web/favicon.svg" width="96" alt="OpenBot icon">
</p>

<h1 align="center">OpenBot</h1>

<p align="center">
  Free, open host software for the <b>MakerBot Replicator+</b> and <b>Creality Ender-3</b> printers.<br>
  A <a href="https://www.lighthouseconsulting.com">Lighthouse Consulting</a> project.
</p>

<p align="center">
  <a href="#support-openbot"><img alt="Support OpenBot: suggested $20" src="https://img.shields.io/badge/♥_Support_OpenBot-suggested_$20-d63864?style=for-the-badge"></a>
  <a href="LICENSE"><img alt="License: GPL-3.0-or-later" src="https://img.shields.io/badge/license-GPL--3.0--or--later-2e7de2?style=for-the-badge"></a>
</p>

---

MakerBot has ended support for MakerBot Print and MakerBot Desktop on the Replicator+.
OpenBot replaces them. It slices your models, sends them to the printer over the network,
shows the printer's camera, and changes its Wi-Fi settings. It also drives Creality Ender-3
printers over USB or by SD card, and can share any of these printers with every Mac in the
house, or run on a Raspberry Pi next to the printer.

## What it does

| | MakerBot Replicator+ | Creality Ender-3 family |
|---|---|---|
| Slicing | STL/OBJ → `.makerbot`, with the bundled OrcaSlicer engine | STL/OBJ → G-code, using Creality's own OrcaSlicer profiles |
| Build plate | Several objects: move (drag in 3D), rotate, scale, duplicate, auto-arrange | same |
| Connection | Network (encrypted, pinned certificate, one-time knob-press pairing) | USB (Marlin, checksummed lines with automatic resend) |
| Printing | Upload & print, pause/resume/cancel | Print over USB, or save to an SD card (FAT32 check, safe names, eject) |
| Camera | Live view, snapshots, photo of each finished print | – |
| Printer settings | Wi-Fi scan/join/forget, static IP/DHCP, rename | G-code console, preheat, home, emergency stop |
| Safety | Build-volume and temperature checks on every file | PTFE-safe temperature limits for the stock hotend (240 °C) |

**Sharing (server mode):**
- **Who can print:** one Mac, or a Raspberry Pi, shares a printer with other Macs on the network. Devices join with a 6-digit pairing code and get a role: **Viewer**, **Operator** or **Admin**.
- **Shared queue:** jobs start only after someone confirms the build plate is clear.
- **Web page:** every server also has one at `https://<server>:8765/`, so phones and tablets can watch the camera, manage the queue and approve devices.

## Download and install

### Mac (macOS 12 or later, Apple Silicon and Intel)

Download `OpenBot-<version>-universal2.dmg` from [Releases](https://github.com/dalerks/openbot/releases),
open it and drag **OpenBot** to Applications.

### Raspberry Pi server (Raspberry Pi OS 64-bit, Bookworm or later)

```sh
sudo apt install ./openbot-server_<version>_all.deb
sudo nano /etc/openbot/server.toml        # choose your printer
sudo systemctl start openbot
journalctl -u openbot | grep "setup code" # enter this in OpenBot on your Mac to become Admin
```

Then open `https://<pi-name>.local:8765/` in a browser, or use **Printers → OpenBot servers** in the Mac app.

Manage devices from the Pi's shell with:

```sh
sudo openbot-server server pending
sudo openbot-server server approve CODE
sudo openbot-server server clients
```

## First steps

1. **Replicator+:** Printers → Find Printers → Connect. Trust the printer's certificate, then press the knob on the printer once to pair.
2. **Ender-3:** plug in USB, then Printers → USB printers → Refresh → Connect USB. Or slice and use **Save to SD Card…**.
3. **Prepare:** Add Models → pick printer, material and quality → Slice → Print.

## Building from source

```sh
git clone https://github.com/dalerks/openbot.git && cd openbot
python3.12 -m venv .venv && .venv/bin/pip install -e ".[app,dev]"
.venv/bin/python -m pytest          # 95+ tests; slicing tests need OrcaSlicer (below)
.venv/bin/openbot-app               # the desktop app
.venv/bin/openbot --help            # command line: discover, pair, slice, print, wifi, serve…
```

**Slicer engine:** download [OrcaSlicer 2.4.2](https://github.com/OrcaSlicer/OrcaSlicer/releases) for macOS and place
`OrcaSlicer.app` in `vendor/`, or set `OPENBOT_ORCASLICER` to its executable.

**Packages:**

| Package | Command | Notes |
|---|---|---|
| Mac app + DMG | `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 packaging/macos/build_app.py` | Needs the python.org universal2 Python. Release builds are then signed with a Developer ID and notarized. |
| Raspberry Pi / Debian package | `python3 packaging/build_deb.py` | |

[PLAN.md](PLAN.md) holds the full design: protocol reverse-engineering notes, security model, test results.

## Support OpenBot

OpenBot is free. If it saved your printer from the landfill, please consider supporting its
development: **suggested $20**.

<!-- Replace with the Stripe Payment Link once it exists (also set DONATE_URL in openbot/project.py). -->
**[♥ Donate to OpenBot](https://www.lighthouseconsulting.com)**

## License

Copyright © 2026 Lighthouse Consulting.

OpenBot is free software: you can redistribute it and/or modify it under the terms of the
**GNU General Public License** as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version. See [LICENSE](LICENSE) and
[NOTICE](NOTICE) for third-party components.

OpenBot is not affiliated with or endorsed by MakerBot, UltiMaker, Stratasys or Creality.
"MakerBot", "Replicator" and "Ender" are trademarks of their owners and are used only to say
which printers OpenBot works with.
