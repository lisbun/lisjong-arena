"""Synthetic Stage A0 Tenpai feasibility fixtures.

実RiichiEnv hanchanは1局あたり分単位のcostがかかるため、unit testでは実行
境界を差し替え、契約上有効な合成decision stateでlabel path / seat mapping /
sidecar / qualification boundaryを検証する。production側へgeneric backend
abstractionは導入しない。
"""

from lisjong.policy_contract import DecisionContext, PolicyInput, Seat, Wind
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.meld import MeldKind, PublicMeld
from lisjong.policy_contract.own_hand_state import OwnHandState
from lisjong.policy_contract.player_state import PlayerPublicState
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.round_state import RoundState

from lisjong_arena.learned_policy_offline_q.protocol import TEACHER_SOURCE_REVISION
from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_mjai
from lisjong_arena.stage_a0_tenpai_feasibility.emission import ObservedDecision
from lisjong_arena.stage_a0_tenpai_feasibility.hidden_state import (
    DecisionPointHiddenState,
    PrivilegedSeatState,
)

# 13-tile stable hands used as privileged opponent truth.
TENPAI_HAND = "1m 1m 1m 2p 3p 4p 5s 6s 7s E E W W"
NON_TENPAI_HAND = "1m 3m 5m 7m 9m 1p 3p 5p 7p 9p 1s 3s E"
OPEN_TENPAI_CONCEALED = "2p 3p 4p 5s 6s 7s E E W W"
FOURTEEN_EQUIVALENT_HAND = "1m 1m 1m 2p 3p 4p 5s 6s 7s E E W W N"
INVALID_INVENTORY_HAND = "1m 1m 1m 1m 1m 2p 3p 4p 5s 6s 7s E E"

ACTOR_HAND = "9m 9m 9p 9p 1s 2s 3s 4s 5s 6s 7s 8s 9s 9s"


def tiles(text: str) -> tuple:
    return tuple(tile_from_mjai(part) for part in text.split())


def pon_meld(*, from_seat: Seat) -> PublicMeld:
    called = tile_from_mjai("C")
    return PublicMeld(
        kind=MeldKind.PON,
        tiles=(called, tile_from_mjai("C"), tile_from_mjai("C")),
        from_seat=from_seat,
        called_tile=called,
    )


def ankan_meld() -> PublicMeld:
    tile = tile_from_mjai("F")
    return PublicMeld(
        kind=MeldKind.ANKAN,
        tiles=(tile, tile, tile, tile),
        from_seat=None,
        called_tile=None,
    )


def round_state(*, dealer_seat: Seat = Seat.SEAT_0) -> RoundState:
    return RoundState(
        round_wind=Wind.EAST,
        hand_number=1,
        dealer_seat=dealer_seat,
        honba=0,
        riichi_sticks=0,
        dora_indicators=(tile_from_mjai("1p"),),
        live_wall_tiles_remaining=42,
    )


def seat_plan(
    *,
    actor_seat: Seat,
    opponent_hands: dict,
    opponent_melds: dict | None = None,
    riichi: dict | None = None,
    actor_hand: str = ACTOR_HAND,
):
    """各seatのconcealed truth / meld / riichi stateを1か所で決める。"""
    opponent_melds = {} if opponent_melds is None else opponent_melds
    riichi = {} if riichi is None else riichi
    plan = {}
    for seat in Seat:
        if seat is actor_seat:
            plan[seat] = (tiles(actor_hand), (), RiichiState.NONE)
            continue
        hand = opponent_hands[seat]
        plan[seat] = (
            None if hand is None else tiles(hand),
            opponent_melds.get(seat, ()),
            riichi.get(seat, RiichiState.NONE),
        )
    return plan


def policy_input_for(plan, *, actor_seat: Seat, dealer_seat: Seat) -> PolicyInput:
    players = []
    for seat in Seat:
        _, melds, riichi_state = plan[seat]
        players.append(
            PlayerPublicState(
                score=25000,
                discards=(),
                melds=tuple(melds),
                riichi=riichi_state,
            )
        )
    actor_tiles, _, _ = plan[actor_seat]
    return PolicyInput(
        self_seat=actor_seat,
        round=round_state(dealer_seat=dealer_seat),
        players=tuple(players),
        own_hand=OwnHandState(concealed_tiles=actor_tiles, drawn_tile=None),
    )


def hidden_state_for(
    plan,
    *,
    actor_seat: Seat,
    step_ordinal: int = 0,
    drop_hand_for: Seat | None = None,
    drop_melds_for: Seat | None = None,
) -> DecisionPointHiddenState:
    states = []
    for seat in Seat:
        concealed, melds, riichi_state = plan[seat]
        states.append(
            PrivilegedSeatState(
                seat=seat,
                concealed_tiles=None if seat is drop_hand_for else concealed,
                melds=None if seat is drop_melds_for else tuple(melds),
                riichi_declared=riichi_state is not RiichiState.NONE,
            )
        )
    return DecisionPointHiddenState(
        step_ordinal=step_ordinal,
        pending_seats=(actor_seat,),
        seats=tuple(states),
    )


def decision_context(policy_input: PolicyInput) -> tuple:
    """2つ以上のlegal discardを持つcontextと、teacherが選んだactionを返す。"""
    actor = policy_input.self_seat
    concealed = policy_input.own_hand.concealed_tiles
    chosen = DiscardAction(actor=actor, tile=concealed[0], tsumogiri=False)
    alternative = DiscardAction(actor=actor, tile=concealed[4], tsumogiri=False)
    context = DecisionContext(input=policy_input, legal_actions=(chosen, alternative))
    return context, chosen


def observed_decision(
    plan,
    *,
    actor_seat: Seat,
    dealer_seat: Seat = Seat.SEAT_0,
    step_ordinal: int = 0,
    decision_ordinal: int = 0,
    drop_hand_for: Seat | None = None,
    drop_melds_for: Seat | None = None,
    hidden_step_ordinal: int | None = None,
) -> ObservedDecision:
    policy_input = policy_input_for(
        plan, actor_seat=actor_seat, dealer_seat=dealer_seat
    )
    context, chosen = decision_context(policy_input)
    hidden = hidden_state_for(
        plan,
        actor_seat=actor_seat,
        step_ordinal=(
            step_ordinal if hidden_step_ordinal is None else hidden_step_ordinal
        ),
        drop_hand_for=drop_hand_for,
        drop_melds_for=drop_melds_for,
    )
    return ObservedDecision(
        step_ordinal=step_ordinal,
        decision_ordinal=decision_ordinal,
        actor_seat=actor_seat,
        context=context,
        selected_action=chosen,
        hidden=hidden,
    )


def uniform_plan(hand: str, *, actor_seat: Seat, **kwargs):
    opponents = {seat: hand for seat in Seat if seat is not actor_seat}
    return seat_plan(actor_seat=actor_seat, opponent_hands=opponents, **kwargs)


RETAINED_PROVENANCE = {
    "execution_environment": "riichienv",
    "lisjong_arena_version": "0.1.0",
    "lisjong_arena_revision": "0" * 40,
    "lisjong_version": "0.1.0",
    "lisjong_revision": TEACHER_SOURCE_REVISION,
    "lisjong_engine_version": "0.1.0",
    "lisjong_engine_revision": "2" * 40,
    "riichienv_version": "0.4.10",
    "python_version": "3.14.0",
}

FIXTURE_PROVENANCE = {
    "execution_environment": "riichienv",
    "lisjong_arena_version": "0.1.0",
    "lisjong_arena_revision": "0" * 40,
    "lisjong_version": "0.1.0",
    "lisjong_revision": "1" * 40,
    "lisjong_engine_version": "0.1.0",
    "lisjong_engine_revision": "2" * 40,
    "riichienv_version": "0.4.10",
    "python_version": "3.14.0",
}


def build_retained_dataset(
    destination,
    emissions_by_seed,
    *,
    row_overrides=None,
    legal_action_count_drift=False,
):
    """合成retained flat-BC corpusを、locked seed populationを満たす形で書き出す。

    TRAIN-side seedのrowは与えたemissionのexact bytesから作る。VALIDATION /
    protected TEST seedのrowはfiller値であり、#258 qualification pathはこれを
    読まない（それ自体をtestで固定する）。
    """
    from lisjong_arena.learned_policy_offline_q.artifact import OfflineQDatasetWriter
    from lisjong_arena.learned_policy_offline_q.model import MacroTransitionRow
    from lisjong_arena.learned_policy_offline_q.protocol import (
        DATASET_ORDERED_SEEDS,
        FEATURE_DIMENSION,
        VOCABULARY_SIZE,
        action_family,
        split_for_seed,
    )

    def filler_row(seed, split, ordinal):
        mask = tuple(index in (0, 2) for index in range(VOCABULARY_SIZE))
        values = [0.0] * FEATURE_DIMENSION
        values[(seed + ordinal) % FEATURE_DIMENSION] = 1.0
        return MacroTransitionRow(
            seed=seed,
            split=split,
            round_ordinal=0,
            round_wind="east",
            hand_number=1,
            honba=0,
            actor_seat=0,
            step_ordinal=ordinal,
            decision_ordinal=ordinal,
            feature_values=tuple(values),
            legal_mask=mask,
            behavior_action_index=0,
            behavior_action_family=action_family(0),
            reward=0.0,
            terminal=True,
            next_step_ordinal=None,
            next_decision_ordinal=None,
            next_feature_values=None,
            next_legal_mask=None,
        )

    def aligned_row(seed, split, emission, decision):
        overrides = {} if row_overrides is None else dict(row_overrides)
        legal_mask = emission.row.legal_mask
        if legal_action_count_drift:
            # `legal_action_count` は mask から導出されるので、count だけを
            # ずらすには mask 側を 1 action 分ずらす。
            extra = next(index for index, legal in enumerate(legal_mask) if not legal)
            legal_mask = tuple(
                True if index == extra else legal
                for index, legal in enumerate(legal_mask)
            )
        return MacroTransitionRow(
            seed=seed,
            split=split,
            round_ordinal=overrides.get("round_ordinal", 0),
            round_wind=overrides.get("round_wind", emission.row.round_wind),
            hand_number=overrides.get("hand_number", emission.row.hand_number),
            honba=overrides.get("honba", emission.row.honba),
            actor_seat=emission.row.identity.actor_seat,
            step_ordinal=decision.step_ordinal,
            decision_ordinal=decision.decision_ordinal,
            feature_values=emission.row.feature_values,
            legal_mask=legal_mask,
            behavior_action_index=emission.row.teacher_action_index,
            behavior_action_family=emission.row.teacher_action_family,
            reward=0.0,
            terminal=True,
            next_step_ordinal=None,
            next_decision_ordinal=None,
            next_feature_values=None,
            next_legal_mask=None,
        )

    writer = OfflineQDatasetWriter(destination, provenance=RETAINED_PROVENANCE)
    try:
        for seed in DATASET_ORDERED_SEEDS:
            split = split_for_seed(seed)
            pairs = emissions_by_seed.get(seed)
            if pairs:
                rows = [
                    aligned_row(seed, split, emission, decision)
                    for decision, emission in pairs
                ]
            else:
                rows = [filler_row(seed, split, 0)]
            writer.add_game(
                seed=seed,
                split=split,
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=rows,
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise
