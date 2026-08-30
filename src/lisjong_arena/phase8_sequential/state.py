"""Wind-keyed public previous-HandBelief state for Phase 8 recurrence."""

from dataclasses import dataclass
from math import isfinite

from lisjong.belief import SCALE, wind_index
from lisjong.policy_contract import Wind

from lisjong_arena.phase5_belief_dataset.baseline import (
    predict_conditional_uniform_baseline,
)
from lisjong_arena.phase5_belief_dataset.measurements import (
    ExpectedCountPrediction,
)
from lisjong_arena.phase6_snapshot.tensor import TILE_COUNT_SCALE


@dataclass(frozen=True, slots=True)
class WindExpectedCount:
    wind: Wind
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.wind, Wind):
            raise TypeError("wind must be a Wind")
        if len(self.values) != 34 or any(
            not isfinite(value) or value < 0 for value in self.values
        ):
            raise ValueError(
                "expected-count row must contain 34 finite non-negative cells"
            )


@dataclass(frozen=True, slots=True)
class PreviousBeliefState:
    rows: tuple[WindExpectedCount, WindExpectedCount, WindExpectedCount]

    def __post_init__(self) -> None:
        rows = tuple(self.rows)
        if len(rows) != 3 or len({value.wind for value in rows}) != 3:
            raise ValueError("previous belief must contain three distinct Wind rows")
        object.__setattr__(self, "rows", rows)

    def remap(self, opponent_winds: tuple[Wind, Wind, Wind]) -> tuple[float, ...]:
        if len(opponent_winds) != 3 or len(set(opponent_winds)) != 3:
            raise ValueError("current opponent winds must contain three identities")
        by_wind = {value.wind: value.values for value in self.rows}
        if set(by_wind) != set(opponent_winds):
            raise ValueError("previous and current opponent Wind identities differ")
        return tuple(
            cell / TILE_COUNT_SCALE for wind in opponent_winds for cell in by_wind[wind]
        )


def baseline_initial_state(example) -> PreviousBeliefState:
    """Initialize from the current public anchor without consulting target values."""
    baseline = predict_conditional_uniform_baseline(
        example.example, example.sample.anchor
    )
    return PreviousBeliefState(
        tuple(
            WindExpectedCount(
                wind,
                tuple(
                    value / SCALE
                    for value in baseline.belief.hands[
                        wind_index(wind)
                    ].expected_count_raw
                ),
            )
            for wind in example.opponent_winds
        )
    )


def state_from_prediction(prediction: ExpectedCountPrediction) -> PreviousBeliefState:
    return PreviousBeliefState(
        tuple(WindExpectedCount(row.wind, row.values) for row in prediction.rows)
    )


__all__ = [
    "PreviousBeliefState",
    "WindExpectedCount",
    "baseline_initial_state",
    "state_from_prediction",
]
