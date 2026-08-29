"""Recomputable Phase 4 raw population and storage measurements."""

from collections import Counter, defaultdict
from dataclasses import dataclass

from lisjong_engine.public_state import PublicMeldType
from lisjong_engine.round_evidence import (
    DiscardEvidence,
    DoraIndicatorRevealedEvidence,
    DrawEvidence,
    KanConfirmedEvidence,
    MeldCalledEvidence,
    ResponseEpochClosedEvidence,
    ResponseEpochOpenedEvidence,
    RiichiDeclaredEvidence,
)
from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.derivation import derive_turn_samples
from lisjong_arena.phase4_raw_corpus.model import RawCorpus
from lisjong_arena.phase4_raw_corpus.persistence import PersistedRawCorpus

PHASE3_BASELINE_UNCOMPRESSED_BYTES = 68_297_303
PHASE3_BASELINE_TURN_SAMPLES = 4_010
PHASE3_BASELINE_PREFIX_OCCURRENCES = 413_582


def _counter_tuple(counter: Counter) -> tuple[tuple[str, int], ...]:
    return tuple(sorted((str(key), value) for key, value in counter.items()))


@dataclass(frozen=True, slots=True)
class RawCorpusMeasurements:
    hanchan_count: int
    round_count: int
    total_checkpoints: int
    checkpoints_by_kind: tuple[tuple[str, int], ...]
    derived_turn_samples: int
    expected_count_derivable: int
    structural_wait_available: int
    structural_wait_unavailable: int
    unavailable_reasons: tuple[tuple[str, int], ...]
    evidence_items_by_viewer: tuple[tuple[str, int], ...]
    evidence_types_by_viewer: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    structural_response_epochs: int
    response_outcomes: tuple[tuple[str, int], ...]
    discard_count: int
    riichi_count: int
    call_count: int
    kan_count: int
    dora_reveal_count: int
    uncompressed_bytes: int | None
    compressed_bytes: int | None

    @property
    def compression_ratio(self) -> float | None:
        if self.uncompressed_bytes is None or self.compressed_bytes is None:
            return None
        return self.compressed_bytes / self.uncompressed_bytes

    @property
    def uncompressed_bytes_per_hanchan(self) -> float | None:
        if self.uncompressed_bytes is None:
            return None
        return self.uncompressed_bytes / self.hanchan_count

    @property
    def uncompressed_bytes_per_checkpoint(self) -> float | None:
        if self.uncompressed_bytes is None:
            return None
        return self.uncompressed_bytes / self.total_checkpoints

    @property
    def phase3_byte_reduction(self) -> int | None:
        if self.uncompressed_bytes is None:
            return None
        return PHASE3_BASELINE_UNCOMPRESSED_BYTES - self.uncompressed_bytes

    @property
    def phase3_byte_reduction_percentage(self) -> float | None:
        reduction = self.phase3_byte_reduction
        if reduction is None:
            return None
        return 100 * reduction / PHASE3_BASELINE_UNCOMPRESSED_BYTES


def measure_raw_corpus(
    corpus: RawCorpus, persisted: PersistedRawCorpus | None = None
) -> RawCorpusMeasurements:
    if not isinstance(corpus, RawCorpus):
        raise TypeError("corpus must be a RawCorpus")
    checkpoint_counts = Counter()
    evidence_by_viewer: dict[Seat, Counter] = defaultdict(Counter)
    outcomes_by_viewer: dict[Seat, Counter] = defaultdict(Counter)
    daiminkan_by_viewer: Counter = Counter()
    for game in corpus.games:
        for raw_round in game.rounds:
            for checkpoint in raw_round.checkpoints:
                checkpoint_counts[checkpoint.decision_kind.value] += 1
            for stream in raw_round.viewer_evidence:
                for evidence in stream.evidence:
                    evidence_by_viewer[stream.viewer_seat][type(evidence).__name__] += 1
                    if isinstance(evidence, ResponseEpochClosedEvidence):
                        outcomes_by_viewer[stream.viewer_seat][
                            evidence.outcome.value
                        ] += 1
                    if (
                        isinstance(evidence, MeldCalledEvidence)
                        and evidence.meld.meld_type is PublicMeldType.DAIMINKAN
                    ):
                        daiminkan_by_viewer[stream.viewer_seat] += 1
    globally_public_types = {
        evidence_type
        for evidence_type in set().union(
            *(set(counter) for counter in evidence_by_viewer.values())
        )
        if evidence_type != DrawEvidence.__name__
    }
    for evidence_type in globally_public_types:
        counts = tuple(evidence_by_viewer[viewer][evidence_type] for viewer in Seat)
        if len(set(counts)) != 1:
            raise ValueError(
                f"globally-public evidence count differs by viewer: {evidence_type}"
            )
    if len({tuple(sorted(outcomes_by_viewer[viewer].items())) for viewer in Seat}) != 1:
        raise ValueError("response outcome counts differ by viewer")
    samples = derive_turn_samples(corpus)
    unavailable = Counter(
        row.unavailable_reason.value
        for sample in samples
        for row in sample.labels.structural_waits
        if row.unavailable_reason is not None
    )
    available_count = sum(
        row.is_available for sample in samples for row in sample.labels.structural_waits
    )
    first_viewer = tuple(Seat)[0]
    representative = evidence_by_viewer[first_viewer]
    uncompressed = (
        None
        if persisted is None
        else sum(shard.uncompressed_bytes for shard in persisted.shards)
    )
    compressed = (
        None
        if persisted is None
        else sum(shard.compressed_bytes for shard in persisted.shards)
    )
    return RawCorpusMeasurements(
        hanchan_count=len(corpus.games),
        round_count=sum(len(game.rounds) for game in corpus.games),
        total_checkpoints=sum(checkpoint_counts.values()),
        checkpoints_by_kind=_counter_tuple(checkpoint_counts),
        derived_turn_samples=len(samples),
        expected_count_derivable=sum(
            len(sample.labels.expected_counts) for sample in samples
        ),
        structural_wait_available=available_count,
        structural_wait_unavailable=sum(unavailable.values()),
        unavailable_reasons=_counter_tuple(unavailable),
        evidence_items_by_viewer=tuple(
            (viewer.value, sum(evidence_by_viewer[viewer].values())) for viewer in Seat
        ),
        evidence_types_by_viewer=tuple(
            (viewer.value, _counter_tuple(evidence_by_viewer[viewer]))
            for viewer in Seat
        ),
        structural_response_epochs=representative[ResponseEpochOpenedEvidence.__name__],
        response_outcomes=_counter_tuple(outcomes_by_viewer[first_viewer]),
        discard_count=representative[DiscardEvidence.__name__],
        riichi_count=representative[RiichiDeclaredEvidence.__name__],
        call_count=representative[MeldCalledEvidence.__name__],
        kan_count=(
            representative[KanConfirmedEvidence.__name__]
            + daiminkan_by_viewer[first_viewer]
        ),
        dora_reveal_count=representative[DoraIndicatorRevealedEvidence.__name__],
        uncompressed_bytes=uncompressed,
        compressed_bytes=compressed,
    )


__all__ = [
    "PHASE3_BASELINE_PREFIX_OCCURRENCES",
    "PHASE3_BASELINE_TURN_SAMPLES",
    "PHASE3_BASELINE_UNCOMPRESSED_BYTES",
    "RawCorpusMeasurements",
    "measure_raw_corpus",
]
