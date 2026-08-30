"""Locked sequence identity, inventory, and selection rules for Phase 8."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from math import isclose, isfinite
from pathlib import Path
from statistics import median

from lisjong_engine.seat import Seat

from lisjong_arena.phase5_belief_dataset.model import (
    DatasetPartition,
    GameIdentity,
)

SEQUENCE_SEMANTICS_ID = "phase8-sequential-hand-belief-v1"
INVENTORY_SCHEMA_VERSION = "phase8-sequence-inventory-v1"
SNAPSHOT_VALIDATION_MAE = 0.4863309527332531
PHYSICAL_RESIDUAL_TOLERANCE = 1e-6
CHECKPOINT_TIE_ABS_TOLERANCE = 1e-12
FULL_SEQUENCE_MAX_LENGTH = 64
TRUNCATION_LENGTH = 32
DEPTH_BUCKETS = ("depth 1", "depth 2..4", "depth 5..8", "depth 9+")


class Candidate(Enum):
    S1 = "S1"
    S2 = "S2"


class BpttMode(Enum):
    FULL_SEQUENCE = "full-sequence"
    TRUNCATED = "truncated"


@dataclass(frozen=True, slots=True)
class BpttPolicy:
    mode: BpttMode
    truncation_length: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, BpttMode):
            raise TypeError("mode must be a BpttMode")
        expected = TRUNCATION_LENGTH if self.mode is BpttMode.TRUNCATED else None
        if self.truncation_length != expected:
            raise ValueError("BPTT truncation length differs from the locked policy")


@dataclass(frozen=True, slots=True)
class SequenceKey:
    game: GameIdentity
    round_index: int
    viewer_seat: Seat

    def __post_init__(self) -> None:
        if not isinstance(self.game, GameIdentity):
            raise TypeError("game must be a GameIdentity")
        if type(self.round_index) is not int or self.round_index < 0:
            raise ValueError("round_index must be a non-negative int")
        if not isinstance(self.viewer_seat, Seat):
            raise TypeError("viewer_seat must be a Seat")


@dataclass(frozen=True, slots=True)
class Phase8Sequence:
    key: SequenceKey
    partition: DatasetPartition
    steps: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.key, SequenceKey):
            raise TypeError("key must be a SequenceKey")
        if self.partition not in (DatasetPartition.TRAIN, DatasetPartition.VALIDATION):
            raise ValueError("Phase 8 sequences may contain only TRAIN or VALIDATION")
        steps = tuple(self.steps)
        if not steps:
            raise ValueError("a sequence must contain at least one step")
        references = tuple(value.example for value in steps)
        if any(
            reference.game != self.key.game
            or reference.round_index != self.key.round_index
            or reference.viewer_seat is not self.key.viewer_seat
            or reference.partition is not self.partition
            for reference in references
        ):
            raise ValueError("sequence state crosses its canonical identity boundary")
        checkpoints = tuple(value.checkpoint_index for value in references)
        anchors = tuple(value.anchor_index for value in references)
        if checkpoints != tuple(sorted(checkpoints)) or len(set(checkpoints)) != len(
            checkpoints
        ):
            raise ValueError("sequence checkpoints must be strictly ascending")
        if anchors != tuple(sorted(anchors)) or len(set(anchors)) != len(anchors):
            raise ValueError("checkpoint order conflicts with game-global anchor order")
        for name in ("hand_number", "honba"):
            if len({getattr(value, name) for value in references}) != 1:
                raise ValueError(f"sequence {name} integrity differs")
        revisions = tuple(value.round_revision for value in references)
        if revisions != tuple(sorted(revisions)):
            raise ValueError("sequence round_revision order differs")
        for step, reference in zip(steps, references, strict=True):
            sample = getattr(step, "sample", None)
            if sample is None:
                continue
            anchor = sample.anchor
            if (
                anchor.source.source_class != reference.game.source_class
                or anchor.source.game_seed != reference.game.game_seed
                or anchor.anchor_index != reference.anchor_index
                or anchor.hand_number != reference.hand_number
                or anchor.honba != reference.honba
                or anchor.round_revision != reference.round_revision
                or anchor.viewer_seat is not reference.viewer_seat
            ):
                raise ValueError("sequence reference and materialized anchor differ")
        object.__setattr__(self, "steps", steps)


@dataclass(frozen=True, slots=True)
class PartitionInventory:
    partition: DatasetPartition
    sequence_count: int
    sample_count: int
    minimum_length: int
    mean_length: float
    median_length: float
    maximum_length: int
    depth_bucket_counts: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class SequenceInventory:
    raw_corpus_identity: str
    dataset_identity: str
    partitions: tuple[PartitionInventory, PartitionInventory]
    bptt_policy: BpttPolicy
    test_sequence_count: int = 0

    def __post_init__(self) -> None:
        for name in ("raw_corpus_identity", "dataset_identity"):
            value = getattr(self, name)
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if tuple(value.partition for value in self.partitions) != (
            DatasetPartition.TRAIN,
            DatasetPartition.VALIDATION,
        ):
            raise ValueError(
                "inventory must contain canonical TRAIN/VALIDATION entries"
            )
        if self.test_sequence_count != 0:
            raise ValueError("Phase 8 inventory must confirm zero TEST sequences")


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    candidate: Candidate
    validation_mae: float
    positive_game_count: int
    validation_game_count: int
    physical_validity_passed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Candidate):
            raise TypeError("candidate must be S1 or S2")
        if not isfinite(self.validation_mae) or self.validation_mae < 0:
            raise ValueError("validation_mae must be finite and non-negative")
        if (
            type(self.positive_game_count) is not int
            or type(self.validation_game_count) is not int
            or not 0 <= self.positive_game_count <= self.validation_game_count
            or self.validation_game_count <= 0
        ):
            raise ValueError("validation game counts are invalid")
        if type(self.physical_validity_passed) is not bool:
            raise TypeError("physical_validity_passed must be bool")

    @property
    def delta_mae(self) -> float:
        return SNAPSHOT_VALIDATION_MAE - self.validation_mae

    @property
    def advancement_eligible(self) -> bool:
        return (
            self.delta_mae > 0
            and self.positive_game_count >= 6
            and self.validation_game_count == 10
            and self.physical_validity_passed
        )


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    winner: Candidate | None
    advances_to_phase9: bool
    outcome: str


def depth_bucket(depth: int) -> str:
    if type(depth) is not int or depth <= 0:
        raise ValueError("depth must be a positive int")
    if depth == 1:
        return DEPTH_BUCKETS[0]
    if depth <= 4:
        return DEPTH_BUCKETS[1]
    if depth <= 8:
        return DEPTH_BUCKETS[2]
    return DEPTH_BUCKETS[3]


def bptt_policy_for_maximum_length(maximum_length: int) -> BpttPolicy:
    if type(maximum_length) is not int or maximum_length <= 0:
        raise ValueError("maximum sequence length must be a positive int")
    if maximum_length <= FULL_SEQUENCE_MAX_LENGTH:
        return BpttPolicy(BpttMode.FULL_SEQUENCE, None)
    return BpttPolicy(BpttMode.TRUNCATED, TRUNCATION_LENGTH)


def build_sequences(examples: tuple) -> tuple[Phase8Sequence, ...]:
    """Group already materialized development examples by the exact locked key."""
    if not examples:
        raise ValueError("sequence construction requires examples")
    if any(value.example.partition is DatasetPartition.TEST for value in examples):
        raise ValueError("Phase 8 rejects TEST before sequence construction")
    partitions_by_key = defaultdict(set)
    partitions_by_game = defaultdict(set)
    grouped = defaultdict(list)
    for value in examples:
        reference = value.example
        key = SequenceKey(reference.game, reference.round_index, reference.viewer_seat)
        partitions_by_key[key].add(reference.partition)
        partitions_by_game[reference.game].add(reference.partition)
        grouped[(reference.partition, key)].append(value)
    if any(len(values) != 1 for values in partitions_by_key.values()):
        raise ValueError("a canonical sequence identity crosses partition boundary")
    if any(len(values) != 1 for values in partitions_by_game.values()):
        raise ValueError("a game identity crosses partition boundary")
    order = {DatasetPartition.TRAIN: 0, DatasetPartition.VALIDATION: 1}
    sequences = []
    for (partition, key), values in grouped.items():
        ordered = tuple(
            sorted(values, key=lambda value: value.example.checkpoint_index)
        )
        sequences.append(Phase8Sequence(key, partition, ordered))
    return tuple(
        sorted(
            sequences,
            key=lambda value: (
                order[value.partition],
                value.key.game.source_class,
                value.key.game.game_seed,
                value.key.round_index,
                value.key.viewer_seat.value,
            ),
        )
    )


def _partition_inventory(
    partition: DatasetPartition, sequences: tuple[Phase8Sequence, ...]
) -> PartitionInventory:
    selected = tuple(value for value in sequences if value.partition is partition)
    if not selected:
        raise ValueError(f"inventory requires {partition.value} sequences")
    lengths = tuple(len(value.steps) for value in selected)
    buckets = {name: 0 for name in DEPTH_BUCKETS}
    for length in lengths:
        for depth in range(1, length + 1):
            buckets[depth_bucket(depth)] += 1
    return PartitionInventory(
        partition=partition,
        sequence_count=len(selected),
        sample_count=sum(lengths),
        minimum_length=min(lengths),
        mean_length=sum(lengths) / len(lengths),
        median_length=float(median(lengths)),
        maximum_length=max(lengths),
        depth_bucket_counts=tuple(buckets[name] for name in DEPTH_BUCKETS),
    )


def build_inventory(
    sequences: tuple[Phase8Sequence, ...],
    *,
    raw_corpus_identity: str,
    dataset_identity: str,
) -> SequenceInventory:
    partitions = tuple(
        _partition_inventory(partition, sequences)
        for partition in (DatasetPartition.TRAIN, DatasetPartition.VALIDATION)
    )
    maximum = max(value.maximum_length for value in partitions)
    return SequenceInventory(
        raw_corpus_identity,
        dataset_identity,
        partitions,
        bptt_policy_for_maximum_length(maximum),
    )


def checkpoint_improves(candidate_mae: float, best_mae: float) -> bool:
    if not isfinite(candidate_mae) or candidate_mae < 0:
        raise ValueError("candidate checkpoint MAE is invalid")
    if best_mae == float("inf"):
        return True
    if not isfinite(best_mae) or best_mae < 0:
        raise ValueError("best checkpoint MAE is invalid")
    return candidate_mae < best_mae and not isclose(
        candidate_mae,
        best_mae,
        rel_tol=0,
        abs_tol=CHECKPOINT_TIE_ABS_TOLERANCE,
    )


def physical_validity_passes(
    *,
    constraint_non_convergence_count: int,
    maximum_residual: float,
    concealed_size_inconsistency_max: float,
    conservation_violation_sample_rate: float,
) -> bool:
    values = (
        maximum_residual,
        concealed_size_inconsistency_max,
        conservation_violation_sample_rate,
    )
    if (
        type(constraint_non_convergence_count) is not int
        or constraint_non_convergence_count < 0
        or any(not isfinite(value) or value < 0 for value in values)
    ):
        raise ValueError("physical validity inputs are invalid")
    return (
        constraint_non_convergence_count == 0
        and maximum_residual <= PHYSICAL_RESIDUAL_TOLERANCE
        and concealed_size_inconsistency_max <= PHYSICAL_RESIDUAL_TOLERANCE
        and conservation_violation_sample_rate == 0
    )


def select_candidate(s1: CandidateSummary, s2: CandidateSummary) -> CandidateSelection:
    if s1.candidate is not Candidate.S1 or s2.candidate is not Candidate.S2:
        raise ValueError("selection requires one ordered S1 and S2 summary")
    technically_valid = tuple(
        value for value in (s1, s2) if value.physical_validity_passed
    )
    if not technically_valid:
        return CandidateSelection(None, False, "no sequential candidate advances")
    if len(technically_valid) == 1:
        winner = technically_valid[0]
    elif isclose(
        s1.validation_mae,
        s2.validation_mae,
        rel_tol=0,
        abs_tol=CHECKPOINT_TIE_ABS_TOLERANCE,
    ):
        winner = s1
    else:
        winner = min(technically_valid, key=lambda value: value.validation_mae)
    advances = winner.advancement_eligible
    return CandidateSelection(
        winner.candidate,
        advances,
        winner.candidate.value if advances else "no sequential candidate advances",
    )


def inventory_value(inventory: SequenceInventory) -> dict[str, object]:
    value = {
        "inventory_schema_version": INVENTORY_SCHEMA_VERSION,
        "sequence_semantics_id": SEQUENCE_SEMANTICS_ID,
        "raw_corpus_identity": inventory.raw_corpus_identity,
        "dataset_identity": inventory.dataset_identity,
        "partitions": {
            item.partition.value: {
                "sequence_count": item.sequence_count,
                "sample_count": item.sample_count,
                "minimum_length": item.minimum_length,
                "mean_length": item.mean_length,
                "median_length": item.median_length,
                "maximum_length": item.maximum_length,
                "depth_bucket_counts": dict(
                    zip(DEPTH_BUCKETS, item.depth_bucket_counts, strict=True)
                ),
            }
            for item in inventory.partitions
        },
        "bptt_policy": {
            "mode": inventory.bptt_policy.mode.value,
            "truncation_length": inventory.bptt_policy.truncation_length,
        },
        "test_sequence_count": inventory.test_sequence_count,
    }
    value["inventory_identity"] = hashlib.sha256(_canonical_json(value)).hexdigest()
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def save_inventory(path: str | Path, inventory: SequenceInventory) -> Path:
    path = Path(path)
    if path.exists():
        raise FileExistsError("Phase 8 inventory destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json(inventory_value(inventory)))
    return path


def load_inventory(path: str | Path) -> dict[str, object]:
    data = Path(path).read_bytes()
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("inventory is not valid JSON") from error
    if _canonical_json(value) != data:
        raise ValueError("inventory bytes are not canonical JSON")
    identity = value.pop("inventory_identity", None)
    expected = hashlib.sha256(_canonical_json(value)).hexdigest()
    value["inventory_identity"] = identity
    if identity != expected:
        raise ValueError("inventory logical identity differs")
    if value.get("inventory_schema_version") != INVENTORY_SCHEMA_VERSION:
        raise ValueError("inventory schema version differs")
    if value.get("sequence_semantics_id") != SEQUENCE_SEMANTICS_ID:
        raise ValueError("sequence semantics identity differs")
    if value.get("test_sequence_count") != 0:
        raise ValueError("inventory does not seal TEST")
    if set(value.get("partitions", {})) != {"train", "validation"}:
        raise ValueError("inventory partitions differ")
    maximum = max(
        value["partitions"][name]["maximum_length"] for name in ("train", "validation")
    )
    expected_policy = bptt_policy_for_maximum_length(maximum)
    if value.get("bptt_policy") != {
        "mode": expected_policy.mode.value,
        "truncation_length": expected_policy.truncation_length,
    }:
        raise ValueError("inventory BPTT policy differs from maximum length")
    return value


__all__ = [
    "CHECKPOINT_TIE_ABS_TOLERANCE",
    "DEPTH_BUCKETS",
    "FULL_SEQUENCE_MAX_LENGTH",
    "INVENTORY_SCHEMA_VERSION",
    "PHYSICAL_RESIDUAL_TOLERANCE",
    "SEQUENCE_SEMANTICS_ID",
    "SNAPSHOT_VALIDATION_MAE",
    "TRUNCATION_LENGTH",
    "BpttMode",
    "BpttPolicy",
    "Candidate",
    "CandidateSelection",
    "CandidateSummary",
    "PartitionInventory",
    "Phase8Sequence",
    "SequenceInventory",
    "SequenceKey",
    "bptt_policy_for_maximum_length",
    "build_inventory",
    "build_sequences",
    "checkpoint_improves",
    "depth_bucket",
    "inventory_value",
    "load_inventory",
    "physical_validity_passes",
    "save_inventory",
    "select_candidate",
]
