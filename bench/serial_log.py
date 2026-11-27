#!/usr/bin/env python3
"""Reset the board and print its serial log for a while, with the Wi-Fi SSID redacted.

  bench/serial_log.py [--port /dev/ttyACM0] [--seconds 20] [--no-reset] [--out file]

Needs pyserial (the ESP-IDF Python environment has it). Prints the first IPv4 it sees as
"IP: a.b.c.d" on the last line.
"""

import argparse
import os
import re
import time
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parent.parent


def secrets():
    vals = [os.environ.get("WIFI_SSID"), os.environ.get("WIFI_PASSWORD")]
    env = ROOT / "wifi.env"
    if env.exists():
        for line in env.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?WIFI_(?:SSID|PASSWORD)=(['\"]?)(.*)\1\s*$", line)
            if m:
                vals.append(m.group(2))
    return [v for v in vals if v]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()

    hide = secrets()
    s = serial.Serial(a.port, 115200, timeout=0.2)
    if not a.no_reset:
        # USB-Serial-JTAG: RTS low with DTR high resets into the app.
        s.dtr, s.rts = False, True
        time.sleep(0.1)
        s.rts = False
    out = open(a.out, "w") if a.out else None
    ip, end, buf = None, time.monotonic() + a.seconds, b""
    while time.monotonic() < end:
        buf += s.read(4096)
        *lines, buf = buf.split(b"\n")
        for raw in lines:
            line = raw.decode(errors="replace").rstrip()
            for h in hide:
                line = line.replace(h, "<redacted>")
            line = re.sub(r"\x1b\[[0-9;]*m", "", line)
            print(line, flush=True)
            if out:
                print(line, file=out, flush=True)
            m = re.search(r"(?:ip:|IP:|address:|Got IP:)\s*(\d+\.\d+\.\d+\.\d+)", line)
            if m and not ip and not m.group(1).startswith("0."):
                ip = m.group(1)
    print(f"IP: {ip}")


if __name__ == "__main__":
    main()
