"""Issue #263 pre-execution lock.

The lock is built only on clean reviewed merged main, before any Phase-A result
exposure.  It binds the retained #252 parent artifact, the exact #174 lisjong
revision, one externally audited fresh contiguous Phase-B population, worker
count, write-once destinations, paired statistic, and no-rescue boundary.
"""

from __future__ import annotations

import hashlib
import platform
import sys
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_clean_arena_head,
    require_merged_arena_revision,
    require_new_artifact_destinations,
)
from lisjong_arena.progression_development.paired import (
    artifact_file_digest,
    load_arm_artifact,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .protocol import (
    CANDIDATE_IDENTITY,
    CLASSIFICATION_RULE_ID,
    COMPARATOR_IDENTITY,
    EXECUTION_BRANCH,
    FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
    LISJONG_REVISION,
    MAX_STEPS,
    PARENT_IDENTITY,
    PHASE_A_GAME_COUNT,
    PHASE_A_SEEDS,
    PHASE_B_GAMES_PER_ARM,
    PHASE_B_TOTAL_GAMES,
    ROTATION_COUNT,
    TargetedHonorReleaseProtocolError,
    protocol_document,
    require_exact_candidate_semantics,
    require_exact_comparator,
    require_phase_b_population,
    seed_freshness_block,
)

LOCK_VERSION = 1
EXECUTION_TARGET_TYPE = "reviewed-merged-main-v1"
REQUIRED_DESTINATIONS = (
    "phase_a_diagnostic",
    "candidate_artifact",
    "parent_artifact",
    "paired_result",
    "classified_result",
)
NO_RESCUE_BOUNDARY = (
    "Phase-A #252 diagnostic population is never treated as fresh strength evidence",
    "Phase-B seeds are never extended or replaced after result exposure",
    "no target-gate / R5 / horizon / comparator semantic change",
    "no alternate population or fidelity rescue after result exposure",
    "positive development result is not automatic Champion promotion",
)


class TargetedHonorReleaseLockError(ValueError):
    """Issue #263 pre-execution lock cannot be built or consumed."""


def _identity(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def _runtime_document() -> dict[str, object]:
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "sys_version": sys.version.split()[0],
    }


def _classification_document() -> dict[str, object]:
    return {
        "primary_statistic": (
            "for each locked seed s, mean H focal score across four rotations "
            "minus mean C focal score across the same four rotations"
        ),
        "primary_unit": "ordered seed block",
        "rule_id": CLASSIFICATION_RULE_ID,
        "rule": (
            "normal-approx 95% interval lower > 0 -> SIGNAL; "
            "upper < 0 -> NEGATIVE; otherwise -> INCONCLUSIVE"
        ),
        "secondary_diagnostics_may_alter_classification": False,
    }


def _require_source_parent(path: str | Path):
    artifact = load_arm_artifact(path)
    if artifact.plan.candidate_identity != PARENT_IDENTITY:
        raise TargetedHonorReleaseLockError(
            "source parent artifact is not mechanism-riichi-defense"
        )
    if artifact.plan.seeds != PHASE_A_SEEDS:
        raise TargetedHonorReleaseLockError(
            "source parent artifact is not exact #252 seeds 651..750"
        )
    if artifact.plan.max_steps != MAX_STEPS:
        raise TargetedHonorReleaseLockError(
            "source parent artifact max_steps differs from #252"
        )
    if len(artifact.game_results) != PHASE_A_GAME_COUNT:
        raise TargetedHonorReleaseLockError(
            "source parent artifact must contain exactly 400 games"
        )
    return artifact


def build_lock_document(
    *,
    parent_artifact_path: str | Path,
    destinations: dict[str, str | Path],
    phase_b_seeds: object,
    max_workers: int,
    external_freshness_confirmed: bool,
    additional_allocated_seeds: object = (),
) -> dict[str, object]:
    if type(max_workers) is not int or max_workers <= 0:
        raise TargetedHonorReleaseLockError("max_workers must be a positive int")
    seeds = require_phase_b_population(phase_b_seeds)
    freshness = seed_freshness_block(
        seeds,
        external_freshness_confirmed=external_freshness_confirmed,
        additional_allocated_seeds=additional_allocated_seeds,
    )
    candidate_binding = require_exact_candidate_semantics().to_document()
    comparator_binding = require_exact_comparator()
    source_parent = _require_source_parent(parent_artifact_path)

    try:
        provenance = collect_execution_provenance()
        if provenance.lisjong_revision != LISJONG_REVISION:
            raise TargetedHonorReleaseLockError(
                "installed lisjong revision is not the exact #174 merged revision"
            )
        head = require_clean_arena_head()
        if head != provenance.lisjong_arena_revision:
            raise TargetedHonorReleaseLockError(
                "Arena HEAD differs from collected execution provenance"
            )
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
        require_new_artifact_destinations(
            destinations, required_names=REQUIRED_DESTINATIONS
        )
    except ExecutionSafetyError as exc:
        raise TargetedHonorReleaseLockError(str(exc)) from exc

    payload: dict[str, object] = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in REQUIRED_DESTINATIONS
        },
        "candidate_binding": candidate_binding,
        "classification": _classification_document(),
        "comparator_binding": comparator_binding,
        "execution_target": {
            "branch": EXECUTION_BRANCH,
            "revision": head,
            "target_type": EXECUTION_TARGET_TYPE,
        },
        "lock_version": LOCK_VERSION,
        "no_rescue_boundary": list(NO_RESCUE_BOUNDARY),
        "parent_identity": PARENT_IDENTITY,
        "phase_a": {
            "game_count": PHASE_A_GAME_COUNT,
            "max_workers": max_workers,
            "ordered_seeds": list(PHASE_A_SEEDS),
            "role": "DIAGNOSTIC REUSE ONLY",
            "trajectory_identity_required": True,
            "wall_clock_limit_hours": FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
        },
        "phase_b": {
            "freshness": freshness,
            "games_per_arm": PHASE_B_GAMES_PER_ARM,
            "ordered_seeds": list(seeds),
            "role": "DEVELOPMENT SCREEN",
            "rotation_count": ROTATION_COUNT,
            "total_games": PHASE_B_TOTAL_GAMES,
        },
        "protocol": protocol_document(seeds),
        "provenance": execution_provenance_to_dict(provenance),
        "result_exposed": False,
        "runtime": _runtime_document(),
        "source_parent": {
            "artifact_digest": artifact_file_digest(parent_artifact_path),
            "artifact_path": str(parent_artifact_path),
            "provenance": execution_provenance_to_dict(source_parent.provenance),
        },
    }
    document = dict(payload)
    document["lock_identity"] = _identity(payload)
    return document


def save_lock_document(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    require_new_artifact_destinations(
        {"lock": destination}, required_names=("lock",)
    )
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


_LOCK_FIELDS = {
    "artifact_destinations",
    "candidate_binding",
    "classification",
    "comparator_binding",
    "execution_target",
    "lock_identity",
    "lock_version",
    "no_rescue_boundary",
    "parent_identity",
    "phase_a",
    "phase_b",
    "protocol",
    "provenance",
    "result_exposed",
    "runtime",
    "source_parent",
}


def parse_lock_document(value: object) -> dict[str, object]:
    raw = expect_object(value, _LOCK_FIELDS, "lock")
    if expect_int(raw["lock_version"], "lock.lock_version") != LOCK_VERSION:
        raise TargetedHonorReleaseLockError("unsupported lock version")
    if expect_bool(raw["result_exposed"], "lock.result_exposed"):
        raise TargetedHonorReleaseLockError(
            "pre-execution lock must record result_exposed=false"
        )
    if expect_str(raw["parent_identity"], "lock.parent_identity") != PARENT_IDENTITY:
        raise TargetedHonorReleaseLockError("lock parent identity drifted")

    phase_b = expect_object(
        raw["phase_b"],
        {
            "freshness",
            "games_per_arm",
            "ordered_seeds",
            "role",
            "rotation_count",
            "total_games",
        },
        "lock.phase_b",
    )
    seeds = require_phase_b_population(
        tuple(
            expect_int(item, f"lock.phase_b.ordered_seeds[{index}]")
            for index, item in enumerate(
                expect_list(phase_b["ordered_seeds"], "lock.phase_b.ordered_seeds")
            )
        )
    )
    expected_protocol = protocol_document(seeds)
    if raw["protocol"] != expected_protocol:
        raise TargetedHonorReleaseLockError("lock protocol block drifted")
    if raw["candidate_binding"] != require_exact_candidate_semantics().to_document():
        raise TargetedHonorReleaseLockError("lock candidate binding drifted")
    if raw["comparator_binding"] != require_exact_comparator():
        raise TargetedHonorReleaseLockError("lock comparator binding drifted")
    if raw["classification"] != _classification_document():
        raise TargetedHonorReleaseLockError("lock classification block drifted")
    if raw["no_rescue_boundary"] != list(NO_RESCUE_BOUNDARY):
        raise TargetedHonorReleaseLockError("lock no-rescue boundary drifted")

    phase_a = expect_object(
        raw["phase_a"],
        {
            "game_count",
            "max_workers",
            "ordered_seeds",
            "role",
            "trajectory_identity_required",
            "wall_clock_limit_hours",
        },
        "lock.phase_a",
    )
    if phase_a != {
        "game_count": PHASE_A_GAME_COUNT,
        "max_workers": expect_int(phase_a["max_workers"], "lock.phase_a.max_workers"),
        "ordered_seeds": list(PHASE_A_SEEDS),
        "role": "DIAGNOSTIC REUSE ONLY",
        "trajectory_identity_required": True,
        "wall_clock_limit_hours": FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
    }:
        raise TargetedHonorReleaseLockError("lock Phase-A block drifted")
    if expect_int(phase_a["max_workers"], "lock.phase_a.max_workers") <= 0:
        raise TargetedHonorReleaseLockError("lock max_workers must be positive")

    freshness = expect_object(
        phase_b["freshness"],
        {
            "additional_allocated_seeds",
            "external_collisions",
            "external_freshness_confirmed",
            "fresh",
            "ordered_seeds",
            "repository_collisions",
        },
        "lock.phase_b.freshness",
    )
    if expect_bool(
        freshness["external_freshness_confirmed"],
        "lock.phase_b.freshness.external_freshness_confirmed",
    ) is not True:
        raise TargetedHonorReleaseLockError("external freshness was not confirmed")
    if expect_bool(freshness["fresh"], "lock.phase_b.freshness.fresh") is not True:
        raise TargetedHonorReleaseLockError("locked Phase-B population is not fresh")
    if freshness["ordered_seeds"] != list(seeds):
        raise TargetedHonorReleaseLockError("freshness seed identity drifted")
    if freshness["repository_collisions"] != [] or freshness["external_collisions"] != []:
        raise TargetedHonorReleaseLockError("freshness block contains collisions")

    source = expect_object(
        raw["source_parent"],
        {"artifact_digest", "artifact_path", "provenance"},
        "lock.source_parent",
    )
    source_path = Path(expect_str(source["artifact_path"], "lock.source_parent.artifact_path"))
    source_artifact = _require_source_parent(source_path)
    if expect_str(
        source["artifact_digest"], "lock.source_parent.artifact_digest"
    ) != artifact_file_digest(source_path):
        raise TargetedHonorReleaseLockError("source parent digest changed")
    if parse_execution_provenance(source["provenance"]) != source_artifact.provenance:
        raise TargetedHonorReleaseLockError("source parent provenance changed")

    provenance = parse_execution_provenance(raw["provenance"])
    if provenance.lisjong_revision != LISJONG_REVISION:
        raise TargetedHonorReleaseLockError("lock lisjong revision drifted")

    execution = expect_object(
        raw["execution_target"],
        {"branch", "revision", "target_type"},
        "lock.execution_target",
    )
    if expect_str(execution["branch"], "lock.execution_target.branch") != EXECUTION_BRANCH:
        raise TargetedHonorReleaseLockError("lock execution branch drifted")
    if (
        expect_str(execution["target_type"], "lock.execution_target.target_type")
        != EXECUTION_TARGET_TYPE
    ):
        raise TargetedHonorReleaseLockError("lock execution target type drifted")
    if (
        expect_str(execution["revision"], "lock.execution_target.revision")
        != provenance.lisjong_arena_revision
    ):
        raise TargetedHonorReleaseLockError(
            "lock execution revision differs from provenance"
        )

    destinations = expect_object(
        raw["artifact_destinations"],
        set(REQUIRED_DESTINATIONS),
        "lock.artifact_destinations",
    )
    for name in REQUIRED_DESTINATIONS:
        expect_str(destinations[name], f"lock.artifact_destinations.{name}")

    payload = {key: raw[key] for key in raw if key != "lock_identity"}
    if expect_str(raw["lock_identity"], "lock.lock_identity") != _identity(payload):
        raise TargetedHonorReleaseLockError("lock identity mismatch")
    return dict(raw)


def load_lock_document(path: str | Path) -> dict[str, object]:
    try:
        return parse_lock_document(read_json_document(Path(path)))
    except ArtifactValidationError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise TargetedHonorReleaseLockError(str(exc)) from exc


def require_live_execution_target(
    document: dict[str, object],
) -> SingleRoundExecutionProvenance:
    parsed = parse_lock_document(document)
    locked = parse_execution_provenance(parsed["provenance"])
    try:
        live = collect_execution_provenance()
        head = require_clean_arena_head()
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
    except ExecutionSafetyError as exc:
        raise TargetedHonorReleaseLockError(str(exc)) from exc
    if live != locked or head != locked.lisjong_arena_revision:
        raise TargetedHonorReleaseLockError(
            "live execution target differs from the pre-execution lock"
        )
    return live


def locked_phase_b_seeds(document: dict[str, object]) -> tuple[int, ...]:
    parsed = parse_lock_document(document)
    phase_b = parsed["phase_b"]
    assert isinstance(phase_b, dict)
    return tuple(phase_b["ordered_seeds"])  # type: ignore[arg-type]


def locked_max_workers(document: dict[str, object]) -> int:
    parsed = parse_lock_document(document)
    phase_a = parsed["phase_a"]
    assert isinstance(phase_a, dict)
    return int(phase_a["max_workers"])


def require_locked_destination(
    document: dict[str, object], name: str, path: str | Path
) -> Path:
    parsed = parse_lock_document(document)
    destinations = parsed["artifact_destinations"]
    assert isinstance(destinations, dict)
    if name not in REQUIRED_DESTINATIONS:
        raise TargetedHonorReleaseLockError(f"unknown locked destination {name!r}")
    expected = Path(str(destinations[name]))
    actual = Path(path)
    if actual.resolve(strict=False) != expected.resolve(strict=False):
        raise TargetedHonorReleaseLockError(
            f"destination {name} differs from the pre-execution lock"
        )
    return actual


__all__ = [
    "LOCK_VERSION",
    "NO_RESCUE_BOUNDARY",
    "REQUIRED_DESTINATIONS",
    "TargetedHonorReleaseLockError",
    "build_lock_document",
    "load_lock_document",
    "locked_max_workers",
    "locked_phase_b_seeds",
    "parse_lock_document",
    "require_live_execution_target",
    "require_locked_destination",
    "save_lock_document",
]
