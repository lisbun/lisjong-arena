"""Deterministic Phase 4 raw corpus to compact Phase 5 dataset derivation."""

from collections import Counter, defaultdict

from lisjong_engine.observation import ObservationDecisionKind

from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase4_raw_corpus.derivation import derive_turn_samples_from_game
from lisjong_arena.phase4_raw_corpus.model import RawGame
from lisjong_arena.phase4_raw_corpus.persistence import PersistedRawCorpus
from lisjong_arena.phase5_belief_dataset.model import (
    BUILDER_SEMANTICS_ID,
    BeliefDataset,
    DatasetPartition,
    GameAssignment,
    GameIdentity,
    PartitionSummary,
    TargetAvailabilitySummary,
    TurnExampleReference,
)
from lisjong_arena.phase5_belief_dataset.split import (
    FirstPartySplitPolicy,
    assign_first_party_games,
)


def derive_turn_example_references(
    game: RawGame, assignment: GameAssignment
) -> tuple[TurnExampleReference, ...]:
    """Derive compact locators without accepting training truth as an argument."""
    if not isinstance(game, RawGame):
        raise TypeError("game must be a RawGame")
    if not isinstance(assignment, GameAssignment):
        raise TypeError("assignment must be a GameAssignment")
    expected_game = GameIdentity(FIRST_PARTY_SOURCE_CLASS, game.seed)
    if assignment.game != expected_game:
        raise ValueError("assignment must identify the raw game")
    references = []
    anchor_index = 0
    for raw_round in game.rounds:
        for checkpoint in raw_round.checkpoints:
            if checkpoint.decision_kind is not ObservationDecisionKind.TURN:
                continue
            observation = checkpoint.observation
            references.append(
                TurnExampleReference(
                    game=assignment.game,
                    partition=assignment.partition,
                    round_index=raw_round.round_index,
                    checkpoint_index=checkpoint.checkpoint_index,
                    anchor_index=anchor_index,
                    hand_number=observation.hand_number,
                    honba=observation.honba,
                    round_revision=checkpoint.round_revision,
                    viewer_seat=observation.viewer_seat,
                )
            )
            anchor_index += 1
    return tuple(references)


def _validate_reference_sample(
    reference: TurnExampleReference, sample: TrainingSample, game: RawGame
) -> None:
    if not isinstance(sample, TrainingSample):
        raise TypeError("resolved values must be existing TrainingSample values")
    try:
        raw_round = game.rounds[reference.round_index]
        checkpoint = raw_round.checkpoints[reference.checkpoint_index]
    except IndexError as error:
        raise ValueError(
            "dataset reference does not resolve to a raw checkpoint"
        ) from error
    if (
        raw_round.round_index != reference.round_index
        or checkpoint.checkpoint_index != reference.checkpoint_index
        or checkpoint.decision_kind is not ObservationDecisionKind.TURN
        or checkpoint.round_revision != reference.round_revision
        or checkpoint.observation.hand_number != reference.hand_number
        or checkpoint.observation.honba != reference.honba
        or checkpoint.observation.viewer_seat is not reference.viewer_seat
    ):
        raise ValueError(
            "dataset reference does not identify the expected raw checkpoint"
        )
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
        raise ValueError("dataset reference does not resolve to the expected anchor")


def _availability(samples: tuple[TrainingSample, ...]) -> TargetAvailabilitySummary:
    available = 0
    all_zero = 0
    non_zero = 0
    reasons = Counter()
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
    unavailable = sum(reasons.values())
    return TargetAvailabilitySummary(
        target_rows=len(samples) * 3,
        structural_wait_available=available,
        structural_wait_unavailable=unavailable,
        structural_wait_all_zero=all_zero,
        structural_wait_non_zero=non_zero,
        unavailable_reasons=tuple(sorted(reasons.items())),
    )


def build_phase5_belief_dataset(
    persisted_raw: PersistedRawCorpus,
    split_policy: FirstPartySplitPolicy,
) -> BeliefDataset:
    """Build a compact manifest while reusing Phase 2 ``TrainingSample`` values."""
    if not isinstance(persisted_raw, PersistedRawCorpus):
        raise TypeError("persisted_raw must be a PersistedRawCorpus")
    assignments = assign_first_party_games(persisted_raw.corpus, split_policy)
    references = []
    samples_by_partition: dict[DatasetPartition, list[TrainingSample]] = defaultdict(
        list
    )
    for game, assignment in zip(persisted_raw.corpus.games, assignments, strict=True):
        game_references = derive_turn_example_references(game, assignment)
        game_samples = derive_turn_samples_from_game(
            game, persisted_raw.corpus.provenance
        )
        if len(game_references) != len(game_samples):
            raise RuntimeError("TURN references and TrainingSample derivation differ")
        for reference, sample in zip(game_references, game_samples, strict=True):
            _validate_reference_sample(reference, sample, game)
        references.extend(game_references)
        samples_by_partition[assignment.partition].extend(game_samples)
    summaries = tuple(
        PartitionSummary(
            partition,
            len(samples_by_partition[partition]),
            _availability(tuple(samples_by_partition[partition])),
        )
        for partition in DatasetPartition
        if samples_by_partition[partition]
    )
    return BeliefDataset(
        raw_corpus_identity=persisted_raw.corpus_identity,
        provenance=persisted_raw.corpus.provenance,
        builder_semantics_id=BUILDER_SEMANTICS_ID,
        split_policy_id=split_policy.value,
        games=assignments,
        examples=tuple(references),
        partition_summaries=summaries,
    )


def resolve_training_samples(
    dataset: BeliefDataset, persisted_raw: PersistedRawCorpus
) -> tuple[TrainingSample, ...]:
    """Resolve every compact example to exactly one existing TrainingSample."""
    if not isinstance(dataset, BeliefDataset):
        raise TypeError("dataset must be a BeliefDataset")
    if not isinstance(persisted_raw, PersistedRawCorpus):
        raise TypeError("persisted_raw must be a PersistedRawCorpus")
    if dataset.raw_corpus_identity != persisted_raw.corpus_identity:
        raise ValueError("dataset is bound to a different raw corpus identity")
    if dataset.provenance != persisted_raw.corpus.provenance:
        raise ValueError("dataset and raw corpus provenance differ")
    references_by_game: dict[GameIdentity, list[TurnExampleReference]] = defaultdict(
        list
    )
    for reference in dataset.examples:
        references_by_game[reference.game].append(reference)
    resolved = []
    for game, assignment in zip(persisted_raw.corpus.games, dataset.games, strict=True):
        expected_game = GameIdentity(FIRST_PARTY_SOURCE_CLASS, game.seed)
        if assignment.game != expected_game:
            raise ValueError("dataset game order differs from the raw corpus")
        references = tuple(references_by_game[expected_game])
        samples = derive_turn_samples_from_game(game, persisted_raw.corpus.provenance)
        if len(references) != len(samples):
            raise ValueError(
                "dataset does not reference every TURN anchor exactly once"
            )
        for reference, sample in zip(references, samples, strict=True):
            _validate_reference_sample(reference, sample, game)
        resolved.extend(samples)
    if len(resolved) != len(dataset.examples):
        raise ValueError("dataset contains a reference outside its ordered games")
    return tuple(resolved)


__all__ = [
    "build_phase5_belief_dataset",
    "derive_turn_example_references",
    "resolve_training_samples",
]
