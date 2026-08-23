"""1 game x 1 seatへbindされたRiichiLab request_action protocol-facing bridge(Arena-local canonical、Issue #27)。

parsed済みの`request_action`相当dataを受け取り、lisjong `build_decision()`と
`execute_policy()`を再利用して、送信前possible_actions semantic
validationまで完了した送信可能payloadを返す。

```text
RiichiLab request_action
    -> Observation.deserialize_from_base64()
    -> lisjong build_decision()
    -> lisjong execute_policy()
    -> validated canonical InternalAction
    -> lisjong paired mapping.resolve()
    -> original RiichiEnv Action
    -> Arena MJAI response変換
    -> server possible_actionsへの送信前semantic validation
    -> current request_idをbind
    -> send-ready payload
```

WebSocket接続、token、`request_id`のgame内lifecycle管理、timeout
schedulerはこのmoduleの責務ではない。Session lifecycleは
`lisjong_arena.riichilab.session`が担当する。

`Policy` / `DecisionContext` / `InternalAction` / `execute_policy()`の
semantic contractはlisjong-ownedのまま利用するだけであり、ここで
再実装・再検証しない。lisjong Issue #38で確立したcompositionをbehavior-
preservingにArenaへphysical migrationしたものである(Arena Issue #27)。
"""

from collections.abc import Mapping
from dataclasses import dataclass

from lisjong.policy_contract import Policy, Seat, execute_policy
from lisjong.riichienv_adapter import (
    RiichiEnvActionMappingSession,
    SeatMaterializedState,
    build_decision,
    seat_from_player_index,
)

from lisjong_arena.riichilab.adapter_errors import SeatMismatchError
from lisjong_arena.riichilab.mjai_response import build_mjai_response
from lisjong_arena.riichilab.possible_action_validation import (
    validate_against_possible_actions,
)
from lisjong_arena.riichilab.request_action import parse_request_action


@dataclass(frozen=True, slots=True)
class SendReadyResponse:
    """送信前validationを完了した、WebSocketへそのまま渡せるsend-ready payload。

    Arena `Session` / Transportは、この`action`をJSON化してそのまま
    送信できる。tokenやWebSocket state等のtransport固有情報はここへ含めない。
    """

    request_id: int
    action: dict


class RiichiLabSeatAdapter:
    """1 game x 1 seatへ明示的にbindされたRiichiLab request_action protocol bridge runtime。

    `SeatMaterializedState`と`RiichiEnvActionMappingSession`をconstructorで
    1回だけ生成し、以降の`process_request_action()`呼び出しをまたいで
    継続保持する。requestごとに作り直さないことで、連続するObservationの
    event差分をlisjong Adapterが正しく追跡できる。
    """

    __slots__ = ("_self_seat", "_policy", "_tracker", "_mapping_session")

    def __init__(self, self_seat: Seat, policy: Policy) -> None:
        if not isinstance(self_seat, Seat):
            raise TypeError("self_seat must be a Seat")

        self._self_seat = self_seat
        self._policy = policy
        self._tracker = SeatMaterializedState(self_seat)
        self._mapping_session = RiichiEnvActionMappingSession(self_seat)

    @property
    def self_seat(self) -> Seat:
        return self._self_seat

    def process_request_action(self, raw_request_action: Mapping) -> SendReadyResponse:
        """1件の`request_action`を、送信前validation済みのpayloadまで処理する。

        いずれかの段階で失敗した場合はpayloadを返さず、対応する例外を
        送出する。arbitrary fallbackは行わない。
        """
        parsed = parse_request_action(raw_request_action)

        observation_seat = seat_from_player_index(parsed.observation.player_id)
        if observation_seat != self._self_seat:
            raise SeatMismatchError(
                "observation.player_id does not match this adapter's bound seat"
            )

        decision = build_decision(
            self._tracker, parsed.observation, self._mapping_session
        )
        selected = execute_policy(self._policy, decision.context)
        resolved_action = decision.mapping.resolve(selected)

        response = build_mjai_response(resolved_action, selected)

        # 送信payload自体はresolve済みcanonical Actionから構築済みだが、送信
        # 直前にserver possible_actionsへ改めてsemantic再検証する。照合には
        # canonical `InternalAction`ではなく、実際に送ろうとしている
        # `response`を使う(`KakanAction`のようにInternalActionが保持しない
        # 外部semantic情報(元Ponの`consumed`)も落とさずに検証するため)。
        # ここで失敗した場合はresponseを返さない。
        validate_against_possible_actions(response, parsed.possible_actions)

        return SendReadyResponse(request_id=parsed.request_id, action=response)


__all__ = ["RiichiLabSeatAdapter", "SendReadyResponse"]
