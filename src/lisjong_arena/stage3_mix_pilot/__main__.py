"""Narrow explicit Arena #148 population-mix pilot CLI.

```text
plan       locked 3 armのexact identityを出力する
generate   1 armのraw corpus / dataset / manifestを生成する
train      1 armでfixed-budget S2をtrainingする
matrix     3 x 3 cross-population evaluationとfinal classificationを生成する
```

本pilotはdevelopment-onlyである。TEST partitionを選ぶoptionを持たず、
training budget、model family、seeds、split、augmentation fraction、
classification条件をcaller optionにしない。結果を見てからseedを追加・置換する
optionも持たない。
"""

import argparse
import json
import sys
from pathlib import Path

from lisjong_arena.stage3_mix_pilot.artifact import (
    execution_runtime_value,
    load_model,
    model_manifest_without_weights,
    save_model_artifact,
    save_result,
)
from lisjong_arena.stage3_mix_pilot.comparison import compare_against_control
from lisjong_arena.stage3_mix_pilot.experiment import (
    CANDIDATE,
    REFERENCE_ARM_ID,
    build_arm_data,
    configure_torch_runtime,
    evaluate_on_population,
    evaluation_value,
    inventory_summary,
    train_population_candidate,
)
from lisjong_arena.stage3_mix_pilot.generation import (
    generate_mix_arm,
    load_population,
)
from lisjong_arena.stage3_mix_pilot.population import mix_arm_plan, mix_arm_plans
from lisjong_arena.stage3_mix_pilot.protocol import (
    ARM_IDS,
    CONTROL_ARM_ID,
    ORDERED_SEEDS,
    PILOT_ROLE,
    RESULT_SCHEMA_VERSION,
    RETRY_RULE,
    SELECTION_RULE,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)
from lisjong_arena.stage3_mix_pilot.result import classify, selected_recipe

CANDIDATE_ARM_IDS = tuple(name for name in ARM_IDS if name != CONTROL_ARM_ID)


def _keyed_path(value: str) -> tuple[str, Path]:
    key, separator, path = value.partition("=")
    if not separator or key not in ARM_IDS or not path:
        raise argparse.ArgumentTypeError(
            "expected <arm-id>=<path> with an arm id of A, B or C"
        )
    return key, Path(path)


def _keyed_paths(values: list[tuple[str, Path]], name: str) -> dict[str, Path]:
    mapping = dict(values)
    if len(mapping) != len(values):
        raise SystemExit(f"duplicate {name} arm id")
    if tuple(sorted(mapping)) != ARM_IDS:
        raise SystemExit(f"{name} must be given exactly once for A, B and C")
    return mapping


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Run the locked development-only Arena #148 population-mix pilot.")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Print the locked arm plan identities.")
    generate = commands.add_parser(
        "generate", help="Generate one arm raw corpus and dataset."
    )
    generate.add_argument("--arm", required=True, choices=ARM_IDS)
    generate.add_argument("--output", required=True)
    train = commands.add_parser(
        "train", help="Train the fixed S2 candidate on one arm."
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


def _arm_data(directory: Path):
    manifest, persisted_raw, persisted_dataset = load_population(directory)
    data = build_arm_data(
        arm_id=manifest["arm_id"],
        population_identity=manifest["population_identity"],
        persisted_raw=persisted_raw,
        dataset=persisted_dataset.dataset,
    )
    return manifest, data


def _plan_command() -> dict[str, object]:
    plans = mix_arm_plans()
    return {
        "pilot_role": PILOT_ROLE,
        "retry_rule": RETRY_RULE,
        "selection_rule": SELECTION_RULE,
        "ordered_seeds": list(ORDERED_SEEDS),
        "train_seeds": list(TRAIN_SEEDS),
        "validation_seeds": list(VALIDATION_SEEDS),
        "arms": [plan.plan_value() for plan in plans],
        "population_identities": {
            plan.arm_id: plan.population_identity for plan in plans
        },
    }


def _train_command(arguments) -> dict[str, object]:
    artifact_path = Path(arguments.artifact)
    if artifact_path.exists():
        raise FileExistsError(f"artifact destination already exists: {artifact_path}")
    manifest, data = _arm_data(Path(arguments.population_dir))
    plan = mix_arm_plan(data.population_id)
    if plan.population_identity != data.population_identity:
        raise SystemExit(
            "persisted population identity differs from the locked mix pilot plan"
        )
    result = train_population_candidate(data)
    loaded = save_model_artifact(
        artifact_path,
        result.model,
        model_manifest_without_weights(
            arm_id=data.population_id,
            population_identity=data.population_identity,
            raw_corpus_identity=data.raw_corpus_identity,
            dataset_identity=data.dataset_identity,
            inventory=inventory_summary(data),
            result=result,
            runtime=execution_runtime_value(),
        ),
    )
    return {
        "arm_id": data.population_id,
        "population_identity": data.population_identity,
        "raw_corpus_identity": data.raw_corpus_identity,
        "dataset_identity": data.dataset_identity,
        "weights_sha256": loaded.manifest["weights_sha256"],
        "selected_epoch": loaded.manifest["selected_epoch"],
        "parameter_count": loaded.manifest["parameter_count"],
        "training_wall_clock_seconds": loaded.manifest["training_wall_clock_seconds"],
        "within_arm_validation": loaded.manifest["within_arm_validation"],
        "generation_cost": manifest["cost"],
    }


def _arm_value(manifest: dict, data, model_manifest: dict) -> dict[str, object]:
    """1 armのresult entry。

    `validate_result_value()`はこのentryからoutcomeを再導出するため、
    classificationに必要なevidence（`arm_id` / provenance / coverage /
    retention / cost / plan / source attribution / split policy /
    TEST partition flag）をすべてbindする。
    """
    return {
        "arm_id": manifest["arm_id"],
        "split_policy_id": manifest["split_policy_id"],
        "test_partition_present": manifest["test_partition_present"],
        "population_identity": data.population_identity,
        "population_plan": manifest["population_plan"],
        "raw_corpus_identity": data.raw_corpus_identity,
        "dataset_identity": data.dataset_identity,
        "inventory": inventory_summary(data),
        "provenance": manifest["provenance"],
        "generation_runtime": manifest["generation_runtime"],
        "coverage": manifest["coverage"],
        "generation_cost": manifest["cost"],
        "cost_rates": manifest["cost_rates"],
        "distribution_effect": manifest["distribution_effect"],
        "source_attribution": manifest["source_attribution"],
        "dataset_retention": {
            key: value
            for key, value in manifest["dataset_retention"].items()
            if key != "kan_event_rows"
        },
        "dataset_conditional_uniform_baseline": manifest[
            "conditional_uniform_baseline"
        ],
        "model": {
            "weights_sha256": model_manifest["weights_sha256"],
            "weights_bytes": model_manifest["weights_bytes"],
            "parameter_count": model_manifest["parameter_count"],
            "selected_epoch": model_manifest["selected_epoch"],
            "loss_history": model_manifest["loss_history"],
            "training_wall_clock_seconds": model_manifest[
                "training_wall_clock_seconds"
            ],
            "peak_process_ram_bytes": model_manifest["peak_process_ram_bytes"],
            "inference_samples_per_second": model_manifest[
                "inference_samples_per_second"
            ],
            "training_config": model_manifest["training_config"],
            "runtime": model_manifest["runtime"],
        },
    }


def _matrix_command(arguments) -> dict[str, object]:
    result_path = Path(arguments.result)
    if result_path.exists():
        raise FileExistsError(f"result destination already exists: {result_path}")
    configure_torch_runtime()
    population_paths = _keyed_paths(arguments.population, "--population")
    model_paths = _keyed_paths(arguments.model, "--model")
    manifests = {}
    data_by_id = {}
    for arm_id in ARM_IDS:
        manifest, data = _arm_data(population_paths[arm_id])
        if data.population_id != arm_id:
            raise SystemExit(f"{population_paths[arm_id]} is not arm {arm_id}")
        plan = mix_arm_plan(arm_id)
        if plan.population_identity != data.population_identity:
            raise SystemExit(f"arm {arm_id} identity differs from the locked plan")
        manifests[arm_id] = manifest
        data_by_id[arm_id] = data
    identities = {value.dataset_identity for value in data_by_id.values()}
    if len(identities) != len(ARM_IDS):
        raise SystemExit("arm dataset identities must be distinct")

    cells = []
    model_manifests = {}
    for training_id in ARM_IDS:
        model, model_manifest = load_model(model_paths[training_id])
        if model_manifest["training_arm_id"] != training_id:
            raise SystemExit(
                f"model {model_paths[training_id]} was not trained on arm {training_id}"
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
        for validation_id in ARM_IDS:
            evaluation = evaluate_on_population(model, data_by_id[validation_id])
            cell = evaluation_value(evaluation, data_by_id[validation_id])
            cell["training_population_id"] = training_id
            cell["training_population_identity"] = expected.population_identity
            cells.append(cell)

    cell_by_pair = {
        (cell["training_population_id"], cell["validation_population_id"]): cell
        for cell in cells
    }
    comparisons = [
        compare_against_control(
            candidate_arm_id=candidate_id,
            validation_arm_id=validation_id,
            control_cell=cell_by_pair[(CONTROL_ARM_ID, validation_id)],
            candidate_cell=cell_by_pair[(candidate_id, validation_id)],
        )
        for candidate_id in CANDIDATE_ARM_IDS
        for validation_id in ARM_IDS
    ]
    outcome, reasons, gates = classify(manifests, cells, comparisons)
    value = {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "pilot_role": PILOT_ROLE,
        "candidate": CANDIDATE.value,
        "reference_arm_id": REFERENCE_ARM_ID,
        "retry_rule": RETRY_RULE,
        "selection_rule": SELECTION_RULE,
        "evaluation_runtime": execution_runtime_value(),
        "arms": {
            arm_id: _arm_value(
                manifests[arm_id], data_by_id[arm_id], model_manifests[arm_id]
            )
            for arm_id in ARM_IDS
        },
        "cross_population_matrix": cells,
        "paired_comparisons": comparisons,
        "gates": gates,
        "outcome": outcome,
        "outcome_reasons": list(reasons),
        "selected_recipe": selected_recipe(outcome, manifests),
        "test_partition_evaluated": False,
        "accumulated_with_historical_evidence": False,
    }
    save_result(result_path, value)
    return {
        "result": str(result_path),
        "outcome": outcome,
        "outcome_reasons": list(reasons),
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
        "paired_comparisons": [
            {
                "candidate": row["candidate_arm_id"],
                "validation": row["validation_population_id"],
                "pooled_delta_mae": row["pooled_delta_mae"],
                "interval_lower": row["interval_lower"],
                "interval_upper": row["interval_upper"],
                "classification": row["classification"],
            }
            for row in comparisons
        ],
        "selected_recipe": value["selected_recipe"],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        output = _plan_command()
    elif arguments.command == "generate":
        plan = mix_arm_plan(arguments.arm)
        output = generate_mix_arm(arguments.output, plan).manifest
    elif arguments.command == "train":
        output = _train_command(arguments)
    else:
        output = _matrix_command(arguments)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
