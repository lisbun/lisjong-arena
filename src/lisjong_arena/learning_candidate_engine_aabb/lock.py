"""実行前lock（#562）。

lockは実行前に書くwrite-once documentで、``result_exposed = false`` を持つ。
raw record・result・観測値は ``lock_digest`` を参照する。

```text
assemble_lock_document()  純粋関数: 入力の形・protocol値・participant binding・
                          環境・population（K = 100）・allocation・上限を検査してlockを作る
build_lock()              live値（clean merged Arena HEAD、実行環境、3 artifactを実際に
                          loadしたbinding、Seed Registry）を集めてassembleする
require_live_target()     実行の直前・直後に、live値がlockと一致することを要求する
parse_lock_document()     保存済みlockの厳密な読込（offline verifyも使う）
```

- K・protocol条件はprotocol invariantで、callerは変えられない
- timeout（1半荘）・全体wallの上限はcallerが明示する。defaultを持たない
- schema v2（#580）: メモリは ``execution.memory_guard``（``cgroup_peak_v1``、exact scopeの
  ``memory.peak > 6 GiB`` でSTOP、``memory.max = 8 GiB``）で、worker数は8。新しいlockはv2だけを作る
- schema v1（#562〜#573）: ``limits.peak_rss_kib`` を親 + workerのpeak RSS和の上限とする従来の意味。
  保存済みv1 lockの読込・実行の意味は変えない（遡及変更しない）
- seedは全domainの他allocationと、registry外で使った既知seedと重なれば拒否する
- factory・callable・任意codeは保存しない。artifactのpathは実行の値として持ち、
  identity・hashはparticipant bindingで照合する
"""

from pathlib import Path

from lisjong_arena import seed_registry
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
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    ROLE_CANDIDATE,
    ROLE_INCUMBENT,
)
from lisjong_arena.heuristic_candidate_engine_aabb.lock import require_allocated_seeds
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .memory_guard import (
    FORMAL_WORKERS,
    is_memory_guard_document,
    memory_guard_document,
)
from .participants import parse_candidate, parse_incumbent, require_pair
from .protocol import (
    BACKEND,
    ENGINE_REVISION,
    EXPECTED_RULES,
    FORMAL_K,
    FORMAL_SPLIT,
    NO_RESCUE_BOUNDARY,
    POPULATION_PREFIX,
    PROTOCOL_DIGEST,
    PROTOCOL_ID,
    RULES_DIGEST,
    SEED_DOMAIN,
    LearningEngineAabbError,
    protocol_document,
    require_formal_seeds,
    schedule_document,
)
from .runtime import require_artifact_paths, require_ml_runtime

LOCK_SCHEMA_V1 = "arena-learning-candidate-engine-aabb-lock-v1"
LOCK_SCHEMA_V2 = "arena-learning-candidate-engine-aabb-lock-v2"
LOCK_SCHEMA = LOCK_SCHEMA_V1
"""v1の名前（互換）。新しいlockは ``LOCK_SCHEMA_V2``。"""
EXECUTION_BRANCH = "main"
DESTINATIONS = ("diagnostics", "observations", "raw", "result")
LIMIT_FIELDS = ("peak_rss_kib", "timeout_seconds", "wall_seconds")
LIMIT_FIELDS_V2 = ("timeout_seconds", "wall_seconds")

_LOCK_FIELDS = frozenset(
    {
        "artifact_destinations",
        "environment",
        "environment_digest",
        "execution",
        "execution_target",
        "lock_digest",
        "no_rescue_boundary",
        "participants",
        "population",
        "protocol",
        "protocol_digest",
        "result_exposed",
        "rules",
        "rules_digest",
        "schema",
    }
)
_POPULATION_FIELDS = frozenset(
    {
        "allocation_binding",
        "k",
        "ordered_seeds",
        "owner_issue",
        "population",
        "schedule_digest",
        "seed_domain",
        "split",
    }
)
_EXECUTION_FIELDS = frozenset(
    {"artifact_paths", "backend", "limits", "wheel_path", "workers"}
)
_EXECUTION_FIELDS_V2 = _EXECUTION_FIELDS | {"memory_guard"}
_ENVIRONMENT_FIELDS = frozenset(
    {"ml", "native", "provenance", "python", "shanten_backend"}
)


class LearningEngineAabbLockError(LearningEngineAabbError):
    """lockを作れない、読めない、またはlive値と一致しない場合。"""


def _owner_issue(event: object) -> str:
    if type(event) is not int or event <= 0:
        raise LearningEngineAabbLockError("event must be a positive owner Issue number")
    return f"lisbun/lisjong-arena#{event}"


def require_population_seeds(
    ledger: object, binding: object, seeds: tuple[int, ...], *, event: int
) -> dict:
    """allocation bindingをledgerへ照合し、全domainの他allocationとの衝突を拒否する。"""
    try:
        return require_allocated_seeds(
            ledger,
            binding,
            seeds,
            owner_issue=_owner_issue(event),
            population=f"{POPULATION_PREFIX}{event}",
            split=FORMAL_SPLIT,
            protocol=PROTOCOL_ID,
        )
    except LearningEngineAabbError:
        raise
    except Exception as error:
        raise LearningEngineAabbLockError(str(error)) from error


def require_limits(
    value: object, fields: tuple[str, ...] = LIMIT_FIELDS
) -> dict[str, int]:
    """明示的な正の整数上限。v1は1半荘timeout・全体wall・peak RSS（KiB）、v2はpeak RSSなし。"""
    if type(value) is not dict or set(value) != set(fields):
        raise LearningEngineAabbLockError(f"limits must be exactly {fields}")
    for name, item in value.items():
        if type(item) is not int or item <= 0:
            raise LearningEngineAabbLockError(f"limit {name} must be a positive int")
    if value["timeout_seconds"] > value["wall_seconds"]:
        raise LearningEngineAabbLockError(
            "timeout_seconds must not exceed wall_seconds"
        )
    return value


def require_environment(environment: object, candidate: dict) -> dict:
    """環境のschema・engine revision・Rust backend・ML条件・candidate servingを照合する。"""
    if type(environment) is not dict or set(environment) != _ENVIRONMENT_FIELDS:
        raise LearningEngineAabbLockError("environment fields do not match the schema")
    provenance = environment["provenance"]
    native = environment["native"]
    if type(provenance) is not dict or type(native) is not dict:
        raise LearningEngineAabbLockError("environment entries must be objects")
    if provenance.get("lisjong_engine_revision") != ENGINE_REVISION:
        raise LearningEngineAabbLockError(
            "installed lisjong-engine is not the protocol revision"
        )
    lisjong = provenance.get("lisjong_revision")
    if environment["shanten_backend"] != {
        "backend": BACKEND,
        "lisjong_revision": lisjong,
    }:
        raise LearningEngineAabbLockError(
            "shanten backend must be rust for the installed lisjong"
        )
    if native.get("source_revision") != lisjong:
        raise LearningEngineAabbLockError("native SOURCE_REVISION differs from lisjong")
    if candidate["serving"]["lisjong_revision"] != lisjong:
        raise LearningEngineAabbLockError(
            "candidate serving revision differs from the installed lisjong"
        )
    try:
        ml = require_ml_runtime(environment["ml"])
    except LearningEngineAabbError as error:
        raise LearningEngineAabbLockError(str(error)) from error
    python = environment["python"]
    if type(python) is not dict or ml["python_version"] != python.get("version"):
        raise LearningEngineAabbLockError("ML Python version differs from the runtime")
    return environment


def assemble_lock_document(
    *,
    event: int,
    candidate: dict,
    incumbent: dict,
    ordered_seeds: object,
    allocation_binding: object,
    limits: object,
    wheel_path: str,
    artifact_paths: object,
    destinations: dict,
    environment: dict,
    arena_revision: str,
    ledger: object,
    workers: int = 1,
    memory_guard: object = None,
) -> dict[str, object]:
    """入力を検査してlock documentを作る（live値はcallerが渡す）。

    ``memory_guard`` を渡すとv2（#580）、``None`` は従来のv1。
    """
    if type(workers) is not int or not 1 <= workers <= 8:
        raise LearningEngineAabbLockError("formal workers must be an integer 1..8")
    if memory_guard is not None:
        _require_v2_memory(memory_guard, workers)
    seeds = require_formal_seeds(ordered_seeds)
    candidate = parse_candidate(candidate)
    incumbent = parse_incumbent(incumbent)
    require_pair(candidate, incumbent)
    if type(wheel_path) is not str or not Path(wheel_path).is_absolute():
        raise LearningEngineAabbLockError("wheel_path must be an absolute path")
    binding = require_population_seeds(ledger, allocation_binding, seeds, event=event)
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise LearningEngineAabbLockError(
            f"destinations must be exactly {DESTINATIONS}"
        )
    payload = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in DESTINATIONS
        },
        "environment": environment,
        "environment_digest": document_identity(environment),
        "execution": {
            "artifact_paths": dict(require_artifact_paths(artifact_paths)),
            "backend": BACKEND,
            "limits": dict(
                require_limits(
                    limits, LIMIT_FIELDS if memory_guard is None else LIMIT_FIELDS_V2
                )
            ),
            "wheel_path": wheel_path,
            "workers": workers,
        },
        "execution_target": {"branch": EXECUTION_BRANCH, "revision": arena_revision},
        "no_rescue_boundary": list(NO_RESCUE_BOUNDARY),
        "participants": {ROLE_CANDIDATE: candidate, ROLE_INCUMBENT: incumbent},
        "population": {
            "allocation_binding": binding,
            "k": FORMAL_K,
            "ordered_seeds": list(seeds),
            "owner_issue": _owner_issue(event),
            "population": f"{POPULATION_PREFIX}{event}",
            "schedule_digest": document_identity(schedule_document(seeds)),
            "seed_domain": SEED_DOMAIN,
            "split": FORMAL_SPLIT,
        },
        "protocol": protocol_document(),
        "protocol_digest": PROTOCOL_DIGEST,
        "result_exposed": False,
        "rules": dict(EXPECTED_RULES),
        "rules_digest": RULES_DIGEST,
        "schema": LOCK_SCHEMA_V1,
    }
    if memory_guard is not None:
        payload["execution"]["memory_guard"] = dict(memory_guard)
        payload["schema"] = LOCK_SCHEMA_V2
    document = {**payload, "lock_digest": document_identity(payload)}
    return parse_lock_document(document)


def _require_v2_memory(memory_guard: object, workers: object) -> None:
    if not is_memory_guard_document(memory_guard):
        raise LearningEngineAabbLockError(
            "memory_guard must be exactly the cgroup_peak_v1 contract of #580"
        )
    if workers != FORMAL_WORKERS or type(workers) is not int:
        raise LearningEngineAabbLockError(
            f"a cgroup_peak_v1 lock requires exactly {FORMAL_WORKERS} workers"
        )


def is_v2_lock(lock: dict) -> bool:
    return lock["schema"] == LOCK_SCHEMA_V2


def parse_lock_document(value: object) -> dict[str, object]:
    """保存済みlockを厳密に検査する（live値・ledgerは見ない）。"""
    try:
        return _parse_lock(value)
    except LearningEngineAabbError:
        raise
    except (ArtifactValidationError, TypeError, KeyError, ValueError) as error:
        raise LearningEngineAabbLockError(
            f"invalid lock: {type(error).__name__}: {error}"
        ) from error
    except Exception as error:  # heuristic部品のprotocol error
        raise LearningEngineAabbLockError(f"invalid lock: {error}") from error


def _parse_lock(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") not in (
        LOCK_SCHEMA_V1,
        LOCK_SCHEMA_V2,
    ):
        raise LearningEngineAabbLockError("unsupported lock schema")
    v2 = value["schema"] == LOCK_SCHEMA_V2
    if set(value) != _LOCK_FIELDS:
        raise LearningEngineAabbLockError("lock fields do not match the schema")
    if value["result_exposed"] is not False:
        raise LearningEngineAabbLockError(
            "a pre-execution lock must record result_exposed=false"
        )
    if (
        value["protocol"] != protocol_document()
        or value["protocol_digest"] != PROTOCOL_DIGEST
    ):
        raise LearningEngineAabbLockError(
            "lock protocol differs from the protocol constants"
        )
    if value["rules"] != EXPECTED_RULES or value["rules_digest"] != RULES_DIGEST:
        raise LearningEngineAabbLockError("lock rules differ from the protocol rules")
    if value["no_rescue_boundary"] != list(NO_RESCUE_BOUNDARY):
        raise LearningEngineAabbLockError("lock no-rescue boundary drifted")
    participants = value["participants"]
    if type(participants) is not dict or set(participants) != {
        ROLE_CANDIDATE,
        ROLE_INCUMBENT,
    }:
        raise LearningEngineAabbLockError("lock participants must be A and B")
    candidate = parse_candidate(participants[ROLE_CANDIDATE])
    incumbent = parse_incumbent(participants[ROLE_INCUMBENT])
    require_pair(candidate, incumbent)
    population = value["population"]
    if type(population) is not dict or set(population) != _POPULATION_FIELDS:
        raise LearningEngineAabbLockError(
            "lock population fields do not match the schema"
        )
    seeds = require_formal_seeds(population["ordered_seeds"])
    if population["k"] != FORMAL_K or type(population["k"]) is not int:
        raise LearningEngineAabbLockError(f"lock K must be {FORMAL_K}")
    owner = population["owner_issue"]
    prefix = "lisbun/lisjong-arena#"
    number = str(owner)[len(prefix) :] if str(owner).startswith(prefix) else ""
    if not number.isdigit() or owner != _owner_issue(int(number)):
        raise LearningEngineAabbLockError("lock owner_issue is invalid")
    event = int(number)
    if (
        population["population"] != f"{POPULATION_PREFIX}{event}"
        or population["split"] != FORMAL_SPLIT
        or population["seed_domain"] != SEED_DOMAIN
    ):
        raise LearningEngineAabbLockError("lock population identity is invalid")
    if population["schedule_digest"] != document_identity(schedule_document(seeds)):
        raise LearningEngineAabbLockError("lock schedule digest mismatch")
    try:
        seed_registry.validate_binding_shape(
            population["allocation_binding"], seeds=seeds
        )
    except seed_registry.SeedRegistryError as error:
        raise LearningEngineAabbLockError(
            f"lock allocation binding invalid: {error}"
        ) from error
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != (
        _EXECUTION_FIELDS_V2 if v2 else _EXECUTION_FIELDS
    ):
        raise LearningEngineAabbLockError(
            "lock execution fields do not match the schema"
        )
    if execution["backend"] != BACKEND:
        raise LearningEngineAabbLockError("lock execution backend must be rust")
    if type(execution["workers"]) is not int or not 1 <= execution["workers"] <= 8:
        raise LearningEngineAabbLockError("lock formal workers must be an integer 1..8")
    require_limits(execution["limits"], LIMIT_FIELDS_V2 if v2 else LIMIT_FIELDS)
    if v2:
        _require_v2_memory(execution["memory_guard"], execution["workers"])
    require_artifact_paths(execution["artifact_paths"])
    if type(execution["wheel_path"]) is not str or not execution["wheel_path"]:
        raise LearningEngineAabbLockError("lock wheel_path is invalid")
    require_environment(value["environment"], candidate)
    if value["environment_digest"] != document_identity(value["environment"]):
        raise LearningEngineAabbLockError("lock environment digest mismatch")
    target = value["execution_target"]
    if type(target) is not dict or set(target) != {"branch", "revision"}:
        raise LearningEngineAabbLockError("lock execution_target fields do not match")
    if target["branch"] != EXECUTION_BRANCH:
        raise LearningEngineAabbLockError("lock execution branch must be main")
    if target["revision"] != value["environment"]["provenance"].get(
        "lisjong_arena_revision"
    ):
        raise LearningEngineAabbLockError(
            "lock revision differs from the recorded environment"
        )
    if incumbent["serving"]["arena_revision"] != target["revision"]:
        raise LearningEngineAabbLockError(
            "incumbent serving revision differs from the execution target"
        )
    destinations = value["artifact_destinations"]
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise LearningEngineAabbLockError("lock destinations do not match the schema")
    if any(type(item) is not str or not item for item in destinations.values()):
        raise LearningEngineAabbLockError("lock destinations must be non-empty strings")
    if len({Path(item).resolve(strict=False) for item in destinations.values()}) != len(
        DESTINATIONS
    ):
        raise LearningEngineAabbLockError("lock destinations must differ")
    payload = {key: item for key, item in value.items() if key != "lock_digest"}
    if value["lock_digest"] != document_identity(payload):
        raise LearningEngineAabbLockError("lock digest mismatch")
    return value


def locked_event(lock: dict) -> int:
    return int(lock["population"]["owner_issue"].rsplit("#", 1)[1])


# ---------------------------------------------------------------------------
# live gates
# ---------------------------------------------------------------------------


def _live_head() -> str:
    try:
        head = require_clean_arena_head()
        require_merged_arena_revision(head, branch=EXECUTION_BRANCH)
    except ExecutionSafetyError as error:
        raise LearningEngineAabbLockError(str(error)) from error
    return head


def live_values(wheel_path: str, artifact_paths: dict, head: str):
    """このprocessの実行環境と、3 artifactを実際にloadしたbindingを集める。"""
    from .runtime import environment_document, load_participants

    try:
        environment = environment_document(wheel_path)
        loaded = load_participants(
            artifact_paths,
            lisjong_revision=environment["provenance"]["lisjong_revision"],
            arena_revision=head,
        )
    except Exception as error:
        raise LearningEngineAabbLockError(
            f"live environment cannot be verified: {type(error).__name__}: {error}"
        ) from error
    return environment, loaded.candidate, loaded.incumbent


def build_lock(
    *,
    event: int,
    ordered_seeds: object,
    allocation_identity: str,
    limits: object,
    wheel_path: str,
    artifact_paths: dict,
    destinations: dict,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    workers: int = FORMAL_WORKERS,
) -> dict[str, object]:
    """live値を集めてv2 lockを作る（保存はしない）。participantは実際にloadして作る。

    新しい正式lockは常にv2（#580）。``limits`` は ``timeout_seconds`` / ``wall_seconds``。
    """
    try:
        ledger = seed_registry.load_ledger(ledger_path)
        binding = seed_registry.allocation_binding(ledger, allocation_identity)
    except seed_registry.SeedRegistryError as error:
        raise LearningEngineAabbLockError(
            f"seed registry cannot be used: {error}"
        ) from error
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise LearningEngineAabbLockError(str(error)) from error
    head = _live_head()
    environment, candidate, incumbent = live_values(wheel_path, artifact_paths, head)
    if environment["provenance"].get("lisjong_arena_revision") != head:
        raise LearningEngineAabbLockError(
            "Arena HEAD differs from the collected provenance"
        )
    return assemble_lock_document(
        event=event,
        candidate=candidate,
        incumbent=incumbent,
        ordered_seeds=ordered_seeds,
        allocation_binding=binding,
        limits=limits,
        wheel_path=wheel_path,
        artifact_paths=artifact_paths,
        destinations=destinations,
        environment=environment,
        arena_revision=head,
        ledger=ledger,
        workers=workers,
        memory_guard=memory_guard_document(),
    )


def save_lock(document: dict, path: str | Path) -> Path:
    parse_lock_document(document)
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite lock: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_lock(path: str | Path) -> dict[str, object]:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise LearningEngineAabbLockError(f"cannot read lock: {error}") from error
    return parse_lock_document(document)


def require_live_target(
    lock: dict,
    *,
    ledger: object,
    live: tuple[dict, dict, dict] | None = None,
    head: str | None = None,
) -> None:
    """実行の直前・直後に、live HEAD・環境・participant binding・allocationを照合する。

    ``live`` は ``(environment, candidate, incumbent)``、``head`` はArena HEADで、
    testでlive値を差し替えるためのseamである。正式実行では渡さない。
    """
    parsed = parse_lock_document(lock)
    live_head = _live_head() if head is None else head
    if live_head != parsed["execution_target"]["revision"]:
        raise LearningEngineAabbLockError("live Arena HEAD differs from the lock")
    execution = parsed["execution"]
    environment, candidate, incumbent = (
        live_values(execution["wheel_path"], execution["artifact_paths"], live_head)
        if live is None
        else live
    )
    if environment != parsed["environment"]:
        raise LearningEngineAabbLockError(
            "live execution environment differs from the lock"
        )
    participants = parsed["participants"]
    if candidate != participants[ROLE_CANDIDATE]:
        raise LearningEngineAabbLockError(
            "live candidate binding differs from the lock"
        )
    if incumbent != participants[ROLE_INCUMBENT]:
        raise LearningEngineAabbLockError(
            "live incumbent binding differs from the lock"
        )
    population = parsed["population"]
    require_population_seeds(
        ledger,
        population["allocation_binding"],
        tuple(population["ordered_seeds"]),
        event=locked_event(parsed),
    )


__all__ = [
    "DESTINATIONS",
    "LIMIT_FIELDS",
    "LIMIT_FIELDS_V2",
    "LOCK_SCHEMA",
    "LOCK_SCHEMA_V1",
    "LOCK_SCHEMA_V2",
    "LearningEngineAabbLockError",
    "assemble_lock_document",
    "build_lock",
    "is_v2_lock",
    "load_lock",
    "locked_event",
    "parse_lock_document",
    "require_environment",
    "require_limits",
    "require_live_target",
    "require_population_seeds",
    "save_lock",
]
