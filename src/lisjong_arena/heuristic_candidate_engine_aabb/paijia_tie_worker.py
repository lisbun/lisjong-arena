"""#520 同点数牌paijia候補の開発比較で、worker内でPolicyの判断を観測する部分。

```text
段階1（activation）  4席すべてB。各判断で
                      B: 実行経路から1回だけ取得し、その行動だけを対局へ適用する
                      A: 同じDecisionContextで判断ごとに新しく作り、lisjongの
                         execute_policy_with_traceで1回だけ採点する（適用しない）
                      -> 判断ごとに outcome・stage・A/Bの判断時間
段階2（screen）       AABBの6配置。A席はexecute_policy_with_traceで1回だけ判断し、
                      analysisのstageと切替を数える。B席は判断時間だけを計る
```

analysisはlisjongの ``SuitedPaijiaTieAnalysis`` をそのまま運ぶ。Arenaはpaijiaや
評価値を再計算しない。照合するのは、analysisが報告する値どうし・analysisと実際の
行動（Aが返した行動、同じ入力でのBの行動）の整合だけである。

観測はPolicy側の保存物で、客観record（第1層・第2層）へ混ぜない。
"""

import os
import time
from collections import Counter

from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

from .backend_check import canonical_engine_value
from .executor import (
    HanchanTask,
    PlayedHanchan,
    RunStopped,
    _CountingPolicy,
    current_peak_rss_kib,
    environment_document,
    play_task,
)
from .kyoku_record import ROLE_CANDIDATE, ROLE_INCUMBENT, SeatAssignment
from .protocol import locked_rules, parse_participant, resolve_factory
from .record_store import HanchanCapture

ACTIVATION_PLACEMENT = 0
"""段階1は配置の入替をしない。run_scheduleの照合用に固定値0を使う（AABBの配置ではない）。"""

STAGE_SWITCH = "PAIJIA_SWITCH"
STAGE_CHAMPION_MINIMUM = "CHAMPION_MINIMUM_PAIJIA"
ACTIVATED_STAGES = (STAGE_CHAMPION_MINIMUM, STAGE_SWITCH)
"""paijiaを評価した（gateを通過した）stage。"""
NO_ANALYSIS = "NO_ANALYSIS"
"""打牌以外の判断など、``SuitedPaijiaTieAnalysis`` を返さなかった判断。"""

SAME = "same"
TILE_TYPE_CHANGE = "tile-type-change"
RED_ONLY_CHANGE = "red-only-change"
TSUMOGIRI_ONLY = "tsumogiri-only"
OUT_OF_SCOPE_DIFFERENCE = "out-of-scope-difference"
CANDIDATE_ILLEGAL = "candidate-illegal-action"
INVARIANT_VIOLATION = "analysis-invariant-violation"

CHANGE_OUTCOMES = (TILE_TYPE_CHANGE, RED_ONLY_CHANGE)
"""改善候補の変更として数える差（基礎牌種の違い / 赤・通常の違い）。"""
INVALID_OUTCOMES = (OUT_OF_SCOPE_DIFFERENCE, CANDIDATE_ILLEGAL, INVARIANT_VIOLATION)
"""1件でもあれば STOP-INVALID になる差。"""
OUTCOMES = (SAME, TSUMOGIRI_ONLY, *CHANGE_OUTCOMES, *INVALID_OUTCOMES)


class PaijiaTieInvariantError(RunStopped):
    """段階2で、analysisが実際の行動や自身の値と整合しない（STOP）。"""


def _analysis_type():
    # lisjong 0fff891より前の環境でもこのmoduleをimportできるよう、使う時だけ解決する
    from lisjong.policies.placement_aware_speed_call_suited_paijia_tie import (
        SuitedPaijiaTieAnalysis,
    )

    return SuitedPaijiaTieAnalysis


def _is_discard(action) -> bool:
    from lisjong.policy_contract import DiscardAction

    return isinstance(action, DiscardAction)


def _is_honor(tile) -> bool:
    return tile.tile_type.category.name == "HONOR"


def analysis_violations(analysis, returned_action) -> list[str]:
    """analysisが報告する値どうし、およびAが返した行動との整合を確かめる。

    paijia・評価値を再計算せず、analysisが持つ値の関係だけを見る。
    """
    violations = []
    stage = analysis.stage.name
    champion = analysis.champion_action
    selected = analysis.selected_action
    if returned_action != selected:
        violations.append("returned action differs from analysis.selected_action")
    if (stage == STAGE_SWITCH) != (selected != champion):
        violations.append("only PAIJIA_SWITCH may differ from the Champion action")
    if analysis.action_changed != (selected != champion):
        violations.append("action_changed does not describe the actions")
    evaluations = analysis.paijia_evaluations
    if (stage in ACTIVATED_STAGES) != bool(evaluations):
        violations.append("paijia evaluations must exist exactly for activated stages")
    if not evaluations:
        return violations
    paijia = {}
    for evaluation in evaluations:
        if evaluation.action in paijia:
            violations.append("an action is evaluated twice")
        paijia[evaluation.action] = evaluation.paijia
        if _is_honor(evaluation.action.tile):
            violations.append("an honor discard is in the suited evaluations")
    if len({action.tile for action in paijia}) < 2:
        violations.append("fewer than two distinct suited tiles were evaluated")
    if champion not in paijia or selected not in paijia:
        violations.append("Champion or selected action is not among the evaluations")
        return violations
    minimum = min(paijia.values())
    if paijia[selected] != minimum:
        violations.append("selected action is not the minimum paijia")
    if stage == STAGE_SWITCH and paijia[champion] <= minimum:
        violations.append("PAIJIA_SWITCH while the Champion action was already minimum")
    if stage == STAGE_CHAMPION_MINIMUM and paijia[champion] != minimum:
        violations.append("CHAMPION_MINIMUM_PAIJIA while the Champion was not minimum")
    return violations


def change_kind(incumbent_action, candidate_action) -> str:
    """2つの打牌の違いの種類（同じ打牌は呼ばない）。"""
    if candidate_action.tile == incumbent_action.tile:
        return TSUMOGIRI_ONLY
    if candidate_action.tile.tile_type == incumbent_action.tile.tile_type:
        return RED_ONLY_CHANGE
    return TILE_TYPE_CHANGE


def classify_shadow(incumbent_action, candidate_action, analysis):
    """段階1の1判断を分類する。戻り値は ``(outcome, stage, violations)``。

    ``candidate_action`` が ``None`` はAの行動が合法候補に一致しなかったことを表す。
    """
    analysis_type = _analysis_type()
    stage = analysis.stage.name if isinstance(analysis, analysis_type) else NO_ANALYSIS
    if candidate_action is None:
        return CANDIDATE_ILLEGAL, stage, ["candidate action is not a legal action"]
    if isinstance(analysis, analysis_type):
        violations = analysis_violations(analysis, candidate_action)
        if analysis.champion_action != incumbent_action:
            violations.append("analysis.champion_action differs from the B action")
        if violations:
            return INVARIANT_VIOLATION, stage, violations
    elif _is_discard(candidate_action):
        # lisjong #330: 打牌判断のanalysisは常にSuitedPaijiaTieAnalysis
        return INVARIANT_VIOLATION, stage, ["a discard without the candidate analysis"]
    if candidate_action == incumbent_action:
        return SAME, stage, []
    if stage == STAGE_SWITCH:
        return change_kind(incumbent_action, candidate_action), stage, []
    return OUT_OF_SCOPE_DIFFERENCE, stage, ["actions differ outside PAIJIA_SWITCH"]


class _TraceSink:
    __slots__ = ("trace",)

    def __init__(self) -> None:
        self.trace = None

    def on_decision(self, trace) -> None:
        self.trace = trace


def _decide_with_analysis(policy, decision):
    """lisjongのexecute_policy_with_traceでPolicyを1回だけ実行し、行動とanalysisを返す。"""
    from lisjong.policy_contract import execute_policy_with_trace

    sink = _TraceSink()
    action = execute_policy_with_trace(policy, decision, sink)
    return action, sink.trace.analysis


class _ShadowedIncumbent:
    """Bの判断を適用し、同じ入力で新しいAを採点する（Aの行動は適用しない）。"""

    __slots__ = ("_incumbent", "_candidate_factory", "_rows")

    def __init__(self, incumbent, candidate_factory, rows: list) -> None:
        self._incumbent = incumbent
        self._candidate_factory = candidate_factory
        self._rows = rows

    def choose_action(self, decision):
        from lisjong.policy_contract import (
            PolicyActionValidationError,
            execute_policy,
        )

        started = time.perf_counter()
        incumbent_action = execute_policy(self._incumbent, decision)
        incumbent_seconds = time.perf_counter() - started
        candidate = self._candidate_factory()
        started = time.perf_counter()
        try:
            candidate_action, analysis = _decide_with_analysis(candidate, decision)
        except PolicyActionValidationError:
            candidate_action, analysis = None, None
        candidate_seconds = time.perf_counter() - started
        outcome, stage, violations = classify_shadow(
            incumbent_action, candidate_action, analysis
        )
        row = {
            "a_seconds": candidate_seconds,
            "b_seconds": incumbent_seconds,
            "outcome": outcome,
            "stage": stage,
        }
        if outcome != SAME or stage in ACTIVATED_STAGES:
            row["detail"] = {
                "analysis": None
                if analysis is None
                else canonical_engine_value(analysis),
                "candidate_action": None
                if candidate_action is None
                else canonical_engine_value(candidate_action),
                "incumbent_action": canonical_engine_value(incumbent_action),
                "violations": violations,
            }
        self._rows.append(row)
        return incumbent_action


def activation_task(task: HanchanTask, *, shadow=None) -> tuple[HanchanCapture, dict]:
    """段階1の1半荘: B×4席で対局し、全判断でAをshadow採点する。

    ``shadow(incumbent, new_candidate, rows)`` は判断ごとの採点proxy（defaultは#520の
    ``_ShadowedIncumbent``）。#524は同じ枠で候補固有の分類だけを差し替える。proxyは
    Bの行動だけを返し、対局へAの行動を適用しない。
    """
    shadow = _ShadowedIncumbent if shadow is None else shadow
    candidate = parse_participant(task.candidate, "candidate")
    incumbent = parse_participant(task.incumbent, "incumbent")
    candidate_factory = resolve_factory(candidate)
    incumbent_factory = resolve_factory(incumbent)
    candidate_configuration = candidate.behavior_configuration
    # lazy importを判断時間の外で済ませる（作ったinstanceは使わない）
    candidate_factory(**candidate_configuration)

    def new_candidate():
        return candidate_factory(**candidate_configuration)

    policy_calls: Counter = Counter()
    selector_calls: Counter = Counter()
    rows = {seat: [] for seat in EngineSeat}
    policies = {
        seat: _CountingPolicy(
            shadow(
                incumbent_factory(**incumbent.behavior_configuration),
                new_candidate,
                rows[seat],
            ),
            policy_calls,
            seat,
        )
        for seat in EngineSeat
    }

    def count_selector(seat: EngineSeat) -> None:
        selector_calls[seat] += 1

    match = run_policy_hanchan(
        policies, seed=task.seed, rules=locked_rules(), on_selector_call=count_selector
    )
    capture = HanchanCapture(
        match=match,
        seed=task.seed,
        placement=task.placement,
        seats=tuple(
            SeatAssignment(ROLE_INCUMBENT, incumbent.policy_identity)
            for _ in EngineSeat
        ),
        selector_call_counts=tuple(selector_calls[seat] for seat in EngineSeat),
        policy_call_counts=tuple(policy_calls[seat] for seat in EngineSeat),
        round_evidence=None,
    )
    analysis = {
        "seats": [
            {"decisions": rows[seat], "seat": index}
            for index, seat in enumerate(EngineSeat)
        ]
    }
    return capture, analysis


def play_activation_hanchan(task: HanchanTask) -> PlayedHanchan:
    """worker内で段階1の1半荘を実行する（実engine・実Policy）。"""
    environment = environment_document(task.wheel_path)
    capture, analysis = activation_task(task)
    return PlayedHanchan(
        capture,
        environment,
        os.getpid(),
        current_peak_rss_kib(),
        policy_analysis=analysis,
    )


class _TimedIncumbent:
    __slots__ = ("_policy", "_seconds")

    def __init__(self, policy, seconds: list) -> None:
        self._policy = policy
        self._seconds = seconds

    def choose_action(self, decision):
        started = time.perf_counter()
        action = self._policy.choose_action(decision)
        self._seconds.append(time.perf_counter() - started)
        return action


class _AnalyzedCandidate:
    """Aを1回だけ実行し、analysisのstageと切替を数える。行動は変えない。"""

    __slots__ = ("_policy", "_seat")

    def __init__(self, policy, seat: dict) -> None:
        self._policy = policy
        self._seat = seat

    def choose_action(self, decision):
        started = time.perf_counter()
        action, analysis = _decide_with_analysis(self._policy, decision)
        self._seat["decision_seconds"].append(time.perf_counter() - started)
        stage = NO_ANALYSIS
        if not isinstance(analysis, _analysis_type()) and _is_discard(action):
            raise PaijiaTieInvariantError(
                "candidate returned a discard without SuitedPaijiaTieAnalysis"
            )
        if isinstance(analysis, _analysis_type()):
            stage = analysis.stage.name
            violations = analysis_violations(analysis, action)
            if violations:
                raise PaijiaTieInvariantError(
                    f"candidate analysis is inconsistent: {violations}"
                )
            if stage == STAGE_SWITCH:
                self._seat["switches"].append(
                    {
                        "analysis": canonical_engine_value(analysis),
                        "kind": change_kind(analysis.champion_action, action),
                    }
                )
        counts = self._seat["stage_counts"]
        counts[stage] = counts.get(stage, 0) + 1
        return action


def screen_task(task: HanchanTask) -> tuple[HanchanCapture, dict]:
    """段階2の1半荘: 実行は正式runと同じplay_task。Policyを観測用proxyで包むだけ。"""
    seats = []

    def wrap(seat: EngineSeat, role: str, policy):
        entry = {
            "decision_seconds": [],
            "role": role,
            "seat": list(EngineSeat).index(seat),
        }
        seats.append(entry)
        if role == ROLE_CANDIDATE:
            entry["stage_counts"] = {}
            entry["switches"] = []
            return _AnalyzedCandidate(policy, entry)
        return _TimedIncumbent(policy, entry["decision_seconds"])

    capture = play_task(task, wrap_policy=wrap)
    return capture, {"seats": sorted(seats, key=lambda entry: entry["seat"])}


def play_screen_hanchan(task: HanchanTask) -> PlayedHanchan:
    """worker内で段階2の1半荘を実行する（実engine・実Policy）。"""
    environment = environment_document(task.wheel_path)
    capture, analysis = screen_task(task)
    return PlayedHanchan(
        capture,
        environment,
        os.getpid(),
        current_peak_rss_kib(),
        policy_analysis=analysis,
    )


__all__ = [
    "ACTIVATED_STAGES",
    "ACTIVATION_PLACEMENT",
    "CANDIDATE_ILLEGAL",
    "CHANGE_OUTCOMES",
    "INVALID_OUTCOMES",
    "INVARIANT_VIOLATION",
    "NO_ANALYSIS",
    "OUTCOMES",
    "OUT_OF_SCOPE_DIFFERENCE",
    "RED_ONLY_CHANGE",
    "SAME",
    "STAGE_SWITCH",
    "TILE_TYPE_CHANGE",
    "TSUMOGIRI_ONLY",
    "PaijiaTieInvariantError",
    "activation_task",
    "analysis_violations",
    "change_kind",
    "classify_shadow",
    "play_activation_hanchan",
    "play_screen_hanchan",
    "screen_task",
]
