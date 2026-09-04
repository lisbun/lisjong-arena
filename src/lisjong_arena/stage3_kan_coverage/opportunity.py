"""legal kan opportunity / Policy conversionのpurpose-specific diagnostic。

Arena #146では「kan eventが何件出たか」だけでは、rare kindの0件がsourceの
不具合なのか単に機会が無かったのかを分離できない。したがって
`DecisionContext.legal_actions`をsource of truthとして、decisionごとに

```text
legal kan candidates (kind別)
winning action also legal か
eligible no-win kan opportunity か
selected semantic action
```

を数える。

## 情報境界

このdiagnosticはPolicyがdecisionで実際に見た`DecisionContext`だけを読む。
hidden hand、wall truth、omniscient labelを一切参照しない。したがって
opportunity判定はPolicy-visible informationだけで閉じている。

## winning + kan

`KanCoverageYakuhaiCallPolicy`はwinning actionをkanより優先する。したがって
winning actionが同時にlegalなdecisionは **kanを選ぶべきdecisionではない**。
`eligible no-win kan opportunity`だけがselection contractの対象である。

## 作らないもの

generic DecisionRecord / replay platform / decision persistence frameworkは
作らない。observerは既存Policy factory seamをwrapするだけで、Phase 2 / Phase 4の
recording pathもPolicy contractも変更しない。
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from lisjong.policy_contract.action import (
    AnkanAction,
    DaiminkanAction,
    KakanAction,
    RonAction,
    TsumoAction,
)
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.tile import tile_sort_key
from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.stage3_kan_coverage.protocol import (
    DIAGNOSTIC_SCHEMA_VERSION,
    KAN_KINDS,
)

_KAN_KIND_BY_TYPE = {
    DaiminkanAction: "daiminkan",
    AnkanAction: "ankan",
    KakanAction: "kakan",
}
_WINNING_KIND_BY_TYPE = {RonAction: "ron", TsumoAction: "tsumo"}


class KanOpportunityError(RuntimeError):
    """kan opportunity diagnosticのcontract violation。"""


def _tile_value(tile: object) -> list[object]:
    category, rank, is_red = tile_sort_key(tile)
    return [category, rank, is_red]


def kan_action_kind(action: object) -> str | None:
    """kan actionならそのkind名、そうでなければ`None`。"""
    return _KAN_KIND_BY_TYPE.get(type(action))


def winning_action_kind(action: object) -> str | None:
    """winning actionならそのkind名、そうでなければ`None`。"""
    return _WINNING_KIND_BY_TYPE.get(type(action))


def action_descriptor(action: object) -> dict[str, object]:
    """semantic fieldだけからdeterministicなaction descriptorを作る。

    object identityやinput順序へ依存しない。kan / winning以外のActionは
    kind名だけを持つ（本diagnosticはそれ以上を必要としない）。
    """
    if isinstance(action, DaiminkanAction):
        return {
            "kind": "daiminkan",
            "actor": int(action.actor),
            "target": int(action.target),
            "called_tile": _tile_value(action.called_tile),
            "consumed_tiles": [_tile_value(tile) for tile in action.consumed_tiles],
        }
    if isinstance(action, AnkanAction):
        return {
            "kind": "ankan",
            "actor": int(action.actor),
            "tiles": [_tile_value(tile) for tile in action.tiles],
        }
    if isinstance(action, KakanAction):
        return {
            "kind": "kakan",
            "actor": int(action.actor),
            "added_tile": _tile_value(action.added_tile),
            "from_seat": int(action.from_seat),
            "called_tile": _tile_value(action.called_tile),
        }
    if isinstance(action, RonAction):
        return {
            "kind": "ron",
            "actor": int(action.actor),
            "target": int(action.target),
            "winning_tile": _tile_value(action.winning_tile),
        }
    if isinstance(action, TsumoAction):
        return {
            "kind": "tsumo",
            "actor": int(action.actor),
            "winning_tile": _tile_value(action.winning_tile),
        }
    return {"kind": type(action).__name__}


def _descriptor_key(descriptor: dict[str, object]) -> bytes:
    return canonical_json_bytes(descriptor)


@dataclass(frozen=True, slots=True)
class KanDecisionRecord:
    """kan候補が1件以上legalだった1 decisionのdiagnostic record。"""

    game_seed: int
    viewer_seat: Seat
    decision_index: int
    winning_action_legal: bool
    winning_kinds: tuple[str, ...]
    candidates: tuple[bytes, ...]
    candidate_kinds: tuple[str, ...]
    selected_kind: str | None
    selected_descriptor: bytes | None
    policy_input: object = field(compare=True, repr=False)

    @property
    def eligible_no_win(self) -> bool:
        """winning actionが無く、kanを選ぶことが期待されるdecisionか。"""
        return not self.winning_action_legal

    @property
    def selected_kan(self) -> bool:
        return self.selected_kind is not None

    @property
    def distinct_candidate_kinds(self) -> tuple[str, ...]:
        return tuple(kind for kind in KAN_KINDS if kind in self.candidate_kinds)

    def candidate_count(self, kind: str) -> int:
        return sum(1 for value in self.candidate_kinds if value == kind)

    def selected_action_value(self) -> dict[str, object] | None:
        """selectedだったkan actionのsemantic descriptor。"""
        if self.selected_descriptor is None:
            return None
        return _decode(self.selected_descriptor)

    def record_value(self) -> dict[str, object]:
        return {
            "game_seed": self.game_seed,
            "viewer_seat": self.viewer_seat.value,
            "decision_index": self.decision_index,
            "winning_action_legal": self.winning_action_legal,
            "winning_kinds": list(self.winning_kinds),
            "candidate_kinds": list(self.candidate_kinds),
            "candidates": [_decode(value) for value in self.candidates],
            "selected_kind": self.selected_kind,
            "selected_action": (
                None
                if self.selected_descriptor is None
                else _decode(self.selected_descriptor)
            ),
        }


def _decode(value: bytes) -> object:
    """canonical descriptor bytesをJSON valueへ戻す。"""
    return json.loads(value.decode("utf-8"))


class _SeatPass:
    """1 (seed, seat) の1回のPolicy instance実行で観測したdecision列。"""

    __slots__ = ("records", "decision_count")

    def __init__(self) -> None:
        self.records: list[KanDecisionRecord] = []
        self.decision_count = 0


class _ObservingPolicy:
    """delegate Policyのdecisionをそのまま返しつつ観測するwrapper。

    delegateの返却値を変換せず、legality判定も行わない。例外はそのまま伝播
    させる（記録もしない）。
    """

    __slots__ = ("_policy", "_pass", "_game_seed", "_viewer_seat")

    def __init__(
        self, policy: object, seat_pass: _SeatPass, game_seed: int, viewer_seat: Seat
    ) -> None:
        self._policy = policy
        self._pass = seat_pass
        self._game_seed = game_seed
        self._viewer_seat = viewer_seat

    def choose_action(self, decision: DecisionContext):
        action = self._policy.choose_action(decision)
        index = self._pass.decision_count
        self._pass.decision_count = index + 1
        candidates = tuple(
            sorted(
                (
                    _descriptor_key(action_descriptor(value))
                    for value in decision.legal_actions
                    if kan_action_kind(value) is not None
                )
            )
        )
        if not candidates:
            return action
        winning_kinds = tuple(
            sorted(
                {
                    kind
                    for value in decision.legal_actions
                    if (kind := winning_action_kind(value)) is not None
                }
            )
        )
        candidate_kinds = tuple(
            kan_action_kind(value)
            for value in sorted(
                (
                    value
                    for value in decision.legal_actions
                    if kan_action_kind(value) is not None
                ),
                key=lambda value: _descriptor_key(action_descriptor(value)),
            )
        )
        selected_kind = kan_action_kind(action)
        self._pass.records.append(
            KanDecisionRecord(
                game_seed=self._game_seed,
                viewer_seat=self._viewer_seat,
                decision_index=index,
                winning_action_legal=bool(winning_kinds),
                winning_kinds=winning_kinds,
                candidates=candidates,
                candidate_kinds=candidate_kinds,
                selected_kind=selected_kind,
                selected_descriptor=(
                    None
                    if selected_kind is None
                    else _descriptor_key(action_descriptor(action))
                ),
                policy_input=decision.input,
            )
        )
        return action


@dataclass(frozen=True, slots=True)
class KanOpportunityDiagnostic:
    """1 populationのkan opportunity / selection diagnostic。"""

    records: tuple[KanDecisionRecord, ...]
    decision_counts: tuple[tuple[int, str, int], ...]
    passes_per_seat: tuple[int, ...]

    @property
    def total_decisions(self) -> int:
        return sum(value for _seed, _seat, value in self.decision_counts)

    @property
    def selection_contract_violations(self) -> tuple[KanDecisionRecord, ...]:
        """eligible no-win kan opportunityでkanを選ばなかったdecision。"""
        return tuple(
            value
            for value in self.records
            if value.eligible_no_win and not value.selected_kan
        )

    @property
    def selected_records(self) -> tuple[KanDecisionRecord, ...]:
        return tuple(value for value in self.records if value.selected_kan)

    def kind_counts(self, kind: str) -> dict[str, int]:
        with_kind = tuple(
            value for value in self.records if kind in value.candidate_kinds
        )
        eligible = tuple(value for value in with_kind if value.eligible_no_win)
        return {
            "legal_opportunities": len(with_kind),
            "legal_candidate_actions": sum(
                value.candidate_count(kind) for value in with_kind
            ),
            "legal_opportunities_with_winning_action": len(with_kind) - len(eligible),
            "eligible_no_win_opportunities": len(eligible),
            "selected": sum(1 for value in self.records if value.selected_kind == kind),
        }

    def diagnostic_value(self) -> dict[str, object]:
        eligible = tuple(value for value in self.records if value.eligible_no_win)
        multiple_candidates = tuple(
            value for value in self.records if len(value.candidates) > 1
        )
        multiple_kinds = tuple(
            value for value in self.records if len(value.distinct_candidate_kinds) > 1
        )
        return {
            "diagnostic_schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "total_decisions": self.total_decisions,
            "policy_instance_passes_per_seat": list(self.passes_per_seat),
            "kan_opportunity_decisions": len(self.records),
            "eligible_no_win_opportunity_decisions": len(eligible),
            "winning_action_also_legal_decisions": len(self.records) - len(eligible),
            "selected_kan_decisions": len(self.selected_records),
            "selection_contract_violations": len(self.selection_contract_violations),
            "multiple_kan_candidate_decisions": len(multiple_candidates),
            "multiple_kan_kind_decisions": len(multiple_kinds),
            "by_kind": {kind: self.kind_counts(kind) for kind in KAN_KINDS},
            "kan_opportunity_records": [value.record_value() for value in self.records],
        }


class KanOpportunityObserver:
    """Policy factoryをwrapして、decisionごとのkan opportunityを観測する。

    Phase 4 protocolは同じseat assignmentでrecording runとPhase 2 equality
    re-runの2回Policyを実行する。observerはPolicy instanceごとにpassを分け、
    同じ(seed, seat)の全passがexactに一致することを検証してから、canonical
    passだけをdiagnosticへ採用する。一致しない場合は非決定的実行として
    fail closedする。
    """

    def __init__(self) -> None:
        self._passes: dict[tuple[int, Seat], list[_SeatPass]] = {}

    def wrap_factories_by_seed(
        self, factories_by_seed: Mapping[int, Mapping[Seat, Callable[[], object]]]
    ) -> dict[int, dict[Seat, Callable[[], object]]]:
        """seed別 / seat別factoryを観測付きfactoryへ包む。"""
        return {
            seed: {
                seat: self._wrapped_factory(seed, seat, factory)
                for seat, factory in factories.items()
            }
            for seed, factories in factories_by_seed.items()
        }

    def _wrapped_factory(
        self, game_seed: int, viewer_seat: Seat, factory: Callable[[], object]
    ) -> Callable[[], object]:
        passes = self._passes.setdefault((game_seed, viewer_seat), [])

        def build() -> object:
            seat_pass = _SeatPass()
            passes.append(seat_pass)
            return _ObservingPolicy(factory(), seat_pass, game_seed, viewer_seat)

        return build

    def resolve(self) -> KanOpportunityDiagnostic:
        """観測結果をdeterministicなdiagnosticへ確定する。"""
        if not self._passes:
            raise KanOpportunityError("no policy decision was observed")
        pass_counts = set()
        records: list[KanDecisionRecord] = []
        decision_counts: list[tuple[int, str, int]] = []
        for key in sorted(self._passes, key=lambda value: (value[0], value[1].value)):
            game_seed, viewer_seat = key
            passes = self._passes[key]
            pass_counts.add(len(passes))
            canonical = passes[0]
            for other in passes[1:]:
                if (
                    other.decision_count != canonical.decision_count
                    or other.records != canonical.records
                ):
                    raise KanOpportunityError(
                        "repeated execution of the same seed and seat produced a "
                        "different decision sequence"
                    )
            decision_counts.append(
                (game_seed, viewer_seat.value, canonical.decision_count)
            )
            records.extend(canonical.records)
        return KanOpportunityDiagnostic(
            records=tuple(records),
            decision_counts=tuple(decision_counts),
            passes_per_seat=tuple(sorted(pass_counts)),
        )


__all__ = [
    "KanDecisionRecord",
    "KanOpportunityDiagnostic",
    "KanOpportunityError",
    "KanOpportunityObserver",
    "action_descriptor",
    "kan_action_kind",
    "winning_action_kind",
]
