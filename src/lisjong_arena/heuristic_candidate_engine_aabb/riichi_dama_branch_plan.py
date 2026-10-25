"""#540 riichi-dama-branch-v1（#539）の実行前plan（DEVELOPMENT、Heuristic仮説の診断）。

#534以前のplan・protocol・digestは変えず、この診断専用の別documentを使う。

```text
participant  placement-aware-speed-call（同じexact lisjong revisionのChampion、設定{}）×4席
             D armの分岐席だけlisjong RiichiDamaBranchPolicy（inner = 新しいChampion）
run_kind     allocated  固定24 seeds 539000..539023 × 1半荘、配置の入替なし（96 seat-hanchans）
                        Seed Registryの既存engine hanchan domain、protocol riichi-dama-branch-v1、
                        owner = 後続の実行Issue。予約とreadbackは既存workflow
                        開始前の時間見積もりgate（P <= 3時間）の記録を必須にする
             rehearsal  既知の開発用seed 472005・472006（registry外）。Rを観測あり / なしの
                        別planで各1回、観測ありplanでは全D armを別processで2回実行する
上限         1半荘 / 1 arm 600秒、process tree 4 GiB、wall: rehearsal 1時間 / 本実行4時間
```

planはwrite-onceで、Champion identity・seeds・上限・判定条件・RuleSet全field・
実行環境（lisjong / native / engine revision）・保存先を実行前に固定する（``plan_digest``）。
seed・判定・上限はprotocol invariantで、callerは変えられない。
"""

import re
from fractions import Fraction
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
from .riichi_dama_branch_worker import FEATURES_VERSION, PROTOCOL_VERSION

PLAN_SCHEMA = "arena-riichi-dama-branch-v1-plan-v1"
STOP_SCHEMA = "arena-riichi-dama-branch-v1-stop-v1"
ESTIMATE_SCHEMA = "arena-riichi-dama-branch-v1-time-estimate-v1"
EVALUATION_KIND = "DEVELOPMENT"
STAGE = "riichi-dama-branch"
SPLIT = "DEVELOPMENT"
DESIGN_ISSUE = "lisbun/lisjong-arena#539"
IMPLEMENTATION_ISSUE = "lisbun/lisjong-arena#540"
ANALYSIS_ISSUE = "lisbun/lisjong#352"
ALLOCATION_PROTOCOL = PROTOCOL_VERSION
"""Seed Registryのprotocol（#539 §6: 新protocol、domainは既存engine hanchan）。"""

CHAMPION_IDENTITY = "placement-aware-speed-call"
SEEDS = tuple(range(539_000, 539_024))
"""本実行の固定seed（#539 §6）。重複があれば本versionを開始しない（別seedへ移さない）。"""
K = len(SEEDS)
SEAT_HANCHANS = 4 * K
REHEARSAL_SEEDS = (472_005, 472_006)
VERIFY_REPLAY_SEEDS = (539_000, 539_023)
"""strict verifyで再実行する固定2 seed（#539 §8。全分岐のR区間とD arm）。"""
POPULATION = f"{POPULATION_PREFIX}riichi-dama-branch-v1"
REHEARSAL_SOURCE = (
    "registry-external #472 development seeds (also used by #514 dev-check, "
    "#517 P2, #520, #524 and #534 rehearsals); R observed and unobserved once each, "
    "every D arm of the observed run twice in separate worker processes; "
    "no gate decision"
)
OBSERVED = "observed"
UNOBSERVED = "unobserved"
OBSERVATIONS = (OBSERVED, UNOBSERVED)
PURPOSE = (
    "#539 riichi-dama-branch-v1: at the first riichi of each seat and round "
    "chosen by the Champion, replay the hanchan from the start with that seat "
    "discarding the same declaration tile without riichi and staying dama for the "
    "rest of the round; the round result difference D - R of that seat in the "
    "fixed subsets S1 (high-value dama) and S3 (chasing a riichi with at most 4 "
    "unseen wait tiles); DEVELOPMENT diagnostic, not a strength result"
)

TIMEOUT_SECONDS = 600
"""1半荘（R）/ 1 arm（D）の上限。"""
RSS_LIMIT_BYTES = 4 * 1024**3
WALL_LIMIT_SECONDS = {REHEARSAL: 3600, ALLOCATED: 4 * 3600}
"""#539 §7の段階上限。全実行工程6時間・swapなしはOS側の強制（``enforcement``）で扱う。"""

BUDGET_LIMIT_SECONDS = 3 * 3600
"""§7の時間見積もりgate: P = 24 × Rの平均wall + n̂ × D armの平均wall <= 3時間。"""
MINIMUM_BRANCH_RATE = Fraction(345, 100)
"""n̂ = max(rehearsalのseat-hanchanあたりの分岐数, 3.45) × 96（#517の実測値）。"""
WITHIN_BUDGET = "WITHIN-BUDGET"
NOT_STARTED_BUDGET = "NOT-STARTED-BUDGET"

STOP_INVALID = "STOP-INVALID"
STOP_INCOMPLETE = "STOP-INCOMPLETE"
STOP_LOW_ACTIVATION = "STOP-LOW-ACTIVATION"
STOP_NO_SIGNAL = "STOP-NO-SIGNAL"
PROCEED = "PROCEED-TO-CANDIDATE-DESIGN"
DECISION_ORDER = (
    STOP_INVALID,
    STOP_INCOMPLETE,
    STOP_LOW_ACTIVATION,
    STOP_NO_SIGNAL,
    PROCEED,
)
SUBSETS = ("S1", "S3")

CRITERIA = {
    "bootstrap": {
        "clusters": "seeds with at least one branch in the subset, ascending",
        "percentile": (
            "nearest rank: ascending, the ceil(q x 10000)-th value "
            "(P10 = 1000th, P50 = 5000th, P90 = 9000th)"
        ),
        "replicates": 10_000,
        "rng": (
            "random.Random(539) initialised afresh for each subset; randrange(m) "
            "m times with replacement per replicate"
        ),
        "statistic": (
            "sum of the deltas of every branch in the drawn clusters / their count, "
            "exact fractions.Fraction"
        ),
    },
    "delta": (
        "D - R of the branch seat's round result in 100-point units: "
        "scores_after_settlement[seat] - the score at the round start in each arm, "
        "riichi stick payment, pot and honba included; a riichi stick left on the "
        "table in R counts as -1000 in R; final riichi stick awards are a separate "
        "column and not in the delta"
    ),
    "minimum_branches": 24,
    "minimum_seeds": 8,
    "order": list(DECISION_ORDER),
    "seat_hanchan_denominator": SEAT_HANCHANS,
    "seed_denominator": K,
    "signal": "mean delta > 0 and bootstrap P10 > 0",
    "subsets": {
        "S1": (
            "not furiten, W_live non-empty, and for every wait in W_live the dama "
            "ron is SCORED with a ScoreLimit of MANGAN or above"
        ),
        "S3": (
            "at least one opponent whose public riichi state is not NONE, and the "
            "unseen count summed over W is at most 4"
        ),
        "W_live": "the waits with an unseen count of at least 1",
    },
    "not_used_for_the_decision": (
        "all branches, yaku / no-yaku split, the dama-choice diagnostic, win / "
        "deal-in / draw rates; no test over branch rows; no retry, extension or "
        "extra run; S2 is not used"
    ),
}
"""判定（#539 §5・§6、実行前に固定）。結果を見てから指標・閾値・部分集合・seedを変えない。"""

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
        "features_version",
        "implementation_issue",
        "not_a_formal_result",
        "observation",
        "owner_issue",
        "participant",
        "plan_digest",
        "population",
        "protocol_digest",
        "protocol_version",
        "purpose",
        "resource_limits",
        "rules",
        "rules_digest",
        "run_kind",
        "schema",
        "stage",
        "time_estimate",
        "verify_replay_seeds",
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
_ESTIMATE_FIELDS = frozenset(
    {
        "branches",
        "branches_per_seat_hanchan",
        "budget_limit_seconds",
        "d_arms",
        "d_mean_wall_seconds",
        "environment_digest",
        "estimate_digest",
        "execution_target",
        "n_hat",
        "observed_plan_digest",
        "observed_summary_digest",
        "projected_seconds",
        "r_hanchans",
        "r_mean_wall_seconds",
        "rehearsal_seat_hanchans",
        "schema",
        "unobserved_plan_digest",
        "verdict",
    }
)


class RiichiDamaBranchError(EngineAabbProtocolError):
    """#540のplanを作れない・読めない、または保存物が再導出と一致しない場合。"""


def _fail(message: str):
    raise RiichiDamaBranchError(f"riichi-dama-branch-v1: {message}")


def champion(lisjong_revision: str) -> Participant:
    return catalog_participant(CHAMPION_IDENTITY, lisjong_revision)


def schedule(seeds) -> list[dict]:
    """Rの実行順: seed順に1半荘、4席Champion、配置の入替なし（placementは照合用の0）。"""
    return [
        {"placement": 0, "roles": [ROLE_INCUMBENT] * 4, "seed": seed} for seed in seeds
    ]


def schedule_digest(seeds) -> str:
    return document_identity(
        {
            "hanchans": schedule(seeds),
            "protocol_id": PROTOCOL_ID,
            "protocol_version": PROTOCOL_VERSION,
        }
    )


def plan_schedule(plan: dict) -> list[dict]:
    return schedule(plan["population"]["ordered_seeds"])


def plan_champion(plan: dict) -> Participant:
    return parse_participant(plan["participant"], "plan.participant")


def replay_seeds(run_kind: str) -> list[int]:
    """strict verifyで再実行するseed（rehearsalはD armの2回実行で照合するので空）。"""
    return list(VERIFY_REPLAY_SEEDS) if run_kind == ALLOCATED else []


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
    if match is None or int(match.group(1)) <= 540:
        _fail("owner_issue must be the later execution Issue lisbun/lisjong-arena#N")
    return value


def _seeds(run_kind: str, values: object) -> tuple[int, ...]:
    expected = SEEDS if run_kind == ALLOCATED else REHEARSAL_SEEDS
    if type(values) not in (list, tuple) or tuple(values) != expected:
        _fail(f"{run_kind} uses exactly the fixed seeds {expected[0]}..{expected[-1]}")
    return expected


def require_stage_seeds(ledger: object, binding: object, owner_issue: str) -> dict:
    """split DEVELOPMENT・指定owner・専用population・protocol riichi-dama-branch-v1の
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
        raise RiichiDamaBranchError(str(error)) from error


# ---------------------------------------------------------------------------
# 時間見積もりgate（§7）
# ---------------------------------------------------------------------------


def _ratio(value: Fraction) -> dict[str, int]:
    return {"denominator": value.denominator, "numerator": value.numerator}


def _parse_ratio(value: object, context: str) -> Fraction:
    if (
        type(value) is not dict
        or set(value) != {"denominator", "numerator"}
        or type(value["numerator"]) is not int
        or type(value["denominator"]) is not int
        or value["denominator"] <= 0
    ):
        _fail(f"{context} must be a ratio")
    fraction = Fraction(value["numerator"], value["denominator"])
    if (fraction.numerator, fraction.denominator) != (
        value["numerator"],
        value["denominator"],
    ):
        _fail(f"{context} must be in lowest terms")
    return fraction


def projected_seconds(
    *, r_mean_wall_seconds: float, d_mean_wall_seconds: float, n_hat: Fraction
) -> float:
    """P = 24 × Rの平均wall + n̂ × D armの平均wall（秒）。"""
    return K * r_mean_wall_seconds + float(n_hat) * d_mean_wall_seconds


def estimate_values(
    *,
    r_walls: list[float],
    d_walls: list[float],
    branches: int,
    rehearsal_seat_hanchans: int,
) -> dict:
    """rehearsalの実測から見積もりの値と判定を作る（digest・出所はcallerが加える）。"""
    if not r_walls or rehearsal_seat_hanchans <= 0:
        _fail("the estimate needs rehearsal R hanchans")
    if branches > 0 and not d_walls:
        _fail("the estimate needs the rehearsal D arm walls")
    rate = Fraction(branches, rehearsal_seat_hanchans)
    n_hat = max(rate, MINIMUM_BRANCH_RATE) * SEAT_HANCHANS
    r_mean = sum(r_walls) / len(r_walls)
    # D armが1件もない場合は、Rの平均を1 armの上限見込みとして使う（過小にしない）
    d_mean = sum(d_walls) / len(d_walls) if d_walls else r_mean
    projected = projected_seconds(
        r_mean_wall_seconds=r_mean, d_mean_wall_seconds=d_mean, n_hat=n_hat
    )
    return {
        "branches": branches,
        "branches_per_seat_hanchan": _ratio(rate),
        "budget_limit_seconds": BUDGET_LIMIT_SECONDS,
        "d_arms": len(d_walls),
        "d_mean_wall_seconds": d_mean,
        "n_hat": _ratio(n_hat),
        "projected_seconds": projected,
        "r_hanchans": len(r_walls),
        "r_mean_wall_seconds": r_mean,
        "rehearsal_seat_hanchans": rehearsal_seat_hanchans,
        "verdict": WITHIN_BUDGET
        if projected <= BUDGET_LIMIT_SECONDS
        else NOT_STARTED_BUDGET,
    }


def parse_time_estimate(value: object) -> dict:
    """見積もりdocumentの形と、記録した値からの再計算・判定・digestの一致を要求する。"""
    if type(value) is not dict or value.get("schema") != ESTIMATE_SCHEMA:
        _fail("time estimate schema is unsupported")
    if set(value) != _ESTIMATE_FIELDS:
        _fail("time estimate fields do not match")
    for name in ("r_mean_wall_seconds", "d_mean_wall_seconds", "projected_seconds"):
        if type(value[name]) is not float or not value[name] >= 0:
            _fail(f"time estimate {name} must be a non-negative float")
    for name in ("branches", "d_arms", "r_hanchans", "rehearsal_seat_hanchans"):
        if type(value[name]) is not int or value[name] < 0:
            _fail(f"time estimate {name} must be a non-negative int")
    if value["r_hanchans"] < 1:
        _fail("the time estimate needs rehearsal R hanchans")
    target = value["execution_target"]
    if type(target) is not dict or set(target) != {"branch", "revision"}:
        _fail("time estimate execution_target is invalid")
    rate = _parse_ratio(value["branches_per_seat_hanchan"], "branches_per_seat_hanchan")
    n_hat = _parse_ratio(value["n_hat"], "n_hat")
    if (
        value["rehearsal_seat_hanchans"] != 4 * value["r_hanchans"]
        or rate != Fraction(value["branches"], value["rehearsal_seat_hanchans"])
        or n_hat != max(rate, MINIMUM_BRANCH_RATE) * SEAT_HANCHANS
        or value["budget_limit_seconds"] != BUDGET_LIMIT_SECONDS
        or value["projected_seconds"]
        != projected_seconds(
            r_mean_wall_seconds=value["r_mean_wall_seconds"],
            d_mean_wall_seconds=value["d_mean_wall_seconds"],
            n_hat=n_hat,
        )
    ):
        _fail("time estimate values do not re-derive")
    expected = (
        WITHIN_BUDGET
        if value["projected_seconds"] <= BUDGET_LIMIT_SECONDS
        else NOT_STARTED_BUDGET
    )
    if value["verdict"] != expected:
        _fail("time estimate verdict does not re-derive")
    for name in (
        "environment_digest",
        "observed_plan_digest",
        "observed_summary_digest",
        "unobserved_plan_digest",
    ):
        require_hex(value[name], 64, f"time_estimate.{name}")
    payload = {key: item for key, item in value.items() if key != "estimate_digest"}
    if value["estimate_digest"] != document_identity(payload):
        _fail("time estimate digest mismatch")
    return value


def load_time_estimate(path: str | Path) -> dict:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise RiichiDamaBranchError(
            f"cannot read the time estimate: {error}"
        ) from error
    return parse_time_estimate(document)


def _require_estimate_for(plan_like: dict, estimate: object) -> None:
    parsed = parse_time_estimate(estimate)
    if parsed["verdict"] != WITHIN_BUDGET:
        _fail(
            f"{NOT_STARTED_BUDGET}: the projected wall of "
            f"{parsed['projected_seconds']:.0f} s exceeds {BUDGET_LIMIT_SECONDS} s; "
            "do not reserve seeds or start the run"
        )
    if (
        parsed["environment_digest"] != plan_like["environment_digest"]
        or parsed["execution_target"] != plan_like["execution_target"]
    ):
        _fail("the time estimate was measured with another environment or revision")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


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
    time_estimate: dict | None = None,
) -> dict[str, object]:
    """入力を検査してplanを作る（live値・ledger・見積もりはcallerが渡す）。"""
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
        raise RiichiDamaBranchError(str(error)) from error
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        _fail("Arena revision differs from the environment")
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        _fail(f"destinations must be exactly {DESTINATIONS}")
    digest = schedule_digest(seeds)
    target = {"branch": EXECUTION_BRANCH, "revision": arena_revision}
    if run_kind == ALLOCATED:
        if time_estimate is None:
            _fail("the allocated plan requires the rehearsal time estimate")
        _require_estimate_for(
            {
                "environment_digest": document_identity(environment),
                "execution_target": target,
            },
            time_estimate,
        )
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
        if time_estimate is not None:
            _fail("a rehearsal has no time estimate")
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
        "execution_target": target,
        "features_version": FEATURES_VERSION,
        "implementation_issue": IMPLEMENTATION_ISSUE,
        "not_a_formal_result": True,
        "observation": observation,
        "owner_issue": owner_issue,
        "participant": participant.to_document(),
        "population": population,
        "protocol_digest": PROTOCOL_DIGEST,
        "protocol_version": PROTOCOL_VERSION,
        "purpose": PURPOSE,
        "resource_limits": resource_limits(run_kind, limit_enforcement),
        "rules": EXPECTED_RULES,
        "rules_digest": RULES_DIGEST,
        "run_kind": run_kind,
        "schema": PLAN_SCHEMA,
        "stage": STAGE,
        "time_estimate": time_estimate,
        "verify_replay_seeds": replay_seeds(run_kind),
    }
    return parse_plan({**payload, "plan_digest": document_identity(payload)})


def parse_plan(value: object) -> dict[str, object]:
    """保存済みplanを厳密に検査する（live値・ledgerは見ない）。"""
    try:
        return _parse_plan(value)
    except EngineAabbProtocolError:
        raise
    except (ArtifactValidationError, TypeError, KeyError, ValueError) as error:
        raise RiichiDamaBranchError(
            f"invalid riichi-dama-branch-v1 plan: {type(error).__name__}: {error}"
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
        value["protocol_version"] != PROTOCOL_VERSION
        or value["features_version"] != FEATURES_VERSION
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
    if value["verify_replay_seeds"] != replay_seeds(run_kind):
        _fail("verify replay seeds drifted")
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
            raise RiichiDamaBranchError(
                f"riichi-dama-branch-v1 allocation binding invalid: {error}"
            ) from error
        if binding["seed_domain"] != SEED_DOMAIN:
            _fail("allocation binding is outside the engine hanchan domain")
        _require_estimate_for(value, value["time_estimate"])
    else:
        if population["source"] != REHEARSAL_SOURCE:
            _fail("rehearsal source drifted")
        if value["time_estimate"] is not None:
            _fail("a rehearsal has no time estimate")
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        _fail("execution fields do not match")
    if (
        execution["backend"] != BACKEND
        or execution["workers"] != 1
        or execution["timeout_seconds"] != TIMEOUT_SECONDS
    ):
        _fail("runs one rust worker with the fixed hanchan / arm timeout")
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
        raise RiichiDamaBranchError(str(error)) from error
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
    time_estimate_path: str | Path | None = None,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
) -> dict[str, object]:
    """live値（ledger、clean merged HEAD、実行環境）を集めてplanを作る（保存はしない）。"""
    ledger = binding = estimate = None
    if run_kind == ALLOCATED:
        if time_estimate_path is None:
            _fail("the allocated plan requires --time-estimate")
        estimate = load_time_estimate(time_estimate_path)
        try:
            ledger = seed_registry.load_ledger(ledger_path)
            binding = seed_registry.allocation_binding(ledger, allocation_identity)
        except seed_registry.SeedRegistryError as error:
            raise RiichiDamaBranchError(
                f"seed registry cannot be used: {error}"
            ) from error
    elif allocation_identity is not None or time_estimate_path is not None:
        _fail("a rehearsal has no allocation and no time estimate")
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise RiichiDamaBranchError(str(error)) from error
    try:
        head = _live_head()
        environment = _live_environment(wheel_path)
    except EngineAabbLockError as error:
        raise RiichiDamaBranchError(str(error)) from error
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
        time_estimate=estimate,
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
        raise RiichiDamaBranchError(f"cannot read the plan: {error}") from error
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
        raise RiichiDamaBranchError(str(error)) from error
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
    "BUDGET_LIMIT_SECONDS",
    "CHAMPION_IDENTITY",
    "CRITERIA",
    "DECISION_ORDER",
    "ESTIMATE_SCHEMA",
    "K",
    "NOT_STARTED_BUDGET",
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
    "STOP_NO_SIGNAL",
    "STOP_SCHEMA",
    "SUBSETS",
    "TIMEOUT_SECONDS",
    "UNOBSERVED",
    "VERIFY_REPLAY_SEEDS",
    "WALL_LIMIT_SECONDS",
    "WITHIN_BUDGET",
    "RiichiDamaBranchError",
    "assemble_plan",
    "build_plan",
    "champion",
    "estimate_values",
    "load_plan",
    "load_time_estimate",
    "parse_plan",
    "parse_time_estimate",
    "plan_champion",
    "plan_schedule",
    "projected_seconds",
    "replay_seeds",
    "require_live_target",
    "require_stage_seeds",
    "save_plan",
    "schedule",
    "schedule_digest",
]
