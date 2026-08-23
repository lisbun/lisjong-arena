"""RiichiLab validation/ranked WebSocket lower-level runtime session
(Arena-local canonical, Issue #23)。

`docs/riichilab-client.md`「責務境界」を実装する。RiichiLab
`/ws/validate` / `/ws/ranked`とのtransport lifecycle(接続、
`start_game` / `request_action` / `action_ack` / `validation_result` /
`end_game`、`request_id`のgame内lifecycle管理)だけを担当し、Policy判断・
Observation変換・Action mapping・`possible_actions` semantic validationは
Arena-local `RiichiLabSeatAdapter`(`lisjong_arena.riichilab.adapter`、
Issue #27)をconsumerとして利用する。

`Policy` / `InternalAction`の物理実装とAI-side semantic contractはlisjongに
残る(Arena Issue #27 non-goals)。Adapterから送出された例外はwrapせず、
そのまま伝播させる。

lisjong Issue #38/#39で確立したprotocol-facing bridgeのcontractをbehavior-
preservingにArenaへcanonical physical migrationしたものである(Arena Issue
#23、Issue #27)。
"""

from __future__ import annotations

from collections.abc import Mapping

from lisjong.policy_contract import Policy, Seat

from lisjong_arena.riichilab.adapter import RiichiLabSeatAdapter
from lisjong_arena.riichilab.errors import ProtocolError

_VALIDATION_SEAT = Seat.SEAT_0

_EVENT_TYPE_START_GAME = "start_game"
_EVENT_TYPE_REQUEST_ACTION = "request_action"
_EVENT_TYPE_ACTION_ACK = "action_ack"
_EVENT_TYPE_VALIDATION_RESULT = "validation_result"
_EVENT_TYPE_END_GAME = "end_game"

_KNOWN_ACK_STATUSES = frozenset(
    {"accepted", "rejected", "unparseable", "stale", "defaulted"}
)
_FATAL_ACK_STATUSES = frozenset({"rejected", "unparseable"})
_TIME_BUDGET_FIELDS = ("grace_ms", "bank_ms", "deadline_ms")


class SessionStatus:
    """呼び出し側が確認できる、game lifecycleの現在状態のsnapshot。

    token、raw Observation、raw `request_action`全文等のsecretを含み得る
    transport dataは保持しない。呼び出し時点でSession内部mutable stateから
    切り離されたsnapshotであり、取得後のSession側の変更でこのobjectの内容が
    変化することはない。
    """

    __slots__ = (
        "seat",
        "passed",
        "validation_result_received",
        "end_game_received",
        "failure_reason",
        "requests_received",
        "responses_sent",
        "ack_history",
        "scores",
    )

    def __init__(
        self,
        *,
        seat: Seat | None,
        passed: bool | None,
        validation_result_received: bool,
        end_game_received: bool,
        failure_reason: str | None,
        requests_received: int,
        responses_sent: int,
        ack_history: Mapping[int, tuple[str, ...]],
        scores: tuple[int, int, int, int] | None,
    ) -> None:
        self.seat = seat
        self.passed = passed
        self.validation_result_received = validation_result_received
        self.end_game_received = end_game_received
        self.failure_reason = failure_reason
        self.requests_received = requests_received
        self.responses_sent = responses_sent
        self.ack_history = ack_history
        self.scores = scores


def _validate_time_metadata(time_value: object) -> None:
    """`request_action.time`をtransport metadataとして最小限型検証する。"""
    if time_value is None:
        return
    if not isinstance(time_value, Mapping):
        raise ProtocolError("request_action time metadata must be a mapping")
    for field_name in _TIME_BUDGET_FIELDS:
        if field_name not in time_value:
            continue
        value = time_value[field_name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError(f"request_action time.{field_name} must be numeric")


def _describe_end_game_scores_shape(event: Mapping, scores: object) -> str:
    """値を含めず、`end_game`のscores schema調査に必要なshapeだけを返す。"""
    event_keys = sorted(event.keys())
    scores_length = len(scores) if isinstance(scores, list) else None
    return (
        f"event_keys={event_keys!r}; "
        f"scores_type={type(scores).__name__}; "
        f"scores_length={scores_length!r}"
    )


class _GameSession:
    """validation/rankedが共有する1 game分のtransport lifecycle。"""

    __slots__ = (
        "_policy",
        "_adapter",
        "_seat",
        "_accepted_request_ids",
        "_last_accepted_request_id",
        "_sent_request_ids",
        "_ack_history",
        "_requests_received",
        "_responses_sent",
        "_end_game_received",
        "_scores",
    )

    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self._adapter: RiichiLabSeatAdapter | None = None
        self._seat: Seat | None = None
        self._accepted_request_ids: set[int] = set()
        self._last_accepted_request_id: int | None = None
        self._sent_request_ids: set[int] = set()
        self._ack_history: dict[int, list[str]] = {}
        self._requests_received = 0
        self._responses_sent = 0
        self._end_game_received = False
        self._scores: tuple[int, int, int, int] | None = None

    @property
    def is_complete(self) -> bool:
        raise NotImplementedError

    @property
    def terminal_event_name(self) -> str:
        raise NotImplementedError

    def _validation_fields(self) -> tuple[bool | None, bool, str | None]:
        return None, False, None

    def status(self) -> SessionStatus:
        passed, validation_result_received, failure_reason = self._validation_fields()
        return SessionStatus(
            seat=self._seat,
            passed=passed,
            validation_result_received=validation_result_received,
            end_game_received=self._end_game_received,
            failure_reason=failure_reason,
            requests_received=self._requests_received,
            responses_sent=self._responses_sent,
            ack_history={
                request_id: tuple(statuses)
                for request_id, statuses in self._ack_history.items()
            },
            scores=self._scores,
        )

    def handle_event(self, event: Mapping) -> dict | None:
        """1件のparsed済みJSON eventをdispatchする。"""
        if not isinstance(event, Mapping):
            raise ProtocolError("event must be a JSON object")

        event_type = event.get("type")
        if event_type == _EVENT_TYPE_START_GAME:
            self._handle_start_game(event)
            return None
        if event_type == _EVENT_TYPE_REQUEST_ACTION:
            return self._handle_request_action(event)
        if event_type == _EVENT_TYPE_ACTION_ACK:
            self._handle_action_ack(event)
            return None
        if event_type == _EVENT_TYPE_VALIDATION_RESULT:
            self._handle_validation_result(event)
            return None
        if event_type == _EVENT_TYPE_END_GAME:
            self._handle_end_game(event)
            return None
        return None

    def _accept_bound_seat(self, seat: Seat) -> None:
        """mode固有のseat制約を検証する。rankedは0..3をすべて受理する。"""

    def _handle_start_game(self, event: Mapping) -> None:
        seat_value = event.get("id")
        if isinstance(seat_value, bool) or not isinstance(seat_value, int):
            raise ProtocolError("start_game is missing a valid integer id")
        if seat_value not in (0, 1, 2, 3):
            raise ProtocolError(f"start_game id out of range: {seat_value!r}")
        seat = Seat(seat_value)

        if self._adapter is not None:
            if seat != self._seat:
                raise ProtocolError(
                    "duplicate start_game reported a different seat than the "
                    "already-bound adapter"
                )
            return

        self._accept_bound_seat(seat)
        self._seat = seat
        self._adapter = RiichiLabSeatAdapter(self_seat=seat, policy=self._policy)

    def _handle_request_action(self, event: Mapping) -> dict:
        if self._adapter is None:
            raise ProtocolError("request_action received before start_game")

        request_id = event.get("request_id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise ProtocolError("request_action is missing a valid integer request_id")
        if request_id in self._accepted_request_ids:
            raise ProtocolError(f"duplicate request_id: {request_id!r}")
        if (
            self._last_accepted_request_id is not None
            and request_id <= self._last_accepted_request_id
        ):
            raise ProtocolError(
                f"request_id did not increase monotonically: {request_id!r} "
                f"<= {self._last_accepted_request_id!r}"
            )

        _validate_time_metadata(event.get("time"))
        self._accepted_request_ids.add(request_id)
        self._last_accepted_request_id = request_id
        self._requests_received += 1

        # Policy判断・Action mapping・送信前validationはAdapterへ委譲する。
        response = self._adapter.process_request_action(event)
        if response.request_id != request_id:
            raise ProtocolError(
                "adapter response request_id does not match the current request"
            )
        if request_id != self._last_accepted_request_id:
            raise ProtocolError("response is no longer bound to the current request")
        if request_id in self._sent_request_ids:
            raise ProtocolError(f"request_id already sent a response: {request_id!r}")

        self._sent_request_ids.add(request_id)
        self._responses_sent += 1
        outgoing = dict(response.action)
        outgoing["request_id"] = response.request_id
        return outgoing

    def _handle_action_ack(self, event: Mapping) -> None:
        request_id = event.get("request_id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise ProtocolError("action_ack is missing a valid integer request_id")
        status = event.get("status")
        if not isinstance(status, str) or status not in _KNOWN_ACK_STATUSES:
            raise ProtocolError(
                f"action_ack has an unknown or invalid status: {status!r}"
            )
        if request_id not in self._accepted_request_ids:
            raise ProtocolError(
                f"action_ack references an unknown or future request_id: {request_id!r}"
            )

        self._ack_history.setdefault(request_id, []).append(status)
        if status in _FATAL_ACK_STATUSES:
            raise ProtocolError(f"action_ack reported a fatal status: {status!r}")

    def _handle_validation_result(self, event: Mapping) -> None:
        # rankedではmode外eventとしてforward-compatibleにignoreする。
        return None

    def _handle_end_game(self, event: Mapping) -> None:
        self._end_game_received = True


class ValidationSession(_GameSession):
    """1 validation game分のtransport lifecycle stateを所有する。"""

    __slots__ = (
        "_validation_result_received",
        "_passed",
        "_failure_reason",
    )

    def __init__(self, policy: Policy) -> None:
        super().__init__(policy)
        self._validation_result_received = False
        self._passed: bool | None = None
        self._failure_reason: str | None = None

    @property
    def is_complete(self) -> bool:
        return self._validation_result_received

    @property
    def terminal_event_name(self) -> str:
        return _EVENT_TYPE_VALIDATION_RESULT

    @property
    def validation_result_received(self) -> bool:
        return self._validation_result_received

    def _validation_fields(self) -> tuple[bool | None, bool, str | None]:
        return (
            self._passed,
            self._validation_result_received,
            self._failure_reason,
        )

    def _accept_bound_seat(self, seat: Seat) -> None:
        if seat != _VALIDATION_SEAT:
            raise ProtocolError(
                f"validation requires seat {int(_VALIDATION_SEAT)}, got {int(seat)!r}"
            )

    def _handle_validation_result(self, event: Mapping) -> None:
        passed = event.get("passed")
        if not isinstance(passed, bool):
            raise ProtocolError("validation_result is missing a valid boolean passed")
        reason = event.get("reason")
        if reason is None:
            reason = event.get("message")
        if reason is not None and not isinstance(reason, str):
            raise ProtocolError("validation_result reason/message must be a string")

        self._validation_result_received = True
        self._passed = passed
        self._failure_reason = reason


class RankedSession(_GameSession):
    """1 ranked hanchan分のtransport lifecycle stateを所有する。

    `start_game.id`の0..3をすべて受理し、`end_game`をterminal eventとする。
    自動requeue・次game・reconnectはこのsessionの責務に含めない。
    """

    __slots__ = ()

    @property
    def is_complete(self) -> bool:
        return self._end_game_received

    @property
    def terminal_event_name(self) -> str:
        return _EVENT_TYPE_END_GAME

    def _handle_end_game(self, event: Mapping) -> None:
        if self._adapter is None:
            raise ProtocolError("end_game received before start_game")
        if "scores" in event:
            scores = event["scores"]
            scores_shape = _describe_end_game_scores_shape(event, scores)
            if not isinstance(scores, list) or len(scores) != 4:
                raise ProtocolError(
                    "ranked end_game scores must be a four-item list; " + scores_shape
                )
            if any(
                isinstance(score, bool) or not isinstance(score, int)
                for score in scores
            ):
                raise ProtocolError(
                    "ranked end_game scores must be integers; " + scores_shape
                )

            self._scores = (scores[0], scores[1], scores[2], scores[3])
        super()._handle_end_game(event)


__all__ = ["RankedSession", "SessionStatus", "ValidationSession"]
