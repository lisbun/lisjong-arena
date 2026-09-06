"""Offline Q CLI: `generate` -> `train-bc` / `train-q` -> `test` -> `smoke`
-> `freeze` -> `screen` (Issue #140), plus the artifact-only diagnosis
(`#152`), P1 Gate A (`#158`) and P1 Gate B (`#162`) commands.

```text
python -m lisjong_arena.learned_policy_offline_q generate  --dataset DIR --report FILE
python -m lisjong_arena.learned_policy_offline_q train-bc  --dataset DIR --checkpoint DIR
python -m lisjong_arena.learned_policy_offline_q train-q   --dataset DIR --checkpoint DIR
python -m lisjong_arena.learned_policy_offline_q test      --dataset DIR \
    --bc-checkpoint DIR --q-checkpoint DIR --result FILE
python -m lisjong_arena.learned_policy_offline_q generate-replacement-test \
    --artifact DIR --report FILE
python -m lisjong_arena.learned_policy_offline_q evaluate-replacement-test \
    --artifact DIR --bc-checkpoint DIR --q-checkpoint DIR --result FILE
python -m lisjong_arena.learned_policy_offline_q smoke     \
    --bc-checkpoint DIR --q-checkpoint DIR --report FILE
python -m lisjong_arena.learned_policy_offline_q freeze    \
    --bc-checkpoint DIR --q-checkpoint DIR \
    --retention-backend NAME --retention-root DIR --retention-key KEY
python -m lisjong_arena.learned_policy_offline_q screen    \
    --bundle DIR --artifact FILE --result FILE
python -m lisjong_arena.learned_policy_offline_q diagnose  \
    --bundle DIR --dataset DIR --replacement-test DIR --result FILE
python -m lisjong_arena.learned_policy_offline_q record-classification \
    --result FILE --outcome NAME --classified-result FILE
python -m lisjong_arena.learned_policy_offline_q p1-gate-a \
    --bundle DIR --dataset DIR --replacement-test DIR --result FILE
python -m lisjong_arena.learned_policy_offline_q p1-record-classification \
    --result FILE --outcome NAME --classified-result FILE
python -m lisjong_arena.learned_policy_offline_q p1-materialize \
    --bundle DIR [--weights FILE | --dataset DIR] \
    --retention-root DIR [--retention-backend NAME] [--retention-key KEY]
python -m lisjong_arena.learned_policy_offline_q p1-gate-b \
    --checkpoint DIR --artifact FILE --result FILE
python -m lisjong_arena.learned_policy_offline_q p1-gate-b-record-classification \
    --result FILE --outcome NAME --classified-result FILE
```

`generate`/`train-bc`/`train-q`はTEST partitionのmetricを一切計算しない。
`test`だけがfrozen checkpointに対してTESTを1回評価し、その実行でのみTEST
exposureを行う。`smoke`はserving semantics検証のみでmodel tuningへ使わない。
`screen`はvalid smoke後にのみ実行する。

`diagnose`と`record-classification`はIssue #152のartifact-only failure
diagnosisであり、新しいgame / seed / training / strength evidenceを作らない。
`diagnose`はretained artifactのstrict readbackだけで動き、outcome
classificationを記録しない。`record-classification`はreview後の
exhaustive outcomeを1件だけ付与する。

`p1-gate-a`と`p1-record-classification`はIssue #158のP1 Gate Aである。
retained artifactのstrict readbackだけを入力に取り、keep-shanten discard
featureを足したP1 derived viewの上でQ-v2を学習し、dataset TEST /
replacement TESTをprimary roleとして1回だけ比較する。新しいgame / seed /
hanchan / TEST exposureを作らない。outcomeはIssue #158が事前lockしたladderが
metricsから機械的に導出し、`p1-record-classification`はその導出結果と一致する
1件だけを記録できる。

`p1-materialize` / `p1-gate-b` / `p1-gate-b-record-classification`はIssue
#162のP1 Gate Bである。`p1-materialize`はexact #158 candidateを
serving checkpointへwrite-onceでmaterializeし（`--weights`がexact retained
weights、`--dataset`がexact deterministic reconstruction）、canonical weights
digestが一致しなければfail closedする。`p1-gate-b`はそのcandidateを
passive tsumogiri x3へ対して既存single-round評価で1回だけ走らせ、immutable
artifactとcanonical summaryを正本とするresult documentを書く。outcomeは
canonical seed-block intervalから機械的に導出し、
`p1-gate-b-record-classification`がその導出結果と一致する1件だけを記録できる。
"""

import argparse
import sys
from pathlib import Path

from lisjong_arena._artifact_io import canonical_json_text

from .artifact import (
    FEATURES_FILENAME,
    LEGAL_MASK_FILENAME,
    MANIFEST_FILENAME,
    NEXT_FEATURES_FILENAME,
    NEXT_LEGAL_MASK_FILENAME,
    ROWS_FILENAME,
    OfflineQDatasetWriter,
    load_dataset,
)
from .protocol import (
    DATASET_ORDERED_SEEDS,
    SERVING_SMOKE_SEEDS,
    Split,
    verify_contract_identity,
)
from .recording import record_replacement_test_game, record_teacher_game
from .support import build_support_gate_report
from .transitions import build_macro_transitions


def _write_json(path: Path, document: dict) -> None:
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json_text(document), encoding="utf-8", newline="\n")


def _generate(arguments: argparse.Namespace) -> int:
    verify_contract_identity()
    dataset_path = Path(arguments.dataset)
    writer = OfflineQDatasetWriter(dataset_path)
    measurements = []
    try:
        for seed in DATASET_ORDERED_SEEDS:
            recording = record_teacher_game(seed)
            entry = writer.add_game(
                seed=seed,
                split=recording.split,
                scores=recording.result.scores,
                ranks=recording.result.ranks,
                rows=build_macro_transitions(recording),
            )
            measurements.append(
                {
                    "seed": seed,
                    "split": recording.split.value,
                    "macro_transition_rows": entry.row_count,
                    "wall_clock_seconds": recording.wall_clock_seconds,
                    "cpu_seconds": recording.cpu_seconds,
                }
            )
            print(
                f"seed={seed} split={recording.split.value} "
                f"rows={entry.row_count} "
                f"wall={recording.wall_clock_seconds:.2f}s",
                flush=True,
            )
        dataset = writer.finalize()
    except BaseException:
        writer.discard()
        raise

    non_finite = dataset.count_non_finite_features()
    support = build_support_gate_report(dataset)
    file_bytes = {
        name: (dataset.path / name).stat().st_size
        for name in (
            MANIFEST_FILENAME,
            ROWS_FILENAME,
            FEATURES_FILENAME,
            LEGAL_MASK_FILENAME,
            NEXT_FEATURES_FILENAME,
            NEXT_LEGAL_MASK_FILENAME,
        )
    }
    document = {
        "dataset_identity": dataset.identity,
        "non_finite_feature_count": non_finite,
        "games": measurements,
        "generation_totals": {
            "hanchan_count": len(measurements),
            "macro_transition_rows": dataset.row_count,
            "wall_clock_seconds": sum(
                entry["wall_clock_seconds"] for entry in measurements
            ),
            "cpu_seconds": sum(entry["cpu_seconds"] for entry in measurements),
        },
        "storage": {
            "file_bytes": file_bytes,
            "total_bytes": sum(file_bytes.values()),
        },
        "support_gate": support.to_document(),
    }
    _write_json(Path(arguments.report), document)
    print(f"dataset_identity={dataset.identity}")
    print(f"rows={dataset.row_count} non_finite_features={non_finite}")
    print(f"supported_indices={len(support.supported_indices)}")
    print(f"unsupported_indices={len(support.unsupported_indices)}")
    print(
        f"combined_support_complete_rate={support.combined_support_complete_rate:.6f}"
    )
    return 0


def _train_bc(arguments: argparse.Namespace) -> int:
    from .bc_training import save_checkpoint, train_bc_model

    dataset = load_dataset(arguments.dataset)
    run = train_bc_model(dataset)
    checkpoint = save_checkpoint(arguments.checkpoint, dataset, run)
    print(f"dataset_identity={dataset.identity}")
    print(f"checkpoint_identity={checkpoint.identity}")
    print(f"selected_epoch={run.selected_epoch}")
    print(
        "selected_validation_choice_masked_ce="
        f"{run.selected_validation_choice_masked_ce:.6f}"
    )
    return 0


def _train_q(arguments: argparse.Namespace) -> int:
    from .q_training import save_checkpoint, train_q_model

    dataset = load_dataset(arguments.dataset)
    run = train_q_model(dataset)
    checkpoint = save_checkpoint(arguments.checkpoint, dataset, run)
    print(f"dataset_identity={dataset.identity}")
    print(f"checkpoint_identity={checkpoint.identity}")
    print(f"selected_epoch={run.selected_epoch}")
    print(f"final_validation_huber_loss={run.final_validation_huber_loss:.6f}")
    print(f"supported_indices={int(run.support_mask.sum())}")
    return 0


def _test(arguments: argparse.Namespace) -> int:
    from . import bc_training, q_training
    from .exposure_evaluation import evaluate_bc_test, evaluate_q_test
    from .split_tensors import load_split_tensors

    dataset = load_dataset(arguments.dataset)
    bc_checkpoint = bc_training.load_checkpoint(arguments.bc_checkpoint)
    q_checkpoint = q_training.load_checkpoint(arguments.q_checkpoint)
    for name, checkpoint in (("BC", bc_checkpoint), ("Q", q_checkpoint)):
        if checkpoint.manifest["dataset_identity"] != dataset.identity:
            raise SystemExit(f"{name} checkpoint was not trained on this dataset")

    tensors = load_split_tensors(dataset)
    bc_diagnostics = evaluate_bc_test(bc_checkpoint.model, tensors[Split.TEST])
    q_diagnostics = evaluate_q_test(
        q_checkpoint.model, tensors[Split.TRAIN], tensors[Split.TEST]
    )
    document = {
        "dataset_identity": dataset.identity,
        "bc_checkpoint_identity": bc_checkpoint.identity,
        "q_checkpoint_identity": q_checkpoint.identity,
        "bc": bc_diagnostics.to_document(),
        "q": q_diagnostics.to_document(),
    }
    _write_json(Path(arguments.result), document)
    print(f"BC choice masked CE      {bc_diagnostics.choice_masked_cross_entropy:.6f}")
    print(f"BC choice exact agreement {bc_diagnostics.choice_exact_agreement:.6f}")
    print(f"Q selected-action Huber  {q_diagnostics.selected_action_huber_loss:.6f}")
    print(f"Q finite Q rate          {q_diagnostics.finite_q_rate:.6f}")
    return 0


def _generate_replacement_test(arguments: argparse.Namespace) -> int:
    """locked replacement TEST population 354..359をTEST-only artifactとして生成する。

    original training datasetへappendせず、独立したartifact identityを持つ。
    """
    from .protocol import REPLACEMENT_TEST_SEEDS
    from .replacement_test import ReplacementTestWriter

    verify_contract_identity()
    writer = ReplacementTestWriter(Path(arguments.artifact))
    measurements = []
    try:
        for seed in REPLACEMENT_TEST_SEEDS:
            recording = record_replacement_test_game(seed)
            entry = writer.add_game(
                seed=seed,
                scores=recording.result.scores,
                ranks=recording.result.ranks,
                rows=build_macro_transitions(recording),
            )
            measurements.append(
                {
                    "seed": seed,
                    "macro_transition_rows": entry.row_count,
                    "wall_clock_seconds": recording.wall_clock_seconds,
                    "cpu_seconds": recording.cpu_seconds,
                }
            )
            print(
                f"seed={seed} rows={entry.row_count} "
                f"wall={recording.wall_clock_seconds:.2f}s",
                flush=True,
            )
        artifact = writer.finalize()
    except BaseException:
        writer.discard()
        raise

    non_finite = artifact.count_non_finite_features()
    document = {
        "artifact_identity": artifact.identity,
        "purpose": artifact.manifest["purpose"],
        "replacement_test_seeds": list(REPLACEMENT_TEST_SEEDS),
        "non_finite_feature_count": non_finite,
        "games": measurements,
        "totals": dict(artifact.manifest["totals"]),
        "provenance": dict(artifact.manifest["provenance"]),
    }
    _write_json(Path(arguments.report), document)
    print(f"artifact_identity={artifact.identity}")
    print(
        f"hanchan={artifact.hanchan_count} rows={artifact.row_count} "
        f"terminal={artifact.terminal_row_count} "
        f"nonterminal={artifact.nonterminal_row_count}"
    )
    print(f"non_finite_feature_count={non_finite}")
    return 0


def _evaluate_replacement_test(arguments: argparse.Namespace) -> int:
    """rebuilt BC / Q candidate pairをreplacement TEST populationへ1回だけexposeする。

    support setはQ checkpointへidentity-boundされた`supported_indices`を正本と
    し、TEST rowからもTRAIN rowからも再計算しない。
    """
    from . import bc_training, q_training
    from .exposure_evaluation import evaluate_bc_test, evaluate_q_with_support_mask
    from .replacement_test import (
        count_unsupported_bootstrap,
        load_replacement_test,
        load_replacement_test_tensors,
        support_complete_flags,
        support_mask_from_checkpoint,
    )

    bc_checkpoint = bc_training.load_checkpoint(arguments.bc_checkpoint)
    q_checkpoint = q_training.load_checkpoint(arguments.q_checkpoint)
    if (
        bc_checkpoint.manifest["dataset_identity"]
        != q_checkpoint.manifest["dataset_identity"]
    ):
        raise SystemExit("BC and Q checkpoints were not trained on the same dataset")

    artifact = load_replacement_test(arguments.artifact)
    tensors = load_replacement_test_tensors(artifact)
    support_mask = support_mask_from_checkpoint(q_checkpoint.supported_indices)

    non_finite = artifact.count_non_finite_features()
    unsupported_bootstrap = count_unsupported_bootstrap(tensors, support_mask)
    complete = support_complete_flags(tensors, support_mask)
    support_complete_count = int(complete.sum())

    bc_diagnostics = evaluate_bc_test(bc_checkpoint.model, tensors)
    q_diagnostics = evaluate_q_with_support_mask(
        q_checkpoint.model, tensors, support_mask
    )

    gates = {
        "checkpoint_strict_readback": True,
        "feature_identity": (
            artifact.manifest["feature"] == bc_checkpoint.manifest["feature"]
            and artifact.manifest["feature"] == q_checkpoint.manifest["feature"]
        ),
        "vocabulary_identity": (
            artifact.manifest["vocabulary"] == bc_checkpoint.manifest["vocabulary"]
            and artifact.manifest["vocabulary"] == q_checkpoint.manifest["vocabulary"]
        ),
        "transition_validation": True,
        "non_finite_feature_count_is_zero": non_finite == 0,
        "finite_q_rate_is_one": q_diagnostics.finite_q_rate == 1.0,
        "unsupported_bootstrap_is_zero": unsupported_bootstrap == 0,
    }
    document = {
        "replacement_test_artifact_identity": artifact.identity,
        "bc_checkpoint_identity": bc_checkpoint.identity,
        "q_checkpoint_identity": q_checkpoint.identity,
        "dataset_identity": q_checkpoint.manifest["dataset_identity"],
        "supported_indices_digest": q_checkpoint.manifest["supported_indices_digest"],
        "hard_validity_gates": gates,
        "artifact_totals": {
            "hanchan_count": artifact.hanchan_count,
            "row_count": artifact.row_count,
            "terminal_row_count": artifact.terminal_row_count,
            "nonterminal_row_count": artifact.nonterminal_row_count,
            "non_finite_feature_count": non_finite,
            "support_complete_count": support_complete_count,
            "support_complete_rate": support_complete_count / artifact.row_count,
            "unsupported_bootstrap_count": unsupported_bootstrap,
        },
        "bc": bc_diagnostics.to_document(),
        "q": q_diagnostics.to_document(),
    }
    _write_json(Path(arguments.result), document)

    for name, passed in gates.items():
        print(f"gate {name}: {'PASS' if passed else 'FAIL'}")
    print(f"BC choice masked CE       {bc_diagnostics.choice_masked_cross_entropy:.6f}")
    print(f"BC choice exact agreement {bc_diagnostics.choice_exact_agreement:.6f}")
    print(f"Q selected-action Huber   {q_diagnostics.selected_action_huber_loss:.6f}")
    print(f"Q finite Q rate           {q_diagnostics.finite_q_rate:.6f}")
    if not all(gates.values()):
        print("REPLACEMENT TEST INVALID")
        return 1
    return 0


def _smoke(arguments: argparse.Namespace) -> int:
    from . import q_training as _q_training
    from .serving import create_bc_hybrid_runtime, create_q_hybrid_runtime
    from .smoke import run_smoke, summarize_smoke

    q_checkpoint = _q_training.load_checkpoint(arguments.q_checkpoint)
    supported = q_checkpoint.supported_indices
    bc_runtime = create_bc_hybrid_runtime(
        arguments.bc_checkpoint, supported_indices=supported
    )
    q_runtime = create_q_hybrid_runtime(
        arguments.q_checkpoint, supported_indices=supported
    )

    document = {}
    for arm, runtime in (("bc", bc_runtime), ("q", q_runtime)):
        measurements = run_smoke(runtime, SERVING_SMOKE_SEEDS)
        summary = summarize_smoke(arm, measurements)
        document[arm] = {
            "summary": summary.to_document(),
            "games": [item.to_document() for item in measurements],
        }
        print(
            f"{arm}: activation_rate={summary.activation_rate:.4f} "
            f"scaffold_fallback_rate={summary.scaffold_fallback_rate:.4f} "
            f"support_fallback_rate={summary.support_fallback_rate:.4f}"
        )
    _write_json(Path(arguments.report), document)
    return 0


def _freeze(arguments: argparse.Namespace) -> int:
    from .retention import Stage4aRetentionError, freeze_candidates

    try:
        freeze, _ = freeze_candidates(
            bc_checkpoint_path=arguments.bc_checkpoint,
            q_checkpoint_path=arguments.q_checkpoint,
            backend=arguments.retention_backend,
            root=arguments.retention_root,
            key=arguments.retention_key,
        )
    except Stage4aRetentionError as error:
        print("ARTIFACT RETENTION BLOCKED")
        print(str(error))
        return 1
    print(f"bc_checkpoint_identity={freeze.bc_checkpoint_identity}")
    print(f"q_checkpoint_identity={freeze.q_checkpoint_identity}")
    print(f"retention_key={freeze.key}")
    return 0


def _strength_summary_document(summary) -> dict[str, object]:
    """canonical summaryをJSONへ写す。新しい統計semanticsを導入しない。

    `SingleRoundStrengthSummary`は`lisjong_arena.single_round_evaluation`の
    canonical aggregation結果だけを持つdataclassであり、自前の
    `to_document()`を持たない。Stage 4a (`learned_policy_stage4a.result`)と
    同じく、値の写経だけをここで行い、metricを別の式で再計算しない。

    candidate-only Mahjong metricsは`candidate_only_mahjong_metrics`として
    明示し、baselineとの差として読めないようにする。
    """
    metrics = summary.candidate_metrics
    mahjong = metrics.mahjong_metrics
    blocks = summary.seed_block_statistics
    return {
        "game_count": metrics.game_count,
        "strength": {
            "candidate_mean_score": metrics.mean_candidate_score,
            "baseline_mean_score": summary.mean_baseline_score,
            "mean_candidate_game_delta": summary.mean_candidate_game_delta,
            "candidate_seat_mean_scores": list(metrics.seat_mean_scores),
            "seed_block_count": blocks.seed_block_count,
            "mean_seed_block_delta": blocks.mean_seed_block_delta,
            "sample_standard_deviation": blocks.sample_standard_deviation,
            "standard_error": blocks.standard_error,
            "normal_approx_95_interval_lower": blocks.normal_approx_95_interval_lower,
            "normal_approx_95_interval_upper": blocks.normal_approx_95_interval_upper,
            "positive_seed_block_count": blocks.positive_seed_block_count,
            "zero_seed_block_count": blocks.zero_seed_block_count,
            "negative_seed_block_count": blocks.negative_seed_block_count,
        },
        "candidate_only_mahjong_metrics": {
            "round_count": mahjong.round_count,
            "mean_round_score_delta": mahjong.mean_round_score_delta,
            "win_count": mahjong.win_count,
            "win_rate": mahjong.win_rate,
            "mean_win_points": mahjong.mean_win_points,
            "deal_in_count": mahjong.deal_in_count,
            "deal_in_rate": mahjong.deal_in_rate,
            "mean_deal_in_loss": mahjong.mean_deal_in_loss,
            "exhaustive_draw_count": mahjong.exhaustive_draw_count,
            "exhaustive_draw_tenpai_count": mahjong.exhaustive_draw_tenpai_count,
            "exhaustive_draw_tenpai_rate": mahjong.exhaustive_draw_tenpai_rate,
            "tenpai_reached_count": mahjong.tenpai_reached_count,
            "mean_first_tenpai_turn": mahjong.mean_first_tenpai_turn,
        },
    }


def _screen(arguments: argparse.Namespace) -> int:
    from .retention import strict_readback
    from .strength import run_strength_screen

    retained = strict_readback(arguments.bundle)
    measurement = run_strength_screen(retained, arguments.artifact)
    document = {
        **measurement.to_document(),
        "summary": _strength_summary_document(measurement.summary),
    }
    _write_json(Path(arguments.result), document)
    print(f"outcome={measurement.outcome.value}")
    return 0


def _diagnosis_outcome_names():
    """CLI choicesのためだけにexhaustive outcome列挙を読む（torchを要求しない）。"""
    from .diagnosis import DiagnosisOutcome

    return tuple(DiagnosisOutcome)


def _diagnose(arguments: argparse.Namespace) -> int:
    """retained artifactだけでMeasurement A-Dを実行する（Issue #152）。

    新しいgame / seed / trainingを一切作らず、`--bundle`のcandidate pairと
    `--dataset` / `--replacement-test` artifactをstrict readbackした結果に
    対してのみ動く。outcome classificationはここでは記録しない。
    """
    from .artifact import load_dataset
    from .diagnosis import (
        LOCKED_SOURCE_IDENTITIES,
        DiagnosisRole,
        RolePopulation,
        bind_diagnosis_inputs,
        build_diagnosis_result,
        dataset_role_populations,
        diagnose_role,
        validate_diagnosis_result,
    )
    from .replacement_test import (
        load_replacement_test,
        load_replacement_test_tensors,
        support_mask_from_checkpoint,
    )
    from .retention import strict_readback
    from .split_tensors import load_split_tensors

    retained = strict_readback(arguments.bundle)
    dataset = load_dataset(arguments.dataset)
    replacement = load_replacement_test(arguments.replacement_test)
    binding = bind_diagnosis_inputs(
        dataset=dataset,
        bc_checkpoint=retained.bc_checkpoint,
        q_checkpoint=retained.q_checkpoint,
        replacement_test=replacement,
        expected=LOCKED_SOURCE_IDENTITIES,
    )
    support_mask = support_mask_from_checkpoint(retained.q_checkpoint.supported_indices)

    populations = list(dataset_role_populations(dataset, load_split_tensors(dataset)))
    populations.append(
        RolePopulation(
            role=DiagnosisRole.REPLACEMENT_TEST,
            tensors=load_replacement_test_tensors(replacement),
            rows=replacement.rows,
        )
    )
    roles = [
        diagnose_role(
            population,
            bc_model=retained.bc_checkpoint.model,
            q_model=retained.q_checkpoint.model,
            support_mask=support_mask,
        )
        for population in populations
    ]
    document = validate_diagnosis_result(
        build_diagnosis_result(binding=binding, roles=roles)
    )
    _write_json(Path(arguments.result), document)

    for role in document["roles"]:
        counts = role["row_counts"]
        measurement_a = role["measurement_a"]
        print(
            f"{role['role']}: eligible={counts['eligible_row_count']}"
            f"/{counts['total_row_count']} "
            f"q_vs_bc={measurement_a['q_vs_bc_disagreement_count']} "
            f"q_vs_behavior={measurement_a['q_vs_behavior_disagreement_count']} "
            f"bc_vs_behavior={measurement_a['bc_vs_behavior_disagreement_count']} "
            f"measurement_d={role['measurement_d']['status']}"
        )
    print("classification=None")
    print(
        "review the result artifact, then record exactly one exhaustive outcome "
        "with the record-classification command"
    )
    return 0


def _record_classification(arguments: argparse.Namespace) -> int:
    """review後のexhaustive outcomeを1件だけresultへ記録する（Issue #152）。"""
    import json

    from .diagnosis import DiagnosisOutcome, record_classification

    source = Path(arguments.result)
    document = json.loads(source.read_text(encoding="utf-8"))
    classified = record_classification(document, DiagnosisOutcome[arguments.outcome])
    _write_json(Path(arguments.classified_result), classified)
    print(f"classification={classified['classification']}")
    return 0


def _p1_gate_a(arguments: argparse.Namespace) -> int:
    """retained artifactだけでP1 Q-v2を学習し、Gate Aを1回評価する（Issue #158）。

    新しいgame / seed / hanchan / TEST exposureを作らない。P1 featureはlocked
    retained transition rowsからのみ導出し、row order、split membership、
    behavior action、reward、terminal、legal maskを変更しない。
    """
    import torch

    from .artifact import load_dataset
    from .diagnosis import LOCKED_SOURCE_IDENTITIES
    from .p1_features import derive_all_split_tensors, derive_replacement_test_tensors
    from .p1_gate_a import (
        CandidateModelRecord,
        P1GateARole,
        P1RolePopulation,
        bind_gate_a_inputs,
        build_gate_a_result,
        derive_classification,
        evaluate_role,
        validate_gate_a_result,
    )
    from .p1_q_training import (
        model_weights_digest,
        require_p1_split_tensors,
        train_p1_q_model,
        verify_locked_q_protocol_delta,
    )
    from .protocol import Split
    from .replacement_test import (
        load_replacement_test,
        load_replacement_test_tensors,
        support_mask_from_checkpoint,
    )
    from .retention import strict_readback
    from .split_tensors import load_split_tensors
    from .support import support_set_identity

    verify_locked_q_protocol_delta()
    retained = strict_readback(arguments.bundle)
    dataset = load_dataset(arguments.dataset)
    replacement = load_replacement_test(arguments.replacement_test)
    binding = bind_gate_a_inputs(
        dataset=dataset,
        bc_checkpoint=retained.bc_checkpoint,
        q_checkpoint=retained.q_checkpoint,
        replacement_test=replacement,
        expected=LOCKED_SOURCE_IDENTITIES,
    )
    support_mask = support_mask_from_checkpoint(retained.q_checkpoint.supported_indices)

    split_tensors = load_split_tensors(dataset)
    p1_split_tensors, split_coverage = derive_all_split_tensors(split_tensors)
    require_p1_split_tensors(p1_split_tensors)
    replacement_tensors = load_replacement_test_tensors(replacement)
    p1_replacement_tensors, replacement_coverage = derive_replacement_test_tensors(
        replacement_tensors
    )

    run = train_p1_q_model(p1_split_tensors)
    supported_indices = sorted(
        int(index) for index in torch.nonzero(run.support_mask).flatten().tolist()
    )
    candidate = CandidateModelRecord(
        weights_digest=model_weights_digest(run.model),
        selected_epoch=run.selected_epoch,
        final_validation_huber_loss=run.final_validation_huber_loss,
        epoch_history=run.history,
        supported_indices_digest=support_set_identity(supported_indices),
    )

    populations = [
        P1RolePopulation(
            role=role,
            tensors=split_tensors[split],
            p1_tensors=p1_split_tensors[split],
            rows=tuple(
                dataset.rows[index] for index in split_tensors[split].row_indices
            ),
            coverage=split_coverage[split],
        )
        for role, split in (
            (P1GateARole.DATASET_TRAIN, Split.TRAIN),
            (P1GateARole.DATASET_VALIDATION, Split.VALIDATION),
            (P1GateARole.DATASET_TEST, Split.TEST),
        )
    ]
    populations.append(
        P1RolePopulation(
            role=P1GateARole.REPLACEMENT_TEST,
            tensors=replacement_tensors,
            p1_tensors=p1_replacement_tensors,
            rows=replacement.rows,
            coverage=replacement_coverage,
        )
    )
    roles = [
        evaluate_role(
            population,
            q_v1_model=retained.q_checkpoint.model,
            q_v2_model=run.model,
            bc_model=retained.bc_checkpoint.model,
            support_mask=support_mask,
        )
        for population in populations
    ]
    document = validate_gate_a_result(
        build_gate_a_result(binding=binding, candidate=candidate, roles=roles)
    )
    _write_json(Path(arguments.result), document)

    for role in document["roles"]:
        counts = role["row_counts"]
        progression = role["hand_progression"]
        agreement = role["action_agreement"]
        line = (
            f"{role['role']}: eligible={counts['eligible_row_count']}"
            f"/{counts['total_row_count']} "
            f"q_v2_vs_q_v1={agreement['q_v2_vs_q_v1_disagreement_count']} "
            f"hand_progression={progression['status']}"
        )
        if progression["outcome_conditions"] is not None:
            conditions = progression["outcome_conditions"]
            line += (
                f" worsen q_v2={conditions['q_v2_worsen_shanten_rate']:.6f}"
                f" q_v1={conditions['q_v1_worsen_shanten_rate']:.6f}"
                f" signal={conditions['signal']}"
                f" regression={conditions['regression']}"
            )
        print(line)
    print(f"derived_classification={derive_classification(document).value}")
    print("classification=None")
    print(
        "review the result artifact, then record exactly one exhaustive outcome "
        "with the p1-record-classification command"
    )
    return 0


def _p1_outcome_names():
    """CLI choicesのためだけにexhaustive outcome列挙を読む（torchを要求しない）。"""
    from .p1_gate_a import P1GateAOutcome

    return tuple(P1GateAOutcome)


def _p1_record_classification(arguments: argparse.Namespace) -> int:
    """Gate A resultへexhaustive outcomeを1件だけ記録する（Issue #158）。"""
    import json

    from .p1_gate_a import P1GateAOutcome, record_classification

    source = Path(arguments.result)
    document = json.loads(source.read_text(encoding="utf-8"))
    classified = record_classification(document, P1GateAOutcome[arguments.outcome])
    _write_json(Path(arguments.classified_result), classified)
    print(f"classification={classified['classification']}")
    return 0


def _p1_gate_b_outcome_names():
    """CLI choicesのためだけにexhaustive outcome列挙を読む（torchを要求しない）。"""
    from .p1_gate_b import P1GateBOutcome

    return tuple(P1GateBOutcome)


def _p1_materialize(arguments: argparse.Namespace) -> int:
    """exact #158 P1 candidateをserving checkpointへmaterializeする（Issue #162）。

    materialization pathの優先順位はIssue #162どおりである。

    ```text
    A. --weights   exact retained #158 weights -> strict load -> digest verify
    B. --dataset   exact deterministic reconstruction -> digest verify
    ```

    canonical weights digestがexact一致しない場合はfail closedし、seed変更、
    epoch追加、alternate config retry、tolerance acceptanceへ進まない。
    """
    from .artifact import load_dataset
    from .p1_candidate import (
        Stage4aRetentionError,
        load_retained_p1_candidate,
        materialize_p1_serving_checkpoint,
        reconstruct_p1_candidate,
    )
    from .p1_features import derive_all_split_tensors
    from .p1_gate_b import CANDIDATE_RETENTION_KEY, RETENTION_BACKEND
    from .retention import strict_readback
    from .split_tensors import load_split_tensors

    if bool(arguments.weights) == bool(arguments.dataset):
        print("P1 GATE B EVIDENCE BLOCKED")
        print(
            "exactly one materialization source is required: --weights (exact "
            "retained #158 weights) or --dataset (exact deterministic "
            "reconstruction source)"
        )
        return 1

    retained = strict_readback(arguments.bundle)
    supported_indices = retained.q_checkpoint.supported_indices
    if arguments.weights:
        candidate = load_retained_p1_candidate(arguments.weights)
    else:
        dataset = load_dataset(arguments.dataset)
        p1_split_tensors, _ = derive_all_split_tensors(load_split_tensors(dataset))
        candidate = reconstruct_p1_candidate(p1_split_tensors)

    try:
        key, checkpoint = materialize_p1_serving_checkpoint(
            candidate,
            supported_indices=supported_indices,
            backend=arguments.retention_backend or RETENTION_BACKEND,
            root=arguments.retention_root,
            key=arguments.retention_key or CANDIDATE_RETENTION_KEY,
        )
    except Stage4aRetentionError as error:
        print("P1 GATE B EVIDENCE BLOCKED")
        print(str(error))
        return 1

    print(f"materialization_source={candidate.materialization_source}")
    print(f"canonical_model_weights_digest={checkpoint.canonical_model_weights_digest}")
    print(f"candidate_identity={checkpoint.candidate_identity}")
    print(f"selected_epoch={checkpoint.manifest['selected_epoch']}")
    print(f"real_candidate_materialization={checkpoint.real_candidate_materialization}")
    print(f"retention_key={key}")
    return 0


def _p1_gate_b(arguments: argparse.Namespace) -> int:
    """exact P1 candidate vs passive tsumogiri x3のGate Bを1回実行する。

    既存`SingleRoundEvaluationPlan` / `run_single_round_evaluation()` /
    `SingleRoundStrengthArtifact` / canonical aggregationをthin reuseする。
    outcomeはここでは記録せず、review後に
    `p1-gate-b-record-classification`で1件だけ記録する。
    """
    from .p1_candidate import load_p1_serving_checkpoint
    from .p1_gate_b import run_gate_b

    checkpoint = load_p1_serving_checkpoint(arguments.checkpoint)
    measurement = run_gate_b(checkpoint, arguments.artifact)
    document = measurement.document
    _write_json(Path(arguments.result), document)

    summary = document["canonical_summary"]
    blocks = summary["seed_block_statistics"]
    diagnostics = document["serving_diagnostics"]
    print(f"candidate_identity={document['candidate']['identity']}")
    print(f"comparator_identity={document['comparator']['identity']}")
    print(f"game_count={summary['candidate_metrics']['game_count']}")
    print(f"mean_seed_block_delta={blocks['mean_seed_block_delta']:.4f}")
    print(
        "normal_approx_95_interval="
        f"[{blocks['normal_approx_95_interval_lower']:.4f}, "
        f"{blocks['normal_approx_95_interval_upper']:.4f}]"
    )
    print(
        f"activation_rate={diagnostics['activation_rate']:.4f} "
        f"scaffold_fallback_rate={diagnostics['scaffold_fallback_rate']:.4f} "
        f"support_fallback_rate={diagnostics['support_fallback_rate']:.4f}"
    )
    print(f"wall_clock_seconds={measurement.wall_clock_seconds:.1f}")
    print(f"derived_classification={measurement.derived_outcome.value}")
    print("classification=None")
    print(
        "review the result artifact, then record exactly one exhaustive outcome "
        "with the p1-gate-b-record-classification command"
    )
    return 0


def _p1_gate_b_record_classification(arguments: argparse.Namespace) -> int:
    """review後のexhaustive Gate B outcomeを1件だけresultへ記録する。"""
    import json

    from .p1_gate_b import P1GateBOutcome, record_classification

    source = Path(arguments.result)
    document = json.loads(source.read_text(encoding="utf-8"))
    classified = record_classification(document, P1GateBOutcome[arguments.outcome])
    _write_json(Path(arguments.classified_result), classified)
    print(f"classification={classified['classification']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lisjong_arena.learned_policy_offline_q",
        description="Offline Q vertical slice -- BC-vs-Offline-Q controlled comparison",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="generate the locked dataset")
    generate.add_argument("--dataset", required=True)
    generate.add_argument("--report", required=True)
    generate.set_defaults(handler=_generate)

    train_bc = commands.add_parser("train-bc", help="train Arm A (BC control)")
    train_bc.add_argument("--dataset", required=True)
    train_bc.add_argument("--checkpoint", required=True)
    train_bc.set_defaults(handler=_train_bc)

    train_q = commands.add_parser("train-q", help="train Arm B (support-restricted Q)")
    train_q.add_argument("--dataset", required=True)
    train_q.add_argument("--checkpoint", required=True)
    train_q.set_defaults(handler=_train_q)

    test = commands.add_parser("test", help="evaluate both frozen checkpoints once")
    test.add_argument("--dataset", required=True)
    test.add_argument("--bc-checkpoint", required=True)
    test.add_argument("--q-checkpoint", required=True)
    test.add_argument("--result", required=True)
    test.set_defaults(handler=_test)

    generate_replacement = commands.add_parser(
        "generate-replacement-test",
        help="generate the locked replacement TEST artifact (354..359)",
    )
    generate_replacement.add_argument("--artifact", required=True)
    generate_replacement.add_argument("--report", required=True)
    generate_replacement.set_defaults(handler=_generate_replacement_test)

    evaluate_replacement = commands.add_parser(
        "evaluate-replacement-test",
        help="one-shot BC/Q exposure against the replacement TEST artifact",
    )
    evaluate_replacement.add_argument("--artifact", required=True)
    evaluate_replacement.add_argument("--bc-checkpoint", required=True)
    evaluate_replacement.add_argument("--q-checkpoint", required=True)
    evaluate_replacement.add_argument("--result", required=True)
    evaluate_replacement.set_defaults(handler=_evaluate_replacement_test)

    smoke = commands.add_parser("smoke", help="serving smoke for both hybrids")
    smoke.add_argument("--bc-checkpoint", required=True)
    smoke.add_argument("--q-checkpoint", required=True)
    smoke.add_argument("--report", required=True)
    smoke.set_defaults(handler=_smoke)

    freeze = commands.add_parser(
        "freeze", help="retain BC/Q checkpoints before screening"
    )
    freeze.add_argument("--bc-checkpoint", required=True)
    freeze.add_argument("--q-checkpoint", required=True)
    freeze.add_argument("--retention-backend", required=True)
    freeze.add_argument("--retention-root", required=True)
    freeze.add_argument("--retention-key", required=True)
    freeze.set_defaults(handler=_freeze)

    screen = commands.add_parser("screen", help="Q-vs-BC ABBB strength screen")
    screen.add_argument("--bundle", required=True)
    screen.add_argument("--artifact", required=True)
    screen.add_argument("--result", required=True)
    screen.set_defaults(handler=_screen)

    diagnose = commands.add_parser(
        "diagnose",
        help="artifact-only Q-vs-BC failure diagnosis (Measurement A-D)",
    )
    diagnose.add_argument("--bundle", required=True)
    diagnose.add_argument("--dataset", required=True)
    diagnose.add_argument("--replacement-test", required=True)
    diagnose.add_argument("--result", required=True)
    diagnose.set_defaults(handler=_diagnose)

    record = commands.add_parser(
        "record-classification",
        help="record one exhaustive diagnosis outcome onto a diagnosis result",
    )
    record.add_argument("--result", required=True)
    record.add_argument("--classified-result", required=True)
    record.add_argument(
        "--outcome",
        required=True,
        choices=[outcome.name for outcome in _diagnosis_outcome_names()],
    )
    record.set_defaults(handler=_record_classification)

    p1_gate_a = commands.add_parser(
        "p1-gate-a",
        help="artifact-only P1 keep-shanten Gate A (train Q-v2 and compare once)",
    )
    p1_gate_a.add_argument("--bundle", required=True)
    p1_gate_a.add_argument("--dataset", required=True)
    p1_gate_a.add_argument("--replacement-test", required=True)
    p1_gate_a.add_argument("--result", required=True)
    p1_gate_a.set_defaults(handler=_p1_gate_a)

    p1_record = commands.add_parser(
        "p1-record-classification",
        help="record one exhaustive P1 Gate A outcome onto a Gate A result",
    )
    p1_record.add_argument("--result", required=True)
    p1_record.add_argument("--classified-result", required=True)
    p1_record.add_argument(
        "--outcome",
        required=True,
        choices=[outcome.name for outcome in _p1_outcome_names()],
    )
    p1_record.set_defaults(handler=_p1_record_classification)

    p1_materialize = commands.add_parser(
        "p1-materialize",
        help="materialize the exact #158 P1 candidate as a serving checkpoint",
    )
    p1_materialize.add_argument("--bundle", required=True)
    p1_materialize.add_argument("--weights")
    p1_materialize.add_argument("--dataset")
    p1_materialize.add_argument("--retention-backend")
    p1_materialize.add_argument("--retention-root", required=True)
    p1_materialize.add_argument("--retention-key")
    p1_materialize.set_defaults(handler=_p1_materialize)

    p1_gate_b = commands.add_parser(
        "p1-gate-b",
        help="P1 candidate vs passive tsumogiri x3 single-round Gate B",
    )
    p1_gate_b.add_argument("--checkpoint", required=True)
    p1_gate_b.add_argument("--artifact", required=True)
    p1_gate_b.add_argument("--result", required=True)
    p1_gate_b.set_defaults(handler=_p1_gate_b)

    p1_gate_b_record = commands.add_parser(
        "p1-gate-b-record-classification",
        help="record one exhaustive P1 Gate B outcome onto a Gate B result",
    )
    p1_gate_b_record.add_argument("--result", required=True)
    p1_gate_b_record.add_argument("--classified-result", required=True)
    p1_gate_b_record.add_argument(
        "--outcome",
        required=True,
        choices=[outcome.name for outcome in _p1_gate_b_outcome_names()],
    )
    p1_gate_b_record.set_defaults(handler=_p1_gate_b_record_classification)

    arguments = parser.parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
