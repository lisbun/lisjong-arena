"""Consume lisjong's staged values without recomputing tile efficiency."""

from dataclasses import dataclass

from lisjong.policies.two_step_ukeire import (
    TwoStepUkeireAnalysis,
    TwoStepUkeireCandidateEvaluation,
)
from lisjong.policy_contract.action import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    KakanAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    TsumoAction,
)
from lisjong.structural_efficiency import discard_action_sort_key

WIN = (RonAction, TsumoAction)
CALL = (ChiAction, PonAction, DaiminkanAction, AnkanAction, KakanAction)


class OffenseError(ValueError):
    """Invalid O0 evidence; never substitute an action or incomplete result."""


@dataclass(frozen=True)
class Stages:
    candidates: tuple[TwoStepUkeireCandidateEvaluation, ...]
    shanten: frozenset
    ukeire: frozenset
    second_step: frozenset | None

    @property
    def optimal(self):
        return self.ukeire if self.second_step is None else self.second_step


def stage_sets(evaluations) -> Stages:
    """Set reductions only. None is never converted to an evaluated zero."""
    candidates = tuple(evaluations)
    if not candidates or any(
        not isinstance(e, TwoStepUkeireCandidateEvaluation) for e in candidates
    ):
        raise OffenseError("canonical candidate evaluations required")
    if len({e.action for e in candidates}) != len(candidates):
        raise OffenseError("duplicate candidate identity")
    minimum = min(e.post_discard_shanten for e in candidates)
    first = tuple(e for e in candidates if e.post_discard_shanten == minimum)
    for e in candidates:
        reached = e in first
        if (e.current_ukeire_count is not None) != reached:
            raise OffenseError("current-ukeire stage missingness differs from lisjong")
        if e.current_ukeire_count is not None and e.current_ukeire_count < 0:
            raise OffenseError("negative current ukeire")
    maximum = max(e.current_ukeire_count for e in first)
    second = tuple(e for e in first if e.current_ukeire_count == maximum)
    applicable = len(second) > 1 and minimum != 0
    for e in candidates:
        if (e.second_step_ukeire_score is not None) != (applicable and e in second):
            raise OffenseError("second-step stage missingness differs from lisjong")
        if e.second_step_ukeire_score is not None and e.second_step_ukeire_score < 0:
            raise OffenseError("negative second-step score")
    final = None
    if applicable:
        best = max(e.second_step_ukeire_score for e in second)
        final = frozenset(
            e.action for e in second if e.second_step_ukeire_score == best
        )
    return Stages(
        candidates,
        frozenset(e.action for e in first),
        frozenset(e.action for e in second),
        final,
    )


def audit_trace(trace) -> Stages | None:
    """Check the O0 contract on a validated actual teacher trace, without rescue."""
    legal, selected = trace.legal_actions, trace.selected_action
    if isinstance(selected, CALL):
        raise OffenseError("OFFENSE TEACHER NOT QUALIFIED: voluntary meld/kan")
    if any(isinstance(a, WIN) for a in legal):
        expected = WIN
    elif any(isinstance(a, RiichiAction) for a in legal):
        expected = (RiichiAction,)
    elif any(isinstance(a, DiscardAction) for a in legal):
        expected = (DiscardAction,)
    else:
        expected = (PassAction,)
    if not isinstance(selected, expected):
        raise OffenseError("OFFENSE TEACHER NOT QUALIFIED: action priority")
    if expected != (DiscardAction,):
        if trace.analysis is not None:
            raise OffenseError("unexpected evaluation outside ordinary discard")
        return None
    if not isinstance(trace.analysis, TwoStepUkeireAnalysis):
        raise OffenseError("canonical TwoStepUkeireAnalysis missing")
    stages = stage_sets(trace.analysis.candidate_evaluations)
    if {e.action for e in stages.candidates} != {
        a for a in legal if isinstance(a, DiscardAction)
    }:
        raise OffenseError("candidate coverage differs from legal discards")
    if selected != min(stages.optimal, key=discard_action_sort_key):
        raise OffenseError("OFFENSE TEACHER NOT QUALIFIED: discard hierarchy/tie-break")
    return stages


def audit_discard(stages: Stages, action) -> dict:
    """Per-action semantic evidence for future consumers, with explicit eligibility.

    Upstream-stage misses are not successes at later stages. A None conditional
    result has no place in that conditional denominator. No learner is run here.
    """
    evaluation = next((e for e in stages.candidates if e.action == action), None)
    shanten_ok = action in stages.shanten
    ukeire_ok = action in stages.ukeire
    second_eligible = stages.second_step is not None
    return {
        "shanten_agreement": shanten_ok,
        "shanten_regret": None
        if evaluation is None
        else (
            evaluation.post_discard_shanten
            - min(e.post_discard_shanten for e in stages.candidates)
        ),
        "ukeire_eligible": True,
        "ukeire_conditional_agreement": ukeire_ok if shanten_ok else None,
        "ukeire_regret": (
            max(
                e.current_ukeire_count
                for e in stages.candidates
                if e.action in stages.shanten
            )
            - evaluation.current_ukeire_count
        )
        if shanten_ok
        else None,
        "second_step_eligible": second_eligible,
        "second_step_conditional_agreement": (
            action in stages.second_step if second_eligible and ukeire_ok else None
        ),
        "second_step_regret": (
            max(
                e.second_step_ukeire_score
                for e in stages.candidates
                if e.action in stages.ukeire
            )
            - evaluation.second_step_ukeire_score
        )
        if second_eligible and ukeire_ok
        else None,
    }
