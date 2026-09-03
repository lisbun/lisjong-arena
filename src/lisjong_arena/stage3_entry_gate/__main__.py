"""Narrow explicit Stage 3 Entry Gate pilot CLI.

```text
plan       locked 3 populationのexact identityを出力する
generate   1 populationのraw corpus / dataset / manifestを生成する
train      1 populationでfixed-budget S2をtrainingする
matrix     3 x 3 cross-population evaluation resultを生成する
```

Stage 3はdevelopment-onlyである。TEST partitionを選ぶoptionを持たず、
training budget、model family、seeds、splitをcaller optionにしない。
"""

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena.stage3_entry_gate.artifact import (
    RESULT_SCHEMA_VERSION,
    execution_runtime_value,
    load_model,
    model_manifest_without_weights,
    save_model_artifact,
    save_result,
)
from lisjong_arena.stage3_entry_gate.experiment import (
    CANDIDATE,
    REFERENCE_ARM_ID,
    build_population_data,
    evaluate_on_population,
    evaluation_value,
    inventory_summary,
    train_population_candidate,
)
from lisjong_arena.stage3_entry_gate.generation import (
    generate_population,
    load_population,
)
from lisjong_arena.stage3_entry_gate.population import (
    PILOT_ROLE,
    plan_for_population_id,
    stage3_population_plans,
)

POPULATION_IDS = ("A", "B", "C")


def _keyed_path(value: str) -> tuple[str, Path]:
    key, separator, path = value.partition("=")
    if not separator or key not in POPULATION_IDS or not path:
        raise argparse.ArgumentTypeError(
            "expected <population-id>=<path> with a population id of A, B or C"
        )
    return key, Path(path)


def _keyed_paths(values: list[tuple[str, Path]], name: str) -> dict[str, Path]:
    mapping = dict(values)
    if len(mapping) != len(values):
        raise SystemExit(f"duplicate {name} population id")
    if tuple(sorted(mapping)) != POPULATION_IDS:
        raise SystemExit(f"{name} must be given exactly once for A, B and C")
    return mapping


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the locked development-only Stage 3 Entry Gate first-party "
            "population pilot."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Print the locked population plan identities.")
    generate = commands.add_parser(
        "generate", help="Generate one population raw corpus and dataset."
    )
    generate.add_argument("--population", required=True, choices=POPULATION_IDS)
    generate.add_argument("--output", required=True)
    train = commands.add_parser(
        "train", help="Train the fixed S2 candidate on one population."
    )
    train.add_argument("--population-dir", required=True)
    train.add_argument("--artifact", required=True)
    matrix = commands.add_parser(
        "matrix", help="Evaluate every trained candidate on every VALIDATION."
    )
    matrix.add_argument(
        "--population", required=True, action="append", type=_keyed_path
    )
    matrix.add_argument("--model", required=True, action="append", type=_keyed_path)
    matrix.add_argument("--result", required=True)
    return parser


def _population_data(directory: Path):
    manifest, persisted_raw, persisted_dataset = load_population(directory)
    data = build_population_data(
        population_id=manifest["population_plan"]["population_id"],
        population_identity=manifest["population_identity"],
        persisted_raw=persisted_raw,
        dataset=persisted_dataset.dataset,
    )
    return manifest, data


def _train_command(arguments) -> dict[str, object]:
    artifact_path = Path(arguments.artifact)
    if artifact_path.exists():
        raise FileExistsError(f"artifact destination already exists: {artifact_path}")
    manifest, data = _population_data(Path(arguments.population_dir))
    plan = plan_for_population_id(data.population_id)
    if plan.population_identity != data.population_identity:
        raise SystemExit(
            "persisted population identity differs from the locked Stage 3 plan"
        )
    result = train_population_candidate(data)
    loaded = save_model_artifact(
        artifact_path,
        result.model,
        model_manifest_without_weights(
            population_id=data.population_id,
            population_identity=data.population_identity,
            raw_corpus_identity=data.raw_corpus_identity,
            dataset_identity=data.dataset_identity,
            inventory=inventory_summary(data),
            result=result,
            runtime=execution_runtime_value(),
        ),
    )
    return {
        "population_id": data.population_id,
        "population_identity": data.population_identity,
        "raw_corpus_identity": data.raw_corpus_identity,
        "dataset_identity": data.dataset_identity,
        "weights_sha256": loaded.manifest["weights_sha256"],
        "selected_epoch": loaded.manifest["selected_epoch"],
        "parameter_count": loaded.manifest["parameter_count"],
        "training_wall_clock_seconds": loaded.manifest["training_wall_clock_seconds"],
        "within_population_validation": loaded.manifest["within_population_validation"],
        "generation_cost": manifest["cost"],
    }


def _hard_gate(
    manifest: dict[str, object], cell: dict[str, object]
) -> dict[str, object]:
    """機械的に確認できるhard gate条件だけを集める。

    population selectionそのものはここで自動化しない。最小MAEやstrongest
    Policyを機械的に選ばないことがEntry Gateのdecision ruleである。
    """
    provenance = manifest["provenance"]
    coverage = manifest["coverage"]
    return {
        "deterministic_generation_verified": True,
        "source_revisions_fully_resolved": bool(provenance["fully_resolved"]),
        "rules_fingerprint": provenance["effective_rules"]["fingerprint"],
        "split_policy_id": manifest["split_policy_id"],
        "test_partition_present": bool(manifest["test_partition_present"]),
        "hanchan_generated": coverage["events"]["hanchan"],
        "physical_validity_passed": bool(
            cell["physical_consistency"]["blocking_gate_passed"]
        ),
        "runtime_and_storage_measured": True,
    }


def _matrix_command(arguments) -> dict[str, object]:
    result_path = Path(arguments.result)
    if result_path.exists():
        raise FileExistsError(f"result destination already exists: {result_path}")
    population_paths = _keyed_paths(arguments.population, "--population")
    model_paths = _keyed_paths(arguments.model, "--model")
    manifests = {}
    data_by_id = {}
    for population_id in POPULATION_IDS:
        manifest, data = _population_data(population_paths[population_id])
        if data.population_id != population_id:
            raise SystemExit(
                f"{population_paths[population_id]} is not population {population_id}"
            )
        plan = plan_for_population_id(population_id)
        if plan.population_identity != data.population_identity:
            raise SystemExit(
                f"population {population_id} identity differs from the locked plan"
            )
        manifests[population_id] = manifest
        data_by_id[population_id] = data
    identities = {value.dataset_identity for value in data_by_id.values()}
    if len(identities) != len(POPULATION_IDS):
        raise SystemExit("population dataset identities must be distinct")

    cells = []
    model_manifests = {}
    for training_id in POPULATION_IDS:
        model, model_manifest = load_model(model_paths[training_id])
        if model_manifest["training_population_id"] != training_id:
            raise SystemExit(
                f"model {model_paths[training_id]} was not trained on population "
                f"{training_id}"
            )
        expected = data_by_id[training_id]
        if (
            model_manifest["training_population_identity"]
            != expected.population_identity
            or model_manifest["dataset_identity"] != expected.dataset_identity
            or model_manifest["raw_corpus_identity"] != expected.raw_corpus_identity
        ):
            raise SystemExit(
                f"model {training_id} is bound to different population artifacts"
            )
        model_manifests[training_id] = model_manifest
        for validation_id in POPULATION_IDS:
            evaluation = evaluate_on_population(model, data_by_id[validation_id])
            cell = evaluation_value(evaluation, data_by_id[validation_id])
            cell["training_population_id"] = training_id
            cell["training_population_identity"] = expected.population_identity
            cells.append(cell)

    populations = {
        population_id: {
            "population_identity": data_by_id[population_id].population_identity,
            "population_plan": manifests[population_id]["population_plan"],
            "raw_corpus_identity": data_by_id[population_id].raw_corpus_identity,
            "dataset_identity": data_by_id[population_id].dataset_identity,
            "inventory": inventory_summary(data_by_id[population_id]),
            "provenance": manifests[population_id]["provenance"],
            "generation_runtime": manifests[population_id]["generation_runtime"],
            "coverage": manifests[population_id]["coverage"],
            "generation_cost": manifests[population_id]["cost"],
            "dataset_conditional_uniform_baseline": manifests[population_id][
                "conditional_uniform_baseline"
            ],
            "model": {
                "weights_sha256": model_manifests[population_id]["weights_sha256"],
                "weights_bytes": model_manifests[population_id]["weights_bytes"],
                "parameter_count": model_manifests[population_id]["parameter_count"],
                "selected_epoch": model_manifests[population_id]["selected_epoch"],
                "loss_history": model_manifests[population_id]["loss_history"],
                "training_wall_clock_seconds": model_manifests[population_id][
                    "training_wall_clock_seconds"
                ],
                "peak_process_ram_bytes": model_manifests[population_id][
                    "peak_process_ram_bytes"
                ],
                "inference_samples_per_second": model_manifests[population_id][
                    "inference_samples_per_second"
                ],
                "training_config": model_manifests[population_id]["training_config"],
                "runtime": model_manifests[population_id]["runtime"],
            },
        }
        for population_id in POPULATION_IDS
    }
    within = {
        population_id: next(
            cell
            for cell in cells
            if cell["training_population_id"] == population_id
            and cell["validation_population_id"] == population_id
        )
        for population_id in POPULATION_IDS
    }
    value = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "candidate": CANDIDATE.value,
        "reference_arm_id": REFERENCE_ARM_ID,
        "evaluation_runtime": execution_runtime_value(),
        "populations": populations,
        "cross_population_matrix": cells,
        "hard_gate": {
            population_id: _hard_gate(manifests[population_id], within[population_id])
            for population_id in POPULATION_IDS
        },
        "test_partition_evaluated": False,
        "accumulated_with_stage2_formal_holdout": False,
    }
    save_result(result_path, value)
    return {
        "result": str(result_path),
        "cross_population_matrix": [
            {
                "training": cell["training_population_id"],
                "validation": cell["validation_population_id"],
                "conditional_uniform_mae": cell["conditional_uniform_validation_mae"],
                "sequential_mae": cell["sequential_validation_mae"],
                "delta_mae": cell["delta_mae_vs_conditional_uniform"],
                "physical_validity_passed": cell["physical_consistency"][
                    "blocking_gate_passed"
                ],
            }
            for cell in cells
        ],
        "hard_gate": value["hard_gate"],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        output = {
            "pilot_role": PILOT_ROLE,
            "populations": [plan.plan_value() for plan in stage3_population_plans()],
            "population_identities": {
                plan.population_id: plan.population_identity
                for plan in stage3_population_plans()
            },
        }
    elif arguments.command == "generate":
        plan = plan_for_population_id(arguments.population)
        report = generate_population(plan, arguments.output)
        output = report.manifest
    elif arguments.command == "train":
        output = _train_command(arguments)
    else:
        output = _matrix_command(arguments)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
