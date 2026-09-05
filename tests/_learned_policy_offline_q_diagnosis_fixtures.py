"""Issue #152 diagnosis fixtures.

Measurement Dは`own_hand.tile_counts`から純手牌を復元するため、既存の合成
feature（ほぼ全ゼロ）ではreconstructionがambiguousになる。ここでは
`own_hand`グループだけを実際の手牌へ差し替えたfeature rowと、そこから
組み立てたvalidなdiagnosis result documentを提供する。

result document fixtureは`validate_diagnosis_result()`が要求する形をすべて
満たす。すなわちlocked constant、4 role全部、Measurement A-Dの全field、
countsから再導出できるrateである。**この fixture を痩せさせると validation の
negative test が意味を失うため、role や measurement を省略しない。**

hidden information（相手手牌・山）は一切含めない。
"""

from lisjong.action_vocabulary import encode_action
from lisjong.policy_contract import Seat, Tile, TileCategory, TileType
from lisjong.policy_contract.action import DiscardAction

from lisjong_arena.learned_policy_input.feature import TILE_INDEX
from lisjong_arena.learned_policy_input.tensor import TILE_COUNT_SCALE
from lisjong_arena.learned_policy_offline_q.artifact import (
    feature_block,
    vocabulary_block,
)
from lisjong_arena.learned_policy_offline_q.diagnosis import (
    DIAGNOSIS_ID,
    DIAGNOSIS_LIMITATIONS,
    DIAGNOSIS_SCHEMA_VERSION,
    FIXED_QUANTILES,
    LOCKED_SOURCE_IDENTITIES,
    PREDECESSOR_ISSUE,
    RETAINED_STRENGTH_CONTEXT,
    RETENTION_BACKEND,
    RETENTION_KEY,
    SOURCE_ISSUE,
    TD_TARGET_MODEL,
    DiagnosisRole,
)
from lisjong_arena.learned_policy_offline_q.hand_progression import (
    OWN_HAND_TILE_COUNT_START,
    MeasurementAvailability,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    FEATURE_DIMENSION,
    GAMMA,
    PROTOCOL_ID,
    Split,
)

ELIGIBLE_ROW_COUNT = 4
"""fixture roleごとのeligible row数。全measurementの母数を揃える。"""

TERMINAL_ROW_COUNT = 1
AGREE_ROW_COUNT = 3
DISAGREE_ROW_COUNT = ELIGIBLE_ROW_COUNT - AGREE_ROW_COUNT


def tile(label: str) -> Tile:
    """`"3m"` / `"7z"`形式のlabelを`Tile`へ変換する（赤牌は扱わない）。"""
    category = {
        "m": TileCategory.MANZU,
        "p": TileCategory.PINZU,
        "s": TileCategory.SOUZU,
        "z": TileCategory.HONOR,
    }[label[-1]]
    return Tile(TileType(category, int(label[:-1])))


def hand_tiles(notation: str) -> tuple[Tile, ...]:
    """`"123m45p"`形式のnotationを`Tile`列へ変換する。"""
    tiles: list[Tile] = []
    digits = ""
    for character in notation:
        if character.isdigit():
            digits += character
        else:
            tiles.extend(tile(f"{digit}{character}") for digit in digits)
            digits = ""
    if digits:
        raise ValueError("hand notation must end with a category suffix")
    return tuple(tiles)


def discard_index(label: str, *, tsumogiri: bool = False) -> int:
    """打牌のvocabulary indexを、locked codecから導出する。"""
    return encode_action(
        DiscardAction(actor=Seat(0), tile=tile(label), tsumogiri=tsumogiri)
    )


def feature_row_with_hand(concealed_tiles, *, filler: float = 0.0) -> list[float]:
    """`own_hand.tile_counts`だけを実手牌にしたfeature rowを作る。

    `filler`はown_hand以外のindexへ入れる値であり、Measurement Dが
    own_handグループだけを読むことを確かめるために使う。
    """
    values = [filler] * FEATURE_DIMENSION
    counts = [0] * len(TILE_INDEX)
    for item in concealed_tiles:
        counts[TILE_INDEX[item]] += 1
    for offset, count in enumerate(counts):
        values[OWN_HAND_TILE_COUNT_START + offset] = count / TILE_COUNT_SCALE
    return values


def summary(count: int, *, value: float = 1.0) -> dict:
    """locked quantile setを持つfixed summary（またはNone母数のsummary）。"""
    if count == 0:
        return {"count": 0, "mean": None, "quantiles": None}
    return {
        "count": count,
        "mean": value,
        "quantiles": {format(item, "g"): value for item in FIXED_QUANTILES},
    }


def _stratum(label: str, row_count: int, disagreements: int) -> dict:
    return {
        "stratum": label,
        "row_count": row_count,
        "q_vs_bc_disagreement_count": disagreements,
        "q_vs_bc_disagreement_rate": disagreements / row_count,
        "q_vs_behavior_disagreement_count": disagreements,
        "q_vs_behavior_disagreement_rate": disagreements / row_count,
        "bc_vs_behavior_disagreement_count": 0,
        "bc_vs_behavior_disagreement_rate": 0.0,
    }


def _measurement_a() -> dict:
    rows = ELIGIBLE_ROW_COUNT
    disagree = DISAGREE_ROW_COUNT
    return {
        "eligible_row_count": rows,
        "q_vs_bc_disagreement_count": disagree,
        "q_vs_bc_disagreement_rate": disagree / rows,
        "q_vs_behavior_disagreement_count": disagree,
        "q_vs_behavior_disagreement_rate": disagree / rows,
        "bc_vs_behavior_disagreement_count": 0,
        "bc_vs_behavior_disagreement_rate": 0.0,
        "stratifications": {
            "legal_action_count": [_stratum("2", rows, disagree)],
            "transition_terminality": [
                _stratum("nonterminal", rows - TERMINAL_ROW_COUNT, disagree),
                _stratum("terminal", TERMINAL_ROW_COUNT, 0),
            ],
            "round_ordinal": [_stratum("0", rows, disagree)],
            "decision_depth": [_stratum("0-3", rows, disagree)],
        },
    }


def _measurement_b() -> dict:
    metrics = (
        "q_top1_value",
        "q_top2_value",
        "q_margin",
        "q_value_of_bc_action",
        "q_value_of_behavior_action",
        "q_selected_vs_bc_selected_gap",
        "q_selected_vs_behavior_gap",
    )
    return {
        "all_eligible_rows": {name: summary(ELIGIBLE_ROW_COUNT) for name in metrics},
        "q_bc_agree_rows": {name: summary(AGREE_ROW_COUNT) for name in metrics},
        "q_bc_disagree_rows": {name: summary(DISAGREE_ROW_COUNT) for name in metrics},
    }


def _bootstrap_block() -> dict:
    return {
        "all_bootstrap_eligible_rows": summary(ELIGIBLE_ROW_COUNT),
        "q_bc_agree_rows": summary(AGREE_ROW_COUNT),
        "q_bc_disagree_rows": summary(DISAGREE_ROW_COUNT),
        "terminal_rows": summary(TERMINAL_ROW_COUNT),
        "nonterminal_rows": summary(ELIGIBLE_ROW_COUNT - TERMINAL_ROW_COUNT),
    }


def _measurement_c() -> dict:
    return {
        "eligible_row_count": ELIGIBLE_ROW_COUNT,
        "terminal_row_count": TERMINAL_ROW_COUNT,
        "nonterminal_row_count": ELIGIBLE_ROW_COUNT - TERMINAL_ROW_COUNT,
        "bootstrap_eligible_row_count": ELIGIBLE_ROW_COUNT,
        "unsupported_bootstrap_row_count": 0,
        "gamma": GAMMA,
        "td_target_model": TD_TARGET_MODEL,
        "immediate_reward": {
            "all_eligible_rows": summary(ELIGIBLE_ROW_COUNT),
            "terminal_rows": summary(TERMINAL_ROW_COUNT),
            "nonterminal_rows": summary(ELIGIBLE_ROW_COUNT - TERMINAL_ROW_COUNT),
            "q_bc_agree_rows": summary(AGREE_ROW_COUNT),
            "q_bc_disagree_rows": summary(DISAGREE_ROW_COUNT),
        },
        "td_target": _bootstrap_block(),
        "predicted_selected_q": _bootstrap_block(),
        "absolute_bellman_residual": _bootstrap_block(),
    }


def _unavailable_measurement_d() -> dict:
    return {
        "status": MeasurementAvailability.UNAVAILABLE.value,
        "unavailable_reason": "synthetic rows carry no concealed hand",
        "post_discard_shanten": None,
        "ukeire": {
            "status": MeasurementAvailability.UNAVAILABLE.value,
            "unavailable_reason": "no reusable first-party ukeire contract",
        },
    }


def _hand_progression_arm(worsen: int) -> dict:
    rows = ELIGIBLE_ROW_COUNT
    return {
        "row_count": rows,
        "post_discard_shanten": summary(rows),
        "keep_shanten_count": rows - worsen,
        "keep_shanten_rate": (rows - worsen) / rows,
        "worsen_shanten_count": worsen,
        "worsen_shanten_rate": worsen / rows,
    }


def available_measurement_d(*, q_worsen: int = 4, other_worsen: int = 0) -> dict:
    """Measurement Dが`AVAILABLE`なroleのsummary block。"""
    rows = ELIGIBLE_ROW_COUNT
    arms = {
        "q": _hand_progression_arm(q_worsen),
        "bc": _hand_progression_arm(other_worsen),
        "behavior": _hand_progression_arm(other_worsen),
    }
    higher = q_worsen - other_worsen
    pair = {
        "row_count": rows,
        "lower_post_discard_shanten_count": 0,
        "equal_post_discard_shanten_count": rows - higher,
        "higher_post_discard_shanten_count": higher,
        "higher_post_discard_shanten_rate": higher / rows,
        "worsen_shanten_rate_difference": (
            arms["q"]["worsen_shanten_rate"] - arms["bc"]["worsen_shanten_rate"]
        ),
    }
    return {
        "status": MeasurementAvailability.AVAILABLE.value,
        "unavailable_reason": None,
        "post_discard_shanten": {
            **arms,
            "q_vs_bc": dict(pair),
            "q_vs_behavior": dict(pair),
        },
        "ukeire": {
            "status": MeasurementAvailability.UNAVAILABLE.value,
            "unavailable_reason": "no reusable first-party ukeire contract",
        },
    }


_ROLE_SOURCE_ARTIFACT = {
    DiagnosisRole.DATASET_TRAIN: "dataset",
    DiagnosisRole.DATASET_VALIDATION: "dataset",
    DiagnosisRole.DATASET_TEST: "dataset",
    DiagnosisRole.REPLACEMENT_TEST: "replacement-test",
}
_ROLE_SPLIT = {
    DiagnosisRole.DATASET_TRAIN: Split.TRAIN.value,
    DiagnosisRole.DATASET_VALIDATION: Split.VALIDATION.value,
    DiagnosisRole.DATASET_TEST: Split.TEST.value,
    DiagnosisRole.REPLACEMENT_TEST: Split.TEST.value,
}


def role_document(role: DiagnosisRole, *, measurement_d: dict | None = None) -> dict:
    """1 roleぶんのvalidなrole document。"""
    return {
        "role": role.value,
        "source_artifact": _ROLE_SOURCE_ARTIFACT[role],
        "split": _ROLE_SPLIT[role],
        "is_generalization_evidence": False,
        "row_counts": {
            "total_row_count": 6,
            "choice_row_count": 5,
            "ordinary_discard_row_count": 5,
            "support_complete_row_count": ELIGIBLE_ROW_COUNT,
            "eligible_row_count": ELIGIBLE_ROW_COUNT,
            "excluded_row_count": 6 - ELIGIBLE_ROW_COUNT,
        },
        "measurement_a": _measurement_a(),
        "measurement_b": _measurement_b(),
        "measurement_c": _measurement_c(),
        "measurement_d": (
            _unavailable_measurement_d() if measurement_d is None else measurement_d
        ),
    }


def valid_result_document(
    *,
    real_artifact_execution: bool = True,
    hand_progression_available: bool = False,
) -> dict:
    """`validate_diagnosis_result()`を通る完全なresult document。

    4 role全部、Measurement A-Dの全field、countsから再導出できるrateを持つ。
    `real_artifact_execution`をFalseにする場合、`input_artifact_identities`も
    locked値から外れていなければならない（両者はvalidatorが突き合わせる）。
    """
    identities = LOCKED_SOURCE_IDENTITIES.to_document()
    if not real_artifact_execution:
        identities = {name: "0" * 64 for name in identities}
    return {
        "diagnosis_schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "diagnosis_id": DIAGNOSIS_ID,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issue": PREDECESSOR_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "retention": {"backend": RETENTION_BACKEND, "key": RETENTION_KEY},
        "input_artifact_identities": {
            **identities,
            "real_artifact_execution": real_artifact_execution,
        },
        "locked_source_identities": LOCKED_SOURCE_IDENTITIES.to_document(),
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "fixed_quantiles": [format(value, "g") for value in FIXED_QUANTILES],
        "roles": [
            role_document(
                role,
                measurement_d=(
                    available_measurement_d() if hand_progression_available else None
                ),
            )
            for role in DiagnosisRole
        ],
        "retained_strength_context": RETAINED_STRENGTH_CONTEXT,
        "limitations": list(DIAGNOSIS_LIMITATIONS),
        "classification": None,
    }
