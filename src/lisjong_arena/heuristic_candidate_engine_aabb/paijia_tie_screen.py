"""#520 段階2: 1候補の固定開発比較（32 seeds × 6配置 = 192半荘、1回、DEVELOPMENT）。

正式runと同じexecutor・局単位record（#513）・block統計（#514）を使う。違いは、Policyを
観測用proxyで包み、Aのanalysis（stage・切替）とA/Bの判断時間を客観recordとは別の
``policy-analysis.json`` に保存することだけである。

```text
run     live gate -> 192半荘（seed順 -> placement 0..5） -> live gate
        -> 第1層・第2層・scheduleを書込前に検査
        -> <output>/{raw/, policy-analysis.json, summary.json, timings.json}
verify  保存したplan・raw・policy-analysisだけからsummaryを再導出（別process、offline）
report  verifyに通った保存物から時間・RSS・容量・時計差を作る（再導出可能）
```

- 主指標 ``D_s = (S_A - S_B) / 120``（seed等重み）。区間は記述用で、正式classificationは出さない
- 進行条件（実行前に固定）: D平均 > 0、素点成分のA−B平均 >= 0、seed等重みの放銃率差
  A−B <= +0.01、整合性（全件・第1層/第2層VALID・analysis整合・上限内）
  -> ``PROMISING-DEVELOPMENT`` / ``NOT-PROMISING-DEVELOPMENT``（採用ではない）
- 失敗・timeout・上限超過・analysisの不整合はSTOP-INCOMPLETEで、部分標本の結果を出さない
- rehearsal（既知の開発用seed 2つ × 6配置）は同じ経路の動作確認で、進行判定を出さない
"""

from collections.abc import Callable
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .executor import ExecutedHanchan, HanchanTask, PlayedHanchan, RunStopped
from .kyoku_record import ROLE_CANDIDATE, ROLE_INCUMBENT, verify_schedule
from .paijia_tie_plan import (
    ALLOCATED,
    EVALUATION_KIND,
    SCREEN,
    SUMMARY_FILENAME,
    TIMINGS_FILENAME,
    EngineAabbPaijiaTieError,
    clock_document,
    default_clocks,
    discard_change_kind,
    distribution,
    document_sha256,
    execute_plan,
    load_plan,
    memory_summary,
    output_files,
    plan_participants,
    plan_schedule,
    read_document,
    require_live_target,
    require_switch_analysis,
    require_timings,
    seconds,
)
from .paijia_tie_worker import (
    CHANGE_OUTCOMES,
    NO_ANALYSIS,
    STAGE_SWITCH,
    play_screen_hanchan,
)
from .protocol import (
    INTERNAL_UNITS_PER_POINT,
    SEAT_RESULTS_PER_ROLE_PER_BLOCK,
    expected_schedule,
)
from .record_store import (
    RecordBindings,
    build_records,
    read_kyoku_records,
    require_bindings,
    write_kyoku_records,
)
from .secondary_metrics import summarize_secondary_metrics
from .statistics import block_totals, summarize_blocks

ANALYSIS_SCHEMA = "arena-heuristic-candidate-engine-aabb-paijia-tie-analysis-v1"
SUMMARY_SCHEMA = "arena-heuristic-candidate-engine-aabb-paijia-tie-screen-v1"
REPORT_SCHEMA = "arena-heuristic-candidate-engine-aabb-paijia-tie-screen-report-v1"
RAW_DIRNAME = "raw"
ANALYSIS_FILENAME = "policy-analysis.json"
REPORT_FILENAME = "report.json"
PROMISING = "PROMISING-DEVELOPMENT"
NOT_PROMISING = "NOT-PROMISING-DEVELOPMENT"
COMPLETE_STATUS = "SCREEN COMPLETE; DEVELOPMENT, NOT A FORMAL RESULT"
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO PROGRESSION DECISION"

_DIVISOR = SEAT_RESULTS_PER_ROLE_PER_BLOCK * INTERNAL_UNITS_PER_POINT
_KINDS = (*CHANGE_OUTCOMES, "tsumogiri-only")


def _bindings(plan: dict) -> RecordBindings:
    return RecordBindings(
        lock_digest=plan["plan_digest"], environment_digest=plan["environment_digest"]
    )


def _expected(plan: dict):
    candidate, incumbent = plan_participants(plan)
    return expected_schedule(plan_schedule(plan), candidate, incumbent)


# ---------------------------------------------------------------------------
# Policy analysis（客観recordとは別の保存物）
# ---------------------------------------------------------------------------


def _require_analysis(plan: dict, analysis: object, hanchans) -> None:
    """policy-analysisの形と、判断数・roleを第1層recordと照合する。"""
    schedule = plan_schedule(plan)
    if (
        type(analysis) is not dict
        or set(analysis) != {"hanchans", "plan_digest", "schema"}
        or analysis["schema"] != ANALYSIS_SCHEMA
        or analysis["plan_digest"] != plan["plan_digest"]
        or type(analysis["hanchans"]) is not list
        or len(analysis["hanchans"]) != len(schedule)
    ):
        raise EngineAabbPaijiaTieError("policy analysis does not belong to the plan")
    for item, observed, record in zip(
        schedule, analysis["hanchans"], hanchans, strict=True
    ):
        context = f"seed {item['seed']} placement {item['placement']}"
        if (
            type(observed) is not dict
            or set(observed) != {"placement", "seats", "seed"}
            or (observed["seed"], observed["placement"])
            != (item["seed"], item["placement"])
            or type(observed["seats"]) is not list
            or [seat.get("seat") for seat in observed["seats"]] != [0, 1, 2, 3]
        ):
            raise EngineAabbPaijiaTieError(f"{context}: analysis seats do not match")
        for seat, role, count in zip(
            observed["seats"],
            item["roles"],
            record["policy_call_counts"],
            strict=True,
        ):
            _require_seat(seat, role, count, f"{context} seat {seat['seat']}")


def _require_seat(seat: dict, role: str, count: int, context: str) -> None:
    fields = {"decision_seconds", "role", "seat"}
    if role == ROLE_CANDIDATE:
        fields |= {"stage_counts", "switches"}
    if set(seat) != fields or seat["role"] != role:
        raise EngineAabbPaijiaTieError(f"{context}: role or fields do not match")
    values = seat["decision_seconds"]
    if type(values) is not list or len(values) != count:
        raise EngineAabbPaijiaTieError(
            f"{context}: decisions differ from the Policy call count"
        )
    for value in values:
        seconds(value, f"{context} decision_seconds")
    if role == ROLE_INCUMBENT:
        return
    stages = seat["stage_counts"]
    if (
        type(stages) is not dict
        or any(type(key) is not str for key in stages)
        or any(type(value) is not int or value <= 0 for value in stages.values())
        or sum(stages.values()) != count
    ):
        raise EngineAabbPaijiaTieError(f"{context}: stage counts are invalid")
    switches = seat["switches"]
    if (
        type(switches) is not list
        or len(switches) != stages.get(STAGE_SWITCH, 0)
        or any(
            type(entry) is not dict
            or set(entry) != {"analysis", "kind"}
            or entry["kind"] not in _KINDS
            for entry in switches
        )
    ):
        raise EngineAabbPaijiaTieError(f"{context}: switches are invalid")
    for entry in switches:
        # 内容の整合はここで、改変はsummaryが拘束するfile digestで検出する
        champion, selected = require_switch_analysis(entry["analysis"], context)
        if discard_change_kind(champion, selected, context) != entry["kind"]:
            raise EngineAabbPaijiaTieError(f"{context}: switch kind differs from tiles")


def _activation(analysis: dict) -> dict[str, object]:
    by_stage: dict[str, int] = {}
    kinds = dict.fromkeys(_KINDS, 0)
    per_seed: dict[int, int] = {}
    seconds_by_role = {ROLE_CANDIDATE: [], ROLE_INCUMBENT: []}
    for observed in analysis["hanchans"]:
        per_seed.setdefault(observed["seed"], 0)
        for seat in observed["seats"]:
            seconds_by_role[seat["role"]].extend(seat["decision_seconds"])
            if seat["role"] != ROLE_CANDIDATE:
                continue
            for stage, count in seat["stage_counts"].items():
                by_stage[stage] = by_stage.get(stage, 0) + count
            for entry in seat["switches"]:
                kinds[entry["kind"]] += 1
                if entry["kind"] in CHANGE_OUTCOMES:
                    per_seed[observed["seed"]] += 1
    changes = sum(kinds[name] for name in CHANGE_OUTCOMES)
    return {
        "activation": {
            "candidate_decisions": len(seconds_by_role[ROLE_CANDIDATE]),
            "candidate_decisions_by_stage": dict(sorted(by_stage.items())),
            "candidate_decisions_without_analysis": by_stage.get(NO_ANALYSIS, 0),
            "changes": changes,
            "changes_by_kind": kinds,
            "per_seed": [
                {"changes": count, "seed": seed} for seed, count in per_seed.items()
            ],
            "seeds_with_change": sum(1 for count in per_seed.values() if count > 0),
        },
        "decision_seconds": {
            role: distribution(values) for role, values in seconds_by_role.items()
        },
    }


# ---------------------------------------------------------------------------
# summary・進行条件（保存物だけから再導出できる値）
# ---------------------------------------------------------------------------


def _mean(values) -> float | None:
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    return sum(values) / len(values)


def progression(plan: dict, statistic: dict, secondary: dict) -> dict[str, object]:
    """実行前に固定した進行条件。nullの値は条件を満たさない（fail closed）。"""
    conditions = plan["criteria"]["conditions"]
    base = _mean(
        row["difference"]["base_points"] / _DIVISOR
        for row in secondary["point_decomposition"]
    )
    deal_in = secondary["kyoku_metrics"]["paired"]["deal_in_rate"]["mean_difference"]
    checks = {
        "base_points_mean_difference_pt": {
            "pass": base is not None
            and base >= conditions["base_points_mean_difference_pt_at_least"],
            "value": base,
        },
        "deal_in_rate_mean_difference": {
            "pass": deal_in is not None
            and deal_in <= conditions["deal_in_rate_mean_difference_at_most"],
            "value": deal_in,
        },
        "integrity": {
            "pass": True,
            "value": "complete, layer 1 and layer 2 VALID, analysis consistent, "
            "within limits at write time; confirm with verify and the hash match",
        },
        "mean_d_pt": {
            "pass": statistic["mean_d"] > conditions["mean_d_pt_above"],
            "value": statistic["mean_d"],
        },
    }
    passed = all(check["pass"] for check in checks.values())
    labels = plan["criteria"]["labels"]
    return {
        "checks": checks,
        "decision": labels["all_conditions"] if passed else labels["otherwise"],
    }


def build_summary(plan: dict, records, analysis: dict) -> dict[str, object]:
    verify_schedule(records.hanchans, _expected(plan))
    require_bindings(records, _bindings(plan))
    if not records.event_fact_status.valid:
        raise EngineAabbPaijiaTieError("the screen requires layer 2 to be VALID")
    _require_analysis(plan, analysis, records.hanchans)
    blocks = block_totals(records.hanchans)
    full = summarize_blocks([block["difference_internal"] for block in blocks])
    statistic = {key: full[key] for key in ("k", "lower", "mean_d", "sd", "upper", "z")}
    secondary = summarize_secondary_metrics(records)
    observed = _activation(analysis)
    payload = {
        "inputs": {"policy_analysis_sha256": document_sha256(analysis)},
        "activation": observed["activation"],
        "aggregation_revision": plan["execution_target"]["revision"],
        "blocks": [
            {**block, "d": block["difference_internal"] / _DIVISOR} for block in blocks
        ],
        "decision_seconds": observed["decision_seconds"],
        "evaluation_kind": EVALUATION_KIND,
        "event_fact_status": records.event_fact_status.to_value(),
        "hanchan_count": len(records.hanchans),
        "interval_note": plan["criteria"]["interval"],
        "kyoku_count": sum(len(row["kyokus"]) for row in records.hanchans),
        "not_a_formal_result": True,
        "plan_digest": plan["plan_digest"],
        "progression": progression(plan, statistic, secondary)
        if plan["run_kind"] == ALLOCATED
        else None,
        "raw_manifest_sha256": records.manifest_sha256,
        "run_kind": plan["run_kind"],
        "schema": SUMMARY_SCHEMA,
        "secondary_metrics": secondary,
        "stage": SCREEN,
        "statistic": statistic,
        "status": COMPLETE_STATUS
        if plan["run_kind"] == ALLOCATED
        else REHEARSAL_STATUS,
    }
    return {**payload, "summary_digest": document_identity(payload)}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _publish(plan: dict, clock: Callable[[], float]):
    def publish(staging: Path, executed: list[ExecutedHanchan], timings: dict):
        captures = [item.capture for item in executed]
        # 書込前に第1層・第2層・scheduleを確かめる（失敗時は何も書かない）
        hanchans, _, _ = build_records(captures, event_facts_required=True)
        verify_schedule(hanchans, _expected(plan))
        analysis = {
            "hanchans": [
                {
                    "placement": item.capture.placement,
                    "seats": item.policy_analysis["seats"],
                    "seed": item.capture.seed,
                }
                for item in executed
            ],
            "plan_digest": plan["plan_digest"],
            "schema": ANALYSIS_SCHEMA,
        }
        _require_analysis(plan, analysis, hanchans)
        write_started = clock()
        records = write_kyoku_records(
            staging / RAW_DIRNAME,
            captures,
            producer_revision=plan["execution_target"]["revision"],
            event_facts_required=True,
            bindings=_bindings(plan),
        )
        write_new_artifact_file(
            staging / ANALYSIS_FILENAME, canonical_json_text(analysis)
        )
        timings["write_seconds"] = clock() - write_started
        summary_started = clock()
        summary = build_summary(plan, records, analysis)
        write_new_artifact_file(
            staging / SUMMARY_FILENAME, canonical_json_text(summary)
        )
        timings["summary_seconds"] = clock() - summary_started
        return summary

    return publish


def run_screen(
    plan_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play: Callable[[HanchanTask], PlayedHanchan] = play_screen_hanchan,
    live_gate: Callable[..., None] = require_live_target,
    on_hanchan: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] | None = None,
    realtime: Callable[[], float] | None = None,
) -> dict[str, object]:
    """段階2（またはそのrehearsal）を実行して公開する。``play`` 等はtestのseam。"""
    plan = load_plan(plan_path, stage=SCREEN)
    default_clock, default_realtime = default_clocks()
    clock = clock or default_clock
    realtime = realtime or default_realtime
    return execute_plan(
        plan,
        ledger_path=ledger_path,
        play=play,
        live_gate=live_gate,
        publish=_publish(plan, clock),
        on_hanchan=on_hanchan,
        clock=clock,
        realtime=realtime,
    )


# ---------------------------------------------------------------------------
# verify / report
# ---------------------------------------------------------------------------


def _verified_outputs(plan_path: str | Path):
    plan = load_plan(plan_path, stage=SCREEN)
    output = Path(plan["artifact_destinations"]["output"])
    recorded = read_document(output / SUMMARY_FILENAME, "screen summary")
    records = read_kyoku_records(output / RAW_DIRNAME, event_facts_required=True)
    analysis = read_document(output / ANALYSIS_FILENAME, "policy analysis")
    summary = build_summary(plan, records, analysis)
    if recorded != summary:
        raise EngineAabbPaijiaTieError("screen summary differs from the re-derivation")
    timings = require_timings(
        plan,
        read_document(output / TIMINGS_FILENAME, "timings"),
        [len(row["kyokus"]) for row in records.hanchans],
    )
    files = output_files(output, exclude=(REPORT_FILENAME,))
    return plan, output, summary, timings, files


def build_report(plan: dict, summary: dict, timings: dict, files: list) -> dict:
    """時間・RSS・容量・時計差（再導出可能）。強さの値は持たない。"""
    rows = timings["hanchans"]
    walls = [row["wall_seconds"] for row in rows]
    blocks = [sum(walls[start : start + 6]) for start in range(0, len(walls), 6)]
    payload = {
        "clocks": clock_document(timings),
        "files": files,
        "memory": memory_summary(rows),
        "not_a_formal_result": True,
        "plan_digest": plan["plan_digest"],
        "schema": REPORT_SCHEMA,
        "storage": {
            "analysis_bytes": sum(
                item["bytes"] for item in files if item["path"] == ANALYSIS_FILENAME
            ),
            "raw_bytes": sum(
                item["bytes"] for item in files if item["path"].startswith("raw/")
            ),
            "total_bytes": sum(item["bytes"] for item in files),
        },
        "summary_digest": summary["summary_digest"],
        "timing": {
            "block_wall_seconds": distribution(blocks),
            "hanchan_wall_seconds": distribution(walls),
            "kyoku_per_hanchan": distribution([row["kyoku_count"] for row in rows]),
            "schedule_wall_seconds": timings["schedule_wall_seconds"],
            "summary_seconds": timings["summary_seconds"],
            "timeout_seconds": plan["execution"]["timeout_seconds"],
            "write_seconds": timings["write_seconds"],
        },
    }
    return {**payload, "report_digest": document_identity(payload)}


def verify_screen(plan_path: str | Path) -> dict[str, object]:
    """保存物からsummary（とreportがあればreport）を再導出し、完全一致を要求する。

    engineの再実行・Policyの生成・live ledgerの照合はしない。
    """
    plan, output, summary, timings, files = _verified_outputs(plan_path)
    report_digest = None
    report_path = output / REPORT_FILENAME
    if report_path.exists():
        report = read_document(report_path, "screen report")
        if report != build_report(plan, summary, timings, files):
            raise EngineAabbPaijiaTieError(
                "screen report differs from the re-derivation"
            )
        report_digest = report["report_digest"]
    progression_document = summary["progression"]
    return {
        "clocks": clock_document(timings),
        "decision": None
        if progression_document is None
        else progression_document["decision"],
        "files": files,
        "plan_digest": plan["plan_digest"],
        "report_digest": report_digest,
        "status": summary["status"],
        "summary_digest": summary["summary_digest"],
    }


def write_report(plan_path: str | Path) -> dict[str, object]:
    plan, output, summary, timings, files = _verified_outputs(plan_path)
    if (output / REPORT_FILENAME).exists():
        raise FileExistsError(f"refusing to overwrite: {output / REPORT_FILENAME}")
    report = build_report(plan, summary, timings, files)
    write_new_artifact_file(output / REPORT_FILENAME, canonical_json_text(report))
    return report


__all__ = [
    "COMPLETE_STATUS",
    "NOT_PROMISING",
    "PROMISING",
    "REHEARSAL_STATUS",
    "RunStopped",
    "build_report",
    "build_summary",
    "progression",
    "run_screen",
    "verify_screen",
    "write_report",
]
