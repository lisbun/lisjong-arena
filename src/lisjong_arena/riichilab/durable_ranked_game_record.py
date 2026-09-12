"""RiichiLab rankedでlisjong自身が完走した1半荘のdurable raw record(Issue #168)。

`--trace` / `RIICHILAB_TRACE_PATH`のprotocol traceはdiagnostic / wire-level
evidenceであり、process終了後にresearch raw sourceとして使えることまでは
保証しない。このmoduleは同じ`JsonlProtocolTraceWriter`が書いたwire evidenceを
source of truthとして再利用したまま、

```text
completed ranked hanchan
    -> raw protocol trace
    -> RankedGameResult semantics
    -> provenance / integrity manifest
    -> strict cross-process readback
```

をcompleted bundleとして成立させる。

意味境界は次のとおり固定する。

```text
protocol trace
    diagnostic / wire-level evidence

durable ranked record
    completed one-hanchan research raw source

training dataset
    downstream consumer-specific artifact

omniscient game log
    提供しない(本recordはlisjong自身が観測したplayer-visible情報だけを持つ)
```

standard RiichiEnv実行の``lisjong_arena.durable_local_game_record``(Issue
#155 / #207)とはschemaもdomain modelも共有しない。versioning、payload digest、
deterministic record identity、fresh staging、strict readback後だけのpublish、
existing targetをsilent overwriteしないというintegrity patternだけを同じ
考え方で適用し、project-wide canonical ``GameRecord``は導入しない。

credential safetyは「書いてからredactする」のではなく、record layerへtokenや
Authorization headerを渡す経路自体を作らないことで担保する。record bundleを
組み立てる関数群はtokenを引数に取らない。
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lisjong.policy_contract.policy import Policy
from lisjong.policy_contract.seat import Seat

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    parse_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.riichilab.ranked import RankedGameResult, run_ranked_game
from lisjong_arena.riichilab.request_action import (
    ParsedRequestAction,
    parse_request_action,
)
from lisjong_arena.riichilab.session import (
    EVENT_TYPE_ACTION_ACK,
    EVENT_TYPE_END_GAME,
    EVENT_TYPE_REQUEST_ACTION,
    EVENT_TYPE_START_GAME,
    FATAL_ACK_STATUSES,
    KNOWN_ACK_STATUSES,
)
from lisjong_arena.riichilab.transport import DEFAULT_RANKED_URL

# provenance introspection(package version / VCS revision / Arena source
# revision)はArenaに1つの実装だけを置く。ここでは``single_round_artifact``の
# 既存seamをそのまま呼び、git / install metadataの読み方を複製しない。ABBB
# artifactと違い本recordはresolveできない値をfail closedにせず、
# ``UNRESOLVED_PROVENANCE_VALUE``として明示する(Issue #168)。
from lisjong_arena.single_round_artifact import (
    SingleRoundArtifactError,
    _arena_source_revision,
    _package_version,
    _vcs_revision,
)

RANKED_GAME_RECORD_SCHEMA_ID = "lisjong-arena-riichilab-durable-ranked-game-record"
RANKED_GAME_RECORD_SCHEMA_VERSION = 1
RANKED_GAME_RECORD_EXECUTION_BACKEND = "riichilab-ranked"
RANKED_GAME_RECORD_MODE = "ranked"
RANKED_GAME_RECORD_COMPLETION_STATUS = "completed"
RANKED_GAME_RECORD_EXECUTION_ENVIRONMENT = "riichilab-ranked"

UNRESOLVED_PROVENANCE_VALUE = "unresolved"

MANIFEST_FILENAME = "manifest.json"
PROTOCOL_TRACE_FILENAME = "protocol.jsonl"
RESULT_FILENAME = "result.json"

_EXPECTED_FILES = frozenset(
    {MANIFEST_FILENAME, PROTOCOL_TRACE_FILENAME, RESULT_FILENAME}
)
_PAYLOAD_NAMES = ("protocol_trace", "result")
_PAYLOAD_FILENAMES = {
    "protocol_trace": PROTOCOL_TRACE_FILENAME,
    "result": RESULT_FILENAME,
}

_SHA256_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")

_DIRECTION_RECV = "recv"
_DIRECTION_SEND = "send"
_DIRECTIONS = frozenset({_DIRECTION_RECV, _DIRECTION_SEND})

_TRACE_ENTRY_KEYS = {"direction", "event_type", "payload", "timestamp"}
_PAYLOAD_REFERENCE_KEYS = {"byte_count", "filename", "sha256"}
_RESULT_KEYS = {
    "ack_history",
    "end_game_received",
    "requests_received",
    "responses_sent",
    "scores",
    "seat",
}
_ACK_HISTORY_ENTRY_KEYS = {"request_id", "statuses"}
_MANIFEST_KEYS = {
    "bound_seat",
    "completion_status",
    "execution_backend",
    "mode",
    "payloads",
    "provenance",
    "record_identity",
    "schema_id",
    "schema_version",
}
_PROVENANCE_KEYS = {
    "execution_environment",
    "lisjong_arena_revision",
    "lisjong_arena_version",
    "lisjong_engine_revision",
    "lisjong_engine_version",
    "lisjong_revision",
    "lisjong_version",
    "policy_identity",
    "profile_identity",
    "python_implementation",
    "python_version",
    "riichienv_version",
}


class DurableRankedGameRecordError(ArtifactValidationError):
    """durable ranked recordを生成・検証・readbackできない場合。"""


@dataclass(frozen=True, slots=True)
class RankedRecordPayloadReference:
    """manifestが固定する1 payloadのphysical nameとcontent identity。"""

    filename: str
    sha256: str
    byte_count: int

    def __post_init__(self) -> None:
        if type(self.filename) is not str or not self.filename:
            raise ValueError("filename must be a non-empty str")
        if (
            type(self.sha256) is not str
            or len(self.sha256) != _SHA256_LENGTH
            or not _HEX_DIGITS.issuperset(self.sha256)
        ):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        if type(self.byte_count) is not int:
            raise TypeError("byte_count must be an int")
        if self.byte_count <= 0:
            raise ValueError("byte_count must be positive")


@dataclass(frozen=True, slots=True)
class RankedRecordProvenance:
    """durable ranked record専用のexecution provenance。

    ABBB評価用の``SingleRoundExecutionProvenance``とは別contractである。
    あちらはRiichiEnv evaluation semanticsの再現性のためすべての
    revisionをfull commit IDとして必須にするが、ranked recordは外部
    serviceとのlive対局中に取得するものであり、resolveできない値のために
    completed hanchanのraw dataを捨てない。

    resolveできなかった値は推測せず``UNRESOLVED_PROVENANCE_VALUE``として
    明示する。machine username、absolute local path、credentialはここへ
    含めない(record identityにも含まれない)。
    """

    execution_environment: str
    lisjong_arena_version: str
    lisjong_arena_revision: str
    lisjong_version: str
    lisjong_revision: str
    lisjong_engine_version: str
    lisjong_engine_revision: str
    riichienv_version: str
    python_version: str
    python_implementation: str
    profile_identity: str
    policy_identity: str

    def __post_init__(self) -> None:
        for field_name in _PROVENANCE_KEYS:
            value = getattr(self, field_name)
            if type(value) is not str:
                raise TypeError(f"{field_name} must be a str")
            if not value:
                raise ValueError(f"{field_name} must not be empty")
        if self.execution_environment != RANKED_GAME_RECORD_EXECUTION_ENVIRONMENT:
            raise ValueError(
                f"unsupported execution environment: {self.execution_environment!r}"
            )


@dataclass(frozen=True, slots=True)
class ProtocolTraceEntry:
    """protocol trace 1行分のlossless readback。

    ``payload``は`JsonlProtocolTraceWriter`が記録したraw protocol object
    そのものであり、unknown fieldを含めて落とさない。
    """

    timestamp: str
    direction: str
    event_type: Any
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.timestamp) is not str or not self.timestamp:
            raise ValueError("timestamp must be a non-empty str")
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"unsupported trace direction: {self.direction!r}")
        if type(self.payload) is not dict:
            raise TypeError("payload must be a JSON object")


@dataclass(frozen=True, slots=True)
class RankedDecisionReadback:
    """1 `request_action`をconsumerがそのまま辿れる形へ束ねたreadback。

    ここではPolicyInput tensor、action vocabulary index、reward、Q target、
    BC label、HandBelief labelのいずれへも変換しない。downstream dataset
    builderが必要とするraw factだけを、再解釈せずに並べる。
    """

    request_id: int
    seat: Seat
    observation: Any
    observation_base64: str
    possible_actions: tuple
    time: Any
    request_payload: Mapping[str, object]
    sent_action: Mapping[str, object]
    ack_statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DurableRankedGameRecord:
    """strict loaderが復元・検証したcompleted durable ranked record。"""

    record_identity: str
    bound_seat: Seat
    provenance: RankedRecordProvenance
    result: RankedGameResult
    protocol_entries: tuple[ProtocolTraceEntry, ...]
    payloads: Mapping[str, RankedRecordPayloadReference]

    def __post_init__(self) -> None:
        if (
            type(self.record_identity) is not str
            or len(self.record_identity) != _SHA256_LENGTH
            or not _HEX_DIGITS.issuperset(self.record_identity)
        ):
            raise ValueError("record_identity must be a lowercase SHA-256 digest")
        if not isinstance(self.bound_seat, Seat):
            raise TypeError("bound_seat must be a Seat")
        if not isinstance(self.provenance, RankedRecordProvenance):
            raise TypeError("provenance must be a RankedRecordProvenance")
        if not isinstance(self.result, RankedGameResult):
            raise TypeError("result must be a RankedGameResult")
        if not self.result.end_game_received:
            raise ValueError("a completed record requires a received end_game")
        if self.result.seat != self.bound_seat:
            raise ValueError("result seat does not match the bound seat")
        if any(
            not isinstance(entry, ProtocolTraceEntry) for entry in self.protocol_entries
        ):
            raise TypeError("protocol_entries must contain only ProtocolTraceEntry")
        if not isinstance(self.payloads, Mapping) or set(self.payloads) != set(
            _PAYLOAD_NAMES
        ):
            raise ValueError("payloads must contain the two record payloads")
        if any(
            not isinstance(reference, RankedRecordPayloadReference)
            for reference in self.payloads.values()
        ):
            raise TypeError("payloads must contain only payload references")
        object.__setattr__(self, "protocol_entries", tuple(self.protocol_entries))
        object.__setattr__(self, "payloads", dict(self.payloads))


@dataclass(frozen=True, slots=True)
class RankedGameRecordSummary:
    """consumer utility smoke向けのsmall deterministic summary。"""

    record_identity: str
    seat: int
    requests: int
    responses: int
    acknowledged_requests: int
    deserialized_observations: int
    possible_action_total: int
    scores: tuple[int, int, int, int] | None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(document: dict[str, Any]) -> bytes:
    return canonical_json_text(document).encode("utf-8")


def _construct(factory, context: str, **values):
    try:
        return factory(**values)
    except (TypeError, ValueError) as exc:
        raise DurableRankedGameRecordError(f"{context} is invalid: {exc}") from exc


def _resolved_or_unresolved(resolve) -> str:
    """provenance valueを1つresolveし、確認できない場合だけ`unresolved`にする。"""
    try:
        value = resolve()
    except SingleRoundArtifactError:
        return UNRESOLVED_PROVENANCE_VALUE
    return value if type(value) is str and value else UNRESOLVED_PROVENANCE_VALUE


def collect_ranked_record_provenance(
    *,
    profile_identity: str | None = None,
    policy_identity: str | None = None,
) -> RankedRecordProvenance:
    """実際にresolveできた値だけからranked record provenanceを組み立てる。

    ``profile_identity`` / ``policy_identity``はcaller(CLI / acquisition)が
    知っているidentityであり、未指定なら`unresolved`として記録する。
    credential環境変数の名前・値、machine username、local pathは渡さない。
    """
    return RankedRecordProvenance(
        execution_environment=RANKED_GAME_RECORD_EXECUTION_ENVIRONMENT,
        lisjong_arena_version=_resolved_or_unresolved(
            lambda: _package_version("lisjong-arena")
        ),
        lisjong_arena_revision=_resolved_or_unresolved(_arena_source_revision),
        lisjong_version=_resolved_or_unresolved(lambda: _package_version("lisjong")),
        lisjong_revision=_resolved_or_unresolved(lambda: _vcs_revision("lisjong")),
        lisjong_engine_version=_resolved_or_unresolved(
            lambda: _package_version("lisjong-engine")
        ),
        lisjong_engine_revision=_resolved_or_unresolved(
            lambda: _vcs_revision("lisjong-engine")
        ),
        riichienv_version=_resolved_or_unresolved(
            lambda: _package_version("riichienv")
        ),
        python_version=platform.python_version() or UNRESOLVED_PROVENANCE_VALUE,
        python_implementation=(
            platform.python_implementation() or UNRESOLVED_PROVENANCE_VALUE
        ),
        profile_identity=profile_identity or UNRESOLVED_PROVENANCE_VALUE,
        policy_identity=policy_identity or UNRESOLVED_PROVENANCE_VALUE,
    )


def _provenance_document(provenance: RankedRecordProvenance) -> dict[str, Any]:
    return {
        field_name: getattr(provenance, field_name)
        for field_name in sorted(_PROVENANCE_KEYS)
    }


def _parse_provenance(value: object) -> RankedRecordProvenance:
    raw = expect_object(value, _PROVENANCE_KEYS, "manifest.provenance")
    return _construct(
        RankedRecordProvenance,
        "manifest.provenance",
        **{
            field_name: expect_str(raw[field_name], f"manifest.provenance.{field_name}")
            for field_name in _PROVENANCE_KEYS
        },
    )


def _result_document(result: RankedGameResult) -> dict[str, Any]:
    return {
        "ack_history": [
            {"request_id": request_id, "statuses": list(result.ack_history[request_id])}
            for request_id in sorted(result.ack_history)
        ],
        "end_game_received": result.end_game_received,
        "requests_received": result.requests_received,
        "responses_sent": result.responses_sent,
        "scores": None if result.scores is None else list(result.scores),
        "seat": int(result.seat),
    }


def _parse_seat(value: object, context: str) -> Seat:
    seat_value = expect_int(value, context)
    try:
        return Seat(seat_value)
    except ValueError as exc:
        raise DurableRankedGameRecordError(f"{context} is not a valid seat") from exc


def _parse_scores(value: object, context: str) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    raw = expect_list(value, context)
    if len(raw) != 4:
        raise DurableRankedGameRecordError(f"{context} must contain four scores")
    scores = tuple(
        expect_int(item, f"{context}[{index}]") for index, item in enumerate(raw)
    )
    return scores


def _parse_result(value: object) -> RankedGameResult:
    raw = expect_object(value, _RESULT_KEYS, "result")
    ack_entries = expect_list(raw["ack_history"], "result.ack_history")
    ack_history: dict[int, tuple[str, ...]] = {}
    previous_request_id: int | None = None
    for index, item in enumerate(ack_entries):
        context = f"result.ack_history[{index}]"
        entry = expect_object(item, _ACK_HISTORY_ENTRY_KEYS, context)
        request_id = expect_int(entry["request_id"], f"{context}.request_id")
        if previous_request_id is not None and request_id <= previous_request_id:
            raise DurableRankedGameRecordError(
                "result.ack_history must be ordered by unique request_id"
            )
        previous_request_id = request_id
        statuses = tuple(
            expect_str(status, f"{context}.statuses[{status_index}]")
            for status_index, status in enumerate(
                expect_list(entry["statuses"], f"{context}.statuses")
            )
        )
        if not statuses:
            raise DurableRankedGameRecordError(f"{context}.statuses must not be empty")
        ack_history[request_id] = statuses

    return _construct(
        RankedGameResult,
        "result",
        end_game_received=expect_bool(
            raw["end_game_received"], "result.end_game_received"
        ),
        seat=_parse_seat(raw["seat"], "result.seat"),
        requests_received=expect_int(
            raw["requests_received"], "result.requests_received"
        ),
        responses_sent=expect_int(raw["responses_sent"], "result.responses_sent"),
        ack_history=ack_history,
        scores=_parse_scores(raw["scores"], "result.scores"),
    )


def _payload_reference(filename: str, data: bytes) -> RankedRecordPayloadReference:
    return RankedRecordPayloadReference(
        filename=filename, sha256=_sha256(data), byte_count=len(data)
    )


def _payload_reference_document(
    reference: RankedRecordPayloadReference,
) -> dict[str, object]:
    return {
        "byte_count": reference.byte_count,
        "filename": reference.filename,
        "sha256": reference.sha256,
    }


def _parse_payload_reference(
    value: object, context: str
) -> RankedRecordPayloadReference:
    raw = expect_object(value, _PAYLOAD_REFERENCE_KEYS, context)
    return _construct(
        RankedRecordPayloadReference,
        context,
        filename=expect_str(raw["filename"], f"{context}.filename"),
        sha256=expect_str(raw["sha256"], f"{context}.sha256"),
        byte_count=expect_int(raw["byte_count"], f"{context}.byte_count"),
    )


def _manifest_without_identity(
    *,
    bound_seat: Seat,
    provenance: RankedRecordProvenance,
    payloads: Mapping[str, RankedRecordPayloadReference],
) -> dict[str, Any]:
    """record identityの対象となるsemantic manifest。

    storage path、machine username、timestampは含めない。したがって同じ
    hanchanのbundleを別pathへ置いてもsemantic identityは変わらず、protocol
    payloadやresultが1 byteでも違えばidentityは変わる。
    """
    return {
        "bound_seat": int(bound_seat),
        "completion_status": RANKED_GAME_RECORD_COMPLETION_STATUS,
        "execution_backend": RANKED_GAME_RECORD_EXECUTION_BACKEND,
        "mode": RANKED_GAME_RECORD_MODE,
        "payloads": {
            name: _payload_reference_document(payloads[name]) for name in _PAYLOAD_NAMES
        },
        "provenance": _provenance_document(provenance),
        "schema_id": RANKED_GAME_RECORD_SCHEMA_ID,
        "schema_version": RANKED_GAME_RECORD_SCHEMA_VERSION,
    }


def _record_identity(manifest_without_identity: dict[str, Any]) -> str:
    return _sha256(_canonical_bytes(manifest_without_identity))


def _manifest_document(**kwargs) -> dict[str, Any]:
    value = _manifest_without_identity(**kwargs)
    return {**value, "record_identity": _record_identity(value)}


def _parse_timestamp(value: object, context: str) -> str:
    timestamp = expect_str(value, context)
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise DurableRankedGameRecordError(
            f"{context} is not an ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise DurableRankedGameRecordError(f"{context} must be timezone-aware")
    return timestamp


def _parse_trace_entries(data: bytes) -> tuple[ProtocolTraceEntry, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DurableRankedGameRecordError("protocol trace is not valid UTF-8") from exc
    if not text.endswith("\n"):
        raise DurableRankedGameRecordError("protocol trace is truncated")

    entries: list[ProtocolTraceEntry] = []
    for index, line in enumerate(text.splitlines()):
        context = f"protocol trace line {index + 1}"
        if not line.strip():
            raise DurableRankedGameRecordError(f"{context} is empty")
        try:
            document = parse_json_text(line)
        except ValueError as exc:
            raise DurableRankedGameRecordError(f"{context} is not valid JSON") from exc
        raw = expect_object(document, _TRACE_ENTRY_KEYS, context)
        payload = raw["payload"]
        if type(payload) is not dict:
            raise DurableRankedGameRecordError(f"{context}.payload must be an object")
        event_type = raw["event_type"]
        if event_type != payload.get("type"):
            raise DurableRankedGameRecordError(
                f"{context}.event_type does not match the recorded payload type"
            )
        entries.append(
            _construct(
                ProtocolTraceEntry,
                context,
                timestamp=_parse_timestamp(raw["timestamp"], f"{context}.timestamp"),
                direction=expect_str(raw["direction"], f"{context}.direction"),
                event_type=event_type,
                payload=payload,
            )
        )
    if not entries:
        raise DurableRankedGameRecordError("protocol trace contains no events")
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class _CorrelatedTrace:
    """trace由来のlifecycle facts。resultとの整合はcaller側で照合する。"""

    seat: Seat
    request_ids: tuple[int, ...]
    requests: Mapping[int, Mapping[str, object]]
    responses: Mapping[int, Mapping[str, object]]
    ack_history: Mapping[int, tuple[str, ...]]
    scores: tuple[int, int, int, int] | None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DurableRankedGameRecordError(message)


def _correlate_trace(entries: Sequence[ProtocolTraceEntry]) -> _CorrelatedTrace:
    """`RankedSession` lifecycle semanticsどおりにtraceをcorrelateする。

    現在のprotocolが保証していない仮定(たとえば全requestへ一定個数のackが
    存在する)は追加しない。検証するのは現在のsession実装がgame進行中に
    すでに強制しているlifecycle条件と、completed hanchanのshapeである。
    """
    seat: Seat | None = None
    request_ids: list[int] = []
    requests: dict[int, Mapping[str, object]] = {}
    responses: dict[int, Mapping[str, object]] = {}
    ack_history: dict[int, list[str]] = {}
    scores: tuple[int, int, int, int] | None = None
    last_request_id: int | None = None
    end_game_seen = False

    for index, entry in enumerate(entries):
        context = f"protocol trace line {index + 1}"
        _require(not end_game_seen, "protocol trace continues past end_game")
        payload = entry.payload

        if entry.direction == _DIRECTION_SEND:
            request_id = payload.get("request_id")
            _require(
                not isinstance(request_id, bool) and isinstance(request_id, int),
                f"{context} sent action is missing a valid integer request_id",
            )
            _require(
                request_id in requests,
                f"{context} sent action references an unknown request_id",
            )
            _require(
                request_id == last_request_id,
                f"{context} sent action is not bound to the current request",
            )
            _require(
                request_id not in responses,
                f"{context} sent a second response for one request_id",
            )
            responses[request_id] = payload
            continue

        event_type = payload.get("type")
        if event_type == EVENT_TYPE_START_GAME:
            seat_value = payload.get("id")
            _require(
                not isinstance(seat_value, bool)
                and isinstance(seat_value, int)
                and seat_value in (0, 1, 2, 3),
                f"{context} start_game is missing a valid seat id",
            )
            bound = Seat(seat_value)
            _require(
                seat is None or seat == bound,
                f"{context} start_game reports a different seat than the bound seat",
            )
            seat = bound
        elif event_type == EVENT_TYPE_REQUEST_ACTION:
            _require(seat is not None, f"{context} request_action precedes start_game")
            request_id = payload.get("request_id")
            _require(
                not isinstance(request_id, bool) and isinstance(request_id, int),
                f"{context} request_action is missing a valid integer request_id",
            )
            _require(
                request_id not in requests,
                f"{context} repeats an already-recorded request_id",
            )
            _require(
                last_request_id is None or request_id > last_request_id,
                f"{context} request_id does not increase monotonically",
            )
            possible_actions = payload.get("possible_actions")
            _require(
                isinstance(possible_actions, list),
                f"{context} request_action is missing possible_actions",
            )
            _require(
                isinstance(payload.get("observation"), str),
                f"{context} request_action is missing a base64 observation",
            )
            requests[request_id] = payload
            request_ids.append(request_id)
            last_request_id = request_id
        elif event_type == EVENT_TYPE_ACTION_ACK:
            request_id = payload.get("request_id")
            _require(
                not isinstance(request_id, bool) and isinstance(request_id, int),
                f"{context} action_ack is missing a valid integer request_id",
            )
            _require(
                request_id in requests,
                f"{context} action_ack references an unknown request_id",
            )
            status = payload.get("status")
            _require(
                isinstance(status, str) and status in KNOWN_ACK_STATUSES,
                f"{context} action_ack has an unknown or invalid status",
            )
            _require(
                status not in FATAL_ACK_STATUSES,
                f"{context} action_ack reported a fatal status; "
                "a fatal acknowledgement is never a completed record",
            )
            ack_history.setdefault(request_id, []).append(status)
        elif event_type == EVENT_TYPE_END_GAME:
            _require(seat is not None, f"{context} end_game precedes start_game")
            if "scores" in payload:
                raw_scores = payload["scores"]
                _require(
                    isinstance(raw_scores, list) and len(raw_scores) == 4,
                    f"{context} end_game scores must be a four-item list",
                )
                _require(
                    all(
                        not isinstance(score, bool) and isinstance(score, int)
                        for score in raw_scores
                    ),
                    f"{context} end_game scores must be integers",
                )
                scores = (
                    raw_scores[0],
                    raw_scores[1],
                    raw_scores[2],
                    raw_scores[3],
                )
            end_game_seen = True
        # 未知のevent typeは現在のsessionと同じくforward-compatibleに保持する。

    _require(end_game_seen, "completed record requires a recorded end_game")
    _require(seat is not None, "completed record requires a bound seat")
    _require(
        set(responses) == set(requests),
        "every recorded request_action must have exactly one recorded response",
    )
    return _CorrelatedTrace(
        seat=seat,
        request_ids=tuple(request_ids),
        requests=requests,
        responses=responses,
        ack_history={
            request_id: tuple(statuses) for request_id, statuses in ack_history.items()
        },
        scores=scores,
    )


def _validate_recorded_request_actions(
    correlated: _CorrelatedTrace, bound_seat: Seat
) -> None:
    """recorded `request_action`がcanonical pathで復元できることを検証する。

    completed recordのinvariantとして、existing `parse_request_action()`へ
    すべてのrecorded requestを通す。独自のObservation decoderは持たず、
    復元不能なrecordはcompleted durable recordとして受理しない。

    `observation.player_id`がbound seatと一致することも同時に確認する。これは
    execution時点で`RiichiLabSeatAdapter`が既に強制しているinvariantであり、
    新しいprotocol assumptionではない。

    この検証をloader側へ置くことで、record corruptionをconsumer seam
    (`iter_ranked_decisions()`)で初めて発見する構造にしない。
    """
    for request_id in correlated.request_ids:
        payload = correlated.requests[request_id]
        try:
            parsed = parse_request_action(payload)
        except Exception as exc:
            raise DurableRankedGameRecordError(
                f"recorded request_action {request_id} cannot be deserialized "
                "through the canonical parser"
            ) from exc
        if int(parsed.observation.player_id) != int(bound_seat):
            raise DurableRankedGameRecordError(
                f"recorded request_action {request_id} observation does not "
                "belong to the bound seat"
            )


def _bundle_files(directory: Path) -> None:
    if not directory.is_dir():
        raise DurableRankedGameRecordError("record directory is missing")
    try:
        names = {entry.name for entry in directory.iterdir()}
    except OSError as exc:
        raise DurableRankedGameRecordError("record directory cannot be read") from exc
    if names != _EXPECTED_FILES:
        raise DurableRankedGameRecordError("record contains missing or extra files")
    for name in _EXPECTED_FILES:
        candidate = directory / name
        if candidate.is_symlink() or not candidate.is_file():
            raise DurableRankedGameRecordError(
                "record entries must be regular files inside the bundle"
            )


def _read_payload(directory: Path, reference: RankedRecordPayloadReference) -> bytes:
    path = directory / reference.filename
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DurableRankedGameRecordError(
            f"{reference.filename} cannot be read"
        ) from exc
    if len(data) != reference.byte_count:
        raise DurableRankedGameRecordError(f"{reference.filename} byte count mismatch")
    if _sha256(data) != reference.sha256:
        raise DurableRankedGameRecordError(f"{reference.filename} digest mismatch")
    return data


def _load_ranked_game_record(path: str | Path) -> DurableRankedGameRecord:
    directory = Path(path)
    _bundle_files(directory)

    manifest_document = read_json_document(directory / MANIFEST_FILENAME)
    manifest = expect_object(manifest_document, _MANIFEST_KEYS, "manifest")
    if (
        expect_str(manifest["schema_id"], "manifest.schema_id")
        != RANKED_GAME_RECORD_SCHEMA_ID
    ):
        raise DurableRankedGameRecordError("unsupported record schema id")
    if (
        expect_int(manifest["schema_version"], "manifest.schema_version")
        != RANKED_GAME_RECORD_SCHEMA_VERSION
    ):
        raise DurableRankedGameRecordError("unsupported record schema version")
    if (
        expect_str(manifest["execution_backend"], "manifest.execution_backend")
        != RANKED_GAME_RECORD_EXECUTION_BACKEND
    ):
        raise DurableRankedGameRecordError("unsupported execution backend")
    if expect_str(manifest["mode"], "manifest.mode") != RANKED_GAME_RECORD_MODE:
        raise DurableRankedGameRecordError("unsupported execution mode")
    if (
        expect_str(manifest["completion_status"], "manifest.completion_status")
        != RANKED_GAME_RECORD_COMPLETION_STATUS
    ):
        raise DurableRankedGameRecordError("only completed ranked records are readable")

    bound_seat = _parse_seat(manifest["bound_seat"], "manifest.bound_seat")
    provenance = _parse_provenance(manifest["provenance"])

    payloads_raw = expect_object(
        manifest["payloads"], set(_PAYLOAD_NAMES), "manifest.payloads"
    )
    payloads = {
        name: _parse_payload_reference(payloads_raw[name], f"manifest.payloads.{name}")
        for name in _PAYLOAD_NAMES
    }
    if any(
        payloads[name].filename != _PAYLOAD_FILENAMES[name] for name in _PAYLOAD_NAMES
    ):
        raise DurableRankedGameRecordError("manifest payload filenames are invalid")

    recorded_identity = expect_str(
        manifest["record_identity"], "manifest.record_identity"
    )
    without_identity = dict(manifest)
    without_identity.pop("record_identity")
    if recorded_identity != _record_identity(without_identity):
        raise DurableRankedGameRecordError("record identity mismatch")

    trace_data = _read_payload(directory, payloads["protocol_trace"])
    result_data = _read_payload(directory, payloads["result"])

    entries = _parse_trace_entries(trace_data)
    try:
        result_document = parse_json_text(result_data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DurableRankedGameRecordError("result payload is not valid JSON") from exc
    result = _parse_result(result_document)

    if not result.end_game_received:
        raise DurableRankedGameRecordError(
            "a completed record requires a received end_game"
        )
    correlated = _correlate_trace(entries)
    if correlated.seat != bound_seat or result.seat != bound_seat:
        raise DurableRankedGameRecordError(
            "manifest, result and protocol trace do not agree on the bound seat"
        )
    _validate_recorded_request_actions(correlated, bound_seat)
    if len(correlated.requests) != result.requests_received:
        raise DurableRankedGameRecordError(
            "recorded request count does not match the ranked result"
        )
    if len(correlated.responses) != result.responses_sent:
        raise DurableRankedGameRecordError(
            "recorded response count does not match the ranked result"
        )
    if dict(correlated.ack_history) != dict(result.ack_history):
        raise DurableRankedGameRecordError(
            "recorded acknowledgements do not match the ranked result"
        )
    if correlated.scores != result.scores:
        raise DurableRankedGameRecordError(
            "recorded end_game scores do not match the ranked result"
        )

    return _construct(
        DurableRankedGameRecord,
        "record",
        record_identity=recorded_identity,
        bound_seat=bound_seat,
        provenance=provenance,
        result=result,
        protocol_entries=entries,
        payloads=payloads,
    )


def load_ranked_game_record(path: str | Path) -> DurableRankedGameRecord:
    """completed ranked bundleをfail closedに検証して復元する。

    partial / corrupt / tampered / unsupported bundleはcompleted recordと
    して受理しない。
    """
    try:
        return _load_ranked_game_record(path)
    except DurableRankedGameRecordError:
        raise
    except (ArtifactValidationError, TypeError, ValueError) as exc:
        raise DurableRankedGameRecordError(
            "record is malformed or semantically inconsistent"
        ) from exc


def iter_ranked_decisions(
    record: DurableRankedGameRecord,
) -> tuple[RankedDecisionReadback, ...]:
    """1 recordのdecisionをrequest_id順にraw factとして並べる小さなconsumer seam。

    Observation復元にはexisting `parse_request_action()`をそのまま使い、
    独自のObservation decoderを持たない。tensor / label / reward等の
    consumer-specific変換はここでは行わない(downstream dataset builderの責務)。

    completed recordでは同じcanonical deserializeを
    `load_ranked_game_record()`が既にinvariantとして検証済みである。ここは
    その正本parserをconsumer向けに再利用するだけであり、record corruptionを
    最初に発見する場所ではない。
    """
    if not isinstance(record, DurableRankedGameRecord):
        raise TypeError("record must be a DurableRankedGameRecord")
    correlated = _correlate_trace(record.protocol_entries)
    readbacks: list[RankedDecisionReadback] = []
    for request_id in correlated.request_ids:
        request_payload = correlated.requests[request_id]
        try:
            parsed: ParsedRequestAction = parse_request_action(request_payload)
        except Exception as exc:
            raise DurableRankedGameRecordError(
                f"recorded request_action {request_id} cannot be deserialized "
                "through the canonical parser"
            ) from exc
        readbacks.append(
            RankedDecisionReadback(
                request_id=request_id,
                seat=record.bound_seat,
                observation=parsed.observation,
                observation_base64=request_payload["observation"],
                possible_actions=parsed.possible_actions,
                time=parsed.time,
                request_payload=request_payload,
                sent_action=correlated.responses[request_id],
                ack_statuses=record.result.ack_history.get(request_id, ()),
            )
        )
    return tuple(readbacks)


def summarize_ranked_game_record(
    record: DurableRankedGameRecord,
) -> RankedGameRecordSummary:
    """readback pathがraw sourceとして使えることを確認するsmall smoke。"""
    decisions = iter_ranked_decisions(record)
    for decision in decisions:
        # Access自体がconsumer seam。Observationはexisting canonical parserで
        # 復元済みであり、bound seatとの一致もloaderが検証済みである。ここで
        # feature / labelへは変換しない。
        decision.observation.player_id
        decision.possible_actions
        decision.sent_action
        decision.ack_statuses
    return RankedGameRecordSummary(
        record_identity=record.record_identity,
        seat=int(record.bound_seat),
        requests=record.result.requests_received,
        responses=record.result.responses_sent,
        acknowledged_requests=len(record.result.ack_history),
        deserialized_observations=len(decisions),
        possible_action_total=sum(
            len(decision.possible_actions) for decision in decisions
        ),
        scores=record.result.scores,
    )


def _write_new_bytes(path: Path, data: bytes) -> None:
    """新しいfileだけへraw bytesを書く(protocol traceをbyte単位で保つ)。"""
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(data)
    except Exception:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def save_ranked_game_record(
    result: RankedGameResult,
    protocol_trace_path: str | os.PathLike,
    destination: str | os.PathLike,
    *,
    provenance: RankedRecordProvenance,
) -> DurableRankedGameRecord:
    """completed hanchanをnew immutable bundleへstrict readback後にpublishする。

    `protocol_trace_path`はこのhanchan専用に新しく作られたtrace fileだけを
    渡す。複数gameがappendされたdiagnostic traceを1 recordとして扱わない。

    tokenやAuthorization headerはこの境界へ渡らない。
    """
    if not isinstance(result, RankedGameResult):
        raise TypeError("result must be a RankedGameResult")
    if not result.end_game_received:
        raise DurableRankedGameRecordError(
            "a partial ranked execution is not a completed durable record"
        )
    if not isinstance(result.seat, Seat):
        raise DurableRankedGameRecordError("completed record requires a bound seat")
    if not isinstance(provenance, RankedRecordProvenance):
        raise TypeError("provenance must be a RankedRecordProvenance")

    trace_path = Path(protocol_trace_path)
    if trace_path.is_symlink() or not trace_path.is_file():
        raise DurableRankedGameRecordError(
            "protocol trace file is missing or is not a regular file"
        )
    trace_data = trace_path.read_bytes()
    if not trace_data:
        raise DurableRankedGameRecordError("protocol trace file is empty")

    target = Path(destination)
    if target.exists():
        raise FileExistsError(f"record path already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    result_data = _canonical_bytes(_result_document(result))
    payloads = {
        "protocol_trace": _payload_reference(PROTOCOL_TRACE_FILENAME, trace_data),
        "result": _payload_reference(RESULT_FILENAME, result_data),
    }
    manifest_data = _canonical_bytes(
        _manifest_document(
            bound_seat=result.seat, provenance=provenance, payloads=payloads
        )
    )

    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent)
    )
    published = False
    try:
        _write_new_bytes(staging / PROTOCOL_TRACE_FILENAME, trace_data)
        write_new_artifact_file(staging / RESULT_FILENAME, result_data.decode("utf-8"))
        write_new_artifact_file(
            staging / MANIFEST_FILENAME, manifest_data.decode("utf-8")
        )
        validated = load_ranked_game_record(staging)

        target.mkdir()
        published = True
        for filename in (PROTOCOL_TRACE_FILENAME, RESULT_FILENAME, MANIFEST_FILENAME):
            os.rename(staging / filename, target / filename)
        return validated
    except BaseException:
        if published:
            shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def acquire_ranked_game_record(
    policy: Policy,
    token: str,
    *,
    destination: str | os.PathLike,
    url: str = DEFAULT_RANKED_URL,
    profile_identity: str | None = None,
    policy_identity: str | None = None,
    provenance: RankedRecordProvenance | None = None,
) -> DurableRankedGameRecord:
    """1 ranked hanchanを実行し、完走した場合だけdurable recordへpublishする。

    existing `run_ranked_game()`をそのまま利用し、protocol loggerを再実装
    しない。durable acquisitionはこのhanchan専用のfresh staging trace path
    を使うため、過去のdiagnostic traceへappendされることはない。

    connection failure、`ProtocolError`、fatal `action_ack`、Policy failure、
    unexpected disconnect等ではrecordをfinalizeせず、staging bundleを残さない。

    `token`はexisting transport boundaryへ渡すだけであり、record layerへは
    渡らない。
    """
    target = Path(destination)
    if target.exists():
        raise FileExistsError(f"record path already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    if provenance is None:
        provenance = collect_ranked_record_provenance(
            profile_identity=profile_identity,
            policy_identity=(
                policy_identity
                if policy_identity is not None
                else type(policy).__name__
            ),
        )

    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.acquisition-", dir=target.parent)
    )
    try:
        trace_path = staging / PROTOCOL_TRACE_FILENAME
        result = await run_ranked_game(policy, token, url=url, trace_path=trace_path)
        return save_ranked_game_record(
            result, trace_path, target, provenance=provenance
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "MANIFEST_FILENAME",
    "PROTOCOL_TRACE_FILENAME",
    "RANKED_GAME_RECORD_COMPLETION_STATUS",
    "RANKED_GAME_RECORD_EXECUTION_BACKEND",
    "RANKED_GAME_RECORD_EXECUTION_ENVIRONMENT",
    "RANKED_GAME_RECORD_MODE",
    "RANKED_GAME_RECORD_SCHEMA_ID",
    "RANKED_GAME_RECORD_SCHEMA_VERSION",
    "RESULT_FILENAME",
    "UNRESOLVED_PROVENANCE_VALUE",
    "DurableRankedGameRecord",
    "DurableRankedGameRecordError",
    "ProtocolTraceEntry",
    "RankedDecisionReadback",
    "RankedGameRecordSummary",
    "RankedRecordPayloadReference",
    "RankedRecordProvenance",
    "acquire_ranked_game_record",
    "collect_ranked_record_provenance",
    "iter_ranked_decisions",
    "load_ranked_game_record",
    "save_ranked_game_record",
    "summarize_ranked_game_record",
]
