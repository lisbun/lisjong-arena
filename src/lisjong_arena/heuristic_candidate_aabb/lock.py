"""Issue #375 write-once pre-execution lock (``candidate-lock.json``).

participant / population / seed allocation / protocol / provenanceをresult
exposure前に固定する。merged-main execution discipline、environment identity、
execution provenanceは既存helperをそのままreuseする。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_int,
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
from lisjong_arena.artifact import ExecutionProvenance
from lisjong_arena.environment_identity import (
    EnvironmentIdentityError,
    verify_environment,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity
from lisjong_arena.overall_champion_aabb.protocol import (
    IMPLEMENTATION_SOURCES,
    ParticipantBinding,
)
from lisjong_arena.seed_registry import (
    SeedRegistryError,
    require_allocation_binding,
    validate_binding_shape,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)

from .protocol import (
    ALLOCATION_POPULATION,
    ALLOCATION_SPLIT,
    CANDIDATE_ROLE,
    EXECUTION_BRANCH,
    INCUMBENT_ROLE,
    OWNER_ISSUE,
    PROTOCOL_ID,
    SEED_DOMAIN,
    HeuristicCandidateProtocolError,
    parse_participant,
    protocol_document,
    require_participants,
    require_population,
    require_protocol_document,
)

LOCK_VERSION = 1
EXECUTION_TARGET_TYPE = "reviewed-merged-main-v1"
REQUIRED_DESTINATIONS = ("comparison_artifact", "candidate_result")

NO_RESCUE_BOUNDARY = (
    "locked 100 seed blocks are never extended, replaced, or dropped after "
    "result exposure",
    "the AABB rotation plan is never replaced after result exposure",
    "the primary statistic, uma, oka and return points are never changed after "
    "result exposure",
    "the interval method and classification threshold remain fixed",
    "another population is never rerun to break an inconclusive result",
    "partial or failed execution is never partially adopted",
    "secondary diagnostics never override the primary classification",
    "INCONCLUSIVE is a valid terminal outcome, not permission to add seeds",
)

_PROJECT_PATH = Path(__file__).resolve().parents[3] / "pyproject.toml"


class HeuristicCandidateLockError(ValueError):
    """Issue #375 pre-execution lockを生成または消費できない場合。"""


def _runtime_document() -> dict[str, object]:
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "sys_version": sys.version.split()[0],
    }


def _require_environment_consistent() -> None:
    try:
        result = verify_environment(_PROJECT_PATH)
    except EnvironmentIdentityError as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc
    if not result.ok:
        raise HeuristicCandidateLockError(
            "internal VCS dependency environment is inconsistent: "
            + "; ".join(result.errors)
        )


def require_participant_sources(
    candidate: ParticipantBinding,
    incumbent: ParticipantBinding,
    provenance: SingleRoundExecutionProvenance,
) -> None:
    """両participantのimplementation revisionをlive provenanceへ照合する。"""
    for role, binding in zip(
        (CANDIDATE_ROLE, INCUMBENT_ROLE),
        require_participants(candidate, incumbent),
        strict=True,
    ):
        field = IMPLEMENTATION_SOURCES[binding.implementation_source]
        if binding.implementation_revision != getattr(provenance, field):
            raise HeuristicCandidateLockError(
                f"{role} participant implementation revision does not match the "
                f"live {binding.implementation_source} revision"
            )


def require_seed_allocation(
    ledger: object, binding: object, seeds: tuple[int, ...]
) -> dict[str, object]:
    """seed allocation bindingをlive ledger authorityへfail-closedで照合する。"""
    try:
        validated = validate_binding_shape(binding, seeds=seeds)
        require_allocation_binding(
            ledger,
            validated,
            seeds=seeds,
            owner_issue=OWNER_ISSUE,
            protocol=PROTOCOL_ID,
            seed_domain=SEED_DOMAIN,
            population=ALLOCATION_POPULATION,
            split=ALLOCATION_SPLIT,
        )
    except SeedRegistryError as exc:
        raise HeuristicCandidateLockError(
            f"seed allocation authority invalid: {exc}"
        ) from exc
    return dict(validated)


def build_lock_document(
    *,
    destinations: dict[str, str | Path],
    candidate: ParticipantBinding,
    incumbent: ParticipantBinding,
    seeds: object,
    max_workers: int,
    seed_ledger: object,
    allocation_binding: object,
) -> dict[str, object]:
    """result exposure前のlock documentを構築する。"""
    if type(max_workers) is not int or max_workers <= 0:
        raise HeuristicCandidateLockError("max_workers must be a positive int")
    try:
        candidate_binding, incumbent_binding = require_participants(
            candidate, incumbent
        )
        ordered = require_population(seeds)
    except HeuristicCandidateProtocolError as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc
    allocation = require_seed_allocation(seed_ledger, allocation_binding, ordered)

    try:
        _require_environment_consistent()
        provenance = collect_execution_provenance()
        head = require_clean_arena_head()
        if head != provenance.lisjong_arena_revision:
            raise HeuristicCandidateLockError(
                "Arena HEAD differs from collected execution provenance"
            )
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
        require_new_artifact_destinations(
            destinations, required_names=REQUIRED_DESTINATIONS
        )
    except ExecutionSafetyError as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc
    require_participant_sources(candidate_binding, incumbent_binding, provenance)

    payload: dict[str, object] = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in REQUIRED_DESTINATIONS
        },
        "execution_target": {
            "branch": EXECUTION_BRANCH,
            "revision": head,
            "target_type": EXECUTION_TARGET_TYPE,
        },
        "lock_version": LOCK_VERSION,
        "max_workers": max_workers,
        "no_rescue_boundary": list(NO_RESCUE_BOUNDARY),
        "participants": {
            CANDIDATE_ROLE: candidate_binding.to_document(),
            INCUMBENT_ROLE: incumbent_binding.to_document(),
        },
        "protocol": protocol_document(ordered),
        "provenance": execution_provenance_to_dict(provenance),
        "result_exposed": False,
        "runtime": _runtime_document(),
        "seed_allocation_binding": allocation,
    }
    document = dict(payload)
    document["lock_identity"] = document_identity(payload)
    return document


def save_lock_document(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    try:
        require_new_artifact_destinations(
            {"lock": destination}, required_names=("lock",)
        )
    except ExecutionSafetyError as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


_LOCK_FIELDS = {
    "artifact_destinations",
    "execution_target",
    "lock_identity",
    "lock_version",
    "max_workers",
    "no_rescue_boundary",
    "participants",
    "protocol",
    "provenance",
    "result_exposed",
    "runtime",
    "seed_allocation_binding",
}


def parse_lock_document(value: object) -> dict[str, object]:
    """lock documentをstrictに検証する。不明schema / 欠損fieldは拒否する。"""
    try:
        return _parse_lock_document(value)
    except HeuristicCandidateLockError:
        raise
    except (
        HeuristicCandidateProtocolError,
        ArtifactValidationError,
        SeedRegistryError,
        TypeError,
        ValueError,
        KeyError,
    ) as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc


def _parse_lock_document(value: object) -> dict[str, object]:
    raw = expect_object(value, _LOCK_FIELDS, "lock")
    if expect_int(raw["lock_version"], "lock.lock_version") != LOCK_VERSION:
        raise HeuristicCandidateLockError("unsupported lock version")
    if expect_bool(raw["result_exposed"], "lock.result_exposed"):
        raise HeuristicCandidateLockError(
            "pre-execution lock must record result_exposed=false"
        )
    if expect_int(raw["max_workers"], "lock.max_workers") <= 0:
        raise HeuristicCandidateLockError("lock max_workers must be positive")

    seeds = require_protocol_document(raw["protocol"], "lock.protocol")
    validate_binding_shape(raw["seed_allocation_binding"], seeds=seeds)

    participants = expect_object(
        raw["participants"], {CANDIDATE_ROLE, INCUMBENT_ROLE}, "lock.participants"
    )
    require_participants(
        parse_participant(participants[CANDIDATE_ROLE], "lock.participants.candidate"),
        parse_participant(participants[INCUMBENT_ROLE], "lock.participants.incumbent"),
    )
    if raw["no_rescue_boundary"] != list(NO_RESCUE_BOUNDARY):
        raise HeuristicCandidateLockError("lock no-rescue boundary drifted")

    execution_target = expect_object(
        raw["execution_target"],
        {"branch", "revision", "target_type"},
        "lock.execution_target",
    )
    if execution_target["branch"] != EXECUTION_BRANCH:
        raise HeuristicCandidateLockError("lock execution branch drifted")
    if execution_target["target_type"] != EXECUTION_TARGET_TYPE:
        raise HeuristicCandidateLockError("lock execution target type drifted")
    provenance = parse_execution_provenance(raw["provenance"])
    if execution_target["revision"] != provenance.lisjong_arena_revision:
        raise HeuristicCandidateLockError(
            "lock execution revision differs from recorded provenance"
        )

    destinations = expect_object(
        raw["artifact_destinations"],
        set(REQUIRED_DESTINATIONS),
        "lock.artifact_destinations",
    )
    resolved = {
        Path(
            expect_str(destinations[name], f"lock.artifact_destinations.{name}")
        ).resolve(strict=False)
        for name in REQUIRED_DESTINATIONS
    }
    if len(resolved) != len(REQUIRED_DESTINATIONS):
        raise HeuristicCandidateLockError("locked outputs refer to the same path")

    runtime = expect_object(
        raw["runtime"],
        {"python_implementation", "python_version", "sys_version"},
        "lock.runtime",
    )
    for name in ("python_implementation", "python_version", "sys_version"):
        expect_str(runtime[name], f"lock.runtime.{name}")

    payload = {key: raw[key] for key in raw if key != "lock_identity"}
    if expect_str(raw["lock_identity"], "lock.lock_identity") != document_identity(
        payload
    ):
        raise HeuristicCandidateLockError("lock identity mismatch")
    return dict(raw)


def load_lock_document(path: str | Path) -> dict[str, object]:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc
    return parse_lock_document(document)


def locked_participants(
    document: dict[str, object],
) -> tuple[ParticipantBinding, ParticipantBinding]:
    participants = parse_lock_document(document)["participants"]
    assert isinstance(participants, dict)
    return (
        parse_participant(participants[CANDIDATE_ROLE], "lock.participants.candidate"),
        parse_participant(participants[INCUMBENT_ROLE], "lock.participants.incumbent"),
    )


def locked_seeds(document: dict[str, object]) -> tuple[int, ...]:
    protocol = parse_lock_document(document)["protocol"]
    assert isinstance(protocol, dict)
    return require_population(tuple(protocol["ordered_seeds"]))  # type: ignore[arg-type]


def locked_max_workers(document: dict[str, object]) -> int:
    return int(parse_lock_document(document)["max_workers"])  # type: ignore[arg-type]


def locked_destinations(document: dict[str, object]) -> dict[str, Path]:
    raw = parse_lock_document(document)["artifact_destinations"]
    assert isinstance(raw, dict)
    return {name: Path(str(raw[name])) for name in REQUIRED_DESTINATIONS}


def locked_provenance(document: dict[str, object]) -> SingleRoundExecutionProvenance:
    return parse_execution_provenance(parse_lock_document(document)["provenance"])


def require_live_execution_target(
    document: dict[str, object], *, seed_ledger: object
) -> SingleRoundExecutionProvenance:
    """実行直前(と直後)のlive targetがlockと完全一致することを要求する。"""
    parsed = parse_lock_document(document)
    locked = locked_provenance(parsed)
    try:
        _require_environment_consistent()
        live = collect_execution_provenance()
        head = require_clean_arena_head()
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
    except ExecutionSafetyError as exc:
        raise HeuristicCandidateLockError(str(exc)) from exc
    if live != locked or head != locked.lisjong_arena_revision:
        raise HeuristicCandidateLockError(
            "live execution target differs from the pre-execution lock"
        )
    candidate, incumbent = locked_participants(parsed)
    require_participant_sources(candidate, incumbent, live)
    require_seed_allocation(
        seed_ledger, parsed["seed_allocation_binding"], locked_seeds(parsed)
    )
    return live


def require_comparison_provenance(
    document: dict[str, object], provenance: ExecutionProvenance
) -> None:
    """generic comparison artifactのprovenanceをlockとcross-bindする。"""
    if not isinstance(provenance, ExecutionProvenance):
        raise HeuristicCandidateLockError("provenance must be an ExecutionProvenance")
    locked = locked_provenance(document)
    for name in (
        "execution_environment",
        "lisjong_arena_version",
        "lisjong_version",
        "lisjong_revision",
        "riichienv_version",
        "python_version",
    ):
        if getattr(provenance, name) != getattr(locked, name):
            raise HeuristicCandidateLockError(
                f"comparison artifact provenance {name} differs from the lock"
            )


__all__ = [
    "EXECUTION_TARGET_TYPE",
    "LOCK_VERSION",
    "NO_RESCUE_BOUNDARY",
    "REQUIRED_DESTINATIONS",
    "HeuristicCandidateLockError",
    "build_lock_document",
    "document_identity",
    "load_lock_document",
    "locked_destinations",
    "locked_max_workers",
    "locked_participants",
    "locked_provenance",
    "locked_seeds",
    "parse_lock_document",
    "require_comparison_provenance",
    "require_live_execution_target",
    "require_participant_sources",
    "require_seed_allocation",
    "save_lock_document",
]
