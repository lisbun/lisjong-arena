"""Issue #389 — focal Policy 1 armの実行。

既存ABBB single-round pathをそのまま使う。seat assignment、Policy生成、
raw ``SingleRoundGameResult``構築、aggregationは``single_round_evaluation``の
既存helperを再利用し、並列実行は既存``run_game_jobs``へbenchmark専用の
top-level workerを渡すだけである（Issue #196以降の既存precedentと同じ形）。

benchmark workerが既存pathと異なるのは、``LocalGameRunner``の``trace_sink``へ
``OffenseFactsCollector``を接続する1点だけである。``LocalGameRunner``、
``RoundStatsCollector``、``SeatRoundStats``、single-round artifact v1は変更しない。

Issue #406: ``shanten_backend``を指定した場合だけ、各game実行processで
``shanten_backend_verification.require_shanten_backend``（#400）をprocessごとに
1回行い、各gameの前後でnative call数を検査する。rustでnative callが0、pythonで
native extensionがimportされていれば、そのgameを失敗としてarm全体をfail closed
する。observationはarm artifactへ入れず、呼び出し側が別fileへ記録する。
Issue #409: rustではnative ``API_VERSION``と、lisjong#224の一括構造評価
（``discard_evaluation_call_count()``）のgame前後差も記録する。一括評価は0004参照
Policyだけが使うため、差が0でもgameは失敗にしない（使用確認は記録で行う）。
未指定時の実行経路・結果は変わらない。
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial

from lisjong.policy_contract import Policy, Seat

from lisjong_arena._parallel_execution import (
    GameJob,
    GameJobOutcome,
    check_policy_spec_serializable,
    run_game_jobs,
    validate_max_workers,
)
from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.riichienv.local_game_runner import LocalGameResult, LocalGameRunner
from lisjong_arena.shanten_backend_verification.backend import (
    BACKENDS,
    NATIVE_MODULE,
    PYTHON_BACKEND,
    ShantenBackendVerificationError,
    native_call_count,
    native_discard_evaluation_count,
    require_shanten_backend,
)
from lisjong_arena.single_round_evaluation import (
    ROTATION_COUNT,
    SingleRoundEvaluationError,
    _build_game_result,
    _create_policies,
    _seat_assignment,
    aggregate_candidate_metrics,
)

from .protocol import GAME_MODE, MAX_STEPS, OPPONENT_IDENTITY, opponent_spec
from .record import (
    KyokuOffenseFacts,
    KyokuOffenseRecord,
    OffenseFactsCollector,
    validate_record_against_game_result,
)


class PureOffenseExecutionError(RuntimeError):
    """benchmark workerが不完全または予期しないoutcomeを返した場合。"""


@dataclass(frozen=True, slots=True)
class BenchmarkArmResult:
    """1 focal Policy armの成功した実行結果。

    ``evaluation``は既存ABBB ``SingleRoundEvaluationResult``そのものであり、
    ``offense_records``は同じ順序（seed入力順 -> rotation 0..3）の
    benchmark-owned recordである。
    """

    evaluation: SingleRoundEvaluationResult
    offense_records: tuple[KyokuOffenseRecord, ...]
    shanten_backend_observations: tuple[Mapping[str, object], ...] = ()
    """#406: ``shanten_backend``指定時だけ、game順のper-game backend検査結果。"""

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation, SingleRoundEvaluationResult):
            raise TypeError("evaluation must be a SingleRoundEvaluationResult")
        records = tuple(self.offense_records)
        if len(records) != len(self.evaluation.game_results):
            raise PureOffenseExecutionError(
                "offense records must cover every game result exactly once"
            )
        for record, game_result in zip(
            records, self.evaluation.game_results, strict=True
        ):
            validate_record_against_game_result(record, game_result)
        object.__setattr__(self, "offense_records", records)
        observations = tuple(self.shanten_backend_observations)
        if observations and len(observations) != len(records):
            raise PureOffenseExecutionError(
                "shanten backend observations must cover every game exactly once"
            )
        object.__setattr__(self, "shanten_backend_observations", observations)


@dataclass(frozen=True, slots=True)
class _BenchmarkGameJobOutcome(GameJobOutcome):
    facts: KyokuOffenseFacts | None
    shanten_backend: Mapping[str, object] | None = None


_PROCESS_BACKEND: dict[str, dict[str, object]] = {}
"""processごとに1回だけ検証したshanten backendの記録（#406）。"""


def _verified_process_backend(backend: str) -> dict[str, object]:
    record = _PROCESS_BACKEND.get(backend)
    if record is None:
        record = require_shanten_backend(backend)
        _PROCESS_BACKEND[backend] = record
    return record


def _run_benchmark_game_with_backend_check(
    backend: str,
    policies: Mapping[Seat, Policy],
    *,
    seed: int,
    max_steps: int,
) -> tuple[LocalGameResult, KyokuOffenseFacts, dict[str, object]]:
    """1 gameを実行し、このprocess・このgameのshanten backendを検査する。"""
    process = _verified_process_backend(backend)
    calls_before = native_call_count()
    evaluations_before = native_discard_evaluation_count()
    result, facts = _run_benchmark_game(policies, seed=seed, max_steps=max_steps)
    calls_after = native_call_count()
    evaluations_after = native_discard_evaluation_count()
    if backend == PYTHON_BACKEND:
        if NATIVE_MODULE in sys.modules:
            raise ShantenBackendVerificationError(
                "the python backend imported the native extension during a game"
            )
        native_calls = None
        discard_evaluations = None
    else:
        if (
            calls_before is None
            or calls_after is None
            or evaluations_before is None
            or evaluations_after is None
        ):
            raise ShantenBackendVerificationError(
                f"seed {seed}: the rust backend has no native extension loaded"
            )
        native_calls = calls_after - calls_before
        discard_evaluations = evaluations_after - evaluations_before
        if native_calls < 1:
            raise ShantenBackendVerificationError(
                f"seed {seed}: the rust backend made no native calls in this game"
            )
    native = process["native"]
    observation: dict[str, object] = {
        "backend": process["backend"],
        "pid": process["pid"],
        "lisjong_revision": process["lisjong_revision"],
        "native_source_revision": (
            None if native is None else native["source_revision"]  # type: ignore[index]
        ),
        "native_api_version": (
            None if native is None else native["api_version"]  # type: ignore[index]
        ),
        "native_calls": native_calls,
        "native_discard_evaluations": discard_evaluations,
    }
    return result, facts, observation


def _run_benchmark_game(
    policies: Mapping[Seat, Policy],
    *,
    seed: int,
    max_steps: int,
) -> tuple[LocalGameResult, KyokuOffenseFacts]:
    """1 gameを既存``LocalGameRunner``で実行し、offense factも回収する。

    unit testはこの関数を差し替えて実RiichiEnvを起動せずに検証する。
    """
    collector = OffenseFactsCollector()
    result = LocalGameRunner(
        policies,
        seed=seed,
        game_mode=GAME_MODE,
        max_steps=max_steps,
        trace_sink=collector,
    ).run()
    return result, collector.facts()


def _run_benchmark_game_job(
    job: GameJob, shanten_backend: str | None = None
) -> _BenchmarkGameJobOutcome:
    """spawn worker内部でfresh Policyを生成して1 gameを実行する。"""
    observation = None
    try:
        if shanten_backend is not None:
            _verified_process_backend(shanten_backend)
        policies = _create_policies(
            job.assignment, seed=job.seed, rotation=job.rotation
        )
        if shanten_backend is None:
            result, facts = _run_benchmark_game(
                policies, seed=job.seed, max_steps=job.max_steps
            )
        else:
            result, facts, observation = _run_benchmark_game_with_backend_check(
                shanten_backend, policies, seed=job.seed, max_steps=job.max_steps
            )
    except Exception:
        return _BenchmarkGameJobOutcome(
            seed=job.seed,
            rotation=job.rotation,
            result=None,
            error_text=f"benchmark game failed:\n{traceback.format_exc()}",
            facts=None,
        )
    return _BenchmarkGameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=result,
        error_text=None,
        facts=facts,
        shanten_backend=observation,
    )


def _record(
    result: LocalGameResult,
    facts: KyokuOffenseFacts,
    *,
    seed: int,
    rotation: int,
) -> tuple[SingleRoundGameResult, KyokuOffenseRecord]:
    game_result = _build_game_result(
        result, seed=seed, rotation=rotation, candidate_seat=Seat(rotation)
    )
    record = KyokuOffenseRecord(
        seed=seed, rotation=rotation, focal_seat=Seat(rotation), facts=facts
    )
    try:
        validate_record_against_game_result(record, game_result)
    except ValueError as exc:
        raise SingleRoundEvaluationError(
            f"offense record is inconsistent: {exc}", seed=seed, rotation=rotation
        ) from exc
    return game_result, record


def benchmark_plan(
    focal: PolicySpec, seeds: Sequence[int]
) -> SingleRoundEvaluationPlan:
    """focal vs passive tsumogiri x3の既存ABBB planを作る。"""
    if not isinstance(focal, PolicySpec):
        raise TypeError("focal must be a PolicySpec")
    if focal.identity == OPPONENT_IDENTITY:
        raise ValueError("the focal Policy must not be the passive opponent")
    return SingleRoundEvaluationPlan(
        candidate=focal,
        baseline=opponent_spec(),
        seeds=tuple(seeds),
        max_steps=MAX_STEPS,
    )


def run_benchmark_arm(
    plan: SingleRoundEvaluationPlan,
    *,
    max_workers: int,
    progress_callback: Callable[[int, int], None] | None = None,
    shanten_backend: str | None = None,
) -> BenchmarkArmResult:
    """1 armを実行する。``max_workers == 1``はin-process serial実行。

    実行順序・raw result順序は``seed入力順 -> rotation 0..3``へcanonicalizeする。
    1 gameでも失敗した場合はpartial resultを返さず``SingleRoundEvaluationError``
    を送出する。``shanten_backend``（#406）を指定すると、各game実行processと
    各gameでそのbackendを検査し、observationを``BenchmarkArmResult``へ残す。
    """
    if not isinstance(plan, SingleRoundEvaluationPlan):
        raise TypeError("plan must be a SingleRoundEvaluationPlan")
    if plan.baseline.identity != OPPONENT_IDENTITY:
        raise ValueError("benchmark plan must use the passive tsumogiri opponent")
    if plan.max_steps != MAX_STEPS:
        raise ValueError(f"benchmark plan must use max_steps={MAX_STEPS}")
    if shanten_backend is not None and shanten_backend not in BACKENDS:
        raise ValueError(f"shanten_backend must be one of {BACKENDS}")
    validate_max_workers(max_workers)

    total = ROTATION_COUNT * len(plan.seeds)
    game_results: list[SingleRoundGameResult] = []
    records: list[KyokuOffenseRecord] = []
    observations: list[Mapping[str, object]] = []

    if max_workers == 1:
        if shanten_backend is not None:
            _verified_process_backend(shanten_backend)
        for seed in plan.seeds:
            for rotation in range(ROTATION_COUNT):
                policies = _create_policies(
                    _seat_assignment(plan, rotation), seed=seed, rotation=rotation
                )
                try:
                    if shanten_backend is None:
                        result, facts = _run_benchmark_game(
                            policies, seed=seed, max_steps=plan.max_steps
                        )
                    else:
                        result, facts, observation = (
                            _run_benchmark_game_with_backend_check(
                                shanten_backend,
                                policies,
                                seed=seed,
                                max_steps=plan.max_steps,
                            )
                        )
                        observations.append(observation)
                except Exception as exc:
                    raise SingleRoundEvaluationError(
                        "single game execution failed", seed=seed, rotation=rotation
                    ) from exc
                game_result, record = _record(
                    result, facts, seed=seed, rotation=rotation
                )
                game_results.append(game_result)
                records.append(record)
                if progress_callback is not None:
                    progress_callback(len(game_results), total)
    else:
        check_policy_spec_serializable(plan.candidate)
        check_policy_spec_serializable(plan.baseline)
        jobs = [
            GameJob(
                seed=seed,
                rotation=rotation,
                assignment=_seat_assignment(plan, rotation),
                game_mode=GAME_MODE,
                max_steps=plan.max_steps,
            )
            for seed in plan.seeds
            for rotation in range(ROTATION_COUNT)
        ]
        outcomes = run_game_jobs(
            jobs,
            max_workers=max_workers,
            game_runner=(
                _run_benchmark_game_job
                if shanten_backend is None
                else partial(_run_benchmark_game_job, shanten_backend=shanten_backend)
            ),
            progress_callback=progress_callback,
        )
        for seed in plan.seeds:
            for rotation in range(ROTATION_COUNT):
                outcome = outcomes[(seed, rotation)]
                if outcome.error_text is not None:
                    raise SingleRoundEvaluationError(
                        "single game execution failed in a worker process",
                        seed=seed,
                        rotation=rotation,
                    ) from RuntimeError(outcome.error_text)
                if not isinstance(outcome, _BenchmarkGameJobOutcome):
                    raise PureOffenseExecutionError(
                        "worker returned an unexpected outcome type"
                    )
                if outcome.result is None or outcome.facts is None:
                    raise PureOffenseExecutionError(
                        "worker returned incomplete success"
                    )
                if (shanten_backend is None) != (outcome.shanten_backend is None):
                    raise PureOffenseExecutionError(
                        "worker shanten backend observation does not match the request"
                    )
                game_result, record = _record(
                    outcome.result, outcome.facts, seed=seed, rotation=rotation
                )
                game_results.append(game_result)
                records.append(record)
                if outcome.shanten_backend is not None:
                    observations.append(outcome.shanten_backend)

    frozen_results = tuple(game_results)
    if len(frozen_results) != total:
        raise PureOffenseExecutionError(
            f"expected {total} games but produced {len(frozen_results)}"
        )
    evaluation = SingleRoundEvaluationResult(
        plan=plan,
        game_results=frozen_results,
        candidate_metrics=aggregate_candidate_metrics(
            plan.candidate.identity, frozen_results
        ),
    )
    return BenchmarkArmResult(
        evaluation=evaluation,
        offense_records=tuple(records),
        shanten_backend_observations=tuple(observations),
    )


def shanten_backend_record(
    arm: BenchmarkArmResult, *, backend: str, parent: Mapping[str, object]
) -> dict[str, object]:
    """#406: armのper-game backend observationを集約し、不整合をfail closedする。

    全gameが要求backend・pinned lisjong revision・同じnative ``SOURCE_REVISION``・
    ``API_VERSION``で実行されたこと（rustでは全gameでnative call >= 1）を確認した
    記録を返す。一括構造評価の回数（#409）はworkerごと・全体で集計して残す。
    """
    observations = arm.shanten_backend_observations
    records = arm.offense_records
    if not observations:
        raise PureOffenseExecutionError("the arm has no shanten backend observations")
    expected = {
        "backend": backend,
        "lisjong_revision": parent["lisjong_revision"],
        "native_source_revision": (
            None if parent["native"] is None else parent["native"]["source_revision"]  # type: ignore[index]
        ),
        "native_api_version": (
            None if parent["native"] is None else parent["native"]["api_version"]  # type: ignore[index]
        ),
    }
    workers: dict[int, dict[str, object]] = {}
    for record, observation in zip(records, observations, strict=True):
        for key, value in expected.items():
            if observation[key] != value:
                raise ShantenBackendVerificationError(
                    f"seed {record.seed} rotation {record.rotation}: {key} "
                    f"{observation[key]!r} differs from {value!r}"
                )
        calls = observation["native_calls"]
        evaluations = observation["native_discard_evaluations"]
        if backend == PYTHON_BACKEND:
            if calls is not None or evaluations is not None:
                raise ShantenBackendVerificationError(
                    "a python-backend game reported native calls"
                )
        elif type(calls) is not int or calls < 1:
            raise ShantenBackendVerificationError(
                f"seed {record.seed} rotation {record.rotation}: no native calls"
            )
        elif type(evaluations) is not int or evaluations < 0:
            raise ShantenBackendVerificationError(
                f"seed {record.seed} rotation {record.rotation}: malformed "
                "native discard evaluation count"
            )
        worker = workers.setdefault(
            int(observation["pid"]),  # type: ignore[arg-type]
            {
                "games": 0,
                "native_calls": None if calls is None else 0,
                "native_discard_evaluations": None if evaluations is None else 0,
            },
        )
        worker["games"] += 1  # type: ignore[operator]
        if calls is not None:
            worker["native_calls"] += calls  # type: ignore[operator]
            worker["native_discard_evaluations"] += evaluations  # type: ignore[operator]
    native_calls = [observation["native_calls"] for observation in observations]
    return {
        **expected,
        "parent": dict(parent),
        "games": len(observations),
        "min_native_calls_per_game": (
            None if backend == PYTHON_BACKEND else min(native_calls)  # type: ignore[type-var]
        ),
        "native_discard_evaluations": (
            None
            if backend == PYTHON_BACKEND
            else sum(
                observation["native_discard_evaluations"]  # type: ignore[misc]
                for observation in observations
            )
        ),
        "workers_observed": len(workers),
        "workers": {str(pid): workers[pid] for pid in sorted(workers)},
    }


__all__ = [
    "BenchmarkArmResult",
    "PureOffenseExecutionError",
    "benchmark_plan",
    "run_benchmark_arm",
    "shanten_backend_record",
]
