"""seat-visible ObservationとSeatMaterializedStateから`PolicyInput`を構築する。

lisbun/lisjong `docs/policy-input-schema.md`の「PolicyInputの概念schema」
「Materialized state」「同期不変条件」を実装する。

- current Observationが正本の値(self_seat、scores、round_wind、hand_number、
  dealer_seat、honba、riichi_sticks、現在の公開meld snapshot、自席手牌)は
  都度Observationから直接取得する
- 履歴が必要な値(discard order / tsumogiri / called_by、riichi段階、
  公開済みdora indicator、live wall残数)は`SeatMaterializedState`から取得する

両者が同じseat・同じdecision時点で整合しない場合は`PolicyInput`を生成せず、
`AdapterSyncError`をfail closedとして送出する。未確認・不明な値を0、空tuple、
`None`等で黙って補完しない。
"""

from lisjong.policy_contract.meld import MeldKind, PublicMeld
from lisjong.policy_contract.own_hand_state import OwnHandState
from lisjong.policy_contract.player_state import PlayerPublicState
from lisjong.policy_contract.policy_input import PolicyInput
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.round_state import RoundState
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import tile_sort_key

from lisjong_arena.riichienv.adapter.errors import AdapterSyncError
from lisjong_arena.riichienv.adapter.materialized_state import (
    KyokuIdentity,
    SeatMaterializedState,
    wind_from_round_wind_index,
)
from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_physical_id

_LIVE_WALL_START_COUNT = 84

_MELD_KIND_BY_RIICHIENV_TYPE_NAME = {
    "MeldType.Chi": MeldKind.CHI,
    "MeldType.Pon": MeldKind.PON,
    "MeldType.Daiminkan": MeldKind.DAIMINKAN,
    "MeldType.Ankan": MeldKind.ANKAN,
    "MeldType.Kakan": MeldKind.KAKAN,
}


def _build_public_meld(meld: object) -> PublicMeld:
    kind = _MELD_KIND_BY_RIICHIENV_TYPE_NAME.get(str(meld.meld_type))
    if kind is None:
        raise AdapterSyncError(f"unrecognized RiichiEnv meld_type: {meld.meld_type!r}")

    tiles = tuple(tile_from_physical_id(tile_id) for tile_id in meld.tiles)
    from_seat = None if meld.from_who < 0 else Seat(meld.from_who)
    called_tile = (
        None if meld.called_tile is None else tile_from_physical_id(meld.called_tile)
    )

    return PublicMeld(
        kind=kind, tiles=tiles, from_seat=from_seat, called_tile=called_tile
    )


def _sorted_tiles(tiles) -> tuple:
    return tuple(sorted(tiles, key=tile_sort_key))


def build_policy_input(
    tracker: SeatMaterializedState, observation: object
) -> PolicyInput:
    """`tracker`と現在の`observation`から検証済みの不変`PolicyInput`を構築する。

    内部で`tracker.apply_observation(observation)`を呼び、そのObservationの
    `new_events()`をまず同期してからsnapshotを構築する。materialized state、
    Observation、seat・decisionのいずれかが整合しない場合は`PolicyInput`を
    返さず`AdapterSyncError`を送出する。
    """
    if observation.player_id != int(tracker.self_seat):
        raise AdapterSyncError("observation.player_id does not match tracker.self_seat")

    tracker.apply_observation(observation)

    kyoku_identity = tracker.kyoku_identity
    if kyoku_identity is None:
        raise AdapterSyncError("no start_kyoku has been observed yet")

    observed_identity = KyokuIdentity(
        round_wind=wind_from_round_wind_index(observation.round_wind),
        hand_number=observation.kyoku_index + 1,
        honba=observation.honba,
        dealer_seat=Seat(observation.oya),
    )
    if observed_identity != kyoku_identity:
        raise AdapterSyncError(
            "materialized kyoku identity does not match the current Observation"
        )

    dora_indicators = tracker.dora_indicators
    if len(dora_indicators) != len(observation.dora_indicators):
        raise AdapterSyncError(
            "materialized dora indicator count does not match the current Observation"
        )

    round_state = RoundState(
        round_wind=observed_identity.round_wind,
        hand_number=observed_identity.hand_number,
        dealer_seat=observed_identity.dealer_seat,
        honba=observed_identity.honba,
        riichi_sticks=observation.riichi_sticks,
        dora_indicators=dora_indicators,
        live_wall_tiles_remaining=_LIVE_WALL_START_COUNT - tracker.tsumo_count,
    )

    materialized_discards = tracker.discards
    players = []
    for seat_index in range(4):
        materialized_seat_discards = materialized_discards[seat_index]

        observed_discard_tiles = _sorted_tiles(
            tile_from_physical_id(tile_id)
            for tile_id in observation.discards[seat_index]
        )
        materialized_discard_tiles = _sorted_tiles(
            discard.tile for discard in materialized_seat_discards
        )
        if observed_discard_tiles != materialized_discard_tiles:
            raise AdapterSyncError(
                f"materialized discards for seat {seat_index} do not match "
                "the current Observation"
            )

        riichi_state = tracker.riichi_state[seat_index]
        riichi_declared = observation.riichi_declared[seat_index]
        if riichi_declared and riichi_state is RiichiState.NONE:
            # RiichiEnv 0.4.8実測(lisbun/lisjong `docs/riichienv-investigation.md`
            # の「lisbun/lisjong#28実装時の追加実測」1.を参照): riichi_declared
            # はreach_accepted eventがこのseatのnew_events()へ届く1
            # Observation前にTrueへ切り替わることがある(宣言牌discardが
            # chi/ponでclaim可能な場合)。DECLAREDとACCEPTEDのどちらもこのlag
            # の範囲内として許容するが、reach event自体を取りこぼしたことを
            # 示すNONEとの組み合わせはfail closedする。
            raise AdapterSyncError(
                f"Observation reports riichi_declared for seat {seat_index} "
                "but materialized state is still NONE"
            )
        if not riichi_declared and riichi_state is RiichiState.ACCEPTED:
            raise AdapterSyncError(
                f"materialized state reports ACCEPTED riichi for seat "
                f"{seat_index} but the current Observation does not"
            )

        melds = tuple(
            _build_public_meld(meld) for meld in observation.melds[seat_index]
        )

        players.append(
            PlayerPublicState(
                score=observation.scores[seat_index],
                discards=materialized_seat_discards,
                melds=melds,
                riichi=riichi_state,
            )
        )

    raw_drawn_tile = observation.drawn_tile
    if raw_drawn_tile is not None and raw_drawn_tile not in observation.hand:
        if tracker.pending_chankan_actor is None:
            # 「handにないdrawn_tile」という条件だけでは、未確認の別variantや
            # 実装不整合を槍槓と取り違えかねない。直近に適用したeventが実際に
            # kakanであったことを`pending_chankan_actor`で確認できる場合だけ
            # 槍槓と扱い、それ以外はfail closedする。
            raise AdapterSyncError(
                "drawn_tile is not part of this seat's hand, but no kakan "
                "event was observed immediately before this decision to "
                "explain it as a chankan ron response opportunity"
            )
        if tile_from_physical_id(raw_drawn_tile) != tracker.pending_chankan_tile:
            # actorの一致だけでなく、drawn_tileのsemantic valueが直前kakanの
            # 加槓牌と一致することまで確認する。牌種が異なる場合は槍槓として
            # 説明できないため、未確認値をNoneへ丸めずfail closedする。
            raise AdapterSyncError(
                "drawn_tile is not part of this seat's hand, and its tile "
                "value does not match the tile added by the immediately "
                "preceding kakan"
            )
        # RiichiEnv 0.4.8実測(lisbun/lisjong `docs/riichienv-investigation.md`
        # の「lisbun/lisjong#28実装時の追加実測」2.を参照): 槍槓(chankan)の
        # ron応答機会では、応答するseatのdrawn_tileがそのseatの手牌にない、
        # kakanで加えられた牌(相手の牌)を指す値になる。このseatは実際には
        # 何もツモっていないため、`docs/policy-input-schema.md`の「対応する
        # drawn tileがない場合はNoneとする」規則をここでも適用し、Noneへ
        # 正規化する。
        raw_drawn_tile = None

    own_hand = OwnHandState(
        concealed_tiles=tuple(
            tile_from_physical_id(tile_id) for tile_id in observation.hand
        ),
        drawn_tile=(
            None if raw_drawn_tile is None else tile_from_physical_id(raw_drawn_tile)
        ),
    )

    return PolicyInput(
        self_seat=tracker.self_seat,
        round=round_state,
        players=tuple(players),
        own_hand=own_hand,
    )
