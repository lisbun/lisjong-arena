"""#480: read-only S1/S2 reservation preparation using the existing registry.

This module neither dispatches workflows nor grants execution authority. S3 is
capacity-checked only; formal seeds are never emitted as reservation requests.
"""

import argparse
import re
import sys
from pathlib import Path

from lisjong_arena import seed_registry as registry
from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_object,
    read_json_document,
    write_new_artifact_file,
)

OWNER = "lisbun/lisjong-arena#480"
STAGES = ("S1", "S2", "S3")
SPLITS = {"S1": "DEVELOPMENT-A", "S2": "DEVELOPMENT-B"}
SCHEMA = "arena-strength-loop-seed-preparation-v1"


def _positive(value, name):
    if type(value) is not int or value <= 0:
        raise ArtifactValidationError(f"{name} must be a positive integer")
    return value


def _spec(value):
    spec = expect_object(
        value,
        {
            "schema",
            "run_id",
            "arena_revision",
            "authority_commit",
            "protocol",
            "protocol_revision",
            "provenance_reference",
            "pools",
            "counts",
            "formal_min_remaining",
        },
        "seed preparation spec",
    )
    if spec["schema"] != SCHEMA:
        raise ArtifactValidationError("unsupported seed preparation schema")
    for name in ("arena_revision", "authority_commit"):
        if type(spec[name]) is not str or not re.fullmatch(r"[0-9a-f]{40}", spec[name]):
            raise ArtifactValidationError(f"{name} must be an exact commit SHA")
    for name in ("run_id", "protocol", "protocol_revision"):
        if type(spec[name]) is not str or not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,127}", spec[name]
        ):
            raise ArtifactValidationError(f"{name} must be a stable identifier")
    reference = spec["provenance_reference"]
    if type(reference) is not str or not reference.strip():
        raise ArtifactValidationError("provenance_reference is required")
    pools = expect_object(spec["pools"], set(STAGES), "pools")
    counts = expect_object(spec["counts"], set(SPLITS), "counts")
    seen = set()
    for stage in STAGES:
        if type(pools[stage]) is not list:
            raise ArtifactValidationError("pool membership must be an array")
        members = registry.seeds_from_membership(
            registry.seed_membership_document(pools[stage])
        )
        if seen.intersection(members):
            raise ArtifactValidationError("S1/S2/S3 pools must be disjoint")
        seen.update(members)
        if stage in counts:
            _positive(counts[stage], stage)
    _positive(spec["formal_min_remaining"], "formal_min_remaining")
    return spec


def prepare(spec, ledger):
    """Select smallest available members of explicit pools without reserving them.

    Pool approval and live-ledger acquisition belong to the caller. RESERVED,
    COMMITTED, RETIRED and legacy quarantine all consume capacity permanently.
    """
    spec = _spec(spec)
    ledger = registry.validate_ledger(ledger)
    occupied = set()
    for row in ledger["allocations"]:
        if row["seed_domain"] in (
            registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
            registry.LEGACY_SEED_DOMAIN,
        ):
            occupied.update(registry.seeds_from_membership(row["seed_membership"]))
    available = {
        stage: sorted(set(spec["pools"][stage]) - occupied) for stage in STAGES
    }
    if len(available["S3"]) < spec["formal_min_remaining"]:
        raise ArtifactValidationError("STOP: formal seed capacity below threshold")
    requests = {}
    for stage, split in SPLITS.items():
        count = spec["counts"][stage]
        if len(available[stage]) < count:
            raise ArtifactValidationError(
                f"STOP: insufficient {stage} development seeds"
            )
        members = available[stage][:count]
        requests[stage] = {
            "operation": "reserve",
            "owner_issue": OWNER,
            "protocol": spec["protocol"],
            "seed_domain": registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
            "purpose": f"strength-loop {spec['run_id']} {stage} development selection",
            "population": f"strength-loop-{spec['run_id']}-{stage.lower()}",
            "split": split,
            "seeds": ",".join(map(str, members)),
            "arena_revision": spec["arena_revision"],
            "protocol_revision": spec["protocol_revision"],
            "provenance_reference": spec["provenance_reference"],
        }
    return {
        "schema": SCHEMA,
        "status": "PREPARED_NOT_RESERVED",
        "authority_commit": spec["authority_commit"],
        "ledger_revision": registry.ledger_revision(ledger),
        "available_counts": {stage: len(available[stage]) for stage in STAGES},
        "requests": requests,
    }


def bind(spec, planning_ledger, current_ledger, allocation_ids):
    """Bind workflow-created reservations; never reinterpret a competing allocation.

    Reconstruct requests from the retained planning snapshot. Recheck formal
    capacity in the new snapshot; unrelated reservations do not invalidate the
    binding. Only fresh RESERVED rows may enter a new protocol lock.
    """
    plan = prepare(spec, planning_ledger)
    expect_object(allocation_ids, set(SPLITS), "allocation IDs")
    registry.validate_branch_against_base(current_ledger, planning_ledger)
    occupied_formal = registry.collision_records(
        current_ledger,
        seed_domain=registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        seeds=spec["pools"]["S3"],
    )
    consumed = {seed for row in occupied_formal for seed in row["seeds"]}
    if len(spec["pools"]["S3"]) - len(consumed) < spec["formal_min_remaining"]:
        raise ArtifactValidationError("STOP: formal seed capacity below threshold")
    bindings = {}
    for stage, request in plan["requests"].items():
        identity = allocation_ids[stage]
        if type(identity) is not str:
            raise ArtifactValidationError("allocation ID must be a string")
        binding = registry.allocation_binding(current_ledger, identity)
        row = registry.require_allocation_binding(
            current_ledger,
            binding,
            seeds=registry.parse_seed_spec(request["seeds"]),
            **{
                name: request[name]
                for name in (
                    "owner_issue",
                    "protocol",
                    "seed_domain",
                    "population",
                    "split",
                )
            },
        )
        if row["state"] != registry.RESERVED:
            raise ArtifactValidationError("new loop lock requires RESERVED allocations")
        for name in (
            "arena_revision",
            "protocol_revision",
            "provenance_reference",
            "purpose",
        ):
            if row[name] != request[name]:
                raise ArtifactValidationError(f"allocation {name} differs from request")
        bindings[stage] = binding
    return {
        "schema": SCHEMA,
        "status": "RESERVED_NOT_EXECUTED",
        "planning_ledger_revision": plan["ledger_revision"],
        "current_ledger_revision": registry.ledger_revision(current_ledger),
        "bindings": bindings,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "bind"))
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--planning-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--current-ledger", type=Path)
    parser.add_argument("--allocation-ids", type=Path)
    args = parser.parse_args(argv)
    try:
        spec = read_json_document(args.spec)
        ledger = registry.load_ledger(args.planning_ledger)
        if args.operation == "prepare":
            if args.current_ledger is not None or args.allocation_ids is not None:
                raise ArtifactValidationError("prepare does not accept binding inputs")
            result = prepare(spec, ledger)
        else:
            if args.current_ledger is None or args.allocation_ids is None:
                raise ArtifactValidationError(
                    "bind needs current ledger and allocation IDs"
                )
            result = bind(
                spec,
                ledger,
                registry.load_ledger(args.current_ledger),
                read_json_document(args.allocation_ids),
            )
        serialized = canonical_json_text(result)
        write_new_artifact_file(args.output, serialized)
        if read_json_document(args.output) != result:
            raise ArtifactValidationError("output readback mismatch")
        print(serialized, end="")
    except (ValueError, OSError) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
