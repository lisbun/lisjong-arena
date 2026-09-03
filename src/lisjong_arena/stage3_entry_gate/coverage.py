"""Stage 3 Entry Gate corpus / event coverage measurement.

Entry Gateのpopulation選定は、within-population MAEだけでは決められない。
「そのpopulationがどのbehavior / eventを実際に生んだか」を数える必要がある。
本moduleはそれを既存artifactの再集計だけで行う。

```text
RawCorpus viewer evidence  -> globally-public event coverage
frozen player-safe anchor  -> anchor stratum coverage (player-safe)
ExactTrainingLabels        -> offline diagnostic stratum (omniscient)
```

## Information boundary

anchor stratumのうちpost-call / post-riichi / open-closed / depthは、anchorが
freezeしたplayer-safe evidence prefixと`SeatObservation`だけから数える。
true-tenpai stratumだけはtraining-only labelから数えるoffline diagnosticであり、
featureへは流さない。Phase 6 feature builderはこのmoduleを参照しない。

rare eventが0件でもrowを捏造せず、`0`をunsupported / unmeasured coverageとして
そのまま残す。
"""

from collections import Counter
from dataclasses import dataclass

from lisjong_engine.public_state import PublicMeldType
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import (
    DiscardEvidence,
    DrawEvidence,
    KanConfirmedEvidence,
    MeldCalledEvidence,
    RiichiEstablishedEvidence,
)
from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.model import RawCorpus
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase8_sequential.protocol import DEPTH_BUCKETS, depth_bucket

DEPTH_BUCKET_LABELS = {
    DEPTH_BUCKETS[0]: "early",
    DEPTH_BUCKETS[1]: "early",
    DEPTH_BUCKETS[2]: "mid",
    DEPTH_BUCKETS[3]: "late",
}
"""Phase 8のcanonical depth bucketへ、Issue本文のearly / mid / late名を対応付ける。

新しいbucket境界を作らず、既存bucketのlabelとしてだけ扱う。
"""

_MELD_EVENT_KEYS = tuple(value.value for value in PublicMeldType)


@dataclass(frozen=True, slots=True)
class EventCoverage:
    """globally-publicなround eventのcorpus全体coverage。"""

    hanchan: int
    rounds: int
    total_checkpoints: int
    stable_turn_anchors: int
    opponent_rows: int
    discard: int
    tsumogiri: int
    tedashi: int
    riichi_declaration: int
    riichi_established: int
    chi: int
    pon: int
    daiminkan: int
    ankan: int
    kakan: int
    rinshan_draw: int
    live_wall_draw: int

    @property
    def absent_event_strata(self) -> tuple[str, ...]:
        """0件だったevent stratum名。捏造せずそのまま残す。"""
        return tuple(
            name
            for name in (
                "tsumogiri",
                "tedashi",
                "riichi_declaration",
                "riichi_established",
                "chi",
                "pon",
                "daiminkan",
                "ankan",
                "kakan",
                "rinshan_draw",
            )
            if getattr(self, name) == 0
        )


@dataclass(frozen=True, slots=True)
class AnchorCoverage:
    """TURN anchor stratumのcoverage。"""

    partition: str
    anchors: int
    depth_bucket_counts: tuple[tuple[str, int], ...]
    anchors_after_call: int
    anchors_after_riichi: int
    opponent_rows_open: int
    opponent_rows_closed: int
    opponent_rows_true_tenpai: int
    opponent_rows_true_non_tenpai: int
    opponent_rows_wait_unavailable: int


def _event_coverage(corpus: RawCorpus, anchors: int) -> EventCoverage:
    """viewer不変なpublic eventだけを1 viewer streamから数える。

    `measure_raw_corpus()`がglobally-public evidenceのviewer間一致を既に検証
    しているため、ここでは同じcanonical first viewerのstreamを再集計する。
    viewer-privateなdraw tileは読まない。
    """
    first_viewer = tuple(Seat)[0]
    counts: Counter[str] = Counter()
    rounds = 0
    for game in corpus.games:
        for raw_round in game.rounds:
            rounds += 1
            stream = next(
                value.evidence
                for value in raw_round.viewer_evidence
                if value.viewer_seat is first_viewer
            )
            for evidence in stream:
                if isinstance(evidence, DiscardEvidence):
                    counts["discard"] += 1
                    counts["tsumogiri" if evidence.is_tsumogiri else "tedashi"] += 1
                    if evidence.is_riichi_declaration:
                        counts["riichi_declaration"] += 1
                elif isinstance(evidence, RiichiEstablishedEvidence):
                    counts["riichi_established"] += 1
                elif isinstance(evidence, MeldCalledEvidence):
                    counts[evidence.meld.meld_type.value] += 1
                elif isinstance(evidence, KanConfirmedEvidence):
                    counts[evidence.meld.meld_type.value] += 1
                elif isinstance(evidence, DrawEvidence):
                    counts[
                        "rinshan_draw"
                        if evidence.source is DrawSource.RINSHAN
                        else "live_wall_draw"
                    ] += 1
    return EventCoverage(
        hanchan=len(corpus.games),
        rounds=rounds,
        total_checkpoints=sum(
            len(raw_round.checkpoints)
            for game in corpus.games
            for raw_round in game.rounds
        ),
        stable_turn_anchors=anchors,
        opponent_rows=anchors * 3,
        discard=counts["discard"],
        tsumogiri=counts["tsumogiri"],
        tedashi=counts["tedashi"],
        riichi_declaration=counts["riichi_declaration"],
        riichi_established=counts["riichi_established"],
        chi=counts[PublicMeldType.CHI.value],
        pon=counts[PublicMeldType.PON.value],
        daiminkan=counts[PublicMeldType.DAIMINKAN.value],
        ankan=counts[PublicMeldType.ANKAN.value],
        kakan=counts[PublicMeldType.KAKAN.value],
        rinshan_draw=counts["rinshan_draw"],
        live_wall_draw=counts["live_wall_draw"],
    )


def _anchor_depths(examples: tuple) -> dict[int, int]:
    """canonical sequence keyごとのdepthを、Phase 8と同じ順序規則で割り当てる。"""
    ordered: dict[tuple, list] = {}
    for index, value in enumerate(examples):
        reference = value.example
        key = (reference.game, reference.round_index, reference.viewer_seat)
        ordered.setdefault(key, []).append((reference.checkpoint_index, index))
    depths: dict[int, int] = {}
    for rows in ordered.values():
        for depth, (_checkpoint, index) in enumerate(sorted(rows), start=1):
            depths[index] = depth
    return depths


def _anchor_coverage(partition: DatasetPartition, examples: tuple) -> AnchorCoverage:
    depths = _anchor_depths(examples)
    buckets: Counter[str] = Counter()
    after_call = 0
    after_riichi = 0
    rows_open = 0
    rows_closed = 0
    tenpai = 0
    non_tenpai = 0
    unavailable = 0
    for index, value in enumerate(examples):
        buckets[depth_bucket(depths[index])] += 1
        anchor = value.sample.anchor
        # player-safe: anchorがfreezeしたevidence prefixだけを読む。
        if any(
            isinstance(evidence, (MeldCalledEvidence, KanConfirmedEvidence))
            for evidence in anchor.evidence
        ):
            after_call += 1
        if any(
            isinstance(evidence, RiichiEstablishedEvidence)
            for evidence in anchor.evidence
        ):
            after_riichi += 1
        viewer_index = tuple(Seat).index(anchor.viewer_seat)
        for seat_index, melds in enumerate(anchor.observation.melds):
            if seat_index == viewer_index:
                continue
            if melds.melds:
                rows_open += 1
            else:
                rows_closed += 1
        # training-only diagnostic: featureへは流さない。
        for row in value.sample.labels.structural_waits:
            if row.mask is None:
                unavailable += 1
            elif any(row.mask):
                tenpai += 1
            else:
                non_tenpai += 1
    return AnchorCoverage(
        partition=partition.value,
        anchors=len(examples),
        depth_bucket_counts=tuple((name, buckets[name]) for name in DEPTH_BUCKETS),
        anchors_after_call=after_call,
        anchors_after_riichi=after_riichi,
        opponent_rows_open=rows_open,
        opponent_rows_closed=rows_closed,
        opponent_rows_true_tenpai=tenpai,
        opponent_rows_true_non_tenpai=non_tenpai,
        opponent_rows_wait_unavailable=unavailable,
    )


@dataclass(frozen=True, slots=True)
class PopulationCoverage:
    events: EventCoverage
    partitions: tuple[AnchorCoverage, ...]

    def coverage_value(self) -> dict[str, object]:
        return {
            "events": {
                "hanchan": self.events.hanchan,
                "rounds": self.events.rounds,
                "total_checkpoints": self.events.total_checkpoints,
                "stable_turn_anchors": self.events.stable_turn_anchors,
                "opponent_rows": self.events.opponent_rows,
                "discard": self.events.discard,
                "tsumogiri": self.events.tsumogiri,
                "tedashi": self.events.tedashi,
                "riichi_declaration": self.events.riichi_declaration,
                "riichi_established": self.events.riichi_established,
                "chi": self.events.chi,
                "pon": self.events.pon,
                "daiminkan": self.events.daiminkan,
                "ankan": self.events.ankan,
                "kakan": self.events.kakan,
                "rinshan_draw": self.events.rinshan_draw,
                "live_wall_draw": self.events.live_wall_draw,
            },
            "absent_event_strata": list(self.events.absent_event_strata),
            "depth_bucket_labels": dict(DEPTH_BUCKET_LABELS),
            "partitions": {
                value.partition: {
                    "anchors": value.anchors,
                    "depth_bucket_counts": dict(value.depth_bucket_counts),
                    "anchors_after_call": value.anchors_after_call,
                    "anchors_after_riichi": value.anchors_after_riichi,
                    "opponent_rows_open": value.opponent_rows_open,
                    "opponent_rows_closed": value.opponent_rows_closed,
                    "opponent_rows_true_tenpai": value.opponent_rows_true_tenpai,
                    "opponent_rows_true_non_tenpai": (
                        value.opponent_rows_true_non_tenpai
                    ),
                    "opponent_rows_wait_unavailable": (
                        value.opponent_rows_wait_unavailable
                    ),
                }
                for value in self.partitions
            },
        }


def measure_population_coverage(
    corpus: RawCorpus, examples: tuple
) -> PopulationCoverage:
    """raw corpusとmaterialized development examplesからcoverageを集計する。

    `examples`はdataset canonical順のTRAIN / VALIDATION materialized例である。
    TEST partitionはStage 3 datasetに存在しないため、混入をfail closedする。
    """
    if not isinstance(corpus, RawCorpus):
        raise TypeError("corpus must be a RawCorpus")
    if not examples:
        raise ValueError("coverage measurement requires materialized examples")
    if any(value.example.partition is DatasetPartition.TEST for value in examples):
        raise ValueError("Stage 3 coverage rejects TEST partition examples")
    partitions = tuple(
        _anchor_coverage(
            partition,
            tuple(value for value in examples if value.example.partition is partition),
        )
        for partition in (DatasetPartition.TRAIN, DatasetPartition.VALIDATION)
    )
    if any(value.anchors == 0 for value in partitions):
        raise ValueError("Stage 3 coverage requires TRAIN and VALIDATION anchors")
    return PopulationCoverage(
        events=_event_coverage(corpus, len(examples)), partitions=partitions
    )


__all__ = [
    "DEPTH_BUCKET_LABELS",
    "AnchorCoverage",
    "EventCoverage",
    "PopulationCoverage",
    "measure_population_coverage",
]
