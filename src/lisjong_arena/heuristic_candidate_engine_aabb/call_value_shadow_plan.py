"""#534 call-value-shadow-v1（#533）の実行前plan（DEVELOPMENT、Heuristic仮説の診断）。

#520 / #524のplan・protocol・digestは変えず、この診断専用の別documentを使う。
上限・停止・公開の実行枠と保存物の検査は#520の ``paijia_tie_plan`` を使う。

```text
participant  placement-aware-speed-call（同じexact lisjong revisionのChampion、設定{}）×4席
run_kind     allocated  固定24 seeds 533000..533023 × 1半荘、配置の入替なし（96 seat-hanchans）
                        Seed Registryの既存engine hanchan domain、protocol call-value-shadow-v1、
                        owner = 後続の実行Issue。予約とreadbackは既存workflow
             rehearsal  既知の開発用seed 472005・472006（registry外）。観測あり / なしを
                        別planで各1回（計4半荘）。Action列・客観outcomeの一致を
                        ``call-value-rehearsal-compare`` で照合する。判定は出さない
上限         1半荘600秒、process tree 4 GiB、wall: rehearsal 1時間 / 本実行4時間（固定）
```

planはwrite-onceで、Champion identity・seeds・上限・判定条件・RuleSet全field・
実行環境（lisjong / native / engine revision）・保存先を実行前に固定する（``plan_digest``）。
seed・判定・上限はprotocol invariantで、callerは変えられない。
"""

import re
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
    require_new_artifact_destinations,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .call_value_shadow_worker import SHADOW_VERSION
from .development import catalog_participant
from .kyoku_record import PROTOCOL_ID, ROLE_INCUMBENT
from .lock import (
    EXECUTION_BRANCH,
    EngineAabbLockError,
    _live_environment,
    _live_head,
    _require_environment,
    require_allocated_seeds,
)
from .paijia_tie_plan import ALLOCATED, DESTINATIONS, REHEARSAL, RUN_KINDS
from .protocol import (
    BACKEND,
    EXPECTED_RULES,
    POPULATION_PREFIX,
    PROTOCOL_DIGEST,
    RULES_DIGEST,
    SEED_DOMAIN,
    EngineAabbProtocolError,
    Participant,
    parse_participant,
    require_hex,
)

PLAN_SCHEMA = "arena-call-value-shadow-v1-plan-v1"
STOP_SCHEMA = "arena-call-value-shadow-v1-stop-v1"
EVALUATION_KIND = "DEVELOPMENT"
STAGE = "call-value-shadow"
"""stop記録（#520の実行枠）が使うstage名。stageは1つだけ。"""
SPLIT = "DEVELOPMENT"
DESIGN_ISSUE = "lisbun/lisjong-arena#533"
IMPLEMENTATION_ISSUE = "lisbun/lisjong-arena#534"
ANALYSIS_ISSUE = "lisbun/lisjong#350"
ALLOCATION_PROTOCOL = SHADOW_VERSION
"""Seed Registryのprotocol（#533 §7: 新shadow version、domainは既存engine hanchan）。"""

CHAMPION_IDENTITY = "placement-aware-speed-call"
SEEDS = tuple(range(533_000, 533_024))
"""本実行の固定seed（#533 §5）。重複があれば本versionを開始しない（別seedへ移さない）。"""
K = len(SEEDS)
SEAT_HANCHANS = 4 * K
REHEARSAL_SEEDS = (472_005, 472_006)
POPULATION = f"{POPULATION_PREFIX}call-value-shadow-v1"
REHEARSAL_SOURCE = (
    "registry-external #472 development seeds (also used by #514 dev-check, "
    "#517 P2, #520 and #524 rehearsals); observed and unobserved runs once each; "
    "Action sequences and objective outcomes must match; no gate decision"
)
OBSERVED = "observed"
UNOBSERVED = "unobserved"
OBSERVATIONS = (OBSERVED, UNOBSERVED)
PURPOSE = (
    "#533 call-value-shadow-v1: in NORMAL closed speed calls to route 1-shanten "
    "(threshold 2), how often the actual post-call discard drops below the "
    "adoption threshold and how much FiniteHorizon completion mass a "
    "threshold-keeping alternative gives up; Champion x 4 seats, only Champion "
    "actions are applied; DEVELOPMENT diagnostic, not a strength result"
)

TIMEOUT_SECONDS = 600
RSS_LIMIT_BYTES = 4 * 1024**3
WALL_LIMIT_SECONDS = {REHEARSAL: 3600, ALLOCATED: 4 * 3600}
"""#533 §7の段階上限。全実行工程6時間・swapなしはOS側の強制（``enforcement``）で扱う。"""

STOP_INVALID = "STOP-INVALID"
STOP_INCOMPLETE = "STOP-INCOMPLETE"
STOP_LOW_ACTIVATION = "STOP-LOW-ACTIVATION"
STOP_SPEED_TRADEOFF = "STOP-SPEED-TRADEOFF"
PROCEED = "PROCEED-TO-CANDIDATE-DESIGN"
DECISION_ORDER = (
    STOP_INVALID,
    STOP_INCOMPLETE,
    STOP_LOW_ACTIVATION,
    STOP_SPEED_TRADEOFF,
    PROCEED,
)

CRITERIA = {
    "change": (
        "a primary-target BELOW_THRESHOLD post-call discard whose lisjong "
        "alternative differs in tile type or red identity"
    ),
    "excluded_from_the_decision": [
        "VALUE / SPEED modes",
        "closed-to-tenpai and already-open calls",
        "BELOW_MAX",
        "cancelled call intents",
    ],
    "minimum_changes": 12,
    "minimum_seeds_with_change": 6,
    "median_loss_at_most": {"denominator": 5, "numerator": 1},
    "p90_loss_at_most": {"denominator": 5, "numerator": 2},
    "loss": (
        "L = (B - A) / B as the lisjong integer numerator / denominator; B = A = 0 "
        "is 0 and counted as zero mass; median = mean of the middle two when even; "
        "p90 = nearest rank ceil(0.9 n); exact rational comparison, no float"
    ),
    "order": list(DECISION_ORDER),
    "primary_target": (
        "lisjong SpeedCallDecisionAnalysis.is_primary_target: SPEED_CALL, NORMAL, "
        "closed before the call, route shanten 1, threshold 2"
    ),
    "seat_hanchan_denominator": SEAT_HANCHANS,
    "seed_denominator": K,
    "not_used_for_the_decision": (
        "wins, scores, placements, VALUE / BELOW_MAX results; no confidence "
        "interval or test over decision rows; no retry, extension or extra run"
    ),
}
"""判定（#533 §6、実行前に固定）。結果を見てから指標・seed・対象・代替ruleを変えない。"""

_OWNER = re.compile(r"lisbun/lisjong-arena#([1-9][0-9]*)")
_PLAN_FIELDS = frozenset(
    {
        "analysis_issue",
        "artifact_destinations",
        "criteria",
        "design_issue",
        "environment",
        "environment_digest",
        "evaluation_kind",
        "execution",
        "execution_target",
        "implementation_issue",
        "not_a_formal_result",
        "observation",
        "owner_issue",
        "participant",
        "plan_digest",
        "population",
        "protocol_digest",
        "purpose",
        "resource_limits",
        "rules",
        "rules_digest",
        "run_kind",
        "schema",
        "shadow_version",
        "stage",
    }
)
_ALLOCATED_FIELDS = frozenset(
    {
        "allocation_binding",
        "allocation_protocol",
        "k",
        "ordered_seeds",
        "population",
        "schedule_digest",
        "seed_domain",
        "split",
    }
)
_REHEARSAL_FIELDS = frozenset({"k", "ordered_seeds", "schedule_digest", "source"})
_EXECUTION_FIELDS = frozenset({"backend", "timeout_seconds", "wheel_path", "workers"})
_LIMIT_FIELDS = frozenset({"enforcement", "process_tree_rss_bytes", "wall_seconds"})


class CallValueShadowError(EngineAabbProtocolError):
    """#534のplanを作れない・読めない、または保存物が再導出と一致しない場合。"""


def _fail(message: str):
    raise CallValueShadowError(f"call-value-shadow-v1: {message}")


def champion(lisjong_revision: str) -> Participant:
    return catalog_participant(CHAMPION_IDENTITY, lisjong_revision)


def schedule(seeds) -> list[dict]:
    """実行順: seed順に1半荘、4席Champion、配置の入替なし（placementは照合用の0）。"""
    return [
        {"placement": 0, "roles": [ROLE_INCUMBENT] * 4, "seed": seed} for seed in seeds
    ]


def schedule_digest(seeds) -> str:
    return document_identity(
        {
            "hanchans": schedule(seeds),
            "protocol_id": PROTOCOL_ID,
            "shadow_version": SHADOW_VERSION,
        }
    )


def plan_schedule(plan: dict) -> list[dict]:
    return schedule(plan["population"]["ordered_seeds"])


def plan_champion(plan: dict) -> Participant:
    return parse_participant(plan["participant"], "plan.participant")


def resource_limits(run_kind: str, enforcement: object) -> dict:
    """固定上限。``enforcement`` はOS / process treeの強制方法の説明（必須）。"""
    if type(enforcement) is not str or not enforcement.strip():
        _fail("resource_limits.enforcement must describe the OS-level enforcement")
    return {
        "enforcement": enforcement,
        "process_tree_rss_bytes": RSS_LIMIT_BYTES,
        "wall_seconds": WALL_LIMIT_SECONDS[run_kind],
    }


def _require_owner(value: object) -> str:
    match = _OWNER.fullmatch(value) if type(value) is str else None
    if match is None or int(match.group(1)) <= 534:
        _fail("owner_issue must be the later execution Issue lisbun/lisjong-arena#N")
    return value


def _seeds(run_kind: str, values: object) -> tuple[int, ...]:
    expected = SEEDS if run_kind == ALLOCATED else REHEARSAL_SEEDS
    if type(values) not in (list, tuple) or tuple(values) != expected:
        _fail(f"{run_kind} uses exactly the fixed seeds {expected[0]}..{expected[-1]}")
    return expected


def require_stage_seeds(ledger: object, binding: object, owner_issue: str) -> dict:
    """split DEVELOPMENT・指定owner・専用population・protocol call-value-shadow-v1の
    active allocationで、全domainの他allocationと1 seedも重ならないこと。"""
    try:
        return require_allocated_seeds(
            ledger,
            binding,
            SEEDS,
            owner_issue=owner_issue,
            population=POPULATION,
            split=SPLIT,
            protocol=ALLOCATION_PROTOCOL,
        )
    except EngineAabbLockError as error:
        raise CallValueShadowError(str(error)) from error


def assemble_plan(
    *,
    run_kind: str,
    observation: str,
    owner_issue: str,
    allocation_binding: object,
    limit_enforcement: str,
    wheel_path: str,
    destinations: dict,
    environment: dict,
    arena_revision: str,
    ledger: object,
) -> dict[str, object]:
    """入力を検査してplanを作る（live値・ledgerはcallerが渡す）。"""
    if run_kind not in RUN_KINDS:
        _fail(f"run_kind must be one of {RUN_KINDS}")
    if observation not in OBSERVATIONS:
        _fail(f"observation must be one of {OBSERVATIONS}")
    if run_kind == ALLOCATED and observation != OBSERVED:
        _fail("the allocated run is observed")
    _require_owner(owner_issue)
    seeds = SEEDS if run_kind == ALLOCATED else REHEARSAL_SEEDS
    if type(wheel_path) is not str or not Path(wheel_path).is_absolute():
        _fail("wheel_path must be an absolute path")
    require_hex(arena_revision, 40, "arena_revision")
    if type(environment) is not dict or type(environment.get("provenance")) is not dict:
        _fail("environment must hold provenance")
    participant = champion(environment["provenance"].get("lisjong_revision"))
    try:
        _require_environment(environment, (participant,))
    except EngineAabbLockError as error:
        raise CallValueShadowError(str(error)) from error
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        _fail("Arena revision differs from the environment")
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        _fail(f"destinations must be exactly {DESTINATIONS}")
    digest = schedule_digest(seeds)
    if run_kind == ALLOCATED:
        binding = require_stage_seeds(ledger, allocation_binding, owner_issue)
        population = {
            "allocation_binding": dict(binding),
            "allocation_protocol": ALLOCATION_PROTOCOL,
            "k": K,
            "ordered_seeds": list(seeds),
            "population": POPULATION,
            "schedule_digest": digest,
            "seed_domain": SEED_DOMAIN,
            "split": SPLIT,
        }
    else:
        if allocation_binding is not None:
            _fail("a rehearsal has no allocation")
        population = {
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "schedule_digest": digest,
            "source": REHEARSAL_SOURCE,
        }
    payload = {
        "analysis_issue": ANALYSIS_ISSUE,
        "artifact_destinations": {
            name: str(destinations[name]) for name in DESTINATIONS
        },
        "criteria": CRITERIA,
        "design_issue": DESIGN_ISSUE,
        "environment": environment,
        "environment_digest": document_identity(environment),
        "evaluation_kind": EVALUATION_KIND,
        "execution": {
            "backend": BACKEND,
            "timeout_seconds": TIMEOUT_SECONDS,
            "wheel_path": wheel_path,
            "workers": 1,
        },
        "execution_target": {"branch": EXECUTION_BRANCH, "revision": arena_revision},
        "implementation_issue": IMPLEMENTATION_ISSUE,
        "not_a_formal_result": True,
        "observation": observation,
        "owner_issue": owner_issue,
        "participant": participant.to_document(),
        "population": population,
        "protocol_digest": PROTOCOL_DIGEST,
        "purpose": PURPOSE,
        "resource_limits": resource_limits(run_kind, limit_enforcement),
        "rules": EXPECTED_RULES,
        "rules_digest": RULES_DIGEST,
        "run_kind": run_kind,
        "schema": PLAN_SCHEMA,
        "shadow_version": SHADOW_VERSION,
        "stage": STAGE,
    }
    return parse_plan({**payload, "plan_digest": document_identity(payload)})


def parse_plan(value: object) -> dict[str, object]:
    """保存済みplanを厳密に検査する（live値・ledgerは見ない）。"""
    try:
        return _parse_plan(value)
    except EngineAabbProtocolError:
        raise
    except (ArtifactValidationError, TypeError, KeyError, ValueError) as error:
        raise CallValueShadowError(
            f"invalid call-value-shadow-v1 plan: {type(error).__name__}: {error}"
        ) from error


def _parse_plan(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != PLAN_SCHEMA:
        _fail("plan schema is unsupported")
    if set(value) != _PLAN_FIELDS:
        _fail("plan fields do not match")
    run_kind, observation = value["run_kind"], value["observation"]
    if run_kind not in RUN_KINDS or observation not in OBSERVATIONS:
        _fail("plan run_kind or observation is invalid")
    if run_kind == ALLOCATED and observation != OBSERVED:
        _fail("the allocated run is observed")
    if (
        value["shadow_version"] != SHADOW_VERSION
        or value["stage"] != STAGE
        or value["evaluation_kind"] != EVALUATION_KIND
        or value["not_a_formal_result"] is not True
        or value["design_issue"] != DESIGN_ISSUE
        or value["implementation_issue"] != IMPLEMENTATION_ISSUE
        or value["analysis_issue"] != ANALYSIS_ISSUE
        or value["purpose"] != PURPOSE
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules"] != EXPECTED_RULES
        or value["rules_digest"] != RULES_DIGEST
        or document_identity(value["rules"]) != RULES_DIGEST
    ):
        _fail("plan identity drifted")
    if value["criteria"] != CRITERIA:
        _fail("criteria drifted")
    _require_owner(value["owner_issue"])
    participant = parse_participant(value["participant"], "plan.participant")
    if participant != champion(participant.lisjong_revision):
        _fail("plan must use the Champion with an empty configuration")
    population = value["population"]
    expected = _ALLOCATED_FIELDS if run_kind == ALLOCATED else _REHEARSAL_FIELDS
    if type(population) is not dict or set(population) != expected:
        _fail("population fields do not match")
    seeds = _seeds(run_kind, population["ordered_seeds"])
    if population["k"] != len(seeds) or population["schedule_digest"] != (
        schedule_digest(seeds)
    ):
        _fail("population schedule drifted")
    if run_kind == ALLOCATED:
        if (
            population["population"] != POPULATION
            or population["split"] != SPLIT
            or population["seed_domain"] != SEED_DOMAIN
            or population["allocation_protocol"] != ALLOCATION_PROTOCOL
        ):
            _fail("population identity drifted")
        try:
            binding = seed_registry.validate_binding_shape(
                population["allocation_binding"], seeds=seeds
            )
        except seed_registry.SeedRegistryError as error:
            raise CallValueShadowError(
                f"call-value-shadow-v1 allocation binding invalid: {error}"
            ) from error
        if binding["seed_domain"] != SEED_DOMAIN:
            _fail("allocation binding is outside the engine hanchan domain")
    elif population["source"] != REHEARSAL_SOURCE:
        _fail("rehearsal source drifted")
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        _fail("execution fields do not match")
    if (
        execution["backend"] != BACKEND
        or execution["workers"] != 1
        or execution["timeout_seconds"] != TIMEOUT_SECONDS
    ):
        _fail("runs one rust worker with the fixed hanchan timeout")
    if type(execution["wheel_path"]) is not str or not execution["wheel_path"]:
        _fail("wheel_path is invalid")
    limits = value["resource_limits"]
    if type(limits) is not dict or set(limits) != _LIMIT_FIELDS:
        _fail("resource_limits fields do not match")
    if limits != resource_limits(run_kind, limits["enforcement"]):
        _fail("resource limits differ from the fixed limits")
    try:
        _require_environment(value["environment"], (participant,))
    except EngineAabbLockError as error:
        raise CallValueShadowError(str(error)) from error
    if value["environment_digest"] != document_identity(value["environment"]):
        _fail("environment digest mismatch")
    target = value["execution_target"]
    if type(target) is not dict or set(target) != {"branch", "revision"}:
        _fail("execution_target is invalid")
    if target["branch"] != EXECUTION_BRANCH:
        _fail("runs from main")
    require_hex(target["revision"], 40, "execution_target.revision")
    if target["revision"] != value["environment"]["provenance"].get(
        "lisjong_arena_revision"
    ):
        _fail("plan revision differs from the environment")
    destinations = value["artifact_destinations"]
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        _fail("destinations do not match")
    if any(type(item) is not str or not item for item in destinations.values()):
        _fail("destinations must be strings")
    if len({Path(item).resolve(strict=False) for item in destinations.values()}) != 2:
        _fail("destinations must differ")
    payload = {key: item for key, item in value.items() if key != "plan_digest"}
    if value["plan_digest"] != document_identity(payload):
        _fail("plan digest mismatch")
    return value


def build_plan(
    *,
    run_kind: str,
    observation: str,
    owner_issue: str,
    allocation_identity: str | None,
    limit_enforcement: str,
    wheel_path: str,
    destinations: dict,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
) -> dict[str, object]:
    """live値（ledger、clean merged HEAD、実行環境）を集めてplanを作る（保存はしない）。"""
    ledger = binding = None
    if run_kind == ALLOCATED:
        try:
            ledger = seed_registry.load_ledger(ledger_path)
            binding = seed_registry.allocation_binding(ledger, allocation_identity)
        except seed_registry.SeedRegistryError as error:
            raise CallValueShadowError(
                f"seed registry cannot be used: {error}"
            ) from error
    elif allocation_identity is not None:
        _fail("a rehearsal has no allocation")
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise CallValueShadowError(str(error)) from error
    try:
        head = _live_head()
        environment = _live_environment(wheel_path)
    except EngineAabbLockError as error:
        raise CallValueShadowError(str(error)) from error
    return assemble_plan(
        run_kind=run_kind,
        observation=observation,
        owner_issue=owner_issue,
        allocation_binding=binding,
        limit_enforcement=limit_enforcement,
        wheel_path=wheel_path,
        destinations=destinations,
        environment=environment,
        arena_revision=head,
        ledger=ledger,
    )


def save_plan(document: dict, path: str | Path) -> Path:
    parse_plan(document)
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite the plan: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_plan(path: str | Path) -> dict[str, object]:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise CallValueShadowError(f"cannot read the plan: {error}") from error
    return parse_plan(document)


def require_live_target(
    plan: dict,
    *,
    ledger: object,
    environment: dict | None = None,
    head: str | None = None,
) -> None:
    """実行の直前・直後に、live HEAD・実行環境・（allocatedなら）allocationが
    planと一致することを要求する。``environment`` / ``head`` はtestのseam。"""
    parsed = parse_plan(plan)
    try:
        live_head = _live_head() if head is None else head
        live_environment = (
            _live_environment(parsed["execution"]["wheel_path"])
            if environment is None
            else environment
        )
    except EngineAabbLockError as error:
        raise CallValueShadowError(str(error)) from error
    if live_head != parsed["execution_target"]["revision"]:
        _fail("live Arena HEAD differs from the plan")
    if live_environment != parsed["environment"]:
        _fail("live environment differs from the plan")
    if parsed["run_kind"] == ALLOCATED:
        require_stage_seeds(
            ledger, parsed["population"]["allocation_binding"], parsed["owner_issue"]
        )


__all__ = [
    "ALLOCATION_PROTOCOL",
    "CHAMPION_IDENTITY",
    "CRITERIA",
    "DECISION_ORDER",
    "K",
    "OBSERVATIONS",
    "OBSERVED",
    "PLAN_SCHEMA",
    "POPULATION",
    "PROCEED",
    "REHEARSAL_SEEDS",
    "RSS_LIMIT_BYTES",
    "SEAT_HANCHANS",
    "SEEDS",
    "STOP_INCOMPLETE",
    "STOP_INVALID",
    "STOP_LOW_ACTIVATION",
    "STOP_SCHEMA",
    "STOP_SPEED_TRADEOFF",
    "TIMEOUT_SECONDS",
    "UNOBSERVED",
    "WALL_LIMIT_SECONDS",
    "CallValueShadowError",
    "assemble_plan",
    "build_plan",
    "champion",
    "load_plan",
    "parse_plan",
    "plan_champion",
    "plan_schedule",
    "require_live_target",
    "require_stage_seeds",
    "save_plan",
    "schedule",
    "schedule_digest",
]
