"""Issue #252 Phase A — exact adaptive candidateのreal-game feasibility計測。

同じlocked technical population(647..650 / 4 rotations / 16 games)を worker
設定だけ変えて実行し、

```text
16/16 games完走
execution failureなし
parallel raw outcomes == serial raw outcomes
provenance valid
fastest valid worker settingが決まる
projected 400-game P-arm wall-clock <= 8h
```

をmachine-readableに判定する。gateを通らない場合は
``PROGRESSION DEVELOPMENT EVALUATION INFEASIBLE``へ分類し、Phase Bを実行
不能にする。

## 判定に使わないもの

worker数はexecution-performance settingであってscientific axisではない。
``select_fastest_valid_worker_count()``はwall-clockと正当性(serial一致 /
失敗なし)だけを見る。game score、candidate勝敗、seed別結果は引数にも
入らない。technical gameのscoreはPolicy / protocol / seedの変更根拠にしない。

## 二重実行しないもの

per-decision progression DPをdiagnosticのために再実行しない。activation /
action-change diagnosticsは、既存stable seamが公開している場合だけ記録し、
公開していない場合は``unavailable``としてrecordへ残す(strength protocolは
それでも有効である)。
"""

from __future__ import annotations

import platform
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_bool,
    expect_float,
    expect_int,
    expect_list,
    expect_object,
    expect_optional_float,
    expect_optional_int,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import require_new_artifact_destinations
from lisjong_arena.model import (
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.single_round_artifact import (
    SingleRoundExecutionProvenance,
    execution_provenance_to_dict,
    parse_execution_provenance,
)
from lisjong_arena.single_round_evaluation import (
    run_single_round_evaluation,
    run_single_round_evaluation_parallel,
)

from .lock import (
    load_lock_document,
    locked_logical_cpu_count,
    locked_worker_sweep,
    require_live_execution_target,
    require_locked_destination,
)
from .protocol import (
    FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
    INFEASIBLE_LABEL,
    MAX_STEPS,
    PARENT_IDENTITY,
    PHASE_A_GAME_COUNT,
    PHASE_A_SEEDS,
    PHASE_B_GAMES_PER_ARM,
    ProgressionProtocolError,
    candidate_spec,
    comparator_spec,
    document_identity,
    progression_diagnostics_availability,
    protocol_document,
    require_exact_candidate_semantics,
    require_exact_comparator,
    require_phase_a_population,
    supported_worker_sweep,
)


def _expect_optional_str(value: object, context: str) -> str | None:
    """``None``または``str``だけを受理する(``_artifact_io``にない形)。"""
    if value is None:
        return None
    return expect_str(value, context)


FEASIBILITY_RECORD_VERSION = 1
"""feasibility record documentのschema version。"""

SECONDS_PER_HOUR = 3600.0

_MEASUREMENT_FIELDS = {
    "execution_failed",
    "failure_text",
    "games_completed",
    "games_per_hour",
    "matches_serial_raw_results",
    "raw_results_digest",
    "speedup_vs_serial",
    "wall_clock_seconds",
    "worker_count",
}

_ELAPSED_FIELDS = {
    "game_count",
    "max_seconds",
    "mean_seconds",
    "min_seconds",
    "p50_seconds",
    "p95_seconds",
}

_MACHINE_FIELDS = {
    "logical_cpu_count",
    "machine",
    "platform",
    "processor",
    "python_implementation",
}

_GATE_FIELDS = {
    "failure_reasons",
    "gate_passed",
    "label",
    "projected_phase_b_arm_cpu_hours",
    "projected_phase_b_arm_wall_clock_hours",
    "selected_worker_count",
    "wall_clock_limit_hours",
}

_RECORD_FIELDS = {
    "candidate_binding",
    "comparator_binding",
    "gate",
    "machine",
    "measurements",
    "ordered_seeds",
    "parent_identity",
    "per_game_elapsed",
    "progression_diagnostics",
    "protocol",
    "provenance",
    "record_identity",
    "record_version",
}


class FeasibilityError(ValueError):
    """Phase A feasibility計測またはrecord readbackが成立しない場合。"""


@dataclass(frozen=True, slots=True)
class MachineProfile:
    """projected wall-clockを解釈するために必要な実行機の識別情報。"""

    logical_cpu_count: int
    platform: str
    processor: str
    machine: str
    python_implementation: str

    def to_document(self) -> dict[str, object]:
        return {
            "logical_cpu_count": self.logical_cpu_count,
            "machine": self.machine,
            "platform": self.platform,
            "processor": self.processor,
            "python_implementation": self.python_implementation,
        }


@dataclass(frozen=True, slots=True)
class PerGameElapsedSummary:
    """serial実行で観測した1 gameあたりelapsedの記述統計。"""

    game_count: int
    mean_seconds: float
    p50_seconds: float
    p95_seconds: float
    min_seconds: float
    max_seconds: float

    def to_document(self) -> dict[str, object]:
        return {
            "game_count": self.game_count,
            "max_seconds": self.max_seconds,
            "mean_seconds": self.mean_seconds,
            "min_seconds": self.min_seconds,
            "p50_seconds": self.p50_seconds,
            "p95_seconds": self.p95_seconds,
        }


@dataclass(frozen=True, slots=True)
class WorkerMeasurement:
    """1 worker設定ぶんのtiming / 正当性の観測値。

    scoreやseed別結果は保持しない。worker選択がgame outcomeへ依存できない
    ことを型の形で固定する。``raw_results_digest``はraw結果のfingerprintで
    あり、一致判定そのものは``matches_serial_raw_results``が持つ。
    """

    worker_count: int
    wall_clock_seconds: float
    games_completed: int
    execution_failed: bool
    failure_text: str | None
    raw_results_digest: str | None
    matches_serial_raw_results: bool
    games_per_hour: float
    speedup_vs_serial: float | None

    def __post_init__(self) -> None:
        if type(self.worker_count) is not int or self.worker_count <= 0:
            raise FeasibilityError("worker_count must be a positive int")
        if type(self.wall_clock_seconds) is not float:
            raise FeasibilityError("wall_clock_seconds must be a float")
        if type(self.games_completed) is not int or self.games_completed < 0:
            raise FeasibilityError("games_completed must be a non-negative int")
        for name in ("execution_failed", "matches_serial_raw_results"):
            if type(getattr(self, name)) is not bool:
                raise FeasibilityError(f"{name} must be a bool")

    @property
    def valid(self) -> bool:
        """worker選択の候補になれるか。timingと正当性だけで決まる。"""
        return (
            not self.execution_failed
            and self.matches_serial_raw_results
            and self.games_completed == PHASE_A_GAME_COUNT
            and self.wall_clock_seconds > 0.0
        )

    def to_document(self) -> dict[str, object]:
        return {
            "execution_failed": self.execution_failed,
            "failure_text": self.failure_text,
            "games_completed": self.games_completed,
            "games_per_hour": self.games_per_hour,
            "matches_serial_raw_results": self.matches_serial_raw_results,
            "raw_results_digest": self.raw_results_digest,
            "speedup_vs_serial": self.speedup_vs_serial,
            "wall_clock_seconds": self.wall_clock_seconds,
            "worker_count": self.worker_count,
        }


@dataclass(frozen=True, slots=True)
class FeasibilityGate:
    """事前登録したtechnical gateの判定結果。"""

    gate_passed: bool
    label: str | None
    failure_reasons: tuple[str, ...]
    selected_worker_count: int | None
    projected_phase_b_arm_wall_clock_hours: float | None
    projected_phase_b_arm_cpu_hours: float | None
    wall_clock_limit_hours: float

    def to_document(self) -> dict[str, object]:
        return {
            "failure_reasons": list(self.failure_reasons),
            "gate_passed": self.gate_passed,
            "label": self.label,
            "projected_phase_b_arm_cpu_hours": (self.projected_phase_b_arm_cpu_hours),
            "projected_phase_b_arm_wall_clock_hours": (
                self.projected_phase_b_arm_wall_clock_hours
            ),
            "selected_worker_count": self.selected_worker_count,
            "wall_clock_limit_hours": self.wall_clock_limit_hours,
        }


@dataclass(frozen=True, slots=True)
class FeasibilityRecord:
    """Phase Aのimmutableな技術記録。strength evidenceではない。"""

    record_version: int
    protocol: dict[str, object]
    ordered_seeds: tuple[int, ...]
    candidate_binding: dict[str, object]
    parent_identity: str
    comparator_binding: dict[str, object]
    provenance: SingleRoundExecutionProvenance
    machine: MachineProfile
    measurements: tuple[WorkerMeasurement, ...]
    per_game_elapsed: PerGameElapsedSummary | None
    progression_diagnostics: dict[str, object]
    gate: FeasibilityGate
    record_identity: str

    def to_document(self) -> dict[str, object]:
        return {
            "candidate_binding": dict(self.candidate_binding),
            "comparator_binding": dict(self.comparator_binding),
            "gate": self.gate.to_document(),
            "machine": self.machine.to_document(),
            "measurements": [item.to_document() for item in self.measurements],
            "ordered_seeds": list(self.ordered_seeds),
            "parent_identity": self.parent_identity,
            "per_game_elapsed": (
                None
                if self.per_game_elapsed is None
                else self.per_game_elapsed.to_document()
            ),
            "progression_diagnostics": dict(self.progression_diagnostics),
            "protocol": dict(self.protocol),
            "provenance": execution_provenance_to_dict(self.provenance),
            "record_identity": self.record_identity,
            "record_version": self.record_version,
        }


def collect_machine_profile(logical_cpu_count: int) -> MachineProfile:
    """実行機のprofileを収集する。取得できない値は空文字で埋めない。"""
    if type(logical_cpu_count) is not int or logical_cpu_count <= 0:
        raise FeasibilityError("logical_cpu_count must be a positive int")
    return MachineProfile(
        logical_cpu_count=logical_cpu_count,
        platform=platform.platform(),
        processor=platform.processor() or platform.machine(),
        machine=platform.machine(),
        python_implementation=platform.python_implementation(),
    )


def summarize_per_game_elapsed(
    elapsed_seconds: Sequence[float],
) -> PerGameElapsedSummary:
    """serial実行で観測した1 gameごとelapsedを要約する。"""
    values = sorted(float(value) for value in elapsed_seconds)
    if not values:
        raise FeasibilityError("elapsed_seconds must not be empty")
    if any(value < 0.0 for value in values):
        raise FeasibilityError("elapsed_seconds must not be negative")
    count = len(values)
    return PerGameElapsedSummary(
        game_count=count,
        mean_seconds=sum(values) / count,
        p50_seconds=values[_percentile_index(count, 0.50)],
        p95_seconds=values[_percentile_index(count, 0.95)],
        min_seconds=values[0],
        max_seconds=values[-1],
    )


def _percentile_index(count: int, fraction: float) -> int:
    """nearest-rank percentileのindexを返す(補間しない)。"""
    rank = int(-(-count * fraction // 1))
    return min(max(rank, 1), count) - 1


def raw_results_digest(
    game_results: tuple[SingleRoundGameResult, ...],
) -> str:
    """raw game resultsのdeterministic fingerprintを返す。

    一致判定そのものはdataclass equalityで行う。この値はrecordへ残す
    fingerprintであり、fingerprint一致を正当性の根拠にしない。
    """
    payload = {
        "game_results": [
            {
                "candidate_seat": int(item.candidate_seat),
                "game_mode": item.game_mode,
                "rotation": item.rotation,
                "scores": list(item.scores),
                "seat_round_stats": [asdict(stats) for stats in item.seat_round_stats],
                "seed": item.seed,
            }
            for item in game_results
        ]
    }
    return document_identity(payload)


def default_execute(
    plan: SingleRoundEvaluationPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> SingleRoundEvaluationResult:
    """既存のserial / parallel runnerへそのまま委譲する。

    parallelが失敗してもserialへsilentへfallbackしない。新しいgame runnerも
    generic backend registryも導入しない。
    """
    if max_workers == 1:
        return run_single_round_evaluation(plan, progress_callback=progress_callback)
    return run_single_round_evaluation_parallel(
        plan, max_workers=max_workers, progress_callback=progress_callback
    )


def measure_worker_setting(
    plan: SingleRoundEvaluationPlan,
    *,
    worker_count: int,
    serial_game_results: tuple[SingleRoundGameResult, ...] | None,
    serial_wall_clock_seconds: float | None,
    execute: Callable[..., SingleRoundEvaluationResult] = default_execute,
    clock: Callable[[], float] = time.perf_counter,
    collect_elapsed: bool = False,
) -> tuple[WorkerMeasurement, SingleRoundEvaluationResult | None, tuple[float, ...]]:
    """1 worker設定で同じplanを実行し、timingと正当性を観測する。

    ``serial_game_results``が与えられた場合、raw resultのdataclass equalityで
    serial一致を判定する。``None``(= serial自身の計測)では自分自身を基準と
    するため一致はTrueである。
    """
    if type(worker_count) is not int or worker_count <= 0:
        raise FeasibilityError("worker_count must be a positive int")

    elapsed: list[float] = []
    started = clock()
    previous = started

    def record_elapsed(completed: int, total: int) -> None:
        del completed, total
        nonlocal previous
        now = clock()
        elapsed.append(now - previous)
        previous = now

    callback = record_elapsed if collect_elapsed else None
    try:
        result = execute(plan, max_workers=worker_count, progress_callback=callback)
    except Exception as exc:
        wall_clock = float(clock() - started)
        measurement = WorkerMeasurement(
            worker_count=worker_count,
            wall_clock_seconds=wall_clock,
            games_completed=0,
            execution_failed=True,
            failure_text=f"{type(exc).__name__}: {exc}",
            raw_results_digest=None,
            matches_serial_raw_results=False,
            games_per_hour=0.0,
            speedup_vs_serial=None,
        )
        return measurement, None, ()

    wall_clock = float(clock() - started)
    game_results = result.game_results
    matches = (
        True if serial_game_results is None else game_results == serial_game_results
    )
    games_completed = len(game_results)
    games_per_hour = (
        0.0 if wall_clock <= 0.0 else games_completed * SECONDS_PER_HOUR / wall_clock
    )
    if serial_wall_clock_seconds is None:
        # serial自身の計測。baselineなのでspeedupは定義上1.0である。
        speedup: float | None = 1.0
    elif wall_clock <= 0.0:
        speedup = None
    else:
        speedup = float(serial_wall_clock_seconds) / wall_clock
    measurement = WorkerMeasurement(
        worker_count=worker_count,
        wall_clock_seconds=wall_clock,
        games_completed=games_completed,
        execution_failed=False,
        failure_text=None,
        raw_results_digest=raw_results_digest(game_results),
        matches_serial_raw_results=matches,
        games_per_hour=games_per_hour,
        speedup_vs_serial=speedup,
    )
    return measurement, result, tuple(elapsed)


def select_fastest_valid_worker_count(
    measurements: Sequence[WorkerMeasurement],
) -> int | None:
    """timingと正当性だけでworker設定を選ぶ。

    候補は「execution failureなし」「serial raw resultsと一致」「16/16完走」
    「positive wall-clock」を満たすものだけである。同着はworker数の小さい方を
    選び、選択がscoreやgame outcomeへ依存する余地を残さない。
    """
    valid = [item for item in measurements if item.valid]
    if not valid:
        return None
    best = min(valid, key=lambda item: (item.wall_clock_seconds, item.worker_count))
    return best.worker_count


def project_phase_b_arm_wall_clock_hours(
    measurement: WorkerMeasurement,
) -> float:
    """選ばれたworker設定から400-game P-armのwall-clockをprojectする。"""
    if not measurement.valid:
        raise FeasibilityError("cannot project from an invalid worker measurement")
    per_game = measurement.wall_clock_seconds / measurement.games_completed
    return per_game * PHASE_B_GAMES_PER_ARM / SECONDS_PER_HOUR


def evaluate_feasibility_gate(
    measurements: Sequence[WorkerMeasurement],
    *,
    wall_clock_limit_hours: float = FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
) -> FeasibilityGate:
    """事前登録したgate条件をmachine-readableに判定する。"""
    reasons: list[str] = []
    items = tuple(measurements)
    if not items:
        reasons.append("no worker measurement was recorded")
    if any(item.execution_failed for item in items):
        reasons.append("at least one worker setting failed to execute")
    if any(
        not item.execution_failed and item.games_completed != PHASE_A_GAME_COUNT
        for item in items
    ):
        reasons.append(
            f"a worker setting did not complete {PHASE_A_GAME_COUNT} technical games"
        )
    if any(
        not item.execution_failed and not item.matches_serial_raw_results
        for item in items
    ):
        reasons.append("parallel raw outcomes differ from serial raw outcomes")

    selected = select_fastest_valid_worker_count(items)
    if selected is None:
        reasons.append("no valid worker setting could be selected")
        projected_wall_clock = None
        projected_cpu_hours = None
    else:
        chosen = next(item for item in items if item.worker_count == selected)
        projected_wall_clock = project_phase_b_arm_wall_clock_hours(chosen)
        projected_cpu_hours = projected_wall_clock * selected
        if projected_wall_clock > wall_clock_limit_hours:
            reasons.append(
                "projected 400-game P-arm wall-clock "
                f"{projected_wall_clock:.3f}h exceeds the pre-registered "
                f"{wall_clock_limit_hours:.3f}h bound"
            )

    gate_passed = not reasons
    return FeasibilityGate(
        gate_passed=gate_passed,
        label=None if gate_passed else INFEASIBLE_LABEL,
        failure_reasons=tuple(reasons),
        selected_worker_count=None if not gate_passed else selected,
        projected_phase_b_arm_wall_clock_hours=projected_wall_clock,
        projected_phase_b_arm_cpu_hours=projected_cpu_hours,
        wall_clock_limit_hours=float(wall_clock_limit_hours),
    )


def _require_locked_sweep(
    lock_document: dict[str, object], logical_cpu_count: int
) -> tuple[int, ...]:
    """live machineがlockと同じresolved sweepを与えることを要求する。

    lockは生成時のlogical CPU数と、そこから解決したsweepを記録している。
    別のmachineで走らせるとactual sweepがlockと食い違うため、game 1より前に
    ここでfail closedする。custom sweepでlocked seedsを消費してから
    readbackで気付く順序にはしない。
    """
    locked_cpu_count = locked_logical_cpu_count(lock_document)
    if logical_cpu_count != locked_cpu_count:
        raise FeasibilityError(
            f"live machine reports {logical_cpu_count} logical CPUs but the lock "
            f"was built for {locked_cpu_count}"
        )
    sweep = locked_worker_sweep(lock_document)
    if sweep != supported_worker_sweep(logical_cpu_count):
        raise FeasibilityError(
            "locked resolved worker sweep does not match the sweep this machine "
            "resolves"
        )
    if not sweep or sweep[0] != 1:
        raise FeasibilityError("the worker sweep must start with serial execution")
    return sweep


def run_phase_a_feasibility(
    *,
    lock_path: str | Path,
    destination: str | Path,
    logical_cpu_count: int,
    execute: Callable[..., SingleRoundEvaluationResult] = default_execute,
    clock: Callable[[], float] = time.perf_counter,
) -> FeasibilityRecord:
    """lock済みexecution targetでworker sweepを実行しgateを判定する。

    実行するplanは``candidate P vs passive T x3``であり、Phase Bと同じ
    rotation shapeを使う。technical gameのscoreはrecordへ残さない。

    game 1より前に、pre-execution lockのstrict read、live execution target
    (clean HEAD / merged main containment / provenance)との照合、locked
    destinationとの一致、live machineがlocked resolved sweepと同じsweepを
    解決すること、write-once preflightをすべて済ませる。いずれかが崩れて
    いればrunnerを1度も呼ばない。

    worker sweepはlockが確定した``resolved_worker_sweep``だけを実行する。
    ``max_steps``もprotocol constantであり、callerは指定できない。
    """
    require_exact_candidate_semantics()
    seeds = require_phase_a_population(PHASE_A_SEEDS)

    # --- game 1より前のlock gate ------------------------------------------
    lock_document = load_lock_document(lock_path)
    provenance = require_live_execution_target(lock_document)
    output = require_locked_destination(
        lock_document, "feasibility_record", destination
    )
    require_new_artifact_destinations(
        {"feasibility_record": output}, required_names=("feasibility_record",)
    )
    sweep = _require_locked_sweep(lock_document, logical_cpu_count)

    plan = SingleRoundEvaluationPlan(
        candidate=candidate_spec(),
        baseline=comparator_spec(),
        seeds=seeds,
        max_steps=MAX_STEPS,
    )

    measurements: list[WorkerMeasurement] = []
    serial_results: tuple[SingleRoundGameResult, ...] | None = None
    serial_wall_clock: float | None = None
    per_game_elapsed: PerGameElapsedSummary | None = None

    for worker_count in sweep:
        is_serial = serial_results is None
        measurement, result, elapsed = measure_worker_setting(
            plan,
            worker_count=worker_count,
            serial_game_results=serial_results,
            serial_wall_clock_seconds=serial_wall_clock,
            execute=execute,
            clock=clock,
            collect_elapsed=is_serial,
        )
        measurements.append(measurement)
        if is_serial and result is not None:
            serial_results = result.game_results
            serial_wall_clock = measurement.wall_clock_seconds
            if elapsed:
                per_game_elapsed = summarize_per_game_elapsed(elapsed)

    gate = evaluate_feasibility_gate(measurements)
    # provenanceはlock gateの時点(game 1より前)で固定したものを使う。sweep後に
    # 収集し直すと、実行中のrevision driftを記録側で追認してしまう。
    record = build_feasibility_record(
        ordered_seeds=seeds,
        provenance=provenance,
        machine=collect_machine_profile(logical_cpu_count),
        measurements=tuple(measurements),
        per_game_elapsed=per_game_elapsed,
        gate=gate,
    )
    save_feasibility_record(record, output)
    return load_feasibility_record(output)


def build_feasibility_record(
    *,
    ordered_seeds: tuple[int, ...],
    provenance: SingleRoundExecutionProvenance,
    machine: MachineProfile,
    measurements: tuple[WorkerMeasurement, ...],
    per_game_elapsed: PerGameElapsedSummary | None,
    gate: FeasibilityGate,
) -> FeasibilityRecord:
    """content-addressed identityを付けたfeasibility recordを組み立てる。"""
    candidate_binding = require_exact_candidate_semantics().to_document()
    comparator_binding = require_exact_comparator()
    payload: dict[str, object] = {
        "candidate_binding": candidate_binding,
        "comparator_binding": comparator_binding,
        "gate": gate.to_document(),
        "machine": machine.to_document(),
        "measurements": [item.to_document() for item in measurements],
        "ordered_seeds": list(ordered_seeds),
        "parent_identity": PARENT_IDENTITY,
        "per_game_elapsed": (
            None if per_game_elapsed is None else per_game_elapsed.to_document()
        ),
        "progression_diagnostics": progression_diagnostics_availability(),
        "protocol": protocol_document(),
        "provenance": execution_provenance_to_dict(provenance),
        "record_version": FEASIBILITY_RECORD_VERSION,
    }
    return FeasibilityRecord(
        record_version=FEASIBILITY_RECORD_VERSION,
        protocol=protocol_document(),
        ordered_seeds=ordered_seeds,
        candidate_binding=candidate_binding,
        parent_identity=PARENT_IDENTITY,
        comparator_binding=comparator_binding,
        provenance=provenance,
        machine=machine,
        measurements=measurements,
        per_game_elapsed=per_game_elapsed,
        progression_diagnostics=progression_diagnostics_availability(),
        gate=gate,
        record_identity=document_identity(payload),
    )


def save_feasibility_record(record: FeasibilityRecord, path: str | Path) -> Path:
    """feasibility recordをwrite-onceで保存する。"""
    if not isinstance(record, FeasibilityRecord):
        raise TypeError("record must be a FeasibilityRecord")
    destination = Path(path)
    require_new_artifact_destinations(
        {"feasibility_record": destination},
        required_names=("feasibility_record",),
    )
    write_new_artifact_file(destination, canonical_json_text(record.to_document()))
    return destination


def _parse_measurement(value: object, index: int) -> WorkerMeasurement:
    context = f"measurements[{index}]"
    document = expect_object(value, _MEASUREMENT_FIELDS, context)
    return WorkerMeasurement(
        worker_count=expect_int(document["worker_count"], f"{context}.worker_count"),
        wall_clock_seconds=expect_float(
            document["wall_clock_seconds"], f"{context}.wall_clock_seconds"
        ),
        games_completed=expect_int(
            document["games_completed"], f"{context}.games_completed"
        ),
        execution_failed=expect_bool(
            document["execution_failed"], f"{context}.execution_failed"
        ),
        failure_text=_expect_optional_str(
            document["failure_text"], f"{context}.failure_text"
        ),
        raw_results_digest=_expect_optional_str(
            document["raw_results_digest"], f"{context}.raw_results_digest"
        ),
        matches_serial_raw_results=expect_bool(
            document["matches_serial_raw_results"],
            f"{context}.matches_serial_raw_results",
        ),
        games_per_hour=expect_float(
            document["games_per_hour"], f"{context}.games_per_hour"
        ),
        speedup_vs_serial=expect_optional_float(
            document["speedup_vs_serial"], f"{context}.speedup_vs_serial"
        ),
    )


def _parse_machine(value: object) -> MachineProfile:
    document = expect_object(value, _MACHINE_FIELDS, "machine")
    return MachineProfile(
        logical_cpu_count=expect_int(
            document["logical_cpu_count"], "machine.logical_cpu_count"
        ),
        platform=expect_str(document["platform"], "machine.platform"),
        processor=expect_str(document["processor"], "machine.processor"),
        machine=expect_str(document["machine"], "machine.machine"),
        python_implementation=expect_str(
            document["python_implementation"], "machine.python_implementation"
        ),
    )


def _parse_per_game_elapsed(value: object) -> PerGameElapsedSummary | None:
    if value is None:
        return None
    document = expect_object(value, _ELAPSED_FIELDS, "per_game_elapsed")
    return PerGameElapsedSummary(
        game_count=expect_int(document["game_count"], "per_game_elapsed.game_count"),
        mean_seconds=expect_float(
            document["mean_seconds"], "per_game_elapsed.mean_seconds"
        ),
        p50_seconds=expect_float(
            document["p50_seconds"], "per_game_elapsed.p50_seconds"
        ),
        p95_seconds=expect_float(
            document["p95_seconds"], "per_game_elapsed.p95_seconds"
        ),
        min_seconds=expect_float(
            document["min_seconds"], "per_game_elapsed.min_seconds"
        ),
        max_seconds=expect_float(
            document["max_seconds"], "per_game_elapsed.max_seconds"
        ),
    )


def _parse_gate(value: object) -> FeasibilityGate:
    document = expect_object(value, _GATE_FIELDS, "gate")
    reasons = tuple(
        expect_str(item, f"gate.failure_reasons[{index}]")
        for index, item in enumerate(
            expect_list(document["failure_reasons"], "gate.failure_reasons")
        )
    )
    return FeasibilityGate(
        gate_passed=expect_bool(document["gate_passed"], "gate.gate_passed"),
        label=_expect_optional_str(document["label"], "gate.label"),
        failure_reasons=reasons,
        selected_worker_count=expect_optional_int(
            document["selected_worker_count"], "gate.selected_worker_count"
        ),
        projected_phase_b_arm_wall_clock_hours=expect_optional_float(
            document["projected_phase_b_arm_wall_clock_hours"],
            "gate.projected_phase_b_arm_wall_clock_hours",
        ),
        projected_phase_b_arm_cpu_hours=expect_optional_float(
            document["projected_phase_b_arm_cpu_hours"],
            "gate.projected_phase_b_arm_cpu_hours",
        ),
        wall_clock_limit_hours=expect_float(
            document["wall_clock_limit_hours"], "gate.wall_clock_limit_hours"
        ),
    )


def _require_measurement_contract(
    measurements: tuple[WorkerMeasurement, ...], machine: MachineProfile
) -> None:
    """measurementsが記録済みmachineのlocked sweep contractと整合するか確認する。"""
    if not measurements:
        raise FeasibilityError("a feasibility record must contain worker measurements")
    observed = tuple(item.worker_count for item in measurements)
    expected = supported_worker_sweep(machine.logical_cpu_count)
    if observed != expected:
        raise FeasibilityError(
            f"worker measurements {observed} are not the locked sweep {expected} "
            "for the recorded machine"
        )
    for item in measurements:
        if item.execution_failed:
            if item.raw_results_digest is not None or item.games_completed != 0:
                raise FeasibilityError(
                    "a failed worker measurement must carry no completed games "
                    "and no raw result digest"
                )
            continue
        if item.raw_results_digest is None:
            raise FeasibilityError(
                "a completed worker measurement must carry a raw result digest"
            )


def parse_feasibility_record(value: object) -> FeasibilityRecord:
    """strict readbackでfeasibility recordを復元し、semantic値を再導出する。

    bool / floatをintとして受理しない``_artifact_io``のstrict helperだけを
    使い、key集合の過不足も受理しない。さらにcontent hash一致だけを根拠に
    せず、candidate / comparator / parent binding、diagnostics availability、
    measurementsのsweep contract、``FeasibilityGate``そのものをこのrevisionの
    contractから再導出して突き合わせる。stored ``gate_passed``はauthorityに
    しない。
    """
    document = expect_object(value, _RECORD_FIELDS, "feasibility record")
    version = expect_int(document["record_version"], "record_version")
    if version != FEASIBILITY_RECORD_VERSION:
        raise FeasibilityError(f"unsupported feasibility record version: {version!r}")
    protocol = expect_object(document["protocol"], set(protocol_document()), "protocol")
    if protocol != protocol_document():
        raise FeasibilityError("feasibility record protocol block does not match")
    seeds = tuple(
        expect_int(item, f"ordered_seeds[{index}]")
        for index, item in enumerate(
            expect_list(document["ordered_seeds"], "ordered_seeds")
        )
    )
    try:
        require_phase_a_population(seeds)
    except ProgressionProtocolError as exc:
        raise FeasibilityError(str(exc)) from exc

    measurements = tuple(
        _parse_measurement(item, index)
        for index, item in enumerate(
            expect_list(document["measurements"], "measurements")
        )
    )
    expected_candidate = require_exact_candidate_semantics().to_document()
    candidate_binding = expect_object(
        document["candidate_binding"], set(expected_candidate), "candidate_binding"
    )
    if candidate_binding != expected_candidate:
        raise FeasibilityError(
            "feasibility record candidate binding is not the locked #170 generation"
        )
    expected_comparator = require_exact_comparator()
    comparator_binding = expect_object(
        document["comparator_binding"], set(expected_comparator), "comparator_binding"
    )
    if comparator_binding != expected_comparator:
        raise FeasibilityError(
            "feasibility record comparator binding is not the locked passive T"
        )
    parent_identity = expect_str(document["parent_identity"], "parent_identity")
    if parent_identity != PARENT_IDENTITY:
        raise FeasibilityError("feasibility record parent identity is not the locked C")
    expected_diagnostics = progression_diagnostics_availability()
    diagnostics = expect_object(
        document["progression_diagnostics"],
        set(expected_diagnostics),
        "progression_diagnostics",
    )
    if diagnostics != expected_diagnostics:
        raise FeasibilityError(
            "feasibility record progression diagnostics availability does not match "
            "the current stable seam"
        )

    machine = _parse_machine(document["machine"])
    _require_measurement_contract(measurements, machine)

    gate = _parse_gate(document["gate"])
    rederived_gate = evaluate_feasibility_gate(
        measurements, wall_clock_limit_hours=gate.wall_clock_limit_hours
    )
    if gate.wall_clock_limit_hours != FEASIBILITY_WALL_CLOCK_LIMIT_HOURS:
        raise FeasibilityError(
            "feasibility record does not use the pre-registered wall-clock bound"
        )
    if gate != rederived_gate:
        # stored gate_passed / selected worker をauthorityにしない。
        raise FeasibilityError(
            "stored feasibility gate does not match the gate re-derived from the "
            "recorded worker measurements"
        )
    if gate.gate_passed and gate.selected_worker_count != (
        select_fastest_valid_worker_count(measurements)
    ):
        raise FeasibilityError(
            "stored selected worker count is not the fastest valid setting"
        )

    record = FeasibilityRecord(
        record_version=version,
        protocol=protocol,
        ordered_seeds=seeds,
        candidate_binding=candidate_binding,
        parent_identity=parent_identity,
        comparator_binding=comparator_binding,
        provenance=parse_execution_provenance(document["provenance"]),
        machine=machine,
        measurements=measurements,
        per_game_elapsed=_parse_per_game_elapsed(document["per_game_elapsed"]),
        progression_diagnostics=diagnostics,
        gate=gate,
        record_identity=expect_str(document["record_identity"], "record_identity"),
    )
    payload = {
        name: value
        for name, value in record.to_document().items()
        if name != "record_identity"
    }
    if document_identity(payload) != record.record_identity:
        raise FeasibilityError("feasibility record identity does not match its content")
    return record


def load_feasibility_record(path: str | Path) -> FeasibilityRecord:
    """保存済みfeasibility recordをstrict readbackで読み戻す。"""
    try:
        return parse_feasibility_record(read_json_document(Path(path)))
    except ArtifactValidationError as exc:
        raise FeasibilityError(str(exc)) from exc


def require_passed_gate(record: FeasibilityRecord) -> int:
    """Phase B実行前にgate通過を確認し、選ばれたworker数を返す。"""
    if not isinstance(record, FeasibilityRecord):
        raise TypeError("record must be a FeasibilityRecord")
    if not record.gate.gate_passed:
        raise FeasibilityError(
            f"{INFEASIBLE_LABEL}: "
            + "; ".join(record.gate.failure_reasons or ("gate did not pass",))
        )
    worker_count = record.gate.selected_worker_count
    if type(worker_count) is not int or worker_count <= 0:
        raise FeasibilityError("passed gate must carry a selected worker count")
    return worker_count


__all__ = [
    "FEASIBILITY_RECORD_VERSION",
    "FeasibilityError",
    "FeasibilityGate",
    "FeasibilityRecord",
    "MachineProfile",
    "PerGameElapsedSummary",
    "WorkerMeasurement",
    "build_feasibility_record",
    "collect_machine_profile",
    "evaluate_feasibility_gate",
    "load_feasibility_record",
    "measure_worker_setting",
    "parse_feasibility_record",
    "project_phase_b_arm_wall_clock_hours",
    "raw_results_digest",
    "require_passed_gate",
    "run_phase_a_feasibility",
    "save_feasibility_record",
    "select_fastest_valid_worker_count",
    "summarize_per_game_elapsed",
]
