"""Fixed counterfactual diagnostic task for lisjong-arena Issue #279.

The source population is TwoStepUkeirePolicy x4 under 4p-red-half. At each
predefined choice-discard decision, HandValueAwareTwoStepUkeirePolicy is the
fixed candidate and the source TwoStepUkeirePolicy action is the fixed
reference action. Same-action decisions contribute exactly zero.

The cheap auxiliary measurement is exact conditional structural-completion
probability within two future self-draw slots (H2). The high-fidelity
reference measurement uses the same deterministic exact evaluator at horizon
three (H3). Neither quantity is match strength or ground truth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from lisjong.policies import HandValueAwareTwoStepUkeirePolicy
from lisjong.policies.finite_horizon_completion import (
    _evaluate_and_choose_discard as _evaluate_finite_horizon,
)
from lisjong.policy_contract import Seat
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.decision_context import DecisionContext

from lisjong_arena.policy_catalog import create_two_step
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspectionRecorder,
    LocalGameRunner,
)

GAME_MODE = "4p-red-half"
MAX_STEPS = 10_000
SOURCE_POLICY_IDENTITY = "two-step-x4"
CANDIDATE_POLICY_IDENTITY = "hand-value-aware"
REFERENCE_POLICY_IDENTITY = "two-step"
CHEAP_HORIZON = 2
REFERENCE_HORIZON = 3


@dataclass(frozen=True, slots=True)
class HanchanMeasurement:
    seed: int
    cheap_score: float
    reference_score: float | None
    source_decision_count: int
    eligible_decision_count: int
    same_action_count: int
    disagreement_count: int
    source_generation_seconds: float
    candidate_selection_seconds: float
    cheap_evaluation_seconds: float
    reference_evaluation_seconds: float

    @property
    def total_seconds(self) -> float:
        return (
            self.source_generation_seconds
            + self.candidate_selection_seconds
            + self.cheap_evaluation_seconds
            + self.reference_evaluation_seconds
        )


def _source_inspection(seed: int):
    recorder = LocalGameInspectionRecorder()
    policies = {seat: create_two_step() for seat in Seat}
    started = time.perf_counter()
    LocalGameRunner(
        policies,
        seed=seed,
        game_mode=GAME_MODE,
        max_steps=MAX_STEPS,
        inspection_recorder=recorder,
    ).run()
    elapsed = time.perf_counter() - started
    return recorder.snapshot(), elapsed


def _completion_probability_delta(
    policy_input,
    candidate_action: DiscardAction,
    reference_action: DiscardAction,
    *,
    horizon: int,
) -> float:
    if candidate_action == reference_action:
        return 0.0
    _, analysis = _evaluate_finite_horizon(
        policy_input,
        (candidate_action, reference_action),
        horizon=horizon,
    )
    by_action = {
        evaluation.action: evaluation.completion_mass
        for evaluation in analysis.candidate_evaluations
    }
    denominator = analysis.sequence_denominator
    return (
        by_action[candidate_action] - by_action[reference_action]
    ) / denominator


def measure_hanchan(seed: int, *, include_reference: bool) -> HanchanMeasurement:
    """Measure one source hanchan; H3 is never invoked when include_reference=False."""
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    if type(include_reference) is not bool:
        raise TypeError("include_reference must be a bool")

    inspection, source_seconds = _source_inspection(seed)
    candidate = HandValueAwareTwoStepUkeirePolicy()

    source_decisions = 0
    eligible = 0
    same = 0
    disagreements = 0
    cheap_deltas: list[float] = []
    reference_deltas: list[float] = []
    candidate_seconds = 0.0
    cheap_seconds = 0.0
    reference_seconds = 0.0

    for step in inspection.step_observations:
        for observation in step.seat_decisions:
            source_decisions += 1
            legal_actions = observation.decision_trace.legal_actions
            discard_actions = tuple(
                action for action in legal_actions if isinstance(action, DiscardAction)
            )
            reference_action = observation.decision_trace.selected_action
            if (
                len(discard_actions) < 2
                or not isinstance(reference_action, DiscardAction)
            ):
                continue

            context = DecisionContext(observation.policy_input, legal_actions)
            started = time.perf_counter()
            candidate_action = candidate.choose_action(context)
            candidate_seconds += time.perf_counter() - started
            if not isinstance(candidate_action, DiscardAction):
                continue

            eligible += 1
            if candidate_action == reference_action:
                same += 1
                cheap_deltas.append(0.0)
                if include_reference:
                    reference_deltas.append(0.0)
                continue

            disagreements += 1
            started = time.perf_counter()
            cheap_deltas.append(
                _completion_probability_delta(
                    observation.policy_input,
                    candidate_action,
                    reference_action,
                    horizon=CHEAP_HORIZON,
                )
            )
            cheap_seconds += time.perf_counter() - started

            if include_reference:
                started = time.perf_counter()
                reference_deltas.append(
                    _completion_probability_delta(
                        observation.policy_input,
                        candidate_action,
                        reference_action,
                        horizon=REFERENCE_HORIZON,
                    )
                )
                reference_seconds += time.perf_counter() - started

    # A hanchan with no predefined choice-discard opportunity contributes zero.
    # This keeps every source hanchan in the equal-hanchan-weighted estimand.
    cheap_score = sum(cheap_deltas) / eligible if eligible else 0.0
    reference_score = None
    if include_reference:
        reference_score = (
            sum(reference_deltas) / eligible if eligible else 0.0
        )
        if len(reference_deltas) != eligible:
            raise RuntimeError(
                "reference measurement count does not match eligible decisions"
            )
    if len(cheap_deltas) != eligible:
        raise RuntimeError("cheap measurement count does not match eligible decisions")

    return HanchanMeasurement(
        seed=seed,
        cheap_score=float(cheap_score),
        reference_score=(
            None if reference_score is None else float(reference_score)
        ),
        source_decision_count=source_decisions,
        eligible_decision_count=eligible,
        same_action_count=same,
        disagreement_count=disagreements,
        source_generation_seconds=float(source_seconds),
        candidate_selection_seconds=float(candidate_seconds),
        cheap_evaluation_seconds=float(cheap_seconds),
        reference_evaluation_seconds=float(reference_seconds),
    )


def timing_only_measurement(seed: int) -> dict[str, int | float]:
    """Run one labeled calibration hanchan but expose timing only, never outcomes/counts."""
    measured = measure_hanchan(seed, include_reference=True)
    unlabeled_seconds = (
        measured.source_generation_seconds
        + measured.candidate_selection_seconds
        + measured.cheap_evaluation_seconds
    )
    return {
        "seed": seed,
        "source_generation_seconds": measured.source_generation_seconds,
        "candidate_selection_seconds": measured.candidate_selection_seconds,
        "cheap_evaluation_seconds": measured.cheap_evaluation_seconds,
        "reference_evaluation_seconds": measured.reference_evaluation_seconds,
        "unlabeled_total_seconds": unlabeled_seconds,
        "labeled_total_seconds": (
            unlabeled_seconds + measured.reference_evaluation_seconds
        ),
    }
