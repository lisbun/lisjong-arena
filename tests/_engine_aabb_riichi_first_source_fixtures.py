"""#548 riichi-first-dama-source-v1のtest用builder・fake play。

R・D armの記録は#544のfixture（行動記録とdigestが整合する48判断、K0・K9の2分岐）を使う。
"""

import _engine_aabb_riichi_first_dama_fixtures as pilot
from _engine_aabb_protocol_fixtures import (
    ARENA_REVISION,
    ENVIRONMENT,
    LISJONG_REVISION,
    WHEEL_PATH,
)

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb import (
    riichi_first_dama_source_plan as plan_module,
)
from lisjong_arena.heuristic_candidate_engine_aabb.lock import EXECUTION_BRANCH
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import SEED_DOMAIN
from lisjong_arena.overall_champion_aabb.lock import document_identity

__all__ = ["ARENA_REVISION", "ENVIRONMENT", "LISJONG_REVISION", "WHEEL_PATH"]

OWNER = "lisbun/lisjong-arena#549"
ARTIFACT = "a" * 64
TARGET = {"branch": EXECUTION_BRANCH, "revision": ARENA_REVISION}


def allocation(
    split: str, *, seeds=None, owner=OWNER, population=plan_module.POPULATION
):
    return seed_registry.build_allocation_record(
        owner_issue=owner,
        protocol=plan_module.ALLOCATION_PROTOCOL,
        seed_domain=SEED_DOMAIN,
        purpose="test",
        population=population,
        split=split,
        seeds=seeds or plan_module.SPLIT_SEEDS[split],
        arena_revision="e" * 40,
        protocol_revision="test",
        provenance_reference="test",
        allocation_timestamp="2026-10-27T00:00:00Z",
    )


TRAIN_RECORD = allocation("TRAIN")
TEST_RECORD = allocation("TEST")


def ledger(*records):
    document = seed_registry.new_ledger()
    document["allocations"] = list(records or (TRAIN_RECORD, TEST_RECORD))
    document["allocations"].sort(key=seed_registry._sort_key)
    return seed_registry.validate_ledger(document)


def binding(document, record):
    return seed_registry.allocation_binding(document, record["allocation_identity"])


def time_estimate(
    *, basis=None, r_wall=45.0, d_wall=24.0, branches=23, r_hanchans=2, **changes
):
    values = plan_module.estimate_values(
        r_walls=[r_wall] * r_hanchans,
        d_walls=[d_wall] * branches,
        branches=branches,
        seat_hanchans=4 * r_hanchans,
    )
    payload = {
        **values,
        "basis": basis or {"kind": "rehearsal", "plan_digests": ["1" * 64, "2" * 64]},
        "environment_digest": document_identity(ENVIRONMENT),
        "execution_target": TARGET,
        "schema": plan_module.ESTIMATE_SCHEMA,
    }
    payload.update(changes)
    return {**payload, "estimate_digest": document_identity(payload)}


def block_estimate(previous_plan: dict, **kwargs):
    return time_estimate(
        basis={"kind": "block", "plan_digests": [previous_plan["plan_digest"]]},
        **kwargs,
    )


def plan_document(
    tmp,
    *,
    block="TRAIN-1",
    run_kind="allocated",
    observation="observed",
    name=None,
    **overrides,
):
    document = overrides.pop("ledger", None) or ledger()
    allocated = run_kind == "allocated"
    split = plan_module.BLOCKS[block][0] if allocated else None
    record = TRAIN_RECORD if split == "TRAIN" else TEST_RECORD
    overrides.setdefault(
        "allocation_binding", binding(document, record) if allocated else None
    )
    overrides.setdefault("artifact_identity", ARTIFACT if split == "TEST" else None)
    overrides.setdefault("time_estimate", time_estimate() if allocated else None)
    overrides.setdefault("session_elapsed_seconds", 1000.0 if allocated else None)
    overrides.setdefault("collected_before_seconds", 0.0 if allocated else None)
    overrides.setdefault("previous_blocks", [] if allocated else None)
    label = name or f"{block if allocated else 'rehearsal'}-{observation}"
    arguments = {
        "run_kind": run_kind,
        "observation": observation,
        "owner_issue": OWNER,
        "block": block if allocated else None,
        "limit_enforcement": "synthetic test value",
        "wheel_path": WHEEL_PATH,
        "destinations": {
            "diagnostics": str(tmp / f"{label}-diagnostics"),
            "output": str(tmp / f"{label}-output"),
        },
        "environment": ENVIRONMENT,
        "arena_revision": ARENA_REVISION,
        "ledger": document,
    }
    arguments.update(overrides)
    return plan_module.assemble_plan(**arguments)


def write_plan(tmp, **kwargs):
    document = plan_document(tmp, **kwargs)
    label = kwargs.get("name") or (
        f"{kwargs.get('block', 'TRAIN-1')}-{kwargs.get('observation', 'observed')}"
        if kwargs.get("run_kind", "allocated") == "allocated"
        else f"rehearsal-{kwargs.get('observation', 'observed')}"
    )
    path = tmp / f"{label}.plan.json"
    plan_module.save_plan(document, path)
    return path, document


def gate(plan, *, ledger):
    plan_module.require_live_target(
        plan, ledger=ledger, environment=ENVIRONMENT, head=ARENA_REVISION
    )


fake_r_both = pilot.fake_r_both
fake_r_unobserved = pilot.fake_r_unobserved
fake_d_better = pilot.fake_d_better
fake_d_riichi_at_branch = pilot.fake_d_riichi_at_branch
read_lines = pilot.read_lines


def fake_r_failure(task):
    if task.seed == plan_module.BLOCKS["TRAIN-1"][1][2]:
        raise RuntimeError("synthetic worker failure")
    return fake_r_both(task)


def fake_d_riichi_first_seed(task):
    """最初のseedのK0分岐だけ、分岐点の判断でリーチが適用された（INVALID）。"""
    if task.branch_ordinal == 0:
        return pilot._d_played(
            task, "better", branch_action=pilot.encode_action(pilot.RIICHI)
        )
    return fake_d_better(task)
