"""#534 call-value-shadow-v1（#533）の実行・summary・判定・strict verify。

```text
run      live gate -> 半荘（seed順、4席Champion、配置の入替なし） -> live gate
         -> <output>/{shadow.jsonl, hanchans.json, summary.json, timings.json, SHA256SUMS}
         （観測なしrehearsalはshadow.jsonlを持たない）
verify   別processで保存物だけを読み、hash・schedule・形を検査し、保存したpublic context
         から同じrevisionのlisjong解析を再導出して記録と照合し、summaryと判定を再導出する
compare  rehearsalの観測あり / なしの2つのplanで、seedごとのAction列digestと
         客観outcome（CompletedMatch digest・終局・最終点）の一致を要求する
```

- ``shadow.jsonl``: 1行目はheader、以降は半荘ごとに1行（座席別の判断数・call機会数、
  call意図ごとの対応・取消・lisjong解析・public pre / post context）。private truthなし
- ``hanchans.json``: 客観値（局数・終了理由・final_points・``CompletedMatch`` digest・
  呼び出し数・席ごとの本番Action列digest）。analysisと混ぜない
- 判定は実行前に固定した順に1つだけ出す（allocatedのみ）:
  ``STOP-INVALID`` > ``STOP-INCOMPLETE``（実行が完了しないとsummaryを作らない）>
  ``STOP-LOW-ACTIVATION`` > ``STOP-SPEED-TRADEOFF`` > ``PROCEED-TO-CANDIDATE-DESIGN``
- 比率は整数の分子 / 分母（``fractions.Fraction``）で比べ、floatで判定しない。
  判断行を独立標本とした信頼区間・検定は出さない
- Arenaはproxy・mass・代替・分類を再計算しない。lisjongの値どうしの整合と、
  保存contextからの同じAPIの再導出だけを照合する
"""

import json
import math
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    parse_json_text,
    sha256_bytes,
    write_new_artifact_file,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .call_value_shadow_plan import (
    EVALUATION_KIND,
    OBSERVED,
    PROCEED,
    SEAT_HANCHANS,
    STOP_INVALID,
    STOP_LOW_ACTIVATION,
    STOP_SCHEMA,
    STOP_SPEED_TRADEOFF,
    UNOBSERVED,
    CallValueShadowError,
    load_plan,
    plan_champion,
    plan_schedule,
    require_live_target,
)
from .call_value_shadow_worker import (
    CANCELLATIONS,
    ESTABLISHED,
    INVALID,
    ROUTES,
    SHADOW_VERSION,
    SPEED_CALL,
    STATUSES,
    decode_action,
    decode_context,
    meld_matches_call,
    play_observed_hanchan,
    play_unobserved_hanchan,
)
from .executor import ExecutedHanchan, HanchanTask, PlayedHanchan
from .lock import EngineAabbLockError, _live_environment
from .paijia_tie_activation import _hanchan_document
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
    read_document,
    require_timings,
)

SHADOW_FILENAME = "shadow.jsonl"
HANCHANS_FILENAME = "hanchans.json"
SUMS_FILENAME = "SHA256SUMS"
SHADOW_SCHEMA = "arena-call-value-shadow-v1-shadow-v1"
HANCHANS_SCHEMA = "arena-call-value-shadow-v1-hanchans-v1"
SUMMARY_SCHEMA = "arena-call-value-shadow-v1-summary-v1"
VERIFY_SCHEMA = "arena-call-value-shadow-v1-verify-v1"
COMPARE_SCHEMA = "arena-call-value-shadow-v1-rehearsal-compare-v1"

COMPLETE_STATUS = (
    "CALL-VALUE SHADOW COMPLETE; DEVELOPMENT DIAGNOSTIC, NOT A STRENGTH RESULT"
)
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO GATE DECISION"

PRIMARY_CLASSIFICATION = "PostCallClassification.BELOW_THRESHOLD"
CLASSIFICATIONS = (
    "PostCallClassification.BELOW_MAX",
    PRIMARY_CLASSIFICATION,
    "PostCallClassification.CONSISTENT",
)
CHANGE_KINDS = ("red-only-change", "tile-type-change")

_HANCHAN_FIELDS = frozenset(
    {
        "action_sequence_sha256",
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
_SHADOW_HANCHAN_FIELDS = frozenset(
    {"calls", "kind", "placement", "rounds", "seats", "seed"}
)
_SEAT_FIELDS = frozenset({"call_opportunities", "decisions", "seat"})
_CALL_FIELDS = frozenset(
    {
        "actor",
        "call",
        "call_analysis",
        "decision_ordinal",
        "invalid",
        "is_primary_target",
        "post",
        "pre_context",
        "resolution",
        "response_ordinal",
        "round_ordinal",
    }
)
_POST_FIELDS = frozenset({"analysis", "context", "decision_ordinal", "discard"})


def _fail(message: str):
    raise CallValueShadowError(f"call-value-shadow-v1: {message}")


def _ratio(value: Fraction) -> dict[str, int]:
    return {"denominator": value.denominator, "numerator": value.numerator}


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


# ---------------------------------------------------------------------------
# 保存物の形
# ---------------------------------------------------------------------------


def shadow_lines(plan: dict, executed: list[ExecutedHanchan]) -> list[dict]:
    header = {
        "kind": "header",
        "plan_digest": plan["plan_digest"],
        "schema": SHADOW_SCHEMA,
        "shadow_version": SHADOW_VERSION,
    }
    rows = []
    for item in executed:
        observed = item.policy_analysis
        rows.append(
            {
                "calls": observed["calls"],
                "kind": "hanchan",
                "placement": item.capture.placement,
                "rounds": observed["rounds"],
                "seats": observed["seats"],
                "seed": item.capture.seed,
            }
        )
    return [header, *rows]


def canonical_line(value: object) -> str:
    """JSON Linesの1行（key順・区切りを固定、非有限数を拒否）。"""
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


def shadow_text(lines: list[dict]) -> str:
    return "".join(canonical_line(line) for line in lines)


def read_shadow(path: Path) -> list[dict]:
    """canonical JSONの行だけを受け付ける（空行・非canonical・改行なしを拒否）。"""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CallValueShadowError(f"cannot read the shadow: {error}") from error
    if not text.endswith("\n"):
        _fail("shadow.jsonl must end with a newline")
    lines = []
    for raw in text[:-1].split("\n"):
        try:
            value = parse_json_text(raw)
        except (ArtifactValidationError, ValueError) as error:
            raise CallValueShadowError(f"shadow line is invalid: {error}") from error
        if canonical_line(value) != raw + "\n":
            _fail("shadow line is not canonical JSON")
        lines.append(value)
    return lines


def hanchans_document(plan: dict, executed: list[ExecutedHanchan]) -> dict:
    return {
        "hanchans": [
            {
                **_hanchan_document(item),
                "action_sequence_sha256": item.policy_analysis[
                    "action_sequence_sha256"
                ],
            }
            for item in executed
        ],
        "plan_digest": plan["plan_digest"],
        "schema": HANCHANS_SCHEMA,
    }


def require_hanchans(plan: dict, hanchans: object) -> None:
    """客観recordが計画したscheduleと完全一致すること（不足・重複・順序の入替を拒否）。"""
    expected = [(item["seed"], item["placement"]) for item in plan_schedule(plan)]
    if (
        type(hanchans) is not dict
        or set(hanchans) != {"hanchans", "plan_digest", "schema"}
        or hanchans["schema"] != HANCHANS_SCHEMA
        or hanchans["plan_digest"] != plan["plan_digest"]
        or type(hanchans["hanchans"]) is not list
        or any(type(row) is not dict for row in hanchans["hanchans"])
        or [(row.get("seed"), row.get("placement")) for row in hanchans["hanchans"]]
        != expected
    ):
        _fail("hanchans do not cover the planned schedule")
    for row in hanchans["hanchans"]:
        context = f"seed {row['seed']}"
        if set(row) != _HANCHAN_FIELDS:
            _fail(f"{context}: hanchan fields do not match")
        counts = row["policy_call_counts"]
        if (
            type(counts) is not list
            or len(counts) != 4
            or not all(_nonnegative_int(count) for count in counts)
            or row["selector_call_counts"] != counts
        ):
            _fail(f"{context}: call counts are invalid")
        if type(row["kyoku_count"]) is not int or row["kyoku_count"] <= 0:
            _fail(f"{context}: kyoku_count is invalid")
        digests = row["action_sequence_sha256"]
        if (
            type(digests) is not list
            or len(digests) != 4
            or any(type(item) is not str or len(item) != 64 for item in digests)
        ):
            _fail(f"{context}: action sequence digests are invalid")


def require_shadow(plan: dict, lines: object, hanchans: dict) -> None:
    """shadowの形、scheduleとの完全一致、判断数とPolicy呼び出し数の一致を検査する。"""
    require_hanchans(plan, hanchans)
    header = {
        "kind": "header",
        "plan_digest": plan["plan_digest"],
        "schema": SHADOW_SCHEMA,
        "shadow_version": SHADOW_VERSION,
    }
    if type(lines) is not list or not lines or lines[0] != header:
        _fail("shadow header does not belong to the plan")
    rows = lines[1:]
    expected = [(item["seed"], item["placement"]) for item in plan_schedule(plan)]
    if (
        any(type(row) is not dict for row in rows)
        or [(row.get("seed"), row.get("placement")) for row in rows] != expected
    ):
        _fail("shadow does not cover the planned schedule")
    for row, objective in zip(rows, hanchans["hanchans"], strict=True):
        context = f"seed {row['seed']}"
        if set(row) != _SHADOW_HANCHAN_FIELDS or row["kind"] != "hanchan":
            _fail(f"{context}: shadow hanchan fields do not match")
        if row["rounds"] != objective["kyoku_count"]:
            _fail(f"{context}: observed rounds differ from the played rounds")
        seats = row["seats"]
        if (
            type(seats) is not list
            or any(
                type(seat) is not dict or set(seat) != _SEAT_FIELDS for seat in seats
            )
            or [seat["seat"] for seat in seats] != [0, 1, 2, 3]
        ):
            _fail(f"{context}: shadow seats do not match")
        for seat, count in zip(seats, objective["policy_call_counts"], strict=True):
            if seat["decisions"] != count:
                _fail(f"{context}: observed decisions differ from the Policy calls")
            if not _nonnegative_int(seat["call_opportunities"]) or (
                seat["call_opportunities"] > count
            ):
                _fail(f"{context}: call opportunities are invalid")
        calls = row["calls"]
        if type(calls) is not list:
            _fail(f"{context}: calls must be a list")
        keys = set()
        for call in calls:
            _require_call(call, row, context)
            key = (call["round_ordinal"], call["actor"], call["decision_ordinal"])
            if key in keys:
                _fail(f"{context}: a call decision is recorded twice")
            keys.add(key)
        order = [
            (call["round_ordinal"], call["decision_ordinal"], call["actor"])
            for call in calls
        ]
        if [item[0] for item in order] != sorted(item[0] for item in order):
            _fail(f"{context}: calls are not in round order")
        posts = [
            (call["actor"], call["post"]["decision_ordinal"])
            for call in calls
            if call["post"] is not None
        ]
        if len(posts) != len(set(posts)):
            _fail(f"{context}: a post-call decision is matched twice")
        for actor in range(4):
            intents = sum(1 for call in calls if call["actor"] == actor)
            if intents > seats[actor]["call_opportunities"]:
                _fail(f"{context}: more call intents than call opportunities")


def _require_call(call: object, row: dict, context: str) -> None:
    if type(call) is not dict or set(call) != _CALL_FIELDS:
        _fail(f"{context}: call fields do not match")
    for name in ("actor", "decision_ordinal", "response_ordinal", "round_ordinal"):
        if not _nonnegative_int(call[name]):
            _fail(f"{context}: call {name} is invalid")
    if call["actor"] > 3 or call["round_ordinal"] >= row["rounds"]:
        _fail(f"{context}: call identity is out of range")
    if call["decision_ordinal"] >= row["seats"][call["actor"]]["decisions"]:
        _fail(f"{context}: call decision ordinal is out of range")
    invalid = call["invalid"]
    if type(invalid) is not list or any(type(item) is not str for item in invalid):
        _fail(f"{context}: invalid reasons must be strings")
    resolution = call["resolution"]
    if (
        type(resolution) is not dict
        or set(resolution) != {"detail", "status"}
        or resolution["status"] not in STATUSES
    ):
        _fail(f"{context}: call resolution is invalid")
    if resolution["status"] == INVALID and not invalid:
        _fail(f"{context}: an INVALID resolution needs reasons")
    if type(call["call"]) is not dict:
        _fail(f"{context}: the call action is not recorded")
    post = call["post"]
    if post is not None:
        if type(post) is not dict or set(post) != _POST_FIELDS:
            _fail(f"{context}: post fields do not match")
        if not _nonnegative_int(post["decision_ordinal"]) or not (
            call["decision_ordinal"]
            < post["decision_ordinal"]
            < row["seats"][call["actor"]]["decisions"]
        ):
            _fail(f"{context}: post-call decision ordinal is out of order")
        if resolution["status"] != ESTABLISHED:
            _fail(f"{context}: a post-call discard for a call that was not established")
    if invalid:
        return
    analysis = call["call_analysis"]
    if (
        type(analysis) is not dict
        or analysis.get("__type") != "SpeedCallDecisionAnalysis"
        or analysis.get("route") not in ROUTES
        or type(call["pre_context"]) is not dict
        or type(call["is_primary_target"]) is not bool
    ):
        _fail(f"{context}: a valid call has no call analysis")
    if call["is_primary_target"] and analysis["route"] != SPEED_CALL:
        _fail(f"{context}: a primary target outside a speed call")
    needs_post = resolution["status"] == ESTABLISHED and analysis["route"] == SPEED_CALL
    if needs_post != (post is not None):
        _fail(
            f"{context}: post-call analysis must exist exactly for established speed calls"
        )
    if post is None:
        return
    post_analysis = post["analysis"]
    if (
        type(post_analysis) is not dict
        or post_analysis.get("__type") != "PostCallDiscardAnalysis"
        or post_analysis.get("call_analysis") != analysis
        or post_analysis.get("classification") not in CLASSIFICATIONS
        or type(post["context"]) is not dict
        or type(post["discard"]) is not dict
    ):
        _fail(f"{context}: post-call analysis is invalid")


# ---------------------------------------------------------------------------
# lisjongの値どうしの整合（再計算ではない）
# ---------------------------------------------------------------------------


def _facts(post_analysis: dict, action: dict) -> dict:
    matches = [
        item
        for item in post_analysis["candidates"]
        if type(item) is dict and item.get("action") == action
    ]
    if len(matches) != 1:
        raise CallValueShadowError("an action is not exactly one lisjong candidate")
    return matches[0]


def post_call_row(call: dict, seed: int) -> dict:
    """成立したspeed callの直後打牌1件を、lisjongの値から読む（整合しなければ例外）。"""
    analysis = call["post"]["analysis"]
    call_analysis = analysis["call_analysis"]
    horizon = analysis["horizon"]
    hidden = analysis["hidden_tile_count"]
    denominator = analysis["sequence_denominator"]
    candidates = analysis["candidates"]
    if not (
        type(horizon) is int
        and horizon > 0
        and _nonnegative_int(hidden)
        and type(denominator) is int
        and denominator > 0
        and type(candidates) is list
        and candidates
    ):
        raise CallValueShadowError("post-call horizon / denominator is invalid")
    for item in candidates:
        if (
            type(item) is not dict
            or not all(
                _nonnegative_int(item.get(name))
                for name in ("completion_mass", "route_shanten", "ukeire")
            )
            or type(item.get("value")) is not int
        ):
            raise CallValueShadowError("a post-call candidate is invalid")
        if item["completion_mass"] > denominator:
            raise CallValueShadowError("completion mass exceeds the denominator")
    discard = analysis["discard"]
    chosen = _facts(analysis, discard)
    if chosen["route_shanten"] != call_analysis["route_shanten"]:
        raise CallValueShadowError("the chosen discard changed the route shanten")
    classification = analysis["classification"]
    row = {
        "actor": call["actor"],
        "classification": classification,
        "closed_before_call": call_analysis["closed_before_call"],
        "decision_ordinal": call["decision_ordinal"],
        "mode": call_analysis["mode"],
        "primary_target": call["is_primary_target"],
        "round_ordinal": call["round_ordinal"],
        "route_shanten": call_analysis["route_shanten"],
        "seed": seed,
        "value_below_call": call_analysis["value"] - chosen["value"],
    }
    alternative, loss = analysis["alternative"], analysis["speed_loss"]
    below = classification == PRIMARY_CLASSIFICATION
    if below != (alternative is not None) or below != (loss is not None):
        raise CallValueShadowError(
            "alternative / speed loss exist only below threshold"
        )
    if not below:
        return row
    alternative_facts = _facts(analysis, alternative)
    if alternative_facts["route_shanten"] != call_analysis["route_shanten"] or (
        alternative_facts["value"] < call_analysis["minimum_value"]
    ):
        raise CallValueShadowError("the alternative does not keep route and threshold")
    chosen_mass = chosen["completion_mass"]
    alternative_mass = alternative_facts["completion_mass"]
    if alternative_mass > chosen_mass:
        raise CallValueShadowError("the alternative mass exceeds the chosen mass")
    zero = chosen_mass == 0
    expected_loss = {
        "__type": "SpeedLoss",
        "mass_difference": chosen_mass - alternative_mass,
        "relative_denominator": 1 if zero else chosen_mass,
        "relative_numerator": chosen_mass - alternative_mass,
        "zero_mass": zero,
    }
    if loss != expected_loss:
        raise CallValueShadowError("speed loss differs from the candidate masses")
    kind = discard_change_kind(discard, alternative, "post-call discard")
    if kind not in CHANGE_KINDS:
        raise CallValueShadowError("a tsumogiri-only difference in a mandatory discard")
    row["below_threshold"] = {
        "alternative_mass": alternative_mass,
        "change_kind": kind,
        "chosen_mass": chosen_mass,
        "hidden_tile_count": hidden,
        "horizon": horizon,
        "loss": _ratio(
            Fraction(loss["relative_numerator"], loss["relative_denominator"])
        ),
        "loss_lisjong": {
            "denominator": loss["relative_denominator"],
            "numerator": loss["relative_numerator"],
        },
        "mass_difference": loss["mass_difference"],
        "proxy_difference": {
            "denominator": denominator,
            "numerator": chosen_mass - alternative_mass,
        },
        "sequence_denominator": denominator,
        "ukeire_difference": chosen["ukeire"] - alternative_facts["ukeire"],
        "value_difference": alternative_facts["value"] - chosen["value"],
        "zero_mass": zero,
    }
    return row


# ---------------------------------------------------------------------------
# 分位・判定（整数比）
# ---------------------------------------------------------------------------


def median(values: list[Fraction]) -> Fraction:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def p90(values: list[Fraction]) -> Fraction:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(Fraction(9, 10) * len(ordered))) - 1]


def gate_decision(
    criteria: dict, *, invalid: int, changes: list[tuple[int, Fraction]]
) -> tuple[str, dict]:
    """#533 §6の順に1つだけ判定する（``changes`` は主対象の変更行の (seed, L)）。"""
    seeds = len({seed for seed, _ in changes})
    losses = [loss for _, loss in changes]
    checks = {
        "changes": len(changes),
        "invalid": invalid,
        "median_loss": _ratio(median(losses)) if losses else None,
        "minimum_changes": criteria["minimum_changes"],
        "minimum_seeds_with_change": criteria["minimum_seeds_with_change"],
        "p90_loss": _ratio(p90(losses)) if losses else None,
        "seeds_with_change": seeds,
    }
    if invalid > 0:
        return STOP_INVALID, checks
    if len(changes) < criteria["minimum_changes"] or (
        seeds < criteria["minimum_seeds_with_change"]
    ):
        return STOP_LOW_ACTIVATION, checks
    median_limit = Fraction(
        criteria["median_loss_at_most"]["numerator"],
        criteria["median_loss_at_most"]["denominator"],
    )
    p90_limit = Fraction(
        criteria["p90_loss_at_most"]["numerator"],
        criteria["p90_loss_at_most"]["denominator"],
    )
    if median(losses) > median_limit or p90(losses) > p90_limit:
        return STOP_SPEED_TRADEOFF, checks
    return PROCEED, checks


# ---------------------------------------------------------------------------
# summary（保存物だけから再導出できる値）
# ---------------------------------------------------------------------------


def _count(counter: dict, key: str, amount: int = 1) -> None:
    counter[key] = counter.get(key, 0) + amount


def _fraction(numerator: int, denominator: int) -> dict:
    return {"denominator": denominator, "numerator": numerator}


def build_summary(plan: dict, lines: list[dict], hanchans: dict, timings: dict) -> dict:
    """summaryは保存物（plan・shadow・hanchans・timingsの半荘行とschedule wall）だけから決まる。"""
    require_shadow(plan, lines, hanchans)
    require_timings(
        plan,
        timings,
        [row["kyoku_count"] for row in hanchans["hanchans"]],
        complete=False,
        schedule=plan_schedule(plan),
    )
    decisions = opportunities = 0
    intents_by_route = dict.fromkeys(ROUTES, 0)
    resolutions = dict.fromkeys(STATUSES, 0)
    invalid_calls = 0
    inconsistent = []
    secondary: dict[str, int] = {}
    below_max_value_gaps: dict[str, int] = {}
    primary_rows = []
    per_seed = []
    counts = dict.fromkeys(
        (
            "primary_below_threshold",
            "primary_calls",
            "primary_changes",
            "primary_with_alternative",
            "primary_zero_mass",
            "red_only_changes",
            "speed_calls_established",
            "tile_type_changes",
        ),
        0,
    )
    for row in lines[1:]:
        seed = row["seed"]
        seat_changes = [0, 0, 0, 0]
        seat_primary = [0, 0, 0, 0]
        seat_intents = [0, 0, 0, 0]
        for seat in row["seats"]:
            decisions += seat["decisions"]
            opportunities += seat["call_opportunities"]
        for call in row["calls"]:
            seat_intents[call["actor"]] += 1
            resolutions[call["resolution"]["status"]] += 1
            if call["invalid"]:
                invalid_calls += 1
                continue
            intents_by_route[call["call_analysis"]["route"]] += 1
            if call["post"] is None:
                continue
            counts["speed_calls_established"] += 1
            try:
                facts = post_call_row(call, seed)
            except CallValueShadowError as error:
                inconsistent.append(
                    f"seed {seed} seat {call['actor']} decision "
                    f"{call['decision_ordinal']}: {error}"
                )
                continue
            key = (
                f"{facts['mode']}|primary={facts['primary_target']}|"
                f"{facts['classification']}"
            )
            _count(secondary, key)
            if facts["classification"] == "PostCallClassification.BELOW_MAX":
                _count(below_max_value_gaps, str(facts["value_below_call"]))
            if not facts["primary_target"]:
                continue
            counts["primary_calls"] += 1
            seat_primary[call["actor"]] += 1
            if facts["classification"] != PRIMARY_CLASSIFICATION:
                continue
            counts["primary_below_threshold"] += 1
            counts["primary_with_alternative"] += 1
            counts["primary_changes"] += 1
            seat_changes[call["actor"]] += 1
            below = facts["below_threshold"]
            counts["primary_zero_mass"] += below["zero_mass"]
            counts[
                "red_only_changes"
                if below["change_kind"] == "red-only-change"
                else "tile_type_changes"
            ] += 1
            primary_rows.append(facts)
        per_seed.append(
            {
                "call_intents_by_seat": seat_intents,
                "primary_calls_by_seat": seat_primary,
                "primary_changes": sum(seat_changes),
                "primary_changes_by_seat": seat_changes,
                "seed": seed,
            }
        )
    seat_hanchans = 4 * len(per_seed)
    intents = sum(resolutions.values())
    cancelled = {name: resolutions[name] for name in CANCELLATIONS}
    invalid_total = invalid_calls + len(inconsistent)
    changes = [
        (
            facts["seed"],
            Fraction(
                facts["below_threshold"]["loss"]["numerator"],
                facts["below_threshold"]["loss"]["denominator"],
            ),
        )
        for facts in primary_rows
    ]
    gate = None
    if plan["run_kind"] == ALLOCATED:
        decision, checks = gate_decision(
            plan["criteria"], invalid=invalid_total, changes=changes
        )
        gate = {"checks": checks, "decision": decision}
    losses = [loss for _, loss in changes]
    rows = timings["hanchans"]
    payload = {
        "activation": {
            **counts,
            "call_intents": intents,
            "call_intents_by_route": intents_by_route,
            "call_opportunities": opportunities,
            "cancelled": cancelled,
            "decisions": decisions,
            "established": resolutions[ESTABLISHED],
            "inconsistent_analyses": inconsistent,
            "invalid_calls": invalid_calls,
            "primary_changes_per_seat_hanchan": _fraction(
                counts["primary_changes"], seat_hanchans
            ),
            "seat_hanchans": seat_hanchans,
            "seeds": len(per_seed),
            "seeds_with_primary_change": len({seed for seed, _ in changes}),
        },
        "evaluation_kind": EVALUATION_KIND,
        "gate": gate,
        "hanchan_count": len(rows),
        "inputs": {
            "hanchans_sha256": document_sha256(hanchans),
            "shadow_sha256": sha256_bytes(shadow_text(lines).encode("utf-8")),
        },
        "kyoku_count": sum(row["kyoku_count"] for row in rows),
        "memory": memory_summary(rows),
        "not_a_strength_result": True,
        "observation": plan["observation"],
        "per_seed": per_seed,
        "plan_digest": plan["plan_digest"],
        "primary_rows": primary_rows,
        "primary_loss": {
            "count": len(losses),
            "median": _ratio(median(losses)) if losses else None,
            "p90": _ratio(p90(losses)) if losses else None,
        },
        "run_kind": plan["run_kind"],
        "schema": SUMMARY_SCHEMA,
        "secondary": {
            "below_max_value_gaps": dict(sorted(below_max_value_gaps.items())),
            "established_speed_calls_by_mode_target_classification": dict(
                sorted(secondary.items())
            ),
            "note": (
                "descriptive only; VALUE / SPEED / non-primary / BELOW_MAX never "
                "enter the decision"
            ),
        },
        "shadow_version": SHADOW_VERSION,
        "status": COMPLETE_STATUS if gate is not None else REHEARSAL_STATUS,
        "timing": {
            "hanchan_wall_seconds": distribution([row["wall_seconds"] for row in rows]),
            "kyoku_per_hanchan": distribution([row["kyoku_count"] for row in rows]),
            "schedule_wall_seconds": timings["schedule_wall_seconds"],
        },
    }
    if plan["run_kind"] == ALLOCATED and seat_hanchans != SEAT_HANCHANS:
        _fail("the allocated run does not cover 96 seat-hanchans")
    return {**payload, "summary_digest": document_identity(payload)}


def build_unobserved_summary(plan: dict, hanchans: dict, timings: dict) -> dict:
    require_hanchans(plan, hanchans)
    require_timings(
        plan,
        timings,
        [row["kyoku_count"] for row in hanchans["hanchans"]],
        complete=False,
        schedule=plan_schedule(plan),
    )
    rows = timings["hanchans"]
    payload = {
        "evaluation_kind": EVALUATION_KIND,
        "gate": None,
        "hanchan_count": len(rows),
        "inputs": {"hanchans_sha256": document_sha256(hanchans)},
        "kyoku_count": sum(row["kyoku_count"] for row in rows),
        "memory": memory_summary(rows),
        "not_a_strength_result": True,
        "observation": UNOBSERVED,
        "plan_digest": plan["plan_digest"],
        "run_kind": plan["run_kind"],
        "schema": SUMMARY_SCHEMA,
        "shadow_version": SHADOW_VERSION,
        "status": REHEARSAL_STATUS,
        "timing": {
            "hanchan_wall_seconds": distribution([row["wall_seconds"] for row in rows]),
            "schedule_wall_seconds": timings["schedule_wall_seconds"],
        },
    }
    return {**payload, "summary_digest": document_identity(payload)}


# ---------------------------------------------------------------------------
# 保存contextからのlisjong解析の再導出
# ---------------------------------------------------------------------------


def _canonical(value):
    from .backend_check import canonical_engine_value

    return canonical_engine_value(value)


def rederive_calls(lines: list[dict]) -> int:
    """保存したpublic contextから同じrevisionのlisjong解析を再導出し、記録と照合する。

    戻り値は再導出した解析の数。不一致は ``CallValueShadowError``。
    """
    from lisjong.policies import speed_call_analysis as api

    count = 0
    for row in lines[1:]:
        for call in row["calls"]:
            context = (
                f"seed {row['seed']} seat {call['actor']} decision "
                f"{call['decision_ordinal']}"
            )
            if call["pre_context"] is None:
                if not call["invalid"]:
                    _fail(f"{context}: a valid call without its context")
                continue
            try:
                pre = decode_context(call["pre_context"])
                selected = decode_action(call["call"])
            except Exception as error:
                raise CallValueShadowError(
                    f"{context}: stored call context cannot be read: {error}"
                ) from error
            if (
                selected not in pre.legal_actions
                or int(selected.actor) != call["actor"]
            ):
                _fail(f"{context}: the call is not a legal action of its actor")
            try:
                analysis = api.analyze_speed_call_decision(pre, selected)
            except Exception as error:
                if call["call_analysis"] is not None or not call["invalid"]:
                    raise CallValueShadowError(
                        f"{context}: lisjong rejects a recorded call analysis: {error}"
                    ) from error
                continue
            if (
                _canonical(analysis) != call["call_analysis"]
                or analysis.is_primary_target is not call["is_primary_target"]
            ):
                _fail(f"{context}: re-derived call analysis differs")
            count += 1
            post = call["post"]
            if post is None or post["context"] is None:
                continue
            try:
                post_context = decode_context(post["context"])
                discard = decode_action(post["discard"])
            except Exception as error:
                raise CallValueShadowError(
                    f"{context}: stored post-call context cannot be read: {error}"
                ) from error
            melds = post_context.input.players[call["actor"]].melds
            if not melds or not meld_matches_call(melds[-1], selected):
                _fail(f"{context}: the post-call context does not hold the call")
            try:
                post_analysis = api.analyze_post_call_discard(
                    post_context, analysis, discard
                )
            except Exception as error:
                if post["analysis"] is not None or not call["invalid"]:
                    raise CallValueShadowError(
                        f"{context}: lisjong rejects a recorded post-call analysis: "
                        f"{error}"
                    ) from error
                continue
            if _canonical(post_analysis) != post["analysis"]:
                _fail(f"{context}: re-derived post-call analysis differs")
            count += 1
    return count


# ---------------------------------------------------------------------------
# run / verify
# ---------------------------------------------------------------------------


def _sums_text(directory: Path) -> str:
    entries = output_files(directory, exclude=(SUMS_FILENAME,))
    return "".join(f"{item['sha256']}  {item['path']}\n" for item in entries)


def _finalize(staging: Path) -> None:
    write_new_artifact_file(staging / SUMS_FILENAME, _sums_text(staging))


def _publish(plan: dict, clock: Callable[[], float]):
    def publish(staging: Path, executed: list[ExecutedHanchan], timings: dict):
        write_started = clock()
        hanchans = hanchans_document(plan, executed)
        if plan["observation"] == OBSERVED:
            lines = shadow_lines(plan, executed)
            require_shadow(plan, lines, hanchans)
            write_new_artifact_file(staging / SHADOW_FILENAME, shadow_text(lines))
        else:
            require_hanchans(plan, hanchans)
        write_new_artifact_file(
            staging / HANCHANS_FILENAME, canonical_json_text(hanchans)
        )
        timings["write_seconds"] = clock() - write_started
        summary_started = clock()
        if plan["observation"] == OBSERVED:
            summary = build_summary(plan, lines, hanchans, timings)
        else:
            summary = build_unobserved_summary(plan, hanchans, timings)
        write_new_artifact_file(
            staging / SUMMARY_FILENAME, canonical_json_text(summary)
        )
        timings["summary_seconds"] = clock() - summary_started
        return summary

    return publish


def run_plan(
    plan_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play: Callable[[HanchanTask], PlayedHanchan] | None = None,
    live_gate: Callable[..., None] = require_live_target,
    on_hanchan: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] | None = None,
    realtime: Callable[[], float] | None = None,
) -> dict[str, object]:
    """実行して公開する（失敗はSTOP-INCOMPLETEで何も公開しない）。

    ``play`` / ``live_gate`` / clockはtestのseam。
    """
    plan = load_plan(plan_path)
    if play is None:
        play = (
            play_observed_hanchan
            if plan["observation"] == OBSERVED
            else play_unobserved_hanchan
        )
    default_clock, default_realtime = default_clocks()
    clock = clock or default_clock
    realtime = realtime or default_realtime
    champion = plan_champion(plan)
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
        schedule=plan_schedule(plan),
        participants=(champion, champion),
        finalize=_finalize,
    )


_ANALYSIS_ENVIRONMENT = ("native", "python", "shanten_backend")


def require_analysis_environment(plan: dict, environment: dict) -> None:
    """再導出に使うlisjong / native / Python / engineがplanと同じであること。"""
    expected = plan["environment"]
    for name in _ANALYSIS_ENVIRONMENT:
        if environment.get(name) != expected[name]:
            _fail(f"verify environment {name} differs from the plan")
    for name in ("lisjong_revision", "lisjong_engine_revision"):
        if environment["provenance"].get(name) != expected["provenance"].get(name):
            _fail(f"verify environment {name} differs from the plan")


def verified_plan(
    plan_path: str | Path, *, environment: dict | None = None
) -> tuple[dict, dict, dict, list[dict], int]:
    """保存物を検査・再導出し、``(plan, summary, timings, files, rederived)`` を返す。"""
    plan = load_plan(plan_path)
    if environment is None:
        try:
            environment = _live_environment(plan["execution"]["wheel_path"])
        except EngineAabbLockError as error:
            raise CallValueShadowError(str(error)) from error
    output = Path(plan["artifact_destinations"]["output"])
    sums = output / SUMS_FILENAME
    try:
        recorded_sums = sums.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CallValueShadowError(f"cannot read {SUMS_FILENAME}: {error}") from error
    if recorded_sums != _sums_text(output):
        _fail("output files differ from SHA256SUMS (missing, extra or changed)")
    expected_files = {
        SUMMARY_FILENAME,
        HANCHANS_FILENAME,
        TIMINGS_FILENAME,
        SUMS_FILENAME,
    }
    if plan["observation"] == OBSERVED:
        expected_files.add(SHADOW_FILENAME)
    files = output_files(output)
    if {item["path"] for item in files} != expected_files:
        _fail("output files do not match the expected set")
    recorded = read_document(output / SUMMARY_FILENAME, "summary")
    hanchans = read_document(output / HANCHANS_FILENAME, "hanchans")
    timings = read_document(output / TIMINGS_FILENAME, "timings")
    rederived = 0
    if plan["observation"] == OBSERVED:
        lines = read_shadow(output / SHADOW_FILENAME)
        summary = build_summary(plan, lines, hanchans, timings)
        require_analysis_environment(plan, environment)
        rederived = rederive_calls(lines)
    else:
        summary = build_unobserved_summary(plan, hanchans, timings)
    if recorded != summary:
        _fail("summary differs from the re-derivation")
    require_timings(
        plan,
        timings,
        [row["kyoku_count"] for row in hanchans["hanchans"]],
        schedule=plan_schedule(plan),
    )
    return plan, summary, timings, files, rederived


def verify_plan(
    plan_path: str | Path,
    *,
    output: str | Path | None = None,
    environment: dict | None = None,
) -> dict[str, object]:
    plan, summary, timings, files, rederived = verified_plan(
        plan_path, environment=environment
    )
    gate = summary["gate"]
    document = {
        "clocks": clock_document(timings),
        "decision": None if gate is None else gate["decision"],
        "files": files,
        "observation": plan["observation"],
        "output_bytes": sum(item["bytes"] for item in files),
        "plan_digest": plan["plan_digest"],
        "rederived_lisjong_analyses": rederived,
        "run_kind": plan["run_kind"],
        "schema": VERIFY_SCHEMA,
        "status": summary["status"],
        "summary_digest": summary["summary_digest"],
    }
    if output is not None:
        destination = Path(output)
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite: {destination}")
        write_new_artifact_file(destination, canonical_json_text(document))
    return document


_COMPARED = (
    "action_sequence_sha256",
    "completed_match_sha256",
    "end_reason",
    "final_points",
    "kyoku_count",
    "policy_call_counts",
)


def compare_rehearsals(
    observed_plan: str | Path,
    unobserved_plan: str | Path,
    *,
    output: str | Path | None = None,
    environment: dict | None = None,
) -> dict[str, object]:
    """観測あり / なしのrehearsalで、Action列と客観outcomeの一致を要求する。"""
    observed, _, _, _, _ = verified_plan(observed_plan, environment=environment)
    unobserved, _, _, _, _ = verified_plan(unobserved_plan, environment=environment)
    if (observed["run_kind"], observed["observation"]) != ("rehearsal", OBSERVED) or (
        unobserved["run_kind"],
        unobserved["observation"],
    ) != ("rehearsal", UNOBSERVED):
        _fail("compare takes an observed and an unobserved rehearsal plan")
    for name in ("environment_digest", "execution_target", "participant", "population"):
        if observed[name] != unobserved[name]:
            _fail(f"rehearsal plans differ in {name}")
    rows = []
    for plan in (observed, unobserved):
        path = Path(plan["artifact_destinations"]["output"]) / HANCHANS_FILENAME
        rows.append(read_document(path, "hanchans")["hanchans"])
    seeds = []
    for left, right in zip(*rows, strict=True):
        differences = [name for name in _COMPARED if left[name] != right[name]]
        seeds.append({"differences": differences, "seed": left["seed"]})
    match = all(not item["differences"] for item in seeds)
    document = {
        "compared_fields": list(_COMPARED),
        "match": match,
        "observed_plan_digest": observed["plan_digest"],
        "schema": COMPARE_SCHEMA,
        "seeds": seeds,
        "unobserved_plan_digest": unobserved["plan_digest"],
    }
    if output is not None:
        destination = Path(output)
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite: {destination}")
        write_new_artifact_file(destination, canonical_json_text(document))
    if not match:
        _fail("observed and unobserved rehearsals differ (STOP-INVALID)")
    return document


__all__ = [
    "COMPLETE_STATUS",
    "REHEARSAL_STATUS",
    "build_summary",
    "build_unobserved_summary",
    "compare_rehearsals",
    "gate_decision",
    "median",
    "p90",
    "post_call_row",
    "read_shadow",
    "rederive_calls",
    "require_shadow",
    "run_plan",
    "verify_plan",
]
