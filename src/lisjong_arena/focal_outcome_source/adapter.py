"""Generation-only focal Policy adapter（#359、#79 A4）。

lisjongの``select_residual_exploration``はpure functionで、serving ``Policy``を
実装しない。既存``LocalGameRunner``は各seatにPolicy instanceを要求するため、
source生成のfocal seatにだけこの小さなadapterを置く。

```text
DecisionContextを1回受け取る
  -> current focal_decision_ordinalからtokenを導出
  -> select_residual_exploration(decision, token)を1回呼ぶ
  -> selected canonical legal actionを返す
  -> 同じselector resultをdecision / ordinal / tokenと一緒にcapture
  -> ordinalを1増やす
```

- serving用のPolicyではない。analysis capabilityも持たないため、captureした
  監査値は``DecisionTrace`` / ``GameTrace``へ混入しない
- 1 game / 1 focal seatごとにfresh instanceを作る。可変の探索stateは
  ordinalだけで、captureはsource writerへ渡す監査記録である
- ordinalはこのadapterが実際に受け取った``DecisionContext``の数であり、
  O0 guard、single-survivor、およびRiichiEnv / Adapterが``DecisionContext``として
  surfaceしたforced tsumogiriを含む。surfaceされないものは数えない
"""

from dataclasses import dataclass

from lisjong.learning import ResidualExplorationDecision, select_residual_exploration
from lisjong.policy_contract import DecisionContext, InternalAction, Seat

from .exploration_token import exploration_token


class FocalAdapterError(Exception):
    """focal adapterが受け取ったdecisionまたはselector結果が契約に反する場合。"""


@dataclass(frozen=True, slots=True)
class FocalSelectionCapture:
    """1 focal decisionについて、ordinal / token / selector結果を束ねた監査値。"""

    focal_decision_ordinal: int
    exploration_token: str
    decision: DecisionContext
    selection: ResidualExplorationDecision


class FocalExplorationPolicy:
    """focal seatだけで使うgeneration-only Policy adapter。"""

    __slots__ = ("_captures", "_focal_seat", "_game_seed", "_selector")

    def __init__(self, *, game_seed: int, focal_seat: Seat, selector=None) -> None:
        if type(game_seed) is not int or game_seed < 0:
            raise TypeError("game_seed must be a non-negative int")
        if not isinstance(focal_seat, Seat):
            raise TypeError("focal_seat must be a Seat")
        self._game_seed = game_seed
        self._focal_seat = focal_seat
        # testでselectorの呼び出し回数を観測するためだけのseam。productionは
        # 常にlisjongのselect_residual_explorationを使う。
        self._selector = select_residual_exploration if selector is None else selector
        self._captures: list[FocalSelectionCapture] = []

    @property
    def focal_seat(self) -> Seat:
        return self._focal_seat

    @property
    def captures(self) -> tuple[FocalSelectionCapture, ...]:
        return tuple(self._captures)

    def choose_action(self, decision: DecisionContext) -> InternalAction:
        if not isinstance(decision, DecisionContext):
            raise FocalAdapterError("decision must be a DecisionContext")
        if decision.input.self_seat != self._focal_seat:
            raise FocalAdapterError("focal adapter received another seat's decision")
        ordinal = len(self._captures)
        token = exploration_token(
            game_seed=self._game_seed,
            focal_seat=int(self._focal_seat),
            focal_decision_ordinal=ordinal,
        )
        selection = self._selector(decision, token)
        if not isinstance(selection, ResidualExplorationDecision):
            raise FocalAdapterError(
                "selector must return a ResidualExplorationDecision"
            )
        if not any(selection.action is legal for legal in decision.legal_actions):
            raise FocalAdapterError(
                "selector action is not a canonical legal action object"
            )
        self._captures.append(
            FocalSelectionCapture(
                focal_decision_ordinal=ordinal,
                exploration_token=token,
                decision=decision,
                selection=selection,
            )
        )
        return selection.action


__all__ = [
    "FocalAdapterError",
    "FocalExplorationPolicy",
    "FocalSelectionCapture",
]
