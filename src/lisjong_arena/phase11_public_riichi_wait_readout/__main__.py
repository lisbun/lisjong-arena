"""Narrow CLI for the post-merge, one-shot Arena #172 experiment."""

import argparse
import json
import sys
import time
from pathlib import Path

from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.stage3_optimization_saturation.experiment import (
    saturation_population_data,
)

from .artifact import (
    load_lock,
    load_model,
    model_manifest_without_weights,
    save_lock,
    save_model,
    save_result,
)
from .coverage import build_coverage, load_coverage, save_coverage
from .data import extract_frozen_latents, partition_records
from .evaluation import evaluate_readout
from .lock import current_receipt, require_current_lock
from .model import freeze_e160
from .protocol import (
    COVERAGE_MINIMUM_HANCHAN,
    Phase11Error,
    evaluation_value,
    exact,
    identity,
    readout_value,
    representation_value,
    retained_value,
    training_value,
)
from .result import assemble_result
from .retained import load_retained, retained_readback_value
from .training import train_readout


def _roots(parser):
    parser.add_argument("--corpus-root", required=True)
    parser.add_argument("--phase157-root", required=True)
    parser.add_argument("--phase167-root", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the locked frozen-E160 public-riichi wait readout experiment."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Print the immutable Arena #172 protocol.")
    verify = commands.add_parser(
        "verify", help="Strictly read retained #150/#167 evidence."
    )
    _roots(verify)
    lock = commands.add_parser("lock", help="Create the pre-result execution lock.")
    _roots(lock)
    lock.add_argument("--arena-revision", required=True)
    lock.add_argument("--artifact-audit", required=True)
    lock.add_argument("--output", required=True)
    preflight = commands.add_parser(
        "preflight", help="Audit eligibility and target coverage."
    )
    _roots(preflight)
    preflight.add_argument("--lock", required=True)
    preflight.add_argument("--coverage", required=True)
    train = commands.add_parser(
        "train", help="Train the fixed readout head exactly once."
    )
    _roots(train)
    train.add_argument("--lock", required=True)
    train.add_argument("--coverage", required=True)
    train.add_argument("--artifact", required=True)
    evaluate = commands.add_parser("evaluate", help="Publish the exhaustive result.")
    _roots(evaluate)
    evaluate.add_argument("--lock", required=True)
    evaluate.add_argument("--coverage", required=True)
    evaluate.add_argument("--artifact")
    evaluate.add_argument("--result", required=True)
    return parser


def _plan() -> dict[str, object]:
    return {
        "retained": retained_value(),
        "representation": representation_value(),
        "readout": readout_value(),
        "training": training_value(),
        "evaluation": evaluation_value(),
    }


def _load_evidence(arguments):
    return load_retained(
        arguments.corpus_root, arguments.phase157_root, arguments.phase167_root
    )


def _require(arguments):
    lock = load_lock(arguments.lock)
    require_current_lock(
        lock,
        corpus_root=arguments.corpus_root,
        phase157_root=arguments.phase157_root,
        phase167_root=arguments.phase167_root,
    )
    return lock


def _records(evidence):
    full = saturation_population_data(evidence.raw, evidence.dataset)
    snapshot = freeze_e160(evidence.e160_model)
    records = extract_frozen_latents(
        evidence.e160_model, full.train_sequences + full.validation_sequences
    )
    return records, snapshot


def _verify(arguments) -> dict[str, object]:
    evidence = _load_evidence(arguments)
    return {
        "retained": retained_readback_value(evidence),
        "phase167_source_revisions": evidence.phase167_lock["provenance"][
            "source_revisions"
        ],
        "phase167_runtime": evidence.phase167_lock["runtime"],
        "e160_parameter_count": sum(
            parameter.numel() for parameter in evidence.e160_model.parameters()
        ),
        "regenerated": False,
        "retrained": False,
    }


def _lock(arguments) -> dict[str, object]:
    value = current_receipt(
        arena_revision=arguments.arena_revision,
        corpus_root=arguments.corpus_root,
        phase157_root=arguments.phase157_root,
        phase167_root=arguments.phase167_root,
        artifact_audit=arguments.artifact_audit,
    )
    save_lock(arguments.output, value)
    return {
        "lock": str(Path(arguments.output)),
        "execution_lock_identity": identity(value),
    }


def _preflight(arguments) -> dict[str, object]:
    lock = _require(arguments)
    evidence = _load_evidence(arguments)
    records, snapshot = _records(evidence)
    coverage = build_coverage(records, identity(lock))
    save_coverage(arguments.coverage, coverage, identity(lock))
    return {
        "coverage": str(Path(arguments.coverage)),
        "coverage_identity": identity(coverage),
        "partitions": coverage["partitions"],
        "semantic_valid": coverage["semantic_valid"],
        "frozen_digest": snapshot.digest,
    }


def _train(arguments) -> dict[str, object]:
    lock = _require(arguments)
    coverage = load_coverage(arguments.coverage, identity(lock))
    if not coverage["semantic_valid"]:
        raise Phase11Error(
            "available all-zero established-riichi rows require STOP / INVALID"
        )
    if (
        coverage["partitions"]["validation"]["eligible_hanchan"]
        < COVERAGE_MINIMUM_HANCHAN
    ):
        raise Phase11Error("coverage gate does not permit readout training")
    evidence = _load_evidence(arguments)
    records, snapshot = _records(evidence)
    rederived = build_coverage(records, identity(lock))
    exact(
        rederived,
        {name: item for name, item in coverage.items() if name != "coverage_identity"},
        "coverage re-derived before training",
    )
    started = time.process_time()
    result = train_readout(
        evidence.e160_model,
        snapshot,
        partition_records(records, DatasetPartition.TRAIN),
        partition_records(records, DatasetPartition.VALIDATION),
    )
    cpu_seconds = time.process_time() - started
    manifest = model_manifest_without_weights(
        lock=lock,
        coverage_identity=coverage["coverage_identity"],
        result=result,
        training_cpu_seconds=cpu_seconds,
    )
    loaded = save_model(
        arguments.artifact,
        result.head,
        manifest,
        lock,
        coverage["coverage_identity"],
    )
    return {
        "artifact": str(Path(arguments.artifact)),
        "selected_epoch": loaded["selected_epoch"],
        "epochs_run": loaded["epochs_run"],
        "weights_sha256": loaded["weights_sha256"],
        "frozen_digest_before": loaded["frozen_digest_before"],
        "frozen_digest_after": loaded["frozen_digest_after"],
    }


def _evaluate(arguments) -> dict[str, object]:
    lock = _require(arguments)
    coverage = load_coverage(arguments.coverage, identity(lock))
    baseline = (
        coverage["train_prevalence_baseline"] if coverage["semantic_valid"] else None
    )
    model_manifest = None
    evaluation_evidence = None
    if (
        coverage["semantic_valid"]
        and coverage["partitions"]["validation"]["eligible_hanchan"]
        >= COVERAGE_MINIMUM_HANCHAN
    ):
        if arguments.artifact is None:
            raise Phase11Error("sufficient coverage requires --artifact")
        head, model_manifest = load_model(
            arguments.artifact, lock, coverage["coverage_identity"]
        )
        retained = _load_evidence(arguments)
        records, snapshot = _records(retained)
        exact(
            snapshot.digest,
            model_manifest["frozen_digest_before"],
            "current E160 bytes against training guard",
        )
        exact(
            build_coverage(records, identity(lock)),
            {
                name: item
                for name, item in coverage.items()
                if name != "coverage_identity"
            },
            "coverage re-derived before result exposure",
        )
        evaluation_evidence = evaluate_readout(
            head, partition_records(records, DatasetPartition.VALIDATION), baseline
        )
    value = assemble_result(
        lock,
        coverage,
        baseline=baseline,
        model_manifest=model_manifest,
        evaluation_evidence=evaluation_evidence,
    )
    save_result(arguments.result, value, lock)
    return {
        "result": str(Path(arguments.result)),
        "outcome": value["outcome"],
        "diagnostics": value["diagnostics"],
        "metrics": value["metrics"],
        "comparison": value["comparison"],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        output = _plan()
    elif arguments.command == "verify":
        output = _verify(arguments)
    elif arguments.command == "lock":
        output = _lock(arguments)
    elif arguments.command == "preflight":
        output = _preflight(arguments)
    elif arguments.command == "train":
        output = _train(arguments)
    else:
        output = _evaluate(arguments)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Phase11Error, OSError, ValueError) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        sys.exit(1)
