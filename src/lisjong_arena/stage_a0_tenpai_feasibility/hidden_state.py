"""current flat-BC decision pointのsame-state privileged観測seam。

```text
RiichiEnv (pre-action state)
    -> DecisionPointObserver.on_decision_point()
    -> DecisionPointHiddenState      (4 seatのconcealed hand / meld / riichi)
```

`LocalGameRunner`はこのsnapshotをPolicy / `PolicyInput` / `GameTrace`へ渡さ
ない。snapshotは`env.step()`を呼ぶ直前、つまりそのstepの全seat decisionが
`PolicyInput`を構築するのと同一のlogical stateで取得する。したがってlabel側は
後続stateからのheuristic reconstructionを一切行わない。

同一stateであることは仮定せず、`require_same_decision_state()`が

```text
round identity        hand / honba / round wind / dealer
actor own hand        privileged concealed multiset == public own-hand multiset
public meld snapshot  privileged melds == PolicyInput.players[seat].melds
riichi binding        public RiichiState と privileged riichi_declared の整合
```

をfail closedで検証する。1つでも一致しなければlabelを作らずhard failureにする。
"""

from collections import Counter
from dataclasses import dataclass

from lisjong.policy_contract import PolicyInput, Seat
from lisjong.policy_contract.meld import PublicMeld
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.tile import Tile, tile_sort_key

from lisjong_arena.riichienv.adapter.policy_input import build_public_meld
from lisjong_arena.riichienv.adapter.tile_conversion import tile_from_physical_id

from .errors import StageA0AlignmentError

SEAT_COUNT = 4


@dataclass(frozen=True, slots=True)
class PrivilegedSeatState:
    """1 seatのpre-action privileged state。

    `concealed_tiles` / `melds`が`None`なのは、source側がその値を提供でき
    なかった場合だけであり、空tupleとは意味が違う（空tupleは「meldが0件」と
    いうfactである）。
    """

    seat: Seat
    concealed_tiles: tuple[Tile, ...] | None
    melds: tuple[PublicMeld, ...] | None
    riichi_declared: bool

    def __post_init__(self) -> None:
        if not isinstance(self.seat, Seat):
            raise TypeError("seat must be a lisjong Seat")
        if self.concealed_tiles is not None and any(
            not isinstance(tile, Tile) for tile in self.concealed_tiles
        ):
            raise TypeError("concealed_tiles must contain only lisjong Tile values")
        if self.melds is not None and any(
            not isinstance(meld, PublicMeld) for meld in self.melds
        ):
            raise TypeError("melds must contain only lisjong PublicMeld values")
        if type(self.riichi_declared) is not bool:
            raise TypeError("riichi_declared must be an exact bool")


@dataclass(frozen=True, slots=True)
class DecisionPointHiddenState:
    """1 `env.step()`直前のpre-action privileged snapshot。"""

    step_ordinal: int
    pending_seats: tuple[Seat, ...]
    seats: tuple[PrivilegedSeatState, ...]

    def __post_init__(self) -> None:
        if type(self.step_ordinal) is not int or self.step_ordinal < 0:
            raise ValueError("step_ordinal must be a non-negative int")
        if len(self.seats) != SEAT_COUNT:
            raise StageA0AlignmentError(
                "a decision point snapshot must carry exactly four seat states"
            )
        if tuple(state.seat for state in self.seats) != tuple(Seat):
            raise StageA0AlignmentError(
                "seat states must be ordered by ascending canonical seat"
            )
        if not self.pending_seats:
            raise StageA0AlignmentError("a decision point must have a pending seat")
        if len(set(self.pending_seats)) != len(self.pending_seats):
            raise StageA0AlignmentError("pending seats must not repeat")

    def seat_state(self, seat: Seat) -> PrivilegedSeatState:
        if not isinstance(seat, Seat):
            raise TypeError("seat must be a lisjong Seat")
        return self.seats[int(seat)]


def capture_decision_point_hidden_state(
    env: object,
    *,
    step_ordinal: int,
    pending_player_ids: tuple[int, ...],
) -> DecisionPointHiddenState:
    """RiichiEnvのpre-action stateからprivileged snapshotを構築する。

    読むのは`hands` / `melds` / `riichi_declared`という公開されたenv accessor
    だけであり、private / internal attributeへは依存しない。wall order、future
    draw、future action、final resultは読まない（Stage A0 sidecar contractの
    外である）。
    """
    if type(step_ordinal) is not int or step_ordinal < 0:
        raise ValueError("step_ordinal must be a non-negative int")

    hands = env.hands
    melds = env.melds
    riichi_declared = env.riichi_declared
    for name, value in (
        ("hands", hands),
        ("melds", melds),
        ("riichi_declared", riichi_declared),
    ):
        if len(value) != SEAT_COUNT:
            raise StageA0AlignmentError(
                f"RiichiEnv.{name} must expose exactly four seats"
            )

    states = []
    for index, seat in enumerate(Seat):
        states.append(
            PrivilegedSeatState(
                seat=seat,
                concealed_tiles=tuple(
                    tile_from_physical_id(tile_id) for tile_id in hands[index]
                ),
                melds=tuple(build_public_meld(meld) for meld in melds[index]),
                riichi_declared=bool(riichi_declared[index]),
            )
        )
    return DecisionPointHiddenState(
        step_ordinal=step_ordinal,
        pending_seats=tuple(Seat(player_id) for player_id in pending_player_ids),
        seats=tuple(states),
    )


class DecisionPointHiddenStateRecorder:
    """stepごとのprivileged snapshotをin-memoryへ蓄積するopt-in observer。

    `LocalGameRunner`へ渡すが、runnerはこのobserverの値をPolicyへも
    `PolicyInput`へも`GameTrace`へも接続しない。
    """

    __slots__ = ("_snapshots",)

    def __init__(self) -> None:
        self._snapshots: dict[int, DecisionPointHiddenState] = {}

    def on_decision_point(
        self,
        *,
        step_ordinal: int,
        env: object,
        pending_player_ids: tuple[int, ...],
    ) -> None:
        if step_ordinal in self._snapshots:
            raise StageA0AlignmentError(
                f"decision point {step_ordinal} was observed more than once"
            )
        self._snapshots[step_ordinal] = capture_decision_point_hidden_state(
            env,
            step_ordinal=step_ordinal,
            pending_player_ids=pending_player_ids,
        )

    def snapshot(self, step_ordinal: int) -> DecisionPointHiddenState:
        state = self._snapshots.get(step_ordinal)
        if state is None:
            raise StageA0AlignmentError(
                f"no privileged snapshot was observed for step {step_ordinal}"
            )
        return state

    @property
    def observed_step_count(self) -> int:
        return len(self._snapshots)


def _sorted_tiles(tiles) -> tuple[Tile, ...]:
    return tuple(sorted(tiles, key=tile_sort_key))


def require_same_decision_state(
    policy_input: PolicyInput,
    hidden: DecisionPointHiddenState,
    actor_seat: Seat,
) -> None:
    """public rowとprivileged snapshotが同一decision stateであることを検証する。

    一致しない場合はlabelを作らず`StageA0AlignmentError`でfail closedする。
    positionの推測もseat attachmentの推測も行わない。
    """
    if not isinstance(policy_input, PolicyInput):
        raise TypeError("policy_input must be a PolicyInput")
    if not isinstance(hidden, DecisionPointHiddenState):
        raise TypeError("hidden must be a DecisionPointHiddenState")
    if not isinstance(actor_seat, Seat):
        raise TypeError("actor_seat must be a lisjong Seat")

    if policy_input.self_seat != actor_seat:
        raise StageA0AlignmentError(
            "the public row's self seat does not match the labelled actor seat"
        )
    if actor_seat not in hidden.pending_seats:
        raise StageA0AlignmentError(
            "the labelled actor seat was not pending at this decision point"
        )

    actor_state = hidden.seat_state(actor_seat)
    if actor_state.concealed_tiles is None:
        raise StageA0AlignmentError(
            "the privileged snapshot does not carry the actor's own hand, so "
            "the same-state binding cannot be demonstrated"
        )
    public_own = Counter(_sorted_tiles(policy_input.own_hand.concealed_tiles))
    privileged_own = Counter(_sorted_tiles(actor_state.concealed_tiles))
    if public_own != privileged_own:
        raise StageA0AlignmentError(
            "the privileged actor hand does not reproduce the public own-hand "
            "state; the two surfaces are not the same decision point"
        )

    for seat in Seat:
        public_state = policy_input.players[int(seat)]
        seat_state = hidden.seat_state(seat)
        if seat_state.melds is None:
            raise StageA0AlignmentError(
                f"the privileged snapshot does not carry melds for seat {int(seat)}"
            )
        if tuple(seat_state.melds) != tuple(public_state.melds):
            raise StageA0AlignmentError(
                f"privileged and public meld snapshots differ for seat {int(seat)}"
            )
        if public_state.riichi is RiichiState.NONE and seat_state.riichi_declared:
            raise StageA0AlignmentError(
                f"public state reports no riichi for seat {int(seat)} but the "
                "privileged snapshot reports a declaration"
            )
        if public_state.riichi is RiichiState.ACCEPTED and not (
            seat_state.riichi_declared
        ):
            raise StageA0AlignmentError(
                f"public state reports accepted riichi for seat {int(seat)} but "
                "the privileged snapshot does not"
            )


__all__ = [
    "SEAT_COUNT",
    "DecisionPointHiddenState",
    "DecisionPointHiddenStateRecorder",
    "PrivilegedSeatState",
    "capture_decision_point_hidden_state",
    "require_same_decision_state",
]
