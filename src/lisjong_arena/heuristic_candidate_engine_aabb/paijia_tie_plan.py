"""#520 同点数牌paijia候補の開発比較（DEVELOPMENT）の実行前plan・共通の実行枠。

正式lock（``FORMAL-EVAL``）・校正（#517）・dev-check（#514）の検査は変えず、この
実験専用の別documentを使う。結果はDEVELOPMENTで、正式のCANDIDATE SUPERIOR等は出さない。

```text
stage       activation（段階1: B自己対局8半荘、Aをshadow採点）
            screen    （段階2: 32 seeds × 6配置 = 192半荘、1回）
run_kind    allocated  Seed Registryの新規DEVELOPMENT allocation（owner #520）
            rehearsal  既知の開発用seed 472005・472006（registry外）で同じ経路を1回通す
                       動作確認。判定は出さない
```

planはwrite-onceで、pair・seeds・timeout・資源上限・判定条件・実行環境・保存先を
実行前に固定する（``plan_digest``）。defaultはない。段階2（allocated）のplanは、
段階1の保存物をverifyで再導出し、判定が ``PROCEED`` であることを作成時に要求する。
"""

import datetime
import math
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    read_json_document,
    sha256_bytes,
    staged_artifact_directory,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_new_artifact_destinations,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

from .development import catalog_participant
from .executor import (
    ExecutedHanchan,
    HanchanTask,
    PlayedHanchan,
    RunStopped,
    current_peak_rss_kib,
    run_schedule,
)
from .kyoku_record import PROTOCOL_ID, ROLE_CANDIDATE, ROLE_INCUMBENT
from .lock import (
    EXECUTION_BRANCH,
    EngineAabbLockError,
    _live_environment,
    _live_head,
    _require_environment,
    require_allocated_seeds,
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
    placement_schedule,
    require_hex,
    require_pair,
    require_seeds,
)

PLAN_SCHEMA = "arena-heuristic-candidate-engine-aabb-paijia-tie-plan-v1"
STOP_SCHEMA = "arena-heuristic-candidate-engine-aabb-paijia-tie-stop-v1"
EVALUATION_KIND = "DEVELOPMENT"
SPLIT = "DEVELOPMENT"
OWNER_ISSUE = "lisbun/lisjong-arena#520"
STOP_STATUS = "STOP-INCOMPLETE"

CANDIDATE_IDENTITY = "placement-aware-speed-call-suited-paijia-tie-v1"
INCUMBENT_IDENTITY = "placement-aware-speed-call"
PAIR = (CANDIDATE_IDENTITY, INCUMBENT_IDENTITY)
"""A（lisjong #330の候補）、B（同じrevisionの現Champion）。歴史的固定C0ではない。"""

ACTIVATION = "activation"
SCREEN = "screen"
STAGES = (ACTIVATION, SCREEN)
ALLOCATED = "allocated"
REHEARSAL = "rehearsal"
RUN_KINDS = (ALLOCATED, REHEARSAL)

STAGE_K = {ACTIVATION: 8, SCREEN: 32}
"""固定規模（Issue #520）。追加seed・早期停止・Kの変更はしない。"""
POPULATIONS = {
    ACTIVATION: f"{POPULATION_PREFIX}paijia-tie-activation-520",
    SCREEN: f"{POPULATION_PREFIX}paijia-tie-screen-520",
}
REHEARSAL_SEEDS = (472_005, 472_006)
REHEARSAL_SOURCE = (
    "registry-external #472 development seeds (also used by #514 dev-check and "
    "#517 P2); rehearsal of the same runner, no gate or progression decision"
)
PURPOSES = {
    ACTIVATION: (
        "#520 stage 1: activation frequency and time gate; B self-play x 4 seats, "
        "every decision shadow-scored by a fresh A; only B actions are applied; "
        "DEVELOPMENT, not a strength result"
    ),
    SCREEN: (
        "#520 stage 2: fixed development screen of one candidate, 32 seeds x 6 "
        "placements once; DEVELOPMENT, never a formal classification"
    ),
}
SCREEN_HANCHANS = STAGE_K[SCREEN] * 6

ACTIVATION_CRITERIA = {
    "budget": {
        "margin": 0.8,
        "projection": (
            "screen_hanchans * mean over stage-1 hanchans of "
            "(wall_seconds - (A decision seconds + B decision seconds) / 2)"
        ),
        "rule": "PASS when projection <= margin * screen wall_seconds",
        "screen_hanchans": SCREEN_HANCHANS,
    },
    "change_outcomes": ["red-only-change", "tile-type-change"],
    "excluded_from_changes": ["tsumogiri-only"],
    "invalid_outcomes": [
        "analysis-invariant-violation",
        "candidate-illegal-action",
        "out-of-scope-difference",
    ],
    "minimum_changes": 32,
    "minimum_seeds_with_change": 4,
    "order": ["STOP-INVALID", "STOP-LOW-ACTIVATION", "STOP-BUDGET", "PROCEED"],
    "seat_hanchan_denominator": STAGE_K[ACTIVATION] * 4,
}
"""段階1の判定（実行前に固定）。変更頻度は予算配分の基準で、検出力や改善の証拠ではない。"""

SCREEN_CRITERIA = {
    "conditions": {
        "base_points_mean_difference_pt_at_least": 0.0,
        "deal_in_rate_mean_difference_at_most": 0.01,
        "integrity": (
            "complete 192/192, hash-checked, separate-process verify, layer 1 and "
            "layer 2 VALID, candidate analysis consistent, within approved limits"
        ),
        "mean_d_pt_above": 0.0,
    },
    "interval": (
        "descriptive only: mean D +/- 1.96 * SD(K-1) / sqrt(K); small-K normal "
        "approximation, not a power claim and not a promotion rule"
    ),
    "labels": {
        "all_conditions": "PROMISING-DEVELOPMENT",
        "otherwise": "NOT-PROMISING-DEVELOPMENT",
    },
    "primary": "D_s = (S_A(s) - S_B(s)) / 120 pt / seat-result, seed-equal mean",
    "secondary": (
        "seed-level numerators and denominators from the stored records; "
        "denominator 0 is null; never replaces the primary decision"
    ),
}
"""段階2の進行条件（実行前に固定）。結果を見てから閾値・指標を替えない。"""

CRITERIA = {ACTIVATION: ACTIVATION_CRITERIA, SCREEN: SCREEN_CRITERIA}

PLAN_FILENAME = "plan.json"
TIMINGS_FILENAME = "timings.json"
SUMMARY_FILENAME = "summary.json"
DESTINATIONS = ("diagnostics", "output")

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
        "prerequisite",
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
_LIMIT_FIELDS = frozenset({"enforcement", "process_tree_rss_bytes", "wall_seconds"})
_PREREQUISITE_FIELDS = frozenset(
    {"activation_plan_digest", "activation_summary_digest"}
)


class EngineAabbPaijiaTieError(EngineAabbProtocolError):
    """#520のplanを作れない・読めない、または保存物が再導出と一致しない場合。"""


def participants(lisjong_revision: str) -> tuple[Participant, Participant]:
    candidate, incumbent = (
        catalog_participant(identity, lisjong_revision) for identity in PAIR
    )
    require_pair(candidate, incumbent)
    return candidate, incumbent


def schedule(stage: str, seeds: Sequence[int]) -> list[dict]:
    """実行順。段階1はseed順に1半荘（4席B、配置の入替なし）、段階2は§2.2の6配置。"""
    if stage == ACTIVATION:
        return [
            {"placement": 0, "roles": [ROLE_INCUMBENT] * 4, "seed": seed}
            for seed in seeds
        ]
    return placement_schedule(seeds)


def schedule_digest(stage: str, seeds: Sequence[int]) -> str:
    return document_identity(
        {"hanchans": schedule(stage, seeds), "protocol_id": PROTOCOL_ID, "stage": stage}
    )


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise EngineAabbPaijiaTieError(f"{name} must be an explicit positive int")
    return value


def _require_limits(value: object) -> dict:
    if type(value) is not dict or set(value) != _LIMIT_FIELDS:
        raise EngineAabbPaijiaTieError(
            f"resource_limits must have exactly {sorted(_LIMIT_FIELDS)}"
        )
    _positive_int(value["wall_seconds"], "wall_seconds")
    _positive_int(value["process_tree_rss_bytes"], "process_tree_rss_bytes")
    if type(value["enforcement"]) is not str or not value["enforcement"].strip():
        raise EngineAabbPaijiaTieError(
            "resource_limits.enforcement must describe the OS-level enforcement"
        )
    return value


def _require_criteria(stage: str, value: object) -> dict:
    if stage == SCREEN:
        if value != SCREEN_CRITERIA:
            raise EngineAabbPaijiaTieError("screen criteria drifted")
        return value
    if type(value) is not dict or set(value) != {*ACTIVATION_CRITERIA, "screen_limits"}:
        raise EngineAabbPaijiaTieError("activation criteria fields do not match")
    if {key: value[key] for key in ACTIVATION_CRITERIA} != ACTIVATION_CRITERIA:
        raise EngineAabbPaijiaTieError("activation criteria drifted")
    _require_limits(value["screen_limits"])
    return value


def _stage_seeds(stage: str, run_kind: str, values: object) -> tuple[int, ...]:
    if run_kind == REHEARSAL:
        if list(values) != list(REHEARSAL_SEEDS):
            raise EngineAabbPaijiaTieError(
                f"a rehearsal uses exactly the development seeds {REHEARSAL_SEEDS}"
            )
        return REHEARSAL_SEEDS
    seeds = require_seeds(values)
    if len(seeds) != STAGE_K[stage]:
        raise EngineAabbPaijiaTieError(
            f"stage {stage} uses exactly {STAGE_K[stage]} seeds"
        )
    return seeds


def require_stage_seeds(ledger: object, binding: object, seeds, *, stage: str) -> dict:
    """split ``DEVELOPMENT``・owner #520・段階別populationのactive allocationで、
    全domainの他allocationと1 seedも重ならないこと（段階1と段階2も重ならない）。"""
    try:
        return require_allocated_seeds(
            ledger,
            binding,
            tuple(seeds),
            owner_issue=OWNER_ISSUE,
            population=POPULATIONS[stage],
            split=SPLIT,
        )
    except EngineAabbLockError as error:
        raise EngineAabbPaijiaTieError(str(error)) from error


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def assemble_plan(
    *,
    stage: str,
    run_kind: str,
    ordered_seeds: object,
    allocation_binding: object,
    timeout_seconds: int,
    resource_limits: object,
    screen_limits: object,
    prerequisite: object,
    wheel_path: str,
    destinations: dict,
    environment: dict,
    arena_revision: str,
    ledger: object,
) -> dict[str, object]:
    """入力を検査してplanを作る（live値・ledger・前段の検証結果はcallerが渡す）。"""
    if stage not in STAGES or run_kind not in RUN_KINDS:
        raise EngineAabbPaijiaTieError(f"stage must be {STAGES}, run_kind {RUN_KINDS}")
    seeds = _stage_seeds(stage, run_kind, ordered_seeds)
    _positive_int(timeout_seconds, "timeout_seconds")
    limits = dict(_require_limits(resource_limits))
    if type(wheel_path) is not str or not Path(wheel_path).is_absolute():
        raise EngineAabbPaijiaTieError("wheel_path must be an absolute path")
    require_hex(arena_revision, 40, "arena_revision")
    if type(environment) is not dict or type(environment.get("provenance")) is not dict:
        raise EngineAabbPaijiaTieError("environment must hold provenance")
    candidate, incumbent = participants(
        environment["provenance"].get("lisjong_revision")
    )
    try:
        _require_environment(environment, (candidate, incumbent))
    except EngineAabbLockError as error:
        raise EngineAabbPaijiaTieError(str(error)) from error
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        raise EngineAabbPaijiaTieError("Arena revision differs from the environment")
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise EngineAabbPaijiaTieError(f"destinations must be exactly {DESTINATIONS}")
    criteria = dict(CRITERIA[stage])
    if stage == ACTIVATION:
        criteria["screen_limits"] = dict(_require_limits(screen_limits))
    elif screen_limits is not None:
        raise EngineAabbPaijiaTieError("screen_limits belong to the activation plan")
    digest = schedule_digest(stage, seeds)
    if run_kind == ALLOCATED:
        binding = require_stage_seeds(ledger, allocation_binding, seeds, stage=stage)
        population = {
            "allocation_binding": dict(binding),
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "population": POPULATIONS[stage],
            "schedule_digest": digest,
            "seed_domain": SEED_DOMAIN,
            "split": SPLIT,
        }
    else:
        if allocation_binding is not None:
            raise EngineAabbPaijiaTieError("a rehearsal has no allocation")
        population = {
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "schedule_digest": digest,
            "source": REHEARSAL_SOURCE,
        }
    _require_prerequisite(stage, run_kind, prerequisite, limits)
    payload = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in DESTINATIONS
        },
        "criteria": criteria,
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
            ROLE_CANDIDATE: candidate.to_document(),
            ROLE_INCUMBENT: incumbent.to_document(),
        },
        "population": population,
        "prerequisite": prerequisite,
        "protocol_digest": PROTOCOL_DIGEST,
        "purpose": PURPOSES[stage],
        "resource_limits": limits,
        "rules_digest": RULES_DIGEST,
        "run_kind": run_kind,
        "schema": PLAN_SCHEMA,
        "stage": stage,
    }
    return parse_plan({**payload, "plan_digest": document_identity(payload)})


def _require_prerequisite(
    stage: str, run_kind: str, value: object, limits: dict
) -> None:
    if stage == ACTIVATION or run_kind == REHEARSAL:
        if value is not None:
            raise EngineAabbPaijiaTieError(
                "only an allocated screen plan binds a stage-1 result"
            )
        return
    if type(value) is not dict or set(value) != _PREREQUISITE_FIELDS | {
        "screen_limits"
    }:
        raise EngineAabbPaijiaTieError(
            "an allocated screen plan must bind the verified stage-1 result"
        )
    require_hex(value["activation_plan_digest"], 64, "activation_plan_digest")
    require_hex(value["activation_summary_digest"], 64, "activation_summary_digest")
    if value["screen_limits"] != limits:
        raise EngineAabbPaijiaTieError(
            "screen resource limits differ from those fixed in the stage-1 plan"
        )


def parse_plan(value: object) -> dict[str, object]:
    """保存済みplanを厳密に検査する（live値・ledgerは見ない）。"""
    try:
        return _parse_plan(value)
    except EngineAabbProtocolError:
        raise
    except (ArtifactValidationError, TypeError, KeyError, ValueError) as error:
        raise EngineAabbPaijiaTieError(
            f"invalid #520 plan: {type(error).__name__}: {error}"
        ) from error


def _parse_plan(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != PLAN_SCHEMA:
        raise EngineAabbPaijiaTieError("unsupported #520 plan schema")
    if set(value) != _PLAN_FIELDS:
        raise EngineAabbPaijiaTieError("#520 plan fields do not match")
    stage, run_kind = value["stage"], value["run_kind"]
    if stage not in STAGES or run_kind not in RUN_KINDS:
        raise EngineAabbPaijiaTieError("#520 plan stage or run_kind is invalid")
    if (
        value["evaluation_kind"] != EVALUATION_KIND
        or value["not_a_formal_result"] is not True
        or value["owner_issue"] != OWNER_ISSUE
        or value["purpose"] != PURPOSES[stage]
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules_digest"] != RULES_DIGEST
    ):
        raise EngineAabbPaijiaTieError("#520 plan identity drifted")
    _require_criteria(stage, value["criteria"])
    entries = value["participants"]
    if type(entries) is not dict or set(entries) != {ROLE_CANDIDATE, ROLE_INCUMBENT}:
        raise EngineAabbPaijiaTieError("#520 participants must be A and B")
    candidate = parse_participant(entries[ROLE_CANDIDATE], "plan.participants.A")
    incumbent = parse_participant(entries[ROLE_INCUMBENT], "plan.participants.B")
    if (candidate, incumbent) != participants(candidate.lisjong_revision):
        raise EngineAabbPaijiaTieError("#520 plan must use the fixed pair")
    population = value["population"]
    expected = _ALLOCATED_FIELDS if run_kind == ALLOCATED else _REHEARSAL_FIELDS
    if type(population) is not dict or set(population) != expected:
        raise EngineAabbPaijiaTieError("#520 population fields do not match")
    seeds = _stage_seeds(stage, run_kind, population["ordered_seeds"])
    if population["k"] != len(seeds) or population[
        "schedule_digest"
    ] != schedule_digest(stage, seeds):
        raise EngineAabbPaijiaTieError("#520 population schedule drifted")
    if run_kind == ALLOCATED:
        if (
            population["population"] != POPULATIONS[stage]
            or population["split"] != SPLIT
            or population["seed_domain"] != SEED_DOMAIN
        ):
            raise EngineAabbPaijiaTieError("#520 population identity drifted")
        try:
            seed_registry.validate_binding_shape(
                population["allocation_binding"], seeds=seeds
            )
        except seed_registry.SeedRegistryError as error:
            raise EngineAabbPaijiaTieError(
                f"#520 allocation binding invalid: {error}"
            ) from error
    elif population["source"] != REHEARSAL_SOURCE:
        raise EngineAabbPaijiaTieError("#520 rehearsal source drifted")
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        raise EngineAabbPaijiaTieError("#520 execution fields do not match")
    if execution["backend"] != BACKEND or execution["workers"] != 1:
        raise EngineAabbPaijiaTieError("#520 runs one rust worker")
    _positive_int(execution["timeout_seconds"], "timeout_seconds")
    if type(execution["wheel_path"]) is not str or not execution["wheel_path"]:
        raise EngineAabbPaijiaTieError("#520 wheel_path is invalid")
    limits = _require_limits(value["resource_limits"])
    _require_prerequisite(stage, run_kind, value["prerequisite"], limits)
    try:
        _require_environment(value["environment"], (candidate, incumbent))
    except EngineAabbLockError as error:
        raise EngineAabbPaijiaTieError(str(error)) from error
    if value["environment_digest"] != document_identity(value["environment"]):
        raise EngineAabbPaijiaTieError("#520 environment digest mismatch")
    target = value["execution_target"]
    if type(target) is not dict or set(target) != {"branch", "revision"}:
        raise EngineAabbPaijiaTieError("#520 execution_target is invalid")
    if target["branch"] != EXECUTION_BRANCH:
        raise EngineAabbPaijiaTieError("#520 runs from main")
    require_hex(target["revision"], 40, "execution_target.revision")
    if target["revision"] != value["environment"]["provenance"].get(
        "lisjong_arena_revision"
    ):
        raise EngineAabbPaijiaTieError("plan revision differs from the environment")
    destinations = value["artifact_destinations"]
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise EngineAabbPaijiaTieError("#520 destinations do not match")
    if any(type(item) is not str or not item for item in destinations.values()):
        raise EngineAabbPaijiaTieError("#520 destinations must be strings")
    if len({Path(item).resolve(strict=False) for item in destinations.values()}) != 2:
        raise EngineAabbPaijiaTieError("#520 destinations must differ")
    payload = {key: item for key, item in value.items() if key != "plan_digest"}
    if value["plan_digest"] != document_identity(payload):
        raise EngineAabbPaijiaTieError("#520 plan digest mismatch")
    return value


def build_plan(
    *,
    stage: str,
    run_kind: str,
    ordered_seeds: object,
    allocation_identity: str | None,
    timeout_seconds: int,
    resource_limits: object,
    screen_limits: object,
    prerequisite: object,
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
            raise EngineAabbPaijiaTieError(
                f"seed registry cannot be used: {error}"
            ) from error
    elif allocation_identity is not None:
        raise EngineAabbPaijiaTieError("a rehearsal has no allocation")
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise EngineAabbPaijiaTieError(str(error)) from error
    try:
        head = _live_head()
        environment = _live_environment(wheel_path)
    except EngineAabbLockError as error:
        raise EngineAabbPaijiaTieError(str(error)) from error
    return assemble_plan(
        stage=stage,
        run_kind=run_kind,
        ordered_seeds=ordered_seeds,
        allocation_binding=binding,
        timeout_seconds=timeout_seconds,
        resource_limits=resource_limits,
        screen_limits=screen_limits,
        prerequisite=prerequisite,
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
        raise FileExistsError(f"refusing to overwrite #520 plan: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_plan(path: str | Path, *, stage: str | None = None) -> dict[str, object]:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise EngineAabbPaijiaTieError(f"cannot read #520 plan: {error}") from error
    plan = parse_plan(document)
    if stage is not None and plan["stage"] != stage:
        raise EngineAabbPaijiaTieError(
            f"plan is for stage {plan['stage']}, not {stage}"
        )
    return plan


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
        raise EngineAabbPaijiaTieError(str(error)) from error
    if live_head != parsed["execution_target"]["revision"]:
        raise EngineAabbPaijiaTieError("live Arena HEAD differs from the plan")
    if live_environment != parsed["environment"]:
        raise EngineAabbPaijiaTieError("live environment differs from the plan")
    if parsed["run_kind"] == ALLOCATED:
        population = parsed["population"]
        require_stage_seeds(
            ledger,
            population["allocation_binding"],
            population["ordered_seeds"],
            stage=parsed["stage"],
        )


def plan_participants(plan: dict) -> tuple[Participant, Participant]:
    entries = plan["participants"]
    return (
        parse_participant(entries[ROLE_CANDIDATE], "plan.participants.A"),
        parse_participant(entries[ROLE_INCUMBENT], "plan.participants.B"),
    )


def plan_schedule(plan: dict) -> list[dict]:
    return schedule(plan["stage"], plan["population"]["ordered_seeds"])


# ---------------------------------------------------------------------------
# 実行枠（上限・停止・公開）
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_stop(
    diagnostics: Path,
    plan: dict,
    error: BaseException,
    done: int,
    *,
    schema: str = STOP_SCHEMA,
):
    diagnostics.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    document = {
        "completed_hanchans": done,
        "error": f"{type(error).__name__}: {error}",
        "not_a_result": True,
        "plan_digest": plan["plan_digest"],
        "schema": schema,
        "stage": plan["stage"],
        "status": STOP_STATUS,
    }
    write_new_artifact_file(
        diagnostics / f"stop-{stamp}.json", canonical_json_text(document)
    )


def execute_plan(
    plan: dict,
    *,
    ledger_path: str | Path | None,
    play: Callable[[HanchanTask], PlayedHanchan],
    live_gate: Callable[..., None],
    publish: Callable[[Path, list[ExecutedHanchan], dict], dict],
    on_hanchan: Callable[[int, int], None] | None,
    clock: Callable[[], float],
    realtime: Callable[[], float],
    stop_schema: str = STOP_SCHEMA,
    schedule: Sequence[dict] | None = None,
    participants: tuple[Participant, Participant] | None = None,
    finalize: Callable[[Path], None] | None = None,
) -> dict[str, object]:
    """planのscheduleを1 workerで実行し、``publish(staging, executed, timings)`` が
    書いた保存物を ``<output>`` へ公開する。失敗はSTOP-INCOMPLETEで何も公開しない。

    stage全体のwall上限はmonotonicとrealtimeの両方で確かめ、長い方で判定する
    （#517でmonotonicがrealtimeより約401 s短かった）。process tree全体の強制は
    OS側（plan ``resource_limits.enforcement``）で行う。

    ``schedule`` / ``participants`` は#520と別形のplan（#534）が渡す実行順と
    (candidate, incumbent)。``None`` なら#520のplanから読む。``finalize(staging)`` は
    ``timings.json`` を書いた後、公開前に呼ぶoptionalなhook（#534の``SHA256SUMS``）。
    """
    output = Path(plan["artifact_destinations"]["output"])
    diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
    if output.exists():
        raise EngineAabbPaijiaTieError(
            "output already exists; outputs are write-once and a completed "
            "stage is never re-run"
        )
    limits = plan["resource_limits"]
    rows: list[dict] = []
    started_at = utc_now()
    started = clock()
    started_real = realtime()

    def elapsed() -> float:
        return max(clock() - started, realtime() - started_real)

    def measure(item: ExecutedHanchan) -> None:
        parent_kib = current_peak_rss_kib()
        rows.append(
            {
                "kyoku_count": len(item.capture.match.history),
                "parent_peak_rss_kib": parent_kib,
                "placement": item.capture.placement,
                "seed": item.capture.seed,
                "wall_seconds": item.wall_seconds,
                "worker_peak_rss_kib": item.peak_rss_kib,
            }
        )
        if elapsed() > limits["wall_seconds"]:
            raise RunStopped(
                f"stage exceeded the wall limit of {limits['wall_seconds']} s "
                f"after {len(rows)} hanchans"
            )
        known = [
            value for value in (parent_kib, item.peak_rss_kib) if value is not None
        ]
        if sum(known) * 1024 > limits["process_tree_rss_bytes"]:
            raise RunStopped(
                "parent + worker peak RSS exceeded the process tree limit of "
                f"{limits['process_tree_rss_bytes']} bytes"
            )

    def ledger():
        if plan["run_kind"] != ALLOCATED:
            return None
        return seed_registry.load_ledger(ledger_path)

    try:
        live_gate(plan, ledger=ledger())
        candidate, incumbent = (
            plan_participants(plan) if participants is None else participants
        )
        executed = run_schedule(
            hanchans=plan_schedule(plan) if schedule is None else list(schedule),
            candidate=candidate,
            incumbent=incumbent,
            wheel_path=plan["execution"]["wheel_path"],
            timeout_seconds=plan["execution"]["timeout_seconds"],
            expected_environment=plan["environment"],
            play=play,
            on_hanchan=on_hanchan,
            on_executed=measure,
        )
        schedule_seconds = clock() - started
        live_gate(plan, ledger=ledger())
        if any(item.policy_analysis is None for item in executed):
            raise RunStopped("a hanchan returned no Policy observation")
        with staged_artifact_directory(output) as staging:
            timings = {
                "hanchans": rows,
                "not_rederived": True,
                "plan_digest": plan["plan_digest"],
                "schedule_wall_seconds": schedule_seconds,
                "started_at_utc": started_at,
            }
            summary = publish(staging, executed, timings)
            timings["finished_at_utc"] = utc_now()
            timings["monotonic_elapsed_seconds"] = clock() - started
            timings["realtime_elapsed_seconds"] = realtime() - started_real
            write_new_artifact_file(
                staging / TIMINGS_FILENAME, canonical_json_text(timings)
            )
            if finalize is not None:
                finalize(staging)
            if elapsed() > limits["wall_seconds"]:
                raise RunStopped("stage exceeded the wall limit while saving")
    except BaseException as error:
        write_stop(diagnostics, plan, error, len(rows), schema=stop_schema)
        if not isinstance(error, Exception) or isinstance(
            error, (RunStopped, EngineAabbProtocolError)
        ):
            raise
        raise RunStopped(f"{STOP_STATUS}: {type(error).__name__}: {error}") from error
    return summary


def default_clocks() -> tuple[Callable[[], float], Callable[[], float]]:
    return time.monotonic, time.time


# ---------------------------------------------------------------------------
# 保存物の読込・検査（verify / report共通）
# ---------------------------------------------------------------------------

_TIMING_ROW_FIELDS = frozenset(
    {
        "kyoku_count",
        "parent_peak_rss_kib",
        "placement",
        "seed",
        "wall_seconds",
        "worker_peak_rss_kib",
    }
)
_TIMING_TOTAL_FIELDS = (
    "monotonic_elapsed_seconds",
    "realtime_elapsed_seconds",
    "schedule_wall_seconds",
    "write_seconds",
    "summary_seconds",
)


def read_document(path: Path, context: str) -> object:
    try:
        return read_json_document(path)
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise EngineAabbPaijiaTieError(f"cannot read {context}: {error}") from error


def seconds(value: object, context: str) -> float:
    """有限の非負数（boolは拒否）。"""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise EngineAabbPaijiaTieError(
            f"{context} must be a finite non-negative number"
        )
    return value


def rss(value: object, context: str) -> int | None:
    """正の整数（KiB）、または計測できなかったことを示す ``None``。"""
    if value is not None and (type(value) is not int or value <= 0):
        raise EngineAabbPaijiaTieError(f"{context} must be a positive int or null")
    return value


def require_timings(
    plan: dict,
    timings: object,
    kyoku_counts: Sequence[int],
    *,
    complete: bool = True,
    schedule: Sequence[dict] | None = None,
) -> dict:
    """timingsの形・値を検査し、半荘の並びと局数を保存済みの客観値と照合する。

    ``complete=False`` は書込中（保存・summaryの秒数と終了時の経過がまだない）の検査で、
    summaryが使う値（半荘ごとの行とschedule wall）だけを見る。

    実行時と同じ上限もここで再確認する: 半荘ごとの「親 + workerのpeak RSS」の既知値の和、
    schedule wall、``complete`` ならmax(monotonic経過, realtime経過)。RSSの欠測（null）は
    欠測のまま扱い、0とみなさない。verify・report・段階2の前提の作成はすべてこの完全検査を通す。
    """
    limits = plan["resource_limits"]
    if type(timings) is not dict or timings.get("plan_digest") != plan["plan_digest"]:
        raise EngineAabbPaijiaTieError("timings do not belong to the plan")
    rows = timings.get("hanchans")
    planned = plan_schedule(plan) if schedule is None else schedule
    expected = [(item["seed"], item["placement"]) for item in planned]
    if (
        type(rows) is not list
        or any(type(row) is not dict or set(row) != _TIMING_ROW_FIELDS for row in rows)
        or [(row["seed"], row["placement"]) for row in rows] != expected
    ):
        raise EngineAabbPaijiaTieError("timings do not cover the planned schedule")
    if len(kyoku_counts) != len(rows):
        raise EngineAabbPaijiaTieError("stored hanchans do not cover the schedule")
    for row, kyoku_count in zip(rows, kyoku_counts, strict=True):
        context = f"timings seed {row['seed']} placement {row['placement']}"
        seconds(row["wall_seconds"], f"{context} wall_seconds")
        kyoku = row["kyoku_count"]
        if type(kyoku) is not int or kyoku <= 0:
            raise EngineAabbPaijiaTieError(
                f"{context} kyoku_count must be a positive int"
            )
        if kyoku != kyoku_count:
            raise EngineAabbPaijiaTieError(
                f"{context} kyoku_count differs from the stored record"
            )
        known = [
            value
            for value in (
                rss(row["worker_peak_rss_kib"], f"{context} worker_peak_rss_kib"),
                rss(row["parent_peak_rss_kib"], f"{context} parent_peak_rss_kib"),
            )
            if value is not None
        ]
        if sum(known) * 1024 > limits["process_tree_rss_bytes"]:
            raise EngineAabbPaijiaTieError(
                f"{context}: parent + worker peak RSS exceeds the plan limit"
            )
    for name in _TIMING_TOTAL_FIELDS if complete else ("schedule_wall_seconds",):
        seconds(timings.get(name), f"timings {name}")
    elapsed = [timings["schedule_wall_seconds"]]
    if complete:
        elapsed += [
            timings["monotonic_elapsed_seconds"],
            timings["realtime_elapsed_seconds"],
        ]
    if max(elapsed) > limits["wall_seconds"]:
        raise EngineAabbPaijiaTieError(
            "timings exceed the stage wall limit of the plan"
        )
    return timings


def document_sha256(document: object) -> str:
    """保存したJSON documentの内容digest（書いたcanonical JSONのSHA-256）。

    summaryがPolicy側の保存物をこのdigestで拘束するので、summaryに現れない詳細
    （analysis・行動）を書き換えてもsummaryの再導出が一致しなくなる。
    """
    return sha256_bytes(canonical_json_text(document).encode("utf-8"))


def _canonical_tile(action: object, context: str) -> tuple:
    try:
        tile = action["tile"]
        return (tile["tile_type"]["category"], tile["tile_type"]["rank"]), tile[
            "is_red"
        ]
    except (TypeError, KeyError) as error:
        raise EngineAabbPaijiaTieError(f"{context}: action has no tile") from error


def discard_change_kind(incumbent: object, candidate: object, context: str) -> str:
    """保存した正規化済みの2打牌（違う行動）から、違いの種類を読む（worker側と同じ規則）。"""
    if type(incumbent) is not dict or type(candidate) is not dict:
        raise EngineAabbPaijiaTieError(f"{context}: actions must be recorded")
    if incumbent == candidate:
        raise EngineAabbPaijiaTieError(f"{context}: a change with equal actions")
    if incumbent.get("__type") != "DiscardAction" or (
        candidate.get("__type") != "DiscardAction"
    ):
        raise EngineAabbPaijiaTieError(f"{context}: a change must be two discards")
    incumbent_tile = _canonical_tile(incumbent, context)
    candidate_tile = _canonical_tile(candidate, context)
    if incumbent_tile == candidate_tile:
        return "tsumogiri-only"
    if incumbent_tile[0] == candidate_tile[0]:
        return "red-only-change"
    return "tile-type-change"


def require_switch_analysis(analysis: object, context: str) -> tuple[dict, dict]:
    """保存した ``PAIJIA_SWITCH`` のanalysis（lisjongの値の正規化表現）の形を確かめ、
    ``(champion_action, selected_action)`` を返す。paijia・評価値は再計算しない。"""
    if (
        type(analysis) is not dict
        or analysis.get("__type") != "SuitedPaijiaTieAnalysis"
        or analysis.get("stage") != "SuitedPaijiaTieStage.PAIJIA_SWITCH"
        or analysis.get("action_changed") is not True
        or type(analysis.get("paijia_evaluations")) is not list
        or len(analysis["paijia_evaluations"]) < 2
    ):
        raise EngineAabbPaijiaTieError(f"{context}: switch analysis is invalid")
    champion = analysis.get("champion_action")
    selected = analysis.get("selected_action")
    evaluated = [
        entry.get("action") if type(entry) is dict else None
        for entry in analysis["paijia_evaluations"]
    ]
    if champion not in evaluated or selected not in evaluated:
        raise EngineAabbPaijiaTieError(
            f"{context}: switch actions are not among the evaluations"
        )
    return champion, selected


def clock_document(timings: dict) -> dict[str, float]:
    """stage全体のmonotonic・realtimeの経過と差（#517の約401 sの差を監視する）。"""
    return {
        "monotonic_elapsed_seconds": timings["monotonic_elapsed_seconds"],
        "realtime_elapsed_seconds": timings["realtime_elapsed_seconds"],
        "realtime_minus_monotonic_seconds": timings["realtime_elapsed_seconds"]
        - timings["monotonic_elapsed_seconds"],
    }


def output_files(output: str | Path, *, exclude: Sequence[str] = ()) -> list[dict]:
    """保存物のpath・bytes・SHA-256。生成側と回収側の照合に使う。"""
    root = Path(output)
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in exclude:
            continue
        data = path.read_bytes()
        files.append(
            {
                "bytes": len(data),
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_bytes(data),
            }
        )
    return files


def nearest_rank(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(quantile * len(ordered))) - 1]


def distribution(values: Sequence[float]) -> dict[str, object]:
    """中央値・p95（nearest-rank）・最大・件数。空ならnull。"""
    if not values:
        return {"count": 0, "max": None, "median": None, "p95": None, "total": 0.0}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    return {
        "count": len(ordered),
        "max": ordered[-1],
        "median": median,
        "p95": nearest_rank(ordered, 0.95),
        "total": sum(ordered),
    }


def memory_summary(rows: Sequence[dict]) -> dict[str, object]:
    """RSSの欠測（null）は0として扱わず、件数を記録する。"""
    worker = [row["worker_peak_rss_kib"] for row in rows if row["worker_peak_rss_kib"]]
    parent = [row["parent_peak_rss_kib"] for row in rows if row["parent_peak_rss_kib"]]
    both = [
        row["worker_peak_rss_kib"] + row["parent_peak_rss_kib"]
        for row in rows
        if row["worker_peak_rss_kib"] and row["parent_peak_rss_kib"]
    ]
    return {
        "parent_peak_rss_kib_max": max(parent) if parent else None,
        "parent_peak_rss_missing_rows": len(rows) - len(parent),
        "sum_of_peaks_kib_max": max(both) if both else None,
        "sum_of_peaks_rows": len(both),
        "worker_peak_rss_kib_max": max(worker) if worker else None,
        "worker_peak_rss_missing_rows": len(rows) - len(worker),
    }


__all__ = [
    "ACTIVATION",
    "ACTIVATION_CRITERIA",
    "ALLOCATED",
    "CANDIDATE_IDENTITY",
    "EVALUATION_KIND",
    "INCUMBENT_IDENTITY",
    "OWNER_ISSUE",
    "PAIR",
    "PLAN_SCHEMA",
    "POPULATIONS",
    "REHEARSAL",
    "REHEARSAL_SEEDS",
    "SCREEN",
    "SCREEN_CRITERIA",
    "SCREEN_HANCHANS",
    "SPLIT",
    "STAGE_K",
    "STOP_STATUS",
    "EngineAabbPaijiaTieError",
    "assemble_plan",
    "build_plan",
    "execute_plan",
    "load_plan",
    "parse_plan",
    "participants",
    "require_live_target",
    "require_stage_seeds",
    "save_plan",
    "schedule",
]
