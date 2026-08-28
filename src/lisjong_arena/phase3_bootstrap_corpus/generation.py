"""Fixed Phase 3 first-party bootstrap generation orchestration。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from lisjong_engine.rules import RuleSet

from lisjong_arena.phase2_training_anchor.extraction import extract_phase2_game
from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    collect_pipeline_provenance,
)
from lisjong_arena.phase2_training_anchor.rule_provenance import normalize_effective_rules
from lisjong_arena.phase3_bootstrap_corpus.artifact import (
    FIXED_SEEDS,
    CorpusCounts,
    Phase3BootstrapArtifactError,
    build_phase3_bootstrap_value,
    save_phase3_bootstrap_corpus,
)


@dataclass(frozen=True, slots=True)
class Phase3GenerationReport:
    """1回のfixed generationのdeterministic counts + run-local measurement。"""

    output_path: str
    counts: CorpusCounts
    wall_clock_seconds: float
    artifact_bytes: int
    canonical_sha256: str

    def __post_init__(self) -> None:
        if not self.output_path:
            raise ValueError("output_path must not be empty")
        if type(self.wall_clock_seconds) is not float or self.wall_clock_seconds < 0:
            raise ValueError("wall_clock_seconds must be a non-negative float")
        if type(self.artifact_bytes) is not int or self.artifact_bytes <= 0:
            raise ValueError("artifact_bytes must be a positive int")
        if type(self.canonical_sha256) is not str or len(self.canonical_sha256) != 64:
            raise ValueError("canonical_sha256 must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class Phase3RepeatReport:
    """同一fixed specを別pathへ2回生成したreproducibility check結果。"""

    first: Phase3GenerationReport
    second: Phase3GenerationReport

    def __post_init__(self) -> None:
        if self.first.canonical_sha256 != self.second.canonical_sha256:
            raise Phase3BootstrapArtifactError(
                "repeat generation canonical SHA-256 mismatch"
            )
        if self.first.counts != self.second.counts:
            raise Phase3BootstrapArtifactError("repeat generation counts mismatch")


def _require_resolved_provenance(provenance) -> None:
    if not provenance.source_revisions.fully_resolved:
        raise Phase3BootstrapArtifactError(
            "Phase 3 persistent generation requires fully resolved lisjong, "
            "lisjong-engine and lisjong-arena revisions"
        )


def generate_phase3_bootstrap_corpus(path: str | Path) -> Phase3GenerationReport:
    """parent #26でlockされた8半荘だけを生成・保存・strict readbackする。"""
    started = time.perf_counter()
    rules = RuleSet.default()
    provenance = collect_pipeline_provenance(rules)
    # VCS metadataが解決できない環境では、8半荘を回す前にfail closedする。
    _require_resolved_provenance(provenance)
    extractions = tuple(
        extract_phase2_game(seed, rules=rules)
        for seed in FIXED_SEEDS
    )
    value = build_phase3_bootstrap_value(
        extractions,
        provenance,
        normalize_effective_rules(rules),
    )
    readback = save_phase3_bootstrap_corpus(value, path)
    elapsed = float(time.perf_counter() - started)
    return Phase3GenerationReport(
        output_path=str(Path(path)),
        counts=readback.counts,
        wall_clock_seconds=elapsed,
        artifact_bytes=readback.artifact_bytes,
        canonical_sha256=readback.canonical_sha256,
    )


def generate_phase3_reproducibility_check(
    first_path: str | Path,
    second_path: str | Path,
) -> Phase3RepeatReport:
    """fixed generationを2回実行し、canonical digestとcounts一致を要求する。"""
    return Phase3RepeatReport(
        first=generate_phase3_bootstrap_corpus(first_path),
        second=generate_phase3_bootstrap_corpus(second_path),
    )


__all__ = [
    "Phase3GenerationReport",
    "Phase3RepeatReport",
    "generate_phase3_bootstrap_corpus",
    "generate_phase3_reproducibility_check",
]
