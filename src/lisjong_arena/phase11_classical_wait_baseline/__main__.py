"""Operator CLI for Arena #222."""

import argparse
import copy
import json
import sys
from pathlib import Path

from .artifact import (
    load_feature_summary,
    load_lock,
    load_model,
    load_result,
    save_feature_summary,
    save_lock,
    save_model,
    save_result,
)
from .data import coverage_value, load_retained_records
from .evaluation import (
    assemble_result,
    baseline_log_loss,
    evaluate_classical,
    fit_train_prevalence,
)
from .lock import current_receipt, require_current_lock
from .model import fit_offset_logistic
from .protocol import (
    ROLE,
    SCHEMA,
    ClassicalWaitError,
    evaluation_value,
    exact,
    feature_value,
    identity,
    retained_value,
    solver_value,
)

LOCK_FILENAME = "execution-lock.json"
FEATURE_SUMMARY_FILENAME = "feature-summary.json"
MODEL_DIRNAME = "classical-model"
RESULT_FILENAME = "result.json"


def _paths(root: str | Path) -> dict[str, Path]:
    root = Path(root)
    return {
        "root": root,
        "lock": root / LOCK_FILENAME,
        "feature_summary": root / FEATURE_SUMMARY_FILENAME,
        "model": root / MODEL_DIRNAME,
        "result": root / RESULT_FILENAME,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Arena #222 classical public-state structural-wait diagnostic"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("plan")

    lock = commands.add_parser("lock")
    lock.add_argument("--out-root", required=True)
    lock.add_argument("--corpus-root", required=True)
    lock.add_argument("--arena-revision", required=True)
    lock.add_argument("--artifact-audit", required=True)

    for name in ("preflight", "train", "evaluate", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--out-root", required=True)
        command.add_argument("--corpus-root", required=True)

    return parser


def _plan() -> dict[str, object]:
    return {
        "schema": SCHEMA + "/plan",
        "role": ROLE,
        "retained": retained_value(),
        "features": feature_value(),
        "solver": solver_value(),
        "evaluation": evaluation_value(),
        "commands": ["lock", "preflight", "train", "evaluate", "verify"],
        "new_hanchan_generation": 0,
        "formal_test_exposure": False,
        "e160_retraining": False,
        "validation_model_selection": False,
        "feature_search": False,
    }


def _lock(arguments) -> dict[str, object]:
    paths = _paths(arguments.out_root)
    if paths["root"].exists():
        raise FileExistsError(
            f"#222 output root must not exist before lock: {paths['root']}"
        )
    receipt = current_receipt(
        arena_revision=arguments.arena_revision,
        corpus_root=arguments.corpus_root,
        artifact_audit=arguments.artifact_audit,
    )
    save_lock(paths["lock"], receipt)
    return {
        "execution_lock_identity": identity(receipt),
        "result_exposed": False,
        "validation_reference_log_loss": receipt["baseline_reference"][
            "validation_log_loss"
        ],
        "lock_written": str(paths["lock"]),
    }


def _preflight(arguments) -> dict[str, object]:
    paths = _paths(arguments.out_root)
    lock = load_lock(paths["lock"])
    lock_identity = require_current_lock(lock, corpus_root=arguments.corpus_root)
    train, validation, _retained = load_retained_records(arguments.corpus_root)
    baseline = fit_train_prevalence(train)
    exact(
        identity(baseline),
        lock["baseline_reference"]["baseline_identity"],
        "preflight baseline identity",
    )
    exact(
        baseline_log_loss(validation, baseline),
        lock["baseline_reference"]["validation_log_loss"],
        "preflight baseline log loss",
    )
    return {
        "execution_lock_identity": lock_identity,
        "train_coverage": coverage_value(train),
        "validation_coverage": coverage_value(validation),
        "baseline_identity": identity(baseline),
        "result_exposed": False,
        "preflight": "PASS",
    }


def _train(arguments) -> dict[str, object]:
    paths = _paths(arguments.out_root)
    if paths["feature_summary"].exists() or paths["model"].exists():
        raise FileExistsError("#222 TRAIN destinations must be write-once")
    lock = load_lock(paths["lock"])
    lock_identity = require_current_lock(lock, corpus_root=arguments.corpus_root)
    train, _validation, _retained = load_retained_records(arguments.corpus_root)
    baseline = fit_train_prevalence(train)
    exact(
        identity(baseline),
        lock["baseline_reference"]["baseline_identity"],
        "TRAIN baseline identity",
    )
    model, feature_summary = fit_offset_logistic(
        train,
        baseline,
        execution_lock_identity=lock_identity,
    )
    save_model(paths["model"], model, lock_identity, baseline)
    save_feature_summary(paths["feature_summary"], feature_summary, lock_identity)
    return {
        "execution_lock_identity": lock_identity,
        "model_identity": identity(model),
        "train_cells": model["train_cells"],
        "initial_train_log_loss": model["initial_train_log_loss"],
        "final_train_log_loss": model["final_train_log_loss"],
        "optimizer_iterations": model["optimizer_iterations"],
        "validation_evaluated": False,
        "model_written": str(paths["model"]),
        "feature_summary_written": str(paths["feature_summary"]),
    }


def _evaluate(arguments) -> dict[str, object]:
    paths = _paths(arguments.out_root)
    if paths["result"].exists():
        raise FileExistsError("#222 result destination must be write-once")
    lock = load_lock(paths["lock"])
    lock_identity = require_current_lock(lock, corpus_root=arguments.corpus_root)
    train, validation, _retained = load_retained_records(arguments.corpus_root)
    baseline = fit_train_prevalence(train)
    exact(
        identity(baseline),
        lock["baseline_reference"]["baseline_identity"],
        "evaluation baseline identity",
    )
    load_feature_summary(paths["feature_summary"], lock_identity)
    model = load_model(paths["model"], lock_identity, baseline)
    evidence = evaluate_classical(validation, baseline, model)
    result = assemble_result(lock, baseline, model, evidence)
    save_result(paths["result"], result, lock)
    return {
        "result_identity": identity(result),
        "model_identity": identity(model),
        "baseline_log_loss": result["metrics"]["baseline_log_loss"],
        "classical_log_loss": result["metrics"]["classical_log_loss"],
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
    lock = load_lock(paths["lock"])
    lock_identity = require_current_lock(lock, corpus_root=arguments.corpus_root)
    train, validation, _retained = load_retained_records(arguments.corpus_root)
    baseline = fit_train_prevalence(train)
    exact(
        identity(baseline),
        lock["baseline_reference"]["baseline_identity"],
        "verification baseline identity",
    )
    load_feature_summary(paths["feature_summary"], lock_identity)
    model = load_model(paths["model"], lock_identity, baseline)
    persisted = load_result(paths["result"], lock)
    evidence = evaluate_classical(validation, baseline, model)
    rederived = assemble_result(lock, baseline, model, evidence)
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
    except (ClassicalWaitError, FileExistsError, OSError, ValueError) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        sys.exit(1)
