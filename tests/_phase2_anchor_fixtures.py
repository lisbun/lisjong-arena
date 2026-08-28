"""Phase 2 anchor pipeline testで共有するdeterministic fixture helper。

実対局を回さずにplayer-safe / label boundaryを固定するためのlisjong-side
tile helperと、`RuleSet`のfield単位variantを作るhelperを提供する。
"""

from dataclasses import replace

from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policy_contract import (
    MeldKind,
    PublicMeld,
    Seat,
    Tile,
    TileCategory,
    TileType,
    Wind,
)
from lisjong_engine.driver import run_hanchan
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import ObservationDecisionKind, SeatObservation
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.policy_selector import PolicySeatSelector
from lisjong_arena.phase2_training_anchor.training_labels import OpponentIdentity


def tile(category: TileCategory, rank: int, *, is_red: bool = False) -> Tile:
    return Tile(TileType(category, rank), is_red)


def manzu(rank: int, *, is_red: bool = False) -> Tile:
    return tile(TileCategory.MANZU, rank, is_red=is_red)


def pinzu(rank: int, *, is_red: bool = False) -> Tile:
    return tile(TileCategory.PINZU, rank, is_red=is_red)


def souzu(rank: int, *, is_red: bool = False) -> Tile:
    return tile(TileCategory.SOUZU, rank, is_red=is_red)


def honor(rank: int) -> Tile:
    return tile(TileCategory.HONOR, rank)


def hand(*tiles: Tile) -> tuple[Tile, ...]:
    return tuple(tiles)


def pon(tile_value: Tile, from_seat: Seat = Seat.SEAT_1) -> PublicMeld:
    """同一tile typeのPon meld。structural 3-equivalentとして数えられる。"""
    return PublicMeld(
        kind=MeldKind.PON,
        tiles=(tile_value, tile_value, tile_value),
        from_seat=from_seat,
        called_tile=tile_value,
    )


def opponent_identity(
    seat: Seat = Seat.SEAT_1,
    wind: Wind = Wind.SOUTH,
    offset: int = 1,
) -> OpponentIdentity:
    return OpponentIdentity(seat=seat, wind=wind, viewer_relative_offset=offset)


def rules_with(**overrides) -> RuleSet:
    """`RuleSet.default()`から指定fieldだけを差し替えたvariantを作る。"""
    return replace(RuleSet.default(), **overrides)


def run_game_with_recorder(match_state: MatchState, recorder) -> None:
    """`recorder.observe()`をTURN観測へ挟みつつ、1 hanchanを最後まで進める。

    online selectorへ渡す情報は変えないため、trajectory自体はrecorderなしの
    場合と同一である。
    """

    def _make(seat: EngineSeat):
        delegate = PolicySeatSelector(seat, TwoStepUkeirePolicy())

        def _selector(observation: SeatObservation, options):
            recorder.observe(observation)
            return delegate(observation, options)

        return _selector

    run_hanchan(match_state, {seat: _make(seat) for seat in EngineSeat})


class _HaltAtAnchor(Exception):
    """指定anchorへ到達した時点で`run_hanchan`を停止させるsentinel。"""


class HaltedAnchor:
    """あるTURN anchorで停止した実対局のstateとcaptured value。"""

    def __init__(
        self,
        match_state: MatchState,
        observation: SeatObservation,
        anchor_index: int,
    ) -> None:
        self.match_state = match_state
        self.observation = observation
        self.anchor_index = anchor_index

    @property
    def round_state(self):
        active_round = self.match_state.active_round
        if active_round is None:
            raise RuntimeError("the halted match has no active round")
        return active_round


def halt_at_turn_anchor(seed: int, anchor_index: int = 0) -> HaltedAnchor:
    """実対局を`anchor_index`番目のTURN anchorまで進めて停止させる。

    停止時点の`MatchState`はそのdecision pointのままであり、omniscient
    round stateとplayer-safe observationを同じanchorで読める。engine
    transactionは適用されないため、停止後のstateはanchor時点と一致する。
    """
    match_state = MatchState(seed=seed, rules=RuleSet.default())
    captured: dict[str, object] = {}
    seen = 0

    def _selector(observation: SeatObservation, options):
        nonlocal seen
        if observation.decision_kind is ObservationDecisionKind.TURN:
            if seen == anchor_index:
                captured["observation"] = observation
                raise _HaltAtAnchor
            seen += 1
        return PolicySeatSelector(observation.viewer_seat, TwoStepUkeirePolicy())(
            observation, options
        )

    selectors = {seat: _selector for seat in EngineSeat}
    try:
        run_hanchan(match_state, selectors)
    except _HaltAtAnchor:
        pass
    else:  # pragma: no cover - fixture misuse
        raise AssertionError(f"anchor {anchor_index} was never reached for seed {seed}")

    return HaltedAnchor(match_state, captured["observation"], anchor_index)
