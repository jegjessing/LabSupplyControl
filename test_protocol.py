"""Protocol tests — no hardware required.

Validates the ASCII '<...>' frame builders and reply parsers, plus a fake-serial
round-trip through KuaiquPSU. Run: python3 test_protocol.py
"""

from __future__ import annotations

from kuaiqu import protocol as p


def test_build_set_frames():
    # Set 4.58 V -> <01 004580 000>
    assert p.build_set_frame(p.CMD_SET_VOLTAGE, 4.58) == "<01004580000>"
    # Set 6.92 A -> <03 006920 000>
    assert p.build_set_frame(p.CMD_SET_CURRENT, 6.92) == "<03006920000>"
    # Rounding and zero-padding
    assert p.build_set_frame(p.CMD_SET_VOLTAGE, 12.1) == "<01012100000>"
    assert p.build_set_frame(p.CMD_SET_VOLTAGE, 0) == "<01000000000>"


def test_build_set_frame_rejects_out_of_range():
    for bad in (-0.1, 1000.0):
        try:
            p.build_set_frame(p.CMD_SET_VOLTAGE, bad)
        except p.ProtocolError:
            pass
        else:
            raise AssertionError(f"expected ProtocolError for {bad}")


def test_parse_voltage():
    assert abs(p.parse_voltage("<12004580001>") - 4.58) < 1e-9
    assert abs(p.parse_voltage("<12000000001>") - 0.0) < 1e-9


def test_parse_current_cv_and_cc():
    cc, amps = p.parse_current("<14000183000>")
    assert cc is False and abs(amps - 0.183) < 1e-9
    cc, amps = p.parse_current("<C4000001001>")
    assert cc is True and abs(amps - 0.001) < 1e-9


def test_parse_rejects_malformed():
    for bad in ("", "<12>", "12004580001", "<12004580001", "<xx004580001>"):
        try:
            p.parse_voltage(bad)
        except p.ProtocolError:
            pass
        else:
            raise AssertionError(f"expected ProtocolError for {bad!r}")


def test_fake_serial_roundtrip():
    """Drive KuaiquPSU against an in-memory serial that emulates the device."""
    from kuaiqu import psu as psu_mod

    class FakeSerial:
        """Minimal device simulator speaking the ASCII protocol."""

        is_open = True

        def __init__(self, *_, **__):
            self._rx = b""
            self.voltage = 4.58
            self.current = 0.183

        def write(self, data):
            frame = data.decode("ascii")
            code = frame[1:3]
            if code == p.CMD_READ_VOLTAGE:        # 02 -> reply 12
                milli = round(self.voltage * 1000)
                self._rx = f"<12{milli:06d}000>".encode("ascii")
            elif code == p.CMD_READ_CURRENT:      # 04 -> reply 14 (CV)
                milli = round(self.current * 1000)
                self._rx = f"<14{milli:06d}000>".encode("ascii")
            elif code == p.CMD_SET_VOLTAGE:
                self.voltage = int(frame[3:9]) / 1000.0
                self._rx = b"<11OK0000000>"
            elif code == p.CMD_SET_CURRENT:
                self.current = int(frame[3:9]) / 1000.0
                self._rx = b"<13OK0000000>"
            else:                                 # connect / output etc.
                self._rx = b"<19OK0000000>"
            return len(data)

        def read_until(self, terminator):
            end = self._rx.find(terminator)
            if end == -1:
                chunk, self._rx = self._rx, b""
                return chunk
            chunk, self._rx = self._rx[:end + 1], self._rx[end + 1:]
            return chunk

        def reset_input_buffer(self):
            pass

        def close(self):
            self.is_open = False

    original = psu_mod.serial.Serial
    psu_mod.serial.Serial = FakeSerial
    try:
        dev = psu_mod.KuaiquPSU("fake")
        dev.set_remote(True)
        dev.set_voltage_current(5.0, 2.0)
        m = dev.read_measurements()
        assert abs(m.voltage - 5.0) < 1e-4
        assert abs(m.current - 2.0) < 1e-4
        assert m.constant_current is False
        assert abs(m.power - 10.0) < 1e-3
        dev.close()
    finally:
        psu_mod.serial.Serial = original


def test_charge_plan_math():
    import charging
    plan = charging.ChargePlan(charging.PROFILES["liion"], cells=3, capacity_ah=2.0,
                               c_rate=0.5)
    assert plan.target_voltage == 12.60          # 4.20 * 3
    assert plan.charge_current == 1.0            # 0.5C of 2 Ah
    assert plan.cutoff_current == 0.2            # C/10
    assert plan.float_voltage is None
    assert plan.fits(30, 5) and not plan.fits(12, 5)


def test_charge_lithium_terminates():
    import charging
    plan = charging.ChargePlan(charging.PROFILES["liion"], 1, 2.0, 0.5)
    ctrl = charging.ChargeController(plan)
    assert ctrl.update(constant_current=True, current=1.0) is charging.Action.NONE
    assert ctrl.phase is charging.Phase.BULK
    # device drops to CV -> absorption
    assert ctrl.update(constant_current=False, current=0.9) is charging.Action.NONE
    assert ctrl.phase is charging.Phase.ABSORPTION
    # taper below cutoff (0.2 A) for DEBOUNCE readings -> STOP
    actions = [ctrl.update(False, 0.1) for _ in range(charging.ChargeController.DEBOUNCE)]
    assert actions[-1] is charging.Action.STOP
    assert ctrl.phase is charging.Phase.DONE


def test_charge_leadacid_floats():
    import charging
    plan = charging.ChargePlan(charging.PROFILES["leadacid"], 6, 7.0, 0.2)
    ctrl = charging.ChargeController(plan)
    ctrl.update(constant_current=True, current=1.4)          # bulk
    ctrl.update(constant_current=False, current=1.0)         # -> absorption
    actions = [ctrl.update(False, 0.01) for _ in range(charging.ChargeController.DEBOUNCE)]
    assert actions[-1] is charging.Action.SET_FLOAT
    assert ctrl.phase is charging.Phase.FLOAT
    assert plan.float_voltage == round(2.275 * 6, 2)


def test_sequence_parse():
    import sequence
    text = (
        "time,output,voltage,current\n"
        "00:00:00,on,5.0,1.0\n"
        "# a comment\n"
        "\n"
        "00:00:30,off\n"
        "60,on\n"
        "00:01:30,on,12.0,0.5\n"
    )
    steps = sequence.parse_sequence(text, 30, 5)
    assert len(steps) == 4
    assert steps[0].time_s == 0 and steps[0].output and steps[0].voltage == 5.0
    assert steps[1].time_s == 30 and steps[1].output is False
    assert steps[1].voltage is None and steps[1].current is None
    assert steps[2].time_s == 60 and steps[2].output and steps[2].voltage is None
    assert steps[3].time_s == 90 and steps[3].voltage == 12.0


def test_sequence_sorts_and_validates():
    import sequence
    steps = sequence.parse_sequence("30,off\n0,on,5,1\n", 30, 5)
    assert [s.time_s for s in steps] == [0, 30]        # sorted by time
    for bad in ("0,on,99,1", "0,maybe", "x,on", ""):
        try:
            sequence.parse_sequence(bad, 30, 5)
        except sequence.SequenceError:
            pass
        else:
            raise AssertionError(f"expected SequenceError for {bad!r}")


def test_parse_time_formats():
    import sequence
    assert sequence.parse_time("90") == 90
    assert sequence.parse_time("01:30") == 90
    assert sequence.parse_time("01:01:30") == 3690


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run()
