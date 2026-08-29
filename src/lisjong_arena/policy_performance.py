"""first-party lisjong Policyのopt-in performance profiling / timing path。

lisbun/lisjong-arena#87で追加した、``FiniteHorizon`` / ``Combined``系Policyの
次の高速化対象をprofile-drivenに判断するためのopt-in development diagnostic
である。既存ABBB single-round evaluation substrate
(``lisjong_arena.single_round_evaluation``)をそのまま再利用し、新しい
evaluation protocolやgame rule / rotation ruleは追加しない。

timing modeとprofile modeは明確に分離する。

    timing mode
        unprofiled wall-clock performance measurement (正本)
        ``time.perf_counter_ns()``相当のmonotonic clockでcandidate
        ``choose_action()``呼び出し境界だけを計測する

    profile mode
        instrumented hotspot discovery
        ``cProfile`` / ``pstats``でcandidate decision内のfunction call count /
        self time / cumulative timeを観測する。ここで得たelapsed timeを
        absolute latencyやbefore/after speedupのperformance claimへ使用しない

どちらのmodeも、ABBB rotation中のcandidate Policy invocationだけを計測対象と
する。candidateの``PolicySpec.factory``だけを計測用にwrapし、baselineの
Policyや既存``run_single_round_evaluation()``のPolicy instance lifecycle
(seat間・game間で共有しない、各game・各seatごとにfactoryから新規生成する)は
変更しない。計測のためにPolicy decisionを追加実行しない。実際に発生する1回の
``choose_action()``呼び出しをその場で計測するだけであり、``LocalGameRunner``
やlisjong-owned``execute_policy()``の呼び出し境界そのものも変更しない。

``PolicyInput`` / ``PolicyDecision`` / ``DecisionTrace`` / ``AnalysisTrace`` /
``GameTrace`` / ``SingleRoundEvaluationResult``等のcanonical evaluation
schemaへperformance fieldを追加しない。ここで定義するvalueはすべて
Arena-owned opt-in development diagnosticとして独立している。

初期scopeはworkers=1のserial executionだけを正本とする。この module は常に
``run_single_round_evaluation()``(serial)だけを呼び、
``run_single_round_evaluation_parallel()``は一切使わない。worker別profile
aggregationやmultiprocessing scaling profileはこのmoduleのscope外である。
"""

from __future__ import annotations

import cProfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import ceil, isfinite

from lisjong.policy_contract import Policy

from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
)
from lisjong_arena.single_round_evaluation import run_single_round_evaluation

DecisionClock = Callable[[], int]
"""``time.perf_counter_ns()``相当の、ns単位monotonic high-resolution clock。"""

EvaluationClock = Callable[[], float]
"""``time.perf_counter()``相当の、秒単位monotonic clock。"""


def _instrument_candidate(
    spec: PolicySpec, wrap: Callable[[Policy], Policy]
) -> PolicySpec:
    """candidateの``factory``だけを計測用にwrapした新しい``PolicySpec``を返す。

    ``identity``は変更しない。baseline側の``PolicySpec``はこの関数の対象外
    であり、呼び出し側は変更せずそのまま使う。wrapped factoryは呼ばれるたびに
    既存``spec.factory()``を1回呼んでfreshなPolicy instanceを取得し、
    その返り値をwrapするだけなので、既存の「各game・各seatごとにfactoryから
    新規生成し、instanceをseat間・game間で共有しない」というPolicy instance
    lifecycle契約は変更しない。
    """
    if not isinstance(spec, PolicySpec):
        raise TypeError("spec must be a PolicySpec")
    if not callable(wrap):
        raise TypeError("wrap must be callable")

    def wrapped_factory() -> Policy:
        return wrap(spec.factory())

    return PolicySpec(identity=spec.identity, factory=wrapped_factory)


def _instrumented_plan(
    plan: SingleRoundEvaluationPlan, wrap: Callable[[Policy], Policy]
) -> SingleRoundEvaluationPlan:
    return SingleRoundEvaluationPlan(
        candidate=_instrument_candidate(plan.candidate, wrap),
        baseline=plan.baseline,
        seeds=plan.seeds,
        max_steps=plan.max_steps,
    )


class _TimingInstrumentedPolicy:
    """実Policyの``choose_action()``呼び出し1回だけをclockで計測するshim。

    ``choose_action()``は実Policyの``choose_action()``をちょうど1回呼ぶだけで
    あり、計測のための追加実行は行わない。実行時間はns単位のintとして
    ``durations``へ追加する。実Policyが例外を送出した場合はdurationを記録
    せず、そのまま伝播させる(既存のPolicy例外伝播semanticsを保つ)。
    """

    __slots__ = ("_clock", "_durations", "_wrapped")

    def __init__(
        self,
        wrapped: Policy,
        durations: list[int],
        *,
        clock: DecisionClock,
    ) -> None:
        self._wrapped = wrapped
        self._durations = durations
        self._clock = clock

    def choose_action(self, decision: object) -> object:
        start = self._clock()
        result = self._wrapped.choose_action(decision)
        elapsed = self._clock() - start
        self._durations.append(elapsed)
        return result


class _ProfileInstrumentedPolicy:
    """実Policyの``choose_action()``呼び出し1回だけを共有``cProfile.Profile``へ蓄積するshim。

    ``Profile.runcall()``は呼び出し前後で``enable()`` / ``disable()``するだけの
    標準libraryの薄いwrapperであり、Policyを追加実行しない。複数decisionに
    わたる呼び出しは同じ``Profile`` instanceへ累積する。例外は
    ``Profile.runcall()``内部のtry/finallyにより``disable()``を経てそのまま
    伝播する。
    """

    __slots__ = ("_profiler", "_wrapped")

    def __init__(self, wrapped: Policy, profiler: cProfile.Profile) -> None:
        self._wrapped = wrapped
        self._profiler = profiler

    def choose_action(self, decision: object) -> object:
        return self._profiler.runcall(self._wrapped.choose_action, decision)


def _percentile(sorted_values: Sequence[int], percentile: float) -> int:
    """nearest-rank法で``percentile``(0 < percentile <= 100)に対応するvalueを返す。

    ``rank = ceil(percentile / 100 * n)``を1-based順位とし、0-based indexへ
    変換する。``sorted_values``は昇順であることを呼び出し側が保証する。この
    定義はdeterministicかつ入力順序に依存しない。
    """
    n = len(sorted_values)
    rank = ceil(percentile / 100 * n)
    index = max(0, min(n - 1, rank - 1))
    return sorted_values[index]


@dataclass(frozen=True, slots=True)
class DecisionTimingMetrics:
    """candidate Policy decisionだけのtiming aggregation(timing modeの正本)。

    percentileは``_percentile()``のnearest-rank法で固定する。母集団は
    candidate seatの``choose_action()``呼び出しだけであり、baselineの
    decisionは含まない。
    """

    decision_count: int
    total_decision_time_ns: int
    mean_decision_latency_ns: float
    p50_decision_latency_ns: int
    p95_decision_latency_ns: int
    max_decision_latency_ns: int
    decisions_per_second: float

    def __post_init__(self) -> None:
        if type(self.decision_count) is not int:
            raise TypeError("decision_count must be an int")
        if self.decision_count <= 0:
            raise ValueError("decision_count must be positive")

        for name in (
            "total_decision_time_ns",
            "p50_decision_latency_ns",
            "p95_decision_latency_ns",
            "max_decision_latency_ns",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an int")
            if value < 0:
                raise ValueError(f"{name} must not be negative")

        for name in ("mean_decision_latency_ns", "decisions_per_second"):
            value = getattr(self, name)
            if type(value) is not float:
                raise TypeError(f"{name} must be a float")
            if value != value or value < 0:
                raise ValueError(f"{name} must be a non-negative float")

        if self.p50_decision_latency_ns > self.max_decision_latency_ns:
            raise ValueError(
                "p50_decision_latency_ns must not exceed max_decision_latency_ns"
            )
        if self.p95_decision_latency_ns > self.max_decision_latency_ns:
            raise ValueError(
                "p95_decision_latency_ns must not exceed max_decision_latency_ns"
            )
        if self.p50_decision_latency_ns > self.p95_decision_latency_ns:
            raise ValueError(
                "p50_decision_latency_ns must not exceed p95_decision_latency_ns"
            )


def aggregate_decision_timings(durations_ns: Sequence[int]) -> DecisionTimingMetrics:
    """candidate decision durations(ns)から決定的な``DecisionTimingMetrics``を導出する。

    ``durations_ns``はcandidate seatの``choose_action()``呼び出しだけを、
    発生順で保持したns単位のint collectionである。集計前に昇順へsortする
    ため、``durations_ns``自体の入力順序には依存しない。
    """
    if isinstance(durations_ns, (str, bytes, bytearray)):
        raise TypeError("durations_ns must be an ordered collection of ints")
    try:
        durations = tuple(durations_ns)
    except TypeError:
        raise TypeError("durations_ns must be an iterable") from None
    if not durations:
        raise ValueError("durations_ns must not be empty")
    if any(type(value) is not int for value in durations):
        raise TypeError("durations_ns must contain only ints")
    if any(value < 0 for value in durations):
        raise ValueError("durations_ns must not contain negative values")

    sorted_durations = tuple(sorted(durations))
    count = len(sorted_durations)
    total = sum(sorted_durations)
    mean = total / count
    total_seconds = total / 1_000_000_000
    throughput = count / total_seconds if total_seconds > 0 else float("inf")

    return DecisionTimingMetrics(
        decision_count=count,
        total_decision_time_ns=total,
        mean_decision_latency_ns=mean,
        p50_decision_latency_ns=_percentile(sorted_durations, 50),
        p95_decision_latency_ns=_percentile(sorted_durations, 95),
        max_decision_latency_ns=sorted_durations[-1],
        decisions_per_second=throughput,
    )


@dataclass(frozen=True, slots=True)
class PolicyTimingProfileResult:
    """1回のtiming mode profileの実行条件・raw evaluation result・candidate timing集計。

    ``result``は既存``SingleRoundEvaluationResult``をそのまま持つ
    (canonical schemaは変更しない)。``result.plan.candidate.factory``は
    計測用にwrapされているが、``result.plan.candidate.identity``は呼び出し側
    が渡した``candidate``のidentityと一致する。
    """

    result: SingleRoundEvaluationResult
    candidate_decision_metrics: DecisionTimingMetrics
    evaluation_elapsed_seconds: float
    games_per_second: float

    def __post_init__(self) -> None:
        if not isinstance(self.result, SingleRoundEvaluationResult):
            raise TypeError("result must be a SingleRoundEvaluationResult")
        if not isinstance(self.candidate_decision_metrics, DecisionTimingMetrics):
            raise TypeError(
                "candidate_decision_metrics must be a DecisionTimingMetrics"
            )
        if type(self.evaluation_elapsed_seconds) is not float:
            raise TypeError("evaluation_elapsed_seconds must be a float")
        if (
            not isfinite(self.evaluation_elapsed_seconds)
            or self.evaluation_elapsed_seconds < 0
        ):
            raise ValueError(
                "evaluation_elapsed_seconds must be a non-negative finite float"
            )
        if type(self.games_per_second) is not float:
            raise TypeError("games_per_second must be a float")
        if self.games_per_second != self.games_per_second or self.games_per_second < 0:
            raise ValueError("games_per_second must be a non-negative float")


def run_policy_timing_profile(
    plan: SingleRoundEvaluationPlan,
    *,
    decision_clock: DecisionClock = time.perf_counter_ns,
    evaluation_clock: EvaluationClock = time.perf_counter,
    progress_callback: Callable[[int, int], None] | None = None,
) -> PolicyTimingProfileResult:
    """既存ABBB single-round評価を1回だけ実行し、candidate decisionのtimingを計測する。

    常に``run_single_round_evaluation()``(serial)だけを呼ぶ。
    ``run_single_round_evaluation_parallel()``は使わない。timing modeでは
    workers=1のserial executionをperformance diagnosisの正本とするためで
    ある。``decision_clock``/``evaluation_clock``はtestでfake / injected
    clockに差し替えるためのopt-inであり、既定値はいずれも
    ``time.perf_counter*``相当のmonotonic clockである。
    """
    if not isinstance(plan, SingleRoundEvaluationPlan):
        raise TypeError("plan must be a SingleRoundEvaluationPlan")

    durations: list[int] = []
    instrumented_plan = _instrumented_plan(
        plan,
        lambda policy: _TimingInstrumentedPolicy(
            policy, durations, clock=decision_clock
        ),
    )

    start = evaluation_clock()
    if progress_callback is None:
        result = run_single_round_evaluation(instrumented_plan)
    else:
        result = run_single_round_evaluation(
            instrumented_plan, progress_callback=progress_callback
        )
    elapsed = evaluation_clock() - start
    if elapsed < 0:
        raise ValueError("evaluation_clock must be monotonic non-decreasing")

    metrics = aggregate_decision_timings(tuple(durations))
    game_count = len(result.game_results)
    games_per_second = float(game_count) / elapsed if elapsed > 0 else float("inf")

    return PolicyTimingProfileResult(
        result=result,
        candidate_decision_metrics=metrics,
        evaluation_elapsed_seconds=float(elapsed),
        games_per_second=games_per_second,
    )


_FIRST_PARTY_PACKAGE_ANCHORS = (
    "lisjong_engine",
    "lisjong_arena",
    "lisjong",
    "riichienv",
)
"""qualified module hintを導出する際に探す、first-party package rootの名前。

判定はfilename path segment上の完全一致で行い、部分文字列一致による
偶然の誤検出("not_lisjong"等)を避ける。"""


def _module_hint_from_filename(filename: str) -> str:
    """profile対象functionのfilenameから、first-party moduleのdotted名を推測する。

    これはhuman-readable reportのための best-effort hint であり、stable
    public schemaではない。first-party package root配下と判定できない
    filename(標準library、C拡張、``<string>``等)は空文字列を返す。
    """
    parts = filename.replace("\\", "/").split("/")
    for anchor in _FIRST_PARTY_PACKAGE_ANCHORS:
        if anchor not in parts:
            continue
        index = parts.index(anchor)
        module_parts = list(parts[index:])
        if module_parts and module_parts[-1].endswith(".py"):
            module_parts[-1] = module_parts[-1][: -len(".py")]
        if module_parts and module_parts[-1] == "__init__":
            module_parts = module_parts[:-1]
        if module_parts:
            return ".".join(module_parts)
    return ""


@dataclass(frozen=True, slots=True)
class ProfileFunctionStat:
    """profile mode(hotspot discovery)における1 functionのread-only observation。

    特定のfunction名やmoduleをstable public schemaとして固定するものでは
    ない。implementation profiling reportであり、内部function structureの
    refactorを妨げない。
    """

    module: str
    qualified_name: str
    call_count: int
    self_time_seconds: float
    cumulative_time_seconds: float

    def __post_init__(self) -> None:
        if type(self.module) is not str:
            raise TypeError("module must be a str")
        if type(self.qualified_name) is not str:
            raise TypeError("qualified_name must be a str")
        if not self.qualified_name:
            raise ValueError("qualified_name must not be empty")
        if type(self.call_count) is not int:
            raise TypeError("call_count must be an int")
        if self.call_count < 0:
            raise ValueError("call_count must not be negative")
        for name in ("self_time_seconds", "cumulative_time_seconds"):
            value = getattr(self, name)
            if type(value) is not float:
                raise TypeError(f"{name} must be a float")
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a non-negative finite float")


def _sort_function_stats(
    stats: Sequence[ProfileFunctionStat],
) -> tuple[ProfileFunctionStat, ...]:
    """``self_time_seconds``降順、同値は``qualified_name``昇順で決定的にsortする。"""
    return tuple(
        sorted(stats, key=lambda item: (-item.self_time_seconds, item.qualified_name))
    )


def _extract_profile_function_stats(
    profiler: cProfile.Profile,
) -> tuple[ProfileFunctionStat, ...]:
    """``cProfile.Profile``の蓄積statsから決定的な``ProfileFunctionStat``列を作る。

    標準libraryの``Profile.getstats()``が返す生の``profiler_entry``
    (``code`` / ``callcount`` / ``totaltime`` / ``inlinetime``)だけを読み、
    ``_sort_function_stats()``でdeterministicな順序へcanonicalizeする。
    """
    entries: list[ProfileFunctionStat] = []
    for entry in profiler.getstats():
        code = entry.code
        if isinstance(code, str):
            filename = "~"
            qualified_name = code
        else:
            filename = code.co_filename
            qualified_name = f"{filename}:{code.co_firstlineno}({code.co_name})"
        entries.append(
            ProfileFunctionStat(
                module=_module_hint_from_filename(filename),
                qualified_name=qualified_name,
                call_count=entry.callcount,
                self_time_seconds=entry.inlinetime,
                cumulative_time_seconds=entry.totaltime,
            )
        )
    return _sort_function_stats(entries)


@dataclass(frozen=True, slots=True)
class PolicyHotspotProfileResult:
    """1回のprofile mode(hotspot discovery)の実行条件・raw evaluation result・function hotspots。

    ``function_stats``のelapsed timeは、instrumentation overheadを含む
    profile modeの計測値である。absolute latencyやbefore/after speedupの
    performance claimには使用しない(timing modeが計測の正本)。
    """

    result: SingleRoundEvaluationResult
    function_stats: tuple[ProfileFunctionStat, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.result, SingleRoundEvaluationResult):
            raise TypeError("result must be a SingleRoundEvaluationResult")
        try:
            function_stats = tuple(self.function_stats)
        except TypeError:
            raise TypeError("function_stats must be an iterable") from None
        if any(not isinstance(item, ProfileFunctionStat) for item in function_stats):
            raise TypeError("function_stats must contain only ProfileFunctionStat")
        object.__setattr__(self, "function_stats", function_stats)


def run_policy_hotspot_profile(
    plan: SingleRoundEvaluationPlan,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> PolicyHotspotProfileResult:
    """既存ABBB single-round評価を1回だけ実行し、candidate decision内のhotspotを``cProfile``で観測する。

    常に``run_single_round_evaluation()``(serial)だけを呼ぶ。
    ``run_single_round_evaluation_parallel()``は使わない。profile modeは
    hotspot discovery専用であり、ここで得たelapsed timeをtiming performance
    claimへ使用しない。
    """
    if not isinstance(plan, SingleRoundEvaluationPlan):
        raise TypeError("plan must be a SingleRoundEvaluationPlan")

    profiler = cProfile.Profile()
    instrumented_plan = _instrumented_plan(
        plan, lambda policy: _ProfileInstrumentedPolicy(policy, profiler)
    )

    if progress_callback is None:
        result = run_single_round_evaluation(instrumented_plan)
    else:
        result = run_single_round_evaluation(
            instrumented_plan, progress_callback=progress_callback
        )

    return PolicyHotspotProfileResult(
        result=result, function_stats=_extract_profile_function_stats(profiler)
    )


__all__ = [
    "DecisionClock",
    "DecisionTimingMetrics",
    "EvaluationClock",
    "PolicyHotspotProfileResult",
    "PolicyTimingProfileResult",
    "ProfileFunctionStat",
    "aggregate_decision_timings",
    "run_policy_hotspot_profile",
    "run_policy_timing_profile",
]
