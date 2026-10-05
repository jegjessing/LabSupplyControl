#!/usr/bin/env python3
"""Confirm the supply speaks the ASCII '<...>' protocol (not Modbus).

Per the Nice-Power / KUAIQU 'Serial-port-communication-protocol VER 02' and the
working PowerLinkESP firmware, this family uses plain ASCII frames:

    < CC DDDDDD AAA >      command(2) + value*1000 (6 digits) + address(3)

This sends a connect frame and then a few read-voltage / read-current variants
and prints whatever comes back. A reply like <12......000> or <14......000>
(or C4......) confirms the protocol — then we rewrite the driver for real.

    python3 diagnose.py
"""

from __future__ import annotations

import time

import serial
from serial.tools import list_ports

CONNECT = "<09100000000>"
READ_CMDS = [
    ("read voltage — manual code 02", "<02000000000>"),
    ("read voltage — PowerLinkESP code 12", "<12000000000>"),
    ("read current — manual code 04", "<04000000000>"),
    ("read current — PowerLinkESP code 14", "<14000000000>"),
]


def pick_port() -> str | None:
    ports = list_ports.comports()
    for info in ports:
        parts = (info.device, info.description, info.manufacturer, info.hwid)
        hay = " ".join(x for x in parts if x).lower()
        if any(t in hay for t in ("cp2102", "cp210x", "10c4", "silicon lab",
                                   "sps", "kuaiqu")):
            return info.device
    return ports[0].device if ports else None


def exchange(conn: serial.Serial, frame: str) -> bytes:
    conn.reset_input_buffer()
    conn.write(frame.encode("ascii"))
    time.sleep(0.25)
    return conn.read(64)  # returns after the timeout with whatever arrived


def main() -> int:
    port = pick_port()
    if not port:
        print("No serial port found.")
        return 1
    print(f"Port: {port} @ 9600 8N1\n")

    conn = serial.Serial(port, 9600, bytesize=serial.EIGHTBITS,
                         parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                         timeout=1.0, write_timeout=1.0)
    time.sleep(0.3)
    try:
        print(f"PC-> {CONNECT}   (connect / enter remote)")
        reply = exchange(conn, CONNECT)
        print(f"  <- {reply.decode('ascii', 'replace')!r}   raw={reply.hex()}\n")

        for label, cmd in READ_CMDS:
            reply = exchange(conn, cmd)
            print(f"PC-> {cmd}   ({label})")
            print(f"  <- {reply.decode('ascii', 'replace')!r}   raw={reply.hex()}")
    finally:
        conn.close()

    print("\nIf any reply above looks like <12......> / <14......> / <C4......>, "
          "the ASCII protocol is confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
