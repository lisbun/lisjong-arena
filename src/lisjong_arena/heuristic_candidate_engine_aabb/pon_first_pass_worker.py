"""#553 pon-first-pass-branch-v1（lisjong #362のP1 pilot）のworker。

```text
R      Champion×4で半荘を最後まで実行する
         全席の判断ごとに [round ordinal, seat, 行動, 合法手の中の位置, 合法手の数, 合法手のdigest]
         を判断順に記録する（R1と同じ行動記録）
         分岐点の候補: lisjong ``is_pon_branch_candidate``（門前の自席の合法なPon）が真になった
           その席・その局の最初の判断。判断時点のpublic context・選んだPon・lisjongの鳴き解析
           （#350、call-value-shadow-v1）を記録する
         局の終了（``on_round_evidence_complete``、分岐席のplayer-safe evidence）で、候補のPonが
           成立したか（ESTABLISHED -> 分岐）、他家ロンで取り消されたか（CANCELLED-RON -> 分岐に
           しない、件数と位置だけ）を決める。それ以外（優先鳴き・理由なしの消失）は起こりえない
           ので INVALID
D arm  同じseedで半荘の先頭から再実行し、分岐席だけlisjong ``PonFirstPassBranchPolicy``へ替える。
       対象局のevidenceで、分岐点の打牌に分岐席の副露がないこと（Passの適用）と、分岐後に
       分岐席が選んだ鳴きの成立 / 取消を読む。対象局の精算（``on_delivery``）で打ち切る
```

- **記録のfield分離**: 分岐記録は ``decision_input``（分岐席の判断時点のcontext・選んだPon・
  鳴き解析。将来の特徴入力の元）と ``selection``（同じwindowの解決結果・成立 / 取消。標本選択と
  照合に使い、特徴にはしない）に分ける。D armの結果（精算・分岐後の行動）はD側の記録に置く
- wrapperの``calls_after_branch_count``は分岐後にinnerが鳴きを**選んだ**回数（ロンで取り消された
  鳴きを含みうる）。**成立**はevidenceから別に記録する
- workerは観測した値をそのまま返し、照合とINVALIDの判定は親processの``pon_first_pass``が行う
- 解析・wrapperの入力はその判断のpublic ``DecisionContext``だけ。後続event・他席の情報・
  privileged observer dataを渡さない
"""

import os
import time
from collections import Counter

from lisjong_engine.round_completion import RoundCompletionFact
from lisjong_engine.round_evidence import MeldCalledEvidence
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.domain_conversion import seat_from_engine_seat
from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

from .backend_check import canonical_engine_value
from .call_value_shadow_worker import (
    CANCELLED_RON,
    ESTABLISHED,
    INVALID,
    CallResolutionError,
    canonical_digest,
    decode_action,
    decode_context,
    encode_action,
    encode_context,
    resolve_call,
)
from .call_value_shadow_worker import SHADOW_VERSION as CALL_ANALYSIS_VERSION
from .executor import PlayedHanchan, _CountingPolicy, current_peak_rss_kib
from .executor import environment_document as _environment_document
from .kyoku_record import ROLE_INCUMBENT, SeatAssignment
from .protocol import locked_rules
from .record_store import HanchanCapture
from .riichi_dama_branch_worker import (
    ActionLog,
    ArmCapture,
    BranchArmSettled,
    BranchObservationError,
    BranchTask,
    _champion,
    _Observed,
    _Unobserved,
    context_round,
    fact_round,
    settlement_of_fact,
)
from .riichi_first_dama_worker import decision_entry, legal_digest

PROTOCOL_VERSION = "pon-first-pass-branch-v1"
"""#362のP1介入の意味定義のversion。"""
WRAPPER_VERSION = "pon-first-pass-branch-wrapper-v1"
"""lisjong ``PON_FIRST_PASS_BRANCH_VERSION``（#363）と一致を要求する。"""
CALL_ANALYSIS = CALL_ANALYSIS_VERSION
"""分岐点の鳴き解析（lisjong #350 ``analyze_speed_call_decision``）のversion。記述と将来の
特徴の元で、判定には使わない。"""

DECISION_INPUT_FIELDS = frozenset(
    {"call_analysis", "call_analysis_error", "context", "context_sha256", "pon"}
)
"""分岐席の判断時点の値だけ（将来の特徴入力の元）。"""
SELECTION_FIELDS = frozenset({"called_discard_order", "resolution"})
"""標本選択・照合だけに使う値（windowの解決結果）。特徴にはしない。"""
MEASUREMENT_KEY = "measurement"
"""D armの時間の測定値（記録本体と分けて返す。親processが切り離してtimingsへ置く）。

再実行で一致しないので、行動・結果の完全一致の比較対象に入れない。"""
MEASUREMENT_CLOCK = "worker-monotonic"
"""worker内の``time.monotonic``。``pre_branch_seconds``はrunnerの開始から分岐点の判断を
記録するまで（分岐点の判断を含む）、``arm_seconds``はrunnerの開始から対象局の精算での打ち切り
（または失敗）まで。"""


def split_measurement(analysis: dict) -> tuple[dict, dict | None]:
    """D armの観測を (記録本体, 測定値) に分ける。"""
    record = {key: value for key, value in analysis.items() if key != MEASUREMENT_KEY}
    return record, analysis.get(MEASUREMENT_KEY)


def analysis_api():
    """lisjongのP1 wrapper・分岐点の共有判定（lisjong #363以降）。"""
    from lisjong.policies import pon_first_pass_branch

    if (
        getattr(pon_first_pass_branch, "PON_FIRST_PASS_BRANCH_VERSION", None)
        != WRAPPER_VERSION
    ):
        raise BranchObservationError(
            "installed lisjong does not implement the P1 wrapper version"
        )
    return pon_first_pass_branch


def call_analysis_api():
    """lisjongの鳴き解析（#350）。"""
    from lisjong.policies import speed_call_analysis

    if speed_call_analysis.CALL_VALUE_SHADOW_VERSION != CALL_ANALYSIS:
        raise BranchObservationError(
            "installed lisjong implements a different call analysis version"
        )
    return speed_call_analysis


def _is_call(action) -> bool:
    from lisjong.policy_contract.action import (
        ChiAction,
        DaiminkanAction,
        PonAction,
    )

    return isinstance(action, (ChiAction, PonAction, DaiminkanAction))


def called_discard_order(decision, call) -> int:
    """鳴かれた打牌（放銃者の河の最後の牌）のorder。判断時点のpublic contextから読む。"""
    river = decision.input.players[int(call.target)].discards
    if not river or river[-1].tile != call.called_tile:
        raise CallResolutionError("the called tile is not the target's last discard")
    return river[-1].order


def call_analysis(decision, action) -> dict:
    """分岐点の鳴き解析（``{"call_analysis": ..., "call_analysis_error": ...}``）。"""
    try:
        value = call_analysis_api().analyze_speed_call_decision(decision, action)
        return {
            "call_analysis": canonical_engine_value(value),
            "call_analysis_error": None,
        }
    except Exception as error:  # SpeedCallAnalysisError・tile conservation等。INVALID
        return {
            "call_analysis": None,
            "call_analysis_error": f"{type(error).__name__}: {error}",
        }


def _projections(completion) -> dict[int, tuple]:
    return {
        int(seat_from_engine_seat(item.viewer_seat)): tuple(item.evidence)
        for item in completion.projections
    }


# ---------------------------------------------------------------------------
# R
# ---------------------------------------------------------------------------


class PonRObserver:
    """Rの全席の行動記録と、門前の最初のPon（分岐点の候補）・その成立 / 取消を記録する。"""

    def __init__(self) -> None:
        self.log = ActionLog()
        self.decisions: list[list] = []
        self.candidates: list[dict] = []
        self._pending: list[tuple[dict, object]] = []
        self._round_seats: set[int] = set()

    def observe(self, seat: int, decision, action) -> None:
        self.decisions.append(
            decision_entry(self.log.round_ordinal, seat, decision, action)
        )
        index = self.log.append(seat, action)
        if seat in self._round_seats:
            return
        if not analysis_api().is_pon_branch_candidate(decision, action):
            return
        self._round_seats.add(seat)
        record = {
            "decision_index": index,
            "decision_input": {
                "context": None,
                "context_sha256": None,
                "pon": encode_action(action),
                **call_analysis(decision, action),
            },
            "invalid": [],
            "post_sha256": None,
            "prefix_sha256": self.log.digest(0, index),
            "round": context_round(decision.input),
            "round_ordinal": self.log.round_ordinal,
            "seat": seat,
            "selection": {"called_discard_order": None, "resolution": None},
        }
        try:
            context = encode_context(decision)
            record["decision_input"]["context"] = context
            record["decision_input"]["context_sha256"] = canonical_digest(context)
        except Exception as error:  # codecの拒否。対局は続け、INVALIDになる
            record["invalid"].append(f"context: {type(error).__name__}: {error}")
        try:
            record["selection"]["called_discard_order"] = called_discard_order(
                decision, action
            )
        except CallResolutionError as error:
            record["invalid"].append(f"called discard: {error}")
        self.candidates.append(record)
        self._pending.append((record, action))

    def on_round_evidence_complete(self, completion) -> None:
        projections = _projections(completion)
        for record, action in self._pending:
            if record["round_ordinal"] != self.log.round_ordinal:
                record["invalid"].append("a candidate was carried into another round")
            order = record["selection"]["called_discard_order"]
            if order is None:
                record["selection"]["resolution"] = INVALID
                continue
            try:
                status, _ = resolve_call(action, order, projections[record["seat"]])
            except (CallResolutionError, KeyError) as error:
                record["selection"]["resolution"] = INVALID
                record["invalid"].append(f"resolution: {error}")
                continue
            record["selection"]["resolution"] = status
            if status not in (ESTABLISHED, CANCELLED_RON):
                record["invalid"].append(
                    f"a Champion Pon from a closed hand was {status}"
                )
        self._pending = []

    def on_delivery(self, items: tuple) -> None:
        for item in items:
            if not isinstance(item, RoundCompletionFact):
                continue
            identity = fact_round(item)
            for record in self.candidates:
                if record["round_ordinal"] == self.log.round_ordinal and (
                    record["round"] != identity
                ):
                    record["invalid"].append(
                        "the candidate context belongs to another round"
                    )
            if self._pending:
                for record, _ in self._pending:
                    record["selection"]["resolution"] = INVALID
                    record["invalid"].append("the round ended without its evidence")
                self._pending = []
            self.log.round_ordinal += 1
            self._round_seats = set()

    def finish(self, rounds: int) -> dict:
        if self._pending:
            for record, _ in self._pending:
                record["selection"]["resolution"] = INVALID
                record["invalid"].append("a candidate is pending at the end")
            self._pending = []
        if rounds != self.log.round_ordinal:
            raise BranchObservationError(
                "round completion facts differ from the played rounds"
            )
        for record in self.candidates:
            record["post_sha256"] = self.log.digest(
                record["decision_index"], self.log.round_stop(record["round_ordinal"])
            )
        return {
            "action_sequence_sha256": self.log.seat_digests(),
            "actions": self.decisions,
            "candidates": self.candidates,
            "decisions": len(self.log.entries),
            "rounds": self.log.round_ordinal,
        }


def r_hanchan(task, *, observed: bool, runner=run_policy_hanchan):
    """Champion×4席の1半荘。``(capture, analysis)`` を返す（``observed`` でも行動は同じ）。"""
    champion, factory = _champion(task)
    observer = PonRObserver() if observed else _Unobserved()
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
        on_round_evidence_complete=observer.on_round_evidence_complete
        if observed
        else None,
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
    return capture, observer.finish(len(match.history))


def rederive_call_analysis(decision_input: dict) -> dict:
    """保存したcontextとPonから鳴き解析を再計算する（verify用）。"""
    try:
        decision = decode_context(decision_input["context"])
        action = decode_action(decision_input["pon"])
    except Exception as error:  # codecの拒否
        return {
            "call_analysis": None,
            "call_analysis_error": f"{type(error).__name__}: {error}",
        }
    return call_analysis(decision, action)


# ---------------------------------------------------------------------------
# D arm
# ---------------------------------------------------------------------------


def task_branch(record: dict) -> dict:
    """D armの実行に渡す値（Rの結果・windowの解決結果は含めない）。"""
    return {
        "pon": record["decision_input"]["pon"],
        "round": record["round"],
        "round_ordinal": record["round_ordinal"],
        "seat": record["seat"],
    }


class PonDObserver:
    """D armの全席の行動・分岐点・分岐後の鳴きと、対象局のevidence・completion factを記録する。"""

    def __init__(self, branch_seat: int, target_ordinal: int) -> None:
        self.log = ActionLog()
        self.branch_seat = branch_seat
        self.target_ordinal = target_ordinal
        self.wrapper = None
        self.branch_index: int | None = None
        self.branch_context_sha256: str | None = None
        self.branch_discard_order: int | None = None
        self.calls: list[dict] = []
        self.pass_applied: bool | None = None
        self.invalid: list[str] = []
        self._calls: list[tuple[dict, int, object]] = []
        self._seat_decisions = 0
        self.clock = time.monotonic
        self.started: float | None = None
        self.pre_branch_seconds: float | None = None

    def observe(self, seat: int, decision, action) -> None:
        was_branched = self.branch_index is not None
        index = self.log.append(seat, action)
        if seat != self.branch_seat or self.wrapper is None:
            return
        if not was_branched:
            if self.wrapper.status.value != "branched":
                return
            self.branch_index = index
            if self.started is not None:
                self.pre_branch_seconds = self.clock() - self.started
            self.branch_context_sha256 = canonical_digest(encode_context(decision))
            pon = self.wrapper.branch_pon
            try:
                self.branch_discard_order = called_discard_order(decision, pon)
            except CallResolutionError as error:
                self.invalid.append(f"branch discard: {error}")
            return
        if self.log.round_ordinal != self.target_ordinal:
            return
        self._seat_decisions += 1
        if not _is_call(action):
            return
        record = {
            "decision_index": index,
            "kind": encode_action(action)["kind"],
            "resolution": None,
            "seat_decisions_after_branch": self._seat_decisions,
        }
        self.calls.append(record)
        try:
            order = called_discard_order(decision, action)
        except CallResolutionError as error:
            record["resolution"] = INVALID
            self.invalid.append(f"call after the branch: {error}")
            return
        self._calls.append((record, order, action))

    def on_round_evidence_complete(self, completion) -> None:
        if self.log.round_ordinal != self.target_ordinal:
            return
        evidence = _projections(completion).get(self.branch_seat)
        if evidence is None:
            self.invalid.append("the target round has no evidence of the branch seat")
            return
        seat = list(EngineSeat)[self.branch_seat]
        if self.branch_discard_order is not None:
            self.pass_applied = not any(
                isinstance(item, MeldCalledEvidence)
                and item.seat is seat
                and item.called_discard_order == self.branch_discard_order
                for item in evidence
            )
        for record, order, action in self._calls:
            try:
                status, _ = resolve_call(action, order, evidence)
            except CallResolutionError as error:
                record["resolution"] = INVALID
                self.invalid.append(f"call after the branch: {error}")
                continue
            record["resolution"] = status
        self._calls = []

    def on_delivery(self, items: tuple) -> None:
        from lisjong_engine.round_completion import MatchCompletionFact

        rounds = [item for item in items if isinstance(item, RoundCompletionFact)]
        matches = [item for item in items if isinstance(item, MatchCompletionFact)]
        for fact in rounds:
            if self.log.round_ordinal == self.target_ordinal:
                raise BranchArmSettled(fact, matches[0] if matches else None)
            self.log.round_ordinal += 1


def d_arm(task: BranchTask, *, runner=run_policy_hanchan) -> dict:
    """1 D armを実行し、照合前の観測値を返す（INVALIDの判定は親processが行う）。"""
    from lisjong.policies.riichi_dama_branch import RoundIdentity
    from lisjong.policy_contract import PolicyActionValidationError
    from lisjong.policy_contract.wind import Wind

    api = analysis_api()
    champion, factory = _champion(task)
    branch = task.branch
    seat = branch["seat"]
    target = RoundIdentity(
        Wind(branch["round"]["prevailing_wind"]),
        branch["round"]["hand_number"],
        branch["round"]["honba"],
    )
    wrapper = api.PonFirstPassBranchPolicy(
        factory(**champion.behavior_configuration), target, decode_action(branch["pon"])
    )
    observer = PonDObserver(seat, branch["round_ordinal"])
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
    observer.started = observer.clock()
    try:
        runner(
            policies,
            seed=task.seed,
            rules=locked_rules(),
            on_delivery=observer.on_delivery,
            on_round_evidence_complete=observer.on_round_evidence_complete,
        )
    except BranchArmSettled as stop:
        settled = stop
    except (api.PonFirstPassBranchError, PolicyActionValidationError) as error:
        invalid.append(f"D arm: {type(error).__name__}: {error}")
    else:
        invalid.append("the hanchan finished without settling the target round")
    arm_seconds = observer.clock() - observer.started
    index = observer.branch_index
    stop = len(observer.log.entries)
    return {
        MEASUREMENT_KEY: {
            "arm_seconds": arm_seconds,
            "clock": MEASUREMENT_CLOCK,
            "pre_branch_seconds": observer.pre_branch_seconds,
        },
        "branch_action": None if index is None else observer.log.entries[index][2],
        "branch_context_sha256": observer.branch_context_sha256,
        "branch_decision_index": index,
        "branch_ordinal": task.branch_ordinal,
        "calls_after_branch": observer.calls,
        "decisions": stop,
        "invalid": invalid + observer.invalid,
        "pass_applied": observer.pass_applied,
        "post_sha256": None if index is None else observer.log.digest(index, stop),
        "prefix_sha256": None if index is None else observer.log.digest(0, index),
        "round": None if settled is None else fact_round(settled.round_fact),
        "round_ordinal": observer.log.round_ordinal,
        "seat": seat,
        "seed": task.seed,
        "settlement": None
        if settled is None
        else settlement_of_fact(settled.round_fact, settled.match_fact),
        "status": wrapper.status.value,
        "wrapper_calls_after_branch": wrapper.calls_after_branch_count,
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
    "CALL_ANALYSIS",
    "DECISION_INPUT_FIELDS",
    "MEASUREMENT_CLOCK",
    "MEASUREMENT_KEY",
    "PROTOCOL_VERSION",
    "SELECTION_FIELDS",
    "WRAPPER_VERSION",
    "PonDObserver",
    "PonRObserver",
    "analysis_api",
    "call_analysis",
    "call_analysis_api",
    "called_discard_order",
    "d_arm",
    "legal_digest",
    "play_d_arm",
    "play_r_observed",
    "play_r_unobserved",
    "r_hanchan",
    "rederive_call_analysis",
    "split_measurement",
    "task_branch",
]
