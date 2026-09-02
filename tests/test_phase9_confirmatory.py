"""Synthetic Phase 9 protocol, data-boundary, guard, and artifact tests."""

import argparse
import copy
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _phase3_bootstrap_fixtures import resolved_provenance
from _phase4_raw_corpus_fixtures import fixture_corpus_for_seeds

from lisjong_arena.phase4_raw_corpus.persistence import save_raw_corpus
from lisjong_arena.phase5_belief_dataset.model import GameIdentity
from lisjong_arena.phase5_belief_dataset.persistence import (
    load_belief_dataset,
    save_belief_dataset,
)
from lisjong_arena.phase8_sequential.evaluation import (
    remap_predictions_by_reference,
)
from lisjong_arena.phase9_confirmatory.__main__ import (
    _evaluate_command,
    _generate_command,
    _lock_command,
    _parser,
)
from lisjong_arena.phase9_confirmatory.artifact import (
    load_result,
    result_with_identity,
    save_result,
)
from lisjong_arena.phase9_confirmatory.data import (
    build_phase9_holdout_dataset,
    holdout_lock_value,
    validate_holdout_dataset,
)
from lisjong_arena.phase9_confirmatory.preflight import (
    artifact_file_state,
    generation_report_value,
    require_formal_execution_authorization,
    verify_artifact_state,
    verify_current_checkout_revision,
    verify_formal_evaluation_runtime,
    verify_frozen_arms,
)
from lisjong_arena.phase9_confirmatory.protocol import (
    BOOTSTRAP_CLUSTERS_PER_REPLICATE,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_RNG,
    BOOTSTRAP_SEED,
    DEPTH_BUCKETS,
    EVALUATION_REVISIONS,
    EVALUATION_RIICHIENV_VERSION,
    EVALUATION_TORCH_VERSION,
    HISTORICAL_REVISIONS,
    HISTORICAL_TREES,
    HOLDOUT_GAME_COUNT,
    HOLDOUT_ROLE,
    HOLDOUT_SEEDS,
    LOCKED_RULE_FINGERPRINT,
    LOCKED_SUBGROUPS,
    MATERIALITY_EPSILON,
    PROTOCOL_ID,
    S2_ARTIFACT_IDENTITY,
    S2_WEIGHTS_SHA256,
    SNAPSHOT_ARTIFACT_IDENTITY,
    SNAPSHOT_WEIGHTS_SHA256,
    FamilyClassification,
    PairedGameCluster,
    classify_family,
    paired_hanchan_bootstrap,
    physical_gate_passes,
    pooled_delta,
    robustness_diagnostics,
    validate_holdout_games,
)


def _historical_fixture_corpus():
    corpus = fixture_corpus_for_seeds(HOLDOUT_SEEDS)
    return replace(
        corpus,
        provenance=resolved_provenance(
            lisjong=HISTORICAL_REVISIONS["lisjong"],
            lisjong_engine=HISTORICAL_REVISIONS["lisjong_engine"],
            lisjong_arena=HISTORICAL_REVISIONS["lisjong_arena"],
        ),
    )


def _clusters(delta: float = 0.003) -> tuple[PairedGameCluster, ...]:
    return tuple(
        PairedGameCluster(
            GameIdentity("first-party-bootstrap", seed),
            anchor_count=1,
            cell_count=102,
            snapshot_absolute_error_sum=51.0,
            s2_absolute_error_sum=51.0 - delta * 102,
        )
        for seed in HOLDOUT_SEEDS
    )


def _physical(passed: bool = True) -> dict[str, object]:
    return {
        "constraint_non_convergence_count": 0 if passed else 1,
        "maximum_row_column_residual": 0.0,
        "concealed_size_inconsistency_max": 0.0,
        "physical_conservation_violation_sample_rate": 0.0,
        "conservation_total_excess": 0.0,
        "conservation_mean_excess_per_sample": 0.0,
        "blocking_gate_passed": passed,
    }


def _subgroups(delta: float) -> dict[str, list[dict[str, object]]]:
    row_count = HOLDOUT_GAME_COUNT * 3
    result = {}
    for family, names in LOCKED_SUBGROUPS:
        result[family] = [
            {
                "group": name,
                "available": index == 0,
                "sample_count": HOLDOUT_GAME_COUNT if index == 0 else 0,
                "row_count": row_count if index == 0 else 0,
                "cell_count": row_count * 34 if index == 0 else 0,
                "snapshot_mae": 0.5 if index == 0 else None,
                "s2_mae": 0.5 - delta if index == 0 else None,
                "delta_mae": delta if index == 0 else None,
            }
            for index, name in enumerate(names)
        ]
    return result


def _result_value() -> dict[str, object]:
    clusters = _clusters()
    delta = pooled_delta(clusters)
    generation_report = generation_report_value(
        "b" * 64,
        {
            "runtime": {
                "python": "3.14.0",
                "revisions": HISTORICAL_REVISIONS,
                "riichienv": "0.4.8",
                "rule_fingerprint": LOCKED_RULE_FINGERPRINT,
                "policy_count": 4,
                "distinct_policy_instances": 4,
            },
            "generation": {
                "raw_corpus_identity": "c" * 64,
                "ordered_seeds": list(HOLDOUT_SEEDS),
                "hanchan_count": HOLDOUT_GAME_COUNT,
                "turn_anchor_count": HOLDOUT_GAME_COUNT,
                "failure_count": 0,
                "phase2_equality_verified": True,
            },
        },
    )
    locked_sources = {
        name: {
            "declared_revision": revision,
            "resolved_revision": revision,
            "checkout_revision": revision,
            "tree": HISTORICAL_TREES[name],
            "checkout_tree": HISTORICAL_TREES[name],
            **(
                {"acquisition_ref": "archive/handbelief-phase5-e667890"}
                if name == "lisjong_arena"
                else {}
            ),
        }
        for name, revision in HISTORICAL_REVISIONS.items()
    }
    per_game = [
        {
            "source_class": cluster.game.source_class,
            "game_seed": cluster.game.game_seed,
            "anchor_count": cluster.anchor_count,
            "snapshot_mae": cluster.snapshot_mae,
            "s2_mae": cluster.s2_mae,
            "delta_mae": cluster.delta_mae,
        }
        for cluster in clusters
    ]
    return {
        "result_schema_version": "phase9-confirmatory-result-v1",
        "protocol_identity": PROTOCOL_ID,
        "creation_software_revision": "a" * 40,
        "preflight_identity": "b" * 64,
        "raw_corpus_identity": "c" * 64,
        "dataset_identity": "d" * 64,
        "holdout": {
            "role": HOLDOUT_ROLE,
            "ordered_seeds": list(HOLDOUT_SEEDS),
            "game_count": HOLDOUT_GAME_COUNT,
        },
        "frozen_arms": {
            "snapshot": {
                "artifact_logical_identity": SNAPSHOT_ARTIFACT_IDENTITY,
                "weights_sha256": SNAPSHOT_WEIGHTS_SHA256,
                "parameter_count": 134_856,
                "model": {"family": "snapshot"},
                "feature_semantics_id": "phase6-history-snapshot-v1",
            },
            "s2": {
                "artifact_logical_identity": S2_ARTIFACT_IDENTITY,
                "weights_sha256": S2_WEIGHTS_SHA256,
                "parameter_count": 459_080,
                "selected_epoch": 40,
                "candidate": "S2",
                "model": {"family": "s2"},
                "feature_semantics_id": "phase6-history-snapshot-v1",
                "sequence_semantics_id": "phase8-sequential-hand-belief-v1",
                "previous_belief_semantics": {
                    "axis": "Wind->expected_count[34]",
                    "current_order": "explicit-opponent_winds-remap",
                    "scale": 4.0,
                    "source": "prior-self-prediction",
                },
                "initial_state_semantics": {
                    "depth_1_previous_belief": (
                        "current-public-conditional-uniform-baseline"
                    ),
                    "s2_latent": "zeros",
                },
                "self_rollout_semantics": "prediction_t->previous_belief_t+1",
                "test_partition_evaluated": False,
            },
        },
        "generation_provenance": {
            "locked": {
                "policy_population": "TwoStepUkeirePolicy x4",
                "riichienv_version": "0.4.8",
                "effective_rules": {"fingerprint": LOCKED_RULE_FINGERPRINT},
                "sources": locked_sources,
            },
            "executed": generation_report,
            "holdout_lock": {
                "role": HOLDOUT_ROLE,
                "raw_corpus_identity": "c" * 64,
                "dataset_identity": "d" * 64,
                "eligible_turn_anchor_count": HOLDOUT_GAME_COUNT,
                "game_atomic_membership": True,
                "training_on_phase9_holdout": False,
                "model_selection_on_phase9_holdout": False,
            },
        },
        "runtime_provenance": {
            "python": "3.14.0",
            "torch": EVALUATION_TORCH_VERSION,
            "riichienv": EVALUATION_RIICHIENV_VERSION,
            "device": "cpu",
            "torch_thread_count": 1,
            "deterministic_algorithms": True,
            "installed_revisions": EVALUATION_REVISIONS,
        },
        "pairing": {
            "eligible_anchor_count": HOLDOUT_GAME_COUNT,
            "ordered_anchor_identities": [f"{seed:064x}" for seed in HOLDOUT_SEEDS],
            "identity_order_eligibility_equal": True,
        },
        "primary_metrics": {
            "snapshot": {"per_tile_mae": 0.5},
            "s2": {"per_tile_mae": 0.5 - delta},
            "delta_mae": delta,
            "relative_improvement": delta / 0.5,
            "materiality_epsilon": MATERIALITY_EPSILON,
        },
        "bootstrap": {
            "rng": BOOTSTRAP_RNG,
            "seed": BOOTSTRAP_SEED,
            "replicates": BOOTSTRAP_REPLICATES,
            "clusters_per_replicate": BOOTSTRAP_CLUSTERS_PER_REPLICATE,
            "interval": "percentile-95",
            "sampling": "whole-matched-hanchan-with-replacement",
            "ci_lower": delta,
            "ci_upper": delta,
        },
        "diagnostics": {
            "per_game": per_game,
            "game_direction_counts": {
                "positive": HOLDOUT_GAME_COUNT,
                "zero": 0,
                "negative": 0,
            },
            "game_macro_mean_delta_mae": delta,
            "median_per_game_delta_mae": delta,
            "leave_one_hanchan_out": [
                {
                    "omitted_source_class": "first-party-bootstrap",
                    "omitted_game_seed": seed,
                    "delta_mae": delta,
                }
                for seed in HOLDOUT_SEEDS
            ],
            "sequence_depth": [
                {
                    "bucket": bucket,
                    "sample_count": HOLDOUT_GAME_COUNT if index == 0 else 0,
                    "snapshot_mae": 0.5 if index == 0 else None,
                    "s2_mae": 0.5 - delta if index == 0 else None,
                    "delta_mae": delta if index == 0 else None,
                }
                for index, bucket in enumerate(DEPTH_BUCKETS)
            ],
            "subgroups": _subgroups(delta),
        },
        "physical_consistency": {
            "snapshot": _physical(),
            "s2": _physical(),
            "blocking_gate_passed": True,
        },
        "training_on_phase9_holdout": False,
        "model_selection_on_phase9_holdout": False,
        "artifact_files_unchanged": True,
        "classification": FamilyClassification.SEQUENTIAL_FAMILY_LOCKED.value,
    }


class Phase9ProtocolTest(unittest.TestCase):
    def test_locked_population_and_bootstrap_configuration(self) -> None:
        self.assertEqual(HOLDOUT_SEEDS, tuple(range(160, 180)))
        self.assertEqual(HOLDOUT_GAME_COUNT, 20)
        self.assertEqual(BOOTSTRAP_REPLICATES, 20_000)
        self.assertEqual(BOOTSTRAP_SEED, 0)
        self.assertEqual(BOOTSTRAP_CLUSTERS_PER_REPLICATE, 20)

    def test_exact_seed_membership_rejects_every_difference(self) -> None:
        validate_holdout_games(tuple(cluster.game for cluster in _clusters()))
        with self.assertRaisesRegex(ValueError, "160..179"):
            validate_holdout_games(tuple(cluster.game for cluster in _clusters())[:-1])
        games = list(cluster.game for cluster in _clusters())
        games[-1] = GameIdentity("first-party-bootstrap", 180)
        with self.assertRaisesRegex(ValueError, "160..179"):
            validate_holdout_games(tuple(games))

    def test_pooled_delta_and_whole_game_bootstrap_are_deterministic(self) -> None:
        clusters = _clusters()
        self.assertAlmostEqual(pooled_delta(clusters), 0.003)
        self.assertEqual(
            paired_hanchan_bootstrap(clusters), paired_hanchan_bootstrap(clusters)
        )
        diagnostics = robustness_diagnostics(clusters)
        self.assertEqual(diagnostics.positive_game_count, 20)
        self.assertEqual(len(diagnostics.leave_one_game_out_deltas), 20)

    def test_exhaustive_classification_and_physical_precedence(self) -> None:
        def classify(**values):
            return classify_family(validity_ok=True, ci_upper=0.01, **values)

        self.assertIs(
            classify(delta_mae=0.0025, ci_lower=0.00001),
            FamilyClassification.SEQUENTIAL_FAMILY_LOCKED,
        )
        self.assertIs(
            classify(delta_mae=0.002499, ci_lower=0.00001),
            FamilyClassification.SNAPSHOT_FAMILY_LOCKED,
        )
        self.assertIs(
            classify(delta_mae=0.003, ci_lower=0.0),
            FamilyClassification.SNAPSHOT_FAMILY_LOCKED,
        )
        self.assertIs(
            classify_family(
                source_semantics_ok=False,
                validity_ok=False,
                delta_mae=0.0,
                ci_lower=0.0,
                ci_upper=0.0,
            ),
            FamilyClassification.REFORMULATE,
        )
        self.assertIs(
            classify_family(
                validity_ok=False,
                delta_mae=1.0,
                ci_lower=1.0,
                ci_upper=1.0,
            ),
            FamilyClassification.STOP_REWORK,
        )
        self.assertFalse(
            physical_gate_passes(
                constraint_non_convergence_count=1,
                maximum_residual=0.0,
                concealed_size_inconsistency_max=0.0,
                conservation_violation_sample_rate=0.0,
            )
        )

    def test_formal_commands_fail_closed_without_post_merge_guard(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "post-merge"):
                require_formal_execution_authorization()

    def test_cli_has_no_seed_override_and_separates_workflow(self) -> None:
        parser = _parser()
        help_text = parser.format_help()
        for command in ("preflight", "generate", "lock-holdout", "evaluate"):
            self.assertIn(command, help_text)
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        option_strings = {
            option
            for command_parser in subparsers.choices.values()
            for action in command_parser._actions
            for option in action.option_strings
        }
        self.assertTrue(
            {"--seed", "--epsilon", "--bootstrap-replicates"}.isdisjoint(option_strings)
        )

    def test_creation_revision_rejects_revision_drift_and_dirty_tracked_state(
        self,
    ) -> None:
        revision = "a" * 40
        with patch(
            "lisjong_arena.phase9_confirmatory.preflight._git",
            side_effect=(revision, ""),
        ):
            self.assertEqual(verify_current_checkout_revision(revision), revision)
        with patch(
            "lisjong_arena.phase9_confirmatory.preflight._git",
            return_value="b" * 40,
        ):
            with self.assertRaisesRegex(RuntimeError, "current Arena checkout"):
                verify_current_checkout_revision(revision)
        with patch(
            "lisjong_arena.phase9_confirmatory.preflight.__file__",
            str(Path.cwd() / "elsewhere" / "preflight.py"),
        ):
            with self.assertRaisesRegex(RuntimeError, "not from the current checkout"):
                verify_current_checkout_revision(revision)
        for dirty in ("M  staged.py", " M worktree.py"):
            with (
                self.subTest(dirty=dirty),
                patch(
                    "lisjong_arena.phase9_confirmatory.preflight._git",
                    side_effect=(revision, dirty),
                ),
                self.assertRaisesRegex(RuntimeError, "tracked or staged"),
            ):
                verify_current_checkout_revision(revision)

    def test_every_formal_stage_revalidates_preflight_checkout(self) -> None:
        revision = "a" * 40
        preflight = {
            "creation_software_revision": revision,
            "preflight_identity": "b" * 64,
        }
        commands = (
            (
                _generate_command,
                SimpleNamespace(
                    preflight="preflight.json",
                    snapshot_artifact="snapshot",
                    s2_artifact="s2",
                    historical_python="python",
                    raw_output="raw",
                    report_output="report",
                ),
            ),
            (
                _lock_command,
                SimpleNamespace(
                    preflight="preflight.json",
                    generation_report="generation.json",
                    raw="raw",
                    dataset_output="dataset",
                ),
            ),
            (
                _evaluate_command,
                SimpleNamespace(
                    preflight="preflight.json",
                    creation_revision=revision,
                    generation_report="generation.json",
                    raw="raw",
                    dataset="dataset",
                    snapshot_artifact="snapshot",
                    s2_artifact="s2",
                    result_output="result",
                ),
            ),
        )
        for command, arguments in commands:
            with (
                self.subTest(command=command.__name__),
                patch(
                    "lisjong_arena.phase9_confirmatory.__main__.require_formal_execution_authorization"
                ),
                patch(
                    "lisjong_arena.phase9_confirmatory.__main__.load_preflight",
                    return_value=preflight,
                ),
                patch(
                    "lisjong_arena.phase9_confirmatory.__main__.verify_current_checkout_revision",
                    side_effect=RuntimeError("revision drift"),
                ) as verify,
                self.assertRaisesRegex(RuntimeError, "revision drift"),
            ):
                command(arguments)
            verify.assert_called_once_with(revision)

    def test_formal_evaluation_runtime_requires_exact_noneditable_pins(self) -> None:
        class FakeDistribution:
            def __init__(self, *, version="", revision=None, editable=False):
                self.version = version
                self._revision = revision
                self._editable = editable

            def read_text(self, _name):
                if self._editable:
                    return json.dumps(
                        {"url": "file:///local", "dir_info": {"editable": True}}
                    )
                return json.dumps(
                    {
                        "url": "https://github.com/lisbun/example.git",
                        "vcs_info": {"vcs": "git", "commit_id": self._revision},
                    }
                )

        def exact_distribution(name):
            if name == "riichienv":
                return FakeDistribution(version=EVALUATION_RIICHIENV_VERSION)
            key = name.replace("-", "_")
            return FakeDistribution(revision=EVALUATION_REVISIONS[key])

        fake_torch = SimpleNamespace(
            __version__=EVALUATION_TORCH_VERSION,
            cuda=SimpleNamespace(is_available=lambda: False),
        )
        with (
            patch.object(sys, "version_info", (3, 14, 0)),
            patch(
                "lisjong_arena.phase9_confirmatory.preflight.distribution",
                side_effect=exact_distribution,
            ),
            patch(
                "lisjong_arena.phase9_confirmatory.preflight.importlib.import_module",
                return_value=fake_torch,
            ),
        ):
            runtime = verify_formal_evaluation_runtime()
        self.assertEqual(runtime["installed_revisions"], EVALUATION_REVISIONS)

        def editable_distribution(name):
            if name == "lisjong":
                return FakeDistribution(editable=True)
            return exact_distribution(name)

        with (
            patch.object(sys, "version_info", (3, 14, 0)),
            patch(
                "lisjong_arena.phase9_confirmatory.preflight.distribution",
                side_effect=editable_distribution,
            ),
            self.assertRaisesRegex(RuntimeError, "local/editable"),
        ):
            verify_formal_evaluation_runtime()

        def missing_provenance_distribution(name):
            if name == "lisjong":
                return SimpleNamespace(read_text=lambda _name: None)
            return exact_distribution(name)

        with (
            patch.object(sys, "version_info", (3, 14, 0)),
            patch(
                "lisjong_arena.phase9_confirmatory.preflight.distribution",
                side_effect=missing_provenance_distribution,
            ),
            self.assertRaisesRegex(RuntimeError, "VCS provenance"),
        ):
            verify_formal_evaluation_runtime()

        def wrong_revision_distribution(name):
            if name == "lisjong":
                return FakeDistribution(revision="0" * 40)
            return exact_distribution(name)

        with (
            patch.object(sys, "version_info", (3, 14, 0)),
            patch(
                "lisjong_arena.phase9_confirmatory.preflight.distribution",
                side_effect=wrong_revision_distribution,
            ),
            self.assertRaisesRegex(RuntimeError, "dependency revisions"),
        ):
            verify_formal_evaluation_runtime()

        def wrong_riichienv_distribution(name):
            if name == "riichienv":
                return FakeDistribution(version="0.4.9")
            return exact_distribution(name)

        with (
            patch.object(sys, "version_info", (3, 14, 0)),
            patch(
                "lisjong_arena.phase9_confirmatory.preflight.distribution",
                side_effect=wrong_riichienv_distribution,
            ),
            self.assertRaisesRegex(RuntimeError, "RiichiEnv"),
        ):
            verify_formal_evaluation_runtime()

        wrong_torch = SimpleNamespace(
            __version__="2.13.1+cpu",
            cuda=SimpleNamespace(is_available=lambda: False),
        )
        with (
            patch.object(sys, "version_info", (3, 14, 0)),
            patch(
                "lisjong_arena.phase9_confirmatory.preflight.distribution",
                side_effect=exact_distribution,
            ),
            patch(
                "lisjong_arena.phase9_confirmatory.preflight.importlib.import_module",
                return_value=wrong_torch,
            ),
            self.assertRaisesRegex(RuntimeError, "PyTorch 2.13.0 CPU"),
        ):
            verify_formal_evaluation_runtime()


class Phase9DatasetTest(unittest.TestCase):
    def test_fresh_dataset_is_separate_test_only_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            raw = save_raw_corpus(_historical_fixture_corpus(), root / "raw")
            dataset = build_phase9_holdout_dataset(raw)
            saved = save_belief_dataset(dataset, root / "dataset")
            loaded = load_belief_dataset(root / "dataset").dataset
            samples = validate_holdout_dataset(loaded, raw)
            self.assertEqual(len(samples), HOLDOUT_GAME_COUNT)
            lock = holdout_lock_value(saved.dataset)
            self.assertEqual(lock["role"], HOLDOUT_ROLE)
            self.assertFalse(lock["training_on_phase9_holdout"])
            self.assertFalse(lock["model_selection_on_phase9_holdout"])
            self.assertEqual(lock["eligible_turn_anchor_count"], 20)
            changed_reference = replace(
                loaded.examples[0], hand_number=loaded.examples[0].hand_number + 1
            )
            with self.assertRaisesRegex(ValueError, "exact raw-corpus derivation"):
                validate_holdout_dataset(
                    replace(
                        loaded,
                        examples=(changed_reference, *loaded.examples[1:]),
                    ),
                    raw,
                )
            predictions = tuple(
                SimpleNamespace(example=reference)
                for reference in reversed(loaded.examples)
            )
            aligned = remap_predictions_by_reference(loaded.examples, predictions)
            self.assertEqual(
                tuple(prediction.example for prediction in aligned), loaded.examples
            )
            with self.assertRaisesRegex(ValueError, "identities differ"):
                remap_predictions_by_reference(loaded.examples, predictions[:-1])

    def test_generation_provenance_must_be_exact(self) -> None:
        corpus = _historical_fixture_corpus()
        bad = replace(
            corpus,
            provenance=resolved_provenance(
                lisjong="0" * 40,
                lisjong_engine=HISTORICAL_REVISIONS["lisjong_engine"],
                lisjong_arena=HISTORICAL_REVISIONS["lisjong_arena"],
            ),
        )
        with tempfile.TemporaryDirectory() as root_name:
            raw = save_raw_corpus(bad, Path(root_name) / "raw")
            with self.assertRaisesRegex(ValueError, "historical revisions"):
                build_phase9_holdout_dataset(raw)


class Phase9ArtifactTest(unittest.TestCase):
    def test_frozen_arm_lock_and_bytes_are_checked_without_mutation(self) -> None:
        snapshot_manifest = {
            "weights_sha256": SNAPSHOT_WEIGHTS_SHA256,
            "parameter_count": 134_856,
            "feature_semantics_id": "phase6-history-snapshot-v1",
            "test_partition_evaluated": False,
            "model": {"family": "snapshot"},
        }
        s2_manifest = {
            "candidate": "S2",
            "weights_sha256": S2_WEIGHTS_SHA256,
            "parameter_count": 459_080,
            "selected_epoch": 40,
            "feature_semantics_id": "phase6-history-snapshot-v1",
            "sequence_semantics_id": "phase8-sequential-hand-belief-v1",
            "test_partition_evaluated": False,
            "model": {"family": "s2"},
            "previous_belief_semantics": {
                "axis": "Wind->expected_count[34]",
                "current_order": "explicit-opponent_winds-remap",
                "scale": 4.0,
                "source": "prior-self-prediction",
            },
            "initial_state_semantics": {
                "depth_1_previous_belief": (
                    "current-public-conditional-uniform-baseline"
                ),
                "s2_latent": "zeros",
            },
            "self_rollout_semantics": "prediction_t->previous_belief_t+1",
        }
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            snapshot = root / "snapshot"
            s2 = root / "s2"
            for path in (snapshot, s2):
                path.mkdir()
                (path / "manifest.json").write_bytes(b"manifest")
                (path / "weights.pt").write_bytes(b"weights")
            with (
                patch(
                    "lisjong_arena.phase9_confirmatory.preflight.load_snapshot_artifact",
                    return_value=SimpleNamespace(manifest=snapshot_manifest),
                ),
                patch(
                    "lisjong_arena.phase9_confirmatory.preflight.load_s2_artifact",
                    return_value=SimpleNamespace(manifest=s2_manifest),
                ),
                patch(
                    "lisjong_arena.phase9_confirmatory.preflight.snapshot_logical_identity",
                    return_value=SNAPSHOT_ARTIFACT_IDENTITY,
                ),
                patch(
                    "lisjong_arena.phase9_confirmatory.preflight.s2_logical_identity",
                    return_value=S2_ARTIFACT_IDENTITY,
                ),
            ):
                _snapshot, _s2, state = verify_frozen_arms(snapshot, s2)
            self.assertEqual(state["snapshot"], artifact_file_state(snapshot))
            (s2 / "weights.pt").write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "preflight"):
                verify_artifact_state(snapshot, s2, state)

    def test_result_is_immutable_strict_and_integrity_bound(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            destination = Path(root_name) / "result"
            result = save_result(destination, _result_value())
            self.assertEqual(load_result(destination), result)
            with self.assertRaises(FileExistsError):
                save_result(destination, _result_value())
            path = destination / "result.json"
            tampered = json.loads(path.read_bytes())
            tampered["classification"] = (
                FamilyClassification.SNAPSHOT_FAMILY_LOCKED.value
            )
            path.write_text(
                json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "logical identity"):
                load_result(destination)

    def test_result_rejects_missing_empty_and_malformed_locked_strata(self) -> None:
        cases = {}
        missing = copy.deepcopy(_result_value())
        missing["diagnostics"]["subgroups"].pop("public_riichi_state")
        cases["missing family"] = missing
        empty = copy.deepcopy(_result_value())
        empty["diagnostics"]["subgroups"]["true_tenpai_state"] = []
        cases["empty family"] = empty
        malformed = copy.deepcopy(_result_value())
        malformed_group = malformed["diagnostics"]["subgroups"][
            "opponent_relative_seat"
        ][1]
        malformed_group["available"] = True
        cases["malformed empty group"] = malformed
        malformed_count = copy.deepcopy(_result_value())
        malformed_count["diagnostics"]["subgroups"]["public_riichi_state"][0][
            "cell_count"
        ] -= 1
        cases["malformed count"] = malformed_count
        for name, value in cases.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    ValueError, "subgroup|groups|group|count semantics"
                ),
            ):
                result_with_identity(value)

    def test_result_rejects_malformed_depth_diagnostics(self) -> None:
        missing_diagnostic = copy.deepcopy(_result_value())
        missing_diagnostic["diagnostics"].pop("game_macro_mean_delta_mae")
        with self.assertRaisesRegex(ValueError, "diagnostic fields"):
            result_with_identity(missing_diagnostic)
        missing_field = copy.deepcopy(_result_value())
        missing_field["diagnostics"]["sequence_depth"][0].pop("delta_mae")
        with self.assertRaisesRegex(ValueError, "depth diagnostic fields"):
            result_with_identity(missing_field)
        invented_empty_metric = copy.deepcopy(_result_value())
        invented_empty_metric["diagnostics"]["sequence_depth"][1]["snapshot_mae"] = 0.0
        with self.assertRaisesRegex(ValueError, "empty Phase 9 depth"):
            result_with_identity(invented_empty_metric)

    def test_generation_report_binds_runtime_and_preflight(self) -> None:
        execution = {
            "runtime": {
                "python": "3.14.0",
                "revisions": HISTORICAL_REVISIONS,
                "riichienv": "0.4.8",
                "rule_fingerprint": LOCKED_RULE_FINGERPRINT,
                "policy_count": 4,
                "distinct_policy_instances": 4,
            },
            "generation": {
                "raw_corpus_identity": "e" * 64,
                "ordered_seeds": list(HOLDOUT_SEEDS),
                "hanchan_count": 20,
                "turn_anchor_count": 20,
                "failure_count": 0,
                "phase2_equality_verified": True,
            },
        }
        report = generation_report_value("f" * 64, execution)
        self.assertEqual(report["preflight_identity"], "f" * 64)


if __name__ == "__main__":
    unittest.main()
