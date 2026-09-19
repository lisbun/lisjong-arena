"""Tests for lisbun/lisjong-arena#259 Stage A0 protocol lock."""

import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _stage_a0_tenpai_fixtures import (
    NON_TENPAI_HAND,
    TENPAI_HAND,
    build_retained_dataset,
    observed_decision,
    uniform_plan,
)
from lisjong.policy_contract import Seat

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_offline_q.artifact import load_dataset_seed_prefix
from lisjong_arena.learned_policy_offline_q.protocol import Split, split_for_seed
from lisjong_arena.stage_a0_tenpai_feasibility.emission import emit_decision
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import LoadedSidecar
from lisjong_arena.stage_a0_tenpai_protocol_lock import artifact as lock_artifact
from lisjong_arena.stage_a0_tenpai_protocol_lock.baseline import (
    baseline1_probability,
    binary_log_loss,
    build_train_baseline_parameters,
)
from lisjong_arena.stage_a0_tenpai_protocol_lock.materialize import (
    LoadedPublicKeys,
    PublicKeyRecord,
    _align_scientific_prefix,
    load_public_keys,
    write_public_keys,
)
from lisjong_arena.stage_a0_tenpai_protocol_lock.protocol import (
    DOWNSTREAM_SEEDS,
    DOWNSTREAM_STATUS,
    EXPECTED_258_REPORT_IDENTITY,
    EXPECTED_258_SIDECAR_IDENTITY,
    LOCK_A_SCHEMA_VERSION,
    LOCK_B_SCHEMA_VERSION,
    PROTECTED_TEST_SEEDS,
    RETAINED_DATASET_IDENTITY,
    SCIENTIFIC_SEEDS,
    TRAIN_SEEDS,
    TRAINING_SEEDS,
    VALIDATION_SEEDS,
    contract_fingerprint,
    historical_precision_document,
    static_contract_document,
)


class StaticProtocolTest(unittest.TestCase):
    def test_exact_three_training_seeds_and_anchor_are_locked(self):
        self.assertEqual(TRAINING_SEEDS, (0, 1, 2))
        self.assertEqual(
            static_contract_document()["training"]["interactive_anchor_seed"], 0
        )

    def test_scientific_prefix_excludes_protected_test(self):
        self.assertEqual(SCIENTIFIC_SEEDS, tuple(range(245, 271)))
        self.assertEqual(PROTECTED_TEST_SEEDS, tuple(range(271, 277)))
        self.assertFalse(set(SCIENTIFIC_SEEDS).intersection(PROTECTED_TEST_SEEDS))

    def test_downstream_budget_is_derived_from_the_locked_precision_criterion(self):
        precision = historical_precision_document()
        self.assertEqual(DOWNSTREAM_STATUS, "A0 DOWNSTREAM ENABLED")
        self.assertTrue(precision["criterion_satisfied"])
        self.assertEqual(len(DOWNSTREAM_SEEDS), 206)
        self.assertEqual(DOWNSTREAM_SEEDS, tuple(range(37_700, 37_906)))
        self.assertLessEqual(
            precision["projected_normal_95_half_width"],
            precision["acceptable_95_half_width"],
        )
        self.assertLessEqual(
            precision["classical_mde"]["projected_mde"],
            precision["minimum_meaningful_score_effect"],
        )
        self.assertEqual(static_contract_document()["downstream"]["total_games"], 1648)

    def test_contract_fingerprint_is_deterministic(self):
        self.assertEqual(len(contract_fingerprint()), 64)
        self.assertEqual(contract_fingerprint(), contract_fingerprint())


class BaselineContractTest(unittest.TestCase):
    def _artifacts(self):
        cells = []
        records = []
        for seed in TRAIN_SEEDS:
            actor = Seat(seed % 4)
            hand = TENPAI_HAND if seed % 2 else NON_TENPAI_HAND
            decision = observed_decision(
                uniform_plan(hand, actor_seat=actor),
                actor_seat=actor,
            )
            emission = emit_decision(
                decision,
                source_identity=RETAINED_DATASET_IDENTITY,
                seed=seed,
                split=Split.TRAIN,
            )
            cells.extend(emission.cells)
            for cell in emission.cells:
                records.append(
                    PublicKeyRecord(
                        source_identity=RETAINED_DATASET_IDENTITY,
                        seed=seed,
                        split=Split.TRAIN,
                        step_ordinal=decision.step_ordinal,
                        decision_ordinal=decision.decision_ordinal,
                        actor_seat=int(actor),
                        relative_offset=cell.identity.viewer_relative_offset,
                        live_wall_tiles_remaining=42,
                        public_meld_count=0,
                    )
                )
        sidecar = LoadedSidecar(
            path=Path("."),
            manifest={
                "sidecar_identity": "fixture-sidecar",
                "protocol": {"source_identity": RETAINED_DATASET_IDENTITY},
            },
            cells=tuple(cells),
        )
        public = LoadedPublicKeys(
            path=Path("."),
            manifest={
                "artifact_identity": "fixture-public",
                "source_identity": RETAINED_DATASET_IDENTITY,
                "sidecar_identity": "fixture-sidecar",
            },
            records=tuple(records),
        )
        return sidecar, public

    def test_baseline_uses_train_only_jeffreys_probability(self):
        sidecar, public = self._artifacts()
        parameters = build_train_baseline_parameters(sidecar, public)
        self.assertEqual(parameters["eligible_cell_count"], 60)
        self.assertEqual(parameters["baseline0"]["positive"], 30)
        self.assertEqual(parameters["baseline0"]["jeffreys_probability"], 0.5)
        self.assertEqual(
            baseline1_probability(
                parameters,
                live_wall_tiles_remaining=42,
                public_meld_count=0,
            ),
            0.5,
        )

    def test_baseline_backoff_and_out_of_domain_behavior_are_fixed(self):
        sidecar, public = self._artifacts()
        parameters = build_train_baseline_parameters(sidecar, public)
        self.assertEqual(
            baseline1_probability(
                parameters,
                live_wall_tiles_remaining=42,
                public_meld_count=1,
            ),
            0.5,
        )
        self.assertEqual(
            baseline1_probability(
                parameters,
                live_wall_tiles_remaining=41,
                public_meld_count=0,
            ),
            0.5,
        )
        with self.assertRaises(Exception):
            baseline1_probability(
                parameters,
                live_wall_tiles_remaining=85,
                public_meld_count=0,
            )

    def test_binary_log_loss_clips_but_rejects_invalid_probabilities(self):
        self.assertTrue(math.isfinite(binary_log_loss(1, 1.0)))
        self.assertTrue(math.isfinite(binary_log_loss(0, 0.0)))
        with self.assertRaises(Exception):
            binary_log_loss(1, float("nan"))
        with self.assertRaises(Exception):
            binary_log_loss(1, 1.1)


class ScientificMaterializationTest(unittest.TestCase):
    def _scientific_fixture(self, destination):
        emissions_by_seed = {}
        decisions = {}
        for seed in SCIENTIFIC_SEEDS:
            actor = Seat(seed % 4)
            decision = observed_decision(
                uniform_plan(TENPAI_HAND, actor_seat=actor),
                actor_seat=actor,
            )
            emission = emit_decision(
                decision,
                source_identity="fixture",
                seed=seed,
                split=split_for_seed(seed),
            )
            emissions_by_seed[seed] = ((decision, emission),)
            decisions[seed] = (decision,)
        build_retained_dataset(destination, emissions_by_seed)
        return decisions

    def test_exact_alignment_materializes_train_validation_only(self):
        with TemporaryDirectory() as directory:
            dataset_path = Path(directory) / "dataset"
            decisions = self._scientific_fixture(dataset_path)
            dataset = load_dataset_seed_prefix(dataset_path, SCIENTIFIC_SEEDS)
            cells, keys = _align_scientific_prefix(
                dataset,
                observed_decision_source=lambda seed: decisions[seed],
            )
            self.assertEqual(len(dataset.rows), len(SCIENTIFIC_SEEDS))
            self.assertEqual(len(cells), len(SCIENTIFIC_SEEDS) * 3)
            self.assertEqual(len(keys), len(SCIENTIFIC_SEEDS) * 3)
            self.assertEqual({key.seed for key in keys}, set(SCIENTIFIC_SEEDS))
            self.assertFalse(
                {key.seed for key in keys}.intersection(PROTECTED_TEST_SEEDS)
            )

    def test_public_key_artifact_round_trips_strictly(self):
        records = (
            PublicKeyRecord(
                source_identity=RETAINED_DATASET_IDENTITY,
                seed=TRAIN_SEEDS[0],
                split=Split.TRAIN,
                step_ordinal=0,
                decision_ordinal=0,
                actor_seat=0,
                relative_offset=1,
                live_wall_tiles_remaining=42,
                public_meld_count=0,
            ),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "public"
            written = write_public_keys(
                path,
                records,
                sidecar_identity="a" * 64,
                provenance={"fixture": "yes"},
            )
            reloaded = load_public_keys(path)
            self.assertEqual(reloaded.identity, written.identity)
            document = json.loads((path / "manifest.json").read_text("utf-8"))
            document["cell_count"] = 2
            (path / "manifest.json").write_text(
                canonical_json_text(document), encoding="utf-8"
            )
            with self.assertRaises(Exception):
                load_public_keys(path)


class LockArtifactTest(unittest.TestCase):
    def _baseline(self):
        return BaselineContractTest()._artifacts()

    def _lock_a(self):
        contract = static_contract_document()
        document = {
            "schema_version": LOCK_A_SCHEMA_VERSION,
            "contract_fingerprint": contract_fingerprint(),
            "contract": contract,
            "prerequisite": {
                "issue": "lisbun/lisjong-arena#258",
                "report_identity": EXPECTED_258_REPORT_IDENTITY,
                "sidecar_identity": EXPECTED_258_SIDECAR_IDENTITY,
                "route": "retained-augmentation",
                "hard_outcome": "RETAINED AUGMENTATION QUALIFIED",
            },
            "dataset": {
                "identity": RETAINED_DATASET_IDENTITY,
                "train_partition_identity": "b" * 64,
                "validation_partition_identity": "c" * 64,
                "train_seeds": list(TRAIN_SEEDS),
                "validation_seeds": list(VALIDATION_SEEDS),
                "protected_test_seeds_unread": list(PROTECTED_TEST_SEEDS),
            },
            "creation_provenance": {"lisjong_arena_revision": "d" * 40},
            "exposure": {
                "model_training_executed": False,
                "validation_model_inference_executed": False,
                "validation_target_summary_exposed": False,
                "protected_test_payload_read": False,
                "interactive_execution_executed": False,
            },
        }
        document["lock_identity"] = lock_artifact._identity(document)
        return document

    def test_lock_a_strict_readback_rejects_extra_field(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "lock-a.json"
            document = self._lock_a()
            lock_artifact.save_lock(document, path)
            self.assertEqual(
                lock_artifact.load_lock_a(path)["lock_identity"],
                document["lock_identity"],
            )
            broken = dict(document)
            broken["unexpected"] = True
            path.unlink()
            path.write_text(canonical_json_text(broken), encoding="utf-8")
            with self.assertRaises(Exception):
                lock_artifact.load_lock_a(path)

    def test_lock_b_strict_readback_preserves_no_exposure_claims(self):
        sidecar, public = BaselineContractTest()._artifacts()
        baseline = build_train_baseline_parameters(sidecar, public)
        contract = static_contract_document()
        document = {
            "schema_version": LOCK_B_SCHEMA_VERSION,
            "lock_a_identity": "e" * 64,
            "contract_fingerprint": contract_fingerprint(),
            "contract": contract,
            "data": {
                "retained_dataset_identity": RETAINED_DATASET_IDENTITY,
                "train_partition_identity": "b" * 64,
                "validation_partition_identity": "c" * 64,
                "scientific_sidecar_identity": "f" * 64,
                "public_keys_identity": "1" * 64,
                "scientific_source_provenance": {
                    "lisjong_revision": contract["route"]["source_semantics"][
                        "lisjong_revision"
                    ],
                    "lisjong_engine_revision": contract["route"]["source_semantics"][
                        "lisjong_engine_revision"
                    ],
                    "riichienv_version": contract["route"]["source_semantics"][
                        "riichienv_version"
                    ],
                    "python_version": contract["route"]["source_semantics"][
                        "python_version"
                    ],
                },
                "scientific_seeds": list(SCIENTIFIC_SEEDS),
                "protected_test_seeds_unread": list(PROTECTED_TEST_SEEDS),
            },
            "baseline_parameters": baseline,
            "execution_provenance": {
                "lisjong_arena_revision": "d" * 40,
                "lisjong_revision": contract["downstream"]["opponent"][
                    "lisjong_revision"
                ],
                "lisjong_engine_revision": contract["downstream"]["opponent"][
                    "lisjong_engine_revision"
                ],
                "riichienv_version": contract["downstream"]["opponent"][
                    "riichienv_version"
                ],
            },
            "precision_preflight": historical_precision_document(),
            "downstream_status": DOWNSTREAM_STATUS,
            "hard_outcome": lock_artifact.LOCKED_ENABLED_OUTCOME,
            "exposure": {
                "model_training_executed": False,
                "validation_model_inference_executed": False,
                "validation_target_summary_exposed": False,
                "protected_test_payload_read": False,
                "interactive_execution_executed": False,
            },
        }
        document["lock_identity"] = lock_artifact._identity(document)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "lock-b.json"
            lock_artifact.save_lock(document, path)
            loaded = lock_artifact.load_lock_b(path)
            self.assertEqual(
                loaded["hard_outcome"], lock_artifact.LOCKED_ENABLED_OUTCOME
            )
            self.assertFalse(loaded["exposure"]["validation_target_summary_exposed"])


if __name__ == "__main__":
    unittest.main()
