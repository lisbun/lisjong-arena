"""Issue #152 diagnosis fixtures.

Measurement Dは`own_hand.tile_counts`から純手牌を復元するため、既存の合成
feature（ほぼ全ゼロ）ではreconstructionがambiguousになる。ここでは
`own_hand`グループだけを実際の手牌へ差し替えたfeature rowと、そこから
組み立てたvalidなdiagnosis result documentを提供する。

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
)
from lisjong_arena.learned_policy_offline_q.hand_progression import (
    OWN_HAND_TILE_COUNT_START,
    MeasurementAvailability,
)
from lisjong_arena.learned_policy_offline_q.protocol import (
    FEATURE_DIMENSION,
    PROTOCOL_ID,
)


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


def _measurement_a(row_count: int) -> dict:
    return {
        "eligible_row_count": row_count,
        "q_vs_bc_disagreement_count": 1,
        "q_vs_bc_disagreement_rate": 1 / row_count,
        "q_vs_behavior_disagreement_count": 1,
        "q_vs_behavior_disagreement_rate": 1 / row_count,
        "bc_vs_behavior_disagreement_count": 0,
        "bc_vs_behavior_disagreement_rate": 0.0,
        "stratifications": {
            "legal_action_count": [
                {
                    "stratum": "2",
                    "row_count": row_count,
                    "q_vs_bc_disagreement_count": 1,
                    "q_vs_bc_disagreement_rate": 1 / row_count,
                    "q_vs_behavior_disagreement_count": 1,
                    "q_vs_behavior_disagreement_rate": 1 / row_count,
                    "bc_vs_behavior_disagreement_count": 0,
                    "bc_vs_behavior_disagreement_rate": 0.0,
                }
            ]
        },
    }


def valid_result_document(*, real_artifact_execution: bool = True) -> dict:
    """`validate_diagnosis_result()`を通る最小のresult document。"""
    row_count = 4
    return {
        "diagnosis_schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "diagnosis_id": DIAGNOSIS_ID,
        "source_issue": SOURCE_ISSUE,
        "predecessor_issue": PREDECESSOR_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "retention": {"backend": RETENTION_BACKEND, "key": RETENTION_KEY},
        "input_artifact_identities": {
            **LOCKED_SOURCE_IDENTITIES.to_document(),
            "real_artifact_execution": real_artifact_execution,
        },
        "locked_source_identities": LOCKED_SOURCE_IDENTITIES.to_document(),
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "fixed_quantiles": [format(value, "g") for value in FIXED_QUANTILES],
        "roles": [
            {
                "role": "replacement-test",
                "source_artifact": "replacement-test",
                "split": "test",
                "is_generalization_evidence": False,
                "row_counts": {
                    "total_row_count": 6,
                    "choice_row_count": 5,
                    "ordinary_discard_row_count": 5,
                    "support_complete_row_count": 4,
                    "eligible_row_count": row_count,
                    "excluded_row_count": 2,
                },
                "measurement_a": _measurement_a(row_count),
                "measurement_b": {},
                "measurement_c": {
                    "eligible_row_count": row_count,
                    "terminal_row_count": 1,
                    "nonterminal_row_count": 3,
                    "bootstrap_eligible_row_count": row_count,
                    "unsupported_bootstrap_row_count": 0,
                },
                "measurement_d": {
                    "status": MeasurementAvailability.UNAVAILABLE.value,
                    "unavailable_reason": "synthetic rows carry no concealed hand",
                    "post_discard_shanten": None,
                    "ukeire": {
                        "status": MeasurementAvailability.UNAVAILABLE.value,
                        "unavailable_reason": "no reusable first-party contract",
                    },
                },
            }
        ],
        "retained_strength_context": RETAINED_STRENGTH_CONTEXT,
        "limitations": list(DIAGNOSIS_LIMITATIONS),
        "classification": None,
    }
