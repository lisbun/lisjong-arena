"""Issue #165 FiniteHorizon-teacher curriculum fixtures.

実RiichiEnv hanchanもreal 100-game rolloutも使わずに、`#165`のcontract自体を
検証するためのfixtureを提供する。

- 実手牌を持つ合成curriculum dataset（arm別のlegal discard / behavior分布）
- 100 game / 25 seed block分のraw single-round results
- `validate_rollout_result()`が要求する形を完全に満たすresult document
- torchを必要としないcandidate manifest snapshot

`#158` P1 derivationは`own_hand.tile_counts`から純手牌を復元するため、
feature rowは`_learned_policy_offline_q_p1_gate_a_fixtures`と同じ実手牌
helperを再利用する。hidden information（相手手牌・山・future outcome）は
一切含めない。

Arm YとArm Fのlegal discard集合を変えることで、teacher差がTRAIN support、
row分布、candidate identityへ波及することをtestできる合成pairにしている。
"""

from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_diagnosis_fixtures import (
    discard_index,
    feature_row_with_hand,
    hand_tiles,
)
from _round_stats_fixtures import neutral_seat_round_stats_tuple
from _single_round_artifact_fixtures import provenance
from lisjong.policy_contract import Seat

from lisjong_arena.learned_policy_offline_q.artifact import vocabulary_block
from lisjong_arena.learned_policy_offline_q.fh_curriculum import (
    DATASET_ORDERED_SEEDS,
    ROLLOUT_ORDERED_SEEDS,
    ROLLOUT_ROTATIONS_PER_SEED,
    CurriculumArm,
    arm_block,
    candidate_retention_block,
    split_for_seed,
    teacher_block,
)
from lisjong_arena.learned_policy_offline_q.fh_curriculum_candidate import (
    CANDIDATE_SCHEMA_VERSION,
    CHECKPOINT_SELECTION,
    SELECTED_EPOCH,
    LoadedArmCandidate,
    candidate_binding_document,
    candidate_logical_identity,
)
from lisjong_arena.learned_policy_offline_q.fh_curriculum_dataset import (
    FiniteHorizonCurriculumDatasetWriter,
)
from lisjong_arena.learned_policy_offline_q.fh_curriculum_rollout import (
    build_rollout_result,
)
from lisjong_arena.learned_policy_offline_q.model import MacroTransitionRow
from lisjong_arena.learned_policy_offline_q.p1_features import p1_feature_block
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    P1_EXPECTED_PARAMETER_COUNT,
    p1_model_block,
    p1_training_block,
)
from lisjong_arena.learned_policy_offline_q.p1_serving import (
    fallback_policy_block,
    hybrid_activation_block,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    PROTOCOL_ID,
    VOCABULARY_SIZE,
    action_family,
)
from lisjong_arena.learned_policy_offline_q.strength import ActivationDiagnostics
from lisjong_arena.learned_policy_offline_q.support import support_set_identity
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

FIXTURE_PROVENANCE = {
    "execution_environment": "riichienv",
    "lisjong_arena_version": "0.1.0",
    "lisjong_arena_revision": "a" * 40,
    "lisjong_version": "0.1.0",
    "lisjong_revision": "b" * 40,
    "lisjong_engine_version": "0.1.0",
    "lisjong_engine_revision": "c" * 40,
    "riichienv_version": "0.4.8",
    "python_version": "3.14.0",
}

CURRICULUM_HAND = "123456789m123p1s5z"
"""4面子 + `1s` + `5z`。`1s` / `5z`は打っても向聴数が変わらない。"""

KEEP_SHANTEN_TILE = "1s"
WORSEN_SHANTEN_TILE = "9m"
SECOND_KEEP_SHANTEN_TILE = "5z"

ARM_DISCARD_LABELS = {
    CurriculumArm.CONTROL: (WORSEN_SHANTEN_TILE, KEEP_SHANTEN_TILE),
    CurriculumArm.CURRICULUM: (
        WORSEN_SHANTEN_TILE,
        KEEP_SHANTEN_TILE,
        SECOND_KEEP_SHANTEN_TILE,
    ),
}
"""armごとに異なるlegal discard集合。teacher差がsupportへ波及する状況を作る。"""

ARM_LEGAL_INDICES = {
    arm: tuple(discard_index(label) for label in labels)
    for arm, labels in ARM_DISCARD_LABELS.items()
}

ARM_FAMILY_COUNTS = {
    CurriculumArm.CONTROL: {"discard": 6, "pass": 2, "riichi": 1},
    CurriculumArm.CURRICULUM: {"discard": 6, "pass": 1, "tsumo": 1},
}
"""合成teacher decisionのaction-family分布。armごとに違う値にしてある。"""


def arm_legal_mask(arm: CurriculumArm) -> tuple[bool, ...]:
    chosen = set(ARM_LEGAL_INDICES[arm])
    return tuple(index in chosen for index in range(VOCABULARY_SIZE))


def arm_feature_values(seed: int, ordinal: int) -> tuple[float, ...]:
    """実手牌を持つdeterministicなfeature row。

    own_hand以外のindexにseed / ordinal由来のfillerを入れる。P1 derivationが
    own_handグループだけを読むことを、この差で確認できる。
    """
    values = feature_row_with_hand(hand_tiles(CURRICULUM_HAND))
    values[0] = float(seed % 7) / 8.0
    values[1] = float(ordinal % 5) / 8.0
    return tuple(values)


def arm_transition_row(
    arm: CurriculumArm, seed: int, ordinal: int, *, rows_per_game: int
) -> MacroTransitionRow:
    indices = ARM_LEGAL_INDICES[arm]
    behavior_index = indices[ordinal % len(indices)]
    terminal = ordinal == rows_per_game - 1
    kwargs = dict(
        seed=seed,
        split=split_for_seed(seed),
        round_ordinal=ordinal // 4,
        round_wind="east",
        hand_number=1 + (ordinal // 4) % 4,
        honba=0,
        actor_seat=0,
        step_ordinal=ordinal,
        decision_ordinal=ordinal,
        feature_values=arm_feature_values(seed, ordinal),
        legal_mask=arm_legal_mask(arm),
        behavior_action_index=behavior_index,
        behavior_action_family=action_family(behavior_index),
        reward=float(ordinal - (rows_per_game // 2)) / 10000.0,
        terminal=terminal,
    )
    if terminal:
        kwargs.update(
            next_step_ordinal=None,
            next_decision_ordinal=None,
            next_feature_values=None,
            next_legal_mask=None,
        )
    else:
        kwargs.update(
            next_step_ordinal=ordinal + 1,
            next_decision_ordinal=ordinal + 1,
            next_feature_values=arm_feature_values(seed, ordinal + 1),
            next_legal_mask=arm_legal_mask(arm),
        )
    return MacroTransitionRow(**kwargs)


def write_curriculum_dataset(
    destination,
    arm: CurriculumArm,
    *,
    rows_per_game: int = 6,
    provenance_document: dict | None = None,
):
    """locked seed populationを満たす、実手牌つき合成arm datasetを書き出す。"""
    writer = FiniteHorizonCurriculumDatasetWriter(
        destination,
        arm=arm,
        provenance=FIXTURE_PROVENANCE
        if provenance_document is None
        else provenance_document,
    )
    counts = dict(ARM_FAMILY_COUNTS[arm])
    counts["discard"] = rows_per_game
    try:
        for seed in DATASET_ORDERED_SEEDS:
            writer.add_game(
                seed=seed,
                split=split_for_seed(seed),
                scores=(25_000, 25_000, 25_000, 25_000),
                ranks=(1, 2, 3, 4),
                decision_count=sum(counts.values()),
                teacher_action_family_counts=counts,
                rows=(
                    arm_transition_row(arm, seed, ordinal, rows_per_game=rows_per_game)
                    for ordinal in range(rows_per_game)
                ),
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise


def write_dataset_pair(root: Path, *, rows_per_game: int = 6):
    """Arm Y / Arm Fの合成dataset pairを、別pathへ書き出す。"""
    return (
        write_curriculum_dataset(
            root / "dataset-arm-y", CurriculumArm.CONTROL, rows_per_game=rows_per_game
        ),
        write_curriculum_dataset(
            root / "dataset-arm-f",
            CurriculumArm.CURRICULUM,
            rows_per_game=rows_per_game,
        ),
    )


# --- Candidate snapshots (no torch) ---------------------------------------

FIXTURE_WEIGHTS_DIGEST = {
    CurriculumArm.CONTROL: "1" * 64,
    CurriculumArm.CURRICULUM: "2" * 64,
}
FIXTURE_DATASET_IDENTITY = {
    CurriculumArm.CONTROL: "3" * 64,
    CurriculumArm.CURRICULUM: "4" * 64,
}
FIXTURE_SUPPORT_INDICES = {
    CurriculumArm.CONTROL: ARM_LEGAL_INDICES[CurriculumArm.CONTROL],
    CurriculumArm.CURRICULUM: ARM_LEGAL_INDICES[CurriculumArm.CURRICULUM],
}


def fixture_binding(
    arm: CurriculumArm,
    *,
    weights_digest: str | None = None,
    dataset_identity: str | None = None,
    support_digest: str | None = None,
    source_revisions: dict | None = None,
) -> dict:
    return candidate_binding_document(
        arm=arm,
        source_dataset_identity=(
            FIXTURE_DATASET_IDENTITY[arm]
            if dataset_identity is None
            else dataset_identity
        ),
        canonical_model_weights_digest=(
            FIXTURE_WEIGHTS_DIGEST[arm] if weights_digest is None else weights_digest
        ),
        support_set_digest=(
            support_set_identity(sorted(FIXTURE_SUPPORT_INDICES[arm]))
            if support_digest is None
            else support_digest
        ),
        source_revisions=(
            FIXTURE_PROVENANCE if source_revisions is None else source_revisions
        ),
    )


def fixture_candidate(arm: CurriculumArm, **overrides) -> LoadedArmCandidate:
    """torchを持たないcandidate manifest snapshot。

    `candidate_block()`と`require_candidate_pair()`が読むmanifest fieldを
    すべて持つ。weightsのstrict loadは`fh_curriculum_candidate`側のML testが
    担当するため、ここではmodelを持たないsnapshotで足りる。
    """
    binding = fixture_binding(arm, **overrides)
    indices = sorted(FIXTURE_SUPPORT_INDICES[arm])
    manifest = {
        "checkpoint_schema_version": CANDIDATE_SCHEMA_VERSION,
        "experiment": {
            "experiment_id": "arena-learned-policy-finite-horizon-curriculum-165",
            "source_issue": "lisbun/lisjong-arena#165",
            "predecessor_issues": [
                "lisbun/lisjong-arena#140",
                "lisbun/lisjong-arena#152",
                "lisbun/lisjong-arena#158",
                "lisbun/lisjong-arena#162",
            ],
            "parent_issue": "lisbun/lisjong-project#45",
        },
        "protocol_id": PROTOCOL_ID,
        "arm": arm_block(arm),
        "teacher": teacher_block(arm),
        "candidate_identity": candidate_logical_identity(binding),
        "candidate_binding": binding,
        "canonical_model_weights_digest": binding["canonical_model_weights_digest"],
        "weights_sha256": "5" * 64,
        "weights_bytes": 1024,
        "source_dataset_identity": binding["source_dataset_identity"],
        "source_dataset_arm": arm.value,
        "p1_feature": p1_feature_block(),
        "action_vocabulary": vocabulary_block(),
        "supported_indices": list(indices),
        "supported_indices_digest": binding["support_set_digest"],
        "support_size": len(indices),
        "support_coverage": {
            "train_row_count": 120,
            "validation_row_count": 36,
            "train_support_complete_rate": 1.0,
            "validation_support_complete_rate": 1.0,
            "combined_support_complete_rate": 1.0,
            "unsupported_index_count": 0,
        },
        "model": p1_model_block(),
        "training": p1_training_block(),
        "parameter_count": P1_EXPECTED_PARAMETER_COUNT,
        "selected_epoch": SELECTED_EPOCH,
        "checkpoint_selection": CHECKPOINT_SELECTION,
        "hybrid_activation": hybrid_activation_block(),
        "fallback_policy": fallback_policy_block(),
        "source_revisions": dict(binding["source_revisions"]),
        "runtime_provenance": dict(binding["source_revisions"]),
        "source_revisions_match_dataset": True,
        "runtime": {"device": "cpu", "torch_version": "2.13.0", "torch_threads": 1},
        "retention": candidate_retention_block(arm),
        "strength_claim": None,
    }
    return LoadedArmCandidate(
        path=Path(f"fixture-candidate-{arm.value}"),
        manifest=manifest,
        model=None,
        arm=arm,
        supported_indices=frozenset(indices),
    )


# --- Rollout raw results ---------------------------------------------------


def scores_for_scaled_delta(
    rotation: int, scaled_delta: int
) -> tuple[int, int, int, int]:
    """`3 * candidate - sum(baseline 3席)`がexactに`scaled_delta`になるscores。"""
    scores = [25_000, 25_000, 25_000, 25_000]
    baseline_seat = (rotation + 1) % ROLLOUT_ROTATIONS_PER_SEED
    scores[baseline_seat] -= scaled_delta
    return tuple(scores)


def rollout_game_results(
    *,
    scaled_delta_for_seed=lambda seed: 1_200,
    seeds: tuple[int, ...] = ROLLOUT_ORDERED_SEEDS,
) -> tuple[SingleRoundGameResult, ...]:
    """25 seed x 4 rotationのraw results。"""
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
        for rotation in range(ROLLOUT_ROTATIONS_PER_SEED)
    )


def positive_delta(seed: int) -> int:
    return 1_200


def negative_delta(seed: int) -> int:
    return -1_200


def inconclusive_delta(seed: int) -> int:
    """符号が交互に変わり、intervalが0を跨ぐseed block列。"""
    return 12_000 if seed % 2 == 0 else -12_000


def rollout_evaluation_result(
    results: tuple[SingleRoundGameResult, ...],
    *,
    candidate_identity: str,
    baseline_identity: str,
) -> SingleRoundEvaluationResult:
    def _factory():
        raise AssertionError("fixture plans never instantiate a Policy")

    plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(identity=candidate_identity, factory=_factory),
        baseline=PolicySpec(identity=baseline_identity, factory=_factory),
        seeds=tuple(dict.fromkeys(item.seed for item in results)),
    )
    return SingleRoundEvaluationResult(
        plan=plan,
        game_results=results,
        candidate_metrics=aggregate_candidate_metrics(candidate_identity, results),
    )


def save_rollout_artifact(
    path: Path,
    results: tuple[SingleRoundGameResult, ...],
    *,
    candidate_identity: str,
    baseline_identity: str,
):
    """install metadataへ依存せず、固定provenanceでartifactを保存し読み直す。"""
    result = rollout_evaluation_result(
        results,
        candidate_identity=candidate_identity,
        baseline_identity=baseline_identity,
    )
    with mock.patch(
        "lisjong_arena.single_round_artifact.collect_execution_provenance",
        return_value=provenance(),
    ):
        save_single_round_artifact(result, path)
    return load_single_round_artifact(path)


def fixture_diagnostics(
    *,
    total_decisions: int = 1_000,
    total_activations: int = 700,
    total_scaffold_fallbacks: int = 250,
    total_support_fallbacks: int = 50,
) -> ActivationDiagnostics:
    return ActivationDiagnostics(
        policy_instance_count=100,
        total_decisions=total_decisions,
        total_activations=total_activations,
        total_scaffold_fallbacks=total_scaffold_fallbacks,
        total_support_fallbacks=total_support_fallbacks,
    )


def rollout_result_document(
    path: Path,
    *,
    scaled_delta_for_seed=positive_delta,
    curriculum: LoadedArmCandidate | None = None,
    control: LoadedArmCandidate | None = None,
    curriculum_diagnostics: ActivationDiagnostics | None = None,
    control_diagnostics: ActivationDiagnostics | None = None,
    offline_diagnostics: dict | None = None,
) -> dict:
    """完全なrollout result documentを、実artifactのreadbackから組み立てる。"""
    curriculum = (
        fixture_candidate(CurriculumArm.CURRICULUM)
        if curriculum is None
        else curriculum
    )
    control = fixture_candidate(CurriculumArm.CONTROL) if control is None else control
    results = rollout_game_results(scaled_delta_for_seed=scaled_delta_for_seed)
    artifact = save_rollout_artifact(
        path,
        results,
        candidate_identity=curriculum.candidate_identity,
        baseline_identity=control.candidate_identity,
    )
    summary = summarize_single_round_strength(
        aggregate_candidate_metrics(
            artifact.plan.candidate_identity, artifact.game_results
        ),
        artifact.game_results,
    )
    with mock.patch(
        "lisjong_arena.learned_policy_offline_q.fh_curriculum_rollout"
        ".collect_execution_provenance",
        return_value=provenance(),
    ):
        return build_rollout_result(
            curriculum=curriculum,
            control=control,
            artifact=artifact,
            artifact_path=path,
            summary=summary,
            curriculum_diagnostics=(
                fixture_diagnostics()
                if curriculum_diagnostics is None
                else curriculum_diagnostics
            ),
            control_diagnostics=(
                fixture_diagnostics(
                    total_decisions=3_000,
                    total_activations=2_100,
                    total_scaffold_fallbacks=750,
                    total_support_fallbacks=150,
                )
                if control_diagnostics is None
                else control_diagnostics
            ),
            offline_diagnostics=offline_diagnostics,
        )
