"""Synthetic Offline Q macro-transition fixtures.

実RiichiEnv hanchanは1局あたり分単位のcostがかかるため、macro-transition
boundary（same actor / same round次decision探索、terminal score binding、
cross-round bootstrapしないこと）はunit testで直接synthetic
`LocalGameInspection`を組み立てて検証し、teacher実行を再現しない。
production側へgeneric backend abstractionは導入しない。

`PolicyInput`等のlisjong契約型は plain immutable value typeであり、RiichiEnv
なしにこのmoduleが直接構築できる。
"""

from lisjong.policy_contract import (
    DecisionTrace,
    OwnHandState,
    PlayerPublicState,
    PolicyInput,
    RiichiState,
    RoundState,
    Seat,
    Tile,
    Wind,
)
from lisjong.policy_contract.action import DiscardAction, RiichiAction
from lisjong.policy_contract.tile import TileCategory, TileType

from lisjong_arena.game_trace import GameTrace, GameTraceEvent
from lisjong_arena.learned_policy_offline_q.protocol import GAME_MODE, Split
from lisjong_arena.learned_policy_stage2.recording import GameRecording
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspection,
    LocalGameResult,
    SeatDecisionObservation,
    StepDecisionObservation,
)
from lisjong_arena.riichienv.round_stats import SeatRoundStats


def _manzu(rank: int) -> Tile:
    return Tile(TileType(TileCategory.MANZU, rank))


def make_own_hand() -> OwnHandState:
    tiles = tuple(_manzu(rank) for rank in range(1, 10)) + tuple(
        Tile(TileType(TileCategory.PINZU, rank)) for rank in range(1, 6)
    )
    return OwnHandState(concealed_tiles=tiles, drawn_tile=tiles[-1])


def make_player_state(score: int) -> PlayerPublicState:
    return PlayerPublicState(
        score=score, discards=(), melds=(), riichi=RiichiState.NONE
    )


def make_round_state(round_wind: Wind, hand_number: int, honba: int = 0) -> RoundState:
    return RoundState(
        round_wind=round_wind,
        hand_number=hand_number,
        dealer_seat=Seat.SEAT_0,
        honba=honba,
        riichi_sticks=0,
        dora_indicators=(),
        live_wall_tiles_remaining=50,
    )


def make_policy_input(
    self_seat: Seat, round_state: RoundState, scores: tuple[int, int, int, int]
) -> PolicyInput:
    players = tuple(make_player_state(scores[seat]) for seat in range(4))
    return PolicyInput(
        self_seat=self_seat,
        round=round_state,
        players=players,
        own_hand=make_own_hand(),
    )


def eligible_discard_decision(
    seat: Seat,
    round_state: RoundState,
    scores: tuple[int, int, int, int],
    *,
    legal_ranks: tuple[int, ...],
    selected_rank: int,
) -> SeatDecisionObservation:
    """全legal_actionsがDiscardActionのchoice decision (len(legal_ranks) >= 2)。"""
    policy_input = make_policy_input(seat, round_state, scores)
    legal_actions = tuple(
        DiscardAction(actor=seat, tile=_manzu(rank), tsumogiri=False)
        for rank in legal_ranks
    )
    selected = DiscardAction(actor=seat, tile=_manzu(selected_rank), tsumogiri=False)
    trace = DecisionTrace(legal_actions=legal_actions, selected_action=selected)
    return SeatDecisionObservation(
        seat=seat, policy_input=policy_input, decision_trace=trace
    )


def forced_discard_decision(
    seat: Seat,
    round_state: RoundState,
    scores: tuple[int, int, int, int],
    *,
    rank: int,
) -> SeatDecisionObservation:
    """len(legal_actions) == 1のforced decision（activation対象外）。"""
    policy_input = make_policy_input(seat, round_state, scores)
    action = DiscardAction(actor=seat, tile=_manzu(rank), tsumogiri=True)
    trace = DecisionTrace(legal_actions=(action,), selected_action=action)
    return SeatDecisionObservation(
        seat=seat, policy_input=policy_input, decision_trace=trace
    )


def riichi_choice_decision(
    seat: Seat, round_state: RoundState, scores: tuple[int, int, int, int]
) -> SeatDecisionObservation:
    """legal_actionsに非DiscardActionを含むchoice decision（activation対象外）。"""
    policy_input = make_policy_input(seat, round_state, scores)
    discard = DiscardAction(actor=seat, tile=_manzu(1), tsumogiri=False)
    riichi = RiichiAction(actor=seat)
    trace = DecisionTrace(legal_actions=(discard, riichi), selected_action=discard)
    return SeatDecisionObservation(
        seat=seat, policy_input=policy_input, decision_trace=trace
    )


def make_step(
    step_ordinal: int, seat_decisions: tuple[SeatDecisionObservation, ...]
) -> StepDecisionObservation:
    return StepDecisionObservation(
        step_ordinal=step_ordinal,
        event_sequence_start=0,
        event_sequence_end=0,
        seat_decisions=seat_decisions,
    )


def make_game_trace(seed: int) -> GameTrace:
    return GameTrace(
        seed=seed,
        game_mode=GAME_MODE,
        events=(GameTraceEvent(sequence=0, event='{"type": "start_game"}'),),
    )


def make_result(
    seed: int, scores: tuple[int, int, int, int], *, steps: int, decisions: int
) -> LocalGameResult:
    seat_round_stats = tuple(
        SeatRoundStats(
            start_score=25000,
            end_score=scores[seat],
            won=False,
            win_points=None,
            dealt_in=False,
            deal_in_loss=None,
            exhaustive_draw=False,
            tenpai_at_exhaustive_draw=None,
            first_tenpai_turn=None,
        )
        for seat in range(4)
    )
    return LocalGameResult(
        seed=seed,
        game_mode=GAME_MODE,
        scores=scores,
        ranks=(1, 2, 3, 4),
        steps=steps,
        decisions=decisions,
        seat_round_stats=seat_round_stats,
    )


def make_recording(
    seed: int,
    split: Split,
    steps: tuple[StepDecisionObservation, ...],
    final_scores: tuple[int, int, int, int],
) -> GameRecording:
    decisions = sum(len(step.seat_decisions) for step in steps)
    result = make_result(seed, final_scores, steps=len(steps), decisions=decisions)
    inspection = LocalGameInspection(
        result=result,
        game_trace=make_game_trace(seed),
        step_observations=steps,
    )
    return GameRecording(
        seed=seed,
        split=split,
        result=result,
        inspection=inspection,
        wall_clock_seconds=0.0,
        cpu_seconds=0.0,
    )
