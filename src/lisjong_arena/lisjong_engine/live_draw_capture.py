"""engine経路の通常ツモopt-in capture（#596、設計 #594、方式B）。

lisjong-engineの`run_hanchan(on_transaction_observation=...)`が渡すprivileged
`TransactionObservation`から、**通常ツモ（`DrawSource.LIVE_WALL`）ごとに**、
ツモ直前の盤面と実現牌を1行として作る。

```text
run_hanchan
  on_transaction_observation ──> LiveDrawCapture（一方向のobserver）
  on_round_evidence_complete ──> LiveDrawCapture（独立した件数照合用、player-safe）
        ↓ finish(CompletedMatch)
CapturedHanchan（行と局ごとの件数）
        ↓ live_draw_store.write_live_draw_capture()
privileged artifact（正式result・局記録・GameTraceとは別）
```

- Arenaは手牌の畳み込みや状態遷移を実装しない。ツモ直前の盤面は、DRAW
  transactionの**直前に受け取った観測**の最後のstepの`RoundCheckpoint`を
  そのまま使う（事後checkpointから実現牌を取り除いて逆算しない）
- 各行は`identity` / `pre_draw` / `realized`に分ける。`pre_draw`はツモ直前に
  成立していた盤面だけを持つ。`identity`（seed等）と`realized`（実現牌）を
  合わせると未来を再構成できるため、artifact全体はprivilegedとして扱い、
  価値関数・条件付き分布の入力へは`pre_draw`から射影したfieldだけを渡す
  （射影の定義はlisjongの次段階）
- 嶺上ツモ（`DrawSource.RINSHAN`）は行を作らず、局ごとの件数だけを数える
- 完全性は、captureの自己カウンタではなく、player-safeな
  `on_round_evidence_complete`の`DrawEvidence`（別経路）と照合する
- 観測はPolicy・selector・player-safe deliveryへ渡さない。observerの例外は
  engineの契約どおり実行全体を失敗させ、成功分だけの結果を作らない
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lisjong_engine.match_state import CompletedMatch
from lisjong_engine.meld import Ankan, Chi, Daiminkan, Kakan, Pon
from lisjong_engine.round_event import DrawSource, TileDrawnEvent
from lisjong_engine.round_evidence import DrawEvidence
from lisjong_engine.round_evidence_completion import RoundEvidenceCompletion
from lisjong_engine.round_phase import RoundPhase
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.tile import STANDARD_TILES, Tile
from lisjong_engine.transaction_observation import (
    RoundCheckpoint,
    SeatCheckpoint,
    TransactionObservation,
    TransactionStepKind,
)

from lisjong_arena._artifact_io import ArtifactValidationError

from .completed_match_facts import seat_index

ROW_SCHEMA = "arena-engine-live-draw-capture-row-v1"

TOTAL_TILE_COUNT = 136
DEAD_WALL_SIZE = 14

_SEATS = tuple(EngineSeat)
_SEAT_INDEX = {seat: seat_index(seat) for seat in _SEATS}
_MELD_TYPES = {
    Pon: "pon",
    Chi: "chi",
    Daiminkan: "daiminkan",
    Kakan: "kakan",
    Ankan: "ankan",
}
_WINDS = ("east", "south", "west", "north")
_RIICHI_STATUSES = ("none", "riichi", "double_riichi")
_FURITEN_REASONS = ("temporary", "riichi")
_RIICHI_OUTCOMES = ("established", "failed_by_ron")
_DRAW_SOURCES = ("live_wall", "rinshan")


class LiveDrawCaptureError(ArtifactValidationError):
    """通常ツモcaptureの観測列・行・artifactが契約を満たさない場合。"""


# ---------------------------------------------------------------------------
# engine値 → JSON-native値（serializeだけ。意味の再計算はしない）
# ---------------------------------------------------------------------------


def _tile(tile: object) -> int:
    """物理tileを136枚ID（0..135）へ。標準牌集合（赤五を含む）との一致を要求する。"""
    if not isinstance(tile, Tile):
        raise LiveDrawCaptureError("expected an engine Tile")
    if STANDARD_TILES[tile.id] != tile:
        raise LiveDrawCaptureError("tile is not a standard 136-tile set member")
    return tile.id


def _seat(seat: object) -> int:
    if not isinstance(seat, EngineSeat):
        raise LiveDrawCaptureError("expected an engine Seat")
    return _SEAT_INDEX[seat]


def _meld(meld: object) -> dict[str, object]:
    meld_type = _MELD_TYPES.get(type(meld))
    if meld_type is None:
        raise LiveDrawCaptureError(f"unsupported meld type: {type(meld).__name__}")
    if isinstance(meld, Ankan):
        called_tile = None
        source_seat = None
    else:
        called_tile = _tile(meld.called_tile)
        source_seat = _seat(meld.source_seat)
    return {
        "called_tile": called_tile,
        "meld_type": meld_type,
        "source_seat": source_seat,
        "tiles": [_tile(tile) for tile in meld.tiles],
    }


def _seat_checkpoint(seat: SeatCheckpoint) -> dict[str, object]:
    return {
        "discards": [
            {
                "called_by": None
                if discard.called_by is None
                else _seat(discard.called_by),
                "is_tsumogiri": discard.is_tsumogiri,
                "tile": _tile(discard.tile),
            }
            for discard in seat.discards
        ],
        "drawn_tile": None if seat.drawn_tile is None else _tile(seat.drawn_tile),
        "drawn_tile_source": None
        if seat.drawn_tile_source is None
        else seat.drawn_tile_source.value,
        "hand_tiles": [_tile(tile) for tile in seat.hand_tiles],
        "is_ippatsu": seat.is_ippatsu,
        "melds": [_meld(meld) for meld in seat.melds],
        "missed_ron_furiten": None
        if seat.missed_ron_furiten is None
        else seat.missed_ron_furiten.value,
        "riichi_status": seat.riichi_status.value,
        "seat": _seat(seat.seat),
        "seat_wind": seat.seat_wind.value,
    }


def serialize_checkpoint(checkpoint: RoundCheckpoint) -> dict[str, object]:
    """`RoundCheckpoint`をJSON-native値へ。pending状態はツモ直前に空であることを別に検査する。"""
    if not isinstance(checkpoint, RoundCheckpoint):
        raise LiveDrawCaptureError("expected an engine RoundCheckpoint")
    return {
        "dealer_seat": _seat(checkpoint.dealer_seat),
        "live_tiles_remaining": checkpoint.live_tiles_remaining,
        "prevailing_wind": checkpoint.prevailing_wind.value,
        "revealed_dora_indicators": [
            _tile(tile) for tile in checkpoint.revealed_dora_indicators
        ],
        "riichi_finalizations": [
            {
                "contribution_points": None
                if finalization.contribution is None
                else finalization.contribution.points,
                "outcome": finalization.outcome.value,
                "seat": _seat(finalization.declaration.seat),
            }
            for finalization in checkpoint.riichi_finalizations
        ],
        "rinshan_tiles_remaining": checkpoint.rinshan_tiles_remaining,
        "seats": [_seat_checkpoint(seat) for seat in checkpoint.seats],
    }


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 行の検査（作成時と読込時で同じ関数を使う）
# ---------------------------------------------------------------------------

_ROW_FIELDS = {"identity", "pre_draw", "realized", "schema"}
_IDENTITY_FIELDS = {
    "draw_index",
    "drawer_seat",
    "placement",
    "revision",
    "round_ordinal",
    "seed",
}
_PRE_DRAW_FIELDS = {"checkpoint", "current_seat", "phase", "revision"}
_REALIZED_FIELDS = {"post_draw_checkpoint_sha256", "source", "tile"}
_CHECKPOINT_FIELDS = {
    "dealer_seat",
    "live_tiles_remaining",
    "prevailing_wind",
    "revealed_dora_indicators",
    "riichi_finalizations",
    "rinshan_tiles_remaining",
    "seats",
}
_SEAT_FIELDS = {
    "discards",
    "drawn_tile",
    "drawn_tile_source",
    "hand_tiles",
    "is_ippatsu",
    "melds",
    "missed_ron_furiten",
    "riichi_status",
    "seat",
    "seat_wind",
}
_DISCARD_FIELDS = {"called_by", "is_tsumogiri", "tile"}
_MELD_FIELDS = {"called_tile", "meld_type", "source_seat", "tiles"}
_FINALIZATION_FIELDS = {"contribution_points", "outcome", "seat"}


def _object(value: object, fields: set[str], context: str) -> dict[str, object]:
    if type(value) is not dict:
        raise LiveDrawCaptureError(f"{context} must be an object")
    if set(value) != fields:
        raise LiveDrawCaptureError(f"{context} fields are invalid")
    return value


def _list(value: object, context: str) -> list[object]:
    if type(value) is not list:
        raise LiveDrawCaptureError(f"{context} must be an array")
    return value


def _int(value: object, context: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise LiveDrawCaptureError(f"{context} must be an integer >= {minimum}")
    return value


def _bool(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise LiveDrawCaptureError(f"{context} must be a boolean")
    return value


def _seat_value(value: object, context: str) -> int:
    if type(value) is not int or not 0 <= value <= 3:
        raise LiveDrawCaptureError(f"{context} must be a seat index 0..3")
    return value


def _optional_seat(value: object, context: str) -> int | None:
    return None if value is None else _seat_value(value, context)


def _tile_value(value: object, context: str) -> int:
    if type(value) is not int or not 0 <= value < TOTAL_TILE_COUNT:
        raise LiveDrawCaptureError(f"{context} must be a tile id 0..135")
    return value


def _optional_tile(value: object, context: str) -> int | None:
    return None if value is None else _tile_value(value, context)


def _enum(value: object, allowed: Sequence[str], context: str) -> str:
    if type(value) is not str or value not in allowed:
        raise LiveDrawCaptureError(f"{context} is not a known value")
    return value


def _tiles(value: object, context: str) -> list[int]:
    return [
        _tile_value(tile, f"{context}[{index}]")
        for index, tile in enumerate(_list(value, context))
    ]


def _validate_seat_shape(raw: object, index: int) -> dict[str, object]:
    context = f"pre_draw.checkpoint.seats[{index}]"
    seat = _object(raw, _SEAT_FIELDS, context)
    if _seat_value(seat["seat"], f"{context}.seat") != index:
        raise LiveDrawCaptureError("checkpoint seats must be in seat order")
    _enum(seat["seat_wind"], _WINDS, f"{context}.seat_wind")
    _tiles(seat["hand_tiles"], f"{context}.hand_tiles")
    _optional_tile(seat["drawn_tile"], f"{context}.drawn_tile")
    if seat["drawn_tile_source"] is not None:
        _enum(seat["drawn_tile_source"], _DRAW_SOURCES, f"{context}.drawn_tile_source")
    for position, discard in enumerate(_list(seat["discards"], f"{context}.discards")):
        item = _object(discard, _DISCARD_FIELDS, f"{context}.discards[{position}]")
        _tile_value(item["tile"], f"{context}.discards[{position}].tile")
        _bool(item["is_tsumogiri"], f"{context}.discards[{position}].is_tsumogiri")
        _optional_seat(item["called_by"], f"{context}.discards[{position}].called_by")
    for position, meld in enumerate(_list(seat["melds"], f"{context}.melds")):
        item = _object(meld, _MELD_FIELDS, f"{context}.melds[{position}]")
        meld_type = _enum(
            item["meld_type"], tuple(_MELD_TYPES.values()), f"{context}.melds.type"
        )
        tiles = _tiles(item["tiles"], f"{context}.melds[{position}].tiles")
        expected_size = 3 if meld_type in ("pon", "chi") else 4
        if len(tiles) != expected_size:
            raise LiveDrawCaptureError(f"{context}.melds[{position}] size is invalid")
        if meld_type == "ankan":
            if item["called_tile"] is not None or item["source_seat"] is not None:
                raise LiveDrawCaptureError("ankan must not carry a called tile")
        else:
            called = _tile_value(item["called_tile"], f"{context}.melds.called_tile")
            if called not in tiles:
                raise LiveDrawCaptureError("meld called_tile must be one of its tiles")
            if _seat_value(item["source_seat"], f"{context}.melds.source") == index:
                raise LiveDrawCaptureError("a meld cannot be called from its owner")
    _enum(seat["riichi_status"], _RIICHI_STATUSES, f"{context}.riichi_status")
    _bool(seat["is_ippatsu"], f"{context}.is_ippatsu")
    if seat["missed_ron_furiten"] is not None:
        _enum(
            seat["missed_ron_furiten"],
            _FURITEN_REASONS,
            f"{context}.missed_ron_furiten",
        )
    return seat


def visible_tile_ids(checkpoint: Mapping[str, object]) -> list[int]:
    """盤面で見えている物理tile ID（全席の手牌・副露・鳴かれていない河・公開表示牌）。

    鳴かれた河の牌は副露の`called_tile`として数え、河の側から除く（二重計上しない）。
    """
    visible: list[int] = list(checkpoint["revealed_dora_indicators"])
    for seat in checkpoint["seats"]:
        visible.extend(seat["hand_tiles"])
        for meld in seat["melds"]:
            visible.extend(meld["tiles"])
        visible.extend(
            discard["tile"]
            for discard in seat["discards"]
            if discard["called_by"] is None
        )
    return visible


def _validate_board(checkpoint: dict[str, object], realized_tile: int) -> None:
    """物理tileの一意性、鳴かれた河と副露の対応、枚数の不変条件を検査する。"""
    seats = checkpoint["seats"]
    visible = visible_tile_ids(checkpoint)
    duplicates = [tile for tile, count in Counter(visible).items() if count > 1]
    if duplicates:
        raise LiveDrawCaptureError(
            "a physical tile appears more than once on the board"
        )

    called_from: Counter[tuple[int, int, int]] = Counter()
    for owner, seat in enumerate(seats):
        for meld in seat["melds"]:
            if meld["meld_type"] in ("pon", "chi", "daiminkan", "kakan"):
                called_from[(meld["called_tile"], meld["source_seat"], owner)] += 1
    called_discards: Counter[tuple[int, int, int]] = Counter()
    for discarder, seat in enumerate(seats):
        for discard in seat["discards"]:
            if discard["called_by"] is not None:
                if discard["called_by"] == discarder:
                    raise LiveDrawCaptureError(
                        "a discard cannot be called by its owner"
                    )
                called_discards[(discard["tile"], discarder, discard["called_by"])] += 1
    if called_discards != called_from:
        raise LiveDrawCaptureError("called discards do not match the melds")

    if realized_tile in set(visible):
        raise LiveDrawCaptureError(
            "the realized tile was already visible before the draw"
        )
    indicators = len(checkpoint["revealed_dora_indicators"])
    unseen = TOTAL_TILE_COUNT - len(visible)
    expected = checkpoint["live_tiles_remaining"] + DEAD_WALL_SIZE - indicators
    if unseen != expected:
        raise LiveDrawCaptureError(
            "unseen tile count does not match the remaining wall "
            f"({unseen} != {expected})"
        )


def validate_row(row: object) -> dict[str, object]:
    """1行の形・ツモ直前条件・盤面の整合を検査する（作成時・読込時で共通）。"""
    row = _object(row, _ROW_FIELDS, "row")
    if row["schema"] != ROW_SCHEMA:
        raise LiveDrawCaptureError("row schema is unknown")

    identity = _object(row["identity"], _IDENTITY_FIELDS, "identity")
    if type(identity["seed"]) is not int:
        raise LiveDrawCaptureError("identity.seed must be an integer")
    _int(identity["placement"], "identity.placement")
    _int(identity["round_ordinal"], "identity.round_ordinal", minimum=1)
    draw_revision = _int(identity["revision"], "identity.revision", minimum=2)
    _int(identity["draw_index"], "identity.draw_index")
    drawer = _seat_value(identity["drawer_seat"], "identity.drawer_seat")

    pre_draw = _object(row["pre_draw"], _PRE_DRAW_FIELDS, "pre_draw")
    if pre_draw["phase"] != RoundPhase.AWAITING_DRAW.value:
        raise LiveDrawCaptureError("pre_draw phase must be awaiting_draw")
    if _seat_value(pre_draw["current_seat"], "pre_draw.current_seat") != drawer:
        raise LiveDrawCaptureError("pre_draw current_seat must be the drawer")
    if _int(pre_draw["revision"], "pre_draw.revision", minimum=1) != draw_revision - 1:
        raise LiveDrawCaptureError(
            "pre_draw revision must immediately precede the draw"
        )

    checkpoint = _object(
        pre_draw["checkpoint"], _CHECKPOINT_FIELDS, "pre_draw.checkpoint"
    )
    _seat_value(checkpoint["dealer_seat"], "pre_draw.checkpoint.dealer_seat")
    _enum(checkpoint["prevailing_wind"], _WINDS, "pre_draw.checkpoint.prevailing_wind")
    live = _int(
        checkpoint["live_tiles_remaining"], "pre_draw.checkpoint.live", minimum=1
    )
    if live > TOTAL_TILE_COUNT - DEAD_WALL_SIZE:
        raise LiveDrawCaptureError("live_tiles_remaining is out of range")
    rinshan = _int(checkpoint["rinshan_tiles_remaining"], "pre_draw.checkpoint.rinshan")
    if rinshan > 4:
        raise LiveDrawCaptureError("rinshan_tiles_remaining is out of range")
    indicators = _tiles(
        checkpoint["revealed_dora_indicators"], "pre_draw.checkpoint.indicators"
    )
    if not 1 <= len(indicators) <= 5:
        raise LiveDrawCaptureError("revealed dora indicator count is out of range")
    for position, raw in enumerate(
        _list(checkpoint["riichi_finalizations"], "pre_draw.checkpoint.finalizations")
    ):
        item = _object(raw, _FINALIZATION_FIELDS, f"finalizations[{position}]")
        _seat_value(item["seat"], f"finalizations[{position}].seat")
        _enum(item["outcome"], _RIICHI_OUTCOMES, f"finalizations[{position}].outcome")
        if item["contribution_points"] is not None:
            _int(item["contribution_points"], "finalizations.contribution", minimum=1)
    seats = _list(checkpoint["seats"], "pre_draw.checkpoint.seats")
    if len(seats) != 4:
        raise LiveDrawCaptureError("checkpoint must contain four seats")
    for index, seat in enumerate(seats):
        _validate_seat_shape(seat, index)
        if seat["drawn_tile"] is not None:
            raise LiveDrawCaptureError("no seat may hold a drawn tile before a draw")

    realized = _object(row["realized"], _REALIZED_FIELDS, "realized")
    if realized["source"] != DrawSource.LIVE_WALL.value:
        raise LiveDrawCaptureError("only live-wall draws are captured")
    realized_tile = _tile_value(realized["tile"], "realized.tile")
    digest = realized["post_draw_checkpoint_sha256"]
    if type(digest) is not str or len(digest) != 64:
        raise LiveDrawCaptureError("realized.post_draw_checkpoint_sha256 is invalid")
    if any(character not in "0123456789abcdef" for character in digest):
        raise LiveDrawCaptureError("realized.post_draw_checkpoint_sha256 is invalid")

    _validate_board(checkpoint, realized_tile)
    return row


# ---------------------------------------------------------------------------
# 観測列からの行の作成
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedRound:
    """1局分のcapture結果。`live_draw_count`は行数、`rinshan_draw_count`は件数だけ。"""

    round_ordinal: int
    live_draw_count: int
    rinshan_draw_count: int


@dataclass(frozen=True)
class CapturedHanchan:
    """1半荘分のcapture結果（行は観測順）。"""

    seed: int
    placement: int
    rows: tuple[dict[str, object], ...]
    rounds: tuple[CapturedRound, ...]


@dataclass
class _RoundState:
    round_ordinal: int
    last: TransactionObservation
    live_draws: list[tuple[int, int]]  # (drawer seat, tile id)
    rinshan_draws: int = 0


class LiveDrawCapture:
    """1半荘分の一方向observer。`run_hanchan`の2つのcallbackへ渡す。

    `on_transaction_observation`（privileged）から行を作り、
    `on_round_evidence_complete`（player-safe、別経路）を完全性の照合に使う。
    受け取った値はこのobject内のbufferだけへ保持し、どこにも返さない
    （`finish()`の結果だけを返す）。
    """

    def __init__(self, *, seed: int, placement: int) -> None:
        if type(seed) is not int:
            raise TypeError("seed must be an int")
        if type(placement) is not int or placement < 0:
            raise TypeError("placement must be a non-negative int")
        self._seed = seed
        self._placement = placement
        self._rows: list[dict[str, object]] = []
        self._rounds: list[CapturedRound] = []
        self._current: _RoundState | None = None
        self._closed_round_draws: list[list[tuple[int, int]]] = []
        self._evidence: list[RoundEvidenceCompletion] = []
        self._finished = False

    # -- transaction channel -------------------------------------------------

    def on_transaction_observation(self, observation: TransactionObservation) -> None:
        self._require_open()
        if not isinstance(observation, TransactionObservation):
            raise LiveDrawCaptureError("expected a TransactionObservation")
        first = observation.steps[0]
        if first.kind is TransactionStepKind.ROUND_STARTED:
            self._start_round(observation)
            return
        current = self._current
        if current is None:
            raise LiveDrawCaptureError(
                "observation stream must begin each round with ROUND_STARTED"
            )
        if observation.round_ordinal != current.round_ordinal:
            raise LiveDrawCaptureError("observation crossed a round boundary")
        if observation.revision != current.last.revision + 1:
            raise LiveDrawCaptureError("observation revisions must be contiguous")
        if any(
            step.kind is TransactionStepKind.ROUND_STARTED for step in observation.steps
        ):
            raise LiveDrawCaptureError("ROUND_STARTED must be a transaction of its own")

        draw_steps = [
            step for step in observation.steps if step.kind is TransactionStepKind.DRAW
        ]
        if draw_steps:
            if len(observation.steps) != 1:
                raise LiveDrawCaptureError("a draw must be a transaction of its own")
            self._record_draw(current, observation)
        else:
            for step in observation.steps:
                if any(isinstance(event, TileDrawnEvent) for event in step.events):
                    raise LiveDrawCaptureError(
                        "a draw event appeared outside a DRAW step"
                    )
        current.last = observation
        if any(
            step.kind is TransactionStepKind.ROUND_ENDED for step in observation.steps
        ):
            self._close_round(current)

    def _start_round(self, observation: TransactionObservation) -> None:
        if len(observation.steps) != 1:
            raise LiveDrawCaptureError("ROUND_STARTED must be a transaction of its own")
        if self._current is not None:
            raise LiveDrawCaptureError("a round started before the previous one ended")
        expected = len(self._rounds) + 1
        if observation.round_ordinal != expected:
            raise LiveDrawCaptureError(
                "round ordinals must start at 1 and be contiguous "
                "(a resumed round is not supported)"
            )
        self._current = _RoundState(
            round_ordinal=observation.round_ordinal,
            last=observation,
            live_draws=[],
        )

    def _record_draw(
        self, current: _RoundState, observation: TransactionObservation
    ) -> None:
        step = observation.steps[0]
        if len(step.events) != 1 or not isinstance(step.events[0], TileDrawnEvent):
            raise LiveDrawCaptureError("a DRAW step must carry exactly one draw event")
        event = step.events[0]
        if event.source is DrawSource.RINSHAN:
            current.rinshan_draws += 1
            return
        if event.source is not DrawSource.LIVE_WALL:
            raise LiveDrawCaptureError("unknown draw source")

        previous = current.last
        pre_checkpoint = previous.steps[-1].checkpoint
        if previous.phase is not RoundPhase.AWAITING_DRAW:
            raise LiveDrawCaptureError(
                "the observation before a draw must await a draw"
            )
        if previous.current_seat is not event.seat:
            raise LiveDrawCaptureError("the drawer must be the current seat")
        if (
            pre_checkpoint.pending_riichi_declaration is not None
            or pre_checkpoint.pending_kakan is not None
            or pre_checkpoint.pending_ankan is not None
        ):
            raise LiveDrawCaptureError(
                "pending declarations must be empty before a draw"
            )

        pre = serialize_checkpoint(pre_checkpoint)
        post = serialize_checkpoint(step.checkpoint)
        tile = _tile(event.tile)
        drawer = _seat(event.seat)
        _require_post_matches(pre, post, drawer=drawer, tile=tile)

        row = {
            "identity": {
                "draw_index": len(current.live_draws),
                "drawer_seat": drawer,
                "placement": self._placement,
                "revision": observation.revision,
                "round_ordinal": observation.round_ordinal,
                "seed": self._seed,
            },
            "pre_draw": {
                "checkpoint": pre,
                "current_seat": _seat(previous.current_seat),
                "phase": previous.phase.value,
                "revision": previous.revision,
            },
            "realized": {
                "post_draw_checkpoint_sha256": _digest(post),
                "source": DrawSource.LIVE_WALL.value,
                "tile": tile,
            },
            "schema": ROW_SCHEMA,
        }
        validate_row(row)
        self._rows.append(row)
        current.live_draws.append((drawer, tile))

    def _close_round(self, current: _RoundState) -> None:
        self._rounds.append(
            CapturedRound(
                round_ordinal=current.round_ordinal,
                live_draw_count=len(current.live_draws),
                rinshan_draw_count=current.rinshan_draws,
            )
        )
        self._closed_round_draws.append(current.live_draws)
        self._current = None

    # -- independent evidence channel ----------------------------------------

    def on_round_evidence_complete(self, completion: RoundEvidenceCompletion) -> None:
        self._require_open()
        if not isinstance(completion, RoundEvidenceCompletion):
            raise LiveDrawCaptureError("expected a RoundEvidenceCompletion")
        self._evidence.append(completion)

    # -- completion ------------------------------------------------------------

    def finish(self, completed: CompletedMatch) -> CapturedHanchan:
        """半荘の完了後に、別経路のevidenceと照合して結果を返す。"""
        self._require_open()
        self._finished = True
        if not isinstance(completed, CompletedMatch):
            raise LiveDrawCaptureError("expected a CompletedMatch")
        if self._current is not None:
            raise LiveDrawCaptureError("the last round did not end")
        round_count = len(completed.history)
        if len(self._rounds) != round_count:
            raise LiveDrawCaptureError("captured round count differs from the match")
        if len(self._evidence) != round_count:
            raise LiveDrawCaptureError(
                "round evidence count differs from the match "
                "(on_round_evidence_complete must be wired)"
            )
        for index, (captured, evidence) in enumerate(
            zip(self._rounds, self._evidence, strict=True)
        ):
            live, rinshan = _evidence_draws(evidence)
            draws = self._closed_round_draws[index]
            if [seat for seat, _ in draws] != [seat for seat, _ in live]:
                raise LiveDrawCaptureError(
                    f"round {captured.round_ordinal}: live-wall draws differ from "
                    "the independent round evidence"
                )
            for (seat, tile), (_, public) in zip(draws, live, strict=True):
                if public is not None and public != _public_key(tile):
                    raise LiveDrawCaptureError(
                        f"round {captured.round_ordinal}: drawer evidence tile "
                        "differs from the captured tile"
                    )
            if captured.rinshan_draw_count != rinshan:
                raise LiveDrawCaptureError(
                    f"round {captured.round_ordinal}: rinshan draw count differs "
                    "from the independent round evidence"
                )
        return CapturedHanchan(
            seed=self._seed,
            placement=self._placement,
            rows=tuple(self._rows),
            rounds=tuple(self._rounds),
        )

    def _require_open(self) -> None:
        if self._finished:
            raise LiveDrawCaptureError("capture is already finished")


def _public_key(tile_id: int) -> tuple[int, bool]:
    tile = STANDARD_TILES[tile_id]
    return (tile.tile_type.id, tile.is_red)


def _evidence_draws(
    completion: RoundEvidenceCompletion,
) -> tuple[list[tuple[int, tuple[int, bool] | None]], int]:
    """4 viewerの`DrawEvidence`から、通常ツモの(席, 自席viewerの牌)列と嶺上件数を作る。

    4 viewerで席・取得元の列が一致することを要求する。牌はdrawer自身のviewerの
    evidenceにだけ含まれる（player-safeな契約）。
    """
    sequences = []
    own_tiles: dict[int, list[tuple[int, bool]]] = {}
    for projection in completion.projections:
        viewer = _seat(projection.viewer_seat)
        draws = [
            evidence
            for evidence in projection.evidence
            if isinstance(evidence, DrawEvidence)
        ]
        sequences.append([(_seat(item.seat), item.source) for item in draws])
        own_tiles[viewer] = [
            (item.tile.tile_type.id, item.tile.is_red)
            for item in draws
            if item.source is DrawSource.LIVE_WALL
            and _seat(item.seat) == viewer
            and item.tile is not None
        ]
    if len(sequences) != 4 or any(sequence != sequences[0] for sequence in sequences):
        raise LiveDrawCaptureError("viewers disagree on the round draw evidence")
    live: list[tuple[int, tuple[int, bool] | None]] = []
    cursor = {seat: 0 for seat in range(4)}
    rinshan = 0
    for seat, source in sequences[0]:
        if source is DrawSource.RINSHAN:
            rinshan += 1
            continue
        tiles = own_tiles.get(seat, [])
        position = cursor[seat]
        live.append((seat, tiles[position] if position < len(tiles) else None))
        cursor[seat] = position + 1
    for seat, tiles in own_tiles.items():
        if cursor[seat] != len(tiles):
            raise LiveDrawCaptureError("drawer evidence tile count is inconsistent")
    return live, rinshan


def _require_post_matches(
    pre: dict[str, object], post: dict[str, object], *, drawer: int, tile: int
) -> None:
    """事後盤面 = ツモ直前盤面 + 実現牌（drawerの手牌だけ）であることを検査する。

    `hand_tiles`はツモ牌を含むので、drawerの手牌は多重集合として
    pre + {tile}と一致し、ほかの席・河・副露・表示牌は変わらない。同巡見逃し
    フリテンの解除（drawerの`missed_ron_furiten`がTEMPORARYからNoneへ）だけは許す。
    """
    if post["live_tiles_remaining"] != pre["live_tiles_remaining"] - 1:
        raise LiveDrawCaptureError("a live-wall draw must consume one live tile")
    for key in (
        "dealer_seat",
        "prevailing_wind",
        "revealed_dora_indicators",
        "riichi_finalizations",
        "rinshan_tiles_remaining",
    ):
        if post[key] != pre[key]:
            raise LiveDrawCaptureError(f"a draw must not change {key}")
    for index, (before, after) in enumerate(
        zip(pre["seats"], post["seats"], strict=True)
    ):
        if index != drawer:
            if before != after:
                raise LiveDrawCaptureError("a draw must not change another seat")
            continue
        if Counter(after["hand_tiles"]) != Counter([*before["hand_tiles"], tile]):
            raise LiveDrawCaptureError(
                "drawer hand must be the pre-draw hand plus the tile"
            )
        if after["drawn_tile"] != tile or after["drawn_tile_source"] != "live_wall":
            raise LiveDrawCaptureError("drawer must hold the realized live-wall tile")
        for key in ("discards", "melds", "riichi_status", "is_ippatsu", "seat_wind"):
            if after[key] != before[key]:
                raise LiveDrawCaptureError(f"a draw must not change drawer {key}")
        if after["missed_ron_furiten"] != before["missed_ron_furiten"] and not (
            before["missed_ron_furiten"] == "temporary"
            and after["missed_ron_furiten"] is None
        ):
            raise LiveDrawCaptureError("a draw may only clear temporary furiten")


def capture_live_draw_hanchan(
    policies: object,
    *,
    seed: int,
    placement: int,
    rules=None,
    on_round_evidence_complete: Callable[[object], None] | None = None,
    on_selector_call: Callable[[EngineSeat], None] | None = None,
    on_delivery: Callable[[tuple], None] | None = None,
) -> tuple[CompletedMatch, CapturedHanchan]:
    """`run_policy_hanchan`を通常ツモcapture付きで1回実行する（opt-in）。

    呼び出し元の`on_round_evidence_complete`は、captureの照合用に受け取った後で
    そのまま渡す。captureはPolicy・selector・deliveryの経路へ何も渡さない。
    """
    from .hanchan import run_policy_hanchan

    capture = LiveDrawCapture(seed=seed, placement=placement)

    def evidence(completion):
        capture.on_round_evidence_complete(completion)
        if on_round_evidence_complete is not None:
            on_round_evidence_complete(completion)

    completed = run_policy_hanchan(
        policies,
        seed=seed,
        rules=rules,
        on_round_evidence_complete=evidence,
        on_selector_call=on_selector_call,
        on_delivery=on_delivery,
        on_transaction_observation=capture.on_transaction_observation,
    )
    return completed, capture.finish(completed)


__all__ = [
    "CapturedHanchan",
    "CapturedRound",
    "LiveDrawCapture",
    "LiveDrawCaptureError",
    "ROW_SCHEMA",
    "capture_live_draw_hanchan",
    "serialize_checkpoint",
    "validate_row",
    "visible_tile_ids",
]
