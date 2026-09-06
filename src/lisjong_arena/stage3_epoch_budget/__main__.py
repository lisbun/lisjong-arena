"""Arena #157 bounded epoch-budget adequacy studyのnarrow explicit CLI。

```text
plan       locked arms / single axis / comparison semanticsを出力する
verify     #150 retained artifactをstrict readbackし、identityを照合する
lock       live runtimeのexecution receiptを作る（installed pinsをfail closedで確認）
train      E80を1回だけtrainingし、model artifactをpublishする
compare    determinism gate / paired comparison / exhaustive outcomeを生成する
```

本childはdevelopment-onlyである。TEST partitionを選ぶoptionを持たず、seeds、
split、population、model family、learning rate、weight decay、batch、patience、
seed、bootstrap定数、classification条件をcaller optionにしない。`max_epochs`
すらoptionではなく、locked 80である。結果を見てepochを増やすoptionも、
additional seedやadditional dataを足すoptionも持たない。

`lock`が作るreceiptのidentityはE80 training開始前にIssue #157へ記録する。以降の
commandはそのreceipt fileを必須引数として受け取り、live runtimeと一致しなければ
実行を拒否する。
"""

import argparse
import json
import sys
import time
from pathlib import Path

from lisjong_arena.stage3_scale_learning_curve.result import evaluation_record

from .artifact import (
    model_manifest_without_weights,
    save_model_artifact,
    save_result,
)
from .comparison import determinism_gate
from .experiment import (
    budget_binding,
    budget_population_data,
    budget_train_view,
    configure_torch_runtime,
    train_budget_arm,
)
from .lock import comparison_semantics, current_receipt, require_current_lock
from .lock import validate_lock as validate_budget_lock
from .protocol import (
    ARMS,
    BASELINE_MAX_EPOCHS,
    BUDGET_ARM,
    BUDGET_MAX_EPOCHS,
    DECISION_RULE,
    EXECUTION_DECISION,
    PRIMARY_AXIS,
    RETRY_RULE,
    ROLE,
    BudgetError,
    ScaleError,
    baseline_training_lock,
    budget_training_lock,
    identity,
)
from .result import assemble_result
from .retained import load_retained, retained_value


def _read_lock(path: str) -> dict[str, object]:
    return validate_budget_lock(json.loads(Path(path).read_bytes()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the locked development-only Arena #157 epoch-budget adequacy study."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Print the locked arms and comparison semantics.")
    verify = commands.add_parser(
        "verify", help="Strict readback of the retained #150 artifacts."
    )
    verify.add_argument("--artifact-root", required=True)
    lock = commands.add_parser("lock", help="Build the live execution receipt.")
    lock.add_argument("--arena-revision", required=True)
    lock.add_argument("--artifact-root", required=True)
    lock.add_argument("--artifact-audit", required=True)
    lock.add_argument("--output", required=True)
    train = commands.add_parser("train", help="Train the E80 arm exactly once.")
    train.add_argument("--lock", required=True)
    train.add_argument("--artifact-root", required=True)
    train.add_argument("--artifact", required=True)
    compare = commands.add_parser(
        "compare", help="Assemble the paired comparison and the #157 outcome."
    )
    compare.add_argument("--lock", required=True)
    compare.add_argument("--artifact-root", required=True)
    compare.add_argument("--budget-artifact", required=True)
    compare.add_argument("--result", required=True)
    return parser


def _plan_command() -> dict[str, object]:
    return {
        "role": ROLE,
        "arms": list(ARMS),
        "primary_axis": PRIMARY_AXIS,
        "baseline_max_epochs": BASELINE_MAX_EPOCHS,
        "budget_max_epochs": BUDGET_MAX_EPOCHS,
        "retained": retained_value(),
        "baseline_training_lock": baseline_training_lock(),
        "budget_training_lock": budget_training_lock(),
        "comparison": comparison_semantics(),
        "decision_rule": DECISION_RULE,
        "retry_rule": RETRY_RULE,
        "execution_decision": EXECUTION_DECISION,
    }


def _verify_command(arguments) -> dict[str, object]:
    retained = load_retained(arguments.artifact_root)
    manifest = retained.manifest
    return {
        "artifact_root": str(Path(arguments.artifact_root)),
        "retained_execution_lock_identity": identity(retained.lock),
        "population_identity": retained.population["population_identity"],
        "raw_corpus_identity": retained.population["raw_corpus_identity"],
        "dataset_identity": retained.population["dataset_identity"],
        "weights_sha256": manifest["weights_sha256"],
        "scale": manifest["scale"],
        "selected_epoch": manifest["selected_epoch"],
        "epochs_run": len(manifest["loss_history"]),
        "train_seeds": [
            manifest["subset"]["train_seeds"][0],
            manifest["subset"]["train_seeds"][-1],
        ],
        "train_hanchan": len(manifest["subset"]["train_seeds"]),
        "train_anchors": len(manifest["train_anchor_identities"]),
        "validation_seeds": [
            manifest["subset"]["validation_seeds"][0],
            manifest["subset"]["validation_seeds"][-1],
        ],
        "validation_hanchan": len(manifest["subset"]["validation_seeds"]),
        "validation_anchors": len(
            manifest["evaluation"]["validation_anchor_identities"]
        ),
        "test_partition_present": manifest["subset"]["test_partition_present"],
        "pooled_validation_mae": manifest["evaluation"]["pooled_mae"],
        "regenerated": False,
    }


def _lock_command(arguments) -> dict[str, object]:
    destination = Path(arguments.output)
    if destination.exists():
        raise FileExistsError(f"lock destination already exists: {destination}")
    receipt = current_receipt(
        arena_revision=arguments.arena_revision,
        artifact_root=arguments.artifact_root,
        artifact_audit=arguments.artifact_audit,
    )
    from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(receipt))
    return {
        "lock": str(destination),
        "execution_lock_identity": identity(receipt),
        "runtime": receipt["runtime"],
        "retained_runtime": receipt["retained_runtime"],
        "platform_matches_retained": receipt["platform_matches_retained"],
        "source_revisions": receipt["provenance"]["source_revisions"],
        "budget_training_lock": receipt["budget_training_lock"],
    }


def _train_command(arguments) -> dict[str, object]:
    lock = _read_lock(arguments.lock)
    artifact_path = Path(arguments.artifact)
    if artifact_path.exists():
        raise FileExistsError(f"artifact destination already exists: {artifact_path}")
    # trainingとevaluationはinstalled lisjongのbelief mathに依存する。lockが宣言
    # するpinsだけでなく、いま走っているruntimeがそのlockと一致することを要求する。
    require_current_lock(lock, arguments.artifact_root)
    configure_torch_runtime()
    retained = load_retained(arguments.artifact_root)
    full = budget_population_data(retained.raw, retained.dataset)
    started = time.process_time()
    result = train_budget_arm(budget_train_view(full))
    training_cpu_seconds = time.process_time() - started
    evaluation = evaluation_record(result.validation, full, result.inference_throughput)
    loaded = save_model_artifact(
        artifact_path,
        result.model,
        model_manifest_without_weights(
            lock=lock,
            binding=budget_binding(full, retained.lock["provenance"]),
            result=result,
            evaluation=evaluation,
            training_cpu_seconds=training_cpu_seconds,
        ),
        retained.population,
        lock,
        retained.manifest,
    )
    manifest = loaded.manifest
    return {
        "arm": BUDGET_ARM,
        "artifact": str(artifact_path),
        "population_identity": retained.population["population_identity"],
        "dataset_identity": retained.population["dataset_identity"],
        "train_hanchan": len(manifest["subset"]["train_seeds"]),
        "train_anchors": len(manifest["train_anchor_identities"]),
        "max_epochs": manifest["training_lock"]["training_config"]["max_epochs"],
        "epochs_run": len(manifest["loss_history"]),
        "selected_epoch": manifest["selected_epoch"],
        "pooled_validation_mae": evaluation["pooled_mae"],
        "conditional_uniform_mae": evaluation["conditional_uniform_mae"],
        "physical_validity_passed": evaluation["physical_consistency"][
            "blocking_gate_passed"
        ],
        "determinism_gate": determinism_gate(
            retained.manifest["loss_history"], manifest["loss_history"]
        ),
        "weights_sha256": manifest["weights_sha256"],
        "cost": manifest["cost"],
    }


def _compare_command(arguments) -> dict[str, object]:
    from .artifact import load_model

    lock = _read_lock(arguments.lock)
    result_path = Path(arguments.result)
    if result_path.exists():
        raise FileExistsError(f"result destination already exists: {result_path}")
    require_current_lock(lock, arguments.artifact_root)
    configure_torch_runtime()
    retained = load_retained(arguments.artifact_root)
    # manifestだけを読まず、checkpointをlocked S2へstrict loadする。weightsを
    # shapeもkeyも違うstate dictへ差し替え、byte数とSHA-256を整合的に書き換えた
    # artifactは、strict loadを通らないとここで落ちる。
    _model, budget_manifest = load_model(
        arguments.budget_artifact, retained.population, lock, retained.manifest
    )
    value = assemble_result(
        retained.lock, retained.population, retained.manifest, budget_manifest, lock
    )
    save_result(result_path, value, lock)
    return {
        "result": str(result_path),
        "outcome": value["outcome"],
        "reasons": value["reasons"],
        "gates": value["gates"],
        "determinism_gate": value["determinism_gate"],
        "structural_monotonicity": value["structural_monotonicity"],
        "selected_epoch": {
            arm: value["metrics"][arm]["selected_epoch"] for arm in ARMS
        },
        "pooled_validation_mae": {
            arm: value["metrics"][arm]["pooled_expected_count_mae"] for arm in ARMS
        },
        "comparison": None
        if value["comparison"] is None
        else {
            "pooled_delta_mae": value["comparison"]["pooled_delta_mae"],
            "interval_lower": value["comparison"]["interval_lower"],
            "interval_upper": value["comparison"]["interval_upper"],
            "classification": value["comparison"]["classification"],
        },
        "cost_accounting": value["cost_accounting"],
        "selection_exposure": value["selection_exposure"],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "plan":
        output = _plan_command()
    elif arguments.command == "verify":
        output = _verify_command(arguments)
    elif arguments.command == "lock":
        output = _lock_command(arguments)
    elif arguments.command == "train":
        output = _train_command(arguments)
    else:
        output = _compare_command(arguments)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BudgetError, ScaleError) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        sys.exit(1)
