"""Offline Q macro-transition dataset row value type and its hard invariants.

1 rowは「1つのeligible ordinary-discard decisionから、同一seat・同一roundの
次のeligible ordinary-discard decision、またはround terminalまでの
macro-transition」を表すimmutable valueである。

```text
eligible discard state s_t
    -- yakuhai-call selected discard -->
    zero or more opponent / scaffold / ineligible decisions
    -->
next eligible discard state s_{t+1} for the same seat
or round terminal
```

Stage 2の`Stage2DecisionRow`（`arena-learned-policy-stage2-dataset-v1`相当）は
変更せず、このrowは独立したversioned schema
（`arena-learned-policy-offlineq-transition-v1`）を持つ。

hidden opponent hand、wall truth、future state、future outcome、oracle
information、teacher-internal analysisはrowへ入れない。`reward`と
`terminal`はtraining label / transitionとしてのみ保持し、`feature_values`
（runtime feature）へは漏らさない。
"""

import math
from dataclasses import dataclass

from .errors import OfflineQTransitionError
from .protocol import FEATURE_DIMENSION, VOCABULARY_SIZE, Split, action_family

TRANSITION_SCHEMA_VERSION = "arena-learned-policy-offlineq-transition-v1"

_FLOAT32_MAX = float.fromhex("0x1.fffffep+127")


def _require_feature_values(values: object, field_name: str) -> None:
    if type(values) is not tuple:
        raise TypeError(f"{field_name} must be an exact tuple")
    if len(values) != FEATURE_DIMENSION:
        raise OfflineQTransitionError(
            f"{field_name} dimension must be {FEATURE_DIMENSION}; got {len(values)}"
        )
    if any(type(value) is not float for value in values):
        raise TypeError(f"{field_name} must contain only exact floats")
    if any(not math.isfinite(value) or abs(value) > _FLOAT32_MAX for value in values):
        raise OfflineQTransitionError(f"{field_name} must all be finite float32")


def _require_legal_mask(mask: object, field_name: str) -> None:
    if type(mask) is not tuple:
        raise TypeError(f"{field_name} must be an exact tuple")
    if len(mask) != VOCABULARY_SIZE:
        raise OfflineQTransitionError(
            f"{field_name} dimension must be {VOCABULARY_SIZE}; got {len(mask)}"
        )
    if any(type(value) is not bool for value in mask):
        raise TypeError(f"{field_name} must contain only exact bools")
    if not any(mask):
        raise OfflineQTransitionError(f"{field_name} must contain at least one action")


@dataclass(frozen=True, slots=True)
class MacroTransitionRow:
    """1つのeligible ordinary-discard macro-transitionのversioned dataset row。"""

    seed: int
    split: Split
    round_ordinal: int
    round_wind: str
    hand_number: int
    honba: int
    actor_seat: int

    step_ordinal: int
    decision_ordinal: int

    feature_values: tuple[float, ...]
    legal_mask: tuple[bool, ...]
    behavior_action_index: int
    behavior_action_family: str

    reward: float
    terminal: bool

    next_step_ordinal: int | None
    next_decision_ordinal: int | None
    next_feature_values: tuple[float, ...] | None
    next_legal_mask: tuple[bool, ...] | None

    def __post_init__(self) -> None:
        for name in (
            "seed",
            "round_ordinal",
            "hand_number",
            "honba",
            "actor_seat",
            "step_ordinal",
            "decision_ordinal",
            "behavior_action_index",
        ):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an int")
        for name in ("round_ordinal", "step_ordinal", "decision_ordinal", "honba"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if not isinstance(self.split, Split):
            raise TypeError("split must be a Split")
        if type(self.round_wind) is not str or not self.round_wind:
            raise TypeError("round_wind must be a non-empty str")
        if not 1 <= self.hand_number <= 4:
            raise ValueError("hand_number must be in 1..4")
        if not 0 <= self.actor_seat <= 3:
            raise ValueError("actor_seat must be in 0..3")

        _require_feature_values(self.feature_values, "feature_values")
        _require_legal_mask(self.legal_mask, "legal_mask")

        if not 0 <= self.behavior_action_index < VOCABULARY_SIZE:
            raise OfflineQTransitionError(
                f"behavior action index must be in range(0, {VOCABULARY_SIZE})"
            )
        if not self.legal_mask[self.behavior_action_index]:
            raise OfflineQTransitionError(
                "behavior action index is not legal in this decision"
            )
        expected_family = action_family(self.behavior_action_index)
        if self.behavior_action_family != expected_family:
            raise OfflineQTransitionError(
                "behavior action family does not match the vocabulary block"
            )
        if self.legal_action_count < 2:
            raise OfflineQTransitionError(
                "a macro-transition source decision must be a choice decision "
                "(len(legal_actions) >= 2)"
            )

        if type(self.reward) is not float or not math.isfinite(self.reward):
            raise OfflineQTransitionError("reward must be a finite float")
        if type(self.terminal) is not bool:
            raise TypeError("terminal must be an exact bool")

        next_fields = (
            self.next_step_ordinal,
            self.next_decision_ordinal,
            self.next_feature_values,
            self.next_legal_mask,
        )
        if self.terminal:
            if any(value is not None for value in next_fields):
                raise OfflineQTransitionError(
                    "a terminal transition must not carry a next eligible state"
                )
            return

        if any(value is None for value in next_fields):
            raise OfflineQTransitionError(
                "a nonterminal transition must carry exactly one next eligible state"
            )
        if type(self.next_step_ordinal) is not int or self.next_step_ordinal < 0:
            raise OfflineQTransitionError(
                "next_step_ordinal must be a non-negative int"
            )
        if type(self.next_decision_ordinal) is not int:
            raise OfflineQTransitionError("next_decision_ordinal must be an int")
        if self.next_decision_ordinal <= self.decision_ordinal:
            raise OfflineQTransitionError(
                "next_decision_ordinal must be strictly later than decision_ordinal"
            )
        if self.next_step_ordinal < self.step_ordinal:
            raise OfflineQTransitionError(
                "next_step_ordinal must not precede step_ordinal"
            )
        _require_feature_values(self.next_feature_values, "next_feature_values")
        _require_legal_mask(self.next_legal_mask, "next_legal_mask")

    @property
    def legal_action_count(self) -> int:
        return sum(self.legal_mask)

    @property
    def legal_action_indices(self) -> tuple[int, ...]:
        return tuple(index for index, legal in enumerate(self.legal_mask) if legal)

    @property
    def next_legal_action_indices(self) -> tuple[int, ...] | None:
        if self.next_legal_mask is None:
            return None
        return tuple(index for index, legal in enumerate(self.next_legal_mask) if legal)


__all__ = ["TRANSITION_SCHEMA_VERSION", "MacroTransitionRow"]
