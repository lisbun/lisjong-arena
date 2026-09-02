"""PolicyInput-only semantic feature for the first Learned Policy experiment.

This module intentionally owns an Arena-local experiment representation.  It does
not extend the lisjong ``PolicyInput`` contract and does not accept a
``DecisionContext``, legal actions, engine state, or HandBelief value.
"""

from dataclasses import dataclass
from enum import IntEnum

from lisjong.policy_contract import (
    Discard,
    MeldKind,
    OwnHandState,
    PlayerPublicState,
    PolicyInput,
    PublicMeld,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)

from .errors import (
    PolicyInputFeatureValidationError,
    UnsupportedFeatureSemanticsError,
)

FEATURE_SEMANTICS_ID = "arena-policy-input-feature-v1"

# These are rule/physical bounds, not observed dataset maxima.  Values beyond a
# bound are rejected instead of clipped or silently truncated.
MAX_DORA_INDICATORS = 5
MAX_MELDS_PER_PLAYER = 4
MAX_GLOBAL_DISCARDS = 136
MAX_CONCEALED_TILES = 14
MAX_LIVE_WALL_TILES = 84


class RelativeSeat(IntEnum):
    """Self-relative seat axis used by this experiment schema."""

    SELF = 0
    SHIMOCHA = 1
    TOIMEN = 2
    KAMICHA = 3


RELATIVE_SEAT_AXIS = (
    RelativeSeat.SELF,
    RelativeSeat.SHIMOCHA,
    RelativeSeat.TOIMEN,
    RelativeSeat.KAMICHA,
)
WIND_AXIS = (Wind.EAST, Wind.SOUTH, Wind.WEST, Wind.NORTH)
RIICHI_AXIS = (RiichiState.NONE, RiichiState.DECLARED, RiichiState.ACCEPTED)
MELD_KIND_AXIS = (
    MeldKind.CHI,
    MeldKind.PON,
    MeldKind.DAIMINKAN,
    MeldKind.ANKAN,
    MeldKind.KAKAN,
)

_SUITED_CATEGORIES = (
    TileCategory.MANZU,
    TileCategory.PINZU,
    TileCategory.SOUZU,
)
_BASE_TILE_TYPES = tuple(
    TileType(category, rank) for category in _SUITED_CATEGORIES for rank in range(1, 10)
) + tuple(TileType(TileCategory.HONOR, rank) for rank in range(1, 8))
TILE_AXIS = tuple(Tile(tile_type) for tile_type in _BASE_TILE_TYPES) + tuple(
    Tile(TileType(category, 5), is_red=True) for category in _SUITED_CATEGORIES
)
TILE_INDEX = {tile: index for index, tile in enumerate(TILE_AXIS)}
TILE_AXIS_SIZE = 37

if len(TILE_AXIS) != TILE_AXIS_SIZE or len(TILE_INDEX) != TILE_AXIS_SIZE:
    raise RuntimeError("the v1 tile axis must contain exactly 37 unique values")


def _validation_error(message: str) -> PolicyInputFeatureValidationError:
    return PolicyInputFeatureValidationError(message)


def _require_exact_tuple(value: object, context: str) -> tuple:
    if type(value) is not tuple:
        raise _validation_error(f"{context} must be an exact tuple")
    return value


def _require_exact_int(value: object, context: str) -> int:
    if type(value) is not int:
        raise _validation_error(f"{context} must be an exact int")
    return value


def _require_exact_bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise _validation_error(f"{context} must be an exact bool")
    return value


def _require_exact_type(value: object, expected: type, context: str):
    if type(value) is not expected:
        raise _validation_error(f"{context} must be an exact {expected.__name__}")
    return value


def _validate_tile(value: object, context: str) -> Tile:
    tile = _require_exact_type(value, Tile, context)
    _require_exact_type(tile.tile_type, TileType, f"{context}.tile_type")
    _require_exact_type(
        tile.tile_type.category,
        TileCategory,
        f"{context}.tile_type.category",
    )
    _require_exact_int(tile.tile_type.rank, f"{context}.tile_type.rank")
    _require_exact_bool(tile.is_red, f"{context}.is_red")
    if tile not in TILE_INDEX:
        raise _validation_error(f"{context} is not on the v1 37-tile axis")
    return tile


def _validate_optional_tile(value: object, context: str) -> Tile | None:
    if value is None:
        return None
    return _validate_tile(value, context)


def _validate_seat(value: object, context: str) -> Seat:
    return _require_exact_type(value, Seat, context)


def _validate_optional_seat(value: object, context: str) -> Seat | None:
    if value is None:
        return None
    return _validate_seat(value, context)


def _relative_seat(absolute: Seat, self_seat: Seat) -> RelativeSeat:
    return RelativeSeat((int(absolute) - int(self_seat)) % 4)


def _tile_counts(tiles: tuple[Tile, ...]) -> tuple[int, ...]:
    counts = [0] * TILE_AXIS_SIZE
    for tile in tiles:
        counts[TILE_INDEX[tile]] += 1
    return tuple(counts)


def _pad(values: tuple, maximum: int, context: str) -> tuple:
    if len(values) > maximum:
        raise _validation_error(
            f"{context} length {len(values)} exceeds the v1 maximum {maximum}"
        )
    return values + (None,) * (maximum - len(values))


def _validate_padded(
    values: object, maximum: int, item_type: type, context: str
) -> tuple:
    items = _require_exact_tuple(values, context)
    if len(items) != maximum:
        raise _validation_error(f"{context} must contain exactly {maximum} slots")
    padding_started = False
    for index, item in enumerate(items):
        if item is None:
            padding_started = True
        elif padding_started:
            raise _validation_error(
                f"{context} has a value after padding at slot {index}"
            )
        elif type(item) is not item_type:
            raise _validation_error(
                f"{context}[{index}] must be an exact {item_type.__name__} or None"
            )
    return items


@dataclass(frozen=True, slots=True)
class MeldFeature:
    kind: MeldKind
    tile_counts: tuple[int, ...]
    from_seat: RelativeSeat | None
    called_tile: Tile | None

    def __post_init__(self) -> None:
        _require_exact_type(self.kind, MeldKind, "meld.kind")
        counts = _require_exact_tuple(self.tile_counts, "meld.tile_counts")
        if len(counts) != TILE_AXIS_SIZE:
            raise _validation_error(
                f"meld.tile_counts must contain {TILE_AXIS_SIZE} values"
            )
        if any(type(value) is not int or not 0 <= value <= 4 for value in counts):
            raise _validation_error("meld.tile_counts must contain exact ints in 0..4")
        expected_count = 3 if self.kind in (MeldKind.CHI, MeldKind.PON) else 4
        if sum(counts) != expected_count:
            raise _validation_error(
                f"{self.kind.value} tile counts must sum to {expected_count}"
            )
        if self.kind is MeldKind.ANKAN:
            if self.from_seat is not None or self.called_tile is not None:
                raise _validation_error("ankan must not have source metadata")
        else:
            _require_exact_type(self.from_seat, RelativeSeat, "meld.from_seat")
            called_tile = _validate_tile(self.called_tile, "meld.called_tile")
            if counts[TILE_INDEX[called_tile]] == 0:
                raise _validation_error("meld.called_tile must occur in tile_counts")


@dataclass(frozen=True, slots=True)
class RelativePlayerFeature:
    relative_seat: RelativeSeat
    score: int
    riichi: RiichiState
    melds: tuple[MeldFeature | None, ...]

    def __post_init__(self) -> None:
        _require_exact_type(self.relative_seat, RelativeSeat, "player.relative_seat")
        _require_exact_int(self.score, "player.score")
        _require_exact_type(self.riichi, RiichiState, "player.riichi")
        _validate_padded(
            self.melds,
            MAX_MELDS_PER_PLAYER,
            MeldFeature,
            "player.melds",
        )


@dataclass(frozen=True, slots=True)
class OrderedDiscardFeature:
    discarder: RelativeSeat
    tile: Tile
    tsumogiri: bool
    called_by: RelativeSeat | None

    def __post_init__(self) -> None:
        _require_exact_type(self.discarder, RelativeSeat, "discard.discarder")
        _validate_tile(self.tile, "discard.tile")
        _require_exact_bool(self.tsumogiri, "discard.tsumogiri")
        if self.called_by is not None:
            _require_exact_type(self.called_by, RelativeSeat, "discard.called_by")
            if self.called_by is self.discarder:
                raise _validation_error("discard.called_by must differ from discarder")


@dataclass(frozen=True, slots=True)
class PolicyInputFeature:
    """Fixed semantic value built solely from one exact ``PolicyInput``."""

    round_wind: Wind
    hand_number: int
    dealer_relative_seat: RelativeSeat
    self_wind: Wind
    honba: int
    riichi_sticks: int
    live_wall_tiles_remaining: int
    dora_indicators: tuple[Tile | None, ...]
    players: tuple[RelativePlayerFeature, ...]
    discards: tuple[OrderedDiscardFeature | None, ...]
    own_tile_counts: tuple[int, ...]
    drawn_tile: Tile | None

    def __post_init__(self) -> None:
        _require_exact_type(self.round_wind, Wind, "feature.round_wind")
        hand_number = _require_exact_int(self.hand_number, "feature.hand_number")
        if not 1 <= hand_number <= 4:
            raise _validation_error("feature.hand_number must be in 1..4")
        _require_exact_type(
            self.dealer_relative_seat,
            RelativeSeat,
            "feature.dealer_relative_seat",
        )
        _require_exact_type(self.self_wind, Wind, "feature.self_wind")
        expected_wind = WIND_AXIS[(-int(self.dealer_relative_seat)) % 4]
        if self.self_wind is not expected_wind:
            raise _validation_error("feature.self_wind and dealer relation differ")
        for name, value in (
            ("honba", self.honba),
            ("riichi_sticks", self.riichi_sticks),
            ("live_wall_tiles_remaining", self.live_wall_tiles_remaining),
        ):
            number = _require_exact_int(value, f"feature.{name}")
            if number < 0:
                raise _validation_error(f"feature.{name} must not be negative")
        if self.live_wall_tiles_remaining > MAX_LIVE_WALL_TILES:
            raise _validation_error(
                "feature.live_wall_tiles_remaining exceeds the v1 maximum "
                f"{MAX_LIVE_WALL_TILES}"
            )
        dora = _validate_padded(
            self.dora_indicators,
            MAX_DORA_INDICATORS,
            Tile,
            "feature.dora_indicators",
        )
        for index, tile in enumerate(dora):
            if tile is not None:
                _validate_tile(tile, f"feature.dora_indicators[{index}]")
        players = _require_exact_tuple(self.players, "feature.players")
        if len(players) != 4 or any(
            type(value) is not RelativePlayerFeature for value in players
        ):
            raise _validation_error(
                "feature.players must contain four exact RelativePlayerFeature values"
            )
        if tuple(value.relative_seat for value in players) != RELATIVE_SEAT_AXIS:
            raise _validation_error("feature.players must use self-relative seat order")
        _validate_padded(
            self.discards,
            MAX_GLOBAL_DISCARDS,
            OrderedDiscardFeature,
            "feature.discards",
        )
        counts = _require_exact_tuple(self.own_tile_counts, "feature.own_tile_counts")
        if len(counts) != TILE_AXIS_SIZE:
            raise _validation_error(
                f"feature.own_tile_counts must contain {TILE_AXIS_SIZE} values"
            )
        if any(type(value) is not int or not 0 <= value <= 4 for value in counts):
            raise _validation_error(
                "feature.own_tile_counts must contain exact ints in 0..4"
            )
        if sum(counts) > MAX_CONCEALED_TILES:
            raise _validation_error(
                "feature.own_tile_counts exceed the v1 concealed-tile maximum"
            )
        drawn_tile = _validate_optional_tile(self.drawn_tile, "feature.drawn_tile")
        if drawn_tile is not None and counts[TILE_INDEX[drawn_tile]] == 0:
            raise _validation_error("feature.drawn_tile must occur in own_tile_counts")


def _validate_meld(
    value: object,
    *,
    owner: Seat,
    self_seat: Seat,
    context: str,
) -> MeldFeature:
    meld = _require_exact_type(value, PublicMeld, context)
    _require_exact_type(meld.kind, MeldKind, f"{context}.kind")
    tiles = _require_exact_tuple(meld.tiles, f"{context}.tiles")
    for index, tile in enumerate(tiles):
        _validate_tile(tile, f"{context}.tiles[{index}]")
    from_seat = _validate_optional_seat(meld.from_seat, f"{context}.from_seat")
    called_tile = _validate_optional_tile(meld.called_tile, f"{context}.called_tile")
    if from_seat is not None and from_seat is owner:
        raise _validation_error(f"{context}.from_seat must differ from its owner")
    return MeldFeature(
        kind=meld.kind,
        tile_counts=_tile_counts(tiles),
        from_seat=(None if from_seat is None else _relative_seat(from_seat, self_seat)),
        called_tile=called_tile,
    )


def _validate_discard(
    value: object,
    *,
    discarder: Seat,
    self_seat: Seat,
    context: str,
) -> tuple[int, OrderedDiscardFeature]:
    discard = _require_exact_type(value, Discard, context)
    tile = _validate_tile(discard.tile, f"{context}.tile")
    _require_exact_bool(discard.tsumogiri, f"{context}.tsumogiri")
    order = _require_exact_int(discard.order, f"{context}.order")
    if order < 0:
        raise _validation_error(f"{context}.order must not be negative")
    called_by = _validate_optional_seat(discard.called_by, f"{context}.called_by")
    if called_by is discarder:
        raise _validation_error(f"{context}.called_by must differ from discarder")
    return (
        order,
        OrderedDiscardFeature(
            discarder=_relative_seat(discarder, self_seat),
            tile=tile,
            tsumogiri=discard.tsumogiri,
            called_by=(
                None if called_by is None else _relative_seat(called_by, self_seat)
            ),
        ),
    )


def build_policy_input_feature(
    policy_input: PolicyInput,
    *,
    version: str = FEATURE_SEMANTICS_ID,
) -> PolicyInputFeature:
    """Build the v1 feature using only one exact current ``PolicyInput``.

    Unsupported versions, subclasses, malformed cross-field discard ordering,
    and values beyond a fixed physical bound fail closed.  No value is clipped or
    truncated.
    """
    if version != FEATURE_SEMANTICS_ID:
        raise UnsupportedFeatureSemanticsError(
            f"unsupported feature semantics version: {version!r}"
        )
    if type(policy_input) is not PolicyInput:
        raise TypeError("policy_input must be an exact PolicyInput")

    self_seat = _validate_seat(policy_input.self_seat, "policy_input.self_seat")
    round_state = _require_exact_type(
        policy_input.round, RoundState, "policy_input.round"
    )
    round_wind = _require_exact_type(
        round_state.round_wind, Wind, "policy_input.round.round_wind"
    )
    hand_number = _require_exact_int(
        round_state.hand_number, "policy_input.round.hand_number"
    )
    if not 1 <= hand_number <= 4:
        raise _validation_error("policy_input.round.hand_number must be in 1..4")
    dealer_seat = _validate_seat(
        round_state.dealer_seat, "policy_input.round.dealer_seat"
    )
    honba = _require_exact_int(round_state.honba, "policy_input.round.honba")
    riichi_sticks = _require_exact_int(
        round_state.riichi_sticks, "policy_input.round.riichi_sticks"
    )
    live_wall = _require_exact_int(
        round_state.live_wall_tiles_remaining,
        "policy_input.round.live_wall_tiles_remaining",
    )
    if honba < 0 or riichi_sticks < 0 or live_wall < 0:
        raise _validation_error("round counters must not be negative")
    if live_wall > MAX_LIVE_WALL_TILES:
        raise _validation_error(
            "policy_input.round.live_wall_tiles_remaining exceeds the v1 maximum "
            f"{MAX_LIVE_WALL_TILES}"
        )
    dora_values = _require_exact_tuple(
        round_state.dora_indicators, "policy_input.round.dora_indicators"
    )
    for index, tile in enumerate(dora_values):
        _validate_tile(tile, f"policy_input.round.dora_indicators[{index}]")
    dora_indicators = _pad(
        dora_values,
        MAX_DORA_INDICATORS,
        "policy_input.round.dora_indicators",
    )

    absolute_players = _require_exact_tuple(
        policy_input.players, "policy_input.players"
    )
    if len(absolute_players) != 4:
        raise _validation_error("policy_input.players must contain exactly four values")
    players_by_relative: list[RelativePlayerFeature | None] = [None] * 4
    ordered_discards: list[tuple[int, OrderedDiscardFeature]] = []
    for absolute_index, value in enumerate(absolute_players):
        seat = Seat(absolute_index)
        player = _require_exact_type(
            value,
            PlayerPublicState,
            f"policy_input.players[{absolute_index}]",
        )
        score = _require_exact_int(
            player.score, f"policy_input.players[{absolute_index}].score"
        )
        riichi = _require_exact_type(
            player.riichi,
            RiichiState,
            f"policy_input.players[{absolute_index}].riichi",
        )
        meld_values = _require_exact_tuple(
            player.melds, f"policy_input.players[{absolute_index}].melds"
        )
        melds = tuple(
            _validate_meld(
                meld,
                owner=seat,
                self_seat=self_seat,
                context=f"policy_input.players[{absolute_index}].melds[{index}]",
            )
            for index, meld in enumerate(meld_values)
        )
        padded_melds = _pad(
            melds,
            MAX_MELDS_PER_PLAYER,
            f"policy_input.players[{absolute_index}].melds",
        )
        discard_values = _require_exact_tuple(
            player.discards,
            f"policy_input.players[{absolute_index}].discards",
        )
        seat_discards = tuple(
            _validate_discard(
                discard,
                discarder=seat,
                self_seat=self_seat,
                context=(f"policy_input.players[{absolute_index}].discards[{index}]"),
            )
            for index, discard in enumerate(discard_values)
        )
        if tuple(order for order, _ in seat_discards) != tuple(
            sorted(order for order, _ in seat_discards)
        ):
            raise _validation_error(
                f"policy_input.players[{absolute_index}].discards must preserve order"
            )
        ordered_discards.extend(seat_discards)
        relative = _relative_seat(seat, self_seat)
        players_by_relative[int(relative)] = RelativePlayerFeature(
            relative_seat=relative,
            score=score,
            riichi=riichi,
            melds=padded_melds,
        )

    ordered_discards.sort(key=lambda value: value[0])
    if tuple(order for order, _ in ordered_discards) != tuple(
        range(len(ordered_discards))
    ):
        raise _validation_error(
            "policy_input discard orders must be globally unique and contiguous from zero"
        )
    discards = _pad(
        tuple(value for _, value in ordered_discards),
        MAX_GLOBAL_DISCARDS,
        "policy_input global discards",
    )

    own_hand = _require_exact_type(
        policy_input.own_hand, OwnHandState, "policy_input.own_hand"
    )
    concealed_tiles = _require_exact_tuple(
        own_hand.concealed_tiles, "policy_input.own_hand.concealed_tiles"
    )
    if len(concealed_tiles) > MAX_CONCEALED_TILES:
        raise _validation_error(
            "policy_input.own_hand.concealed_tiles length "
            f"{len(concealed_tiles)} exceeds the v1 maximum {MAX_CONCEALED_TILES}"
        )
    for index, tile in enumerate(concealed_tiles):
        _validate_tile(tile, f"policy_input.own_hand.concealed_tiles[{index}]")
    drawn_tile = _validate_optional_tile(
        own_hand.drawn_tile, "policy_input.own_hand.drawn_tile"
    )
    if drawn_tile is not None and drawn_tile not in concealed_tiles:
        raise _validation_error(
            "policy_input.own_hand.drawn_tile must occur in concealed_tiles"
        )

    dealer_relative = _relative_seat(dealer_seat, self_seat)
    feature = PolicyInputFeature(
        round_wind=round_wind,
        hand_number=hand_number,
        dealer_relative_seat=dealer_relative,
        self_wind=WIND_AXIS[(-int(dealer_relative)) % 4],
        honba=honba,
        riichi_sticks=riichi_sticks,
        live_wall_tiles_remaining=live_wall,
        dora_indicators=dora_indicators,
        players=tuple(players_by_relative),
        discards=discards,
        own_tile_counts=_tile_counts(concealed_tiles),
        drawn_tile=drawn_tile,
    )
    return feature


__all__ = [
    "FEATURE_SEMANTICS_ID",
    "MAX_CONCEALED_TILES",
    "MAX_DORA_INDICATORS",
    "MAX_GLOBAL_DISCARDS",
    "MAX_LIVE_WALL_TILES",
    "MAX_MELDS_PER_PLAYER",
    "MELD_KIND_AXIS",
    "MeldFeature",
    "OrderedDiscardFeature",
    "PolicyInputFeature",
    "RELATIVE_SEAT_AXIS",
    "RIICHI_AXIS",
    "RelativePlayerFeature",
    "RelativeSeat",
    "TILE_AXIS",
    "TILE_AXIS_SIZE",
    "WIND_AXIS",
    "build_policy_input_feature",
]
