"""High-level driver for Nice-Power / KUAIQU single-channel supplies.

`KuaiquPSU` owns a pyserial connection and exposes intent-level methods
(set_voltage, read_measurements, ...) over the ASCII '<...>' protocol in
``protocol.py``. All port access is serialized with a lock so a background
poller and GUI callbacks can share one instance safely.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass

import serial
from serial.tools import list_ports

from . import protocol as p
from .protocol import ProtocolError


@dataclass
class Measurement:
    """A single snapshot of the supply's live output."""
    voltage: float          # measured output volts
    current: float          # measured output amps
    constant_current: bool  # True = CC mode, False = CV mode

    @property
    def power(self) -> float:
        return self.voltage * self.current


@dataclass
class SerialPort:
    device: str
    description: str

    def __str__(self) -> str:
        return f"{self.device} — {self.description}"


def list_serial_ports(
    name_filter: str | Iterable[str] | None = None,
) -> list[SerialPort]:
    """Return available serial ports (USB-serial adapters show up here).

    ``name_filter`` may be a single term or an iterable of terms. A port is
    kept if *any* term (case-insensitive) appears in its device name or USB
    identity (description, product, manufacturer, hwid). Nice-Power adapters
    enumerate by their USB-serial bridge chip (e.g. a Silicon Labs CP2102),
    not by "SPS", so the filter needs a few terms to catch them.
    """
    if isinstance(name_filter, str):
        terms = [name_filter]
    elif name_filter is None:
        terms = []
    else:
        terms = list(name_filter)
    terms = [term.lower() for term in terms if term]

    ports = []
    for info in list_ports.comports():
        parts = [info.device, info.name, info.description,
                 info.manufacturer, info.product, info.hwid]
        haystack = " ".join(part for part in parts if part).lower()
        if terms and not any(term in haystack for term in terms):
            continue
        description = info.description or info.product or "serial port"
        ports.append(SerialPort(info.device, description))
    return sorted(ports, key=lambda pt: pt.device)


class KuaiquPSU:
    """Talks the ASCII protocol to one supply. Every method takes the lock."""

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 0.6):
        self._lock = threading.Lock()
        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            write_timeout=timeout,
        )

    def close(self) -> None:
        with self._lock:
            if self._serial.is_open:
                self._serial.close()

    @property
    def is_open(self) -> bool:
        return self._serial.is_open

    # --- Transport ----------------------------------------------------------

    def _command(self, frame: str) -> str:
        """Send one frame and return the device's reply (read up to '>').

        Returns '' if the device doesn't answer within the timeout (some
        commands, e.g. output on/off, are not acknowledged).
        """
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write(frame.encode("ascii"))
            raw = self._serial.read_until(p.END.encode("ascii"))
            return raw.decode("ascii", "replace")

    # --- Session ------------------------------------------------------------

    def set_remote(self, remote: bool) -> None:
        """Announce connect/disconnect. Connect switches the device to remote
        (the front panel locks); disconnect hands control back to the panel."""
        self._command(p.FRAME_CONNECT if remote else p.FRAME_DISCONNECT)

    # --- Output -------------------------------------------------------------

    def set_output(self, on: bool) -> None:
        self._command(p.FRAME_OUTPUT_ON if on else p.FRAME_OUTPUT_OFF)

    # --- Set-points ---------------------------------------------------------

    def set_voltage(self, volts: float) -> None:
        self._command(p.build_set_frame(p.CMD_SET_VOLTAGE, volts))

    def set_current(self, amps: float) -> None:
        self._command(p.build_set_frame(p.CMD_SET_CURRENT, amps))

    def set_voltage_current(self, volts: float, amps: float) -> None:
        self.set_voltage(volts)
        self.set_current(amps)

    # --- Readback -----------------------------------------------------------

    def read_measurements(self) -> Measurement:
        voltage = p.parse_voltage(self._command(p.FRAME_READ_VOLTAGE))
        constant_current, current = p.parse_current(self._command(p.FRAME_READ_CURRENT))
        return Measurement(
            voltage=voltage, current=current, constant_current=constant_current)
