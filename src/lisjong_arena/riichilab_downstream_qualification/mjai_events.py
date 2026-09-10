"""#170 server-side MJAI eventのstrict readerとviewer-safe projection。

このmoduleはIssue #203のinformation-flow boundaryの入口である。retainedな
#170 recordはserver-side logであり、`start_kyoku.tehais`や他家の`tsumo.pai`の
ようなomniscient fieldを持つ。したがってplayer-safe側のstateは、raw eventを
直接読まず、必ず`project_visible_event()`が返す`VisibleEvent`だけから構成する。

```text
raw MJAI event
    -> project_visible_event(event, viewer)   # viewerが観測できる分だけ
    -> VisibleEvent
    -> PlayerSafeRoundState                   # player_safe.py
```

`scrub_hidden_fields()`は、そのviewerが観測できないfieldを落としたeventを
返す。projectionが本当にhidden fieldを消費していなければ、raw eventからの
projectionとscrub済みeventからのprojectionは一致する。この一致をreplay中に
毎event検証し、`leakage_check_failures`として計数する。

未知のnotation、未知のevent type、欠落fieldはheuristicで補完せず
`MjaiReplayError`でfail closedする。例外messageはlocal diagnostic専用であり、
report artifactへはreason codeだけを載せる（raw third-party contentをartifactへ
持ち出さないため）。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile, tile_sort_key
from lisjong.policy_contract.wind import Wind

# MJAI牌表記 -> lisjong `Tile`のcanonical seam。RiichiLab protocol bridgeの
# `possible_action_validation`も同じ関数でserver側MJAI牌を解釈しており、
# ここでnotation解釈を二重定義しない。赤5は`"5mr"`等として保存され、
# 未確認表記(`"0m"`等)はfail closedになる。
from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_mjai

_SEAT_COUNT = 4
_INITIAL_HAND_SIZE = 13

_WIND_BY_BAKAZE = {"E": Wind.EAST, "S": Wind.SOUTH, "W": Wind.WEST, "N": Wind.NORTH}

# Issue #170のhidden-information diagnosticが「masked」として数える表記。
# 未知のnotation誤りとmask由来の欠落を、reason codeとして区別するために使う。
MASKED_TILE_MARKERS = frozenset({"?", "??", "_", "masked", "unknown"})

_TILE_NOTATION_ERRORS = (TypeError, ValueError)


class UnsupportedReason(Enum):
    """exact reconstructionできなかったstateのreason code。

    reason codeはreport artifactへ載せる唯一のfailure表現である。raw event
    fragmentやtile列をartifactへ持ち出さない。
    """

    UNRECOGNIZED_TILE_NOTATION = "unrecognized_tile_notation"
    UNRECOGNIZED_EVENT_TYPE = "unrecognized_event_type"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    EVENT_OUT_OF_ORDER = "event_out_of_order"
    HAND_ACCOUNTING_VIOLATION = "hand_accounting_violation"
    AMBIGUOUS_KAKAN_SOURCE_PON = "ambiguous_kakan_source_pon"
    AMBIGUOUS_CALL_TARGET = "ambiguous_call_target"
    RON_TRIGGER_CONTEXT_UNRESOLVED = "ron_trigger_context_unresolved"
    TSUMO_TRIGGER_CONTEXT_UNRESOLVED = "tsumo_trigger_context_unresolved"
    DECISION_ACTION_NOT_OBSERVED = "decision_action_not_observed"
    UNSUPPORTED_ACTION_FAMILY = "unsupported_action_family"
    INCOMPLETE_INITIAL_HANDS = "incomplete_initial_hands"
    MASKED_OR_MISSING_DRAW = "masked_or_missing_draw"
    PARTICIPATION_SEAT_JOIN_FAILED = "participation_seat_join_failed"


class MjaiReplayError(Exception):
    """exact reconstructionを続行できないstateを表すfail-closed例外。"""

    __slots__ = ("reason",)

    def __init__(self, reason: UnsupportedReason, detail: str) -> None:
        if not isinstance(reason, UnsupportedReason):
            raise TypeError("reason must be an UnsupportedReason")
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason


class VisibleEventKind(Enum):
    """viewerが観測できるevent種別。"""

    ROUND_START = "round_start"
    SELF_DRAW = "self_draw"
    OPPONENT_DRAW = "opponent_draw"
    DISCARD = "discard"
    CHI = "chi"
    PON = "pon"
    DAIMINKAN = "daiminkan"
    ANKAN = "ankan"
    KAKAN = "kakan"
    REACH_DECLARED = "reach_declared"
    REACH_ACCEPTED = "reach_accepted"
    DORA_REVEALED = "dora_revealed"
    ROUND_TERMINAL = "round_terminal"
    ROUND_END = "round_end"
    IGNORED = "ignored"


# 局内でstateを更新するraw action event type。ここに無いevent typeは、
# 局のstate machineに対してnon-criticalなserver metadataとして扱う
# (`riichilab_corpus.validation`のforward compatibility方針と同じ)。
ACTION_EVENT_TYPES = frozenset(
    {
        "dahai",
        "chi",
        "pon",
        "daiminkan",
        "ankan",
        "kakan",
        "reach",
        "hora",
        "ryukyoku",
        "none",
    }
)

_KNOWN_EVENT_TYPES = ACTION_EVENT_TYPES | {
    "start_game",
    "start_kyoku",
    "tsumo",
    "reach_accepted",
    "dora",
    "end_kyoku",
    "end_game",
}

_TERMINAL_EVENT_TYPES = frozenset({"hora", "ryukyoku"})


@dataclass(frozen=True, slots=True)
class VisibleRoundStart:
    """viewerがstart_kyokuで観測できる局開始情報。

    `viewer_concealed_tiles`はviewer自身の配牌だけである。他家の`tehais`は
    このvalueへ入らない。
    """

    prevailing_wind: Wind
    hand_number: int
    honba: int
    riichi_sticks: int | None
    dealer_seat: Seat
    dora_indicator: Tile
    seat_scores: tuple[int, ...] | None
    viewer_concealed_tiles: tuple[Tile, ...]


@dataclass(frozen=True, slots=True)
class VisibleEvent:
    """1件のraw eventを、あるviewerの観測可能情報だけへprojectionしたvalue。"""

    kind: VisibleEventKind
    actor: Seat | None = None
    target: Seat | None = None
    tile: Tile | None = None
    tiles: tuple[Tile, ...] = ()
    tsumogiri: bool | None = None
    start: VisibleRoundStart | None = None


def _mapping(event: object) -> Mapping:
    if not isinstance(event, Mapping):
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD, "event is not a mapping"
        )
    return event


def event_type(event: object) -> str:
    value = _mapping(event).get("type")
    if type(value) is not str or not value:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD, "event has no type"
        )
    return value


def is_known_event_type(value: str) -> bool:
    return value in _KNOWN_EVENT_TYPES


def is_terminal_event_type(value: str) -> bool:
    return value in _TERMINAL_EVENT_TYPES


def read_seat(event: Mapping, field: str) -> Seat:
    value = event.get(field)
    if type(value) is not int or not 0 <= value < _SEAT_COUNT:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD,
            f"{field} must be a seat index from 0 through 3",
        )
    return Seat(value)


def read_optional_seat(event: Mapping, field: str) -> Seat | None:
    if field not in event:
        return None
    return read_seat(event, field)


def read_int(event: Mapping, field: str) -> int:
    value = event.get(field)
    if type(value) is not int:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD, f"{field} must be an int"
        )
    return value


def read_optional_int(event: Mapping, field: str) -> int | None:
    if field not in event:
        return None
    return read_int(event, field)


def read_bool(event: Mapping, field: str) -> bool:
    value = event.get(field)
    if type(value) is not bool:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD, f"{field} must be a bool"
        )
    return value


def read_tile(event: Mapping, field: str) -> Tile:
    value = event.get(field)
    if type(value) is not str:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD,
            f"{field} must be an MJAI tile string",
        )
    try:
        return tile_from_mjai(value)
    except _TILE_NOTATION_ERRORS as error:
        raise MjaiReplayError(
            UnsupportedReason.UNRECOGNIZED_TILE_NOTATION,
            f"{field} is not a supported MJAI tile notation",
        ) from error


def read_tiles(event: Mapping, field: str, count: int) -> tuple[Tile, ...]:
    value = event.get(field)
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != count
    ):
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD,
            f"{field} must contain exactly {count} MJAI tile strings",
        )
    tiles = []
    for item in value:
        if type(item) is not str:
            raise MjaiReplayError(
                UnsupportedReason.MISSING_REQUIRED_FIELD,
                f"{field} entries must be MJAI tile strings",
            )
        try:
            tiles.append(tile_from_mjai(item))
        except _TILE_NOTATION_ERRORS as error:
            raise MjaiReplayError(
                UnsupportedReason.UNRECOGNIZED_TILE_NOTATION,
                f"{field} contains an unsupported MJAI tile notation",
            ) from error
    return tuple(sorted(tiles, key=tile_sort_key))


def _read_seat_scores(event: Mapping) -> tuple[int, ...] | None:
    """`scores`はserver metadataとして任意扱いにする。

    欠落を0等で補完せず`None`（unknown）として表現する。存在する場合だけ
    4 seat分のintであることを検証する。
    """
    if "scores" not in event:
        return None
    value = event["scores"]
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != _SEAT_COUNT
        or any(type(item) is not int for item in value)
    ):
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD,
            "scores must contain exactly four integer seat scores",
        )
    return tuple(value)


def read_own_initial_hand(event: Mapping, viewer: Seat) -> tuple[Tile, ...]:
    """`start_kyoku.tehais`から、viewer自身の配牌だけを読み出す。

    他家のentryはindex参照すらしない。omniscientなserver recordから
    player-safe projectionを作るための中心的な境界である。
    """
    value = event.get("tehais")
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != _SEAT_COUNT
    ):
        raise MjaiReplayError(
            UnsupportedReason.INCOMPLETE_INITIAL_HANDS,
            "start_kyoku must carry four seat hands",
        )
    own = value[int(viewer)]
    if (
        not isinstance(own, Sequence)
        or isinstance(own, (str, bytes))
        or len(own) != _INITIAL_HAND_SIZE
    ):
        raise MjaiReplayError(
            UnsupportedReason.INCOMPLETE_INITIAL_HANDS,
            "start_kyoku hand must contain thirteen tiles",
        )
    tiles = []
    for item in own:
        if type(item) is not str:
            raise MjaiReplayError(
                UnsupportedReason.INCOMPLETE_INITIAL_HANDS,
                "start_kyoku hand entries must be MJAI tile strings",
            )
        try:
            tiles.append(tile_from_mjai(item))
        except _TILE_NOTATION_ERRORS as error:
            raise MjaiReplayError(
                UnsupportedReason.UNRECOGNIZED_TILE_NOTATION,
                "start_kyoku hand contains an unsupported MJAI tile notation",
            ) from error
    return tuple(sorted(tiles, key=tile_sort_key))


def _read_round_start(event: Mapping, viewer: Seat) -> VisibleRoundStart:
    bakaze = event.get("bakaze")
    prevailing_wind = _WIND_BY_BAKAZE.get(bakaze) if type(bakaze) is str else None
    if prevailing_wind is None:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD,
            "start_kyoku bakaze must be one of E / S / W / N",
        )
    hand_number = read_int(event, "kyoku")
    if hand_number < 1:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD, "start_kyoku kyoku must be >= 1"
        )
    honba = read_int(event, "honba")
    if honba < 0:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD, "start_kyoku honba must be >= 0"
        )
    riichi_sticks = read_optional_int(event, "kyotaku")
    if riichi_sticks is not None and riichi_sticks < 0:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD, "start_kyoku kyotaku must be >= 0"
        )
    return VisibleRoundStart(
        prevailing_wind=prevailing_wind,
        hand_number=hand_number,
        honba=honba,
        riichi_sticks=riichi_sticks,
        dealer_seat=read_seat(event, "oya"),
        dora_indicator=read_tile(event, "dora_marker"),
        seat_scores=_read_seat_scores(event),
        viewer_concealed_tiles=read_own_initial_hand(event, viewer),
    )


def project_visible_event(event: object, viewer: Seat) -> VisibleEvent:
    """raw MJAI eventを、`viewer`が観測できる情報だけへprojectionする。

    ここで落とすserver-only truthは次のとおりである。

    ```text
    start_kyoku.tehais[other]   他家の配牌
    tsumo.pai (actor != viewer) 他家のtsumo牌 = future wall / 未公開draw
    hora / ryukyoku payload     終局時に公開されるhand・裏dora・点数移動
    ```

    `hora` / `ryukyoku`はround terminalとしてだけ観測し、payloadを読まない。
    terminal以降のdecisionは存在しないため、terminal payloadをplayer-safe
    stateへ持ち込む理由がない。
    """
    if not isinstance(viewer, Seat):
        raise TypeError("viewer must be a Seat")
    mapping = _mapping(event)
    kind = event_type(mapping)

    if kind == "start_kyoku":
        return VisibleEvent(
            kind=VisibleEventKind.ROUND_START, start=_read_round_start(mapping, viewer)
        )
    if kind == "tsumo":
        actor = read_seat(mapping, "actor")
        if actor != viewer:
            return VisibleEvent(kind=VisibleEventKind.OPPONENT_DRAW, actor=actor)
        return VisibleEvent(
            kind=VisibleEventKind.SELF_DRAW,
            actor=actor,
            tile=read_tile(mapping, "pai"),
        )
    if kind == "dahai":
        return VisibleEvent(
            kind=VisibleEventKind.DISCARD,
            actor=read_seat(mapping, "actor"),
            tile=read_tile(mapping, "pai"),
            tsumogiri=read_bool(mapping, "tsumogiri"),
        )
    if kind in {"chi", "pon", "daiminkan"}:
        consumed_count = 2 if kind in {"chi", "pon"} else 3
        return VisibleEvent(
            kind=VisibleEventKind[kind.upper()],
            actor=read_seat(mapping, "actor"),
            target=read_seat(mapping, "target"),
            tile=read_tile(mapping, "pai"),
            tiles=read_tiles(mapping, "consumed", consumed_count),
        )
    if kind == "ankan":
        return VisibleEvent(
            kind=VisibleEventKind.ANKAN,
            actor=read_seat(mapping, "actor"),
            tiles=read_tiles(mapping, "consumed", 4),
        )
    if kind == "kakan":
        return VisibleEvent(
            kind=VisibleEventKind.KAKAN,
            actor=read_seat(mapping, "actor"),
            tile=read_tile(mapping, "pai"),
            tiles=read_tiles(mapping, "consumed", 3),
        )
    if kind == "reach":
        return VisibleEvent(
            kind=VisibleEventKind.REACH_DECLARED, actor=read_seat(mapping, "actor")
        )
    if kind == "reach_accepted":
        return VisibleEvent(
            kind=VisibleEventKind.REACH_ACCEPTED, actor=read_seat(mapping, "actor")
        )
    if kind == "dora":
        return VisibleEvent(
            kind=VisibleEventKind.DORA_REVEALED,
            tile=read_tile(mapping, "dora_marker"),
        )
    if kind in _TERMINAL_EVENT_TYPES:
        return VisibleEvent(kind=VisibleEventKind.ROUND_TERMINAL)
    if kind == "end_kyoku":
        return VisibleEvent(kind=VisibleEventKind.ROUND_END)
    if kind in {"start_game", "end_game", "none"}:
        return VisibleEvent(kind=VisibleEventKind.IGNORED)
    # 未知のserver metadataは、局のstate machineへ影響しないものとして無視する。
    return VisibleEvent(kind=VisibleEventKind.IGNORED)


def scrub_hidden_fields(event: object, viewer: Seat) -> dict:
    """`viewer`が観測できないfieldを落としたeventのcopyを返す。

    leakage checkの参照入力であり、production replay pathでstate構成には
    使わない。`project_visible_event()`がhidden fieldを一切読んでいなければ、
    raw eventからのprojectionとこのcopyからのprojectionは一致する。
    """
    if not isinstance(viewer, Seat):
        raise TypeError("viewer must be a Seat")
    mapping = _mapping(event)
    kind = event_type(mapping)
    scrubbed = dict(mapping)

    if kind == "start_kyoku":
        hands = mapping.get("tehais")
        if isinstance(hands, Sequence) and not isinstance(hands, (str, bytes)):
            scrubbed["tehais"] = [
                list(hand) if index == int(viewer) else None
                for index, hand in enumerate(hands)
            ]
        return scrubbed
    if kind == "tsumo":
        actor = mapping.get("actor")
        if actor != int(viewer):
            scrubbed.pop("pai", None)
        return scrubbed
    if kind in _TERMINAL_EVENT_TYPES:
        return {"type": kind}
    return scrubbed


__all__ = [
    "ACTION_EVENT_TYPES",
    "MASKED_TILE_MARKERS",
    "MjaiReplayError",
    "UnsupportedReason",
    "VisibleEvent",
    "VisibleEventKind",
    "VisibleRoundStart",
    "event_type",
    "is_known_event_type",
    "is_terminal_event_type",
    "project_visible_event",
    "read_bool",
    "read_int",
    "read_optional_int",
    "read_optional_seat",
    "read_seat",
    "read_tile",
    "read_tiles",
    "scrub_hidden_fields",
]
