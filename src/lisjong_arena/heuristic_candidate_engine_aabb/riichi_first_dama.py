"""#544 riichi-first-dama-branch-v1（lisjong #356のR1 pilot）の実行・照合・summary・strict verify。

```text
run       live gate -> R（seed順、Champion×4、配置の入替なし）
          -> D arm（Rの分岐点ごとに先頭から再実行。rehearsalの観測ありplanは全D armを
             別processでもう1回）-> live gate
          -> <output>/{hanchans.json, actions.jsonl, branches.jsonl, features.jsonl,
                       summary.json, timings.json, SHA256SUMS}（観測なしrehearsalはhanchansだけ）
          未完了ならoutputを作らず、diagnosticsへstop記録（それまでのtask行を含む）を残す
verify    別processで保存物だけを読み、hash・file集合・schedule・形・行動記録とdigestの整合を
          検査し、特徴・Δ・分散・費用判定を再導出する。allocatedは範囲の最小・最大の2 seedの
          Rと全D armを再実行し、行動記録を含めて完全一致を要求する
compare   rehearsalの観測あり / なしのRの一致と、D armの2回実行の一致を要求する
estimate  照合済みrehearsalから本実行＋verify＋回収の見積もりを作る（開始条件はplanで判定）
```

- riichi-dama-branch-v1（#540）の汎用部品（局記録・精算・JSON Lines・実行）を再利用し、
  schema・INVALID・summary・判定はこのprotocol専用にする。v1の契約・保存物は変えない
- **分岐後の分岐席のリーチは正常**。分岐点の判断でリーチが適用されたこと（分岐点の行動が
  宣言牌でない）、wrapperと行動列のリーチ数の不一致はINVALID
- 判定は ``STOP-INVALID`` > ``STOP-INCOMPLETE`` > ``PILOT-COMPLETE``。平均Δに基づく判定はしない。
  収集費用の一次判定は学習・採用へ進む判定ではない
"""

import datetime
import math
import random
import statistics
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import (
    canonical_json_text,
    sha256_bytes,
    staged_artifact_directory,
    write_new_artifact_file,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .call_value_shadow_worker import (
    action_sequence_digest,
    canonical_digest,
    decode_action,
)
from .errors import KyokuRecordError
from .executor import (
    ExecutedHanchan,
    HanchanTask,
    RunStopped,
    current_peak_rss_kib,
)
from .kyoku_record import (
    PROTOCOL_ID,
    SEAT_COUNT,
    RecordLayout,
    project_row,
    validate_hanchan_row,
)
from .lock import EngineAabbLockError, _live_environment
from .paijia_tie_activation import _hanchan_document
from .paijia_tie_plan import (
    ALLOCATED,
    REHEARSAL,
    SUMMARY_FILENAME,
    TIMINGS_FILENAME,
    clock_document,
    default_clocks,
    document_sha256,
    memory_summary,
    output_files,
    read_document,
    utc_now,
)
from .protocol import EngineAabbProtocolError
from .riichi_dama_branch import (
    ARM_D,
    ARM_R,
    _execute,
    _r_tasks,
    _rss,
    _seat_outcome,
    _seconds,
    _sums_text,
    _timing_summary,
    _validate_keys,
    _validate_seats,
    branch_id,
    d_settlement,
    delta_stats,
    execute_branches,
    expected_tasks,
    jsonl_text,
    nearest_rank,
    r_settlement,
    read_jsonl,
    rederive_features,
    require_analysis_environment,
    task_branch,
)
from .riichi_dama_branch_worker import BranchTask
from .riichi_first_dama_plan import (
    ALPHA,
    COLLECTION_BUDGET_SECONDS,
    COST_NOT_ESTIMABLE,
    COST_OVER_BUDGET,
    COST_VERIFY_SEEDS,
    COST_WITHIN_BUDGET,
    ESTIMATE_SCHEMA,
    EVALUATION_KIND,
    OBSERVED,
    PILOT_COMPLETE,
    POWER,
    RECOVERY_ALLOWANCE_SECONDS,
    STOP_INCOMPLETE,
    STOP_INVALID,
    STOP_SCHEMA,
    TARGET_EFFECT,
    UNOBSERVED,
    RiichiFirstDamaError,
    estimate_values,
    load_plan,
    parse_time_estimate,
    plan_champion,
    plan_schedule,
    require_live_target,
)
from .riichi_first_dama_worker import (
    FEATURES_VERSION,
    PROTOCOL_VERSION,
    WRAPPER_VERSION,
    play_d_arm,
    play_r_observed,
    play_r_unobserved,
)

HANCHANS_FILENAME = "hanchans.json"
ACTIONS_FILENAME = "actions.jsonl"
BRANCHES_FILENAME = "branches.jsonl"
FEATURES_FILENAME = "features.jsonl"
SUMS_FILENAME = "SHA256SUMS"
HANCHANS_SCHEMA = "arena-riichi-first-dama-branch-v1-hanchans-v1"
ACTIONS_SCHEMA = "arena-riichi-first-dama-branch-v1-actions-v1"
BRANCHES_SCHEMA = "arena-riichi-first-dama-branch-v1-branches-v1"
FEATURES_SCHEMA = "arena-riichi-first-dama-branch-v1-features-v1"
"""lisjong ``riichi-dama-features-v2`` のJSONを運ぶ。"""
SUMMARY_SCHEMA = "arena-riichi-first-dama-branch-v1-summary-v1"
TIMINGS_SCHEMA = "arena-riichi-first-dama-branch-v1-timings-v1"
VERIFY_SCHEMA = "arena-riichi-first-dama-branch-v1-verify-v1"
COMPARE_SCHEMA = "arena-riichi-first-dama-branch-v1-rehearsal-compare-v1"
KYOKU_RECORD_SCHEMA = "arena-riichi-first-dama-branch-v1-kyoku-record-v1"

COMPLETE_STATUS = "R1 PILOT COMPLETE; DEVELOPMENT PILOT, NOT A STRENGTH RESULT"
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO PILOT STATISTIC"
STOP_STATUS = (
    "STOP-INCOMPLETE; NOT A RESULT; NO FORMAL AGGREGATION OF THE COMPLETED PART"
)
BOOTSTRAP_SEED = 544
BOOTSTRAP_REPLICATES = 10_000
QUANTILES = {"P10": Fraction(1, 10), "P50": Fraction(1, 2), "P90": Fraction(9, 10)}
_SEAT_OUTCOMES = ("win", "deal_in", "draw", "other")


def _fail(message: str):
    raise RiichiFirstDamaError(f"riichi-first-dama-branch-v1: {message}")


def _ratio(value: Fraction) -> dict[str, int]:
    return {"denominator": value.denominator, "numerator": value.numerator}


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


LAYOUT = RecordLayout(
    protocol_id=PROTOCOL_ID,
    kyoku_record_schema=KYOKU_RECORD_SCHEMA,
    event_fact_schema="arena-riichi-first-dama-branch-v1-event-fact-v1",
    manifest_schema="arena-riichi-first-dama-branch-v1-record-manifest-v1",
    key_fields=("placement",),
    validate_keys=_validate_keys,
    validate_seats=_validate_seats,
)

# ---------------------------------------------------------------------------
# Rの局記録・行動記録
# ---------------------------------------------------------------------------

_HANCHAN_FIELDS = frozenset(
    {
        "action_sequence_sha256",
        "completed_match_sha256",
        "decisions",
        "end_reason",
        "final_points",
        "kyoku_count",
        "kyoku_record",
        "placement",
        "policy_call_counts",
        "riichi_declined",
        "seed",
        "selector_call_counts",
    }
)


def hanchan_row(item: ExecutedHanchan, *, observed: bool) -> dict:
    capture = item.capture
    analysis = item.policy_analysis
    record = project_row(
        capture.match,
        seed=capture.seed,
        keys={"placement": capture.placement},
        seats=capture.seats,
        selector_call_counts=capture.selector_call_counts,
        policy_call_counts=capture.policy_call_counts,
        layout=LAYOUT,
    )
    return {
        **_hanchan_document(item),
        "action_sequence_sha256": analysis["action_sequence_sha256"],
        "decisions": analysis["decisions"],
        "kyoku_record": record,
        "riichi_declined": analysis["riichi_declined"] if observed else None,
    }


def hanchans_document(
    plan: dict, executed: list[ExecutedHanchan], *, observed: bool
) -> dict:
    return {
        "hanchans": [hanchan_row(item, observed=observed) for item in executed],
        "observation": OBSERVED if observed else UNOBSERVED,
        "plan_digest": plan["plan_digest"],
        "schema": HANCHANS_SCHEMA,
    }


def require_hanchans(plan: dict, hanchans: object) -> None:
    """Rの客観recordが計画したscheduleと完全一致すること（不足・重複・順序の入替を拒否）。"""
    expected = [(item["seed"], item["placement"]) for item in plan_schedule(plan)]
    if (
        type(hanchans) is not dict
        or set(hanchans) != {"hanchans", "observation", "plan_digest", "schema"}
        or hanchans["schema"] != HANCHANS_SCHEMA
        or hanchans["plan_digest"] != plan["plan_digest"]
        or hanchans["observation"] != plan["observation"]
        or type(hanchans["hanchans"]) is not list
        or any(type(row) is not dict for row in hanchans["hanchans"])
        or [(row.get("seed"), row.get("placement")) for row in hanchans["hanchans"]]
        != expected
    ):
        _fail("hanchans do not cover the planned schedule")
    observed = plan["observation"] == OBSERVED
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
            or row["decisions"] != sum(counts)
        ):
            _fail(f"{context}: call counts are invalid")
        digests = row["action_sequence_sha256"]
        if (
            type(digests) is not list
            or len(digests) != 4
            or any(type(item) is not str or len(item) != 64 for item in digests)
        ):
            _fail(f"{context}: action sequence digests are invalid")
        declined = row["riichi_declined"]
        if observed:
            if (
                type(declined) is not list
                or len(declined) != 4
                or not all(_nonnegative_int(item) for item in declined)
            ):
                _fail(f"{context}: riichi_declined is invalid")
        elif declined is not None:
            _fail(f"{context}: an unobserved R has no riichi counts")
        try:
            record = validate_hanchan_row(row["kyoku_record"], LAYOUT)
        except KyokuRecordError as error:
            raise RiichiFirstDamaError(f"{context}: kyoku record: {error}") from error
        if (
            record["seed"] != row["seed"]
            or record["placement"] != row["placement"]
            or record["policy_call_counts"] != counts
            or len(record["kyokus"]) != row["kyoku_count"]
        ):
            _fail(f"{context}: kyoku record differs from the hanchan row")


def _header(plan: dict, schema: str) -> dict:
    return {
        "features_version": FEATURES_VERSION,
        "kind": "header",
        "plan_digest": plan["plan_digest"],
        "protocol_version": PROTOCOL_VERSION,
        "schema": schema,
        "wrapper_version": WRAPPER_VERSION,
    }


def action_lines(plan: dict, executed_r: list[ExecutedHanchan]) -> list[dict]:
    lines = [_header(plan, ACTIONS_SCHEMA)]
    for item in executed_r:
        lines.append(
            {
                "decisions": item.policy_analysis["actions"],
                "kind": "hanchan",
                "seed": item.capture.seed,
            }
        )
    return lines


def _entries_digest(decisions: list[list], start: int, stop: int) -> str:
    """worker ``ActionLog.digest`` と同じ ``[start, stop)`` の (seat, 行動) 列のdigest。"""
    return canonical_digest([[entry[1], entry[2]] for entry in decisions[start:stop]])


def require_action_lines(plan: dict, lines: object, hanchans: dict) -> None:
    """行動記録の形・判断順・局・席・合法手の位置と、局記録・Action列digestとの整合。"""
    require_hanchans(plan, hanchans)
    if type(lines) is not list or not lines:
        _fail("actions are empty")
    if lines[0] != _header(plan, ACTIONS_SCHEMA):
        _fail("actions header does not belong to the plan")
    rows = hanchans["hanchans"]
    if len(lines) - 1 != len(rows):
        _fail("actions and hanchans differ in count (one-to-one)")
    for line, row in zip(lines[1:], rows, strict=True):
        context = f"actions of seed {row['seed']}"
        if (
            type(line) is not dict
            or set(line) != {"decisions", "kind", "seed"}
            or line["kind"] != "hanchan"
            or line["seed"] != row["seed"]
        ):
            _fail(f"{context}: fields or seed do not match")
        decisions = line["decisions"]
        if type(decisions) is not list or len(decisions) != row["decisions"]:
            _fail(f"{context}: the number of decisions differs from the hanchan")
        per_seat: list[list] = [[], [], [], []]
        previous = 0
        for index, entry in enumerate(decisions):
            if type(entry) is not list or len(entry) != 6:
                _fail(f"{context}: decision {index} has another shape")
            ordinal, seat, value, position, count, digest = entry
            if (
                not _nonnegative_int(ordinal)
                or ordinal < previous
                or ordinal >= row["kyoku_count"]
                or type(seat) is not int
                or not 0 <= seat < SEAT_COUNT
                or type(value) is not dict
                or not _nonnegative_int(position)
                or type(count) is not int
                or not 0 <= position < count
                or type(digest) is not str
                or len(digest) != 64
            ):
                _fail(f"{context}: decision {index} is out of order or invalid")
            previous = ordinal
            try:
                per_seat[seat].append(decode_action(value))
            except Exception as error:
                raise RiichiFirstDamaError(
                    f"{context}: decision {index} action cannot be decoded: "
                    f"{type(error).__name__}"
                ) from error
        if [len(items) for items in per_seat] != row["policy_call_counts"]:
            _fail(f"{context}: per-seat decision counts differ from the hanchan")
        if [action_sequence_digest(items) for items in per_seat] != row[
            "action_sequence_sha256"
        ]:
            _fail(f"{context}: per-seat action digests differ from the hanchan")


# ---------------------------------------------------------------------------
# 分岐の記録
# ---------------------------------------------------------------------------


def branch_lines(
    plan: dict,
    hanchans: dict,
    branches: list[tuple[int, dict]],
    d_records: dict[int, dict],
    repeat_records: dict[int, dict] | None,
) -> list[dict]:
    rows = {row["seed"]: row for row in hanchans["hanchans"]}
    lines = [_header(plan, BRANCHES_SCHEMA)]
    for ordinal, (seed, record) in enumerate(branches):
        lines.append(
            {
                "branch_id": branch_id(seed, record["round_ordinal"], record["seat"]),
                "branch_ordinal": ordinal,
                "context": record["context"],
                "context_sha256": record["context_sha256"],
                "d": d_records.get(ordinal),
                "d_repeat": None
                if repeat_records is None
                else repeat_records.get(ordinal),
                "decision_index": record["decision_index"],
                "declaration": record["declaration"],
                "declaration_decision_index": record["declaration_decision_index"],
                "kind": "branch",
                "prefix_sha256": record["prefix_sha256"],
                "r_invalid": record["invalid"],
                "r_post_sha256": record["post_sha256"],
                "r_settlement": r_settlement(
                    rows[seed]["kyoku_record"], record["round_ordinal"]
                ),
                "riichi": record["riichi"],
                "round": record["round"],
                "round_ordinal": record["round_ordinal"],
                "seat": record["seat"],
                "seed": seed,
            }
        )
    return lines


def feature_lines(plan: dict, branches: list[tuple[int, dict]]) -> list[dict]:
    lines = [_header(plan, FEATURES_SCHEMA)]
    for ordinal, (seed, record) in enumerate(branches):
        lines.append(
            {
                "branch_id": branch_id(seed, record["round_ordinal"], record["seat"]),
                "branch_ordinal": ordinal,
                "error": record["features"]["error"],
                "features": record["features"]["features"],
                "kind": "features",
            }
        )
    return lines


_BRANCH_FIELDS = frozenset(
    {
        "branch_id",
        "branch_ordinal",
        "context",
        "context_sha256",
        "d",
        "d_repeat",
        "decision_index",
        "declaration",
        "declaration_decision_index",
        "kind",
        "prefix_sha256",
        "r_invalid",
        "r_post_sha256",
        "r_settlement",
        "riichi",
        "round",
        "round_ordinal",
        "seat",
        "seed",
    }
)
_D_FIELDS = frozenset(
    {
        "branch_action",
        "branch_context_sha256",
        "branch_decision_index",
        "branch_ordinal",
        "decisions",
        "first_riichi_after_branch",
        "invalid",
        "post_sha256",
        "prefix_sha256",
        "riichi_after_branch",
        "round",
        "round_ordinal",
        "seat",
        "seed",
        "settlement",
        "status",
        "wrapper_riichi_after_branch",
    }
)
_FEATURE_LINE_FIELDS = frozenset(
    {"branch_id", "branch_ordinal", "error", "features", "kind"}
)


def require_branch_lines(plan: dict, lines: object, hanchans: dict) -> None:
    """分岐行の形・順序・一意性・Rとの一対一を検査する（INVALIDの判定は別）。"""
    require_hanchans(plan, hanchans)
    if type(lines) is not list or not lines:
        _fail("branches are empty")
    if lines[0] != _header(plan, BRANCHES_SCHEMA):
        _fail("branches header does not belong to the plan")
    rows = {row["seed"]: row for row in hanchans["hanchans"]}
    seeds = [item["seed"] for item in plan_schedule(plan)]
    keys = []
    for ordinal, line in enumerate(lines[1:]):
        context = f"branch {ordinal}"
        if type(line) is not dict or set(line) != _BRANCH_FIELDS:
            _fail(f"{context}: branch fields do not match")
        if line["kind"] != "branch" or line["branch_ordinal"] != ordinal:
            _fail(f"{context}: branch ordinal is not sequential")
        if line["seed"] not in rows:
            _fail(f"{context}: the seed is not in the schedule")
        if type(line["seat"]) is not int or not 0 <= line["seat"] < 4:
            _fail(f"{context}: seat is invalid")
        row = rows[line["seed"]]
        if (
            not _nonnegative_int(line["decision_index"])
            or line["decision_index"] >= row["decisions"]
            or not _nonnegative_int(line["round_ordinal"])
            or line["round_ordinal"] >= row["kyoku_count"]
        ):
            _fail(f"{context}: decision index or round is outside the hanchan")
        if line["branch_id"] != branch_id(
            line["seed"], line["round_ordinal"], line["seat"]
        ):
            _fail(f"{context}: branch id does not match")
        if type(line["r_invalid"]) is not list or any(
            type(item) is not str for item in line["r_invalid"]
        ):
            _fail(f"{context}: r_invalid must be a list of strings")
        for name in ("d", "d_repeat"):
            record = line[name]
            if record is None:
                continue
            if type(record) is not dict or set(record) != _D_FIELDS:
                _fail(f"{context}: {name} fields do not match")
            if (
                record["branch_ordinal"] != ordinal
                or record["seed"] != line["seed"]
                or record["seat"] != line["seat"]
            ):
                _fail(f"{context}: {name} belongs to another branch")
        if line["d"] is None and line["declaration"] is not None:
            _fail(f"{context}: a branch with its declaration has no D arm")
        if line["d"] is not None and line["declaration"] is None:
            _fail(f"{context}: a D arm without the declaration")
        repeat_expected = plan["run_kind"] == REHEARSAL
        if (line["d_repeat"] is not None) is not (
            repeat_expected and line["d"] is not None
        ):
            _fail(f"{context}: the D arm repeat does not match the run kind")
        keys.append((seeds.index(line["seed"]), line["decision_index"]))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        _fail("branches are not in schedule and decision order or are duplicated")
    per_round = [
        (line["seed"], line["round_ordinal"], line["seat"]) for line in lines[1:]
    ]
    if len(per_round) != len(set(per_round)):
        _fail("a seat has two branch points in one round")


def require_branches_match_actions(branches: list[dict], actions: list[dict]) -> None:
    """分岐点・宣言牌の位置と、Rの行動記録から再計算したdigestが分岐行と一致すること。"""
    by_seed = {line["seed"]: line["decisions"] for line in actions[1:]}
    for line in branches[1:]:
        decisions = by_seed[line["seed"]]
        index = line["decision_index"]
        entry = decisions[index]
        if (
            entry[0] != line["round_ordinal"]
            or entry[1] != line["seat"]
            or entry[2] != line["riichi"]
        ):
            _fail(f"branch {line['branch_id']}: the R riichi is not in the actions")
        if line["prefix_sha256"] != _entries_digest(decisions, 0, index):
            _fail(f"branch {line['branch_id']}: the R prefix differs from the actions")
        if line["declaration"] is not None:
            following = line["declaration_decision_index"]
            if (
                following != index + 1
                or following >= len(decisions)
                or decisions[following][2] != (line["declaration"])
            ):
                _fail(
                    f"branch {line['branch_id']}: the R declaration is not in the actions"
                )
        stop = max(
            (
                i + 1
                for i, item in enumerate(decisions)
                if item[0] == line["round_ordinal"]
            ),
            default=0,
        )
        if line["r_post_sha256"] is not None and line["r_post_sha256"] != (
            _entries_digest(decisions, index, stop)
        ):
            _fail(f"branch {line['branch_id']}: the R post digest differs")


def require_feature_lines(plan: dict, lines: object, branches: list[dict]) -> None:
    if type(lines) is not list or not lines:
        _fail("features are empty")
    if lines[0] != _header(plan, FEATURES_SCHEMA):
        _fail("features header does not belong to the plan")
    if len(lines) != len(branches):
        _fail("features and branches differ in count (one-to-one)")
    for line, branch in zip(lines[1:], branches[1:], strict=True):
        if type(line) is not dict or set(line) != _FEATURE_LINE_FIELDS:
            _fail("feature fields do not match")
        if (
            line["kind"] != "features"
            or line["branch_ordinal"] != branch["branch_ordinal"]
            or line["branch_id"] != branch["branch_id"]
        ):
            _fail("features and branches are not one-to-one")
        if (line["error"] is None) is (line["features"] is None):
            _fail("a feature line holds exactly one of error and features")


# ---------------------------------------------------------------------------
# 1分岐の照合・局収支・Δ
# ---------------------------------------------------------------------------


def _first_riichi_shape(value: object) -> bool:
    return value is None or (
        type(value) is dict
        and set(value) == {"decision_index", "seat_decisions_after_branch"}
        and _nonnegative_int(value["decision_index"])
        and type(value["seat_decisions_after_branch"]) is int
        and value["seat_decisions_after_branch"] >= 1
    )


def evaluate_branch(line: dict, feature: dict, row: dict) -> dict:
    """1分岐のINVALID理由・局収支・Δ・記述値を返す。"""
    seat = line["seat"]
    invalid = [f"R: {reason}" for reason in line["r_invalid"]]
    result = {
        "branch_id": line["branch_id"],
        "delta": None,
        "final_riichi_stick_awards": None,
        "first_riichi_after_branch": None,
        "g_d": None,
        "g_r": None,
        "invalid": invalid,
        "outcomes": None,
        "riichi_after_branch": None,
        "seed": line["seed"],
    }
    expected_r = r_settlement(row["kyoku_record"], line["round_ordinal"])
    if line["r_settlement"] != expected_r:
        invalid.append("the R settlement differs from the kyoku record")
    if expected_r["round"] != line["round"]:
        invalid.append("the branch context belongs to another round than the record")
    if line["declaration"] is None or line["context"] is None:
        invalid.append("the declaration or the context is missing")
    if feature["error"] is not None:
        invalid.append(f"lisjong rejects the features: {feature['error']}")
    record = line["d"]
    if record is None:
        invalid.append("the branch has no D arm")
        return result
    invalid.extend(f"D: {reason}" for reason in record["invalid"])
    if record["status"] != "branched":
        invalid.append(f"the D arm wrapper status is {record['status']}")
    if record["branch_decision_index"] != line["decision_index"]:
        invalid.append("the D branch point is at another decision index")
    if record["prefix_sha256"] != line["prefix_sha256"]:
        invalid.append("the actions before the branch point differ from R")
    if record["branch_context_sha256"] != line["context_sha256"]:
        invalid.append("the D branch context differs from R")
    if record["branch_action"] != line["declaration"]:
        # 分岐点で宣言牌をリーチせずに打っていない（分岐点のリーチの適用を含む）
        invalid.append("the D branch point did not discard the declaration tile")
    if (
        not _nonnegative_int(record["riichi_after_branch"])
        or record["riichi_after_branch"] != record["wrapper_riichi_after_branch"]
        or not _first_riichi_shape(record["first_riichi_after_branch"])
        or (record["first_riichi_after_branch"] is None)
        is not (record["riichi_after_branch"] == 0)
    ):
        invalid.append(
            "the riichi after the branch differs between the wrapper and the actions"
        )
    elif record["first_riichi_after_branch"] is not None and not (
        record["first_riichi_after_branch"]["decision_index"]
        > (record["branch_decision_index"] or 0)
    ):
        invalid.append("the riichi after the branch is not after the branch point")
    if (
        record["round"] != line["round"]
        or record["round_ordinal"] != (line["round_ordinal"])
    ):
        invalid.append("the D arm settled another round")
    if line["d_repeat"] is not None and line["d_repeat"] != record:
        invalid.append("the D arm repeat differs")
    result["riichi_after_branch"] = record["riichi_after_branch"]
    result["first_riichi_after_branch"] = record["first_riichi_after_branch"]
    try:
        settled = d_settlement(record)
    except EngineAabbProtocolError as error:
        invalid.append(str(error))
        return result
    if settled is None:
        invalid.append("the D arm has no settlement")
        return result
    if settled["points_before"] != expected_r["points_before"]:
        invalid.append("the D round does not start from the R scores")
    g_r, g_d = expected_r["point_deltas"][seat], settled["point_deltas"][seat]
    if g_r % 100 or g_d % 100:
        invalid.append("a round result is not a multiple of 100 points")
        return result
    result["g_r"], result["g_d"] = g_r // 100, g_d // 100
    result["delta"] = result["g_d"] - result["g_r"]
    result["outcomes"] = {
        ARM_R: _seat_outcome(expected_r, seat),
        ARM_D: _seat_outcome(settled, seat),
    }
    result["final_riichi_stick_awards"] = {
        ARM_R: None
        if expected_r["final_riichi_stick_awards"] is None
        else expected_r["final_riichi_stick_awards"][seat],
        ARM_D: None
        if settled["final_riichi_stick_awards"] is None
        else settled["final_riichi_stick_awards"][seat],
    }
    return result


# ---------------------------------------------------------------------------
# 分散・σ_u・bootstrap（整数の和からFractionで厳密に）
# ---------------------------------------------------------------------------


def _cluster(rows: list[dict]) -> tuple[int, ...]:
    """(n, ΣR, ΣD, ΣR², ΣD², ΣRD) の和（Δ = D − R）。"""
    return (
        len(rows),
        sum(row["g_r"] for row in rows),
        sum(row["g_d"] for row in rows),
        sum(row["g_r"] ** 2 for row in rows),
        sum(row["g_d"] ** 2 for row in rows),
        sum(row["g_r"] * row["g_d"] for row in rows),
    )


def _moments(total: tuple[int, ...]) -> dict | None:
    n, sr, sd, srr, sdd, srd = total
    if n < 2:
        return None
    var_r = Fraction(srr * n - sr * sr, n * (n - 1))
    var_d = Fraction(sdd * n - sd * sd, n * (n - 1))
    cov = Fraction(srd * n - sr * sd, n * (n - 1))
    var_delta = var_d + var_r - 2 * cov
    return {"cov": cov, "var_d": var_d, "var_delta": var_delta, "var_r": var_r}


def _pair_ratio(total: tuple[int, ...]) -> Fraction | None:
    moments = _moments(total)
    if moments is None or moments["var_r"] + moments["var_d"] == 0:
        return None
    return moments["var_delta"] / (moments["var_r"] + moments["var_d"])


def _sigma_u_squared(clusters: list[tuple[int, ...]]) -> Fraction | None:
    """σ_u² = Var_s(ΣΔ_s − μ n_s) / n̄²（seedの標本分散、n − 1）。"""
    m = len(clusters)
    if m < 2:
        return None
    sums = [item[2] - item[1] for item in clusters]
    counts = [item[0] for item in clusters]
    total = sum(counts)
    mu = Fraction(sum(sums), total)
    units = [s - mu * n for s, n in zip(sums, counts, strict=True)]
    mean = sum(units, Fraction(0)) / m
    variance = sum(((u - mean) ** 2 for u in units), Fraction(0)) / (m - 1)
    n_bar = Fraction(total, m)
    return variance / (n_bar * n_bar)


def _add(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(a + b for a, b in zip(left, right, strict=True))


def variance_bootstrap(clusters: list[tuple[int, ...]]) -> dict:
    """seed clusterの復元抽出（``random.Random(544)`` をsummaryごとに1回初期化）。"""
    m = len(clusters)
    if m < 2:
        return {"ratio": None, "sigma_u_squared": None}
    rng = random.Random(BOOTSTRAP_SEED)
    ratios: list[Fraction] = []
    sigmas: list[Fraction] = []
    defined = True
    for _ in range(BOOTSTRAP_REPLICATES):
        drawn = [clusters[rng.randrange(m)] for _ in range(m)]
        total = (0, 0, 0, 0, 0, 0)
        for item in drawn:
            total = _add(total, item)
        ratio = _pair_ratio(total)
        if ratio is None:
            defined = False
        else:
            ratios.append(ratio)
        sigmas.append(_sigma_u_squared(drawn))
    ratios.sort()
    sigmas.sort()
    return {
        "ratio": {
            name: _ratio(nearest_rank(ratios, q)) for name, q in QUANTILES.items()
        }
        if defined
        else None,
        "sigma_u_squared": {
            name: _ratio(nearest_rank(sigmas, q)) for name, q in QUANTILES.items()
        },
    }


def variance_summary(valid: list[dict]) -> dict:
    by_seed: dict[int, list[dict]] = {}
    for row in valid:
        by_seed.setdefault(row["seed"], []).append(row)
    clusters = [_cluster(by_seed[seed]) for seed in sorted(by_seed)]
    total = _cluster(valid)
    moments = _moments(total)
    ratio = _pair_ratio(total)
    sigma = _sigma_u_squared(clusters)
    correlation = None
    if moments is not None and moments["var_r"] > 0 and moments["var_d"] > 0:
        correlation = float(moments["cov"]) / math.sqrt(
            float(moments["var_r"]) * float(moments["var_d"])
        )
    return {
        "branch": None
        if moments is None
        else {name: _ratio(value) for name, value in sorted(moments.items())},
        "bootstrap": variance_bootstrap(clusters),
        "correlation": correlation,
        "count": len(valid),
        "ratio": None if ratio is None else _ratio(ratio),
        "seeds": len(clusters),
        "sigma_u": None if sigma is None else math.sqrt(sigma),
        "sigma_u_squared": None if sigma is None else _ratio(sigma),
    }


def cost_screening(variance: dict, timings: dict, seeds: int) -> dict:
    """収集費用の一次判定（学習・採用へ進む判定ではない）。"""
    rows = timings["tasks"]
    r_walls = [row["wall_seconds"] for row in rows if row["arm"] == ARM_R]
    d_walls = [row["wall_seconds"] for row in rows if row["arm"] == ARM_D]
    branches_per_seed = Fraction(len(d_walls), seeds)
    cost_per_seed = sum(r_walls) / len(r_walls) + (
        float(branches_per_seed) * (sum(d_walls) / len(d_walls)) if d_walls else 0.0
    )
    normal = statistics.NormalDist()
    z = normal.inv_cdf(1 - float(ALPHA)) + normal.inv_cdf(float(POWER))
    bootstrap = variance["bootstrap"]["sigma_u_squared"]
    base = {
        "branches_per_seed": _ratio(branches_per_seed),
        "budget_seconds": COLLECTION_BUDGET_SECONDS,
        "cost_per_seed_seconds": cost_per_seed,
        "not_a_learning_or_adoption_decision": True,
        "target_effect_100_points": TARGET_EFFECT,
        "z_sum": z,
    }
    if bootstrap is None:
        return {
            **base,
            "required_seconds": None,
            "seeds_needed": None,
            "sigma_u_p90": None,
            "verdict": COST_NOT_ESTIMABLE,
        }
    p90 = Fraction(bootstrap["P90"]["numerator"], bootstrap["P90"]["denominator"])
    needed = math.ceil(z * z * float(p90) / (TARGET_EFFECT * TARGET_EFFECT))
    required = (needed + COST_VERIFY_SEEDS) * cost_per_seed + RECOVERY_ALLOWANCE_SECONDS
    return {
        **base,
        "required_seconds": required,
        "seeds_needed": needed,
        "sigma_u_p90": math.sqrt(p90),
        "verdict": COST_WITHIN_BUDGET
        if required <= COLLECTION_BUDGET_SECONDS
        else COST_OVER_BUDGET,
    }


def resumed_riichi_summary(valid: list[dict]) -> dict:
    """分岐後に再びリーチした割合と、それまでの分岐席の判断数（記述のみ）。"""
    resumed = [row for row in valid if row["first_riichi_after_branch"] is not None]
    turns = [
        row["first_riichi_after_branch"]["seat_decisions_after_branch"]
        for row in resumed
    ]
    return {
        "branches": len(valid),
        "resumed": len(resumed),
        "seat_decisions_until_resumed": [
            [value, turns.count(value)] for value in sorted(set(turns))
        ],
        "total_riichi_after_branch": sum(row["riichi_after_branch"] for row in valid),
        "wins_without_resumed_riichi": sum(
            1
            for row in valid
            if row["first_riichi_after_branch"] is None
            and row["outcomes"][ARM_D] == "win"
        ),
    }


# ---------------------------------------------------------------------------
# timings
# ---------------------------------------------------------------------------

_TIMING_ROW_FIELDS = frozenset(
    {
        "arm",
        "branch_ordinal",
        "parent_peak_rss_kib",
        "seed",
        "wall_seconds",
        "worker_peak_rss_kib",
    }
)
_TIMING_FIELDS = frozenset(
    {
        "finished_at_utc",
        "monotonic_elapsed_seconds",
        "not_rederived",
        "plan_digest",
        "realtime_elapsed_seconds",
        "schedule_wall_seconds",
        "schema",
        "started_at_utc",
        "summary_seconds",
        "tasks",
        "write_seconds",
    }
)


def require_timings(
    plan: dict, timings: object, tasks: list[tuple], *, complete: bool = True
) -> dict:
    """timingsの形・task順と、実行時と同じ上限（RSS・wall）を再確認する。"""
    limits = plan["resource_limits"]
    if type(timings) is not dict or timings.get("plan_digest") != plan["plan_digest"]:
        _fail("timings do not belong to the plan")
    rows = timings.get("tasks")
    if (
        type(rows) is not list
        or any(type(row) is not dict or set(row) != _TIMING_ROW_FIELDS for row in rows)
        or [(row["arm"], row["seed"], row["branch_ordinal"]) for row in rows] != tasks
    ):
        _fail("timings do not cover the executed tasks in order")
    for index, row in enumerate(rows):
        _seconds(row["wall_seconds"], f"tasks[{index}].wall_seconds")
        known = [
            value
            for value in (
                _rss(row["parent_peak_rss_kib"], f"tasks[{index}].parent"),
                _rss(row["worker_peak_rss_kib"], f"tasks[{index}].worker"),
            )
            if value is not None
        ]
        if sum(known) * 1024 > limits["process_tree_rss_bytes"]:
            _fail("parent + worker peak RSS exceeded the process tree limit")
    schedule = _seconds(timings.get("schedule_wall_seconds"), "schedule_wall_seconds")
    if schedule > limits["wall_seconds"]:
        _fail("the schedule exceeded the wall limit")
    if complete:
        if set(timings) != _TIMING_FIELDS or timings["schema"] != TIMINGS_SCHEMA:
            _fail("timings fields do not match")
        elapsed = max(
            _seconds(timings["monotonic_elapsed_seconds"], "monotonic"),
            _seconds(timings["realtime_elapsed_seconds"], "realtime"),
        )
        if elapsed > limits["wall_seconds"]:
            _fail("the run exceeded the wall limit")
        if timings["not_rederived"] is not True:
            _fail("timings are measurements and are not re-derived")
    return timings


# ---------------------------------------------------------------------------
# summary（保存物だけから再導出できる値）
# ---------------------------------------------------------------------------


def build_summary(
    plan: dict,
    hanchans: dict,
    actions: list[dict],
    branches: list[dict],
    features: list[dict],
    timings: dict,
) -> dict:
    """summaryは保存物（plan・hanchans・actions・branches・features・timingsのtask行）だけから決まる。"""
    require_action_lines(plan, actions, hanchans)
    require_branch_lines(plan, branches, hanchans)
    require_branches_match_actions(branches, actions)
    require_feature_lines(plan, features, branches)
    require_timings(plan, timings, expected_tasks(plan, branches), complete=False)
    rows_by_seed = {row["seed"]: row for row in hanchans["hanchans"]}
    evaluated = [
        evaluate_branch(line, feature, rows_by_seed[line["seed"]])
        for line, feature in zip(branches[1:], features[1:], strict=True)
    ]
    invalid = [row for row in evaluated if row["invalid"]]
    valid = [row for row in evaluated if not row["invalid"]]
    outcome_counts = {
        arm: {name: 0 for name in _SEAT_OUTCOMES} for arm in (ARM_R, ARM_D)
    }
    for row in valid:
        for arm in (ARM_R, ARM_D):
            outcome_counts[arm][row["outcomes"][arm]] += 1
    allocated = plan["run_kind"] == ALLOCATED
    decision = variance = cost = None
    status = REHEARSAL_STATUS
    if allocated:
        status = COMPLETE_STATUS
        decision = STOP_INVALID if invalid else PILOT_COMPLETE
        variance = variance_summary(valid)
        if decision == PILOT_COMPLETE:
            cost = cost_screening(variance, timings, len(hanchans["hanchans"]))
    payload = {
        "branches": len(evaluated),
        "cost_screening": cost,
        "decision": decision,
        "decisions": sum(row["decisions"] for row in hanchans["hanchans"]),
        "descriptive": {
            "delta": delta_stats(valid, with_bootstrap=False),
            "not_used_for_any_decision": True,
            "seat_outcomes": outcome_counts,
        },
        "evaluation_kind": EVALUATION_KIND,
        "hanchan_count": len(hanchans["hanchans"]),
        "inputs": {
            "actions_sha256": sha256_bytes(jsonl_text(actions).encode("utf-8")),
            "branches_sha256": sha256_bytes(jsonl_text(branches).encode("utf-8")),
            "features_sha256": sha256_bytes(jsonl_text(features).encode("utf-8")),
            "hanchans_sha256": document_sha256(hanchans),
        },
        "invalid": {
            "branches": [
                {"branch_id": row["branch_id"], "reasons": row["invalid"]}
                for row in invalid
            ],
            "count": len(invalid),
        },
        "kyoku_count": sum(row["kyoku_count"] for row in hanchans["hanchans"]),
        "memory": memory_summary(timings["tasks"]),
        "not_a_strength_result": True,
        "observation": plan["observation"],
        "plan_digest": plan["plan_digest"],
        "protocol_version": PROTOCOL_VERSION,
        "quality": {
            "resumed_riichi": resumed_riichi_summary(valid),
            "riichi_declined": sum(
                sum(row["riichi_declined"]) for row in hanchans["hanchans"]
            ),
        },
        "rows": [dict(row) for row in evaluated],
        "run_kind": plan["run_kind"],
        "schema": SUMMARY_SCHEMA,
        "seat_hanchans": 4 * len(hanchans["hanchans"]),
        "status": status,
        "timing": _timing_summary(timings),
        "variance": variance,
        "wrapper_version": WRAPPER_VERSION,
    }
    return {**payload, "summary_digest": document_identity(payload)}


def build_unobserved_summary(plan: dict, hanchans: dict, timings: dict) -> dict:
    require_hanchans(plan, hanchans)
    require_timings(plan, timings, expected_tasks(plan, None), complete=False)
    payload = {
        "decision": None,
        "evaluation_kind": EVALUATION_KIND,
        "hanchan_count": len(hanchans["hanchans"]),
        "inputs": {"hanchans_sha256": document_sha256(hanchans)},
        "kyoku_count": sum(row["kyoku_count"] for row in hanchans["hanchans"]),
        "memory": memory_summary(timings["tasks"]),
        "not_a_strength_result": True,
        "observation": UNOBSERVED,
        "plan_digest": plan["plan_digest"],
        "protocol_version": PROTOCOL_VERSION,
        "run_kind": plan["run_kind"],
        "schema": SUMMARY_SCHEMA,
        "status": REHEARSAL_STATUS,
        "timing": _timing_summary(timings),
    }
    return {**payload, "summary_digest": document_identity(payload)}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def write_stop(
    diagnostics: Path,
    plan: dict,
    error: BaseException,
    rows: list[dict],
    *,
    started_at: str,
    elapsed_seconds: float,
) -> Path:
    """未完了の実行状況をwrite-onceで残す（成功分を使った正式な集計・判定はしない）。"""
    diagnostics.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    document = {
        "completed_tasks": len(rows),
        "decision": STOP_INCOMPLETE,
        "elapsed_seconds": elapsed_seconds,
        "error": f"{type(error).__name__}: {error}",
        "finished_at_utc": utc_now(),
        "not_a_result": True,
        "plan_digest": plan["plan_digest"],
        "schema": STOP_SCHEMA,
        "stage": plan["stage"],
        "started_at_utc": started_at,
        "status": STOP_STATUS,
        "tasks": rows,
    }
    path = diagnostics / f"stop-{stamp}.json"
    write_new_artifact_file(path, canonical_json_text(document))
    return path


def run_plan(
    plan_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play_r: Callable | None = None,
    play_d: Callable = play_d_arm,
    live_gate: Callable[..., None] = require_live_target,
    on_task: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] | None = None,
    realtime: Callable[[], float] | None = None,
) -> dict[str, object]:
    """実行して公開する。未完了はSTOP-INCOMPLETEで、outputを作らずstop記録を残す。

    ``play_r`` / ``play_d`` / ``live_gate`` / clockはtestのseam。
    """
    plan = load_plan(plan_path)
    observed = plan["observation"] == OBSERVED
    if play_r is None:
        play_r = play_r_observed if observed else play_r_unobserved
    default_clock, default_realtime = default_clocks()
    clock = clock or default_clock
    realtime = realtime or default_realtime
    output = Path(plan["artifact_destinations"]["output"])
    diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
    if output.exists():
        _fail("output already exists; outputs are write-once and never re-run")
    limits = plan["resource_limits"]
    rows: list[dict] = []
    started_at = utc_now()
    started = clock()
    started_real = realtime()

    def elapsed() -> float:
        return max(clock() - started, realtime() - started_real)

    def measure(arm: str):
        def record(item: ExecutedHanchan) -> None:
            parent_kib = current_peak_rss_kib()
            rows.append(
                {
                    "arm": arm,
                    "branch_ordinal": getattr(item.capture, "branch_ordinal", None),
                    "parent_peak_rss_kib": parent_kib,
                    "seed": item.capture.seed,
                    "wall_seconds": item.wall_seconds,
                    "worker_peak_rss_kib": item.peak_rss_kib,
                }
            )
            if elapsed() > limits["wall_seconds"]:
                raise RunStopped(
                    f"the run exceeded the wall limit of {limits['wall_seconds']} s "
                    f"after {len(rows)} tasks"
                )
            known = [
                value for value in (parent_kib, item.peak_rss_kib) if value is not None
            ]
            if sum(known) * 1024 > limits["process_tree_rss_bytes"]:
                raise RunStopped(
                    "parent + worker peak RSS exceeded the process tree limit of "
                    f"{limits['process_tree_rss_bytes']} bytes"
                )

        return record

    def ledger():
        if plan["run_kind"] != ALLOCATED:
            return None
        return seed_registry.load_ledger(ledger_path)

    try:
        live_gate(plan, ledger=ledger())
        if observed:
            executed_r, branches, d_records, repeat_records = execute_branches(
                plan,
                play_r=play_r,
                play_d=play_d,
                measure=measure,
                on_task=on_task,
                repeat=plan["run_kind"] == REHEARSAL,
            )
        else:
            executed_r = _execute(
                plan,
                _r_tasks(plan),
                key_fields=("placement",),
                play=play_r,
                on_executed=measure(ARM_R),
                on_task=on_task,
            )
        schedule_seconds = clock() - started
        live_gate(plan, ledger=ledger())
        with staged_artifact_directory(output) as staging:
            timings = {
                "not_rederived": True,
                "plan_digest": plan["plan_digest"],
                "schedule_wall_seconds": schedule_seconds,
                "schema": TIMINGS_SCHEMA,
                "started_at_utc": started_at,
                "tasks": rows,
            }
            write_started = clock()
            hanchans = hanchans_document(plan, executed_r, observed=observed)
            write_new_artifact_file(
                staging / HANCHANS_FILENAME, canonical_json_text(hanchans)
            )
            if observed:
                actions = action_lines(plan, executed_r)
                lines = branch_lines(
                    plan, hanchans, branches, d_records, repeat_records
                )
                features = feature_lines(plan, branches)
                write_new_artifact_file(staging / ACTIONS_FILENAME, jsonl_text(actions))
                write_new_artifact_file(staging / BRANCHES_FILENAME, jsonl_text(lines))
                write_new_artifact_file(
                    staging / FEATURES_FILENAME, jsonl_text(features)
                )
            timings["write_seconds"] = clock() - write_started
            summary_started = clock()
            if observed:
                summary = build_summary(
                    plan, hanchans, actions, lines, features, timings
                )
            else:
                summary = build_unobserved_summary(plan, hanchans, timings)
            write_new_artifact_file(
                staging / SUMMARY_FILENAME, canonical_json_text(summary)
            )
            timings["summary_seconds"] = clock() - summary_started
            timings["finished_at_utc"] = utc_now()
            timings["monotonic_elapsed_seconds"] = clock() - started
            timings["realtime_elapsed_seconds"] = realtime() - started_real
            write_new_artifact_file(
                staging / TIMINGS_FILENAME, canonical_json_text(timings)
            )
            write_new_artifact_file(staging / SUMS_FILENAME, _sums_text(staging))
            if elapsed() > limits["wall_seconds"]:
                raise RunStopped("the run exceeded the wall limit while saving")
    except BaseException as error:
        write_stop(
            diagnostics,
            plan,
            error,
            rows,
            started_at=started_at,
            elapsed_seconds=elapsed(),
        )
        if not isinstance(error, Exception) or isinstance(
            error, (RunStopped, EngineAabbProtocolError)
        ):
            raise
        raise RunStopped(f"STOP-INCOMPLETE: {type(error).__name__}: {error}") from error
    return summary


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

_R_BRANCH_KEYS = (
    ("context", "context"),
    ("context_sha256", "context_sha256"),
    ("decision_index", "decision_index"),
    ("declaration", "declaration"),
    ("declaration_decision_index", "declaration_decision_index"),
    ("prefix_sha256", "prefix_sha256"),
    ("r_invalid", "invalid"),
    ("r_post_sha256", "post_sha256"),
    ("riichi", "riichi"),
    ("round", "round"),
    ("round_ordinal", "round_ordinal"),
    ("seat", "seat"),
)


def replay_seeds_of(
    plan: dict,
    hanchans: dict,
    actions: list[dict],
    branches: list[dict],
    features: list[dict],
    *,
    play_r: Callable,
    play_d: Callable,
) -> dict:
    """固定2 seedのRと全D armを再実行し、行動記録を含めて記録との完全一致を要求する。"""
    seeds = plan["verify_replay_seeds"]
    if not seeds:
        return {"arms": 0, "seeds": []}
    rows = {row["seed"]: row for row in hanchans["hanchans"]}
    recorded_actions = {line["seed"]: line["decisions"] for line in actions[1:]}
    features_by_ordinal = {line["branch_ordinal"]: line for line in features[1:]}
    stored: dict[int, list[dict]] = {}
    for line in branches[1:]:
        stored.setdefault(line["seed"], []).append(line)
    champion = plan_champion(plan).to_document()
    tasks = [
        HanchanTask(
            seed=seed,
            placement=0,
            candidate=champion,
            incumbent=champion,
            wheel_path=plan["execution"]["wheel_path"],
        )
        for seed in seeds
    ]
    executed = _execute(
        plan,
        tasks,
        key_fields=("placement",),
        play=play_r,
        on_executed=lambda item: None,
        on_task=None,
    )
    arms = 0
    for seed, item in zip(seeds, executed, strict=True):
        if hanchan_row(item, observed=True) != rows[seed]:
            _fail(f"seed {seed}: the replayed R differs from the record (STOP-INVALID)")
        if item.policy_analysis["actions"] != recorded_actions[seed]:
            _fail(f"seed {seed}: the replayed R actions differ (STOP-INVALID)")
        records = sorted(
            item.policy_analysis["branches"],
            key=lambda record: record["decision_index"],
        )
        lines = stored.get(seed, [])
        if len(records) != len(lines):
            _fail(f"seed {seed}: the replayed R has another number of branches")
        d_tasks = []
        for record, line in zip(records, lines, strict=True):
            if any(line[name] != record[key] for name, key in _R_BRANCH_KEYS):
                _fail(f"branch {line['branch_id']}: the replayed R branch differs")
            feature = features_by_ordinal[line["branch_ordinal"]]
            if record["features"] != {
                "error": feature["error"],
                "features": feature["features"],
            }:
                _fail(f"branch {line['branch_id']}: the replayed features differ")
            if record["declaration"] is not None:
                d_tasks.append(
                    (
                        line,
                        BranchTask(
                            seed=seed,
                            branch_ordinal=line["branch_ordinal"],
                            branch=task_branch(record),
                            incumbent=champion,
                            wheel_path=plan["execution"]["wheel_path"],
                        ),
                    )
                )
        if d_tasks:
            replayed = _execute(
                plan,
                [task for _, task in d_tasks],
                key_fields=("branch_ordinal",),
                play=play_d,
                on_executed=lambda item: None,
                on_task=None,
            )
            for (line, _), arm in zip(d_tasks, replayed, strict=True):
                if arm.policy_analysis != line["d"]:
                    _fail(
                        f"branch {line['branch_id']}: the replayed D arm differs "
                        "(STOP-INVALID)"
                    )
                arms += 1
    return {"arms": arms, "seeds": list(seeds)}


def verified_plan(
    plan_path: str | Path,
    *,
    environment: dict | None = None,
    play_r: Callable = play_r_observed,
    play_d: Callable = play_d_arm,
) -> tuple[dict, dict, dict, list[dict], dict]:
    """保存物を検査・再導出し、``(plan, summary, timings, files, rederived)`` を返す。"""
    plan = load_plan(plan_path)
    if environment is None:
        try:
            environment = _live_environment(plan["execution"]["wheel_path"])
        except EngineAabbLockError as error:
            raise RiichiFirstDamaError(str(error)) from error
    output = Path(plan["artifact_destinations"]["output"])
    try:
        recorded_sums = (output / SUMS_FILENAME).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RiichiFirstDamaError(f"cannot read {SUMS_FILENAME}: {error}") from error
    if recorded_sums != _sums_text(output):
        _fail("output files differ from SHA256SUMS (missing, extra or changed)")
    observed = plan["observation"] == OBSERVED
    expected_files = {
        SUMMARY_FILENAME,
        HANCHANS_FILENAME,
        TIMINGS_FILENAME,
        SUMS_FILENAME,
    }
    if observed:
        expected_files |= {ACTIONS_FILENAME, BRANCHES_FILENAME, FEATURES_FILENAME}
    files = output_files(output)
    if {item["path"] for item in files} != expected_files:
        _fail("output files do not match the expected set")
    recorded = read_document(output / SUMMARY_FILENAME, "summary")
    hanchans = read_document(output / HANCHANS_FILENAME, "hanchans")
    timings = read_document(output / TIMINGS_FILENAME, "timings")
    rederived = {"features": 0, "replay": {"arms": 0, "seeds": []}}
    if observed:
        actions = read_jsonl(output / ACTIONS_FILENAME, "actions")
        branches = read_jsonl(output / BRANCHES_FILENAME, "branches")
        features = read_jsonl(output / FEATURES_FILENAME, "features")
        summary = build_summary(plan, hanchans, actions, branches, features, timings)
        require_analysis_environment(plan, environment)
        rederived["features"] = rederive_features(branches, features)
        tasks = expected_tasks(plan, branches)
    else:
        summary = build_unobserved_summary(plan, hanchans, timings)
        tasks = expected_tasks(plan, None)
    if recorded != summary:
        _fail("summary differs from the re-derivation")
    require_timings(plan, timings, tasks)
    if observed:
        rederived["replay"] = replay_seeds_of(
            plan, hanchans, actions, branches, features, play_r=play_r, play_d=play_d
        )
    return plan, summary, timings, files, rederived


def _write_once(output: str | Path | None, document: dict) -> None:
    if output is None:
        return
    destination = Path(output)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))


def verify_plan(
    plan_path: str | Path,
    *,
    output: str | Path | None = None,
    environment: dict | None = None,
    play_r: Callable = play_r_observed,
    play_d: Callable = play_d_arm,
) -> dict[str, object]:
    plan, summary, timings, files, rederived = verified_plan(
        plan_path, environment=environment, play_r=play_r, play_d=play_d
    )
    cost = summary.get("cost_screening")
    document = {
        "clocks": clock_document(timings),
        "cost_screening": None if cost is None else cost["verdict"],
        "decision": summary["decision"],
        "files": files,
        "observation": plan["observation"],
        "output_bytes": sum(item["bytes"] for item in files),
        "plan_digest": plan["plan_digest"],
        "rederived_lisjong_features": rederived["features"],
        "replayed": rederived["replay"],
        "run_kind": plan["run_kind"],
        "schema": VERIFY_SCHEMA,
        "status": summary["status"],
        "summary_digest": summary["summary_digest"],
    }
    _write_once(output, document)
    return document


_COMPARED = (
    "action_sequence_sha256",
    "completed_match_sha256",
    "decisions",
    "end_reason",
    "final_points",
    "kyoku_count",
    "kyoku_record",
    "policy_call_counts",
)


def compare_rehearsals(
    observed_plan: str | Path,
    unobserved_plan: str | Path,
    *,
    output: str | Path | None = None,
    environment: dict | None = None,
) -> dict[str, object]:
    """観測あり / なしのRの一致と、観測ありplanのD armの2回実行の一致を要求する。"""
    observed, summary, _, _, _ = verified_plan(observed_plan, environment=environment)
    unobserved, _, _, _, _ = verified_plan(unobserved_plan, environment=environment)
    if (observed["run_kind"], observed["observation"]) != (REHEARSAL, OBSERVED) or (
        unobserved["run_kind"],
        unobserved["observation"],
    ) != (REHEARSAL, UNOBSERVED):
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
    invalid = summary["invalid"]["count"]
    match = all(not item["differences"] for item in seeds) and invalid == 0
    document = {
        "branches": summary["branches"],
        "compared_fields": list(_COMPARED),
        "invalid_branches": invalid,
        "match": match,
        "observed_plan_digest": observed["plan_digest"],
        "observed_summary_digest": summary["summary_digest"],
        "schema": COMPARE_SCHEMA,
        "seeds": seeds,
        "unobserved_plan_digest": unobserved["plan_digest"],
    }
    _write_once(output, document)
    if not match:
        _fail(
            "rehearsal R observed / unobserved, the D arm repeat or a branch check "
            "differ (STOP-INVALID)"
        )
    return document


def estimate_budget(
    observed_plan: str | Path,
    unobserved_plan: str | Path,
    *,
    output: str | Path | None = None,
    environment: dict | None = None,
) -> dict[str, object]:
    """照合済みrehearsalの実測から本実行＋verify＋回収の見積もりを作る。"""
    compared = compare_rehearsals(
        observed_plan, unobserved_plan, environment=environment
    )
    plan, summary, timings, _, _ = verified_plan(observed_plan, environment=environment)
    other, _, other_timings, _, _ = verified_plan(
        unobserved_plan, environment=environment
    )
    rows = timings["tasks"]
    elapsed = sum(
        max(item["monotonic_elapsed_seconds"], item["realtime_elapsed_seconds"])
        for item in (timings, other_timings)
    )
    values = estimate_values(
        r_walls=[row["wall_seconds"] for row in rows if row["arm"] == ARM_R],
        d_walls=[row["wall_seconds"] for row in rows if row["arm"] == ARM_D],
        branches=summary["branches"],
        rehearsal_seat_hanchans=summary["seat_hanchans"],
        rehearsal_elapsed_seconds=elapsed,
    )
    payload = {
        **values,
        "environment_digest": plan["environment_digest"],
        "execution_target": plan["execution_target"],
        "observed_plan_digest": plan["plan_digest"],
        "observed_summary_digest": compared["observed_summary_digest"],
        "schema": ESTIMATE_SCHEMA,
        "unobserved_plan_digest": other["plan_digest"],
    }
    document = parse_time_estimate(
        {**payload, "estimate_digest": document_identity(payload)}
    )
    _write_once(output, document)
    return document


__all__ = [
    "ACTIONS_SCHEMA",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "BRANCHES_SCHEMA",
    "COMPLETE_STATUS",
    "LAYOUT",
    "REHEARSAL_STATUS",
    "STOP_STATUS",
    "action_lines",
    "branch_lines",
    "build_summary",
    "build_unobserved_summary",
    "compare_rehearsals",
    "cost_screening",
    "estimate_budget",
    "evaluate_branch",
    "feature_lines",
    "hanchan_row",
    "hanchans_document",
    "require_action_lines",
    "require_branch_lines",
    "require_branches_match_actions",
    "require_hanchans",
    "require_timings",
    "run_plan",
    "variance_bootstrap",
    "variance_summary",
    "verify_plan",
    "write_stop",
]
