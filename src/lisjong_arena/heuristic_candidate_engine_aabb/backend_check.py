"""校正前のPython/Rust照合P2（#517、仕様§9）。

正式runnerのRust固定は変えず、校正専用の限定照合入口として、既知の開発用seedを
同じpair・engine・RuleSetでbackendだけを変えて1回ずつ実行する。backendはprocessごとに
``LISJONG_SHANTEN_BACKEND`` で選ばれるので、2つのbackendは別processで実行する。

```text
backend-check --backend python   (LISJONG_SHANTEN_BACKEND=python)  -> python.json
backend-check --backend rust     (LISJONG_SHANTEN_BACKEND=rust)    -> rust.json
backend-compare python.json rust.json（第3のprocess）-> MATCH / MISMATCH
```

各半荘で比べるもの（完全一致だけを合格とし、許容誤差を置かない）:

- 局数、``end_reason``、``final_points``
- ``CompletedMatch`` 全体の正規化表現（各局の ``CompletedRound`` の結果・精算・
  transfer・本場・供託、``final_score`` を含む）
- Policyが返した選択行動の列（seat、正規化した ``InternalAction``）

正規化は ``canonical_engine_value``（enumは ``型名.名前``、dataclass / slotsはfield名と値、
集合・dictはsortしたlist）。digestはその値のcanonical JSONのSHA-256である。
不一致のときにRustを補正したり、Pythonでの正式実行へ自動で切り替えたりはしない（J5）。
"""

import dataclasses
import enum
import json
import os
import platform
from collections.abc import Callable
from pathlib import Path

from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    sha256_bytes,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_clean_arena_head,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity
from lisjong_arena.single_round_artifact import (
    collect_execution_provenance,
    execution_provenance_to_dict,
)

from .calibration import OWNER_ISSUE, calibration_participants
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

SCHEMA = "arena-heuristic-candidate-engine-aabb-backend-check-v1"
COMPARISON_SCHEMA = "arena-heuristic-candidate-engine-aabb-backend-comparison-v1"
BACKEND_CHECK_SEEDS = (472_005, 472_006)
"""#472の開発用seed（registry外、正式・校正populationから除外済み）。"""
PYTHON = "python"
RUST = "rust"
BACKENDS = (PYTHON, RUST)
COMPARED_FIELDS = (
    "completed_match",
    "end_reason",
    "final_points",
    "kyoku_count",
    "selected_actions",
)
MATCH = "MATCH"
MISMATCH = "MISMATCH"

_FIELDS = frozenset(
    {
        "arena_revision",
        "backend",
        "document_digest",
        "environment",
        "environment_digest",
        "hanchans",
        "owner_issue",
        "ordered_seeds",
        "participants",
        "protocol_digest",
        "protocol_id",
        "rules_digest",
        "schema",
        "timings",
    }
)


class EngineAabbBackendCheckError(EngineAabbProtocolError):
    """P2の実行・読込・照合ができない場合（不一致そのものはcomparisonに記録する）。"""


def canonical_engine_value(value: object) -> object:
    """process・backendに依存しない正規化表現（#472のP2と同じ規則）。"""
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {"__type": type(value).__name__}
        for field in dataclasses.fields(value):
            out[field.name] = canonical_engine_value(getattr(value, field.name))
        return out
    if isinstance(value, (list, tuple)):
        return [canonical_engine_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (canonical_engine_value(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if isinstance(value, dict):
        return sorted(
            (
                [canonical_engine_value(key), canonical_engine_value(item)]
                for key, item in value.items()
            ),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    slots = []
    for klass in type(value).__mro__:
        slots.extend(getattr(klass, "__slots__", ()))
    if slots:
        out = {"__type": type(value).__name__}
        for name in slots:
            if not name.startswith("__"):
                out[name] = canonical_engine_value(getattr(value, name))
        return out
    raise EngineAabbBackendCheckError(
        f"cannot canonicalize {type(value).__name__} for the backend check"
    )


def _value_digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(text.encode("utf-8"))


def backend_environment(backend: str, wheel_path: str) -> dict[str, object]:
    """このprocessのbackendと実行環境。rustは正式runと同じ ``environment_document``。"""
    if backend == RUST:
        return environment_document(wheel_path)
    if backend != PYTHON:
        raise EngineAabbBackendCheckError(f"backend must be one of {BACKENDS}")
    from lisjong_arena.shanten_backend_verification.backend import (
        require_shanten_backend,
    )

    record = require_shanten_backend(PYTHON)
    return {
        "native": None,
        "provenance": execution_provenance_to_dict(collect_execution_provenance()),
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "shanten_backend": {
            "backend": record["backend"],
            "lisjong_revision": record["lisjong_revision"],
        },
    }


def play_backend_check_hanchan(task: HanchanTask) -> PlayedHanchan:
    """worker内で1半荘を実行し、Policyの選択行動の列も返す。

    backendはworkerが継承した ``LISJONG_SHANTEN_BACKEND`` で、ここで検査する。
    """
    backend = os.environ.get("LISJONG_SHANTEN_BACKEND")
    environment = backend_environment(
        backend if backend in BACKENDS else "unset", task.wheel_path
    )
    actions = []

    def record(seat, action) -> None:
        actions.append([seat.name, canonical_engine_value(action)])

    capture = play_task(task, on_action=record)
    return PlayedHanchan(
        capture, environment, os.getpid(), current_peak_rss_kib(), tuple(actions)
    )


def _require_environment(environment: dict, backend: str) -> str:
    provenance = environment["provenance"]
    if provenance.get("lisjong_engine_revision") != ENGINE_REVISION:
        raise EngineAabbBackendCheckError(
            "installed lisjong-engine is not the protocol revision"
        )
    lisjong = provenance.get("lisjong_revision")
    if environment["shanten_backend"] != {
        "backend": backend,
        "lisjong_revision": lisjong,
    }:
        raise EngineAabbBackendCheckError(f"process backend is not {backend}")
    if (environment["native"] is None) != (backend == PYTHON):
        raise EngineAabbBackendCheckError(
            "native extension state differs from the backend"
        )
    return lisjong


def run_backend_check(
    output_path: str | Path,
    *,
    backend: str,
    wheel_path: str,
    timeout_seconds: int,
    play: Callable[[HanchanTask], PlayedHanchan] = play_backend_check_hanchan,
    environment: dict | None = None,
    arena_revision: str | None = None,
) -> dict[str, object]:
    """開発用12半荘を1つのbackendで実行し、照合用documentをwrite-onceで書く。

    ``play`` / ``environment`` / ``arena_revision`` はtestのseamで、実行では渡さない。
    """
    output = Path(output_path)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite: {output}")
    if backend not in BACKENDS:
        raise EngineAabbBackendCheckError(f"backend must be one of {BACKENDS}")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise EngineAabbBackendCheckError(
            "timeout_seconds must be an explicit positive int"
        )
    if arena_revision is None:
        try:
            arena_revision = require_clean_arena_head()
        except ExecutionSafetyError as error:
            raise EngineAabbBackendCheckError(str(error)) from error
    require_hex(arena_revision, 40, "arena_revision")
    if environment is None:
        environment = backend_environment(backend, wheel_path)
    lisjong = _require_environment(environment, backend)
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        raise EngineAabbBackendCheckError("Arena revision differs from the environment")
    candidate, incumbent = calibration_participants(lisjong)
    hanchans = placement_schedule(BACKEND_CHECK_SEEDS)
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
    # 第1層・第2層とscheduleが成り立つ実行であることも確かめる（保存はしない）
    rows, _, _ = build_records(captures, event_facts_required=True)
    verify_schedule(rows, expected_schedule(hanchans, candidate, incumbent))
    documents = []
    for item in executed:
        actions = item.selected_actions
        if actions is None or len(actions) != sum(item.capture.policy_call_counts):
            raise RunStopped("selected actions do not match the Policy call counts")
        match = item.capture.match
        completed = canonical_engine_value(match)
        documents.append(
            {
                "completed_match": completed,
                "completed_match_sha256": _value_digest(completed),
                "end_reason": canonical_engine_value(match.end_reason),
                "final_points": [
                    match.final_score.for_seat(seat).final_points for seat in EngineSeat
                ],
                "kyoku_count": len(match.history),
                "placement": item.capture.placement,
                "seed": item.capture.seed,
                "selected_actions": [list(entry) for entry in actions],
                "selected_actions_sha256": _value_digest([list(e) for e in actions]),
            }
        )
    payload = {
        "arena_revision": arena_revision,
        "backend": backend,
        "environment": environment,
        "environment_digest": document_identity(environment),
        "hanchans": documents,
        "ordered_seeds": list(BACKEND_CHECK_SEEDS),
        "owner_issue": OWNER_ISSUE,
        "participants": {
            ROLE_CANDIDATE: candidate.to_document(),
            ROLE_INCUMBENT: incumbent.to_document(),
        },
        "protocol_digest": PROTOCOL_DIGEST,
        "protocol_id": PROTOCOL_ID,
        "rules_digest": RULES_DIGEST,
        "schema": SCHEMA,
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


def load_backend_check(path: str | Path) -> dict[str, object]:
    try:
        value = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise EngineAabbBackendCheckError(
            f"cannot read backend check: {error}"
        ) from error
    if (
        type(value) is not dict
        or value.get("schema") != SCHEMA
        or set(value) != _FIELDS
    ):
        raise EngineAabbBackendCheckError("unsupported backend check document")
    payload = {key: item for key, item in value.items() if key != "document_digest"}
    if value["document_digest"] != document_identity(payload):
        raise EngineAabbBackendCheckError("backend check digest mismatch")
    if value["environment_digest"] != document_identity(value["environment"]):
        raise EngineAabbBackendCheckError("backend check environment digest mismatch")
    if value["backend"] not in BACKENDS:
        raise EngineAabbBackendCheckError("backend check has an unknown backend")
    lisjong = _require_environment(value["environment"], value["backend"])
    participants = value["participants"]
    expected = calibration_participants(lisjong)
    if (
        type(participants) is not dict
        or set(participants) != {ROLE_CANDIDATE, ROLE_INCUMBENT}
        or parse_participant(participants[ROLE_CANDIDATE], "A") != expected[0]
        or parse_participant(participants[ROLE_INCUMBENT], "B") != expected[1]
    ):
        raise EngineAabbBackendCheckError("backend check participants drifted")
    if (
        value["ordered_seeds"] != list(BACKEND_CHECK_SEEDS)
        or value["owner_issue"] != OWNER_ISSUE
        or value["protocol_id"] != PROTOCOL_ID
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules_digest"] != RULES_DIGEST
    ):
        raise EngineAabbBackendCheckError("backend check identity drifted")
    schedule = [
        (item["seed"], item["placement"])
        for item in placement_schedule(BACKEND_CHECK_SEEDS)
    ]
    if [(row["seed"], row["placement"]) for row in value["hanchans"]] != schedule:
        raise EngineAabbBackendCheckError("backend check does not cover the schedule")
    for row in value["hanchans"]:
        if row["completed_match_sha256"] != _value_digest(
            row["completed_match"]
        ) or row["selected_actions_sha256"] != _value_digest(row["selected_actions"]):
            raise EngineAabbBackendCheckError("backend check row digest mismatch")
    return value


def compare_backend_checks(
    python_path: str | Path, rust_path: str | Path
) -> dict[str, object]:
    """2つのbackendのdocumentを読み、条件の同一性と全半荘の完全一致を確かめる。"""
    python_document = load_backend_check(python_path)
    rust_document = load_backend_check(rust_path)
    if (python_document["backend"], rust_document["backend"]) != (PYTHON, RUST):
        raise EngineAabbBackendCheckError("compare a python document with a rust one")
    for name in ("arena_revision", "participants"):
        if python_document[name] != rust_document[name]:
            raise EngineAabbBackendCheckError(f"backend checks differ in {name}")
    for name in ("provenance", "python"):
        if python_document["environment"][name] != rust_document["environment"][name]:
            raise EngineAabbBackendCheckError(
                f"backend checks differ in environment {name}"
            )
    mismatches = []
    for left, right in zip(
        python_document["hanchans"], rust_document["hanchans"], strict=True
    ):
        for field in COMPARED_FIELDS:
            if left[field] != right[field]:
                mismatches.append(
                    {
                        "field": field,
                        "placement": left["placement"],
                        "seed": left["seed"],
                    }
                )
    payload = {
        "arena_revision": rust_document["arena_revision"],
        "compared_fields": list(COMPARED_FIELDS),
        "hanchan_count": len(rust_document["hanchans"]),
        "kyoku_count": sum(row["kyoku_count"] for row in rust_document["hanchans"]),
        "mismatches": mismatches,
        "python_document_digest": python_document["document_digest"],
        "rust_document_digest": rust_document["document_digest"],
        "rust_native": rust_document["environment"]["native"],
        "schema": COMPARISON_SCHEMA,
        "selected_action_count": sum(
            len(row["selected_actions"]) for row in rust_document["hanchans"]
        ),
        "status": MISMATCH if mismatches else MATCH,
    }
    return {**payload, "comparison_digest": document_identity(payload)}


__all__ = [
    "BACKEND_CHECK_SEEDS",
    "COMPARED_FIELDS",
    "COMPARISON_SCHEMA",
    "MATCH",
    "MISMATCH",
    "SCHEMA",
    "EngineAabbBackendCheckError",
    "backend_environment",
    "canonical_engine_value",
    "compare_backend_checks",
    "load_backend_check",
    "play_backend_check_hanchan",
    "run_backend_check",
]
