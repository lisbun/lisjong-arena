"""通常ツモcapture（#596）のprivileged artifact: write-once保存とstrict readback。

```text
<destination>/
  manifest.json     arena-engine-live-draw-capture-manifest-v1
  live-draws.jsonl  arena-engine-live-draw-capture-row-v1（1通常ツモ1行、canonical JSON）
```

- 正式result・局記録・`GameTrace`とは別のschema・別の保存先にする。行は
  `identity`（seed等）と`realized`（実現牌）を含み、全体としては未来を
  再構成できる情報を持つため、manifestに`access`と`retention`を必ず記録する
- 書き込みには、実行前に決めた半荘の計画（`(seed, placement)`の順序つき列）を
  capture結果とは独立に渡す。captureした半荘がその計画と過不足・重複・順序違いなく
  一致しない場合は何も公開しない（完全なseed blockだけを公開する）。計画はmanifestへ
  記録し、読込時も半荘の列と一致することを検査する
- destinationは新規に限る。同じparentのstagingへ書き、読み戻し検査に通った
  場合だけ公開する（失敗時は何も公開しない）
- 読込時は作成時と同じ行検査（`live_draw_capture.validate_row`）を行い、
  manifestの件数・bytes・SHA-256・半荘ごとの局数と件数、行の順序・一意性と照合する
- readerは各行を`identity` / `pre_draw` / `realized`に分けて返す。価値関数・
  条件付き分布の入力へ渡してよいのは`pre_draw`から射影したfieldだけである
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena._artifact_io import (
    canonical_json_text,
    parse_json_text,
    read_json_document,
    sha256_bytes,
    staged_artifact_directory,
    write_new_artifact_file,
)

from .live_draw_capture import (
    ROW_SCHEMA,
    CapturedHanchan,
    CapturedRound,
    LiveDrawCaptureError,
    validate_row,
)

MANIFEST_SCHEMA = "arena-engine-live-draw-capture-manifest-v1"
MANIFEST_FILENAME = "manifest.json"
ROWS_FILENAME = "live-draws.jsonl"
INFORMATION_CLASS = "privileged-offline-only"

_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_MANIFEST_FIELDS = {
    "access",
    "hanchans",
    "information_class",
    "payload",
    "plan",
    "producer",
    "retention",
    "row_schema",
    "rules",
    "schema",
}
_PRODUCER_FIELDS = {"arena_revision", "engine_revision"}
_HANCHAN_FIELDS = {"placement", "rounds", "seed"}
_ROUND_FIELDS = {"live_draw_count", "rinshan_draw_count", "round_ordinal"}
_PAYLOAD_FIELDS = {"bytes", "file", "rows", "sha256"}


@dataclass(frozen=True)
class LiveDrawRecord:
    """読み戻した1行。3つの部分を分けて持つ。"""

    identity: dict[str, object]
    pre_draw: dict[str, object]
    realized: dict[str, object]


@dataclass(frozen=True)
class LiveDrawArtifact:
    manifest: dict[str, object]
    records: tuple[LiveDrawRecord, ...]


def _canonical_line(row: dict[str, object]) -> str:
    return (
        json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _require_text(value: object, context: str) -> str:
    if type(value) is not str or not value.strip():
        raise LiveDrawCaptureError(f"{context} must be a non-empty string")
    return value


def _require_revision(value: object, context: str) -> str:
    if type(value) is not str or not _REVISION.fullmatch(value):
        raise LiveDrawCaptureError(f"{context} must be a 40-character revision")
    return value


def _hanchan_entry(hanchan: CapturedHanchan) -> dict[str, object]:
    return {
        "placement": hanchan.placement,
        "rounds": [
            {
                "live_draw_count": item.live_draw_count,
                "rinshan_draw_count": item.rinshan_draw_count,
                "round_ordinal": item.round_ordinal,
            }
            for item in hanchan.rounds
        ],
        "seed": hanchan.seed,
    }


def _check_rows_against_hanchans(
    rows: Sequence[dict[str, object]], hanchans: Sequence[dict[str, object]]
) -> None:
    """行の順序・一意性・連番を、manifestの半荘・局ごとの件数と照合する。"""
    seen_keys: set[tuple[int, int]] = set()
    cursor = 0
    for hanchan_index, hanchan in enumerate(hanchans):
        key = (hanchan["seed"], hanchan["placement"])
        if key in seen_keys:
            raise LiveDrawCaptureError("(seed, placement) must be unique")
        seen_keys.add(key)
        for expected_ordinal, round_entry in enumerate(hanchan["rounds"], start=1):
            if round_entry["round_ordinal"] != expected_ordinal:
                raise LiveDrawCaptureError(
                    f"hanchans[{hanchan_index}] round ordinals must be 1..n"
                )
            previous_revision = 0
            for draw_index in range(round_entry["live_draw_count"]):
                if cursor >= len(rows):
                    raise LiveDrawCaptureError("payload has fewer rows than declared")
                identity = rows[cursor]["identity"]
                expected = (
                    hanchan["seed"],
                    hanchan["placement"],
                    expected_ordinal,
                    draw_index,
                )
                actual = (
                    identity["seed"],
                    identity["placement"],
                    identity["round_ordinal"],
                    identity["draw_index"],
                )
                if actual != expected:
                    raise LiveDrawCaptureError(
                        f"row {cursor} identity is out of order or misplaced"
                    )
                if identity["revision"] <= previous_revision:
                    raise LiveDrawCaptureError(
                        f"row {cursor} revision must increase within a round"
                    )
                previous_revision = identity["revision"]
                cursor += 1
    if cursor != len(rows):
        raise LiveDrawCaptureError("payload has more rows than declared")


def _validate_plan(value: object) -> list[tuple[int, int]]:
    """計画（`(seed, placement)`の順序つき列）の形と一意性を検査する。"""
    if type(value) not in (list, tuple) or not value:
        raise LiveDrawCaptureError("the hanchan plan must be a non-empty sequence")
    plan = []
    for index, item in enumerate(value):
        if type(item) not in (list, tuple) or len(item) != 2:
            raise LiveDrawCaptureError(f"plan[{index}] must be (seed, placement)")
        seed, placement = item
        if type(seed) is not int:
            raise LiveDrawCaptureError(f"plan[{index}] seed must be an integer")
        if type(placement) is not int or placement < 0:
            raise LiveDrawCaptureError(f"plan[{index}] placement is invalid")
        plan.append((seed, placement))
    if len(set(plan)) != len(plan):
        raise LiveDrawCaptureError("the hanchan plan must not repeat (seed, placement)")
    return plan


def _require_plan_match(
    plan: Sequence[tuple[int, int]], captured: Sequence[tuple[int, int]]
) -> None:
    missing = [key for key in plan if key not in captured]
    extra = [key for key in captured if key not in plan]
    if missing or extra:
        raise LiveDrawCaptureError(
            f"captured hanchans differ from the plan (missing={missing}, extra={extra})"
        )
    if list(captured) != list(plan):
        raise LiveDrawCaptureError("captured hanchans must follow the plan order")


def _validate_hanchans(value: object) -> list[dict[str, object]]:
    if type(value) is not list or not value:
        raise LiveDrawCaptureError("manifest.hanchans must be a non-empty array")
    result = []
    for index, raw in enumerate(value):
        if type(raw) is not dict or set(raw) != _HANCHAN_FIELDS:
            raise LiveDrawCaptureError(f"manifest.hanchans[{index}] fields are invalid")
        if type(raw["seed"]) is not int:
            raise LiveDrawCaptureError("manifest hanchan seed must be an integer")
        if type(raw["placement"]) is not int or raw["placement"] < 0:
            raise LiveDrawCaptureError("manifest hanchan placement is invalid")
        rounds = raw["rounds"]
        if type(rounds) is not list or not rounds:
            raise LiveDrawCaptureError("manifest hanchan rounds must be non-empty")
        for item in rounds:
            if type(item) is not dict or set(item) != _ROUND_FIELDS:
                raise LiveDrawCaptureError("manifest round fields are invalid")
            for field in _ROUND_FIELDS:
                if type(item[field]) is not int or item[field] < 0:
                    raise LiveDrawCaptureError(f"manifest round {field} is invalid")
        result.append(raw)
    return result


def write_live_draw_capture(
    destination: Path,
    hanchans: Sequence[CapturedHanchan],
    *,
    plan: Sequence[tuple[int, int]],
    arena_revision: str,
    engine_revision: str,
    rules: str,
    access: str,
    retention: str,
) -> LiveDrawArtifact:
    """captureした半荘列をwrite-onceで保存し、strict readbackした結果を返す。

    `plan`は実行前に決めた`(seed, placement)`の順序つき列で、capture結果とは
    独立に渡す。過不足・重複・順序違いがあれば何も公開しない。
    `access`（誰が読めるか）と`retention`（いつまで保持するか）は必須の記述値。
    """
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    _require_revision(arena_revision, "arena_revision")
    _require_revision(engine_revision, "engine_revision")
    _require_text(rules, "rules")
    _require_text(access, "access")
    _require_text(retention, "retention")
    expected = _validate_plan(plan)
    hanchans = tuple(hanchans)
    if not hanchans:
        raise LiveDrawCaptureError("at least one captured hanchan is required")
    if any(not isinstance(hanchan, CapturedHanchan) for hanchan in hanchans):
        raise LiveDrawCaptureError("hanchans must be CapturedHanchan values")
    for hanchan in hanchans:
        if any(not isinstance(item, CapturedRound) for item in hanchan.rounds):
            raise LiveDrawCaptureError("rounds must be CapturedRound values")

    _require_plan_match(
        expected, [(hanchan.seed, hanchan.placement) for hanchan in hanchans]
    )

    rows = [row for hanchan in hanchans for row in hanchan.rows]
    for row in rows:
        validate_row(row)
    entries = [_hanchan_entry(hanchan) for hanchan in hanchans]
    _check_rows_against_hanchans(rows, entries)

    payload = "".join(_canonical_line(row) for row in rows)
    payload_bytes = payload.encode("utf-8")
    manifest = {
        "access": access,
        "hanchans": entries,
        "information_class": INFORMATION_CLASS,
        "payload": {
            "bytes": len(payload_bytes),
            "file": ROWS_FILENAME,
            "rows": len(rows),
            "sha256": sha256_bytes(payload_bytes),
        },
        "plan": [[seed, placement] for seed, placement in expected],
        "producer": {
            "arena_revision": arena_revision,
            "engine_revision": engine_revision,
        },
        "retention": retention,
        "row_schema": ROW_SCHEMA,
        "rules": rules,
        "schema": MANIFEST_SCHEMA,
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    with staged_artifact_directory(destination) as staging:
        write_new_artifact_file(staging / ROWS_FILENAME, payload)
        write_new_artifact_file(
            staging / MANIFEST_FILENAME, canonical_json_text(manifest)
        )
        read_live_draw_capture(staging)
    return read_live_draw_capture(destination)


def read_live_draw_capture(source: Path) -> LiveDrawArtifact:
    """artifactをstrictに読み戻す。どの不整合も`LiveDrawCaptureError`にする。"""
    source = Path(source)
    names = {path.name for path in source.iterdir()}
    if names != {MANIFEST_FILENAME, ROWS_FILENAME}:
        raise LiveDrawCaptureError("artifact file set is invalid")
    try:
        manifest = read_json_document(source / MANIFEST_FILENAME)
    except ValueError as exc:
        raise LiveDrawCaptureError("manifest is not valid JSON") from exc
    if type(manifest) is not dict or set(manifest) != _MANIFEST_FIELDS:
        raise LiveDrawCaptureError("manifest fields are invalid")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise LiveDrawCaptureError("manifest schema is unknown")
    if manifest["row_schema"] != ROW_SCHEMA:
        raise LiveDrawCaptureError("row schema is unknown")
    if manifest["information_class"] != INFORMATION_CLASS:
        raise LiveDrawCaptureError("information_class must be privileged-offline-only")
    _require_text(manifest["access"], "manifest.access")
    _require_text(manifest["retention"], "manifest.retention")
    _require_text(manifest["rules"], "manifest.rules")
    producer = manifest["producer"]
    if type(producer) is not dict or set(producer) != _PRODUCER_FIELDS:
        raise LiveDrawCaptureError("manifest.producer fields are invalid")
    _require_revision(producer["arena_revision"], "producer.arena_revision")
    _require_revision(producer["engine_revision"], "producer.engine_revision")
    hanchans = _validate_hanchans(manifest["hanchans"])
    _require_plan_match(
        _validate_plan(manifest["plan"]),
        [(hanchan["seed"], hanchan["placement"]) for hanchan in hanchans],
    )
    payload = manifest["payload"]
    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise LiveDrawCaptureError("manifest.payload fields are invalid")
    if payload["file"] != ROWS_FILENAME:
        raise LiveDrawCaptureError("manifest.payload.file is invalid")
    if type(payload["sha256"]) is not str or not _SHA256.fullmatch(payload["sha256"]):
        raise LiveDrawCaptureError("manifest.payload.sha256 is invalid")

    data = (source / ROWS_FILENAME).read_bytes()
    if type(payload["bytes"]) is not int or len(data) != payload["bytes"]:
        raise LiveDrawCaptureError("payload byte count differs from the manifest")
    if sha256_bytes(data) != payload["sha256"]:
        raise LiveDrawCaptureError("payload digest differs from the manifest")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise LiveDrawCaptureError("payload is not valid UTF-8") from exc
    if text and not text.endswith("\n"):
        raise LiveDrawCaptureError("payload must end with a newline")
    lines = text.splitlines(keepends=True)
    if type(payload["rows"]) is not int or len(lines) != payload["rows"]:
        raise LiveDrawCaptureError("payload row count differs from the manifest")

    rows = []
    for index, line in enumerate(lines):
        try:
            row = parse_json_text(line)
        except ValueError as exc:
            raise LiveDrawCaptureError(f"row {index} is not valid JSON") from exc
        if type(row) is not dict or _canonical_line(row) != line:
            raise LiveDrawCaptureError(f"row {index} is not canonical JSON")
        rows.append(validate_row(row))
    _check_rows_against_hanchans(rows, hanchans)
    return LiveDrawArtifact(
        manifest=manifest,
        records=tuple(
            LiveDrawRecord(
                identity=row["identity"],
                pre_draw=row["pre_draw"],
                realized=row["realized"],
            )
            for row in rows
        ),
    )


__all__ = [
    "INFORMATION_CLASS",
    "LiveDrawArtifact",
    "LiveDrawRecord",
    "MANIFEST_SCHEMA",
    "read_live_draw_capture",
    "write_live_draw_capture",
]
