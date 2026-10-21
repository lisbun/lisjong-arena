"""実行前lock（#514、仕様§2.3・§4.3・§5）。

lockは実行前に書くwrite-once documentで、``result_exposed = false`` を持つ。
raw recordとresultは ``lock_digest`` を参照する。

```text
assemble_lock_document()  純粋関数: 入力の形・protocol値・participant・population・
                          allocation（渡されたledgerに対して）を検査してlockを作る
build_lock()              live値（Arena HEAD、実行環境、Seed Registry）を集めてassembleする
require_live_target()     実行の直前・直後に、live値がlockと一致することを要求する
parse_lock_document()     保存済みlockの厳密な読込（offline verifyも使う）
```

- K、timeout、Kの根拠はcallerが明示する。defaultを持たない（J1 / J2は校正後に決める）
- seedは、全domainの他allocation、registry外で使った既知seed（#472の472000〜472006）と
  重なれば拒否する。台帳が読めないときのfallbackはない
- 学習artifactを使うparticipantは受理しない。factory・callable自体は保存しない
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
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .kyoku_record import PROTOCOL_ID, ROLE_CANDIDATE, ROLE_INCUMBENT
from .protocol import (
    BACKEND,
    ENGINE_REVISION,
    EXPECTED_RULES,
    FORMAL_SPLIT,
    NO_RESCUE_BOUNDARY,
    POPULATION_PREFIX,
    PROTOCOL_DIGEST,
    RULES_DIGEST,
    SEED_DOMAIN,
    EngineAabbProtocolError,
    Participant,
    parse_participant,
    protocol_document,
    require_hex,
    require_pair,
    require_seeds,
    schedule_document,
)

LOCK_SCHEMA = "arena-heuristic-candidate-engine-aabb-lock-v1"
EXECUTION_BRANCH = "main"
DESTINATIONS = ("raw", "result", "diagnostics")
INCUMBENT_ROLES = ("current-champion", "fixed-c0")
"""incumbentの種別（§2.1）。1 eventのincumbentは1つで、累積進捗と昇格判断の標本は分ける。"""
K_RATIONALE_FIELDS = ("cost_estimate", "power", "sd_estimate", "target_effect")

_LOCK_FIELDS = frozenset(
    {
        "artifact_destinations",
        "environment",
        "environment_digest",
        "execution",
        "execution_target",
        "incumbent_role",
        "k_rationale",
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
_EXECUTION_FIELDS = frozenset({"backend", "timeout_seconds", "wheel_path", "workers"})


class EngineAabbLockError(EngineAabbProtocolError):
    """lockを作れない、読めない、またはlive値と一致しない場合。"""


def _owner_issue(event: int) -> str:
    if type(event) is not int or event <= 0:
        raise EngineAabbLockError("event must be a positive owner Issue number")
    return f"lisbun/lisjong-arena#{event}"


def require_population_seeds(
    ledger: object, binding: object, seeds: tuple[int, ...], *, event: int
) -> dict:
    """allocation bindingをledgerへ照合し、全domainの他allocationとの衝突を拒否する。"""
    return require_allocated_seeds(
        ledger,
        binding,
        seeds,
        owner_issue=_owner_issue(event),
        population=f"{POPULATION_PREFIX}{event}",
        split=FORMAL_SPLIT,
    )


def require_allocated_seeds(
    ledger: object,
    binding: object,
    seeds: tuple[int, ...],
    *,
    owner_issue: str,
    population: str,
    split: str,
) -> dict:
    """bindingが、このprotocol・engine domainの指定ownerのactive allocationであることと、
    全domainの他allocationと1 seedも重ならないことを要求する。

    正式lockは ``require_population_seeds``（split ``FORMAL-EVAL``）を通して使う。
    """
    try:
        validated = seed_registry.validate_binding_shape(binding, seeds=seeds)
        record = seed_registry.require_allocation_binding(
            ledger,
            validated,
            seeds=seeds,
            owner_issue=owner_issue,
            protocol=PROTOCOL_ID,
            seed_domain=SEED_DOMAIN,
            population=population,
            split=split,
        )
        ledger_document = seed_registry.validate_ledger(ledger)
    except seed_registry.SeedRegistryError as error:
        raise EngineAabbLockError(
            f"seed allocation authority invalid: {error}"
        ) from error
    requested = set(seeds)
    collisions = []
    for other in ledger_document["allocations"]:
        if other["allocation_identity"] == record["allocation_identity"]:
            continue
        overlap = requested & set(
            seed_registry.seeds_from_membership(other["seed_membership"])
        )
        if overlap:
            collisions.append(
                (other["owner_issue"], other["seed_domain"], sorted(overlap))
            )
    if collisions:
        raise EngineAabbLockError(f"seeds collide with other allocations: {collisions}")
    return dict(validated)


def _require_k_rationale(value: object) -> dict:
    if type(value) is not dict or set(value) != set(K_RATIONALE_FIELDS):
        raise EngineAabbLockError(f"k_rationale must have exactly {K_RATIONALE_FIELDS}")
    if any(type(value[name]) is not str or not value[name].strip() for name in value):
        raise EngineAabbLockError("k_rationale entries must be non-empty strings")
    return value


def _require_environment(environment: object, participants: tuple[Participant, ...]):
    if type(environment) is not dict:
        raise EngineAabbLockError("environment must be an object")
    try:
        provenance = environment["provenance"]
        backend = environment["shanten_backend"]
        native = environment["native"]
    except KeyError as error:
        raise EngineAabbLockError(f"environment is missing {error}") from error
    if set(environment) != {"native", "provenance", "python", "shanten_backend"}:
        raise EngineAabbLockError("environment fields do not match the schema")
    if provenance.get("lisjong_engine_revision") != ENGINE_REVISION:
        raise EngineAabbLockError(
            "installed lisjong-engine is not the protocol revision"
        )
    lisjong = provenance.get("lisjong_revision")
    if backend != {"backend": BACKEND, "lisjong_revision": lisjong}:
        raise EngineAabbLockError(
            "shanten backend must be rust for the installed lisjong"
        )
    if native.get("source_revision") != lisjong:
        raise EngineAabbLockError("native SOURCE_REVISION differs from lisjong")
    for participant in participants:
        if participant.lisjong_revision != lisjong:
            raise EngineAabbLockError(
                f"{participant.policy_identity} lisjong revision differs from the "
                "installed lisjong"
            )


def assemble_lock_document(
    *,
    event: int,
    candidate: Participant,
    incumbent: Participant,
    incumbent_role: str,
    ordered_seeds: object,
    k: int,
    allocation_binding: object,
    k_rationale: object,
    timeout_seconds: int,
    wheel_path: str,
    destinations: dict,
    environment: dict,
    arena_revision: str,
    ledger: object,
) -> dict[str, object]:
    """入力を検査してlock documentを作る（live値はcallerが渡す）。"""
    seeds = require_seeds(ordered_seeds)
    if type(k) is not int or k != len(seeds):
        raise EngineAabbLockError("K must equal the number of ordered seeds")
    require_pair(candidate, incumbent)
    if incumbent_role not in INCUMBENT_ROLES:
        raise EngineAabbLockError(f"incumbent_role must be one of {INCUMBENT_ROLES}")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise EngineAabbLockError("timeout_seconds must be an explicit positive int")
    if type(wheel_path) is not str or not Path(wheel_path).is_absolute():
        raise EngineAabbLockError("wheel_path must be an absolute path")
    require_hex(arena_revision, 40, "arena_revision")
    _require_environment(environment, (candidate, incumbent))
    binding = require_population_seeds(ledger, allocation_binding, seeds, event=event)
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise EngineAabbLockError(f"destinations must be exactly {DESTINATIONS}")
    payload = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in DESTINATIONS
        },
        "environment": environment,
        "environment_digest": document_identity(environment),
        "execution": {
            "backend": BACKEND,
            "timeout_seconds": timeout_seconds,
            "wheel_path": wheel_path,
            "workers": 1,
        },
        "execution_target": {"branch": EXECUTION_BRANCH, "revision": arena_revision},
        "incumbent_role": incumbent_role,
        "k_rationale": dict(_require_k_rationale(k_rationale)),
        "no_rescue_boundary": list(NO_RESCUE_BOUNDARY),
        "participants": {
            ROLE_CANDIDATE: candidate.to_document(),
            ROLE_INCUMBENT: incumbent.to_document(),
        },
        "population": {
            "allocation_binding": binding,
            "k": k,
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
        "schema": LOCK_SCHEMA,
    }
    document = {**payload, "lock_digest": document_identity(payload)}
    return parse_lock_document(document)


def parse_lock_document(value: object) -> dict[str, object]:
    """保存済みlockを厳密に検査する（live値・ledgerは見ない）。"""
    try:
        return _parse_lock(value)
    except EngineAabbProtocolError:
        raise
    except (ArtifactValidationError, TypeError, KeyError, ValueError) as error:
        raise EngineAabbLockError(
            f"invalid lock: {type(error).__name__}: {error}"
        ) from error


def _parse_lock(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != LOCK_SCHEMA:
        raise EngineAabbLockError("unsupported lock schema")
    if set(value) != _LOCK_FIELDS:
        raise EngineAabbLockError("lock fields do not match the schema")
    if value["result_exposed"] is not False:
        raise EngineAabbLockError(
            "a pre-execution lock must record result_exposed=false"
        )
    if (
        value["protocol"] != protocol_document()
        or value["protocol_digest"] != PROTOCOL_DIGEST
    ):
        raise EngineAabbLockError("lock protocol differs from the protocol constants")
    if value["rules"] != EXPECTED_RULES or value["rules_digest"] != RULES_DIGEST:
        raise EngineAabbLockError("lock rules differ from §5.2")
    if value["no_rescue_boundary"] != list(NO_RESCUE_BOUNDARY):
        raise EngineAabbLockError("lock no-rescue boundary drifted")
    if value["incumbent_role"] not in INCUMBENT_ROLES:
        raise EngineAabbLockError("lock incumbent_role is invalid")
    participants = value["participants"]
    if type(participants) is not dict or set(participants) != {
        ROLE_CANDIDATE,
        ROLE_INCUMBENT,
    }:
        raise EngineAabbLockError("lock participants must be A and B")
    candidate = parse_participant(participants[ROLE_CANDIDATE], "lock.participants.A")
    incumbent = parse_participant(participants[ROLE_INCUMBENT], "lock.participants.B")
    require_pair(candidate, incumbent)
    population = value["population"]
    if type(population) is not dict or set(population) != _POPULATION_FIELDS:
        raise EngineAabbLockError("lock population fields do not match the schema")
    seeds = require_seeds(population["ordered_seeds"])
    if type(population["k"]) is not int or population["k"] != len(seeds):
        raise EngineAabbLockError("lock K differs from the ordered seeds")
    owner = population["owner_issue"]
    event = (
        int(str(owner).rsplit("#", 1)[-1])
        if str(owner).startswith("lisbun/lisjong-arena#")
        else None
    )
    if event is None or owner != _owner_issue(event):
        raise EngineAabbLockError("lock owner_issue is invalid")
    if (
        population["population"] != f"{POPULATION_PREFIX}{event}"
        or population["split"] != FORMAL_SPLIT
        or population["seed_domain"] != SEED_DOMAIN
    ):
        raise EngineAabbLockError("lock population identity differs from §5.5")
    if population["schedule_digest"] != document_identity(schedule_document(seeds)):
        raise EngineAabbLockError("lock schedule digest mismatch")
    try:
        seed_registry.validate_binding_shape(
            population["allocation_binding"], seeds=seeds
        )
    except seed_registry.SeedRegistryError as error:
        raise EngineAabbLockError(
            f"lock allocation binding invalid: {error}"
        ) from error
    _require_k_rationale(value["k_rationale"])
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        raise EngineAabbLockError("lock execution fields do not match the schema")
    if execution["backend"] != BACKEND or execution["workers"] != 1:
        raise EngineAabbLockError("lock execution must be one rust worker")
    timeout = execution["timeout_seconds"]
    if type(timeout) is not int or timeout <= 0:
        raise EngineAabbLockError("lock timeout_seconds must be a positive int")
    if type(execution["wheel_path"]) is not str or not execution["wheel_path"]:
        raise EngineAabbLockError("lock wheel_path is invalid")
    _require_environment(value["environment"], (candidate, incumbent))
    if value["environment_digest"] != document_identity(value["environment"]):
        raise EngineAabbLockError("lock environment digest mismatch")
    target = value["execution_target"]
    if type(target) is not dict or set(target) != {"branch", "revision"}:
        raise EngineAabbLockError("lock execution_target fields do not match")
    if target["branch"] != EXECUTION_BRANCH:
        raise EngineAabbLockError("lock execution branch must be main")
    require_hex(target["revision"], 40, "lock execution_target.revision")
    if target["revision"] != value["environment"]["provenance"].get(
        "lisjong_arena_revision"
    ):
        raise EngineAabbLockError("lock revision differs from the recorded environment")
    destinations = value["artifact_destinations"]
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise EngineAabbLockError("lock destinations do not match the schema")
    if any(type(item) is not str or not item for item in destinations.values()):
        raise EngineAabbLockError("lock destinations must be non-empty strings")
    if len({Path(item).resolve(strict=False) for item in destinations.values()}) != 3:
        raise EngineAabbLockError("lock destinations must differ")
    payload = {key: item for key, item in value.items() if key != "lock_digest"}
    if value["lock_digest"] != document_identity(payload):
        raise EngineAabbLockError("lock digest mismatch")
    return value


def locked_participants(lock: dict) -> tuple[Participant, Participant]:
    participants = lock["participants"]
    return (
        parse_participant(participants[ROLE_CANDIDATE], "lock.participants.A"),
        parse_participant(participants[ROLE_INCUMBENT], "lock.participants.B"),
    )


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
        raise EngineAabbLockError(str(error)) from error
    return head


def _live_environment(wheel_path: str) -> dict:
    from .executor import environment_document

    try:
        return environment_document(wheel_path)
    except Exception as error:
        raise EngineAabbLockError(
            f"live environment cannot be verified: {type(error).__name__}: {error}"
        ) from error


def build_lock(
    *,
    event: int,
    candidate: Participant,
    incumbent: Participant,
    incumbent_role: str,
    ordered_seeds: object,
    k: int,
    allocation_identity: str,
    k_rationale: object,
    timeout_seconds: int,
    wheel_path: str,
    destinations: dict,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
) -> dict[str, object]:
    """live値を集めてlockを作る（保存はしない）。"""
    try:
        ledger = seed_registry.load_ledger(ledger_path)
        binding = seed_registry.allocation_binding(ledger, allocation_identity)
    except seed_registry.SeedRegistryError as error:
        raise EngineAabbLockError(f"seed registry cannot be used: {error}") from error
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise EngineAabbLockError(str(error)) from error
    head = _live_head()
    environment = _live_environment(wheel_path)
    if environment["provenance"].get("lisjong_arena_revision") != head:
        raise EngineAabbLockError("Arena HEAD differs from the collected provenance")
    return assemble_lock_document(
        event=event,
        candidate=candidate,
        incumbent=incumbent,
        incumbent_role=incumbent_role,
        ordered_seeds=ordered_seeds,
        k=k,
        allocation_binding=binding,
        k_rationale=k_rationale,
        timeout_seconds=timeout_seconds,
        wheel_path=wheel_path,
        destinations=destinations,
        environment=environment,
        arena_revision=head,
        ledger=ledger,
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
        raise EngineAabbLockError(f"cannot read lock: {error}") from error
    return parse_lock_document(document)


def require_live_target(
    lock: dict,
    *,
    ledger: object,
    environment: dict | None = None,
    head: str | None = None,
) -> None:
    """実行の直前・直後に、live HEAD・実行環境・allocationがlockと一致することを要求する。

    ``environment`` / ``head`` はtestでlive値を差し替えるためのseamで、正式実行では
    渡さない（このprocessから集める）。
    """
    parsed = parse_lock_document(lock)
    live_head = _live_head() if head is None else head
    live_environment = (
        _live_environment(parsed["execution"]["wheel_path"])
        if environment is None
        else environment
    )
    if live_head != parsed["execution_target"]["revision"]:
        raise EngineAabbLockError("live Arena HEAD differs from the lock")
    if live_environment != parsed["environment"]:
        raise EngineAabbLockError("live execution environment differs from the lock")
    population = parsed["population"]
    require_population_seeds(
        ledger,
        population["allocation_binding"],
        tuple(population["ordered_seeds"]),
        event=locked_event(parsed),
    )


__all__ = [
    "DESTINATIONS",
    "INCUMBENT_ROLES",
    "K_RATIONALE_FIELDS",
    "LOCK_SCHEMA",
    "EngineAabbLockError",
    "assemble_lock_document",
    "build_lock",
    "load_lock",
    "locked_event",
    "locked_participants",
    "parse_lock_document",
    "require_allocated_seeds",
    "require_live_target",
    "require_population_seeds",
    "save_lock",
]
