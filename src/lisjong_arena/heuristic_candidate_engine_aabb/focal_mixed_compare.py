"""#522 混成focal比較の実行・summary・verify・report（DEVELOPMENT）。

```text
run     live gate -> 8K半荘（seed順 -> rotation 0..3 -> 条件A, B） -> live gate
        -> 第1層・第2層・scheduleを書込前に検査
        -> <output>/{raw/, policy-analysis.json, summary.json, timings.json}
verify  保存したplan・raw・policy-analysisだけからsummaryを再導出（別process、offline）
report  verifyに通った保存物から時間・RSS・容量・時計差・所要見込みを作る
```

主指標（focal席だけ。focalはseatのrole ``focal`` で抽出し、identityでは選ばない）:

```text
F_X(s, r) = 条件Xの半荘でrole focalのseat（= seat r）のfinal_points（内部単位）
D_s       = sum_r (F_A(s, r) - F_B(s, r)) / 40        [pt / focal seat-result]
mean D、SD(K-1)、記述用区間 mean ± 1.96 SD / sqrt(K)
LOO_s     = s以外のK-1 seedのD平均。全sのうち最小値
```

進行条件（実行前に固定）: D平均 > 0、focal素点成分のA−B平均 >= 0、focal放銃率の
seed等重み差 <= +0.01、LOO最小値 > 0、整合性 -> ``RECONFIRMED-DEVELOPMENT`` /
``NOT-RECONFIRMED-DEVELOPMENT``。境界の0は整数（内部単位）の和で判定する。
失敗・timeout・上限超過・analysis不整合はSTOP-INCOMPLETEで、部分結果を出さない。
"""

import math
from collections.abc import Callable, Sequence
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .executor import ExecutedHanchan, RunStopped
from .focal_mixed_plan import (
    ALLOCATED,
    COMPARE_HANCHANS,
    CONDITION_A,
    CONDITION_B,
    CONDITIONS,
    DIVISOR,
    EVALUATION_KIND,
    HANCHANS_PER_SEED,
    INTERVAL_Z,
    LAYOUT,
    OPPONENT_ROLES,
    ROLE_FOCAL,
    ROTATIONS,
    SUMMARY_FILENAME,
    TIMINGS_FILENAME,
    FocalHanchanCapture,
    FocalMixedError,
    default_clocks,
    execute_plan,
    load_plan,
    plan_schedule,
    require_live_target,
    require_timings,
    verify_schedule,
)
from .focal_mixed_worker import play_focal_hanchan
from .paijia_tie_plan import (
    clock_document,
    discard_change_kind,
    distribution,
    document_sha256,
    memory_summary,
    output_files,
    read_document,
    require_switch_analysis,
    seconds,
)
from .paijia_tie_worker import CHANGE_OUTCOMES, NO_ANALYSIS, STAGE_SWITCH
from .protocol import EngineAabbProtocolError
from .record_store import (
    RecordBindings,
    build_records,
    read_kyoku_records,
    require_bindings,
    write_kyoku_records,
)
from .secondary_metrics import summarize_seat_groups

ANALYSIS_SCHEMA = "arena-heuristic-candidate-engine-focal-mixed-analysis-v1"
SUMMARY_SCHEMA = "arena-heuristic-candidate-engine-focal-mixed-summary-v1"
REPORT_SCHEMA = "arena-heuristic-candidate-engine-focal-mixed-report-v1"
SECONDARY_SCHEMA = "arena-heuristic-candidate-engine-focal-mixed-secondary-metrics-v1"
RAW_DIRNAME = "raw"
ANALYSIS_FILENAME = "policy-analysis.json"
REPORT_FILENAME = "report.json"
COMPLETE_STATUS = "COMPARISON COMPLETE; DEVELOPMENT, NOT A FORMAL RESULT"
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO PROGRESSION DECISION"

_KINDS = (*CHANGE_OUTCOMES, "tsumogiri-only")
_TIMING_ROLES = ("focal-A", "focal-B", *OPPONENT_ROLES)
"""判断時間の集計単位。O1はBと同じidentityだが、focal Bへ含めない。"""


def _bindings(plan: dict) -> RecordBindings:
    return RecordBindings(
        lock_digest=plan["plan_digest"], environment_digest=plan["environment_digest"]
    )


def _wrap(error: Exception) -> FocalMixedError:
    return FocalMixedError(str(error))


# ---------------------------------------------------------------------------
# focal endpoint（第1層rowだけから）
# ---------------------------------------------------------------------------


def focal_seat(row: dict) -> int:
    """role ``focal`` のseat（identityでは選ばない。Bと同じidentityのO1席がある）。"""
    seats = [
        index for index, item in enumerate(row["seats"]) if item["role"] == ROLE_FOCAL
    ]
    if len(seats) != 1 or seats[0] != row["rotation"]:
        raise FocalMixedError("each hanchan must have one focal seat at seat r")
    return seats[0]


def focal_blocks(hanchans: Sequence[dict]) -> list[dict]:
    """seed blockごとのfocal final_points（条件A / B × rotation 0..3）とD。

    rowはscheduleの順（seed -> rotation -> A, B）であることをcallerが確かめている。
    """
    if not hanchans or len(hanchans) % HANCHANS_PER_SEED:
        raise FocalMixedError("hanchan count must be 8K")
    blocks = []
    for start in range(0, len(hanchans), HANCHANS_PER_SEED):
        rows = hanchans[start : start + HANCHANS_PER_SEED]
        seed = rows[0]["seed"]
        keys = [(row["seed"], row["rotation"], row["condition"]) for row in rows]
        if keys != [
            (seed, rotation, condition)
            for rotation in range(ROTATIONS)
            for condition in CONDITIONS
        ]:
            raise FocalMixedError("each seed block must hold r 0..3 x A, B in order")
        points = {CONDITION_A: [], CONDITION_B: []}
        for row in rows:
            points[row["condition"]].append(
                row["final_score"][focal_seat(row)]["final_points"]
            )
        difference = sum(points[CONDITION_A]) - sum(points[CONDITION_B])
        blocks.append(
            {
                "d": difference / DIVISOR,
                "difference_internal": difference,
                "f_a_internal": points[CONDITION_A],
                "f_b_internal": points[CONDITION_B],
                "seed": seed,
            }
        )
    seeds = [block["seed"] for block in blocks]
    if len(set(seeds)) != len(seeds):
        raise FocalMixedError("a seed block appears more than once")
    return blocks


def summarize_d(differences_internal: Sequence[int]) -> dict[str, object]:
    """seed blockのD（内部単位の整数差から）の平均・SD(K-1)・記述用区間。K=1は平均だけ。"""
    values = list(differences_internal)
    if not values or any(type(value) is not int for value in values):
        raise FocalMixedError("block differences must be a non-empty list of ints")
    k = len(values)
    d = [value / DIVISOR for value in values]
    mean = sum(d) / k
    if k < 2:
        return {
            "interval_crosses_zero": None,
            "k": k,
            "lower": None,
            "mean_d": mean,
            "sd": None,
            "upper": None,
            "z": INTERVAL_Z,
        }
    sd = math.sqrt(sum((value - mean) ** 2 for value in d) / (k - 1))
    half = INTERVAL_Z * sd / math.sqrt(k)
    lower, upper = mean - half, mean + half
    return {
        "interval_crosses_zero": lower <= 0 <= upper,
        "k": k,
        "lower": lower,
        "mean_d": mean,
        "sd": sd,
        "upper": upper,
        "z": INTERVAL_Z,
    }


def leave_one_seed_out(blocks: Sequence[dict]) -> dict[str, object] | None:
    """各seedを除いた残りK-1 seedのD平均。K < 2では定義しない（``None``）。

    ``minimum_numerator_internal`` は最小値に対応する「除いた後の差の和」（整数）で、
    境界0の判定に使う（浮動小数の丸めで0を正にしない）。
    """
    k = len(blocks)
    if k < 2:
        return None
    total = sum(block["difference_internal"] for block in blocks)
    entries = [
        {
            "mean_d_without": (total - block["difference_internal"])
            / (k - 1)
            / DIVISOR,
            "numerator_internal": total - block["difference_internal"],
            "seed": block["seed"],
        }
        for block in blocks
    ]
    minimum = min(
        entries, key=lambda entry: (entry["numerator_internal"], entry["seed"])
    )
    largest = max(
        blocks, key=lambda block: (block["difference_internal"], -block["seed"])
    )
    without_largest = None
    if largest["difference_internal"] > 0:
        without_largest = next(
            entry for entry in entries if entry["seed"] == largest["seed"]
        )
    return {
        "minimum_mean_d": minimum["mean_d_without"],
        "minimum_numerator_internal": minimum["numerator_internal"],
        "minimum_seed": minimum["seed"],
        "per_seed": [
            {"mean_d_without": entry["mean_d_without"], "seed": entry["seed"]}
            for entry in entries
        ],
        "without_largest_positive_block": None
        if without_largest is None
        else {
            "mean_d_without": without_largest["mean_d_without"],
            "seed": without_largest["seed"],
        },
    }


def focal_secondary_metrics(records) -> dict[str, object]:
    """focal席だけを条件（A / B）で数えた副指標（相手席は数えない）。"""
    hanchans = records.hanchans
    groups = []
    for start in range(0, len(hanchans), HANCHANS_PER_SEED):
        seed = hanchans[start]["seed"]
        groups.append(
            (
                seed,
                [
                    (index, focal_seat(hanchans[index]), hanchans[index]["condition"])
                    for index in range(start, start + HANCHANS_PER_SEED)
                ],
            )
        )
    return summarize_seat_groups(
        records, groups, layout=LAYOUT, schema=SECONDARY_SCHEMA
    )


# ---------------------------------------------------------------------------
# Policy analysis（客観recordとは別の保存物）
# ---------------------------------------------------------------------------


def _require_analysis(plan: dict, analysis: object, hanchans) -> None:
    """policy-analysisの形と、判断数・roleを第1層recordと照合する。"""
    items = plan_schedule(plan)
    if (
        type(analysis) is not dict
        or set(analysis) != {"hanchans", "plan_digest", "schema"}
        or analysis["schema"] != ANALYSIS_SCHEMA
        or analysis["plan_digest"] != plan["plan_digest"]
        or type(analysis["hanchans"]) is not list
        or len(analysis["hanchans"]) != len(items)
    ):
        raise FocalMixedError("policy analysis does not belong to the plan")
    for item, observed, record in zip(
        items, analysis["hanchans"], hanchans, strict=True
    ):
        key = (item["seed"], item["rotation"], item["condition"])
        context = "seed {} rotation {} condition {}".format(*key)
        if (
            type(observed) is not dict
            or set(observed) != {"condition", "rotation", "seats", "seed"}
            or (observed["seed"], observed["rotation"], observed["condition"]) != key
            or type(observed["seats"]) is not list
            or [
                seat.get("seat") if type(seat) is dict else None
                for seat in observed["seats"]
            ]
            != [0, 1, 2, 3]
        ):
            raise FocalMixedError(f"{context}: analysis seats do not match")
        for seat, role, count in zip(
            observed["seats"], item["roles"], record["policy_call_counts"], strict=True
        ):
            analyzed = role == ROLE_FOCAL and item["condition"] == CONDITION_A
            _require_seat(seat, role, analyzed, count, f"{context} seat {seat['seat']}")


def _require_seat(seat: dict, role: str, analyzed: bool, count: int, context: str):
    fields = {"decision_seconds", "role", "seat"}
    if analyzed:
        fields |= {"stage_counts", "switches"}
    if set(seat) != fields or seat["role"] != role:
        raise FocalMixedError(f"{context}: role or fields do not match")
    values = seat["decision_seconds"]
    if type(values) is not list or len(values) != count:
        raise FocalMixedError(f"{context}: decisions differ from the Policy call count")
    try:
        for value in values:
            seconds(value, f"{context} decision_seconds")
        if not analyzed:
            return
        stages = seat["stage_counts"]
        if (
            type(stages) is not dict
            or any(type(key) is not str for key in stages)
            or any(type(value) is not int or value <= 0 for value in stages.values())
            or sum(stages.values()) != count
        ):
            raise FocalMixedError(f"{context}: stage counts are invalid")
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
            raise FocalMixedError(f"{context}: switches are invalid")
        for entry in switches:
            champion, selected = require_switch_analysis(entry["analysis"], context)
            if discard_change_kind(champion, selected, context) != entry["kind"]:
                raise FocalMixedError(f"{context}: switch kind differs from tiles")
    except FocalMixedError:
        raise
    except EngineAabbProtocolError as error:
        raise _wrap(error) from error


def _observation(analysis: dict) -> dict[str, object]:
    by_stage: dict[str, int] = {}
    kinds = dict.fromkeys(_KINDS, 0)
    per_seed: dict[int, int] = {}
    timing = {name: [] for name in _TIMING_ROLES}
    for observed in analysis["hanchans"]:
        per_seed.setdefault(observed["seed"], 0)
        for seat in observed["seats"]:
            role = seat["role"]
            name = f"focal-{observed['condition']}" if role == ROLE_FOCAL else role
            timing[name].extend(seat["decision_seconds"])
            if "stage_counts" not in seat:
                continue
            for stage, count in seat["stage_counts"].items():
                by_stage[stage] = by_stage.get(stage, 0) + count
            for entry in seat["switches"]:
                kinds[entry["kind"]] += 1
                if entry["kind"] in CHANGE_OUTCOMES:
                    per_seed[observed["seed"]] += 1
    return {
        "activation": {
            "candidate_decisions": len(timing["focal-A"]),
            "candidate_decisions_by_stage": dict(sorted(by_stage.items())),
            "candidate_decisions_without_analysis": by_stage.get(NO_ANALYSIS, 0),
            "changes": sum(kinds[name] for name in CHANGE_OUTCOMES),
            "changes_by_kind": kinds,
            "per_seed": [
                {"changes": count, "seed": seed} for seed, count in per_seed.items()
            ],
            "seeds_with_change": sum(1 for count in per_seed.values() if count > 0),
        },
        "decision_seconds": {
            name: distribution(timing[name]) for name in _TIMING_ROLES
        },
    }


# ---------------------------------------------------------------------------
# summary・進行条件（保存物だけから再導出できる値）
# ---------------------------------------------------------------------------


def progression(plan: dict, blocks, statistic, loo, secondary) -> dict[str, object]:
    """実行前に固定した進行条件。nullは条件を満たさない（fail closed）。"""
    conditions = plan["criteria"]["conditions"]
    base_rows = [
        row["difference"]["base_points"] for row in secondary["point_decomposition"]
    ]
    base_mean = sum(base_rows) / len(base_rows) / DIVISOR if base_rows else None
    deal_in = secondary["kyoku_metrics"]["paired"]["deal_in_rate"]["mean_difference"]
    total = sum(block["difference_internal"] for block in blocks)
    checks = {
        "base_points_mean_difference_pt": {
            "pass": bool(base_rows)
            and sum(base_rows) >= conditions["base_points_mean_difference_pt_at_least"],
            "value": base_mean,
        },
        "deal_in_rate_mean_difference": {
            "pass": deal_in is not None
            and deal_in <= conditions["deal_in_rate_mean_difference_at_most"],
            "value": deal_in,
        },
        "integrity": {
            "pass": True,
            "value": "complete, layer 1 and layer 2 VALID, focal A analysis "
            "consistent, within limits at write time; confirm with verify and the "
            "hash match",
        },
        "leave_one_seed_out_minimum_mean_d_pt": {
            "pass": loo is not None
            and loo["minimum_numerator_internal"]
            > conditions["leave_one_seed_out_minimum_mean_d_pt_above"],
            "value": None if loo is None else loo["minimum_mean_d"],
        },
        "mean_d_pt": {
            "pass": total > conditions["mean_d_pt_above"],
            "value": statistic["mean_d"],
        },
    }
    passed = all(check["pass"] for check in checks.values())
    labels = plan["criteria"]["labels"]
    return {
        "checks": checks,
        "decision": labels["all_conditions"] if passed else labels["otherwise"],
        "interval_crosses_zero": statistic["interval_crosses_zero"],
    }


def build_summary(plan: dict, records, analysis: dict) -> dict[str, object]:
    verify_schedule(plan, records.hanchans)
    require_bindings(records, _bindings(plan))
    if not records.event_fact_status.valid:
        raise FocalMixedError("the comparison requires layer 2 to be VALID")
    _require_analysis(plan, analysis, records.hanchans)
    blocks = focal_blocks(records.hanchans)
    statistic = summarize_d([block["difference_internal"] for block in blocks])
    loo = leave_one_seed_out(blocks)
    secondary = focal_secondary_metrics(records)
    observed = _observation(analysis)
    allocated = plan["run_kind"] == ALLOCATED
    payload = {
        "activation": observed["activation"],
        "aggregation_revision": plan["execution_target"]["revision"],
        "blocks": blocks,
        "decision_seconds": observed["decision_seconds"],
        "evaluation_kind": EVALUATION_KIND,
        "event_fact_status": records.event_fact_status.to_value(),
        "hanchan_count": len(records.hanchans),
        "inputs": {"policy_analysis_sha256": document_sha256(analysis)},
        "interval_note": plan["criteria"]["interval"],
        "kyoku_count": sum(len(row["kyokus"]) for row in records.hanchans),
        "leave_one_seed_out": loo,
        "not_a_formal_result": True,
        "plan_digest": plan["plan_digest"],
        "progression": progression(plan, blocks, statistic, loo, secondary)
        if allocated
        else None,
        "raw_manifest_sha256": records.manifest_sha256,
        "run_kind": plan["run_kind"],
        "schema": SUMMARY_SCHEMA,
        "secondary_metrics": secondary,
        "statistic": statistic,
        "status": COMPLETE_STATUS if allocated else REHEARSAL_STATUS,
    }
    return {**payload, "summary_digest": document_identity(payload)}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _publish(plan: dict, clock: Callable[[], float]):
    def publish(staging: Path, executed: list[ExecutedHanchan], timings: dict):
        captures = [item.capture for item in executed]
        # 書込前に第1層・第2層・scheduleを確かめる（失敗時は何も書かない）
        hanchans, _, _ = build_records(
            captures,
            event_facts_required=True,
            layout=LAYOUT,
            capture_type=FocalHanchanCapture,
        )
        verify_schedule(plan, hanchans)
        analysis = {
            "hanchans": [
                {
                    "condition": item.capture.condition,
                    "rotation": item.capture.rotation,
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
            layout=LAYOUT,
            capture_type=FocalHanchanCapture,
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


def run_comparison(
    plan_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play: Callable = play_focal_hanchan,
    live_gate: Callable[..., None] = require_live_target,
    on_hanchan: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] | None = None,
    realtime: Callable[[], float] | None = None,
) -> dict[str, object]:
    """比較（またはそのrehearsal）を実行して公開する。``play`` 等はtestのseam。"""
    plan = load_plan(plan_path)
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
    plan = load_plan(plan_path)
    output = Path(plan["artifact_destinations"]["output"])
    recorded = read_document(output / SUMMARY_FILENAME, "summary")
    records = read_kyoku_records(
        output / RAW_DIRNAME, event_facts_required=True, layout=LAYOUT
    )
    analysis = read_document(output / ANALYSIS_FILENAME, "policy analysis")
    summary = build_summary(plan, records, analysis)
    if recorded != summary:
        raise FocalMixedError("summary differs from the re-derivation")
    timings = require_timings(
        plan,
        read_document(output / TIMINGS_FILENAME, "timings"),
        [len(row["kyokus"]) for row in records.hanchans],
    )
    files = output_files(output, exclude=(REPORT_FILENAME,))
    return plan, output, summary, timings, files


def build_report(plan: dict, summary: dict, timings: dict, files: list) -> dict:
    """時間・RSS・容量・時計差（再導出可能）。強さの値は持たない。

    rehearsalでは、観測した平均から本比較（256半荘）の所要を単純換算した見込みを持つ。
    STOP-BUDGETの判断は実行計画（docs）の固定規則で行い、ここでは判定しない。
    """
    rows = timings["hanchans"]
    walls = [row["wall_seconds"] for row in rows]
    blocks = [
        sum(walls[start : start + HANCHANS_PER_SEED])
        for start in range(0, len(walls), HANCHANS_PER_SEED)
    ]
    projection = None
    if plan["run_kind"] != ALLOCATED:
        scale = COMPARE_HANCHANS / len(rows)
        saving = timings["write_seconds"] + timings["summary_seconds"]
        projection = {
            "compare_hanchans": COMPARE_HANCHANS,
            "mean_hanchan_wall_seconds": sum(walls) / len(walls),
            "projected_schedule_seconds": sum(walls) * scale,
            "projected_write_and_summary_seconds": saving * scale,
            "rule": (
                "simple linear scaling of this rehearsal; verify, collection and the "
                "hash match are measured separately"
            ),
        }
    payload = {
        "clocks": clock_document(timings),
        "files": files,
        "memory": memory_summary(rows),
        "not_a_formal_result": True,
        "plan_digest": plan["plan_digest"],
        "projection": projection,
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
            "hanchan_wall_seconds": distribution(walls),
            "kyoku_per_hanchan": distribution([row["kyoku_count"] for row in rows]),
            "schedule_wall_seconds": timings["schedule_wall_seconds"],
            "seed_block_wall_seconds": distribution(blocks),
            "summary_seconds": timings["summary_seconds"],
            "timeout_seconds": plan["execution"]["timeout_seconds"],
            "write_seconds": timings["write_seconds"],
        },
    }
    return {**payload, "report_digest": document_identity(payload)}


def verify_comparison(plan_path: str | Path) -> dict[str, object]:
    """保存物からsummary（とreportがあればreport）を再導出し、完全一致を要求する。

    engineの再実行・Policyの生成・live ledgerの照合はしない。
    """
    plan, output, summary, timings, files = _verified_outputs(plan_path)
    report_digest = None
    report_path = output / REPORT_FILENAME
    if report_path.exists():
        report = read_document(report_path, "report")
        if report != build_report(plan, summary, timings, files):
            raise FocalMixedError("report differs from the re-derivation")
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
    "REHEARSAL_STATUS",
    "RunStopped",
    "build_report",
    "build_summary",
    "focal_blocks",
    "focal_seat",
    "focal_secondary_metrics",
    "leave_one_seed_out",
    "progression",
    "run_comparison",
    "summarize_d",
    "verify_comparison",
    "write_report",
]
