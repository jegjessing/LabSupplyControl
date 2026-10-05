"""Battery charge profiles and a CC-CV charge state machine.

A bench PSU natively does CC then CV; what it *doesn't* do is terminate the
charge. This module adds that: given a chemistry profile and a pack (cells +
capacity), it computes the set-points and runs a small state machine that
reacts to live measurements to taper-terminate (Li) or drop to float (lead-acid).

SAFETY: this is not a substitute for a proper charger. There is no cell
balancing and no temperature sensing here — multi-cell lithium packs need a
BMS/balancer, and all charging should be supervised.

Pure logic only (no serial, no Qt) so it can be unit-tested; the app executes
the actions this module decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Phase(Enum):
    BULK = "Bulk (CC)"
    ABSORPTION = "Absorption (CV)"
    FLOAT = "Float"
    DONE = "Done"


class Action(Enum):
    NONE = "none"
    SET_FLOAT = "set_float"   # lower the voltage set-point to the float level
    STOP = "stop"             # charge complete: turn the output off


@dataclass(frozen=True)
class ChemistryProfile:
    key: str
    name: str
    v_charge_per_cell: float            # CC->CV target (absorption) per cell
    v_max_per_cell: float               # absolute safety ceiling per cell
    default_c_rate: float               # charge current as a fraction of capacity
    v_float_per_cell: float | None = None      # lead-acid float; None -> terminate
    termination_c_rate: float | None = None    # taper cutoff fraction; None -> no auto-stop
    needs_balancer: bool = False
    default_cells: int = 1
    note: str = ""


PROFILES: dict[str, ChemistryProfile] = {
    "leadacid": ChemistryProfile(
        key="leadacid", name="Lead-acid / AGM / gel",
        v_charge_per_cell=2.40, v_float_per_cell=2.275, v_max_per_cell=2.45,
        default_c_rate=0.20, termination_c_rate=0.05, default_cells=6,
        note="CC bulk → 2.40 V/cell absorption → 2.275 V/cell float."),
    "liion": ChemistryProfile(
        key="liion", name="Li-ion (4.20 V/cell)",
        v_charge_per_cell=4.20, v_max_per_cell=4.25,
        default_c_rate=0.50, termination_c_rate=0.10, needs_balancer=True,
        default_cells=1,
        note="CC → 4.20 V/cell CV → stop at ~C/10. Use a BMS for multi-cell."),
    "lifepo4": ChemistryProfile(
        key="lifepo4", name="LiFePO4 (3.65 V/cell)",
        v_charge_per_cell=3.65, v_max_per_cell=3.70,
        default_c_rate=0.50, termination_c_rate=0.10, needs_balancer=True,
        default_cells=1,
        note="CC → 3.65 V/cell CV → stop at ~C/10. Use a BMS for multi-cell."),
}


@dataclass(frozen=True)
class ChargePlan:
    """A concrete charge for one pack, derived from a profile + cells + capacity."""
    profile: ChemistryProfile
    cells: int
    capacity_ah: float
    c_rate: float

    @property
    def target_voltage(self) -> float:
        return round(self.profile.v_charge_per_cell * self.cells, 2)

    @property
    def float_voltage(self) -> float | None:
        if self.profile.v_float_per_cell is None:
            return None
        return round(self.profile.v_float_per_cell * self.cells, 2)

    @property
    def ceiling_voltage(self) -> float:
        return round(self.profile.v_max_per_cell * self.cells, 2)

    @property
    def charge_current(self) -> float:
        return round(self.c_rate * self.capacity_ah, 3)

    @property
    def cutoff_current(self) -> float | None:
        if self.profile.termination_c_rate is None:
            return None
        return round(self.profile.termination_c_rate * self.capacity_ah, 3)

    def fits(self, max_voltage: float, max_current: float) -> bool:
        return self.target_voltage <= max_voltage and self.charge_current <= max_current


class ChargeController:
    """Reacts to each live Measurement and decides the next Action."""

    # Consecutive below-cutoff readings before terminating (debounces noise).
    DEBOUNCE = 5

    def __init__(self, plan: ChargePlan):
        self.plan = plan
        self.phase = Phase.BULK
        self._below = 0

    def update(self, constant_current: bool, current: float) -> Action:
        """Advance the state machine for one measurement; return an Action."""
        if self.phase in (Phase.DONE, Phase.FLOAT):
            return Action.NONE

        if self.phase is Phase.BULK:
            if not constant_current:          # device has entered CV -> absorbing
                self.phase = Phase.ABSORPTION
            return Action.NONE

        # ABSORPTION: watch the current taper toward the cutoff.
        cutoff = self.plan.cutoff_current
        if cutoff is None:
            return Action.NONE
        if current > cutoff:
            self._below = 0
            return Action.NONE
        self._below += 1
        if self._below < self.DEBOUNCE:
            return Action.NONE
        if self.plan.float_voltage is not None:
            self.phase = Phase.FLOAT
            return Action.SET_FLOAT
        self.phase = Phase.DONE
        return Action.STOP
