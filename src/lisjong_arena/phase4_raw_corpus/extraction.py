"""First-party execution recorder for the Phase 4 raw corpus."""

from dataclasses import dataclass

from lisjong.policies import TwoStepUkeirePolicy
from lisjong_engine.driver import ActionSelector, run_hanchan
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import SeatObservation
from lisjong_engine.public_state import public_tile
from lisjong_engine.round_evidence_builder import build_round_evidence
from lisjong_engine.round_evidence_completion import RoundEvidenceCompletion
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat

from lisjong_arena.lisjong_engine.policy_selector import PolicySeatSelector
from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    collect_pipeline_provenance,
)
from lisjong_arena.phase4_raw_corpus.model import (
    CheckpointTruth,
    DecisionCheckpoint,
    OpponentConcealedTruth,
    RawGame,
    RawRound,
    ViewerEvidence,
)


@dataclass(frozen=True, slots=True)
class _PendingCheckpoint:
    checkpoint: DecisionCheckpoint
    truth: CheckpointTruth
    checkpoint_time_evidence: tuple


class Phase4RawRecorder:
    """Capture every real selector callback and close each round from engine evidence."""

    def __init__(self, match_state: MatchState) -> None:
        if not isinstance(match_state, MatchState):
            raise TypeError("match_state must be a MatchState")
        self._match_state = match_state
        self._pending: list[_PendingCheckpoint] = []
        self._rounds: list[RawRound] = []
        self.total_selector_callbacks = 0

    def observe(self, observation: SeatObservation) -> None:
        if not isinstance(observation, SeatObservation):
            raise TypeError("observation must be a SeatObservation")
        round_state = self._match_state.active_round
        if round_state is None:
            raise RuntimeError("a decision checkpoint requires an active round")
        position = self._match_state.position
        if (
            observation.hand_number != position.hand_number
            or observation.honba != position.honba
            or observation.dealer_seat is not position.dealer_seat
            or observation.prevailing_wind is not position.prevailing_wind
        ):
            raise RuntimeError(
                "decision observation does not match active round identity"
            )
        evidence = build_round_evidence(round_state, observation.viewer_seat)
        index = len(self._pending)
        checkpoint = DecisionCheckpoint(
            checkpoint_index=index,
            round_revision=round_state.revision,
            observation=observation,
            evidence_cutoff=len(evidence),
        )
        opponents = tuple(
            OpponentConcealedTruth(
                opponent_seat=seat,
                concealed_tiles=tuple(
                    sorted(
                        (public_tile(tile) for tile in round_state.hand_tiles(seat)),
                        key=lambda tile: (tile.tile_type.id, tile.is_red),
                    )
                ),
            )
            for seat in Seat
            if seat is not observation.viewer_seat
        )
        truth = CheckpointTruth(index, observation.viewer_seat, opponents)
        self._pending.append(_PendingCheckpoint(checkpoint, truth, evidence))
        self.total_selector_callbacks += 1

    def complete_round(self, completion: RoundEvidenceCompletion) -> None:
        if not isinstance(completion, RoundEvidenceCompletion):
            raise TypeError("completion must be a RoundEvidenceCompletion")
        position = self._match_state.position
        if (
            completion.hand_number != position.hand_number
            or completion.honba != position.honba
            or completion.dealer_seat is not position.dealer_seat
            or completion.prevailing_wind is not position.prevailing_wind
        ):
            raise RuntimeError("round completion does not match active round identity")
        stream_by_viewer = {
            projection.viewer_seat: projection.evidence
            for projection in completion.projections
        }
        for pending in self._pending:
            checkpoint = pending.checkpoint
            final = stream_by_viewer[checkpoint.viewer_seat]
            if final[: checkpoint.evidence_cutoff] != pending.checkpoint_time_evidence:
                raise RuntimeError(
                    "checkpoint-time evidence is not the exact final stream prefix"
                )
        self._rounds.append(
            RawRound(
                round_index=len(self._rounds),
                prevailing_wind=completion.prevailing_wind,
                hand_number=completion.hand_number,
                dealer_seat=completion.dealer_seat,
                honba=completion.honba,
                viewer_evidence=tuple(
                    ViewerEvidence(value.viewer_seat, value.evidence)
                    for value in completion.projections
                ),
                checkpoints=tuple(value.checkpoint for value in self._pending),
                training_truth=tuple(value.truth for value in self._pending),
            )
        )
        self._pending.clear()

    def finish(self) -> tuple[RawRound, ...]:
        if self._pending:
            raise RuntimeError("game ended with checkpoints not assigned to a round")
        if (
            sum(len(value.checkpoints) for value in self._rounds)
            != self.total_selector_callbacks
        ):
            raise RuntimeError("selector callback/checkpoint count mismatch")
        return tuple(self._rounds)


class _RecordingSelector:
    __slots__ = ("_delegate", "_recorder")

    def __init__(self, delegate: ActionSelector, recorder: Phase4RawRecorder) -> None:
        self._delegate = delegate
        self._recorder = recorder

    def __call__(self, observation: SeatObservation, options: object) -> object:
        self._recorder.observe(observation)
        return self._delegate(observation, options)


def extract_phase4_raw_game(seed: int, *, rules: RuleSet | None = None) -> RawGame:
    if type(seed) is not int:
        raise TypeError("seed must be an int")
    effective_rules = rules or RuleSet.default()
    if not isinstance(effective_rules, RuleSet):
        raise TypeError("rules must be a RuleSet or None")
    match_state = MatchState(seed=seed, rules=effective_rules)
    recorder = Phase4RawRecorder(match_state)
    selectors = {
        seat: _RecordingSelector(
            PolicySeatSelector(seat, TwoStepUkeirePolicy()), recorder
        )
        for seat in Seat
    }
    run_hanchan(
        match_state,
        selectors,
        on_round_evidence_complete=recorder.complete_round,
    )
    return RawGame(seed=seed, rounds=recorder.finish())


def phase4_provenance(rules: RuleSet | None = None):
    """Resolve the same source revisions and effective rules as Phase 2/3."""
    return collect_pipeline_provenance(rules or RuleSet.default())


__all__ = [
    "FIRST_PARTY_SOURCE_CLASS",
    "Phase4RawRecorder",
    "extract_phase4_raw_game",
    "phase4_provenance",
]
