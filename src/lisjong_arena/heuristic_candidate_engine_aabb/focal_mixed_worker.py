"""#522 混成focal比較の1半荘をworker内で実行する部分。

```text
seat r       focal: 条件AならA（execute_policy_with_traceで1回だけ判断し、
                    analysisのstage・切替を数える）、条件BならB（判断時間だけ）
seat r+1..3  O1 / O2 / O3（判断時間だけ）
各seatのPolicyは半荘ごと・seatごとにfactoryから新しく作る（共有しない）
```

Aのanalysisの検査・保存は#520のproxy（``paijia_tie_worker``）をそのまま使う。
Policy側の観測は客観record（第1層・第2層）へ混ぜず、別の保存物にする。
"""

import os
from collections import Counter

from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

from .executor import (
    PlayedHanchan,
    RunStopped,
    _CountingPolicy,
    current_peak_rss_kib,
    environment_document,
)
from .focal_mixed_plan import (
    CONDITION_A,
    CONDITIONS,
    PARTICIPANT_ROLES,
    ROLE_FOCAL,
    FocalHanchanCapture,
    FocalTask,
    seat_roles,
)
from .kyoku_record import SeatAssignment
from .paijia_tie_worker import _AnalyzedCandidate, _TimedIncumbent
from .protocol import locked_rules, parse_participant, resolve_factory


def _seat_participant(task: FocalTask, role: str, participants: dict):
    return participants[task.condition if role == ROLE_FOCAL else role]


def focal_task(task: FocalTask) -> tuple[FocalHanchanCapture, dict]:
    """1半荘を実行し、captureとPolicy側の観測（seatごとの判断時間、Aのanalysis）を返す。"""
    if task.condition not in CONDITIONS or set(task.participants) != set(
        PARTICIPANT_ROLES
    ):
        raise RunStopped("task condition or participants are invalid")
    participants = {
        role: parse_participant(document, f"participants.{role}")
        for role, document in task.participants.items()
    }
    roles = seat_roles(task.rotation)
    policy_calls: Counter = Counter()
    selector_calls: Counter = Counter()
    policies = {}
    observed = []
    assignments = []
    for index, seat in enumerate(EngineSeat):
        role = roles[index]
        participant = _seat_participant(task, role, participants)
        factory = resolve_factory(participant)
        # 半荘ごと・seatごとに新しいPolicy（seat間・半荘間・条件間で共有しない）
        policy = factory(**participant.behavior_configuration)
        entry = {"decision_seconds": [], "role": role, "seat": index}
        if role == ROLE_FOCAL and task.condition == CONDITION_A:
            entry["stage_counts"] = {}
            entry["switches"] = []
            policy = _AnalyzedCandidate(policy, entry)
        else:
            policy = _TimedIncumbent(policy, entry["decision_seconds"])
        observed.append(entry)
        policies[seat] = _CountingPolicy(policy, policy_calls, seat)
        assignments.append(SeatAssignment(role, participant.policy_identity))
    completions = []

    def count_selector(seat: EngineSeat) -> None:
        selector_calls[seat] += 1

    match = run_policy_hanchan(
        policies,
        seed=task.seed,
        rules=locked_rules(),
        on_round_evidence_complete=completions.append,
        on_selector_call=count_selector,
    )
    capture = FocalHanchanCapture(
        match=match,
        seed=task.seed,
        rotation=task.rotation,
        condition=task.condition,
        seats=tuple(assignments),
        selector_call_counts=tuple(selector_calls[seat] for seat in EngineSeat),
        policy_call_counts=tuple(policy_calls[seat] for seat in EngineSeat),
        round_evidence=tuple(completions),
    )
    return capture, {"seats": observed}


def play_focal_hanchan(task: FocalTask) -> PlayedHanchan:
    """worker内で1半荘を実行する（実engine・実Policy）。"""
    environment = environment_document(task.wheel_path)
    capture, analysis = focal_task(task)
    return PlayedHanchan(
        capture,
        environment,
        os.getpid(),
        current_peak_rss_kib(),
        policy_analysis=analysis,
    )


__all__ = ["focal_task", "play_focal_hanchan"]
