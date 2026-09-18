"""RiichiLab ranked live presentation seam(`lisbun/lisjong-play#41` cross-repo prerequisite)。

RiichiLab ranked実行中に、Policyが実際に見たplayer-visible decision stateだけを
外部presentation consumerへ渡すための、bounded / non-blockingなdelivery
boundaryである。

```text
RiichiLab request_action
    -> parse_request_action()
    -> RiichiLabSeatAdapter.process_request_action_with_decision_facts()
         -> build_decision() -> DecisionContext.input (PolicyInput)
         -> execute_policy() -> canonical InternalAction
         -> MJAI response / possible_actions validation
         -> send-ready response              (authoritative execution path)
                |
                +-> RankedDecisionPresentation      (immutable player-visible fact)
                       -> BoundedRankedPresentationBuffer
                       -> external presentation consumer (read-only)
```

設計境界:

- presentationはplayer-visible Policy decision stateのread-only consumerで
  あり、ranked execution timingを支配しない。publishはbounded bufferへの
  O(1) appendだけであり、consumer側の処理をranked response path上で同期
  実行しない。consumerが遅くても、またはdrainを完全に止めても、Policy
  response・`possible_actions` validation・WebSocket send・次requestの処理は
  遅延しない
- presentation factの正本は、同じdecisionでPolicyへ実際に渡した
  `DecisionContext.input`(`PolicyInput`)である。raw transport payload
  (raw `request_action` JSON、base64 Observation)をpresentation APIにしない
- token / Authorization / credential / WebSocket objectはpresentation fact
  にもbufferにも一切保持しない
- generic event bus / telemetry framework / viewer frameworkではない。
  RiichiLab ranked live presentation専用のbounded seamに限定し、
  project-wideなpublish/subscribe抽象へ拡張しない
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from lisjong.policy_contract import InternalAction, PolicyInput, Seat

#: decision snapshot bufferの既定容量。明示的・有限であり、unbounded Queueは
#: 使わない。consumerは必要に応じて別の有限capacityを指定できる。
DEFAULT_DECISION_BUFFER_CAPACITY = 64


@dataclass(frozen=True, slots=True)
class RankedDecisionPresentation:
    """1 decision分のimmutableなplayer-visible presentation fact。

    `policy_input`は、この`request_id`の決定でPolicyへ実際に渡した
    `DecisionContext.input`そのものである。presentation向けに再計算・
    再解釈した別のprojectionではないため、consumerはraw `request_action`
    JSONやbase64 Observationを再parseする必要がない。

    `selected_action`は`execute_policy()`が合法候補へ照合して返した
    canonical `InternalAction`である。outgoing MJAI payloadは、canonical
    selected actionで表示に足りる限りここへ重複させない。

    raw transport payload、base64 Observation、token / Authorization /
    credential、WebSocket objectは含めない。Policy-internal analysis
    (shanten / ukeire / HandBelief / danger / value estimate等)も含めない。
    """

    request_id: int
    self_seat: Seat
    policy_input: PolicyInput
    selected_action: InternalAction

    def __post_init__(self) -> None:
        if isinstance(self.request_id, bool) or not isinstance(self.request_id, int):
            raise TypeError("request_id must be an int")
        if not isinstance(self.self_seat, Seat):
            raise TypeError("self_seat must be a Seat")
        if not isinstance(self.policy_input, PolicyInput):
            raise TypeError("policy_input must be a PolicyInput")
        if self.policy_input.self_seat != self.self_seat:
            raise ValueError("policy_input must belong to the bound seat")
        if getattr(self.selected_action, "actor", None) != self.self_seat:
            raise ValueError("selected_action must be acted by the bound seat")


@dataclass(frozen=True, slots=True)
class RankedCompletionPresentation:
    """完走した`end_game`のterminal presentation fact。

    `scores`はSession lifecycleが受理したfinal scoresであり、presentation側で
    推測・補完しない(RiichiLabが`scores`を伴わない`end_game`を送った場合は
    `None`のまま)。順位・yaku・han / fu等、protocolがexactに提供しない
    round-result detailはここで導出しない。
    """

    self_seat: Seat
    scores: tuple[int, int, int, int] | None

    def __post_init__(self) -> None:
        if not isinstance(self.self_seat, Seat):
            raise TypeError("self_seat must be a Seat")
        if self.scores is None:
            return
        if not isinstance(self.scores, tuple) or len(self.scores) != 4:
            raise TypeError("scores must be None or a four-item tuple")
        if any(
            isinstance(score, bool) or not isinstance(score, int)
            for score in self.scores
        ):
            raise TypeError("scores must contain only integers")


@dataclass(frozen=True, slots=True)
class RankedFailurePresentation:
    """ranked runが完走しなかったことを示すterminal presentation fact。

    viewerがrenderingの停止をsuccessful match completionと取り違えないための
    最小限のfactである。failure messageやraw transport payloadは持たず、
    例外のtype名だけを持つ。RiichiLab側messageやprotocol payloadには
    credentialやserver由来の生dataが混ざり得るため、presentation boundaryを
    越えて運ばない。
    """

    failure_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.failure_type, str) or not self.failure_type:
            raise TypeError("failure_type must be a non-empty string")


@dataclass(frozen=True, slots=True)
class RankedPresentationBatch:
    """1回の`drain()`で引き渡すpresentation factのsnapshot。

    `decisions`は受信順であり、`completion` / `failure`はそのbatchまでに
    確定したterminal factである。`coalesced_decisions`は、前回の`drain()`
    以降にbuffer満杯によってcoalesceされた古いdecision snapshotの件数を示す。
    """

    decisions: tuple[RankedDecisionPresentation, ...]
    completion: RankedCompletionPresentation | None
    failure: RankedFailurePresentation | None
    coalesced_decisions: int

    @property
    def is_empty(self) -> bool:
        return not self.decisions and self.completion is None and self.failure is None


class BoundedRankedPresentationBuffer:
    """ranked execution pathとpresentation consumerを分離するbounded buffer。

    publish側(ranked worker)はlockを保持したままO(1)の追加だけを行い、
    consumer側の処理を一切呼び出さない。consumerは`drain()`でbatchを取り出し、
    描画等の重い処理はlockの外・ranked execution pathの外で行う。任意の
    consumer callbackをranked response path上で同期実行する設計にはしない。

    decision snapshotはcumulative player-visible stateであるため、capacityに
    達した場合は最も古いdecision snapshotをcoalesce(drop)して最新を保持する
    (`coalesced_decisions`で件数を可視化する)。terminal fact
    (`RankedCompletionPresentation` / `RankedFailurePresentation`)はこの
    capacityの外の専用slotで保持し、silentにdropしない。

    `detach()`後のpublishはno-op相当であり、例外を送出しない。presentation
    consumerが閉じてもranked sessionをabortさせないためである。
    """

    __slots__ = (
        "_capacity",
        "_lock",
        "_decisions",
        "_completion",
        "_failure",
        "_attached",
        "_coalesced_since_drain",
        "_total_coalesced_decisions",
    )

    def __init__(self, *, capacity: int = DEFAULT_DECISION_BUFFER_CAPACITY) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError("capacity must be an int")
        if capacity < 1:
            raise ValueError("capacity must be a positive int")

        self._capacity = capacity
        self._lock = threading.Lock()
        self._decisions: deque[RankedDecisionPresentation] = deque(maxlen=capacity)
        self._completion: RankedCompletionPresentation | None = None
        self._failure: RankedFailurePresentation | None = None
        self._attached = True
        self._coalesced_since_drain = 0
        self._total_coalesced_decisions = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def is_attached(self) -> bool:
        with self._lock:
            return self._attached

    @property
    def pending_decisions(self) -> int:
        """未drainのdecision snapshot件数。常に`capacity`以下である。"""
        with self._lock:
            return len(self._decisions)

    @property
    def total_coalesced_decisions(self) -> int:
        """このbufferがこれまでにcoalesceした古いdecision snapshotの累計。"""
        with self._lock:
            return self._total_coalesced_decisions

    def publish_decision(self, decision: RankedDecisionPresentation) -> None:
        """1件のdecision factをbufferへ追加する(non-blocking)。"""
        if not isinstance(decision, RankedDecisionPresentation):
            raise TypeError("decision must be a RankedDecisionPresentation")
        with self._lock:
            if not self._attached:
                return
            if len(self._decisions) == self._capacity:
                self._coalesced_since_drain += 1
                self._total_coalesced_decisions += 1
            self._decisions.append(decision)

    def publish_completion(self, completion: RankedCompletionPresentation) -> None:
        """完走terminal factを専用slotへ記録する(capacityの影響を受けない)。"""
        if not isinstance(completion, RankedCompletionPresentation):
            raise TypeError("completion must be a RankedCompletionPresentation")
        with self._lock:
            if not self._attached or self._completion is not None:
                return
            self._completion = completion

    def publish_failure(self, failure: RankedFailurePresentation) -> None:
        """failure terminal factを専用slotへ記録する(capacityの影響を受けない)。"""
        if not isinstance(failure, RankedFailurePresentation):
            raise TypeError("failure must be a RankedFailurePresentation")
        with self._lock:
            if not self._attached or self._failure is not None:
                return
            self._failure = failure

    def drain(self) -> RankedPresentationBatch:
        """未消費のpresentation factをまとめて取り出す。

        取り出したfactはbufferから除かれる。戻り値はSession側mutable state
        から切り離されたimmutable snapshotであり、以降のpublishで内容が
        変化することはない。
        """
        with self._lock:
            decisions = tuple(self._decisions)
            self._decisions.clear()
            completion = self._completion
            self._completion = None
            failure = self._failure
            self._failure = None
            coalesced = self._coalesced_since_drain
            self._coalesced_since_drain = 0

        return RankedPresentationBatch(
            decisions=decisions,
            completion=completion,
            failure=failure,
            coalesced_decisions=coalesced,
        )

    def detach(self) -> None:
        """consumerの離脱を記録し、以降のpublishをno-op相当にする。

        すでにdetach済みの場合も含めてidempotentであり、例外を送出しない。
        ranked sessionはこの呼び出しによって中断されない。
        """
        with self._lock:
            self._attached = False
            self._decisions.clear()
            self._completion = None
            self._failure = None
            self._coalesced_since_drain = 0


__all__ = [
    "DEFAULT_DECISION_BUFFER_CAPACITY",
    "BoundedRankedPresentationBuffer",
    "RankedCompletionPresentation",
    "RankedDecisionPresentation",
    "RankedFailurePresentation",
    "RankedPresentationBatch",
]
