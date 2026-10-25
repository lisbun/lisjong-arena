"""#544 riichi-first-dama-branch-v1（lisjong #356のR1）のworker。

riichi-dama-branch-v1（#540）のworkerを再利用し、次だけを変える。

```text
R      v1と同じ分岐点・宣言牌・context・特徴に加え、全席の判断ごとに
         [round ordinal, seat, 行動, 合法手の中の位置, 合法手の数, 合法手のdigest]
       を判断順に返す（objectiveな行動記録。将来、記録行動の再生を照合するため）
D arm  分岐席だけlisjongの``RiichiFirstDamaBranchPolicy``（分岐点でRの宣言牌をリーチせずに打ち、
       以後はリーチを含めChampionのまま）。v1と同じ打ち切り・digestに加え、分岐点で実際に
       打った行動、分岐後に分岐席が選んだリーチの数・最初の位置を返す
```

- workerは観測した値をそのまま返し、照合とINVALIDの判定は親processの``riichi_first_dama``が行う
- 合法手のdigestは``encode_action``の列（engineが渡した元の順序）の``canonical_digest``。
  Policy内部の解析・非公開情報は含めない
"""

import os
from collections import Counter

from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

from .call_value_shadow_worker import canonical_digest, decode_action, encode_action
from .executor import PlayedHanchan, _CountingPolicy, current_peak_rss_kib
from .executor import environment_document as _environment_document
from .kyoku_record import ROLE_INCUMBENT, SeatAssignment
from .protocol import locked_rules
from .record_store import HanchanCapture
from .riichi_dama_branch_worker import FEATURES_VERSION as _V1_FEATURES_VERSION
from .riichi_dama_branch_worker import (
    ArmCapture,
    BranchArmSettled,
    BranchObservationError,
    BranchTask,
    DObserver,
    RObserver,
    _champion,
    _Observed,
    _Unobserved,
    branch_features,
    fact_round,
    settlement_of_fact,
)

PROTOCOL_VERSION = "riichi-first-dama-branch-v1"
"""#356のR1介入の意味定義のversion（riichi-dama-branch-v1とは別）。"""
FEATURES_VERSION = _V1_FEATURES_VERSION
"""lisjong ``riichi-dama-features-v2``（v1と同じ特徴。判定には使わない）。"""
WRAPPER_VERSION = "riichi-first-dama-branch-wrapper-v1"
"""lisjong ``RIICHI_FIRST_DAMA_BRANCH_VERSION``（#357）と一致を要求する。"""


def analysis_api():
    """lisjongの特徴API・R1 wrapper（lisjong #357以降）。"""
    from lisjong.policies import riichi_dama_branch

    if riichi_dama_branch.RIICHI_DAMA_FEATURES_VERSION != FEATURES_VERSION:
        raise BranchObservationError(
            "installed lisjong implements a different riichi-dama features version"
        )
    if (
        getattr(riichi_dama_branch, "RIICHI_FIRST_DAMA_BRANCH_VERSION", None)
        != WRAPPER_VERSION
    ):
        raise BranchObservationError(
            "installed lisjong does not implement the R1 wrapper version"
        )
    return riichi_dama_branch


def legal_digest(decision) -> str:
    return canonical_digest([encode_action(item) for item in decision.legal_actions])


def decision_entry(round_ordinal: int, seat: int, decision, action) -> list:
    """1判断の行動記録（局・席・行動・合法手の中の位置・合法手の数・合法手のdigest）。"""
    legal = tuple(decision.legal_actions)
    return [
        round_ordinal,
        seat,
        encode_action(action),
        legal.index(action),
        len(legal),
        legal_digest(decision),
    ]


class RFirstObserver(RObserver):
    """v1のRObserverに、全席の判断ごとの行動記録を加える。"""

    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[list] = []

    def observe(self, seat: int, decision, action) -> None:
        self.decisions.append(
            decision_entry(self.log.round_ordinal, seat, decision, action)
        )
        super().observe(seat, decision, action)

    def finish(self, rounds: int) -> dict:
        analysis = super().finish(rounds)
        return {**analysis, "actions": self.decisions}


def r_hanchan(task, *, observed: bool, runner=run_policy_hanchan):
    """Champion×4席の1半荘。``(capture, analysis)`` を返す（``observed`` でも行動は同じ）。"""
    champion, factory = _champion(task)
    observer = RFirstObserver() if observed else _Unobserved()
    policy_calls: Counter = Counter()
    selector_calls: Counter = Counter()
    policies = {
        seat: _CountingPolicy(
            _Observed(factory(**champion.behavior_configuration), index, observer),
            policy_calls,
            seat,
        )
        for index, seat in enumerate(EngineSeat)
    }

    def count_selector(seat: EngineSeat) -> None:
        selector_calls[seat] += 1

    match = runner(
        policies,
        seed=task.seed,
        rules=locked_rules(),
        on_selector_call=count_selector,
        on_delivery=observer.on_delivery if observed else None,
    )
    capture = HanchanCapture(
        match=match,
        seed=task.seed,
        placement=task.placement,
        seats=tuple(
            SeatAssignment(ROLE_INCUMBENT, champion.policy_identity) for _ in EngineSeat
        ),
        selector_call_counts=tuple(selector_calls[seat] for seat in EngineSeat),
        policy_call_counts=tuple(policy_calls[seat] for seat in EngineSeat),
        round_evidence=None,
    )
    if not observed:
        return capture, {
            "action_sequence_sha256": observer.log.seat_digests(),
            "decisions": len(observer.log.entries),
        }
    analysis = observer.finish(len(match.history))
    for record in analysis["branches"]:
        record["features"] = branch_features(record)
    return capture, analysis


def _first_riichi_after(observer: DObserver) -> tuple[int, dict | None]:
    """対象局で分岐後に分岐席が選んだリーチの数と、最初のリーチの位置。"""
    if observer.branch_index is None:
        return 0, None
    count = 0
    first = None
    seat_decisions = 0
    for index, (ordinal, seat, value) in enumerate(observer.log.entries):
        if (
            index <= observer.branch_index
            or ordinal != observer.target_ordinal
            or seat != observer.branch_seat
        ):
            continue
        seat_decisions += 1
        if value.get("kind") != "riichi":
            continue
        count += 1
        if first is None:
            first = {
                "decision_index": index,
                "seat_decisions_after_branch": seat_decisions,
            }
    return count, first


def d_arm(task: BranchTask, *, runner=run_policy_hanchan) -> dict:
    """1 D armを実行し、照合前の観測値を返す（INVALIDの判定は親processが行う）。"""
    from lisjong.policy_contract import PolicyActionValidationError
    from lisjong.policy_contract.wind import Wind

    api = analysis_api()
    champion, factory = _champion(task)
    branch = task.branch
    seat = branch["seat"]
    declaration = decode_action(branch["declaration"])
    target = api.RoundIdentity(
        Wind(branch["round"]["prevailing_wind"]),
        branch["round"]["hand_number"],
        branch["round"]["honba"],
    )
    wrapper = api.RiichiFirstDamaBranchPolicy(
        factory(**champion.behavior_configuration), target, declaration
    )
    observer = DObserver(seat, branch["round_ordinal"])
    observer.wrapper = wrapper
    calls: Counter = Counter()
    policies = {
        engine_seat: _CountingPolicy(
            _Observed(
                wrapper
                if index == seat
                else factory(**champion.behavior_configuration),
                index,
                observer,
            ),
            calls,
            engine_seat,
        )
        for index, engine_seat in enumerate(EngineSeat)
    }
    invalid: list[str] = []
    settled: BranchArmSettled | None = None
    try:
        runner(
            policies,
            seed=task.seed,
            rules=locked_rules(),
            on_delivery=observer.on_delivery,
        )
    except BranchArmSettled as stop:
        settled = stop
    except (api.RiichiDamaBranchError, PolicyActionValidationError) as error:
        invalid.append(f"D arm: {type(error).__name__}: {error}")
    else:
        invalid.append("the hanchan finished without settling the target round")
    index = observer.branch_index
    stop = len(observer.log.entries)
    riichi_after, first = _first_riichi_after(observer)
    return {
        "branch_action": None if index is None else observer.log.entries[index][2],
        "branch_context_sha256": observer.branch_context_sha256,
        "branch_decision_index": index,
        "branch_ordinal": task.branch_ordinal,
        "decisions": stop,
        "first_riichi_after_branch": first,
        "invalid": invalid,
        "post_sha256": None if index is None else observer.log.digest(index, stop),
        "prefix_sha256": None if index is None else observer.log.digest(0, index),
        "riichi_after_branch": riichi_after,
        "round": None if settled is None else fact_round(settled.round_fact),
        "round_ordinal": observer.log.round_ordinal,
        "seat": seat,
        "seed": task.seed,
        "settlement": None
        if settled is None
        else settlement_of_fact(settled.round_fact, settled.match_fact),
        "status": wrapper.status.value,
        "wrapper_riichi_after_branch": wrapper.riichi_after_branch_count,
    }


# ---------------------------------------------------------------------------
# workerの入口（spawn workerへ渡すmodule-level関数）
# ---------------------------------------------------------------------------


def _played(capture, analysis, task) -> PlayedHanchan:
    return PlayedHanchan(
        capture,
        _environment_document(task.wheel_path),
        os.getpid(),
        current_peak_rss_kib(),
        policy_analysis=analysis,
    )


def play_r_observed(task) -> PlayedHanchan:
    capture, analysis = r_hanchan(task, observed=True)
    return _played(capture, analysis, task)


def play_r_unobserved(task) -> PlayedHanchan:
    capture, analysis = r_hanchan(task, observed=False)
    return _played(capture, analysis, task)


def play_d_arm(task: BranchTask) -> PlayedHanchan:
    record = d_arm(task)
    return _played(ArmCapture(task.seed, task.branch_ordinal), record, task)


__all__ = [
    "FEATURES_VERSION",
    "PROTOCOL_VERSION",
    "WRAPPER_VERSION",
    "RFirstObserver",
    "analysis_api",
    "d_arm",
    "decision_entry",
    "legal_digest",
    "play_d_arm",
    "play_r_observed",
    "play_r_unobserved",
    "r_hanchan",
]
