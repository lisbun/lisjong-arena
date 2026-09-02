"""Fresh TEST-only raw-corpus and dataset boundary for Phase 9."""

from collections import Counter
from dataclasses import dataclass

from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase4_raw_corpus.derivation import derive_turn_samples_from_game
from lisjong_arena.phase4_raw_corpus.persistence import PersistedRawCorpus
from lisjong_arena.phase5_belief_dataset.builder import (
    derive_turn_example_references,
    resolve_training_samples,
)
from lisjong_arena.phase5_belief_dataset.model import (
    BUILDER_SEMANTICS_ID,
    BeliefDataset,
    DatasetPartition,
    GameAssignment,
    GameIdentity,
    PartitionSummary,
    TargetAvailabilitySummary,
)
from lisjong_arena.phase8_sequential.protocol import SequenceKey

from .protocol import (
    HISTORICAL_REVISIONS,
    HOLDOUT_ROLE,
    HOLDOUT_SEEDS,
    LOCKED_RULE_FINGERPRINT,
    validate_holdout_games,
)

PHASE9_SPLIT_POLICY_ID = "phase9-seeds-160-179-confirmatory-test-only-v1"


def _availability(samples: tuple) -> TargetAvailabilitySummary:
    reasons = Counter()
    available = all_zero = non_zero = 0
    for sample in samples:
        for row in sample.labels.structural_waits:
            if row.mask is None:
                reasons[row.unavailable_reason.value] += 1
            else:
                available += 1
                if any(row.mask):
                    non_zero += 1
                else:
                    all_zero += 1
    return TargetAvailabilitySummary(
        target_rows=len(samples) * 3,
        structural_wait_available=available,
        structural_wait_unavailable=sum(reasons.values()),
        structural_wait_all_zero=all_zero,
        structural_wait_non_zero=non_zero,
        unavailable_reasons=tuple(sorted(reasons.items())),
    )


def validate_generation_provenance(persisted_raw: PersistedRawCorpus) -> None:
    if not isinstance(persisted_raw, PersistedRawCorpus):
        raise TypeError("persisted_raw must be a PersistedRawCorpus")
    provenance = persisted_raw.corpus.provenance
    revisions = provenance.source_revisions
    actual = {
        "lisjong": revisions.lisjong,
        "lisjong_engine": revisions.lisjong_engine,
        "lisjong_arena": revisions.lisjong_arena,
    }
    if actual != HISTORICAL_REVISIONS:
        raise ValueError("raw corpus does not use the exact historical revisions")
    if provenance.effective_rules.fingerprint != LOCKED_RULE_FINGERPRINT:
        raise ValueError("raw corpus effective-rule fingerprint differs")
    if (provenance.effective_rules.name, provenance.effective_rules.version) != (
        "project-standard-v1",
        1,
    ):
        raise ValueError("raw corpus effective-rule identity differs")
    seeds = tuple(game.seed for game in persisted_raw.corpus.games)
    if seeds != HOLDOUT_SEEDS:
        raise ValueError("raw corpus must contain exactly seeds 160..179")


def build_phase9_holdout_dataset(
    persisted_raw: PersistedRawCorpus,
) -> BeliefDataset:
    """Derive Phase-5-compatible references without joining the Phase 5 dataset."""
    validate_generation_provenance(persisted_raw)
    assignments = tuple(
        GameAssignment(
            GameIdentity(FIRST_PARTY_SOURCE_CLASS, seed), DatasetPartition.TEST
        )
        for seed in HOLDOUT_SEEDS
    )
    references = []
    samples = []
    for game, assignment in zip(persisted_raw.corpus.games, assignments, strict=True):
        game_references = derive_turn_example_references(game, assignment)
        game_samples = derive_turn_samples_from_game(
            game, persisted_raw.corpus.provenance
        )
        if len(game_references) != len(game_samples):
            raise RuntimeError("TURN references and labels differ")
        references.extend(game_references)
        samples.extend(game_samples)
    if not references:
        raise ValueError("Phase 9 holdout must contain eligible TURN anchors")
    return BeliefDataset(
        raw_corpus_identity=persisted_raw.corpus_identity,
        provenance=persisted_raw.corpus.provenance,
        builder_semantics_id=BUILDER_SEMANTICS_ID,
        split_policy_id=PHASE9_SPLIT_POLICY_ID,
        games=assignments,
        examples=tuple(references),
        partition_summaries=(
            PartitionSummary(
                DatasetPartition.TEST, len(references), _availability(tuple(samples))
            ),
        ),
    )


def validate_holdout_dataset(
    dataset: BeliefDataset, persisted_raw: PersistedRawCorpus
) -> tuple:
    validate_generation_provenance(persisted_raw)
    if not isinstance(dataset, BeliefDataset):
        raise TypeError("dataset must be a BeliefDataset")
    if dataset.raw_corpus_identity != persisted_raw.corpus_identity:
        raise ValueError("dataset and raw corpus identities differ")
    if dataset.provenance != persisted_raw.corpus.provenance:
        raise ValueError("dataset and raw corpus provenance differ")
    if dataset.builder_semantics_id != BUILDER_SEMANTICS_ID:
        raise ValueError("dataset builder semantics differ")
    if dataset.split_policy_id != PHASE9_SPLIT_POLICY_ID:
        raise ValueError("dataset is not Phase 9 confirmatory TEST-only")
    games = tuple(assignment.game for assignment in dataset.games)
    validate_holdout_games(games)
    if any(
        assignment.partition is not DatasetPartition.TEST
        for assignment in dataset.games
    ):
        raise ValueError("Phase 9 games must be TEST-only")
    if not dataset.examples or any(
        example.partition is not DatasetPartition.TEST for example in dataset.examples
    ):
        raise ValueError("Phase 9 anchors must be non-empty and TEST-only")
    if tuple(dict.fromkeys(example.game for example in dataset.examples)) != games:
        raise ValueError("Phase 9 anchors do not cover exact ordered games")
    expected = build_phase9_holdout_dataset(persisted_raw)
    if dataset != expected:
        raise ValueError("Phase 9 dataset differs from exact raw-corpus derivation")
    resolved = resolve_training_samples(dataset, persisted_raw)
    if len(resolved) != len(dataset.examples):
        raise RuntimeError("Phase 9 anchor readback differs")
    return resolved


@dataclass(frozen=True, slots=True)
class Phase9Sequence:
    key: SequenceKey
    steps: tuple

    def __post_init__(self) -> None:
        if not isinstance(self.key, SequenceKey):
            raise TypeError("key must be a SequenceKey")
        steps = tuple(self.steps)
        if not steps:
            raise ValueError("sequence must contain at least one step")
        references = tuple(step.example for step in steps)
        if any(
            reference.partition is not DatasetPartition.TEST
            or reference.game != self.key.game
            or reference.round_index != self.key.round_index
            or reference.viewer_seat is not self.key.viewer_seat
            for reference in references
        ):
            raise ValueError("sequence crosses game/round/viewer/TEST boundary")
        checkpoints = tuple(reference.checkpoint_index for reference in references)
        anchors = tuple(reference.anchor_index for reference in references)
        if checkpoints != tuple(sorted(set(checkpoints))):
            raise ValueError("sequence checkpoint order differs")
        if anchors != tuple(sorted(set(anchors))):
            raise ValueError("sequence anchor order differs")
        revisions = tuple(reference.round_revision for reference in references)
        if revisions != tuple(sorted(revisions)):
            raise ValueError("sequence revision order differs")
        for name in ("hand_number", "honba"):
            if len({getattr(reference, name) for reference in references}) != 1:
                raise ValueError(f"sequence {name} integrity differs")
        for step, reference in zip(steps, references, strict=True):
            anchor = step.sample.anchor
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


def build_holdout_sequences(examples: tuple) -> tuple[Phase9Sequence, ...]:
    if not examples:
        raise ValueError("Phase 9 sequence construction requires examples")
    grouped = {}
    for example in examples:
        reference = example.example
        if reference.partition is not DatasetPartition.TEST:
            raise ValueError("Phase 9 sequence input must be TEST-only")
        key = SequenceKey(reference.game, reference.round_index, reference.viewer_seat)
        grouped.setdefault(key, []).append(example)
    sequences = tuple(
        Phase9Sequence(
            key,
            tuple(sorted(values, key=lambda value: value.example.checkpoint_index)),
        )
        for key, values in grouped.items()
    )
    return tuple(
        sorted(
            sequences,
            key=lambda value: (
                value.key.game.source_class,
                value.key.game.game_seed,
                value.key.round_index,
                value.key.viewer_seat.value,
            ),
        )
    )


def holdout_lock_value(dataset: BeliefDataset) -> dict[str, object]:
    games = tuple(assignment.game for assignment in dataset.games)
    validate_holdout_games(games)
    return {
        "role": HOLDOUT_ROLE,
        "raw_corpus_identity": dataset.raw_corpus_identity,
        "dataset_identity": dataset.dataset_identity,
        "split_policy_id": PHASE9_SPLIT_POLICY_ID,
        "ordered_games": [
            {"source_class": game.source_class, "game_seed": game.game_seed}
            for game in games
        ],
        "eligible_turn_anchor_count": len(dataset.examples),
        "game_atomic_membership": True,
        "training_on_phase9_holdout": False,
        "model_selection_on_phase9_holdout": False,
    }


__all__ = [
    "PHASE9_SPLIT_POLICY_ID",
    "Phase9Sequence",
    "build_holdout_sequences",
    "build_phase9_holdout_dataset",
    "holdout_lock_value",
    "validate_generation_provenance",
    "validate_holdout_dataset",
]
