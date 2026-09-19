"""Retained #172 rows and fixed player-visible features for Arena #222."""

from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase6_snapshot.feature import (
    Phase6SnapshotFeature,
    build_phase6_snapshot_feature,
)
from lisjong_arena.phase11_public_riichi_wait_readout.data import (
    OpponentTarget,
    _targets_for_step,
)
from lisjong_arena.stage3_epoch_budget.retained import (
    POPULATION_DIRNAME,
    load_retained_lock,
    validate_retained_population,
)
from lisjong_arena.stage3_optimization_saturation.experiment import (
    saturation_population_data,
)
from lisjong_arena.stage3_scale_learning_curve.generation import load_population
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)

from .protocol import (
    FEATURE_DIM,
    FEATURE_NAMES,
    TILE_KIND_COUNT,
    ClassicalWaitError,
    exact,
    retained_value,
)


@dataclass(frozen=True, slots=True)
class ClassicalExample:
    partition: DatasetPartition
    source_class: str
    game_seed: int
    round_index: int
    anchor_identity: str
    depth: int
    snapshot: Phase6SnapshotFeature
    targets: tuple[OpponentTarget, OpponentTarget, OpponentTarget]


def records_from_sequences(sequences: tuple) -> tuple[ClassicalExample, ...]:
    if not sequences:
        raise ClassicalWaitError("classical feature extraction requires sequences")
    records = []
    for sequence in sequences:
        for depth, step in enumerate(sequence.steps, start=1):
            records.append(
                ClassicalExample(
                    partition=sequence.partition,
                    source_class=sequence.key.game.source_class,
                    game_seed=sequence.key.game.game_seed,
                    round_index=sequence.key.round_index,
                    anchor_identity=step.example.identity,
                    depth=depth,
                    snapshot=build_phase6_snapshot_feature(step.sample.anchor),
                    targets=_targets_for_step(step),
                )
            )
    return tuple(records)


def load_retained_records(
    corpus_root: str | Path,
) -> tuple[
    tuple[ClassicalExample, ...], tuple[ClassicalExample, ...], dict[str, object]
]:
    """Strict-read exact #150 corpus and materialize the exact #172 data surface.

    No model artifact is loaded, no hanchan is generated, and no TEST partition exists.
    """
    root = Path(corpus_root)
    lock = load_retained_lock(root)
    population, raw, dataset = load_population(root / POPULATION_DIRNAME, lock)
    validate_retained_population(population)
    expected = retained_value()
    for key in ("population_identity", "raw_corpus_identity", "dataset_identity"):
        exact(population[key], expected[key], f"retained {key}")
    full = saturation_population_data(raw, dataset)
    train = records_from_sequences(full.train_sequences)
    validation = records_from_sequences(full.validation_sequences)
    exact(
        sorted({record.game_seed for record in train}),
        list(TRAIN_SEEDS),
        "retained TRAIN hanchan membership",
    )
    exact(
        sorted({record.game_seed for record in validation}),
        list(VALIDATION_SEEDS),
        "retained VALIDATION hanchan membership",
    )
    if any(record.partition is not DatasetPartition.TRAIN for record in train):
        raise ClassicalWaitError("TRAIN records contain a non-TRAIN partition")
    if any(
        record.partition is not DatasetPartition.VALIDATION for record in validation
    ):
        raise ClassicalWaitError(
            "VALIDATION records contain a non-VALIDATION partition"
        )
    return (
        train,
        validation,
        {
            "phase150_execution_lock_identity": expected[
                "phase150_execution_lock_identity"
            ],
            "population_identity": population["population_identity"],
            "raw_corpus_identity": population["raw_corpus_identity"],
            "dataset_identity": population["dataset_identity"],
            "train_seeds": list(TRAIN_SEEDS),
            "validation_seeds": list(VALIDATION_SEEDS),
            "formal_test": False,
        },
    )


def coverage_value(records: tuple[ClassicalExample, ...]) -> dict[str, object]:
    if not records:
        raise ClassicalWaitError("coverage requires records")
    eligible_rows = 0
    unavailable_rows = 0
    all_zero_rows = 0
    eligible_games = set()
    per_tile_positives = [0] * TILE_KIND_COUNT
    for record in records:
        for target in record.targets:
            if not target.established:
                continue
            if target.mask is None:
                unavailable_rows += 1
                continue
            eligible_rows += 1
            eligible_games.add((record.source_class, record.game_seed))
            if not any(target.mask):
                all_zero_rows += 1
            for index, value in enumerate(target.mask):
                per_tile_positives[index] += value
    return {
        "eligible_hanchan": len(eligible_games),
        "eligible_rows": eligible_rows,
        "eligible_cells": eligible_rows * TILE_KIND_COUNT,
        "unavailable_rows": unavailable_rows,
        "all_zero_rows": all_zero_rows,
        "per_tile_positives": per_tile_positives,
    }


def _suited_rank(tile_index: int) -> tuple[int, int] | None:
    if type(tile_index) is not int or not 0 <= tile_index < TILE_KIND_COUNT:
        raise ClassicalWaitError("candidate tile index must be in 0..33")
    if tile_index >= 27:
        return None
    return tile_index // 9, tile_index % 9 + 1


def _index(suit: int, rank: int) -> int:
    if not 0 <= suit <= 2 or not 1 <= rank <= 9:
        raise ClassicalWaitError("invalid suited tile geometry")
    return suit * 9 + rank - 1


def _requirements(
    suited: tuple[int, int] | None,
) -> tuple[
    tuple[int, int] | None,
    tuple[int, int] | None,
    tuple[int, int] | None,
    tuple[int, int] | None,
]:
    if suited is None:
        return None, None, None, None
    suit, rank = suited
    penchan = None
    if rank == 3:
        penchan = (_index(suit, 1), _index(suit, 2))
    elif rank == 7:
        penchan = (_index(suit, 8), _index(suit, 9))
    kanchan = (
        (_index(suit, rank - 1), _index(suit, rank + 1)) if 2 <= rank <= 8 else None
    )
    ryanmen_low = (
        (_index(suit, rank + 1), _index(suit, rank + 2)) if 1 <= rank <= 6 else None
    )
    ryanmen_high = (
        (_index(suit, rank - 2), _index(suit, rank - 1)) if 4 <= rank <= 9 else None
    )
    return penchan, kanchan, ryanmen_low, ryanmen_high


def _support(
    remaining: tuple[int, ...], requirement: tuple[int, int] | None
) -> tuple[float, float]:
    if requirement is None:
        return 0.0, 0.0
    left, right = (remaining[index] for index in requirement)
    if any(type(value) is not int or not 0 <= value <= 4 for value in (left, right)):
        raise ClassicalWaitError(
            "remaining inventory must use exact integer counts in 0..4"
        )
    if left == 0 or right == 0:
        return 0.0, 0.0
    return 1.0, min(left, right) / 4.0


def feature_vector(
    snapshot: Phase6SnapshotFeature,
    target: OpponentTarget,
    tile_index: int,
) -> tuple[float, ...]:
    """Build the locked 13-dimensional public-state feature vector.

    Structural-wait truth is deliberately not read here. The target contributes only
    public identity/timing metadata already derived from the player-safe anchor.
    """
    if not target.established or target.riichi_junme is None:
        raise ClassicalWaitError(
            "primary features require an established-riichi target"
        )
    if not 1 <= target.riichi_junme <= 18:
        raise ClassicalWaitError("riichi declaration junme must be in 1..18")
    if len(snapshot.remaining_tile_counts) != TILE_KIND_COUNT:
        raise ClassicalWaitError("remaining inventory must contain 34 base tile kinds")
    remaining = snapshot.remaining_tile_counts
    suited = _suited_rank(tile_index)
    candidate_remaining = remaining[tile_index]
    if type(candidate_remaining) is not int or not 0 <= candidate_remaining <= 4:
        raise ClassicalWaitError("candidate unseen count must be an integer in 0..4")
    public_by_wind = {opponent.wind.value: opponent for opponent in snapshot.opponents}
    if len(public_by_wind) != len(snapshot.opponents):
        raise ClassicalWaitError("public opponent Wind mapping is ambiguous")
    try:
        opponent = public_by_wind[target.wind]
    except KeyError as error:
        raise ClassicalWaitError(
            "target Wind is missing from public opponents"
        ) from error
    if len(opponent.discard_counts) != TILE_KIND_COUNT:
        raise ClassicalWaitError(
            "opponent river counts must contain 34 base tile kinds"
        )

    penchan_req, kanchan_req, low_req, high_req = _requirements(suited)
    penchan_possible, penchan_support = _support(remaining, penchan_req)
    kanchan_possible, kanchan_support = _support(remaining, kanchan_req)
    low_possible, low_support = _support(remaining, low_req)
    high_possible, high_support = _support(remaining, high_req)

    low_counterpart = None
    high_counterpart = None
    if suited is not None:
        suit, rank = suited
        if 1 <= rank <= 6:
            low_counterpart = _index(suit, rank + 3)
        if 4 <= rank <= 9:
            high_counterpart = _index(suit, rank - 3)

    values = (
        candidate_remaining / 4.0,
        penchan_possible,
        kanchan_possible,
        low_possible,
        high_possible,
        penchan_support,
        kanchan_support,
        low_support,
        high_support,
        float(opponent.discard_counts[tile_index] > 0),
        float(
            low_counterpart is not None and opponent.discard_counts[low_counterpart] > 0
        ),
        float(
            high_counterpart is not None
            and opponent.discard_counts[high_counterpart] > 0
        ),
        target.riichi_junme / 18.0,
    )
    if len(values) != FEATURE_DIM or len(values) != len(FEATURE_NAMES):
        raise ClassicalWaitError("feature dimension differs from the locked contract")
    if any(not 0.0 <= value <= 1.0 for value in values):
        raise ClassicalWaitError("all fixed v1 features must remain in [0,1]")
    return values


def eligible_cells(records: tuple[ClassicalExample, ...]):
    for record in records:
        for target in record.targets:
            if not target.eligible:
                continue
            for tile_index, label in enumerate(target.mask):
                yield (
                    record,
                    target,
                    tile_index,
                    feature_vector(record.snapshot, target, tile_index),
                    label,
                )


def tile_class(tile_index: int) -> str:
    suited = _suited_rank(tile_index)
    if suited is None:
        return "honor"
    _suit, rank = suited
    return "terminal" if rank in (1, 9) else "simple"


def riichi_turn_bucket(junme: int) -> str:
    if not 1 <= junme <= 18:
        raise ClassicalWaitError("riichi declaration junme must be in 1..18")
    if junme <= 6:
        return "01-06"
    if junme <= 12:
        return "07-12"
    return "13-18"


__all__ = [
    "ClassicalExample",
    "coverage_value",
    "eligible_cells",
    "feature_vector",
    "load_retained_records",
    "records_from_sequences",
    "riichi_turn_bucket",
    "tile_class",
]
