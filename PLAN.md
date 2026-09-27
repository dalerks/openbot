# OpenBot — a MakerBot Replicator+ Replacement Host

A **macOS** desktop app (GPL-3.0) for **Apple Silicon and Intel Macs** that
replaces MakerBot Desktop / MakerBot Print for the **MakerBot Replicator+**
(5th-gen "Birdwing" platform). It covers:

- Slicing STL/3MF/OBJ into `.makerbot` print files, with the extruder chosen per print
- Network printing
- Live monitoring
- **Camera**
- **Wi-Fi / network configuration**
- Filament and maintenance actions
- **Server/client mode:** one instance shares the printer, and other Macs connect to it
- **Headless Raspberry Pi server**

It needs no MakerBot account, no cloud services, and **no firmware
modification**. Everything uses the printer's stock, still-shipping API.

---

## 1. Research findings (reverse engineering)

### 1.1 Sources

| Source | What it gave us |
|---|---|
| `charely6/Makerbot-5gen-plus` — extracted Replicator **Mini+** firmware 2.6.3 (`Release_Birdwing`, build 736) | **Primary source.** It includes kaiten, the printer's Python 3.4 control daemon, as plain `.py` source, and `usr/include/kaitenstub.hh`, an auto-generated stub listing every RPC method and its parameters. |
| `garfield-arlene/queue3d` | Replicator+-specific protocol work, **tested on real hardware**: printing, camera, token behavior, and an OrcaSlicer → `.makerbot` pipeline. |
| `tjhorner/makerbot-rpc` (Go), `tjhorner/node-makerbot-rpc`, `abdyfranco/makerbot-php`, `charely6/makerbot-gen5-api` (Python) | Cross-checks for discovery, HTTP `/auth`, file transfer, and camera framing. |
| `charely6/mbotmake` | G-code → `.makerbot` converter. `bot_type` for the Replicator+ is `replicator_b`. |
| makerbot-users "5th Gen Custom Firmware" thread | Firmware package layout: zip of `rootfs` (UBIFS), `uImage`, `manifest.json`, `hash`, `signature`. |

> The Mini+ and Replicator+ run the same Birdwing/kaiten codebase; `machine_type`
> and hardware config differ. Phase 0 checks every method below against the
> actual Replicator+ before we build on it.

### 1.2 Printer software stack
- Buildroot Linux, **kaiten** (Python, `/usr/lib/python3.x/site-packages/kaiten`)
- **connman** for networking (driven over D-Bus); the init script is `/etc/init.d/S45connman`
- An LCD UI process that talks to kaiten as the `lcd` client. This is what shows "press the knob to authorize".

### 1.3 Network ports

| Port | Proto | Purpose |
|---|---|---|
| 12307 | UDP | Discovery. Send `{"command":"broadcast"}` from source port 12309; the printer replies with JSON (`machine_name`, `ip`, `iserial`, `machine_type`, `firmware_version`, …). |
| mDNS | UDP | `_makerbot-jsonrpc._tcp`, an alternative discovery method |
| 80 | HTTP | `/auth` pairing endpoint (legacy); `GET /settings/frame.png` serves a still image written by `capture_image` |
| **9999** | TCP | JSON-RPC 2.0, **plaintext, "unsecure" channel** |
| **12309** | **TCP+TLS** | JSON-RPC 2.0, **"secure" channel**. Self-signed server cert (`/var/ssl/server.crt`), **no client certificate required**. |

JSON-RPC framing: bare JSON objects back-to-back with no delimiter, so the
reader must count braces. Some notifications (`camera_frame`) are followed by
**raw binary** on the same socket.

### 1.4 Authentication (key finding)

kaiten has privilege levels. Connections start at priv 1; `authenticate`,
`reauthorize`, and `authorize` raise them to priv 2. Methods marked
`require_secure` **only work on the TLS port 12309**, or on the local pipe
at priv 3. The secure-only methods are:

`authorize`, `reauthorize`, `wifi_connect`, `wifi_fre_authorize`,
`add_local_auth`, `add_makerbot_account`, `set_thingiverse_credentials`, `fcgi_reauthorize`

**Why older tools struggled:** the token that `authenticate` accepts on port 9999
is a **single-use** `one_time_token`; kaiten `pop()`s it. queue3d observed this
("pairing tokens only good for one session"). The durable credential is
something else.

**Our flow, account-free and persistent:**
1. **First pairing:** connect TLS to `:12309` and call
   `authorize(username="OpenBot@<hostname>", local_secret=<32 random bytes, hex>)`.
   The printer prompts on the LCD, the user presses the knob, and the call
   returns `{one_time_token, local_code}`.
   **Store `local_secret` and `local_code` in the OS keychain.**
2. **Every later session:** connect TLS to `:12309` and call
   `reauthorize(username, local_secret=…, local_code=…)`. That authenticates the
   TLS connection itself to priv 2, with no knob press.
   - **Run all traffic on this TLS connection.** It has full access plus the
     secure-only methods.
   - Fallback: pass the returned `one_time_token` to `authenticate` on `:9999`
     if TLS turns out slow for large uploads.
3. **Unpairing:** `deauthorize(username)`; `get_authorized` lists paired clients.

**TLS (verified in Phase 0):** the printer negotiates **TLS 1.2 /
ECDHE-RSA-AES256-GCM-SHA384**, so no legacy cipher settings are needed. Its
certificate is self-signed, so it can't pass normal verification. How we
trust it is covered in §3.3.

### 1.5 Wi-Fi / network API (from kaiten `dbus.py`)

| Method | Params | Returns / notes |
|---|---|---|
| `network_state` | – | `{state: offline\|wifi\|ethernet, ip, netmask, gateway, dns[], static, name}` |
| `wifi_scan` | `force_rescan?: bool` | `[{path, name, strength 0–100, password: none\|stored\|required}]`; `path` is the connman service path |
| `wifi_connect` 🔒 | `path, password?, name?` (name for hidden SSIDs) | Result in `network_state` format. **TLS only.** Error 56 while tethered; error 50 if the AP disappeared. |
| `wifi_disconnect` | `path?` | |
| `wifi_forget` | `path?` | Forget a saved AP |
| `wifi_enable` / `wifi_disable` | – | Radio on/off |
| `wifi_reset` | – | Wipe connman state and restart it |
| `wifi_signal_strength` | `ssid, iface` | |
| `get_static_ipv4` | `service_path` (or `"ethernet"`) | `{use_static, ip?, netmask?, gateway?, dns?}` |
| `set_static_ipv4` | `service_path, ip?, netmask?, gateway?, dns?[], use_static=true` | `use_static=false` switches back to DHCP. Error 78 on bad input. |
| `change_machine_name` | `machine_name` | |

**Operational caveat:** changing Wi-Fi *over* Wi-Fi drops the session. The UI must:
1. Warn the user, and recommend Ethernet while reconfiguring.
2. Fire `wifi_connect` and expect the socket to close.
3. Rediscover the printer by `iserial` via UDP/mDNS on the new network.
4. Re-run `reauthorize`.

### 1.6 Camera (verified on a real Replicator+ by queue3d)
- `request_camera_stream` → the printer pushes repeated `{"method":"camera_frame"}` notifications.
  Each one is followed immediately by a **16-byte big-endian header**:
  `[u32 total_len_incl_header][u32 width][u32 height][u32 format]`,
  where format 1 = YUYV and 2 = JPEG. The frame bytes come next.
- The Replicator+ camera ("NMG HD WebCam") is **640×480 JPEG**. We should still support YUYV (the 320×240 cam on older units) by converting to RGB.
- `end_camera_stream` stops it. Frames can keep arriving for a moment after this, so keep reading raw frames until ~3 s of silence (queue3d learned this the hard way).
- `request_camera_frame` returned "method not found" on queue3d's printer, but **works on ours (fw 2.6.2)**. Use it for single snapshots when available, and fall back to a brief stream otherwise.
- Fallback still image: `capture_image(output_file="/home/settings/frame.png")`, then `GET http://<ip>/settings/frame.png`.
- Use a **dedicated second connection** for the camera so binary frames never interleave with control traffic.

### 1.7 Printing and control

- **Upload + print:**
  1. `print(filepath="<name>.makerbot", transfer_wait=true)`
  2. `process_method("build_plate_cleared")`
  3. `put_init(file_id, filepath, length, block_size)`
  4. Repeated `put_raw(file_id, length)`: a JSON header followed immediately by that many raw bytes. queue3d uses 50 KB blocks.
  5. `put_term(file_id, length, crc=<CRC32 of whole file>)`
- **Job control:** `cancel`; `process_method("suspend"|"resume")`; `print_again`; `acknowledged`
- **Status:** `get_system_information`, plus `state_notification` and `system_notification` pushes. These include temps, `current_process`, `progress`, `step`, errors, and filament presence.
- **Filament and tools:** `load_filament(tool_index)`, `unload_filament`, `preheat`, `cool`, `load_print_tool`, `get_tool_usage_stats`
- **Maintenance:** `assisted_level`, `manual_level`, `calibrate_z_offset`, `home`, `reboot`, `run_diagnostics`, `zip_logs`, `enable_leds`/`disable_leds`, `toggle_sound`
- **Housekeeping:**
  - `set_analytics_enabled(false)` and `set_reflector_enabled(false)` turn off dead MakerBot cloud features.
  - `get_print_history` and `get_statistics` feed the history view.
- **Firmware:** `update_available_firmware`, `download_and_install_firmware`. Read-only in v1; we never push firmware.

The full method list from `kaitenstub.hh` is in Appendix A.

### 1.8 `.makerbot` file format
A ZIP containing:
- `meta.json`: `bot_type: "replicator_b"`, `tool_type` (Smart Extruder+ `mk13` / Tough `mk13_impla`), temps, bounding box, duration, material, and `miracle_config`
- `print.jsontoolpath`: a JSON array of per-move commands. There is **no G-code** in the file.
- `thumbnail_*.png` at several sizes, used by the printer's LCD

No gcode→`.makerbot` converter can read support/model roles back, so we keep
the intermediate G-code for the preview.

---

## 2. Product scope

**v1 (MVP)**
1. Discover printers (UDP broadcast + mDNS + manual IP) and show a list with name, IP, serial, and firmware.
2. Pair once (knob press), then auto-reconnect.
3. Import STL/3MF/OBJ with a 3D build-plate view (Replicator+ volume **295 × 195 × 165 mm**).
4. Slice with bundled presets. The **extruder is a print option** (see §2.1), and the material list (PLA, Tough PLA, …) filters to what that extruder supports.
5. Send and print, with upload progress, the "clear build plate" confirmation, and pause/resume/cancel.
6. Live dashboard with temps, progress, time remaining, current step, and errors.
7. **Live camera view** and snapshot saving (auto-snapshot at job end).
8. **Network settings:** scan, connect (including hidden SSIDs), forget, radio on/off, static IP / DHCP, rename the printer.
9. Filament load/unload, preheat/cool.
10. Export `.makerbot` to USB stick for offline use.
11. **Server / client mode** (see §3.1). One OpenBot instance runs as the **server**: it owns the printer connection and pairing. Other OpenBot instances on the network connect to it as **clients** and get the same features: status, printing, camera, filament, and Wi-Fi (admins only).
12. **Simple shared print queue.** It's needed once several clients can send jobs: first in, first out, and each job needs a human to confirm the plate is clear before it starts.
13. **Raspberry Pi server** (see §3.2). A headless `openbot serve` package for Raspberry Pi OS that sits next to the printer 24/7. Macs connect to it as clients.
14. **Runs on Apple Silicon and Intel Macs.** One universal2 app, macOS 12 Monterey or later.

### 2.1 Extruder as a print option
A dropdown in the Prepare panel's print settings offers:

| Choice | `meta.json` `tool_type` | Notes |
|---|---|---|
| Smart Extruder+ | `mk13` | Default preset set |
| Tough Smart Extruder+ | `mk13_impla` | Tough PLA presets. queue3d's profile is the starting point. |
| Experimental Extruder | `mk13_experimental` | Starts from the Smart Extruder+ presets, with third-party material temperature ranges unlocked |

- Each extruder has its own set of OrcaSlicer process and filament presets. Switching extruder swaps the preset set and re-validates the material choice.
- **Auto-default:** when a printer is connected, read the attached toolhead from `get_system_information` (`toolheads.extruder[0].tool_id`; verified in Phase 0, your extruder reports **8**) and preselect the matching extruder. The full tool_id → extruder table gets filled in as each extruder type is seen.
- **Mismatch guard:** before sending, if the file's `tool_type` doesn't match the attached extruder, show a warning and require confirmation. The printer would reject or misprint otherwise.
- The choice is saved per project, and the last-used choice becomes the default for new projects.

**v2**
- Timelapse (a frame per layer change from `state_notification`)
- Advanced queue: priorities, admin approval, per-user history
- Multiple printers per server
- Leveling/Z-calibration wizards
- Log bundle download
- G-code / toolpath preview with supports highlighted
- Slicing on the Pi server, for thin clients, using OrcaSlicer's official aarch64 Linux build (queue3d confirmed it)
- Generic Linux x86 server package
- Remote access from outside the home network, recommended via Tailscale/VPN rather than port-forwarding

**Explicit non-goals:**
- Custom firmware. It isn't needed, and a bad flash can brick the printer.
- Thingiverse/MakerBot cloud login.
- Support for the pre-5th-gen Replicator 2/2X, which use S3G/X3G over USB, a different protocol.

---

## 3. Architecture

One program runs in three roles:
- **Local:** talks to the printer directly. It's the same as a server nobody else is connected to.
- **Server:** owns the printer and shares it.
- **Client:** talks to a server instead of the printer.

The UI never knows which role it's in. It only talks to a **`PrinterBackend`**
interface, which has two implementations.

```
 CLIENT Mac(s)                                   SERVER Mac (next to the printer)
┌────────────────────────────┐                  ┌──────────────────────────────────────────┐
│ OpenBot app (Qt UI)        │                  │ OpenBot app (Qt UI)  — or `openbot serve` │
│  Prepare│Monitor│Camera│…  │                  │  Prepare│Monitor│Camera│Network│Server   │
│            │               │                  │            │                              │
│  PrinterBackend interface  │                  │  PrinterBackend interface                 │
│            │               │                  │            │                              │
│  RemoteBackend ────────────┼── HTTPS + WSS ──▶│  openbot.server (aiohttp)                 │
│  (openbot.remote)          │   :8765, TLS     │   • auth + roles   • job queue            │
│                            │   Bonjour        │   • event fan-out  • camera relay         │
│  openbot.slicing (local)   │   _openbot._tcp  │            │                              │
└────────────────────────────┘                  │  LocalBackend (openbot.printer)           │
                                                │  openbot.slicing                          │
                                                └────────────┬─────────────────────────────┘
                                                             │ :12309 TLS (control)
                                                             │ :9999  TCP (camera)
                                                             │ :12307 UDP / mDNS (discovery)
                                                      ┌──────▼───────┐
                                                      │ Replicator+  │
                                                      └──────────────┘
```

### 3.1 Server / client mode

**Why a server rather than every Mac talking to the printer directly:**
- **Pairing and credentials live in one place.** Pairing uses a knob press, and the `local_code` sits in one Keychain.
- **The printer's weak ARM CPU streams the camera once.** The server relays frames to any number of clients.
- **Commands are serialised through one owner.** Two Macs can't race to start jobs, and there's a single shared queue.
- **Clients don't need to see the printer** (no UDP broadcast, no Local Network quirks). They only need the server.

**Running the server**
- In the app: **Settings → Server → "Share this printer on the network"**. The app keeps serving while its window is closed, via a menu-bar icon.
- Headless on a Mac: `openbot serve --printer <serial|ip>`, with an optional **LaunchAgent** so it starts at login. Useful on a spare Mac mini.
- Headless on a **Raspberry Pi**: see §3.2.
- The server announces itself over **Bonjour** as `_openbot._tcp` (TXT record: server name, printer name/serial, version, TLS cert fingerprint).

**Connecting a client**
1. The client's Printers panel lists OpenBot servers it finds on Bonjour next to directly found printers. Manual `host:port` entry also works.
2. **Pairing, which mirrors the knob press.** The client asks to join, and the server shows **"MacBook-Air wants to connect — code 482 913. Allow as Operator / Viewer / Deny"**. The user checks that the code matches on both screens and approves it.
3. The server issues a long-lived **client token**, and the client stores it in its Keychain. The server's TLS certificate goes through the same trust flow as a printer's (§3.3).
4. The server's admin can see and revoke clients under **Settings → Server → Clients**.

**Roles**

| Role | Can do |
|---|---|
| **Viewer** | See status and camera, download snapshots |
| **Operator** | Viewer, plus: upload/queue/start/pause/cancel prints, filament load/unload, preheat/cool |
| **Admin** | Operator, plus: Wi-Fi/IP settings, rename, leveling/calibration, reboot, pair/unpair the printer, manage clients |

The server's own UI is always Admin.

**Server API (v1)**

TLS on one port (default **8765**) with a self-signed cert generated on first
run. Every call sends `Authorization: Bearer <client token>`.

| Endpoint | Purpose |
|---|---|
| `POST /api/pair`, `GET /api/pair/{id}` | Pairing request, then poll for the approval result |
| `WSS /api/ws` | **JSON-RPC 2.0** control channel. Method names mirror `PrinterBackend` (`status`, `start_job`, `cancel`, `load_filament`, `wifi_scan`, `wifi_connect`, `set_static_ipv4`, …). The server pushes events: `state`, `job`, `queue`, `printer_connection`. |
| `POST /api/jobs` | Upload a `.makerbot` file as a stream (resumable) and add it to the queue. Returns the job id. |
| `GET /api/jobs`, `DELETE /api/jobs/{id}` | View and manage the queue |
| `GET /api/camera.mjpeg` | Live camera relayed as MJPEG (multipart JPEG). Works in the Qt client and in any browser. |
| `GET /api/camera/snapshot.jpg` | Latest frame |
| `GET /api/info` | Version, printer, capabilities. Refuses mismatched protocol versions with a clear message. |

**Where slicing happens:** **on the client**, which has its own bundled OrcaSlicer. The client uploads the finished `.makerbot` file, so the server doesn't need spare CPU. Slicing on the server is a v2 option for thin clients.

**Job flow through the server**
1. Client uploads the job; it goes into the queue.
2. When it reaches the head of the queue, the server marks it **"waiting for plate clear"** and notifies all clients.
3. An Operator on any client, or on the server itself, confirms the plate is clear with the camera image shown.
4. The server calls `print` → `build_plate_cleared` → `put_*` to the printer.
5. When the job ends, the server attaches an auto-snapshot to the job and moves to the next one.

**Wi-Fi changes through the server (Admin only)**
- The server runs the same disconnect → rediscover → reauthorize flow as §1.5.
- Clients see "printer reconnecting…" rather than an error.
- Warning: if the *server* Mac relies on the printer's current network, moving the printer to another network strands it. The UI checks that the server can reach the target network first, by matching the Wi-Fi name to the server's own current network, and warns if not.

**Security**
- The server listens on the LAN only.
- TLS and a token are required on every endpoint. There is no anonymous access, including the camera.
- Tokens are stored only as hashes on the server.
- Pairing requests are rate-limited.
- For remote access, use a VPN such as Tailscale. We don't document port forwarding.

### 3.2 Raspberry Pi server

The same `openbot.server` + `openbot.printer` code, with **no Qt and no
slicer**. Clients slice on their Mac, so the Pi only relays commands, files,
and camera frames. That keeps it light.

**Hardware and OS**
- **Recommended:** Raspberry Pi 4 (2 GB+) or Pi 5.
- **Works:** Pi 3B+.
- **Not recommended:** Pi Zero 2 W. It can run, but has only 512 MB RAM and relaying the camera to several clients is tight.
- **OS:** Raspberry Pi OS (Bookworm or later), **64-bit**. It ships Python 3.11, so the server and printer libraries must support **Python 3.11+**. The Mac app bundles its own 3.12.

**Install:** pick one of these. Both install a `systemd` service that starts at boot and restarts on crash.
- **Primary: an apt repo / `.deb` package.**
  1. Add the OpenBot apt source, then run `sudo apt install openbot-server`.
  2. That installs into `/opt/openbot` (its own venv), with config at `/etc/openbot/server.toml` and data (queue, uploaded jobs, snapshots, TLS cert, tokens) in `/var/lib/openbot`.
  3. It runs as an unprivileged `openbot` user.
- **Alternative: a one-line install script** that sets up the same layout, for people who don't want to add a repo.

**Setup without a screen**

A Pi has no UI to press "Allow" on, so the process differs from a Mac server:
1. On first start, the server generates a one-time **setup code**. It prints the code to the console (`journalctl -u openbot`) and to `sudo openbot-server setup-code`.
2. On the Mac, the Pi appears under Bonjour as "OpenBot server (raspberrypi)". The user enters the setup code, and that Mac becomes the first **Admin**.
3. From that Admin Mac, the user picks the printer and pairs it (press the printer's knob when asked). The `local_code` is stored on the Pi in `/var/lib/openbot` with mode 0600, since there's no Keychain on Linux.
4. Later join requests from other Macs show up **on every Admin client's screen** to approve, instead of on the server's screen. There's also a CLI fallback: `sudo openbot-server clients approve <code>`.
5. Admins manage server settings from the Mac app (**Server settings** page, pushed over the API). Editing `/etc/openbot/server.toml` over SSH is an alternative.

**Two ways to wire the Pi to the printer**
- **Same LAN (default):** the Pi and the printer are both on the home network by Wi-Fi or Ethernet.
- **Direct cable, optional:** run an Ethernet cable from the Pi to the printer, and use Wi-Fi for the Pi's own link to the home network.
  - The printer is then reachable only through the Pi. That's more reliable and more private.
  - The printer gets a link-local 169.254.x.x address (seen in the firmware's handshake), or the Pi runs a tiny DHCP server (dnsmasq) on `eth0`. The installer offers this as a checkbox.
  - Changing the printer's Wi-Fi from a client still works.

**Pi-specific engineering**
- **Camera relay passes JPEG through untouched** (no decode/re-encode). One printer stream fans out to N clients, which costs almost no CPU.
- Stream uploads straight to disk (`/var/lib/openbot/jobs`), never holding a file in RAM. Show a warning if the SD card is low on space.
- **Reduce SD card wear:** logs go to journald with limits, and the queue database is SQLite in WAL mode.
- Uses `avahi` for the Bonjour advert and `zeroconf` for printer discovery. Both work on Pi OS out of the box.
- Nothing on the server may import Qt or macOS APIs. CI enforces this by running the server test suite on **Linux arm64** (GitHub Actions arm64 runners) as well as macOS.

### 3.3 Trusting the printer's certificate

**What the certificate looks like** (read from your printer in Phase 0):

| Field | Value |
|---|---|
| Subject / issuer | `CN=MakerBot Replicator, O=MakerBot Industries` (self-signed) |
| Valid | 2016-06-14 → year 4754 |
| Flags | `CA:TRUE`, no subjectAltName |
| SHA-256 fingerprint | `AE:60:D1:21:…:4C:BC:35` |

The certificate isn't inside the firmware image, so it's probably generated
per printer at the factory. We can't prove it's unique to each unit, though,
and it doesn't name the printer's IP or hostname. So OpenBot trusts **this exact
certificate for this exact printer** (keyed by serial number). It never
trusts "anything MakerBot-signed".

**What the user sees**

On first connection (or pairing), a **"Trust this printer?"** sheet shows:
- the printer's name and serial
- the certificate's subject, issuer, and validity dates
- the SHA-256 fingerprint, in groups for easy comparison

The choices are:
1. **Trust this printer's certificate** (default). OpenBot saves the certificate and verifies every later connection against it.
2. **Trust for this session only.** Nothing is saved, and the question is asked again next time.
3. **Cancel.** Nothing connects.

**Settings → Printers → Certificate** offers:
- **View** the details, and **Export** the certificate as `.pem`/`.cer`.
- **Import certificate…** Pre-trust a printer from a file, for example one exported on another Mac or fetched over a direct Ethernet cable. Then the first connection needs no prompt.
- **Remove trust.** The next connection asks again.
- **Also add to macOS Keychain (optional, off by default).** This makes the trust visible to other Mac tools too, not just OpenBot.
  - It runs `security add-trusted-cert` into the **login** keychain, with trust **restricted to the SSL policy for that printer's IP/hostname** (`-p ssl -s <host>`). It never adds the certificate as an unrestricted root.
  - This matters because the certificate is flagged `CA:TRUE`: unrestricted root trust would let anyone who obtains its private key impersonate *any* website to this Mac.
  - macOS asks for the user's password. Removing trust in OpenBot removes the Keychain entry too.

**If the certificate changes:**
- **OpenBot refuses to connect.** It shows old and new fingerprints side by side ("This printer's identity changed — this happens after a factory reset or mainboard replacement, or if something on your network is impersonating it").
- Continuing requires explicit **Trust new certificate**. There is no silent re-trust.

**How it's implemented**
- The trusted PEM is loaded as the *only* trust anchor: `ssl.SSLContext` with `verify_mode=CERT_REQUIRED`, `load_verify_locations(cadata=pem)`, and `check_hostname=False`, since the certificate has no name to match. This is real TLS verification rather than skip-verify-then-compare. Phase 0 tested it against your printer: verified connection OK, and the default trust store correctly rejects the certificate.
- Storage:
  - **Mac:** the app's data folder, `~/Library/Application Support/OpenBot/trust/<serial>.pem`.
  - **Pi server:** `/var/lib/openbot/trust/`, managed from an Admin client's Server settings.
- The same trust sheet, import/export, and change warning apply to an **OpenBot server's** certificate when a client connects.

**Stack: Python 3.12 + PySide6 (Qt 6), macOS app; Python 3.11+ for the Pi server.**
- All the reference code is Python: kaiten itself, queue3d, and mbotmake. Under GPL we can reuse it directly.
- The same Python code runs the GUI, the headless `openbot serve`, and later a Linux/Raspberry Pi server. A Swift app would need the protocol written twice.
- Networking is **asyncio** throughout: the printer link, the aiohttp server, and the client. `qasync` bridges it to the Qt event loop.
- Qt on macOS: native menus, Dark Mode, Retina, a Metal-backed 3D viewport (Qt Quick 3D), a camera widget, and credentials in the macOS Keychain (via `keyring`).
- **Mac targets: Apple Silicon (M1–M4 and later) and Intel Macs.**
  - Ship one **universal2** `OpenBot.app` containing native arm64 and x86_64 code, so it doesn't run under Rosetta on new Macs.
  - Minimum **macOS 12 Monterey**, which covers Intel Macs from about 2015 onward. It's also the minimum for Qt 6.8.
  - Every bundled native piece must be universal2 or shipped as a pair: Python, PySide6 wheels, `cryptography`, and the OrcaSlicer CLI. OrcaSlicer's macOS release is universal. Phase 2 confirms its minimum macOS version, and if it's newer than 12 we pin an older OrcaSlicer release.
  - All Macs that run macOS 12 support Metal, so the 3D viewport works on Intel integrated graphics too. Test on one to check performance with large models.
- **Packaging:** Briefcase → `OpenBot.app` in a signed, notarized `.dmg`, with OrcaSlicer.app's CLI bundled inside `Contents/Resources`.
- **macOS permissions:**
  - **Local Network** (`NSLocalNetworkUsageDescription`), plus the `_makerbot-jsonrpc._tcp` and `_openbot._tcp` Bonjour services in `NSBonjourServices`. Without these, macOS 14+ silently blocks discovery.
  - When server mode is turned on, macOS will ask whether to allow incoming connections. Document this prompt in the user guide.
  - The app sandbox stays **off**. Raw UDP broadcast and running a bundled slicer are painful under it, and we're shipping outside the App Store.

**Slicing engine:** bundle the **OrcaSlicer** CLI (AGPL, run as a separate
executable; a universal macOS build exists). Slice through a generated `.3mf`
project, because its `--load-settings` path hits a compatibility-gate bug in
2.4.x (documented in queue3d). Then convert G-code → `.makerbot` with a
**vendored, cleaned-up mbotmake**.

**Licence: GPL-3.0-or-later.** What we can reuse:

| Code | Licence | Use |
|---|---|---|
| `charely6/mbotmake` | GPL-3.0 | Vendor into `slicing/toolpath.py` and `slicing/package.py`, keeping the copyright notice |
| `garfield-arlene/queue3d` | MIT | Reuse its socket/camera handling, OrcaSlicer profile and 3MF wrapper, with attribution |
| `charely6/makerbot-gen5-api` | LGPL | Reuse the discovery code |
| `tjhorner/*` | No licence file | **Reference only.** Don't copy code. |
| OrcaSlicer | AGPL | Bundled unmodified. Link to its source in the About box. |
| kaiten firmware source | MakerBot proprietary | Used to learn the protocol only. **Never copy it into our repo.** |

### Repository layout
```
openbot/
  backend.py         # PrinterBackend protocol (abstract): status, jobs, camera, filament,
                     #   network, maintenance + event stream; roles checked here
  local_backend.py   # PrinterBackend → openbot.printer (direct to the Replicator+)
  queue.py           # job queue + "waiting for plate clear" state machine (used by server & local)
  server/
    app.py           # aiohttp app: REST + WebSocket JSON-RPC, TLS, Bonjour advert
    auth.py          # pairing codes, client tokens (hashed), roles
    camera_relay.py  # one printer stream → MJPEG fan-out to N clients
    service.py       # `openbot serve`, LaunchAgent install/uninstall
  remote/
    backend.py       # RemoteBackend: PrinterBackend over HTTPS/WSS to a server
    discovery.py     # browse _openbot._tcp
  printer/
    transport.py     # TLS + TCP sockets, brace-counting JSON reader, raw-mode reads
    rpc.py           # request/response ids, notification dispatch, timeouts
    discovery.py     # UDP 12307 broadcast + zeroconf
    auth.py          # authorize/reauthorize, keychain storage
    trust.py         # per-printer cert store, verified SSL context, import/export,
                     #   change detection, optional macOS Keychain add/remove
    client.py        # high-level Printer API (status, print, filament, ...)
    upload.py        # put_init/put_raw/put_term, CRC32, progress callbacks
    camera.py        # stream reader, 16-byte header, JPEG/YUYV → QImage
    network.py       # wifi_* / static IP / network_state + reconnect-by-serial
  slicing/
    orca.py          # locate/run bundled OrcaSlicer, 3mf wrapper
    toolpath.py      # gcode → jsontoolpath commands
    package.py       # meta.json, thumbnails, zip → .makerbot
    profiles/
      extruders.json # extruder id ↔ tool_type ↔ tool_id ↔ allowed materials
      mk13/ mk13_impla/ mk13_experimental/   # per-extruder process + filament presets
  app/               # PySide6 UI
  cli.py             # `openbot discover|pair|status|print|camera|wifi|serve ...`
tests/
  fake_printer/      # asyncio mock kaiten (TLS+TCP) for CI
  backend_contract/  # one test suite run against BOTH LocalBackend and RemoteBackend(server(LocalBackend))
  fixtures/          # captured traffic, sample .makerbot files
tools/probe.py       # Phase 0 hardware verification script
packaging/
  macos/             # Briefcase config, Info.plist keys, entitlements, notarize script, LaunchAgent plist
  debian/            # openbot-server .deb: systemd unit, postinst (user, dirs, cert), apt repo scripts
  install.sh         # one-line Pi installer (alternative to apt)
```

---

## 4. Implementation phases

### Phase 0 — Hardware verification (≈2–3 days)
Write `tools/probe.py`, run it against the real Replicator+, and record the
results in `docs/protocol.md`. It should:
1. UDP broadcast discovery, then capture the reply (confirm `machine_type`, `bot_type`, firmware version).
2. Connect to TLS `:12309`. Record the negotiated TLS version and cipher, and the cert fingerprint.
3. `handshake`, then `authorize` with `local_secret` (knob press), then disconnect, then `reauthorize` on a new connection. **Confirms persistent pairing.**
4. `network_state`, `wifi_scan`, `get_static_ipv4("ethernet")` (read-only).
5. `get_system_information`; capture ~60 s of `state_notification` traffic while idle and during a short print. Record `toolhead_0_status.tool_id` for each extruder you own, for the auto-default in §2.1.
6. Camera: `request_camera_stream` → save 5 frames → `end_camera_stream`.
7. Send one known-good `.makerbot` (sliced by the old MakerBot Print) and print it.
8. Check that `wifi_connect` is rejected on `:9999` and accepted on `:12309`.

**Exit criteria:** every method used in v1 is confirmed or has a documented
workaround. Captured traffic becomes test fixtures.

#### Phase 0 results — 2026-09-27, MakerBot Replicator+ (fw 2.6.2 build 734, `machine_type=horseshoe`, `bot_type=replicator_b`, API 1.9.0)

| # | Check | Result |
|---|---|---|
| 1 | Discovery | ⚠️ **Bonjour works through macOS's own `dns-sd` / mDNSResponder.** python-zeroconf's own multicast socket saw nothing on the Mac, so the Mac uses the system daemon and the Pi uses zeroconf. UDP broadcast got no reply, most likely because MakerBot Print's `conveyor-svc` was running (macOS Local Network privacy may also play a part). The Bonjour record has `ip=None`, and `Makerbot-XXXXXX.local` **does not resolve**. Bonjour plus the ARP match finds the printer. |
| 2 | TLS :12309 | ✅ TLS 1.2, ECDHE-RSA-AES256-GCM-SHA384. Handshake answered on both ports. |
| 3 | Pairing | ✅ `authorize` + knob press gave a `local_code`. ✅ `reauthorize` on a new connection worked with no knob press. ✅ `one_time_token` works on :9999 **once only**. |
| 4 | Read-only network | ✅ `network_state` (Ethernet, DHCP, Wi-Fi radio enabled), `wifi_scan` (3 APs, including a hidden one), `get_static_ipv4`, `get_authorized`, `get_tool_usage_stats`, `get_system_information` |
| 5 | Status | ✅ Idle printer pushes `system_notification` about every 1.4 s. The extruder is at `toolheads.extruder[0]` (tool_id **8**, filament present). `state_notification` still needs checking during a print. |
| 6 | Camera | ✅ `request_camera_stream` gives 640×480 JPEG frames (~38 KB each) with a 16-byte header, as documented. ✅ `request_camera_frame` is supported. |
| 7 | Print | ✅ MakerBot Print's bundled `1cm_x_1cm_block_Rep+.makerbot` (`replicator_b`, `mk13`, PLA 215 °C) uploaded over TLS (66,783 bytes in 2.6 s) and **printed successfully**. Steps seen: `initial_heating` → `homing` → `final_heating` → `printing` 0–100 → `end_sequence` → `cleaning_up` (`complete: true`), about 10 minutes in total. During the print the printer sends `state_notification` a few times a second; 368 were captured. |
| 8 | Secure channel | ✅ `wifi_connect` on :9999 is refused with `-32604 privileged information on unsecure channel`. ✅ :12309 counts as secure. |

**Changes these results caused:**
- Removed the legacy-TLS workaround.
- Added the §3.3 certificate trust design.
- Fixed the status field path.
- `request_camera_frame` is now used when available.
- **Discovery needs an address fallback.** Bonjour gives the printer's name and serial but no usable address. Resolve it in this order:
  1. The UDP broadcast reply (it includes `ip`).
  2. The Mac's ARP table: the **last 12 hex digits of `iserial` are the printer's MAC address** (verified: `…3C70590A1B2C` ↔ `3c:70:59:0a:1b:2c`).
  3. A quick :9999 handshake sweep of the local /24.
  4. Manual IP.
- The app should detect a running MakerBot Print / `conveyor-svc` and offer to stop it.

Also noted: `get_authorized` lists an `ANON` entry with 4 local auths. Those are MakerBot Print's existing pairings. OpenBot doesn't touch them.

### Phase 1 — Protocol library + CLI (≈1.5–2 weeks)
- `transport`/`rpc` with a fake-printer test harness in CI
- `discovery`, `auth` (keychain via `keyring`), `client`, `upload`
- `camera`, `network`
- Define the **`PrinterBackend`** interface and `LocalBackend` now, so the CLI and later the UI only use the interface.
- CLI commands: `discover`, `pair`, `status --watch`, `print file.makerbot`, `camera --snapshot out.jpg`, `wifi scan|connect|forget`, `ip static|dhcp`
- **Milestone:** a full print and a Wi-Fi change done from the terminal.

**Status (2026-09-27): library + CLI built; 37 automated tests pass against a fake printer.**

Verified with `openbot` against the real printer:
- `discover`, via Bonjour + ARP while MakerBot Print was running
- `status`, during a print
- `cert show`, which matched the trusted fingerprint
- `ip show`
- `wifi scan`
- `camera --snapshot`

Still to verify on hardware, because each changes the printer:
- `pair` through the CLI (the Phase 0 pairing was imported instead)
- `print` through the CLI (Phase 0 used the probe)
- `wifi forget`/`on`/`off` (`wifi connect` ✅ verified: joined HomeWiFi while Ethernet stayed up, got 192.168.1.115, and answered there with the same trusted certificate)
- Finding: with Ethernet and Wi-Fi both up, `network_state` reports only the primary (Ethernet) connection. The Network UI must also show the Wi-Fi link, using `wifi_scan`'s `saved` flag and a handshake on the Wi-Fi address.
- `ip static`/`dhcp`
- `rename`
- `filament`
- `pause`/`resume`/`cancel`

### Phase 2 — Slicing pipeline (≈2 weeks)
- OrcaSlicer bundling (universal macOS build) and locating it inside the app bundle
- Replicator+ printer/process/filament profiles (start from queue3d's `makerbot-plus-tough-extruder.json`)
- G-code → jsontoolpath converter. Validate it by byte-comparing against files from the official MakerBot Print where possible, and by printing calibration cubes.
- `meta.json` + thumbnails + packaging
- **Milestone:** STL → `.makerbot` → successful print of a calibration cube, a benchy, and an overhang test.

**Status (2026-09-27): pipeline built; not yet printed on hardware.**

The pipeline: STL/OBJ → centred on the bed → 3MF project with the settings embedded → bundled OrcaSlicer 2.4.2 (universal, `vendor/OrcaSlicer.app`) → our G-code → jsontoolpath converter → `.makerbot` with rendered thumbnails. The `openbot slice` and `openbot print model.stl` commands use it.

Output matches MakerBot Print's own Replicator+ file:
- relative `a`, mm/s feedrates, bed-centred coordinates
- the same start retract at (-150, -100)
- fan commands
- no temperature commands; heating comes from `meta.json`

Safety limits are enforced: the build volume, 250 °C maximum, and no arc moves, inch units or second extruder.

Profile changes from queue3d's profile:
- no heated bed
- firmware-owned start/end G-code
- relative E with `G92 E0` on each layer
- skirt at first-layer speed

**Open:**
- Print an OpenBot-sliced file on the real printer.
- Tune speeds and fan. The profile inherits queue3d's conservative 20–40 mm/s; MakerBot uses up to 90.
- Confirm the Tough PLA material code `im-pla` on hardware.
- 3MF model input.

### Phase 3 — Desktop app (≈3 weeks)
- **Printers panel:** discovery, pairing wizard ("Press the knob on your printer now"), connection health
- **Prepare:** 3D plate, move/rotate/scale/lay-flat/arrange, slicing settings (basic + advanced), slice preview with layer slider, time and material estimate
- **Monitor:** temps chart, progress, step, pause/resume/cancel, **live camera panel** with snapshot, auto end-of-job photo
- **Network:**
  - Current connection card
  - Wi-Fi list with signal bars, lock icon, and "saved" badge
  - Connect dialog with a hidden-SSID option
  - Forget, radio toggle, DHCP/static form with validation, rename printer
  - The disconnect → rediscover → re-auth flow
- **Maintenance:** load/unload filament, preheat/cool, leveling, Z-offset, reboot, download logs, analytics/reflector off
- Settings, error surfaces mapped from kaiten error codes, logging

**Status (2026-09-27): first working version (`openbot-app` / `python -m openbot.app`).** It uses PySide6 6.8 LTS (universal, macOS 12+) with qasync.

Pages:
- **Printers:** discovery, manual IP, connect/disconnect.
- **Prepare:** 3D plate view, the settings form with the extruder auto-selected from the printer, slicing, thumbnail, save, and print with a camera-photo confirmation.
- **Multiple objects:** Add Models… (several files at once), Remove (button or Delete/Backspace), Clear Plate. Objects are auto-arranged in rows 5 mm apart with no overlap, the selected object is highlighted in the 3D view, and all objects slice into one print. `openbot slice a.stl b.stl …` does the same from the CLI.
- **Monitor:** status, progress, temperatures, pause/resume/cancel/done, live camera, snapshot.
- **Network:** Wi-Fi list, join (including hidden networks), forget, disconnect, radio, DHCP/static IP, rename.
- **Maintenance:** filament, heat, and certificate export/import/Keychain/remove.

Dialogs: trust (§3.3), certificate changed, pairing (knob), Wi-Fi password, print confirmation.

It auto-reconnects with backoff.

`tools/app_smoke.py` drives the whole app headlessly against the fake printer, including a real OrcaSlicer slice, and screenshots every page. It passes.

Bug found by the smoke test and fixed: `connect()` now checks certificate trust before pairing.

**Open:**
- Layer-by-layer toolpath preview.
- Move/rotate/scale on the plate.
- Temperature chart.
- Settings window.
- App icon and packaging (Phase 4).

### Phase 3b — Server / client mode (≈2–3 weeks)
- `queue.py` job state machine: queued → waiting-for-plate-clear → uploading → printing → done/failed/cancelled
- `openbot.server`: aiohttp + TLS cert generation, WebSocket JSON-RPC, job upload, MJPEG camera relay, Bonjour advert
- Pairing with confirmation codes, client tokens, roles, and the Clients management screen
- `RemoteBackend` + `_openbot._tcp` browsing in the Printers panel
- Menu-bar "keep serving" mode, `openbot serve`, and LaunchAgent install
- The **backend contract test suite** runs against Local and Remote-via-server, so every feature is guaranteed to work in both modes
- **Milestone:** two Macs; the client slices and queues a job, the server-side user confirms the plate, the print runs, and both watch the camera live. Then an Admin client changes the printer's Wi-Fi and both reconnect.

### Phase 3c — Raspberry Pi server (≈1–1.5 weeks)
- Keep the server/printer packages Qt-free and compatible with Python 3.11. Add a Linux arm64 CI job.
- Headless setup-code flow, Admin-client approval of new clients, and the server settings page in the Mac app
- `.deb` + systemd unit + apt repo, and `install.sh`
- Optional direct-cable mode (dnsmasq on `eth0`)
- Stress test on a Pi 4: 3 clients watching the camera during a print, plus a 50 MB upload
- **Milestone:** fresh SD card → install → pair from an Intel Mac and an Apple Silicon Mac → full print with the camera watched on both.

### Phase 4 — Hardening & release (≈1–2 weeks)
- Reconnect/backoff and network-change handling, on both links: server↔printer and client↔server
- Server asleep: tell the user to prevent sleep while serving (`caffeinate`-style assertion while jobs are queued or printing)
- Camera and upload under a flaky link
- Guard against double-starts (reject a print while one is running)
- Signed and notarized universal2 `.dmg`
- **Test matrix:**

  | Mac | macOS versions |
  |---|---|
  | Apple Silicon | 12, latest |
  | Intel | 12, latest |

  | Pi | Checked |
  |---|---|
  | Pi 4 | full tests |
  | Pi 5 | full tests |
  | Pi 3B+ | smoke test |

  Also check the Local Network and incoming-connection prompts on macOS 14+. CI builds on both arm64 and x86_64 macOS runners, and uses `lipo -info` to confirm every binary in the bundle is universal.
- Pi `.deb` published to the apt repo; tested on a fresh 64-bit Raspberry Pi OS install
- Automatic updates via Sparkle, optional.
- User guide, including "MakerBot Print is still running → stop its background service; it can hold the discovery port"

**Total:** roughly 11–14 weeks for one developer. Server/client mode adds 2–3 weeks and the Pi server about 1–1.5. Reusing GPL code trims about a week.

---

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Replicator+ firmware differs from the extracted Mini+ source | Phase 0 probes every method first. Our RPC layer treats "method not found" as a capability flag, not a crash. |
| Old TLS on the printer is rejected by modern OpenSSL | Custom SSL context (SECLEVEL=0, TLS ≥1.0). If that still fails, bundle an OpenSSL build with legacy support for this one socket. |
| Wi-Fi change strands the printer off-network | Recommend Ethernet during changes. Show a timeout-and-rediscover flow. The printer's own LCD Wi-Fi menu is always the recovery path. |
| Converter output rejected or printing incorrectly | Validate against official files. Start with conservative speeds. The first prints are supervised. |
| Another host (old MakerBot Print/Desktop "conveyor") holds the printer connection | Detect it and tell the user to quit or stop the old service. |
| Safety: remote start of a heated machine | Always require the build-plate-cleared confirmation dialog. Show camera before starting. Keep cancel one click away. |
| macOS Local Network privacy blocks discovery | Declare the Info.plist keys (§3). Manual-IP entry always works as a fallback. |
| Server Mac sleeps or quits mid-print | The print keeps running on the printer on its own. The server reattaches on wake. It holds a no-sleep power assertion while printing, and clients show "server offline" rather than failing silently. |
| Client and server run different versions | `GET /api/info` reports the API version. Mismatched versions get a clear "update OpenBot" message. |
| Printer certificate is shared across units, or its key leaks | Trust is scoped to one certificate for one serial, and Keychain trust is SSL-only and host-restricted, never an unrestricted root (§3.3). A changed certificate blocks the connection until the user re-trusts it. |
| Someone on the LAN connects to the server | TLS and tokens on every endpoint, pairing needs approval at the server, roles, rate limiting |
| Wrong extruder chosen for a print | Auto-default from the attached toolhead, plus the pre-send mismatch warning (§2.1) |
| Legal/licensing | Interoperability reverse engineering of our own device; no MakerBot code shipped; no firmware modified; no MakerBot trademarks in the product name or UI. |

---

## 6. Decisions (2026-09-27)
1. **Platform:** the desktop app runs on macOS, on **both Apple Silicon and Intel Macs** (universal2, macOS 12+). The server also runs on **Raspberry Pi** (Pi OS 64-bit, headless).
2. **Extruder:** a per-print option covering Smart Extruder+, Tough and Experimental, auto-defaulted from the attached toolhead (§2.1).
3. **Form factor:** a desktop program, not web-based. **In v1 any instance can run as a server, and other instances connect to it as clients** (§3.1). **A headless Raspberry Pi server is also in v1** (§3.2).
4. **Licence:** GPL-3.0-or-later. Reuse mbotmake, queue3d and gen5-api code with attribution.

---

## Appendix A — kaiten JSON-RPC methods (from firmware `kaitenstub.hh`)
acknowledged, add_local_auth🔒, add_makerbot_account🔒, assisted_level, authenticate,
authorize🔒, birdwing_list, bot_maintained, bronx_upload, brooklyn_upload,
calibrate_z_offset, cancel, capture_image, change_machine_name, clear_all_z_pause,
clear_authorized, clear_queue, clear_ssh_id, clear_z_pause_mm, close_queue,
cloud_slice_print, cool, copy_ssh_id, deauthorize, desync_account,
disable_check_build_plate, disable_leds, disable_z_pause, download_and_install_firmware,
drm_print, dump_machine_config, enable_check_build_plate, enable_leds, enable_z_pause,
end_camera_stream, execute_queue, expire_thingiverse_credentials, external_print,
fcgi_reauthorize🔒, firmware_cleanup, first_contact, get_authorized,
get_available_z_offset_adjustment, get_cloud_services_info, get_config, get_init,
get_machine_config, get_persistent_statistics, get_print_history, get_queue_status,
get_sound_state, get_static_ipv4, get_statistics, get_system_information,
get_tool_usage_stats, get_unique_identifiers, get_z_adjusted_offset, handshake,
has_z_calibration_routine, home, is_endstop_triggered, kill_power, library_print,
load_filament, load_print_tool, machine_action_command, machine_query_command,
machine_query_process, manual_level, network_state, open_queue, park, ping, preheat,
print, print_again, process_method, put_init, put_raw, put_term, reauthorize🔒, reboot,
register_client_name, register_fcgi, register_lcd, request_camera_frame,
request_camera_stream, reset_lcd, reset_to_factory, restart_ui, resume_boot,
run_diagnostics, set_analytics_enabled, set_auto_unload, set_reflector_enabled,
set_static_ipv4, set_thingiverse_credentials🔒, set_toolhead_error_visibility,
set_z_adjusted_offset, set_z_pause_mm, setup_printer, sync_account_to_bot,
toggle_sound, transfer_progress, unload_filament, update_available_firmware,
wifi_connect🔒, wifi_disable, wifi_disconnect, wifi_enable, wifi_forget,
wifi_fre_authorize🔒, wifi_reset, wifi_scan, wifi_setup, wifi_signal_strength,
yonkers_upload, zip_logs

🔒 = `require_secure` (TLS port 12309 only)

## Appendix B — References
- https://github.com/charely6/Makerbot-5gen-plus (extracted firmware; kaiten source)
- https://github.com/garfield-arlene/queue3d (Replicator+ hardware-verified client + slicing)
- https://github.com/tjhorner/makerbot-rpc · https://github.com/tjhorner/node-makerbot-rpc
- https://github.com/abdyfranco/makerbot-php · https://github.com/charely6/makerbot-gen5-api
- https://github.com/charely6/mbotmake
- https://groups.google.com/g/makerbot-users/c/CyEPytxdXi0 (firmware package format)
- https://github.com/charely6/Makerbot-5gen-plus/discussions/2


---

## 7. Creality Ender-3 support (added 2026-09-27)

The user also has a **Creality Ender-3 / Ender-3 Pro**. It isn't near this Mac, so everything so far is tested against a simulated Marlin printer.

**Printers.** `openbot/machines.py` is a catalogue of printers:
- the Replicator+ (network, `.makerbot`)
- Marlin USB printers (`.gcode`): Ender-3, Ender-3 Pro (default), Ender-3 V2, Ender-3 S1 and Ender-3 V3 SE

For the Enders:
- **Profiles:** OpenBot flattens OrcaSlicer's bundled Creality profiles (`slicing/orca_profiles.py` resolves the `inherits` chains).
- **Checked against OrcaSlicer:** bed size and height are tested against OrcaSlicer's own profiles, and the available qualities are detected from them.

**Temperature safety.**
- OrcaSlicer's generic Creality filaments are tuned for all-metal hotends (PETG 255 °C, ABS 260, TPU 240). The stock Ender-3 / Pro / V2 hotend is PTFE-lined and degrades above about 240 °C.
- OpenBot sets its own defaults: PLA 205 °C / bed 60, PETG 235 / 70, ABS 240 / 100, TPU 225 / 40.
- It enforces a per-machine limit: 240 °C for the Ender-3 / Pro / V2 and 260 °C for the S1 and V3 SE, with a 100 °C bed.
- The limits are checked in the settings, in the final slicer config, and in the finished G-code.

**Slicing.** Model → OrcaSlicer → G-code.
- Every move is checked against the build volume, and nozzle temperatures against the limit.
- Time, filament and layer counts come from OrcaSlicer's own estimates.
- The purge line is excluded from the model bounding box.

**SD cards (`openbot/drives.py`).**
- Lists removable drives, skipping disk images.
- Warns about exFAT; stock Marlin needs FAT32.
- Uses safe short filenames, copies atomically, then syncs and ejects.
- The same flow puts `.makerbot` files on a USB stick for the Replicator+.

**USB, Marlin (`openbot/marlin/`).**
- Numbered, checksummed lines with ok/resend flow control and "busy:" keep-alive.
- A halted printer (thermal protection, M112) is detected.
- Temperatures are auto-reported (`M155`), with polling for firmware that can't.
- Printing streams over USB, with pause, resume, and cancel. Cancel turns the heaters off, lifts Z 10 mm and switches the motors off.
- The printer's own SD card: list (long filenames), print, auto-reported progress, and abort (`M524`).
- Preheat per material, home, motors off, emergency stop (`M112`), and a G-code console.
- USB printers are **never auto-reconnected**, because reopening the port can reset the board and kill an SD print.
- The Connect USB button always warns about that first.

**App.**
- Each backend declares its capabilities, and the UI hides what a printer can't do.
- For a USB Ender: no Network page and no camera; instead a Console page, an SD card panel, emergency stop, bed temperature, and heating presets.
- Prepare has a printer picker, a bed temperature field, "Save to SD Card…" and "Open Print File…" (`.makerbot` or `.gcode`, with the G-code safety-checked).
- The 3D view follows the selected printer's bed.

**Tests.**
- 80 automated tests, including a pty-based fake Marlin with checksum and line-number validation, forced resends, and SD card and temperature simulation.
- `tools/app_smoke.py` now runs both a Replicator+ scenario and an Ender scenario: USB connect, G-code slice, save to SD, USB print, printer SD list, and console.

Bug fixed along the way: modal dialogs opened from inside asyncio tasks could crash qasync ("Cannot enter into task"). They now always open on the next event-loop turn.

**Still to verify on the real Ender-3 Pro:**
- the actual USB chip and port name
- whether opening the port resets the board
- baud rate: 115200 is assumed; some firmware uses 250000
- the M115 capabilities of its firmware (stock 1.1.x firmware may lack `M155` or `M524`; fallbacks exist)
- an SD print from a card written by OpenBot
- a short USB-streamed print


---

## 8. Progress: autonomous steps (2026-09-27)

### 8.1 Object transforms ✅
`slicing/plate.py`: each object has its own position, rotation (X/Y/Z) and uniform scale.
- **Arranging:** objects are auto-arranged until one is moved by hand. After that, new objects go to the first free spot, and **Arrange All** lays everything out again.
- **Warnings:** overlaps and objects over the edge are shown in red, and Slice is disabled until they're fixed.

**Selected object panel:** X/Y, Rotate, Scale %, Tip over X/Y, Reset, Duplicate, Arrange All.

**3D view:** click an object to select it and drag it across the plate. Dragging empty space orbits. The view follows the chosen printer's bed.

**Checks:** 8 unit tests, and a real OpenGL window test with a simulated mouse drag, which moved the object exactly +40/−30 mm.

### 8.2 Server / client mode ✅ (§3.1)
**Server:** `openbot/server/` (aiohttp over TLS; self-signed EC certificate).
- 6-digit pairing codes, and a one-time setup code for a headless server's first Admin.
- Viewer, Operator and Admin roles.
- Job queue: queued → waiting for plate → printing → done/failed/cancelled, with a photo of the finished print.
- MJPEG camera relay and Bonjour `_openbot._tcp`.

**Client:** `openbot/remote/`. `RemoteBackend` pins the server's certificate. "Print" uploads into the queue.

**App:**
- **Printers:** an OpenBot servers section, with setup-code entry.
- **Queue page:** "Plate Is Clear: Start" (with a camera photo), cancel, remove, and view the result photo.
- **Share page:** share the connected printer, approve or deny with a role, remove access. It also manages a remote server when you're its Admin.
- Pages a role can't use are hidden (Network and Console need Admin).

**CLI:**
- `openbot serve [--config]` runs the server.
- `openbot server pending|approve CODE|deny|clients|revoke` manages it through a local console-admin token.

**Tests:** 9 end-to-end server tests, headless CLI testing across processes, and a two-window app scenario (A shares, B joins as Operator, queues a print, confirms the plate, and the printer receives it).

**Bugs found and fixed:**
- The console admin was wiping the setup code.
- Job files were renamed to random IDs, so the printer's screen showed an ID instead of the file name.
- The G-code check rejected files without OrcaSlicer's layer markers.

### 8.3 Raspberry Pi server ✅ (§3.2)
**Package:** `packaging/build_deb.py` produces `dist/openbot-server_<ver>_all.deb` without needing dpkg.
- systemd unit (hardened, `dialout` group for USB).
- `/etc/openbot/server.toml` as a conffile.
- `openbot-server` wrapper: `sudo openbot-server server pending/approve…`, `setup-code`, `-p IP pair`.
- postinst builds a private Python venv in `/opt/openbot`.

**Checked in Docker on Debian Bookworm arm64** (same base and architecture as 64-bit Raspberry Pi OS):
- Install: creates the `openbot` user, locks down the data folder, runs the server from the config file, and passes the Qt-free import check.
- Pairing works through the wrapper.
- **91 tests pass on Linux + Python 3.11** (6 skipped because they need OrcaSlicer).
- `systemd-analyze verify` passes.
- Remove keeps the config; purge removes everything.

**Fixed:** validation without OrcaSlicer, an adduser warning, and unbuffered logging.

### 8.4 macOS app ✅ (Phase 4, except signing)
**Build:** `packaging/macos/build_app.py`, run with the python.org universal2 Python.
- Makes a clean build venv.
- Merges per-architecture wheels into universal2 (`delocate-merge`).
- PyInstaller bundle with the Info.plist Local Network and Bonjour keys, and LSMinimumSystemVersion 12.0.
- Generated app icon, OrcaSlicer bundled in `Contents/Resources`, an ad-hoc signature, and a `.dmg`.

**2026 dependency facts:**
- cryptography ≥49 and zeroconf no longer ship Intel Mac wheels. Universal builds pin `cryptography<49` (48.0.1, the last universal2 release) and use zeroconf's pure-Python mode.
- Revisit these when Intel support is dropped or the packages change.

**Verified:**
- Every Mach-O file in the bundle is universal.
- `OpenBot --self-test` passes natively on arm64 **and under Rosetta as x86_64**, including real slices for the Replicator+ and Ender-3 Pro with the bundled OrcaSlicer.
- The GUI launches and quits cleanly.

**Outputs:** `dist/OpenBot.app` (703 MB) and `dist/OpenBot-0.1.0-universal2.dmg` (331 MB).

### Still needs the owner (not done autonomously)
1. **Developer ID signing and notarization.** This needs your Apple Developer account.
   `codesign --deep --options runtime --sign "Developer ID Application: …" dist/OpenBot.app`, then `xcrun notarytool submit … --wait` and `xcrun stapler staple`.
2. **Real prints:** an OpenBot-sliced file on the Replicator+, and the Ender-3 Pro over USB and SD (see §7).
3. **A real Raspberry Pi:** flash Pi OS 64-bit, `sudo apt install ./openbot-server_0.1.0_all.deb`, edit `/etc/openbot/server.toml`.
4. **Publishing** the apt repository and the DMG. These are outward-facing, so they're left for you to decide.
