"""L2a / Arm Yの開発準備入口（#567、準備計画#564 §C、設計lisjong#368）。

正式protocol（K = 100）の入口を小さくして使い回さず、別のplan / report kindで
固定18半荘を1回だけ実行し、保存物の別process verifyと再現比較を行う。
強さの結果・正式判定ではない（``not_a_strength_result = true``、主統計・分類を出さない）。

```text
schedule（定数。callerは変えられない）
  development   seed 472005, 472006 × placement 0..5           12半荘
  repeat        seed 472005 × placement 0..5（同じ6半荘の再実行）  6半荘
plan（write-once）  固定schedule・3 artifactのbinding・環境・Arena revision・上限・出力先
run                 全体wallの計測開始 -> OS上限の照合 -> live gate
                    -> development（spawn worker 1） -> repeat（新しいspawn worker 1）
                    -> live gate -> 書込前検査 -> staging -> 上限確認 -> 1回のrenameで公開
<output>/           plan.json、observations.json、report.json、SHA256SUMS
  development/      raw/（局record 第1層・第2層、manifest）、actions.json、SHA256SUMS
  repeat/           同上
verify              保存物だけ（engine・Policyを起動しない）: SHA256SUMS、plan、schedule、
                    binding、行動数とPolicy呼び出し数、観測値、report（4指標・再現比較）を再導出
```

- 再利用: participant binding・live値（``lock``）、worker runtime（``runtime``）、
  ``executor.run_tasks``、資源上限（``limits``、#565）、局record（``record_store``）、
  副指標（``secondary_metrics``）
- seedはregistry外の既使用開発seed（#472）で、Seed Registryに予約・変更をしない。
  台帳のどのallocationとも重なれば止める
- 失敗・timeout・上限超過・binding不一致はevent全体STOP（outputを公開しない。
  ``diagnostics/stop-*.json`` だけ）。自動retry・seed差替え・件数削減はしない
- 再現比較の不一致はSTOPではなく、reportに ``MISMATCH`` として事実を記録する
  （準備の出口判定は実行Issueで行う）
"""

import datetime
import hashlib
import math
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from lisjong_arena import seed_registry
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
    require_new_artifact_destinations,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    RunStopped,
    current_peak_rss_kib,
    run_tasks,
)
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    AABB_LAYOUT,
    ROLE_CANDIDATE,
    ROLE_INCUMBENT,
    SEAT_COUNT,
    RecordLayout,
    verify_schedule,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    HanchanCapture,
    RecordBindings,
    VerifiedKyokuRecords,
    build_records,
    read_kyoku_records,
    require_bindings,
    write_kyoku_records,
)
from lisjong_arena.heuristic_candidate_engine_aabb.secondary_metrics import (
    DECOMPOSITION_COMPONENTS,
    summarize_seat_groups,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .limits import EventBudget, ResourceGuard, probe_os_limits, require_os_limits
from .lock import (
    EXECUTION_BRANCH,
    LearningEngineAabbLockError,
    live_values,
    require_environment,
    require_limits,
)
from .participants import parse_candidate, parse_incumbent, require_pair
from .protocol import (
    BACKEND,
    EXPECTED_RULES,
    PROTOCOL_DIGEST,
    RULES_DIGEST,
    LearningEngineAabbError,
    expected_schedule,
    placement_schedule,
)
from .result import _require_hanchan_observation, summarize_observations
from .runtime import (
    LearningHanchanTask,
    play_learning_hanchan,
    require_artifact_paths,
)

DEVELOPMENT_ID = "arena-learning-candidate-engine-aabb-development-v1"
PLAN_SCHEMA = "arena-learning-candidate-engine-aabb-development-plan-v1"
REPORT_SCHEMA = "arena-learning-candidate-engine-aabb-development-report-v1"
OBSERVATIONS_SCHEMA = "arena-learning-candidate-engine-aabb-development-observations-v1"
ACTIONS_SCHEMA = "arena-learning-candidate-engine-aabb-development-actions-v1"
STOP_SCHEMA = "arena-learning-candidate-engine-aabb-development-stop-v1"
SECONDARY_METRICS_SCHEMA = (
    "arena-learning-candidate-engine-aabb-development-secondary-facts-v1"
)
EVALUATION_KIND = "DEVELOPMENT"
OWNER_ISSUE = "lisbun/lisjong-arena#564"
IMPLEMENTATION_ISSUE = "lisbun/lisjong-arena#567"
PURPOSE = (
    "preparation check (lisjong#368 gates 3-4): fixed 18 hanchans, store -> "
    "separate-process verify -> reproduction; not a strength result, not a formal "
    "result, no primary statistic or classification"
)

KNOWN_DEVELOPMENT_SEEDS = frozenset(range(472_000, 472_007))
"""#472で使ったregistry外の開発用seed（``docs/engine-aabb-evaluation.md``）。"""
DEVELOPMENT = "development"
REPEAT = "repeat"
RUNS = (DEVELOPMENT, REPEAT)
RUN_SEEDS = {DEVELOPMENT: (472_005, 472_006), REPEAT: (472_005,)}
HANCHAN_TOTAL = 18
MATCH = "MATCH"
MISMATCH = "MISMATCH"

PLAN_FILENAME = "plan.json"
REPORT_FILENAME = "report.json"
OBSERVATIONS_FILENAME = "observations.json"
ACTIONS_FILENAME = "actions.json"
RAW_DIRNAME = "raw"
SUMS_FILENAME = "SHA256SUMS"

LAYOUT = RecordLayout(
    protocol_id=DEVELOPMENT_ID,
    kyoku_record_schema="arena-learning-candidate-engine-aabb-development-kyoku-record-v1",
    event_fact_schema="arena-learning-candidate-engine-aabb-development-event-fact-v1",
    manifest_schema="arena-learning-candidate-engine-aabb-development-manifest-v1",
    key_fields=AABB_LAYOUT.key_fields,
    validate_keys=AABB_LAYOUT.validate_keys,
    validate_seats=AABB_LAYOUT.validate_seats,
)
"""開発用の局record layout。正式rawとschema名を分け、正式rawとして読めないようにする。"""

_PLAN_FIELDS = frozenset(
    {
        "diagnostics_dir",
        "environment",
        "environment_digest",
        "evaluation_kind",
        "execution",
        "execution_target",
        "implementation_issue",
        "not_a_strength_result",
        "output_dir",
        "owner_issue",
        "participants",
        "plan_digest",
        "protocol_digest",
        "purpose",
        "rules",
        "rules_digest",
        "schedule",
        "schedule_digest",
        "schema",
    }
)
_EXECUTION_FIELDS = frozenset(
    {"artifact_paths", "backend", "limits", "wheel_path", "workers"}
)


class LearningDevelopmentError(LearningEngineAabbError):
    """開発準備入口のplan・保存物・verifyが契約を満たさない場合。"""


# ---------------------------------------------------------------------------
# schedule（定数）
# ---------------------------------------------------------------------------


def schedule_document() -> dict[str, object]:
    """固定18半荘のschedule。引数を持たない（seed・配置・件数を外から変えられない）。"""
    return {
        "protocol_id": DEVELOPMENT_ID,
        "runs": {run: placement_schedule(RUN_SEEDS[run]) for run in RUNS},
    }


def _self_check() -> None:
    runs = schedule_document()["runs"]
    if sum(len(items) for items in runs.values()) != HANCHAN_TOTAL:
        raise LearningDevelopmentError("the development schedule must be 18 hanchans")
    seeds = {item["seed"] for items in runs.values() for item in items}
    if not seeds <= KNOWN_DEVELOPMENT_SEEDS:
        raise LearningDevelopmentError("development seeds must be the known #472 seeds")


_self_check()


def require_unallocated_seeds(ledger: object) -> None:
    """開発seedがSeed Registryのどのallocation（全domain）とも重ならないこと（読取りだけ）。"""
    try:
        allocated = seed_registry.allocated_seeds(ledger)
    except seed_registry.SeedRegistryError as error:
        raise LearningDevelopmentError(
            f"seed registry cannot be used: {error}"
        ) from error
    seeds = {seed for run in RUNS for seed in RUN_SEEDS[run]}
    overlap = sorted(seeds & allocated)
    if overlap:
        raise LearningDevelopmentError(
            f"development seeds collide with registry allocations: {overlap}"
        )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def assemble_plan(
    *,
    candidate: dict,
    incumbent: dict,
    limits: object,
    wheel_path: str,
    artifact_paths: object,
    output_dir: str,
    diagnostics_dir: str,
    environment: dict,
    arena_revision: str,
    ledger: object,
) -> dict[str, object]:
    """入力を検査してplanを作る（live値はcallerが渡す）。scheduleは定数。"""
    require_unallocated_seeds(ledger)
    schedule = schedule_document()
    payload = {
        "diagnostics_dir": diagnostics_dir,
        "environment": environment,
        "environment_digest": document_identity(environment),
        "evaluation_kind": EVALUATION_KIND,
        "execution": {
            "artifact_paths": dict(require_artifact_paths(artifact_paths)),
            "backend": BACKEND,
            "limits": dict(require_limits(limits)),
            "wheel_path": wheel_path,
            "workers": 1,
        },
        "execution_target": {"branch": EXECUTION_BRANCH, "revision": arena_revision},
        "implementation_issue": IMPLEMENTATION_ISSUE,
        "not_a_strength_result": True,
        "output_dir": output_dir,
        "owner_issue": OWNER_ISSUE,
        "participants": {ROLE_CANDIDATE: candidate, ROLE_INCUMBENT: incumbent},
        "protocol_digest": PROTOCOL_DIGEST,
        "purpose": PURPOSE,
        "rules": dict(EXPECTED_RULES),
        "rules_digest": RULES_DIGEST,
        "schedule": schedule,
        "schedule_digest": document_identity(schedule),
        "schema": PLAN_SCHEMA,
    }
    return parse_plan({**payload, "plan_digest": document_identity(payload)})


def parse_plan(value: object) -> dict[str, object]:
    """保存済みplanを厳密に検査する（live値・ledgerは見ない）。"""
    try:
        return _parse_plan(value)
    except LearningDevelopmentError:
        raise
    except LearningEngineAabbError as error:
        raise LearningDevelopmentError(f"invalid plan: {error}") from error
    except (ArtifactValidationError, TypeError, KeyError, ValueError) as error:
        raise LearningDevelopmentError(
            f"invalid plan: {type(error).__name__}: {error}"
        ) from error


def _parse_plan(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != PLAN_SCHEMA:
        raise LearningDevelopmentError("unsupported development plan schema")
    if set(value) != _PLAN_FIELDS:
        raise LearningDevelopmentError(
            "development plan fields do not match the schema"
        )
    if (
        value["evaluation_kind"] != EVALUATION_KIND
        or value["not_a_strength_result"] is not True
        or value["owner_issue"] != OWNER_ISSUE
        or value["implementation_issue"] != IMPLEMENTATION_ISSUE
        or value["purpose"] != PURPOSE
    ):
        raise LearningDevelopmentError("development plan identity drifted")
    if (
        value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules"] != EXPECTED_RULES
        or value["rules_digest"] != RULES_DIGEST
    ):
        raise LearningDevelopmentError("development plan protocol / rules drifted")
    schedule = schedule_document()
    if value["schedule"] != schedule or value["schedule_digest"] != document_identity(
        schedule
    ):
        raise LearningDevelopmentError(
            "development schedule differs from the fixed 18 hanchans"
        )
    participants = value["participants"]
    if type(participants) is not dict or set(participants) != {
        ROLE_CANDIDATE,
        ROLE_INCUMBENT,
    }:
        raise LearningDevelopmentError("development plan participants must be A and B")
    candidate = parse_candidate(participants[ROLE_CANDIDATE])
    incumbent = parse_incumbent(participants[ROLE_INCUMBENT])
    require_pair(candidate, incumbent)
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        raise LearningDevelopmentError("plan execution fields do not match the schema")
    if execution["backend"] != BACKEND or execution["workers"] != 1:
        raise LearningDevelopmentError("plan execution must be one rust worker")
    require_limits(execution["limits"])
    require_artifact_paths(execution["artifact_paths"])
    if (
        type(execution["wheel_path"]) is not str
        or not Path(execution["wheel_path"]).is_absolute()
    ):
        raise LearningDevelopmentError("plan wheel_path must be an absolute path")
    require_environment(value["environment"], candidate)
    if value["environment_digest"] != document_identity(value["environment"]):
        raise LearningDevelopmentError("plan environment digest mismatch")
    target = value["execution_target"]
    if type(target) is not dict or set(target) != {"branch", "revision"}:
        raise LearningDevelopmentError("plan execution_target fields do not match")
    if target["branch"] != EXECUTION_BRANCH or target["revision"] != value[
        "environment"
    ]["provenance"].get("lisjong_arena_revision"):
        raise LearningDevelopmentError("plan revision differs from the environment")
    if incumbent["serving"]["arena_revision"] != target["revision"]:
        raise LearningDevelopmentError(
            "incumbent serving revision differs from the execution target"
        )
    paths = [value["output_dir"], value["diagnostics_dir"]]
    if any(type(item) is not str or not Path(item).is_absolute() for item in paths):
        raise LearningDevelopmentError("plan output / diagnostics must be absolute")
    if Path(paths[0]).resolve(strict=False) == Path(paths[1]).resolve(strict=False):
        raise LearningDevelopmentError("plan output and diagnostics must differ")
    payload = {key: item for key, item in value.items() if key != "plan_digest"}
    if value["plan_digest"] != document_identity(payload):
        raise LearningDevelopmentError("development plan digest mismatch")
    return value


def _live_head() -> str:
    try:
        head = require_clean_arena_head()
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
    except ExecutionSafetyError as error:
        raise LearningDevelopmentError(str(error)) from error
    return head


def build_plan(
    *,
    limits: object,
    wheel_path: str,
    artifact_paths: dict,
    output_dir: str,
    diagnostics_dir: str,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
) -> dict[str, object]:
    """live値（clean merged HEAD、環境、3 artifactを実際にloadしたbinding）でplanを作る。"""
    try:
        ledger = seed_registry.load_ledger(ledger_path)
    except seed_registry.SeedRegistryError as error:
        raise LearningDevelopmentError(
            f"seed registry cannot be used: {error}"
        ) from error
    try:
        require_new_artifact_destinations(
            {"diagnostics": diagnostics_dir, "output": output_dir},
            required_names=("diagnostics", "output"),
        )
    except ExecutionSafetyError as error:
        raise LearningDevelopmentError(str(error)) from error
    head = _live_head()
    environment, candidate, incumbent = live_values(wheel_path, artifact_paths, head)
    if environment["provenance"].get("lisjong_arena_revision") != head:
        raise LearningDevelopmentError(
            "Arena HEAD differs from the collected provenance"
        )
    return assemble_plan(
        candidate=candidate,
        incumbent=incumbent,
        limits=limits,
        wheel_path=wheel_path,
        artifact_paths=artifact_paths,
        output_dir=output_dir,
        diagnostics_dir=diagnostics_dir,
        environment=environment,
        arena_revision=head,
        ledger=ledger,
    )


def save_plan(document: dict, path: str | Path) -> Path:
    parse_plan(document)
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite plan: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_plan(path: str | Path) -> dict[str, object]:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise LearningDevelopmentError(f"cannot read plan: {error}") from error
    return parse_plan(document)


def require_live_development(
    plan: dict,
    *,
    ledger: object,
    live: tuple[dict, dict, dict] | None = None,
    head: str | None = None,
) -> None:
    """実行の直前・直後に、live HEAD・環境・participant binding・seedの未予約を照合する。

    ``live`` / ``head`` はtestでlive値を差し替えるseamで、実行では渡さない。
    """
    parsed = parse_plan(plan)
    live_head = _live_head() if head is None else head
    if live_head != parsed["execution_target"]["revision"]:
        raise LearningDevelopmentError("live Arena HEAD differs from the plan")
    execution = parsed["execution"]
    try:
        environment, candidate, incumbent = (
            live_values(execution["wheel_path"], execution["artifact_paths"], live_head)
            if live is None
            else live
        )
    except LearningEngineAabbLockError as error:
        raise LearningDevelopmentError(str(error)) from error
    if environment != parsed["environment"]:
        raise LearningDevelopmentError(
            "live execution environment differs from the plan"
        )
    participants = parsed["participants"]
    if candidate != participants[ROLE_CANDIDATE]:
        raise LearningDevelopmentError("live candidate binding differs from the plan")
    if incumbent != participants[ROLE_INCUMBENT]:
        raise LearningDevelopmentError("live incumbent binding differs from the plan")
    require_unallocated_seeds(ledger)


# ---------------------------------------------------------------------------
# 保存物の部品
# ---------------------------------------------------------------------------


def _bindings(plan: dict) -> RecordBindings:
    return RecordBindings(
        lock_digest=plan["plan_digest"], environment_digest=plan["environment_digest"]
    )


def _identities(plan: dict) -> tuple[str, str]:
    participants = plan["participants"]
    return (
        participants[ROLE_CANDIDATE]["policy_identity"],
        participants[ROLE_INCUMBENT]["policy_identity"],
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_run_records(plan: dict, run: str, records: VerifiedKyokuRecords) -> None:
    """recordがrunの固定schedule・participant・plan bindingに一致すること。"""
    try:
        verify_schedule(
            records.hanchans,
            expected_schedule(plan["schedule"]["runs"][run], *_identities(plan)),
        )
        require_bindings(records, _bindings(plan))
    except LearningEngineAabbError:
        raise
    except Exception as error:
        raise LearningDevelopmentError(
            f"{run} records do not match the plan: {error}"
        ) from error


def actions_document(plan: dict, run: str, executed) -> dict[str, object]:
    hanchans = []
    for item in executed:
        if item.selected_actions is None:
            raise LearningDevelopmentError("the worker did not record selected actions")
        actions = [list(entry) for entry in item.selected_actions]
        hanchans.append(
            {
                "actions": actions,
                "actions_sha256": _sha256_text(canonical_json_text(actions)),
                "placement": item.capture.placement,
                "seed": item.capture.seed,
            }
        )
    return {
        "hanchans": hanchans,
        "plan_digest": plan["plan_digest"],
        "run": run,
        "schema": ACTIONS_SCHEMA,
    }


def parse_actions(
    plan: dict, run: str, value: object, records: VerifiedKyokuRecords
) -> dict:
    """行動列のschema・schedule・hashと、seatごとの行動数 = Policy呼び出し数を検査する。"""
    if type(value) is not dict or set(value) != {
        "hanchans",
        "plan_digest",
        "run",
        "schema",
    }:
        raise LearningDevelopmentError(f"{run} actions fields do not match the schema")
    if (
        value["schema"] != ACTIONS_SCHEMA
        or value["plan_digest"] != plan["plan_digest"]
        or value["run"] != run
    ):
        raise LearningDevelopmentError(f"{run} actions belong to another plan / run")
    schedule = plan["schedule"]["runs"][run]
    hanchans = value["hanchans"]
    if type(hanchans) is not list or len(hanchans) != len(schedule):
        raise LearningDevelopmentError(f"{run} actions must cover every hanchan")
    seat_names = ("EAST", "SOUTH", "WEST", "NORTH")
    for index, (item, expected, row) in enumerate(
        zip(hanchans, schedule, records.hanchans, strict=True)
    ):
        context = f"{run} actions[{index}]"
        if type(item) is not dict or set(item) != {
            "actions",
            "actions_sha256",
            "placement",
            "seed",
        }:
            raise LearningDevelopmentError(f"{context} fields do not match the schema")
        if (item["seed"], item["placement"]) != (
            expected["seed"],
            expected["placement"],
        ):
            raise LearningDevelopmentError(f"{context} is not the scheduled hanchan")
        actions = item["actions"]
        if type(actions) is not list or any(
            type(entry) is not list or len(entry) != 2 or entry[0] not in seat_names
            for entry in actions
        ):
            raise LearningDevelopmentError(f"{context} actions are malformed")
        if item["actions_sha256"] != _sha256_text(canonical_json_text(actions)):
            raise LearningDevelopmentError(f"{context} actions hash mismatch")
        counts = [
            sum(1 for entry in actions if entry[0] == seat_names[seat])
            for seat in range(SEAT_COUNT)
        ]
        if counts != list(row["policy_call_counts"]):
            raise LearningDevelopmentError(
                f"{context} action counts differ from the Policy call counts"
            )
    return value


_ROLES = (ROLE_CANDIDATE, ROLE_INCUMBENT)
EXCLUDED_RATES = frozenset({"mean_rank"})
"""平均順位は正式protocolの主指標の材料なので、開発reportには出さない。"""
POINT_COMPONENTS = tuple(name for name, _ in DECOMPOSITION_COMPONENTS)
"""点数分解のうち局の精算（和了収入・放銃支払等）だけ。最終点・順位点は出さない。"""


def _rates(paired: dict) -> dict[str, object]:
    """seedごと・roleごとの分子 / 分母 / 率だけ（A − Bの差・SD・区間は落とす）。"""
    return {
        name: {
            "denominator": item["denominator"],
            "numerator": item["numerator"],
            "per_seed": [
                {"seed": row["seed"], **{role: row[role] for role in _ROLES}}
                for row in item["per_seed"]
            ],
        }
        for name, item in paired.items()
        if name not in EXCLUDED_RATES
    }


def _metrics_facts(layer: dict | None) -> dict[str, object] | None:
    if layer is None:
        return None
    # conditionalはrole別の分子 / 分母 / 値だけで、差・区間を持たない
    return {"conditional": layer["conditional"], "rates": _rates(layer["paired"])}


def secondary_metrics(records: VerifiedKyokuRecords) -> dict[str, object]:
    """正式resultと同じ定義・同じ関数で数えた局指標の、role別の事実だけ（#567）。

    共有部品 ``summarize_seat_groups`` の出力から、seedごと・roleごとの分子 / 分母 / 率と
    局精算の点数分解だけを射影する。A − Bの差、平均順位・順位分布、最終点、SD・区間は
    出さない（開発reportは主統計・区間・classificationを持たない）。
    """
    groups: dict[int, list] = {}
    for index, row in enumerate(records.hanchans):
        groups.setdefault(row["seed"], []).extend(
            (index, seat, row["seats"][seat]["role"]) for seat in range(SEAT_COUNT)
        )
    summary = summarize_seat_groups(
        records, list(groups.items()), layout=LAYOUT, schema=SECONDARY_METRICS_SCHEMA
    )
    return {
        "event_fact_metrics": _metrics_facts(summary["event_fact_metrics"]),
        "event_fact_status": summary["event_fact_status"],
        "facts_only": True,
        "kyoku_metrics": _metrics_facts(summary["kyoku_metrics"]),
        "point_components": [
            {
                "seed": item["seed"],
                **{
                    role: {name: item[role][name] for name in POINT_COMPONENTS}
                    for role in _ROLES
                },
            }
            for item in summary["point_decomposition"]
        ],
        "schema": SECONDARY_METRICS_SCHEMA,
        "seeds": summary["seeds"],
    }


def reproduction(
    records: dict[str, VerifiedKyokuRecords], actions: dict[str, dict]
) -> dict[str, object]:
    """repeatの各半荘を、developmentの同じseed・配置と決定的payloadで比べる。

    比べるのは局record（局精算・最終点・rank・呼び出し数）、第2層（状態とrow）、
    選択行動の列。時間・RSS・path・timestampは保存物の決定的payloadに含まれない。
    """

    def index(run: str) -> dict:
        rows = records[run].hanchans
        events = records[run].event_facts
        status = records[run].event_fact_status.to_value()
        return {
            (row["seed"], row["placement"]): {
                "actions": action["actions"],
                "event_facts": {
                    "row": None if events is None else events[position],
                    "status": status,
                },
                "kyoku_record": row,
            }
            for position, (row, action) in enumerate(
                zip(rows, actions[run]["hanchans"], strict=True)
            )
        }

    original, repeated = index(DEVELOPMENT), index(REPEAT)
    hanchans = []
    for item in schedule_document()["runs"][REPEAT]:
        key = (item["seed"], item["placement"])
        first, second = original[key], repeated[key]
        differing = []
        for name in ("kyoku_record", "event_facts"):
            for field in sorted(set(first[name]) | set(second[name])):
                if first[name].get(field) != second[name].get(field):
                    differing.append(f"{name}.{field}")
        if first["actions"] != second["actions"]:
            differing.append("actions")
        hanchans.append(
            {
                "differing": differing,
                "placement": item["placement"],
                "seed": item["seed"],
                "status": MATCH if not differing else MISMATCH,
            }
        )
    return {
        "compared": "kyoku records, event facts and selected actions; time, RSS, "
        "path and timestamps are excluded",
        "hanchans": hanchans,
        "seeds": list(RUN_SEEDS[REPEAT]),
        "status": MATCH
        if all(item["status"] == MATCH for item in hanchans)
        else MISMATCH,
    }


def build_report(
    plan: dict,
    records: dict[str, VerifiedKyokuRecords],
    actions: dict[str, dict],
    *,
    actions_sha256: dict[str, str],
    observations_sha256: str,
) -> dict[str, object]:
    """保存物から再導出できる事実だけのreport。主統計・分類は出さない。"""
    for run in RUNS:
        require_run_records(plan, run, records[run])
        parse_actions(plan, run, actions[run], records[run])
    payload = {
        "aggregation_revision": plan["execution_target"]["revision"],
        "evaluation_kind": EVALUATION_KIND,
        "not_a_strength_result": True,
        "observations_sha256": observations_sha256,
        "plan_digest": plan["plan_digest"],
        "reproduction": reproduction(records, actions),
        "runs": {
            run: {
                "actions_sha256": actions_sha256[run],
                "event_fact_status": records[run].event_fact_status.to_value(),
                "hanchan_count": len(records[run].hanchans),
                "kyoku_count": sum(len(row["kyokus"]) for row in records[run].hanchans),
                "raw_manifest_sha256": records[run].manifest_sha256,
                "secondary_metrics": secondary_metrics(records[run]),
            }
            for run in RUNS
        },
        "schema": REPORT_SCHEMA,
    }
    return {**payload, "report_digest": document_identity(payload)}


# ---------------------------------------------------------------------------
# observations（時間 / RSS / bytes。決定的再導出の対象外）
# ---------------------------------------------------------------------------

_OBSERVATION_FIELDS = frozenset(
    {
        "not_deterministic",
        "output_bytes",
        "parent_peak_rss_kib",
        "phase_seconds",
        "plan_digest",
        "runs",
        "schema",
        "summary",
    }
)
PHASES = (
    "pre_run_gate",
    DEVELOPMENT,
    REPEAT,
    "post_run_gate",
    "pre_write_checks",
    "records_written",
)


def _observation_rows(executed) -> list[dict]:
    rows = []
    for item in executed:
        analysis = item.policy_analysis or {}
        rows.append(
            {
                "decision_seconds": analysis.get("decision_seconds"),
                "decisions": analysis.get("decisions"),
                "peak_rss_kib": item.peak_rss_kib,
                "placement": item.capture.placement,
                "seed": item.capture.seed,
                "wall_seconds": item.wall_seconds,
            }
        )
    return rows


def parse_observations(plan: dict, value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _OBSERVATION_FIELDS:
        raise LearningDevelopmentError("observation fields do not match the schema")
    if value["schema"] != OBSERVATIONS_SCHEMA or value["not_deterministic"] is not True:
        raise LearningDevelopmentError("unsupported observation schema")
    if value["plan_digest"] != plan["plan_digest"]:
        raise LearningDevelopmentError("observations belong to another plan")
    runs = value["runs"]
    if type(runs) is not dict or set(runs) != set(RUNS):
        raise LearningDevelopmentError("observations must cover both runs")
    try:
        for run in RUNS:
            schedule = plan["schedule"]["runs"][run]
            rows = runs[run]
            if type(rows) is not list or len(rows) != len(schedule):
                raise LearningDevelopmentError(
                    f"{run} observations must cover every hanchan"
                )
            for index, (item, expected) in enumerate(zip(rows, schedule, strict=True)):
                _require_hanchan_observation(item, expected, f"{run}[{index}]")
    except LearningDevelopmentError:
        raise
    except LearningEngineAabbError as error:
        raise LearningDevelopmentError(str(error)) from error
    if value["summary"] != {run: summarize_observations(runs[run]) for run in RUNS}:
        raise LearningDevelopmentError("observation summary does not match")
    parent = value["parent_peak_rss_kib"]
    if type(parent) is not int or parent <= 0:
        raise LearningDevelopmentError("parent_peak_rss_kib must be a positive int")
    phases = value["phase_seconds"]
    if type(phases) is not dict or set(phases) != set(PHASES):
        raise LearningDevelopmentError("phase_seconds fields do not match")
    previous = 0.0
    for name in PHASES:
        item = phases[name]
        if type(item) not in (int, float) or not math.isfinite(item) or item < previous:
            raise LearningDevelopmentError(
                "phase_seconds must be finite and non-decreasing"
            )
        previous = item
    sizes = value["output_bytes"]
    if (
        type(sizes) is not dict
        or set(sizes) != set(RUNS)
        or any(type(size) is not int or size <= 0 for size in sizes.values())
    ):
        raise LearningDevelopmentError("output_bytes must hold positive ints per run")
    return value


# ---------------------------------------------------------------------------
# SHA256SUMS
# ---------------------------------------------------------------------------


def _files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path != directory / SUMS_FILENAME
    )


def _sums_text(directory: Path, files: list[Path]) -> str:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
        f"{path.relative_to(directory).as_posix()}\n"
        for path in files
    ]
    return "".join(lines)


def write_sums(directory: Path) -> None:
    write_new_artifact_file(
        directory / SUMS_FILENAME, _sums_text(directory, _files(directory))
    )


def require_sums(directory: Path) -> None:
    """SHA256SUMSが、directory内の全file（自分以外）と過不足なく一致すること。"""
    path = directory / SUMS_FILENAME
    try:
        recorded = path.read_text(encoding="utf-8")
    except OSError as error:
        raise LearningDevelopmentError(f"cannot read {path}: {error}") from error
    if recorded != _sums_text(directory, _files(directory)):
        raise LearningDevelopmentError(
            f"{path} does not match the stored files (missing, extra or changed)"
        )


def _directory_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in _files(directory))


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _tasks(plan: dict, run: str) -> list[LearningHanchanTask]:
    participants = plan["participants"]
    execution = plan["execution"]
    return [
        LearningHanchanTask(
            seed=item["seed"],
            placement=item["placement"],
            candidate=participants[ROLE_CANDIDATE],
            incumbent=participants[ROLE_INCUMBENT],
            artifact_paths=execution["artifact_paths"],
            wheel_path=execution["wheel_path"],
            record_actions=True,
        )
        for item in plan["schedule"]["runs"][run]
    ]


def _write_stop(plan: dict, error: BaseException, done: int) -> None:
    diagnostics = Path(plan["diagnostics_dir"])
    diagnostics.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    document = {
        "completed_hanchans": done,
        "error": f"{type(error).__name__}: {error}",
        "not_a_result": True,
        "plan_digest": plan["plan_digest"],
        "schema": STOP_SCHEMA,
        "status": "STOP / INVALID",
    }
    write_new_artifact_file(
        diagnostics / f"stop-{stamp}.json", canonical_json_text(document)
    )


def _publish(
    plan: dict,
    executed: dict[str, list],
    output: Path,
    *,
    phases: dict[str, float],
    budget: EventBudget,
    parent_peak_rss_kib: Callable[[], int | None],
    before_publish: Callable[[], None],
) -> dict[str, object]:
    """stagingへ全部書き、上限を確かめ、1回のrenameで公開する（失敗時はstagingを捨てる）。"""
    staging = output.with_name(f".{output.name}.staging-{uuid.uuid4().hex}")
    staging.mkdir()
    try:
        write_new_artifact_file(staging / PLAN_FILENAME, canonical_json_text(plan))
        records, actions, actions_sha256, sizes = {}, {}, {}, {}
        for run in RUNS:
            directory = staging / run
            directory.mkdir()
            records[run] = write_kyoku_records(
                directory / RAW_DIRNAME,
                [item.capture for item in executed[run]],
                producer_revision=plan["execution_target"]["revision"],
                event_facts_required=False,
                bindings=_bindings(plan),
                layout=LAYOUT,
                capture_type=HanchanCapture,
            )
            actions[run] = actions_document(plan, run, executed[run])
            text = canonical_json_text(actions[run])
            write_new_artifact_file(directory / ACTIONS_FILENAME, text)
            actions_sha256[run] = _sha256_text(text)
            write_sums(directory)
            sizes[run] = _directory_bytes(directory)
        phases["records_written"] = budget.elapsed()
        parent = parent_peak_rss_kib()
        runs = {run: _observation_rows(executed[run]) for run in RUNS}
        observations = {
            "not_deterministic": True,
            "output_bytes": sizes,
            "parent_peak_rss_kib": parent,
            "phase_seconds": dict(phases),
            "plan_digest": plan["plan_digest"],
            "runs": runs,
            "schema": OBSERVATIONS_SCHEMA,
            "summary": {run: summarize_observations(runs[run]) for run in RUNS},
        }
        parse_observations(plan, observations)
        text = canonical_json_text(observations)
        write_new_artifact_file(staging / OBSERVATIONS_FILENAME, text)
        report = build_report(
            plan,
            records,
            actions,
            actions_sha256=actions_sha256,
            observations_sha256=_sha256_text(text),
        )
        write_new_artifact_file(staging / REPORT_FILENAME, canonical_json_text(report))
        write_sums(staging)
        if output.exists():
            raise FileExistsError("the output directory appeared during the run")
        before_publish()
        staging.rename(output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def run_development(
    plan_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play: Callable[[LearningHanchanTask], object] = play_learning_hanchan,
    live_gate: Callable[..., None] = require_live_development,
    on_hanchan: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    realtime: Callable[[], float] = time.time,
    os_limits: Callable[[], dict] = probe_os_limits,
    parent_peak_rss_kib: Callable[[], int | None] = current_peak_rss_kib,
) -> dict[str, object]:
    """planした固定18半荘を実行し、保存したreportを返す。

    ``play`` / ``live_gate`` / ``clock`` / ``realtime`` / ``os_limits`` /
    ``parent_peak_rss_kib`` はtestの差替え用seamで、実行では渡さない。
    """
    started = (clock(), realtime())
    plan = load_plan(plan_path)
    limits = plan["execution"]["limits"]
    budget = EventBudget(
        limits["wall_seconds"], clock=clock, realtime=realtime, started=started
    )
    guard = ResourceGuard(limits, budget, parent_peak_rss_kib)
    output = Path(plan["output_dir"])
    if output.exists():
        raise LearningDevelopmentError(
            "output already exists; development outputs are write-once and never re-run"
        )
    progress = {"done": 0}
    phases: dict[str, float] = {}
    executed: dict[str, list] = {}

    def gate() -> None:
        live_gate(plan, ledger=seed_registry.load_ledger(ledger_path))

    try:
        require_os_limits(limits, os_limits())
        gate()
        budget.check("after the pre-run live gate")
        phases["pre_run_gate"] = budget.elapsed()
        for run in RUNS:
            offset = progress["done"]

            def count(done: int, total: int, offset=offset) -> None:
                progress["done"] = offset + done
                if on_hanchan is not None:
                    on_hanchan(offset + done, HANCHAN_TOTAL)

            executed[run] = run_tasks(
                _tasks(plan, run),
                key_fields=("placement",),
                timeout_seconds=limits["timeout_seconds"],
                expected_environment=plan["environment"],
                play=play,
                on_hanchan=count,
                on_executed=guard,
                remaining_seconds=budget.remaining,
            )
            phases[run] = budget.elapsed()
        gate()
        budget.check("after the post-run live gate")
        phases["post_run_gate"] = budget.elapsed()
        # 書込前に、両runの第1層・schedule・行動列が成り立つことを確かめる
        for run in RUNS:
            hanchans, _, _ = build_records(
                [item.capture for item in executed[run]],
                event_facts_required=False,
                layout=LAYOUT,
            )
            verify_schedule(
                hanchans,
                expected_schedule(plan["schedule"]["runs"][run], *_identities(plan)),
            )
            actions_document(plan, run, executed[run])
        budget.check("after the pre-write checks")
        phases["pre_write_checks"] = budget.elapsed()
        everything = [item for run in RUNS for item in executed[run]]
        report = _publish(
            plan,
            executed,
            output,
            phases=phases,
            budget=budget,
            parent_peak_rss_kib=parent_peak_rss_kib,
            before_publish=lambda: guard.before_publish(everything),
        )
    except BaseException as error:
        _write_stop(plan, error, progress["done"])
        if not isinstance(error, Exception) or isinstance(
            error, (RunStopped, LearningEngineAabbError)
        ):
            raise
        raise RunStopped(f"STOP / INVALID: {type(error).__name__}: {error}") from error
    return report


# ---------------------------------------------------------------------------
# verify（保存物だけ。engine・Policyを起動しない）
# ---------------------------------------------------------------------------


def _read(path: Path) -> object:
    try:
        document = read_json_document(path)
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise LearningDevelopmentError(f"cannot read {path}: {error}") from error
    if path.read_text(encoding="utf-8") != canonical_json_text(document):
        raise LearningDevelopmentError(f"{path} is not canonical")
    return document


def verify_development(output_dir: str | Path) -> dict[str, object]:
    """保存物だけからreportを再導出し、記録値と完全一致することを確かめる。"""
    output = Path(output_dir)
    require_sums(output)
    for run in RUNS:
        require_sums(output / run)
    plan = parse_plan(_read(output / PLAN_FILENAME))
    records, actions, actions_sha256 = {}, {}, {}
    for run in RUNS:
        try:
            records[run] = read_kyoku_records(
                output / run / RAW_DIRNAME, event_facts_required=False, layout=LAYOUT
            )
        except Exception as error:
            raise LearningDevelopmentError(
                f"{run} raw record is invalid: {error}"
            ) from error
        path = output / run / ACTIONS_FILENAME
        actions[run] = _read(path)
        actions_sha256[run] = _sha256_text(path.read_text(encoding="utf-8"))
    observations_path = output / OBSERVATIONS_FILENAME
    parse_observations(plan, _read(observations_path))
    recorded = _read(output / REPORT_FILENAME)
    if type(recorded) is not dict or recorded.get("schema") != REPORT_SCHEMA:
        raise LearningDevelopmentError("unsupported development report schema")
    rederived = build_report(
        plan,
        records,
        actions,
        actions_sha256=actions_sha256,
        observations_sha256=_sha256_text(observations_path.read_text(encoding="utf-8")),
    )
    if recorded != rederived:
        differing = sorted(
            key
            for key in set(recorded) | set(rederived)
            if recorded.get(key) != rederived.get(key)
        )
        raise LearningDevelopmentError(
            f"recorded report differs from the re-derivation: {differing}"
        )
    return rederived


__all__ = [
    "DEVELOPMENT",
    "DEVELOPMENT_ID",
    "EVALUATION_KIND",
    "HANCHAN_TOTAL",
    "KNOWN_DEVELOPMENT_SEEDS",
    "LAYOUT",
    "MATCH",
    "MISMATCH",
    "PLAN_SCHEMA",
    "REPEAT",
    "REPORT_SCHEMA",
    "RUNS",
    "RUN_SEEDS",
    "LearningDevelopmentError",
    "assemble_plan",
    "build_plan",
    "build_report",
    "load_plan",
    "parse_plan",
    "require_live_development",
    "require_unallocated_seeds",
    "run_development",
    "save_plan",
    "schedule_document",
    "verify_development",
]
