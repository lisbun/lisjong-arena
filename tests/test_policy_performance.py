"""実RiichiEnvを起動しない``lisjong_arena.policy_performance``のunit test。

``lisjong_arena.single_round_evaluation._run_single_game``を差し替える
既存パターン(``tests/test_single_round_evaluation.py``)を踏襲し、instrumentation
wrapper、timing aggregation、profile aggregation、candidate-only measurement、
rotationを跨いだcandidate identity追跡、Policy二重実行の不在、例外伝播、
serial-only性をfake / injected clockで高速かつdeterministicに固定する。
"""

import cProfile
import inspect
import itertools
import unittest
from collections.abc import Mapping
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Policy, Seat

from lisjong_arena import policy_performance
from lisjong_arena.model import PolicySpec, SingleRoundEvaluationPlan
from lisjong_arena.policy_performance import (
    DecisionTimingMetrics,
    ProfileFunctionStat,
    _extract_profile_function_stats,
    _instrument_candidate,
    _module_hint_from_filename,
    _percentile,
    _ProfileInstrumentedPolicy,
    _sort_function_stats,
    _TimingInstrumentedPolicy,
    aggregate_decision_timings,
    run_policy_hotspot_profile,
    run_policy_timing_profile,
)
from lisjong_arena.riichienv.local_game_runner import LocalGameResult
from lisjong_arena.single_round_evaluation import (
    GAME_MODE,
    ROTATION_COUNT,
    SingleRoundEvaluationError,
)


class _SentinelAction:
    """本testではAction identityの意味を検証しないため、任意のsentinelでよい。"""


class _RecordingPolicy:
    """``choose_action()``の呼び出し回数と受け取ったdecisionを記録するfake Policy。"""

    def __init__(
        self, *, action: object | None = None, error: Exception | None = None
    ) -> None:
        self.call_count = 0
        self.received_decisions: list[object] = []
        self._action = action if action is not None else _SentinelAction()
        self._error = error

    def choose_action(self, decision: object) -> object:
        self.call_count += 1
        self.received_decisions.append(decision)
        if self._error is not None:
            raise self._error
        return self._action


class _RecordingFactory:
    """呼ばれるたびに新しい``_RecordingPolicy``を生成し、生成したinstanceを保持する。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.instances: list[_RecordingPolicy] = []

    def __call__(self) -> _RecordingPolicy:
        instance = _RecordingPolicy(error=self._error)
        self.instances.append(instance)
        return instance


class _BaselinePolicy:
    """baseline seat用の無害なfake Policy。

    実``LocalGameRunner``は4 seatすべてで``choose_action()``を呼ぶため、
    candidate-only measurementを正しく検証するには、baselineが実際に
    呼ばれても(instrumentationがcandidateのfactoryだけをwrapする以上)
    timing / profile集計へ混ざらないことを確認する必要がある。そのため
    baseline側は例外を送出せず、通常どおり呼び出しに応答する。
    """

    def __init__(self) -> None:
        self.call_count = 0

    def choose_action(self, decision: object) -> object:
        self.call_count += 1
        return _SentinelAction()


def _incrementing_clock(step: int = 100, *, start: int = 0):
    """呼ぶたびに``step``ずつ進む決定的なfake monotonic clock。"""
    counter = itertools.count(start=start, step=step)
    return lambda: next(counter)


class TimingInstrumentedPolicyTest(unittest.TestCase):
    def test_records_exactly_one_duration_per_call_using_the_injected_clock(
        self,
    ) -> None:
        wrapped = _RecordingPolicy()
        durations: list[int] = []
        policy = _TimingInstrumentedPolicy(
            wrapped, durations, clock=_incrementing_clock(step=100)
        )

        result_one = policy.choose_action("decision-1")
        result_two = policy.choose_action("decision-2")

        self.assertIs(result_one, wrapped._action)
        self.assertIs(result_two, wrapped._action)
        self.assertEqual(wrapped.call_count, 2)
        self.assertEqual(durations, [100, 100])

    def test_does_not_record_a_duration_when_the_wrapped_policy_raises(self) -> None:
        failure = RuntimeError("boom")
        wrapped = _RecordingPolicy(error=failure)
        durations: list[int] = []
        policy = _TimingInstrumentedPolicy(
            wrapped, durations, clock=_incrementing_clock()
        )

        with self.assertRaises(RuntimeError):
            policy.choose_action("decision")

        self.assertEqual(wrapped.call_count, 1)
        self.assertEqual(durations, [])


class ProfileInstrumentedPolicyTest(unittest.TestCase):
    def test_runcall_invokes_the_wrapped_policy_exactly_once(self) -> None:
        wrapped = _RecordingPolicy()
        profiler = cProfile.Profile()
        policy = _ProfileInstrumentedPolicy(wrapped, profiler)

        result = policy.choose_action("decision")

        self.assertIs(result, wrapped._action)
        self.assertEqual(wrapped.call_count, 1)
        self.assertGreater(len(profiler.getstats()), 0)

    def test_exception_propagates_and_profiler_stays_usable(self) -> None:
        failure = RuntimeError("boom")
        wrapped = _RecordingPolicy(error=failure)
        profiler = cProfile.Profile()
        policy = _ProfileInstrumentedPolicy(wrapped, profiler)

        with self.assertRaises(RuntimeError):
            policy.choose_action("decision")

        self.assertEqual(wrapped.call_count, 1)
        # Profile.runcall()のtry/finallyによりdisable()済みで、再利用できる。
        self.assertIsInstance(profiler.getstats(), list)


class PercentileTest(unittest.TestCase):
    def test_nearest_rank_percentiles_on_a_known_dataset(self) -> None:
        values = tuple(range(10, 101, 10))  # 10, 20, ..., 100 (n=10)
        self.assertEqual(_percentile(values, 50), 50)
        self.assertEqual(_percentile(values, 95), 100)
        self.assertEqual(_percentile(values, 100), 100)

    def test_single_value_dataset(self) -> None:
        self.assertEqual(_percentile((42,), 50), 42)
        self.assertEqual(_percentile((42,), 95), 42)


class AggregateDecisionTimingsTest(unittest.TestCase):
    def test_known_dataset_produces_exact_aggregation(self) -> None:
        durations = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
        metrics = aggregate_decision_timings(durations)

        self.assertIsInstance(metrics, DecisionTimingMetrics)
        self.assertEqual(metrics.decision_count, 10)
        self.assertEqual(metrics.total_decision_time_ns, 550)
        self.assertEqual(metrics.mean_decision_latency_ns, 55.0)
        self.assertEqual(metrics.p50_decision_latency_ns, 50)
        self.assertEqual(metrics.p95_decision_latency_ns, 100)
        self.assertEqual(metrics.max_decision_latency_ns, 100)
        self.assertAlmostEqual(metrics.decisions_per_second, 10 / (550 / 1_000_000_000))

    def test_aggregation_is_independent_of_input_order(self) -> None:
        ordered = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
        shuffled = (100, 10, 80, 20, 90, 30, 70, 40, 60, 50)
        self.assertEqual(
            aggregate_decision_timings(ordered), aggregate_decision_timings(shuffled)
        )

    def test_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            aggregate_decision_timings(())

    def test_rejects_negative_durations(self) -> None:
        with self.assertRaises(ValueError):
            aggregate_decision_timings((10, -1))

    def test_rejects_non_int_durations(self) -> None:
        with self.assertRaises(TypeError):
            aggregate_decision_timings((10, 20.5))

    def test_zero_only_durations_yield_infinite_throughput_not_a_crash(self) -> None:
        metrics = aggregate_decision_timings((0, 0, 0))
        self.assertEqual(metrics.total_decision_time_ns, 0)
        self.assertEqual(metrics.decisions_per_second, float("inf"))


class DecisionTimingMetricsValidationTest(unittest.TestCase):
    def _valid_kwargs(self) -> dict[str, object]:
        return dict(
            decision_count=1,
            total_decision_time_ns=100,
            mean_decision_latency_ns=100.0,
            p50_decision_latency_ns=100,
            p95_decision_latency_ns=100,
            max_decision_latency_ns=100,
            decisions_per_second=1.0,
        )

    def test_p50_must_not_exceed_p95(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs.update(p50_decision_latency_ns=200, p95_decision_latency_ns=100)
        with self.assertRaises(ValueError):
            DecisionTimingMetrics(**kwargs)

    def test_p95_must_not_exceed_max(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs.update(p95_decision_latency_ns=200, max_decision_latency_ns=100)
        with self.assertRaises(ValueError):
            DecisionTimingMetrics(**kwargs)


class ModuleHintTest(unittest.TestCase):
    def test_identifies_first_party_lisjong_module(self) -> None:
        self.assertEqual(
            _module_hint_from_filename(
                "/x/site-packages/lisjong/hand_evaluation/shanten.py"
            ),
            "lisjong.hand_evaluation.shanten",
        )

    def test_identifies_first_party_lisjong_arena_module(self) -> None:
        self.assertEqual(
            _module_hint_from_filename("/x/src/lisjong_arena/policy_performance.py"),
            "lisjong_arena.policy_performance",
        )

    def test_does_not_confuse_lisjong_arena_with_lisjong(self) -> None:
        hint = _module_hint_from_filename("/x/src/lisjong_arena/model.py")
        self.assertTrue(hint.startswith("lisjong_arena"))

    def test_returns_empty_string_for_non_first_party_paths(self) -> None:
        self.assertEqual(_module_hint_from_filename("/usr/lib/python3.14/os.py"), "")
        self.assertEqual(_module_hint_from_filename("~"), "")

    def test_strips_init_suffix(self) -> None:
        self.assertEqual(
            _module_hint_from_filename("/x/lisjong/policies/__init__.py"),
            "lisjong.policies",
        )


class SortFunctionStatsTest(unittest.TestCase):
    def _stat(self, *, name: str, self_time: float) -> ProfileFunctionStat:
        return ProfileFunctionStat(
            module="lisjong",
            qualified_name=name,
            call_count=1,
            self_time_seconds=self_time,
            cumulative_time_seconds=self_time,
        )

    def test_sorts_by_self_time_descending_then_name_ascending(self) -> None:
        low = self._stat(name="b", self_time=1.0)
        high = self._stat(name="a", self_time=2.0)
        tie_a = self._stat(name="tie_a", self_time=1.0)
        tie_b = self._stat(name="tie_b", self_time=1.0)

        sorted_stats = _sort_function_stats([low, high, tie_b, tie_a])

        self.assertEqual(
            [item.qualified_name for item in sorted_stats],
            ["a", "b", "tie_a", "tie_b"],
        )


class ExtractProfileFunctionStatsTest(unittest.TestCase):
    def test_extracts_a_profiled_function_with_positive_call_count(self) -> None:
        def target() -> int:
            return sum(range(100))

        profiler = cProfile.Profile()
        profiler.runcall(target)

        stats = _extract_profile_function_stats(profiler)

        matching = [item for item in stats if "target" in item.qualified_name]
        self.assertEqual(len(matching), 1)
        self.assertGreaterEqual(matching[0].call_count, 1)
        self.assertGreaterEqual(
            matching[0].cumulative_time_seconds, matching[0].self_time_seconds
        )


class InstrumentCandidateTest(unittest.TestCase):
    def test_wrapped_factory_preserves_identity_and_calls_the_original_factory_once(
        self,
    ) -> None:
        factory = _RecordingFactory()
        spec = PolicySpec(identity="candidate-x", factory=factory)
        seen_wrap_calls: list[Policy] = []

        def wrap(policy: Policy) -> Policy:
            seen_wrap_calls.append(policy)
            return policy

        wrapped_spec = _instrument_candidate(spec, wrap)

        self.assertEqual(wrapped_spec.identity, "candidate-x")
        produced = wrapped_spec.factory()
        self.assertEqual(len(factory.instances), 1)
        self.assertIs(produced, factory.instances[0])
        self.assertEqual(seen_wrap_calls, [factory.instances[0]])


def _plan(
    *, seeds: tuple[int, ...] = (12345,), candidate_error: Exception | None = None
) -> tuple[SingleRoundEvaluationPlan, _RecordingFactory]:
    candidate_factory = _RecordingFactory(error=candidate_error)
    plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(identity="candidate", factory=candidate_factory),
        baseline=PolicySpec(identity="baseline", factory=_BaselinePolicy),
        seeds=seeds,
    )
    return plan, candidate_factory


class _FakeGameRunner:
    """``_run_single_game``差し替え用のfake。

    実``LocalGameRunner``が1 decisionごとに4 seatすべてのPolicyを呼ぶ挙動を
    模擬するため、渡された全seat(baselineも含む)へ``choose_action()``を
    1回ずつ呼び出す。candidateのfactoryだけが計測用にwrapされているため、
    timing / profile集計にはcandidate分だけが現れるはずである。
    """

    def __call__(
        self,
        policies: Mapping[Seat, Policy],
        *,
        seed: int,
        max_steps: int,
    ) -> LocalGameResult:
        for seat in Seat:
            policies[seat].choose_action(f"decision-seed{seed}-seat{int(seat)}")

        scores = (30_000, 25_000, 25_000, 20_000)
        return LocalGameResult(
            seed=seed,
            game_mode=GAME_MODE,
            scores=scores,
            ranks=(1, 2, 2, 4),
            steps=1,
            decisions=4,
            seat_round_stats=neutral_seat_round_stats_tuple(scores),
        )


class RunPolicyTimingProfileTest(unittest.TestCase):
    def test_measures_only_candidate_decisions_across_rotation(self) -> None:
        seeds = (1, 2, 3)
        plan, candidate_factory = _plan(seeds=seeds)

        with mock.patch(
            "lisjong_arena.single_round_evaluation._run_single_game", _FakeGameRunner()
        ):
            profile = run_policy_timing_profile(
                plan, decision_clock=_incrementing_clock(step=1_000)
            )

        expected_candidate_decisions = ROTATION_COUNT * len(seeds)
        self.assertEqual(
            profile.candidate_decision_metrics.decision_count,
            expected_candidate_decisions,
        )
        # 1 candidate Policy instance per (seed, rotation), each invoked exactly once.
        self.assertEqual(len(candidate_factory.instances), expected_candidate_decisions)
        for instance in candidate_factory.instances:
            self.assertEqual(instance.call_count, 1)

    def test_propagates_policy_exceptions_without_swallowing_them(self) -> None:
        failure = RuntimeError("policy exploded")
        plan, _ = _plan(seeds=(1,), candidate_error=failure)

        with mock.patch(
            "lisjong_arena.single_round_evaluation._run_single_game", _FakeGameRunner()
        ):
            with self.assertRaises(SingleRoundEvaluationError) as ctx:
                run_policy_timing_profile(plan, decision_clock=_incrementing_clock())
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_evaluation_elapsed_uses_the_injected_evaluation_clock(self) -> None:
        plan, _ = _plan(seeds=(1,))
        evaluation_clock = _incrementing_clock(step=5, start=0)

        with mock.patch(
            "lisjong_arena.single_round_evaluation._run_single_game", _FakeGameRunner()
        ):
            profile = run_policy_timing_profile(
                plan,
                decision_clock=_incrementing_clock(),
                evaluation_clock=evaluation_clock,
            )

        self.assertEqual(profile.evaluation_elapsed_seconds, 5.0)
        self.assertEqual(
            profile.games_per_second, len(profile.result.game_results) / 5.0
        )


class RunPolicyHotspotProfileTest(unittest.TestCase):
    def test_measures_only_candidate_decisions_and_reports_function_stats(self) -> None:
        seeds = (1, 2)
        plan, candidate_factory = _plan(seeds=seeds)

        with mock.patch(
            "lisjong_arena.single_round_evaluation._run_single_game", _FakeGameRunner()
        ):
            profile = run_policy_hotspot_profile(plan)

        expected_candidate_decisions = ROTATION_COUNT * len(seeds)
        self.assertEqual(len(candidate_factory.instances), expected_candidate_decisions)
        for instance in candidate_factory.instances:
            self.assertEqual(instance.call_count, 1)
        self.assertGreater(len(profile.function_stats), 0)
        matching = [
            item
            for item in profile.function_stats
            if "choose_action" in item.qualified_name
        ]
        self.assertTrue(matching)

    def test_propagates_policy_exceptions_without_swallowing_them(self) -> None:
        failure = RuntimeError("policy exploded")
        plan, _ = _plan(seeds=(1,), candidate_error=failure)

        with mock.patch(
            "lisjong_arena.single_round_evaluation._run_single_game", _FakeGameRunner()
        ):
            with self.assertRaises(SingleRoundEvaluationError) as ctx:
                run_policy_hotspot_profile(plan)
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)


class SerialOnlyScopeTest(unittest.TestCase):
    """workers=1のserial executionだけを正本とする、というIssue #87の初期scope制約。

    timing / profile modeいずれの公開APIにも``workers`` / ``max_workers``引数が
    存在しないことを構造的に固定する。これにより
    ``run_single_round_evaluation_parallel()``を呼び出す経路自体が存在しない。
    """

    def test_run_policy_timing_profile_has_no_worker_count_parameter(self) -> None:
        parameters = inspect.signature(run_policy_timing_profile).parameters
        self.assertNotIn("workers", parameters)
        self.assertNotIn("max_workers", parameters)

    def test_run_policy_hotspot_profile_has_no_worker_count_parameter(self) -> None:
        parameters = inspect.signature(run_policy_hotspot_profile).parameters
        self.assertNotIn("workers", parameters)
        self.assertNotIn("max_workers", parameters)

    def test_module_does_not_reference_the_parallel_evaluation_entry_point(
        self,
    ) -> None:
        self.assertNotIn(
            "run_single_round_evaluation_parallel", dir(policy_performance)
        )


if __name__ == "__main__":
    unittest.main()
