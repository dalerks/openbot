"""Find USB serial ports that look like 3D printers."""

from dataclasses import dataclass

# USB-serial chips used on Creality and similar boards.
KNOWN_CHIPS = {
    (0x1A86, 0x7523): "CH340 (Creality 4.2.x / Melzi)",
    (0x1A86, 0x55D4): "CH9102",
    (0x10C4, 0xEA60): "CP2102",
    (0x0403, 0x6001): "FTDI",
    (0x0483, 0x5740): "STM32 virtual COM",
    (0x2341, 0x0042): "Arduino Mega 2560",
    (0x2A03, 0x0042): "Arduino Mega 2560",
}


@dataclass
class SerialPort:
    device: str
    description: str
    likely_printer: bool


def list_ports():
    from serial.tools import list_ports as lp
    out = []
    for p in lp.comports():
        dev = p.device
        # macOS exposes each port as /dev/tty.* and /dev/cu.*; cu.* doesn't block on open.
        if dev.startswith("/dev/tty.") and dev.replace("/dev/tty.", "/dev/cu.") != dev:
            continue
        if any(s in dev for s in ("Bluetooth", "debug-console")):
            continue
        chip = KNOWN_CHIPS.get((p.vid, p.pid)) if p.vid else None
        likely = chip is not None or any(s in dev.lower() for s in
                                         ("usbserial", "wchusbserial", "usbmodem", "ttyusb",
                                          "ttyacm"))
        desc = chip or p.description or "serial port"
        out.append(SerialPort(dev, desc, likely))
    return sorted(out, key=lambda s: (not s.likely_printer, s.device))
