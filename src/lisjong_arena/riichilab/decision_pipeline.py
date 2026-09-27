"""`drive_session()`の受信 / decision分離に使う部品(Issue #418)。

Policy decisionはWebSocketと同じevent loop上で同期実行すると、slow decision
の間keepalive / control frame処理を止める(#416)。`drive_session()`は次の形で
それを避ける。

```text
FrameReader task (event loop)      transport.recv()を止めずに続け、frameを
                                   bounded bufferへ積む
drive loop (event loop)            frameを順番どおりsessionへ渡し、acceptと
                                   送信を行う
decision worker (single thread)    Adapter work(decide / synchronize)を
                                   FIFOで1件ずつ実行する
```

このmoduleはtransport / sessionのsemanticsを持たない部品だけを置く。
順序・discard規則の本体は`transport.drive_session()`にある。

## `request_action.time`の意味と時刻基準

RiichiLabの`request_action.time`は、serverがそのrequestを出した時点からの
相対時間(ms)である。実traceでは`deadline_ms == grace_ms + bank_ms`
(例: `3000 + 15000 = 18000`)であり、`bank_ms`はそのseatの残りbankである。
server deadlineを過ぎたrequestはserverがdefaultし、`action_ack`
`defaulted`を返す。default後に届いたresponseには`stale`が返る。

serverの送信時刻はclientから観測できないため、localの基準はこのclientが
そのframeを`transport.recv()`から受け取った時刻(`ConnectionTiming`の
monotonic clock)とする。`FrameReader`はdecision中もrecvを続けるので、この
時刻はwire到着時刻に近い。ただしserver送信時刻より必ず後であるため、local
cutoffは`RESPONSE_SAFETY_MARGIN_SECONDS`だけ手前に置く。

```text
cutoff = recv時刻 + (deadline_ms、なければgrace_ms + bank_ms) / 1000
         - RESPONSE_SAFETY_MARGIN_SECONDS
```

`time`が無い、または必要なfieldが有限の数値でない場合はlocal cutoffを
持たない。その場合もserverの`defaulted` / `stale` ackによるdiscardは働く。
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

#: server送信からlocal recvまでの遅延とresponse送信の遅延を見込む余裕。
#: local cutoffはserver deadlineよりこれだけ手前になる。
RESPONSE_SAFETY_MARGIN_SECONDS = 0.5

#: drive loopが未処理のまま保持してよいframe数。超過は`DecisionBacklogError`。
#: live requestを待つ間(最長でもそのrequestのcutoffまで)だけ溜まる。
MAX_BUFFERED_FRAMES = 1024

#: Adapter work待ちの`request_action`数。超過は`DecisionBacklogError`。
#: slow decisionの間にserverがdefaultした後続requestが溜まる。その
#: Observationは順番どおり同期する必要があるため捨てられない。
MAX_PENDING_REQUESTS = 64

#: serverがそのrequestをもう受け付けないことを示すack status。
LATE_ACK_STATUSES = frozenset({"stale", "defaulted"})


def response_window_seconds(time_value: object) -> float | None:
    """`request_action.time`からserver deadlineまでの相対秒数を返す。

    `deadline_ms`を優先し、無ければ`grace_ms + bank_ms`を使う。どちらも
    求められない場合は`None`。型の検証自体はsession側の責務であり、ここでは
    有限の数値でない値を「deadline不明」として扱う。
    """
    if not isinstance(time_value, Mapping):
        return None
    deadline_ms = _finite_number(time_value.get("deadline_ms"))
    if deadline_ms is None:
        grace_ms = _finite_number(time_value.get("grace_ms"))
        bank_ms = _finite_number(time_value.get("bank_ms"))
        if grace_ms is None or bank_ms is None:
            return None
        deadline_ms = grace_ms + bank_ms
    return deadline_ms / 1000.0


def response_cutoff(received_at: float, time_value: object) -> float | None:
    """このrequestへresponseを送ってよい最後のlocal時刻。不明なら`None`。"""
    window = response_window_seconds(time_value)
    if window is None:
        return None
    return received_at + window - RESPONSE_SAFETY_MARGIN_SECONDS


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


@dataclass(eq=False, slots=True)
class PendingRequest:
    """受理済みで、Adapter workまたはresponse送信を待つ`request_action`。"""

    request_id: int
    event: Mapping
    received_at: float
    cutoff_at: float | None

    def expired(self, now: float) -> bool:
        return self.cutoff_at is not None and now >= self.cutoff_at


@dataclass(frozen=True, slots=True)
class ReceivedFrame:
    """`FrameReader`が受け取った1 frame、またはrecvの失敗。

    `event`はtext frameを先読みparseできた場合のJSON object。parseできない
    frameはdrive loopが順番どおり`parse_json_event()`でfail closedする。
    """

    received_at: float
    message: str | bytes | None = None
    event: dict | None = None
    failure: Exception | None = None


class FrameReader:
    """`transport.recv()`を続け、frameを順番どおりbounded bufferへ積む。

    decision中もrecvを止めないことで、`websockets`の受信queueを満杯にして
    読み取りを止める(= keepalive PONGも読まれなくなる)ことを避ける。
    frameの解釈・sessionへの適用はしない。例外は次の2つだけ:

    - mode固有terminal eventを読んだら、それ以上recvしない
    - `stale` / `defaulted` ackの`request_id`を`late_ack_ids`へ記録する。
      drive loopはこれで、まだ処理していないackによってもlive requestの
      結果を送らないと判断できる

    recvの失敗(切断を含む)もframeとして積み、drive loopが順番どおり扱う。
    """

    __slots__ = (
        "_transport",
        "_clock",
        "_terminal_event_name",
        "_notify",
        "frames",
        "late_ack_ids",
        "overflowed",
    )

    def __init__(
        self,
        transport: object,
        *,
        clock: Callable[[], float],
        terminal_event_name: str,
        notify: Callable[[], None],
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._terminal_event_name = terminal_event_name
        self._notify = notify
        self.frames: deque[ReceivedFrame] = deque()
        self.late_ack_ids: set[int] = set()
        self.overflowed = False

    async def run(self) -> None:
        try:
            await self._read()
        finally:
            self._notify()

    async def _read(self) -> None:
        while True:
            try:
                message = await self._transport.recv()
            except Exception as error:
                self.frames.append(
                    ReceivedFrame(received_at=self._clock(), failure=error)
                )
                return
            received_at = self._clock()
            if len(self.frames) >= MAX_BUFFERED_FRAMES:
                self.overflowed = True
                return
            event = _peek_event(message)
            self.frames.append(
                ReceivedFrame(received_at=received_at, message=message, event=event)
            )
            self._notify()
            if event is None:
                continue
            event_type = event.get("type")
            if event_type == "action_ack":
                self._note_ack(event)
            elif event_type == self._terminal_event_name:
                return

    def _note_ack(self, event: dict) -> None:
        request_id = event.get("request_id")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            return
        if event.get("status") in LATE_ACK_STATUSES:
            self.late_ack_ids.add(request_id)


def _peek_event(message: object) -> dict | None:
    """text frameをJSON objectとして読めればそのobject、それ以外は`None`。"""
    if not isinstance(message, str):
        return None
    try:
        parsed = json.loads(message)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def wait_for_notification(event: asyncio.Event, timeout: float | None) -> None:
    """`event`がsetされるか`timeout`秒経つまで待つ。"""
    if timeout is not None and timeout <= 0:
        return
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except TimeoutError:
        pass


__all__ = [
    "LATE_ACK_STATUSES",
    "MAX_BUFFERED_FRAMES",
    "MAX_PENDING_REQUESTS",
    "RESPONSE_SAFETY_MARGIN_SECONDS",
    "FrameReader",
    "PendingRequest",
    "ReceivedFrame",
    "response_cutoff",
    "response_window_seconds",
    "wait_for_notification",
]
