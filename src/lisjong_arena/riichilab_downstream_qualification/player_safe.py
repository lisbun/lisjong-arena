"""decision時点でfreezeする、Issue #203のplayer-safe reconstruction state。

このmoduleはraw MJAI eventを直接読まない。`mjai_events.project_visible_event()`
が返す`VisibleEvent`だけを前から順に適用し、decision point到達時点の値を
frozen snapshotとしてfreezeする。completed gameやfinal stateから過去decisionを
逆算する経路をprimary implementationにしない。

```text
VisibleEvent prefix
    -> PlayerSafeRoundState.apply()      # forward only
    -> snapshot()                        # decision到達時にfreeze
    -> PlayerSafeDecisionSnapshot        # immutable value
```

`PlayerSafeDecisionSnapshot`は次を持たない。持たせてはならない。

```text
他家のconcealed hand
future wall / future draw / future action
terminal result / future score change
server-only hidden truth
```

hidden truthはこのmoduleを通らず、`hidden_truth.py`が別channelで保持する。
1 seatぶんの`PlayerSafeRoundState`が知る「自分の手牌」は、そのseatにとっては
player-safe情報であり、同時に他seatから見たhidden truthである。hidden channelは
この非対称性を利用して、全seat分のstateからtruthをjoinする（`hidden_truth.py`）。
"""

from dataclasses import dataclass, replace
from enum import Enum

from lisjong.policy_contract.discard import Discard
from lisjong.policy_contract.meld import MeldKind, PublicMeld
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile, tile_sort_key
from lisjong.policy_contract.wind import Wind

from lisjong_arena.riichilab_downstream_qualification.mjai_events import (
    MjaiReplayError,
    UnsupportedReason,
    VisibleEvent,
    VisibleEventKind,
)

_SEAT_COUNT = 4
_MELD_STRUCTURAL_EQUIVALENT_COUNT = 3


class DecisionKind(Enum):
    """exactに存在証明できるdecision pointの種別。

    server logはpassやlegal action setを明示しないため、ここへ推測で
    decision kindを追加しない。
    """

    TURN = "turn"
    """自身のtsumo直後のpre-action decision。"""

    POST_CALL_DISCARD = "post_call_discard"
    """chi / pon直後の打牌decision。"""

    RIICHI_DISCARD = "riichi_discard"
    """reach宣言直後の宣言牌decision。"""

    CALL_RESPONSE = "call_response"
    """他家のdahai / kakanに対して、実際にcall / ronが観測されたdecision。"""


@dataclass(frozen=True, slots=True)
class PlayerSafePublicView:
    """局内の公開state。全seatのplayer-safe stateが同じ値を導出できる。

    4 seat分の`PlayerSafeRoundState`が同一のdecision pointで異なる公開stateを
    導出した場合、それはreplay inconsistencyであり計数する。
    """

    melds: tuple[tuple[PublicMeld, ...], ...]
    discards: tuple[tuple[Discard, ...], ...]
    riichi_states: tuple[RiichiState, ...]
    dora_indicators: tuple[Tile, ...]
    accepted_riichi_declarations: int
    """この局で成立した立直の件数。局内で観測したreach_accepted数である。"""

    round_start_riichi_sticks: int | None
    """`start_kyoku`時点の供託棒数。局中の増減を含まない。"""

    draw_counts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PlayerSafeDecisionSnapshot:
    """1 decision pointでfreezeされたplayer-safe value。

    fieldの一覧そのものがinformation-flow boundaryの宣言である。opponent
    concealed hand、wall、未来event、terminal resultに対応するfieldは存在
    しない。

    `round_start_seat_scores`はcurrent scoreではない。局中のscore移動
    （立直供託の支払い、和了・流局の点数移動）は麻雀rules semanticsに属し、
    Arenaはそれを再実装しない。したがってscoreは`start_kyoku`が公開した
    局開始時点の値としてだけ保持し、consumerがcurrent scoreと誤認しないよう
    field名とdocstringで固定する。局中に観測できる公開事実は
    `accepted_riichi_declarations`として別に持つ。
    """

    viewer_seat: Seat
    decision_kind: DecisionKind
    prevailing_wind: Wind
    hand_number: int
    honba: int
    dealer_seat: Seat
    round_start_seat_scores: tuple[int, ...] | None
    own_concealed_tiles: tuple[Tile, ...]
    own_drawn_tile: Tile | None
    public: PlayerSafePublicView
    visible_event_index: int

    @property
    def own_melds(self) -> tuple[PublicMeld, ...]:
        return self.public.melds[int(self.viewer_seat)]

    @property
    def is_open_hand(self) -> bool:
        """ankan以外のmeldを持つかどうか（門前でないか）。"""
        return any(meld.kind is not MeldKind.ANKAN for meld in self.own_melds)

    @property
    def is_riichi_declared(self) -> bool:
        return self.public.riichi_states[int(self.viewer_seat)] is not RiichiState.NONE


def resolve_kakan_source_pon(
    melds: tuple[PublicMeld, ...], added_tile: Tile, consumed: tuple[Tile, ...]
) -> tuple[int, PublicMeld]:
    """kakanの元Ponを、added tileだけでなくconsumed 3枚と併せて一意に解決する。

    added tileの牌種だけで元Ponを推測しない。候補がちょうど1件でない場合、
    またはserver recordの`consumed`が候補Ponのtile多重集合と一致しない場合は
    fail closedする（赤5 identityを含めて一致を要求する）。
    """
    candidates = [
        (index, meld)
        for index, meld in enumerate(melds)
        if meld.kind is MeldKind.PON
        and meld.tiles[0].tile_type == added_tile.tile_type
        and meld.tiles == consumed
    ]
    if len(candidates) != 1:
        raise MjaiReplayError(
            UnsupportedReason.AMBIGUOUS_KAKAN_SOURCE_PON,
            "kakan does not resolve to exactly one existing pon meld",
        )
    return candidates[0]


class PlayerSafeRoundState:
    """1 seat視点の、局内player-safe state。

    `apply()`は`VisibleEvent`だけを受け取る。raw eventを受け取るoverloadを
    持たせない（型境界そのものをinformation-flow boundaryにする）。
    """

    __slots__ = (
        "_viewer_seat",
        "_started",
        "_prevailing_wind",
        "_hand_number",
        "_honba",
        "_dealer_seat",
        "_round_start_seat_scores",
        "_round_start_riichi_sticks",
        "_accepted_riichi_declarations",
        "_own_concealed",
        "_own_drawn",
        "_melds",
        "_discards",
        "_riichi_states",
        "_dora_indicators",
        "_draw_counts",
        "_next_discard_order",
        "_applied_count",
        "_terminal_seen",
    )

    def __init__(self, viewer_seat: Seat) -> None:
        if not isinstance(viewer_seat, Seat):
            raise TypeError("viewer_seat must be a Seat")
        self._viewer_seat = viewer_seat
        self._started = False
        self._prevailing_wind: Wind | None = None
        self._hand_number = 0
        self._honba = 0
        self._dealer_seat: Seat | None = None
        self._round_start_seat_scores: tuple[int, ...] | None = None
        self._round_start_riichi_sticks: int | None = None
        self._accepted_riichi_declarations = 0
        self._own_concealed: tuple[Tile, ...] = ()
        self._own_drawn: Tile | None = None
        self._melds: list[tuple[PublicMeld, ...]] = [() for _ in range(_SEAT_COUNT)]
        self._discards: list[tuple[Discard, ...]] = [() for _ in range(_SEAT_COUNT)]
        self._riichi_states = [RiichiState.NONE] * _SEAT_COUNT
        self._dora_indicators: tuple[Tile, ...] = ()
        self._draw_counts = [0] * _SEAT_COUNT
        self._next_discard_order = 0
        self._applied_count = 0
        self._terminal_seen = False

    @property
    def viewer_seat(self) -> Seat:
        return self._viewer_seat

    @property
    def started(self) -> bool:
        return self._started

    @property
    def terminal_seen(self) -> bool:
        return self._terminal_seen

    @property
    def own_concealed_tiles(self) -> tuple[Tile, ...]:
        return self._own_concealed

    @property
    def own_melds(self) -> tuple[PublicMeld, ...]:
        return self._melds[int(self._viewer_seat)]

    @property
    def applied_count(self) -> int:
        return self._applied_count

    def structural_hand_size(self) -> int:
        """`len(concealed) + 3 * len(melds)`。open-meld調整後のhand size。"""
        return len(self._own_concealed) + _MELD_STRUCTURAL_EQUIVALENT_COUNT * len(
            self.own_melds
        )

    def public_view(self) -> PlayerSafePublicView:
        return PlayerSafePublicView(
            melds=tuple(self._melds),
            discards=tuple(self._discards),
            riichi_states=tuple(self._riichi_states),
            dora_indicators=self._dora_indicators,
            accepted_riichi_declarations=self._accepted_riichi_declarations,
            round_start_riichi_sticks=self._round_start_riichi_sticks,
            draw_counts=tuple(self._draw_counts),
        )

    def snapshot(self, decision_kind: DecisionKind) -> PlayerSafeDecisionSnapshot:
        """現在までに適用済みのvisible prefixだけからsnapshotをfreezeする。"""
        if not isinstance(decision_kind, DecisionKind):
            raise TypeError("decision_kind must be a DecisionKind")
        if not self._started or self._prevailing_wind is None:
            raise MjaiReplayError(
                UnsupportedReason.EVENT_OUT_OF_ORDER,
                "cannot freeze a decision snapshot before start_kyoku",
            )
        assert self._dealer_seat is not None
        return PlayerSafeDecisionSnapshot(
            viewer_seat=self._viewer_seat,
            decision_kind=decision_kind,
            prevailing_wind=self._prevailing_wind,
            hand_number=self._hand_number,
            honba=self._honba,
            dealer_seat=self._dealer_seat,
            round_start_seat_scores=self._round_start_seat_scores,
            own_concealed_tiles=self._own_concealed,
            own_drawn_tile=self._own_drawn,
            public=self.public_view(),
            visible_event_index=self._applied_count,
        )

    def apply(self, event: VisibleEvent) -> None:
        """1件のvisible eventを前向きに適用する。"""
        if not isinstance(event, VisibleEvent):
            raise TypeError("event must be a VisibleEvent")
        kind = event.kind
        if kind is VisibleEventKind.ROUND_START:
            self._apply_round_start(event)
        elif kind is VisibleEventKind.IGNORED:
            pass
        elif kind is VisibleEventKind.ROUND_END:
            self._started = False
        elif not self._started:
            raise MjaiReplayError(
                UnsupportedReason.EVENT_OUT_OF_ORDER,
                f"{kind.value} occurs outside an active round",
            )
        elif kind is VisibleEventKind.SELF_DRAW:
            self._apply_self_draw(event)
        elif kind is VisibleEventKind.OPPONENT_DRAW:
            self._draw_counts[int(_require_actor(event))] += 1
        elif kind is VisibleEventKind.DISCARD:
            self._apply_discard(event)
        elif kind in {
            VisibleEventKind.CHI,
            VisibleEventKind.PON,
            VisibleEventKind.DAIMINKAN,
        }:
            self._apply_call(event)
        elif kind is VisibleEventKind.ANKAN:
            self._apply_ankan(event)
        elif kind is VisibleEventKind.KAKAN:
            self._apply_kakan(event)
        elif kind is VisibleEventKind.REACH_DECLARED:
            self._apply_reach(event)
        elif kind is VisibleEventKind.REACH_ACCEPTED:
            self._apply_reach_accepted(event)
        elif kind is VisibleEventKind.DORA_REVEALED:
            self._dora_indicators += (_require_tile(event),)
        elif kind is VisibleEventKind.ROUND_TERMINAL:
            self._terminal_seen = True
        else:  # pragma: no cover - VisibleEventKindの網羅漏れ検出用
            raise MjaiReplayError(
                UnsupportedReason.UNRECOGNIZED_EVENT_TYPE,
                f"unhandled visible event kind {kind.value}",
            )
        self._applied_count += 1

    def _apply_round_start(self, event: VisibleEvent) -> None:
        start = event.start
        if start is None:
            raise MjaiReplayError(
                UnsupportedReason.MISSING_REQUIRED_FIELD,
                "round start event carries no start payload",
            )
        self._started = True
        self._terminal_seen = False
        self._prevailing_wind = start.prevailing_wind
        self._hand_number = start.hand_number
        self._honba = start.honba
        self._dealer_seat = start.dealer_seat
        self._round_start_seat_scores = start.round_start_seat_scores
        self._round_start_riichi_sticks = start.round_start_riichi_sticks
        self._accepted_riichi_declarations = 0
        self._own_concealed = start.viewer_concealed_tiles
        self._own_drawn = None
        self._melds = [() for _ in range(_SEAT_COUNT)]
        self._discards = [() for _ in range(_SEAT_COUNT)]
        self._riichi_states = [RiichiState.NONE] * _SEAT_COUNT
        self._dora_indicators = (start.dora_indicator,)
        self._draw_counts = [0] * _SEAT_COUNT
        self._next_discard_order = 0

    def _apply_self_draw(self, event: VisibleEvent) -> None:
        actor = _require_actor(event)
        if actor != self._viewer_seat:
            raise MjaiReplayError(
                UnsupportedReason.EVENT_OUT_OF_ORDER,
                "self draw actor does not match the viewer seat",
            )
        tile = _require_tile(event)
        self._own_concealed = _insert_tile(self._own_concealed, tile)
        self._own_drawn = tile
        self._draw_counts[int(actor)] += 1

    def _apply_discard(self, event: VisibleEvent) -> None:
        actor = _require_actor(event)
        tile = _require_tile(event)
        if event.tsumogiri is None:
            raise MjaiReplayError(
                UnsupportedReason.MISSING_REQUIRED_FIELD, "discard has no tsumogiri"
            )
        if actor == self._viewer_seat:
            self._own_concealed = self._remove_own_tiles((tile,))
            self._own_drawn = None
        self._discards[int(actor)] += (
            Discard(
                tile=tile,
                tsumogiri=event.tsumogiri,
                order=self._next_discard_order,
                called_by=None,
            ),
        )
        self._next_discard_order += 1

    def _apply_call(self, event: VisibleEvent) -> None:
        actor = _require_actor(event)
        target = event.target
        called_tile = _require_tile(event)
        if target is None or target == actor:
            raise MjaiReplayError(
                UnsupportedReason.AMBIGUOUS_CALL_TARGET,
                "call target is missing or equals the actor",
            )
        self._consume_called_discard(target, actor, called_tile)
        if actor == self._viewer_seat:
            self._own_concealed = self._remove_own_tiles(event.tiles)
            self._own_drawn = None
        kind = {
            VisibleEventKind.CHI: MeldKind.CHI,
            VisibleEventKind.PON: MeldKind.PON,
            VisibleEventKind.DAIMINKAN: MeldKind.DAIMINKAN,
        }[event.kind]
        self._melds[int(actor)] += (
            PublicMeld(
                kind=kind,
                tiles=event.tiles + (called_tile,),
                from_seat=target,
                called_tile=called_tile,
            ),
        )

    def _apply_ankan(self, event: VisibleEvent) -> None:
        actor = _require_actor(event)
        if actor == self._viewer_seat:
            self._own_concealed = self._remove_own_tiles(event.tiles)
            self._own_drawn = None
        self._melds[int(actor)] += (
            PublicMeld(
                kind=MeldKind.ANKAN, tiles=event.tiles, from_seat=None, called_tile=None
            ),
        )

    def _apply_kakan(self, event: VisibleEvent) -> None:
        actor = _require_actor(event)
        added_tile = _require_tile(event)
        index, pon = resolve_kakan_source_pon(
            self._melds[int(actor)], added_tile, event.tiles
        )
        if actor == self._viewer_seat:
            self._own_concealed = self._remove_own_tiles((added_tile,))
            self._own_drawn = None
        melds = list(self._melds[int(actor)])
        melds[index] = PublicMeld(
            kind=MeldKind.KAKAN,
            tiles=pon.tiles + (added_tile,),
            from_seat=pon.from_seat,
            called_tile=pon.called_tile,
        )
        self._melds[int(actor)] = tuple(melds)

    def _apply_reach(self, event: VisibleEvent) -> None:
        actor = _require_actor(event)
        if self._riichi_states[int(actor)] is not RiichiState.NONE:
            raise MjaiReplayError(
                UnsupportedReason.EVENT_OUT_OF_ORDER,
                "reach declared while riichi state is not NONE",
            )
        self._riichi_states[int(actor)] = RiichiState.DECLARED

    def _apply_reach_accepted(self, event: VisibleEvent) -> None:
        actor = _require_actor(event)
        if self._riichi_states[int(actor)] is not RiichiState.DECLARED:
            raise MjaiReplayError(
                UnsupportedReason.EVENT_OUT_OF_ORDER,
                "reach_accepted without a preceding reach declaration",
            )
        self._riichi_states[int(actor)] = RiichiState.ACCEPTED
        self._accepted_riichi_declarations += 1

    def _consume_called_discard(
        self, target: Seat, actor: Seat, called_tile: Tile
    ) -> None:
        discards = self._discards[int(target)]
        if not discards:
            raise MjaiReplayError(
                UnsupportedReason.AMBIGUOUS_CALL_TARGET,
                "call references a seat with no recorded discard",
            )
        last = discards[-1]
        if last.called_by is not None or last.tile != called_tile:
            raise MjaiReplayError(
                UnsupportedReason.AMBIGUOUS_CALL_TARGET,
                "call does not match the target's most recent uncalled discard",
            )
        self._discards[int(target)] = discards[:-1] + (replace(last, called_by=actor),)

    def _remove_own_tiles(self, tiles: tuple[Tile, ...]) -> tuple[Tile, ...]:
        remaining = list(self._own_concealed)
        for tile in tiles:
            try:
                remaining.remove(tile)
            except ValueError as error:
                raise MjaiReplayError(
                    UnsupportedReason.HAND_ACCOUNTING_VIOLATION,
                    "action consumes a tile the acting seat does not hold",
                ) from error
        return tuple(remaining)


def _require_actor(event: VisibleEvent) -> Seat:
    if event.actor is None:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD,
            f"{event.kind.value} event carries no actor",
        )
    return event.actor


def _require_tile(event: VisibleEvent) -> Tile:
    if event.tile is None:
        raise MjaiReplayError(
            UnsupportedReason.MISSING_REQUIRED_FIELD,
            f"{event.kind.value} event carries no tile",
        )
    return event.tile


def _insert_tile(tiles: tuple[Tile, ...], tile: Tile) -> tuple[Tile, ...]:
    return tuple(sorted(tiles + (tile,), key=tile_sort_key))


__all__ = [
    "DecisionKind",
    "PlayerSafeDecisionSnapshot",
    "PlayerSafePublicView",
    "PlayerSafeRoundState",
    "resolve_kakan_source_pon",
]
