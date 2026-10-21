"""局単位recordのmanifestとwrite-once I/O（#513、仕様§10.3〜§10.6）。

```text
<destination>/
  manifest.json          schema、protocol、rules identity、producer revision、
                         bindings（lock / 環境provenanceのdigest）、各payloadの
                         行数・bytes・SHA-256、第2層の状態
  kyoku-records.jsonl    第1層。1半荘1行（canonical JSON）
  event-facts.jsonl      第2層。1半荘1行。第2層がVALIDのときだけ存在する
```

- destinationは新規に限る。同じparentのstagingへ書き、読み戻し検査に通った
  場合だけrenameで公開する（失敗時は何も公開しない）
- 読込時は作成時と同じrow検査を行い、manifestの件数・bytes・hashと照合する
- 第2層の失敗の扱い（§10.6）:
  ``event_facts_required=True``（実行前確認）では作成・読込とも例外にする。
  ``False``（正式実行）では第2層全体を``INVALID``にして理由を残し、第2層の
  副指標を一切出さない。第1層の検査失敗は常に例外で、第2層の失敗へ読み替えない
- このrecordは局単位の観測記録であり、正式評価のPASS / 判定ではない。lock・
  環境provenanceとの照合、主指標・classificationは後続（#514）が行う
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lisjong_engine.match_state import CompletedMatch

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    parse_json_text,
    staged_artifact_directory,
    write_new_artifact_file,
)

from .errors import EventFactError, KyokuRecordError
from .event_facts import (
    EVENT_FACT_SCHEMA,
    project_event_facts,
    validate_event_fact_row,
)
from .kyoku_record import (
    KYOKU_RECORD_SCHEMA,
    PROTOCOL_ID,
    RULES_IDENTITY,
    SeatAssignment,
    canonical_line,
    project_hanchan,
    require_unique_hanchans,
    validate_hanchan_row,
)

MANIFEST_SCHEMA = "arena-heuristic-candidate-engine-aabb-record-manifest-v1"
MANIFEST_FILENAME = "manifest.json"
KYOKU_RECORD_FILENAME = "kyoku-records.jsonl"
EVENT_FACT_FILENAME = "event-facts.jsonl"

EVENT_FACTS_VALID = "VALID"
EVENT_FACTS_INVALID = "INVALID"

_MANIFEST_FIELDS = frozenset(
    {
        "bindings",
        "event_facts",
        "hanchan_count",
        "kyoku_records",
        "producer",
        "protocol",
        "rules",
        "schema",
    }
)
_BINDING_FIELDS = frozenset({"environment_digest", "lock_digest"})
_PRODUCER_FIELDS = frozenset({"arena_revision"})
_PAYLOAD_FIELDS = frozenset({"bytes", "file", "rows", "schema", "sha256"})
_INVALID_FIELDS = frozenset({"reason", "status"})
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class HanchanCapture:
    """runnerが1半荘の実行から渡す値（同じ実行の戻り値とcallback出力）。

    ``round_evidence``は``on_round_evidence_complete``が渡した値の列であり、
    取得していない場合は``None``（未取得）とする。``None``を0件として扱わない。
    """

    match: CompletedMatch
    seed: int
    placement: int
    seats: tuple[SeatAssignment, SeatAssignment, SeatAssignment, SeatAssignment]
    selector_call_counts: tuple[int, int, int, int]
    policy_call_counts: tuple[int, int, int, int]
    round_evidence: tuple[object, ...] | None


@dataclass(frozen=True, slots=True)
class EventFactStatus:
    """第2層の状態。``INVALID``では理由を持ち、第2層の副指標を出さない。"""

    status: str
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.status == EVENT_FACTS_VALID

    def to_value(self) -> dict[str, object]:
        if self.valid:
            return {"status": self.status}
        return {"reason": self.reason, "status": self.status}


@dataclass(frozen=True, slots=True)
class RecordBindings:
    """後続（#514）が結合するlockと環境provenanceのdigest（未定は``None``）。"""

    lock_digest: str | None = None
    environment_digest: str | None = None

    def to_value(self) -> dict[str, object]:
        return {
            "environment_digest": self.environment_digest,
            "lock_digest": self.lock_digest,
        }


@dataclass(frozen=True, slots=True)
class VerifiedKyokuRecords:
    """作成または読込の検査に通った局単位record。正式評価の判定ではない。"""

    hanchans: tuple[dict, ...]
    event_facts: tuple[dict, ...] | None
    event_fact_status: EventFactStatus
    bindings: RecordBindings
    producer_revision: str
    manifest_sha256: str


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _optional_digest(value: object, context: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not _HEX64.fullmatch(value):
        raise KyokuRecordError(f"{context} must be a 64-hex digest or null")
    return value


def _require_revision(value: object) -> str:
    if type(value) is not str or not _HEX40.fullmatch(value):
        raise KyokuRecordError("producer arena_revision must be a 40-hex revision")
    return value


def _require_bindings(bindings: object) -> RecordBindings:
    if not isinstance(bindings, RecordBindings):
        raise KyokuRecordError("bindings must be RecordBindings")
    _optional_digest(bindings.lock_digest, "bindings.lock_digest")
    _optional_digest(bindings.environment_digest, "bindings.environment_digest")
    return bindings


def _payload_text(rows: Sequence[dict]) -> str:
    return "".join(canonical_line(row) for row in rows)


def _payload_info(schema: str, filename: str, text: str, rows: int) -> dict:
    data = text.encode("utf-8")
    return {
        "bytes": len(data),
        "file": filename,
        "rows": rows,
        "schema": schema,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _build_manifest(
    *,
    producer_revision: str,
    bindings: RecordBindings,
    hanchan_count: int,
    kyoku_payload: dict,
    event_facts: dict,
) -> dict:
    return {
        "bindings": bindings.to_value(),
        "event_facts": event_facts,
        "hanchan_count": hanchan_count,
        "kyoku_records": kyoku_payload,
        "producer": {"arena_revision": producer_revision},
        "protocol": PROTOCOL_ID,
        "rules": dict(RULES_IDENTITY),
        "schema": MANIFEST_SCHEMA,
    }


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def build_records(
    captures: Sequence[HanchanCapture], *, event_facts_required: bool
) -> tuple[list[dict], list[dict] | None, EventFactStatus]:
    """全半荘を射影・検査する（I/Oなし）。

    第1層の失敗は``KyokuRecordError``で止める。第2層は、最初に失敗した半荘の
    理由で全体を``INVALID``にし、以降の第2層rowを作らない（部分出力しない）。
    """
    if isinstance(captures, (str, bytes)) or not isinstance(captures, Sequence):
        raise KyokuRecordError("captures must be a sequence of HanchanCapture")
    if not captures:
        raise KyokuRecordError("a kyoku record needs at least one hanchan")
    hanchans: list[dict] = []
    events: list[dict] | None = []
    status = EventFactStatus(EVENT_FACTS_VALID)
    for capture in captures:
        if not isinstance(capture, HanchanCapture):
            raise KyokuRecordError("captures must contain HanchanCapture values")
        row = project_hanchan(
            capture.match,
            seed=capture.seed,
            placement=capture.placement,
            seats=capture.seats,
            selector_call_counts=capture.selector_call_counts,
            policy_call_counts=capture.policy_call_counts,
        )
        hanchans.append(row)
        if events is None:
            continue
        try:
            events.append(project_event_facts(capture.round_evidence, row))
        except EventFactError as error:
            if event_facts_required:
                raise
            events = None
            status = EventFactStatus(EVENT_FACTS_INVALID, str(error))
    require_unique_hanchans(hanchans)
    return hanchans, events, status


def write_kyoku_records(
    destination: str | Path,
    captures: Sequence[HanchanCapture],
    *,
    producer_revision: str,
    event_facts_required: bool,
    bindings: RecordBindings = RecordBindings(),
) -> VerifiedKyokuRecords:
    """局単位recordを新しいdirectoryへwrite-onceで保存し、読み戻して返す。"""
    _require_revision(producer_revision)
    _require_bindings(bindings)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"parent directory does not exist: {destination}")
    hanchans, events, status = build_records(
        captures, event_facts_required=event_facts_required
    )
    kyoku_text = _payload_text(hanchans)
    kyoku_payload = _payload_info(
        KYOKU_RECORD_SCHEMA, KYOKU_RECORD_FILENAME, kyoku_text, len(hanchans)
    )
    if events is not None:
        event_text = _payload_text(events)
        event_payload = {
            **_payload_info(
                EVENT_FACT_SCHEMA, EVENT_FACT_FILENAME, event_text, len(events)
            ),
            "status": EVENT_FACTS_VALID,
        }
    else:
        event_payload = status.to_value()
    manifest = _build_manifest(
        producer_revision=producer_revision,
        bindings=bindings,
        hanchan_count=len(hanchans),
        kyoku_payload=kyoku_payload,
        event_facts=event_payload,
    )
    with staged_artifact_directory(destination) as staging:
        write_new_artifact_file(staging / KYOKU_RECORD_FILENAME, kyoku_text)
        if events is not None:
            write_new_artifact_file(staging / EVENT_FACT_FILENAME, event_text)
        write_new_artifact_file(
            staging / MANIFEST_FILENAME, canonical_json_text(manifest)
        )
        # 公開前に、読込と同じ検査を保存bytesに対して行う（renameはbytesを変えない）
        verified = read_kyoku_records(
            staging, event_facts_required=event_facts_required
        )
        if verified.event_fact_status != status:
            raise KyokuRecordError("event fact status changed on readback")
    return verified


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def _fields(value: object, fields: frozenset[str], context: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise KyokuRecordError(f"{context} fields do not match the schema")
    return value


def _read_lines(path: Path, payload: dict, error_type, context: str) -> list[dict]:
    """payload fileをbytes・hash・行数・canonical形式まで照合して読む。"""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise error_type(f"{context}: cannot read {path.name}: {error}") from error
    if len(data) != payload["bytes"] or (
        hashlib.sha256(data).hexdigest() != payload["sha256"]
    ):
        raise error_type(f"{context}: {path.name} size / sha256 mismatch")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise error_type(f"{context}: {path.name} is not UTF-8") from error
    # str.splitlines()はU+2028等でも分割するため使わない（canonical JSONは
    # ensure_ascii=Falseで、identity文字列にそれらを含み得る）
    parts = text.split("\n")
    if parts[-1] != "":
        raise error_type(f"{context}: {path.name} does not end with a newline")
    lines = [part + "\n" for part in parts[:-1]]
    if len(lines) != payload["rows"]:
        raise error_type(f"{context}: {path.name} row count mismatch")
    rows = []
    for index, line in enumerate(lines):
        try:
            row = parse_json_text(line)
        except (ValueError, ArtifactValidationError) as error:
            raise error_type(f"{context}: {path.name}[{index}] is not JSON") from error
        if canonical_line(row) != line:
            raise error_type(f"{context}: {path.name}[{index}] is not canonical")
        rows.append(row)
    return rows


def _check_payload(value: object, schema: str, filename: str, error_type, context: str):
    if type(value) is not dict:
        raise error_type(f"{context} must be an object")
    body = dict(value)
    if error_type is EventFactError:
        if body.pop("status", None) != EVENT_FACTS_VALID:
            raise error_type(f"{context}.status is not VALID")
    if set(body) != _PAYLOAD_FIELDS:
        raise error_type(f"{context} fields do not match the schema")
    if body["schema"] != schema or body["file"] != filename:
        raise error_type(f"{context} has an unsupported schema or file")
    for name in ("bytes", "rows"):
        if type(body[name]) is not int or body[name] < 0:
            raise error_type(f"{context}.{name} must be a non-negative int")
    if type(body["sha256"]) is not str or not _HEX64.fullmatch(body["sha256"]):
        raise error_type(f"{context}.sha256 is invalid")
    return body


def read_kyoku_records(
    path: str | Path, *, event_facts_required: bool
) -> VerifiedKyokuRecords:
    """保存済みrecordを読み、作成時と同じ検査を行って返す。"""
    root = Path(path)
    try:
        manifest_bytes = (root / MANIFEST_FILENAME).read_bytes()
        manifest = parse_json_text(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, ArtifactValidationError) as error:
        raise KyokuRecordError(f"invalid kyoku record manifest: {error}") from error
    if type(manifest) is not dict or manifest.get("schema") != MANIFEST_SCHEMA:
        raise KyokuRecordError("unsupported kyoku record manifest schema")
    if canonical_json_text(manifest).encode("utf-8") != manifest_bytes:
        raise KyokuRecordError("kyoku record manifest is not canonical")
    _fields(manifest, _MANIFEST_FIELDS, "manifest")
    if manifest["protocol"] != PROTOCOL_ID:
        raise KyokuRecordError("manifest.protocol mismatch")
    if manifest["rules"] != RULES_IDENTITY:
        raise KyokuRecordError("manifest.rules mismatch")
    producer = _fields(manifest["producer"], _PRODUCER_FIELDS, "manifest.producer")
    revision = _require_revision(producer["arena_revision"])
    binding_value = _fields(manifest["bindings"], _BINDING_FIELDS, "manifest.bindings")
    bindings = RecordBindings(
        lock_digest=_optional_digest(binding_value["lock_digest"], "lock_digest"),
        environment_digest=_optional_digest(
            binding_value["environment_digest"], "environment_digest"
        ),
    )
    count = manifest["hanchan_count"]
    if type(count) is not int or count < 1:
        raise KyokuRecordError("manifest.hanchan_count must be a positive int")

    kyoku_payload = _check_payload(
        manifest["kyoku_records"],
        KYOKU_RECORD_SCHEMA,
        KYOKU_RECORD_FILENAME,
        KyokuRecordError,
        "manifest.kyoku_records",
    )
    if kyoku_payload["rows"] != count:
        raise KyokuRecordError("manifest.kyoku_records.rows != hanchan_count")

    event_value = manifest["event_facts"]
    claims_valid = type(event_value) is dict and (
        event_value.get("status") == EVENT_FACTS_VALID
    )
    if not claims_valid:
        _fields(event_value, _INVALID_FIELDS, "manifest.event_facts")
        if event_value["status"] != EVENT_FACTS_INVALID or (
            type(event_value["reason"]) is not str or not event_value["reason"]
        ):
            raise KyokuRecordError("manifest.event_facts status / reason is invalid")
    allowed = {MANIFEST_FILENAME, KYOKU_RECORD_FILENAME}
    if claims_valid:
        allowed.add(EVENT_FACT_FILENAME)
    present = {child.name for child in root.iterdir()}
    if present - allowed or KYOKU_RECORD_FILENAME not in present:
        raise KyokuRecordError(
            f"missing / unexpected record files: {sorted(present ^ allowed)}"
        )

    # 第1層: 失敗は常に例外
    hanchans = [
        validate_hanchan_row(row)
        for row in _read_lines(
            root / KYOKU_RECORD_FILENAME,
            kyoku_payload,
            KyokuRecordError,
            "kyoku records",
        )
    ]
    require_unique_hanchans(hanchans)

    # 第2層: 第1層が通った後で、独立に判定する
    events = None
    if claims_valid:
        try:
            event_payload = _check_payload(
                event_value,
                EVENT_FACT_SCHEMA,
                EVENT_FACT_FILENAME,
                EventFactError,
                "manifest.event_facts",
            )
            if event_payload["rows"] != count:
                raise EventFactError("manifest.event_facts.rows != hanchan_count")
            if EVENT_FACT_FILENAME not in present:
                raise EventFactError("event fact file is missing")
            rows = _read_lines(
                root / EVENT_FACT_FILENAME, event_payload, EventFactError, "event facts"
            )
            events = tuple(
                validate_event_fact_row(row, hanchan)
                for row, hanchan in zip(rows, hanchans, strict=True)
            )
            status = EventFactStatus(EVENT_FACTS_VALID)
        except EventFactError as error:
            if event_facts_required:
                raise
            events = None
            status = EventFactStatus(
                EVENT_FACTS_INVALID, f"read-time verification failed: {error}"
            )
    else:
        if event_facts_required:
            raise EventFactError(
                f"event facts are required but recorded INVALID: {event_value['reason']}"
            )
        status = EventFactStatus(EVENT_FACTS_INVALID, event_value["reason"])
    return VerifiedKyokuRecords(
        hanchans=tuple(hanchans),
        event_facts=events,
        event_fact_status=status,
        bindings=bindings,
        producer_revision=revision,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def require_bindings(records: VerifiedKyokuRecords, expected: RecordBindings) -> None:
    """recordのbindingsが後続の期待するlock / 環境digestと一致するか（結合点）。"""
    _require_bindings(expected)
    if records.bindings != expected:
        raise KyokuRecordError("record bindings differ from the expected lock / env")


__all__ = [
    "EVENT_FACTS_INVALID",
    "EVENT_FACTS_VALID",
    "EVENT_FACT_FILENAME",
    "KYOKU_RECORD_FILENAME",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "EventFactStatus",
    "HanchanCapture",
    "RecordBindings",
    "VerifiedKyokuRecords",
    "build_records",
    "read_kyoku_records",
    "require_bindings",
    "write_kyoku_records",
]
