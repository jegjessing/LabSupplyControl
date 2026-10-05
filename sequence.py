"""Parse a CSV timed-action sequence for the supply.

Columns: time, output, voltage, current

    time     HH:MM:SS, MM:SS, or plain seconds — elapsed from sequence start
    output   on / off
    voltage  optional volts (blank = keep the current set-point)
    current  optional amps  (blank = keep the current set-point)

A header row is optional; blank lines and '#' comments are ignored. Steps are
returned sorted by time. Example:

    time,output,voltage,current
    00:00:00,on,5.0,1.0
    00:00:30,off
    00:01:00,on
    00:01:30,on,12.0,0.5

Pure logic (no Qt/serial) so it can be unit-tested.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


class SequenceError(Exception):
    """The CSV could not be parsed into a valid sequence."""


@dataclass(frozen=True)
class Step:
    time_s: float
    output: bool
    voltage: float | None
    current: float | None


def parse_time(text: str) -> float:
    """Seconds from 'HH:MM:SS', 'MM:SS', or a plain number."""
    text = text.strip()
    parts = text.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise SequenceError(f"bad time {text!r}")


def _parse_output(text: str) -> bool:
    value = text.strip().lower()
    if value in ("on", "1", "true", "yes"):
        return True
    if value in ("off", "0", "false", "no"):
        return False
    raise SequenceError(f"bad output {text!r} (use on/off)")


def _parse_optional(text: str, label: str, maximum: float) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise SequenceError(f"bad {label} {text!r}")
    if not 0 <= value <= maximum:
        raise SequenceError(f"{label} {value} out of range 0..{maximum:g}")
    return value


def parse_sequence(text: str, max_voltage: float, max_current: float) -> list[Step]:
    steps: list[Step] = []
    first_content = True
    for lineno, row in enumerate(csv.reader(io.StringIO(text)), 1):
        cells = [c.strip() for c in row]
        if not any(cells) or cells[0].startswith("#"):
            continue
        if first_content:
            first_content = False
            try:                       # skip an optional header row
                parse_time(cells[0])
            except SequenceError:
                continue
        if len(cells) < 2:
            raise SequenceError(f"line {lineno}: need at least time,output")
        try:
            step = Step(
                time_s=parse_time(cells[0]),
                output=_parse_output(cells[1]),
                voltage=_parse_optional(cells[2], "voltage", max_voltage)
                if len(cells) > 2 else None,
                current=_parse_optional(cells[3], "current", max_current)
                if len(cells) > 3 else None,
            )
        except SequenceError as exc:
            raise SequenceError(f"line {lineno}: {exc}")
        steps.append(step)
    if not steps:
        raise SequenceError("no steps found")
    steps.sort(key=lambda s: s.time_s)
    return steps
