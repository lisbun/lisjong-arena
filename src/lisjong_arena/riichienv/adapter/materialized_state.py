"""RiichiEnv seat-visible eventから同期するmaterialized state。

lisbun/lisjong `docs/policy-input-schema.md`の「Materialized state」、
`docs/architecture.md`のRiichiEnv Adapter責務を実装する。1つの`self_seat`
視点について、seat-visible`Observation.new_events()`を継続的に処理し、
`PolicyInput`生成に履歴が必要な公開意味状態だけを保持する。

- 各seatのdiscard履歴(tile、tsumogiri、局内`order`、`called_by`)
- 各seatのriichi段階(NONE / DECLARED / ACCEPTED)
- 公開済みdora indicator
- live wall残数算出用のkyoku内tsumo event数
- 現在のkyoku identity(場風・局・本場・親)。resetの正本として使用する

副露(meld)state自体はここでは保持しない。RiichiEnv 0.4.8の実測
(lisbun/lisjong `docs/riichienv-investigation.md`の「kakan元pon解決とmeld公開
状態」)で、`Observation.melds`がkakanのin-place更新を含む現在snapshotを直接
提供することを確認したため、meldはdecisionごとに`Observation.melds`から直接
構築する(`policy_input.py`を参照)。このtrackerでmeldを二重管理しない。

`env.mjai_log`、他家の非公開情報、RiichiEnv `Action`、Policyの過去判断は
保持しない。同一`Observation` instanceの二重適用は`AdapterSyncError`で
拒否する。RiichiEnv 0.4.8のevent自体には一意なevent IDが存在しないため
(lisbun/lisjong `docs/riichienv-investigation.md`の「event重複防止に関する
追加実測」)、「新しいObservationを受け取るたびに、その`new_events()`全体を
1回だけ適用する」という運用規則そのものをここで守る。
"""

import json
from dataclasses import replace
from typing import NamedTuple

from lisjong.policy_contract.discard import Discard
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile
from lisjong.policy_contract.wind import Wind

from lisjong_arena.riichienv.adapter.errors import AdapterSyncError
from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_mjai

_WIND_BY_BAKAZE = {"E": Wind.EAST, "S": Wind.SOUTH, "W": Wind.WEST, "N": Wind.NORTH}
_WIND_BY_ROUND_WIND_INDEX = {0: Wind.EAST, 1: Wind.SOUTH, 2: Wind.WEST, 3: Wind.NORTH}

# kyoku開始後に届く、特別な状態更新を必要としないevent種別。
# ankanはObservation.meldsから直接構築するためここでは扱わない。kakanは
# 別途_dispatch_eventでpending_chankan_actorを更新するため、ここには含めない。
# hora / ryukyoku / end_kyoku / end_gameはkyoku・対局の終了を表すが、
# 後続のstart_kyokuが無条件に全stateをresetするため、ここでの個別処理は
# 不要である。start_game(kyoku開始前に届く)は_dispatch_eventで別途扱う。
_NO_OP_EVENT_TYPES = frozenset({"ankan", "hora", "ryukyoku", "end_kyoku", "end_game"})

# 単一のexcept節で複数typeを指定するとparenthesizeが必要になるが、
# ローカルのruff format実行環境で括弧が意図せず削除される既知の問題が
# あったため、named constantへ切り出して単一nameのexceptにしている。
_ROUND_WIND_LOOKUP_ERRORS = (KeyError, TypeError)


def wind_from_round_wind_index(value: object) -> Wind:
    """`Observation.round_wind`(int)をlisjong `Wind`へ変換する。"""
    try:
        return _WIND_BY_ROUND_WIND_INDEX[value]
    except _ROUND_WIND_LOOKUP_ERRORS:
        raise AdapterSyncError(f"unrecognized round_wind index: {value!r}") from None


class KyokuIdentity(NamedTuple):
    """1 kyokuを識別する公開情報。start_kyoku resetの正本として保持する。"""

    round_wind: Wind
    hand_number: int
    honba: int
    dealer_seat: Seat


class SeatMaterializedState:
    """`self_seat`視点のseat-visible materialized state。

    RiichiEnv `Observation`を直接保持せず、`apply_observation()`が処理した
    event由来の正規化済み値だけを保持する。
    """

    def __init__(self, self_seat: Seat) -> None:
        if not isinstance(self_seat, Seat):
            raise TypeError("self_seat must be a Seat")

        self.self_seat = self_seat
        self._kyoku_identity: KyokuIdentity | None = None
        self._discards: list[list[Discard]] = [[] for _ in range(4)]
        self._next_discard_order = 0
        self._riichi_state: list[RiichiState] = [RiichiState.NONE] * 4
        self._dora_indicators: list = []
        self._tsumo_count = 0
        self._pending_chankan_actor: Seat | None = None
        self._pending_chankan_tile: Tile | None = None
        self._last_applied_observation: object | None = None

    @property
    def kyoku_identity(self) -> KyokuIdentity | None:
        return self._kyoku_identity

    @property
    def discards(self) -> tuple[tuple[Discard, ...], ...]:
        return tuple(tuple(seat_discards) for seat_discards in self._discards)

    @property
    def riichi_state(self) -> tuple[RiichiState, ...]:
        return tuple(self._riichi_state)

    @property
    def dora_indicators(self) -> tuple:
        return tuple(self._dora_indicators)

    @property
    def tsumo_count(self) -> int:
        return self._tsumo_count

    @property
    def pending_chankan_actor(self) -> Seat | None:
        """直近に適用したeventがkakanだった場合、そのkakanのactor。

        それ以外のeventが1件でも適用されると`None`へ戻る。RiichiEnv 0.4.8の
        実装事実(lisbun/lisjong `docs/riichienv-investigation.md`の「槍槓
        (chankan)のtarget解決」)どおり、kakan成立直後に届くchankan ron応答
        機会のObservationを識別するためだけに使う値であり、それ以外の
        contextでkakan発生を推測する用途には使わない。`pending_chankan_tile`
        と対で使う。
        """
        return self._pending_chankan_actor

    @property
    def pending_chankan_tile(self) -> Tile | None:
        """直近に適用したeventがkakanだった場合、そのkakanで加えられた牌。

        `pending_chankan_actor`と同じ寿命(kakan以外のeventが1件でも
        適用されると`None`へ戻る)を持つ。`drawn_tile`が自席handにない値を
        「槍槓中の相手の加槓牌」と判断する際、actorの一致だけでなく、この
        牌のsemantic valueが`drawn_tile`と一致することまで確認するために
        使う(lisbun/lisjong `docs/riichienv-investigation.md`の「lisbun/lisjong
        #28実装時の追加実測」2.を参照)。
        """
        return self._pending_chankan_tile

    def apply_observation(self, observation: object) -> None:
        """このseatの新しいObservationが持つ`new_events()`を1回だけ適用する。

        同一Observation instanceを続けて渡すと`AdapterSyncError`にする。
        RiichiEnv側にevent IDが存在しないため、「新しいObservation instance
        単位で1回だけ未適用分として扱う」という運用規則そのもので重複を防ぐ。
        """
        if observation.player_id != int(self.self_seat):
            raise AdapterSyncError(
                "observation.player_id does not match this tracker's self_seat"
            )
        if observation is self._last_applied_observation:
            raise AdapterSyncError("this Observation instance was already applied")

        events = [json.loads(raw_event) for raw_event in observation.new_events()]
        for event in events:
            self._apply_event(event)

        self._last_applied_observation = observation

    def _apply_event(self, event: dict) -> None:
        event_type = event.get("type")
        try:
            self._dispatch_event(event_type, event)

            # pending_chankan_actor / pending_chankan_tileは「直近に適用した
            # eventがkakanか」だけを表すため、kakan以外のeventが1件でも
            # 処理されたらここで必ずNoneへ戻す。
            if event_type == "kakan":
                self._pending_chankan_actor = Seat(int(event["actor"]))
                self._pending_chankan_tile = tile_from_mjai(event["pai"])
            else:
                self._pending_chankan_actor = None
                self._pending_chankan_tile = None
        except KeyError as exc:
            raise AdapterSyncError(
                f"malformed {event_type!r} event: missing {exc}"
            ) from exc

    def _dispatch_event(self, event_type: object, event: dict) -> None:
        if event_type == "start_kyoku":
            self._reset_kyoku(event)
            return
        if event_type == "start_game":
            # game全体の開始を表し、最初のstart_kyoku前に届く。
            return

        if self._kyoku_identity is None:
            raise AdapterSyncError(
                f"received {event_type!r} event before any start_kyoku"
            )

        if event_type == "tsumo":
            self._tsumo_count += 1
            return
        if event_type == "dahai":
            self._apply_dahai(event)
            return
        if event_type in ("chi", "pon", "daiminkan"):
            self._apply_call(event)
            return
        if event_type == "reach":
            self._apply_reach(event)
            return
        if event_type == "reach_accepted":
            self._apply_reach_accepted(event)
            return
        if event_type == "dora":
            self._dora_indicators.append(tile_from_mjai(event["dora_marker"]))
            return
        if event_type == "kakan":
            # Observation.meldsから直接構築するため、meld state自体は
            # ここでは更新しない。pending_chankan_actor /
            # pending_chankan_tileの更新は呼び出し元の_apply_eventが行う。
            return
        if event_type in _NO_OP_EVENT_TYPES:
            return

        raise AdapterSyncError(f"unrecognized event type: {event_type!r}")

    def _reset_kyoku(self, event: dict) -> None:
        round_wind = _WIND_BY_BAKAZE.get(event["bakaze"])
        if round_wind is None:
            raise AdapterSyncError(f"unrecognized bakaze: {event['bakaze']!r}")

        self._kyoku_identity = KyokuIdentity(
            round_wind=round_wind,
            hand_number=int(event["kyoku"]),
            honba=int(event["honba"]),
            dealer_seat=Seat(int(event["oya"])),
        )
        self._discards = [[] for _ in range(4)]
        self._next_discard_order = 0
        self._riichi_state = [RiichiState.NONE] * 4
        self._dora_indicators = [tile_from_mjai(event["dora_marker"])]
        self._tsumo_count = 0
        self._pending_chankan_actor = None
        self._pending_chankan_tile = None

    def _apply_dahai(self, event: dict) -> None:
        actor = Seat(int(event["actor"]))
        tsumogiri = event["tsumogiri"]
        if type(tsumogiri) is not bool:
            raise AdapterSyncError("dahai event tsumogiri must be a bool")

        discard = Discard(
            tile=tile_from_mjai(event["pai"]),
            tsumogiri=tsumogiri,
            order=self._next_discard_order,
            called_by=None,
        )
        self._discards[actor].append(discard)
        self._next_discard_order += 1

    def _apply_call(self, event: dict) -> None:
        target = Seat(int(event["target"]))
        called_tile = tile_from_mjai(event["pai"])

        target_discards = self._discards[target]
        if not target_discards:
            raise AdapterSyncError(
                "call event references a seat with no recorded discards"
            )

        last_index = len(target_discards) - 1
        last_discard = target_discards[last_index]
        if last_discard.called_by is not None or last_discard.tile != called_tile:
            raise AdapterSyncError(
                "call event does not match the target's most recent discard"
            )

        target_discards[last_index] = replace(
            last_discard, called_by=Seat(int(event["actor"]))
        )

    def _apply_reach(self, event: dict) -> None:
        actor = Seat(int(event["actor"]))
        if self._riichi_state[actor] is not RiichiState.NONE:
            raise AdapterSyncError(
                f"reach event received for seat {actor} while riichi is not NONE"
            )
        self._riichi_state[actor] = RiichiState.DECLARED

    def _apply_reach_accepted(self, event: dict) -> None:
        actor = Seat(int(event["actor"]))
        if self._riichi_state[actor] is not RiichiState.DECLARED:
            raise AdapterSyncError(
                f"reach_accepted event received for seat {actor} "
                "without a preceding reach"
            )
        self._riichi_state[actor] = RiichiState.ACCEPTED
