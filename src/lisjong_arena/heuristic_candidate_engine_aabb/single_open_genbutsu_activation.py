"""#524 単独副露者genbutsu候補のshadow変更頻度・時間gate（B自己対局、Aのshadow採点）。

```text
run     live gate -> 8半荘（seed順、4席B、配置の入替なし） -> live gate
        -> <output>/{shadow.json, hanchans.json, summary.json, timings.json}
verify  保存したplan・shadow・hanchans・timingsだけからsummaryを再導出（別process、offline）
```

- ``shadow.json`` はPolicy側の観測（判断ごとのoutcome・stage・A（trace / 通常）とBの
  判断時間、差がある判断と比較値を計算した判断だけlisjongのanalysis）。``hanchans.json`` は
  客観値（局数、終了理由、final_points、``CompletedMatch`` のdigest、呼び出し数）。混ぜない
- 勝敗・点数は判定に使わない。判定は実行前に固定した順に1つだけ出す:
  ``STOP-INVALID`` > ``STOP-LOW-ACTIVATION`` > ``PROCEED``
- 変更数の分母は全32 seat-hanchans。ツモ切り / 手出しだけの違いは変更に数えない
- 192半荘screenの時間見積りは記述値で、予算判定（STOP-BUDGET）は後続の実行Issueで行う
- rehearsal（既知の開発用seed）は同じ経路の動作確認で、判定を出さない
"""

from collections.abc import Callable
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .executor import ExecutedHanchan, HanchanTask, PlayedHanchan
from .paijia_tie_activation import HANCHANS_FILENAME, SHADOW_FILENAME, _hanchan_document
from .paijia_tie_plan import (
    ALLOCATED,
    SUMMARY_FILENAME,
    TIMINGS_FILENAME,
    clock_document,
    default_clocks,
    discard_change_kind,
    distribution,
    document_sha256,
    execute_plan,
    memory_summary,
    output_files,
    plan_schedule,
    read_document,
    require_timings,
    seconds,
)
from .paijia_tie_worker import CANDIDATE_ILLEGAL, NO_ANALYSIS, SAME
from .paijia_tie_worker import CHANGE_OUTCOMES as _CHANGES
from .single_open_genbutsu_plan import (
    EVALUATION_KIND,
    STOP_SCHEMA,
    EngineAabbSingleOpenGenbutsuError,
    load_plan,
    require_live_target,
)
from .single_open_genbutsu_worker import (
    EVALUATED_STAGES,
    GATE_STAGES,
    INVALID_OUTCOMES,
    OUTCOMES,
    SINGLE_OPEN_STAGES,
    STAGE_NO_QUALIFYING,
    STAGE_SWITCHED,
    TRACE_MISMATCH,
    play_genbutsu_activation_hanchan,
)

SHADOW_SCHEMA = "arena-heuristic-candidate-engine-aabb-single-open-genbutsu-shadow-v1"
HANCHANS_SCHEMA = (
    "arena-heuristic-candidate-engine-aabb-single-open-genbutsu-hanchans-v1"
)
SUMMARY_SCHEMA = (
    "arena-heuristic-candidate-engine-aabb-single-open-genbutsu-activation-v1"
)

STOP_INVALID = "STOP-INVALID"
STOP_LOW_ACTIVATION = "STOP-LOW-ACTIVATION"
PROCEED = "PROCEED"
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO GATE DECISION"
COMPLETE_STATUS = "ACTIVATION GATE COMPLETE; DEVELOPMENT, NOT A STRENGTH RESULT"

_STAGES = (*GATE_STAGES, NO_ANALYSIS)
_ROW_FIELDS = frozenset(
    {"a_plain_seconds", "a_seconds", "b_seconds", "outcome", "stage"}
)
_DETAIL_FIELDS = frozenset(
    {
        "analysis",
        "candidate_action",
        "incumbent_action",
        "plain_candidate_action",
        "violations",
    }
)
_HANCHAN_FIELDS = frozenset(
    {
        "completed_match_sha256",
        "end_reason",
        "final_points",
        "kyoku_count",
        "placement",
        "policy_call_counts",
        "seed",
        "selector_call_counts",
    }
)
_ANALYSIS_TYPE = "SingleOpenGenbutsuAnalysis"


def _fail(message: str):
    raise EngineAabbSingleOpenGenbutsuError(message)


# ---------------------------------------------------------------------------
# 保存物の形
# ---------------------------------------------------------------------------


def _require_shadow(plan: dict, shadow: object, hanchans: object) -> None:
    """shadow・hanchansの形と、判断数とPolicy呼び出し数の一致を検査する。

    scheduleとの完全一致を要求するので、半荘の不足・重複・順序の入替は拒否される。
    """
    expected = [(item["seed"], item["placement"]) for item in plan_schedule(plan)]
    for name, document, schema in (
        ("shadow", shadow, SHADOW_SCHEMA),
        ("hanchans", hanchans, HANCHANS_SCHEMA),
    ):
        if (
            type(document) is not dict
            or document.get("schema") != schema
            or document.get("plan_digest") != plan["plan_digest"]
            or set(document) != {"hanchans", "plan_digest", "schema"}
            or type(document["hanchans"]) is not list
            or any(type(row) is not dict for row in document["hanchans"])
            or [(row.get("seed"), row.get("placement")) for row in document["hanchans"]]
            != expected
        ):
            _fail(f"{name} does not cover the planned schedule")
    for objective, observed in zip(
        hanchans["hanchans"], shadow["hanchans"], strict=True
    ):
        context = f"seed {objective['seed']}"
        if set(objective) != _HANCHAN_FIELDS:
            _fail(f"{context}: hanchan fields do not match")
        counts = objective["policy_call_counts"]
        if (
            type(counts) is not list
            or len(counts) != 4
            or any(type(count) is not int or count < 0 for count in counts)
            or objective["selector_call_counts"] != counts
        ):
            _fail(f"{context}: call counts are invalid")
        kyoku = objective["kyoku_count"]
        if type(kyoku) is not int or kyoku <= 0:
            _fail(f"{context}: kyoku_count is invalid")
        seats = observed.get("seats")
        if (
            set(observed) != {"placement", "seats", "seed"}
            or type(seats) is not list
            or any(type(seat) is not dict for seat in seats)
            or [seat.get("seat") for seat in seats] != [0, 1, 2, 3]
        ):
            _fail(f"{context}: shadow seats do not match")
        for seat, count in zip(seats, counts, strict=True):
            rows = seat.get("decisions")
            if set(seat) != {"decisions", "seat"} or type(rows) is not list:
                _fail(f"{context}: shadow seat is invalid")
            if len(rows) != count:
                _fail(
                    f"{context} seat {seat['seat']}: decisions differ from the "
                    "Policy call count"
                )
            for row in rows:
                _require_row(row, context)


def _require_row(row: object, context: str) -> None:
    if type(row) is not dict or not _ROW_FIELDS <= set(row) <= _ROW_FIELDS | {"detail"}:
        _fail(f"{context}: decision fields do not match")
    if row["outcome"] not in OUTCOMES or row["stage"] not in _STAGES:
        _fail(f"{context}: decision outcome or stage is invalid")
    for name in ("a_plain_seconds", "a_seconds", "b_seconds"):
        seconds(row[name], f"{context} {name}")
    needs_detail = row["outcome"] != SAME or row["stage"] in EVALUATED_STAGES
    if needs_detail != ("detail" in row):
        _fail(f"{context}: decision detail is missing")
    if not needs_detail:
        return
    if type(row["detail"]) is not dict or set(row["detail"]) != _DETAIL_FIELDS:
        _fail(f"{context}: decision detail is invalid")
    _require_detail(row["outcome"], row["stage"], row["detail"], context)


def _require_detail(outcome: str, stage: str, detail: dict, context: str) -> None:
    """保存したdetailがoutcomeと整合することを確かめる（gate・比較値は再計算しない）。

    detailはsummaryに現れないので、形はここで、値の改変はsummaryが拘束する
    ``shadow.json`` のdigestで検出する。
    """
    incumbent = detail["incumbent_action"]
    candidate = detail["candidate_action"]
    plain = detail["plain_candidate_action"]
    analysis = detail["analysis"]
    violations = detail["violations"]
    if type(violations) is not list or any(type(v) is not str for v in violations):
        _fail(f"{context}: violations must be strings")
    if type(incumbent) is not dict:
        _fail(f"{context}: the B action is not recorded")
    if outcome in INVALID_OUTCOMES:
        if not violations:
            _fail(f"{context}: an invalid row needs reasons")
        if (outcome == CANDIDATE_ILLEGAL) != (candidate is None and plain is None):
            _fail(f"{context}: illegal-action detail mismatch")
        if (outcome == TRACE_MISMATCH) != (candidate != plain):
            _fail(f"{context}: trace-mismatch detail mismatch")
        return
    if violations:
        _fail(f"{context}: a valid row has violations")
    if type(candidate) is not dict:
        _fail(f"{context}: the A action is not recorded")
    if plain != candidate:
        _fail(f"{context}: normal and trace A actions differ in a valid row")
    if (
        type(analysis) is not dict
        or analysis.get("__type") != _ANALYSIS_TYPE
        or analysis.get("stage") != f"SingleOpenGenbutsuStage.{stage}"
        or analysis.get("parent_action") != incumbent
        or analysis.get("selected_action") != candidate
        or analysis.get("action_changed") is not (candidate != incumbent)
        or type(analysis.get("qualifying_actions")) is not list
    ):
        _fail(f"{context}: analysis differs from the recorded actions")
    qualifying = analysis["qualifying_actions"]
    if outcome == SAME:
        if candidate != incumbent or stage != STAGE_NO_QUALIFYING or qualifying:
            _fail(f"{context}: same-action detail mismatch")
        return
    if stage != STAGE_SWITCHED or candidate not in qualifying:
        _fail(f"{context}: a change outside a qualifying SWITCHED")
    if discard_change_kind(incumbent, candidate, context) != outcome:
        _fail(f"{context}: change kind differs from tiles")


# ---------------------------------------------------------------------------
# summary・gate（保存物だけから再導出できる値）
# ---------------------------------------------------------------------------


def _count(counter: dict, key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def build_summary(plan: dict, shadow: dict, hanchans: dict, timings: dict) -> dict:
    """summaryは保存物（plan・shadow・hanchans・timingsの半荘行とschedule wall）だけから
    決まる。保存・summary自体の秒数と終了時の経過は使わない。"""
    _require_shadow(plan, shadow, hanchans)
    require_timings(
        plan,
        timings,
        [row["kyoku_count"] for row in hanchans["hanchans"]],
        complete=False,
    )
    by_stage: dict[str, int] = {}
    by_outcome = dict.fromkeys(OUTCOMES, 0)
    a_seconds, a_plain_seconds, b_seconds = [], [], []
    per_seed = []
    adjusted = []
    for observed, row in zip(shadow["hanchans"], timings["hanchans"], strict=True):
        by_seat = {"alternatives": [], "changes": [], "single_open": []}
        a_total = a_plain_total = b_total = 0.0
        for seat in observed["seats"]:
            counts = dict.fromkeys(by_seat, 0)
            for decision in seat["decisions"]:
                stage = decision["stage"]
                _count(by_stage, stage)
                by_outcome[decision["outcome"]] += 1
                a_seconds.append(decision["a_seconds"])
                a_plain_seconds.append(decision["a_plain_seconds"])
                b_seconds.append(decision["b_seconds"])
                a_total += decision["a_seconds"]
                a_plain_total += decision["a_plain_seconds"]
                b_total += decision["b_seconds"]
                counts["changes"] += decision["outcome"] in _CHANGES
                counts["single_open"] += stage in SINGLE_OPEN_STAGES
                counts["alternatives"] += stage in EVALUATED_STAGES
            for name, value in counts.items():
                by_seat[name].append(value)
        per_seed.append(
            {
                "alternative_decisions_by_seat": by_seat["alternatives"],
                "changes": sum(by_seat["changes"]),
                "changes_by_seat": by_seat["changes"],
                "seed": observed["seed"],
                "single_open_decisions_by_seat": by_seat["single_open"],
            }
        )
        if a_total + a_plain_total + b_total > row["wall_seconds"]:
            _fail(f"seed {observed['seed']}: decision seconds exceed the hanchan wall")
        adjusted.append(row["wall_seconds"] - a_plain_total - (a_total + b_total) / 2)
    changes = sum(by_outcome[name] for name in _CHANGES)
    invalid = {name: by_outcome[name] for name in INVALID_OUTCOMES}
    seeds_with_change = sum(1 for row in per_seed if row["changes"] > 0)
    seat_hanchans = 4 * len(per_seed)
    criteria = plan["criteria"]
    checks = {
        "changes": changes,
        "invalid": sum(invalid.values()),
        "minimum_changes": criteria["minimum_changes"],
        "minimum_seeds_with_change": criteria["minimum_seeds_with_change"],
        "seeds_with_change": seeds_with_change,
    }
    gate = None
    if plan["run_kind"] == ALLOCATED:
        if checks["invalid"] > 0:
            decision = STOP_INVALID
        elif (
            changes < criteria["minimum_changes"]
            or seeds_with_change < criteria["minimum_seeds_with_change"]
        ):
            decision = STOP_LOW_ACTIVATION
        else:
            decision = PROCEED
        gate = {"checks": checks, "decision": decision}
    rows = timings["hanchans"]
    mean_adjusted = sum(adjusted) / len(adjusted)
    payload = {
        "inputs": {
            "hanchans_sha256": document_sha256(hanchans),
            "shadow_sha256": document_sha256(shadow),
        },
        "activation": {
            "alternative_decisions": sum(by_stage.get(s, 0) for s in EVALUATED_STAGES),
            "by_outcome": by_outcome,
            "by_stage": dict(sorted(by_stage.items())),
            "changes": changes,
            "changes_per_seat_hanchan": changes / seat_hanchans,
            "decisions": sum(by_outcome.values()),
            "invalid": invalid,
            "per_seed": per_seed,
            "red_only_changes": by_outcome["red-only-change"],
            "seat_hanchans": seat_hanchans,
            "seeds_with_change": seeds_with_change,
            "single_open_decisions": sum(
                by_stage.get(s, 0) for s in SINGLE_OPEN_STAGES
            ),
            "switched_decisions": by_stage.get(STAGE_SWITCHED, 0),
            "tile_type_changes": by_outcome["tile-type-change"],
            "tsumogiri_only_differences": by_outcome["tsumogiri-only"],
        },
        "decision_seconds": {
            "A_shadow_normal": distribution(a_plain_seconds),
            "A_shadow_trace": distribution(a_seconds),
            "B": distribution(b_seconds),
        },
        "evaluation_kind": EVALUATION_KIND,
        "gate": gate,
        "hanchan_count": len(rows),
        "kyoku_count": sum(row["kyoku_count"] for row in rows),
        "memory": memory_summary(rows),
        "not_a_strength_result": True,
        "plan_digest": plan["plan_digest"],
        "run_kind": plan["run_kind"],
        "schema": SUMMARY_SCHEMA,
        "screen_projection": {
            "descriptive_only": True,
            "mean_adjusted_hanchan_seconds": mean_adjusted,
            "projected_screen_schedule_seconds": criteria["screen_hanchans"]
            * mean_adjusted,
            "screen_hanchans": criteria["screen_hanchans"],
        },
        "stage": plan["stage"],
        "status": COMPLETE_STATUS if gate is not None else REHEARSAL_STATUS,
        "timing": {
            "hanchan_wall_seconds": distribution([row["wall_seconds"] for row in rows]),
            "kyoku_per_hanchan": distribution([row["kyoku_count"] for row in rows]),
            "schedule_wall_seconds": timings["schedule_wall_seconds"],
        },
    }
    return {**payload, "summary_digest": document_identity(payload)}


# ---------------------------------------------------------------------------
# run / verify
# ---------------------------------------------------------------------------


def _publish(plan: dict, clock: Callable[[], float]):
    def publish(staging: Path, executed: list[ExecutedHanchan], timings: dict):
        write_started = clock()
        shadow = {
            "hanchans": [
                {
                    "placement": item.capture.placement,
                    "seats": item.policy_analysis["seats"],
                    "seed": item.capture.seed,
                }
                for item in executed
            ],
            "plan_digest": plan["plan_digest"],
            "schema": SHADOW_SCHEMA,
        }
        hanchans = {
            "hanchans": [_hanchan_document(item) for item in executed],
            "plan_digest": plan["plan_digest"],
            "schema": HANCHANS_SCHEMA,
        }
        _require_shadow(plan, shadow, hanchans)
        write_new_artifact_file(staging / SHADOW_FILENAME, canonical_json_text(shadow))
        write_new_artifact_file(
            staging / HANCHANS_FILENAME, canonical_json_text(hanchans)
        )
        timings["write_seconds"] = clock() - write_started
        summary_started = clock()
        summary = build_summary(plan, shadow, hanchans, timings)
        write_new_artifact_file(
            staging / SUMMARY_FILENAME, canonical_json_text(summary)
        )
        timings["summary_seconds"] = clock() - summary_started
        return summary

    return publish


def run_activation(
    plan_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play: Callable[[HanchanTask], PlayedHanchan] = play_genbutsu_activation_hanchan,
    live_gate: Callable[..., None] = require_live_target,
    on_hanchan: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] | None = None,
    realtime: Callable[[], float] | None = None,
) -> dict[str, object]:
    """実行して公開する（失敗はSTOP-INCOMPLETEで何も公開しない）。

    ``play`` / ``live_gate`` / clockはtestのseam。
    """
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
        stop_schema=STOP_SCHEMA,
    )


def verified_activation(plan_path: str | Path) -> tuple[dict, dict, list[dict], dict]:
    """保存物からsummaryを再導出し、記録値との完全一致を要求する（engine・Policyは使わない）。"""
    plan = load_plan(plan_path)
    output = Path(plan["artifact_destinations"]["output"])
    recorded = read_document(output / SUMMARY_FILENAME, "activation summary")
    hanchans = read_document(output / HANCHANS_FILENAME, "hanchans")
    timings = read_document(output / TIMINGS_FILENAME, "timings")
    summary = build_summary(
        plan, read_document(output / SHADOW_FILENAME, "shadow"), hanchans, timings
    )
    if recorded != summary:
        _fail("activation summary differs from the re-derivation")
    require_timings(plan, timings, [row["kyoku_count"] for row in hanchans["hanchans"]])
    return plan, summary, output_files(output), clock_document(timings)


def verify_activation(plan_path: str | Path) -> dict[str, object]:
    plan, summary, files, clocks = verified_activation(plan_path)
    gate = summary["gate"]
    return {
        "clocks": clocks,
        "decision": None if gate is None else gate["decision"],
        "files": files,
        "output_bytes": sum(item["bytes"] for item in files),
        "plan_digest": plan["plan_digest"],
        "status": summary["status"],
        "summary_digest": summary["summary_digest"],
    }


__all__ = [
    "COMPLETE_STATUS",
    "PROCEED",
    "REHEARSAL_STATUS",
    "STOP_INVALID",
    "STOP_LOW_ACTIVATION",
    "build_summary",
    "run_activation",
    "verify_activation",
]
