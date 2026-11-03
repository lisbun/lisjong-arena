"""#556 pon-first-pass-source-v1（lisjong #365 / #366のP1学習source）の実行前plan。

#553（pilot）の観測・照合・strict verifyを再利用し、seed・ブロック・split・時間管理だけを変える。
pilotのplan・保存物・判定は変えない。

```text
participant  Champion×4（同じexact lisjong revision、設定{}）。D armの分岐席だけlisjong
             PonFirstPassBranchPolicy（門前の最初のPonだけPassへ替え、以後はChampionのまま）
block        TRAIN-1 556000..556039 → TRAIN-2 556040..556079 → TRAIN-3 556080..556119
             → TEST-1 556200..556239 → TEST-2 556240..556279 → TEST-3 556280..556319
             （この順。各40 seeds。allocationはsplitごと: TRAIN 556000..556119、TEST 556200..556319）
             verifyで再実行するのは各ブロックの最小・最大seed
TEST         lisjong #366で凍結したartifactのidentityを束縛しないとplanを作れない
開始条件     seed数40のまま、最新の見積もりで次の両方を満たすときだけ作れる（満たさなければ
             NOT-STARTED-BUDGET。seed数を縮めない）
             1. (40 + 2)c + 900 ≤ min(3時間, 4時間 − sessionの経過)（#553のblock_start_condition）
             2. 累積の収集時間 + 1の見積もり ≤ 14時間
rehearsal    472005・472006（registry外、動作確認だけ）
```

summaryはΔの統計を出さない（TESTのΔをArenaで集計しない。評価はlisjong #366で1回だけ）。
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
from .paijia_tie_plan import ALLOCATED, DESTINATIONS, REHEARSAL, RUN_KINDS, output_files
from .pon_first_pass_worker import CALL_ANALYSIS, WRAPPER_VERSION
from .protocol import (
    BACKEND,
    EXPECTED_RULES,
    POPULATION_PREFIX,
    PROTOCOL_DIGEST,
    RULES_DIGEST,
    SEED_DOMAIN,
    EngineAabbProtocolError,
    parse_participant,
    require_hex,
)
from .riichi_dama_branch_plan import champion, schedule

PROTOCOL_VERSION = "pon-first-pass-source-v1"
PLAN_SCHEMA = "arena-pon-first-pass-source-v1-plan-v1"
STOP_SCHEMA = "arena-pon-first-pass-source-v1-stop-v1"
ESTIMATE_SCHEMA = "arena-pon-first-pass-source-v1-time-estimate-v1"
SCHEMAS = {
    "actions": "arena-pon-first-pass-source-v1-actions-v1",
    "branches": "arena-pon-first-pass-source-v1-branches-v1",
    "compare": "arena-pon-first-pass-source-v1-rehearsal-compare-v1",
    "hanchans": "arena-pon-first-pass-source-v1-hanchans-v1",
    "stop": STOP_SCHEMA,
    "summary": "arena-pon-first-pass-source-v1-summary-v1",
    "timings": "arena-pon-first-pass-source-v1-timings-v1",
    "verify": "arena-pon-first-pass-source-v1-verify-v1",
}
"""lisjong #366の`docs/p1-value-learning.md` §2がbranches / summaryのschemaを読む。"""
KYOKU_RECORD_SCHEMA = "arena-pon-first-pass-source-v1-kyoku-record-v1"
EVALUATION_KIND = "DEVELOPMENT"
STAGE = "pon-first-pass-source"
DESIGN_ISSUE = "lisbun/lisjong#365"
IMPLEMENTATION_ISSUE = "lisbun/lisjong-arena#556"
CONSUMER_ISSUE = "lisbun/lisjong#366"
WRAPPER_ISSUE = "lisbun/lisjong#363"
ALLOCATION_PROTOCOL = PROTOCOL_VERSION
POPULATION = f"{POPULATION_PREFIX}pon-first-pass-source-v1"
TRAIN = "TRAIN"
TEST = "TEST"
BLOCK_SEEDS = 40
"""1ブロックのseed数（配分で固定。開始条件を満たさないときも縮めない）。"""
BLOCK_ORDER = ("TRAIN-1", "TRAIN-2", "TRAIN-3", "TEST-1", "TEST-2", "TEST-3")
BLOCKS = {
    "TRAIN-1": (TRAIN, tuple(range(556_000, 556_040))),
    "TRAIN-2": (TRAIN, tuple(range(556_040, 556_080))),
    "TRAIN-3": (TRAIN, tuple(range(556_080, 556_120))),
    "TEST-1": (TEST, tuple(range(556_200, 556_240))),
    "TEST-2": (TEST, tuple(range(556_240, 556_280))),
    "TEST-3": (TEST, tuple(range(556_280, 556_320))),
}
SPLIT_SEEDS = {
    split: tuple(
        seed
        for name in BLOCK_ORDER
        if BLOCKS[name][0] == split
        for seed in BLOCKS[name][1]
    )
    for split in (TRAIN, TEST)
}
REHEARSAL_SEEDS = (472_005, 472_006)
REHEARSAL_SOURCE = (
    "registry-external #472 development seeds (also used by earlier rehearsals); "
    "R observed and unobserved once each, every D arm of the observed run twice in "
    "separate worker processes; operational check only"
)
OBSERVED = "observed"
UNOBSERVED = "unobserved"
OBSERVATIONS = (OBSERVED, UNOBSERVED)
PURPOSE = (
    "lisjong #365 P1 learning source: at the first Pon chosen by the Champion from a "
    "closed hand of each seat and round that was not cancelled by another player's "
    "ron, replay the hanchan from the start with that seat passing instead and then "
    "returning to the Champion; record the candidate rows for the TRAIN / TEST split "
    "consumed once by lisjong #366; no delta statistic is aggregated in Arena; not a "
    "strength result"
)

TIMEOUT_SECONDS = 600
RSS_LIMIT_BYTES = 4 * 1024**3
REHEARSAL_WALL_SECONDS = 3600
COLLECTION_BUDGET_SECONDS = 14 * 3600
"""収集・verify・回収の累積上限（#365の決定、worker 1換算14時間）。gate・rehearsalは別枠の準備費用。"""
MINIMUM_BRANCH_RATE = Fraction(116, 96)
"""見積もりの分岐率（seat-hanchanあたり）の下限: P1 pilot（#555）の成立116 / 96 seat-hanchans。

r̂（seedあたり）= 4 × max(見積もり元の率, 116/96)。rehearsal（2 seeds）の少ない分岐で
時間を過小に見積もらないため。"""
WITHIN_BUDGET = "WITHIN-BUDGET"
NOT_STARTED_BUDGET = "NOT-STARTED-BUDGET"
SOURCE_COMPLETE = "SOURCE-COMPLETE"
STOP_INVALID = "STOP-INVALID"
STOP_INCOMPLETE = "STOP-INCOMPLETE"
ESTIMATE_BASES = ("rehearsal", "block")

CRITERIA = {
    "decision": (
        "STOP-INVALID if any candidate is INVALID; STOP-INCOMPLETE if the block did not "
        "complete (no summary, no formal aggregation of the completed part); otherwise "
        "SOURCE-COMPLETE"
    ),
    "invalid": (
        "the #553 INVALID rules (candidate predicate, resolution, Pass application, "
        "calls after the branch, actions)"
    ),
    "no_delta_statistic": (
        "the summary holds no delta statistic; lisjong #366 evaluates the TEST split once"
    ),
    "population": (
        "candidates: the first Pon chosen by the Champion from a closed hand of each "
        "seat and round; ESTABLISHED candidates are branches with a D arm; candidates "
        "cancelled by another player's ron are counted and are not branches"
    ),
    "start": (
        "with the block size fixed at 40 seeds: (40 + 2) c + 900 <= min(3 h, 4 h - "
        "session elapsed) and collected + projected <= 14 h, c = mean R wall + r mean "
        "D wall, r = 4 max(branches / seat-hanchans, 116/96); the estimate comes from "
        "the rehearsal for TRAIN-1 and from the immediately previous block after; "
        "otherwise NOT-STARTED-BUDGET and the block is not shrunk"
    ),
}

_OWNER = re.compile(r"lisbun/lisjong-arena#([1-9][0-9]*)")
_PLAN_FIELDS = frozenset(
    {
        "artifact_destinations",
        "artifact_identity",
        "block",
        "call_analysis_version",
        "consumer_issue",
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
        "split",
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
        "block_condition",
        "collected_before_seconds",
        "collection_budget_seconds",
        "previous_blocks",
        "projected_block_seconds",
        "time_estimate",
        "verdict",
    }
)
_ESTIMATE_FIELDS = frozenset(
    {
        "basis",
        "branches",
        "branches_per_seat_hanchan",
        "branches_per_seed",
        "cost_per_seed_seconds",
        "d_arms",
        "d_mean_wall_seconds",
        "environment_digest",
        "estimate_digest",
        "execution_target",
        "projected_block_seconds",
        "r_hanchans",
        "r_mean_wall_seconds",
        "recovery_allowance_seconds",
        "schema",
        "seat_hanchans",
    }
)
_PREVIOUS_FIELDS = frozenset(
    {"block", "plan_digest", "run_elapsed_seconds", "summary_digest"}
)


class PonFirstSourceError(EngineAabbProtocolError):
    """#556のplanを作れない・読めない、または保存物が再導出と一致しない場合。"""


def _fail(message: str):
    raise PonFirstSourceError(f"pon-first-pass-source-v1: {message}")


def schedule_digest(seeds) -> str:
    return document_identity(
        {
            "hanchans": schedule(seeds),
            "protocol_id": PROTOCOL_ID,
            "protocol_version": PROTOCOL_VERSION,
        }
    )


def block_seeds(block: str | None, run_kind: str) -> tuple[int, ...]:
    if run_kind == REHEARSAL:
        if block is not None:
            _fail("a rehearsal has no block")
        return REHEARSAL_SEEDS
    if block not in BLOCKS:
        _fail(f"block must be one of {BLOCK_ORDER}")
    return BLOCKS[block][1]


def replay_seeds(block: str | None, run_kind: str) -> list[int]:
    """各ブロックの最小・最大seed（rehearsalはD armの2回実行で照合するので空）。"""
    if run_kind == REHEARSAL:
        return []
    seeds = block_seeds(block, run_kind)
    return [seeds[0], seeds[-1]]


def resource_limits(enforcement: object, wall_seconds: float) -> dict:
    if type(enforcement) is not str or not enforcement.strip():
        _fail("resource_limits.enforcement must describe the OS-level enforcement")
    return {
        "enforcement": enforcement,
        "process_tree_rss_bytes": RSS_LIMIT_BYTES,
        "wall_seconds": wall_seconds,
    }


def _require_owner(value: object) -> str:
    match = _OWNER.fullmatch(value) if type(value) is str else None
    if match is None or int(match.group(1)) <= 556:
        _fail("owner_issue must be the later execution Issue lisbun/lisjong-arena#N")
    return value


def require_stage_seeds(ledger, binding, owner_issue: str, split: str) -> dict:
    """splitのallocation（TRAIN 556000..556119 / TEST 556200..556319）で、全domainの
    他allocationと重ならないこと。"""
    try:
        return require_allocated_seeds(
            ledger,
            binding,
            SPLIT_SEEDS[split],
            owner_issue=owner_issue,
            population=POPULATION,
            split=split,
            protocol=ALLOCATION_PROTOCOL,
        )
    except EngineAabbLockError as error:
        raise PonFirstSourceError(str(error)) from error


# ---------------------------------------------------------------------------
# 時間見積もり・開始条件
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


def _finite(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and value >= 0


def _per_seed(rate: Fraction) -> Fraction:
    return 4 * max(rate, MINIMUM_BRANCH_RATE)


def _block_condition(estimate: dict, session_elapsed_seconds: float) -> dict:
    """#553の ``block_start_condition``（(n + 2)c + 900 ≤ min(3時間, 4時間 − 経過)）。"""
    try:
        return allocation_rules.block_start_condition(
            block_seeds=BLOCK_SEEDS,
            r_mean=estimate["r_mean_wall_seconds"],
            d_mean=estimate["d_mean_wall_seconds"],
            rate=_parse_ratio(estimate["branches_per_seed"], "branches_per_seed"),
            session_elapsed_seconds=session_elapsed_seconds,
        )
    except ValueError as error:
        raise PonFirstSourceError(f"pon-first-pass-source-v1: {error}") from error


def estimate_values(*, r_walls, d_walls, branches: int, seat_hanchans: int) -> dict:
    """実測（R・D armのwall、成立分岐数）からブロックの見積もりを作る（出所はcallerが加える）。"""
    if not r_walls or seat_hanchans <= 0:
        _fail("the estimate needs R hanchans")
    if branches > 0 and not d_walls:
        _fail("the estimate needs the D arm walls")
    rate = Fraction(branches, seat_hanchans)
    r_mean = sum(r_walls) / len(r_walls)
    # D armが1件もない場合は、Rの平均を1 armの上限見込みとして使う（過小にしない）
    d_mean = sum(d_walls) / len(d_walls) if d_walls else r_mean
    values = {
        "branches": branches,
        "branches_per_seat_hanchan": _ratio(rate),
        "branches_per_seed": _ratio(_per_seed(rate)),
        "d_arms": len(d_walls),
        "d_mean_wall_seconds": d_mean,
        "r_hanchans": len(r_walls),
        "r_mean_wall_seconds": r_mean,
        "recovery_allowance_seconds": allocation_rules.RECOVERY_SECONDS,
        "seat_hanchans": seat_hanchans,
    }
    condition = _block_condition(values, 0.0)
    return {
        **values,
        "cost_per_seed_seconds": condition["cost_per_seed_seconds"],
        "projected_block_seconds": condition["projected_seconds"],
    }


def parse_time_estimate(value: object) -> dict:
    if type(value) is not dict or value.get("schema") != ESTIMATE_SCHEMA:
        _fail("time estimate schema is unsupported")
    if set(value) != _ESTIMATE_FIELDS:
        _fail("time estimate fields do not match")
    for name in (
        "cost_per_seed_seconds",
        "d_mean_wall_seconds",
        "projected_block_seconds",
        "r_mean_wall_seconds",
    ):
        if not _finite(value[name]):
            _fail(f"time estimate {name} must be a finite non-negative float")
    for name in ("branches", "d_arms", "r_hanchans", "seat_hanchans"):
        if type(value[name]) is not int or value[name] < 0:
            _fail(f"time estimate {name} must be a non-negative int")
    basis = value["basis"]
    if (
        type(basis) is not dict
        or set(basis) != {"kind", "plan_digests"}
        or basis["kind"] not in ESTIMATE_BASES
        or type(basis["plan_digests"]) is not list
        or len(basis["plan_digests"]) != (2 if basis["kind"] == "rehearsal" else 1)
    ):
        _fail("time estimate basis is invalid")
    for digest in basis["plan_digests"]:
        require_hex(digest, 64, "time_estimate.basis.plan_digests")
    rate = _parse_ratio(value["branches_per_seat_hanchan"], "branches_per_seat_hanchan")
    per_seed = _parse_ratio(value["branches_per_seed"], "branches_per_seed")
    if (
        value["r_hanchans"] < 1
        or value["seat_hanchans"] != 4 * value["r_hanchans"]
        or rate != Fraction(value["branches"], value["seat_hanchans"])
        or per_seed != _per_seed(rate)
        or value["recovery_allowance_seconds"] != allocation_rules.RECOVERY_SECONDS
    ):
        _fail("time estimate values do not re-derive")
    condition = _block_condition(value, 0.0)
    if (
        value["cost_per_seed_seconds"],
        value["projected_block_seconds"],
    ) != (condition["cost_per_seed_seconds"], condition["projected_seconds"]):
        _fail("time estimate values do not re-derive")
    require_hex(value["environment_digest"], 64, "time_estimate.environment_digest")
    target = value["execution_target"]
    if type(target) is not dict or set(target) != {"branch", "revision"}:
        _fail("time estimate execution_target is invalid")
    payload = {key: item for key, item in value.items() if key != "estimate_digest"}
    if value["estimate_digest"] != document_identity(payload):
        _fail("time estimate digest mismatch")
    return value


def load_time_estimate(path: str | Path) -> dict:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise PonFirstSourceError(f"cannot read the time estimate: {error}") from error
    return parse_time_estimate(document)


def completed_block(plan: dict) -> dict:
    """ブロックのoutputがSOURCE-COMPLETEで、SHA256SUMSと実fileが一致することを確かめる。"""
    output = Path(plan["artifact_destinations"]["output"])
    try:
        recorded = (output / "SHA256SUMS").read_bytes().decode("utf-8")
        summary = read_json_document(output / "summary.json")
        timings = read_json_document(output / "timings.json")
    except (ArtifactValidationError, OSError, UnicodeDecodeError, ValueError) as error:
        raise PonFirstSourceError(
            f"block {plan['block']} output cannot be read: {error}"
        ) from error
    listed = "".join(
        f"{item['sha256']}  {item['path']}\n"
        for item in output_files(output, exclude=("SHA256SUMS",))
    )
    if recorded != listed:
        _fail(f"block {plan['block']} output differs from SHA256SUMS")
    if (
        summary.get("decision") != SOURCE_COMPLETE
        or summary.get("plan_digest") != plan["plan_digest"]
    ):
        _fail(f"block {plan['block']} is not SOURCE-COMPLETE")
    run_elapsed = max(
        timings["monotonic_elapsed_seconds"], timings["realtime_elapsed_seconds"]
    )
    return {
        "block": plan["block"],
        "plan_digest": plan["plan_digest"],
        "run_elapsed_seconds": float(run_elapsed),
        "summary_digest": summary["summary_digest"],
    }


def start_condition(
    *,
    block: str,
    estimate: dict,
    session_elapsed_seconds: object,
    collected_before_seconds: object,
    previous_blocks: list[dict],
) -> dict:
    """ブロックの開始条件（seed数40のまま）。判定値を返し、拒否はcallerが行う。"""
    parsed = parse_time_estimate(estimate)
    if block not in BLOCK_ORDER:
        _fail(f"block must be one of {BLOCK_ORDER}")
    index = BLOCK_ORDER.index(block)
    if type(previous_blocks) is not list or any(
        type(item) is not dict
        or set(item) != _PREVIOUS_FIELDS
        or not _finite(item["run_elapsed_seconds"])
        for item in previous_blocks
    ):
        _fail("previous block records are invalid")
    if [item["block"] for item in previous_blocks] != list(BLOCK_ORDER[:index]):
        _fail(
            f"{block} needs the completed blocks {list(BLOCK_ORDER[:index])} in order"
        )
    basis = parsed["basis"]
    if index == 0 and basis["kind"] != "rehearsal":
        _fail("TRAIN-1 uses the rehearsal estimate")
    if index > 0 and (
        basis["kind"] != "block"
        or basis["plan_digests"] != [previous_blocks[-1]["plan_digest"]]
    ):
        _fail(f"{block} uses the estimate from the immediately previous block")
    for name, value in (
        ("session_elapsed_seconds", session_elapsed_seconds),
        ("collected_before_seconds", collected_before_seconds),
    ):
        if not _finite(value):
            _fail(f"{name} must be a finite non-negative float")
    lower = math.fsum(item["run_elapsed_seconds"] for item in previous_blocks)
    if collected_before_seconds < lower:
        _fail(
            "collected_before_seconds is smaller than the recorded runs of the "
            "previous blocks"
        )
    condition = _block_condition(parsed, session_elapsed_seconds)
    projected = condition["projected_seconds"]
    within = (
        condition["verdict"] == allocation_rules.BLOCK_WITHIN_LIMITS
        and collected_before_seconds + projected <= COLLECTION_BUDGET_SECONDS
    )
    return {
        "block_condition": condition,
        "collected_before_seconds": collected_before_seconds,
        "collection_budget_seconds": COLLECTION_BUDGET_SECONDS,
        "previous_blocks": previous_blocks,
        "projected_block_seconds": projected,
        "time_estimate": parsed,
        "verdict": WITHIN_BUDGET if within else NOT_STARTED_BUDGET,
    }


def _require_start(plan_like: dict, value: object) -> dict:
    if type(value) is not dict or set(value) != _START_FIELDS:
        _fail("start_condition fields do not match")
    condition = value["block_condition"]
    if type(condition) is not dict:
        _fail("start_condition.block_condition is invalid")
    expected = start_condition(
        block=plan_like["block"],
        estimate=value["time_estimate"],
        session_elapsed_seconds=condition.get("session_elapsed_seconds"),
        collected_before_seconds=value["collected_before_seconds"],
        previous_blocks=value["previous_blocks"],
    )
    if value != expected:
        _fail("start_condition does not re-derive")
    if expected["verdict"] != WITHIN_BUDGET:
        block = expected["block_condition"]
        _fail(
            f"{NOT_STARTED_BUDGET}: the projected block of "
            f"{expected['projected_block_seconds']:.0f} s does not fit min(3 h, 4 h - "
            f"{block['session_elapsed_seconds']:.0f} s) or the 14 h collection with "
            f"{expected['collected_before_seconds']:.0f} s already collected; do not "
            "start and do not shrink the block"
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
    block: str | None,
    allocation_binding: object,
    artifact_identity: str | None,
    limit_enforcement: str,
    wheel_path: str,
    destinations: dict,
    environment: dict,
    arena_revision: str,
    ledger: object,
    time_estimate: dict | None = None,
    session_elapsed_seconds: float | None = None,
    collected_before_seconds: float | None = None,
    previous_blocks: list[dict] | None = None,
) -> dict[str, object]:
    """入力を検査してplanを作る（live値・ledger・見積もり・先行ブロックはcallerが渡す）。"""
    if run_kind not in RUN_KINDS:
        _fail(f"run_kind must be one of {RUN_KINDS}")
    if observation not in OBSERVATIONS:
        _fail(f"observation must be one of {OBSERVATIONS}")
    if run_kind == ALLOCATED and observation != OBSERVED:
        _fail("an allocated block is observed")
    _require_owner(owner_issue)
    seeds = block_seeds(block, run_kind)
    if type(wheel_path) is not str or not Path(wheel_path).is_absolute():
        _fail("wheel_path must be an absolute path")
    require_hex(arena_revision, 40, "arena_revision")
    if type(environment) is not dict or type(environment.get("provenance")) is not dict:
        _fail("environment must hold provenance")
    participant = champion(environment["provenance"].get("lisjong_revision"))
    try:
        _require_environment(environment, (participant,))
    except EngineAabbLockError as error:
        raise PonFirstSourceError(str(error)) from error
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        _fail("Arena revision differs from the environment")
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        _fail(f"destinations must be exactly {DESTINATIONS}")
    target = {"branch": EXECUTION_BRANCH, "revision": arena_revision}
    environment_digest = document_identity(environment)
    split = None
    if run_kind == ALLOCATED:
        split = BLOCKS[block][0]
        if split == TEST:
            require_hex(artifact_identity, 64, "artifact_identity")
        elif artifact_identity is not None:
            _fail("a TRAIN block has no artifact identity")
        if (
            time_estimate is None
            or session_elapsed_seconds is None
            or collected_before_seconds is None
        ):
            _fail(
                "an allocated block needs the estimate, the session elapsed and the "
                "collected seconds"
            )
        condition = _require_start(
            {
                "block": block,
                "environment_digest": environment_digest,
                "execution_target": target,
            },
            start_condition(
                block=block,
                estimate=time_estimate,
                session_elapsed_seconds=session_elapsed_seconds,
                collected_before_seconds=collected_before_seconds,
                previous_blocks=previous_blocks or [],
            ),
        )
        binding = require_stage_seeds(ledger, allocation_binding, owner_issue, split)
        population = {
            "allocation_binding": dict(binding),
            "allocation_protocol": ALLOCATION_PROTOCOL,
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "population": POPULATION,
            "schedule_digest": schedule_digest(seeds),
            "seed_domain": SEED_DOMAIN,
            "split": split,
        }
        wall = condition["block_condition"]["limit_seconds"]
    else:
        if (
            allocation_binding is not None
            or artifact_identity is not None
            or time_estimate is not None
            or session_elapsed_seconds is not None
            or collected_before_seconds is not None
            or previous_blocks
        ):
            _fail(
                "a rehearsal has no allocation, artifact, estimate or start condition"
            )
        condition = None
        population = {
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "schedule_digest": schedule_digest(seeds),
            "source": REHEARSAL_SOURCE,
        }
        wall = float(REHEARSAL_WALL_SECONDS)
    payload = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in DESTINATIONS
        },
        "artifact_identity": artifact_identity,
        "block": block,
        "call_analysis_version": CALL_ANALYSIS,
        "consumer_issue": CONSUMER_ISSUE,
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
        "resource_limits": resource_limits(limit_enforcement, wall),
        "rules": EXPECTED_RULES,
        "rules_digest": RULES_DIGEST,
        "run_kind": run_kind,
        "schema": PLAN_SCHEMA,
        "split": split,
        "stage": STAGE,
        "start_condition": condition,
        "verify_replay_seeds": replay_seeds(block, run_kind),
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
        raise PonFirstSourceError(
            f"invalid pon-first-pass-source-v1 plan: {type(error).__name__}: {error}"
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
        _fail("an allocated block is observed")
    if (
        value["protocol_version"] != PROTOCOL_VERSION
        or value["call_analysis_version"] != CALL_ANALYSIS
        or value["wrapper_version"] != WRAPPER_VERSION
        or value["stage"] != STAGE
        or value["evaluation_kind"] != EVALUATION_KIND
        or value["not_a_formal_result"] is not True
        or value["design_issue"] != DESIGN_ISSUE
        or value["implementation_issue"] != IMPLEMENTATION_ISSUE
        or value["consumer_issue"] != CONSUMER_ISSUE
        or value["wrapper_issue"] != WRAPPER_ISSUE
        or value["purpose"] != PURPOSE
        or value["criteria"] != CRITERIA
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules"] != EXPECTED_RULES
        or value["rules_digest"] != RULES_DIGEST
        or document_identity(value["rules"]) != RULES_DIGEST
    ):
        _fail("plan identity drifted")
    block = value["block"]
    seeds = block_seeds(block, run_kind)
    if value["verify_replay_seeds"] != replay_seeds(block, run_kind):
        _fail("verify replay seeds drifted")
    _require_owner(value["owner_issue"])
    participant = parse_participant(value["participant"], "plan.participant")
    if participant != champion(participant.lisjong_revision):
        _fail("plan must use the Champion with an empty configuration")
    population = value["population"]
    expected = _ALLOCATED_FIELDS if run_kind == ALLOCATED else _REHEARSAL_FIELDS
    if type(population) is not dict or set(population) != expected:
        _fail("population fields do not match")
    if (
        population["ordered_seeds"] != list(seeds)
        or population["k"] != len(seeds)
        or population["schedule_digest"] != schedule_digest(seeds)
    ):
        _fail("population schedule drifted")
    limits = value["resource_limits"]
    if type(limits) is not dict or set(limits) != _LIMIT_FIELDS:
        _fail("resource_limits fields do not match")
    if run_kind == ALLOCATED:
        split = BLOCKS[block][0]
        if value["split"] != split or population["split"] != split:
            _fail("split drifted")
        if split == TEST:
            require_hex(value["artifact_identity"], 64, "artifact_identity")
        elif value["artifact_identity"] is not None:
            _fail("a TRAIN block has no artifact identity")
        if (
            population["population"] != POPULATION
            or population["seed_domain"] != SEED_DOMAIN
            or population["allocation_protocol"] != ALLOCATION_PROTOCOL
        ):
            _fail("population identity drifted")
        try:
            binding = seed_registry.validate_binding_shape(
                population["allocation_binding"], seeds=SPLIT_SEEDS[split]
            )
        except seed_registry.SeedRegistryError as error:
            raise PonFirstSourceError(
                f"pon-first-pass-source-v1 allocation binding invalid: {error}"
            ) from error
        if binding["seed_domain"] != SEED_DOMAIN:
            _fail("allocation binding is outside the engine hanchan domain")
        condition = _require_start(value, value["start_condition"])
        wall = condition["block_condition"]["limit_seconds"]
    else:
        if (
            value["split"] is not None
            or value["artifact_identity"] is not None
            or value["start_condition"] is not None
            or population["source"] != REHEARSAL_SOURCE
        ):
            _fail("a rehearsal has no split, artifact or start condition")
        wall = float(REHEARSAL_WALL_SECONDS)
    if limits != resource_limits(limits["enforcement"], wall):
        _fail("resource limits differ from the fixed limits")
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        _fail("execution fields do not match")
    if (
        execution["backend"] != BACKEND
        or execution["workers"] != 1
        or execution["timeout_seconds"] != TIMEOUT_SECONDS
        or type(execution["wheel_path"]) is not str
        or not execution["wheel_path"]
    ):
        _fail("runs one rust worker with the fixed hanchan / arm timeout")
    try:
        _require_environment(value["environment"], (participant,))
    except EngineAabbLockError as error:
        raise PonFirstSourceError(str(error)) from error
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
        raise PonFirstSourceError(f"cannot read the plan: {error}") from error
    return parse_plan(document)


def previous_block_records(
    paths, *, environment_digest: str, execution_target: dict
) -> list[dict]:
    """先行ブロックのplanを順に読み、同じ環境・revisionで完了していることを確かめる。"""
    records = []
    for path in paths:
        plan = load_plan(path)
        if plan["run_kind"] != ALLOCATED:
            _fail("a previous block must be an allocated block")
        if (
            plan["environment_digest"] != environment_digest
            or plan["execution_target"] != execution_target
        ):
            _fail("a previous block ran with another environment or revision")
        records.append(completed_block(plan))
    return records


def build_plan(
    *,
    run_kind: str,
    observation: str,
    owner_issue: str,
    block: str | None,
    allocation_identity: str | None,
    artifact_identity: str | None,
    limit_enforcement: str,
    wheel_path: str,
    destinations: dict,
    time_estimate_path: str | Path | None = None,
    session_elapsed_seconds: float | None = None,
    collected_before_seconds: float | None = None,
    previous_plans=(),
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
) -> dict[str, object]:
    """live値（ledger、clean merged HEAD、実行環境、先行ブロック）を集めてplanを作る。"""
    ledger = binding = estimate = None
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise PonFirstSourceError(str(error)) from error
    try:
        head = _live_head()
        environment = _live_environment(wheel_path)
    except EngineAabbLockError as error:
        raise PonFirstSourceError(str(error)) from error
    target = {"branch": EXECUTION_BRANCH, "revision": head}
    previous = []
    if run_kind == ALLOCATED:
        if time_estimate_path is None:
            _fail("an allocated block requires --time-estimate")
        estimate = load_time_estimate(time_estimate_path)
        previous = previous_block_records(
            previous_plans,
            environment_digest=document_identity(environment),
            execution_target=target,
        )
        try:
            ledger = seed_registry.load_ledger(ledger_path)
            binding = seed_registry.allocation_binding(ledger, allocation_identity)
        except seed_registry.SeedRegistryError as error:
            raise PonFirstSourceError(
                f"seed registry cannot be used: {error}"
            ) from error
    elif (
        allocation_identity is not None
        or time_estimate_path is not None
        or previous_plans
    ):
        _fail("a rehearsal has no allocation, estimate or previous blocks")
    return assemble_plan(
        run_kind=run_kind,
        observation=observation,
        owner_issue=owner_issue,
        block=block,
        allocation_binding=binding,
        artifact_identity=artifact_identity,
        limit_enforcement=limit_enforcement,
        wheel_path=wheel_path,
        destinations=destinations,
        environment=environment,
        arena_revision=head,
        ledger=ledger,
        time_estimate=estimate,
        session_elapsed_seconds=session_elapsed_seconds,
        collected_before_seconds=collected_before_seconds,
        previous_blocks=previous,
    )


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
        raise PonFirstSourceError(str(error)) from error
    if live_head != parsed["execution_target"]["revision"]:
        _fail("live Arena HEAD differs from the plan")
    if live_environment != parsed["environment"]:
        _fail("live environment differs from the plan")
    if parsed["run_kind"] == ALLOCATED:
        require_stage_seeds(
            ledger,
            parsed["population"]["allocation_binding"],
            parsed["owner_issue"],
            parsed["split"],
        )


__all__ = [
    "ALLOCATION_PROTOCOL",
    "BLOCKS",
    "BLOCK_ORDER",
    "BLOCK_SEEDS",
    "COLLECTION_BUDGET_SECONDS",
    "CRITERIA",
    "ESTIMATE_SCHEMA",
    "KYOKU_RECORD_SCHEMA",
    "MINIMUM_BRANCH_RATE",
    "NOT_STARTED_BUDGET",
    "OBSERVATIONS",
    "OBSERVED",
    "PLAN_SCHEMA",
    "POPULATION",
    "PROTOCOL_VERSION",
    "REHEARSAL_SEEDS",
    "SCHEMAS",
    "SOURCE_COMPLETE",
    "SPLIT_SEEDS",
    "STOP_INCOMPLETE",
    "STOP_INVALID",
    "STOP_SCHEMA",
    "TEST",
    "TRAIN",
    "UNOBSERVED",
    "WITHIN_BUDGET",
    "PonFirstSourceError",
    "assemble_plan",
    "block_seeds",
    "build_plan",
    "completed_block",
    "estimate_values",
    "load_plan",
    "load_time_estimate",
    "parse_plan",
    "parse_time_estimate",
    "previous_block_records",
    "replay_seeds",
    "require_live_target",
    "require_stage_seeds",
    "save_plan",
    "start_condition",
]
