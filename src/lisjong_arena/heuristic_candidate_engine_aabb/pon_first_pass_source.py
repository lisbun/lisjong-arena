"""#556 pon-first-pass-source-v1: P1学習sourceの実行・照合・summary・strict verify・見積もり。

#553（pilot）の``pon_first_pass``の観測・照合・field分離・D armの測定値・2 seed再実行verifyを
そのまま使い（``ProtocolSpec``で登録）、このprotocolのschema・header・summaryだけを与える。

- actions / branchesのheaderは、pilotのfieldに ``allocation_identity`` ・ ``artifact_identity`` ・
  ``block`` ・ ``split`` を加える（lisjong #366の`docs/p1-value-learning.md` §2の契約）
- summaryはΔの統計・分散・平均・分岐ごとのΔ・配分判定を出さない。判定はSOURCE-COMPLETE /
  STOP-INVALID（未完了はSTOP-INCOMPLETEのstop記録だけ）
- 見積もりは、TRAIN-1はrehearsal、以後は直前ブロックの実測から作る
- allocated blockのstrict verifyはplanのdiagnosticsへ開始記録を排他的に作ってから行い、成功記録
  ``verify.json`` （または失敗記録）を残す。次のブロックのplanはこの成功記録を要求する
"""

import datetime
from collections.abc import Callable
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from . import pon_first_pass as core
from .kyoku_record import PROTOCOL_ID, RecordLayout
from .paijia_tie_plan import ALLOCATED, default_clocks, memory_summary
from .pon_first_pass_source_plan import (
    ESTIMATE_SCHEMA,
    EVALUATION_KIND,
    KYOKU_RECORD_SCHEMA,
    PLAN_SCHEMA,
    PROTOCOL_VERSION,
    SCHEMAS,
    SOURCE_COMPLETE,
    STOP_INVALID,
    VERIFY_FAILED_FILENAME,
    VERIFY_FAILED_SCHEMA,
    VERIFY_FILENAME,
    VERIFY_START_FILENAME,
    VERIFY_START_SCHEMA,
    PonFirstSourceError,
    completed_block,
    estimate_values,
    load_plan,
    parse_time_estimate,
    require_live_target,
    run_elapsed_of,
)
from .pon_first_pass_worker import WRAPPER_VERSION, play_d_arm, play_r_observed
from .riichi_dama_branch import (
    ARM_D,
    ARM_R,
    _timing_summary,
    _validate_keys,
    _validate_seats,
)

COMPLETE_STATUS = (
    "P1 LEARNING SOURCE BLOCK COMPLETE; DEVELOPMENT SOURCE, NO DELTA STATISTIC, "
    "NOT A STRENGTH RESULT"
)
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO SOURCE BLOCK"

LAYOUT = RecordLayout(
    protocol_id=PROTOCOL_ID,
    kyoku_record_schema=KYOKU_RECORD_SCHEMA,
    event_fact_schema="arena-pon-first-pass-source-v1-event-fact-v1",
    manifest_schema="arena-pon-first-pass-source-v1-record-manifest-v1",
    key_fields=("placement",),
    validate_keys=_validate_keys,
    validate_seats=_validate_seats,
)
_PRE_BRANCH_FIELDS = (
    "arm_seconds",
    "clock",
    "d_arms",
    "d_arms_without_branch_point",
    "pre_branch_seconds",
    "share",
)
"""分岐前の割合のうち、sourceのsummaryに残す値（pilotの費用削減の提案判定は持たない）。"""


def _fail(message: str):
    raise PonFirstSourceError(f"pon-first-pass-source-v1: {message}")


def header_extra(plan: dict) -> dict:
    allocated = plan["run_kind"] == ALLOCATED
    return {
        "allocation_identity": plan["population"]["allocation_binding"][
            "allocation_identity"
        ]
        if allocated
        else None,
        "artifact_identity": plan["artifact_identity"],
        "block": plan["block"],
        "split": plan["split"],
    }


def summary(
    plan: dict, common: dict, evaluated: list, invalid: list, valid: list
) -> dict:
    """Δの統計を持たないsummary（品質・INVALID・候補 / 成立 / 取消の件数・時間・分岐前の割合）。"""
    hanchans = common["hanchans"]
    timings = common["timings"]
    allocated = plan["run_kind"] == ALLOCATED
    decision = (STOP_INVALID if invalid else SOURCE_COMPLETE) if allocated else None
    valid_branches = [row for row in valid if row["status"] == "branch"]
    pre_branch = core.pre_branch_summary(timings, None)
    return {
        "block": plan["block"],
        "decision": decision,
        "decisions": sum(row["decisions"] for row in hanchans["hanchans"]),
        "evaluation_kind": EVALUATION_KIND,
        "hanchan_count": len(hanchans["hanchans"]),
        "inputs": common["inputs"],
        "invalid": {
            "candidates": [
                {"branch_id": row["branch_id"], "reasons": row["invalid"]}
                for row in invalid
            ],
            "count": len(invalid),
        },
        "kyoku_count": sum(row["kyoku_count"] for row in hanchans["hanchans"]),
        "memory": memory_summary(timings["tasks"]),
        "no_delta_statistic": True,
        "not_a_strength_result": True,
        "observation": plan["observation"],
        "plan_digest": plan["plan_digest"],
        "population": core.population_summary(evaluated),
        "pre_branch": {name: pre_branch[name] for name in _PRE_BRANCH_FIELDS},
        "protocol_version": PROTOCOL_VERSION,
        "quality": {
            "recalls": core.recall_summary(valid_branches),
            "valid_branches": len(valid_branches),
            "valid_candidates": len(valid),
        },
        "run_kind": plan["run_kind"],
        "schema": SCHEMAS["summary"],
        "seat_hanchans": 4 * len(hanchans["hanchans"]),
        "split": plan["split"],
        "status": COMPLETE_STATUS if allocated else REHEARSAL_STATUS,
        "timing": _timing_summary(timings),
        "wrapper_version": WRAPPER_VERSION,
    }


SOURCE = core.ProtocolSpec(
    plan_schema=PLAN_SCHEMA,
    protocol_version=PROTOCOL_VERSION,
    schemas=SCHEMAS,
    layout=LAYOUT,
    load_plan=load_plan,
    require_live_target=require_live_target,
    complete_status=COMPLETE_STATUS,
    rehearsal_status=REHEARSAL_STATUS,
    header_extra=header_extra,
    summary=summary,
)
core.register_protocol(SOURCE)

run_plan = core.run_plan
compare_rehearsals = core.compare_rehearsals


def _utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def verify_plan(
    plan_path,
    *,
    output=None,
    environment=None,
    play_r: Callable = play_r_observed,
    play_d: Callable = play_d_arm,
    clock: Callable[[], float] | None = None,
    realtime: Callable[[], float] | None = None,
) -> dict:
    """strict verify。allocated blockは記録先をplanのdiagnosticsに固定し、1回だけ行う。

    rehearsalはpilotと同じ（``output`` は任意）。allocated blockは開始記録
    ``verify-started.json`` を排他的に作り（既存なら再実行しない）、成功なら ``verify.json`` 、
    再実行の不一致・保存物の不整合・run + verifyの上限超過なら ``verify-failed.json`` を残して止まる。
    """
    plan = load_plan(plan_path)
    if plan["run_kind"] != ALLOCATED:
        return core.verify_plan(
            plan_path,
            output=output,
            environment=environment,
            play_r=play_r,
            play_d=play_d,
        )
    if output is not None:
        _fail("an allocated block writes its verify record to the plan diagnostics")
    default_clock, default_realtime = default_clocks()
    clock = clock or default_clock
    realtime = realtime or default_realtime
    diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
    diagnostics.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    try:
        write_new_artifact_file(
            diagnostics / VERIFY_START_FILENAME,
            canonical_json_text(
                {
                    "plan_digest": plan["plan_digest"],
                    "schema": VERIFY_START_SCHEMA,
                    "started_at_utc": started_at,
                }
            ),
        )
    except (FileExistsError, ArtifactValidationError, OSError) as error:
        raise PonFirstSourceError(
            f"pon-first-pass-source-v1: the strict verify of block {plan['block']} "
            f"has already been started; it is not re-run: {error}"
        ) from error
    started, started_real = clock(), realtime()

    def elapsed() -> float:
        return max(clock() - started, realtime() - started_real)

    try:
        document = core.verify_plan(
            plan_path, environment=environment, play_r=play_r, play_d=play_d
        )
        timings = read_json_document(
            Path(plan["artifact_destinations"]["output"]) / "timings.json"
        )
        run_elapsed = run_elapsed_of(timings)
        verify_elapsed = float(elapsed())
        wall = plan["resource_limits"]["wall_seconds"]
        if run_elapsed + verify_elapsed > wall:
            _fail(
                f"run {run_elapsed:.0f} s + verify {verify_elapsed:.0f} s exceeded the "
                f"plan limit of {wall:.0f} s"
            )
        payload = {
            **document,
            "run_elapsed_seconds": run_elapsed,
            "verify_elapsed_seconds": verify_elapsed,
            "wall_seconds": wall,
        }
        record = {**payload, "verify_digest": document_identity(payload)}
        write_new_artifact_file(
            diagnostics / VERIFY_FILENAME, canonical_json_text(record)
        )
    except BaseException as error:
        write_new_artifact_file(
            diagnostics / VERIFY_FAILED_FILENAME,
            canonical_json_text(
                {
                    "elapsed_seconds": float(elapsed()),
                    "error": f"{type(error).__name__}: {error}",
                    "finished_at_utc": _utc_now(),
                    "not_a_result": True,
                    "plan_digest": plan["plan_digest"],
                    "schema": VERIFY_FAILED_SCHEMA,
                    "started_at_utc": started_at,
                }
            ),
        )
        raise
    return record


def _write_once(output: str | Path | None, document: dict) -> None:
    if output is None:
        return
    destination = Path(output)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))


def _estimate(
    plan: dict, timings: dict, branches: int, seat_hanchans: int, basis: dict
) -> dict:
    rows = timings["tasks"]
    values = estimate_values(
        r_walls=[row["wall_seconds"] for row in rows if row["arm"] == ARM_R],
        d_walls=[row["wall_seconds"] for row in rows if row["arm"] == ARM_D],
        branches=branches,
        seat_hanchans=seat_hanchans,
    )
    payload = {
        **values,
        "basis": basis,
        "environment_digest": plan["environment_digest"],
        "execution_target": plan["execution_target"],
        "schema": ESTIMATE_SCHEMA,
    }
    return parse_time_estimate(
        {**payload, "estimate_digest": document_identity(payload)}
    )


def estimate_from_rehearsal(
    observed_plan, unobserved_plan, *, output=None, environment=None
) -> dict:
    """照合済みrehearsalの実測からTRAIN-1の見積もりを作る。"""
    compared = compare_rehearsals(
        observed_plan, unobserved_plan, environment=environment
    )
    plan, summary_document, timings, _, _ = core.verified_plan(
        observed_plan, environment=environment
    )
    if core.spec_of(plan) is not SOURCE:
        _fail("the rehearsal belongs to another protocol")
    document = _estimate(
        plan,
        timings,
        summary_document["population"]["branches"],
        summary_document["seat_hanchans"],
        {
            "kind": "rehearsal",
            "plan_digests": [plan["plan_digest"], compared["unobserved_plan_digest"]],
        },
    )
    _write_once(output, document)
    return document


def estimate_from_block(block_plan, *, output=None) -> dict:
    """完了した直前ブロックの実測（SOURCE-COMPLETE、SHA256SUMS一致）から次のブロックの見積もりを作る。"""
    plan = load_plan(block_plan)
    if plan["run_kind"] != ALLOCATED:
        _fail("the estimate is made from an allocated block")
    completed_block(plan)
    output_dir = Path(plan["artifact_destinations"]["output"])
    try:
        timings = read_json_document(output_dir / "timings.json")
        summary_document = read_json_document(output_dir / "summary.json")
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise PonFirstSourceError(f"cannot read the block output: {error}") from error
    core.require_timings(
        plan,
        timings,
        [(row["arm"], row["seed"], row["branch_ordinal"]) for row in timings["tasks"]],
    )
    document = _estimate(
        plan,
        timings,
        summary_document["population"]["branches"],
        summary_document["seat_hanchans"],
        {"kind": "block", "plan_digests": [plan["plan_digest"]]},
    )
    _write_once(output, document)
    return document


__all__ = [
    "COMPLETE_STATUS",
    "LAYOUT",
    "REHEARSAL_STATUS",
    "SOURCE",
    "compare_rehearsals",
    "estimate_from_block",
    "estimate_from_rehearsal",
    "header_extra",
    "run_plan",
    "summary",
    "verify_plan",
]
