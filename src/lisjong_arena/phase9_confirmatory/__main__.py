"""Narrow explicit Phase 9 preflight, generation, lock, and evaluation CLI."""

import argparse
import json
import sys

from lisjong_arena.phase4_raw_corpus.persistence import load_raw_corpus
from lisjong_arena.phase5_belief_dataset.persistence import (
    load_belief_dataset,
    save_belief_dataset,
)

from .data import (
    build_phase9_holdout_dataset,
    holdout_lock_value,
    validate_holdout_dataset,
)
from .preflight import (
    generate_formal_raw_corpus,
    load_generation_report,
    load_preflight,
    preflight_value,
    require_formal_execution_authorization,
    save_generation_report,
    save_preflight,
    verify_artifact_state,
    verify_current_checkout_revision,
)


def _revision(value: str) -> str:
    if len(value) != 40 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise argparse.ArgumentTypeError("revision must be a full lowercase SHA")
    return value


def _artifact_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot-artifact", required=True)
    parser.add_argument("--s2-artifact", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the locked Phase 9 confirmatory workflow."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser(
        "preflight", help="Verify frozen arms and exact historical checkouts."
    )
    _artifact_arguments(preflight)
    preflight.add_argument("--lisjong-checkout", required=True)
    preflight.add_argument("--engine-checkout", required=True)
    preflight.add_argument("--arena-checkout", required=True)
    preflight.add_argument("--creation-revision", required=True, type=_revision)
    preflight.add_argument("--output", required=True)

    generate = commands.add_parser(
        "generate", help="Explicitly invoke the exact historical raw generator."
    )
    _artifact_arguments(generate)
    generate.add_argument("--preflight", required=True)
    generate.add_argument("--historical-python", required=True)
    generate.add_argument("--raw-output", required=True)
    generate.add_argument("--report-output", required=True)

    lock = commands.add_parser(
        "lock-holdout", help="Persist the fresh Phase-5-compatible TEST-only dataset."
    )
    lock.add_argument("--preflight", required=True)
    lock.add_argument("--generation-report", required=True)
    lock.add_argument("--raw", required=True)
    lock.add_argument("--dataset-output", required=True)

    evaluate = commands.add_parser(
        "evaluate", help="Run the one-shot paired frozen snapshot/S2 evaluation."
    )
    _artifact_arguments(evaluate)
    evaluate.add_argument("--preflight", required=True)
    evaluate.add_argument("--generation-report", required=True)
    evaluate.add_argument("--raw", required=True)
    evaluate.add_argument("--dataset", required=True)
    evaluate.add_argument("--result-output", required=True)
    evaluate.add_argument("--creation-revision", required=True, type=_revision)
    return parser


def _preflight_command(arguments) -> dict[str, object]:
    verify_current_checkout_revision(arguments.creation_revision)
    value = preflight_value(
        snapshot_path=arguments.snapshot_artifact,
        s2_path=arguments.s2_artifact,
        lisjong_repo=arguments.lisjong_checkout,
        engine_repo=arguments.engine_checkout,
        arena_repo=arguments.arena_checkout,
        creation_software_revision=arguments.creation_revision,
    )
    save_preflight(arguments.output, value)
    return value


def _generate_command(arguments) -> dict[str, object]:
    require_formal_execution_authorization()
    preflight = load_preflight(arguments.preflight)
    verify_artifact_state(
        arguments.snapshot_artifact,
        arguments.s2_artifact,
        preflight["artifact_files"],
    )
    execution = generate_formal_raw_corpus(
        historical_python=arguments.historical_python,
        destination=arguments.raw_output,
    )
    return save_generation_report(
        arguments.report_output, preflight["preflight_identity"], execution
    )


def _lock_command(arguments) -> dict[str, object]:
    require_formal_execution_authorization()
    preflight = load_preflight(arguments.preflight)
    generation = load_generation_report(arguments.generation_report)
    if generation["preflight_identity"] != preflight["preflight_identity"]:
        raise RuntimeError("generation report belongs to another preflight")
    raw = load_raw_corpus(arguments.raw)
    if raw.corpus_identity != generation["generation"]["raw_corpus_identity"]:
        raise RuntimeError("generation report and raw corpus identity differ")
    dataset = build_phase9_holdout_dataset(raw)
    persisted = save_belief_dataset(dataset, arguments.dataset_output)
    validate_holdout_dataset(persisted.dataset, raw)
    value = holdout_lock_value(persisted.dataset)
    if (
        value["eligible_turn_anchor_count"]
        != generation["generation"]["turn_anchor_count"]
    ):
        raise RuntimeError("generation and dataset TURN anchor counts differ")
    return value


def _evaluate_command(arguments) -> dict[str, object]:
    require_formal_execution_authorization()
    verify_current_checkout_revision(arguments.creation_revision)
    preflight = load_preflight(arguments.preflight)
    generation = load_generation_report(arguments.generation_report)
    if generation["preflight_identity"] != preflight["preflight_identity"]:
        raise RuntimeError("generation report belongs to another preflight")
    raw = load_raw_corpus(arguments.raw)
    dataset = load_belief_dataset(arguments.dataset).dataset
    if raw.corpus_identity != generation["generation"]["raw_corpus_identity"]:
        raise RuntimeError("generation report and raw corpus identity differ")
    from .evaluation import evaluate_and_save

    return evaluate_and_save(
        persisted_raw=raw,
        dataset=dataset,
        preflight_path=arguments.preflight,
        snapshot_path=arguments.snapshot_artifact,
        s2_path=arguments.s2_artifact,
        generation_report=generation,
        result_destination=arguments.result_output,
        creation_software_revision=arguments.creation_revision,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "preflight":
        value = _preflight_command(arguments)
    elif arguments.command == "generate":
        value = _generate_command(arguments)
    elif arguments.command == "lock-holdout":
        value = _lock_command(arguments)
    else:
        value = _evaluate_command(arguments)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
