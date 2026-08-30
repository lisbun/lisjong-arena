"""Phase 4 deduplicated first-party HandBelief raw corpus."""

from .derivation import (
    derive_player_safe_anchor,
    derive_training_labels,
    derive_turn_samples,
    derive_turn_samples_from_game,
)
from .extraction import Phase4RawRecorder, extract_phase4_raw_game, phase4_provenance
from .generation import (
    Phase4GenerationReport,
    generate_phase4_raw_corpus,
    generate_phase4_raw_corpus_for_seeds,
)
from .measurements import RawCorpusMeasurements, measure_raw_corpus
from .model import (
    FIXED_SEEDS,
    GENERATION_PROTOCOL_ID,
    MAX_GAMES_PER_SHARD,
    SCHEMA_VERSION,
    CheckpointTruth,
    DecisionCheckpoint,
    OpponentConcealedTruth,
    Phase4RawCorpusError,
    RawCorpus,
    RawGame,
    RawRound,
    ViewerEvidence,
)
from .persistence import (
    MANIFEST_FILENAME,
    PersistedRawCorpus,
    ShardInfo,
    corpus_identity,
    load_raw_corpus,
    save_raw_corpus,
)

__all__ = [
    "FIXED_SEEDS",
    "GENERATION_PROTOCOL_ID",
    "MANIFEST_FILENAME",
    "MAX_GAMES_PER_SHARD",
    "SCHEMA_VERSION",
    "CheckpointTruth",
    "DecisionCheckpoint",
    "OpponentConcealedTruth",
    "Phase4RawCorpusError",
    "Phase4GenerationReport",
    "Phase4RawRecorder",
    "PersistedRawCorpus",
    "RawCorpus",
    "RawCorpusMeasurements",
    "RawGame",
    "RawRound",
    "ShardInfo",
    "ViewerEvidence",
    "corpus_identity",
    "derive_player_safe_anchor",
    "derive_training_labels",
    "derive_turn_samples",
    "derive_turn_samples_from_game",
    "extract_phase4_raw_game",
    "generate_phase4_raw_corpus",
    "generate_phase4_raw_corpus_for_seeds",
    "load_raw_corpus",
    "measure_raw_corpus",
    "phase4_provenance",
    "save_raw_corpus",
]
