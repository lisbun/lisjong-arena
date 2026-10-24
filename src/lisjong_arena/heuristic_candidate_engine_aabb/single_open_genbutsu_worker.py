"""#524 単独副露者genbutsu候補のshadow変更頻度gateで、worker内でPolicyの判断を観測する部分。

```text
4席すべてB。各判断で
  B: 実行経路から1回だけ取得し、その行動だけを対局へ適用する
  A: 同じDecisionContextで判断ごとに新しく作り、通常実行（execute_policy）と
     trace実行（execute_policy_with_trace）を別々の新しいinstanceで1回ずつ行う
     （適用しない）。2つの行動が違えばSTOP-INVALIDの差として記録する
  -> 判断ごとに outcome・stage・A（trace / 通常）とBの判断時間
```

半荘の実行枠は#520の ``activation_task`` をそのまま使い、判断ごとの採点proxyだけを
差し替える。analysisはlisjong #343の ``SingleOpenGenbutsuAnalysis`` をそのまま運ぶ。
Arenaはgate・現物・向聴・受入・価値proxyを再計算しない。照合するのは、analysisが
報告する値どうし・analysisと実際の行動（Aが返した行動、同じ入力でのBの行動）の整合だけ。

観測はPolicy側の保存物で、客観record（第1層・第2層）へ混ぜない。
"""

import os
import time

from .backend_check import canonical_engine_value
from .executor import (
    HanchanTask,
    PlayedHanchan,
    current_peak_rss_kib,
    environment_document,
)
from .paijia_tie_worker import (
    CANDIDATE_ILLEGAL,
    CHANGE_OUTCOMES,
    INVARIANT_VIOLATION,
    NO_ANALYSIS,
    OUT_OF_SCOPE_DIFFERENCE,
    RED_ONLY_CHANGE,
    SAME,
    TILE_TYPE_CHANGE,
    TSUMOGIRI_ONLY,
    _decide_with_analysis,
    _is_discard,
    activation_task,
    change_kind,
)

STAGE_SWITCHED = "SWITCHED"
STAGE_NO_QUALIFYING = "NO_QUALIFYING_ALTERNATIVE"
STAGE_PARENT_GENBUTSU = "PARENT_ALREADY_GENBUTSU"
STAGE_NO_GENBUTSU = "NO_GENBUTSU_ALTERNATIVE"
GATE_STAGES = (
    "NOT_NORMAL_TURN",
    "SELF_RIICHI",
    "SELF_OPEN_HAND",
    "OPPONENT_RIICHI",
    "NO_OPEN_OPPONENT",
    "MULTIPLE_OPEN_OPPONENTS",
    "NOT_NORMAL_SITUATION",
    "TENPAI_REACHABLE",
    STAGE_PARENT_GENBUTSU,
    STAGE_NO_GENBUTSU,
    STAGE_NO_QUALIFYING,
    STAGE_SWITCHED,
)
"""lisjong #343の ``SingleOpenGenbutsuStage`` の全member（gateの順）。"""
SINGLE_OPEN_STAGES = GATE_STAGES[GATE_STAGES.index("NOT_NORMAL_SITUATION") :]
"""対象副露者がちょうど1人に決まった判断のstage（``open_opponent_seat`` を記録する）。"""
MEMBERSHIP_STAGES = GATE_STAGES[GATE_STAGES.index(STAGE_PARENT_GENBUTSU) :]
"""C0の現物判定まで進んだstage（``parent_in_genbutsu`` を記録する）。"""
EVALUATED_STAGES = (STAGE_NO_QUALIFYING, STAGE_SWITCHED)
"""現物の代替打牌があり、比較値を計算したstage（detailを保存する）。"""

TRACE_MISMATCH = "trace-action-mismatch"
INVALID_OUTCOMES = (
    OUT_OF_SCOPE_DIFFERENCE,
    CANDIDATE_ILLEGAL,
    INVARIANT_VIOLATION,
    TRACE_MISMATCH,
)
"""1件でもあれば STOP-INVALID になる差。"""
OUTCOMES = (SAME, TSUMOGIRI_ONLY, *CHANGE_OUTCOMES, *INVALID_OUTCOMES)


def _analysis_type():
    # lisjong d2469f2より前の環境でもこのmoduleをimportできるよう、使う時だけ解決する
    from lisjong.policies.single_open_genbutsu_placement_aware_speed_call import (
        SingleOpenGenbutsuAnalysis,
    )

    return SingleOpenGenbutsuAnalysis


def _not_lower(candidate, parent) -> bool:
    """analysisが報告する値どうしで、代替候補の条件（同向聴・3値が下がらない）を読む。"""
    return (
        candidate.post_discard_shanten == parent.post_discard_shanten
        and candidate.current_ukeire_count >= parent.current_ukeire_count
        and candidate.retained_real_value >= parent.retained_real_value
        and candidate.yaku_route_value >= parent.yaku_route_value
    )


def analysis_violations(analysis, returned_action) -> list[str]:
    """analysisが報告する値どうし、およびAが返した行動との整合を確かめる。

    gate・現物集合・比較値は再計算せず、analysisが持つ値の関係だけを見る。
    """
    violations = []
    stage = analysis.stage.name
    parent = analysis.parent_action
    selected = analysis.selected_action
    if returned_action != selected:
        violations.append("returned action differs from analysis.selected_action")
    if (stage == STAGE_SWITCHED) != (selected != parent):
        violations.append("only SWITCHED may differ from the parent action")
    if analysis.action_changed != (selected != parent):
        violations.append("action_changed does not describe the actions")
    single_open = stage in SINGLE_OPEN_STAGES
    if single_open != (analysis.open_opponent_seat is not None):
        violations.append(
            "open_opponent_seat must exist exactly for single-open stages"
        )
    if not single_open and analysis.genbutsu_tile_types:
        violations.append("genbutsu tile types recorded before a single open opponent")
    genbutsu = set(analysis.genbutsu_tile_types)
    in_genbutsu = analysis.parent_in_genbutsu
    if (stage in MEMBERSHIP_STAGES) != (in_genbutsu is not None):
        violations.append("parent_in_genbutsu must exist exactly for membership stages")
    if in_genbutsu is not None and in_genbutsu != (parent.tile.tile_type in genbutsu):
        violations.append("parent_in_genbutsu differs from the genbutsu tile types")
    if (stage == STAGE_PARENT_GENBUTSU) != (in_genbutsu is True):
        violations.append("only PARENT_ALREADY_GENBUTSU has the parent in genbutsu")
    evaluated = stage in EVALUATED_STAGES
    parent_evaluation = analysis.parent_evaluation
    alternatives = analysis.alternative_evaluations
    qualifying = analysis.qualifying_actions
    if evaluated != (parent_evaluation is not None) or evaluated != bool(alternatives):
        violations.append("evaluations must exist exactly for evaluated stages")
    if not evaluated:
        if qualifying:
            violations.append("qualifying actions outside an evaluated stage")
        return violations
    if parent_evaluation.action != parent:
        violations.append("parent_evaluation is not the parent action")
    if parent_evaluation.post_discard_shanten < 1:
        violations.append("an evaluated parent discard is tenpai")
    actions = [evaluation.action for evaluation in alternatives]
    if len(set(actions)) != len(actions) or parent in actions:
        violations.append("alternatives repeat an action or include the parent")
    if any(action.tile.tile_type not in genbutsu for action in actions):
        violations.append("an alternative is not in the genbutsu tile types")
    expected = tuple(
        evaluation.action
        for evaluation in alternatives
        if _not_lower(evaluation, parent_evaluation)
    )
    if qualifying != expected:
        violations.append(
            "qualifying actions differ from the same-shanten / not-lower values"
        )
    if stage == STAGE_NO_QUALIFYING and qualifying:
        violations.append("NO_QUALIFYING_ALTERNATIVE with qualifying actions")
    if stage == STAGE_SWITCHED and selected not in qualifying:
        violations.append("selected action is not a qualifying alternative")
    return violations


def classify_shadow(incumbent_action, candidate_action, analysis):
    """1判断を分類する（通常実行とtrace実行の一致はcallerが先に確かめる）。

    戻り値は ``(outcome, stage, violations)``。``candidate_action`` が ``None`` は
    Aの行動が合法候補に一致しなかったことを表す。
    """
    analysis_type = _analysis_type()
    ours = isinstance(analysis, analysis_type)
    stage = analysis.stage.name if ours else NO_ANALYSIS
    if candidate_action is None:
        return CANDIDATE_ILLEGAL, stage, ["candidate action is not a legal action"]
    if ours:
        violations = analysis_violations(analysis, candidate_action)
        if analysis.parent_action != incumbent_action:
            violations.append("analysis.parent_action differs from the B action")
        if violations:
            return INVARIANT_VIOLATION, stage, violations
    elif _is_discard(candidate_action):
        # lisjong #343: 打牌判断のanalysisは常にSingleOpenGenbutsuAnalysis
        return INVARIANT_VIOLATION, stage, ["a discard without the candidate analysis"]
    if candidate_action == incumbent_action:
        return SAME, stage, []
    if stage == STAGE_SWITCHED:
        return change_kind(incumbent_action, candidate_action), stage, []
    return OUT_OF_SCOPE_DIFFERENCE, stage, ["actions differ outside SWITCHED"]


def _canonical(value):
    return None if value is None else canonical_engine_value(value)


class _GenbutsuShadow:
    """Bの判断を適用し、同じ入力で新しいAを通常実行・trace実行で採点する（適用しない）。"""

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
            traced_action, analysis = _decide_with_analysis(candidate, decision)
        except PolicyActionValidationError:
            traced_action, analysis = None, None
        traced_seconds = time.perf_counter() - started
        candidate = self._candidate_factory()
        started = time.perf_counter()
        try:
            plain_action = execute_policy(candidate, decision)
        except PolicyActionValidationError:
            plain_action = None
        plain_seconds = time.perf_counter() - started
        if plain_action != traced_action:
            stage = (
                analysis.stage.name
                if isinstance(analysis, _analysis_type())
                else NO_ANALYSIS
            )
            outcome, violations = (
                TRACE_MISMATCH,
                ["normal and trace executions returned different actions"],
            )
        else:
            outcome, stage, violations = classify_shadow(
                incumbent_action, traced_action, analysis
            )
        row = {
            "a_plain_seconds": plain_seconds,
            "a_seconds": traced_seconds,
            "b_seconds": incumbent_seconds,
            "outcome": outcome,
            "stage": stage,
        }
        if outcome != SAME or stage in EVALUATED_STAGES:
            row["detail"] = {
                "analysis": _canonical(analysis),
                "candidate_action": _canonical(traced_action),
                "incumbent_action": canonical_engine_value(incumbent_action),
                "plain_candidate_action": _canonical(plain_action),
                "violations": violations,
            }
        self._rows.append(row)
        return incumbent_action


def play_genbutsu_activation_hanchan(task: HanchanTask) -> PlayedHanchan:
    """worker内で1半荘を実行する（実engine・実Policy）。"""
    environment = environment_document(task.wheel_path)
    capture, analysis = activation_task(task, shadow=_GenbutsuShadow)
    return PlayedHanchan(
        capture,
        environment,
        os.getpid(),
        current_peak_rss_kib(),
        policy_analysis=analysis,
    )


__all__ = [
    "EVALUATED_STAGES",
    "GATE_STAGES",
    "INVALID_OUTCOMES",
    "MEMBERSHIP_STAGES",
    "OUTCOMES",
    "RED_ONLY_CHANGE",
    "SINGLE_OPEN_STAGES",
    "STAGE_SWITCHED",
    "TILE_TYPE_CHANGE",
    "TRACE_MISMATCH",
    "analysis_violations",
    "classify_shadow",
    "play_genbutsu_activation_hanchan",
]
