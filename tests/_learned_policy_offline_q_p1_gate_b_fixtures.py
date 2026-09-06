"""Issue #162 P1 Gate B fixtures.

実RiichiEnvもreal #158 candidateも使わずに、Gate Bのcontract自体を検証する
ためのfixtureを提供する。

- comparator用の`DecisionContext`（win / response / own-turn discard）
- 100 game / 25 seed block分のraw single-round results
- `validate_gate_b_result()`が要求する形を完全に満たすresult document

fixture candidateは`LOCKED_P1_CANDIDATE`と異なる`ExpectedCandidateIdentities`
を使う。したがってcheckpoint / result documentの
`real_candidate_materialization`は常に`False`になり、fixtureのGate B結果を
real Gate B evidenceとしてclassificationへ記録することはできない。

hidden information（相手手牌・山・future outcome）は一切含めない。
"""

from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_fixtures import make_policy_input, make_round_state
from _round_stats_fixtures import neutral_seat_round_stats_tuple
from _single_round_artifact_fixtures import provenance
from lisjong.policy_contract import DecisionContext, Seat, Tile, Wind
from lisjong.policy_contract.action import (
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DiscardAction,
    KyuushuKyuuhaiAction,
    PassAction,
    PonAction,
    RiichiAction,
    RonAction,
    TsumoAction,
)
from lisjong.policy_contract.tile import TileCategory, TileType

from lisjong_arena.learned_policy_offline_q.p1_candidate import (
    LOCKED_P1_CANDIDATE,
    SERVING_CHECKPOINT_SCHEMA_VERSION,
    ExpectedCandidateIdentities,
    LoadedP1ServingCheckpoint,
    candidate_binding_document,
    candidate_logical_identity,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_b import (
    CANDIDATE_RETENTION_KEY,
    GATE_B_ORDERED_SEEDS,
    GATE_B_ROTATIONS_PER_SEED,
    RETENTION_BACKEND,
    build_gate_b_result,
)
from lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator import (
    PASSIVE_TSUMOGIRI_IDENTITY,
    create_passive_tsumogiri,
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
"""locked #158 candidateではない、明示的にfixtureなcandidate identity。"""


def fixture_binding(
    *,
    weights_digest: str = FIXTURE_WEIGHTS_DIGEST,
    support_digest: str = FIXTURE_SUPPORT_DIGEST,
) -> dict:
    return candidate_binding_document(
        canonical_model_weights_digest=weights_digest,
        support_set_digest=support_digest,
    )


def fixture_candidate_identity(**overrides) -> str:
    return candidate_logical_identity(fixture_binding(**overrides))


# --- Comparator decisions -------------------------------------------------


def _manzu(rank: int) -> Tile:
    return Tile(TileType(TileCategory.MANZU, rank))


def _pinzu(rank: int) -> Tile:
    return Tile(TileType(TileCategory.PINZU, rank))


SEAT = Seat.SEAT_0
DRAWN_TILE = _pinzu(5)
"""`make_own_hand()`の`drawn_tile`。tsumogiri discardはこのtileでなければならない。"""


def policy_input(seat: Seat = SEAT):
    return make_policy_input(seat, make_round_state(Wind.EAST, 1), (25_000,) * 4)


def decision(legal_actions, *, seat: Seat = SEAT) -> DecisionContext:
    return DecisionContext(input=policy_input(seat), legal_actions=legal_actions)


def tsumogiri_discard(seat: Seat = SEAT) -> DiscardAction:
    return DiscardAction(actor=seat, tile=DRAWN_TILE, tsumogiri=True)


def tedashi_discards(seat: Seat = SEAT, ranks=(1, 2, 3)) -> tuple[DiscardAction, ...]:
    return tuple(
        DiscardAction(actor=seat, tile=_manzu(rank), tsumogiri=False) for rank in ranks
    )


def ron_action(target: Seat = Seat.SEAT_1, rank: int = 1) -> RonAction:
    return RonAction(actor=SEAT, target=target, winning_tile=_manzu(rank))


def tsumo_action(rank: int = 1) -> TsumoAction:
    return TsumoAction(actor=SEAT, winning_tile=_manzu(rank))


def pass_action() -> PassAction:
    return PassAction(actor=SEAT)


def chi_action() -> ChiAction:
    return ChiAction(
        actor=SEAT,
        target=Seat.SEAT_3,
        called_tile=_manzu(3),
        consumed_tiles=(_manzu(1), _manzu(2)),
    )


def pon_action() -> PonAction:
    return PonAction(
        actor=SEAT,
        target=Seat.SEAT_2,
        called_tile=_manzu(5),
        consumed_tiles=(_manzu(5), _manzu(5)),
    )


def daiminkan_action() -> DaiminkanAction:
    return DaiminkanAction(
        actor=SEAT,
        target=Seat.SEAT_2,
        called_tile=_manzu(7),
        consumed_tiles=(_manzu(7), _manzu(7), _manzu(7)),
    )


def ankan_action() -> AnkanAction:
    return AnkanAction(actor=SEAT, tiles=(_manzu(9),) * 4)


def riichi_action() -> RiichiAction:
    return RiichiAction(actor=SEAT)


def kyuushu_action() -> KyuushuKyuuhaiAction:
    return KyuushuKyuuhaiAction(actor=SEAT)


# --- Gate B raw results ---------------------------------------------------


def scores_for_scaled_delta(
    rotation: int, scaled_delta: int
) -> tuple[int, int, int, int]:
    """`3 * candidate - sum(baseline 3席)`がexactに`scaled_delta`になるscores。"""
    scores = [25_000, 25_000, 25_000, 25_000]
    baseline_seat = (rotation + 1) % GATE_B_ROTATIONS_PER_SEED
    scores[baseline_seat] -= scaled_delta
    return tuple(scores)


def gate_b_game_results(
    *,
    scaled_delta_for_seed=lambda seed: 1_200,
    seeds: tuple[int, ...] = GATE_B_ORDERED_SEEDS,
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
        for rotation in range(GATE_B_ROTATIONS_PER_SEED)
    )


def positive_delta(seed: int) -> int:
    return 1_200


def negative_delta(seed: int) -> int:
    return -1_200


def inconclusive_delta(seed: int) -> int:
    """符号が交互に変わり、intervalが0を跨ぐseed block列。"""
    return 12_000 if seed % 2 == 0 else -12_000


def gate_b_evaluation_result(
    results: tuple[SingleRoundGameResult, ...],
    *,
    candidate_identity: str | None = None,
) -> SingleRoundEvaluationResult:
    identity = (
        fixture_candidate_identity()
        if candidate_identity is None
        else candidate_identity
    )
    plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(identity=identity, factory=create_passive_tsumogiri),
        baseline=PolicySpec(
            identity=PASSIVE_TSUMOGIRI_IDENTITY, factory=create_passive_tsumogiri
        ),
        seeds=tuple(dict.fromkeys(item.seed for item in results)),
    )
    return SingleRoundEvaluationResult(
        plan=plan,
        game_results=results,
        candidate_metrics=aggregate_candidate_metrics(identity, results),
    )


def save_gate_b_artifact(
    path: Path,
    results: tuple[SingleRoundGameResult, ...],
    *,
    candidate_identity: str | None = None,
):
    """install metadataへ依存せず、固定provenanceでartifactを保存し読み直す。"""
    result = gate_b_evaluation_result(results, candidate_identity=candidate_identity)
    with mock.patch(
        "lisjong_arena.single_round_artifact.collect_execution_provenance",
        return_value=provenance(),
    ):
        save_single_round_artifact(result, path)
    return load_single_round_artifact(path)


# --- Gate B result document ----------------------------------------------


def checkpoint_snapshot(
    expected: ExpectedCandidateIdentities,
    *,
    selected_epoch: int = 20,
) -> LoadedP1ServingCheckpoint:
    """`candidate_block()`が読むmanifest fieldだけを持つcheckpoint snapshot。

    `real_candidate_materialization`は`p1_candidate`のcheckpoint manifestと
    同じ規則（`expected == LOCKED_P1_CANDIDATE`）から導出する。fixtureが
    自分でtrueを立てられないため、result documentの検証がその再導出規則を
    実際に強制していることをtestできる。

    weightsのstrict loadは`p1_candidate`側のtestが担当するため、ここでは
    modelを持たないsnapshotで足りる。
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
            "canonical_model_weights_digest": (expected.canonical_model_weights_digest),
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
    """locked Issue #158 identitiesそのものを使うresult-level snapshot。

    real candidate classification pathのpositive testはこれで通す。weights
    bytesは持たないため、これがreal weightsの代用になることはない
    （weightsのstrict loadとdigest verifyは`p1_candidate`側で別にtestする）。
    """
    return checkpoint_snapshot(LOCKED_P1_CANDIDATE, **kwargs)


def fixture_diagnostics(
    *,
    total_decisions: int = 1_000,
    total_activations: int = 600,
    total_scaffold_fallbacks: int = 300,
    total_support_fallbacks: int = 100,
) -> ActivationDiagnostics:
    return ActivationDiagnostics(
        policy_instance_count=100,
        total_decisions=total_decisions,
        total_activations=total_activations,
        total_scaffold_fallbacks=total_scaffold_fallbacks,
        total_support_fallbacks=total_support_fallbacks,
    )


def gate_b_result_document(
    path: Path,
    *,
    scaled_delta_for_seed=positive_delta,
    checkpoint: LoadedP1ServingCheckpoint | None = None,
    diagnostics: ActivationDiagnostics | None = None,
) -> dict:
    """完全なGate B result documentを、実artifactのreadbackから組み立てる。

    `path`はartifact fileのpathである（実在するfileのbytesからdigestを取る）。
    """
    checkpoint = fixture_checkpoint() if checkpoint is None else checkpoint
    results = gate_b_game_results(scaled_delta_for_seed=scaled_delta_for_seed)
    artifact = save_gate_b_artifact(
        path, results, candidate_identity=checkpoint.candidate_identity
    )
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    with mock.patch(
        "lisjong_arena.learned_policy_offline_q.p1_gate_b.collect_execution_provenance",
        return_value=provenance(),
    ):
        return build_gate_b_result(
            checkpoint=checkpoint,
            artifact=artifact,
            artifact_path=path,
            summary=summary,
            diagnostics=(fixture_diagnostics() if diagnostics is None else diagnostics),
        )


LOCKED_RETENTION = {"backend": RETENTION_BACKEND, "key": CANDIDATE_RETENTION_KEY}
