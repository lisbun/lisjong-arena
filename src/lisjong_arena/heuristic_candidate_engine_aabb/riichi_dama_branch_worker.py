"""#540 riichi-dama-branch-v1（#539）のworker: R（Champion×4）とD arm（分岐席だけwrapper）。

```text
R      Champion×4で半荘を最後まで実行する（engine on_deliveryで局の終わりを知る）
         各判断: 全席の行動を順に記録（seat, 行動）。decision index = 半荘内の通し番号
         RiichiActionが合法:
           Championが選んだ   -> 分岐点（その席・その局で最初の1回）
                                 public context（PolicyInput v3 lossless・合法手の元の順序）、
                                 分岐直前までの全席の行動列digestを記録
                                 直後の判断（同じ席の宣言牌選択）のDiscardActionを宣言牌として記録
           選ばなかった       -> 席ごとの件数だけ数える
         半荘の後: 保存したcontextと宣言牌からlisjongの特徴を計算する（対局中には呼ばない）
D arm  同じseedで半荘の先頭から再実行し、分岐席だけlisjongの``RiichiDamaBranchPolicy``へ
       替える（他の3席はChampion）。対象局の精算のcompletion factをon_deliveryで受けたら
       ``BranchArmSettled``（Arena専用）を送出して打ち切る
         分岐直前までの行動列digest、分岐点のcontext digest、wrapperの状態と差し替え回数、
         対象局のcompletion fact（局の結果・point deltas・精算後の点数）を記録する
```

- workerは観測した値をそのまま返し、RとDの照合（digest・index・局identity・精算の整合）と
  INVALIDの判定は親processの``riichi_dama_branch``が行う（verifyでも同じ関数で再導出する）
- Arenaは点数・待ち・役を再計算しない。lisjongの解析値はJSON projectionをそのまま運ぶ
- 解析・wrapperの入力はその判断のpublic ``DecisionContext``と自分の宣言牌だけ。後続event・
  他席の情報・privileged observer data（``on_transaction_observation``）を渡さない
- engine呼び出し（``run_policy_hanchan``）はtestで差し替えられる（``runner``）
"""

import os
from collections import Counter
from dataclasses import dataclass

from lisjong_engine.round_completion import MatchCompletionFact, RoundCompletionFact
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.completed_match_facts import seat_index
from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

from .call_value_shadow_worker import (
    action_sequence_digest,
    canonical_digest,
    decode_action,
    decode_context,
    encode_action,
    encode_context,
)
from .executor import (
    PlayedHanchan,
    _CountingPolicy,
    current_peak_rss_kib,
    environment_document,
)
from .kyoku_record import ROLE_INCUMBENT, SeatAssignment
from .protocol import locked_rules, parse_participant, resolve_factory
from .record_store import HanchanCapture

PROTOCOL_VERSION = "riichi-dama-branch-v1"
"""#539の意味定義のversion。"""
FEATURES_VERSION = "riichi-dama-features-v2"
"""lisjong ``RIICHI_DAMA_FEATURES_VERSION`` と一致を要求する。

v2（lisjong #354）: 通常牌の上限を超える待ちは赤牌で評価し、和了牌（``winning_tile``）と
赤の加点（``red_dora``）を返す。赤の加点でS1の判定が変わりうるため、v1の記録とは別versionとする。
"""


class BranchArmSettled(Exception):  # noqa: N818 - 打ち切りの合図で、失敗ではない
    """D armの対象局の精算後に``on_delivery``から送出するArena専用の打ち切り。"""

    def __init__(
        self, round_fact: RoundCompletionFact, match_fact: MatchCompletionFact | None
    ) -> None:
        super().__init__("the target round of the D arm has been settled")
        self.round_fact = round_fact
        self.match_fact = match_fact


class BranchObservationError(ValueError):
    """workerの観測が半荘の進行と整合しない（R・D arm共通）。"""


def analysis_api():
    """lisjongの特徴API・wrapper（lisjong #352、features v2はlisjong #354以降）。"""
    from lisjong.policies import riichi_dama_branch

    if riichi_dama_branch.RIICHI_DAMA_FEATURES_VERSION != FEATURES_VERSION:
        raise BranchObservationError(
            "installed lisjong implements a different riichi-dama features version"
        )
    return riichi_dama_branch


def _is_riichi(action) -> bool:
    from lisjong.policy_contract.action import RiichiAction

    return isinstance(action, RiichiAction)


def _is_discard(action) -> bool:
    from lisjong.policy_contract.action import DiscardAction

    return isinstance(action, DiscardAction)


def context_round(policy_input) -> dict:
    """判断contextの局identity（場風・局・本場・親）。"""
    state = policy_input.round
    return {
        "dealer_seat": int(state.dealer_seat),
        "hand_number": state.hand_number,
        "honba": state.honba,
        "prevailing_wind": state.round_wind.value,
    }


def fact_round(fact: RoundCompletionFact) -> dict:
    """engineの局completion factの局identity（``context_round`` と同じ形）。"""
    return {
        "dealer_seat": seat_index(fact.dealer_seat),
        "hand_number": fact.hand_number,
        "honba": fact.honba,
        "prevailing_wind": fact.prevailing_wind.value,
    }


def settlement_of_fact(
    fact: RoundCompletionFact, match_fact: MatchCompletionFact | None
) -> dict:
    """局completion factの客観値（D armの精算。Rの局記録と同じ意味の値だけ）。"""
    deltas = [0, 0, 0, 0]
    for item in fact.point_deltas:
        deltas[seat_index(item.seat)] = item.delta
    after = [0, 0, 0, 0]
    for item in fact.scores_after:
        after[seat_index(item.seat)] = item.points
    final = None
    if match_fact is not None:
        final = [0, 0, 0, 0]
        for item in match_fact.final_scores:
            final[seat_index(item.seat)] = item.points
    return {
        "final_raw_scores": final,
        "has_next_round": fact.has_next_round,
        "outcome": fact.outcome.value,
        "point_deltas": deltas,
        "points_after": after,
        "source_seat": None
        if fact.source_seat is None
        else seat_index(fact.source_seat),
        "winner_seats": [seat_index(winner.seat) for winner in fact.winners],
    }


class ActionLog:
    """半荘内の全席の行動を判断順に記録する（局の境界はcompletion factで数える）。"""

    def __init__(self) -> None:
        self.entries: list[tuple[int, int, dict]] = []
        self.actions: list[list] = [[], [], [], []]
        self.round_ordinal = 0

    def append(self, seat: int, action) -> int:
        index = len(self.entries)
        self.entries.append((self.round_ordinal, seat, encode_action(action)))
        self.actions[seat].append(action)
        return index

    def digest(self, start: int, stop: int) -> str:
        """``[start, stop)`` の (seat, 行動) 列のdigest。"""
        return canonical_digest(
            [[seat, value] for _, seat, value in self.entries[start:stop]]
        )

    def round_stop(self, round_ordinal: int) -> int:
        """その局の最後の判断の次のindex。"""
        stop = 0
        for index, (ordinal, _, _) in enumerate(self.entries):
            if ordinal == round_ordinal:
                stop = index + 1
        return stop

    def seat_digests(self) -> list[str]:
        return [action_sequence_digest(actions) for actions in self.actions]


class RObserver:
    """Rの分岐点・宣言牌・contextと、リーチが合法なのに選ばなかった判断を記録する。"""

    def __init__(self) -> None:
        self.log = ActionLog()
        self.branches: list[dict] = []
        self.riichi_declined = [0, 0, 0, 0]
        self._awaiting: dict[int, dict] = {}
        self._round_seats: set[int] = set()

    def observe(self, seat: int, decision, action) -> None:
        index = self.log.append(seat, action)
        pending = self._awaiting.pop(seat, None)
        if pending is not None:
            legal_riichi = any(_is_riichi(item) for item in decision.legal_actions)
            if (
                index == pending["decision_index"] + 1
                and _is_discard(action)
                and not legal_riichi
            ):
                pending["declaration"] = encode_action(action)
                pending["declaration_decision_index"] = index
            else:
                pending["invalid"].append(
                    "the decision after the riichi is not its declaration discard"
                )
        if not any(_is_riichi(item) for item in decision.legal_actions):
            return
        if not _is_riichi(action):
            self.riichi_declined[seat] += 1
            return
        record = {
            "context": None,
            "context_sha256": None,
            "decision_index": index,
            "declaration": None,
            "declaration_decision_index": None,
            "invalid": [],
            "post_sha256": None,
            "prefix_sha256": self.log.digest(0, index),
            "riichi": encode_action(action),
            "round": context_round(decision.input),
            "round_ordinal": self.log.round_ordinal,
            "seat": seat,
        }
        if seat in self._round_seats:
            record["invalid"].append("a second riichi of the seat in one round")
        self._round_seats.add(seat)
        try:
            record["context"] = encode_context(decision)
            record["context_sha256"] = canonical_digest(record["context"])
        except Exception as error:  # codecの拒否。対局は続け、INVALIDになる
            record["invalid"].append(f"context: {type(error).__name__}: {error}")
        self.branches.append(record)
        self._awaiting[seat] = record

    def on_delivery(self, items: tuple) -> None:
        for item in items:
            if isinstance(item, RoundCompletionFact):
                self._complete_round(item)

    def _complete_round(self, fact: RoundCompletionFact) -> None:
        for record in self._awaiting.values():
            record["invalid"].append("the round ended before the declaration discard")
        self._awaiting = {}
        identity = fact_round(fact)
        for record in self.branches:
            if record["round_ordinal"] == self.log.round_ordinal and (
                record["round"] != identity
            ):
                record["invalid"].append("the branch context belongs to another round")
        self.log.round_ordinal += 1
        self._round_seats = set()

    def finish(self, rounds: int) -> dict:
        if self._awaiting:
            for record in self._awaiting.values():
                record["invalid"].append("the hanchan ended before the declaration")
            self._awaiting = {}
        if rounds != self.log.round_ordinal:
            raise BranchObservationError(
                "round completion facts differ from the played rounds"
            )
        for record in self.branches:
            record["post_sha256"] = self.log.digest(
                record["decision_index"], self.log.round_stop(record["round_ordinal"])
            )
        return {
            "action_sequence_sha256": self.log.seat_digests(),
            "branches": self.branches,
            "decisions": len(self.log.entries),
            "riichi_declined": self.riichi_declined,
            "rounds": self.log.round_ordinal,
        }


def branch_features(record: dict) -> dict:
    """保存したcontextと宣言牌からlisjongの特徴を計算する（対局の後に呼ぶ）。

    戻り値は ``{"error": str | None, "features": dict | None}``。lisjongの拒否・
    codecの失敗は ``error`` へ入れる（INVALID）。
    """
    if record["context"] is None or record["declaration"] is None:
        return {"error": "the branch has no context or declaration", "features": None}
    try:
        api = analysis_api()
        decision = decode_context(record["context"])
        declaration = decode_action(record["declaration"])
        features = api.analyze_riichi_decision(decision, declaration.tile)
        return {
            "error": None,
            "features": api.riichi_decision_features_to_value(features),
        }
    except Exception as error:  # RiichiDamaAnalysisError・tile conservation・codec
        return {"error": f"{type(error).__name__}: {error}", "features": None}


class _Observed:
    """Policyの判断を変えずに返し、判断後に観測へ渡す。"""

    __slots__ = ("_policy", "_seat", "_observer")

    def __init__(self, policy, seat: int, observer) -> None:
        self._policy = policy
        self._seat = seat
        self._observer = observer

    def choose_action(self, decision):
        from lisjong.policy_contract import execute_policy

        action = execute_policy(self._policy, decision)
        self._observer.observe(self._seat, decision, action)
        return action


class _Unobserved:
    """観測なしのR（rehearsal）: 行動の列だけを記録する。"""

    def __init__(self) -> None:
        self.log = ActionLog()

    def observe(self, seat: int, decision, action) -> None:
        self.log.append(seat, action)


def _champion(task):
    champion = parse_participant(task.incumbent, "incumbent")
    return champion, resolve_factory(champion)


def r_hanchan(task, *, observed: bool, runner=run_policy_hanchan):
    """Champion×4席の1半荘。``(capture, analysis)`` を返す（``observed`` でも行動は同じ）。"""
    champion, factory = _champion(task)
    observer = RObserver() if observed else _Unobserved()
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


# ---------------------------------------------------------------------------
# D arm
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BranchTask:
    """workerへ渡す1 D armの入力（picklableなdocumentだけを持つ）。

    ``branch`` はRの分岐記録のうちD armの実行に使う値（席・局・decision index・
    局identity・宣言牌）だけで、Rの結果（精算・後続の行動）は含めない。
    """

    seed: int
    branch_ordinal: int
    branch: dict
    incumbent: dict
    wheel_path: str


@dataclass(frozen=True, slots=True)
class ArmCapture:
    """D armの識別（``run_tasks`` の照合に使う）。客観値は ``policy_analysis`` に入る。"""

    seed: int
    branch_ordinal: int


class DObserver:
    """D armの全席の行動・分岐点・対象局のcompletion factを記録する。"""

    def __init__(self, branch_seat: int, target_ordinal: int) -> None:
        self.log = ActionLog()
        self.branch_seat = branch_seat
        self.target_ordinal = target_ordinal
        self.wrapper = None
        self.branch_index: int | None = None
        self.branch_context_sha256: str | None = None

    def observe(self, seat: int, decision, action) -> None:
        was_branched = self.branch_index is not None
        index = self.log.append(seat, action)
        if seat != self.branch_seat or was_branched or self.wrapper is None:
            return
        if self.wrapper.status.value == "branched":
            self.branch_index = index
            self.branch_context_sha256 = canonical_digest(encode_context(decision))

    def on_delivery(self, items: tuple) -> None:
        rounds = [item for item in items if isinstance(item, RoundCompletionFact)]
        matches = [item for item in items if isinstance(item, MatchCompletionFact)]
        for fact in rounds:
            if self.log.round_ordinal == self.target_ordinal:
                raise BranchArmSettled(fact, matches[0] if matches else None)
            self.log.round_ordinal += 1

    def riichi_after_branch(self) -> int:
        """対象局で分岐席が選んだ（適用された）RiichiActionの数。"""
        return sum(
            1
            for ordinal, seat, value in self.log.entries
            if ordinal == self.target_ordinal
            and seat == self.branch_seat
            and value.get("kind") == "riichi"
        )


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
    wrapper = api.RiichiDamaBranchPolicy(
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
    return {
        "branch_context_sha256": observer.branch_context_sha256,
        "branch_decision_index": index,
        "branch_ordinal": task.branch_ordinal,
        "decisions": stop,
        "invalid": invalid,
        "post_sha256": None if index is None else observer.log.digest(index, stop),
        "prefix_sha256": None if index is None else observer.log.digest(0, index),
        "replacements": wrapper.replacement_count,
        "riichi_after_branch": observer.riichi_after_branch(),
        "round": None if settled is None else fact_round(settled.round_fact),
        "round_ordinal": observer.log.round_ordinal,
        "seat": seat,
        "seed": task.seed,
        "settlement": None
        if settled is None
        else settlement_of_fact(settled.round_fact, settled.match_fact),
        "status": wrapper.status.value,
    }


# ---------------------------------------------------------------------------
# workerの入口（spawn workerへ渡すmodule-level関数）
# ---------------------------------------------------------------------------


def _played(capture, analysis, task) -> PlayedHanchan:
    return PlayedHanchan(
        capture,
        environment_document(task.wheel_path),
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
    "ActionLog",
    "ArmCapture",
    "BranchArmSettled",
    "BranchObservationError",
    "BranchTask",
    "DObserver",
    "RObserver",
    "analysis_api",
    "branch_features",
    "context_round",
    "d_arm",
    "fact_round",
    "play_d_arm",
    "play_r_observed",
    "play_r_unobserved",
    "r_hanchan",
    "settlement_of_fact",
]
