"""Direct pinned conditional-uniform baseline over player-safe anchors."""

from dataclasses import dataclass

from lisjong.belief import (
    ConcealedHandBelief,
    estimate_conditional_uniform_hand_belief,
    wind_for_seat,
    wind_index,
)
from lisjong.policy_contract import PolicyInput, Seat

from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.phase2_training_anchor.player_safe_anchor import (
    FrozenPlayerSafeAnchor,
)
from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase5_belief_dataset.model import TurnExampleReference

_STABLE_EQUIVALENT_TILE_COUNT = 13
_MELD_EQUIVALENT_TILE_COUNT = 3


@dataclass(frozen=True, slots=True)
class ConditionalUniformBaselineInput:
    policy_input: PolicyInput
    concealed_slot_counts_by_wind: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if not isinstance(self.policy_input, PolicyInput):
            raise TypeError("policy_input must be a PolicyInput")
        if len(self.concealed_slot_counts_by_wind) != 4:
            raise ValueError("concealed slot counts must use canonical Wind order")


@dataclass(frozen=True, slots=True)
class BaselinePrediction:
    example: TurnExampleReference
    concealed_slot_counts_by_wind: tuple[int, int, int, int]
    belief: ConcealedHandBelief

    def __post_init__(self) -> None:
        if not isinstance(self.example, TurnExampleReference):
            raise TypeError("example must be a TurnExampleReference")
        if len(self.concealed_slot_counts_by_wind) != 4:
            raise ValueError("concealed slot counts must use canonical Wind order")
        if not isinstance(self.belief, ConcealedHandBelief):
            raise TypeError("belief must be a ConcealedHandBelief")


def build_conditional_uniform_baseline_input(
    anchor: FrozenPlayerSafeAnchor,
) -> ConditionalUniformBaselineInput:
    """Build baseline inputs only from the frozen player-safe anchor."""
    if not isinstance(anchor, FrozenPlayerSafeAnchor):
        raise TypeError("anchor must be a FrozenPlayerSafeAnchor")
    policy_input = build_policy_input(anchor.observation)
    counts = [0, 0, 0, 0]
    for seat in Seat:
        wind_number = wind_index(wind_for_seat(seat, policy_input.round.dealer_seat))
        if seat is policy_input.self_seat:
            counts[wind_number] = 0
            continue
        public_melds = policy_input.players[int(seat)].melds
        concealed_slots = _STABLE_EQUIVALENT_TILE_COUNT - (
            _MELD_EQUIVALENT_TILE_COUNT * len(public_melds)
        )
        if concealed_slots < 0:
            raise RuntimeError("public meld count implies negative concealed slots")
        counts[wind_number] = concealed_slots
    return ConditionalUniformBaselineInput(policy_input, tuple(counts))


def predict_conditional_uniform_baseline(
    example: TurnExampleReference,
    anchor: FrozenPlayerSafeAnchor,
) -> BaselinePrediction:
    """Call the pinned lisjong estimator directly; no Arena math is duplicated."""
    value = build_conditional_uniform_baseline_input(anchor)
    return BaselinePrediction(
        example,
        value.concealed_slot_counts_by_wind,
        estimate_conditional_uniform_hand_belief(
            value.policy_input, value.concealed_slot_counts_by_wind
        ),
    )


def predict_dataset_baseline(
    references: tuple[TurnExampleReference, ...],
    samples: tuple[TrainingSample, ...],
) -> tuple[BaselinePrediction, ...]:
    if len(references) != len(samples):
        raise ValueError("references and samples must have equal length")
    return tuple(
        predict_conditional_uniform_baseline(reference, sample.anchor)
        for reference, sample in zip(references, samples, strict=True)
    )


__all__ = [
    "BaselinePrediction",
    "ConditionalUniformBaselineInput",
    "build_conditional_uniform_baseline_input",
    "predict_conditional_uniform_baseline",
    "predict_dataset_baseline",
]
