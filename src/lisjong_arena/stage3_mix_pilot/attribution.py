"""mixed populationにおけるkan opportunity / accountingのsource attribution。

Arena #146のpilotはcoverage source x4だったため、observerが見たdecisionは
すべてcoverage sourceのものだった。本pilotのB / C armは1 hanchanに

```text
yakuhai-call        primary source     3 seats
coverage source     augmentation       1 seat
```

を混在させるので、diagnosticをseat単位でattributeし直す必要がある。

## なぜ全seatを観測するか

`account_selected_kans()`は、observerが見たdecision数がraw corpusのcheckpoint数と
**exactに一致すること** をbinding invariantにしている。coverage seatだけをwrap
するとこの検証が成立しない。したがってPhase 4へは全seatのobserving factoryを渡し、
attributionは後段でplanのcoverage slotだけを取り出して行う。

## 情報境界

`KanDecisionRecord`は既にdecisionの`game_seed` / `viewer_seat`を持つため、
attributionはplanのlocked seat assignmentとの照合だけで閉じる。hidden hand、
wall truth、omniscient labelは一切参照しない。#146のdiagnostic / accounting
moduleは変更せず、restrictionだけをここで行う。

## primary sourceのcontract

`yakuhai-call`にはkan selection contractが無い。したがってprimary source側の
`selection_contract_violations`をhard gateにしない。primary側の値はdescriptive
diagnosticとしてだけ記録する。hard gateはcoverage source側にだけ適用する。
"""

from dataclasses import dataclass

from lisjong_engine.seat import Seat

from lisjong_arena.stage3_kan_coverage.accounting import SelectedKanAccount
from lisjong_arena.stage3_kan_coverage.opportunity import KanOpportunityDiagnostic
from lisjong_arena.stage3_mix_pilot.protocol import KAN_KINDS

_SEAT_BY_VALUE = {seat.value: seat for seat in Seat}


class MixAttributionError(ValueError):
    """source attributionのcontract violation。"""


def restrict_diagnostic(
    diagnostic: KanOpportunityDiagnostic, slots: frozenset[tuple[int, Seat]]
) -> KanOpportunityDiagnostic:
    """observerのdiagnosticを、与えた`(game_seed, Seat)`集合だけへ絞る。

    `KanOpportunityDiagnostic`の集計propertyをそのまま再利用するため、新しい
    diagnostic typeを作らず同じvalue typeを再構成する。
    """
    if not isinstance(diagnostic, KanOpportunityDiagnostic):
        raise TypeError("diagnostic must be a KanOpportunityDiagnostic")
    if not isinstance(slots, frozenset):
        raise TypeError("slots must be a frozenset of (game_seed, Seat)")
    observed = {
        (seed, _SEAT_BY_VALUE[seat])
        for seed, seat, _count in diagnostic.decision_counts
    }
    unknown = slots - observed
    if unknown:
        raise MixAttributionError(
            "the plan declares seats that the observer never saw: "
            f"{sorted((seed, seat.value) for seed, seat in unknown)}"
        )
    return KanOpportunityDiagnostic(
        records=tuple(
            value
            for value in diagnostic.records
            if (value.game_seed, value.viewer_seat) in slots
        ),
        decision_counts=tuple(
            row
            for row in diagnostic.decision_counts
            if (row[0], _SEAT_BY_VALUE[row[1]]) in slots
        ),
        passes_per_seat=diagnostic.passes_per_seat,
    )


def complement_slots(
    diagnostic: KanOpportunityDiagnostic, slots: frozenset[tuple[int, Seat]]
) -> frozenset[tuple[int, Seat]]:
    """observerが見た全seatのうち、与えたslotに含まれないものを返す。"""
    return (
        frozenset(
            (seed, _SEAT_BY_VALUE[seat])
            for seed, seat, _count in diagnostic.decision_counts
        )
        - slots
    )


def restrict_accounts(
    accounts: tuple[SelectedKanAccount, ...], slots: frozenset[tuple[int, Seat]]
) -> tuple[SelectedKanAccount, ...]:
    """selected kan accountを、与えた`(game_seed, Seat)`集合だけへ絞る。"""
    return tuple(
        value for value in accounts if (value.game_seed, value.viewer_seat) in slots
    )


@dataclass(frozen=True, slots=True)
class SourceAttribution:
    """1 armのsource別diagnostic / accounting。

    `coverage_*`はaugmentation sourceのdecisionだけ、`primary_*`は
    `yakuhai-call`のdecisionだけを含む。hard gateはcoverage側にだけかける。
    """

    arm_id: str
    coverage_diagnostic: KanOpportunityDiagnostic
    coverage_accounts: tuple[SelectedKanAccount, ...]
    primary_diagnostic: KanOpportunityDiagnostic
    primary_accounts: tuple[SelectedKanAccount, ...]

    @property
    def coverage_seat_slots(self) -> int:
        return len(self.coverage_diagnostic.decision_counts)

    @property
    def primary_seat_slots(self) -> int:
        return len(self.primary_diagnostic.decision_counts)


def attribute_sources(
    arm_id: str,
    diagnostic: KanOpportunityDiagnostic,
    accounts: tuple[SelectedKanAccount, ...],
    slots: frozenset[tuple[int, Seat]],
) -> SourceAttribution:
    """全seat observationを、coverage source / primary sourceへ分ける。"""
    primary = complement_slots(diagnostic, slots)
    return SourceAttribution(
        arm_id=arm_id,
        coverage_diagnostic=restrict_diagnostic(diagnostic, slots),
        coverage_accounts=restrict_accounts(accounts, slots),
        primary_diagnostic=restrict_diagnostic(diagnostic, primary),
        primary_accounts=restrict_accounts(accounts, primary),
    )


def primary_source_summary(diagnostic: KanOpportunityDiagnostic) -> dict[str, object]:
    """primary sourceのdescriptive diagnostic。

    `yakuhai-call`はkan selection contractを持たないため、ここでは
    `selection_contract_violations`をfield名としても出さない。kanをdeclineする
    ことはprimary sourceにとって正常な挙動である。
    """
    eligible = tuple(value for value in diagnostic.records if value.eligible_no_win)
    return {
        "contract_role": (
            "descriptive only: the primary source has no kan-selection contract, "
            "so declining a legal kan is expected behaviour and is never counted "
            "as a violation"
        ),
        "total_decisions": diagnostic.total_decisions,
        "kan_opportunity_decisions": len(diagnostic.records),
        "eligible_no_win_opportunity_decisions": len(eligible),
        "winning_action_also_legal_decisions": len(diagnostic.records) - len(eligible),
        "selected_kan_decisions": len(diagnostic.selected_records),
        "by_kind": {
            kind: {
                key: value
                for key, value in diagnostic.kind_counts(kind).items()
                if key != "eligible_no_win_opportunities_without_kan_selection"
            }
            for kind in KAN_KINDS
        },
    }


__all__ = [
    "MixAttributionError",
    "SourceAttribution",
    "attribute_sources",
    "complement_slots",
    "primary_source_summary",
    "restrict_accounts",
    "restrict_diagnostic",
]
