"""RiichiLab WebSocket接続そのものを扱う最小限のtransport層(Arena-local
canonical, Issue #23)。

pure transport lifecycle state(`session.py`)とWebSocket API自体を分離する。
validation/rankedは同じconnect/receive/send loopを使い、terminal条件だけを
各sessionへ委譲する。

`websockets`はArena自身のdirect dependencyであり(Issue #23)、Policy
契約・`RiichiLabSeatAdapter`へは依存を逆流させない。

Issue #411: transport failureはsecret-safeな`TransportDiagnostics`
(phase / operation / close code / server reason classification / 直前の
decision所要時間)を`TransportError.diagnostics`へ持つ。rawな事実は
`drive_session()`が例外へ一時的に付け、tokenを知る唯一の場所である
`connect_transport()`が接続の外へ出す前にsanitizeする。

Issue #416: `connect_transport()`は接続中だけ軽量なtiming probe task
(`transport_timing.run_timing_probe()`)を動かし、event-loop lag、keepalive
latency(公開attribute `latency`)、connection state(公開property `state`)を
bounded `ConnectionTiming`へ記録する。`drive_session()`は各`request_action`の
dequeue / decision / send attemptとackを同じ`ConnectionTiming`へ記録し、
transport failure時にそのsnapshotをraw failure factsへ付ける。probeと記録は
観測専用であり、送受信・Policy・session semanticsを変えない。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import websockets
import websockets.exceptions

from lisjong_arena.riichilab.errors import (
    ProtocolError,
    TransportError,
    UnexpectedDisconnectError,
)
from lisjong_arena.riichilab.session import (
    EVENT_TYPE_ACTION_ACK,
    EVENT_TYPE_REQUEST_ACTION,
    RankedSession,
    ValidationSession,
)
from lisjong_arena.riichilab.trace import JsonlProtocolTraceWriter
from lisjong_arena.riichilab.transport_diagnostics import (
    OPERATION_CONNECT,
    OPERATION_RECV,
    OPERATION_SEND,
    PHASE_BEFORE_START_GAME,
    PHASE_CONNECT,
    PHASE_IN_GAME,
    RawTransportFailure,
    sanitize_transport_failure,
)
from lisjong_arena.riichilab.transport_timing import (
    ConnectionTiming,
    run_timing_probe,
)

DEFAULT_VALIDATION_URL = "wss://game.riichi.dev/ws/validate"
DEFAULT_RANKED_URL = "wss://game.riichi.dev/ws/ranked"


class TransportClosed(Exception):
    """`Transport.recv()`がconnection close(正常/異常問わず)を検出した場合。

    `drive_session()`側で`UnexpectedDisconnectError`へ変換する
    ための内部signalであり、呼び出し側の公開APIには漏らさない。

    close frameが分かる場合はcode / reasonを持つ(Issue #411)。reasonは
    rawなので、sanitizeされるまでlogへ出さない。
    """

    def __init__(
        self,
        message: str,
        *,
        code_received: int | None = None,
        reason_received: str | None = None,
        code_sent: int | None = None,
        reason_sent: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code_received = code_received
        self.reason_received = reason_received
        self.code_sent = code_sent
        self.reason_sent = reason_sent

    @classmethod
    def from_connection_closed(
        cls, error: websockets.exceptions.ConnectionClosed
    ) -> TransportClosed:
        received = error.rcvd
        sent = error.sent
        return cls(
            str(error),
            code_received=None if received is None else received.code,
            reason_received=None if received is None else received.reason,
            code_sent=None if sent is None else sent.code,
            reason_sent=None if sent is None else sent.reason,
        )


class Transport(Protocol):
    """game sessionを駆動するために必要な最小限のWebSocket操作。

    実装はtext/binary frameの生データだけを扱う。JSON parse、binary
    frame ignore、fail closedの判断は`drive_session()`側の
    責務とする。
    """

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


class WebSocketTransport:
    """`websockets`library上の実接続を`Transport` protocolへ適合させる薄いwrapper。

    `timing`はこの接続の`ConnectionTiming`(Issue #416)。
    """

    __slots__ = ("_connection", "timing")

    def __init__(
        self, connection: object, timing: ConnectionTiming | None = None
    ) -> None:
        self._connection = connection
        self.timing = timing

    async def recv(self) -> str | bytes:
        try:
            return await self._connection.recv()
        except websockets.exceptions.ConnectionClosed as error:
            raise TransportClosed.from_connection_closed(error) from error

    async def send(self, message: str) -> None:
        try:
            await self._connection.send(message)
        except websockets.exceptions.ConnectionClosed as error:
            raise TransportClosed.from_connection_closed(error) from error

    async def close(self) -> None:
        await self._connection.close()


@asynccontextmanager
async def connect_transport(url: str, token: str) -> AsyncIterator[Transport]:
    """`url`へBearer tokenでWebSocket接続し、`Transport`として提供する。

    `token`はAuthorization headerを設定する目的だけに使い、戻り値の
    `Transport`・結果側には一切保持しない。mid-game reconnectは行わない
    (`websockets.connect()`を`async with`のreconnectループとしてではなく、
    1回の接続としてだけ使用する)。

    接続の内側から出る`TransportError`のraw failure factsは、ここで`token`を
    使ってsanitizeし`diagnostics`へ置き換える(Issue #411)。
    """
    headers = {"Authorization": f"Bearer {token}"}
    connect_diagnostics = None
    try:
        connection = await websockets.connect(url, additional_headers=headers)
    except Exception as error:
        connect_diagnostics = sanitize_transport_failure(
            _connect_failure(error), (token,)
        )
    if connect_diagnostics is not None:
        # exceptブロックの外でraiseし、raw handshake response(body等)を
        # `__context__`にも残さない。
        failure = TransportError(f"failed to connect to {url}")
        failure.diagnostics = connect_diagnostics
        raise failure

    timing = ConnectionTiming(state_source=lambda: _connection_state(connection))
    transport = WebSocketTransport(connection, timing)
    probe = asyncio.create_task(
        run_timing_probe(
            timing,
            latency_source=lambda: getattr(connection, "latency", None),
            state_source=lambda: _connection_state(connection),
        )
    )
    try:
        yield transport
    except TransportError as error:
        raw = getattr(error, _RAW_FAILURE_ATTRIBUTE, None)
        if isinstance(raw, RawTransportFailure):
            error.diagnostics = sanitize_transport_failure(raw, (token,))
            delattr(error, _RAW_FAILURE_ATTRIBUTE)
        _drop_raw_chain(error)
        raise
    finally:
        try:
            await _stop_probe(probe)
        finally:
            await connection.close()


def _connection_state(connection: object) -> object:
    """公開`state`を読む。無い・読めない場合は`None`(evidenceに残さない)。"""
    return getattr(connection, "state", None)


async def _stop_probe(probe: asyncio.Task) -> None:
    """probeをcancelして終了を待つ。

    probe自身の`CancelledError`は`gather(return_exceptions=True)`が結果として
    受け取る。待っているこのtask自身へのcancel要求は`gather`から
    `CancelledError`として伝播する。
    """
    probe.cancel()
    await asyncio.gather(probe, return_exceptions=True)


def _drop_raw_chain(error: BaseException) -> None:
    """raw close reason等を持つ`__cause__` / `__context__`を接続の外へ出さない。

    `ConnectionClosed` / `TransportClosed`はserverのraw close reasonを
    messageに持つ。transport境界を出る例外はsanitize済み`diagnostics`だけを
    持ち、raw例外chainは切り離す。
    """
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True


#: 例外へ一時的に付けるraw failure factsのattribute名(module外へ出さない)。
_RAW_FAILURE_ATTRIBUTE = "_raw_transport_failure"


def _connect_failure(error: BaseException) -> RawTransportFailure:
    """handshake失敗のraw facts。rejectされたHTTP responseがあればstatusとbody。"""
    http_status: int | None = None
    body: str | None = None
    if isinstance(error, websockets.exceptions.InvalidStatus):
        http_status = error.response.status_code
        raw_body = error.response.body
        if raw_body:
            body = bytes(raw_body).decode("utf-8", errors="replace")
    return RawTransportFailure(
        phase=PHASE_CONNECT,
        operation=OPERATION_CONNECT,
        http_status=http_status,
        server_text=body,
    )


def _server_error_text(event: dict) -> str | None:
    """`type`を持たないserver error message(`{"error": ...}`)のtext。"""
    if "type" in event:
        return None
    message = event.get("error")
    return message if isinstance(message, str) else None


def _session_failure(
    session: ValidationSession | RankedSession,
    operation: str,
    closed: TransportClosed,
    *,
    server_text: str | None,
    last_decision_elapsed_seconds: float | None,
    timing: ConnectionTiming,
) -> RawTransportFailure:
    status = session.status()
    return RawTransportFailure(
        phase=PHASE_BEFORE_START_GAME if status.seat is None else PHASE_IN_GAME,
        operation=operation,
        close_code_received=closed.code_received,
        close_code_sent=closed.code_sent,
        # 明示的なserver error messageを優先し、なければ受信したclose reason。
        server_text=server_text if server_text is not None else closed.reason_received,
        local_close_reason=closed.reason_sent,
        requests_received=status.requests_received,
        last_decision_elapsed_seconds=last_decision_elapsed_seconds,
        timing=timing.evidence(),
    )


@asynccontextmanager
async def connect_validation_transport(
    url: str, token: str
) -> AsyncIterator[Transport]:
    """validation connector APIを維持するwrapper。"""
    async with connect_transport(url, token) as transport:
        yield transport


@asynccontextmanager
async def connect_ranked_transport(url: str, token: str) -> AsyncIterator[Transport]:
    """ranked endpointへ1回だけ接続するwrapper。join payloadは送らない。"""
    async with connect_transport(url, token) as transport:
        yield transport


def parse_json_event(message: str) -> dict:
    """text frameをJSON top-level objectとしてparseする。fail closed。"""
    try:
        parsed = json.loads(message)
    except (TypeError, ValueError) as error:
        raise ProtocolError("received text frame is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ProtocolError("received JSON is not a top-level object")
    return parsed


async def drive_session(
    session: ValidationSession | RankedSession,
    transport: Transport,
    *,
    trace: JsonlProtocolTraceWriter | None = None,
) -> None:
    """mode固有terminal eventまで受信し、`session`を進行させる。

    - binary frameはprotocol failureとしてclient全体を落とさずignoreする
    - text frameはJSON objectとしてparseし、parse不能・非objectは
      fail closedする
    - unexpected disconnectは`UnexpectedDisconnectError`として成功扱い
      しない。mid-game reconnectは行わない
    - `trace`が渡された場合(default None・opt-in)、recv eventは
      `session.handle_event()`より前に記録する。これにより unknown eventや、
      malformed known eventが`ProtocolError`になる直前のeventも残る。
      send actionはJSON serializationに成功した後・実`transport.send()`の前に
      記録する。そのため送信recordは「送信を試みた」ことを表し、「相手へ届いた」
      ことは保証しない(実sendが失敗した場合もrecordは残る)
    - transport failureにはraw failure facts(phase / operation / close
      code・reason / `type`なしserver error message / 直前の`request_action`
      処理時間)を付ける(Issue #411)。server error messageは、failure直前に
      受信したframeがそれだった場合だけ使う。`type`なしeventはこれまでどおり
      sessionへ渡し、session semanticsは変えない。raw factsと例外chainは
      `connect_transport()`がsanitize・切り離してから外へ出す
    - `request_action`のdequeue / decision / send attempt時刻とackを、
      transportの`ConnectionTiming`(無ければこの呼び出し専用のもの)へ記録し、
      transport failure時にそのsnapshotを付ける(Issue #416)
    """
    timing = _transport_timing(transport)
    server_text: str | None = None
    last_decision_elapsed_seconds: float | None = None
    while not session.is_complete:
        try:
            message = await transport.recv()
        except TransportClosed as error:
            disconnect = UnexpectedDisconnectError(
                "WebSocket connection closed before "
                f"{session.terminal_event_name} was received"
            )
            setattr(
                disconnect,
                _RAW_FAILURE_ATTRIBUTE,
                _session_failure(
                    session,
                    OPERATION_RECV,
                    error,
                    server_text=server_text,
                    last_decision_elapsed_seconds=last_decision_elapsed_seconds,
                    timing=timing,
                ),
            )
            raise disconnect from error
        received_at = timing.now()

        # server reasonは直前に受信したframeがserver error messageだった場合
        # だけ次のfailureへ結びつける(以後のframeで古いreasonを持ち越さない)。
        server_text = None
        if isinstance(message, bytes):
            continue

        event = parse_json_event(message)
        if trace is not None:
            trace.record("recv", event.get("type"), event)

        server_text = _server_error_text(event)

        event_type = event.get("type")
        decision = None
        decision_started = None
        if event_type == EVENT_TYPE_REQUEST_ACTION:
            decision_started = _decision_clock()
            decision_started_at = timing.now()
        outgoing = session.handle_event(event)
        if decision_started is not None:
            last_decision_elapsed_seconds = _decision_clock() - decision_started
            decision = timing.record_decision(
                request_id=event.get("request_id"),
                time_budget=event.get("time"),
                recv_at=received_at,
                start_at=decision_started_at,
                end_at=timing.now(),
            )
        elif event_type == EVENT_TYPE_ACTION_ACK:
            timing.record_ack(event.get("request_id"), event.get("status"))
        if outgoing is None:
            continue

        try:
            outgoing_text = json.dumps(outgoing)
        except (TypeError, ValueError) as error:
            raise ProtocolError("failed to serialize outgoing action") from error

        if trace is not None:
            trace.record("send", outgoing.get("type"), outgoing)

        if decision is not None:
            timing.record_send_attempt(decision, timing.now())
        try:
            await transport.send(outgoing_text)
        except TransportClosed as error:
            send_failure = TransportError("failed to send action: connection closed")
            setattr(
                send_failure,
                _RAW_FAILURE_ATTRIBUTE,
                _session_failure(
                    session,
                    OPERATION_SEND,
                    error,
                    server_text=server_text,
                    last_decision_elapsed_seconds=last_decision_elapsed_seconds,
                    timing=timing,
                ),
            )
            raise send_failure from error


def _transport_timing(transport: Transport) -> ConnectionTiming:
    """実接続の`ConnectionTiming`。持たないtransport(test double等)には新規作成。"""
    timing = getattr(transport, "timing", None)
    return timing if isinstance(timing, ConnectionTiming) else ConnectionTiming()


def _decision_clock() -> float:
    """`request_action`処理時間の計測用clock(testから差し替え可能)。"""
    return time.perf_counter()


async def drive_validation_session(
    session: ValidationSession,
    transport: Transport,
    *,
    trace: JsonlProtocolTraceWriter | None = None,
) -> None:
    """validation driver APIを維持するwrapper。"""
    await drive_session(session, transport, trace=trace)


async def drive_ranked_session(
    session: RankedSession,
    transport: Transport,
    *,
    trace: JsonlProtocolTraceWriter | None = None,
) -> None:
    """`end_game`まで1 ranked hanchanを駆動する。"""
    await drive_session(session, transport, trace=trace)


__all__ = [
    "DEFAULT_RANKED_URL",
    "DEFAULT_VALIDATION_URL",
    "Transport",
    "TransportClosed",
    "WebSocketTransport",
    "connect_ranked_transport",
    "connect_transport",
    "connect_validation_transport",
    "drive_ranked_session",
    "drive_session",
    "drive_validation_session",
    "parse_json_event",
]
