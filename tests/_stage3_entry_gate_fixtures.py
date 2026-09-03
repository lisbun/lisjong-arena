"""Deterministic Stage 3 Entry Gate fixtures.

実対局を12 hanchan回さずにStage 3のsplit / dataset / sequence / reference arm
boundaryを固定するため、既存Phase 4 fixtureのraw gameをStage 3 seed population
へ複製する。Policy populationそのものの実行はここでは検証しない。
"""

from dataclasses import replace
from functools import cache

from _phase2_anchor_fixtures import halt_at_turn_anchor
from _phase3_bootstrap_fixtures import resolved_provenance
from lisjong_engine.public_state import public_tile
from lisjong_engine.round_evidence import RoundEndedEvidence, RoundEndKind
from lisjong_engine.round_evidence_builder import build_round_evidence
from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.model import (
    CheckpointTruth,
    DecisionCheckpoint,
    OpponentConcealedTruth,
    RawCorpus,
    RawGame,
    RawRound,
    ViewerEvidence,
)
from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.pipeline import run_phase5_pipeline
from lisjong_arena.phase5_belief_dataset.split import (
    STAGE3_DEVELOPMENT_SEEDS,
    FirstPartySplitPolicy,
)

STAGE3_BASE_SEEDS = (1000, 1001)
"""Stage 3 fixtureで使う2つの独立したbase game。

同じseed populationでも中身が違う2 populationを作り、cross-population
evaluation pathを実際に通すために使う。
"""


@cache
def _base_raw_game(base_seed: int) -> RawGame:
    halted = halt_at_turn_anchor(base_seed)
    observation = halted.observation
    round_state = halted.round_state
    checkpoint_evidence = build_round_evidence(round_state, observation.viewer_seat)
    terminal = RoundEndedEvidence(kind=RoundEndKind.EXHAUSTIVE_DRAW)
    streams = tuple(
        ViewerEvidence(viewer, build_round_evidence(round_state, viewer) + (terminal,))
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
    return RawGame(base_seed, (raw_round,))


def stage3_corpus(base_seed: int = STAGE3_BASE_SEEDS[0]) -> RawCorpus:
    """Stage 3 seed populationのsynthetic raw corpus。"""
    base = _base_raw_game(base_seed)
    return RawCorpus(
        resolved_provenance(),
        tuple(replace(base, seed=seed) for seed in STAGE3_DEVELOPMENT_SEEDS),
    )


def stage3_population_artifacts(root, base_seed: int = STAGE3_BASE_SEEDS[0]):
    """1 populationのpersisted raw corpusとPhase 5 datasetを作る。"""
    persisted_raw = save_raw_corpus(stage3_corpus(base_seed), root / "raw")
    report = run_phase5_pipeline(
        persisted_raw, root / "dataset", FirstPartySplitPolicy.STAGE3_DEVELOPMENT
    )
    return persisted_raw, report.persisted_dataset.dataset
