"""Issue #270 write-once pre-execution confirmation lock."""

from __future__ import annotations

import hashlib
import platform
import sys
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
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
from lisjong_arena.environment_identity import (
    EnvironmentIdentityError,
    verify_environment,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .protocol import (
    CLASSIFICATION_RULE_ID,
    EXECUTION_BRANCH,
    GAMES_PER_ARM,
    LISJONG_REVISION,
    PARENT_IDENTITY,
    ROLE,
    ROTATION_COUNT,
    SEED_BLOCK_COUNT,
    TOTAL_GAMES,
    protocol_document,
    require_confirmation_population,
    require_exact_candidate_semantics,
    require_exact_comparator,
    seed_freshness_block,
)

LOCK_VERSION = 1
EXECUTION_TARGET_TYPE = "reviewed-merged-main-v1"
REQUIRED_DESTINATIONS = (
    "candidate_artifact",
    "parent_artifact",
    "candidate_trace",
    "paired_result",
    "classified_result",
)
NO_RESCUE_BOUNDARY = (
    "#263 observations are never pooled into #270 confirmation evidence",
    "locked 2,200 confirmation seeds are never extended or replaced after "
    "result exposure",
    "the consumed population is not rerun for scientific rescue after result exposure",
    "confidence level and primary interval method remain fixed",
    "no H/C/R5/target-gate/comparator semantic change",
    "secondary diagnostics never override the primary classification",
    "CONFIRMED POSITIVE is not automatic Champion promotion",
)
_PROJECT_PATH = Path(__file__).resolve().parents[3] / "pyproject.toml"


class TargetedHonorReleaseConfirmationLockError(ValueError):
    """Issue #270 pre-execution lock cannot be built or consumed."""


def document_identity(payload: dict[str, object]) -> str:
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
        "primary_unit": "paired seed block",
        "rule_id": CLASSIFICATION_RULE_ID,
        "rule": (
            "normal-approx 95% interval lower > 0 -> CONFIRMED POSITIVE; "
            "upper < 0 -> CONFIRMED NEGATIVE; otherwise CONFIRMATION INCONCLUSIVE"
        ),
    }


def _require_environment_consistent() -> None:
    try:
        result = verify_environment(_PROJECT_PATH)
    except EnvironmentIdentityError as exc:
        raise TargetedHonorReleaseConfirmationLockError(str(exc)) from exc
    if not result.ok:
        raise TargetedHonorReleaseConfirmationLockError(
            "internal VCS dependency environment is inconsistent: "
            + "; ".join(result.errors)
        )


def build_lock_document(
    *,
    destinations: dict[str, str | Path],
    confirmation_seeds: object,
    max_workers: int,
    external_freshness_confirmed: bool,
    additional_allocated_seeds: object = (),
) -> dict[str, object]:
    if type(max_workers) is not int or max_workers <= 0:
        raise TargetedHonorReleaseConfirmationLockError(
            "max_workers must be a positive int"
        )
    seeds = require_confirmation_population(confirmation_seeds)
    freshness = seed_freshness_block(
        seeds,
        external_freshness_confirmed=external_freshness_confirmed,
        additional_allocated_seeds=additional_allocated_seeds,
    )
    candidate_binding = require_exact_candidate_semantics().to_document()
    comparator_binding = require_exact_comparator()

    try:
        _require_environment_consistent()
        provenance = collect_execution_provenance()
        if provenance.lisjong_revision != LISJONG_REVISION:
            raise TargetedHonorReleaseConfirmationLockError(
                "installed lisjong revision is not the exact #174 merged revision"
            )
        head = require_clean_arena_head()
        if head != provenance.lisjong_arena_revision:
            raise TargetedHonorReleaseConfirmationLockError(
                "Arena HEAD differs from collected execution provenance"
            )
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
        require_new_artifact_destinations(
            destinations, required_names=REQUIRED_DESTINATIONS
        )
    except ExecutionSafetyError as exc:
        raise TargetedHonorReleaseConfirmationLockError(str(exc)) from exc

    payload: dict[str, object] = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in REQUIRED_DESTINATIONS
        },
        "candidate_binding": candidate_binding,
        "classification": _classification_document(),
        "comparator_binding": comparator_binding,
        "confirmation": {
            "freshness": freshness,
            "games_per_arm": GAMES_PER_ARM,
            "max_workers": max_workers,
            "ordered_seeds": list(seeds),
            "role": ROLE,
            "rotation_count": ROTATION_COUNT,
            "seed_block_count": SEED_BLOCK_COUNT,
            "total_games": TOTAL_GAMES,
        },
        "execution_target": {
            "branch": EXECUTION_BRANCH,
            "revision": head,
            "target_type": EXECUTION_TARGET_TYPE,
        },
        "lock_version": LOCK_VERSION,
        "no_rescue_boundary": list(NO_RESCUE_BOUNDARY),
        "parent_identity": PARENT_IDENTITY,
        "protocol": protocol_document(seeds),
        "provenance": execution_provenance_to_dict(provenance),
        "result_exposed": False,
        "runtime": _runtime_document(),
    }
    document = dict(payload)
    document["lock_identity"] = document_identity(payload)
    return document


def save_lock_document(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    require_new_artifact_destinations({"lock": destination}, required_names=("lock",))
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


_LOCK_FIELDS = {
    "artifact_destinations",
    "candidate_binding",
    "classification",
    "comparator_binding",
    "confirmation",
    "execution_target",
    "lock_identity",
    "lock_version",
    "no_rescue_boundary",
    "parent_identity",
    "protocol",
    "provenance",
    "result_exposed",
    "runtime",
}


def parse_lock_document(value: object) -> dict[str, object]:
    raw = expect_object(value, _LOCK_FIELDS, "lock")
    if expect_int(raw["lock_version"], "lock.lock_version") != LOCK_VERSION:
        raise TargetedHonorReleaseConfirmationLockError("unsupported lock version")
    if expect_bool(raw["result_exposed"], "lock.result_exposed"):
        raise TargetedHonorReleaseConfirmationLockError(
            "pre-execution lock must record result_exposed=false"
        )
    if expect_str(raw["parent_identity"], "lock.parent_identity") != PARENT_IDENTITY:
        raise TargetedHonorReleaseConfirmationLockError("lock parent identity drifted")

    confirmation = expect_object(
        raw["confirmation"],
        {
            "freshness",
            "games_per_arm",
            "max_workers",
            "ordered_seeds",
            "role",
            "rotation_count",
            "seed_block_count",
            "total_games",
        },
        "lock.confirmation",
    )
    seeds = require_confirmation_population(
        tuple(
            expect_int(item, f"lock.confirmation.ordered_seeds[{index}]")
            for index, item in enumerate(
                expect_list(
                    confirmation["ordered_seeds"], "lock.confirmation.ordered_seeds"
                )
            )
        )
    )
    if (
        expect_int(confirmation["games_per_arm"], "lock.confirmation.games_per_arm")
        != GAMES_PER_ARM
    ):
        raise TargetedHonorReleaseConfirmationLockError("lock game count drifted")
    if (
        expect_int(confirmation["rotation_count"], "lock.confirmation.rotation_count")
        != ROTATION_COUNT
    ):
        raise TargetedHonorReleaseConfirmationLockError("lock rotation count drifted")
    if (
        expect_int(
            confirmation["seed_block_count"], "lock.confirmation.seed_block_count"
        )
        != SEED_BLOCK_COUNT
    ):
        raise TargetedHonorReleaseConfirmationLockError("lock seed-block count drifted")
    if (
        expect_int(confirmation["total_games"], "lock.confirmation.total_games")
        != TOTAL_GAMES
    ):
        raise TargetedHonorReleaseConfirmationLockError("lock total-game count drifted")
    if expect_str(confirmation["role"], "lock.confirmation.role") != ROLE:
        raise TargetedHonorReleaseConfirmationLockError("lock role drifted")
    max_workers = expect_int(
        confirmation["max_workers"], "lock.confirmation.max_workers"
    )
    if max_workers <= 0:
        raise TargetedHonorReleaseConfirmationLockError(
            "lock max_workers must be positive"
        )

    expected_protocol = protocol_document(seeds)
    if canonical_json_text(raw["protocol"]) != canonical_json_text(expected_protocol):
        raise TargetedHonorReleaseConfirmationLockError("lock protocol block drifted")
    if canonical_json_text(raw["candidate_binding"]) != canonical_json_text(
        require_exact_candidate_semantics().to_document()
    ):
        raise TargetedHonorReleaseConfirmationLockError(
            "lock candidate binding drifted"
        )
    if canonical_json_text(raw["comparator_binding"]) != canonical_json_text(
        require_exact_comparator()
    ):
        raise TargetedHonorReleaseConfirmationLockError(
            "lock comparator binding drifted"
        )
    if canonical_json_text(raw["classification"]) != canonical_json_text(
        _classification_document()
    ):
        raise TargetedHonorReleaseConfirmationLockError(
            "lock classification block drifted"
        )
    if canonical_json_text(raw["no_rescue_boundary"]) != canonical_json_text(
        list(NO_RESCUE_BOUNDARY)
    ):
        raise TargetedHonorReleaseConfirmationLockError(
            "lock no-rescue boundary drifted"
        )

    freshness = expect_object(
        confirmation["freshness"],
        {
            "additional_allocated_seeds",
            "external_audit_scope",
            "external_collisions",
            "external_freshness_confirmed",
            "fresh",
            "ordered_seeds",
            "repository_collisions",
        },
        "lock.confirmation.freshness",
    )
    if freshness["ordered_seeds"] != list(seeds):
        raise TargetedHonorReleaseConfirmationLockError(
            "freshness population differs from locked population"
        )
    if (
        freshness["fresh"] is not True
        or freshness["external_freshness_confirmed"] is not True
    ):
        raise TargetedHonorReleaseConfirmationLockError(
            "lock freshness confirmation is not affirmative"
        )
    if (
        freshness["repository_collisions"] != []
        or freshness["external_collisions"] != []
    ):
        raise TargetedHonorReleaseConfirmationLockError(
            "lock contains a seed collision"
        )

    execution_target = expect_object(
        raw["execution_target"],
        {"branch", "revision", "target_type"},
        "lock.execution_target",
    )
    if (
        expect_str(execution_target["branch"], "lock.execution_target.branch")
        != EXECUTION_BRANCH
    ):
        raise TargetedHonorReleaseConfirmationLockError("lock branch drifted")
    if (
        expect_str(
            execution_target["target_type"], "lock.execution_target.target_type"
        )
        != EXECUTION_TARGET_TYPE
    ):
        raise TargetedHonorReleaseConfirmationLockError(
            "lock execution target type drifted"
        )

    provenance = parse_execution_provenance(raw["provenance"])
    if provenance.lisjong_revision != LISJONG_REVISION:
        raise TargetedHonorReleaseConfirmationLockError(
            "lock lisjong revision is not exact #174 revision"
        )
    if execution_target["revision"] != provenance.lisjong_arena_revision:
        raise TargetedHonorReleaseConfirmationLockError(
            "lock execution revision differs from provenance"
        )

    destinations = expect_object(
        raw["artifact_destinations"],
        set(REQUIRED_DESTINATIONS),
        "lock.artifact_destinations",
    )
    for name in REQUIRED_DESTINATIONS:
        expect_str(destinations[name], f"lock.artifact_destinations.{name}")

    runtime = expect_object(
        raw["runtime"],
        {"python_implementation", "python_version", "sys_version"},
        "lock.runtime",
    )
    for name in ("python_implementation", "python_version", "sys_version"):
        expect_str(runtime[name], f"lock.runtime.{name}")

    payload = {key: raw[key] for key in raw if key != "lock_identity"}
    if (
        expect_str(raw["lock_identity"], "lock.lock_identity")
        != document_identity(payload)
    ):
        raise TargetedHonorReleaseConfirmationLockError("lock identity mismatch")
    return dict(raw)


def load_lock_document(path: str | Path) -> dict[str, object]:
    try:
        return parse_lock_document(read_json_document(Path(path)))
    except ArtifactValidationError as exc:
        raise TargetedHonorReleaseConfirmationLockError(str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        raise TargetedHonorReleaseConfirmationLockError(str(exc)) from exc


def require_live_execution_target(
    document: dict[str, object],
) -> SingleRoundExecutionProvenance:
    parsed = parse_lock_document(document)
    locked = parse_execution_provenance(parsed["provenance"])
    try:
        _require_environment_consistent()
        live = collect_execution_provenance()
        head = require_clean_arena_head()
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
    except ExecutionSafetyError as exc:
        raise TargetedHonorReleaseConfirmationLockError(str(exc)) from exc
    if live != locked or head != locked.lisjong_arena_revision:
        raise TargetedHonorReleaseConfirmationLockError(
            "live execution target differs from the pre-execution lock"
        )
    return live


def locked_confirmation_seeds(document: dict[str, object]) -> tuple[int, ...]:
    parsed = parse_lock_document(document)
    confirmation = parsed["confirmation"]
    assert isinstance(confirmation, dict)
    return tuple(confirmation["ordered_seeds"])  # type: ignore[arg-type]


def locked_max_workers(document: dict[str, object]) -> int:
    parsed = parse_lock_document(document)
    confirmation = parsed["confirmation"]
    assert isinstance(confirmation, dict)
    return int(confirmation["max_workers"])


def locked_destinations(document: dict[str, object]) -> dict[str, Path]:
    parsed = parse_lock_document(document)
    raw = parsed["artifact_destinations"]
    assert isinstance(raw, dict)
    return {name: Path(str(raw[name])) for name in REQUIRED_DESTINATIONS}


def require_locked_destination(
    document: dict[str, object], name: str, path: str | Path
) -> Path:
    destinations = locked_destinations(document)
    if name not in REQUIRED_DESTINATIONS:
        raise TargetedHonorReleaseConfirmationLockError(
            f"unknown locked destination {name!r}"
        )
    expected = destinations[name]
    actual = Path(path)
    if actual.resolve(strict=False) != expected.resolve(strict=False):
        raise TargetedHonorReleaseConfirmationLockError(
            f"destination {name} differs from the pre-execution lock"
        )
    return actual


__all__ = [
    "LOCK_VERSION",
    "NO_RESCUE_BOUNDARY",
    "REQUIRED_DESTINATIONS",
    "TargetedHonorReleaseConfirmationLockError",
    "build_lock_document",
    "document_identity",
    "load_lock_document",
    "locked_confirmation_seeds",
    "locked_destinations",
    "locked_max_workers",
    "parse_lock_document",
    "require_live_execution_target",
    "require_locked_destination",
    "save_lock_document",
]
