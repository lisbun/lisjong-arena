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
"""

from __future__ import annotations

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
    """`websockets`library上の実接続を`Transport` protocolへ適合させる薄いwrapper。"""

    __slots__ = ("_connection",)

    def __init__(self, connection: object) -> None:
        self._connection = connection

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
    try:
        connection = await websockets.connect(url, additional_headers=headers)
    except Exception as error:
        failure = TransportError(f"failed to connect to {url}")
        failure.diagnostics = sanitize_transport_failure(
            _connect_failure(error), (token,)
        )
        raise failure from error

    transport = WebSocketTransport(connection)
    try:
        yield transport
    except TransportError as error:
        raw = getattr(error, _RAW_FAILURE_ATTRIBUTE, None)
        if isinstance(raw, RawTransportFailure):
            error.diagnostics = sanitize_transport_failure(raw, (token,))
            delattr(error, _RAW_FAILURE_ATTRIBUTE)
        raise
    finally:
        await connection.close()


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
      処理時間)を付ける(Issue #411)。`type`なしeventはこれまでどおり
      sessionへ渡し、session semanticsは変えない
    """
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
                ),
            )
            raise disconnect from error

        if isinstance(message, bytes):
            continue

        event = parse_json_event(message)
        if trace is not None:
            trace.record("recv", event.get("type"), event)

        error_text = _server_error_text(event)
        if error_text is not None:
            server_text = error_text

        decision_started = (
            _decision_clock()
            if event.get("type") == EVENT_TYPE_REQUEST_ACTION
            else None
        )
        outgoing = session.handle_event(event)
        if decision_started is not None:
            last_decision_elapsed_seconds = _decision_clock() - decision_started
        if outgoing is None:
            continue

        try:
            outgoing_text = json.dumps(outgoing)
        except (TypeError, ValueError) as error:
            raise ProtocolError("failed to serialize outgoing action") from error

        if trace is not None:
            trace.record("send", outgoing.get("type"), outgoing)

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
                ),
            )
            raise send_failure from error


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
