"""selected kan -> confirmed / explicit non-confirm -> rinshan accounting。

Arena #146では次の3つを同一視しない。

```text
selected kan      Policyがdecisionで選んだkan action
confirmed kan     public evidenceでmeldとして成立したkan
rinshan draw      成立後の嶺上ツモ
```

selected kanは必ずしもconfirmedされない。current engine semanticsでは少なくとも

```text
kakan / ankan -> 槍槓ronで成立しない
daiminkan     -> 同じ打牌に対する他家のron / callが先行する
```

がlegalなnon-confirm pathである。またconfirmed kanでも四槓散了で局が終了する
場合はrinshanへ進まない。これらを`missing`と誤分類しない。

## Evidence binding

推測を挟まないために、selected decisionとpublic evidenceは既存artifactだけで
一意にbindする。

```text
KanDecisionRecord(game_seed, viewer_seat, decision_index)
    -> raw corpusの同じseatのcheckpoint列のdecision_index番目
    -> checkpoint.evidence_cutoff
    -> 同じviewerのplayer-safe evidence streamのsuffix
```

bindingは`build_policy_input(checkpoint.observation)`がdecision時の
`DecisionContext.input`とexactに一致することで検証する。件数・順序が一致
しない場合はsilentに受け入れずfail closedする。

## 作らないもの

generic replay platform / event-sourcing framework / decision database は
作らない。既存raw corpus evidenceの読み取りだけで閉じる。
"""

from dataclasses import dataclass

from lisjong.policy_contract import Seat as PolicySeat
from lisjong_engine.public_state import PublicMeldType
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import (
    DrawEvidence,
    KanConfirmedEvidence,
    KanDeclaredEvidence,
    MeldCalledEvidence,
    RoundEndedEvidence,
)
from lisjong_engine.seat import Seat

from lisjong_arena.lisjong_engine.domain_conversion import seat_from_engine_seat
from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.phase4_raw_corpus.model import RawCorpus
from lisjong_arena.stage3_kan_coverage.opportunity import KanOpportunityDiagnostic
from lisjong_arena.stage3_kan_coverage.protocol import (
    ACCOUNTING_SCHEMA_VERSION,
    KAN_KINDS,
)

CONFIRMED = "confirmed"
NON_CONFIRM = "explicit-non-confirm"
UNACCOUNTED = "unaccounted"

_MELD_TYPE_BY_KIND = {
    "daiminkan": PublicMeldType.DAIMINKAN,
    "ankan": PublicMeldType.ANKAN,
    "kakan": PublicMeldType.KAKAN,
}

_ENGINE_SEAT_BY_SEAT = {seat_from_engine_seat(seat): seat for seat in Seat}
"""既存engine -> lisjong seat対応表の逆引き。

新しい対応規則を作らず、`domain_conversion`の正本tableをそのまま反転する。
"""


class KanAccountingError(RuntimeError):
    """selected kanとpublic evidenceのbindingが成立しない場合。"""


@dataclass(frozen=True, slots=True)
class SelectedKanAccount:
    """1件のselected kanのevidence accounting。"""

    game_seed: int
    viewer_seat: Seat
    decision_index: int
    round_index: int
    checkpoint_index: int
    kind: str
    outcome: str
    detail: str
    rinshan_expected: bool
    rinshan_observed: bool

    def account_value(self) -> dict[str, object]:
        return {
            "game_seed": self.game_seed,
            "viewer_seat": self.viewer_seat.value,
            "decision_index": self.decision_index,
            "round_index": self.round_index,
            "checkpoint_index": self.checkpoint_index,
            "kind": self.kind,
            "outcome": self.outcome,
            "detail": self.detail,
            "rinshan_expected": self.rinshan_expected,
            "rinshan_observed": self.rinshan_observed,
        }


def _round_end_detail(evidence: RoundEndedEvidence) -> str:
    if evidence.abortive_reason is not None:
        return f"round ended: abortive draw {evidence.abortive_reason.value}"
    if evidence.win_method is not None:
        return f"round ended: {evidence.kind.value} by {evidence.win_method.value}"
    return f"round ended: {evidence.kind.value}"


def _rinshan_after_confirmation(
    stream: tuple, start: int, actor: Seat
) -> tuple[bool, bool, str]:
    """confirmed kanの直後に嶺上ツモが続いたかをpublic evidenceから判定する。

    confirm直後は嶺上ツモか局の終了しか起こらない。局が先に終了した場合は
    `rinshan missing`ではなく「継続が期待されないterminal」として扱う。
    """
    for evidence in stream[start:]:
        if isinstance(evidence, DrawEvidence):
            if evidence.seat is actor and evidence.source is DrawSource.RINSHAN:
                return True, True, "confirmed kan followed by a rinshan draw"
            return (
                True,
                False,
                "confirmed kan followed by a draw that is not the actor's rinshan draw",
            )
        if isinstance(evidence, RoundEndedEvidence):
            return False, False, _round_end_detail(evidence)
    return True, False, "confirmed kan has no subsequent draw or round end evidence"


def classify_selected_kan(
    stream: tuple,
    cutoff: int,
    *,
    kind: str,
    actor: Seat,
    target: Seat | None,
) -> tuple[str, str, bool, bool]:
    """1件のselected kanをpublic evidence suffixから分類する。

    返り値は`(outcome, detail, rinshan_expected, rinshan_observed)`である。
    `stream`はactor自身のplayer-safe evidence列、`cutoff`はそのdecision時点の
    evidence prefix長である。
    """
    if kind not in _MELD_TYPE_BY_KIND:
        raise KanAccountingError(f"unknown kan kind {kind!r}")
    meld_type = _MELD_TYPE_BY_KIND[kind]
    if kind == "daiminkan":
        if target is None:
            raise KanAccountingError("a daiminkan account requires its target seat")
        for index, evidence in enumerate(stream[cutoff:], start=cutoff):
            if isinstance(evidence, MeldCalledEvidence):
                if evidence.seat is actor and evidence.meld.meld_type is meld_type:
                    expected, observed, detail = _rinshan_after_confirmation(
                        stream, index + 1, actor
                    )
                    return CONFIRMED, detail, expected, observed
                return (
                    NON_CONFIRM,
                    "another seat's call resolved the discard response epoch first",
                    False,
                    False,
                )
            if isinstance(evidence, RoundEndedEvidence):
                return NON_CONFIRM, _round_end_detail(evidence), False, False
            if isinstance(evidence, DrawEvidence):
                break
        return (
            UNACCOUNTED,
            "no call, draw or round end evidence resolved the selected daiminkan",
            False,
            False,
        )
    declared = False
    for index, evidence in enumerate(stream[cutoff:], start=cutoff):
        if (
            isinstance(evidence, KanDeclaredEvidence)
            and evidence.seat is actor
            and evidence.meld.meld_type is meld_type
        ):
            declared = True
            continue
        if (
            isinstance(evidence, KanConfirmedEvidence)
            and evidence.seat is actor
            and evidence.meld.meld_type is meld_type
        ):
            if not declared:
                return (
                    UNACCOUNTED,
                    "a confirmed kan appeared without its public declaration",
                    False,
                    False,
                )
            expected, observed, detail = _rinshan_after_confirmation(
                stream, index + 1, actor
            )
            return CONFIRMED, detail, expected, observed
        if isinstance(evidence, RoundEndedEvidence):
            if not declared:
                return (
                    UNACCOUNTED,
                    "the round ended without the selected kan being declared",
                    False,
                    False,
                )
            return NON_CONFIRM, _round_end_detail(evidence), False, False
        if isinstance(evidence, DrawEvidence) and declared:
            return (
                UNACCOUNTED,
                "a draw followed the declaration without confirming the kan",
                False,
                False,
            )
    return (
        UNACCOUNTED,
        "no confirmation or terminal evidence followed the selected kan",
        False,
        False,
    )


def _checkpoints_by_seat(corpus: RawCorpus) -> dict[tuple[int, Seat], tuple]:
    """seed / seat別に、decision順のcheckpointと所属roundを並べる。"""
    ordered: dict[tuple[int, Seat], list] = {}
    for game in corpus.games:
        for raw_round in game.rounds:
            for checkpoint in raw_round.checkpoints:
                key = (game.seed, checkpoint.viewer_seat)
                ordered.setdefault(key, []).append((raw_round, checkpoint))
    return {key: tuple(value) for key, value in ordered.items()}


def account_selected_kans(
    corpus: RawCorpus, diagnostic: KanOpportunityDiagnostic
) -> tuple[SelectedKanAccount, ...]:
    """selected kanをraw corpusのpublic evidenceへbindしてaccountする。"""
    if not isinstance(corpus, RawCorpus):
        raise TypeError("corpus must be a RawCorpus")
    if not isinstance(diagnostic, KanOpportunityDiagnostic):
        raise TypeError("diagnostic must be a KanOpportunityDiagnostic")
    ordered = _checkpoints_by_seat(corpus)
    observed_counts = {
        (seed, seat): count for seed, seat, count in diagnostic.decision_counts
    }
    corpus_counts = {
        (seed, seat.value): len(value) for (seed, seat), value in ordered.items()
    }
    if observed_counts != corpus_counts:
        raise KanAccountingError(
            "observed decision counts differ from the recorded checkpoint counts"
        )
    accounts = []
    for record in diagnostic.selected_records:
        engine_seat = _ENGINE_SEAT_BY_SEAT[record.policy_input.self_seat]
        if engine_seat is not record.viewer_seat:
            raise KanAccountingError(
                "the decision context seat differs from the observed seat"
            )
        rows = ordered[(record.game_seed, record.viewer_seat)]
        raw_round, checkpoint = rows[record.decision_index]
        if build_policy_input(checkpoint.observation) != record.policy_input:
            raise KanAccountingError(
                "the bound checkpoint observation differs from the observed decision"
            )
        stream = next(
            value.evidence
            for value in raw_round.viewer_evidence
            if value.viewer_seat is record.viewer_seat
        )
        descriptor = record.selected_action_value()
        target = descriptor.get("target")
        target_seat = (
            None if target is None else _ENGINE_SEAT_BY_SEAT[PolicySeat(target)]
        )
        outcome, detail, expected, observed = classify_selected_kan(
            stream,
            checkpoint.evidence_cutoff,
            kind=record.selected_kind,
            actor=record.viewer_seat,
            target=target_seat,
        )
        accounts.append(
            SelectedKanAccount(
                game_seed=record.game_seed,
                viewer_seat=record.viewer_seat,
                decision_index=record.decision_index,
                round_index=raw_round.round_index,
                checkpoint_index=checkpoint.checkpoint_index,
                kind=record.selected_kind,
                outcome=outcome,
                detail=detail,
                rinshan_expected=expected,
                rinshan_observed=observed,
            )
        )
    return tuple(accounts)


def accounting_value(accounts: tuple[SelectedKanAccount, ...]) -> dict[str, object]:
    """kan kind別のselected / confirmed / non-confirm / unaccounted集計。"""

    def counts_for(rows: tuple[SelectedKanAccount, ...]) -> dict[str, object]:
        confirmed = tuple(value for value in rows if value.outcome == CONFIRMED)
        continuing = tuple(value for value in confirmed if value.rinshan_expected)
        return {
            "selected": len(rows),
            "confirmed": len(confirmed),
            "explicit_non_confirm": sum(
                1 for value in rows if value.outcome == NON_CONFIRM
            ),
            "unaccounted": sum(1 for value in rows if value.outcome == UNACCOUNTED),
            "confirmed_with_expected_rinshan_continuation": len(continuing),
            "confirmed_without_expected_continuation": (
                len(confirmed) - len(continuing)
            ),
            "rinshan_observed": sum(
                1 for value in continuing if value.rinshan_observed
            ),
            "rinshan_missing": sum(
                1 for value in continuing if not value.rinshan_observed
            ),
        }

    return {
        "accounting_schema_version": ACCOUNTING_SCHEMA_VERSION,
        "totals": counts_for(accounts),
        "by_kind": {
            kind: counts_for(tuple(value for value in accounts if value.kind == kind))
            for kind in KAN_KINDS
        },
        "selected_kan_accounts": [value.account_value() for value in accounts],
    }


__all__ = [
    "CONFIRMED",
    "NON_CONFIRM",
    "UNACCOUNTED",
    "KanAccountingError",
    "SelectedKanAccount",
    "account_selected_kans",
    "accounting_value",
    "classify_selected_kan",
]
