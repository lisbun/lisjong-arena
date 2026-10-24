"""#534 call-value-shadow-v1（#533）の観測: worker内でcall意図→成立/取消→直後打牌を追う。

```text
4席すべてChampion（同じrevisionのPlacementAwareSpeedCallPolicy、設定{}）。各判断で
  Champion: execute_policyで1回だけ判断し、その行動だけを対局へ適用する
  Chi/Ponを選んだ判断（call意図）:
    lisjong analyze_speed_call_decision(public context, 選んだcall)
    -> 意図をpendingへ置き、同じ席の次の判断（context・行動）を後で結び付ける
  局の終了（engine on_round_evidence_complete、actor席のplayer-safe evidence）:
    鳴かれた打牌のorder -> response epochの公開outcome
      CALL かつ MeldCalled.seat == actor・meldが一致 -> ESTABLISHED
      CALL で別席のmeld                             -> CANCELLED priority-call
      RON                                          -> CANCELLED ron
      NO_PUBLIC_RESPONSE の直後に局終了              -> CANCELLED round-end
      それ以外                                       -> INVALID（理由不明の取消）
    ESTABLISHED かつ SPEED_CALL:
      同じ局・同じ席の次の判断（直後のmandatory discard）へ一対一に結び、
      lisjong analyze_post_call_discard(その実際のcontext, call解析, 実打牌)
  半荘の終了時にpendingが残れば INVALID
```

- Arenaはroute・向聴・打点proxy・受入・completion mass・代替・分類を再計算しない。
  lisjongの解析値をそのまま正規化して運ぶ（``canonical_engine_value``）
- 公開eventで成立/取消を決めるのはArena観測側だけ。後続eventや他席の情報を
  Policy・解析の入力へ渡さない（解析の入力はその判断のpublic ``DecisionContext`` だけ）
- 照合・解析に失敗した意図はINVALIDとして記録し、対局は続ける（Champion行動は変えない）。
  1件でもあれば判定はSTOP-INVALID
- public contextは``durable_local_game_record``のPolicyInput v3（lossless）と合法手の
  元の順序で保存し、読み戻した値が元の値と等しいことを記録時に要求する（verifyの再導出用）

観測はPolicy側の保存物で、客観record（hanchans）へ混ぜない。
"""

import hashlib
import json
import os
from collections import Counter

from lisjong_engine.round_evidence import (
    DiscardEvidence,
    DoraIndicatorRevealedEvidence,
    MeldCalledEvidence,
    ResponseEpochClosedEvidence,
    ResponseEpochOpenedEvidence,
    ResponseOutcome,
    ResponseTrigger,
    RiichiDeclaredEvidence,
    RiichiEstablishedEvidence,
    RiichiFailedEvidence,
    RoundEndedEvidence,
)
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.domain_conversion import (
    public_meld_from_engine_meld,
    seat_from_engine_seat,
    tile_from_public_tile,
    wind_from_engine_wind,
)
from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

from .backend_check import canonical_engine_value
from .executor import (
    HanchanTask,
    PlayedHanchan,
    _CountingPolicy,
    current_peak_rss_kib,
    environment_document,
)
from .kyoku_record import ROLE_INCUMBENT, SeatAssignment
from .protocol import locked_rules, parse_participant, resolve_factory
from .record_store import HanchanCapture

SHADOW_VERSION = "call-value-shadow-v1"
"""#533の意味定義のversion（lisjong ``CALL_VALUE_SHADOW_VERSION`` と一致を要求する）。"""

ESTABLISHED = "ESTABLISHED"
CANCELLED_RON = "CANCELLED-RON"
CANCELLED_PRIORITY_CALL = "CANCELLED-PRIORITY-CALL"
CANCELLED_ROUND_END = "CANCELLED-ROUND-END"
CANCELLATIONS = (CANCELLED_PRIORITY_CALL, CANCELLED_ROUND_END, CANCELLED_RON)
INVALID = "INVALID"
STATUSES = (ESTABLISHED, *CANCELLATIONS, INVALID)

SPEED_CALL = "CallDecisionRoute.SPEED_CALL"
ROUTES = (
    "CallDecisionRoute.PARENT_DECISION",
    "CallDecisionRoute.PARENT_PASS",
    SPEED_CALL,
)

_DEFERRED = (
    DoraIndicatorRevealedEvidence,
    RiichiEstablishedEvidence,
    RiichiFailedEvidence,
)
"""epochの公開解決の後に保留から出るevidence（解決の内容ではない）。"""


class CallResolutionError(ValueError):
    """公開evidenceでcall意図の成立/取消を説明できない（INVALID）。"""


def _analysis_api():
    # lisjong 7fc1130より前の環境でもこのmoduleをimportできるよう、使う時だけ解決する
    from lisjong.policies import speed_call_analysis

    if speed_call_analysis.CALL_VALUE_SHADOW_VERSION != SHADOW_VERSION:
        raise CallResolutionError(
            "installed lisjong implements a different call-value-shadow version"
        )
    return speed_call_analysis


def _codec():
    from lisjong_arena import durable_local_game_record as codec

    return codec


def _is_call(action) -> bool:
    from lisjong.policy_contract.action import ChiAction, PonAction

    return isinstance(action, (ChiAction, PonAction))


def encode_action(action) -> dict:
    return _codec()._action_to_value(action, "action")


def decode_action(value: object):
    return _codec()._parse_action(value, "action")


def encode_context(decision) -> dict:
    """public ``DecisionContext`` をlosslessに保存する（読み戻しが等しいことを要求）。"""
    codec = _codec()
    document = {
        "legal_actions": [
            codec._action_to_value(action, f"legal_actions[{index}]")
            for index, action in enumerate(decision.legal_actions)
        ],
        "policy_input": codec._policy_input_to_value_v3(decision.input),
    }
    if decode_context(document) != decision:
        raise CallResolutionError("the stored decision context does not round-trip")
    return document


def decode_context(document: object):
    """保存したcontextを読む（合法手は保存した元の順序のまま）。"""
    from lisjong.policy_contract import DecisionContext

    codec = _codec()
    if type(document) is not dict or set(document) != {"legal_actions", "policy_input"}:
        raise CallResolutionError("decision context fields do not match")
    actions = document["legal_actions"]
    if type(actions) is not list or not actions:
        raise CallResolutionError("decision context has no legal actions")
    return DecisionContext(
        input=codec._parse_policy_input(
            document["policy_input"],
            "policy_input",
            schema_version=codec.LOCAL_GAME_RECORD_SCHEMA_VERSION_WITH_RIICHI_DECLARATION,
        ),
        legal_actions=tuple(
            codec._parse_action(value, f"legal_actions[{index}]")
            for index, value in enumerate(actions)
        ),
    )


def canonical_digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def action_sequence_digest(actions: list) -> str:
    """1席の本番Action列（正規化した値の順序付き列）のdigest。"""
    return canonical_digest([canonical_engine_value(action) for action in actions])


# ---------------------------------------------------------------------------
# 公開evidenceによる成立/取消
# ---------------------------------------------------------------------------


def _expected_meld(call):
    from lisjong.policy_contract.action import ChiAction
    from lisjong.policy_contract.meld import MeldKind

    kind = MeldKind.CHI if isinstance(call, ChiAction) else MeldKind.PON
    return (
        kind,
        call.target,
        call.called_tile,
        Counter((*call.consumed_tiles, call.called_tile)),
    )


def meld_matches_call(meld, call) -> bool:
    kind, target, called, tiles = _expected_meld(call)
    return (
        meld.kind is kind
        and meld.from_seat == target
        and meld.called_tile == called
        and Counter(meld.tiles) == tiles
    )


def resolve_call(call, discard_order: int, evidence) -> tuple[str, object]:
    """actor席のplayer-safe evidenceから、call意図の公開された結果を読む。

    戻り値は ``(status, meld)``。``meld`` は成立したときのlisjong ``PublicMeld``。
    公開evidenceで説明できない場合は ``CallResolutionError``。
    """
    evidence = tuple(evidence)
    target = call.target
    positions = [
        index
        for index, item in enumerate(evidence)
        if isinstance(item, DiscardEvidence) and item.order == discard_order
    ]
    if len(positions) != 1:
        raise CallResolutionError("the called discard is not in the actor's evidence")
    index = positions[0]
    discard = evidence[index]
    if (
        seat_from_engine_seat(discard.seat) != target
        or tile_from_public_tile(discard.tile) != call.called_tile
    ):
        raise CallResolutionError("the called discard differs from the call")
    index += 1
    if index < len(evidence) and isinstance(evidence[index], RiichiDeclaredEvidence):
        index += 1
    opened = evidence[index] if index < len(evidence) else None
    if not (
        isinstance(opened, ResponseEpochOpenedEvidence)
        and opened.trigger is ResponseTrigger.DISCARD
        and opened.source_seat == discard.seat
    ):
        raise CallResolutionError("no response epoch follows the called discard")
    index += 1
    closed = evidence[index] if index < len(evidence) else None
    if not (
        isinstance(closed, ResponseEpochClosedEvidence)
        and closed.trigger is ResponseTrigger.DISCARD
        and closed.source_seat == discard.seat
    ):
        raise CallResolutionError("the response epoch of the call is not closed")
    index += 1
    while index < len(evidence) and isinstance(evidence[index], _DEFERRED):
        index += 1
    following = evidence[index] if index < len(evidence) else None
    if closed.outcome is ResponseOutcome.RON:
        return CANCELLED_RON, None
    if closed.outcome is ResponseOutcome.NO_PUBLIC_RESPONSE:
        if isinstance(following, RoundEndedEvidence):
            return CANCELLED_ROUND_END, None
        raise CallResolutionError("the call vanished without a public reason")
    if closed.outcome is not ResponseOutcome.CALL or not isinstance(
        following, MeldCalledEvidence
    ):
        raise CallResolutionError("a call outcome without a called meld")
    if following.called_discard_order != discard_order:
        raise CallResolutionError("the called meld claims a different discard")
    meld = public_meld_from_engine_meld(following.meld)
    if seat_from_engine_seat(following.seat) != call.actor:
        return CANCELLED_PRIORITY_CALL, None
    if not meld_matches_call(meld, call):
        raise CallResolutionError("the established meld differs from the call")
    return ESTABLISHED, meld


def round_identity(completion) -> dict:
    return {
        "dealer_seat": int(seat_from_engine_seat(completion.dealer_seat)),
        "hand_number": completion.hand_number,
        "honba": completion.honba,
        "round_wind": wind_from_engine_wind(completion.prevailing_wind).value,
    }


def context_round_identity(policy_input) -> dict:
    state = policy_input.round
    return {
        "dealer_seat": int(state.dealer_seat),
        "hand_number": state.hand_number,
        "honba": state.honba,
        "round_wind": state.round_wind.value,
    }


# ---------------------------------------------------------------------------
# 半荘ごとの観測
# ---------------------------------------------------------------------------


class _Intent:
    __slots__ = ("record", "call", "decision", "analysis", "next")

    def __init__(self, record, call, decision, analysis) -> None:
        self.record = record
        self.call = call
        self.decision = decision
        self.analysis = analysis
        self.next = None


class CallValueObserver:
    """1半荘分のcall意図の追跡（Arena観測側だけのcross-decision状態）。"""

    def __init__(self) -> None:
        self.round_ordinal = 0
        self.calls: list[dict] = []
        self.pending: list[_Intent] = []
        self.decisions = [0, 0, 0, 0]
        self.opportunities = [0, 0, 0, 0]
        self._round_opportunities = [0, 0, 0, 0]
        self.actions: list[list] = [[], [], [], []]
        self.post_decisions: set[tuple[int, int]] = set()

    # -- 判断 ---------------------------------------------------------------

    def observe(self, seat: int, decision, action) -> None:
        """Championが選んだ後に呼ぶ（行動は変えない）。"""
        ordinal = self.decisions[seat]
        self.decisions[seat] += 1
        self.actions[seat].append(action)
        for intent in self.pending:
            if intent.record["actor"] == seat and intent.next is None:
                intent.next = (self.round_ordinal, ordinal, decision, action)
        if not any(_is_call(item) for item in decision.legal_actions):
            return
        response_ordinal = self._round_opportunities[seat]
        self._round_opportunities[seat] += 1
        self.opportunities[seat] += 1
        if not _is_call(action):
            return
        record = {
            "actor": seat,
            "call": encode_action(action),
            "call_analysis": None,
            "decision_ordinal": ordinal,
            "invalid": [],
            "is_primary_target": None,
            "post": None,
            "pre_context": None,
            "resolution": None,
            "response_ordinal": response_ordinal,
            "round_ordinal": self.round_ordinal,
        }
        analysis = None
        try:
            record["pre_context"] = encode_context(decision)
            analysis = _analysis_api().analyze_speed_call_decision(decision, action)
            record["call_analysis"] = canonical_engine_value(analysis)
            record["is_primary_target"] = analysis.is_primary_target
        except Exception as error:
            # SpeedCallAnalysisError・codecの拒否・tile conservation等。対局は続け、
            # 判定はSTOP-INVALIDになる（Champion行動は変えない）
            record["invalid"].append(f"call analysis: {type(error).__name__}: {error}")
        self.calls.append(record)
        self.pending.append(_Intent(record, action, decision, analysis))

    # -- 局の終了 -----------------------------------------------------------

    def on_round_complete(self, completion) -> None:
        identity = round_identity(completion)
        projections = {
            int(seat_from_engine_seat(item.viewer_seat)): item.evidence
            for item in completion.projections
        }
        for intent in self.pending:
            self._resolve(intent, identity, projections)
        self.pending = []
        self.round_ordinal += 1
        self._round_opportunities = [0, 0, 0, 0]

    def _resolve(self, intent: _Intent, identity: dict, projections: dict) -> None:
        record = intent.record
        actor = record["actor"]
        invalid = record["invalid"]
        if record["round_ordinal"] != self.round_ordinal:
            invalid.append("a call intent was carried into another round")
        if context_round_identity(intent.decision.input) != identity:
            invalid.append("the call context belongs to another round")
        river = intent.decision.input.players[int(intent.call.target)].discards
        try:
            if not river or river[-1].tile != intent.call.called_tile:
                raise CallResolutionError(
                    "the called tile is not the target's last discard"
                )
            status, meld = resolve_call(
                intent.call, river[-1].order, projections[actor]
            )
        except (CallResolutionError, KeyError) as error:
            record["resolution"] = {"detail": str(error), "status": INVALID}
            invalid.append(f"resolution: {error}")
            return
        record["resolution"] = {"detail": None, "status": status}
        if status != ESTABLISHED:
            return
        following = intent.next
        if following is None:
            invalid.append("an established call has no following decision")
            return
        round_ordinal, ordinal, decision, action = following
        if round_ordinal != record["round_ordinal"]:
            invalid.append("the post-call decision is in another round")
            return
        key = (actor, ordinal)
        if key in self.post_decisions:
            invalid.append("a post-call decision is matched twice")
            return
        self.post_decisions.add(key)
        melds = decision.input.players[actor].melds
        if not melds or melds[-1] != meld:
            invalid.append("the post-call context does not hold the established meld")
            return
        if (
            intent.analysis is None
            or record["call_analysis"].get("route") != SPEED_CALL
        ):
            return
        post = {
            "analysis": None,
            "context": None,
            "decision_ordinal": ordinal,
            "discard": encode_action(action),
        }
        record["post"] = post
        try:
            post["context"] = encode_context(decision)
            analysis = _analysis_api().analyze_post_call_discard(
                decision, intent.analysis, action
            )
            post["analysis"] = canonical_engine_value(analysis)
        except Exception as error:  # SpeedCallAnalysisError・codec・tile conservation
            invalid.append(f"post-call analysis: {type(error).__name__}: {error}")

    # -- 半荘の終了 ---------------------------------------------------------

    def finish(self, rounds: int) -> dict:
        for intent in self.pending:
            intent.record["resolution"] = {
                "detail": "pending at the end of the hanchan",
                "status": INVALID,
            }
            intent.record["invalid"].append("a call intent is pending at the end")
        self.pending = []
        if rounds != self.round_ordinal:
            raise CallResolutionError(
                "round evidence completions differ from the played rounds"
            )
        return {
            "action_sequence_sha256": [
                action_sequence_digest(actions) for actions in self.actions
            ],
            "calls": self.calls,
            "rounds": self.round_ordinal,
            "seats": [
                {
                    "call_opportunities": self.opportunities[seat],
                    "decisions": self.decisions[seat],
                    "seat": seat,
                }
                for seat in range(4)
            ],
        }


class _ObservedChampion:
    """Championの判断だけを返し、判断後に観測へ渡す（行動は変えない）。"""

    __slots__ = ("_policy", "_seat", "_observer")

    def __init__(self, policy, seat: int, observer) -> None:
        self._policy = policy
        self._seat = seat
        self._observer = observer

    def choose_action(self, decision):
        from lisjong.policy_contract import execute_policy

        action = execute_policy(self._policy, decision)
        if self._observer is not None:
            self._observer.observe(self._seat, decision, action)
        return action


class _ActionRecorder:
    """観測なしのrehearsal: 行動の列だけを記録する。"""

    def __init__(self) -> None:
        self.actions: list[list] = [[], [], [], []]

    def observe(self, seat: int, decision, action) -> None:
        self.actions[seat].append(action)


def call_value_task(
    task: HanchanTask, *, observed: bool
) -> tuple[HanchanCapture, dict]:
    """Champion×4席の1半荘。``observed`` ならcall意図を追跡する（行動は同じ）。"""
    champion = parse_participant(task.incumbent, "incumbent")
    factory = resolve_factory(champion)
    observer = CallValueObserver() if observed else _ActionRecorder()
    policy_calls: Counter = Counter()
    selector_calls: Counter = Counter()
    policies = {
        seat: _CountingPolicy(
            _ObservedChampion(
                factory(**champion.behavior_configuration), index, observer
            ),
            policy_calls,
            seat,
        )
        for index, seat in enumerate(EngineSeat)
    }

    def count_selector(seat: EngineSeat) -> None:
        selector_calls[seat] += 1

    match = run_policy_hanchan(
        policies,
        seed=task.seed,
        rules=locked_rules(),
        on_round_evidence_complete=observer.on_round_complete if observed else None,
        on_selector_call=count_selector,
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
    if observed:
        analysis = observer.finish(len(match.history))
    else:
        analysis = {
            "action_sequence_sha256": [
                action_sequence_digest(actions) for actions in observer.actions
            ]
        }
    return capture, analysis


def play_observed_hanchan(task: HanchanTask) -> PlayedHanchan:
    """worker内で観測ありの1半荘を実行する（実engine・実Champion）。"""
    environment = environment_document(task.wheel_path)
    capture, analysis = call_value_task(task, observed=True)
    return PlayedHanchan(
        capture,
        environment,
        os.getpid(),
        current_peak_rss_kib(),
        policy_analysis=analysis,
    )


def play_unobserved_hanchan(task: HanchanTask) -> PlayedHanchan:
    """worker内で観測なし（行動の列だけ）の1半荘を実行する（rehearsal専用）。"""
    environment = environment_document(task.wheel_path)
    capture, analysis = call_value_task(task, observed=False)
    return PlayedHanchan(
        capture,
        environment,
        os.getpid(),
        current_peak_rss_kib(),
        policy_analysis=analysis,
    )


__all__ = [
    "CANCELLATIONS",
    "CANCELLED_PRIORITY_CALL",
    "CANCELLED_ROUND_END",
    "CANCELLED_RON",
    "ESTABLISHED",
    "INVALID",
    "ROUTES",
    "SHADOW_VERSION",
    "SPEED_CALL",
    "STATUSES",
    "CallResolutionError",
    "CallValueObserver",
    "action_sequence_digest",
    "call_value_task",
    "decode_action",
    "decode_context",
    "encode_action",
    "encode_context",
    "meld_matches_call",
    "play_observed_hanchan",
    "play_unobserved_hanchan",
    "resolve_call",
]
