"""RiichiLab WebSocket接続そのものを扱う最小限のtransport層(Arena-local
canonical, Issue #23)。

pure transport lifecycle state(`session.py`)とWebSocket API自体を分離する。
validation/rankedは同じconnect/receive/send loopを使い、terminal条件だけを
各sessionへ委譲する。

`websockets`はArena自身のdirect dependencyであり(Issue #23)、Policy
契約・`RiichiLabSeatAdapter`へは依存を逆流させない。
"""

from __future__ import annotations

import json
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
from lisjong_arena.riichilab.session import RankedSession, ValidationSession
from lisjong_arena.riichilab.trace import JsonlProtocolTraceWriter

DEFAULT_VALIDATION_URL = "wss://game.riichi.dev/ws/validate"
DEFAULT_RANKED_URL = "wss://game.riichi.dev/ws/ranked"


class TransportClosed(Exception):
    """`Transport.recv()`がconnection close(正常/異常問わず)を検出した場合。

    `drive_session()`側で`UnexpectedDisconnectError`へ変換する
    ための内部signalであり、呼び出し側の公開APIには漏らさない。
    """


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
            raise TransportClosed(str(error)) from error

    async def send(self, message: str) -> None:
        try:
            await self._connection.send(message)
        except websockets.exceptions.ConnectionClosed as error:
            raise TransportClosed(str(error)) from error

    async def close(self) -> None:
        await self._connection.close()


@asynccontextmanager
async def connect_transport(url: str, token: str) -> AsyncIterator[Transport]:
    """`url`へBearer tokenでWebSocket接続し、`Transport`として提供する。

    `token`はAuthorization headerを設定する目的だけに使い、戻り値の
    `Transport`・結果側には一切保持しない。mid-game reconnectは行わない
    (`websockets.connect()`を`async with`のreconnectループとしてではなく、
    1回の接続としてだけ使用する)。
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        connection = await websockets.connect(url, additional_headers=headers)
    except Exception as error:
        raise TransportError(f"failed to connect to {url}") from error

    transport = WebSocketTransport(connection)
    try:
        yield transport
    finally:
        await connection.close()


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
    """
    while not session.is_complete:
        try:
            message = await transport.recv()
        except TransportClosed as error:
            raise UnexpectedDisconnectError(
                "WebSocket connection closed before "
                f"{session.terminal_event_name} was received"
            ) from error

        if isinstance(message, bytes):
            continue

        event = parse_json_event(message)
        if trace is not None:
            trace.record("recv", event.get("type"), event)

        outgoing = session.handle_event(event)
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
            raise TransportError("failed to send action: connection closed") from error


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
