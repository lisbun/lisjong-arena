"""Fixed semantic tensorization for Phase 6 snapshot features."""

from lisjong_engine.public_state import PublicMeldType, PublicRiichiStatus
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import ResponseOutcome, ResponseTrigger
from lisjong_engine.wind import Wind

from .feature import Phase6SnapshotFeature

FEATURE_DIM = 919

HONBA_SCALE = 10.0
RIICHI_STICK_SCALE = 4.0
LIVE_WALL_SCALE = 70.0
DISCARD_ORDER_SCALE = 80.0
EVIDENCE_POSITION_SCALE = 512.0
SCORE_SCALE = 100_000.0
TILE_COUNT_SCALE = 4.0
CONCEALED_SLOT_SCALE = 13.0
DRAW_COUNT_SCALE = 70.0
RESPONSE_COUNT_SCALE = 80.0

_WINDS = tuple(Wind)
_RIICHI_STATUSES = tuple(PublicRiichiStatus)
_DRAW_SOURCES = tuple(DrawSource)
_MELD_TYPES = tuple(PublicMeldType)
_TRIGGERS = tuple(ResponseTrigger)
_OUTCOMES = tuple(ResponseOutcome)


def _one_hot(value, axis: tuple) -> list[float]:
    if value not in axis:
        raise ValueError(f"{value!r} is not on the fixed categorical axis")
    return [float(item is value) for item in axis]


def _scaled(values, scale: float) -> list[float]:
    result = []
    for value in values:
        if not isinstance(value, (int, float)):
            raise TypeError("numeric feature values must be int or float")
        result.append(value / scale)
    return result


def tensor_values(feature: Phase6SnapshotFeature) -> tuple[float, ...]:
    """Flatten one raw value using only explicit, fixed semantic scaling."""
    if not isinstance(feature, Phase6SnapshotFeature):
        raise TypeError("feature must be a Phase6SnapshotFeature")
    values: list[float] = []
    values.extend(_one_hot(feature.viewer_wind, _WINDS))
    values.extend(_one_hot(feature.prevailing_wind, _WINDS))
    values.extend(_one_hot(feature.dealer_relation, tuple(range(4))))
    values.extend(_one_hot(feature.hand_number, (1, 2, 3, 4)))
    values.extend(
        (
            feature.honba / HONBA_SCALE,
            feature.riichi_sticks / RIICHI_STICK_SCALE,
            feature.remaining_live_wall_count / LIVE_WALL_SCALE,
            feature.global_public_discard_count / DISCARD_ORDER_SCALE,
            feature.evidence_prefix_length / EVIDENCE_POSITION_SCALE,
        )
    )
    values.extend(_scaled(feature.scores_by_wind, SCORE_SCALE))
    values.extend(_scaled(feature.own_base_tile_counts, TILE_COUNT_SCALE))
    values.extend(_scaled(feature.remaining_tile_counts, TILE_COUNT_SCALE))
    values.extend(_scaled(feature.visible_dora_indicator_counts, TILE_COUNT_SCALE))
    for opponent in feature.opponents:
        values.extend(_one_hot(opponent.wind, _WINDS))
        values.append(opponent.concealed_slot_count / CONCEALED_SLOT_SCALE)
        values.extend(_one_hot(opponent.riichi_status, _RIICHI_STATUSES))
        values.extend(_scaled(opponent.public_meld_tile_counts, TILE_COUNT_SCALE))
        values.extend(_scaled(opponent.meld_kind_counts, TILE_COUNT_SCALE))
        values.extend(_scaled(opponent.discard_counts, TILE_COUNT_SCALE))
        values.extend(_scaled(opponent.tedashi_counts, TILE_COUNT_SCALE))
        values.extend(_scaled(opponent.tsumogiri_counts, TILE_COUNT_SCALE))
        values.extend(_scaled(opponent.last_discard_orders, DISCARD_ORDER_SCALE))
        values.extend(float(value) for value in opponent.last_discard_present)
        values.extend(_scaled(opponent.public_draw_source_counts, DRAW_COUNT_SCALE))
        values.extend(
            (
                float(opponent.riichi_declaration_present),
                opponent.riichi_declaration_discard_order / DISCARD_ORDER_SCALE,
                opponent.last_call_evidence_position / EVIDENCE_POSITION_SCALE,
                float(opponent.last_call_present),
                opponent.last_kan_evidence_position / EVIDENCE_POSITION_SCALE,
                float(opponent.last_kan_present),
            )
        )
        values.extend(
            _scaled(
                opponent.discard_no_public_response_counts,
                TILE_COUNT_SCALE,
            )
        )
        values.extend(
            (
                opponent.kakan_no_public_response_count / TILE_COUNT_SCALE,
                opponent.ankan_no_public_response_count / TILE_COUNT_SCALE,
            )
        )
    values.extend(_scaled(feature.response_history_counts, RESPONSE_COUNT_SCALE))
    if len(values) != FEATURE_DIM:
        raise RuntimeError(
            f"feature tensor dimension drifted: {len(values)} != {FEATURE_DIM}"
        )
    return tuple(values)


def to_tensor(feature: Phase6SnapshotFeature):
    """Lazily import torch so pure Arena runtime imports remain torch-free."""
    import torch

    return torch.tensor(tensor_values(feature), dtype=torch.float32)


__all__ = ["FEATURE_DIM", "tensor_values", "to_tensor"]
