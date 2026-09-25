"""Issue #375 result artifact and bundle-level strict verification.

```text
<bundle>/
  candidate-lock.json     pre-execution lock
  comparison.json         既存 ComparisonArtifact そのもの
  candidate-result.json   このmoduleが所有するpurpose-specific result
```

verifyはrecorded statisticsを信用せず、strict-readした``comparison.json``の
raw seat-resultsからprimary / interval / diagnostics / classificationを再導出し、
recorded documentとの完全一致だけを成功条件とする。
"""

from __future__ import annotations

from pathlib import Path

from lisjong_arena._artifact_io import (
    ArtifactValidationError,
    canonical_json_text,
    expect_int,
    expect_object,
    expect_str,
    read_json_document,
    write_new_artifact_file,
)
from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
    require_new_artifact_destinations,
)
from lisjong_arena.artifact import (
    ComparisonArtifact,
    ComparisonArtifactError,
    load_comparison_artifact,
)
from lisjong_arena.paired_evaluation import artifact_file_digest

from .lock import (
    HeuristicCandidateLockError,
    document_identity,
    load_lock_document,
    locked_participants,
    locked_seeds,
    parse_lock_document,
    require_comparison_provenance,
)
from .protocol import (
    CANDIDATE_ROLE,
    INCUMBENT_ROLE,
    HeuristicCandidateProtocolError,
    protocol_document,
)
from .statistics import (
    HeuristicCandidateStatisticsError,
    block_sign_counts,
    classify,
    derive_secondary_diagnostics,
    derive_seed_blocks,
    summarize_seed_blocks,
)

RESULT_VERSION = 1

_RESULT_FIELDS = {
    "classification",
    "comparison",
    "lock_identity",
    "participants",
    "primary",
    "protocol",
    "result_identity",
    "result_version",
    "secondary_diagnostics",
}


class HeuristicCandidateResultError(ValueError):
    """result artifactまたはbundleが不正な場合(``STOP / INVALID``)。"""


def _comparison_document(
    artifact: ComparisonArtifact, path: str | Path
) -> dict[str, object]:
    provenance = artifact.provenance
    return {
        "artifact_digest": artifact_file_digest(path),
        "comparison_protocol": artifact.comparison_protocol,
        "provenance": {
            "execution_environment": provenance.execution_environment,
            "lisjong_arena_version": provenance.lisjong_arena_version,
            "lisjong_revision": provenance.lisjong_revision,
            "lisjong_version": provenance.lisjong_version,
            "python_version": provenance.python_version,
            "riichienv_version": provenance.riichienv_version,
        },
        "schema_version": artifact.schema_version,
    }


def build_candidate_result(
    *,
    lock_document: dict[str, object],
    comparison_artifact: ComparisonArtifact,
    comparison_artifact_path: str | Path,
) -> dict[str, object]:
    """lockとraw comparison evidenceからresult documentを構築する。"""
    lock = parse_lock_document(lock_document)
    candidate, incumbent = locked_participants(lock)
    seeds = locked_seeds(lock)
    require_comparison_provenance(lock, comparison_artifact.provenance)
    blocks = derive_seed_blocks(
        comparison_artifact, candidate=candidate, incumbent=incumbent, seeds=seeds
    )
    summary = summarize_seed_blocks(blocks)
    diagnostics = derive_secondary_diagnostics(
        comparison_artifact, candidate=candidate, incumbent=incumbent
    )
    payload: dict[str, object] = {
        "classification": classify(summary),
        "comparison": _comparison_document(
            comparison_artifact, comparison_artifact_path
        ),
        "lock_identity": lock["lock_identity"],
        "participants": {
            CANDIDATE_ROLE: candidate.to_document(),
            INCUMBENT_ROLE: incumbent.to_document(),
        },
        "primary": {
            "block_sign_counts": block_sign_counts(blocks),
            "seed_blocks": [block.to_document() for block in blocks],
            "summary": summary.to_document(),
        },
        "protocol": protocol_document(seeds),
        "result_version": RESULT_VERSION,
        "secondary_diagnostics": diagnostics.to_document(),
    }
    document = dict(payload)
    document["result_identity"] = document_identity(payload)
    return document


def save_candidate_result(document: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    try:
        require_new_artifact_destinations(
            {"candidate_result": destination}, required_names=("candidate_result",)
        )
    except ExecutionSafetyError as exc:
        raise HeuristicCandidateResultError(str(exc)) from exc
    write_new_artifact_file(destination, canonical_json_text(document))
    return destination


def load_candidate_result(path: str | Path) -> dict[str, object]:
    """result artifactをstrict-readし、schemaとresult identityを検証する。"""
    try:
        raw = expect_object(
            read_json_document(Path(path)), _RESULT_FIELDS, "candidate_result"
        )
        if (
            expect_int(raw["result_version"], "candidate_result.result_version")
            != RESULT_VERSION
        ):
            raise HeuristicCandidateResultError("unsupported result version")
        payload = {key: raw[key] for key in raw if key != "result_identity"}
        if expect_str(
            raw["result_identity"], "candidate_result.result_identity"
        ) != document_identity(payload):
            raise HeuristicCandidateResultError("result identity mismatch")
    except HeuristicCandidateResultError:
        raise
    except (ArtifactValidationError, OSError, TypeError, ValueError) as exc:
        raise HeuristicCandidateResultError(str(exc)) from exc
    return dict(raw)


def load_bundle_comparison(path: str | Path) -> ComparisonArtifact:
    try:
        return load_comparison_artifact(path)
    except ComparisonArtifactError as exc:
        raise HeuristicCandidateResultError(str(exc)) from exc


def verify_candidate_bundle(
    *,
    lock_path: str | Path,
    comparison_path: str | Path,
    result_path: str | Path,
) -> dict[str, object]:
    """bundle全体をraw evidenceからの再導出でstrictに検証する。"""
    try:
        lock = load_lock_document(lock_path)
    except HeuristicCandidateLockError as exc:
        raise HeuristicCandidateResultError(str(exc)) from exc
    comparison = load_bundle_comparison(comparison_path)
    document = load_candidate_result(result_path)
    try:
        expected = build_candidate_result(
            lock_document=lock,
            comparison_artifact=comparison,
            comparison_artifact_path=comparison_path,
        )
    except (
        HeuristicCandidateLockError,
        HeuristicCandidateProtocolError,
        HeuristicCandidateStatisticsError,
    ) as exc:
        raise HeuristicCandidateResultError(str(exc)) from exc
    if document != expected:
        raise HeuristicCandidateResultError(
            "candidate result differs from the value re-derived from the locked "
            "comparison evidence"
        )
    return document


__all__ = [
    "RESULT_VERSION",
    "HeuristicCandidateResultError",
    "build_candidate_result",
    "load_bundle_comparison",
    "load_candidate_result",
    "save_candidate_result",
    "verify_candidate_bundle",
]
