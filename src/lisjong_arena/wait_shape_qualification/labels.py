"""Canonical Stage-M0 wait-shape targets for lisbun/lisjong-arena#322.

Arena owns only the bounded projection and eligibility contract. Structural wait and
mechanism semantics remain owned by
`lisjong.belief.exact_wait_ground_truth.exact_hand_belief_with_waits()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from lisjong.belief import SCALE
from lisjong.belief.exact_wait_ground_truth import exact_hand_belief_with_waits
from lisjong.belief.hand_belief import HandBelief
from lisjong.policy_contract.meld import PublicMeld
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.tile import Tile

from lisjong_arena.stage_a0_tenpai_feasibility.labels import (
    is_stable_thirteen_equivalent,
    violates_physical_inventory,
)
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import PrivilegedTargetCell

from .protocol import PUBLIC_RIICHI_ELIGIBILITY

_TILE_KIND_COUNT = 34


class WaitShapeQualificationError(ValueError):
    """A #322 target cannot be interpreted without violating the locked contract."""


class WaitShapeAvailability(Enum):
    """Exactly one availability classification for an opponent target."""

    AVAILABLE = "AVAILABLE"
    NOT_ACCEPTED_RIICHI = "NOT_ACCEPTED_RIICHI"
    HIDDEN_HAND_UNAVAILABLE = "HIDDEN_HAND_UNAVAILABLE"
    MELD_STATE_UNAVAILABLE = "MELD_STATE_UNAVAILABLE"
    INVALID_PHYSICAL_INVENTORY = "INVALID_PHYSICAL_INVENTORY"
    NOT_STABLE_13_EQUIVALENT = "NOT_STABLE_13_EQUIVALENT"
    NO_STRUCTURAL_WAIT = "NO_STRUCTURAL_WAIT"
    OTHER_FAIL_CLOSED = "OTHER_FAIL_CLOSED"


@dataclass(frozen=True, slots=True)
class WaitShapeProjection:
    """Deterministic coarse projection of canonical Level-2 mechanism truth."""

    tanki: int
    shanpon: int
    kanchan: int
    penchan: int
    ryanmen: int
    kokushi: int

    def __post_init__(self) -> None:
        for field_name in (
            "tanki",
            "shanpon",
            "kanchan",
            "penchan",
            "ryanmen",
            "kokushi",
        ):
            if getattr(self, field_name) not in (0, 1):
                raise WaitShapeQualificationError(
                    f"{field_name} must be an exact binary label"
                )

    @property
    def ordinary(self) -> tuple[int, int, int, int, int]:
        return (
            self.tanki,
            self.shanpon,
            self.kanchan,
            self.penchan,
            self.ryanmen,
        )

    @property
    def has_any_wait_shape(self) -> bool:
        return any((*self.ordinary, self.kokushi))

    @property
    def is_kokushi_only(self) -> bool:
        return not any(self.ordinary) and self.kokushi == 1


@dataclass(frozen=True, slots=True)
class WaitShapeTarget:
    """One public-riichi opponent target or its fail-closed unavailable reason."""

    availability: WaitShapeAvailability
    projection: WaitShapeProjection | None

    def __post_init__(self) -> None:
        if not isinstance(self.availability, WaitShapeAvailability):
            raise TypeError("availability must be a WaitShapeAvailability")
        if self.availability is WaitShapeAvailability.AVAILABLE:
            if not isinstance(self.projection, WaitShapeProjection):
                raise WaitShapeQualificationError(
                    "an AVAILABLE target must carry a wait-shape projection"
                )
            return
        if self.projection is not None:
            raise WaitShapeQualificationError(
                "an unavailable target must never carry a wait-shape projection"
            )


def _exact_binary_table(belief: HandBelief, field_name: str) -> tuple[int, ...]:
    values = getattr(belief, field_name)
    if values is None:
        raise WaitShapeQualificationError(
            f"{field_name} is unavailable; canonical Level-2 truth is required"
        )
    if len(values) != _TILE_KIND_COUNT:
        raise WaitShapeQualificationError(
            f"{field_name} must contain exactly {_TILE_KIND_COUNT} values"
        )
    if any(value not in (0, SCALE) for value in values):
        raise WaitShapeQualificationError(
            f"{field_name} must contain only exact 0/SCALE ground-truth values"
        )
    return values


def project_exact_wait_shapes(belief: HandBelief) -> WaitShapeProjection:
    """Project exact Level-2 canonical truth to five ordinary shapes + KOKUSHI."""

    if not isinstance(belief, HandBelief):
        raise TypeError("belief must be a HandBelief")

    wait = _exact_binary_table(belief, "wait_probability_raw")
    tanki = _exact_binary_table(belief, "tanki_wait_probability_raw")
    shanpon = _exact_binary_table(belief, "shanpon_wait_probability_raw")
    kanchan = _exact_binary_table(belief, "kanchan_wait_probability_raw")
    penchan = _exact_binary_table(belief, "penchan_wait_probability_raw")
    ryanmen_low = _exact_binary_table(
        belief, "ryanmen_low_side_probability_raw"
    )
    ryanmen_high = _exact_binary_table(
        belief, "ryanmen_high_side_probability_raw"
    )
    kokushi = _exact_binary_table(belief, "kokushi_wait_probability_raw")

    mechanisms = (
        tanki,
        shanpon,
        kanchan,
        penchan,
        ryanmen_low,
        ryanmen_high,
        kokushi,
    )
    for index, wait_value in enumerate(wait):
        expected = SCALE if any(table[index] == SCALE for table in mechanisms) else 0
        if wait_value != expected:
            raise WaitShapeQualificationError(
                "canonical wait_probability_raw must equal the existential OR "
                "of all mechanism tables"
            )

    return WaitShapeProjection(
        tanki=int(any(value == SCALE for value in tanki)),
        shanpon=int(any(value == SCALE for value in shanpon)),
        kanchan=int(any(value == SCALE for value in kanchan)),
        penchan=int(any(value == SCALE for value in penchan)),
        ryanmen=int(
            any(value == SCALE for value in ryanmen_low)
            or any(value == SCALE for value in ryanmen_high)
        ),
        kokushi=int(any(value == SCALE for value in kokushi)),
    )


def build_wait_shape_target(
    *,
    public_riichi: RiichiState,
    privileged_riichi_declared: bool,
    concealed_tiles: tuple[Tile, ...] | None,
    melds: tuple[PublicMeld, ...] | None,
) -> WaitShapeTarget:
    """Build one #322 target without changing the #258 Tenpai label path."""

    if not isinstance(public_riichi, RiichiState):
        raise TypeError("public_riichi must be a RiichiState")
    if type(privileged_riichi_declared) is not bool:
        raise TypeError("privileged_riichi_declared must be an exact bool")

    def unavailable(availability: WaitShapeAvailability) -> WaitShapeTarget:
        return WaitShapeTarget(availability=availability, projection=None)

    if public_riichi is not PUBLIC_RIICHI_ELIGIBILITY:
        return unavailable(WaitShapeAvailability.NOT_ACCEPTED_RIICHI)
    if not privileged_riichi_declared:
        raise WaitShapeQualificationError(
            "public ACCEPTED riichi must bind to privileged riichi_declared=True"
        )
    if concealed_tiles is None:
        return unavailable(WaitShapeAvailability.HIDDEN_HAND_UNAVAILABLE)
    if melds is None:
        return unavailable(WaitShapeAvailability.MELD_STATE_UNAVAILABLE)
    if violates_physical_inventory(concealed_tiles, melds):
        return unavailable(WaitShapeAvailability.INVALID_PHYSICAL_INVENTORY)
    if not is_stable_thirteen_equivalent(concealed_tiles, melds):
        return unavailable(WaitShapeAvailability.NOT_STABLE_13_EQUIVALENT)

    try:
        belief = exact_hand_belief_with_waits(concealed_tiles, melds)
        projection = project_exact_wait_shapes(belief)
    except ValueError:
        return unavailable(WaitShapeAvailability.OTHER_FAIL_CLOSED)

    if not projection.has_any_wait_shape:
        return unavailable(WaitShapeAvailability.NO_STRUCTURAL_WAIT)
    return WaitShapeTarget(
        availability=WaitShapeAvailability.AVAILABLE,
        projection=projection,
    )


def build_wait_shape_target_from_retained_cell(
    cell: PrivilegedTargetCell,
) -> WaitShapeTarget:
    """Reuse a #258 retained sidecar cell as #317 F0 technical input."""

    if not isinstance(cell, PrivilegedTargetCell):
        raise TypeError("cell must be a PrivilegedTargetCell")
    return build_wait_shape_target(
        public_riichi=cell.public_riichi_state,
        privileged_riichi_declared=cell.privileged_riichi_declared,
        concealed_tiles=cell.concealed_tiles,
        melds=cell.melds,
    )


__all__ = [
    "WaitShapeAvailability",
    "WaitShapeProjection",
    "WaitShapeQualificationError",
    "WaitShapeTarget",
    "build_wait_shape_target",
    "build_wait_shape_target_from_retained_cell",
    "project_exact_wait_shapes",
]
