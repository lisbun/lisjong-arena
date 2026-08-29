"""Phase 4 first-party HandBelief raw corpus value contracts."""

from collections import Counter
from dataclasses import dataclass, replace

from lisjong_engine.observation import ObservationDecisionKind, SeatObservation
from lisjong_engine.public_state import PublicTile
from lisjong_engine.round_evidence import (
    DrawEvidence,
    RoundEndedEvidence,
    RoundEvidence,
)
from lisjong_engine.seat import Seat
from lisjong_engine.wind import Wind

from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    TrainingPipelineProvenance,
)

SCHEMA_VERSION = 1
GENERATION_PROTOCOL_ID = "first-party-hand-belief-raw-v1"
FIXED_SEEDS = tuple(range(1000, 1008))
MAX_GAMES_PER_SHARD = 4


class Phase4RawCorpusError(ValueError):
    """Phase 4 contract / persistence violation."""


@dataclass(frozen=True, slots=True)
class ViewerEvidence:
    viewer_seat: Seat
    evidence: tuple[RoundEvidence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.viewer_seat, Seat):
            raise TypeError("viewer_seat must be a Seat")
        values = tuple(self.evidence)
        if any(not isinstance(value, RoundEvidence) for value in values):
            raise TypeError("evidence must contain only RoundEvidence")
        if any(
            isinstance(value, DrawEvidence)
            and value.tile is not None
            and value.seat is not self.viewer_seat
            for value in values
        ):
            raise ValueError(
                "viewer-private draw tile leaked into another viewer stream"
            )
        if not values or not isinstance(values[-1], RoundEndedEvidence):
            raise ValueError("a completed viewer stream must retain terminal evidence")
        object.__setattr__(self, "evidence", values)


@dataclass(frozen=True, slots=True)
class DecisionCheckpoint:
    checkpoint_index: int
    round_revision: int
    observation: SeatObservation
    evidence_cutoff: int

    def __post_init__(self) -> None:
        for name in ("checkpoint_index", "round_revision", "evidence_cutoff"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an int")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if not isinstance(self.observation, SeatObservation):
            raise TypeError("observation must be a SeatObservation")

    @property
    def viewer_seat(self) -> Seat:
        return self.observation.viewer_seat

    @property
    def decision_kind(self) -> ObservationDecisionKind:
        return self.observation.decision_kind


@dataclass(frozen=True, slots=True)
class OpponentConcealedTruth:
    opponent_seat: Seat
    concealed_tiles: tuple[PublicTile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.opponent_seat, Seat):
            raise TypeError("opponent_seat must be a Seat")
        values = tuple(self.concealed_tiles)
        if any(not isinstance(value, PublicTile) for value in values):
            raise TypeError("concealed_tiles must contain only PublicTile")
        if len(values) > 14:
            raise ValueError("concealed truth cannot exceed 14 physical tiles")
        counts = Counter(tile.tile_type for tile in values)
        if any(count > 4 for count in counts.values()):
            raise ValueError("concealed truth exceeds physical tile multiplicity")
        red_counts = Counter(tile.tile_type.category for tile in values if tile.is_red)
        if any(count > 1 for count in red_counts.values()):
            raise ValueError("concealed truth contains duplicate red fives")
        if values != tuple(
            sorted(values, key=lambda tile: (tile.tile_type.id, tile.is_red))
        ):
            raise ValueError(
                "concealed truth must use canonical tile-kind/red ordering"
            )
        object.__setattr__(self, "concealed_tiles", values)


@dataclass(frozen=True, slots=True)
class CheckpointTruth:
    checkpoint_index: int
    viewer_seat: Seat
    opponents: tuple[OpponentConcealedTruth, ...]

    def __post_init__(self) -> None:
        if type(self.checkpoint_index) is not int:
            raise TypeError("checkpoint_index must be an int")
        if self.checkpoint_index < 0:
            raise ValueError("checkpoint_index must be non-negative")
        if not isinstance(self.viewer_seat, Seat):
            raise TypeError("viewer_seat must be a Seat")
        opponents = tuple(self.opponents)
        if any(not isinstance(value, OpponentConcealedTruth) for value in opponents):
            raise TypeError("opponents must contain only OpponentConcealedTruth")
        expected = tuple(seat for seat in Seat if seat is not self.viewer_seat)
        if tuple(value.opponent_seat for value in opponents) != expected:
            raise ValueError(
                "opponents must contain the other three seats in Seat order"
            )
        object.__setattr__(self, "opponents", opponents)


@dataclass(frozen=True, slots=True)
class RawRound:
    round_index: int
    prevailing_wind: Wind
    hand_number: int
    dealer_seat: Seat
    honba: int
    viewer_evidence: tuple[ViewerEvidence, ...]
    checkpoints: tuple[DecisionCheckpoint, ...]
    training_truth: tuple[CheckpointTruth, ...]

    def __post_init__(self) -> None:
        for name in ("round_index", "hand_number", "honba"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an int")
        if self.round_index < 0 or self.honba < 0 or not 1 <= self.hand_number <= 4:
            raise ValueError("invalid round identity range")
        if not isinstance(self.prevailing_wind, Wind) or not isinstance(
            self.dealer_seat, Seat
        ):
            raise TypeError("invalid round identity enum")
        streams = tuple(self.viewer_evidence)
        if any(not isinstance(value, ViewerEvidence) for value in streams):
            raise TypeError("viewer_evidence must contain only ViewerEvidence")
        if tuple(value.viewer_seat for value in streams) != tuple(Seat):
            raise ValueError("viewer_evidence must contain all viewers in Seat order")
        normalized_streams = tuple(
            tuple(
                replace(evidence, tile=None)
                if isinstance(evidence, DrawEvidence)
                else evidence
                for evidence in stream.evidence
            )
            for stream in streams
        )
        if len(set(normalized_streams)) != 1:
            raise ValueError(
                "viewer streams may differ only in the viewer-private draw tile"
            )
        checkpoints = tuple(self.checkpoints)
        truths = tuple(self.training_truth)
        if any(not isinstance(value, DecisionCheckpoint) for value in checkpoints):
            raise TypeError("checkpoints must contain only DecisionCheckpoint")
        if any(not isinstance(value, CheckpointTruth) for value in truths):
            raise TypeError("training_truth must contain only CheckpointTruth")
        indexes = tuple(range(len(checkpoints)))
        if tuple(value.checkpoint_index for value in checkpoints) != indexes:
            raise ValueError("checkpoint indexes must be contiguous from zero")
        if tuple(value.checkpoint_index for value in truths) != indexes:
            raise ValueError("training truth must align exactly with checkpoints")
        if any(
            checkpoint.viewer_seat is not truth.viewer_seat
            for checkpoint, truth in zip(checkpoints, truths, strict=True)
        ):
            raise ValueError("checkpoint and training truth viewer identity mismatch")
        stream_by_viewer = {value.viewer_seat: value.evidence for value in streams}
        if any(
            checkpoint.evidence_cutoff > len(stream_by_viewer[checkpoint.viewer_seat])
            for checkpoint in checkpoints
        ):
            raise ValueError("evidence cutoff exceeds final viewer stream")
        if any(
            checkpoint.observation.hand_number != self.hand_number
            or checkpoint.observation.honba != self.honba
            or checkpoint.observation.dealer_seat is not self.dealer_seat
            or checkpoint.observation.prevailing_wind is not self.prevailing_wind
            for checkpoint in checkpoints
        ):
            raise ValueError("checkpoint observation has the wrong round identity")
        revisions = tuple(checkpoint.round_revision for checkpoint in checkpoints)
        if revisions != tuple(sorted(revisions)):
            raise ValueError("checkpoint revisions must be nondecreasing")
        object.__setattr__(self, "viewer_evidence", streams)
        object.__setattr__(self, "checkpoints", checkpoints)
        object.__setattr__(self, "training_truth", truths)


@dataclass(frozen=True, slots=True)
class RawGame:
    seed: int
    rounds: tuple[RawRound, ...]

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise TypeError("seed must be an int")
        rounds = tuple(self.rounds)
        if any(not isinstance(value, RawRound) for value in rounds):
            raise TypeError("rounds must contain only RawRound")
        if tuple(value.round_index for value in rounds) != tuple(range(len(rounds))):
            raise ValueError("round indexes must be contiguous from zero")
        object.__setattr__(self, "rounds", rounds)


@dataclass(frozen=True, slots=True)
class RawCorpus:
    provenance: TrainingPipelineProvenance
    games: tuple[RawGame, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, TrainingPipelineProvenance):
            raise TypeError("provenance must be TrainingPipelineProvenance")
        games = tuple(self.games)
        if any(not isinstance(value, RawGame) for value in games):
            raise TypeError("games must contain only RawGame")
        seeds = tuple(game.seed for game in games)
        if len(set(seeds)) != len(seeds):
            raise ValueError("game seeds must be unique")
        if seeds != tuple(sorted(seeds)):
            raise ValueError("games must be ordered by ascending seed")
        if seeds != FIXED_SEEDS:
            raise ValueError("corpus games must be exactly the fixed seeds 1000..1007")
        object.__setattr__(self, "games", games)
