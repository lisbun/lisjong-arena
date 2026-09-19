"""Operator CLI for Arena #291's locked matched E160 diagnostic."""

import argparse
import copy
import json
import sys
from pathlib import Path

from .artifact import (
    load_latent_summary,
    load_lock,
    load_model,
    load_result,
    save_latent_summary,
    save_lock,
    save_model,
    save_result,
)
from .data import latent_reference, load_latent_records
from .evaluation import (
    assemble_result,
    baseline_log_loss,
    evaluate_probe,
    fit_train_prevalence,
)
from .lock import current_receipt, require_current_lock
from .model import fit_probe
from .protocol import (
    ROLE,
    SCHEMA,
    E160OffsetProbeError,
    centering_value,
    evaluation_value,
    exact,
    identity,
    probe_value,
    representation_value,
    retained_value,
    solver_value,
)

LOCK_FILENAME = "execution-lock.json"
LATENT_SUMMARY_FILENAME = "latent-summary.json"
MODEL_DIRNAME = "e160-offset-probe"
RESULT_FILENAME = "result.json"


def _paths(root: str | Path) -> dict[str, Path]:
    root = Path(root)
    return {
        "root": root,
        "lock": root / LOCK_FILENAME,
        "latent_summary": root / LATENT_SUMMARY_FILENAME,
        "model": root / MODEL_DIRNAME,
        "result": root / RESULT_FILENAME,
    }


def _roots(parser) -> None:
    parser.add_argument("--corpus-root", required=True)
    parser.add_argument("--phase157-root", required=True)
    parser.add_argument("--phase167-root", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Arena #291 frozen E160 prevalence-offset linear probe"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")

    lock = commands.add_parser("lock")
    _roots(lock)
    lock.add_argument("--out-root", required=True)
    lock.add_argument("--arena-revision", required=True)
    lock.add_argument("--artifact-audit", required=True)

    for name in ("preflight", "train", "evaluate", "verify"):
        command = commands.add_parser(name)
        _roots(command)
        command.add_argument("--out-root", required=True)
    return parser


def _plan() -> dict[str, object]:
    return {
        "schema": SCHEMA + "/plan",
        "role": ROLE,
        "retained": retained_value(),
        "representation": representation_value(),
        "centering": centering_value(),
        "probe": probe_value(),
        "solver": solver_value(),
        "evaluation": evaluation_value(),
        "commands": ["lock", "preflight", "train", "evaluate", "verify"],
        "new_hanchan_generation": 0,
        "formal_test_exposure": False,
        "e160_retraining": False,
        "validation_model_selection": False,
        "hpo": False,
        "rescue": False,
    }


def _load_current(arguments):
    return load_latent_records(
        arguments.corpus_root,
        arguments.phase157_root,
        arguments.phase167_root,
    )


def _require(arguments):
    lock = load_lock(_paths(arguments.out_root)["lock"])
    require_current_lock(
        lock,
        corpus_root=arguments.corpus_root,
        phase157_root=arguments.phase157_root,
        phase167_root=arguments.phase167_root,
    )
    return lock


def _derive(arguments, lock):
    train, validation, evidence, snapshot = _load_current(arguments)
    reference = latent_reference(train, validation, snapshot.digest)
    exact(reference, lock["latent_reference"], "current latent reference")
    baseline = fit_train_prevalence(train)
    exact(
        identity(baseline),
        lock["baseline_reference"]["baseline_identity"],
        "current prevalence baseline",
    )
    exact(
        baseline_log_loss(validation, baseline),
        lock["baseline_reference"]["validation_log_loss"],
        "current baseline log loss",
    )
    return train, validation, evidence, snapshot, reference, baseline


def _lock(arguments) -> dict[str, object]:
    paths = _paths(arguments.out_root)
    if paths["root"].exists():
        raise FileExistsError(
            f"#291 output root must not exist before lock: {paths['root']}"
        )
    receipt = current_receipt(
        arena_revision=arguments.arena_revision,
        corpus_root=arguments.corpus_root,
        phase157_root=arguments.phase157_root,
        phase167_root=arguments.phase167_root,
        artifact_audit=arguments.artifact_audit,
    )
    save_lock(paths["lock"], receipt)
    return {
        "execution_lock_identity": identity(receipt),
        "latent_fingerprint": receipt["latent_reference"]["latent_fingerprint"],
        "centering_identity": receipt["latent_reference"]["centering"][
            "centering_identity"
        ],
        "result_exposed": False,
        "lock_written": str(paths["lock"]),
    }


def _preflight(arguments) -> dict[str, object]:
    paths = _paths(arguments.out_root)
    if paths["latent_summary"].exists():
        raise FileExistsError("#291 latent summary destination must be write-once")
    lock = _require(arguments)
    _train, _validation, _evidence, _snapshot, reference, baseline = _derive(
        arguments, lock
    )
    lock_identity = identity(lock)
    save_latent_summary(paths["latent_summary"], reference, lock_identity)
    return {
        "execution_lock_identity": lock_identity,
        "baseline_identity": identity(baseline),
        "latent_fingerprint": reference["latent_fingerprint"],
        "centering_identity": reference["centering"]["centering_identity"],
        "train_coverage": reference["train_coverage"],
        "validation_coverage": reference["validation_coverage"],
        "result_exposed": False,
        "preflight": "PASS",
    }


def _train(arguments) -> dict[str, object]:
    paths = _paths(arguments.out_root)
    if paths["model"].exists():
        raise FileExistsError("#291 model destination must be write-once")
    lock = _require(arguments)
    train, _validation, evidence, snapshot, reference, baseline = _derive(
        arguments, lock
    )
    lock_identity = identity(lock)
    load_latent_summary(
        paths["latent_summary"],
        lock_identity,
        reference,
    )
    model = fit_probe(
        train,
        baseline,
        reference["centering"],
        execution_lock_identity=lock_identity,
        latent_fingerprint=reference["latent_fingerprint"],
        frozen_model=evidence.e160_model,
        frozen_snapshot=snapshot,
    )
    save_model(
        paths["model"],
        model,
        lock_identity,
        baseline,
        reference["centering"],
        reference["latent_fingerprint"],
    )
    return {
        "execution_lock_identity": lock_identity,
        "model_identity": identity(model),
        "train_cells": model["train_cells"],
        "initial_train_log_loss": model["initial_train_log_loss"],
        "final_train_log_loss": model["final_train_log_loss"],
        "optimizer_iterations": model["optimizer_iterations"],
        "validation_evaluated": False,
        "model_written": str(paths["model"]),
    }


def _evaluate(arguments) -> dict[str, object]:
    paths = _paths(arguments.out_root)
    if paths["result"].exists():
        raise FileExistsError("#291 result destination must be write-once")
    lock = _require(arguments)
    _train, validation, _evidence, _snapshot, reference, baseline = _derive(
        arguments, lock
    )
    lock_identity = identity(lock)
    load_latent_summary(paths["latent_summary"], lock_identity, reference)
    model = load_model(
        paths["model"],
        lock_identity,
        baseline,
        reference["centering"],
        reference["latent_fingerprint"],
    )
    evidence = evaluate_probe(
        validation,
        baseline,
        model,
        reference["centering"],
    )
    result = assemble_result(
        lock,
        baseline,
        model,
        reference["centering"],
        reference["latent_fingerprint"],
        evidence,
    )
    save_result(paths["result"], result, lock)
    return {
        "result_identity": identity(result),
        "model_identity": identity(model),
        "baseline_log_loss": result["metrics"]["baseline_log_loss"],
        "probe_log_loss": result["metrics"]["probe_log_loss"],
        "delta_log_loss": result["metrics"]["delta_log_loss"],
        "paired_interval": [
            result["paired_comparison"]["interval_lower"],
            result["paired_comparison"]["interval_upper"],
        ],
        "positive_hanchan": result["paired_comparison"]["positive_hanchan"],
        "negative_hanchan": result["paired_comparison"]["negative_hanchan"],
        "tied_hanchan": result["paired_comparison"]["tied_hanchan"],
        "outcome": result["outcome"],
        "result_written": str(paths["result"]),
    }


def _verify(arguments) -> dict[str, object]:
    paths = _paths(arguments.out_root)
    lock = _require(arguments)
    _train, validation, _evidence, _snapshot, reference, baseline = _derive(
        arguments, lock
    )
    lock_identity = identity(lock)
    load_latent_summary(paths["latent_summary"], lock_identity, reference)
    model = load_model(
        paths["model"],
        lock_identity,
        baseline,
        reference["centering"],
        reference["latent_fingerprint"],
    )
    persisted = load_result(paths["result"], lock)
    evidence = evaluate_probe(
        validation,
        baseline,
        model,
        reference["centering"],
    )
    rederived = assemble_result(
        lock,
        baseline,
        model,
        reference["centering"],
        reference["latent_fingerprint"],
        evidence,
    )
    persisted_without_identity = copy.deepcopy(persisted)
    result_identity = persisted_without_identity.pop("result_identity")
    exact(persisted_without_identity, rederived, "strict result re-derivation")
    exact(result_identity, identity(rederived), "strict result identity")
    return {
        "execution_lock_identity": lock_identity,
        "model_identity": identity(model),
        "result_identity": result_identity,
        "outcome": rederived["outcome"],
        "strict_readback": "PASS",
        "canonical_rederivation": "PASS",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        output = _plan()
    elif arguments.command == "lock":
        output = _lock(arguments)
    elif arguments.command == "preflight":
        output = _preflight(arguments)
    elif arguments.command == "train":
        output = _train(arguments)
    elif arguments.command == "evaluate":
        output = _evaluate(arguments)
    else:
        output = _verify(arguments)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        E160OffsetProbeError,
        FileExistsError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        sys.exit(1)
