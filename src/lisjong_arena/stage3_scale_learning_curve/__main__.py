"""Arena #150 Phase 10 bounded scale learning curveのnarrow explicit CLI。

```text
plan       locked seed plan / population identity / freshness auditを出力する
lock       live runtimeのexecution receiptを作る（installed pinsをfail closedで確認）
generate   locked 80-hanchan raw corpusとPhase 5 datasetを一度だけ生成する
train      1 scaleでlocked S2をtrainingし、model artifactをpublishする
curve      3 scaleのpaired learning curveとexhaustive outcomeを生成する
```

本childはdevelopment-onlyである。TEST partitionを選ぶoptionを持たず、seeds、
split、augmentation fraction、model family、training budget、bootstrap定数、
classification条件をcaller optionにしない。結果を見てからseedを追加・置換する
optionも、128+へextendするoptionも持たない。

`lock`が作るreceiptのidentityはgeneration前にIssue #150へ記録する。以降の
commandはそのreceipt fileを必須引数として受け取り、live runtimeと一致しなければ
実行を拒否する。
"""

import argparse
import json
import sys
import time
from pathlib import Path

from .artifact import (
    model_manifest_without_weights,
    save_model_artifact,
    save_result,
)
from .experiment import (
    build_data,
    configure_torch_runtime,
    train_scale,
    training_binding,
)
from .generation import generate_population, load_population
from .lock import current_receipt, require_current_lock, validate_lock
from .population import plan_value, population_identity, recipe_value
from .protocol import (
    DECISION_RULE,
    EXECUTION_DECISION,
    ORDERED_SEEDS,
    RETRY_RULE,
    ROLE,
    SCALES,
    ScaleError,
    check_freshness,
    identity,
    train_seeds,
    training_lock,
)
from .result import assemble_result, evaluation_record


def _read_lock(path: str) -> dict[str, object]:
    return validate_lock(json.loads(Path(path).read_bytes()))


def _keyed_path(value: str) -> tuple[str, Path]:
    key, separator, path = value.partition("=")
    if not separator or key not in SCALES or not path:
        raise argparse.ArgumentTypeError(
            "expected <scale>=<path> with a scale of S16, S32 or S64"
        )
    return key, Path(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the locked development-only Arena #150 Phase 10 scale learning curve."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Print the locked Phase 10 plan and identities.")
    lock = commands.add_parser("lock", help="Build the live execution receipt.")
    lock.add_argument("--arena-revision", required=True)
    lock.add_argument("--seed-audit", required=True)
    lock.add_argument("--output", required=True)
    generate = commands.add_parser(
        "generate", help="Generate the locked 80-hanchan corpus and dataset."
    )
    generate.add_argument("--lock", required=True)
    generate.add_argument("--output", required=True)
    train = commands.add_parser("train", help="Train the fixed S2 model on one scale.")
    train.add_argument("--lock", required=True)
    train.add_argument("--population-dir", required=True)
    train.add_argument("--scale", required=True, choices=SCALES)
    train.add_argument("--artifact", required=True)
    curve = commands.add_parser(
        "curve", help="Assemble the paired learning curve and the Phase 10 outcome."
    )
    curve.add_argument("--lock", required=True)
    curve.add_argument("--population-dir", required=True)
    curve.add_argument("--model", required=True, action="append", type=_keyed_path)
    curve.add_argument("--result", required=True)
    return parser


def _plan_command() -> dict[str, object]:
    outcome, overlap = check_freshness(ORDERED_SEEDS)
    return {
        "role": ROLE,
        "decision_rule": DECISION_RULE,
        "retry_rule": RETRY_RULE,
        "execution_decision": EXECUTION_DECISION,
        "population_plan": plan_value(),
        "population_identity": population_identity(),
        "carry_forward_recipe": recipe_value(),
        "training_lock": training_lock(),
        "subsets": {scale: list(train_seeds(scale)) for scale in SCALES},
        "freshness": {"outcome": outcome, "collisions": overlap},
    }


def _lock_command(arguments) -> dict[str, object]:
    destination = Path(arguments.output)
    if destination.exists():
        raise FileExistsError(f"lock destination already exists: {destination}")
    receipt = current_receipt(
        arena_revision=arguments.arena_revision, seed_audit=arguments.seed_audit
    )
    from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(receipt))
    return {
        "lock": str(destination),
        "execution_lock_identity": identity(receipt),
        "runtime": receipt["runtime"],
        "source_revisions": receipt["provenance"]["source_revisions"],
    }


def _train_command(arguments) -> dict[str, object]:
    lock = _read_lock(arguments.lock)
    artifact_path = Path(arguments.artifact)
    if artifact_path.exists():
        raise FileExistsError(f"artifact destination already exists: {artifact_path}")
    # trainingとevaluationはinstalled lisjongのbelief mathに依存する。lockが宣言
    # するpinsだけでなく、いま走っているruntimeがそのlockと一致することを要求する。
    require_current_lock(lock)
    configure_torch_runtime()
    population, raw, dataset = load_population(arguments.population_dir, lock)
    full = build_data(raw, dataset)
    scale = arguments.scale
    started = time.process_time()
    result = train_scale(full, scale)
    training_cpu_seconds = time.process_time() - started
    evaluation = evaluation_record(result.validation, full, result.inference_throughput)
    loaded = save_model_artifact(
        artifact_path,
        result.model,
        model_manifest_without_weights(
            scale=scale,
            lock=lock,
            binding=training_binding(full, scale, lock["provenance"]),
            result=result,
            evaluation=evaluation,
            training_cpu_seconds=training_cpu_seconds,
        ),
        population,
        lock,
    )
    manifest = loaded.manifest
    return {
        "scale": scale,
        "artifact": str(artifact_path),
        "population_identity": population["population_identity"],
        "dataset_identity": population["dataset_identity"],
        "train_hanchan": len(manifest["subset"]["train_seeds"]),
        "train_anchors": len(manifest["train_anchor_identities"]),
        "selected_epoch": manifest["selected_epoch"],
        "pooled_validation_mae": evaluation["pooled_mae"],
        "conditional_uniform_mae": evaluation["conditional_uniform_mae"],
        "physical_validity_passed": evaluation["physical_consistency"][
            "blocking_gate_passed"
        ],
        "weights_sha256": manifest["weights_sha256"],
        "cost": manifest["cost"],
    }


def _curve_command(arguments) -> dict[str, object]:
    from .artifact import load_model_artifact

    lock = _read_lock(arguments.lock)
    result_path = Path(arguments.result)
    if result_path.exists():
        raise FileExistsError(f"result destination already exists: {result_path}")
    require_current_lock(lock)
    configure_torch_runtime()
    population, _raw, _dataset = load_population(arguments.population_dir, lock)
    model_paths = dict(arguments.model)
    if len(model_paths) != len(arguments.model) or sorted(model_paths) != sorted(
        SCALES
    ):
        raise SystemExit("--model must be given exactly once for S16, S32 and S64")
    models = {}
    for scale in SCALES:
        manifest = load_model_artifact(model_paths[scale], population, lock).manifest
        if manifest["scale"] != scale:
            raise SystemExit(f"{model_paths[scale]} is not the {scale} artifact")
        models[scale] = manifest
    value = assemble_result(population, models, lock)
    save_result(result_path, value, lock)
    return {
        "result": str(result_path),
        "outcome": value["outcome"],
        "reasons": value["reasons"],
        "gates": value["gates"],
        "pooled_validation_mae": {
            scale: models[scale]["evaluation"]["pooled_mae"] for scale in SCALES
        },
        "comparisons": [
            {
                "smaller": row["smaller"],
                "larger": row["larger"],
                "pooled_delta_mae": row["pooled_delta_mae"],
                "interval_lower": row["interval_lower"],
                "interval_upper": row["interval_upper"],
                "classification": row["classification"],
            }
            for row in value["comparisons"]
        ],
        "cost_accounting": value["cost_accounting"],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        output = _plan_command()
    elif arguments.command == "lock":
        output = _lock_command(arguments)
    elif arguments.command == "generate":
        lock = _read_lock(arguments.lock)
        output = generate_population(arguments.output, lock)
    elif arguments.command == "train":
        output = _train_command(arguments)
    else:
        output = _curve_command(arguments)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ScaleError as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        sys.exit(1)
