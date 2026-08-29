"""Fixed Issue #85 generation, strict readback and Phase 2 equality orchestration."""

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from lisjong_engine.rules import RuleSet

from lisjong_arena.phase2_training_anchor.extraction import extract_phase2_game
from lisjong_arena.phase4_raw_corpus.derivation import derive_turn_samples
from lisjong_arena.phase4_raw_corpus.extraction import (
    extract_phase4_raw_game,
    phase4_provenance,
)
from lisjong_arena.phase4_raw_corpus.measurements import (
    RawCorpusMeasurements,
    measure_raw_corpus,
)
from lisjong_arena.phase4_raw_corpus.model import FIXED_SEEDS, RawCorpus
from lisjong_arena.phase4_raw_corpus.persistence import (
    PersistedRawCorpus,
    load_raw_corpus,
    save_raw_corpus,
)


@dataclass(frozen=True, slots=True)
class Phase4GenerationReport:
    persisted: PersistedRawCorpus
    measurements: RawCorpusMeasurements
    generation_seconds: float
    readback_seconds: float
    derivation_seconds: float
    failure_count: int = 0
    phase2_equality_verified: bool = True

    def __post_init__(self) -> None:
        if self.failure_count != 0:
            raise ValueError("a successful fail-closed generation has zero failures")
        if not self.phase2_equality_verified:
            raise ValueError("successful generation requires Phase 2 equality")

    @property
    def seconds_per_hanchan(self) -> float:
        return self.generation_seconds / self.measurements.hanchan_count

    @property
    def derived_samples_per_second(self) -> float:
        return self.measurements.derived_turn_samples / self.derivation_seconds

    @property
    def uncompressed_decode_bytes_per_second(self) -> float:
        value = self.measurements.uncompressed_bytes
        if value is None:
            raise RuntimeError("persisted byte measurements are unavailable")
        return value / self.readback_seconds


def generate_phase4_raw_corpus(destination: str | Path) -> Phase4GenerationReport:
    """Run the fixed 1000..1007 protocol; caller cannot vary its identity."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    rules = RuleSet.default()
    provenance = phase4_provenance(rules)
    started = perf_counter()
    games = tuple(extract_phase4_raw_game(seed, rules=rules) for seed in FIXED_SEEDS)
    generation_seconds = perf_counter() - started
    corpus = RawCorpus(provenance, games)
    temporary_destination = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.validation-", dir=destination.parent
        )
    )
    temporary_destination.rmdir()
    owns_temporary_destination = False
    try:
        save_raw_corpus(corpus, temporary_destination)
        owns_temporary_destination = True
        read_started = perf_counter()
        readback = load_raw_corpus(temporary_destination)
        readback_seconds = perf_counter() - read_started
        derive_started = perf_counter()
        derived = derive_turn_samples(readback.corpus)
        derivation_seconds = perf_counter() - derive_started
        direct = tuple(
            sample
            for seed in FIXED_SEEDS
            for sample in extract_phase2_game(seed, rules=rules).samples
        )
        if derived != direct:
            raise RuntimeError("Phase 4 persisted TURN derivation differs from Phase 2")
        measurements = measure_raw_corpus(readback.corpus, readback)
        report = Phase4GenerationReport(
            persisted=readback,
            measurements=measurements,
            generation_seconds=generation_seconds,
            readback_seconds=readback_seconds,
            derivation_seconds=derivation_seconds,
        )
        if destination.exists():
            raise FileExistsError(f"destination already exists: {destination}")
        os.rename(temporary_destination, destination)
        owns_temporary_destination = False
        return report
    finally:
        if owns_temporary_destination and temporary_destination.exists():
            shutil.rmtree(temporary_destination)


__all__ = ["Phase4GenerationReport", "generate_phase4_raw_corpus"]
