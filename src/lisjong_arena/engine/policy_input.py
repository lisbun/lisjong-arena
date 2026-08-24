"""`SeatObservation`からlisjong `PolicyInput`を直接構築する射影。

first-party engineはIssue #38で、consumer-side history reconstructionなしに
current Policy inputを構成できるplayer-safe snapshotを公開している。そのため
RiichiEnv Adapterのような`SeatMaterializedState`をこのpathへ持ち込まず、

```text
SeatObservation
        |
        v
PolicyInput
```

の1段変換だけを行う。drawn tileの推測、last discardからのreaction target
再構築、event lag補正等のRiichiEnv固有workaroundも持ち込まない。
"""

from lisjong.policy_contract import (
    Discard,
    OwnHandState,
    PlayerPublicState,
    PolicyInput,
    RoundState,
    Seat,
)
from lisjong_engine.observation import SeatObservation
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.engine.domain_conversion import (
    public_meld_from_engine_meld,
    riichi_state_from_engine_status,
    seat_from_engine_seat,
    tile_from_public_tile,
    tiles_from_public_tiles,
    wind_from_engine_wind,
)
from lisjong_arena.engine.errors import ObservationProjectionError

# lisjong `PolicyInput.players`はindex自体がSeat identityを表す。engineの
# seat別tupleは`tuple(Seat)`順であることを`SeatObservation`自身が保証して
# いるが、canonical orderの一致はこの境界の責務としてSEAT値で明示する。
_ENGINE_SEAT_ORDER = tuple(EngineSeat)
_SEAT_ORDER = tuple(Seat)


def _discard_from_engine_discard(discard) -> Discard:
    """engine `PublicDiscard`をlisjong `Discard`へ変換する。

    engineの`is_riichi_declaration`はcurrent `lisjong.Discard`契約に対応
    fieldがないため、独自fieldを追加せず、他fieldへ丸めもしない。
    """
    return Discard(
        tile=tile_from_public_tile(discard.tile),
        tsumogiri=discard.is_tsumogiri,
        order=discard.order,
        called_by=(
            None
            if discard.called_by is None
            else seat_from_engine_seat(discard.called_by)
        ),
    )


def _seat_ordered(values, field_name: str) -> tuple:
    """engineのseat別tupleを、`tuple(Seat)`順であることを確認して返す。"""
    if tuple(value.seat for value in values) != _ENGINE_SEAT_ORDER:
        raise ObservationProjectionError(
            f"observation {field_name} must be ordered as tuple(Seat)"
        )
    return tuple(values)


def build_policy_input(observation: object) -> PolicyInput:
    """1つの`SeatObservation`から、そのdecisionの`PolicyInput`を構築する。"""
    if not isinstance(observation, SeatObservation):
        raise TypeError("observation must be a lisjong-engine SeatObservation")

    self_seat = seat_from_engine_seat(observation.viewer_seat)

    discards = _seat_ordered(observation.discards, "discards")
    melds = _seat_ordered(observation.melds, "melds")
    scores = _seat_ordered(observation.scores, "scores")
    riichi_states = _seat_ordered(observation.riichi_states, "riichi_states")

    # engineのseat別tuple位置がlisjong Seat値と一致することへ暗黙に依存せず、
    # seat conversion tableで対応付けてからcanonical orderへ並べ直す。
    states_by_seat: dict[Seat, PlayerPublicState] = {}
    for index, engine_seat in enumerate(_ENGINE_SEAT_ORDER):
        seat = seat_from_engine_seat(engine_seat)
        states_by_seat[seat] = PlayerPublicState(
            score=scores[index].points,
            discards=tuple(
                _discard_from_engine_discard(discard)
                for discard in discards[index].discards
            ),
            melds=tuple(
                public_meld_from_engine_meld(meld) for meld in melds[index].melds
            ),
            riichi=riichi_state_from_engine_status(riichi_states[index].status),
        )
    if set(states_by_seat) != set(_SEAT_ORDER):
        raise ObservationProjectionError(
            "engine seats must cover every lisjong canonical seat exactly once"
        )
    players = tuple(states_by_seat[seat] for seat in _SEAT_ORDER)

    round_state = RoundState(
        round_wind=wind_from_engine_wind(observation.prevailing_wind),
        hand_number=observation.hand_number,
        dealer_seat=seat_from_engine_seat(observation.dealer_seat),
        honba=observation.honba,
        riichi_sticks=observation.riichi_sticks,
        dora_indicators=tiles_from_public_tiles(observation.dora_indicators),
        live_wall_tiles_remaining=observation.remaining_live_wall_count,
    )

    own_hand = OwnHandState(
        concealed_tiles=tiles_from_public_tiles(observation.hand_tiles),
        drawn_tile=(
            None
            if observation.drawn_tile is None
            else tile_from_public_tile(observation.drawn_tile)
        ),
    )

    return PolicyInput(
        self_seat=self_seat,
        round=round_state,
        players=players,
        own_hand=own_hand,
    )
