"""#548 riichi-first-dama-source-v1: R1学習sourceの実行・照合・summary・strict verify・見積もり。

#544（pilot）の``riichi_first_dama``の観測・照合・行動記録・2 seed再実行verifyをそのまま使い
（``ProtocolSpec``で登録）、このprotocolのschema・header・summaryだけを与える。

- branches / actions / featuresのheaderは、pilotのfieldに ``allocation_identity`` ・ ``artifact_identity`` ・
  ``block`` ・ ``split`` を加える（lisjong #360の`docs/r1-value-learning.md` §2の契約）
- summaryはΔの統計・分散・平均・分岐ごとのΔを出さない。判定はSOURCE-COMPLETE / STOP-INVALID
  （未完了はSTOP-INCOMPLETEのstop記録だけ）
- 見積もりは、TRAIN-1はrehearsal、以後は直前ブロックの実測から作る
"""

from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from . import riichi_first_dama as core
from .kyoku_record import PROTOCOL_ID, RecordLayout
from .paijia_tie_plan import ALLOCATED, distribution, memory_summary
from .riichi_dama_branch import ARM_D, ARM_R, _validate_keys, _validate_seats
from .riichi_first_dama_source_plan import (
    ESTIMATE_SCHEMA,
    EVALUATION_KIND,
    KYOKU_RECORD_SCHEMA,
    PLAN_SCHEMA,
    PROTOCOL_VERSION,
    SCHEMAS,
    SOURCE_COMPLETE,
    STOP_INVALID,
    RiichiFirstSourceError,
    completed_block,
    estimate_values,
    load_plan,
    parse_time_estimate,
    require_live_target,
)
from .riichi_first_dama_worker import WRAPPER_VERSION

COMPLETE_STATUS = (
    "R1 LEARNING SOURCE BLOCK COMPLETE; DEVELOPMENT SOURCE, NO DELTA STATISTIC, "
    "NOT A STRENGTH RESULT"
)
REHEARSAL_STATUS = "REHEARSAL COMPLETE; NO SOURCE BLOCK"

LAYOUT = RecordLayout(
    protocol_id=PROTOCOL_ID,
    kyoku_record_schema=KYOKU_RECORD_SCHEMA,
    event_fact_schema="arena-riichi-first-dama-source-v1-event-fact-v1",
    manifest_schema="arena-riichi-first-dama-source-v1-record-manifest-v1",
    key_fields=("placement",),
    validate_keys=_validate_keys,
    validate_seats=_validate_seats,
)


def _fail(message: str):
    raise RiichiFirstSourceError(f"riichi-first-dama-source-v1: {message}")


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
    """Δの統計を持たないsummary（品質・時間・INVALIDだけ）。"""
    hanchans = common["hanchans"]
    timings = common["timings"]
    allocated = plan["run_kind"] == ALLOCATED
    decision = (STOP_INVALID if invalid else SOURCE_COMPLETE) if allocated else None
    return {
        "block": plan["block"],
        "branches": len(evaluated),
        "decision": decision,
        "decisions": sum(row["decisions"] for row in hanchans["hanchans"]),
        "evaluation_kind": EVALUATION_KIND,
        "hanchan_count": len(hanchans["hanchans"]),
        "inputs": common["inputs"],
        "invalid": {
            "branches": [
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
        "protocol_version": PROTOCOL_VERSION,
        "quality": {
            "resumed_riichi_branches": sum(
                1 for row in valid if row["first_riichi_after_branch"] is not None
            ),
            "riichi_declined": sum(
                sum(row["riichi_declined"]) for row in hanchans["hanchans"]
            ),
            "valid_branches": len(valid),
        },
        "run_kind": plan["run_kind"],
        "schema": SCHEMAS["summary"],
        "seat_hanchans": 4 * len(hanchans["hanchans"]),
        "split": plan["split"],
        "status": COMPLETE_STATUS if allocated else REHEARSAL_STATUS,
        "timing": {
            "d_arm_wall_seconds": distribution(
                [row["wall_seconds"] for row in timings["tasks"] if row["arm"] == ARM_D]
            ),
            "r_hanchan_wall_seconds": distribution(
                [row["wall_seconds"] for row in timings["tasks"] if row["arm"] == ARM_R]
            ),
            "schedule_wall_seconds": timings["schedule_wall_seconds"],
        },
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
verify_plan = core.verify_plan
compare_rehearsals = core.compare_rehearsals


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
        summary_document["branches"],
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
        raise RiichiFirstSourceError(
            f"cannot read the block output: {error}"
        ) from error
    core.require_timings(
        plan,
        timings,
        [(row["arm"], row["seed"], row["branch_ordinal"]) for row in timings["tasks"]],
    )
    document = _estimate(
        plan,
        timings,
        summary_document["branches"],
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
