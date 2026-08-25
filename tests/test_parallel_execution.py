"""``lisjong_arena._parallel_execution``のunit test。

実RiichiEnvを起動せず、この private orchestration moduleだけを検証する。

- ``_run_game_job``のfresh Policy lifecycleとerror変換は、
  ``lisjong_arena._parallel_execution.LocalGameRunner``をfakeへ差し替えて
  同一process内で直接呼び出し、高速に固定する。
- ``run_game_jobs``のprocess pool orchestration（実行順序に依存しない結果、
  spawn semantics、fork非依存）は、``game_runner``差し替え口へ
  moduleレベルのfake game runnerを渡し、実際に``ProcessPoolExecutor``で
  別processを起動して検証する。fakeはRiichiEnvへ触れないため高速である。
"""

import time
import unittest
from unittest.mock import patch

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena._parallel_execution import (
    GameJob,
    GameJobOutcome,
    PolicyFactoryNotSerializableError,
    _run_game_job,
    check_policy_spec_serializable,
    run_game_jobs,
    validate_max_workers,
)
from lisjong_arena.model import PolicySpec
from lisjong_arena.riichienv.local_game_runner import LocalGameResult


class _StubPolicy:
    def __init__(self, identity: str) -> None:
        self.identity = identity


class _RecordingFactory:
    """呼び出しごとに新しい``_StubPolicy``を返し、生成したinstanceを記録する。"""

    def __init__(self, identity: str) -> None:
        self.identity = identity
        self.instances: list[_StubPolicy] = []

    def __call__(self) -> _StubPolicy:
        instance = _StubPolicy(self.identity)
        self.instances.append(instance)
        return instance


class _FailingFactory:
    def __init__(self, cause: Exception) -> None:
        self._cause = cause

    def __call__(self) -> _StubPolicy:
        raise self._cause


class _RecordingLocalGameRunner:
    """``LocalGameRunner``の差し替え。渡されたpoliciesを記録するだけ。"""

    captured_policies: list[dict[Seat, object]] = []

    def __init__(
        self, policies: dict[Seat, object], *, seed: int, game_mode: str, max_steps: int
    ) -> None:
        self._policies = policies
        self._seed = seed
        self._game_mode = game_mode

    def run(self) -> LocalGameResult:
        type(self).captured_policies.append(dict(self._policies))
        scores = (30_000, 30_000, 30_000, -90_000 + 30_000)
        return LocalGameResult(
            seed=self._seed,
            game_mode=self._game_mode,
            scores=scores,
            ranks=(1, 2, 3, 4),
            steps=1,
            decisions=4,
            seat_round_stats=neutral_seat_round_stats_tuple(scores),
        )


class _FailingLocalGameRunner:
    def __init__(self, policies, *, seed, game_mode, max_steps) -> None:
        pass

    def run(self) -> LocalGameResult:
        raise RuntimeError("runner exploded")


def _assignment(
    factory_a: object, factory_b: object
) -> tuple[PolicySpec, PolicySpec, PolicySpec, PolicySpec]:
    spec_a = PolicySpec(identity="a", factory=factory_a)
    spec_b = PolicySpec(identity="b", factory=factory_b)
    return (spec_a, spec_a, spec_b, spec_b)


class ValidateMaxWorkersTest(unittest.TestCase):
    def test_accepts_positive_int(self) -> None:
        self.assertEqual(validate_max_workers(4), 4)

    def test_rejects_zero(self) -> None:
        with self.assertRaises(ValueError):
            validate_max_workers(0)

    def test_rejects_negative(self) -> None:
        with self.assertRaises(ValueError):
            validate_max_workers(-1)

    def test_rejects_non_int(self) -> None:
        for value in (4.0, "4", None, True):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    validate_max_workers(value)


# module-level factories so pickle.dumps() can resolve them by reference,
# matching what an actual spawn worker would need to do.
def _top_level_factory() -> _StubPolicy:
    return _StubPolicy("top-level")


class PolicySpecSerializabilityTest(unittest.TestCase):
    def test_accepts_an_importable_top_level_factory(self) -> None:
        spec = PolicySpec(identity="a", factory=_top_level_factory)
        check_policy_spec_serializable(spec)

    def test_rejects_a_lambda(self) -> None:
        spec = PolicySpec(identity="a", factory=lambda: _StubPolicy("a"))
        with self.assertRaises(PolicyFactoryNotSerializableError) as raised:
            check_policy_spec_serializable(spec)
        self.assertEqual(raised.exception.identity, "a")
        self.assertIsNotNone(raised.exception.__cause__)

    def test_rejects_a_local_closure(self) -> None:
        def _make_local_closure_factory():
            def _factory() -> _StubPolicy:
                return _StubPolicy("a")

            return _factory

        spec = PolicySpec(identity="closure", factory=_make_local_closure_factory())
        with self.assertRaises(PolicyFactoryNotSerializableError) as raised:
            check_policy_spec_serializable(spec)
        self.assertEqual(raised.exception.identity, "closure")


class RunGameJobFreshPolicyTest(unittest.TestCase):
    """``_run_game_job``自身がfresh Policy instanceを生成することを固定する。

    ``ProcessPoolExecutor``経由ではなく直接呼び出すことで高速に検証する。
    """

    def setUp(self) -> None:
        _RecordingLocalGameRunner.captured_policies = []
        patcher = patch(
            "lisjong_arena._parallel_execution.LocalGameRunner",
            _RecordingLocalGameRunner,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_seat_gets_a_fresh_instance_from_its_factory(self) -> None:
        factory_a = _RecordingFactory("a")
        factory_b = _RecordingFactory("b")
        job = GameJob(
            seed=1,
            rotation=0,
            assignment=_assignment(factory_a, factory_b),
            game_mode="4p-red-half",
            max_steps=100,
        )

        outcome = _run_game_job(job)

        self.assertIsNone(outcome.error_text)
        self.assertEqual(len(factory_a.instances), 2)
        self.assertEqual(len(factory_b.instances), 2)
        used = _RecordingLocalGameRunner.captured_policies[0]
        self.assertEqual(set(used), set(Seat))
        self.assertEqual(len({id(policy) for policy in used.values()}), 4)

    def test_repeated_jobs_do_not_share_instances_across_calls(self) -> None:
        factory_a = _RecordingFactory("a")
        factory_b = _RecordingFactory("b")
        job = GameJob(
            seed=1,
            rotation=0,
            assignment=_assignment(factory_a, factory_b),
            game_mode="4p-red-half",
            max_steps=100,
        )

        _run_game_job(job)
        _run_game_job(job)

        self.assertEqual(len(factory_a.instances), 4)
        self.assertEqual(len(factory_b.instances), 4)
        first_call, second_call = _RecordingLocalGameRunner.captured_policies
        first_ids = {id(policy) for policy in first_call.values()}
        second_ids = {id(policy) for policy in second_call.values()}
        self.assertEqual(first_ids & second_ids, set())

    def test_policy_factory_failure_is_reported_without_raising(self) -> None:
        cause = RuntimeError("factory exploded")
        job = GameJob(
            seed=7,
            rotation=2,
            assignment=_assignment(_FailingFactory(cause), _RecordingFactory("b")),
            game_mode="4p-red-half",
            max_steps=100,
        )

        outcome = _run_game_job(job)

        self.assertIsInstance(outcome, GameJobOutcome)
        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.seed, 7)
        self.assertEqual(outcome.rotation, 2)
        self.assertIn("policy factory", outcome.error_text)
        self.assertIn("RuntimeError", outcome.error_text)
        self.assertIn("factory exploded", outcome.error_text)
        self.assertEqual(_RecordingLocalGameRunner.captured_policies, [])


class RunGameJobRunnerFailureTest(unittest.TestCase):
    def test_runner_failure_is_reported_without_raising(self) -> None:
        with patch(
            "lisjong_arena._parallel_execution.LocalGameRunner",
            _FailingLocalGameRunner,
        ):
            job = GameJob(
                seed=3,
                rotation=1,
                assignment=_assignment(_RecordingFactory("a"), _RecordingFactory("b")),
                game_mode="4p-red-half",
                max_steps=100,
            )
            outcome = _run_game_job(job)

        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.seed, 3)
        self.assertEqual(outcome.rotation, 1)
        self.assertIn("single game execution failed", outcome.error_text)
        self.assertIn("runner exploded", outcome.error_text)


# --- real ProcessPoolExecutor tests below; fakes must be importable at
# module scope so a spawned child process can resolve them by reference. ---

_PARENT_ONLY_MUTATIONS: list[str] = []
"""spawn childは新しいinterpreterでこのmoduleを再importするため、
parent側でここへ何を追加してもchildからは空のまま見えるはずである
（fork依存でないことの検証に使う）。"""


def _fake_ok_runner(job: GameJob) -> GameJobOutcome:
    return GameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=f"ok:{job.seed}:{job.rotation}",
        error_text=None,
    )


def _fake_ok_runner_reverse_latency(job: GameJob) -> GameJobOutcome:
    """rotationが小さいjobほど遅く終わるfake。completion順を意図的にscrambleする。"""
    time.sleep(0.03 * job.rotation)
    return GameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=f"ok:{job.seed}:{job.rotation}",
        error_text=None,
    )


_FAILING_KEY = (99, 2)


def _fake_runner_with_one_failure(job: GameJob) -> GameJobOutcome:
    if (job.seed, job.rotation) == _FAILING_KEY:
        return GameJobOutcome(
            seed=job.seed, rotation=job.rotation, result=None, error_text="boom"
        )
    return GameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=f"ok:{job.seed}:{job.rotation}",
        error_text=None,
    )


def _fake_runner_that_raises(job: GameJob) -> GameJobOutcome:
    raise RuntimeError("worker exploded before returning an outcome")


def _fake_spawn_probe_runner(job: GameJob) -> GameJobOutcome:
    fork_leak = len(_PARENT_ONLY_MUTATIONS) > 0
    return GameJobOutcome(
        seed=job.seed,
        rotation=job.rotation,
        result=None if fork_leak else "spawn-clean",
        error_text="parent process state leaked into worker (fork semantics?)"
        if fork_leak
        else None,
    )


def _jobs_for(seeds_and_rotations: list[tuple[int, int]]) -> list[GameJob]:
    factory_a = _top_level_factory
    factory_b = _top_level_factory
    assignment = _assignment(factory_a, factory_b)
    return [
        GameJob(
            seed=seed,
            rotation=rotation,
            assignment=assignment,
            game_mode="4p-red-half",
            max_steps=100,
        )
        for seed, rotation in seeds_and_rotations
    ]


class RunGameJobsProcessPoolTest(unittest.TestCase):
    def test_all_jobs_are_executed_and_keyed_by_seed_and_rotation(self) -> None:
        jobs = _jobs_for([(1, 0), (1, 1), (2, 0), (2, 1)])

        outcomes = run_game_jobs(jobs, max_workers=2, game_runner=_fake_ok_runner)

        self.assertEqual(set(outcomes), {(1, 0), (1, 1), (2, 0), (2, 1)})
        for (seed, rotation), outcome in outcomes.items():
            self.assertIsNone(outcome.error_text)
            self.assertEqual(outcome.result, f"ok:{seed}:{rotation}")

    def test_outcomes_are_keyed_correctly_regardless_of_completion_order(self) -> None:
        jobs = _jobs_for([(1, 3), (1, 2), (1, 1), (1, 0)])

        outcomes = run_game_jobs(
            jobs, max_workers=4, game_runner=_fake_ok_runner_reverse_latency
        )

        self.assertEqual(set(outcomes), {(1, 0), (1, 1), (1, 2), (1, 3)})
        for (seed, rotation), outcome in outcomes.items():
            self.assertEqual(outcome.result, f"ok:{seed}:{rotation}")

    def test_empty_jobs_returns_empty_outcomes(self) -> None:
        self.assertEqual(run_game_jobs([], max_workers=2), {})

    def test_invalid_max_workers_is_rejected(self) -> None:
        jobs = _jobs_for([(1, 0)])
        with self.assertRaises(ValueError):
            run_game_jobs(jobs, max_workers=0, game_runner=_fake_ok_runner)

    def test_one_job_failure_is_reported_in_its_own_outcome(self) -> None:
        jobs = _jobs_for([(99, 0), (99, 1), (99, 2), (99, 3)])

        outcomes = run_game_jobs(
            jobs, max_workers=2, game_runner=_fake_runner_with_one_failure
        )

        failing = outcomes[_FAILING_KEY]
        self.assertIsNone(failing.result)
        self.assertEqual(failing.error_text, "boom")
        for key, outcome in outcomes.items():
            if key != _FAILING_KEY:
                self.assertIsNone(outcome.error_text)

    def test_worker_exception_is_reported_in_its_own_outcome(self) -> None:
        job = _jobs_for([(42, 3)])[0]

        outcomes = run_game_jobs(
            [job], max_workers=1, game_runner=_fake_runner_that_raises
        )

        failing = outcomes[(42, 3)]
        self.assertIsNone(failing.result)
        self.assertEqual(failing.seed, 42)
        self.assertEqual(failing.rotation, 3)
        self.assertIn(
            "worker process failed before returning a result", failing.error_text
        )
        self.assertIn("RuntimeError", failing.error_text)
        self.assertIn("worker exploded before returning an outcome", failing.error_text)

    def test_workers_run_in_separate_spawned_processes_not_forked(self) -> None:
        _PARENT_ONLY_MUTATIONS.append("mutated-after-import-in-parent")
        try:
            jobs = _jobs_for([(1, 0), (1, 1)])
            outcomes = run_game_jobs(
                jobs, max_workers=2, game_runner=_fake_spawn_probe_runner
            )
        finally:
            _PARENT_ONLY_MUTATIONS.clear()

        for outcome in outcomes.values():
            self.assertIsNone(
                outcome.error_text,
                "worker observed parent-process mutable state; "
                "this indicates fork semantics instead of spawn",
            )
            self.assertEqual(outcome.result, "spawn-clean")


if __name__ == "__main__":
    unittest.main()
