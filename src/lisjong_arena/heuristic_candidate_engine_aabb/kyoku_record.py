"""第1層: 局精算record（#513、仕様§10.3・§10.4・§7.1）。

``CompletedMatch.history``を正本として、1半荘を1行のJSON objectへ射影し、
作成時と読込時に同じ検査を行う。

```text
CompletedMatch（同じ実行の戻り値）
    -> lisjong_engine.completed_match_facts.match_boundaries()
    -> hanchan row（dict、seat index 0..3、history順）
    -> canonical JSON roundtrip
    -> validate_hanchan_row()      ← 読込時も同じ関数
```

- 保存しない: 和了の翻・符・役内訳、手牌、牌山、和了牌、ドラ表示牌
- Arenaは点数計算・状態遷移を再実装しない。検査はengine値どうしと、lockする
  ``RuleSet.default()``の値を入力にした算術の照合だけである
- K1（schedule）とK2のidentity照合は、後続（#514）が渡す期待値に対して
  ``verify_schedule()``で行う。本moduleはlock型・seed台帳を持たない
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lisjong_engine.match_state import CompletedMatch
from lisjong_engine.round_allocation import derive_round_seed
from lisjong_engine.round_result import (
    AbortiveDrawResult,
    ExhaustiveDrawResult,
    WinResult,
)

from lisjong_arena._artifact_io import parse_json_text
from lisjong_arena.lisjong_engine import completed_match_facts as facts
from lisjong_arena.lisjong_engine.completed_match_facts import seat_index

from .errors import KyokuRecordError

PROTOCOL_ID = "arena-heuristic-candidate-engine-aabb-half-v1"
KYOKU_RECORD_SCHEMA = "arena-heuristic-candidate-engine-aabb-kyoku-record-v1"

RULES_IDENTITY = {
    "constructor": "RuleSet.default",
    "name": "project-standard-v1",
    "version": 1,
}
"""§5.2のRuleSet identity。lockのrules digestとの照合は#514が行う。"""

STARTING_POINTS = 25_000
RETURN_POINTS = 30_000
UMA = (30, 10, -10, -30)
RIICHI_STICK_POINTS = facts.RIICHI_STICK_POINTS
BANKRUPTCY_THRESHOLD = 0
"""§7.1の算術照合に使う``RuleSet.default()``の値（testで一致を固定する）。"""

OKA_TOP_POINTS = (RETURN_POINTS - STARTING_POINTS) * 4 // 1_000
"""1位のoka（pt単位）。内部単位では``10 × OKA_TOP_POINTS``。"""

ROLE_CANDIDATE = "A"
ROLE_INCUMBENT = "B"
ROLES = (ROLE_CANDIDATE, ROLE_INCUMBENT)

PLACEMENT_CANDIDATE_SEATS = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))
"""§2.2の配置表。placement ``p`` でcandidate（A）が座るseat index。"""

SEAT_COUNT = 4
WINDS = ("east", "south", "west")
"""``RoundPosition.prevailing_wind``として受理する値（北場はない）。"""
WIN_METHODS = ("tsumo", "ron")
TSUMO_ORIGINS = ("live_wall", "rinshan")
RON_ORIGINS = ("discard", "kakan", "ankan")
TRANSFER_REASONS = (
    "ron",
    "tsumo",
    "pao_ron",
    "pao_tsumo",
    "honba",
    "noten_penalty",
    "nagashi_mangan",
)
ABORTIVE_DRAW_REASONS = (
    "nine_terminals",
    "four_winds",
    "four_kans",
    "four_riichi",
    "triple_ron",
)
RESULT_KINDS = ("win", "exhaustive_draw", "abortive_draw")

_HANCHAN_CORE_FIELDS = frozenset(
    {
        "end_reason",
        "final_raw_scores",
        "final_riichi_stick_awards",
        "final_score",
        "kyokus",
        "policy_call_counts",
        "seats",
        "seed",
        "selector_call_counts",
    }
)
"""半荘rowのうち、半荘identityのkey（AABBでは ``placement``）以外のfield。"""
_SEAT_FIELDS = frozenset({"policy_identity", "role"})
_FINAL_SCORE_FIELDS = frozenset(
    {
        "bankruptcy_points",
        "base_points",
        "final_points",
        "oka_points",
        "rank",
        "score",
        "uma_points",
    }
)
_KYOKU_FIELDS = frozenset(
    {
        "dealer_continues",
        "dealer_seat",
        "hand_number",
        "honba",
        "kyoku_index",
        "next_position",
        "point_deltas",
        "points_after",
        "points_before",
        "prevailing_wind",
        "result",
        "riichi_contributions",
        "riichi_stick_awards",
        "riichi_sticks_after",
        "riichi_sticks_before",
        "round_ordinal",
        "round_seed",
        "transfers",
    }
)
_POSITION_FIELDS = frozenset(
    {"dealer_seat", "hand_number", "honba", "prevailing_wind", "riichi_sticks"}
)
_WIN_FIELDS = frozenset(
    {"is_last_tile", "method", "origin", "source_seat", "winner_seats"}
)
_EXHAUSTIVE_FIELDS = frozenset({"nagashi_mangan_seats", "tenpai_seats"})
_ABORTIVE_FIELDS = frozenset({"reason"})
_TRANSFER_FIELDS = frozenset({"amount", "payer", "reason", "recipient", "winner_seat"})
_AWARD_FIELDS = frozenset({"amount", "seat"})
_CONTRIBUTION_FIELDS = frozenset({"points", "seat"})


# ---------------------------------------------------------------------------
# input values
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeatAssignment:
    """1 seatのrole（``"A"`` / ``"B"``）とPolicy identity。"""

    role: str
    policy_identity: str

    def to_value(self) -> dict[str, str]:
        return {"policy_identity": self.policy_identity, "role": self.role}


@dataclass(frozen=True, slots=True)
class ExpectedHanchan:
    """K1 / K2の照合に後続（#514）が渡す、lock由来の1半荘の期待値。"""

    seed: int
    placement: int
    seats: tuple[SeatAssignment, SeatAssignment, SeatAssignment, SeatAssignment]


def placement_roles(placement: int) -> tuple[str, str, str, str]:
    """§2.2の配置表から、seat 0..3のroleを返す。"""
    if type(placement) is not int or not 0 <= placement < len(
        PLACEMENT_CANDIDATE_SEATS
    ):
        raise KyokuRecordError("placement must be an int in 0..5")
    candidate = PLACEMENT_CANDIDATE_SEATS[placement]
    return tuple(
        ROLE_CANDIDATE if seat in candidate else ROLE_INCUMBENT
        for seat in range(SEAT_COUNT)
    )


@dataclass(frozen=True, slots=True)
class RecordLayout:
    """半荘rowのidentity・seat roleの意味と、保存物のprotocol / schema名。

    局・終局の検査（K3〜K11）と第2層・manifestのI/Oは共通で、半荘をseed以外の
    どのkeyで識別し、seatのroleをどう検査するかだけをprotocolごとに分ける。
    AABB（#513）は ``AABB_LAYOUT``。#522の混成focal比較は別の値を使い、AABBの
    配置表・role検査を緩めない。

    ``validate_keys(row)`` はkey値の型・範囲、``validate_seats(row)`` はseatの
    role・identityを検査し、どちらも ``KyokuRecordError`` で拒否する。
    """

    protocol_id: str
    kyoku_record_schema: str
    event_fact_schema: str
    manifest_schema: str
    key_fields: tuple[str, ...]
    validate_keys: Callable[[dict], None]
    validate_seats: Callable[[dict], None]

    def hanchan_fields(self) -> frozenset[str]:
        return _HANCHAN_CORE_FIELDS | frozenset(self.key_fields)

    def key(self, row: Mapping) -> tuple:
        """event内で半荘を一意に識別する ``(seed, *keys)``。"""
        return (row["seed"], *(row[name] for name in self.key_fields))

    def context(self, row: Mapping) -> str:
        return " ".join(f"{name} {row[name]}" for name in ("seed", *self.key_fields))


# ---------------------------------------------------------------------------
# projection（作成時のみ）
# ---------------------------------------------------------------------------


def _position_value(position) -> dict[str, object]:
    return {
        "dealer_seat": seat_index(position.dealer_seat),
        "hand_number": position.hand_number,
        "honba": position.honba,
        "prevailing_wind": position.prevailing_wind.value,
        "riichi_sticks": position.riichi_sticks,
    }


def _result_value(result: object) -> dict[str, object]:
    if isinstance(result, WinResult):
        return {
            "kind": "win",
            "win": {
                "is_last_tile": result.is_last_tile,
                "method": result.method.value,
                "origin": result.origin.value,
                "source_seat": None
                if result.source_seat is None
                else seat_index(result.source_seat),
                "winner_seats": [seat_index(winner.seat) for winner in result.winners],
            },
        }
    if isinstance(result, ExhaustiveDrawResult):
        return {
            "exhaustive_draw": {
                "nagashi_mangan_seats": [
                    seat_index(seat) for seat in result.nagashi_mangan_seats
                ],
                "tenpai_seats": [seat_index(seat) for seat in result.tenpai_seats],
            },
            "kind": "exhaustive_draw",
        }
    if isinstance(result, AbortiveDrawResult):
        return {
            "abortive_draw": {"reason": result.reason.value},
            "kind": "abortive_draw",
        }
    raise KyokuRecordError(f"unsupported engine RoundResult: {type(result).__name__}")


def _kyoku_value(index: int, boundary: facts.RoundBoundary) -> dict[str, object]:
    completed = boundary.completed
    position = completed.position_before
    settlement = completed.settlement
    return {
        "dealer_continues": completed.dealer_continues,
        "dealer_seat": seat_index(position.dealer_seat),
        "hand_number": position.hand_number,
        "honba": position.honba,
        "kyoku_index": index,
        "next_position": None
        if completed.next_position is None
        else _position_value(completed.next_position),
        "point_deltas": list(boundary.point_deltas),
        "points_after": list(boundary.points_after),
        "points_before": list(boundary.points_before),
        "prevailing_wind": position.prevailing_wind.value,
        "result": _result_value(completed.result),
        "riichi_contributions": [
            {"points": item.points, "seat": seat_index(item.seat)}
            for item in settlement.riichi_contributions
        ],
        "riichi_stick_awards": [
            {"amount": item.amount, "seat": seat_index(item.recipient)}
            for item in settlement.riichi_stick_awards
        ],
        "riichi_sticks_after": settlement.riichi_sticks_after,
        "riichi_sticks_before": position.riichi_sticks,
        "round_ordinal": completed.random_provenance.round_ordinal,
        "round_seed": completed.random_provenance.round_seed,
        "transfers": [
            {
                "amount": item.amount,
                "payer": seat_index(item.payer),
                "reason": item.reason.value,
                "recipient": seat_index(item.recipient),
                "winner_seat": None
                if item.winner_seat is None
                else seat_index(item.winner_seat),
            }
            for item in settlement.transfers
        ],
    }


def canonical_line(value: object) -> str:
    """1行のcanonical JSON（key順固定、区切りなし、末尾改行）。"""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def roundtrip(value: object) -> object:
    """保存されるbytesと同じ値で検査するため、canonical JSONを経由させる。"""
    return parse_json_text(canonical_line(value))


def project_hanchan(
    match: CompletedMatch,
    *,
    seed: int,
    placement: int,
    seats: Sequence[SeatAssignment],
    selector_call_counts: Sequence[int],
    policy_call_counts: Sequence[int],
) -> dict[str, object]:
    """1半荘の``CompletedMatch``を第1層rowへ射影し、読込時と同じ検査を通す。

    ``selector_call_counts`` / ``policy_call_counts``はrunnerが同じ実行で数えた
    seat index順の値である（§10.3、K12）。
    """
    return project_row(
        match,
        seed=seed,
        keys={"placement": placement},
        seats=seats,
        selector_call_counts=selector_call_counts,
        policy_call_counts=policy_call_counts,
        layout=AABB_LAYOUT,
    )


def project_row(
    match: CompletedMatch,
    *,
    seed: int,
    keys: Mapping[str, object],
    seats: Sequence[SeatAssignment],
    selector_call_counts: Sequence[int],
    policy_call_counts: Sequence[int],
    layout: "RecordLayout",
) -> dict[str, object]:
    """``project_hanchan`` と同じ射影を、``layout`` の半荘identity keyで行う。"""
    if set(keys) != set(layout.key_fields):
        raise KyokuRecordError(f"hanchan keys must be exactly {layout.key_fields}")
    context = layout.context({"seed": seed, **keys})
    try:
        boundaries = facts.match_boundaries(match)
    except facts.CompletedMatchFactError as error:
        raise KyokuRecordError(f"{context}: {error}") from error
    if any(not isinstance(item, SeatAssignment) for item in seats) or len(seats) != 4:
        raise KyokuRecordError("seats must be four SeatAssignment values")
    players = {seat_index(player.seat): player for player in match.final_score.players}
    row = {
        "end_reason": boundaries.end_reason,
        "final_raw_scores": list(boundaries.final_raw_scores),
        "final_riichi_stick_awards": [
            {"amount": amount, "seat": seat}
            for seat, amount in boundaries.final_riichi_stick_awards
        ],
        "final_score": [
            {
                "bankruptcy_points": players[seat].bankruptcy_points,
                "base_points": players[seat].base_points,
                "final_points": players[seat].final_points,
                "oka_points": players[seat].oka_points,
                "rank": players[seat].rank,
                "score": players[seat].score,
                "uma_points": players[seat].uma_points,
            }
            for seat in range(SEAT_COUNT)
        ],
        "kyokus": [
            _kyoku_value(index, boundary)
            for index, boundary in enumerate(boundaries.rounds)
        ],
        **keys,
        "policy_call_counts": list(policy_call_counts),
        "seats": [item.to_value() for item in seats],
        "seed": seed,
        "selector_call_counts": list(selector_call_counts),
    }
    for boundary in boundaries.rounds:
        if boundary.completed.random_provenance.match_seed != seed:
            raise KyokuRecordError(
                f"{context}: CompletedMatch was played with a different match seed"
            )
    return validate_hanchan_row(roundtrip(row), layout)


# ---------------------------------------------------------------------------
# shape helpers
# ---------------------------------------------------------------------------


def _fields(value: object, fields: frozenset[str], context: str) -> dict:
    if type(value) is not dict:
        raise KyokuRecordError(f"{context} must be an object")
    if set(value) != fields:
        unknown = sorted(set(value) - fields)
        missing = sorted(fields - set(value))
        raise KyokuRecordError(
            f"{context} fields do not match the schema "
            f"(unknown {unknown}, missing {missing})"
        )
    return value


def _int(value: object, context: str) -> int:
    if type(value) is not int:
        raise KyokuRecordError(f"{context} must be an int")
    return value


def _nonneg(value: object, context: str) -> int:
    if _int(value, context) < 0:
        raise KyokuRecordError(f"{context} must be non-negative")
    return value


def _positive(value: object, context: str) -> int:
    if _int(value, context) <= 0:
        raise KyokuRecordError(f"{context} must be positive")
    return value


def _bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise KyokuRecordError(f"{context} must be a bool")
    return value


def _str(value: object, context: str) -> str:
    if type(value) is not str or not value:
        raise KyokuRecordError(f"{context} must be a non-empty string")
    return value


def _member(value: object, allowed: Sequence[str], context: str) -> str:
    if type(value) is not str or value not in allowed:
        raise KyokuRecordError(f"{context} has an unsupported value {value!r}")
    return value


def _seat(value: object, context: str) -> int:
    if type(value) is not int or not 0 <= value < SEAT_COUNT:
        raise KyokuRecordError(f"{context} must be a seat index 0..3")
    return value


def _optional_seat(value: object, context: str) -> int | None:
    return None if value is None else _seat(value, context)


def _list(value: object, context: str) -> list:
    if type(value) is not list:
        raise KyokuRecordError(f"{context} must be a list")
    return value


def _seat_list(value: object, context: str, *, allow_empty: bool = True) -> list[int]:
    seats = [_seat(item, context) for item in _list(value, context)]
    if not allow_empty and not seats:
        raise KyokuRecordError(f"{context} must not be empty")
    if len(set(seats)) != len(seats):
        raise KyokuRecordError(f"{context} must not repeat a seat")
    return seats


def _four(value: object, context: str) -> list[int]:
    items = _list(value, context)
    if len(items) != SEAT_COUNT:
        raise KyokuRecordError(f"{context} must have 4 entries")
    return [_int(item, context) for item in items]


def _seed(value: object, context: str) -> int:
    if type(value) is not int or not 0 <= value < 2**32:
        raise KyokuRecordError(f"{context} must be an unsigned 32-bit int")
    return value


def _position(value: object, context: str) -> tuple:
    _fields(value, _POSITION_FIELDS, context)
    wind = _member(value["prevailing_wind"], WINDS, f"{context}.prevailing_wind")
    hand = _int(value["hand_number"], f"{context}.hand_number")
    if not 1 <= hand <= 4:
        raise KyokuRecordError(f"{context}.hand_number must be 1..4")
    dealer = _seat(value["dealer_seat"], f"{context}.dealer_seat")
    if dealer != hand - 1:
        raise KyokuRecordError(f"{context}.dealer_seat does not match hand_number")
    honba = _nonneg(value["honba"], f"{context}.honba")
    sticks = _nonneg(value["riichi_sticks"], f"{context}.riichi_sticks")
    return (wind, hand, dealer, honba, sticks)


def _next_hand(wind: str, hand: int) -> tuple[str, int] | None:
    if hand < 4:
        return (wind, hand + 1)
    index = WINDS.index(wind)
    return None if index + 1 == len(WINDS) else (WINDS[index + 1], 1)


# ---------------------------------------------------------------------------
# kyoku validation（K3〜K9、K11）
# ---------------------------------------------------------------------------


def _validate_result(value: object, context: str) -> dict:
    if type(value) is not dict:
        raise KyokuRecordError(f"{context} must be an object")
    kind = _member(value.get("kind"), RESULT_KINDS, f"{context}.kind")
    # K11: kindごとに必須fieldがそろい、他のkindのfieldがない
    _fields(value, frozenset({"kind", kind}), context)
    body = value[kind]
    inner = f"{context}.{kind}"
    if kind == "win":
        _fields(body, _WIN_FIELDS, inner)
        method = _member(body["method"], WIN_METHODS, f"{inner}.method")
        winners = _seat_list(
            body["winner_seats"], f"{inner}.winner_seats", allow_empty=False
        )
        source = _optional_seat(body["source_seat"], f"{inner}.source_seat")
        _bool(body["is_last_tile"], f"{inner}.is_last_tile")
        if method == "tsumo":
            _member(body["origin"], TSUMO_ORIGINS, f"{inner}.origin")
            if source is not None or len(winners) != 1:
                raise KyokuRecordError(
                    f"{inner} tsumo must have one winner and no source_seat"
                )
        else:
            _member(body["origin"], RON_ORIGINS, f"{inner}.origin")
            if source is None or source in winners:
                raise KyokuRecordError(
                    f"{inner} ron source_seat must be a non-winning seat"
                )
    elif kind == "exhaustive_draw":
        _fields(body, _EXHAUSTIVE_FIELDS, inner)
        _seat_list(body["tenpai_seats"], f"{inner}.tenpai_seats")
        _seat_list(body["nagashi_mangan_seats"], f"{inner}.nagashi_mangan_seats")
    else:
        _fields(body, _ABORTIVE_FIELDS, inner)
        _member(body["reason"], ABORTIVE_DRAW_REASONS, f"{inner}.reason")
    return value


def _validate_kyoku(kyoku: object, *, seed: int, index: int, context: str) -> dict:
    _fields(kyoku, _KYOKU_FIELDS, context)
    # K3: 0始まりの連続index、round_ordinal == index + 1、engine round seed
    if type(kyoku["kyoku_index"]) is not int or kyoku["kyoku_index"] != index:
        raise KyokuRecordError(f"{context}.kyoku_index is not contiguous from 0")
    if _int(kyoku["round_ordinal"], f"{context}.round_ordinal") != index + 1:
        raise KyokuRecordError(f"{context}.round_ordinal != kyoku_index + 1")
    if _int(kyoku["round_seed"], f"{context}.round_seed") != derive_round_seed(
        seed, index + 1
    ):
        raise KyokuRecordError(f"{context}.round_seed is not the engine round seed")
    _position(
        {
            "dealer_seat": kyoku["dealer_seat"],
            "hand_number": kyoku["hand_number"],
            "honba": kyoku["honba"],
            "prevailing_wind": kyoku["prevailing_wind"],
            "riichi_sticks": kyoku["riichi_sticks_before"],
        },
        context,
    )
    _validate_result(kyoku["result"], f"{context}.result")
    before = _four(kyoku["points_before"], f"{context}.points_before")
    deltas = _four(kyoku["point_deltas"], f"{context}.point_deltas")
    after = _four(kyoku["points_after"], f"{context}.points_after")
    sticks_after = _nonneg(kyoku["riichi_sticks_after"], f"{context}.sticks_after")
    # K4 / K5
    try:
        facts.require_kyoku_balance(
            points_before=before,
            point_deltas=deltas,
            points_after=after,
            riichi_sticks_before=kyoku["riichi_sticks_before"],
            riichi_sticks_after=sticks_after,
            context=context,
        )
    except facts.CompletedMatchFactError as error:
        raise KyokuRecordError(str(error)) from error
    # K8: point_deltas == Σ受取 - Σ支払 - 立直供託 + riichi_stick_awards
    expected = [0, 0, 0, 0]
    for position, item in enumerate(_list(kyoku["transfers"], f"{context}.transfers")):
        item_context = f"{context}.transfers[{position}]"
        _fields(item, _TRANSFER_FIELDS, item_context)
        payer = _seat(item["payer"], f"{item_context}.payer")
        recipient = _seat(item["recipient"], f"{item_context}.recipient")
        amount = _positive(item["amount"], f"{item_context}.amount")
        reason = _member(item["reason"], TRANSFER_REASONS, f"{item_context}.reason")
        winner = _optional_seat(item["winner_seat"], f"{item_context}.winner_seat")
        if payer == recipient:
            raise KyokuRecordError(f"{item_context} payer and recipient are equal")
        if (reason == "noten_penalty") is not (winner is None) or (
            winner is not None and winner != recipient
        ):
            raise KyokuRecordError(f"{item_context}.winner_seat is inconsistent")
        expected[payer] -= amount
        expected[recipient] += amount
    contributions = _list(kyoku["riichi_contributions"], f"{context}.contributions")
    contribution_seats = []
    for position, item in enumerate(contributions):
        item_context = f"{context}.riichi_contributions[{position}]"
        _fields(item, _CONTRIBUTION_FIELDS, item_context)
        seat = _seat(item["seat"], f"{item_context}.seat")
        if _int(item["points"], f"{item_context}.points") != RIICHI_STICK_POINTS:
            raise KyokuRecordError(f"{item_context}.points != riichi stick points")
        contribution_seats.append(seat)
        expected[seat] -= RIICHI_STICK_POINTS
    if len(set(contribution_seats)) != len(contribution_seats):
        raise KyokuRecordError(f"{context} riichi contribution seats repeat")
    awards = _list(kyoku["riichi_stick_awards"], f"{context}.riichi_stick_awards")
    for position, item in enumerate(awards):
        item_context = f"{context}.riichi_stick_awards[{position}]"
        _fields(item, _AWARD_FIELDS, item_context)
        seat = _seat(item["seat"], f"{item_context}.seat")
        expected[seat] += _positive(item["amount"], f"{item_context}.amount")
    if expected != deltas:
        raise KyokuRecordError(
            f"{context} point_deltas do not match transfers, riichi contributions "
            "and riichi stick awards"
        )
    _bool(kyoku["dealer_continues"], f"{context}.dealer_continues")
    if kyoku["next_position"] is not None:
        _position(kyoku["next_position"], f"{context}.next_position")
    return kyoku


def _require_dealer_continuation(kyoku: dict, context: str) -> None:
    """K9: ``dealer_continues``と本場の推移をengine値どうしで照合する。"""
    dealer = kyoku["dealer_seat"]
    result = kyoku["result"]
    kind = result["kind"]
    if kind == "win":
        continues = dealer in result["win"]["winner_seats"]
    elif kind == "abortive_draw":
        continues = True
    else:
        continues = dealer in result["exhaustive_draw"]["tenpai_seats"]
    if kyoku["dealer_continues"] is not continues:
        raise KyokuRecordError(f"{context}.dealer_continues contradicts the result")
    following = kyoku["next_position"]
    if following is None:
        return
    current = (kyoku["prevailing_wind"], kyoku["hand_number"])
    destination = (following["prevailing_wind"], following["hand_number"])
    if continues:
        ok = destination == current and following["honba"] == kyoku["honba"] + 1
    else:
        expected_honba = 0 if kind == "win" else kyoku["honba"] + 1
        ok = (
            destination == _next_hand(*current) and following["honba"] == expected_honba
        )
    if not ok:
        raise KyokuRecordError(
            f"{context}.next_position contradicts dealer_continues / honba"
        )


# ---------------------------------------------------------------------------
# final score validation（K10 = §7.1 C1〜C10）
# ---------------------------------------------------------------------------


def _trunc_thousands(value: int) -> int:
    magnitude = abs(value) // 1_000
    return magnitude if value >= 0 else -magnitude


def _validate_final(row: dict, last: dict) -> None:
    context = "final_score"
    scores = _list(row["final_score"], context)
    if len(scores) != SEAT_COUNT:
        raise KyokuRecordError(f"{context} must have 4 entries")
    players = []
    for seat, item in enumerate(scores):
        item_context = f"{context}[{seat}]"
        _fields(item, _FINAL_SCORE_FIELDS, item_context)
        # C10: 保存値はすべて整数（内部単位 1 = 0.1 pt）
        players.append({name: _int(item[name], item_context) for name in item})
    final_raw = _four(row["final_raw_scores"], "final_raw_scores")
    awards = []
    for position, item in enumerate(
        _list(row["final_riichi_stick_awards"], "final_riichi_stick_awards")
    ):
        item_context = f"final_riichi_stick_awards[{position}]"
        _fields(item, _AWARD_FIELDS, item_context)
        awards.append(
            (
                _seat(item["seat"], f"{item_context}.seat"),
                _positive(item["amount"], f"{item_context}.amount"),
            )
        )
    end_reason = _member(
        row["end_reason"], sorted(facts.MATCH_END_REASONS), "end_reason"
    )
    # C1
    if sum(player["final_points"] for player in players) != 0:
        raise KyokuRecordError("C1: final_points do not sum to 0")
    # C2
    for seat, player in enumerate(players):
        if player["final_points"] != (
            player["base_points"]
            + player["uma_points"]
            + player["oka_points"]
            + player["bankruptcy_points"]
        ):
            raise KyokuRecordError(f"C2: seat {seat} final_points decomposition")
    # C3
    try:
        facts.require_final_adjustment(
            final_points_after=last["points_after"],
            final_riichi_sticks_after=last["riichi_sticks_after"],
            final_raw_scores=final_raw,
            awards=awards,
        )
    except facts.CompletedMatchFactError as error:
        raise KyokuRecordError(f"C3: {error}") from error
    # C4
    if sum(final_raw) != SEAT_COUNT * STARTING_POINTS:
        raise KyokuRecordError("C4: final_raw_scores do not sum to 4 x starting")
    # C5
    if [player["score"] for player in players] != final_raw:
        raise KyokuRecordError("C5: FinalPlayerScore.score != final_raw_scores")
    # C6
    order = sorted(range(SEAT_COUNT), key=lambda seat: (-final_raw[seat], seat))
    if [players[seat]["rank"] for seat in order] != [1, 2, 3, 4]:
        raise KyokuRecordError("C6: ranks do not follow (-score, seat order)")
    # C7
    for player in players:
        if player["uma_points"] != 10 * UMA[player["rank"] - 1]:
            raise KyokuRecordError("C7: uma_points mismatch")
        expected_oka = 10 * OKA_TOP_POINTS if player["rank"] == 1 else 0
        if player["oka_points"] != expected_oka:
            raise KyokuRecordError("C7: oka_points mismatch")
    # C8
    non_top = 0
    for seat in order[1:]:
        value = _trunc_thousands(players[seat]["score"] - RETURN_POINTS)
        if players[seat]["base_points"] != 10 * value:
            raise KyokuRecordError(f"C8: seat {seat} base_points mismatch")
        non_top += value
    if players[order[0]]["base_points"] != 10 * (-OKA_TOP_POINTS - non_top):
        raise KyokuRecordError("C8: top base_points mismatch")
    # C9
    penalties = [player["bankruptcy_points"] for player in players]
    if sum(penalties) != 0:
        raise KyokuRecordError("C9: bankruptcy_points do not sum to 0")
    below = {
        seat
        for seat in range(SEAT_COUNT)
        if last["points_after"][seat] < BANKRUPTCY_THRESHOLD
    }
    if end_reason != "bankruptcy":
        if any(penalties) or below:
            raise KyokuRecordError("C9: bankruptcy values without bankruptcy end")
    elif not below or any(
        penalties[seat] < 0 and seat not in below for seat in range(SEAT_COUNT)
    ):
        raise KyokuRecordError("C9: bankruptcy penalty on a seat not below zero")


# ---------------------------------------------------------------------------
# hanchan validation
# ---------------------------------------------------------------------------


def _validate_aabb_keys(row: dict) -> None:
    placement_roles(row["placement"])


def _validate_seats(row: dict) -> None:
    """K2（record内部）: roleが§2.2の配置表と一致し、roleごとにidentityが1つ。"""
    seats = _list(row["seats"], "seats")
    if len(seats) != SEAT_COUNT:
        raise KyokuRecordError("seats must have 4 entries")
    identities: dict[str, set[str]] = {role: set() for role in ROLES}
    for seat, item in enumerate(seats):
        _fields(item, _SEAT_FIELDS, f"seats[{seat}]")
        role = _member(item["role"], ROLES, f"seats[{seat}].role")
        identities[role].add(_str(item["policy_identity"], f"seats[{seat}].identity"))
    if [item["role"] for item in seats] != list(placement_roles(row["placement"])):
        raise KyokuRecordError("K2: seat roles do not match the placement table")
    if any(len(values) != 1 for values in identities.values()) or (
        identities[ROLE_CANDIDATE] == identities[ROLE_INCUMBENT]
    ):
        raise KyokuRecordError(
            "K2: each role must have one policy_identity and A must differ from B"
        )


AABB_LAYOUT = RecordLayout(
    protocol_id=PROTOCOL_ID,
    kyoku_record_schema=KYOKU_RECORD_SCHEMA,
    event_fact_schema="arena-heuristic-candidate-engine-aabb-event-fact-v1",
    manifest_schema="arena-heuristic-candidate-engine-aabb-record-manifest-v1",
    key_fields=("placement",),
    validate_keys=_validate_aabb_keys,
    validate_seats=_validate_seats,
)
"""#513のAABB record（半荘は ``(seed, placement)``、roleはA / Bの配置表）。"""


def validate_hanchan_row(row: object, layout: RecordLayout = AABB_LAYOUT) -> dict:
    """第1層の1半荘rowを検査し、そのまま返す（作成時・読込時の共通検査）。"""
    _fields(row, layout.hanchan_fields(), "hanchan")
    seed = _seed(row["seed"], "seed")
    layout.validate_keys(row)
    context = layout.context(row)
    try:
        layout.validate_seats(row)
        # K12
        selector = _four(row["selector_call_counts"], "selector_call_counts")
        policy = _four(row["policy_call_counts"], "policy_call_counts")
        if any(value < 0 for value in selector + policy):
            raise KyokuRecordError("call counts must be non-negative")
        if selector != policy:
            raise KyokuRecordError(
                "K12: selector and Policy call counts differ for some seat"
            )
        kyokus = _list(row["kyokus"], "kyokus")
        # K3: 1局以上
        if not kyokus:
            raise KyokuRecordError("K3: a hanchan must have at least one kyoku")
        identities = set()
        for index, kyoku in enumerate(kyokus):
            kyoku_context = f"kyokus[{index}]"
            _validate_kyoku(kyoku, seed=seed, index=index, context=kyoku_context)
            identity = (kyoku["prevailing_wind"], kyoku["hand_number"], kyoku["honba"])
            if identity in identities:
                raise KyokuRecordError(f"K3: {kyoku_context} round identity repeats")
            identities.add(identity)
            # K6
            if index == 0:
                if (
                    kyoku["points_before"] != [STARTING_POINTS] * SEAT_COUNT
                    or kyoku["riichi_sticks_before"] != 0
                    or identity != ("east", 1, 0)
                ):
                    raise KyokuRecordError(
                        "K6: the first kyoku must start at East 1, 0 honba, "
                        "no sticks, 25000 each"
                    )
            else:
                previous = kyokus[index - 1]
                try:
                    facts.require_continuity(
                        previous_points_after=previous["points_after"],
                        previous_riichi_sticks_after=previous["riichi_sticks_after"],
                        points_before=kyoku["points_before"],
                        riichi_sticks_before=kyoku["riichi_sticks_before"],
                        context=f"K6: {kyoku_context}",
                    )
                except facts.CompletedMatchFactError as error:
                    raise KyokuRecordError(str(error)) from error
            # K7
            is_last = index == len(kyokus) - 1
            following = kyoku["next_position"]
            if is_last:
                if following is not None:
                    raise KyokuRecordError("K7: the final kyoku has a next_position")
            else:
                nxt = kyokus[index + 1]
                if following != {
                    "dealer_seat": nxt["dealer_seat"],
                    "hand_number": nxt["hand_number"],
                    "honba": nxt["honba"],
                    "prevailing_wind": nxt["prevailing_wind"],
                    "riichi_sticks": nxt["riichi_sticks_before"],
                }:
                    raise KyokuRecordError(
                        f"K7: {kyoku_context}.next_position is not the next kyoku"
                    )
            # K9
            _require_dealer_continuation(kyoku, kyoku_context)
        # K10
        _validate_final(row, kyokus[-1])
    except KyokuRecordError as error:
        raise KyokuRecordError(f"{context}: {error}") from error
    return row


def require_unique_hanchans(
    rows: Sequence[Mapping], layout: RecordLayout = AABB_LAYOUT
) -> None:
    """event内で半荘のkey（AABBは ``(seed, placement)``）が重複しないこと（K1の一部）。"""
    keys = [layout.key(row) for row in rows]
    if len(set(keys)) != len(keys):
        names = ", ".join(("seed", *layout.key_fields))
        raise KyokuRecordError(f"K1: ({names}) appears more than once")


# ---------------------------------------------------------------------------
# K1 / K2 schedule API（期待値は後続が渡す）
# ---------------------------------------------------------------------------


def verify_schedule(
    rows: Sequence[Mapping], expected: Sequence[ExpectedHanchan]
) -> None:
    """recordの半荘列が期待scheduleと件数・順序・seat assignmentまで一致するか。

    ``expected``は後続（#514）がlockのordered seeds、§2.2の配置表、participant
    identityから作る。本関数はlockを読まない。欠落・重複・順序違い・余分な半荘・
    role / identity違いをそれぞれ拒否する。
    """
    if any(not isinstance(item, ExpectedHanchan) for item in expected):
        raise KyokuRecordError("expected schedule must contain ExpectedHanchan values")
    expected_keys = [(item.seed, item.placement) for item in expected]
    if not expected_keys or len(set(expected_keys)) != len(expected_keys):
        raise KyokuRecordError("expected schedule must be non-empty and unique")
    for item in expected:
        if [seat.role for seat in item.seats] != list(placement_roles(item.placement)):
            raise KyokuRecordError(
                "expected schedule seat roles do not match the placement table"
            )
    keys = [(row["seed"], row["placement"]) for row in rows]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise KyokuRecordError(f"K1: duplicated hanchan {duplicated}")
    missing = [key for key in expected_keys if key not in set(keys)]
    if missing:
        raise KyokuRecordError(f"K1: missing hanchan {missing}")
    unexpected = [key for key in keys if key not in set(expected_keys)]
    if unexpected:
        raise KyokuRecordError(f"K1: unexpected hanchan {unexpected}")
    if keys != expected_keys:
        raise KyokuRecordError("K1: hanchan order differs from the expected schedule")
    for row, item in zip(rows, expected, strict=True):
        if row["seats"] != [seat.to_value() for seat in item.seats]:
            raise KyokuRecordError(
                f"K2: seed {item.seed} placement {item.placement} seat roles / "
                "policy identities differ from the expected assignment"
            )


__all__ = [
    "AABB_LAYOUT",
    "BANKRUPTCY_THRESHOLD",
    "KYOKU_RECORD_SCHEMA",
    "OKA_TOP_POINTS",
    "PLACEMENT_CANDIDATE_SEATS",
    "PROTOCOL_ID",
    "RETURN_POINTS",
    "RIICHI_STICK_POINTS",
    "ROLES",
    "ROLE_CANDIDATE",
    "ROLE_INCUMBENT",
    "RULES_IDENTITY",
    "STARTING_POINTS",
    "UMA",
    "ExpectedHanchan",
    "RecordLayout",
    "SeatAssignment",
    "canonical_line",
    "placement_roles",
    "project_hanchan",
    "project_row",
    "require_unique_hanchans",
    "roundtrip",
    "validate_hanchan_row",
    "verify_schedule",
]
