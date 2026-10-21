"""限定校正（CALIBRATION、#517、仕様§11）。

正式runnerと同じexecutor・record・block統計・I/O検査を使い、固定したpair・構成で
32 seed blocksを1回実行して、block差SDと時間・RSS・記録容量を測る。強さの比較・
正式判定ではない。正式lock（``FORMAL-EVAL``）・dev-check（既知12半荘）の検査は
緩めず、このprotocol専用の別documentを使う。

```text
plan     pair・seeds（split CALIBRATION のallocation）・設計値・timeout・資源上限・環境・
         保存先を実行前に固定する（write-once、plan_digest）
run      live gate -> 192半荘（seed順 -> placement 0..5） -> live gate
         -> 第1層・第2層・scheduleを書込前に検査 -> <output>/{raw,summary.json,timings.json}
verify   保存したplan・rawだけからsummaryを再導出し完全一致を求める（別process、offline）
report   summary・timings・保存fileから時間・RSS・容量・J1 / J2の数値を作る（再導出可能）
```

- 第2層も必須（``event_facts_required=True``）。どちらかが無効ならSTOP
- 失敗・timeout・上限超過はSTOP / INCOMPLETE。成功分だけのsummary、seedの差し替え、
  再試行、条件の緩和はしない。``diagnostics/stop-<UTC>.json`` は結果ではない
- summaryはblock差・SD・J1感度だけを持ち、D平均・区間・classificationを出さない
  （校正のD平均の符号・区間を採否に使わない）
"""

import datetime
import math
import random
import statistics as stats
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
    play_hanchan,
    run_schedule,
)
from .kyoku_record import ROLE_CANDIDATE, ROLE_INCUMBENT, verify_schedule
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
    INTERNAL_UNITS_PER_POINT,
    POPULATION_PREFIX,
    PROTOCOL_DIGEST,
    RULES_DIGEST,
    SEAT_RESULTS_PER_ROLE_PER_BLOCK,
    SEED_DOMAIN,
    EngineAabbProtocolError,
    Participant,
    expected_schedule,
    parse_participant,
    require_hex,
    require_seeds,
    schedule_document,
)
from .record_store import (
    RecordBindings,
    build_records,
    read_kyoku_records,
    require_bindings,
    write_kyoku_records,
)
from .secondary_metrics import summarize_secondary_metrics
from .statistics import block_totals

PLAN_SCHEMA = "arena-heuristic-candidate-engine-aabb-calibration-plan-v1"
SUMMARY_SCHEMA = "arena-heuristic-candidate-engine-aabb-calibration-summary-v1"
REPORT_SCHEMA = "arena-heuristic-candidate-engine-aabb-calibration-report-v1"
STOP_SCHEMA = "arena-heuristic-candidate-engine-aabb-calibration-stop-v1"
EVALUATION_KIND = "CALIBRATION"
CALIBRATION_SPLIT = "CALIBRATION"
OWNER_ISSUE = "lisbun/lisjong-arena#517"
POPULATION = f"{POPULATION_PREFIX}calibration-517"
COMPLETE_STATUS = "CALIBRATION COMPLETE; NOT A STRENGTH RESULT"
STOP_STATUS = "STOP / INCOMPLETE"
PURPOSE = (
    "limited calibration (spec §11): block-difference SD, time, RSS and record size "
    "for one fixed pair and configuration; not a strength result"
)
CALIBRATION_PAIR = (
    "one-shanten-defense-placement-aware-speed-call",
    "placement-aware-speed-call",
)
"""A（candidate）、B（incumbent = current-champion）の順。#514 dev-checkとは逆向き。"""
INCUMBENT_ROLE = "current-champion"
CALIBRATION_K = 32
"""固定したpilot規模。正式評価の最小Kではない（J1は別に決める）。"""

DESIGN = {
    "alpha_two_sided": 0.05,
    "bootstrap_quantile": 0.95,
    "bootstrap_quantile_method": "nearest-rank",
    "bootstrap_replicates": 10_000,
    "bootstrap_seed": 514,
    "bootstrap_unit": "seed-block",
    "delta_pt_per_seat_result": 1.0,
    "k_plan_formula": "ceil(((z_alpha + z_beta) * sigma / delta) ** 2)",
    "power": 0.8,
    "z_alpha": 1.959964,
    "z_beta": 0.841621,
}
"""J1の設計仮説（実行前に固定。観測したD平均で置き換えない）。"""

PLAN_FILENAME = "plan.json"
RAW_DIRNAME = "raw"
SUMMARY_FILENAME = "summary.json"
TIMINGS_FILENAME = "timings.json"
REPORT_FILENAME = "report.json"
DESTINATIONS = ("diagnostics", "output")

_DIVISOR = SEAT_RESULTS_PER_ROLE_PER_BLOCK * INTERNAL_UNITS_PER_POINT
_PLAN_FIELDS = frozenset(
    {
        "artifact_destinations",
        "design",
        "environment",
        "environment_digest",
        "evaluation_kind",
        "execution",
        "execution_target",
        "incumbent_role",
        "not_a_strength_result",
        "participants",
        "plan_digest",
        "population",
        "protocol_digest",
        "purpose",
        "resource_limits",
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
_LIMIT_FIELDS = frozenset(
    {"enforcement", "process_tree_rss_bytes", "wall_seconds_per_stage"}
)


class EngineAabbCalibrationError(EngineAabbProtocolError):
    """校正planを作れない・読めない、または校正の保存物が再導出と一致しない場合。"""


def calibration_participants(lisjong_revision: str) -> tuple[Participant, Participant]:
    return tuple(
        catalog_participant(identity, lisjong_revision) for identity in CALIBRATION_PAIR
    )


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise EngineAabbCalibrationError(f"{name} must be an explicit positive int")
    return value


def _require_limits(value: object) -> dict:
    if type(value) is not dict or set(value) != _LIMIT_FIELDS:
        raise EngineAabbCalibrationError(
            f"resource_limits must have exactly {sorted(_LIMIT_FIELDS)}"
        )
    _positive_int(value["wall_seconds_per_stage"], "wall_seconds_per_stage")
    _positive_int(value["process_tree_rss_bytes"], "process_tree_rss_bytes")
    if type(value["enforcement"]) is not str or not value["enforcement"].strip():
        raise EngineAabbCalibrationError(
            "resource_limits.enforcement must describe the OS-level enforcement"
        )
    return value


def _calibration_seeds(values: object) -> tuple[int, ...]:
    seeds = require_seeds(values)
    if len(seeds) != CALIBRATION_K:
        raise EngineAabbCalibrationError(
            f"calibration uses exactly {CALIBRATION_K} seed blocks"
        )
    return seeds


def require_calibration_seeds(ledger: object, binding: object, seeds) -> dict:
    """split ``CALIBRATION`` のallocationで、他のどのallocationとも重ならないこと。"""
    return require_allocated_seeds(
        ledger,
        binding,
        tuple(seeds),
        owner_issue=OWNER_ISSUE,
        population=POPULATION,
        split=CALIBRATION_SPLIT,
    )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def assemble_plan(
    *,
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
    """入力を検査して校正planを作る（live値はcallerが渡す）。"""
    seeds = _calibration_seeds(ordered_seeds)
    _positive_int(timeout_seconds, "timeout_seconds")
    limits = dict(_require_limits(resource_limits))
    if type(wheel_path) is not str or not Path(wheel_path).is_absolute():
        raise EngineAabbCalibrationError("wheel_path must be an absolute path")
    require_hex(arena_revision, 40, "arena_revision")
    if type(environment) is not dict or type(environment.get("provenance")) is not dict:
        raise EngineAabbCalibrationError("environment must hold provenance")
    lisjong = environment["provenance"].get("lisjong_revision")
    candidate, incumbent = calibration_participants(lisjong)
    _require_environment(environment, (candidate, incumbent))
    if environment["provenance"].get("lisjong_arena_revision") != arena_revision:
        raise EngineAabbCalibrationError("Arena revision differs from the environment")
    try:
        binding = require_calibration_seeds(ledger, allocation_binding, seeds)
    except EngineAabbLockError as error:
        raise EngineAabbCalibrationError(str(error)) from error
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise EngineAabbCalibrationError(f"destinations must be exactly {DESTINATIONS}")
    payload = {
        "artifact_destinations": {
            name: str(destinations[name]) for name in DESTINATIONS
        },
        "design": dict(DESIGN),
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
        "incumbent_role": INCUMBENT_ROLE,
        "not_a_strength_result": True,
        "participants": {
            ROLE_CANDIDATE: candidate.to_document(),
            ROLE_INCUMBENT: incumbent.to_document(),
        },
        "population": {
            "allocation_binding": dict(binding),
            "k": len(seeds),
            "ordered_seeds": list(seeds),
            "owner_issue": OWNER_ISSUE,
            "population": POPULATION,
            "schedule_digest": document_identity(schedule_document(seeds)),
            "seed_domain": SEED_DOMAIN,
            "split": CALIBRATION_SPLIT,
        },
        "protocol_digest": PROTOCOL_DIGEST,
        "purpose": PURPOSE,
        "resource_limits": limits,
        "rules_digest": RULES_DIGEST,
        "schema": PLAN_SCHEMA,
    }
    return parse_plan({**payload, "plan_digest": document_identity(payload)})


def parse_plan(value: object) -> dict[str, object]:
    """保存済みplanを厳密に検査する（live値・ledgerは見ない）。"""
    try:
        return _parse_plan(value)
    except EngineAabbProtocolError:
        raise
    except (ArtifactValidationError, TypeError, KeyError, ValueError) as error:
        raise EngineAabbCalibrationError(
            f"invalid calibration plan: {type(error).__name__}: {error}"
        ) from error


def _parse_plan(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("schema") != PLAN_SCHEMA:
        raise EngineAabbCalibrationError("unsupported calibration plan schema")
    if set(value) != _PLAN_FIELDS:
        raise EngineAabbCalibrationError("calibration plan fields do not match")
    if (
        value["evaluation_kind"] != EVALUATION_KIND
        or value["not_a_strength_result"] is not True
        or value["purpose"] != PURPOSE
        or value["incumbent_role"] != INCUMBENT_ROLE
        or value["design"] != DESIGN
        or value["protocol_digest"] != PROTOCOL_DIGEST
        or value["rules_digest"] != RULES_DIGEST
    ):
        raise EngineAabbCalibrationError("calibration plan identity drifted")
    participants = value["participants"]
    if type(participants) is not dict or set(participants) != {
        ROLE_CANDIDATE,
        ROLE_INCUMBENT,
    }:
        raise EngineAabbCalibrationError("calibration participants must be A and B")
    candidate = parse_participant(participants[ROLE_CANDIDATE], "plan.participants.A")
    incumbent = parse_participant(participants[ROLE_INCUMBENT], "plan.participants.B")
    if (candidate.policy_identity, incumbent.policy_identity) != CALIBRATION_PAIR:
        raise EngineAabbCalibrationError("calibration plan must use the fixed pair")
    if (candidate, incumbent) != calibration_participants(candidate.lisjong_revision):
        raise EngineAabbCalibrationError("calibration participants drifted")
    population = value["population"]
    if type(population) is not dict or set(population) != _POPULATION_FIELDS:
        raise EngineAabbCalibrationError("calibration population fields do not match")
    seeds = _calibration_seeds(population["ordered_seeds"])
    if (
        population["k"] != len(seeds)
        or population["owner_issue"] != OWNER_ISSUE
        or population["population"] != POPULATION
        or population["split"] != CALIBRATION_SPLIT
        or population["seed_domain"] != SEED_DOMAIN
        or population["schedule_digest"] != document_identity(schedule_document(seeds))
    ):
        raise EngineAabbCalibrationError("calibration population identity drifted")
    try:
        seed_registry.validate_binding_shape(
            population["allocation_binding"], seeds=seeds
        )
    except seed_registry.SeedRegistryError as error:
        raise EngineAabbCalibrationError(
            f"calibration allocation binding invalid: {error}"
        ) from error
    execution = value["execution"]
    if type(execution) is not dict or set(execution) != _EXECUTION_FIELDS:
        raise EngineAabbCalibrationError("calibration execution fields do not match")
    if execution["backend"] != BACKEND or execution["workers"] != 1:
        raise EngineAabbCalibrationError("calibration runs one rust worker")
    _positive_int(execution["timeout_seconds"], "timeout_seconds")
    if type(execution["wheel_path"]) is not str or not execution["wheel_path"]:
        raise EngineAabbCalibrationError("calibration wheel_path is invalid")
    _require_limits(value["resource_limits"])
    _require_environment(value["environment"], (candidate, incumbent))
    if value["environment_digest"] != document_identity(value["environment"]):
        raise EngineAabbCalibrationError("calibration environment digest mismatch")
    target = value["execution_target"]
    if type(target) is not dict or set(target) != {"branch", "revision"}:
        raise EngineAabbCalibrationError("calibration execution_target is invalid")
    if target["branch"] != EXECUTION_BRANCH:
        raise EngineAabbCalibrationError("calibration runs from main")
    require_hex(target["revision"], 40, "execution_target.revision")
    if target["revision"] != value["environment"]["provenance"].get(
        "lisjong_arena_revision"
    ):
        raise EngineAabbCalibrationError("plan revision differs from the environment")
    destinations = value["artifact_destinations"]
    if type(destinations) is not dict or set(destinations) != set(DESTINATIONS):
        raise EngineAabbCalibrationError("calibration destinations do not match")
    if any(type(item) is not str or not item for item in destinations.values()):
        raise EngineAabbCalibrationError("calibration destinations must be strings")
    if len({Path(item).resolve(strict=False) for item in destinations.values()}) != 2:
        raise EngineAabbCalibrationError("calibration destinations must differ")
    payload = {key: item for key, item in value.items() if key != "plan_digest"}
    if value["plan_digest"] != document_identity(payload):
        raise EngineAabbCalibrationError("calibration plan digest mismatch")
    return value


def build_plan(
    *,
    ordered_seeds: object,
    allocation_identity: str,
    timeout_seconds: int,
    resource_limits: object,
    wheel_path: str,
    destinations: dict,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
) -> dict[str, object]:
    """live値（ledger、clean merged HEAD、実行環境）を集めてplanを作る（保存はしない）。"""
    try:
        ledger = seed_registry.load_ledger(ledger_path)
        binding = seed_registry.allocation_binding(ledger, allocation_identity)
    except seed_registry.SeedRegistryError as error:
        raise EngineAabbCalibrationError(
            f"seed registry cannot be used: {error}"
        ) from error
    try:
        require_new_artifact_destinations(destinations, required_names=DESTINATIONS)
    except ExecutionSafetyError as error:
        raise EngineAabbCalibrationError(str(error)) from error
    head = _live_head()
    return assemble_plan(
        ordered_seeds=ordered_seeds,
        allocation_binding=binding,
        timeout_seconds=timeout_seconds,
        resource_limits=resource_limits,
        wheel_path=wheel_path,
        destinations=destinations,
        environment=_live_environment(wheel_path),
        arena_revision=head,
        ledger=ledger,
    )


def save_plan(document: dict, path: str | Path) -> Path:
    parse_plan(document)
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite calibration plan: {destination}")
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_plan(path: str | Path) -> dict[str, object]:
    try:
        document = read_json_document(Path(path))
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise EngineAabbCalibrationError(
            f"cannot read calibration plan: {error}"
        ) from error
    return parse_plan(document)


def require_live_calibration_target(
    plan: dict,
    *,
    ledger: object,
    environment: dict | None = None,
    head: str | None = None,
) -> None:
    """実行の直前・直後に、live HEAD・実行環境・allocationがplanと一致することを要求する。

    ``environment`` / ``head`` はtestのseamで、実行では渡さない。
    """
    parsed = parse_plan(plan)
    live_head = _live_head() if head is None else head
    live_environment = (
        _live_environment(parsed["execution"]["wheel_path"])
        if environment is None
        else environment
    )
    if live_head != parsed["execution_target"]["revision"]:
        raise EngineAabbCalibrationError("live Arena HEAD differs from the plan")
    if live_environment != parsed["environment"]:
        raise EngineAabbCalibrationError("live environment differs from the plan")
    population = parsed["population"]
    try:
        require_calibration_seeds(
            ledger, population["allocation_binding"], population["ordered_seeds"]
        )
    except EngineAabbLockError as error:
        raise EngineAabbCalibrationError(str(error)) from error


def _participants(plan: dict) -> tuple[Participant, Participant]:
    participants = plan["participants"]
    return (
        parse_participant(participants[ROLE_CANDIDATE], "plan.participants.A"),
        parse_participant(participants[ROLE_INCUMBENT], "plan.participants.B"),
    )


def _bindings(plan: dict) -> RecordBindings:
    return RecordBindings(
        lock_digest=plan["plan_digest"], environment_digest=plan["environment_digest"]
    )


def _hanchans(plan: dict) -> list[dict]:
    return schedule_document(plan["population"]["ordered_seeds"])["hanchans"]


# ---------------------------------------------------------------------------
# variance planning（J1の感度。D平均・区間は出さない）
# ---------------------------------------------------------------------------


def _sd(values: Sequence[float]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def planning_k(sigma: float) -> int:
    """K_plan = ceil(((z_alpha + z_beta) * sigma / delta) ** 2)（計画近似）。"""
    z = DESIGN["z_alpha"] + DESIGN["z_beta"]
    return math.ceil((z * sigma / DESIGN["delta_pt_per_seat_result"]) ** 2)


def bootstrap_sd_upper(values: Sequence[float]) -> dict[str, float]:
    """seed blockを単位にしたbootstrap SDの分布から、片側95%上限（nearest-rank）。"""
    rng = random.Random(DESIGN["bootstrap_seed"])
    k = len(values)
    replicates = DESIGN["bootstrap_replicates"]
    sds = sorted(
        _sd([values[rng.randrange(k)] for _ in range(k)]) for _ in range(replicates)
    )
    rank = math.ceil(DESIGN["bootstrap_quantile"] * replicates)
    return {"sd_median": stats.median(sds), "sd_upper": sds[rank - 1]}


def _planning_row(sigma: float) -> dict[str, object]:
    k = planning_k(sigma)
    return {"hanchans": 6 * k, "k_plan": k, "sigma": sigma}


def variance_planning(blocks: Sequence[dict]) -> dict[str, object]:
    """block差の標本SD、bootstrap SD上限、leave-one-block-outとK_planの感度。"""
    seeds = [block["seed"] for block in blocks]
    d = [block["difference_internal"] / _DIVISOR for block in blocks]
    if len(d) < 3:
        raise EngineAabbCalibrationError("variance planning needs at least 3 blocks")
    sample_sd = _sd(d)
    mean = sum(d) / len(d)
    squared = [(value - mean) ** 2 for value in d]
    total = sum(squared)
    loo = []
    for index, seed in enumerate(seeds):
        rest = d[:index] + d[index + 1 :]
        loo_sd = _sd(rest)
        loo.append(
            {
                "k_plan": planning_k(loo_sd),
                "sd": loo_sd,
                "seed": seed,
                "share_of_squared_deviation": 0.0
                if total == 0
                else squared[index] / total,
            }
        )
    boot = bootstrap_sd_upper(d)
    loo_sds = [row["sd"] for row in loo]
    return {
        "bootstrap": {
            "quantile": DESIGN["bootstrap_quantile"],
            "replicates": DESIGN["bootstrap_replicates"],
            "seed": DESIGN["bootstrap_seed"],
            **boot,
        },
        "k": len(d),
        "leave_one_block_out": {
            "by_seed": loo,
            "sd_max": max(loo_sds),
            "sd_min": min(loo_sds),
        },
        "planning": {
            "sigma_bootstrap_upper": _planning_row(boot["sd_upper"]),
            "sigma_leave_one_out_max": _planning_row(max(loo_sds)),
            "sigma_sample": _planning_row(sample_sd),
        },
        "sd": sample_sd,
    }


# ---------------------------------------------------------------------------
# summary（保存物だけから再導出できる値）
# ---------------------------------------------------------------------------


def build_summary(plan: dict, records) -> dict[str, object]:
    candidate, incumbent = _participants(plan)
    verify_schedule(
        records.hanchans, expected_schedule(_hanchans(plan), candidate, incumbent)
    )
    require_bindings(records, _bindings(plan))
    if not records.event_fact_status.valid:
        raise EngineAabbCalibrationError("calibration requires layer 2 to be VALID")
    blocks = block_totals(records.hanchans)
    payload = {
        "aggregation_revision": plan["execution_target"]["revision"],
        "blocks": [
            {**block, "d": block["difference_internal"] / _DIVISOR} for block in blocks
        ],
        "evaluation_kind": EVALUATION_KIND,
        "event_fact_status": records.event_fact_status.to_value(),
        "hanchan_count": len(records.hanchans),
        "kyoku_count": sum(len(row["kyokus"]) for row in records.hanchans),
        "not_a_strength_result": True,
        "plan_digest": plan["plan_digest"],
        "raw_manifest_sha256": records.manifest_sha256,
        "schema": SUMMARY_SCHEMA,
        "secondary_metrics": summarize_secondary_metrics(records),
        "status": COMPLETE_STATUS,
        "variance": variance_planning(blocks),
    }
    return {**payload, "summary_digest": document_identity(payload)}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_stop(diagnostics: Path, plan: dict, error: BaseException, done: int):
    diagnostics.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    document = {
        "completed_hanchans": done,
        "error": f"{type(error).__name__}: {error}",
        "not_a_result": True,
        "plan_digest": plan["plan_digest"],
        "schema": STOP_SCHEMA,
        "status": STOP_STATUS,
    }
    write_new_artifact_file(
        diagnostics / f"stop-{stamp}.json", canonical_json_text(document)
    )


def run_calibration(
    plan_path: str | Path,
    *,
    ledger_path: str | Path = seed_registry.DEFAULT_LEDGER_PATH,
    play: Callable[[HanchanTask], PlayedHanchan] = play_hanchan,
    live_gate: Callable[..., None] = require_live_calibration_target,
    on_hanchan: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """planした192半荘を実行し、``<output>`` に raw / summary / timings を公開する。

    ``play`` / ``live_gate`` / ``clock`` はtestのseamで、実行では渡さない。
    """
    plan = load_plan(plan_path)
    output = Path(plan["artifact_destinations"]["output"])
    diagnostics = Path(plan["artifact_destinations"]["diagnostics"])
    if output.exists():
        raise EngineAabbCalibrationError(
            "output already exists; calibration outputs are write-once and a "
            "completed calibration is never re-run"
        )
    limits = plan["resource_limits"]
    rows: list[dict] = []
    started_at = _utc_now()
    started = clock()

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
        elapsed = clock() - started
        if elapsed > limits["wall_seconds_per_stage"]:
            raise RunStopped(
                f"calibration exceeded the wall limit of "
                f"{limits['wall_seconds_per_stage']} s after {len(rows)} hanchans"
            )
        known = [
            value for value in (parent_kib, item.peak_rss_kib) if value is not None
        ]
        if sum(known) * 1024 > limits["process_tree_rss_bytes"]:
            raise RunStopped(
                "parent + worker peak RSS exceeded the process tree limit of "
                f"{limits['process_tree_rss_bytes']} bytes"
            )

    try:
        live_gate(plan, ledger=seed_registry.load_ledger(ledger_path))
        candidate, incumbent = _participants(plan)
        executed = run_schedule(
            hanchans=_hanchans(plan),
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
        live_gate(plan, ledger=seed_registry.load_ledger(ledger_path))
        captures = [item.capture for item in executed]
        # 書込前に第1層・第2層・scheduleを確かめる（失敗時は何も書かない）
        hanchans, _, _ = build_records(captures, event_facts_required=True)
        verify_schedule(
            hanchans, expected_schedule(_hanchans(plan), candidate, incumbent)
        )
        revision = plan["execution_target"]["revision"]
        with staged_artifact_directory(output) as staging:
            write_started = clock()
            records = write_kyoku_records(
                staging / RAW_DIRNAME,
                captures,
                producer_revision=revision,
                event_facts_required=True,
                bindings=_bindings(plan),
            )
            raw_seconds = clock() - write_started
            summary_started = clock()
            summary = build_summary(plan, records)
            write_new_artifact_file(
                staging / SUMMARY_FILENAME, canonical_json_text(summary)
            )
            summary_seconds = clock() - summary_started
            timings = {
                "finished_at_utc": _utc_now(),
                "hanchans": rows,
                "not_rederived": True,
                "plan_digest": plan["plan_digest"],
                "raw_write_and_readback_seconds": raw_seconds,
                "schedule_wall_seconds": schedule_seconds,
                "started_at_utc": started_at,
                "summary_seconds": summary_seconds,
            }
            write_new_artifact_file(
                staging / TIMINGS_FILENAME, canonical_json_text(timings)
            )
            if (clock() - started) > limits["wall_seconds_per_stage"]:
                raise RunStopped("calibration exceeded the wall limit while saving")
    except BaseException as error:
        _write_stop(diagnostics, plan, error, len(rows))
        if not isinstance(error, Exception) or isinstance(
            error, (RunStopped, EngineAabbProtocolError)
        ):
            raise
        raise RunStopped(f"{STOP_STATUS}: {type(error).__name__}: {error}") from error
    return summary


# ---------------------------------------------------------------------------
# verify / report
# ---------------------------------------------------------------------------


def _read(path: Path, context: str) -> object:
    try:
        return read_json_document(path)
    except (ArtifactValidationError, OSError, ValueError) as error:
        raise EngineAabbCalibrationError(f"cannot read {context}: {error}") from error


def output_files(output: str | Path) -> list[dict[str, object]]:
    """保存物（report以外）のpath・bytes・SHA-256。生成側と回収側の照合に使う。"""
    root = Path(output)
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == REPORT_FILENAME:
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


def _distribution(values: Sequence[float]) -> dict[str, float]:
    return {
        "max": max(values),
        "mean": sum(values) / len(values),
        "median": stats.median(values),
        "min": min(values),
    }


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
    "raw_write_and_readback_seconds",
    "schedule_wall_seconds",
    "summary_seconds",
)


def _seconds(value: object, context: str) -> float:
    """有限の非負数（boolは拒否）。"""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise EngineAabbCalibrationError(
            f"{context} must be a finite non-negative number"
        )
    return value


def _rss(value: object, context: str) -> int | None:
    """正の整数（KiB）、または計測できなかったことを示す ``None``（0は欠測ではない）。"""
    if value is not None and (type(value) is not int or value <= 0):
        raise EngineAabbCalibrationError(f"{context} must be a positive int or null")
    return value


def _require_timings(plan: dict, timings: object, hanchans: Sequence[dict]) -> dict:
    """timingsの形・値を検査し、局数を保存済みraw recordと照合する。

    ``hanchans`` はschedule照合済みの第1層row（``build_summary`` を通ったrecord）。
    """
    if type(timings) is not dict or timings.get("plan_digest") != plan["plan_digest"]:
        raise EngineAabbCalibrationError("timings do not belong to the plan")
    rows = timings.get("hanchans")
    expected = [(item["seed"], item["placement"]) for item in _hanchans(plan)]
    if (
        type(rows) is not list
        or any(type(row) is not dict or set(row) != _TIMING_ROW_FIELDS for row in rows)
        or [(row["seed"], row["placement"]) for row in rows] != expected
    ):
        raise EngineAabbCalibrationError("timings do not cover the planned schedule")
    if [(row["seed"], row["placement"]) for row in hanchans] != expected:
        raise EngineAabbCalibrationError(
            "raw records do not cover the planned schedule"
        )
    for row, record in zip(rows, hanchans, strict=True):
        context = f"timings seed {row['seed']} placement {row['placement']}"
        _seconds(row["wall_seconds"], f"{context} wall_seconds")
        kyoku = row["kyoku_count"]
        if type(kyoku) is not int or kyoku <= 0:
            raise EngineAabbCalibrationError(
                f"{context} kyoku_count must be a positive int"
            )
        if kyoku != len(record["kyokus"]):
            raise EngineAabbCalibrationError(
                f"{context} kyoku_count differs from the raw record"
            )
        _rss(row["worker_peak_rss_kib"], f"{context} worker_peak_rss_kib")
        _rss(row["parent_peak_rss_kib"], f"{context} parent_peak_rss_kib")
    for name in _TIMING_TOTAL_FIELDS:
        _seconds(timings.get(name), f"timings {name}")
    return timings


def build_report(
    plan: dict, summary: dict, timings: dict, files: list, records
) -> dict:
    """summary・timings・保存file一覧から、時間・RSS・容量・J1 / J2の数値を作る。

    ``records`` はsummaryの再導出に使った保存済みrecord（局数の照合に使う）。
    RSSの欠測（``null``）は0として扱わず、最大値の対象から外して件数を記録する。
    """
    timings = _require_timings(plan, timings, records.hanchans)
    rows = timings["hanchans"]
    walls = [row["wall_seconds"] for row in rows]
    blocks = [sum(walls[start : start + 6]) for start in range(0, len(walls), 6)]
    worker = [
        row["worker_peak_rss_kib"]
        for row in rows
        if row["worker_peak_rss_kib"] is not None
    ]
    parent = [
        row["parent_peak_rss_kib"]
        for row in rows
        if row["parent_peak_rss_kib"] is not None
    ]
    both = [
        row["worker_peak_rss_kib"] + row["parent_peak_rss_kib"]
        for row in rows
        if row["worker_peak_rss_kib"] is not None
        and row["parent_peak_rss_kib"] is not None
    ]
    timeout = plan["execution"]["timeout_seconds"]
    observed_max = max(walls)
    factor_2 = max(60, math.ceil(2 * observed_max / 60) * 60)
    mean_wall = sum(walls) / len(walls)
    raw_bytes = sum(item["bytes"] for item in files if item["path"].startswith("raw/"))
    j1 = {}
    for name, row in summary["variance"]["planning"].items():
        hanchans = row["hanchans"]
        j1[name] = {
            **row,
            "expected_schedule_seconds_at_observed_mean": hanchans * mean_wall,
            "worst_case_schedule_seconds": {
                "factor_2_timeout": hanchans * factor_2,
                "provisional_timeout": hanchans * timeout,
            },
        }
    payload = {
        "files": files,
        "j1_schedule_bounds": j1,
        "j2_timeout": {
            "factor_2_timeout_seconds": factor_2,
            "observed_max_hanchan_seconds": observed_max,
            "provisional_timeout_seconds": timeout,
            "rule": "ceil(2 * observed max / 60) * 60, at least 60",
        },
        "memory": {
            "parent_peak_rss_kib_max": max(parent) if parent else None,
            "parent_peak_rss_missing_rows": len(rows) - len(parent),
            "sum_of_peaks_kib_max": max(both) if both else None,
            "sum_of_peaks_rows": len(both),
            "worker_peak_rss_kib_max": max(worker) if worker else None,
            "worker_peak_rss_missing_rows": len(rows) - len(worker),
        },
        "not_a_strength_result": True,
        "plan_digest": plan["plan_digest"],
        "schema": REPORT_SCHEMA,
        "storage": {
            "raw_bytes": raw_bytes,
            "raw_bytes_per_hanchan": raw_bytes / len(rows),
        },
        "summary_digest": summary["summary_digest"],
        "timing": {
            "block_wall_seconds": _distribution(blocks),
            "hanchan_wall_seconds": _distribution(walls),
            "kyoku_per_hanchan": _distribution([row["kyoku_count"] for row in rows]),
            "raw_write_and_readback_seconds": timings["raw_write_and_readback_seconds"],
            "schedule_wall_seconds": timings["schedule_wall_seconds"],
            "summary_seconds": timings["summary_seconds"],
        },
    }
    return {**payload, "report_digest": document_identity(payload)}


def _verified_outputs(plan_path: str | Path):
    """保存物を読み、summaryの再導出とtimingsの検査（局数のraw照合を含む）を通す。"""
    plan = load_plan(plan_path)
    output = Path(plan["artifact_destinations"]["output"])
    recorded = _read(output / SUMMARY_FILENAME, "calibration summary")
    records = read_kyoku_records(output / RAW_DIRNAME, event_facts_required=True)
    summary = build_summary(plan, records)
    if recorded != summary:
        raise EngineAabbCalibrationError(
            "calibration summary differs from the re-derivation"
        )
    timings = _require_timings(
        plan, _read(output / TIMINGS_FILENAME, "timings"), records.hanchans
    )
    return plan, output, summary, timings, records, output_files(output)


def verify_calibration(plan_path: str | Path) -> dict[str, object]:
    """保存したplan・rawだけからsummaryを再導出し、timingsを検査し、reportがあれば
    それも再導出する。

    reportの有無にかかわらず、timingsの不正値・局数の不一致は拒否する。
    engineの再実行・Policyの生成・live ledgerの照合はしない。戻り値は
    summary digest・report digest・保存file一覧（回収先でのhash照合用）。
    """
    plan, output, summary, timings, records, files = _verified_outputs(plan_path)
    report_digest = None
    report_path = output / REPORT_FILENAME
    if report_path.exists():
        report = _read(report_path, "calibration report")
        if report != build_report(plan, summary, timings, files, records):
            raise EngineAabbCalibrationError(
                "calibration report differs from the re-derivation"
            )
        report_digest = report["report_digest"]
    return {
        "files": files,
        "plan_digest": plan["plan_digest"],
        "report_digest": report_digest,
        "status": summary["status"],
        "summary_digest": summary["summary_digest"],
    }


def write_report(plan_path: str | Path) -> dict[str, object]:
    """verifyと同じ検査に通った保存物からreportを作り、``<output>/report.json`` へ
    write-onceで書く。"""
    plan, output, summary, timings, records, files = _verified_outputs(plan_path)
    if (output / REPORT_FILENAME).exists():
        raise FileExistsError(f"refusing to overwrite: {output / REPORT_FILENAME}")
    report = build_report(plan, summary, timings, files, records)
    write_new_artifact_file(output / REPORT_FILENAME, canonical_json_text(report))
    return report


__all__ = [
    "CALIBRATION_K",
    "CALIBRATION_PAIR",
    "CALIBRATION_SPLIT",
    "COMPLETE_STATUS",
    "DESIGN",
    "EVALUATION_KIND",
    "OWNER_ISSUE",
    "PLAN_SCHEMA",
    "POPULATION",
    "STOP_STATUS",
    "EngineAabbCalibrationError",
    "assemble_plan",
    "bootstrap_sd_upper",
    "build_plan",
    "build_report",
    "build_summary",
    "calibration_participants",
    "load_plan",
    "output_files",
    "parse_plan",
    "planning_k",
    "require_calibration_seeds",
    "require_live_calibration_target",
    "run_calibration",
    "save_plan",
    "variance_planning",
    "verify_calibration",
    "write_report",
]
