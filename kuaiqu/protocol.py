"""ASCII serial protocol for Nice-Power / KUAIQU programmable supplies.

These supplies do NOT speak Modbus. They use a fixed-length ASCII frame:

    < CC DDDDDD AAA >
      │  │      └── 3-digit device address (we send 000; the device answers
      │  │          regardless and echoes its own address in replies)
      │  └───────── 6-digit value = volts/amps * 1000, zero-padded
      └──────────── 2-char command code

No checksum. 9600 8N1. Confirmed against the PowerLinkESP firmware and the
vendor's 'Serial-port-communication-protocol VER 02' document.

Command codes (PC -> device):
    01 set voltage      02 read voltage      03 set current    04 read current
    07 output on        08 output off        091 connect       092 disconnect

Replies (device -> PC):
    <11OK0000AAA> set-voltage ack      <13OK0000AAA> set-current ack
    <19OK0000AAA> connect ack
    <12VVVVVVAAA> voltage = VVVVVV/1000
    <14AAAAAAAAA> current = AAAAAA/1000, CV mode   (code starts '1')
    <C4AAAAAAAAA> current, CC mode                 (code starts 'C')
"""

from __future__ import annotations

START = "<"
END = ">"
ADDRESS = "000"        # device answers frames addressed to 000 and echoes its own

CMD_SET_VOLTAGE = "01"
CMD_READ_VOLTAGE = "02"
CMD_SET_CURRENT = "03"
CMD_READ_CURRENT = "04"

FRAME_CONNECT = "<09100000000>"
FRAME_DISCONNECT = "<09200000000>"
FRAME_OUTPUT_ON = "<07000000000>"
FRAME_OUTPUT_OFF = "<08000000000>"
FRAME_READ_VOLTAGE = "<02000000000>"
FRAME_READ_CURRENT = "<04000000000>"


class ProtocolError(Exception):
    """A reply was missing, malformed, or unexpected."""


def build_set_frame(command: str, value: float) -> str:
    """Build a set-voltage/current frame; ``value`` is volts or amps."""
    milli = round(value * 1000)
    if not 0 <= milli <= 999999:
        raise ProtocolError(f"value {value} out of range")
    return f"{START}{command}{milli:06d}{ADDRESS}{END}"


def _inner(reply: str) -> str:
    """Validate the <...> envelope and return the 11 characters inside it."""
    reply = reply.strip()
    if len(reply) != 13 or reply[0] != START or reply[-1] != END:
        raise ProtocolError(f"malformed reply: {reply!r}")
    return reply[1:-1]


def parse_voltage(reply: str) -> float:
    """Volts from a read-voltage reply (code '12')."""
    inner = _inner(reply)
    if inner[1] != "2":
        raise ProtocolError(f"not a voltage reply: {reply!r}")
    return _value(inner, reply)


def parse_current(reply: str) -> tuple[bool, float]:
    """(constant_current, amps) from a read-current reply ('14' CV / 'C4' CC)."""
    inner = _inner(reply)
    if inner[1] != "4":
        raise ProtocolError(f"not a current reply: {reply!r}")
    return inner[0] == "C", _value(inner, reply)


def _value(inner: str, reply: str) -> float:
    try:
        return int(inner[2:8]) / 1000.0
    except ValueError:
        raise ProtocolError(f"non-numeric value in {reply!r}")
