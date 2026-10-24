"""#524 単独副露者genbutsu候補のshadow変更頻度・時間gate（DEVELOPMENT）の実行前plan。

#520のplan・protocol・digestは変えず、この実験専用の別documentを使う。上限・停止・公開の
実行枠と保存物の検査は#520の ``paijia_tie_plan`` をそのまま使う。

```text
pair      A  placement-aware-speed-call-single-open-genbutsu-v1（lisjong #343）
          B  placement-aware-speed-call（同じexact lisjong revisionの現Champion）
          どちらも設定空・学習artifactなし
stage     activation のみ（B自己対局8半荘、全判断でAをshadow採点。Aは適用しない）
run_kind  allocated  Seed Registryの新規DEVELOPMENT allocation（owner #524）
          rehearsal  既知の開発用seed 472005・472006（registry外）で同じ経路を1回通す
                     動作確認。判定は出さない
```

planはwrite-onceで、pair・seeds・timeout・資源上限・判定条件・実行環境・保存先を
実行前に固定する（``plan_digest``）。defaultはない。後続の192半荘screenは別の実行Issueで
別のplanとして事前登録する（このmoduleは作らない）。
"""

from collections.abc import Sequence
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

from .development import catalog_participant
from .lock import (
    EXECUTION_BRANCH,
    EngineAabbLockError,
    _live_environment,
    _live_head,
    _require_environment,
    require_allocated_seeds,
)
from .paijia_tie_plan import (
    ACTIVATION,
    ALLOCATED,
    DESTINATIONS,
    REHEARSAL,
    REHEARSAL_SEEDS,
    RUN_KINDS,
    _require_limits,
    schedule_digest,
)
from .protocol import (
    BACKEND,
    POPULATION_PREFIX,
    PROTOCOL_DIGEST,
    RULES_DIGEST,
    SEED_DOMAIN,
    EngineAabbProtocolError,
    Participant,
    parse_participant,
    require_hex,
    require_pair,
    require_seeds,
)

PLAN_SCHEMA = "arena-heuristic-candidate-engine-aabb-single-open-genbutsu-plan-v1"
STOP_SCHEMA = "arena-heuristic-candidate-engine-aabb-single-open-genbutsu-stop-v1"
EVALUATION_KIND = "DEVELOPMENT"
SPLIT = "DEVELOPMENT"
OWNER_ISSUE = "lisbun/lisjong-arena#524"

CANDIDATE_IDENTITY = "placement-aware-speed-call-single-open-genbutsu-v1"
INCUMBENT_IDENTITY = "placement-aware-speed-call"
PAIR = (CANDIDATE_IDENTITY, INCUMBENT_IDENTITY)
"""A（lisjong #343の候補）、B（同じrevisionの現Champion）。歴史的固定C0ではない。"""

STAGE = ACTIVATION
K = 8
"""固定規模（Issue #524）。追加seed・早期停止・Kの変更はしない。"""
SEAT_HANCHANS = K * 4
POPULATION = f"{POPULATION_PREFIX}single-open-genbutsu-activation-524"
REHEARSAL_SOURCE = (
    "registry-external #472 development seeds (also used by #514 dev-check, "
    "#517 P2 and the #520 rehearsal); rehearsal of the same runner, no gate decision"
)
PURPOSE = (
    "#524: shadow change-frequency and time gate of the single-open-genbutsu "
    "candidate; B self-play x 4 seats, every decision shadow-scored by fresh A "
    "instances (normal and trace execution); only B actions are applied; "
    "DEVELOPMENT, not a strength result"
)
SCREEN_HANCHANS = 32 * 6
"""後続screen（別Issue）の規模。ここでは時間の記述的な見積りにだけ使う。"""

CRITERIA = {
    "change_outcomes": ["red-only-change", "tile-type-change"],
    "excluded_from_changes": ["tsumogiri-only"],
    "invalid_outcomes": [
        "analysis-invariant-violation",
        "candidate-illegal-action",
        "out-of-scope-difference",
        "trace-action-mismatch",
    ],
    "minimum_changes": 32,
    "minimum_seeds_with_change": 4,
    "not_used_for_the_decision": (
        "shadow wins, scores and placements; the decision is a budget-allocation "
        "rule, not evidence of improvement or of detection power"
    ),
    "order": ["STOP-INVALID", "STOP-LOW-ACTIVATION", "PROCEED"],
    "seat_hanchan_denominator": SEAT_HANCHANS,
    "screen_projection": (
        "descriptive only, not a gate: screen_hanchans * mean over hanchans of "
        "(wall_seconds - A normal seconds - (A trace seconds + B seconds) / 2); "
        "the budget decision belongs to the later execution Issue"
    ),
    "screen_hanchans": SCREEN_HANCHANS,
}
"""判定（実行前に固定）。低頻度でも追加seed・対象/閾値の緩和・候補の差替えをしない。"""

_PLAN_FIELDS = frozenset(
    {
        "artifact_destinations",
        "criteria",
        "environment",
        "environment_digest",
        "evaluation_kind",
        "execution",
        "execution_target",
        "not_a_formal_result",
        "owner_issue",
        "participants",
        "plan_digest",
        "population",
        "protocol_digest",
        "purpose",
        "resource_limits",
        "rules_digest",
        "run_kind",
        "schema",
        "stage",
    }
)
_ALLOCATED_FIELDS = frozenset(
    {
        "allocation_binding",
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


class EngineAabbSingleOpenGenbutsuError(EngineAabbProtocolError):
    """#524のplanを作れない・読めない、または保存物が再導出と一致しない場合。"""


def participants(lisjong_revision: str) -> tuple[Participant, Participant]:
    candidate, incumbent = (
        catalog_participant(identity, lisjong_revision) for identity in PAIR
    )
    require_pair(candidate, incumbent)
    return candidate, incumbent


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise EngineAabbSingleOpenGenbutsuError(
            f"{name} must be an explicit positive int"
        )
    return value


def _seeds(run_kind: str, values: object) -> tuple[int, ...]:
    if run_kind == REHEARSAL:
        if list(values) != list(REHEARSAL_SEEDS):
            raise EngineAabbSingleOpenGenbutsuError(
                f"a rehearsal uses exactly the development seeds {REHEARSAL_SEEDS}"
            )
        return REHEARSAL_SEEDS
    seeds = require_seeds(values)
    if len(seeds) != K:
        raise EngineAabbSingleOpenGenbutsuError(f"#524 uses exactly {K} seeds")
    return seeds


def require_stage_seeds(ledger: object, binding: object, seeds) -> dict:
    """split ``DEVELOPMENT``・owner #524・専用populationのactive allocationで、
    全domainの他allocationと1 seedも重ならないこと（使用済みseedを再利用しない）。"""
    try:
        return require_allocated_seeds(
            ledger,
            binding,
            tuple(seeds),
            owner_issue=OWNER_ISSUE,
            population=POPULATION,
            split=SPLIT,
        )
    except EngineAabbLockError as error:
        raise EngineAabbSingleOpenGenbutsuError(str(error)) from error


def assemble_plan(
    *,
    run_kind: str,
    ordered_seeds: object,
    allocation_binding: object,
    timeout_seconds: int,
    resource_limits: object,
    wheel_path: str,
    destinations: dict,
    environment: dict,
    arena_revision: str,
    ledger: object,
) -> dict[str, object]:
    """入力を検査してplanを作る（live値・ledgerはcallerが渡す）。"""
    if run_kind not in RUN_KINDS:
        raise EngineAabbSingleOpenGenbutsuError(f"run_kind must be {RUN_KINDS}")
    seeds = _seeds(run_kind, ordered_seeds)
    _positive_int(timeout_seconds, "timeout_seconds")
    limits = dict(_require_limits(resource_limits))
    if type(wheel_path) is not str or not Path(wheel_path).is_absolute():
        raise EngineAabbSingleOpenGenbutsuError("wheel_path must be an absolute path")
    require_hex(arena_revision, 40, "arena_revision")
    if type(environment) is not dict or type(environment.get("provenance")) is not dict:
        raise EngineAabbSingleOpenGenbutsuError("environment must hold provenance")
    candidate, incumbent = participants(
        environment["provenance"].get("lisjong_revision")
    )
    try:
        _require_environment(environment, (candidate, incumbent))
    except EngineAabbLockError as error:
        raise EngineAabbSingleOpenGenbutsuError(str(error)) from error
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        raise EngineAabbSingleOpenGenbutsuError(
            "Arena revision differs from the environment"
        )
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise EngineAabbSingleOpenGenbutsuError(
            f"destinations must be exactly {DESTINATIONS}"
        )
    digest = schedule_digest(STAGE, seeds)
    if run_kind == ALLOCATED:
        binding = require_stage_seeds(ledger, allocation_binding, seeds)
        population = {
            "allocation_binding": dict(binding),
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "population": POPULATION,
            "schedule_digest": digest,
            "seed_domain": SEED_DOMAIN,
            "split": SPLIT,
        }
    else:
        if allocation_binding is not None:
            raise EngineAabbSingleOpenGenbutsuError("a rehearsal has no allocation")
        population = {
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "schedule_digest": digest,
            "source": REHEARSAL_SOURCE,
        }
    payload = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in DESTINATIONS
        },
        "criteria": dict(CRITERIA),
        "environment": environment,
        "environment_digest": document_identity(environment),
        "evaluation_kind": EVALUATION_KIND,
        "execution": {
            "backend": BACKEND,
            "timeout_seconds": timeout_seconds,
            "wheel_path": wheel_path,
            "workers": 1,
        },
        "execution_target": {"branch": EXECUTION_BRANCH, "revision": arena_revision},
        "not_a_formal_result": True,
        "owner_issue": OWNER_ISSUE,
        "participants": {
            "A": candidate.to_document(),
            "B": incumbent.to_document(),
        },
        "population": population,
        "protocol_digest": PROTOCOL_DIGEST,
        "purpose": PURPOSE,
        "resource_limits": limits,
        "rules_digest": RULES_DIGEST,
        "run_kind": run_kind,
        "schema": PLAN_SCHEMA,
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
        raise EngineAabbSingleOpenGenbutsuError(
            f"invalid #524 plan: {type(error).__name__}: {error}"
        ) from error


def _fail(message: str):
    raise EngineAabbSingleOpenGenbutsuError(f"#524 {message}")


def _parse_plan(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != PLAN_SCHEMA:
        _fail("plan schema is unsupported")
    if set(value) != _PLAN_FIELDS:
        _fail("plan fields do not match")
    run_kind = value["run_kind"]
    if value["stage"] != STAGE or run_kind not in RUN_KINDS:
        _fail("plan stage or run_kind is invalid")
    if (
        value["evaluation_kind"] != EVALUATION_KIND
        or value["not_a_formal_result"] is not True
        or value["owner_issue"] != OWNER_ISSUE
        or value["purpose"] != PURPOSE
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules_digest"] != RULES_DIGEST
    ):
        _fail("plan identity drifted")
    if value["criteria"] != CRITERIA:
        _fail("criteria drifted")
    entries = value["participants"]
    if type(entries) is not dict or set(entries) != {"A", "B"}:
        _fail("participants must be A and B")
    candidate = parse_participant(entries["A"], "plan.participants.A")
    incumbent = parse_participant(entries["B"], "plan.participants.B")
    if (candidate, incumbent) != participants(candidate.lisjong_revision):
        _fail("plan must use the fixed pair")
    population = value["population"]
    expected = _ALLOCATED_FIELDS if run_kind == ALLOCATED else _REHEARSAL_FIELDS
    if type(population) is not dict or set(population) != expected:
        _fail("population fields do not match")
    seeds = _seeds(run_kind, population["ordered_seeds"])
    if population["k"] != len(seeds) or population[
        "schedule_digest"
    ] != schedule_digest(STAGE, seeds):
        _fail("population schedule drifted")
    if run_kind == ALLOCATED:
        if (
            population["population"] != POPULATION
            or population["split"] != SPLIT
            or population["seed_domain"] != SEED_DOMAIN
        ):
            _fail("population identity drifted")
        try:
            seed_registry.validate_binding_shape(
                population["allocation_binding"], seeds=seeds
            )
        except seed_registry.SeedRegistryError as error:
            raise EngineAabbSingleOpenGenbutsuError(
                f"#524 allocation binding invalid: {error}"
            ) from error
    elif population["source"] != REHEARSAL_SOURCE:
        _fail("rehearsal source drifted")
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        _fail("execution fields do not match")
    if execution["backend"] != BACKEND or execution["workers"] != 1:
        _fail("runs one rust worker")
    _positive_int(execution["timeout_seconds"], "timeout_seconds")
    if type(execution["wheel_path"]) is not str or not execution["wheel_path"]:
        _fail("wheel_path is invalid")
    _require_limits(value["resource_limits"])
    try:
        _require_environment(value["environment"], (candidate, incumbent))
    except EngineAabbLockError as error:
        raise EngineAabbSingleOpenGenbutsuError(str(error)) from error
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
    ordered_seeds: Sequence[int],
    allocation_identity: str | None,
    timeout_seconds: int,
    resource_limits: object,
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
            raise EngineAabbSingleOpenGenbutsuError(
                f"seed registry cannot be used: {error}"
            ) from error
    elif allocation_identity is not None:
        raise EngineAabbSingleOpenGenbutsuError("a rehearsal has no allocation")
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise EngineAabbSingleOpenGenbutsuError(str(error)) from error
    try:
        head = _live_head()
        environment = _live_environment(wheel_path)
    except EngineAabbLockError as error:
        raise EngineAabbSingleOpenGenbutsuError(str(error)) from error
    return assemble_plan(
        run_kind=run_kind,
        ordered_seeds=ordered_seeds,
        allocation_binding=binding,
        timeout_seconds=timeout_seconds,
        resource_limits=resource_limits,
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
        raise FileExistsError(f"refusing to overwrite #524 plan: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_plan(path: str | Path) -> dict[str, object]:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise EngineAabbSingleOpenGenbutsuError(
            f"cannot read #524 plan: {error}"
        ) from error
    return parse_plan(document)


def require_live_target(
    plan: dict,
    *,
    ledger: object,
    environment: dict | None = None,
    head: str | None = None,
) -> None:
    """実行の直前・直後に、live HEAD・実行環境（native identity）・（allocatedなら）
    allocationがplanと一致することを要求する。``environment`` / ``head`` はtestのseam。"""
    parsed = parse_plan(plan)
    try:
        live_head = _live_head() if head is None else head
        live_environment = (
            _live_environment(parsed["execution"]["wheel_path"])
            if environment is None
            else environment
        )
    except EngineAabbLockError as error:
        raise EngineAabbSingleOpenGenbutsuError(str(error)) from error
    if live_head != parsed["execution_target"]["revision"]:
        raise EngineAabbSingleOpenGenbutsuError("live Arena HEAD differs from the plan")
    if live_environment != parsed["environment"]:
        raise EngineAabbSingleOpenGenbutsuError(
            "live environment differs from the plan"
        )
    if parsed["run_kind"] == ALLOCATED:
        population = parsed["population"]
        require_stage_seeds(
            ledger, population["allocation_binding"], population["ordered_seeds"]
        )


__all__ = [
    "CANDIDATE_IDENTITY",
    "CRITERIA",
    "INCUMBENT_IDENTITY",
    "K",
    "OWNER_ISSUE",
    "PAIR",
    "PLAN_SCHEMA",
    "POPULATION",
    "SEAT_HANCHANS",
    "STOP_SCHEMA",
    "EngineAabbSingleOpenGenbutsuError",
    "assemble_plan",
    "build_plan",
    "load_plan",
    "parse_plan",
    "participants",
    "require_live_target",
    "require_stage_seeds",
    "save_plan",
]
