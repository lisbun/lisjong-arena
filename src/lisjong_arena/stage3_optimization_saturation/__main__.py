"""Arena #167 bounded optimization-budget saturation studyのnarrow explicit CLI。

```text
plan       locked arms / single axis / comparison semanticsを出力する
verify     #150 corpusと#157 E80 / resultをstrict readbackし、identityを照合する
lock       live runtimeのexecution receiptを作る（installed pinsをfail closedで確認）
train      E160をepoch 1から1回だけtrainingし、model artifactをpublishする
compare    determinism gate / paired comparison / exhaustive outcomeを生成する
```

本childはdevelopment-onlyである。TEST partitionを選ぶoptionを持たず、seeds、
split、population、model family、learning rate、weight decay、batch、patience、
seed、bootstrap定数、classification条件をcaller optionにしない。`max_epochs`
すらoptionではなく、locked 160である。結果を見てepochをさらに倍にするoptionも、
E80 checkpointからresumeするoptionも、additional seedやadditional dataを足す
optionも持たない。

`lock`が作るreceiptのidentityはE160 training開始前にIssue #167へ記録する。以降の
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
    configure_torch_runtime,
    saturation_binding,
    saturation_population_data,
    saturation_train_view,
    train_saturation_arm,
)
from .lock import comparison_semantics, current_receipt, require_current_lock
from .lock import validate_lock as validate_saturation_lock
from .protocol import (
    ARMS,
    BASELINE_MAX_EPOCHS,
    DECISION_RULE,
    EXECUTION_DECISION,
    NO_EXTENSION_RULE,
    PRIMARY_AXIS,
    RETRY_RULE,
    ROLE,
    SATURATION_ARM,
    SATURATION_MAX_EPOCHS,
    BudgetError,
    SaturationError,
    ScaleError,
    baseline_training_lock,
    identity,
    saturation_training_lock,
)
from .result import assemble_result
from .retained import load_predecessor_result, load_retained, retained_value


def _read_lock(path: str) -> dict[str, object]:
    return validate_saturation_lock(json.loads(Path(path).read_bytes()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the locked development-only Arena #167 optimization-budget "
            "saturation study."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Print the locked arms and comparison semantics.")
    verify = commands.add_parser(
        "verify", help="Strict readback of the retained #150 and #157 artifacts."
    )
    verify.add_argument("--corpus-root", required=True)
    verify.add_argument("--predecessor-root", required=True)
    lock = commands.add_parser("lock", help="Build the live execution receipt.")
    lock.add_argument("--arena-revision", required=True)
    lock.add_argument("--predecessor-root", required=True)
    lock.add_argument("--artifact-audit", required=True)
    lock.add_argument("--output", required=True)
    train = commands.add_parser("train", help="Train the E160 arm exactly once.")
    train.add_argument("--lock", required=True)
    train.add_argument("--corpus-root", required=True)
    train.add_argument("--predecessor-root", required=True)
    train.add_argument("--artifact", required=True)
    compare = commands.add_parser(
        "compare", help="Assemble the paired comparison and the #167 outcome."
    )
    compare.add_argument("--lock", required=True)
    compare.add_argument("--corpus-root", required=True)
    compare.add_argument("--predecessor-root", required=True)
    compare.add_argument("--saturation-artifact", required=True)
    compare.add_argument("--result", required=True)
    return parser


def _plan_command() -> dict[str, object]:
    return {
        "role": ROLE,
        "arms": list(ARMS),
        "primary_axis": PRIMARY_AXIS,
        "baseline_max_epochs": BASELINE_MAX_EPOCHS,
        "saturation_max_epochs": SATURATION_MAX_EPOCHS,
        "retained": retained_value(),
        "baseline_training_lock": baseline_training_lock(),
        "saturation_training_lock": saturation_training_lock(),
        "comparison": comparison_semantics(),
        "decision_rule": DECISION_RULE,
        "retry_rule": RETRY_RULE,
        "no_extension_rule": NO_EXTENSION_RULE,
        "execution_decision": EXECUTION_DECISION,
    }


def _verify_command(arguments) -> dict[str, object]:
    retained = load_retained(arguments.corpus_root, arguments.predecessor_root)
    manifest = retained.baseline_manifest
    predecessor_result = load_predecessor_result(
        arguments.predecessor_root, retained.predecessor_lock
    )
    return {
        "corpus_root": str(Path(arguments.corpus_root)),
        "predecessor_root": str(Path(arguments.predecessor_root)),
        "phase10_execution_lock_identity": identity(retained.phase10_lock),
        "predecessor_execution_lock_identity": identity(retained.predecessor_lock),
        "predecessor_result_identity": predecessor_result["result_identity"],
        "predecessor_outcome": predecessor_result["outcome"],
        "population_identity": retained.population["population_identity"],
        "raw_corpus_identity": retained.population["raw_corpus_identity"],
        "dataset_identity": retained.population["dataset_identity"],
        "phase10_weights_sha256": retained.scale_manifest["weights_sha256"],
        "baseline_weights_sha256": manifest["weights_sha256"],
        "baseline_arm": manifest["arm"],
        "baseline_selected_epoch": manifest["selected_epoch"],
        "baseline_epochs_run": len(manifest["loss_history"]),
        "baseline_loss_history_digest": retained_value()[
            "baseline_loss_history_digest"
        ],
        "baseline_max_epochs": manifest["training_lock"]["training_config"][
            "max_epochs"
        ],
        "baseline_patience": manifest["training_lock"]["training_config"]["patience"],
        "baseline_pooled_validation_mae": manifest["evaluation"]["pooled_mae"],
        "baseline_runtime": manifest["runtime"],
        "baseline_source_revisions": retained.predecessor_lock["provenance"][
            "source_revisions"
        ],
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
        "regenerated": False,
        "baseline_retrained": False,
    }


def _lock_command(arguments) -> dict[str, object]:
    destination = Path(arguments.output)
    if destination.exists():
        raise FileExistsError(f"lock destination already exists: {destination}")
    receipt = current_receipt(
        arena_revision=arguments.arena_revision,
        predecessor_root=arguments.predecessor_root,
        artifact_audit=arguments.artifact_audit,
    )
    from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(receipt))
    return {
        "lock": str(destination),
        "execution_lock_identity": identity(receipt),
        "runtime": receipt["runtime"],
        "predecessor_runtime": receipt["predecessor_runtime"],
        "platform_matches_predecessor": receipt["platform_matches_predecessor"],
        "source_revisions": receipt["provenance"]["source_revisions"],
        "baseline_training_lock": receipt["baseline_training_lock"],
        "saturation_training_lock": receipt["saturation_training_lock"],
    }


def _train_command(arguments) -> dict[str, object]:
    lock = _read_lock(arguments.lock)
    artifact_path = Path(arguments.artifact)
    if artifact_path.exists():
        raise FileExistsError(f"artifact destination already exists: {artifact_path}")
    # trainingとevaluationはinstalled lisjongのbelief mathに依存する。lockが宣言
    # するpinsだけでなく、いま走っているruntimeがそのlockと一致することを要求する。
    require_current_lock(lock, arguments.predecessor_root)
    configure_torch_runtime()
    retained = load_retained(arguments.corpus_root, arguments.predecessor_root)
    full = saturation_population_data(retained.raw, retained.dataset)
    started = time.process_time()
    result = train_saturation_arm(saturation_train_view(full))
    training_cpu_seconds = time.process_time() - started
    evaluation = evaluation_record(result.validation, full, result.inference_throughput)
    loaded = save_model_artifact(
        artifact_path,
        result.model,
        model_manifest_without_weights(
            lock=lock,
            binding=saturation_binding(full, retained.phase10_lock["provenance"]),
            result=result,
            evaluation=evaluation,
            training_cpu_seconds=training_cpu_seconds,
        ),
        retained.population,
        lock,
        retained.baseline_manifest,
    )
    manifest = loaded.manifest
    return {
        "arm": SATURATION_ARM,
        "artifact": str(artifact_path),
        "population_identity": retained.population["population_identity"],
        "dataset_identity": retained.population["dataset_identity"],
        "train_hanchan": len(manifest["subset"]["train_seeds"]),
        "train_anchors": len(manifest["train_anchor_identities"]),
        "max_epochs": manifest["training_lock"]["training_config"]["max_epochs"],
        "patience": manifest["training_lock"]["training_config"]["patience"],
        "epochs_run": len(manifest["loss_history"]),
        "selected_epoch": manifest["selected_epoch"],
        "pooled_validation_mae": evaluation["pooled_mae"],
        "conditional_uniform_mae": evaluation["conditional_uniform_mae"],
        "physical_validity_passed": evaluation["physical_consistency"][
            "blocking_gate_passed"
        ],
        "determinism_gate": determinism_gate(
            retained.baseline_manifest["loss_history"], manifest["loss_history"]
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
    require_current_lock(lock, arguments.predecessor_root)
    configure_torch_runtime()
    retained = load_retained(arguments.corpus_root, arguments.predecessor_root)
    predecessor_result = load_predecessor_result(
        arguments.predecessor_root, retained.predecessor_lock
    )
    # manifestだけを読まず、checkpointをlocked S2へstrict loadする。weightsを
    # shapeもkeyも違うstate dictへ差し替え、byte数とSHA-256を整合的に書き換えた
    # artifactは、strict loadを通らないとここで落ちる。
    _model, saturation_manifest = load_model(
        arguments.saturation_artifact,
        retained.population,
        lock,
        retained.baseline_manifest,
    )
    value = assemble_result(
        retained.phase10_lock,
        retained.population,
        retained.scale_manifest,
        retained.predecessor_lock,
        retained.baseline_manifest,
        saturation_manifest,
        lock,
    )
    save_result(result_path, value, lock)
    return {
        "result": str(result_path),
        "predecessor_result_identity": predecessor_result["result_identity"],
        "predecessor_outcome": predecessor_result["outcome"],
        "outcome": value["outcome"],
        "reasons": value["reasons"],
        "gates": value["gates"],
        "determinism_gate": value["determinism_gate"],
        "structural_monotonicity": value["structural_monotonicity"],
        "saturation": value["saturation"],
        "selected_epoch": {
            arm: value["metrics"][arm]["selected_epoch"] for arm in ARMS
        },
        "epochs_run": {arm: value["metrics"][arm]["epochs_run"] for arm in ARMS},
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
            "positive_hanchan": value["comparison"]["positive_hanchan"],
            "negative_hanchan": value["comparison"]["negative_hanchan"],
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
    except (SaturationError, BudgetError, ScaleError) as error:
        print(f"STOP / INVALID: {error}", file=sys.stderr)
        sys.exit(1)
