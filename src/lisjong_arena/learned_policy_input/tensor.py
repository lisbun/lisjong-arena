"""Versioned flat tensor layout for the Learned Policy v1 semantic feature."""

import hashlib
import math
from dataclasses import dataclass

from .errors import (
    FeatureDimensionError,
    PolicyInputFeatureValidationError,
    UnsupportedTensorSchemaVersionError,
)
from .feature import (
    FEATURE_SEMANTICS_ID,
    MAX_DORA_INDICATORS,
    MAX_GLOBAL_DISCARDS,
    MAX_LIVE_WALL_TILES,
    MAX_MELDS_PER_PLAYER,
    MELD_KIND_AXIS,
    RELATIVE_SEAT_AXIS,
    RIICHI_AXIS,
    TILE_AXIS,
    TILE_AXIS_SIZE,
    WIND_AXIS,
    MeldFeature,
    OrderedDiscardFeature,
    PolicyInputFeature,
)

TENSOR_SCHEMA_VERSION = "arena-policy-input-tensor-v1"
TENSOR_DTYPE = "float32"
FEATURE_DIM = 8204
PADDING_SEMANTICS = (
    "optional_padding=presence-0,payload-all-zero",
    "meld_padding=all-zero",
    "discard_padding=all-zero",
)

HONBA_SCALE = 10.0
RIICHI_STICK_SCALE = 10.0
LIVE_WALL_SCALE = float(MAX_LIVE_WALL_TILES)
SCORE_SCALE = 100_000.0
TILE_COUNT_SCALE = 4.0

_FLOAT32_MAX = float.fromhex("0x1.fffffep+127")
_PADDING_VALUE = 0.0
_TILE_SLOT_DIM = 1 + TILE_AXIS_SIZE
_MELD_SLOT_DIM = 1 + len(MELD_KIND_AXIS) + TILE_AXIS_SIZE + 1 + 4 + 1 + TILE_AXIS_SIZE
_PLAYER_DIM = 1 + len(RIICHI_AXIS) + MAX_MELDS_PER_PLAYER * _MELD_SLOT_DIM
_DISCARD_SLOT_DIM = 1 + 4 + TILE_AXIS_SIZE + 1 + 1 + 4
_OWN_HAND_DIM = TILE_AXIS_SIZE + 1 + TILE_AXIS_SIZE


@dataclass(frozen=True, slots=True)
class FeatureGroup:
    """One contiguous top-level range in the flat v1 layout."""

    name: str
    start: int
    stop: int
    logical_shape: tuple[int, ...]

    @property
    def length(self) -> int:
        return self.stop - self.start


FEATURE_GROUPS = (
    FeatureGroup("round_wind", 0, 4, (4,)),
    FeatureGroup("hand_number", 4, 8, (4,)),
    FeatureGroup("dealer_relative_seat", 8, 12, (4,)),
    FeatureGroup("self_wind", 12, 16, (4,)),
    FeatureGroup("honba", 16, 17, (1,)),
    FeatureGroup("riichi_sticks", 17, 18, (1,)),
    FeatureGroup("live_wall_tiles_remaining", 18, 19, (1,)),
    FeatureGroup(
        "dora_indicators",
        19,
        209,
        (MAX_DORA_INDICATORS, _TILE_SLOT_DIM),
    ),
    FeatureGroup("players", 209, 1601, (4, _PLAYER_DIM)),
    FeatureGroup(
        "discards",
        1601,
        8129,
        (MAX_GLOBAL_DISCARDS, _DISCARD_SLOT_DIM),
    ),
    FeatureGroup("own_hand", 8129, 8204, (_OWN_HAND_DIM,)),
)

if (
    FEATURE_GROUPS[0].start != 0
    or FEATURE_GROUPS[-1].stop != FEATURE_DIM
    or any(
        left.stop != right.start
        for left, right in zip(FEATURE_GROUPS, FEATURE_GROUPS[1:])
    )
):
    raise RuntimeError("v1 feature group ranges contain a hole, overlap, or drift")


def _tile_label(tile) -> str:
    category_suffix = {
        "manzu": "m",
        "pinzu": "p",
        "souzu": "s",
        "honor": "z",
    }[tile.tile_type.category.value]
    suffix = "-red" if tile.is_red else ""
    return f"{tile.tile_type.rank}{category_suffix}{suffix}"


TILE_AXIS_LABELS = tuple(_tile_label(tile) for tile in TILE_AXIS)
RELATIVE_SEAT_LABELS = tuple(value.name.lower() for value in RELATIVE_SEAT_AXIS)


def _scale_label(value: float) -> str:
    return format(value, "g")


def _tile_one_hot_descriptors(prefix: str) -> list[str]:
    return [f"{prefix}.tile[{label}]:one_hot" for label in TILE_AXIS_LABELS]


def _meld_descriptors(player_label: str, slot: int) -> list[str]:
    prefix = f"players[{player_label}].melds[{slot}]"
    values = [f"{prefix}.present:binary"]
    values.extend(f"{prefix}.kind[{kind.value}]:one_hot" for kind in MELD_KIND_AXIS)
    values.extend(
        f"{prefix}.tile_counts[{label}]:count/{_scale_label(TILE_COUNT_SCALE)}"
        for label in TILE_AXIS_LABELS
    )
    values.append(f"{prefix}.from_seat_present:binary")
    values.extend(
        f"{prefix}.from_seat[{label}]:one_hot" for label in RELATIVE_SEAT_LABELS
    )
    values.append(f"{prefix}.called_tile_present:binary")
    values.extend(_tile_one_hot_descriptors(f"{prefix}.called_tile"))
    return values


def _descriptor_groups() -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[str, ...]] = []
    groups.append(
        tuple(f"round.round_wind[{wind.value}]:one_hot" for wind in WIND_AXIS)
    )
    groups.append(tuple(f"round.hand_number[{value}]:one_hot" for value in range(1, 5)))
    groups.append(
        tuple(
            f"round.dealer_relative_seat[{label}]:one_hot"
            for label in RELATIVE_SEAT_LABELS
        )
    )
    groups.append(
        tuple(f"derived.self_wind[{wind.value}]:one_hot" for wind in WIND_AXIS)
    )
    groups.append((f"round.honba:scalar/{_scale_label(HONBA_SCALE)},no_clip",))
    groups.append(
        (f"round.riichi_sticks:scalar/{_scale_label(RIICHI_STICK_SCALE)},no_clip",)
    )
    groups.append(
        (
            "round.live_wall_tiles_remaining:scalar/"
            f"{_scale_label(LIVE_WALL_SCALE)},domain=0..{MAX_LIVE_WALL_TILES}",
        )
    )

    dora: list[str] = []
    for slot in range(MAX_DORA_INDICATORS):
        prefix = f"round.dora_indicators[{slot}]"
        dora.append(f"{prefix}.present:binary")
        dora.extend(_tile_one_hot_descriptors(prefix))
    groups.append(tuple(dora))

    players: list[str] = []
    for label in RELATIVE_SEAT_LABELS:
        prefix = f"players[{label}]"
        players.append(f"{prefix}.score:scalar/{_scale_label(SCORE_SCALE)},no_clip")
        players.extend(
            f"{prefix}.riichi[{state.value}]:one_hot" for state in RIICHI_AXIS
        )
        for slot in range(MAX_MELDS_PER_PLAYER):
            players.extend(_meld_descriptors(label, slot))
    groups.append(tuple(players))

    discards: list[str] = []
    for slot in range(MAX_GLOBAL_DISCARDS):
        prefix = f"discards_by_global_order[{slot}]"
        discards.append(f"{prefix}.present:binary")
        discards.extend(
            f"{prefix}.discarder[{label}]:one_hot" for label in RELATIVE_SEAT_LABELS
        )
        discards.extend(_tile_one_hot_descriptors(prefix))
        discards.append(f"{prefix}.tsumogiri:binary")
        discards.append(f"{prefix}.called_by_present:binary")
        discards.extend(
            f"{prefix}.called_by[{label}]:one_hot" for label in RELATIVE_SEAT_LABELS
        )
    groups.append(tuple(discards))

    own_hand = [
        *(
            f"own_hand.tile_counts[{label}]:count/{_scale_label(TILE_COUNT_SCALE)}"
            for label in TILE_AXIS_LABELS
        ),
        "own_hand.drawn_tile_present:binary",
        *(f"own_hand.drawn_tile.tile[{label}]:one_hot" for label in TILE_AXIS_LABELS),
    ]
    groups.append(tuple(own_hand))
    return tuple(groups)


_DESCRIPTOR_GROUPS = _descriptor_groups()
if len(_DESCRIPTOR_GROUPS) != len(FEATURE_GROUPS):
    raise RuntimeError("descriptor groups and feature groups differ")
for metadata, descriptors in zip(FEATURE_GROUPS, _DESCRIPTOR_GROUPS, strict=True):
    if len(descriptors) != metadata.length:
        raise RuntimeError(
            f"descriptor length drifted for {metadata.name}: "
            f"{len(descriptors)} != {metadata.length}"
        )
FEATURE_INDEX_DESCRIPTORS = tuple(
    descriptor for group in _DESCRIPTOR_GROUPS for descriptor in group
)
if len(FEATURE_INDEX_DESCRIPTORS) != FEATURE_DIM:
    raise RuntimeError("v1 feature descriptors drifted from FEATURE_DIM")
if len(set(FEATURE_INDEX_DESCRIPTORS)) != FEATURE_DIM:
    raise RuntimeError("v1 feature descriptors must be unique")


def _require_version(version: str) -> None:
    if version != TENSOR_SCHEMA_VERSION:
        raise UnsupportedTensorSchemaVersionError(
            f"unsupported tensor schema version: {version!r}"
        )


def schema_fingerprint(*, version: str = TENSOR_SCHEMA_VERSION) -> str:
    """Hash every index descriptor and the schema-level compatibility metadata."""
    _require_version(version)
    lines = (
        f"feature_semantics_id={FEATURE_SEMANTICS_ID}",
        f"tensor_schema_version={TENSOR_SCHEMA_VERSION}",
        f"dtype={TENSOR_DTYPE}",
        f"feature_dim={FEATURE_DIM}",
        *PADDING_SEMANTICS,
        *(f"{index}:{value}" for index, value in enumerate(FEATURE_INDEX_DESCRIPTORS)),
    )
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def _one_hot(value, axis: tuple) -> list[float]:
    if value not in axis:
        raise PolicyInputFeatureValidationError(
            f"{value!r} is not on the fixed categorical axis"
        )
    return [float(item == value) for item in axis]


def _scaled(value: int, scale: float, context: str) -> float:
    if type(value) is not int:
        raise PolicyInputFeatureValidationError(f"{context} must be an exact int")
    try:
        result = value / scale
    except OverflowError:
        raise PolicyInputFeatureValidationError(
            f"{context} cannot be represented as a finite float"
        ) from None
    if not math.isfinite(result) or abs(result) > _FLOAT32_MAX:
        raise PolicyInputFeatureValidationError(
            f"{context} cannot be represented as finite float32"
        )
    return result


def _append_optional_tile(values: list[float], tile) -> None:
    values.append(float(tile is not None))
    if tile is None:
        values.extend((_PADDING_VALUE,) * TILE_AXIS_SIZE)
    else:
        values.extend(_one_hot(tile, TILE_AXIS))


def _append_meld(values: list[float], meld: MeldFeature | None) -> None:
    values.append(float(meld is not None))
    if meld is None:
        values.extend((_PADDING_VALUE,) * (_MELD_SLOT_DIM - 1))
        return
    values.extend(_one_hot(meld.kind, MELD_KIND_AXIS))
    values.extend(
        _scaled(value, TILE_COUNT_SCALE, "meld tile count")
        for value in meld.tile_counts
    )
    values.append(float(meld.from_seat is not None))
    if meld.from_seat is None:
        values.extend((_PADDING_VALUE,) * len(RELATIVE_SEAT_AXIS))
    else:
        values.extend(_one_hot(meld.from_seat, RELATIVE_SEAT_AXIS))
    _append_optional_tile(values, meld.called_tile)


def _append_discard(values: list[float], discard: OrderedDiscardFeature | None) -> None:
    values.append(float(discard is not None))
    if discard is None:
        values.extend((_PADDING_VALUE,) * (_DISCARD_SLOT_DIM - 1))
        return
    values.extend(_one_hot(discard.discarder, RELATIVE_SEAT_AXIS))
    values.extend(_one_hot(discard.tile, TILE_AXIS))
    values.append(float(discard.tsumogiri))
    values.append(float(discard.called_by is not None))
    if discard.called_by is None:
        values.extend((_PADDING_VALUE,) * len(RELATIVE_SEAT_AXIS))
    else:
        values.extend(_one_hot(discard.called_by, RELATIVE_SEAT_AXIS))


def tensor_values(
    feature: PolicyInputFeature,
    *,
    version: str = TENSOR_SCHEMA_VERSION,
) -> tuple[float, ...]:
    """Flatten the exact v1 semantic feature to finite Python floats."""
    _require_version(version)
    if type(feature) is not PolicyInputFeature:
        raise TypeError("feature must be an exact PolicyInputFeature")

    values: list[float] = []
    values.extend(_one_hot(feature.round_wind, WIND_AXIS))
    values.extend(_one_hot(feature.hand_number, (1, 2, 3, 4)))
    values.extend(_one_hot(feature.dealer_relative_seat, RELATIVE_SEAT_AXIS))
    values.extend(_one_hot(feature.self_wind, WIND_AXIS))
    values.append(_scaled(feature.honba, HONBA_SCALE, "honba"))
    values.append(_scaled(feature.riichi_sticks, RIICHI_STICK_SCALE, "riichi_sticks"))
    values.append(
        _scaled(
            feature.live_wall_tiles_remaining,
            LIVE_WALL_SCALE,
            "live_wall_tiles_remaining",
        )
    )
    for tile in feature.dora_indicators:
        _append_optional_tile(values, tile)
    for player in feature.players:
        values.append(_scaled(player.score, SCORE_SCALE, "player score"))
        values.extend(_one_hot(player.riichi, RIICHI_AXIS))
        for meld in player.melds:
            _append_meld(values, meld)
    for discard in feature.discards:
        _append_discard(values, discard)
    values.extend(
        _scaled(value, TILE_COUNT_SCALE, "own tile count")
        for value in feature.own_tile_counts
    )
    _append_optional_tile(values, feature.drawn_tile)

    if len(values) != FEATURE_DIM:
        raise FeatureDimensionError(
            f"feature tensor dimension drifted: {len(values)} != {FEATURE_DIM}"
        )
    if any(not math.isfinite(value) or abs(value) > _FLOAT32_MAX for value in values):
        raise PolicyInputFeatureValidationError(
            "feature tensor values must all be finite float32 values"
        )
    return tuple(values)


def to_tensor(
    feature: PolicyInputFeature,
    *,
    version: str = TENSOR_SCHEMA_VERSION,
):
    """Create one float32 tensor while keeping torch an optional lazy import."""
    values = tensor_values(feature, version=version)
    import torch

    return torch.tensor(values, dtype=torch.float32)


__all__ = [
    "FEATURE_DIM",
    "FEATURE_GROUPS",
    "FEATURE_INDEX_DESCRIPTORS",
    "HONBA_SCALE",
    "LIVE_WALL_SCALE",
    "PADDING_SEMANTICS",
    "RIICHI_STICK_SCALE",
    "SCORE_SCALE",
    "TENSOR_DTYPE",
    "TENSOR_SCHEMA_VERSION",
    "TILE_AXIS_LABELS",
    "TILE_COUNT_SCALE",
    "FeatureGroup",
    "schema_fingerprint",
    "tensor_values",
    "to_tensor",
]
