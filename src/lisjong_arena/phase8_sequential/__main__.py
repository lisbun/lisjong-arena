"""Narrow explicit Phase 8 inventory, training, and comparison CLI."""

import argparse
import json
import platform
import sys
from importlib.metadata import distribution
from pathlib import Path

from lisjong_arena.phase4_raw_corpus.persistence import load_raw_corpus
from lisjong_arena.phase5_belief_dataset.builder import resolve_training_samples
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.persistence import load_belief_dataset

from .artifact import (
    artifact_logical_identity,
    comparison_value,
    load_model_artifact,
    manifest_without_weights,
    save_comparison_result,
    save_model_artifact,
)
from .data import inventory_from_dataset, prepare_formal_examples
from .protocol import (
    Candidate,
    build_sequences,
    inventory_value,
    load_inventory,
    save_inventory,
)


def _revision(value: str) -> str:
    if len(value) != 40 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise argparse.ArgumentTypeError("revision must be a full lowercase commit SHA")
    return value


def _data_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--raw", required=True)
    parser.add_argument("--dataset", required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run locked TRAIN/VALIDATION-only Phase 8 sequential experiments."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser(
        "inventory", help="Persist deterministic reference-only sequence inventory."
    )
    _data_paths(inventory)
    inventory.add_argument("--output", required=True)
    for name, candidate in (("train-s1", Candidate.S1), ("train-s2", Candidate.S2)):
        train = commands.add_parser(
            name, help=f"Train fixed {candidate.value} candidate."
        )
        _data_paths(train)
        train.add_argument("--inventory", required=True)
        train.add_argument("--snapshot-artifact", required=True)
        train.add_argument("--artifact", required=True)
        train.add_argument("--lisjong-revision", required=True, type=_revision)
        train.add_argument("--engine-revision", required=True, type=_revision)
        train.add_argument("--arena-revision", required=True, type=_revision)
        train.set_defaults(candidate=candidate)
    compare = commands.add_parser(
        "compare", help="Select at most one candidate from completed S1/S2 artifacts."
    )
    _data_paths(compare)
    compare.add_argument("--inventory", required=True)
    compare.add_argument("--s1-artifact", required=True)
    compare.add_argument("--s2-artifact", required=True)
    compare.add_argument("--result", required=True)
    compare.add_argument("--arena-revision", required=True, type=_revision)
    return parser


def _installed_revision(distribution_name: str) -> str:
    direct_url = distribution(distribution_name).read_text("direct_url.json")
    if direct_url is None:
        raise RuntimeError(f"{distribution_name} lacks direct_url.json provenance")
    try:
        revision = json.loads(direct_url)["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"{distribution_name} installation lacks exact VCS provenance"
        ) from error
    return _revision(revision)


def _verify_runtime() -> object:
    import torch

    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("formal Phase 8 requires CPython 3.14")
    if torch.__version__ != "2.13.0+cpu":
        raise RuntimeError("formal Phase 8 requires PyTorch 2.13.0 CPU")
    if torch.cuda.is_available():
        raise RuntimeError("formal Phase 8 is CPU-only")
    return torch


def _configure_snapshot_runtime(torch, manifest: dict[str, object]) -> None:
    runtime = manifest.get("runtime")
    if type(runtime) is not dict:
        raise RuntimeError("frozen Phase 6 runtime contract is incomplete")
    if runtime.get("device") != "cpu":
        raise RuntimeError("frozen Phase 6 runtime device differs")
    thread_count = runtime.get("torch_thread_count")
    deterministic = runtime.get("deterministic_algorithms")
    if type(thread_count) is not int or thread_count <= 0:
        raise RuntimeError("frozen Phase 6 torch thread count is invalid")
    if type(deterministic) is not bool:
        raise RuntimeError("frozen Phase 6 deterministic flag is invalid")
    torch.set_num_threads(thread_count)
    torch.use_deterministic_algorithms(deterministic)


def _load_data(raw_path: str, dataset_path: str):
    from .data import validate_formal_dataset

    raw = load_raw_corpus(raw_path)
    dataset = load_belief_dataset(dataset_path).dataset
    validate_formal_dataset(dataset)
    if raw.corpus_identity != dataset.raw_corpus_identity:
        raise RuntimeError("raw corpus and dataset identities differ")
    if raw.corpus.provenance != dataset.provenance:
        raise RuntimeError("raw corpus and dataset provenance differ")
    return raw, dataset


def _verify_inventory(dataset, path: str) -> dict[str, object]:
    persisted = load_inventory(path)
    actual = inventory_value(inventory_from_dataset(dataset))
    if persisted != actual:
        raise RuntimeError(
            "persisted inventory differs from the current locked dataset"
        )
    return persisted


def _training_command(arguments) -> dict[str, object]:
    from lisjong_arena.phase6_snapshot.training import predict_snapshot_examples
    from lisjong_arena.phase7_snapshot_test.evaluation import verify_frozen_artifact

    from .evaluation import verify_snapshot_validation_compatibility
    from .training import train_candidate

    artifact_path = Path(arguments.artifact)
    if artifact_path.exists():
        raise FileExistsError("Phase 8 artifact destination already exists")
    torch = _verify_runtime()
    if _installed_revision("lisjong") != arguments.lisjong_revision:
        raise RuntimeError(
            "declared lisjong revision differs from installed provenance"
        )
    if _installed_revision("lisjong-engine") != arguments.engine_revision:
        raise RuntimeError(
            "declared lisjong-engine revision differs from installed provenance"
        )
    raw, dataset = _load_data(arguments.raw, arguments.dataset)
    inventory = _verify_inventory(dataset, arguments.inventory)
    frozen, _manifest_sha = verify_frozen_artifact(arguments.snapshot_artifact)
    _configure_snapshot_runtime(torch, frozen.manifest)
    samples = resolve_training_samples(dataset, raw)
    development_examples = prepare_formal_examples(dataset, samples)
    canonical_validation_examples = tuple(
        value
        for value in development_examples
        if value.example.partition is DatasetPartition.VALIDATION
    )
    snapshot_predictions, _residual = predict_snapshot_examples(
        frozen.model, canonical_validation_examples
    )
    canonical_validation = verify_snapshot_validation_compatibility(
        dataset.dataset_identity,
        canonical_validation_examples,
        snapshot_predictions,
        frozen.manifest["validation_metrics"],
    )
    sequences = build_sequences(development_examples)
    from .protocol import BpttMode, BpttPolicy

    policy_value = inventory["bptt_policy"]
    policy = BpttPolicy(
        BpttMode(policy_value["mode"]), policy_value["truncation_length"]
    )
    result = train_candidate(
        arguments.candidate,
        sequences,
        dataset_identity=dataset.dataset_identity,
        bptt_policy=policy,
        canonical_validation=canonical_validation,
    )
    source = dataset.provenance.source_revisions
    dataset_revisions = {
        "lisjong": source.lisjong,
        "lisjong_engine": source.lisjong_engine,
        "lisjong_arena": source.lisjong_arena,
    }
    training_revisions = {
        "lisjong": arguments.lisjong_revision,
        "lisjong_engine": arguments.engine_revision,
        "lisjong_arena": arguments.arena_revision,
    }
    runtime = {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "device": "cpu",
        "platform": platform.platform(),
    }
    loaded = save_model_artifact(
        artifact_path,
        result.model,
        manifest_without_weights(
            result=result,
            raw_corpus_identity=dataset.raw_corpus_identity,
            dataset_identity=dataset.dataset_identity,
            dataset_source_revisions=dataset_revisions,
            training_source_revisions=training_revisions,
            inventory=inventory,
            runtime=runtime,
        ),
    )
    return {
        "candidate": arguments.candidate.value,
        "artifact_logical_identity": artifact_logical_identity(loaded.manifest),
        "weights_sha256": loaded.manifest["weights_sha256"],
        "inventory_identity": inventory["inventory_identity"],
        "bptt_policy": inventory["bptt_policy"],
        "parameter_count": result.parameter_count,
        "selected_epoch": result.selected_epoch,
        "training_wall_clock_seconds": result.training_wall_clock_seconds,
        "validation_metrics": loaded.manifest["validation_metrics"],
        "delta_mae": result.validation.delta_mae,
        "positive_game_count": result.validation.positive_game_count,
        "physical_consistency": result.validation.physical_consistency,
        "advancement_eligible": result.validation.summary.advancement_eligible,
        "test_partition_evaluated": False,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "inventory":
        _raw, dataset = _load_data(arguments.raw, arguments.dataset)
        inventory = inventory_from_dataset(dataset)
        save_inventory(arguments.output, inventory)
        output = inventory_value(inventory)
    elif arguments.command in ("train-s1", "train-s2"):
        output = _training_command(arguments)
    else:
        if Path(arguments.result).exists():
            raise FileExistsError("Phase 8 comparison destination already exists")
        _verify_runtime()
        _raw, dataset = _load_data(arguments.raw, arguments.dataset)
        inventory = _verify_inventory(dataset, arguments.inventory)
        s1 = load_model_artifact(arguments.s1_artifact)
        s2 = load_model_artifact(arguments.s2_artifact)
        for artifact in (s1, s2):
            if (
                artifact.manifest["raw_corpus_identity"] != dataset.raw_corpus_identity
                or artifact.manifest["dataset_identity"] != dataset.dataset_identity
                or artifact.manifest["inventory"]["inventory_identity"]
                != inventory["inventory_identity"]
            ):
                raise RuntimeError("candidate artifact does not match current inputs")
        output = comparison_value(
            s1.manifest,
            s2.manifest,
            creation_software_revision=arguments.arena_revision,
        )
        save_comparison_result(arguments.result, output)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
