"""Rust必須lisjong用の新version gate（#531）。Python/Rust照合P2（v1）を置き換える。

lisjong#346でPython計算backendが削除されたので、P2（``backend_check``、同じ12半荘を
Python / Rustで1回ずつ実行して完全一致を求める）は新しいpinでは実行できない。
このgateはPython全対局比較の代わりに、同じrevision・wheelのRust実行を別processで
2回行い、次を確かめる。

- P3: 親と各workerの ``environment_document``（wheel filename・bytes・SHA-256、
  installed native filesとwheelの一致、``SOURCE_REVISION``、``API_VERSION``、backend、
  provenance）が一致し、実行時のpin・wheelが現行の固定値である
- native counter: 各半荘でnumeric向聴のnative呼び出しが1回以上あり、親で本番factory
  経由のR5 native呼び出しが1回以上ある
- 記録整合性: 第1層・第2層の局単位recordとscheduleが成り立つ（正式runと同じ検査）
- 決定性の回帰: 2回の実行で、局数・``end_reason``・``final_points``・``CompletedMatch``
  全体・選択行動の列・native counterが全半荘で完全一致する
- 再導出: ``rust-gate-verify`` は保存済みの2つのrun documentだけからgate documentを
  作り直し、保存値との完全一致だけを成功とする

```text
rust-gate-run --run first  --wheel W --timeout-seconds T --output first.json
rust-gate-run --run second --wheel W --timeout-seconds T --output second.json  （別process）
rust-gate-compare --first first.json --second second.json --output gate.json   PASSでだけ0
rust-gate-verify  --first first.json --second second.json --gate gate.json     再導出
```

入力はP2と同じ#472の開発用seed 472005・472006 × 6配置（12半荘）、校正の固定pair
（registry外。正式・校正populationから除外済み）。強さの比較でも正式結果でもない。
仕様fixture・合法手・情報境界の回帰はlisjongとArenaのCI（このgateがbindするrevision）を
正本とし、このgateはそれを再実行しない。不一致のときにRustを補正したり、別のbackendへ
切り替えたりはしない。

旧P2（``backend-check-v1`` / ``backend-comparison-v1``）とprotocol v1のdigestは変更しない。
保存済みのP2記録は ``backend-compare`` で読み直せる（旧Python側の再実行は記録済み旧環境で行う）。
"""

import os
from collections.abc import Callable
from pathlib import Path

from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_clean_arena_head,
    require_merged_arena_revision,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .backend_check import (
    BACKEND_CHECK_SEEDS,
    COMPARED_FIELDS,
    _value_digest,
    canonical_engine_value,
)
from .calibration import calibration_participants
from .executor import (
    HanchanTask,
    PlayedHanchan,
    RunStopped,
    current_peak_rss_kib,
    environment_document,
    play_task,
    run_schedule,
)
from .kyoku_record import ROLE_CANDIDATE, ROLE_INCUMBENT, verify_schedule
from .protocol import (
    ENGINE_REVISION,
    PROTOCOL_DIGEST,
    PROTOCOL_ID,
    RULES_DIGEST,
    EngineAabbProtocolError,
    expected_schedule,
    parse_participant,
    placement_schedule,
    require_hex,
)
from .record_store import build_records

RUN_SCHEMA = "arena-heuristic-candidate-engine-aabb-rust-only-gate-run-v1"
GATE_SCHEMA = "arena-heuristic-candidate-engine-aabb-rust-only-gate-v1"
GATE_OWNER_ISSUE = "lisbun/lisjong-arena#531"
GATE_SEEDS = BACKEND_CHECK_SEEDS
"""P2と同じ#472の開発用seed（registry外）。"""
RUN_LABELS = ("first", "second")
COUNTER_FIELDS = ("discard_evaluations", "numeric_shanten", "progression_evaluations")
DETERMINISM_FIELDS = (*COMPARED_FIELDS, "native_counters")
PASS = "PASS"
FAIL = "FAIL"

_RUN_FIELDS = frozenset(
    {
        "arena_revision",
        "document_digest",
        "environment",
        "environment_digest",
        "gate_owner_issue",
        "hanchans",
        "ordered_seeds",
        "participants",
        "protocol_digest",
        "protocol_id",
        "r5_probe_calls",
        "rules_digest",
        "run_label",
        "schema",
        "timings",
    }
)
_HANCHAN_FIELDS = frozenset(
    {
        "completed_match",
        "completed_match_sha256",
        "end_reason",
        "final_points",
        "kyoku_count",
        "native_counters",
        "placement",
        "seed",
        "selected_actions",
        "selected_actions_sha256",
    }
)
_GATE_FIELDS = frozenset(
    {
        "arena_revision",
        "compared_fields",
        "environment_digest",
        "first_document_digest",
        "gate_digest",
        "gate_owner_issue",
        "hanchan_count",
        "kyoku_count",
        "lisjong_revision",
        "mismatches",
        "native",
        "native_counter_totals",
        "r5_probe_calls",
        "schema",
        "second_document_digest",
        "selected_action_count",
        "status",
    }
)


class EngineAabbRustGateError(EngineAabbProtocolError):
    """gateの実行・読込・再導出ができない場合（不一致そのものはgate documentに記録する）。"""


def _counter_snapshot() -> dict[str, int]:
    from lisjong_arena.shanten_backend_verification.backend import (
        native_call_count,
        native_discard_evaluation_count,
        native_progression_evaluation_count,
    )

    values = {
        "discard_evaluations": native_discard_evaluation_count(),
        "numeric_shanten": native_call_count(),
        "progression_evaluations": native_progression_evaluation_count(),
    }
    if any(value is None for value in values.values()):
        raise RunStopped("the native extension is not loaded in this worker")
    return values


def play_rust_gate_hanchan(task: HanchanTask) -> PlayedHanchan:
    """worker内で1半荘を実行し、選択行動の列とnative counterの増分も返す。"""
    environment = environment_document(task.wheel_path)
    actions = []

    def record(seat, action) -> None:
        actions.append([seat.name, canonical_engine_value(action)])

    before = _counter_snapshot()
    capture = play_task(task, on_action=record)
    after = _counter_snapshot()
    counters = {name: after[name] - before[name] for name in COUNTER_FIELDS}
    return PlayedHanchan(
        capture,
        environment,
        os.getpid(),
        current_peak_rss_kib(),
        tuple(actions),
        native_counters=counters,
    )


def _require_environment(environment: object) -> str:
    """run documentの環境がRust・同一revisionのnativeで内部整合していることを確かめる。"""
    if type(environment) is not dict:
        raise EngineAabbRustGateError("gate environment is not an object")
    provenance = environment.get("provenance")
    native = environment.get("native")
    backend = environment.get("shanten_backend")
    if type(provenance) is not dict or type(native) is not dict:
        raise EngineAabbRustGateError("gate environment has no native provenance")
    if provenance.get("lisjong_engine_revision") != ENGINE_REVISION:
        raise EngineAabbRustGateError(
            "installed lisjong-engine is not the protocol revision"
        )
    lisjong = provenance.get("lisjong_revision")
    require_hex(lisjong, 40, "lisjong_revision")
    if backend != {"backend": "rust", "lisjong_revision": lisjong}:
        raise EngineAabbRustGateError("gate process backend is not rust")
    if native.get("source_revision") != lisjong:
        raise EngineAabbRustGateError(
            "native SOURCE_REVISION differs from the installed lisjong revision"
        )
    if type(native.get("api_version")) is not int:
        raise EngineAabbRustGateError("native API_VERSION is not recorded")
    require_hex(native.get("wheel_sha256"), 64, "wheel_sha256")
    return lisjong


def _require_counters(value: object, label: str) -> dict[str, int]:
    if type(value) is not dict or set(value) != set(COUNTER_FIELDS):
        raise EngineAabbRustGateError(f"{label}: native counters are malformed")
    for name in COUNTER_FIELDS:
        if type(value[name]) is not int or value[name] < 0:
            raise EngineAabbRustGateError(f"{label}: native counter {name} is invalid")
    if value["numeric_shanten"] < 1:
        raise EngineAabbRustGateError(
            f"{label}: no numeric shanten call reached the native core"
        )
    return value


def run_rust_gate(
    output_path: str | Path,
    *,
    run_label: str,
    wheel_path: str,
    timeout_seconds: int,
    play: Callable[[HanchanTask], PlayedHanchan] = play_rust_gate_hanchan,
    environment: dict | None = None,
    arena_revision: str | None = None,
    r5_probe_calls: int | None = None,
) -> dict[str, object]:
    """開発用12半荘をRustで1回実行し、gate用のrun documentをwrite-onceで書く。

    ``play`` / ``environment`` / ``arena_revision`` / ``r5_probe_calls`` はtestのseamで、
    実行では渡さない。実行ではArena HEADがcleanで ``main`` に含まれること、親の環境が
    現行のpin・wheel（``environment_document``）であることを要求する。
    """
    output = Path(output_path)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite: {output}")
    if run_label not in RUN_LABELS:
        raise EngineAabbRustGateError(f"run label must be one of {RUN_LABELS}")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise EngineAabbRustGateError(
            "timeout_seconds must be an explicit positive int"
        )
    if arena_revision is None:
        try:
            arena_revision = require_merged_arena_revision(require_clean_arena_head())
        except ExecutionSafetyError as error:
            raise EngineAabbRustGateError(str(error)) from error
    require_hex(arena_revision, 40, "arena_revision")
    if environment is None:
        environment = environment_document(wheel_path)
    lisjong = _require_environment(environment)
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        raise EngineAabbRustGateError("Arena revision differs from the environment")
    if r5_probe_calls is None:
        from lisjong_arena.shanten_backend_verification.backend import (
            probe_r5_native,
        )

        r5_probe_calls = probe_r5_native()
    if type(r5_probe_calls) is not int or r5_probe_calls < 1:
        raise EngineAabbRustGateError("the R5 factory did not reach the native core")
    candidate, incumbent = calibration_participants(lisjong)
    hanchans = placement_schedule(GATE_SEEDS)
    executed = run_schedule(
        hanchans=hanchans,
        candidate=candidate,
        incumbent=incumbent,
        wheel_path=wheel_path,
        timeout_seconds=timeout_seconds,
        expected_environment=environment,
        play=play,
    )
    captures = [item.capture for item in executed]
    # 第1層・第2層とscheduleが成り立つ実行であることを確かめる（保存はしない）
    rows, _, _ = build_records(captures, event_facts_required=True)
    verify_schedule(rows, expected_schedule(hanchans, candidate, incumbent))
    documents = []
    for item in executed:
        capture = item.capture
        label = f"seed {capture.seed} placement {capture.placement}"
        actions = item.selected_actions
        if actions is None or len(actions) != sum(capture.policy_call_counts):
            raise RunStopped("selected actions do not match the Policy call counts")
        try:
            counters = _require_counters(item.native_counters, label)
        except EngineAabbRustGateError as error:
            raise RunStopped(str(error)) from None
        match = capture.match
        completed = canonical_engine_value(match)
        selected = [list(entry) for entry in actions]
        documents.append(
            {
                "completed_match": completed,
                "completed_match_sha256": _value_digest(completed),
                "end_reason": canonical_engine_value(match.end_reason),
                "final_points": [
                    match.final_score.for_seat(seat).final_points for seat in EngineSeat
                ],
                "kyoku_count": len(match.history),
                "native_counters": dict(counters),
                "placement": capture.placement,
                "seed": capture.seed,
                "selected_actions": selected,
                "selected_actions_sha256": _value_digest(selected),
            }
        )
    payload = {
        "arena_revision": arena_revision,
        "environment": environment,
        "environment_digest": document_identity(environment),
        "gate_owner_issue": GATE_OWNER_ISSUE,
        "hanchans": documents,
        "ordered_seeds": list(GATE_SEEDS),
        "participants": {
            ROLE_CANDIDATE: candidate.to_document(),
            ROLE_INCUMBENT: incumbent.to_document(),
        },
        "protocol_digest": PROTOCOL_DIGEST,
        "protocol_id": PROTOCOL_ID,
        "r5_probe_calls": r5_probe_calls,
        "rules_digest": RULES_DIGEST,
        "run_label": run_label,
        "schema": RUN_SCHEMA,
        "timings": [
            {
                "placement": item.capture.placement,
                "seed": item.capture.seed,
                "wall_seconds": item.wall_seconds,
                "worker_peak_rss_kib": item.peak_rss_kib,
            }
            for item in executed
        ],
    }
    document = {**payload, "document_digest": document_identity(payload)}
    write_new_artifact_file(output, canonical_json_text(document))
    return document


def _read(path: str | Path, label: str) -> object:
    try:
        return read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise EngineAabbRustGateError(f"cannot read {label}: {error}") from error


def load_rust_gate_run(path: str | Path) -> dict[str, object]:
    """保存済みrun documentを読み、schema・digest・環境・schedule・counterを検査する。"""
    value = _read(path, "rust gate run")
    if (
        type(value) is not dict
        or value.get("schema") != RUN_SCHEMA
        or set(value) != _RUN_FIELDS
    ):
        raise EngineAabbRustGateError("unsupported rust gate run document")
    payload = {key: item for key, item in value.items() if key != "document_digest"}
    if value["document_digest"] != document_identity(payload):
        raise EngineAabbRustGateError("rust gate run digest mismatch")
    if value["environment_digest"] != document_identity(value["environment"]):
        raise EngineAabbRustGateError("rust gate run environment digest mismatch")
    if value["run_label"] not in RUN_LABELS:
        raise EngineAabbRustGateError("rust gate run has an unknown run label")
    require_hex(value["arena_revision"], 40, "arena_revision")
    lisjong = _require_environment(value["environment"])
    if (
        value["environment"]["provenance"].get("lisjong_arena_revision")
        != (value["arena_revision"])
    ):
        raise EngineAabbRustGateError("Arena revision differs from the environment")
    calls = value["r5_probe_calls"]
    if type(calls) is not int or calls < 1:
        raise EngineAabbRustGateError("rust gate run has no R5 native call")
    participants = value["participants"]
    expected = calibration_participants(lisjong)
    if (
        type(participants) is not dict
        or set(participants) != {ROLE_CANDIDATE, ROLE_INCUMBENT}
        or parse_participant(participants[ROLE_CANDIDATE], "A") != expected[0]
        or parse_participant(participants[ROLE_INCUMBENT], "B") != expected[1]
    ):
        raise EngineAabbRustGateError("rust gate run participants drifted")
    if (
        value["ordered_seeds"] != list(GATE_SEEDS)
        or value["gate_owner_issue"] != GATE_OWNER_ISSUE
        or value["protocol_id"] != PROTOCOL_ID
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules_digest"] != RULES_DIGEST
    ):
        raise EngineAabbRustGateError("rust gate run identity drifted")
    schedule = [
        (item["seed"], item["placement"]) for item in placement_schedule(GATE_SEEDS)
    ]
    rows = value["hanchans"]
    if type(rows) is not list or any(
        type(row) is not dict or set(row) != _HANCHAN_FIELDS for row in rows
    ):
        raise EngineAabbRustGateError("rust gate run rows are malformed")
    if [(row["seed"], row["placement"]) for row in rows] != schedule:
        raise EngineAabbRustGateError("rust gate run does not cover the schedule")
    for row in rows:
        if row["completed_match_sha256"] != _value_digest(
            row["completed_match"]
        ) or row["selected_actions_sha256"] != _value_digest(row["selected_actions"]):
            raise EngineAabbRustGateError("rust gate run row digest mismatch")
        _require_counters(
            row["native_counters"], f"seed {row['seed']} placement {row['placement']}"
        )
    return value


def compare_rust_gate_runs(
    first_path: str | Path, second_path: str | Path
) -> dict[str, object]:
    """2つのrun documentを読み、条件の同一性と全半荘の完全一致からgate documentを作る。"""
    first = load_rust_gate_run(first_path)
    second = load_rust_gate_run(second_path)
    if (first["run_label"], second["run_label"]) != RUN_LABELS:
        raise EngineAabbRustGateError("compare the first run with the second one")
    for name in ("arena_revision", "environment_digest", "participants"):
        if first[name] != second[name]:
            raise EngineAabbRustGateError(f"rust gate runs differ in {name}")
    mismatches = []
    for left, right in zip(first["hanchans"], second["hanchans"], strict=True):
        for field in DETERMINISM_FIELDS:
            if left[field] != right[field]:
                mismatches.append(
                    {
                        "field": field,
                        "placement": left["placement"],
                        "seed": left["seed"],
                    }
                )
    rows = first["hanchans"]
    environment = first["environment"]
    native = environment["native"]
    payload = {
        "arena_revision": first["arena_revision"],
        "compared_fields": list(DETERMINISM_FIELDS),
        "environment_digest": first["environment_digest"],
        "first_document_digest": first["document_digest"],
        "gate_owner_issue": GATE_OWNER_ISSUE,
        "hanchan_count": len(rows),
        "kyoku_count": sum(row["kyoku_count"] for row in rows),
        "lisjong_revision": environment["shanten_backend"]["lisjong_revision"],
        "mismatches": mismatches,
        "native": {
            "api_version": native["api_version"],
            "source_revision": native["source_revision"],
            "wheel_file": native["wheel_file"],
            "wheel_sha256": native["wheel_sha256"],
        },
        "native_counter_totals": {
            name: sum(row["native_counters"][name] for row in rows)
            for name in COUNTER_FIELDS
        },
        "r5_probe_calls": [first["r5_probe_calls"], second["r5_probe_calls"]],
        "schema": GATE_SCHEMA,
        "second_document_digest": second["document_digest"],
        "selected_action_count": sum(len(row["selected_actions"]) for row in rows),
        "status": FAIL if mismatches else PASS,
    }
    return {**payload, "gate_digest": document_identity(payload)}


def verify_rust_gate(
    first_path: str | Path, second_path: str | Path, gate_path: str | Path
) -> dict[str, object]:
    """保存済みrun documentだけからgate documentを再導出し、完全一致を要求する。"""
    value = _read(gate_path, "rust gate")
    if (
        type(value) is not dict
        or value.get("schema") != GATE_SCHEMA
        or set(value) != _GATE_FIELDS
    ):
        raise EngineAabbRustGateError("unsupported rust gate document")
    payload = {key: item for key, item in value.items() if key != "gate_digest"}
    if value["gate_digest"] != document_identity(payload):
        raise EngineAabbRustGateError("rust gate digest mismatch")
    derived = compare_rust_gate_runs(first_path, second_path)
    if derived != value:
        raise EngineAabbRustGateError(
            "rust gate does not re-derive from the saved run documents"
        )
    return value


__all__ = [
    "COUNTER_FIELDS",
    "DETERMINISM_FIELDS",
    "FAIL",
    "GATE_OWNER_ISSUE",
    "GATE_SCHEMA",
    "GATE_SEEDS",
    "PASS",
    "RUN_LABELS",
    "RUN_SCHEMA",
    "EngineAabbRustGateError",
    "compare_rust_gate_runs",
    "load_rust_gate_run",
    "play_rust_gate_hanchan",
    "run_rust_gate",
    "verify_rust_gate",
]
