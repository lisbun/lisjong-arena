"""Separated player-safe and training-only derivation from Phase 4 raw values."""

from lisjong.belief import wind_for_seat
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.domain_conversion import (
    public_meld_from_engine_meld,
    seat_from_engine_seat,
    tile_from_public_tile,
    wind_from_engine_wind,
)
from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase2_training_anchor.player_safe_anchor import (
    AnchorSourceIdentity,
    FrozenPlayerSafeAnchor,
    freeze_player_safe_anchor,
)
from lisjong_arena.phase2_training_anchor.training_labels import (
    ExactTrainingLabels,
    LabelAnchorIdentity,
    OpponentIdentity,
    expected_counts_for_concealed_hand,
    structural_wait_for_hand,
)
from lisjong_arena.phase2_training_anchor.training_sample import (
    TrainingSample,
    compose_training_sample,
)
from lisjong_arena.phase4_raw_corpus.model import (
    CheckpointTruth,
    DecisionCheckpoint,
    RawCorpus,
    RawGame,
    RawRound,
)


def derive_player_safe_anchor(
    *,
    game_seed: int,
    checkpoint: DecisionCheckpoint,
    raw_round: RawRound,
    anchor_index: int,
    rule_provenance,
) -> FrozenPlayerSafeAnchor:
    """Derive an anchor without accepting or reading training-only truth."""
    stream = next(
        value.evidence
        for value in raw_round.viewer_evidence
        if value.viewer_seat is checkpoint.viewer_seat
    )
    return freeze_player_safe_anchor(
        source=AnchorSourceIdentity(FIRST_PARTY_SOURCE_CLASS, game_seed),
        observation=checkpoint.observation,
        evidence=stream[: checkpoint.evidence_cutoff],
        round_revision=checkpoint.round_revision,
        anchor_index=anchor_index,
        rule_provenance=rule_provenance,
    )


def derive_training_labels(
    *, game_seed: int, checkpoint: DecisionCheckpoint, truth: CheckpointTruth
) -> ExactTrainingLabels:
    """Derive Phase 2 labels only from raw truth and documented observation context."""
    observation = checkpoint.observation
    if truth.checkpoint_index != checkpoint.checkpoint_index:
        raise ValueError("training truth checkpoint identity mismatch")
    if truth.viewer_seat is not checkpoint.viewer_seat:
        raise ValueError("training truth viewer identity mismatch")
    viewer = seat_from_engine_seat(observation.viewer_seat)
    dealer = seat_from_engine_seat(observation.dealer_seat)
    identities_and_truth = []
    for row in truth.opponents:
        target = seat_from_engine_seat(row.opponent_seat)
        identities_and_truth.append(
            (
                OpponentIdentity(
                    seat=target,
                    wind=wind_for_seat(target, dealer),
                    viewer_relative_offset=(target - viewer) % 4,
                ),
                tuple(tile_from_public_tile(tile) for tile in row.concealed_tiles),
                tuple(
                    public_meld_from_engine_meld(meld)
                    for meld in observation.melds[
                        tuple(EngineSeat).index(row.opponent_seat)
                    ].melds
                ),
            )
        )
    identities_and_truth.sort(key=lambda value: value[0].viewer_relative_offset)
    identity = LabelAnchorIdentity(
        game_seed=game_seed,
        hand_number=observation.hand_number,
        honba=observation.honba,
        round_revision=checkpoint.round_revision,
        viewer_seat=viewer,
        dealer_seat=dealer,
        prevailing_wind=wind_from_engine_wind(observation.prevailing_wind),
    )
    return ExactTrainingLabels(
        anchor_identity=identity,
        expected_counts=tuple(
            expected_counts_for_concealed_hand(opponent, concealed)
            for opponent, concealed, _ in identities_and_truth
        ),
        structural_waits=tuple(
            structural_wait_for_hand(opponent, concealed, melds)
            for opponent, concealed, melds in identities_and_truth
        ),
    )


def derive_turn_samples_from_game(
    game: RawGame, provenance
) -> tuple[TrainingSample, ...]:
    samples = []
    anchor_index = 0
    for raw_round in game.rounds:
        for checkpoint, truth in zip(
            raw_round.checkpoints, raw_round.training_truth, strict=True
        ):
            if checkpoint.decision_kind is not ObservationDecisionKind.TURN:
                continue
            anchor = derive_player_safe_anchor(
                game_seed=game.seed,
                checkpoint=checkpoint,
                raw_round=raw_round,
                anchor_index=anchor_index,
                rule_provenance=provenance.effective_rules,
            )
            labels = derive_training_labels(
                game_seed=game.seed, checkpoint=checkpoint, truth=truth
            )
            samples.append(compose_training_sample(anchor, labels, provenance))
            anchor_index += 1
    return tuple(samples)


def derive_turn_samples(corpus: RawCorpus) -> tuple[TrainingSample, ...]:
    return tuple(
        sample
        for game in corpus.games
        for sample in derive_turn_samples_from_game(game, corpus.provenance)
    )
