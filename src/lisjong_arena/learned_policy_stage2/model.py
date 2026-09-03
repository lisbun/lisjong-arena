"""Stage 2 dataset row value type and its hard invariants.

1 rowは「1つのactual teacher decision」を表すimmutable valueであり、次だけを
持つ。

```text
player-safe PolicyInput feature (arena-policy-input-feature-v1, 8204)
fixed legal mask                (lisjong-action-vocabulary-1, 802)
encoded teacher action index
source game / round / seat identity
split membership
```

hidden opponent hand、wall truth、future state、future outcome、oracle
information、teacher-internal analysis（shanten、ukeire、danger、候補評価、
選択理由）はrowへ入れない。featureはStage 1 encoderの出力をそのまま保持し、
Stage 2側で再定義・再計算しない。
"""

import math
from dataclasses import dataclass

from .errors import Stage2RecordingError
from .protocol import FEATURE_DIMENSION, VOCABULARY_SIZE, Split, action_family

_FLOAT32_MAX = float.fromhex("0x1.fffffep+127")


@dataclass(frozen=True, slots=True)
class Stage2DecisionRow:
    """1 teacher decisionのversioned dataset row。"""

    seed: int
    split: Split
    step_ordinal: int
    decision_ordinal: int
    round_ordinal: int
    round_wind: str
    hand_number: int
    honba: int
    actor_seat: int
    feature_values: tuple[float, ...]
    legal_mask: tuple[bool, ...]
    teacher_action_index: int
    teacher_action_family: str

    def __post_init__(self) -> None:
        for name in (
            "seed",
            "step_ordinal",
            "decision_ordinal",
            "round_ordinal",
            "hand_number",
            "honba",
            "actor_seat",
            "teacher_action_index",
        ):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an int")
        for name in ("step_ordinal", "decision_ordinal", "round_ordinal", "honba"):
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

        if type(self.feature_values) is not tuple:
            raise TypeError("feature_values must be an exact tuple")
        if len(self.feature_values) != FEATURE_DIMENSION:
            raise Stage2RecordingError(
                f"feature dimension must be {FEATURE_DIMENSION}; "
                f"got {len(self.feature_values)}"
            )
        if any(type(value) is not float for value in self.feature_values):
            raise TypeError("feature_values must contain only exact floats")
        if any(
            not math.isfinite(value) or abs(value) > _FLOAT32_MAX
            for value in self.feature_values
        ):
            raise Stage2RecordingError("feature values must all be finite float32")

        if type(self.legal_mask) is not tuple:
            raise TypeError("legal_mask must be an exact tuple")
        if len(self.legal_mask) != VOCABULARY_SIZE:
            raise Stage2RecordingError(
                f"legal mask dimension must be {VOCABULARY_SIZE}; "
                f"got {len(self.legal_mask)}"
            )
        if any(type(value) is not bool for value in self.legal_mask):
            raise TypeError("legal_mask must contain only exact bools")
        if not any(self.legal_mask):
            raise Stage2RecordingError("legal mask must contain at least one action")

        if not 0 <= self.teacher_action_index < VOCABULARY_SIZE:
            raise Stage2RecordingError(
                f"teacher action index must be in range(0, {VOCABULARY_SIZE})"
            )
        if not self.legal_mask[self.teacher_action_index]:
            raise Stage2RecordingError(
                "teacher action index is not legal in this decision"
            )
        expected_family = action_family(self.teacher_action_index)
        if self.teacher_action_family != expected_family:
            raise Stage2RecordingError(
                "teacher action family does not match the vocabulary block"
            )

    @property
    def legal_action_count(self) -> int:
        return sum(self.legal_mask)

    @property
    def is_choice_row(self) -> bool:
        """`len(legal_actions) >= 2` のrowだけをprimary metricの対象とする。"""
        return self.legal_action_count >= 2

    @property
    def legal_action_indices(self) -> tuple[int, ...]:
        return tuple(index for index, legal in enumerate(self.legal_mask) if legal)


__all__ = ["Stage2DecisionRow"]
