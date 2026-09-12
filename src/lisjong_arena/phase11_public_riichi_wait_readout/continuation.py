"""Result-only technical continuation of the executed Arena #172 run (#209).

The #172 one-shot execution already completed retained readback, the
pre-result execution lock, the coverage gate, and the fixed readout training.
Only result exposure failed, inside an aggregate-consistency validator whose
fixed absolute tolerance was not numerically stable. This module exposes the
evidence that the completed run already produced, exactly once, and nothing
else.

It is deliberately not a bypass flag on the ordinary ``evaluate`` command. It
binds the immutable #172 artifacts by byte digest and logical identity, it
verifies the *original* locked dependency revisions rather than whatever the
current Arena main happens to pin, and it separates the two revisions that must
never be confused:

```text
scientific_execution_revision    the fixed #172 Arena revision
technical_continuation_revision  the merged #209 repair revision
```

There is no training here. This module does not import ``.training``, creates
no optimizer, selects no checkpoint, runs no epoch, and never rewrites the
original ``execution-lock.json``, ``coverage.json``, or readout model.
"""

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from lisjong_engine.rules import RuleSet

from lisjong_arena._execution_safety import (
    require_clean_arena_head,
    require_merged_arena_revision,
    require_new_artifact_destinations,
)
from lisjong_arena.phase4_raw_corpus.extraction import phase4_provenance
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.stage3_mix_pilot.generation import _provenance_value

from .artifact import (
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
    load_lock,
    load_model,
    load_result,
    save_result,
    validate_model_manifest,
)
from .coverage import build_coverage, load_coverage
from .data import frozen_latent_records, partition_records
from .evaluation import evaluate_readout, metrics_from_evidence, paired_comparison
from .lock import RUNTIME_FIELDS, _runtime, validate_runtime
from .protocol import (
    COVERAGE_MINIMUM_HANCHAN,
    ENGINE_REVISION,
    FORMAL_TRAINING_CONFIG,
    LISJONG_REVISION,
    ROLE,
    SCHEMA,
    Phase11Error,
    canonical_json_bytes,
    digest,
    exact,
    identity,
)
from .result import assemble_result
from .retained import load_retained

LOCK_FILENAME = "execution-lock.json"
COVERAGE_FILENAME = "coverage.json"
MODEL_DIRNAME = "readout-model"

CONTINUATION_PURPOSE = "TECHNICAL RESULT-ONLY CONTINUATION"
CONTINUATION_BRANCH = "main"
SCIENTIFIC_EXECUTION_REVISION = "93963d85f6201c714cb4fcf39d59e9e09c85766d"

RECEIPT_FIELDS = (
    "schema",
    "role",
    "purpose",
    "scientific_execution_revision",
    "technical_continuation_revision",
    "execution_lock_identity",
    "coverage_file_sha256",
    "coverage_identity",
    "model_manifest_sha256",
    "readout_weights_sha256",
    "selected_epoch",
    "epochs_run",
    "frozen_e160_digest",
    "locked_runtime",
    "continuation_runtime",
    "locked_source_revisions",
    "continuation_source_revisions",
    "result_identity",
    "continuation_audit",
    "retrained",
    "resumed",
    "checkpoint_reselected",
)


@dataclass(frozen=True, slots=True)
class ImmutableArtifactBinding:
    """Byte and logical identity of the artifacts the #172 run already produced.

    These values are protocol invariants of the continuation, not caller
    options. A continuation that would accept a different artifact is not a
    continuation of #172.
    """

    execution_lock_identity: str
    coverage_file_sha256: str
    coverage_identity: str
    model_manifest_sha256: str
    readout_weights_sha256: str
    selected_epoch: int
    epochs_run: int
    frozen_e160_digest: str


PHASE11_ARTIFACT_BINDING = ImmutableArtifactBinding(
    # The lock file holds exactly ``canonical_json_bytes(lock)``, so its file
    # SHA-256 and its logical identity are the same value by construction.
    execution_lock_identity=(
        "5e08169c96568d692b69070245a8e2a6da61b8b1b5d4a9d8069b5dbe0130ce74"
    ),
    coverage_file_sha256=(
        "12d2c4f2c0b64d22701fc47754b2a5076b41fa18bd940084fc51155b0f4e1dfa"
    ),
    coverage_identity=(
        "6d1cdf7696b9e3d5b1e0bbfc7a13f5b3958c8e839ce6e02420f864be0c32a310"
    ),
    model_manifest_sha256=(
        "618f6d366a738c9cf99da8a5a022d8aae2ade4ba824abad3248a9a862f3dcb09"
    ),
    readout_weights_sha256=(
        "91e8a4db8b8d7a664e22142070e2f581d922be8e373a5bb1d6be1c7c33852948"
    ),
    selected_epoch=121,
    epochs_run=127,
    frozen_e160_digest=(
        "581f4d20138291ea7c6b22508105b2ac2ed40cc3b3668e680376f3b9adf0885e"
    ),
)


@dataclass(frozen=True, slots=True)
class BoundArtifacts:
    """The immutable #172 inputs, already bound to their recorded identities."""

    root: Path
    lock: dict
    coverage: dict
    manifest: dict


def _sha256_file(path: Path, name: str) -> str:
    if not path.is_file():
        raise Phase11Error(f"retained #172 {name} is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bind_immutable_artifacts(root: str | Path) -> BoundArtifacts:
    """Read the executed #172 artifacts and fail closed on any byte drift.

    Every file is hashed before it is interpreted, and each recorded logical
    identity is re-derived through the existing #172 loaders. Nothing here
    writes, renames, or rewrites the artifact root.
    """
    binding = PHASE11_ARTIFACT_BINDING
    directory = Path(root)
    if not directory.is_dir():
        raise Phase11Error(f"retained #172 artifact root is missing: {directory}")
    lock_path = directory / LOCK_FILENAME
    coverage_path = directory / COVERAGE_FILENAME
    model_path = directory / MODEL_DIRNAME
    exact(
        _sha256_file(lock_path, LOCK_FILENAME),
        binding.execution_lock_identity,
        "retained #172 execution lock bytes",
    )
    exact(
        _sha256_file(coverage_path, COVERAGE_FILENAME),
        binding.coverage_file_sha256,
        "retained #172 coverage bytes",
    )
    exact(
        _sha256_file(model_path / MANIFEST_FILENAME, MANIFEST_FILENAME),
        binding.model_manifest_sha256,
        "retained #172 readout manifest bytes",
    )
    exact(
        _sha256_file(model_path / WEIGHTS_FILENAME, WEIGHTS_FILENAME),
        binding.readout_weights_sha256,
        "retained #172 readout weight bytes",
    )
    lock = load_lock(lock_path)
    lock_identity = identity(lock)
    exact(
        lock_identity,
        binding.execution_lock_identity,
        "retained #172 execution lock identity",
    )
    exact(
        lock["provenance"]["source_revisions"]["lisjong_arena"],
        SCIENTIFIC_EXECUTION_REVISION,
        "retained #172 scientific execution revision",
    )
    coverage = load_coverage(coverage_path, lock_identity)
    exact(
        coverage["coverage_identity"],
        binding.coverage_identity,
        "retained #172 coverage identity",
    )
    if not coverage["semantic_valid"]:
        raise Phase11Error("the bound #172 coverage is not semantically valid")
    validation = coverage["partitions"]["validation"]
    if validation["eligible_hanchan"] < COVERAGE_MINIMUM_HANCHAN:
        raise Phase11Error("the bound #172 coverage does not pass the coverage gate")
    manifest_bytes = (model_path / MANIFEST_FILENAME).read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase11Error(
            "retained #172 readout manifest is not valid JSON"
        ) from error
    validate_model_manifest(manifest, lock, coverage["coverage_identity"])
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise Phase11Error("retained #172 readout manifest bytes are not canonical")
    exact(manifest["selected_epoch"], binding.selected_epoch, "#172 selected epoch")
    exact(manifest["epochs_run"], binding.epochs_run, "#172 epochs run")
    exact(
        manifest["weights_sha256"],
        binding.readout_weights_sha256,
        "#172 readout weight digest",
    )
    exact(
        manifest["frozen_digest_before"],
        binding.frozen_e160_digest,
        "#172 frozen E160 digest",
    )
    exact(
        manifest["frozen_digest_after"],
        binding.frozen_e160_digest,
        "#172 frozen E160 digest after training",
    )
    return BoundArtifacts(
        root=directory, lock=lock, coverage=coverage, manifest=manifest
    )


def require_continuation_revision(declared: str) -> str:
    """Pin the repair revision: clean, checked out, merged, and not #172's."""
    revision = digest(declared, "technical continuation revision", 40)
    if revision == SCIENTIFIC_EXECUTION_REVISION:
        raise Phase11Error(
            "the technical continuation revision must be the merged #209 repair "
            "commit, never the fixed #172 scientific execution revision"
        )
    exact(
        require_clean_arena_head(),
        revision,
        "clean Arena HEAD against the technical continuation revision",
    )
    require_merged_arena_revision(revision, branch=CONTINUATION_BRANCH)
    return revision


def require_locked_dependencies(lock: dict, *, continuation_revision: str) -> dict:
    """Require the original #172 dependency revisions, not current Arena main.

    Arena main has since moved its ``lisjong`` pin forward. The continuation
    re-derives the #172 scientific evidence, so it must run against the
    revisions the original execution locked; only the Arena revision itself is
    allowed to differ, and only by being exactly the repair revision.
    """
    import torch

    torch.use_deterministic_algorithms(FORMAL_TRAINING_CONFIG.deterministic_algorithms)
    torch.set_num_threads(FORMAL_TRAINING_CONFIG.torch_threads)
    if torch.cuda.is_available():
        raise Phase11Error("the #172 continuation requires the locked CPU-only runtime")
    runtime = validate_runtime(_runtime(), "continuation runtime")
    locked_runtime = lock["runtime"]
    for field in RUNTIME_FIELDS:
        # ``platform`` records the host string and is reported, not enforced:
        # an OS build number is not part of the #172 scientific contract.
        if field == "platform":
            continue
        exact(runtime[field], locked_runtime[field], f"continuation runtime {field}")
    provenance = _provenance_value(phase4_provenance(RuleSet.default()))
    exact(provenance["fully_resolved"], True, "continuation source revisions")
    revisions = provenance["source_revisions"]
    exact(revisions["lisjong"], LISJONG_REVISION, "continuation lisjong revision")
    exact(revisions["lisjong_engine"], ENGINE_REVISION, "continuation engine revision")
    locked_revisions = lock["provenance"]["source_revisions"]
    for name in ("lisjong", "lisjong_engine"):
        exact(revisions[name], locked_revisions[name], f"locked {name} revision")
    exact(
        revisions["lisjong_arena"],
        continuation_revision,
        "installed continuation Arena revision",
    )
    exact(
        {name: item for name, item in provenance.items() if name != "source_revisions"},
        {
            name: item
            for name, item in lock["provenance"].items()
            if name != "source_revisions"
        },
        "continuation provenance semantics",
    )
    return {"runtime": runtime, "provenance": provenance}


def continuation_receipt(
    *,
    bound: BoundArtifacts,
    continuation_revision: str,
    dependencies: dict,
    result_identity: str,
    continuation_audit: str,
) -> dict[str, object]:
    """Technical provenance sidecar; the #172 result schema stays untouched."""
    if type(continuation_audit) is not str or not continuation_audit.strip():
        raise Phase11Error("the continuation requires a dated operator audit string")
    binding = PHASE11_ARTIFACT_BINDING
    value = {
        "schema": SCHEMA + "/continuation-receipt",
        "role": ROLE,
        "purpose": CONTINUATION_PURPOSE,
        "scientific_execution_revision": SCIENTIFIC_EXECUTION_REVISION,
        "technical_continuation_revision": continuation_revision,
        "execution_lock_identity": binding.execution_lock_identity,
        "coverage_file_sha256": binding.coverage_file_sha256,
        "coverage_identity": binding.coverage_identity,
        "model_manifest_sha256": binding.model_manifest_sha256,
        "readout_weights_sha256": binding.readout_weights_sha256,
        "selected_epoch": binding.selected_epoch,
        "epochs_run": binding.epochs_run,
        "frozen_e160_digest": binding.frozen_e160_digest,
        "locked_runtime": bound.lock["runtime"],
        "continuation_runtime": dependencies["runtime"],
        "locked_source_revisions": bound.lock["provenance"]["source_revisions"],
        "continuation_source_revisions": dependencies["provenance"]["source_revisions"],
        "result_identity": digest(result_identity, "continuation result identity"),
        "continuation_audit": continuation_audit,
        "retrained": False,
        "resumed": False,
        "checkpoint_reselected": False,
    }
    return validate_continuation_receipt(value)


def validate_continuation_receipt(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(RECEIPT_FIELDS):
        raise Phase11Error("continuation receipt fields are not exact")
    binding = PHASE11_ARTIFACT_BINDING
    exact(value["schema"], SCHEMA + "/continuation-receipt", "receipt schema")
    exact(value["role"], ROLE, "receipt role")
    exact(value["purpose"], CONTINUATION_PURPOSE, "receipt purpose")
    exact(
        value["scientific_execution_revision"],
        SCIENTIFIC_EXECUTION_REVISION,
        "receipt scientific execution revision",
    )
    continuation_revision = digest(
        value["technical_continuation_revision"],
        "receipt technical continuation revision",
        40,
    )
    if continuation_revision == SCIENTIFIC_EXECUTION_REVISION:
        raise Phase11Error(
            "the receipt must separate the scientific and continuation revisions"
        )
    for field in (
        "execution_lock_identity",
        "coverage_file_sha256",
        "coverage_identity",
        "model_manifest_sha256",
        "readout_weights_sha256",
        "selected_epoch",
        "epochs_run",
        "frozen_e160_digest",
    ):
        exact(value[field], getattr(binding, field), f"receipt {field}")
    validate_runtime(value["locked_runtime"], "receipt locked runtime")
    validate_runtime(value["continuation_runtime"], "receipt continuation runtime")
    for field in ("locked_source_revisions", "continuation_source_revisions"):
        revisions = value[field]
        if type(revisions) is not dict or set(revisions) != {
            "lisjong",
            "lisjong_engine",
            "lisjong_arena",
        }:
            raise Phase11Error(f"receipt {field} are not exact")
        exact(revisions["lisjong"], LISJONG_REVISION, f"receipt {field} lisjong")
        exact(revisions["lisjong_engine"], ENGINE_REVISION, f"receipt {field} engine")
        digest(revisions["lisjong_arena"], f"receipt {field} Arena revision", 40)
    exact(
        value["locked_source_revisions"]["lisjong_arena"],
        SCIENTIFIC_EXECUTION_REVISION,
        "receipt locked Arena revision",
    )
    exact(
        value["continuation_source_revisions"]["lisjong_arena"],
        continuation_revision,
        "receipt continuation Arena revision",
    )
    digest(value["result_identity"], "receipt result identity")
    audit = value["continuation_audit"]
    if type(audit) is not str or not audit.strip():
        raise Phase11Error("the receipt requires a dated operator audit string")
    for field in ("retrained", "resumed", "checkpoint_reselected"):
        if type(value[field]) is not bool:
            raise Phase11Error(f"receipt {field} must be a JSON boolean")
        exact(value[field], False, f"receipt {field}")
    return value


def save_continuation_receipt(path: str | Path, value: dict) -> Path:
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(f"receipt destination already exists: {destination}")
    validate_continuation_receipt(value)
    payload = dict(value)
    payload["receipt_identity"] = identity(value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(payload))
    return destination


def load_continuation_receipt(path: str | Path) -> dict[str, object]:
    data = Path(path).read_bytes()
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase11Error("continuation receipt is not valid JSON") from error
    if canonical_json_bytes(payload) != data or type(payload) is not dict:
        raise Phase11Error("continuation receipt bytes are not canonical JSON")
    recorded = payload.pop("receipt_identity", None)
    exact(recorded, identity(payload), "continuation receipt identity")
    validate_continuation_receipt(payload)
    payload["receipt_identity"] = recorded
    return payload


def continue_result(
    *,
    artifact_root: str | Path,
    corpus_root: str,
    phase157_root: str,
    phase167_root: str,
    continuation_revision: str,
    continuation_audit: str,
    result_path: str | Path,
    receipt_path: str | Path,
) -> dict[str, object]:
    """Expose the #172 result once, from artifacts that are already fixed."""
    require_new_artifact_destinations(
        {"result": result_path, "receipt": receipt_path},
        required_names=("result", "receipt"),
    )
    revision = require_continuation_revision(continuation_revision)
    bound = bind_immutable_artifacts(artifact_root)
    dependencies = require_locked_dependencies(
        bound.lock, continuation_revision=revision
    )
    lock_identity = identity(bound.lock)
    coverage = bound.coverage
    baseline = coverage["train_prevalence_baseline"]
    evidence = load_retained(corpus_root, phase157_root, phase167_root)
    records, snapshot = frozen_latent_records(evidence)
    exact(
        snapshot.digest,
        bound.manifest["frozen_digest_before"],
        "current E160 bytes against the #172 training guard",
    )
    exact(
        build_coverage(records, lock_identity),
        {name: item for name, item in coverage.items() if name != "coverage_identity"},
        "coverage re-derived before continuation result exposure",
    )
    head, manifest = load_model(
        bound.root / MODEL_DIRNAME, bound.lock, coverage["coverage_identity"]
    )
    exact(manifest, bound.manifest, "readout manifest against its bound bytes")
    evaluation_evidence = evaluate_readout(
        head, partition_records(records, DatasetPartition.VALIDATION), baseline
    )
    value = assemble_result(
        bound.lock,
        coverage,
        baseline=baseline,
        model_manifest=manifest,
        evaluation_evidence=evaluation_evidence,
    )
    save_result(result_path, value, bound.lock)
    readback = load_result(result_path, bound.lock)
    exact(readback["result_identity"], identity(value), "continuation result identity")
    recorded_evidence = readback["evaluation_evidence"]
    exact(
        metrics_from_evidence(recorded_evidence),
        readback["metrics"],
        "metrics re-derived from the recorded result",
    )
    comparison = paired_comparison(recorded_evidence)
    exact(
        comparison,
        readback["comparison"],
        "comparison re-derived from the recorded result",
    )
    exact(
        comparison["classification"],
        readback["outcome"],
        "outcome re-derived from the recorded result",
    )
    receipt = continuation_receipt(
        bound=bound,
        continuation_revision=revision,
        dependencies=dependencies,
        result_identity=readback["result_identity"],
        continuation_audit=continuation_audit,
    )
    save_continuation_receipt(receipt_path, receipt)
    load_continuation_receipt(receipt_path)
    return {
        "result": str(Path(result_path)),
        "receipt": str(Path(receipt_path)),
        "result_identity": readback["result_identity"],
        "scientific_execution_revision": SCIENTIFIC_EXECUTION_REVISION,
        "technical_continuation_revision": revision,
        "outcome": readback["outcome"],
        "diagnostics": readback["diagnostics"],
        "metrics": readback["metrics"],
        "comparison": readback["comparison"],
        "retrained": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Expose the already-trained Arena #172 result exactly once from its "
            "immutable artifacts. This path never trains."
        )
    )
    parser.add_argument("--corpus-root", required=True)
    parser.add_argument("--phase157-root", required=True)
    parser.add_argument("--phase167-root", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--continuation-revision", required=True)
    parser.add_argument("--continuation-audit", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    output = continue_result(
        artifact_root=arguments.artifact_root,
        corpus_root=arguments.corpus_root,
        phase157_root=arguments.phase157_root,
        phase167_root=arguments.phase167_root,
        continuation_revision=arguments.continuation_revision,
        continuation_audit=arguments.continuation_audit,
        result_path=arguments.result,
        receipt_path=arguments.receipt,
    )
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


__all__ = [
    "CONTINUATION_PURPOSE",
    "PHASE11_ARTIFACT_BINDING",
    "RECEIPT_FIELDS",
    "SCIENTIFIC_EXECUTION_REVISION",
    "BoundArtifacts",
    "ImmutableArtifactBinding",
    "bind_immutable_artifacts",
    "continuation_receipt",
    "continue_result",
    "load_continuation_receipt",
    "require_continuation_revision",
    "require_locked_dependencies",
    "save_continuation_receipt",
    "validate_continuation_receipt",
]


if __name__ == "__main__":
    # A failure here is a technical continuation failure. It is deliberately
    # not printed with the ``STOP / INVALID`` prefix, which names one of the
    # five exhaustive #172 scientific outcomes.
    try:
        sys.exit(main())
    except (Phase11Error, OSError, ValueError) as error:
        print(f"CONTINUATION STOPPED: {error}", file=sys.stderr)
        sys.exit(1)
