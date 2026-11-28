"""captureと半荘結果を同じ実行から作るoffline paired producer（#598 P1-1）。

```text
produce_paired_split(destination, plan, role, participants)
  for seed in plan.seeds(role), placement 0..5:
      capture_live_draw_hanchan(...)   1回の実行から CompletedMatch と CapturedHanchan
  build_records(...)                   同じ CompletedMatch から第1層の半荘row
<destination>/                          write-once（stagingで読み戻し検査に通った場合だけ公開）
  paired-manifest.json   arena-draw-correction-paired-run-v1
  capture/               #596のprivileged artifact（そのままの検査で読む）
  results.jsonl          第1層の半荘row（canonical JSON、1半荘1行）
```

paired manifestは生成時点に、planのidentity・split role・seed列・participant・rules・revision・
V1の定義と、半荘ごとのcapture行のdigest・結果rowのdigestを記録する。読込（`read_paired_split`）は
planと全項目を照合し、両方の中身からdigestを再計算して一致を要求する。後から別の実行の結果や
captureへ差し替えたものは、digestが合わずに拒否される（署名基盤ではないので、manifestごと
書き換える改ざんまでは防がない）。

通常ツモは各局の最初に親が必ず行うので、captureの行がない局はない。bindingは半荘単位の
digestで、行による局ごとの照合（親・場風）とは独立に働く。

実行前のgate（`participants.py`）: 渡された`ParticipantSource`（実際のfactory object・artifact file）
からbindingを作り直してplanと完全一致すること、測った実行環境（cleanなArena HEAD、installedな
engine / lisjong、native SOURCE_REVISION）がplanのrevisionと一致することを要求し、一致しなければ
1半荘も実行しない。seatへ記録するPolicy identityは、照合済みのbindingの値である。

このmoduleは対局を実行する。実際のseedでの実行はpilot計画（別承認）で行う。
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lisjong.draw_correction import v1_definition
from lisjong_engine.seat import Seat

from lisjong_arena._artifact_io import (
    canonical_json_text,
    parse_json_text,
    sha256_bytes,
    staged_artifact_directory,
    write_new_artifact_file,
)
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    AABB_LAYOUT,
    PLACEMENT_CANDIDATE_SEATS,
    ROLE_CANDIDATE,
    KyokuRecordError,
    SeatAssignment,
    placement_roles,
    validate_hanchan_row,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    HanchanCapture,
    build_records,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import (
    PROTOCOL_ID,
    RULES_DIGEST,
)
from lisjong_arena.lisjong_engine.live_draw_capture import (
    ROW_SCHEMA,
    LiveDrawCaptureError,
    capture_live_draw_hanchan,
)
from lisjong_arena.lisjong_engine.live_draw_store import (
    MANIFEST_FILENAME as CAPTURE_MANIFEST_FILENAME,
)
from lisjong_arena.lisjong_engine.live_draw_store import (
    LiveDrawArtifact,
    read_live_draw_capture,
    write_live_draw_capture,
)

from . import participants as participant_binding
from .participants import ParticipantBindingError
from .plan import (
    ROLES,
    CorrectionPlan,
    DrawCorrectionPlanError,
    read_canonical_document,
)

PAIRED_SCHEMA = "arena-draw-correction-paired-run-v1"
PAIRED_MANIFEST_FILENAME = "paired-manifest.json"
CAPTURE_DIRNAME = "capture"
RESULTS_FILENAME = "results.jsonl"
INFORMATION_CLASS = "privileged-offline-only"
PLACEMENTS = len(PLACEMENT_CANDIDATE_SEATS)

_FIELDS = {
    "access",
    "capture",
    "hanchans",
    "information_class",
    "participants",
    "plan_sha256",
    "protocol_id",
    "results",
    "retention",
    "revisions",
    "role",
    "rules_digest",
    "schema",
    "seeds",
    "value_definition",
}
_CAPTURE_FIELDS = {"directory", "manifest_sha256", "payload_sha256"}
_RESULTS_FIELDS = {"bytes", "file", "rows", "sha256"}
_HANCHAN_FIELDS = {"capture_rows_sha256", "placement", "result_row_sha256", "seed"}


class PairedRunError(DrawCorrectionPlanError):
    """paired artifactが同じ実行・同じplanへ結び付かない場合。"""


@dataclass(frozen=True)
class PairedSplit:
    """読み戻したpaired artifact（planと照合済み）。"""

    plan_sha256: str
    role: str
    seeds: tuple[int, ...]
    manifest_sha256: str
    capture: LiveDrawArtifact
    rows: tuple[dict, ...]


def _line(value: dict) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _digest_lines(values) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(_line(value).encode("utf-8"))
    return digest.hexdigest()


def _record_row(record) -> dict:
    return {
        "identity": record.identity,
        "pre_draw": record.pre_draw,
        "realized": record.realized,
        "schema": ROW_SCHEMA,
    }


def _schedule(seeds) -> list[tuple[int, int]]:
    return [(seed, placement) for seed in seeds for placement in range(PLACEMENTS)]


def _plan_fields(plan: CorrectionPlan, role: str) -> dict:
    return {
        "participants": dict(plan.participants),
        "plan_sha256": plan.sha256,
        "protocol_id": PROTOCOL_ID,
        "revisions": dict(plan.revisions),
        "role": role,
        "rules_digest": RULES_DIGEST,
        "seeds": list(plan.seeds(role)),
        "value_definition": v1_definition(),
    }


def _require_role(plan: CorrectionPlan, role: str) -> None:
    if not isinstance(plan, CorrectionPlan):
        raise PairedRunError("plan must be a CorrectionPlan read back from its file")
    if role not in ROLES:
        raise PairedRunError(f"role must be one of {ROLES}")


def produce_paired_split(
    destination: Path,
    plan: CorrectionPlan,
    role: str,
    *,
    participants: Mapping[str, participant_binding.ParticipantSource],
    access: str,
    retention: str,
) -> PairedSplit:
    """planのsplit roleの全seed × 6配置を1回ずつ実行し、paired artifactを書く。

    `participants`は`{"candidate": ParticipantSource, "incumbent": ParticipantSource}`。
    実行前に、そこから作り直したbindingと測った実行環境がplanと一致することを要求する
    （一致しなければ1半荘も実行しない）。Policyは各半荘・各席ごとに新しく作る。
    """
    destination = Path(destination)
    _require_role(plan, role)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    for text, name in ((access, "access"), (retention, "retention")):
        if type(text) is not str or not text.strip():
            raise PairedRunError(f"{name} must be a non-empty string")
    makers = participant_binding.require_participants(
        {name: plan.participant(name) for name in ("candidate", "incumbent")},
        participants,
    )
    measured = participant_binding.measure_environment()
    if measured != dict(plan.revisions):
        differing = sorted(k for k in measured if measured[k] != plan.revision(k))
        raise ParticipantBindingError(
            f"the execution environment differs from the plan ({differing})"
        )

    schedule = _schedule(plan.seeds(role))
    captures = []
    captured_hanchans = []
    for seed, placement in schedule:
        roles = placement_roles(placement)
        names = ["candidate" if r == ROLE_CANDIDATE else "incumbent" for r in roles]
        policies = {
            seat: makers[name]() for seat, name in zip(Seat, names, strict=True)
        }
        calls = dict.fromkeys(Seat, 0)

        def count(seat, calls=calls):
            calls[seat] += 1

        completed, captured = capture_live_draw_hanchan(
            policies, seed=seed, placement=placement, on_selector_call=count
        )
        counts = tuple(calls[seat] for seat in Seat)
        captures.append(
            HanchanCapture(
                match=completed,
                seed=seed,
                placement=placement,
                seats=tuple(
                    SeatAssignment(r, plan.policy_identity(name))
                    for r, name in zip(roles, names, strict=True)
                ),
                selector_call_counts=counts,
                policy_call_counts=counts,
                round_evidence=None,
            )
        )
        captured_hanchans.append(captured)
    rows, _, _ = build_records(captures, event_facts_required=False)

    results_text = "".join(_line(row) for row in rows)
    results_bytes = results_text.encode("utf-8")
    manifest = {
        **_plan_fields(plan, role),
        "access": access,
        "hanchans": [
            {
                "capture_rows_sha256": _digest_lines(captured.rows),
                "placement": captured.placement,
                "result_row_sha256": _digest_lines([row]),
                "seed": captured.seed,
            }
            for captured, row in zip(captured_hanchans, rows, strict=True)
        ],
        "information_class": INFORMATION_CLASS,
        "results": {
            "bytes": len(results_bytes),
            "file": RESULTS_FILENAME,
            "rows": len(rows),
            "sha256": sha256_bytes(results_bytes),
        },
        "retention": retention,
        "schema": PAIRED_SCHEMA,
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    with staged_artifact_directory(destination) as staging:
        capture = write_live_draw_capture(
            staging / CAPTURE_DIRNAME,
            captured_hanchans,
            plan=schedule,
            arena_revision=plan.revision("arena"),
            engine_revision=plan.revision("engine"),
            rules=RULES_DIGEST,
            access=access,
            retention=retention,
        )
        manifest["capture"] = {
            "directory": CAPTURE_DIRNAME,
            "manifest_sha256": sha256_bytes(
                (staging / CAPTURE_DIRNAME / CAPTURE_MANIFEST_FILENAME).read_bytes()
            ),
            "payload_sha256": capture.manifest["payload"]["sha256"],
        }
        write_new_artifact_file(staging / RESULTS_FILENAME, results_text)
        write_new_artifact_file(
            staging / PAIRED_MANIFEST_FILENAME, canonical_json_text(manifest)
        )
        read_paired_split(staging, plan)
    return read_paired_split(destination, plan)


def read_paired_split(source: Path, plan: CorrectionPlan) -> PairedSplit:
    """paired artifactをstrictに読み戻し、planとの一致と同じ実行のbindingを検査する。"""
    source = Path(source)
    if not isinstance(plan, CorrectionPlan):
        raise PairedRunError("plan must be a CorrectionPlan read back from its file")
    try:
        names = {path.name for path in source.iterdir()}
    except OSError as error:
        raise PairedRunError(f"paired artifact cannot be listed: {error}") from error
    if names != {PAIRED_MANIFEST_FILENAME, CAPTURE_DIRNAME, RESULTS_FILENAME}:
        raise PairedRunError("paired artifact file set is invalid")
    manifest, manifest_sha256 = read_canonical_document(
        source / PAIRED_MANIFEST_FILENAME, PairedRunError
    )
    if set(manifest) != _FIELDS or manifest["schema"] != PAIRED_SCHEMA:
        raise PairedRunError("paired manifest fields / schema are invalid")
    if manifest["information_class"] != INFORMATION_CLASS:
        raise PairedRunError("paired manifest information class is invalid")
    role = manifest["role"]
    if role not in ROLES:
        raise PairedRunError("paired manifest role is unknown")
    expected = _plan_fields(plan, role)
    for field, value in expected.items():
        if manifest[field] != value:
            raise PairedRunError(f"paired manifest {field} differs from the plan")
    schedule = _schedule(plan.seeds(role))

    # capture（#596の検査をそのまま使う）とplan・manifestの照合
    capture_info = manifest["capture"]
    if type(capture_info) is not dict or set(capture_info) != _CAPTURE_FIELDS:
        raise PairedRunError("paired manifest capture fields are invalid")
    if capture_info["directory"] != CAPTURE_DIRNAME:
        raise PairedRunError("paired manifest capture directory is invalid")
    capture_dir = source / CAPTURE_DIRNAME
    try:
        capture = read_live_draw_capture(capture_dir)
    except LiveDrawCaptureError as error:
        raise PairedRunError(f"capture is invalid: {error}") from error
    if (
        sha256_bytes((capture_dir / CAPTURE_MANIFEST_FILENAME).read_bytes())
        != (capture_info["manifest_sha256"])
    ):
        raise PairedRunError("capture manifest differs from the paired manifest")
    if capture.manifest["payload"]["sha256"] != capture_info["payload_sha256"]:
        raise PairedRunError("capture payload differs from the paired manifest")
    if [tuple(item) for item in capture.manifest["plan"]] != schedule:
        raise PairedRunError("capture plan differs from the plan split")
    producer = capture.manifest["producer"]
    if (producer["arena_revision"], producer["engine_revision"]) != (
        plan.revision("arena"),
        plan.revision("engine"),
    ):
        raise PairedRunError("capture producer revisions differ from the plan")
    if capture.manifest["rules"] != RULES_DIGEST:
        raise PairedRunError("capture rules differ from the plan")
    if (capture.manifest["access"], capture.manifest["retention"]) != (
        manifest["access"],
        manifest["retention"],
    ):
        raise PairedRunError(
            "capture access / retention differ from the paired manifest"
        )

    # 結果row
    results_info = manifest["results"]
    if type(results_info) is not dict or set(results_info) != _RESULTS_FIELDS:
        raise PairedRunError("paired manifest results fields are invalid")
    if results_info["file"] != RESULTS_FILENAME:
        raise PairedRunError("paired manifest results file is invalid")
    data = (source / RESULTS_FILENAME).read_bytes()
    if (
        len(data) != results_info["bytes"]
        or sha256_bytes(data) != results_info["sha256"]
    ):
        raise PairedRunError("results bytes / sha256 differ from the paired manifest")
    try:
        lines = data.decode("utf-8").splitlines(keepends=True)
        rows = [parse_json_text(line) for line in lines]
    except ValueError as error:
        raise PairedRunError(f"results cannot be parsed: {error}") from error
    if len(rows) != results_info["rows"] or len(rows) != len(schedule):
        raise PairedRunError("results row count differs")
    for line, row in zip(lines, rows, strict=True):
        if type(row) is not dict or _line(row) != line:
            raise PairedRunError("results rows must be canonical JSON objects")
    try:
        rows = [validate_hanchan_row(row, AABB_LAYOUT) for row in rows]
    except KyokuRecordError as error:
        raise PairedRunError(f"result row is invalid: {error}") from error
    if [(row["seed"], row["placement"]) for row in rows] != schedule:
        raise PairedRunError("result rows differ from the plan split")
    for row in rows:
        for assignment in row["seats"]:
            name = "candidate" if assignment["role"] == ROLE_CANDIDATE else "incumbent"
            if assignment["policy_identity"] != plan.policy_identity(name):
                raise PairedRunError(
                    f"seed {row['seed']} placement {row['placement']}: "
                    "participant differs from the plan"
                )

    # 同じ実行のbinding（半荘ごとのcapture行・結果rowのdigest）
    hanchans = manifest["hanchans"]
    if type(hanchans) is not list or len(hanchans) != len(schedule):
        raise PairedRunError("paired manifest hanchans are invalid")
    by_key: dict[tuple[int, int], list[dict]] = {key: [] for key in schedule}
    for record in capture.records:
        by_key[(record.identity["seed"], record.identity["placement"])].append(
            _record_row(record)
        )
    for entry, key, row in zip(hanchans, schedule, rows, strict=True):
        if type(entry) is not dict or set(entry) != _HANCHAN_FIELDS:
            raise PairedRunError("paired manifest hanchan fields are invalid")
        if (entry["seed"], entry["placement"]) != key:
            raise PairedRunError("paired manifest hanchans differ from the plan split")
        if entry["capture_rows_sha256"] != _digest_lines(by_key[key]):
            raise PairedRunError(f"{key}: capture rows are not the paired run")
        if entry["result_row_sha256"] != _digest_lines([row]):
            raise PairedRunError(f"{key}: result row is not the paired run")

    return PairedSplit(
        plan_sha256=plan.sha256,
        role=role,
        seeds=tuple(plan.seeds(role)),
        manifest_sha256=manifest_sha256,
        capture=capture,
        rows=tuple(rows),
    )


__all__ = [
    "CAPTURE_DIRNAME",
    "PAIRED_MANIFEST_FILENAME",
    "PAIRED_SCHEMA",
    "RESULTS_FILENAME",
    "PairedRunError",
    "PairedSplit",
    "produce_paired_split",
    "read_paired_split",
]
