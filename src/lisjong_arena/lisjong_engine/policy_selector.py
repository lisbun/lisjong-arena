"""lisjong `Policy`をengine `ActionSelector`として利用するためのArena-side callable。

1回のselector呼び出しにつき、

```text
build PolicyInput
        ↓
build legal InternalActions
        ↓
DecisionContext
        ↓
execute_policy()
        ↓
resolve original descriptor
```

を1回だけ行う。selector自身はgame state、rule state、match lifecycle、
retry、evaluation semanticsを所有しない。decision-local mappingは呼び出し
ごとに新しく構築し、呼び出し間で保持しない。

Policy例外と`PolicyActionValidationError`はsilent fallbackせずそのまま伝播
させる。Arena側でのfallback、automatic action substitution、retry、
`Policy.choose_action()`直接呼び出し、独自legal-action validationは
実装しない。
"""

from collections.abc import Mapping

from lisjong.policy_contract import Policy, execute_policy
from lisjong_engine.driver import ActionSelector
from lisjong_engine.observation import SeatObservation
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.decision import build_decision
from lisjong_arena.lisjong_engine.domain_conversion import seat_from_engine_seat
from lisjong_arena.lisjong_engine.errors import SeatIdentityError

_ENGINE_SEATS = tuple(EngineSeat)


class PolicySeatSelector:
    """1 seat分のlisjong `Policy`をengine `ActionSelector`として提示するcallable。"""

    __slots__ = ("_seat", "_policy")

    def __init__(self, seat: EngineSeat, policy: Policy) -> None:
        if not isinstance(seat, EngineSeat):
            raise TypeError("seat must be a lisjong-engine Seat")
        if not callable(getattr(policy, "choose_action", None)):
            raise TypeError("policy must provide a callable choose_action()")
        self._seat = seat
        self._policy = policy

    @property
    def seat(self) -> EngineSeat:
        return self._seat

    @property
    def policy(self) -> Policy:
        return self._policy

    def __call__(self, observation: SeatObservation, options: object) -> object:
        if not isinstance(observation, SeatObservation):
            raise TypeError("observation must be a lisjong-engine SeatObservation")
        if observation.viewer_seat is not self._seat:
            raise SeatIdentityError(
                "observation viewer seat does not match this selector's seat"
            )

        decision = build_decision(observation, options)
        if decision.context.input.self_seat != seat_from_engine_seat(self._seat):
            raise SeatIdentityError(
                "decision context seat does not match this selector's seat"
            )

        selected = execute_policy(self._policy, decision.context)
        return decision.mapping.resolve(selected)


def build_seat_selectors(policies: object) -> dict[EngineSeat, ActionSelector]:
    """4席分のlisjong `Policy`から、engine `SeatSelectors`を構成する。

    4席すべてのPolicyが存在することをfail closedで検証する。seat rotation、
    matchup protocol等のevaluation semanticsはここへ入れない。
    """
    if not isinstance(policies, Mapping):
        raise TypeError("policies must be a mapping keyed by lisjong-engine Seat")
    if set(policies) != set(_ENGINE_SEATS):
        raise ValueError("policies must contain exactly all four lisjong-engine seats")
    return {seat: PolicySeatSelector(seat, policies[seat]) for seat in _ENGINE_SEATS}
