"""#553 pon-first-pass-branch-v1（lisjong #362のP1 pilot）の実行・照合・summary・strict verify。

```text
run       live gate -> R（seed順、Champion×4、配置の入替なし）
          -> D arm（成立した候補ごとに先頭から再実行。rehearsalの観測ありplanは全D armを
             別processでもう1回）-> live gate
          -> <output>/{hanchans.json, actions.jsonl, branches.jsonl, summary.json,
                       timings.json, SHA256SUMS}（観測なしrehearsalはhanchansだけ）
          未完了ならoutputを作らず、diagnosticsへstop記録（それまでのtask行を含む）を残す
verify    別processで保存物だけを読み、hash・file集合・schedule・形・行動記録とdigestの整合を
          検査し、鳴き解析・Δ・分散・配分判定を再導出する。allocatedは範囲の最小・最大の2 seedの
          Rと全D armを再実行し、行動記録を含めて完全一致を要求する
compare   rehearsalの観測あり / なしのRの一致と、D armの2回実行の一致を要求する
estimate  照合済みrehearsalから本実行＋verify＋回収の見積もりを作る（開始条件はplanで判定）
```

- 1行 = 1候補（門前の最初のPon）。成立した候補だけが分岐でD armを持つ。他家ロンで取り消された
  候補は分母とともに数え、分岐にしない（INVALIDでもない）
- 分岐行の ``decision_input``（判断時点の値）と ``selection``（windowの解決結果）はfield集合を
  固定し、混在をstrict readerで拒否する
- 判定は ``STOP-INVALID`` > ``STOP-INCOMPLETE`` > ``NOT-VIABLE-SUPPORT`` / ``DESIGN-POSSIBLE``
  （``pon_first_pass_allocation``）。平均Δと平均差の規模計算は判定に使わない
"""

import datetime
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
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

from . import pon_first_pass_allocation as allocation_rules
from .call_value_shadow_worker import (
    CANCELLED_PRIORITY_CALL,
    CANCELLED_RON,
    ESTABLISHED,
    action_sequence_digest,
    canonical_digest,
    decode_action,
    decode_context,
)
from .errors import KyokuRecordError
from .executor import ExecutedHanchan, HanchanTask, RunStopped, current_peak_rss_kib
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
from .pon_first_pass_plan import (
    ALPHA,
    DESIGN_POSSIBLE,
    ESTIMATE_SCHEMA,
    EVALUATION_KIND,
    NOT_VIABLE_SUPPORT,
    OBSERVED,
    POWER,
    STOP_INCOMPLETE,
    STOP_INVALID,
    STOP_SCHEMA,
    TARGET_EFFECT,
    UNOBSERVED,
    PonFirstPassError,
    estimate_values,
    load_plan,
    parse_time_estimate,
    plan_champion,
    plan_schedule,
    require_live_target,
)
from .pon_first_pass_plan import PLAN_SCHEMA as PILOT_PLAN_SCHEMA
from .pon_first_pass_worker import (
    CALL_ANALYSIS,
    DECISION_INPUT_FIELDS,
    PROTOCOL_VERSION,
    SELECTION_FIELDS,
    WRAPPER_VERSION,
    play_d_arm,
    play_r_observed,
    play_r_unobserved,
    rederive_call_analysis,
    split_measurement,
    task_branch,
)
from .protocol import EngineAabbProtocolError
from .riichi_dama_branch import (
    ARM_D,
    ARM_D_REPEAT,
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
    jsonl_text,
    r_settlement,
    read_jsonl,
    require_analysis_environment,
)
from .riichi_dama_branch_worker import BranchTask
from .riichi_first_dama import _TIMING_FIELDS, variance_summary
from .riichi_first_dama import _TIMING_ROW_FIELDS as _R1_TIMING_ROW_FIELDS

HANCHANS_FILENAME = "hanchans.json"
ACTIONS_FILENAME = "actions.jsonl"
BRANCHES_FILENAME = "branches.jsonl"
SUMS_FILENAME = "SHA256SUMS"
HANCHANS_SCHEMA = "arena-pon-first-pass-branch-v1-hanchans-v1"
ACTIONS_SCHEMA = "arena-pon-first-pass-branch-v1-actions-v1"
BRANCHES_SCHEMA = "arena-pon-first-pass-branch-v1-branches-v1"
SUMMARY_SCHEMA = "arena-pon-first-pass-branch-v1-summary-v1"
TIMINGS_SCHEMA = "arena-pon-first-pass-branch-v1-timings-v1"
VERIFY_SCHEMA = "arena-pon-first-pass-branch-v1-verify-v1"
COMPARE_SCHEMA = "arena-pon-first-pass-branch-v1-rehearsal-compare-v1"
KYOKU_RECORD_SCHEMA = "arena-pon-first-pass-branch-v1-kyoku-record-v1"

COMPLETE_STATUS = "P1 PILOT COMPLETE; DEVELOPMENT PILOT, NOT A STRENGTH RESULT"
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO PILOT STATISTIC"
STOP_STATUS = (
    "STOP-INCOMPLETE; NOT A RESULT; NO FORMAL AGGREGATION OF THE COMPLETED PART"
)
BOOTSTRAP_SEED = 553
PRE_BRANCH_PROPOSAL_SHARE = Fraction(7, 10)
"""生成費用の削減を提案できる分岐前の割合の下限（NOT-VIABLE-SUPPORTのときだけ）。"""
_TIMING_ROW_FIELDS = _R1_TIMING_ROW_FIELDS | {"arm_seconds", "pre_branch_seconds"}
"""R1のtask行に、D armのworker内測定値（``arm_seconds``・``pre_branch_seconds``）を加える。

Rの行は両方null。測定値は再実行で一致しないので、記録本体の完全一致の対象にしない。"""
_SEAT_OUTCOMES = ("win", "deal_in", "draw", "other")
_ROUTES = (
    "CallDecisionRoute.PARENT_DECISION",
    "CallDecisionRoute.PARENT_PASS",
    "CallDecisionRoute.SPEED_CALL",
)


def _fail(message: str):
    raise PonFirstPassError(f"pon-first-pass-branch-v1: {message}")


def _ratio(value: Fraction) -> dict[str, int]:
    return {"denominator": value.denominator, "numerator": value.numerator}


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


LAYOUT = RecordLayout(
    protocol_id=PROTOCOL_ID,
    kyoku_record_schema=KYOKU_RECORD_SCHEMA,
    event_fact_schema="arena-pon-first-pass-branch-v1-event-fact-v1",
    manifest_schema="arena-pon-first-pass-branch-v1-record-manifest-v1",
    key_fields=("placement",),
    validate_keys=_validate_keys,
    validate_seats=_validate_seats,
)


@dataclass(frozen=True)
class ProtocolSpec:
    """同じP1の観測・照合を使うprotocolごとのschema・plan・header・summaryの違い。

    pilot（#553）とsource（#556）は別の契約で、保存物のschemaを共有しない。
    """

    plan_schema: str
    protocol_version: str
    schemas: dict
    layout: RecordLayout
    load_plan: object
    require_live_target: object
    complete_status: str
    rehearsal_status: str
    header_extra: object = None
    summary: object = None
    """``None``ならpilotのsummary。sourceは ``summary(plan, common, evaluated, invalid, valid)``。"""


PILOT = ProtocolSpec(
    plan_schema=PILOT_PLAN_SCHEMA,
    protocol_version=PROTOCOL_VERSION,
    schemas={
        "actions": ACTIONS_SCHEMA,
        "branches": BRANCHES_SCHEMA,
        "compare": COMPARE_SCHEMA,
        "hanchans": HANCHANS_SCHEMA,
        "stop": STOP_SCHEMA,
        "summary": SUMMARY_SCHEMA,
        "timings": TIMINGS_SCHEMA,
        "verify": VERIFY_SCHEMA,
    },
    layout=LAYOUT,
    load_plan=load_plan,
    require_live_target=require_live_target,
    complete_status=COMPLETE_STATUS,
    rehearsal_status=REHEARSAL_STATUS,
)
_PROTOCOLS = {PILOT.plan_schema: PILOT}


def register_protocol(spec: ProtocolSpec) -> None:
    """別protocol（#556のsource）を登録する。同じplan schemaの再登録は同じspecだけ。"""
    existing = _PROTOCOLS.setdefault(spec.plan_schema, spec)
    if existing is not spec:
        _fail(f"protocol {spec.plan_schema} is already registered")


def spec_of(plan: dict) -> ProtocolSpec:
    try:
        return _PROTOCOLS[plan["schema"]]
    except KeyError, TypeError:
        _fail("the plan belongs to no registered protocol")


def load_any_plan(plan_path: str | Path) -> dict:
    """保存済みplanのschemaから登録済みprotocolを選び、そのprotocolのstrict loadで読む。"""
    raw = read_document(Path(plan_path), "plan")
    return spec_of(raw).load_plan(plan_path)


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
        "seed",
        "selector_call_counts",
    }
)


def hanchan_row(item: ExecutedHanchan, layout: RecordLayout = LAYOUT) -> dict:
    capture = item.capture
    analysis = item.policy_analysis
    record = project_row(
        capture.match,
        seed=capture.seed,
        keys={"placement": capture.placement},
        seats=capture.seats,
        selector_call_counts=capture.selector_call_counts,
        policy_call_counts=capture.policy_call_counts,
        layout=layout,
    )
    return {
        **_hanchan_document(item),
        "action_sequence_sha256": analysis["action_sequence_sha256"],
        "decisions": analysis["decisions"],
        "kyoku_record": record,
    }


def hanchans_document(
    plan: dict, executed: list[ExecutedHanchan], *, observed: bool
) -> dict:
    return {
        "hanchans": [hanchan_row(item, spec_of(plan).layout) for item in executed],
        "observation": OBSERVED if observed else UNOBSERVED,
        "plan_digest": plan["plan_digest"],
        "schema": spec_of(plan).schemas["hanchans"],
    }


def require_hanchans(plan: dict, hanchans: object) -> None:
    """Rの客観recordが計画したscheduleと完全一致すること（不足・重複・順序の入替を拒否）。"""
    spec = spec_of(plan)
    expected = [(item["seed"], item["placement"]) for item in plan_schedule(plan)]
    if (
        type(hanchans) is not dict
        or set(hanchans) != {"hanchans", "observation", "plan_digest", "schema"}
        or hanchans["schema"] != spec.schemas["hanchans"]
        or hanchans["plan_digest"] != plan["plan_digest"]
        or hanchans["observation"] != plan["observation"]
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
        try:
            record = validate_hanchan_row(row["kyoku_record"], spec.layout)
        except KyokuRecordError as error:
            raise PonFirstPassError(f"{context}: kyoku record: {error}") from error
        if (
            record["seed"] != row["seed"]
            or record["placement"] != row["placement"]
            or record["policy_call_counts"] != counts
            or len(record["kyokus"]) != row["kyoku_count"]
        ):
            _fail(f"{context}: kyoku record differs from the hanchan row")


def _header(plan: dict, kind: str) -> dict:
    spec = spec_of(plan)
    header = {
        "call_analysis_version": CALL_ANALYSIS,
        "kind": "header",
        "plan_digest": plan["plan_digest"],
        "protocol_version": spec.protocol_version,
        "schema": spec.schemas[kind],
        "wrapper_version": WRAPPER_VERSION,
    }
    if spec.header_extra is not None:
        header.update(spec.header_extra(plan))
    return header


def action_lines(plan: dict, executed_r: list[ExecutedHanchan]) -> list[dict]:
    lines = [_header(plan, "actions")]
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
    if lines[0] != _header(plan, "actions"):
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
                raise PonFirstPassError(
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
# 候補・分岐の記録
# ---------------------------------------------------------------------------


def r_candidates(executed_r: list[ExecutedHanchan]) -> list[tuple[int, dict]]:
    """Rの候補を (seed, record) でseed順・decision index順に並べる。"""
    ordered = []
    for item in executed_r:
        records = sorted(
            item.policy_analysis["candidates"],
            key=lambda record: record["decision_index"],
        )
        ordered.extend((item.capture.seed, record) for record in records)
    return ordered


def _is_branch(record: dict) -> bool:
    return record["selection"]["resolution"] == ESTABLISHED


def branch_tasks(plan: dict, candidates: list[tuple[int, dict]]) -> list[BranchTask]:
    """成立した候補だけD armを実行する（取消は分岐にしない。不明はINVALIDとして残る）。"""
    champion = plan_champion(plan).to_document()
    return [
        BranchTask(
            seed=seed,
            branch_ordinal=ordinal,
            branch=task_branch(record),
            incumbent=champion,
            wheel_path=plan["execution"]["wheel_path"],
        )
        for ordinal, (seed, record) in enumerate(candidates)
        if _is_branch(record)
    ]


def execute_candidates(
    plan: dict,
    *,
    play_r: Callable,
    play_d: Callable,
    measure: Callable[[str], Callable[[ExecutedHanchan], None]],
    on_task: Callable[[int, int], None] | None = None,
    repeat: bool = False,
) -> tuple[list[ExecutedHanchan], list, dict, dict | None]:
    """Rの全半荘 → 成立した候補の全D arm（``repeat`` なら新しいworkerでもう1回）。"""
    executed_r = _execute(
        plan,
        _r_tasks(plan),
        key_fields=("placement",),
        play=play_r,
        on_executed=measure(ARM_R),
        on_task=on_task,
    )
    if any(item.policy_analysis is None for item in executed_r):
        raise RunStopped("an R hanchan returned no observation")
    candidates = r_candidates(executed_r)
    tasks = branch_tasks(plan, candidates)
    d_records: dict[int, dict] = {}
    repeat_records: dict[int, dict] | None = {} if repeat else None
    if tasks:
        for arm, target in ((ARM_D, d_records), (ARM_D_REPEAT, repeat_records)):
            if target is None:
                continue
            executed = _execute(
                plan,
                tasks,
                key_fields=("branch_ordinal",),
                play=play_d,
                on_executed=measure(arm),
                on_task=on_task,
            )
            for task, item in zip(tasks, executed, strict=True):
                target[task.branch_ordinal] = split_measurement(item.policy_analysis)[0]
    return executed_r, candidates, d_records, repeat_records


def branch_lines(
    plan: dict,
    hanchans: dict,
    candidates: list[tuple[int, dict]],
    d_records: dict[int, dict],
    repeat_records: dict[int, dict] | None,
) -> list[dict]:
    rows = {row["seed"]: row for row in hanchans["hanchans"]}
    lines = [_header(plan, "branches")]
    for ordinal, (seed, record) in enumerate(candidates):
        lines.append(
            {
                "branch_id": branch_id(seed, record["round_ordinal"], record["seat"]),
                "branch_ordinal": ordinal,
                "d": d_records.get(ordinal),
                "d_repeat": None
                if repeat_records is None
                else repeat_records.get(ordinal),
                "decision_index": record["decision_index"],
                "decision_input": dict(record["decision_input"]),
                "kind": "candidate",
                "prefix_sha256": record["prefix_sha256"],
                "r_invalid": record["invalid"],
                "r_post_sha256": record["post_sha256"],
                "r_settlement": r_settlement(
                    rows[seed]["kyoku_record"], record["round_ordinal"]
                ),
                "round": record["round"],
                "round_ordinal": record["round_ordinal"],
                "seat": record["seat"],
                "seed": seed,
                "selection": dict(record["selection"]),
            }
        )
    return lines


_BRANCH_FIELDS = frozenset(
    {
        "branch_id",
        "branch_ordinal",
        "d",
        "d_repeat",
        "decision_index",
        "decision_input",
        "kind",
        "prefix_sha256",
        "r_invalid",
        "r_post_sha256",
        "r_settlement",
        "round",
        "round_ordinal",
        "seat",
        "seed",
        "selection",
    }
)
_D_FIELDS = frozenset(
    {
        "branch_action",
        "branch_context_sha256",
        "branch_decision_index",
        "branch_ordinal",
        "calls_after_branch",
        "decisions",
        "invalid",
        "pass_applied",
        "post_sha256",
        "prefix_sha256",
        "round",
        "round_ordinal",
        "seat",
        "seed",
        "settlement",
        "status",
        "wrapper_calls_after_branch",
    }
)
_CALL_FIELDS = frozenset(
    {"decision_index", "kind", "resolution", "seat_decisions_after_branch"}
)


def require_branch_lines(plan: dict, lines: object, hanchans: dict) -> None:
    """候補行の形・順序・一意性・field分離・Rとの一対一を検査する（INVALIDの判定は別）。"""
    require_hanchans(plan, hanchans)
    if type(lines) is not list or not lines:
        _fail("branches are empty")
    if lines[0] != _header(plan, "branches"):
        _fail("branches header does not belong to the plan")
    rows = {row["seed"]: row for row in hanchans["hanchans"]}
    seeds = [item["seed"] for item in plan_schedule(plan)]
    keys = []
    for ordinal, line in enumerate(lines[1:]):
        context = f"candidate {ordinal}"
        if type(line) is not dict or set(line) != _BRANCH_FIELDS:
            _fail(f"{context}: candidate fields do not match")
        if line["kind"] != "candidate" or line["branch_ordinal"] != ordinal:
            _fail(f"{context}: candidate ordinal is not sequential")
        # 判断時点の値と標本選択・照合の値を混ぜない（特徴入力への混入を防ぐ）
        if (
            type(line["decision_input"]) is not dict
            or set(line["decision_input"]) != DECISION_INPUT_FIELDS
        ):
            _fail(
                f"{context}: decision_input holds other than the decision-time values"
            )
        if (
            type(line["selection"]) is not dict
            or set(line["selection"]) != SELECTION_FIELDS
        ):
            _fail(f"{context}: selection holds other than the resolution values")
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
                _fail(f"{context}: {name} belongs to another candidate")
            calls = record["calls_after_branch"]
            if type(calls) is not list or any(
                type(item) is not dict or set(item) != _CALL_FIELDS for item in calls
            ):
                _fail(f"{context}: {name} calls after the branch are invalid")
        established = line["selection"]["resolution"] == ESTABLISHED
        if (line["d"] is not None) is not established:
            _fail(f"{context}: a D arm exists exactly for an established candidate")
        repeat_expected = plan["run_kind"] == REHEARSAL
        if (line["d_repeat"] is not None) is not (
            repeat_expected and line["d"] is not None
        ):
            _fail(f"{context}: the D arm repeat does not match the run kind")
        keys.append((seeds.index(line["seed"]), line["decision_index"]))
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        _fail("candidates are not in schedule and decision order or are duplicated")
    per_round = [
        (line["seed"], line["round_ordinal"], line["seat"]) for line in lines[1:]
    ]
    if len(per_round) != len(set(per_round)):
        _fail("a seat has two candidates in one round")


def require_branches_match_actions(branches: list[dict], actions: list[dict]) -> None:
    """候補の位置・Pon・digestが、Rの行動記録から再計算した値と一致すること。"""
    by_seed = {line["seed"]: line["decisions"] for line in actions[1:]}
    for line in branches[1:]:
        decisions = by_seed[line["seed"]]
        index = line["decision_index"]
        entry = decisions[index]
        if (
            entry[0] != line["round_ordinal"]
            or entry[1] != line["seat"]
            or entry[2] != line["decision_input"]["pon"]
        ):
            _fail(f"candidate {line['branch_id']}: the R Pon is not in the actions")
        if line["prefix_sha256"] != _entries_digest(decisions, 0, index):
            _fail(f"candidate {line['branch_id']}: the R prefix differs")
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
            _fail(f"candidate {line['branch_id']}: the R post digest differs")


# ---------------------------------------------------------------------------
# 1候補の照合・局収支・Δ
# ---------------------------------------------------------------------------


def _opponent_riichi(context: dict | None) -> bool | None:
    """分岐点のpublic contextで他家のリーチ（宣言中を含む）があるか（記述用の公開事実）。"""
    if context is None:
        return None
    try:
        decision = decode_context(context)
    except Exception:  # codecの拒否は別のINVALIDとして数える
        return None
    from lisjong.policy_contract.riichi import RiichiState

    own = int(decision.input.self_seat)
    return any(
        index != own and player.riichi is not RiichiState.NONE
        for index, player in enumerate(decision.input.players)
    )


def _route(decision_input: dict) -> str | None:
    analysis = decision_input["call_analysis"]
    return None if type(analysis) is not dict else analysis.get("route")


def _calls_check(record: dict) -> str | None:
    """分岐後の鳴き（選択と成立）の整合。問題があれば理由を返す。"""
    calls = record["calls_after_branch"]
    previous = record["branch_decision_index"]
    turns = 0
    for item in calls:
        if (
            not _nonnegative_int(item["decision_index"])
            or previous is None
            or item["decision_index"] <= previous
            or type(item["seat_decisions_after_branch"]) is not int
            or item["seat_decisions_after_branch"] <= turns
            or item["kind"] not in ("chi", "pon", "daiminkan")
        ):
            return "the calls after the branch are out of order or invalid"
        # 分岐後はChampionの継続方策なので、Chiは他家のPon・大明槓に優先されて取り消されうる
        # （正常）。Ponが優先鳴きに負けることは牌の枚数上ない
        allowed = (ESTABLISHED, CANCELLED_RON) + (
            (CANCELLED_PRIORITY_CALL,) if item["kind"] == "chi" else ()
        )
        if item["resolution"] not in allowed:
            return f"a {item['kind']} after the branch was {item['resolution']}"
        previous = item["decision_index"]
        turns = item["seat_decisions_after_branch"]
    if record["wrapper_calls_after_branch"] != len(calls):
        return "the selected calls after the branch differ between the wrapper and the actions"
    return None


def evaluate_line(line: dict, row: dict) -> dict:
    """1候補のINVALID理由・分類・局収支・Δ・記述値を返す。"""
    seat = line["seat"]
    invalid = [f"R: {reason}" for reason in line["r_invalid"]]
    decision_input = line["decision_input"]
    resolution = line["selection"]["resolution"]
    result = {
        "branch_id": line["branch_id"],
        "calls_after_branch": None,
        "delta": None,
        "final_riichi_stick_awards": None,
        "g_d": None,
        "g_r": None,
        "invalid": invalid,
        "opponent_riichi": _opponent_riichi(decision_input["context"]),
        "outcomes": None,
        "route": _route(decision_input),
        "seed": line["seed"],
        "status": None,
    }
    expected_r = r_settlement(row["kyoku_record"], line["round_ordinal"])
    if line["r_settlement"] != expected_r:
        invalid.append("the R settlement differs from the kyoku record")
    if expected_r["round"] != line["round"]:
        invalid.append("the candidate context belongs to another round than the record")
    if decision_input["context"] is None:
        invalid.append("the candidate context is missing")
    if decision_input["call_analysis_error"] is not None:
        invalid.append(
            f"lisjong rejects the call analysis: {decision_input['call_analysis_error']}"
        )
    if result["route"] not in _ROUTES:
        invalid.append("the call analysis has no known route")
    if result["opponent_riichi"] is None:
        invalid.append("the candidate context cannot be read")
    if resolution == CANCELLED_RON:
        result["status"] = "ron_cancelled"
        try:
            target = int(decode_action(decision_input["pon"]).target)
        except Exception:  # codecの拒否
            target = None
        if (
            expected_r["outcome"] != "win"
            or seat in expected_r["winner_seats"]
            or expected_r["source_seat"] != target
        ):
            invalid.append(
                "a ron-cancelled candidate has no ron by another player on the "
                "called discard"
            )
        return result
    if resolution != ESTABLISHED:
        invalid.append(f"the candidate resolution is {resolution}")
        return result
    result["status"] = "branch"
    record = line["d"]
    invalid.extend(f"D: {reason}" for reason in record["invalid"])
    if record["status"] != "branched":
        invalid.append(f"the D arm wrapper status is {record['status']}")
    if record["branch_decision_index"] != line["decision_index"]:
        invalid.append("the D branch point is at another decision index")
    if record["prefix_sha256"] != line["prefix_sha256"]:
        invalid.append("the actions before the branch point differ from R")
    if record["branch_context_sha256"] != decision_input["context_sha256"]:
        invalid.append("the D branch context differs from R")
    if type(record["branch_action"]) is not dict or record["branch_action"].get(
        "kind"
    ) != ("pass"):
        invalid.append("the D branch point did not pass")
    if record["pass_applied"] is not True:
        invalid.append("the branch seat holds a meld of the passed discard in D")
    reason = _calls_check(record)
    if reason is not None:
        invalid.append(reason)
    if (
        record["round"] != line["round"]
        or record["round_ordinal"] != line["round_ordinal"]
    ):
        invalid.append("the D arm settled another round")
    if line["d_repeat"] is not None and line["d_repeat"] != record:
        invalid.append("the D arm repeat differs")
    result["calls_after_branch"] = [
        {"kind": item["kind"], "resolution": item["resolution"]}
        for item in record["calls_after_branch"]
    ]
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
# 記述・配分（保存物だけから再導出できる値）
# ---------------------------------------------------------------------------


def population_summary(evaluated: list[dict]) -> dict:
    """全対象判断（候補）・成立分岐・ロン取消の件数と取消率（分母つき）。"""
    candidates = [row for row in evaluated if row["status"] is not None]
    branches = sum(1 for row in candidates if row["status"] == "branch")
    cancelled = sum(1 for row in candidates if row["status"] == "ron_cancelled")
    total = branches + cancelled
    return {
        "branches": branches,
        "candidates": total,
        "ron_cancel_rate": None if total == 0 else _ratio(Fraction(cancelled, total)),
        "ron_cancelled": cancelled,
    }


def recall_summary(valid: list[dict]) -> dict:
    """分岐後の再鳴き（選択と成立を分ける）と、それまでの記述（判定に使わない）。"""
    selected = sum(len(row["calls_after_branch"]) for row in valid)
    established = [
        [
            item
            for item in row["calls_after_branch"]
            if item["resolution"] == ESTABLISHED
        ]
        for row in valid
    ]
    kinds = [item["kind"] for items in established for item in items]
    resolutions = [
        item["resolution"] for row in valid for item in row["calls_after_branch"]
    ]
    return {
        "branches": len(valid),
        "branches_with_established_call": sum(1 for items in established if items),
        "cancelled_calls": {
            "priority_call": resolutions.count(CANCELLED_PRIORITY_CALL),
            "ron": resolutions.count(CANCELLED_RON),
        },
        "established_calls": len(kinds),
        "established_kinds": {kind: kinds.count(kind) for kind in sorted(set(kinds))},
        "selected_calls": selected,
    }


def breakdown(valid: list[dict]) -> dict:
    """記述用の内訳: lisjongの鳴き解析のroute（役牌parent経路 / スピード鳴き）と他家リーチ。"""
    return {
        "opponent_riichi": {
            "false": sum(1 for row in valid if row["opponent_riichi"] is False),
            "true": sum(1 for row in valid if row["opponent_riichi"] is True),
        },
        "route": {
            route: sum(1 for row in valid if row["route"] == route) for route in _ROUTES
        },
    }


def mean_difference_sizing(variance: dict) -> dict:
    """平均差の規模計算（記録のみ。停止条件ではなく、E2の検出力でもない）。"""
    normal = statistics.NormalDist()
    z = normal.inv_cdf(1 - float(ALPHA)) + normal.inv_cdf(float(POWER))
    bootstrap = variance["bootstrap"]["sigma_u_squared"]
    if bootstrap is None:
        return {
            "not_a_stop_condition": True,
            "seeds_needed": None,
            "sigma_u_p90": None,
            "target_effect_100_points": TARGET_EFFECT,
            "z_sum": z,
        }
    p90 = Fraction(bootstrap["P90"]["numerator"], bootstrap["P90"]["denominator"])
    return {
        "not_a_stop_condition": True,
        "seeds_needed": math.ceil(z * z * float(p90) / (TARGET_EFFECT * TARGET_EFFECT)),
        "sigma_u_p90": math.sqrt(p90),
        "target_effect_100_points": TARGET_EFFECT,
        "z_sum": z,
    }


def support_from_timings(timings: dict, *, branches: int, seeds: int) -> dict:
    rows = timings["tasks"]
    r_walls = [row["wall_seconds"] for row in rows if row["arm"] == ARM_R]
    d_walls = [row["wall_seconds"] for row in rows if row["arm"] == ARM_D]
    if not r_walls:
        _fail("the support judgment needs R walls")
    return allocation_rules.support_judgment(
        r_mean=sum(r_walls) / len(r_walls),
        d_mean=sum(d_walls) / len(d_walls) if d_walls else None,
        branches=branches,
        seeds=seeds,
    )


def pre_branch_summary(timings: dict, decision: str | None) -> dict:
    """D armの分岐前の再実行時間の割合（worker内のmonotonic測定値から）。

    割合 = Σ pre_branch_seconds / Σ arm_seconds（全D arm、分岐点に届かなかった行は分母・分子とも
    除いて件数を記録）。判断回数の割合で代用しない。NOT-VIABLE-SUPPORTかつ割合 ≥ 7/10のときだけ、
    生成費用の削減（記録行動の再生計測）を提案できる（自動では着手しない）。
    """
    rows = [row for row in timings["tasks"] if row["arm"] == ARM_D]
    measured = [row for row in rows if row["pre_branch_seconds"] is not None]
    pre = sum(row["pre_branch_seconds"] for row in measured)
    arm = sum(row["arm_seconds"] for row in measured)
    share = None if arm <= 0 else pre / arm
    return {
        "arm_seconds": arm,
        "clock": "worker-monotonic",
        "d_arms": len(rows),
        "d_arms_without_branch_point": len(rows) - len(measured),
        "pre_branch_seconds": pre,
        "proposal_share": _ratio(PRE_BRANCH_PROPOSAL_SHARE),
        "share": share,
        "cost_reduction_may_be_proposed": decision == NOT_VIABLE_SUPPORT
        and share is not None
        and share >= float(PRE_BRANCH_PROPOSAL_SHARE),
    }


def _require_measurement(row: dict, index: int) -> None:
    pre, arm = row["pre_branch_seconds"], row["arm_seconds"]
    if row["arm"] == ARM_R:
        if pre is not None or arm is not None:
            _fail(f"tasks[{index}]: an R row has no D arm measurement")
        return
    _seconds(arm, f"tasks[{index}].arm_seconds")
    if pre is not None and _seconds(pre, f"tasks[{index}].pre_branch_seconds") > arm:
        _fail(f"tasks[{index}]: the pre-branch time exceeds the D arm time")


def require_measurement(measurement: object, context: str) -> dict:
    """worker内の測定値の形と範囲（再実行で値は一致しないので、値の一致は要求しない）。"""
    if (
        type(measurement) is not dict
        or set(measurement) != {"arm_seconds", "clock", "pre_branch_seconds"}
        or measurement["clock"] != "worker-monotonic"
    ):
        _fail(f"{context}: the D arm measurement is missing or has another shape")
    _require_measurement({"arm": ARM_D, **measurement}, 0)
    return measurement


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
        _require_measurement(row, index)
    schedule = _seconds(timings.get("schedule_wall_seconds"), "schedule_wall_seconds")
    if schedule > limits["wall_seconds"]:
        _fail("the schedule exceeded the wall limit")
    if complete:
        if (
            set(timings) != _TIMING_FIELDS
            or timings["schema"] != spec_of(plan).schemas["timings"]
        ):
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


def expected_tasks(plan: dict, branches: list[dict] | None) -> list[tuple]:
    """timingsの行が持つべき (arm, seed, branch_ordinal) の順序。"""
    tasks = [(ARM_R, item["seed"], None) for item in plan_schedule(plan)]
    if branches is None:
        return tasks
    arms = [line for line in branches[1:] if line["d"] is not None]
    tasks.extend((ARM_D, line["seed"], line["branch_ordinal"]) for line in arms)
    if plan["run_kind"] == REHEARSAL:
        tasks.extend(
            (ARM_D_REPEAT, line["seed"], line["branch_ordinal"]) for line in arms
        )
    return tasks


def build_summary(
    plan: dict,
    hanchans: dict,
    actions: list[dict],
    branches: list[dict],
    timings: dict,
) -> dict:
    """summaryは保存物（plan・hanchans・actions・branches・timingsのtask行）だけから決まる。"""
    require_action_lines(plan, actions, hanchans)
    require_branch_lines(plan, branches, hanchans)
    require_branches_match_actions(branches, actions)
    require_timings(plan, timings, expected_tasks(plan, branches), complete=False)
    rows_by_seed = {row["seed"]: row for row in hanchans["hanchans"]}
    evaluated = [
        evaluate_line(line, rows_by_seed[line["seed"]]) for line in branches[1:]
    ]
    invalid = [row for row in evaluated if row["invalid"]]
    valid = [row for row in evaluated if not row["invalid"]]
    spec = spec_of(plan)
    if spec.summary is not None:
        common = {
            "inputs": {
                "actions_sha256": sha256_bytes(jsonl_text(actions).encode("utf-8")),
                "branches_sha256": sha256_bytes(jsonl_text(branches).encode("utf-8")),
                "hanchans_sha256": document_sha256(hanchans),
            },
            "hanchans": hanchans,
            "timings": timings,
        }
        payload = spec.summary(plan, common, evaluated, invalid, valid)
        return {**payload, "summary_digest": document_identity(payload)}
    valid_branches = [row for row in valid if row["status"] == "branch"]
    outcome_counts = {
        arm: {name: 0 for name in _SEAT_OUTCOMES} for arm in (ARM_R, ARM_D)
    }
    for row in valid_branches:
        for arm in (ARM_R, ARM_D):
            outcome_counts[arm][row["outcomes"][arm]] += 1
    allocated = plan["run_kind"] == ALLOCATED
    decision = variance = support = sizing = None
    status = REHEARSAL_STATUS
    if allocated:
        status = COMPLETE_STATUS
        variance = variance_summary(valid_branches, bootstrap_seed=BOOTSTRAP_SEED)
        sizing = mean_difference_sizing(variance)
        if invalid:
            decision = STOP_INVALID
        else:
            support = support_from_timings(
                timings,
                branches=len(valid_branches),
                seeds=len(hanchans["hanchans"]),
            )
            decision = (
                DESIGN_POSSIBLE
                if support["verdict"] == allocation_rules.SUPPORT_OK
                else NOT_VIABLE_SUPPORT
            )
    payload = {
        "breakdown": breakdown(valid_branches),
        "decision": decision,
        "decisions": sum(row["decisions"] for row in hanchans["hanchans"]),
        "descriptive": {
            "delta": delta_stats(valid_branches, with_bootstrap=False),
            "not_used_for_any_decision": True,
            "seat_outcomes": outcome_counts,
        },
        "evaluation_kind": EVALUATION_KIND,
        "hanchan_count": len(hanchans["hanchans"]),
        "inputs": {
            "actions_sha256": sha256_bytes(jsonl_text(actions).encode("utf-8")),
            "branches_sha256": sha256_bytes(jsonl_text(branches).encode("utf-8")),
            "hanchans_sha256": document_sha256(hanchans),
        },
        "invalid": {
            "candidates": [
                {"branch_id": row["branch_id"], "reasons": row["invalid"]}
                for row in invalid
            ],
            "count": len(invalid),
        },
        "kyoku_count": sum(row["kyoku_count"] for row in hanchans["hanchans"]),
        "mean_difference_sizing": sizing,
        "memory": memory_summary(timings["tasks"]),
        "not_a_strength_result": True,
        "observation": plan["observation"],
        "plan_digest": plan["plan_digest"],
        "population": population_summary(evaluated),
        "pre_branch": pre_branch_summary(timings, decision),
        "protocol_version": spec.protocol_version,
        "recalls": recall_summary(valid_branches),
        "rows": [dict(row) for row in evaluated],
        "run_kind": plan["run_kind"],
        "schema": spec.schemas["summary"],
        "seat_hanchans": 4 * len(hanchans["hanchans"]),
        "status": status,
        "support": support,
        "timing": _timing_summary(timings),
        "variance": variance,
        "wrapper_version": WRAPPER_VERSION,
    }
    return {**payload, "summary_digest": document_identity(payload)}


def build_unobserved_summary(plan: dict, hanchans: dict, timings: dict) -> dict:
    spec = spec_of(plan)
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
        "protocol_version": spec.protocol_version,
        "run_kind": plan["run_kind"],
        "schema": spec.schemas["summary"],
        "status": spec.rehearsal_status,
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
        "schema": spec_of(plan).schemas["stop"],
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
    live_gate: Callable[..., None] | None = None,
    on_task: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] | None = None,
    realtime: Callable[[], float] | None = None,
) -> dict[str, object]:
    """実行して公開する。未完了はSTOP-INCOMPLETEで、outputを作らずstop記録を残す。

    ``play_r`` / ``play_d`` / ``live_gate`` / clockはtestのseam。``live_gate`` の既定はplanの
    protocolのlive gate。
    """
    plan = load_any_plan(plan_path)
    spec = spec_of(plan)
    live_gate = live_gate or spec.require_live_target
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
            measurement = {"arm_seconds": None, "pre_branch_seconds": None}
            if arm != ARM_R:
                found = require_measurement(
                    split_measurement(item.policy_analysis or {})[1],
                    f"{arm} arm of seed {item.capture.seed}",
                )
                measurement = {
                    "arm_seconds": found["arm_seconds"],
                    "pre_branch_seconds": found["pre_branch_seconds"],
                }
            rows.append(
                {
                    "arm": arm,
                    "branch_ordinal": getattr(item.capture, "branch_ordinal", None),
                    "parent_peak_rss_kib": parent_kib,
                    "seed": item.capture.seed,
                    "wall_seconds": item.wall_seconds,
                    "worker_peak_rss_kib": item.peak_rss_kib,
                    **measurement,
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
            executed_r, candidates, d_records, repeat_records = execute_candidates(
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
                "schema": spec.schemas["timings"],
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
                    plan, hanchans, candidates, d_records, repeat_records
                )
                write_new_artifact_file(staging / ACTIONS_FILENAME, jsonl_text(actions))
                write_new_artifact_file(staging / BRANCHES_FILENAME, jsonl_text(lines))
            timings["write_seconds"] = clock() - write_started
            summary_started = clock()
            if observed:
                summary = build_summary(plan, hanchans, actions, lines, timings)
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

_R_CANDIDATE_KEYS = (
    ("decision_index", "decision_index"),
    ("decision_input", "decision_input"),
    ("prefix_sha256", "prefix_sha256"),
    ("r_invalid", "invalid"),
    ("r_post_sha256", "post_sha256"),
    ("round", "round"),
    ("round_ordinal", "round_ordinal"),
    ("seat", "seat"),
    ("selection", "selection"),
)


def rederive_analysis(branches: list[dict]) -> int:
    """保存したcontext・Ponからlisjongの鳴き解析を再計算し、記録との一致を要求する。"""
    count = 0
    for line in branches[1:]:
        decision_input = line["decision_input"]
        if decision_input["context"] is None:
            continue
        expected = rederive_call_analysis(decision_input)
        if (
            expected["call_analysis"] != decision_input["call_analysis"]
            or expected["call_analysis_error"] != decision_input["call_analysis_error"]
        ):
            _fail(
                f"candidate {line['branch_id']}: the call analysis does not re-derive"
            )
        count += 1
    return count


def replay_seeds_of(
    plan: dict,
    hanchans: dict,
    actions: list[dict],
    branches: list[dict],
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
        if hanchan_row(item, spec_of(plan).layout) != rows[seed]:
            _fail(f"seed {seed}: the replayed R differs from the record (STOP-INVALID)")
        if item.policy_analysis["actions"] != recorded_actions[seed]:
            _fail(f"seed {seed}: the replayed R actions differ (STOP-INVALID)")
        records = sorted(
            item.policy_analysis["candidates"],
            key=lambda record: record["decision_index"],
        )
        lines = stored.get(seed, [])
        if len(records) != len(lines):
            _fail(f"seed {seed}: the replayed R has another number of candidates")
        d_tasks = []
        for record, line in zip(records, lines, strict=True):
            if any(line[name] != record[key] for name, key in _R_CANDIDATE_KEYS):
                _fail(
                    f"candidate {line['branch_id']}: the replayed R candidate differs"
                )
            if _is_branch(record):
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
                record, measurement = split_measurement(arm.policy_analysis)
                # 測定値は再実行で一致しないので、形と範囲だけを検査する
                require_measurement(measurement, f"replayed {line['branch_id']}")
                if record != line["d"]:
                    _fail(
                        f"candidate {line['branch_id']}: the replayed D arm differs "
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
    plan = load_any_plan(plan_path)
    if environment is None:
        try:
            environment = _live_environment(plan["execution"]["wheel_path"])
        except EngineAabbLockError as error:
            raise PonFirstPassError(str(error)) from error
    output = Path(plan["artifact_destinations"]["output"])
    try:
        recorded_sums = (output / SUMS_FILENAME).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise PonFirstPassError(f"cannot read {SUMS_FILENAME}: {error}") from error
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
        expected_files |= {ACTIONS_FILENAME, BRANCHES_FILENAME}
    files = output_files(output)
    if {item["path"] for item in files} != expected_files:
        _fail("output files do not match the expected set")
    recorded = read_document(output / SUMMARY_FILENAME, "summary")
    hanchans = read_document(output / HANCHANS_FILENAME, "hanchans")
    timings = read_document(output / TIMINGS_FILENAME, "timings")
    rederived = {"call_analysis": 0, "replay": {"arms": 0, "seeds": []}}
    if observed:
        actions = read_jsonl(output / ACTIONS_FILENAME, "actions")
        branches = read_jsonl(output / BRANCHES_FILENAME, "branches")
        summary = build_summary(plan, hanchans, actions, branches, timings)
        require_analysis_environment(plan, environment)
        rederived["call_analysis"] = rederive_analysis(branches)
        tasks = expected_tasks(plan, branches)
    else:
        summary = build_unobserved_summary(plan, hanchans, timings)
        tasks = expected_tasks(plan, None)
    if recorded != summary:
        _fail("summary differs from the re-derivation")
    require_timings(plan, timings, tasks)
    if observed:
        rederived["replay"] = replay_seeds_of(
            plan, hanchans, actions, branches, play_r=play_r, play_d=play_d
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
    support = summary.get("support")
    document = {
        "clocks": clock_document(timings),
        "decision": summary["decision"],
        "files": files,
        "observation": plan["observation"],
        "output_bytes": sum(item["bytes"] for item in files),
        "plan_digest": plan["plan_digest"],
        "rederived_call_analysis": rederived["call_analysis"],
        "replayed": rederived["replay"],
        "run_kind": plan["run_kind"],
        "schema": spec_of(plan).schemas["verify"],
        "status": summary["status"],
        "summary_digest": summary["summary_digest"],
        "support": None if support is None else support["verdict"],
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
    for name in (
        "environment_digest",
        "execution_target",
        "participant",
        "population",
        "schema",
    ):
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
        "candidates": summary["population"]["candidates"],
        "compared_fields": list(_COMPARED),
        "invalid_candidates": invalid,
        "match": match,
        "observed_plan_digest": observed["plan_digest"],
        "observed_summary_digest": summary["summary_digest"],
        "schema": spec_of(observed).schemas["compare"],
        "seeds": seeds,
        "unobserved_plan_digest": unobserved["plan_digest"],
    }
    _write_once(output, document)
    if not match:
        _fail(
            "rehearsal R observed / unobserved, the D arm repeat or a candidate check "
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
    """照合済みrehearsalの実測から本実行＋verify＋回収の見積もりを作る（pilotだけ）。"""
    if spec_of(load_any_plan(observed_plan)) is not PILOT:
        _fail("estimate_budget is the pilot estimate; use the protocol's own estimate")
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
        branches=summary["population"]["branches"],
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
    "BOOTSTRAP_SEED",
    "BRANCHES_SCHEMA",
    "COMPLETE_STATUS",
    "ProtocolSpec",
    "LAYOUT",
    "PILOT",
    "REHEARSAL_STATUS",
    "STOP_STATUS",
    "action_lines",
    "branch_lines",
    "breakdown",
    "build_summary",
    "build_unobserved_summary",
    "compare_rehearsals",
    "estimate_budget",
    "evaluate_line",
    "execute_candidates",
    "hanchan_row",
    "hanchans_document",
    "load_any_plan",
    "mean_difference_sizing",
    "population_summary",
    "recall_summary",
    "require_action_lines",
    "require_branch_lines",
    "require_branches_match_actions",
    "require_hanchans",
    "require_timings",
    "register_protocol",
    "run_plan",
    "spec_of",
    "support_from_timings",
    "verify_plan",
    "write_stop",
]
