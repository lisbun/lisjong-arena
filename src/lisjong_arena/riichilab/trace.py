"""RiichiLab protocol eventをsecret-safeなJSON Linesとして記録するtrace writer
(Arena-local canonical, Issue #23)。

`docs/riichilab-client.md`「protocol trace」を実装する。このmoduleの責務は
JSONL recordの生成、timestamp付与、fileへのappendに限定する。Policy契約、
`DecisionContext`、`RiichiLabSeatAdapter`へtracing責務を持ち込まない。

secret safetyは「credentialを書いてからredactする」のではなく、構造上
credentialがこのmoduleへ渡らないことで担保する。`JsonlProtocolTraceWriter`は
出力先pathだけを受け取り、BOT_TOKENやAuthorization headerを一切引数に
取らない。呼び出し元(`transport.drive_session()`)もtoken/Authorization
情報を持たない`Transport`/session側の値だけをrecordへ渡す。

lisjong Issue #45で確立したcontractをbehavior-preservingにArenaへcanonical
physical migrationしたものである(Arena Issue #23)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from lisjong_arena.riichilab.errors import RiichiLabClientError


class ProtocolTraceError(RiichiLabClientError):
    """protocol traceの書き込み・open・closeに失敗した場合。

    tracingを明示的に有効化した利用者に対して、trace保存の失敗を
    silentに無視せず通知するための専用例外。既存の`ProtocolError` /
    `TransportError`が表す意味(protocol lifecycle違反、transport送受信
    失敗)とは独立させ、trace保存失敗をそれらへ混同させない。
    """


class JsonlProtocolTraceWriter:
    """1 sessionのprotocol eventを1行1JSONとして`path`へ追記するwriter。

    `path`の親directoryが存在しない場合は作成する。書き込み・close失敗を
    silentに無視せず`ProtocolTraceError`として送出する。
    """

    __slots__ = ("_path", "_file")

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8")
        except OSError as error:
            raise ProtocolTraceError(
                f"failed to open protocol trace file: {self._path}"
            ) from error

    def record(self, direction: str, event_type: object, payload: Mapping) -> None:
        """1件のrecv/send protocol event・actionを1行のJSONLとして追記する。

        `payload`は呼び出し側がすでにJSON-serializable(受信済みparsed
        JSON、または送信前serialization済み)であることを保証した
        protocol payloadだけを渡す。
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "event_type": event_type,
            "payload": payload,
        }
        try:
            line = json.dumps(record)
        except (TypeError, ValueError) as error:
            raise ProtocolTraceError(
                "failed to serialize protocol trace record"
            ) from error

        try:
            self._file.write(line + "\n")
            self._file.flush()
        except (OSError, ValueError) as error:
            raise ProtocolTraceError(
                f"failed to write protocol trace record: {self._path}"
            ) from error

    def close(self) -> None:
        try:
            self._file.close()
        except OSError as error:
            raise ProtocolTraceError(
                f"failed to close protocol trace file: {self._path}"
            ) from error


__all__ = ["JsonlProtocolTraceWriter", "ProtocolTraceError"]
