"""Issue #173 shanten guard fixtures.

実RiichiEnvもreal `#162` candidateも使わずに、guard selection semantics /
candidate identity / diagnostic result documentのcontract自体を検証するための
fixtureを提供する。

fixture candidateは`LOCKED_P1_CANDIDATE`と異なる`ExpectedCandidateIdentities`
を使う。したがってcheckpoint / result documentの
`real_candidate_materialization`は常に`False`になり、fixtureのdiagnostic
結果をreal Issue #173 evidenceとしてclassificationへ記録することはできない。

hidden information（相手手牌・山・future outcome）は一切含めない。
"""

from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_fixtures import make_policy_input, make_round_state
from _round_stats_fixtures import neutral_seat_round_stats_tuple
from _single_round_artifact_fixtures import provenance
from lisjong.policy_contract import DecisionContext, Seat, Tile, Wind
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.tile import TileCategory, TileType

from lisjong_arena.learned_policy_offline_q.p1_candidate import (
    LOCKED_P1_CANDIDATE,
    SERVING_CHECKPOINT_SCHEMA_VERSION,
    ExpectedCandidateIdentities,
    LoadedP1ServingCheckpoint,
    candidate_binding_document,
    candidate_logical_identity,
)
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard import GuardDiagnostics
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard_diagnostic import (
    ORDERED_SEEDS,
    ROTATIONS_PER_SEED,
    build_diagnostic_result,
)
from lisjong_arena.learned_policy_offline_q.strength import ActivationDiagnostics
from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.single_round_artifact import (
    load_single_round_artifact,
    save_single_round_artifact,
)
from lisjong_arena.single_round_evaluation import (
    aggregate_candidate_metrics,
    summarize_single_round_strength,
)

FIXTURE_WEIGHTS_DIGEST = "1" * 64
FIXTURE_DATASET_IDENTITY = "2" * 64
FIXTURE_SUPPORT_DIGEST = "3" * 64

FIXTURE_EXPECTED = ExpectedCandidateIdentities(
    canonical_model_weights_digest=FIXTURE_WEIGHTS_DIGEST,
    source_dataset_identity=FIXTURE_DATASET_IDENTITY,
    support_set_digest=FIXTURE_SUPPORT_DIGEST,
)
"""locked #162 candidateではない、明示的にfixtureなcandidate identity。"""


def fixture_binding(
    *,
    weights_digest: str = FIXTURE_WEIGHTS_DIGEST,
    support_digest: str = FIXTURE_SUPPORT_DIGEST,
) -> dict:
    return candidate_binding_document(
        canonical_model_weights_digest=weights_digest,
        support_set_digest=support_digest,
    )


def fixture_base_candidate_identity(**overrides) -> str:
    return candidate_logical_identity(fixture_binding(**overrides))


# --- Hand fixture ------------------------------------------------------------
#
# `make_own_hand()`（`_learned_policy_offline_q_fixtures.py`）は
# 1m2m3m4m5m6m7m8m9m1p2p3p4p5p（tenpai, shanten=0）である。この固定手牌上で:
#
#   discarding any of 1m..9m -> shanten worsens (0 -> 1)
#   discarding 1p, 2p, 4p, 5p -> shanten keeps (0 -> 0)
#   discarding 3p -> shanten worsens (0 -> 1)
#
# は`keep_shanten_tile_mask()`から機械的に確認済みである（このfixture module
# 自体はshanten semanticsを一切再実装しない）。


def _manzu(rank: int) -> Tile:
    return Tile(TileType(TileCategory.MANZU, rank))


def _pinzu(rank: int) -> Tile:
    return Tile(TileType(TileCategory.PINZU, rank))


WORSEN_TILES = tuple(_manzu(rank) for rank in range(1, 10)) + (_pinzu(3),)
KEEP_TILES = (_pinzu(1), _pinzu(2), _pinzu(4), _pinzu(5))

SEAT = Seat.SEAT_0


def policy_input(seat: Seat = SEAT):
    return make_policy_input(seat, make_round_state(Wind.EAST, 1), (25_000,) * 4)


def discard(tile: Tile, *, seat: Seat = SEAT, tsumogiri: bool = False) -> DiscardAction:
    return DiscardAction(actor=seat, tile=tile, tsumogiri=tsumogiri)


def decision(legal_actions, *, seat: Seat = SEAT) -> DecisionContext:
    return DecisionContext(input=policy_input(seat), legal_actions=legal_actions)


def mixed_decision(
    *, worsen_count: int = 2, keep_count: int = 2, seat: Seat = SEAT
) -> DecisionContext:
    """worsen / keepを両方含むeligible choice decision（K non-empty）。"""
    actions = tuple(
        discard(tile, seat=seat) for tile in WORSEN_TILES[:worsen_count]
    ) + tuple(discard(tile, seat=seat) for tile in KEEP_TILES[:keep_count])
    return decision(actions, seat=seat)


def all_worsen_decision(*, count: int = 3, seat: Seat = SEAT) -> DecisionContext:
    """keep-shanten legal discardが1件も無いeligible choice decision（K空）。"""
    return decision(
        tuple(discard(tile, seat=seat) for tile in WORSEN_TILES[:count]), seat=seat
    )


def all_keep_decision(*, count: int = 2, seat: Seat = SEAT) -> DecisionContext:
    """legal discardが全てkeep-shantenのeligible choice decision。"""
    return decision(
        tuple(discard(tile, seat=seat) for tile in KEEP_TILES[:count]), seat=seat
    )


# --- Checkpoint snapshot -------------------------------------------------


def checkpoint_snapshot(
    expected: ExpectedCandidateIdentities, *, selected_epoch: int = 20
) -> LoadedP1ServingCheckpoint:
    """`guarded_candidate_block()` / `unguarded_candidate_block()`が読む
    manifest fieldだけを持つcheckpoint snapshot。

    `real_candidate_materialization`は`p1_candidate`のcheckpoint manifestと
    同じ規則（`expected == LOCKED_P1_CANDIDATE`）から導出する。weightsの
    strict loadは`p1_candidate`側のtestが担当するため、ここではmodelを持たない
    snapshotで足りる。
    """
    binding = candidate_binding_document(
        canonical_model_weights_digest=expected.canonical_model_weights_digest,
        support_set_digest=expected.support_set_digest,
    )
    return LoadedP1ServingCheckpoint(
        path=Path("fixture-checkpoint"),
        manifest={
            "candidate_identity": candidate_logical_identity(binding),
            "candidate_binding": binding,
            "canonical_model_weights_digest": expected.canonical_model_weights_digest,
            "checkpoint_schema_version": SERVING_CHECKPOINT_SCHEMA_VERSION,
            "materialization_source": "exact-retained-158-weights",
            "selected_epoch": selected_epoch,
            "source_dataset_identity": expected.source_dataset_identity,
            "supported_indices_digest": expected.support_set_digest,
            "expected_identities": expected.to_document(),
            "real_candidate_materialization": expected == LOCKED_P1_CANDIDATE,
        },
        model=None,
        supported_indices=frozenset({0, 1}),
    )


def fixture_checkpoint(**kwargs) -> LoadedP1ServingCheckpoint:
    """fixture identitiesのcheckpoint snapshot（常にnon-real）。"""
    return checkpoint_snapshot(FIXTURE_EXPECTED, **kwargs)


def locked_checkpoint(**kwargs) -> LoadedP1ServingCheckpoint:
    """locked Issue #162 identitiesそのものを使うresult-level snapshot。"""
    return checkpoint_snapshot(LOCKED_P1_CANDIDATE, **kwargs)


# --- Raw single-round results ----------------------------------------------


def scores_for_scaled_delta(
    rotation: int, scaled_delta: int
) -> tuple[int, int, int, int]:
    """`3 * candidate(G) - sum(baseline(U) 3席)`がexactに`scaled_delta`になるscores。"""
    scores = [25_000, 25_000, 25_000, 25_000]
    baseline_seat = (rotation + 1) % ROTATIONS_PER_SEED
    scores[baseline_seat] -= scaled_delta
    return tuple(scores)


def diagnostic_game_results(
    *,
    scaled_delta_for_seed=lambda seed: 1_200,
    seeds: tuple[int, ...] = ORDERED_SEEDS,
) -> tuple[SingleRoundGameResult, ...]:
    """25 seed x 4 rotationのraw results。seed blockごとのdeltaを指定できる。"""
    return tuple(
        SingleRoundGameResult(
            seed=seed,
            rotation=rotation,
            game_mode="4p-red-single",
            candidate_seat=Seat(rotation),
            scores=scores_for_scaled_delta(rotation, scaled_delta_for_seed(seed)),
            seat_round_stats=neutral_seat_round_stats_tuple(
                scores_for_scaled_delta(rotation, scaled_delta_for_seed(seed))
            ),
        )
        for seed in seeds
        for rotation in range(ROTATIONS_PER_SEED)
    )


def positive_delta(seed: int) -> int:
    return 1_200


def negative_delta(seed: int) -> int:
    return -1_200


def inconclusive_delta(seed: int) -> int:
    """符号が交互に変わり、intervalが0を跨ぐseed block列。"""
    return 12_000 if seed % 2 == 0 else -12_000


def diagnostic_evaluation_result(
    results: tuple[SingleRoundGameResult, ...],
    *,
    guarded_identity: str | None = None,
    unguarded_identity: str = "unguarded-fixture-identity",
) -> SingleRoundEvaluationResult:
    identity = (
        fixture_base_candidate_identity()
        if guarded_identity is None
        else guarded_identity
    )
    plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(identity=identity, factory=object),
        baseline=PolicySpec(identity=unguarded_identity, factory=object),
        seeds=tuple(dict.fromkeys(item.seed for item in results)),
    )
    return SingleRoundEvaluationResult(
        plan=plan,
        game_results=results,
        candidate_metrics=aggregate_candidate_metrics(identity, results),
    )


def save_diagnostic_artifact(
    path: Path,
    results: tuple[SingleRoundGameResult, ...],
    *,
    guarded_identity: str | None = None,
    unguarded_identity: str = "unguarded-fixture-identity",
):
    """install metadataへ依存せず、固定provenanceでartifactを保存し読み直す。"""
    result = diagnostic_evaluation_result(
        results,
        guarded_identity=guarded_identity,
        unguarded_identity=unguarded_identity,
    )
    with mock.patch(
        "lisjong_arena.single_round_artifact.collect_execution_provenance",
        return_value=provenance(),
    ):
        save_single_round_artifact(result, path)
    return load_single_round_artifact(path)


# --- Diagnostics fixtures ----------------------------------------------------


def fixture_activation_diagnostics(
    *,
    total_decisions: int = 100,
    total_activations: int = 100,
    total_scaffold_fallbacks: int = 0,
    total_support_fallbacks: int = 0,
) -> ActivationDiagnostics:
    return ActivationDiagnostics(
        policy_instance_count=100,
        total_decisions=total_decisions,
        total_activations=total_activations,
        total_scaffold_fallbacks=total_scaffold_fallbacks,
        total_support_fallbacks=total_support_fallbacks,
    )


def fixture_guard_diagnostics(*, active: bool = True) -> GuardDiagnostics:
    """`GuardDiagnostics`の内部不変条件を満たす最小fixture。

    `active=False`は`action_change_count == 0`（`SHANTEN GUARD INACTIVE`が
    intervalの符号より優先されることを検証するため）を満たす。
    """
    if active:
        return GuardDiagnostics(
            learned_decision_count=100,
            keep_shanten_available_count=100,
            no_keep_shanten_available_count=0,
            unguarded_worsen_count=100,
            unguarded_keep_count=0,
            action_change_count=100,
            guarded_worsen_among_available_count=0,
            mean_baseline_post_discard_shanten=1.0,
            mean_guarded_post_discard_shanten=0.0,
            paired_lower_count=100,
            paired_equal_count=0,
            paired_higher_count=0,
        )
    return GuardDiagnostics(
        learned_decision_count=100,
        keep_shanten_available_count=0,
        no_keep_shanten_available_count=100,
        unguarded_worsen_count=40,
        unguarded_keep_count=60,
        action_change_count=0,
        guarded_worsen_among_available_count=0,
        mean_baseline_post_discard_shanten=0.4,
        mean_guarded_post_discard_shanten=0.4,
        paired_lower_count=0,
        paired_equal_count=100,
        paired_higher_count=0,
    )


def diagnostic_result_document(
    path: Path,
    *,
    scaled_delta_for_seed=positive_delta,
    checkpoint: LoadedP1ServingCheckpoint | None = None,
    guard_diagnostics: GuardDiagnostics | None = None,
    guarded_activation_diagnostics: ActivationDiagnostics | None = None,
    unguarded_activation_diagnostics: ActivationDiagnostics | None = None,
) -> dict:
    """完全なdiagnostic result documentを、実artifactのreadbackから組み立てる。

    `path`はartifact fileのpathである（実在するfileのbytesからdigestを取る）。
    """
    from lisjong_arena.learned_policy_offline_q.p1_shanten_guard_diagnostic import (
        guard_candidate_identity as _guard_candidate_identity,
    )

    checkpoint = fixture_checkpoint() if checkpoint is None else checkpoint
    guarded_identity = _guard_candidate_identity(
        checkpoint.manifest["candidate_binding"]
    )
    results = diagnostic_game_results(scaled_delta_for_seed=scaled_delta_for_seed)
    artifact = save_diagnostic_artifact(
        path,
        results,
        guarded_identity=guarded_identity,
        unguarded_identity=checkpoint.candidate_identity,
    )
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    with mock.patch(
        "lisjong_arena.learned_policy_offline_q.p1_shanten_guard_diagnostic."
        "collect_execution_provenance",
        return_value=provenance(),
    ):
        return build_diagnostic_result(
            checkpoint=checkpoint,
            artifact=artifact,
            artifact_path=path,
            summary=summary,
            guard_diagnostics=(
                fixture_guard_diagnostics()
                if guard_diagnostics is None
                else guard_diagnostics
            ),
            guarded_activation_diagnostics=(
                fixture_activation_diagnostics()
                if guarded_activation_diagnostics is None
                else guarded_activation_diagnostics
            ),
            unguarded_activation_diagnostics=(
                fixture_activation_diagnostics(
                    total_decisions=300, total_activations=300
                )
                if unguarded_activation_diagnostics is None
                else unguarded_activation_diagnostics
            ),
        )
