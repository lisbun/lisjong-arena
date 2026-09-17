"""same-state public row + privileged annotationのco-emission。

1 decisionについて、public rowとprivileged cellを同一row identityで作る。
retained augmentation routeとfresh live-label routeはこの1つの実装を共有し、
違うのはdecisionの取得元とsource identityだけである。

```text
ObservedDecision (pre-action decision point)
    -> require_same_decision_state()   public row state == privileged label state
    -> PublicDecisionRow               8204 feature / 802 legal mask / teacher action
    -> PrivilegedTargetCell x 3        relative slot -> canonical seat -> hidden truth
```

privileged値はcellだけに入り、rowへは入らない。
"""

from dataclasses import dataclass

from lisjong.policy_contract import DecisionContext, Seat

from .errors import StageA0AlignmentError
from .hidden_state import DecisionPointHiddenState, require_same_decision_state
from .labels import build_opponent_target, opponent_identity
from .protocol import RELATIVE_OPPONENT_OFFSETS, Split
from .public_row import (
    DecisionRowIdentity,
    PublicDecisionRow,
    build_public_decision_row,
)
from .sidecar import PrivilegedTargetCell


@dataclass(frozen=True, slots=True)
class ObservedDecision:
    """1 teacher decisionのpublic contextと、同じstateのprivileged snapshot。"""

    step_ordinal: int
    decision_ordinal: int
    actor_seat: Seat
    context: DecisionContext
    selected_action: object
    hidden: DecisionPointHiddenState

    def __post_init__(self) -> None:
        if not isinstance(self.actor_seat, Seat):
            raise TypeError("actor_seat must be a lisjong Seat")
        if not isinstance(self.context, DecisionContext):
            raise TypeError("context must be a DecisionContext")
        if not isinstance(self.hidden, DecisionPointHiddenState):
            raise TypeError("hidden must be a DecisionPointHiddenState")
        if self.hidden.step_ordinal != self.step_ordinal:
            raise StageA0AlignmentError(
                "the privileged snapshot belongs to a different step; label "
                "state and row state must be the same decision point"
            )


@dataclass(frozen=True, slots=True)
class DecisionEmission:
    """1 decisionのpublic rowと、その3つのopponent-target cell。"""

    row: PublicDecisionRow
    cells: tuple[PrivilegedTargetCell, ...]

    def __post_init__(self) -> None:
        if len(self.cells) != len(RELATIVE_OPPONENT_OFFSETS):
            raise StageA0AlignmentError(
                "every decision row must carry exactly three opponent cells"
            )
        offsets = tuple(cell.identity.viewer_relative_offset for cell in self.cells)
        if offsets != RELATIVE_OPPONENT_OFFSETS:
            raise StageA0AlignmentError(
                "opponent cells must be ordered by ascending relative offset"
            )
        if len({int(cell.identity.seat) for cell in self.cells}) != len(self.cells):
            raise StageA0AlignmentError(
                "opponent cells must reference three distinct canonical seats"
            )
        if self.row.identity.actor_seat in {
            int(cell.identity.seat) for cell in self.cells
        }:
            raise StageA0AlignmentError(
                "the acting seat must never appear as its own opponent target"
            )
        for cell in self.cells:
            if cell.row_identity != self.row.identity:
                raise StageA0AlignmentError(
                    "opponent cells must carry the row identity of their row"
                )
            if cell.public_row_digest != self.row.row_digest():
                raise StageA0AlignmentError(
                    "opponent cells must reference the digest of their public row"
                )


def emit_decision(
    decision: ObservedDecision,
    *,
    source_identity: str,
    seed: int,
    split: Split | None = None,
) -> DecisionEmission:
    """1 decisionのpublic rowとprivileged cellを同一row identityで作る。

    same-state bindingが成立しない場合はlabelを作らずhard failureにする。
    """
    if not isinstance(decision, ObservedDecision):
        raise TypeError("decision must be an ObservedDecision")

    policy_input = decision.context.input
    require_same_decision_state(policy_input, decision.hidden, decision.actor_seat)

    identity = DecisionRowIdentity(
        source_identity=source_identity,
        seed=seed,
        step_ordinal=decision.step_ordinal,
        decision_ordinal=decision.decision_ordinal,
        actor_seat=int(decision.actor_seat),
    )
    row = build_public_decision_row(
        identity, decision.context, decision.selected_action, split=split
    )
    row_digest = row.row_digest()
    dealer_seat = policy_input.round.dealer_seat

    cells = []
    for offset in RELATIVE_OPPONENT_OFFSETS:
        target_identity = opponent_identity(decision.actor_seat, offset, dealer_seat)
        target_seat = target_identity.seat
        public_state = policy_input.players[int(target_seat)]
        seat_state = decision.hidden.seat_state(target_seat)
        target = build_opponent_target(
            target_identity,
            seat_resolved=True,
            public_riichi=public_state.riichi,
            privileged_riichi_declared=seat_state.riichi_declared,
            concealed_tiles=seat_state.concealed_tiles,
            melds=seat_state.melds,
        )
        cells.append(
            PrivilegedTargetCell(
                row_identity=identity,
                public_row_digest=row_digest,
                dealer_seat=int(dealer_seat),
                identity=target_identity,
                concealed_tiles=seat_state.concealed_tiles,
                melds=seat_state.melds,
                public_riichi_state=public_state.riichi,
                privileged_riichi_declared=seat_state.riichi_declared,
                target=target,
            )
        )

    return DecisionEmission(row=row, cells=tuple(cells))


__all__ = [
    "DecisionEmission",
    "ObservedDecision",
    "emit_decision",
]
