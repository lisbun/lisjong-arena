"""Issue #158 P1 Gate A fixtures.

P1 derived featureは`own_hand.tile_counts`から純手牌を復元するため、既存の
合成feature（ほぼ全ゼロ）ではderivationがambiguousになる。ここでは
`own_hand`グループを実手牌にした合成dataset / replacement TEST artifactと、
`validate_gate_a_result()`が要求する形を完全に満たすresult documentを提供する。

result document fixtureはlocked constant、4 role全部、action agreement、
hand progressionのarm / pair / per-seed / outcome conditionをすべて持つ。
**この fixture を痩せさせると validation の negative test が意味を失うため、
role や metric を省略しない。**

hidden information（相手手牌・山・future outcome）は一切含めない。
"""

from _learned_policy_offline_q_artifact_fixtures import (
    FIXTURE_PROVENANCE,
)
from _learned_policy_offline_q_diagnosis_fixtures import (
    discard_index,
    feature_row_with_hand,
    hand_tiles,
)

from lisjong_arena.learned_policy_offline_q.artifact import (
    OfflineQDatasetWriter,
    feature_block,
    vocabulary_block,
)
from lisjong_arena.learned_policy_offline_q.diagnosis import (
    FIXED_QUANTILES,
    LOCKED_SOURCE_IDENTITIES,
    RETENTION_BACKEND,
    RETENTION_KEY,
)
from lisjong_arena.learned_policy_offline_q.hand_progression import (
    MeasurementAvailability,
)
from lisjong_arena.learned_policy_offline_q.model import MacroTransitionRow
from lisjong_arena.learned_policy_offline_q.p1_features import p1_feature_block
from lisjong_arena.learned_policy_offline_q.p1_gate_a import (
    AGREEMENT_PAIRS,
    ARMS,
    CHANGED_AXIS,
    GENERATION_BUDGET,
    INTERPRETATION_BOUNDARY,
    P1_GATE_A_ID,
    P1_GATE_A_LIMITATIONS,
    P1_GATE_A_SCHEMA_VERSION,
    PAIRS,
    PARENT_ISSUE,
    PREDECESSOR_ISSUES,
    PRIMARY_ROLES,
    SOURCE_ISSUE,
    P1GateARole,
    outcome_conditions,
)
from lisjong_arena.learned_policy_offline_q.p1_q_training import (
    p1_model_block,
    p1_training_block,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATASET_ORDERED_SEEDS,
    MAXIMUM_EPOCHS,
    PROTOCOL_ID,
    REPLACEMENT_TEST_SEEDS,
    VOCABULARY_SIZE,
    Split,
    action_family,
    split_for_seed,
)
from lisjong_arena.learned_policy_offline_q.replacement_test import (
    ReplacementTestWriter,
)

TENPAI_HAND = "123456789m123p1s5z"
"""4面子 + 単騎候補2枚。`1s` / `5z`は打っても向聴数が変わらない。"""

SECOND_HAND = "123456789m123p1s6z"
"""別のfeature rowを作るための手牌。`5z`が`6z`へ変わるだけで、legal discard
候補（`9m` / `1s`）は両方の手牌に必ず存在する。"""

KEEP_SHANTEN_TILE = "1s"
"""`TENPAI_HAND`から打っても向聴数が変わらない牌。"""

WORSEN_SHANTEN_TILE = "9m"
"""`TENPAI_HAND`から打つと面子が崩れ、向聴数が悪化する牌。"""

LEGAL_DISCARD_LABELS = (WORSEN_SHANTEN_TILE, KEEP_SHANTEN_TILE)
LEGAL_INDICES = tuple(discard_index(label) for label in LEGAL_DISCARD_LABELS)

ELIGIBLE_ROW_COUNT = 4
"""fixture roleごとのeligible row数。全metricの母数を揃える。"""


def legal_mask() -> tuple[bool, ...]:
    """`LEGAL_INDICES`だけが立ったlegal mask。全てordinary discardである。"""
    chosen = set(LEGAL_INDICES)
    return tuple(index in chosen for index in range(VOCABULARY_SIZE))


def hand_feature_values(seed: int, ordinal: int) -> tuple[float, ...]:
    """実手牌を持つdeterministicなfeature row。

    own_hand以外のindexにはseed / ordinal由来のfillerを入れる。P1 derivationが
    own_handグループだけを読むことを、この差で確認できる。
    """
    notation = TENPAI_HAND if (seed + ordinal) % 2 == 0 else SECOND_HAND
    values = feature_row_with_hand(hand_tiles(notation))
    values[0] = float(seed % 7) / 8.0
    values[1] = float(ordinal % 5) / 8.0
    return tuple(values)


def hand_transition_row(
    seed: int, ordinal: int, *, split: Split, rows_per_game: int
) -> MacroTransitionRow:
    """実手牌を持つ合成macro-transition row。"""
    behavior_index = LEGAL_INDICES[ordinal % len(LEGAL_INDICES)]
    terminal = ordinal == rows_per_game - 1
    kwargs = dict(
        seed=seed,
        split=split,
        round_ordinal=ordinal // 4,
        round_wind="east",
        hand_number=1 + (ordinal // 4) % 4,
        honba=0,
        actor_seat=0,
        step_ordinal=ordinal,
        decision_ordinal=ordinal,
        feature_values=hand_feature_values(seed, ordinal),
        legal_mask=legal_mask(),
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
            next_feature_values=hand_feature_values(seed, ordinal + 1),
            next_legal_mask=legal_mask(),
        )
    return MacroTransitionRow(**kwargs)


def write_hand_dataset(destination, *, rows_per_game: int = 6):
    """locked seed populationを満たす、実手牌つき合成datasetを書き出す。"""
    writer = OfflineQDatasetWriter(destination, provenance=FIXTURE_PROVENANCE)
    try:
        for seed in DATASET_ORDERED_SEEDS:
            writer.add_game(
                seed=seed,
                split=split_for_seed(seed),
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=(
                    hand_transition_row(
                        seed,
                        ordinal,
                        split=split_for_seed(seed),
                        rows_per_game=rows_per_game,
                    )
                    for ordinal in range(rows_per_game)
                ),
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise


def write_hand_replacement_test(destination, *, rows_per_game: int = 6):
    """locked replacement TEST populationを満たす、実手牌つき合成artifact。"""
    writer = ReplacementTestWriter(destination, provenance=FIXTURE_PROVENANCE)
    try:
        for seed in REPLACEMENT_TEST_SEEDS:
            writer.add_game(
                seed=seed,
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=(
                    hand_transition_row(
                        seed,
                        ordinal,
                        split=Split.TEST,
                        rows_per_game=rows_per_game,
                    )
                    for ordinal in range(rows_per_game)
                ),
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise


# --- Result document fixture ---------------------------------------------


def summary(count: int, *, value: float = 1.0) -> dict:
    """locked quantile setを持つfixed summary。"""
    if count == 0:
        return {"count": 0, "mean": None, "quantiles": None}
    return {
        "count": count,
        "mean": value,
        "quantiles": {format(item, "g"): value for item in FIXED_QUANTILES},
    }


def arm_summary(worsen: int, *, rows: int = ELIGIBLE_ROW_COUNT) -> dict:
    return {
        "row_count": rows,
        "post_discard_shanten": summary(rows),
        "keep_shanten_count": rows - worsen,
        "keep_shanten_rate": (rows - worsen) / rows,
        "worsen_shanten_count": worsen,
        "worsen_shanten_rate": worsen / rows,
    }


def pair_summary(
    left: dict, right: dict, *, lower: int, higher: int, rows: int = ELIGIBLE_ROW_COUNT
) -> dict:
    return {
        "row_count": rows,
        "lower_post_discard_shanten_count": lower,
        "equal_post_discard_shanten_count": rows - lower - higher,
        "higher_post_discard_shanten_count": higher,
        "higher_post_discard_shanten_rate": higher / rows,
        "worsen_shanten_rate_difference": (
            left["worsen_shanten_rate"] - right["worsen_shanten_rate"]
        ),
    }


def hand_progression_block(
    *, q_v2_worsen: int, q_v1_worsen: int, bc_worsen: int = 0
) -> dict:
    """指定したworsen countから、内部整合したhand progression blockを作る。"""
    rows = ELIGIBLE_ROW_COUNT
    arms = {
        "q_v2": arm_summary(q_v2_worsen),
        "q_v1": arm_summary(q_v1_worsen),
        "bc": arm_summary(bc_worsen),
        "behavior": arm_summary(bc_worsen),
    }
    improvement = q_v1_worsen - q_v2_worsen
    pairs = {
        "q_v2_vs_q_v1": pair_summary(
            arms["q_v2"],
            arms["q_v1"],
            lower=max(improvement, 0),
            higher=max(-improvement, 0),
        ),
        "q_v2_vs_bc": pair_summary(
            arms["q_v2"],
            arms["bc"],
            lower=0,
            higher=max(q_v2_worsen - bc_worsen, 0),
        ),
        "q_v2_vs_behavior": pair_summary(
            arms["q_v2"],
            arms["behavior"],
            lower=0,
            higher=max(q_v2_worsen - bc_worsen, 0),
        ),
        "q_v1_vs_bc": pair_summary(
            arms["q_v1"],
            arms["bc"],
            lower=0,
            higher=max(q_v1_worsen - bc_worsen, 0),
        ),
        "q_v1_vs_behavior": pair_summary(
            arms["q_v1"],
            arms["behavior"],
            lower=0,
            higher=max(q_v1_worsen - bc_worsen, 0),
        ),
    }
    per_seed = [
        {
            "seed": DATASET_ORDERED_SEEDS[0],
            "row_count": rows,
            "q_v2_worsen_shanten_count": q_v2_worsen,
            "q_v1_worsen_shanten_count": q_v1_worsen,
            "lower_post_discard_shanten_count": max(improvement, 0),
            "equal_post_discard_shanten_count": rows - abs(improvement),
            "higher_post_discard_shanten_count": max(-improvement, 0),
        }
    ]
    return {
        "status": MeasurementAvailability.AVAILABLE.value,
        "unavailable_reason": None,
        "arms": arms,
        "pairs": pairs,
        "per_seed": per_seed,
        "outcome_conditions": outcome_conditions(arms, pairs),
    }


def unavailable_hand_progression() -> dict:
    return {
        "status": MeasurementAvailability.UNAVAILABLE.value,
        "unavailable_reason": "synthetic rows carry no concealed hand",
        "arms": None,
        "pairs": None,
        "per_seed": None,
        "outcome_conditions": None,
    }


def action_agreement_block(*, rows: int = ELIGIBLE_ROW_COUNT) -> dict:
    document: dict = {"eligible_row_count": rows}
    for index, pair in enumerate(AGREEMENT_PAIRS):
        count = index % (rows + 1)
        document[f"{pair}_disagreement_count"] = count
        document[f"{pair}_disagreement_rate"] = count / rows
    return document


_ROLE_SOURCE_ARTIFACT = {
    P1GateARole.DATASET_TRAIN: "dataset",
    P1GateARole.DATASET_VALIDATION: "dataset",
    P1GateARole.DATASET_TEST: "dataset",
    P1GateARole.REPLACEMENT_TEST: "replacement-test",
}
_ROLE_SPLIT = {
    P1GateARole.DATASET_TRAIN: Split.TRAIN.value,
    P1GateARole.DATASET_VALIDATION: Split.VALIDATION.value,
    P1GateARole.DATASET_TEST: Split.TEST.value,
    P1GateARole.REPLACEMENT_TEST: Split.TEST.value,
}

TOTAL_ROW_COUNT = 6


def role_document(role: P1GateARole, *, hand_progression: dict | None = None) -> dict:
    """1 roleぶんのvalidなrole document。"""
    return {
        "role": role.value,
        "source_artifact": _ROLE_SOURCE_ARTIFACT[role],
        "split": _ROLE_SPLIT[role],
        "is_primary_role": role in PRIMARY_ROLES,
        "is_generalization_evidence": False,
        "row_counts": {
            "total_row_count": TOTAL_ROW_COUNT,
            "choice_row_count": 5,
            "ordinary_discard_row_count": 5,
            "support_complete_row_count": ELIGIBLE_ROW_COUNT,
            "eligible_row_count": ELIGIBLE_ROW_COUNT,
            "excluded_row_count": TOTAL_ROW_COUNT - ELIGIBLE_ROW_COUNT,
        },
        "derived_coverage": {
            "row_count": TOTAL_ROW_COUNT,
            "source_rows_derived": TOTAL_ROW_COUNT,
            "nonterminal_next_rows_derived": TOTAL_ROW_COUNT - 1,
            "terminal_next_rows_zero_padded": 1,
            "imputed_row_count": 0,
        },
        "action_agreement": action_agreement_block(),
        "hand_progression": (
            unavailable_hand_progression()
            if hand_progression is None
            else hand_progression
        ),
    }


def candidate_block() -> dict:
    return {
        "model": p1_model_block(),
        "training": p1_training_block(),
        "weights_digest": "a" * 64,
        "selected_epoch": MAXIMUM_EPOCHS,
        "final_validation_huber_loss": 0.125,
        "epoch_history": [
            {
                "epoch": epoch,
                "train_huber_loss": 0.5,
                "validation_huber_loss": 0.25,
            }
            for epoch in range(1, MAXIMUM_EPOCHS + 1)
        ],
        "supported_indices_digest": (LOCKED_SOURCE_IDENTITIES.supported_indices_digest),
    }


SIGNAL_PROGRESSION = {"q_v2_worsen": 1, "q_v1_worsen": 3, "bc_worsen": 0}
"""両primary roleでsignal条件3つをすべて満たすworsen count。"""

REGRESSION_PROGRESSION = {"q_v2_worsen": 3, "q_v1_worsen": 1, "bc_worsen": 0}
"""両primary roleでregression条件3つをすべて満たすworsen count。"""

NEUTRAL_PROGRESSION = {"q_v2_worsen": 2, "q_v1_worsen": 2, "bc_worsen": 0}
"""signalでもregressionでもないworsen count。"""


def valid_result_document(
    *,
    real_artifact_execution: bool = True,
    primary_progression: dict | None = None,
    dataset_test_progression: dict | None = None,
    replacement_test_progression: dict | None = None,
    classification: str | None = None,
) -> dict:
    """`validate_gate_a_result()`を通る完全なresult document。

    `real_artifact_execution`をFalseにする場合、`input_artifact_identities`も
    locked値から外れていなければならない（両者はvalidatorが突き合わせる）。
    """
    identities = LOCKED_SOURCE_IDENTITIES.to_document()
    if not real_artifact_execution:
        identities = {name: "0" * 64 for name in identities}
    shared = SIGNAL_PROGRESSION if primary_progression is None else primary_progression
    if dataset_test_progression is None:
        dataset_test_progression = shared
    if replacement_test_progression is None:
        replacement_test_progression = shared
    roles = [
        role_document(P1GateARole.DATASET_TRAIN),
        role_document(P1GateARole.DATASET_VALIDATION),
        role_document(
            P1GateARole.DATASET_TEST,
            hand_progression=hand_progression_block(**dataset_test_progression),
        ),
        role_document(
            P1GateARole.REPLACEMENT_TEST,
            hand_progression=hand_progression_block(**replacement_test_progression),
        ),
    ]
    return {
        "p1_gate_a_schema_version": P1_GATE_A_SCHEMA_VERSION,
        "p1_gate_a_id": P1_GATE_A_ID,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issues": list(PREDECESSOR_ISSUES),
        "parent_issue": PARENT_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "retention": {"backend": RETENTION_BACKEND, "key": RETENTION_KEY},
        "input_artifact_identities": {
            **identities,
            "real_artifact_execution": real_artifact_execution,
        },
        "locked_source_identities": LOCKED_SOURCE_IDENTITIES.to_document(),
        "feature": feature_block(),
        "derived_feature": p1_feature_block(),
        "vocabulary": vocabulary_block(),
        "candidate": candidate_block(),
        "changed_axis": list(CHANGED_AXIS),
        "generation_budget": dict(GENERATION_BUDGET),
        "fixed_quantiles": [format(value, "g") for value in FIXED_QUANTILES],
        "primary_roles": [role.value for role in PRIMARY_ROLES],
        "roles": roles,
        "limitations": list(P1_GATE_A_LIMITATIONS),
        "interpretation_boundary": {
            "positive_claim_limit": INTERPRETATION_BOUNDARY["positive_claim_limit"],
            "forbidden_claims": list(INTERPRETATION_BOUNDARY["forbidden_claims"]),
            "negative_claim_limit": INTERPRETATION_BOUNDARY["negative_claim_limit"],
        },
        "classification": classification,
    }


__all__ = [
    "ARMS",
    "ELIGIBLE_ROW_COUNT",
    "KEEP_SHANTEN_TILE",
    "LEGAL_DISCARD_LABELS",
    "LEGAL_INDICES",
    "NEUTRAL_PROGRESSION",
    "PAIRS",
    "REGRESSION_PROGRESSION",
    "SECOND_HAND",
    "SIGNAL_PROGRESSION",
    "TENPAI_HAND",
    "TOTAL_ROW_COUNT",
    "WORSEN_SHANTEN_TILE",
    "action_agreement_block",
    "arm_summary",
    "candidate_block",
    "hand_feature_values",
    "hand_progression_block",
    "hand_transition_row",
    "legal_mask",
    "pair_summary",
    "role_document",
    "summary",
    "unavailable_hand_progression",
    "valid_result_document",
    "write_hand_dataset",
    "write_hand_replacement_test",
]
