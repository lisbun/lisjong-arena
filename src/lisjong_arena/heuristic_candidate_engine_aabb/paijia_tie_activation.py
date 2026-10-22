"""#520 段階1: 固定入力の変更頻度・時間gate（B自己対局、Aのshadow採点）。

```text
run     live gate -> 8半荘（seed順、4席B、配置の入替なし） -> live gate
        -> <output>/{shadow.json, hanchans.json, summary.json, timings.json}
verify  保存したplan・shadow・hanchans・timingsだけからsummaryを再導出（別process、offline）
```

- ``shadow.json`` はPolicy側の観測（判断ごとのoutcome・stage・A/B判断時間、差がある判断と
  gate通過判断だけlisjongのanalysis）。``hanchans.json`` は客観値（局数、終了理由、
  final_points、``CompletedMatch`` のdigest、呼び出し数）。両者を混ぜない
- 勝敗は判定に使わない。判定は実行前に固定した順に1つだけ出す:
  ``STOP-INVALID`` > ``STOP-LOW-ACTIVATION`` > ``STOP-BUDGET`` > ``PROCEED``
- 変更数の分母は全32 seat-hanchans。ツモ切り / 手出しだけの違いは変更に数えない
- rehearsal（既知の開発用seed）は同じ経路の動作確認で、判定を出さない
"""

from collections.abc import Callable
from pathlib import Path

from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text, write_new_artifact_file
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .backend_check import _value_digest, canonical_engine_value
from .executor import ExecutedHanchan, HanchanTask, PlayedHanchan, RunStopped
from .paijia_tie_plan import (
    ACTIVATION,
    ALLOCATED,
    EVALUATION_KIND,
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
    plan_schedule,
    read_document,
    require_live_target,
    require_switch_analysis,
    require_timings,
    seconds,
)
from .paijia_tie_worker import (
    ACTIVATED_STAGES,
    CHANGE_OUTCOMES,
    INVALID_OUTCOMES,
    OUTCOMES,
    SAME,
    STAGE_SWITCH,
    TSUMOGIRI_ONLY,
    play_activation_hanchan,
)

SHADOW_SCHEMA = "arena-heuristic-candidate-engine-aabb-paijia-tie-shadow-v1"
HANCHANS_SCHEMA = "arena-heuristic-candidate-engine-aabb-paijia-tie-hanchans-v1"
SUMMARY_SCHEMA = "arena-heuristic-candidate-engine-aabb-paijia-tie-activation-v1"
SHADOW_FILENAME = "shadow.json"
HANCHANS_FILENAME = "hanchans.json"

STOP_INVALID = "STOP-INVALID"
STOP_LOW_ACTIVATION = "STOP-LOW-ACTIVATION"
STOP_BUDGET = "STOP-BUDGET"
PROCEED = "PROCEED"
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO GATE DECISION"
COMPLETE_STATUS = "ACTIVATION GATE COMPLETE; DEVELOPMENT, NOT A STRENGTH RESULT"

_ROW_FIELDS = frozenset({"a_seconds", "b_seconds", "outcome", "stage"})
_DETAIL_FIELDS = frozenset(
    {"analysis", "candidate_action", "incumbent_action", "violations"}
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


# ---------------------------------------------------------------------------
# 保存物の形
# ---------------------------------------------------------------------------


def _hanchan_document(item: ExecutedHanchan) -> dict[str, object]:
    capture = item.capture
    match = capture.match
    if capture.selector_call_counts != capture.policy_call_counts:
        raise RunStopped(f"seed {capture.seed}: selector and Policy call counts differ")
    return {
        "completed_match_sha256": _value_digest(canonical_engine_value(match)),
        "end_reason": canonical_engine_value(match.end_reason),
        "final_points": [
            match.final_score.for_seat(seat).final_points for seat in EngineSeat
        ],
        "kyoku_count": len(match.history),
        "placement": capture.placement,
        "policy_call_counts": list(capture.policy_call_counts),
        "seed": capture.seed,
        "selector_call_counts": list(capture.selector_call_counts),
    }


def _require_shadow(plan: dict, shadow: object, hanchans: object) -> None:
    """shadow・hanchansの形と、判断数とPolicy呼び出し数の一致を検査する。"""
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
            or [(row.get("seed"), row.get("placement")) for row in document["hanchans"]]
            != expected
        ):
            raise EngineAabbPaijiaTieError(
                f"{name} does not cover the planned schedule"
            )
    for objective, observed in zip(
        hanchans["hanchans"], shadow["hanchans"], strict=True
    ):
        context = f"seed {objective['seed']}"
        if set(objective) != _HANCHAN_FIELDS:
            raise EngineAabbPaijiaTieError(f"{context}: hanchan fields do not match")
        counts = objective["policy_call_counts"]
        if (
            type(counts) is not list
            or len(counts) != 4
            or any(type(count) is not int or count < 0 for count in counts)
            or objective["selector_call_counts"] != counts
        ):
            raise EngineAabbPaijiaTieError(f"{context}: call counts are invalid")
        kyoku = objective["kyoku_count"]
        if type(kyoku) is not int or kyoku <= 0:
            raise EngineAabbPaijiaTieError(f"{context}: kyoku_count is invalid")
        seats = observed.get("seats")
        if (
            set(observed) != {"placement", "seats", "seed"}
            or type(seats) is not list
            or [seat.get("seat") for seat in seats] != [0, 1, 2, 3]
        ):
            raise EngineAabbPaijiaTieError(f"{context}: shadow seats do not match")
        for seat, count in zip(seats, counts, strict=True):
            rows = seat.get("decisions")
            if set(seat) != {"decisions", "seat"} or type(rows) is not list:
                raise EngineAabbPaijiaTieError(f"{context}: shadow seat is invalid")
            if len(rows) != count:
                raise EngineAabbPaijiaTieError(
                    f"{context} seat {seat['seat']}: decisions differ from the "
                    "Policy call count"
                )
            for row in rows:
                _require_row(row, context)


def _require_row(row: object, context: str) -> None:
    if type(row) is not dict or not _ROW_FIELDS <= set(row) <= _ROW_FIELDS | {"detail"}:
        raise EngineAabbPaijiaTieError(f"{context}: decision fields do not match")
    if row["outcome"] not in OUTCOMES or type(row["stage"]) is not str:
        raise EngineAabbPaijiaTieError(f"{context}: decision outcome is invalid")
    seconds(row["a_seconds"], f"{context} a_seconds")
    seconds(row["b_seconds"], f"{context} b_seconds")
    needs_detail = row["outcome"] != SAME or row["stage"] in ACTIVATED_STAGES
    if needs_detail != ("detail" in row):
        raise EngineAabbPaijiaTieError(f"{context}: decision detail is missing")
    if needs_detail and (
        type(row["detail"]) is not dict or set(row["detail"]) != _DETAIL_FIELDS
    ):
        raise EngineAabbPaijiaTieError(f"{context}: decision detail is invalid")
    if row["outcome"] in (*CHANGE_OUTCOMES, TSUMOGIRI_ONLY) and (
        row["stage"] != STAGE_SWITCH
    ):
        raise EngineAabbPaijiaTieError(f"{context}: a change outside PAIJIA_SWITCH")
    if needs_detail:
        _require_detail(row["outcome"], row["stage"], row["detail"], context)


def _require_detail(outcome: str, stage: str, detail: dict, context: str) -> None:
    """保存したdetailがoutcomeと整合することを確かめる（paijia・評価値は再計算しない）。

    detailはsummaryに現れないので、内容はここで、改変はsummaryが拘束する
    ``shadow.json`` のdigestで検出する。
    """
    incumbent = detail["incumbent_action"]
    candidate = detail["candidate_action"]
    analysis = detail["analysis"]
    violations = detail["violations"]
    if type(violations) is not list or any(type(v) is not str for v in violations):
        raise EngineAabbPaijiaTieError(f"{context}: violations must be strings")
    if type(incumbent) is not dict:
        raise EngineAabbPaijiaTieError(f"{context}: the B action is not recorded")
    if outcome in INVALID_OUTCOMES:
        if not violations:
            raise EngineAabbPaijiaTieError(f"{context}: an invalid row needs reasons")
        if (outcome == "candidate-illegal-action") != (candidate is None):
            raise EngineAabbPaijiaTieError(f"{context}: illegal-action detail mismatch")
        return
    if violations:
        raise EngineAabbPaijiaTieError(f"{context}: a valid row has violations")
    if type(candidate) is not dict:
        raise EngineAabbPaijiaTieError(f"{context}: the A action is not recorded")
    if outcome == SAME:
        if (
            candidate != incumbent
            or type(analysis) is not dict
            or analysis.get("__type") != "SuitedPaijiaTieAnalysis"
            or analysis.get("stage") != f"SuitedPaijiaTieStage.{stage}"
            or analysis.get("champion_action") != incumbent
            or analysis.get("selected_action") != candidate
        ):
            raise EngineAabbPaijiaTieError(f"{context}: same-action detail mismatch")
        return
    champion, selected = require_switch_analysis(analysis, context)
    if champion != incumbent or selected != candidate:
        raise EngineAabbPaijiaTieError(
            f"{context}: switch analysis differs from the recorded actions"
        )
    if discard_change_kind(incumbent, candidate, context) != outcome:
        raise EngineAabbPaijiaTieError(f"{context}: change kind differs from tiles")


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
    a_seconds, b_seconds = [], []
    per_seed = []
    adjusted = []
    for observed, row in zip(shadow["hanchans"], timings["hanchans"], strict=True):
        seat_changes = []
        hanchan_policy_seconds = 0.0
        for seat in observed["seats"]:
            changes = 0
            for decision in seat["decisions"]:
                _count(by_stage, decision["stage"])
                by_outcome[decision["outcome"]] += 1
                a_seconds.append(decision["a_seconds"])
                b_seconds.append(decision["b_seconds"])
                hanchan_policy_seconds += decision["a_seconds"] + decision["b_seconds"]
                changes += decision["outcome"] in CHANGE_OUTCOMES
            seat_changes.append(changes)
        per_seed.append(
            {
                "changes": sum(seat_changes),
                "changes_by_seat": seat_changes,
                "seed": observed["seed"],
            }
        )
        if hanchan_policy_seconds > row["wall_seconds"]:
            raise EngineAabbPaijiaTieError(
                f"seed {observed['seed']}: decision seconds exceed the hanchan wall"
            )
        adjusted.append(row["wall_seconds"] - hanchan_policy_seconds / 2)
    changes = sum(by_outcome[name] for name in CHANGE_OUTCOMES)
    invalid = {name: by_outcome[name] for name in INVALID_OUTCOMES}
    seeds_with_change = sum(1 for row in per_seed if row["changes"] > 0)
    seat_hanchans = 4 * len(per_seed)
    criteria = plan["criteria"]
    budget = criteria["budget"]
    screen_wall = criteria["screen_limits"]["wall_seconds"]
    mean_adjusted = sum(adjusted) / len(adjusted)
    projection = budget["screen_hanchans"] * mean_adjusted
    projection_document = {
        "margin": budget["margin"],
        "mean_adjusted_hanchan_seconds": mean_adjusted,
        "projected_screen_schedule_seconds": projection,
        "screen_hanchans": budget["screen_hanchans"],
        "screen_wall_seconds": screen_wall,
        "within_budget": projection <= budget["margin"] * screen_wall,
        "worst_case_screen_seconds_at_timeout": budget["screen_hanchans"]
        * plan["execution"]["timeout_seconds"],
    }
    checks = {
        "changes": changes,
        "invalid": sum(invalid.values()),
        "minimum_changes": criteria["minimum_changes"],
        "minimum_seeds_with_change": criteria["minimum_seeds_with_change"],
        "seeds_with_change": seeds_with_change,
        "within_budget": projection_document["within_budget"],
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
        elif not projection_document["within_budget"]:
            decision = STOP_BUDGET
        else:
            decision = PROCEED
        gate = {"checks": checks, "decision": decision}
    rows = timings["hanchans"]
    payload = {
        "inputs": {
            "hanchans_sha256": document_sha256(hanchans),
            "shadow_sha256": document_sha256(shadow),
        },
        "activation": {
            "by_outcome": by_outcome,
            "by_stage": dict(sorted(by_stage.items())),
            "changes": changes,
            "changes_per_seat_hanchan": changes / seat_hanchans,
            "decisions": sum(by_outcome.values()),
            "gate_passed_decisions": sum(by_stage.get(s, 0) for s in ACTIVATED_STAGES),
            "invalid": invalid,
            "per_seed": per_seed,
            "red_only_changes": by_outcome["red-only-change"],
            "seat_hanchans": seat_hanchans,
            "seeds_with_change": seeds_with_change,
            "tile_type_changes": by_outcome["tile-type-change"],
            "tsumogiri_only_differences": by_outcome[TSUMOGIRI_ONLY],
        },
        "decision_seconds": {
            "A_shadow": distribution(a_seconds),
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
        "screen_projection": projection_document,
        "stage": ACTIVATION,
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
    play: Callable[[HanchanTask], PlayedHanchan] = play_activation_hanchan,
    live_gate: Callable[..., None] = require_live_target,
    on_hanchan: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] | None = None,
    realtime: Callable[[], float] | None = None,
) -> dict[str, object]:
    """段階1を実行して公開する。``play`` / ``live_gate`` / clockはtestのseam。"""
    plan = load_plan(plan_path, stage=ACTIVATION)
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


def verified_activation(plan_path: str | Path) -> tuple[dict, dict, list[dict]]:
    """保存物からsummaryを再導出し、記録値との完全一致を要求する（engine・Policyは使わない）。"""
    plan = load_plan(plan_path, stage=ACTIVATION)
    output = Path(plan["artifact_destinations"]["output"])
    recorded = read_document(output / SUMMARY_FILENAME, "activation summary")
    hanchans = read_document(output / HANCHANS_FILENAME, "hanchans")
    timings = read_document(output / TIMINGS_FILENAME, "timings")
    summary = build_summary(
        plan, read_document(output / SHADOW_FILENAME, "shadow"), hanchans, timings
    )
    if recorded != summary:
        raise EngineAabbPaijiaTieError(
            "activation summary differs from the re-derivation"
        )
    require_timings(plan, timings, [row["kyoku_count"] for row in hanchans["hanchans"]])
    return plan, summary, output_files(output), clock_document(timings)


def verify_activation(plan_path: str | Path) -> dict[str, object]:
    plan, summary, files, clocks = verified_activation(plan_path)
    gate = summary["gate"]
    return {
        "clocks": clocks,
        "decision": None if gate is None else gate["decision"],
        "files": files,
        "plan_digest": plan["plan_digest"],
        "status": summary["status"],
        "summary_digest": summary["summary_digest"],
    }


def screen_prerequisite(activation_plan_path: str | Path) -> dict[str, object]:
    """段階2（allocated）のplanが拘束する段階1の結果。PROCEEDでなければ作らない。"""
    plan, summary, _, _ = verified_activation(activation_plan_path)
    if plan["run_kind"] != ALLOCATED or summary["gate"] is None:
        raise EngineAabbPaijiaTieError("the screen needs an allocated stage-1 result")
    if summary["gate"]["decision"] != PROCEED:
        raise EngineAabbPaijiaTieError(
            f"stage 1 ended with {summary['gate']['decision']}; the screen is not run"
        )
    return {
        "activation_plan_digest": plan["plan_digest"],
        "activation_summary_digest": summary["summary_digest"],
        "screen_limits": plan["criteria"]["screen_limits"],
    }


__all__ = [
    "COMPLETE_STATUS",
    "PROCEED",
    "REHEARSAL_STATUS",
    "STOP_BUDGET",
    "STOP_INVALID",
    "STOP_LOW_ACTIVATION",
    "build_summary",
    "run_activation",
    "screen_prerequisite",
    "verify_activation",
]
