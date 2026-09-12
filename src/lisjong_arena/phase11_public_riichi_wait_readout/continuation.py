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
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from lisjong_engine.rules import RuleSet

from lisjong_arena._execution_safety import (
    ExecutionSafetyError,
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
RESULT_FILENAME = "result.json"
CONTINUATION_DIRNAME = "continuation"
CONTINUATION_LOCK_FILENAME = "continuation-lock.json"
PREPARED_RESULT_FILENAME = "prepared-result.json"

CONTINUATION_PURPOSE = "TECHNICAL RESULT-ONLY CONTINUATION"
CONTINUATION_BRANCH = "main"
SCIENTIFIC_EXECUTION_REVISION = "93963d85f6201c714cb4fcf39d59e9e09c85766d"

CONTINUATION_LOCK_FIELDS = (
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
    "prepared_result_sha256",
    "prepared_result_bytes",
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


@dataclass(frozen=True, slots=True)
class PreparedContinuation:
    """Strictly read continuation lock and its immutable prepared result."""

    root: Path
    lock: dict
    result: dict


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


def _validate_repair_revision(revision: object) -> str:
    revision = digest(revision, "technical continuation revision", 40)
    if revision == SCIENTIFIC_EXECUTION_REVISION:
        raise Phase11Error(
            "the technical continuation revision must be the merged #209 repair "
            "commit, never the fixed #172 scientific execution revision"
        )
    return revision


def current_continuation_revision() -> str:
    """Lock the clean checked-out revision after proving it is merged to main."""
    revision = _validate_repair_revision(require_clean_arena_head())
    require_merged_arena_revision(revision, branch=CONTINUATION_BRANCH)
    return revision


def require_locked_continuation_revision(locked: object) -> str:
    """Require the exact already-locked repair revision, never a newer main."""
    revision = _validate_repair_revision(locked)
    exact(
        require_clean_arena_head(),
        revision,
        "clean Arena HEAD against the locked technical continuation revision",
    )
    require_merged_arena_revision(revision, branch=CONTINUATION_BRANCH)
    return revision


def require_locked_dependencies(lock: dict, *, continuation_revision: str) -> dict:
    """Require the original #172 dependency revisions, not current Arena main.

    Arena main has since moved its ``lisjong`` pin forward. Preparation
    re-derives the #172 scientific evidence and exposure revalidates its locked
    environment, so both must run against the original dependency revisions.
    Only Arena may differ, and only at the exact locked repair revision.
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


def continuation_lock(
    *,
    bound: BoundArtifacts,
    continuation_revision: str,
    dependencies: dict,
    result_identity: str,
    prepared_result_sha256: str,
    prepared_result_bytes: int,
    continuation_audit: str,
) -> dict[str, object]:
    """Precommit technical provenance without changing the #172 result schema."""
    if type(continuation_audit) is not str or not continuation_audit.strip():
        raise Phase11Error("the continuation requires a dated operator audit string")
    binding = PHASE11_ARTIFACT_BINDING
    value = {
        "schema": SCHEMA + "/continuation-lock",
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
        "prepared_result_sha256": digest(
            prepared_result_sha256, "prepared result SHA-256"
        ),
        "prepared_result_bytes": prepared_result_bytes,
        "continuation_audit": continuation_audit,
        "retrained": False,
        "resumed": False,
        "checkpoint_reselected": False,
    }
    return validate_continuation_lock(value)


def validate_continuation_lock(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(CONTINUATION_LOCK_FIELDS):
        raise Phase11Error("continuation lock fields are not exact")
    binding = PHASE11_ARTIFACT_BINDING
    exact(value["schema"], SCHEMA + "/continuation-lock", "continuation lock schema")
    exact(value["role"], ROLE, "continuation lock role")
    exact(value["purpose"], CONTINUATION_PURPOSE, "continuation lock purpose")
    exact(
        value["scientific_execution_revision"],
        SCIENTIFIC_EXECUTION_REVISION,
        "continuation lock scientific execution revision",
    )
    continuation_revision = digest(
        value["technical_continuation_revision"],
        "continuation lock technical continuation revision",
        40,
    )
    if continuation_revision == SCIENTIFIC_EXECUTION_REVISION:
        raise Phase11Error(
            "the continuation lock must separate the scientific and repair revisions"
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
        exact(value[field], getattr(binding, field), f"continuation lock {field}")
    validate_runtime(value["locked_runtime"], "continuation lock locked runtime")
    validate_runtime(value["continuation_runtime"], "continuation lock current runtime")
    for field in ("locked_source_revisions", "continuation_source_revisions"):
        revisions = value[field]
        if type(revisions) is not dict or set(revisions) != {
            "lisjong",
            "lisjong_engine",
            "lisjong_arena",
        }:
            raise Phase11Error(f"continuation lock {field} are not exact")
        exact(
            revisions["lisjong"], LISJONG_REVISION, f"continuation lock {field} lisjong"
        )
        exact(
            revisions["lisjong_engine"],
            ENGINE_REVISION,
            f"continuation lock {field} engine",
        )
        digest(
            revisions["lisjong_arena"],
            f"continuation lock {field} Arena revision",
            40,
        )
    exact(
        value["locked_source_revisions"]["lisjong_arena"],
        SCIENTIFIC_EXECUTION_REVISION,
        "continuation lock scientific Arena revision",
    )
    exact(
        value["continuation_source_revisions"]["lisjong_arena"],
        continuation_revision,
        "continuation lock repair Arena revision",
    )
    digest(value["result_identity"], "continuation lock result identity")
    digest(value["prepared_result_sha256"], "prepared result SHA-256")
    if (
        type(value["prepared_result_bytes"]) is not int
        or value["prepared_result_bytes"] <= 0
    ):
        raise Phase11Error("prepared result byte count is invalid")
    audit = value["continuation_audit"]
    if type(audit) is not str or not audit.strip():
        raise Phase11Error("the continuation lock requires a dated operator audit")
    for field in ("retrained", "resumed", "checkpoint_reselected"):
        if type(value[field]) is not bool:
            raise Phase11Error(f"continuation lock {field} must be a JSON boolean")
        exact(value[field], False, f"continuation lock {field}")
    return value


def save_continuation_lock(path: str | Path, value: dict) -> Path:
    destination = Path(path)
    if destination.exists():
        raise FileExistsError(
            f"continuation lock destination already exists: {destination}"
        )
    validate_continuation_lock(value)
    payload = dict(value)
    payload["continuation_lock_identity"] = identity(value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(payload))
    return destination


def load_continuation_lock(path: str | Path) -> dict[str, object]:
    data = Path(path).read_bytes()
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Phase11Error("continuation lock is not valid JSON") from error
    if canonical_json_bytes(payload) != data or type(payload) is not dict:
        raise Phase11Error("continuation lock bytes are not canonical JSON")
    recorded = payload.pop("continuation_lock_identity", None)
    exact(recorded, identity(payload), "continuation lock identity")
    validate_continuation_lock(payload)
    payload["continuation_lock_identity"] = recorded
    return payload


def _prospective_result(
    bound: BoundArtifacts,
    *,
    corpus_root: str,
    phase157_root: str,
    phase167_root: str,
) -> dict[str, object]:
    """Evaluate once before the technical lock is committed."""
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
    return assemble_result(
        bound.lock,
        coverage,
        baseline=baseline,
        model_manifest=manifest,
        evaluation_evidence=evaluation_evidence,
    )


def _strict_result_summary(readback: dict, expected_identity: str) -> dict[str, object]:
    exact(
        readback["result_identity"],
        expected_identity,
        "precommitted continuation result identity",
    )
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
    return {
        "result_identity": readback["result_identity"],
        "outcome": readback["outcome"],
        "diagnostics": readback["diagnostics"],
        "metrics": readback["metrics"],
        "comparison": readback["comparison"],
    }


def _load_prepared_continuation(
    directory: str | Path, scientific_lock: dict
) -> PreparedContinuation:
    root = Path(directory)
    if not root.is_dir() or {path.name for path in root.iterdir()} != {
        CONTINUATION_LOCK_FILENAME,
        PREPARED_RESULT_FILENAME,
    }:
        raise Phase11Error("prepared continuation contains missing or extra files")
    locked = load_continuation_lock(root / CONTINUATION_LOCK_FILENAME)
    prepared_path = root / PREPARED_RESULT_FILENAME
    prepared_bytes = prepared_path.read_bytes()
    exact(
        len(prepared_bytes),
        locked["prepared_result_bytes"],
        "prepared result byte count",
    )
    exact(
        hashlib.sha256(prepared_bytes).hexdigest(),
        locked["prepared_result_sha256"],
        "prepared result SHA-256",
    )
    result = load_result(prepared_path, scientific_lock)
    _strict_result_summary(result, locked["result_identity"])
    return PreparedContinuation(root=root, lock=locked, result=result)


def _validate_locked_context(
    locked: dict, bound: BoundArtifacts, dependencies: dict
) -> None:
    exact(
        locked["locked_runtime"],
        bound.lock["runtime"],
        "continuation lock against scientific runtime",
    )
    exact(
        locked["locked_source_revisions"],
        bound.lock["provenance"]["source_revisions"],
        "continuation lock against scientific revisions",
    )
    exact(
        locked["continuation_runtime"],
        dependencies["runtime"],
        "current runtime against continuation lock",
    )
    exact(
        locked["continuation_source_revisions"],
        dependencies["provenance"]["source_revisions"],
        "current source revisions against continuation lock",
    )


def prepare_continuation(
    *,
    artifact_root: str | Path,
    corpus_root: str,
    phase157_root: str,
    phase167_root: str,
    continuation_audit: str,
) -> dict[str, object]:
    """Atomically precommit the repair revision and one evaluated result."""
    root = Path(artifact_root)
    result_path = root / RESULT_FILENAME
    continuation_path = root / CONTINUATION_DIRNAME
    require_new_artifact_destinations(
        {"result": result_path, "continuation": continuation_path},
        required_names=("result", "continuation"),
    )
    revision = current_continuation_revision()
    bound = bind_immutable_artifacts(root)
    dependencies = require_locked_dependencies(
        bound.lock, continuation_revision=revision
    )
    value = _prospective_result(
        bound,
        corpus_root=corpus_root,
        phase157_root=phase157_root,
        phase167_root=phase167_root,
    )
    prospective_identity = identity(value)
    with TemporaryDirectory(
        prefix=f".{CONTINUATION_DIRNAME}-staging-", dir=root
    ) as staging_name:
        staging = Path(staging_name)
        prepared_path = staging / PREPARED_RESULT_FILENAME
        save_result(prepared_path, value, bound.lock)
        prepared_bytes = prepared_path.read_bytes()
        locked = continuation_lock(
            bound=bound,
            continuation_revision=revision,
            dependencies=dependencies,
            result_identity=prospective_identity,
            prepared_result_sha256=hashlib.sha256(prepared_bytes).hexdigest(),
            prepared_result_bytes=len(prepared_bytes),
            continuation_audit=continuation_audit,
        )
        save_continuation_lock(staging / CONTINUATION_LOCK_FILENAME, locked)
        prepared = _load_prepared_continuation(staging, bound.lock)
        _validate_locked_context(prepared.lock, bound, dependencies)
        staging.rename(continuation_path)
    prepared = _load_prepared_continuation(continuation_path, bound.lock)
    _validate_locked_context(prepared.lock, bound, dependencies)
    return {
        "continuation": str(continuation_path),
        "continuation_lock_identity": prepared.lock["continuation_lock_identity"],
        "result_identity": prepared.lock["result_identity"],
        "scientific_execution_revision": SCIENTIFIC_EXECUTION_REVISION,
        "technical_continuation_revision": revision,
        "result_exposed": False,
        "retrained": False,
    }


def _publish_new_file(source: Path, destination: Path) -> None:
    """Publish complete bytes atomically and without an overwrite window."""
    data = source.read_bytes()
    with TemporaryDirectory(
        prefix=f".{destination.name}-staging-", dir=destination.parent
    ) as staging_name:
        staged = Path(staging_name) / destination.name
        staged.write_bytes(data)
        try:
            os.link(staged, destination)
        except FileExistsError as error:
            raise ExecutionSafetyError(
                "locked output result already exists; outputs are write-once"
            ) from error


def expose_result(*, artifact_root: str | Path) -> dict[str, object]:
    """Publish only the prepared result; never rerun evaluation or training."""
    root = Path(artifact_root)
    result_path = root / RESULT_FILENAME
    require_new_artifact_destinations(
        {"result": result_path}, required_names=("result",)
    )
    continuation_path = root / CONTINUATION_DIRNAME

    # The lock is the first interpreted artifact. Tampering stops before any
    # scientific artifact loading, and this exposure path has no evaluation.
    locked = load_continuation_lock(continuation_path / CONTINUATION_LOCK_FILENAME)
    revision = require_locked_continuation_revision(
        locked["technical_continuation_revision"]
    )
    bound = bind_immutable_artifacts(root)
    dependencies = require_locked_dependencies(
        bound.lock, continuation_revision=revision
    )
    _validate_locked_context(locked, bound, dependencies)
    prepared = _load_prepared_continuation(continuation_path, bound.lock)
    exact(prepared.lock, locked, "prepared continuation lock readback")
    _publish_new_file(prepared.root / PREPARED_RESULT_FILENAME, result_path)
    readback = load_result(result_path, bound.lock)
    summary = _strict_result_summary(readback, locked["result_identity"])
    return {
        "result": str(result_path),
        "continuation": str(continuation_path),
        "continuation_lock_identity": locked["continuation_lock_identity"],
        "scientific_execution_revision": SCIENTIFIC_EXECUTION_REVISION,
        "technical_continuation_revision": revision,
        **summary,
        "retrained": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and expose the already-trained Arena #172 result without "
            "ever training or rerunning evaluation during recovery."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="precommit the exact repair revision and prospective result"
    )
    prepare.add_argument("--corpus-root", required=True)
    prepare.add_argument("--phase157-root", required=True)
    prepare.add_argument("--phase167-root", required=True)
    prepare.add_argument("--artifact-root", required=True)
    prepare.add_argument("--continuation-audit", required=True)
    expose = commands.add_parser(
        "expose", help="atomically publish only the already-prepared result"
    )
    expose.add_argument("--artifact-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "prepare":
        output = prepare_continuation(
            artifact_root=arguments.artifact_root,
            corpus_root=arguments.corpus_root,
            phase157_root=arguments.phase157_root,
            phase167_root=arguments.phase167_root,
            continuation_audit=arguments.continuation_audit,
        )
    else:
        output = expose_result(artifact_root=arguments.artifact_root)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


__all__ = [
    "CONTINUATION_LOCK_FIELDS",
    "CONTINUATION_PURPOSE",
    "PHASE11_ARTIFACT_BINDING",
    "SCIENTIFIC_EXECUTION_REVISION",
    "BoundArtifacts",
    "ImmutableArtifactBinding",
    "PreparedContinuation",
    "bind_immutable_artifacts",
    "continuation_lock",
    "current_continuation_revision",
    "expose_result",
    "load_continuation_lock",
    "prepare_continuation",
    "require_locked_continuation_revision",
    "require_locked_dependencies",
    "save_continuation_lock",
    "validate_continuation_lock",
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
