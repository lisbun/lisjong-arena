"""Small deterministic Phase 4 raw corpus fixtures."""

from dataclasses import replace
from functools import cache

from _phase2_anchor_fixtures import halt_at_turn_anchor
from _phase3_bootstrap_fixtures import resolved_provenance
from lisjong_engine.public_state import public_tile
from lisjong_engine.round_evidence import RoundEndedEvidence, RoundEndKind
from lisjong_engine.round_evidence_builder import build_round_evidence
from lisjong_engine.seat import Seat

from lisjong_arena.phase2_training_anchor.extraction import (
    FIRST_PARTY_SOURCE_CLASS,
    Phase2AnchorRecorder,
)
from lisjong_arena.phase2_training_anchor.player_safe_anchor import AnchorSourceIdentity
from lisjong_arena.phase4_raw_corpus.model import (
    FIXED_SEEDS,
    CheckpointTruth,
    DecisionCheckpoint,
    OpponentConcealedTruth,
    RawCorpus,
    RawGame,
    RawRound,
    ViewerEvidence,
)


@cache
def base_raw_game() -> RawGame:
    halted = halt_at_turn_anchor(FIXED_SEEDS[0])
    observation = halted.observation
    round_state = halted.round_state
    checkpoint_evidence = build_round_evidence(round_state, observation.viewer_seat)
    terminal = RoundEndedEvidence(kind=RoundEndKind.EXHAUSTIVE_DRAW)
    streams = tuple(
        ViewerEvidence(
            viewer,
            build_round_evidence(round_state, viewer) + (terminal,),
        )
        for viewer in Seat
    )
    checkpoint = DecisionCheckpoint(
        checkpoint_index=0,
        round_revision=round_state.revision,
        observation=observation,
        evidence_cutoff=len(checkpoint_evidence),
    )
    truth = CheckpointTruth(
        checkpoint_index=0,
        viewer_seat=observation.viewer_seat,
        opponents=tuple(
            OpponentConcealedTruth(
                seat,
                tuple(
                    sorted(
                        (public_tile(tile) for tile in round_state.hand_tiles(seat)),
                        key=lambda tile: (tile.tile_type.id, tile.is_red),
                    )
                ),
            )
            for seat in Seat
            if seat is not observation.viewer_seat
        ),
    )
    raw_round = RawRound(
        round_index=0,
        prevailing_wind=observation.prevailing_wind,
        hand_number=observation.hand_number,
        dealer_seat=observation.dealer_seat,
        honba=observation.honba,
        viewer_evidence=streams,
        checkpoints=(checkpoint,),
        training_truth=(truth,),
    )
    return RawGame(FIXED_SEEDS[0], (raw_round,))


def fixture_corpus() -> RawCorpus:
    return fixture_corpus_for_seeds(FIXED_SEEDS)


def fixture_corpus_for_seeds(seeds: tuple[int, ...]) -> RawCorpus:
    base = base_raw_game()
    return RawCorpus(
        resolved_provenance(),
        tuple(replace(base, seed=seed) for seed in seeds),
    )


@cache
def direct_phase2_sample():
    halted = halt_at_turn_anchor(FIXED_SEEDS[0])
    recorder = Phase2AnchorRecorder(
        halted.match_state,
        AnchorSourceIdentity(FIRST_PARTY_SOURCE_CLASS, FIXED_SEEDS[0]),
    )
    recorder.observe(halted.observation)
    return replace(recorder.samples[0], provenance=resolved_provenance())
