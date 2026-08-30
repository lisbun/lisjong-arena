"""Immutable compact Phase 5 HandBelief dataset contracts."""

import hashlib
from collections import Counter
from dataclasses import dataclass
from enum import Enum

from lisjong_engine.seat import Seat

from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    TrainingPipelineProvenance,
)
from lisjong_arena.phase4_raw_corpus.codec import (
    canonical_json_bytes,
    provenance_to_dict,
)
from lisjong_arena.phase4_raw_corpus.model import GENERATION_PROTOCOL_ID

DATASET_SCHEMA_VERSION = 1
BUILDER_SEMANTICS_ID = "phase4-turn-training-sample-reference-v1"
_SHA256_LENGTH = 64


class Phase5BeliefDatasetError(ValueError):
    """Phase 5 dataset contract or persistence violation."""


class DatasetPartition(Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


def _digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class GameIdentity:
    source_class: str
    game_seed: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_class, str) or not self.source_class:
            raise ValueError("source_class must be a non-empty str")
        if type(self.game_seed) is not int:
            raise TypeError("game_seed must be an int")


@dataclass(frozen=True, slots=True)
class GameAssignment:
    game: GameIdentity
    partition: DatasetPartition

    def __post_init__(self) -> None:
        if not isinstance(self.game, GameIdentity):
            raise TypeError("game must be a GameIdentity")
        if not isinstance(self.partition, DatasetPartition):
            raise TypeError("partition must be a DatasetPartition")


@dataclass(frozen=True, slots=True)
class TurnExampleReference:
    """Compact locator for one TURN anchor and its three target rows."""

    game: GameIdentity
    partition: DatasetPartition
    round_index: int
    checkpoint_index: int
    anchor_index: int
    hand_number: int
    honba: int
    round_revision: int
    viewer_seat: Seat

    def __post_init__(self) -> None:
        if not isinstance(self.game, GameIdentity):
            raise TypeError("game must be a GameIdentity")
        if not isinstance(self.partition, DatasetPartition):
            raise TypeError("partition must be a DatasetPartition")
        for name in (
            "round_index",
            "checkpoint_index",
            "anchor_index",
            "honba",
            "round_revision",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        if type(self.hand_number) is not int or self.hand_number < 1:
            raise ValueError("hand_number must be a positive int")
        if not isinstance(self.viewer_seat, Seat):
            raise TypeError("viewer_seat must be a lisjong-engine Seat")

    @property
    def identity(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "source_class": self.game.source_class,
                    "game_seed": self.game.game_seed,
                    "round_index": self.round_index,
                    "checkpoint_index": self.checkpoint_index,
                    "anchor_index": self.anchor_index,
                    "hand_number": self.hand_number,
                    "honba": self.honba,
                    "round_revision": self.round_revision,
                    "viewer_seat": self.viewer_seat.value,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class TargetAvailabilitySummary:
    target_rows: int
    structural_wait_available: int
    structural_wait_unavailable: int
    structural_wait_all_zero: int
    structural_wait_non_zero: int
    unavailable_reasons: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        for name in (
            "target_rows",
            "structural_wait_available",
            "structural_wait_unavailable",
            "structural_wait_all_zero",
            "structural_wait_non_zero",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        reasons = tuple(self.unavailable_reasons)
        if any(
            not isinstance(reason, str)
            or not reason
            or type(count) is not int
            or count <= 0
            for reason, count in reasons
        ):
            raise ValueError("unavailable reasons must be positive named counts")
        if reasons != tuple(sorted(reasons)) or len(
            {name for name, _ in reasons}
        ) != len(reasons):
            raise ValueError("unavailable reasons must be unique and sorted")
        if self.structural_wait_available != (
            self.structural_wait_all_zero + self.structural_wait_non_zero
        ):
            raise ValueError("available wait rows must equal zero plus non-zero rows")
        if self.target_rows != (
            self.structural_wait_available + self.structural_wait_unavailable
        ):
            raise ValueError("target rows must equal available plus unavailable rows")
        if self.structural_wait_unavailable != sum(count for _, count in reasons):
            raise ValueError("unavailable reason counts must cover unavailable rows")
        object.__setattr__(self, "unavailable_reasons", reasons)

    @property
    def available_rate(self) -> float:
        return self.structural_wait_available / self.target_rows

    @property
    def unavailable_rate(self) -> float:
        return self.structural_wait_unavailable / self.target_rows


@dataclass(frozen=True, slots=True)
class PartitionSummary:
    partition: DatasetPartition
    sample_count: int
    availability: TargetAvailabilitySummary

    def __post_init__(self) -> None:
        if not isinstance(self.partition, DatasetPartition):
            raise TypeError("partition must be a DatasetPartition")
        if type(self.sample_count) is not int or self.sample_count <= 0:
            raise ValueError("sample_count must be a positive int")
        if not isinstance(self.availability, TargetAvailabilitySummary):
            raise TypeError("availability must be TargetAvailabilitySummary")
        if self.availability.target_rows != self.sample_count * 3:
            raise ValueError("one TURN example must retain exactly three target rows")


def _game_value(assignment: GameAssignment) -> dict[str, object]:
    return {
        "source_class": assignment.game.source_class,
        "game_seed": assignment.game.game_seed,
        "partition": assignment.partition.value,
    }


def _example_value(reference: TurnExampleReference) -> dict[str, object]:
    return {
        "example_identity": reference.identity,
        "source_class": reference.game.source_class,
        "game_seed": reference.game.game_seed,
        "partition": reference.partition.value,
        "round_index": reference.round_index,
        "checkpoint_index": reference.checkpoint_index,
        "anchor_index": reference.anchor_index,
        "hand_number": reference.hand_number,
        "honba": reference.honba,
        "round_revision": reference.round_revision,
        "viewer_seat": reference.viewer_seat.value,
    }


def _availability_value(value: TargetAvailabilitySummary) -> dict[str, object]:
    return {
        "target_rows": value.target_rows,
        "structural_wait_available": value.structural_wait_available,
        "structural_wait_unavailable": value.structural_wait_unavailable,
        "structural_wait_all_zero": value.structural_wait_all_zero,
        "structural_wait_non_zero": value.structural_wait_non_zero,
        "unavailable_reasons": [
            {"reason": reason, "count": count}
            for reason, count in value.unavailable_reasons
        ],
    }


@dataclass(frozen=True, slots=True)
class BeliefDataset:
    raw_corpus_identity: str
    provenance: TrainingPipelineProvenance
    builder_semantics_id: str
    split_policy_id: str
    games: tuple[GameAssignment, ...]
    examples: tuple[TurnExampleReference, ...]
    partition_summaries: tuple[PartitionSummary, ...]

    def __post_init__(self) -> None:
        _digest(self.raw_corpus_identity, "raw_corpus_identity")
        if not isinstance(self.provenance, TrainingPipelineProvenance):
            raise TypeError("provenance must be TrainingPipelineProvenance")
        if not self.provenance.source_revisions.fully_resolved:
            raise ValueError("derived dataset requires fully resolved source revisions")
        if self.builder_semantics_id != BUILDER_SEMANTICS_ID:
            raise ValueError("unknown builder semantics identity")
        if not isinstance(self.split_policy_id, str) or not self.split_policy_id:
            raise ValueError("split_policy_id must be a non-empty str")
        games = tuple(self.games)
        examples = tuple(self.examples)
        summaries = tuple(self.partition_summaries)
        if not games or any(not isinstance(value, GameAssignment) for value in games):
            raise ValueError("games must contain GameAssignment values")
        if not examples or any(
            not isinstance(value, TurnExampleReference) for value in examples
        ):
            raise ValueError("examples must contain TurnExampleReference values")
        if any(not isinstance(value, PartitionSummary) for value in summaries):
            raise TypeError("partition_summaries must contain PartitionSummary values")
        game_keys = tuple(
            (value.game.source_class, value.game.game_seed) for value in games
        )
        if game_keys != tuple(sorted(set(game_keys))):
            raise ValueError("games must be unique and ordered by source and seed")
        assignment_by_game = {value.game: value.partition for value in games}
        if any(
            reference.game not in assignment_by_game
            or assignment_by_game[reference.game] is not reference.partition
            for reference in examples
        ):
            raise ValueError("every example must use its game's atomic partition")
        identities = tuple(reference.identity for reference in examples)
        if len(set(identities)) != len(identities):
            raise ValueError("example identities must be unique")
        game_order = {assignment.game: index for index, assignment in enumerate(games)}
        example_order = tuple(
            (game_order[reference.game], reference.anchor_index)
            for reference in examples
        )
        if example_order != tuple(sorted(example_order)):
            raise ValueError("examples must be ordered by game and anchor index")
        anchors_by_game: dict[GameIdentity, list[int]] = {
            assignment.game: [] for assignment in games
        }
        for reference in examples:
            anchors_by_game[reference.game].append(reference.anchor_index)
        if any(
            not indexes or tuple(indexes) != tuple(range(len(indexes)))
            for indexes in anchors_by_game.values()
        ):
            raise ValueError("each game must have anchor indexes contiguous from zero")
        counts = Counter(reference.partition for reference in examples)
        expected_summary_order = tuple(
            partition for partition in DatasetPartition if partition in counts
        )
        if tuple(summary.partition for summary in summaries) != expected_summary_order:
            raise ValueError("partition summaries must use canonical non-empty order")
        if any(
            summary.sample_count != counts[summary.partition] for summary in summaries
        ):
            raise ValueError("partition sample counts must match example assignments")
        object.__setattr__(self, "games", games)
        object.__setattr__(self, "examples", examples)
        object.__setattr__(self, "partition_summaries", summaries)

    @property
    def sample_count(self) -> int:
        return len(self.examples)

    def identity_value(self) -> dict[str, object]:
        return {
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "raw_generation_protocol_id": GENERATION_PROTOCOL_ID,
            "raw_corpus_identity": self.raw_corpus_identity,
            "provenance": provenance_to_dict(self.provenance),
            "builder_semantics_id": self.builder_semantics_id,
            "split_policy_id": self.split_policy_id,
            "ordered_games": [_game_value(value) for value in self.games],
            "ordered_examples": [_example_value(value) for value in self.examples],
            "partition_summaries": [
                {
                    "partition": summary.partition.value,
                    "sample_count": summary.sample_count,
                    "target_availability": _availability_value(summary.availability),
                }
                for summary in self.partition_summaries
            ],
        }

    @property
    def dataset_identity(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_value())).hexdigest()


__all__ = [
    "BUILDER_SEMANTICS_ID",
    "DATASET_SCHEMA_VERSION",
    "BeliefDataset",
    "DatasetPartition",
    "GameAssignment",
    "GameIdentity",
    "PartitionSummary",
    "Phase5BeliefDatasetError",
    "TargetAvailabilitySummary",
    "TurnExampleReference",
]
