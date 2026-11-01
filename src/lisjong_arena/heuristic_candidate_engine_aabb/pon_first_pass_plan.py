"""#553 pon-first-pass-branch-v1（lisjong #362のP1 pilot）の実行前plan。

R1（#544）のplan・判定・保存物は変えず、このpilot専用の別documentを使う。

```text
participant  placement-aware-speed-call（同じexact lisjong revisionのChampion、設定{}）×4席
             D armの分岐席だけlisjong PonFirstPassBranchPolicy（inner = 新しいChampion）。
             門前の最初のPonだけPassへ替え、以後はChampionのまま（lisjong #363）
run_kind     allocated  固定24 seeds 553000..553023 × 1半荘、配置の入替なし（96 seat-hanchans）
                        Seed Registryの既存engine hanchan domain、protocol pon-first-pass-branch-v1、
                        owner = 後続の実行Issue。verifyで再実行するのは範囲の最小・最大の2 seed
                        開始条件: rehearsalからの本実行＋verify＋回収の見積もりが、3時間以内かつ
                        全工程4時間 − 経過時間（gate・rehearsal等）以内
             rehearsal  既知の開発用seed 472005・472006（registry外、動作確認だけ）
上限         1半荘 / 1 arm 600秒、process tree 4 GiB、wall: rehearsal 1時間 / 本実行は開始条件の残り
```

pilotの目的は分岐の頻度・対の分散・1 seedの費用・データ品質の確認だけで、平均Δに基づく判定は
しない。判定（``pon_first_pass_allocation``）は本学習を設計できるかの判断で、学習開始の承認では
ない。seed・判定・上限はprotocol invariantで、callerは変えられない。
"""

import math
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

from . import pon_first_pass_allocation as allocation_rules
from .kyoku_record import PROTOCOL_ID
from .lock import (
    EXECUTION_BRANCH,
    EngineAabbLockError,
    _live_environment,
    _live_head,
    _require_environment,
    require_allocated_seeds,
)
from .paijia_tie_plan import ALLOCATED, DESTINATIONS, RUN_KINDS
from .pon_first_pass_worker import CALL_ANALYSIS, PROTOCOL_VERSION, WRAPPER_VERSION
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
from .riichi_dama_branch_plan import champion, schedule

PLAN_SCHEMA = "arena-pon-first-pass-branch-v1-plan-v1"
STOP_SCHEMA = "arena-pon-first-pass-branch-v1-stop-v1"
ESTIMATE_SCHEMA = "arena-pon-first-pass-branch-v1-time-estimate-v1"
EVALUATION_KIND = "DEVELOPMENT"
STAGE = "pon-first-pass-branch"
SPLIT = "DEVELOPMENT"
DESIGN_ISSUE = "lisbun/lisjong#362"
IMPLEMENTATION_ISSUE = "lisbun/lisjong-arena#553"
WRAPPER_ISSUE = "lisbun/lisjong#363"
ALLOCATION_PROTOCOL = PROTOCOL_VERSION
"""Seed Registryのprotocol（新protocol、domainは既存engine hanchan）。"""

SEEDS = tuple(range(553_000, 553_024))
"""本実行の固定seed（24）。重複があれば開始しない（別seedへ移さない）。"""
K = len(SEEDS)
SEAT_HANCHANS = 4 * K
REHEARSAL_SEEDS = (472_005, 472_006)
VERIFY_REPLAY_SEEDS = (SEEDS[0], SEEDS[-1])
"""strict verifyで再実行する2 seed: 予約範囲の最小と最大（結果を見る前に固定する規則）。"""
POPULATION = f"{POPULATION_PREFIX}pon-first-pass-pilot-v1"
REHEARSAL_SOURCE = (
    "registry-external #472 development seeds (also used by earlier rehearsals); "
    "R observed and unobserved once each, every D arm of the observed run twice in "
    "separate worker processes; operational check only, no pilot statistic is "
    "carried forward"
)
OBSERVED = "observed"
UNOBSERVED = "unobserved"
OBSERVATIONS = (OBSERVED, UNOBSERVED)
PURPOSE = (
    "lisjong #362 P1 pilot: at the first Pon chosen by the Champion from a closed "
    "hand of each seat and round that was not cancelled by another player's ron, "
    "replay the hanchan from the start with that seat passing instead and then "
    "returning to the Champion; measure the branch frequency, the paired variance "
    "of the round result of that seat, the cost per seed and the data quality; "
    "DEVELOPMENT pilot, no decision on the mean delta, not a strength result"
)

TIMEOUT_SECONDS = 600
"""1半荘（R）/ 1 arm（D）の上限。"""
RSS_LIMIT_BYTES = 4 * 1024**3
REHEARSAL_WALL_SECONDS = 3600
MAIN_BLOCK_SECONDS = 3 * 3600
"""本実行＋verify＋回収の上限。"""
TOTAL_LIMIT_SECONDS = 4 * 3600
"""全工程（gate・rehearsal・本計測）の上限。"""
RECOVERY_ALLOWANCE_SECONDS = 900
"""見積もりに足す固定分: verifyの再実行以外（鳴き解析の再導出・hash）と回収。"""
MINIMUM_BRANCH_RATE = Fraction(155, 96)
"""見積もりの分岐率の下限: ChampionのPon全体（Arena #536の155 / 96 seat-hanchans）。

門前の最初のPonはその一部なので、時間を過小に見積もらないための上限側の値。"""
WITHIN_BUDGET = "WITHIN-BUDGET"
NOT_STARTED_BUDGET = "NOT-STARTED-BUDGET"

STOP_INVALID = "STOP-INVALID"
STOP_INCOMPLETE = "STOP-INCOMPLETE"
NOT_VIABLE_SUPPORT = allocation_rules.NOT_VIABLE_SUPPORT
DESIGN_POSSIBLE = "DESIGN-POSSIBLE"
DECISION_ORDER = (STOP_INVALID, STOP_INCOMPLETE, NOT_VIABLE_SUPPORT, DESIGN_POSSIBLE)

TARGET_EFFECT = 5
"""平均差の規模計算の基準d*（100点単位で5）。記録だけで判定に使わない。"""
ALPHA = Fraction(5, 100)
POWER = Fraction(8, 10)

CRITERIA = {
    "allocation": {
        "block_capacity": "n_blk = floor((9900 - 2c) / c)",
        "block_start": (
            "each learning block keeps its allocated seed count and starts only if "
            "(n + 2) c + 900 <= min(10800, 14400 - session elapsed) with the latest "
            "estimate; otherwise it does not start (the allocation is never shrunk "
            "after seeing results)"
        ),
        "budget_seconds": allocation_rules.COLLECTION_BUDGET_SECONDS,
        "cost_per_seed": "c = mean R wall + (established branches / seeds) x mean D wall",
        "split_seeds": (
            "T(m) = min(m n_blk, floor((21600 - m (2c + 900)) / c)); m a positive "
            "integer; valid only if n_blk >= 2 and every block has at least 2 seeds; "
            "T* = max over valid m, the smallest such m, block sizes differ by at "
            "most 1"
        ),
        "support": (
            "SUPPORT-OK if T* x branches / seeds >= 500 (projected future TEST "
            "branches; a resource allocation floor, not a statistical sufficiency "
            "condition); NOT-VIABLE-SUPPORT if not, if there is no valid allocation "
            "or if there is no established branch (the D arm mean is undefined)"
        ),
    },
    "decision": (
        "STOP-INVALID if any candidate is INVALID; STOP-INCOMPLETE if the run did "
        "not complete (no summary, no formal aggregation of the completed part); "
        "otherwise NOT-VIABLE-SUPPORT or DESIGN-POSSIBLE by the allocation. "
        "DESIGN-POSSIBLE means only that the learning can be designed from quality, "
        "data volume and cost; the power of E2 is not obtained; not an approval to "
        "start learning"
    ),
    "delta": (
        "D - R of the branch seat's round result in 100-point units, the same "
        "settlement definition as riichi-first-dama-branch-v1; final riichi stick "
        "awards are a separate column and not in the delta"
    ),
    "order": list(DECISION_ORDER),
    "population": (
        "candidates: the first Pon chosen by the Champion from a closed hand (no Chi, "
        "Pon, daiminkan or kakan; ankan allowed) of each seat and round, by the lisjong "
        "shared predicate; established candidates are branches; candidates cancelled "
        "by another player's ron are counted with their denominator and are not "
        "branches; the per-target-decision effect is (1 - cancel rate) x the "
        "per-branch effect"
    ),
    "reference_only": {
        "mean_difference_sizing": (
            "K = ceil(((z_0.95 + z_0.8) x sigma_u(P90) / d*)^2), d* = 5, recorded "
            "only, not a stop condition and not the power of E2"
        ),
        "target_effect_100_points": TARGET_EFFECT,
    },
    "variance": {
        "branch": (
            "Var(G_R), Var(G_D), Cov(G_R, G_D) and Var(delta) with n - 1, the ratio "
            "Var(delta) / (Var(G_R) + Var(G_D)); G in 100-point units"
        ),
        "bootstrap": (
            "seed clusters with at least one valid branch, ascending; "
            "random.Random(553) initialised once per summary; randrange(m) m times "
            "with replacement per replicate, 10000 replicates; P10 / P50 / P90 by "
            "nearest rank of the ratio and of sigma_u^2"
        ),
        "sigma_u": (
            "sigma_u^2 = sample variance over seeds of (sum delta_s - mu n_s) / "
            "n_bar^2, mu = sum delta / sum n, n_bar = sum n / seeds"
        ),
    },
    "not_used_for_any_decision": (
        "the mean, median and distribution of delta, the outcomes, the call analysis, "
        "the yakuhai / opponent-riichi breakdown and the calls after the branch are "
        "descriptive; no test over branch rows; no retry, extension, extra seed or "
        "subset"
    ),
}
"""判定（実行前に固定）。結果を見てから指標・閾値・seedを変えない。"""

_OWNER = re.compile(r"lisbun/lisjong-arena#([1-9][0-9]*)")
_PLAN_FIELDS = frozenset(
    {
        "artifact_destinations",
        "call_analysis_version",
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
        "protocol_version",
        "purpose",
        "resource_limits",
        "rules",
        "rules_digest",
        "run_kind",
        "schema",
        "stage",
        "start_condition",
        "verify_replay_seeds",
        "wrapper_issue",
        "wrapper_version",
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
_START_FIELDS = frozenset(
    {
        "block_limit_seconds",
        "elapsed_before_seconds",
        "projected_main_seconds",
        "remaining_seconds",
        "time_estimate",
        "total_limit_seconds",
        "verdict",
    }
)
_ESTIMATE_FIELDS = frozenset(
    {
        "branches",
        "branches_per_seat_hanchan",
        "d_arms",
        "d_mean_wall_seconds",
        "environment_digest",
        "estimate_digest",
        "execution_target",
        "n_hat",
        "n_hat_verify",
        "observed_plan_digest",
        "observed_summary_digest",
        "projected_main_seconds",
        "projected_run_seconds",
        "projected_verify_seconds",
        "r_hanchans",
        "r_mean_wall_seconds",
        "recovery_allowance_seconds",
        "rehearsal_elapsed_seconds",
        "rehearsal_seat_hanchans",
        "schema",
        "unobserved_plan_digest",
    }
)


class PonFirstPassError(EngineAabbProtocolError):
    """#553のplanを作れない・読めない、または保存物が再導出と一致しない場合。"""


def _fail(message: str):
    raise PonFirstPassError(f"pon-first-pass-branch-v1: {message}")


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


def resource_limits(run_kind: str, enforcement: object, wall_seconds: float) -> dict:
    """固定上限。``enforcement`` はOS / process treeの強制方法の説明（必須）。"""
    if type(enforcement) is not str or not enforcement.strip():
        _fail("resource_limits.enforcement must describe the OS-level enforcement")
    return {
        "enforcement": enforcement,
        "process_tree_rss_bytes": RSS_LIMIT_BYTES,
        "wall_seconds": wall_seconds,
    }


def _require_owner(value: object) -> str:
    match = _OWNER.fullmatch(value) if type(value) is str else None
    if match is None or int(match.group(1)) <= 553:
        _fail("owner_issue must be the later execution Issue lisbun/lisjong-arena#N")
    return value


def _seeds(run_kind: str, values: object) -> tuple[int, ...]:
    expected = SEEDS if run_kind == ALLOCATED else REHEARSAL_SEEDS
    if type(values) not in (list, tuple) or tuple(values) != expected:
        _fail(f"{run_kind} uses exactly the fixed seeds {expected[0]}..{expected[-1]}")
    return expected


def require_stage_seeds(ledger: object, binding: object, owner_issue: str) -> dict:
    """split DEVELOPMENT・指定owner・専用population・protocolのactive allocationで、
    全domainの他allocationと1 seedも重ならないこと。"""
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
        raise PonFirstPassError(str(error)) from error


# ---------------------------------------------------------------------------
# 時間見積もりと開始条件（全工程4時間を優先する）
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


def _projection(
    *, r_mean: float, d_mean: float, n_hat: Fraction, n_hat_verify: Fraction
) -> tuple[float, float, float]:
    run = K * r_mean + float(n_hat) * d_mean
    verify = len(VERIFY_REPLAY_SEEDS) * r_mean + float(n_hat_verify) * d_mean
    return run, verify, run + verify + RECOVERY_ALLOWANCE_SECONDS


def estimate_values(
    *,
    r_walls: list[float],
    d_walls: list[float],
    branches: int,
    rehearsal_seat_hanchans: int,
    rehearsal_elapsed_seconds: float,
) -> dict:
    """rehearsalの実測から本実行＋verify＋回収の見積もりを作る（digest・出所はcallerが加える）。"""
    if not r_walls or rehearsal_seat_hanchans <= 0:
        _fail("the estimate needs rehearsal R hanchans")
    if branches > 0 and not d_walls:
        _fail("the estimate needs the rehearsal D arm walls")
    rate = Fraction(branches, rehearsal_seat_hanchans)
    floor = max(rate, MINIMUM_BRANCH_RATE)
    n_hat = floor * SEAT_HANCHANS
    n_hat_verify = floor * 4 * len(VERIFY_REPLAY_SEEDS)
    r_mean = sum(r_walls) / len(r_walls)
    # D armが1件もない場合は、Rの平均を1 armの上限見込みとして使う（過小にしない）
    d_mean = sum(d_walls) / len(d_walls) if d_walls else r_mean
    run, verify, main = _projection(
        r_mean=r_mean, d_mean=d_mean, n_hat=n_hat, n_hat_verify=n_hat_verify
    )
    return {
        "branches": branches,
        "branches_per_seat_hanchan": _ratio(rate),
        "d_arms": len(d_walls),
        "d_mean_wall_seconds": d_mean,
        "n_hat": _ratio(n_hat),
        "n_hat_verify": _ratio(n_hat_verify),
        "projected_main_seconds": main,
        "projected_run_seconds": run,
        "projected_verify_seconds": verify,
        "r_hanchans": len(r_walls),
        "r_mean_wall_seconds": r_mean,
        "recovery_allowance_seconds": RECOVERY_ALLOWANCE_SECONDS,
        "rehearsal_elapsed_seconds": float(rehearsal_elapsed_seconds),
        "rehearsal_seat_hanchans": rehearsal_seat_hanchans,
    }


def _finite_nonnegative(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and value >= 0


def parse_time_estimate(value: object) -> dict:
    """見積もりdocumentの形と、記録した値からの再計算・digestの一致を要求する。"""
    if type(value) is not dict or value.get("schema") != ESTIMATE_SCHEMA:
        _fail("time estimate schema is unsupported")
    if set(value) != _ESTIMATE_FIELDS:
        _fail("time estimate fields do not match")
    for name in (
        "r_mean_wall_seconds",
        "d_mean_wall_seconds",
        "projected_main_seconds",
        "projected_run_seconds",
        "projected_verify_seconds",
        "rehearsal_elapsed_seconds",
    ):
        if not _finite_nonnegative(value[name]):
            _fail(f"time estimate {name} must be a finite non-negative float")
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
    n_hat_verify = _parse_ratio(value["n_hat_verify"], "n_hat_verify")
    floor = max(rate, MINIMUM_BRANCH_RATE)
    expected = _projection(
        r_mean=value["r_mean_wall_seconds"],
        d_mean=value["d_mean_wall_seconds"],
        n_hat=n_hat,
        n_hat_verify=n_hat_verify,
    )
    if (
        value["rehearsal_seat_hanchans"] != 4 * value["r_hanchans"]
        or rate != Fraction(value["branches"], value["rehearsal_seat_hanchans"])
        or n_hat != floor * SEAT_HANCHANS
        or n_hat_verify != floor * 4 * len(VERIFY_REPLAY_SEEDS)
        or value["recovery_allowance_seconds"] != RECOVERY_ALLOWANCE_SECONDS
        or (
            value["projected_run_seconds"],
            value["projected_verify_seconds"],
            value["projected_main_seconds"],
        )
        != expected
    ):
        _fail("time estimate values do not re-derive")
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
        raise PonFirstPassError(f"cannot read the time estimate: {error}") from error
    return parse_time_estimate(document)


def start_condition(estimate: dict, elapsed_before_seconds: object) -> dict:
    """全工程4時間を優先した本実行の開始条件（満たさなければNOT-STARTED-BUDGET）。

    ``elapsed_before_seconds`` は、本計測より前の全段階（gate・rehearsal等）の経過時間の
    実測合計（watchdogの max(monotonic, realtime) の和）。rehearsalの記録より小さい値は拒否する。
    """
    parsed = parse_time_estimate(estimate)
    if not _finite_nonnegative(elapsed_before_seconds):
        _fail("elapsed_before_seconds must be a finite non-negative float")
    if elapsed_before_seconds < parsed["rehearsal_elapsed_seconds"]:
        _fail("elapsed_before_seconds is smaller than the recorded rehearsal elapsed")
    remaining = TOTAL_LIMIT_SECONDS - elapsed_before_seconds
    block = min(float(MAIN_BLOCK_SECONDS), remaining)
    projected = parsed["projected_main_seconds"]
    return {
        "block_limit_seconds": block,
        "elapsed_before_seconds": elapsed_before_seconds,
        "projected_main_seconds": projected,
        "remaining_seconds": remaining,
        "time_estimate": parsed,
        "total_limit_seconds": TOTAL_LIMIT_SECONDS,
        "verdict": WITHIN_BUDGET
        if 0 < block and projected <= block
        else NOT_STARTED_BUDGET,
    }


def _require_start_condition(plan_like: dict, value: object) -> dict:
    if type(value) is not dict or set(value) != _START_FIELDS:
        _fail("start_condition fields do not match")
    expected = start_condition(value["time_estimate"], value["elapsed_before_seconds"])
    if value != expected:
        _fail("start_condition does not re-derive")
    if expected["verdict"] != WITHIN_BUDGET:
        _fail(
            f"{NOT_STARTED_BUDGET}: the projected run + verify + recovery of "
            f"{expected['projected_main_seconds']:.0f} s exceeds the block limit of "
            f"{expected['block_limit_seconds']:.0f} s (3 h and the 4 h total minus "
            "the elapsed stages); do not reserve seeds or start the run"
        )
    estimate = expected["time_estimate"]
    if (
        estimate["environment_digest"] != plan_like["environment_digest"]
        or estimate["execution_target"] != plan_like["execution_target"]
    ):
        _fail("the time estimate was measured with another environment or revision")
    return expected


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
    elapsed_before_seconds: float | None = None,
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
        raise PonFirstPassError(str(error)) from error
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        _fail("Arena revision differs from the environment")
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        _fail(f"destinations must be exactly {DESTINATIONS}")
    digest = schedule_digest(seeds)
    target = {"branch": EXECUTION_BRANCH, "revision": arena_revision}
    environment_digest = document_identity(environment)
    if run_kind == ALLOCATED:
        if time_estimate is None or elapsed_before_seconds is None:
            _fail(
                "the allocated plan requires the rehearsal time estimate and the "
                "elapsed seconds of the earlier stages"
            )
        condition = _require_start_condition(
            {"environment_digest": environment_digest, "execution_target": target},
            start_condition(time_estimate, elapsed_before_seconds),
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
        wall = condition["block_limit_seconds"]
    else:
        if allocation_binding is not None:
            _fail("a rehearsal has no allocation")
        if time_estimate is not None or elapsed_before_seconds is not None:
            _fail("a rehearsal has no time estimate and no start condition")
        condition = None
        population = {
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "schedule_digest": digest,
            "source": REHEARSAL_SOURCE,
        }
        wall = float(REHEARSAL_WALL_SECONDS)
    payload = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in DESTINATIONS
        },
        "call_analysis_version": CALL_ANALYSIS,
        "criteria": CRITERIA,
        "design_issue": DESIGN_ISSUE,
        "environment": environment,
        "environment_digest": environment_digest,
        "evaluation_kind": EVALUATION_KIND,
        "execution": {
            "backend": BACKEND,
            "timeout_seconds": TIMEOUT_SECONDS,
            "wheel_path": wheel_path,
            "workers": 1,
        },
        "execution_target": target,
        "implementation_issue": IMPLEMENTATION_ISSUE,
        "not_a_formal_result": True,
        "observation": observation,
        "owner_issue": owner_issue,
        "participant": participant.to_document(),
        "population": population,
        "protocol_digest": PROTOCOL_DIGEST,
        "protocol_version": PROTOCOL_VERSION,
        "purpose": PURPOSE,
        "resource_limits": resource_limits(run_kind, limit_enforcement, wall),
        "rules": EXPECTED_RULES,
        "rules_digest": RULES_DIGEST,
        "run_kind": run_kind,
        "schema": PLAN_SCHEMA,
        "stage": STAGE,
        "start_condition": condition,
        "verify_replay_seeds": replay_seeds(run_kind),
        "wrapper_issue": WRAPPER_ISSUE,
        "wrapper_version": WRAPPER_VERSION,
    }
    return parse_plan({**payload, "plan_digest": document_identity(payload)})


def parse_plan(value: object) -> dict[str, object]:
    """保存済みplanを厳密に検査する（live値・ledgerは見ない）。"""
    try:
        return _parse_plan(value)
    except EngineAabbProtocolError:
        raise
    except (ArtifactValidationError, TypeError, KeyError, ValueError) as error:
        raise PonFirstPassError(
            f"invalid pon-first-pass-branch-v1 plan: {type(error).__name__}: {error}"
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
        or value["call_analysis_version"] != CALL_ANALYSIS
        or value["wrapper_version"] != WRAPPER_VERSION
        or value["stage"] != STAGE
        or value["evaluation_kind"] != EVALUATION_KIND
        or value["not_a_formal_result"] is not True
        or value["design_issue"] != DESIGN_ISSUE
        or value["implementation_issue"] != IMPLEMENTATION_ISSUE
        or value["wrapper_issue"] != WRAPPER_ISSUE
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
    limits = value["resource_limits"]
    if type(limits) is not dict or set(limits) != _LIMIT_FIELDS:
        _fail("resource_limits fields do not match")
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
            raise PonFirstPassError(
                f"pon-first-pass-branch-v1 allocation binding invalid: {error}"
            ) from error
        if binding["seed_domain"] != SEED_DOMAIN:
            _fail("allocation binding is outside the engine hanchan domain")
        condition = _require_start_condition(value, value["start_condition"])
        wall = condition["block_limit_seconds"]
    else:
        if population["source"] != REHEARSAL_SOURCE:
            _fail("rehearsal source drifted")
        if value["start_condition"] is not None:
            _fail("a rehearsal has no start condition")
        wall = float(REHEARSAL_WALL_SECONDS)
    if limits != resource_limits(run_kind, limits["enforcement"], wall):
        _fail("resource limits differ from the fixed limits")
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
    try:
        _require_environment(value["environment"], (participant,))
    except EngineAabbLockError as error:
        raise PonFirstPassError(str(error)) from error
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
    elapsed_before_seconds: float | None = None,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
) -> dict[str, object]:
    """live値（ledger、clean merged HEAD、実行環境）を集めてplanを作る（保存はしない）。"""
    ledger = binding = estimate = None
    if run_kind == ALLOCATED:
        if time_estimate_path is None or elapsed_before_seconds is None:
            _fail("the allocated plan requires --time-estimate and --elapsed-seconds")
        estimate = load_time_estimate(time_estimate_path)
        try:
            ledger = seed_registry.load_ledger(ledger_path)
            binding = seed_registry.allocation_binding(ledger, allocation_identity)
        except seed_registry.SeedRegistryError as error:
            raise PonFirstPassError(f"seed registry cannot be used: {error}") from error
    elif (
        allocation_identity is not None
        or time_estimate_path is not None
        or elapsed_before_seconds is not None
    ):
        _fail("a rehearsal has no allocation, time estimate or elapsed seconds")
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise PonFirstPassError(str(error)) from error
    try:
        head = _live_head()
        environment = _live_environment(wheel_path)
    except EngineAabbLockError as error:
        raise PonFirstPassError(str(error)) from error
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
        elapsed_before_seconds=elapsed_before_seconds,
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
        raise PonFirstPassError(f"cannot read the plan: {error}") from error
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
        raise PonFirstPassError(str(error)) from error
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
    "CRITERIA",
    "DECISION_ORDER",
    "DESIGN_POSSIBLE",
    "ESTIMATE_SCHEMA",
    "K",
    "MAIN_BLOCK_SECONDS",
    "MINIMUM_BRANCH_RATE",
    "NOT_STARTED_BUDGET",
    "NOT_VIABLE_SUPPORT",
    "OBSERVATIONS",
    "OBSERVED",
    "PLAN_SCHEMA",
    "POPULATION",
    "RECOVERY_ALLOWANCE_SECONDS",
    "REHEARSAL_SEEDS",
    "RSS_LIMIT_BYTES",
    "SEAT_HANCHANS",
    "SEEDS",
    "STOP_INCOMPLETE",
    "STOP_INVALID",
    "STOP_SCHEMA",
    "TARGET_EFFECT",
    "TIMEOUT_SECONDS",
    "TOTAL_LIMIT_SECONDS",
    "UNOBSERVED",
    "VERIFY_REPLAY_SEEDS",
    "WITHIN_BUDGET",
    "PonFirstPassError",
    "assemble_plan",
    "build_plan",
    "estimate_values",
    "load_plan",
    "load_time_estimate",
    "parse_plan",
    "parse_time_estimate",
    "plan_champion",
    "plan_schedule",
    "replay_seeds",
    "require_live_target",
    "require_stage_seeds",
    "save_plan",
    "schedule_digest",
    "start_condition",
]
