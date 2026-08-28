"""Phase 3 bootstrap corpus testsのsmall deterministic fixture。"""

from dataclasses import replace
from functools import cache

from _phase2_anchor_fixtures import halt_at_turn_anchor
from lisjong_engine.rules import RuleSet

from lisjong_arena.phase2_training_anchor.extraction import (
    FIRST_PARTY_SOURCE_CLASS,
    Phase2AnchorRecorder,
    Phase2GameExtraction,
)
from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    SourceRevisions,
    TrainingPipelineProvenance,
)
from lisjong_arena.phase2_training_anchor.player_safe_anchor import AnchorSourceIdentity
from lisjong_arena.phase2_training_anchor.training_sample import TrainingSample
from lisjong_arena.phase3_bootstrap_corpus.artifact import FIXED_SEEDS


@cache
def _base_sample() -> TrainingSample:
    halted = halt_at_turn_anchor(FIXED_SEEDS[0])
    source = AnchorSourceIdentity(
        source_class=FIRST_PARTY_SOURCE_CLASS,
        game_seed=FIXED_SEEDS[0],
    )
    recorder = Phase2AnchorRecorder(halted.match_state, source)
    recorder.observe(halted.observation)
    return recorder.samples[0]


def resolved_provenance(
    *,
    lisjong: str | None = "1" * 40,
    lisjong_engine: str | None = "2" * 40,
    lisjong_arena: str | None = "3" * 40,
) -> TrainingPipelineProvenance:
    base = _base_sample().provenance
    return replace(
        base,
        source_revisions=SourceRevisions(
            lisjong=lisjong,
            lisjong_engine=lisjong_engine,
            lisjong_arena=lisjong_arena,
        ),
    )


def sample_for_seed(seed: int, provenance: TrainingPipelineProvenance) -> TrainingSample:
    base = _base_sample()
    source = AnchorSourceIdentity(
        source_class=FIRST_PARTY_SOURCE_CLASS,
        game_seed=seed,
    )
    anchor = replace(base.anchor, source=source, anchor_index=0)
    labels = replace(
        base.labels,
        anchor_identity=replace(base.labels.anchor_identity, game_seed=seed),
    )
    return TrainingSample(anchor=anchor, labels=labels, provenance=provenance)


def fixed_extractions(
    provenance: TrainingPipelineProvenance | None = None,
) -> tuple[Phase2GameExtraction, ...]:
    provenance = provenance or resolved_provenance()
    return tuple(
        Phase2GameExtraction(
            source=AnchorSourceIdentity(
                source_class=FIRST_PARTY_SOURCE_CLASS,
                game_seed=seed,
            ),
            total_decisions=1,
            turn_anchors=1,
            samples=(sample_for_seed(seed, provenance),),
        )
        for seed in FIXED_SEEDS
    )


def normalized_default_rules() -> str:
    from lisjong_arena.phase2_training_anchor.rule_provenance import (
        normalize_effective_rules,
    )

    return normalize_effective_rules(RuleSet.default())
