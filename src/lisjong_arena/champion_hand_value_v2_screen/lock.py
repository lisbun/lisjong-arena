"""Pre-execution lock for Issue #297."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_clean_arena_head,
    require_merged_arena_revision,
    require_new_artifact_destinations,
)
from lisjong_arena._parallel_execution import validate_max_workers
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    collect_execution_provenance,
    execution_provenance_to_dict,
)

from .protocol import (
    BASELINE_CLASS_NAME,
    BASELINE_IDENTITY,
    BASELINE_SOURCE_MODULE,
    CANDIDATE_CLASS_NAME,
    CANDIDATE_IDENTITY,
    CANDIDATE_SOURCE_MODULE,
    CLASSIFICATION_RULE_ID,
    GAME_MODE,
    LISJONG_ENGINE_REVISION,
    LISJONG_REVISION,
    RIICHIENV_VERSION,
    ROTATION_COUNT,
    SEED_BLOCK_COUNT,
    TOTAL_GAMES,
    protocol_document,
    require_exact_policy_semantics,
    require_population,
    seed_freshness_block,
)

LOCK_VERSION = 1
EXECUTION_BRANCH = "main"
REQUIRED_DESTINATIONS = (
    "strength_artifact",
    "composition_trace",
    "result",
    "classified_result",
)


class ChampionHandValueV2LockError(ValueError):
    """Issue #297 pre-execution lock is malformed or cannot be honored."""


def _identity(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        canonical_json_text(dict(payload)).encode("utf-8")
    ).hexdigest()


def _validate_provenance(
    provenance: SingleRoundExecutionProvenance,
    *,
    head: str,
) -> None:
    if provenance.lisjong_arena_revision != head:
        raise ChampionHandValueV2LockError(
            "Arena provenance differs from clean execution HEAD"
        )
    if provenance.lisjong_revision != LISJONG_REVISION:
        raise ChampionHandValueV2LockError(
            "lisjong revision must equal exact #183 merged revision"
        )
    if provenance.lisjong_engine_revision != LISJONG_ENGINE_REVISION:
        raise ChampionHandValueV2LockError("lisjong-engine revision drifted")
    if provenance.riichienv_version != RIICHIENV_VERSION:
        raise ChampionHandValueV2LockError("RiichiEnv version drifted")


def build_lock_document(
    *,
    destinations: Mapping[str, str | Path],
    seeds: object,
    max_workers: int,
    external_freshness_confirmed: bool,
    additional_allocated_seeds: object = (),
) -> dict[str, object]:
    require_exact_policy_semantics()
    ordered = require_population(seeds)
    validate_max_workers(max_workers)
    freshness = seed_freshness_block(
        ordered,
        external_freshness_confirmed=external_freshness_confirmed,
        additional_allocated_seeds=additional_allocated_seeds,
    )

    if set(destinations) != set(REQUIRED_DESTINATIONS):
        raise ChampionHandValueV2LockError(
            "artifact destinations must match the fixed #297 contract"
        )
    normalized_destinations = {
        name: str(Path(destinations[name])) for name in REQUIRED_DESTINATIONS
    }
    try:
        require_new_artifact_destinations(
            normalized_destinations,
            required_names=REQUIRED_DESTINATIONS,
        )
        head = require_clean_arena_head()
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
    except ExecutionSafetyError as exc:
        raise ChampionHandValueV2LockError(str(exc)) from exc

    provenance = collect_execution_provenance()
    _validate_provenance(provenance, head=head)

    payload: dict[str, object] = {
        "lock_version": LOCK_VERSION,
        "result_exposed": False,
        "execution_target": {
            "branch": EXECUTION_BRANCH,
            "arena_revision": head,
            "type": "reviewed-merged-main-v1",
        },
        "provenance": execution_provenance_to_dict(provenance),
        "protocol": protocol_document(ordered),
        "freshness": freshness,
        "worker_count": max_workers,
        "participants": {
            "candidate": {
                "identity": CANDIDATE_IDENTITY,
                "class": CANDIDATE_CLASS_NAME,
                "module": CANDIDATE_SOURCE_MODULE,
            },
            "baseline": {
                "identity": BASELINE_IDENTITY,
                "class": BASELINE_CLASS_NAME,
                "module": BASELINE_SOURCE_MODULE,
            },
        },
        "scientific_contract": {
            "seed_block_count": SEED_BLOCK_COUNT,
            "rotation_count": ROTATION_COUNT,
            "total_games": TOTAL_GAMES,
            "game_mode": GAME_MODE,
            "primary_unit": "ordered seed block",
            "classification_rule_id": CLASSIFICATION_RULE_ID,
            "no_rescue": (
                "no seed extension/replacement, comparator substitution, "
                "threshold change, or same-population rescue after result exposure"
            ),
        },
        "destinations": normalized_destinations,
    }
    document = dict(payload)
    document["lock_identity"] = _identity(payload)
    return document


def save_lock_document(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    try:
        require_new_artifact_destinations(
            {"lock": destination},
            required_names=("lock",),
        )
    except ExecutionSafetyError as exc:
        raise ChampionHandValueV2LockError(str(exc)) from exc
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def _parse_lock_document(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ChampionHandValueV2LockError("lock must be a JSON object")
    raw = dict(value)
    expected = {
        "lock_version",
        "result_exposed",
        "execution_target",
        "provenance",
        "protocol",
        "freshness",
        "worker_count",
        "participants",
        "scientific_contract",
        "destinations",
        "lock_identity",
    }
    if set(raw) != expected:
        raise ChampionHandValueV2LockError("lock fields differ from contract")
    if raw["lock_version"] != LOCK_VERSION:
        raise ChampionHandValueV2LockError("unsupported lock version")
    if raw["result_exposed"] is not False:
        raise ChampionHandValueV2LockError(
            "pre-execution lock must have result_exposed=false"
        )
    if type(raw["worker_count"]) is not int or raw["worker_count"] <= 0:
        raise ChampionHandValueV2LockError("worker_count must be positive")
    protocol = raw["protocol"]
    if type(protocol) is not dict:
        raise ChampionHandValueV2LockError("protocol block is invalid")
    seeds = protocol.get("ordered_seeds")
    require_population(seeds)
    destinations = raw["destinations"]
    if type(destinations) is not dict or set(destinations) != set(
        REQUIRED_DESTINATIONS
    ):
        raise ChampionHandValueV2LockError("destination block is invalid")
    if any(
        type(destinations[name]) is not str or not destinations[name]
        for name in REQUIRED_DESTINATIONS
    ):
        raise ChampionHandValueV2LockError("destination paths are invalid")
    payload = {key: raw[key] for key in raw if key != "lock_identity"}
    if type(raw["lock_identity"]) is not str or raw["lock_identity"] != _identity(
        payload
    ):
        raise ChampionHandValueV2LockError("lock identity mismatch")
    return raw


def load_lock_document(path: str | Path) -> dict[str, object]:
    try:
        return _parse_lock_document(read_json_document(Path(path)))
    except ArtifactValidationError as exc:
        raise ChampionHandValueV2LockError(str(exc)) from exc
    except (TypeError, ValueError, KeyError) as exc:
        if isinstance(exc, ChampionHandValueV2LockError):
            raise
        raise ChampionHandValueV2LockError(str(exc)) from exc


def locked_seeds(lock: Mapping[str, object]) -> tuple[int, ...]:
    protocol = lock["protocol"]
    if type(protocol) is not dict:
        raise ChampionHandValueV2LockError("protocol block is invalid")
    return require_population(protocol["ordered_seeds"])


def locked_max_workers(lock: Mapping[str, object]) -> int:
    value = lock["worker_count"]
    if type(value) is not int or value <= 0:
        raise ChampionHandValueV2LockError("worker_count is invalid")
    return value


def locked_destinations(lock: Mapping[str, object]) -> dict[str, Path]:
    raw = lock["destinations"]
    if type(raw) is not dict:
        raise ChampionHandValueV2LockError("destinations are invalid")
    return {name: Path(raw[name]) for name in REQUIRED_DESTINATIONS}


def require_live_execution_target(
    lock: Mapping[str, object],
) -> SingleRoundExecutionProvenance:
    try:
        head = require_clean_arena_head()
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
    except ExecutionSafetyError as exc:
        raise ChampionHandValueV2LockError(str(exc)) from exc
    execution_target = lock["execution_target"]
    if type(execution_target) is not dict:
        raise ChampionHandValueV2LockError("execution target is invalid")
    if head != execution_target.get("arena_revision"):
        raise ChampionHandValueV2LockError(
            "live Arena HEAD differs from locked merged main revision"
        )
    live = collect_execution_provenance()
    _validate_provenance(live, head=head)
    if execution_provenance_to_dict(live) != lock["provenance"]:
        raise ChampionHandValueV2LockError(
            "live execution provenance differs from pre-execution lock"
        )
    require_exact_policy_semantics()
    return live


__all__ = [
    "ChampionHandValueV2LockError",
    "LOCK_VERSION",
    "REQUIRED_DESTINATIONS",
    "build_lock_document",
    "load_lock_document",
    "locked_destinations",
    "locked_max_workers",
    "locked_seeds",
    "require_live_execution_target",
    "save_lock_document",
]
