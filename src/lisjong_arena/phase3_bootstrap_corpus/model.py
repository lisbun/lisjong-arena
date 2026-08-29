"""Phase 3 first-party bootstrap artifact identities and immutable summaries。"""

from dataclasses import dataclass

from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    TrainingPipelineProvenance,
)

SCHEMA_VERSION = 1
GENERATION_PROTOCOL = "phase3-first-party-bootstrap-v1"
FIXED_SEEDS = tuple(range(1000, 1008))
FIXED_EXECUTION = "lisjong-engine.run_hanchan"
FIXED_POLICY = "TwoStepUkeirePolicy"
FIXED_POLICY_SEAT_COUNT = 4
FIXED_RULES = "RuleSet.default()"
FIXED_ANCHOR = "turn-pre-action"
FIXED_SAMPLE_CONTRACT = "phase2.TrainingSample"


class Phase3BootstrapArtifactError(ValueError):
    """Phase 3 bootstrap artifactの生成・検証に失敗した場合。"""


@dataclass(frozen=True, slots=True)
class CorpusCounts:
    """canonical corpusから再計算できるdeterministic coverage counts。

    evidence系は各sampleにfreezeされたordered evidence prefix内での出現数であり、
    game eventのunique countではない。
    """

    hanchan_count: int
    total_decisions: int
    sample_count: int
    samples_per_hanchan: float
    expected_count_sample_count: int
    structural_wait_available_count: int
    structural_wait_unavailable_count: int
    structural_wait_unavailable_reasons: tuple[tuple[str, int], ...]
    evidence_item_prefix_occurrences: int
    riichi_evidence_prefix_occurrences: int
    call_evidence_prefix_occurrences: int
    kan_evidence_prefix_occurrences: int
    response_epoch_evidence_prefix_occurrences: int
    non_action_response_evidence_prefix_occurrences: int

    def __post_init__(self) -> None:
        integer_fields = (
            "hanchan_count",
            "total_decisions",
            "sample_count",
            "expected_count_sample_count",
            "structural_wait_available_count",
            "structural_wait_unavailable_count",
            "evidence_item_prefix_occurrences",
            "riichi_evidence_prefix_occurrences",
            "call_evidence_prefix_occurrences",
            "kan_evidence_prefix_occurrences",
            "response_epoch_evidence_prefix_occurrences",
            "non_action_response_evidence_prefix_occurrences",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        if self.hanchan_count <= 0:
            raise ValueError("hanchan_count must be positive")
        if type(self.samples_per_hanchan) is not float:
            raise TypeError("samples_per_hanchan must be a float")
        if self.samples_per_hanchan != self.sample_count / self.hanchan_count:
            raise ValueError("samples_per_hanchan is inconsistent")
        if self.expected_count_sample_count != self.sample_count:
            raise ValueError("every sample must carry expected-count labels")
        if (
            self.structural_wait_available_count
            + self.structural_wait_unavailable_count
            != self.sample_count * 3
        ):
            raise ValueError("structural-wait row counts are inconsistent")
        if type(self.structural_wait_unavailable_reasons) is not tuple:
            raise TypeError("structural_wait_unavailable_reasons must be a tuple")
        if self.structural_wait_unavailable_reasons != tuple(
            sorted(self.structural_wait_unavailable_reasons)
        ):
            raise ValueError("structural-wait reason counts must be sorted")
        if (
            sum(count for _, count in self.structural_wait_unavailable_reasons)
            != self.structural_wait_unavailable_count
        ):
            raise ValueError("structural-wait reason counts are inconsistent")
        if any(
            type(count) is not int or count <= 0
            for _, count in self.structural_wait_unavailable_reasons
        ):
            raise ValueError("structural-wait reason counts must be positive ints")


@dataclass(frozen=True, slots=True)
class ValidatedBootstrapCorpus:
    """strict readback済みartifactのvalidation-only summary。"""

    schema_version: int
    generation_protocol: str
    game_seeds: tuple[int, ...]
    provenance: TrainingPipelineProvenance
    counts: CorpusCounts
    canonical_sha256: str
    artifact_bytes: int
