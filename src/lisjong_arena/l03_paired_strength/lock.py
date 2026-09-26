"""#385 write-once pre-execution lock（``paired-strength-lock.json``）。

lockは実行hostで、result exposure前に1回だけ作る。次をbindする。

- protocol v1 block（parent、candidate / baseline / opponent identity、backend /
  rules、revision、seed population、pairing、budget、endpoint、区間法、
  invalid handling、terminal rule）
- frozen game scheduleのdigest
- live Seed Registry allocation binding（owner / protocol / population / split /
  engine seed domain / membership）とそのallocationの``arena_revision``
- 実行target（merged ``main``のclean Arena HEAD）とinstalled lisjong /
  lisjong-engine / torch / Python
- candidate artifactのfile digest / artifact identity / runtime identity
- write-once result destination
- ``result_exposed = false``

strict readback（``require_live_target``）は同じinputからlive環境でpayloadを
再構成し、lockとの完全一致を要求する。
"""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Callable
from importlib import metadata
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
from lisjong_arena.seed_registry import (
    SeedRegistryError,
    find_allocation,
    require_allocation_binding,
    seeds_from_membership,
    validate_binding_shape,
    validate_ledger,
)

from .protocol import (
    ALLOCATION_POPULATION,
    ALLOCATION_SPLIT,
    BASELINE_RUNTIME_IDENTITY,
    CANDIDATE_ARTIFACT_FILE_SHA256,
    CANDIDATE_ARTIFACT_IDENTITY,
    CANDIDATE_RUNTIME_IDENTITY,
    EXCLUDED_DIAGNOSTIC_SEEDS,
    LISJONG_ENGINE_REVISION,
    LISJONG_REVISION,
    ORDERED_SEEDS,
    OWNER_ISSUE,
    PROTOCOL_ID,
    RULES,
    SEED_DOMAIN,
    TORCH_VERSION,
    PairedStrengthProtocolError,
    game_schedule,
    protocol_document,
    require_protocol_document,
)

LOCK_VERSION = 1
EXECUTION_BRANCH = "main"
RESULT_DESTINATION = "result"

NO_RESCUE_BOUNDARY = (
    "the 1,000 locked seed blocks are never extended, replaced, re-seeded or "
    "dropped after result exposure",
    "the focal-seat schedule and the candidate / baseline pairing are never "
    "changed after result exposure",
    "the primary endpoint, interval method and IMPROVED threshold are never "
    "changed after result exposure",
    "invalid or failed hanchan are never skipped or selectively rerun, and a "
    "partial event is never adopted",
    "secondary diagnostics never replace the primary endpoint",
    "no training, HPO, envelope change or threshold tuning is authorized by "
    "any outcome of this event",
    "NOT ESTABLISHED is a valid terminal outcome, not permission to add seeds",
)

_PAYLOAD_FIELDS = {
    "artifact_destinations",
    "candidate_artifact",
    "environment",
    "execution_target",
    "lock_version",
    "no_rescue_boundary",
    "protocol",
    "result_exposed",
    "schedule_sha256",
    "seed_allocation",
}
_LOCK_FIELDS = _PAYLOAD_FIELDS | {"lock_identity"}


class PairedStrengthLockError(ValueError):
    """#385 pre-execution lockを生成・検証できない場合。"""


def document_identity(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


def schedule_sha256() -> str:
    """frozen 8,000 hanchan scheduleのcanonical JSON digest。"""
    text = json.dumps(
        [assignment.to_document() for assignment in game_schedule()],
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# live environment probes（testはここを差し替える）
# ---------------------------------------------------------------------------


def _arena_head() -> str:
    head = require_clean_arena_head()
    return require_merged_arena_revision(head, branch=EXECUTION_BRANCH)


def _installed_vcs_revision(distribution: str) -> str:
    text = metadata.distribution(distribution).read_text("direct_url.json")
    info = None if text is None else json.loads(text).get("vcs_info", {})
    revision = None if info is None else info.get("commit_id")
    if type(revision) is not str:
        raise PairedStrengthLockError(
            f"{distribution} is not installed from an exact VCS revision"
        )
    return revision


def _installed_environment() -> dict[str, str]:
    from lisjong_engine.rules import RuleSet

    rules = RuleSet.default()
    if (rules.name, rules.version) != (RULES["name"], RULES["version"]):
        raise PairedStrengthLockError("lisjong-engine RuleSet.default() drifted")
    try:
        torch_version = metadata.version("torch")
    except metadata.PackageNotFoundError as exc:
        raise PairedStrengthLockError("torch is not installed") from exc
    return {
        "lisjong": _installed_vcs_revision("lisjong"),
        "lisjong-engine": _installed_vcs_revision("lisjong-engine"),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "torch": torch_version,
    }


def _load_candidate_runtime(artifact_path: Path) -> object:
    from lisjong.learning import load_outcome_q_policy_factory

    return load_outcome_q_policy_factory(artifact_path)


def _baseline_runtime_identity() -> str:
    from lisjong.learning import ConstantResidualRuntime

    return ConstantResidualRuntime().identity


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def require_environment(environment: dict[str, str]) -> dict[str, str]:
    if environment.get("lisjong") != LISJONG_REVISION:
        raise PairedStrengthLockError(
            f"installed lisjong must be {LISJONG_REVISION} "
            f"but is {environment.get('lisjong')}"
        )
    if environment.get("lisjong-engine") != LISJONG_ENGINE_REVISION:
        raise PairedStrengthLockError(
            f"installed lisjong-engine must be {LISJONG_ENGINE_REVISION} "
            f"but is {environment.get('lisjong-engine')}"
        )
    if str(environment.get("torch", "")).split("+", 1)[0] != TORCH_VERSION:
        raise PairedStrengthLockError(f"installed torch must be {TORCH_VERSION}")
    if environment.get("python_implementation") != "CPython":
        raise PairedStrengthLockError("the event must run on CPython")
    return environment


def artifact_file_digests(artifact_path: Path) -> dict[str, str]:
    root = Path(artifact_path)
    if not root.is_dir():
        raise PairedStrengthLockError("candidate artifact directory does not exist")
    names = {path.name for path in root.iterdir()}
    if names != set(CANDIDATE_ARTIFACT_FILE_SHA256):
        raise PairedStrengthLockError(
            "candidate artifact directory must contain exactly "
            f"{sorted(CANDIDATE_ARTIFACT_FILE_SHA256)}"
        )
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in sorted(names)
    }


def require_candidate_artifact(
    artifact_path: Path, *, loader: Callable[[Path], object]
) -> dict[str, object]:
    """artifact bytes / identity / runtime identityをfrozen値へ照合する。"""
    digests = artifact_file_digests(artifact_path)
    if digests != CANDIDATE_ARTIFACT_FILE_SHA256:
        raise PairedStrengthLockError(
            "candidate artifact bytes differ from the frozen Step D digests"
        )
    runtime = loader(artifact_path)
    if getattr(runtime, "artifact_identity", None) != CANDIDATE_ARTIFACT_IDENTITY:
        raise PairedStrengthLockError("candidate artifact identity mismatch")
    if getattr(runtime, "identity", None) != CANDIDATE_RUNTIME_IDENTITY:
        raise PairedStrengthLockError("candidate runtime identity mismatch")
    if _baseline_runtime_identity() != BASELINE_RUNTIME_IDENTITY:
        raise PairedStrengthLockError("baseline runtime identity mismatch")
    return {
        "artifact_identity": CANDIDATE_ARTIFACT_IDENTITY,
        "baseline_runtime_identity": BASELINE_RUNTIME_IDENTITY,
        "file_sha256": digests,
        "runtime_identity": CANDIDATE_RUNTIME_IDENTITY,
    }


def require_seed_allocation(
    ledger: object, binding: object, *, arena_revision: str
) -> dict[str, object]:
    """live ledgerでallocationのownership / freshness / revisionを確認する。"""
    try:
        validated = validate_binding_shape(binding, seeds=ORDERED_SEEDS)
        record = require_allocation_binding(
            ledger,
            validated,
            seeds=ORDERED_SEEDS,
            owner_issue=OWNER_ISSUE,
            protocol=PROTOCOL_ID,
            seed_domain=SEED_DOMAIN,
            population=ALLOCATION_POPULATION,
            split=ALLOCATION_SPLIT,
        )
        live = validate_ledger(ledger)
        own = find_allocation(live, validated["allocation_identity"])
    except SeedRegistryError as exc:
        raise PairedStrengthLockError(
            f"seed allocation authority invalid: {exc}"
        ) from exc
    if record["arena_revision"] != arena_revision:
        raise PairedStrengthLockError(
            "seed allocation arena_revision is not the executing Arena revision"
        )
    population = set(ORDERED_SEEDS)
    if population & set(EXCLUDED_DIAGNOSTIC_SEEDS):
        raise PairedStrengthLockError("population overlaps diagnostic seeds")
    for other in live["allocations"]:
        if other is own:
            continue
        if population & set(seeds_from_membership(other["seed_membership"])):
            raise PairedStrengthLockError(
                "strength population is not disjoint from allocation "
                f"{other['allocation_identity']} ({other['population']})"
            )
    return {
        "arena_revision": record["arena_revision"],
        "binding": dict(validated),
        "protocol_revision": record["protocol_revision"],
    }


def _live_payload(
    *,
    artifact_path: Path,
    seed_ledger: object,
    allocation_binding: object,
    result_destination: str | Path,
    loader: Callable[[Path], object],
) -> dict[str, object]:
    try:
        head = _arena_head()
    except ExecutionSafetyError as exc:
        raise PairedStrengthLockError(str(exc)) from exc
    environment = require_environment(_installed_environment())
    allocation = require_seed_allocation(
        seed_ledger, allocation_binding, arena_revision=head
    )
    candidate = require_candidate_artifact(Path(artifact_path), loader=loader)
    return {
        "artifact_destinations": {
            RESULT_DESTINATION: str(Path(result_destination).resolve(strict=False))
        },
        "candidate_artifact": candidate,
        "environment": environment,
        "execution_target": {"branch": EXECUTION_BRANCH, "revision": head},
        "lock_version": LOCK_VERSION,
        "no_rescue_boundary": list(NO_RESCUE_BOUNDARY),
        "protocol": protocol_document(),
        "result_exposed": False,
        "schedule_sha256": schedule_sha256(),
        "seed_allocation": allocation,
    }


def _require_new_destination(path: str | Path) -> None:
    try:
        require_new_artifact_destinations(
            {RESULT_DESTINATION: path}, required_names=(RESULT_DESTINATION,)
        )
    except ExecutionSafetyError as exc:
        raise PairedStrengthLockError(str(exc)) from exc


# ---------------------------------------------------------------------------
# build / save / parse
# ---------------------------------------------------------------------------


def build_lock_document(
    *,
    artifact_path: str | Path,
    seed_ledger: object,
    allocation_binding: object,
    result_destination: str | Path,
    loader: Callable[[Path], object] = _load_candidate_runtime,
) -> dict[str, object]:
    """result exposure前のlock documentを構築する。"""
    _require_new_destination(result_destination)
    payload = _live_payload(
        artifact_path=Path(artifact_path),
        seed_ledger=seed_ledger,
        allocation_binding=allocation_binding,
        result_destination=result_destination,
        loader=loader,
    )
    return {**payload, "lock_identity": document_identity(payload)}


def save_lock_document(document: dict[str, object], path: str | Path) -> Path:
    parse_lock_document(document)
    destination = Path(path)
    try:
        require_new_artifact_destinations(
            {"lock": destination}, required_names=("lock",)
        )
    except ExecutionSafetyError as exc:
        raise PairedStrengthLockError(str(exc)) from exc
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def parse_lock_document(value: object) -> dict[str, object]:
    """lock documentをstrictに検証する。不明schema / drift / 欠損は拒否する。"""
    try:
        return _parse_lock_document(value)
    except PairedStrengthLockError:
        raise
    except (
        ArtifactValidationError,
        PairedStrengthProtocolError,
        SeedRegistryError,
        TypeError,
        KeyError,
    ) as exc:
        raise PairedStrengthLockError(str(exc)) from exc


def _parse_lock_document(value: object) -> dict[str, object]:
    raw = expect_object(value, _LOCK_FIELDS, "lock")
    if expect_int(raw["lock_version"], "lock.lock_version") != LOCK_VERSION:
        raise PairedStrengthLockError("unsupported lock version")
    if expect_bool(raw["result_exposed"], "lock.result_exposed"):
        raise PairedStrengthLockError(
            "pre-execution lock must record result_exposed=false"
        )
    require_protocol_document(raw["protocol"], "lock.protocol")
    if raw["schedule_sha256"] != schedule_sha256():
        raise PairedStrengthLockError("lock schedule digest drifted")
    if raw["no_rescue_boundary"] != list(NO_RESCUE_BOUNDARY):
        raise PairedStrengthLockError("lock no-rescue boundary drifted")

    target = expect_object(
        raw["execution_target"], {"branch", "revision"}, "lock.execution_target"
    )
    if target["branch"] != EXECUTION_BRANCH:
        raise PairedStrengthLockError("lock execution branch drifted")
    revision = expect_str(target["revision"], "lock.execution_target.revision")

    environment = expect_object(
        raw["environment"],
        {"lisjong", "lisjong-engine", "python", "python_implementation", "torch"},
        "lock.environment",
    )
    for name, item in environment.items():
        expect_str(item, f"lock.environment.{name}")
    require_environment(dict(environment))

    candidate = expect_object(
        raw["candidate_artifact"],
        {
            "artifact_identity",
            "baseline_runtime_identity",
            "file_sha256",
            "runtime_identity",
        },
        "lock.candidate_artifact",
    )
    if (
        candidate["artifact_identity"] != CANDIDATE_ARTIFACT_IDENTITY
        or candidate["runtime_identity"] != CANDIDATE_RUNTIME_IDENTITY
        or candidate["baseline_runtime_identity"] != BASELINE_RUNTIME_IDENTITY
        or candidate["file_sha256"] != CANDIDATE_ARTIFACT_FILE_SHA256
    ):
        raise PairedStrengthLockError("lock candidate artifact binding drifted")

    allocation = expect_object(
        raw["seed_allocation"],
        {"arena_revision", "binding", "protocol_revision"},
        "lock.seed_allocation",
    )
    validate_binding_shape(allocation["binding"], seeds=ORDERED_SEEDS)
    if allocation["binding"]["seed_domain"] != SEED_DOMAIN:
        raise PairedStrengthLockError("lock seed domain drifted")
    if allocation["arena_revision"] != revision:
        raise PairedStrengthLockError(
            "lock seed allocation arena_revision differs from the execution target"
        )

    destinations = expect_object(
        raw["artifact_destinations"], {RESULT_DESTINATION}, "lock.artifact_destinations"
    )
    expect_str(destinations[RESULT_DESTINATION], "lock.artifact_destinations.result")

    payload = {key: raw[key] for key in _PAYLOAD_FIELDS}
    if expect_str(raw["lock_identity"], "lock.lock_identity") != document_identity(
        payload
    ):
        raise PairedStrengthLockError("lock identity mismatch")
    return dict(raw)


def load_lock_document(path: str | Path) -> dict[str, object]:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as exc:
        raise PairedStrengthLockError(str(exc)) from exc
    return parse_lock_document(document)


def locked_result_destination(document: dict[str, object]) -> Path:
    destinations = parse_lock_document(document)["artifact_destinations"]
    assert isinstance(destinations, dict)
    return Path(str(destinations[RESULT_DESTINATION]))


def require_live_target(
    document: dict[str, object],
    *,
    artifact_path: str | Path,
    seed_ledger: object,
    loader: Callable[[Path], object] = _load_candidate_runtime,
) -> dict[str, object]:
    """strict readback: live環境でpayloadを再構成し、lockとの完全一致を要求する。

    result destinationが未作成であることも要求する（write-once / 未exposure）。
    """
    parsed = parse_lock_document(document)
    destination = locked_result_destination(parsed)
    _require_new_destination(destination)
    allocation = parsed["seed_allocation"]
    assert isinstance(allocation, dict)
    live = _live_payload(
        artifact_path=Path(artifact_path),
        seed_ledger=seed_ledger,
        allocation_binding=allocation["binding"],
        result_destination=destination,
        loader=loader,
    )
    locked = {key: parsed[key] for key in _PAYLOAD_FIELDS}
    if live != locked:
        drifted = sorted(key for key in _PAYLOAD_FIELDS if live[key] != locked[key])
        raise PairedStrengthLockError(
            "live execution target differs from the pre-execution lock: "
            + ", ".join(drifted)
        )
    return parsed


__all__ = [
    "EXECUTION_BRANCH",
    "LOCK_VERSION",
    "NO_RESCUE_BOUNDARY",
    "PairedStrengthLockError",
    "artifact_file_digests",
    "build_lock_document",
    "document_identity",
    "load_lock_document",
    "locked_result_destination",
    "parse_lock_document",
    "require_candidate_artifact",
    "require_environment",
    "require_live_target",
    "require_seed_allocation",
    "save_lock_document",
    "schedule_sha256",
]
