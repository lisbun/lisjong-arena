"""Arena #150 Phase 10 scale learning curveのtorch非依存test。

seed lock、population construction、nested subset、source / runtime lock、
raw / dataset binding、paired comparison、classification、exhaustive outcome、
strict artifact contractの境界を固定する。80 hanchanの実生成もtrainingも
行わない。

#148 historical protocol / seeds / schema / validatorは変更しないので、
regression boundaryとして`330..353`側のconstantsも合わせて固定する。
"""

import json
import tempfile
import unittest
from pathlib import Path

from _stage3_scale_learning_curve_fixtures import (
    SCALE_MAE,
    evaluation_value,
    lock_value,
    model_manifest,
    model_manifests,
    population_manifest,
    provenance_value,
    result_value,
    runtime_value,
    scale_artifacts,
)

from lisjong_arena.learned_policy_offline_q.protocol import REPLACEMENT_TEST_SEEDS
from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.split import (
    MIX_PILOT_DEVELOPMENT_SEEDS,
    MIX_PILOT_TRAIN_SEEDS,
    MIX_PILOT_VALIDATION_SEEDS,
    SCALE_LEARNING_CURVE_SEEDS,
    FirstPartySplitPolicy,
    partition_for_first_party_game,
)
from lisjong_arena.stage3_mix_pilot.protocol import (
    AUGMENTATION_IDENTITY,
    AUGMENTATION_REFERENCE,
    PRIMARY_IDENTITY,
)
from lisjong_arena.stage3_mix_pilot.protocol import (
    ORDERED_SEEDS as MIX_PILOT_ORDERED_SEEDS,
)
from lisjong_arena.stage3_scale_learning_curve.artifact import (
    expected_train_anchors,
    load_result,
    save_result,
    selected_epoch_from_history,
    validate_model_manifest,
    validate_nested_subsets,
)
from lisjong_arena.stage3_scale_learning_curve.comparison import (
    classify_interval,
    compare,
    comparisons,
)
from lisjong_arena.stage3_scale_learning_curve.generation import (
    evidence_value,
    load_population,
    validate_manifest,
)
from lisjong_arena.stage3_scale_learning_curve.lock import validate_lock
from lisjong_arena.stage3_scale_learning_curve.population import (
    assert_recipe_is_seed_free,
    assignments,
    coverage_seat_index,
    occupancy,
    plan_value,
    population_identity,
    recipe_value,
    seat_policy_factories_by_seed,
    subset_binding,
)
from lisjong_arena.stage3_scale_learning_curve.protocol import (
    BENEFIT_INCONCLUSIVE,
    BOOTSTRAP,
    CLEAR_IMPROVEMENT,
    CLEAR_REGRESSION,
    COVERAGE_SLOTS,
    CURVE,
    INCONCLUSIVE,
    ORDERED_SEEDS,
    OUTCOMES,
    PRIMARY_CURVE_PAIR,
    REGRESSION,
    SCALES,
    SEAT_SLOTS,
    SEED_PLAN_REFORMULATE,
    SIGNAL,
    SPLIT_POLICY,
    STOP_INVALID,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
    ScaleError,
    check_freshness,
    declared_occupied_seeds,
    identity,
    train_seeds,
)
from lisjong_arena.stage3_scale_learning_curve.result import (
    assemble_result,
    classify,
    validate_evaluation,
    validate_result,
)


def _clone(value):
    return json.loads(json.dumps(value))


class SeedPlanTest(unittest.TestCase):
    """`SEED PLAN REFORMULATE`とlocked `360..439`の固定。"""

    def test_the_preferred_354_range_collides_with_the_140_replacement_test(self):
        outcome, overlap = check_freshness(tuple(range(354, 434)))
        self.assertEqual(outcome, SEED_PLAN_REFORMULATE)
        self.assertEqual(tuple(overlap), REPLACEMENT_TEST_SEEDS)

    def test_the_replacement_test_seeds_are_treated_as_occupied(self):
        self.assertTrue(set(REPLACEMENT_TEST_SEEDS) <= declared_occupied_seeds())

    def test_the_locked_360_range_is_fresh(self):
        self.assertEqual(check_freshness(ORDERED_SEEDS), (None, []))

    def test_the_locked_population_is_exactly_360_to_439(self):
        self.assertEqual(ORDERED_SEEDS, tuple(range(360, 440)))
        self.assertEqual(len(ORDERED_SEEDS), 80)
        self.assertEqual(TRAIN_SEEDS, tuple(range(360, 424)))
        self.assertEqual(VALIDATION_SEEDS, tuple(range(424, 440)))
        self.assertEqual(len(TRAIN_SEEDS), 64)
        self.assertEqual(len(VALIDATION_SEEDS), 16)
        self.assertEqual(set(TRAIN_SEEDS) & set(VALIDATION_SEEDS), set())

    def test_the_split_policy_declares_the_same_population(self):
        self.assertEqual(SCALE_LEARNING_CURVE_SEEDS, ORDERED_SEEDS)
        self.assertEqual(SPLIT_POLICY, FirstPartySplitPolicy.SCALE_LEARNING_CURVE)

    def test_the_split_has_no_test_partition(self):
        for seed in TRAIN_SEEDS:
            self.assertIs(
                partition_for_first_party_game(
                    FIRST_PARTY_SOURCE_CLASS, seed, SPLIT_POLICY
                ),
                DatasetPartition.TRAIN,
            )
        for seed in VALIDATION_SEEDS:
            self.assertIs(
                partition_for_first_party_game(
                    FIRST_PARTY_SOURCE_CLASS, seed, SPLIT_POLICY
                ),
                DatasetPartition.VALIDATION,
            )
        for seed in (359, 440, 1000):
            with self.assertRaises(ValueError):
                partition_for_first_party_game(
                    FIRST_PARTY_SOURCE_CLASS, seed, SPLIT_POLICY
                )

    def test_a_collision_after_result_exposure_cannot_be_reformulated(self):
        with self.assertRaises(ScaleError):
            check_freshness(tuple(range(354, 434)), result_exposed=True)

    def test_the_seed_plan_shape_is_fixed(self):
        for seeds in (
            tuple(range(360, 420)),
            tuple(range(360, 440, 1))[:-1] + (500,),
            list(range(360, 440)),
        ):
            with self.assertRaises(ScaleError):
                check_freshness(seeds)


class HistoricalBoundaryTest(unittest.TestCase):
    """#148 historical protocolをPhase 10が書き換えていないこと。"""

    def test_mix_pilot_seeds_are_unchanged(self):
        self.assertEqual(MIX_PILOT_TRAIN_SEEDS, tuple(range(330, 348)))
        self.assertEqual(MIX_PILOT_VALIDATION_SEEDS, tuple(range(348, 354)))
        self.assertEqual(MIX_PILOT_DEVELOPMENT_SEEDS, tuple(range(330, 354)))
        self.assertEqual(MIX_PILOT_ORDERED_SEEDS, tuple(range(330, 354)))

    def test_mix_pilot_split_policy_value_is_unchanged(self):
        self.assertEqual(
            FirstPartySplitPolicy.MIX_PILOT_DEVELOPMENT.value,
            "first-party-seeds-330-353-18-6-development-only-v1",
        )

    def test_phase10_does_not_reuse_the_historical_population(self):
        self.assertEqual(set(ORDERED_SEEDS) & set(MIX_PILOT_DEVELOPMENT_SEEDS), set())


class PopulationTest(unittest.TestCase):
    """locked 12.5% recipeとnested subsetのexact construction。"""

    def test_the_recipe_is_the_locked_first_party_recipe(self):
        recipe = recipe_value()
        self.assertEqual(recipe["primary"]["identity"], PRIMARY_IDENTITY)
        self.assertEqual(recipe["augmentation"]["identity"], AUGMENTATION_IDENTITY)
        self.assertEqual(recipe["augmentation"]["reference"], AUGMENTATION_REFERENCE)
        self.assertEqual(recipe["augmentation_fraction"], 0.125)

    def test_the_augmentation_fraction_is_exact(self):
        plan = plan_value()
        self.assertEqual(plan["seat_slots"], 320)
        self.assertEqual(plan["coverage_slots"], 40)
        self.assertEqual(SEAT_SLOTS, 320)
        self.assertEqual(COVERAGE_SLOTS, 40)
        coverage = sum(
            row["seat_identities"].count(AUGMENTATION_IDENTITY)
            for row in plan["assignments"]
        )
        self.assertEqual(coverage, 40)
        self.assertEqual(coverage / plan["seat_slots"], 0.125)

    def test_at_most_one_coverage_seat_per_hanchan(self):
        for row in assignments():
            self.assertLessEqual(row["seat_identities"].count(AUGMENTATION_IDENTITY), 1)

    def test_seat_balance_is_exact_for_the_population_and_every_subset(self):
        self.assertEqual(occupancy(ORDERED_SEEDS), [10, 10, 10, 10])
        self.assertEqual(occupancy(train_seeds("S16")), [2, 2, 2, 2])
        self.assertEqual(occupancy(train_seeds("S32")), [4, 4, 4, 4])
        self.assertEqual(occupancy(train_seeds("S64")), [8, 8, 8, 8])
        self.assertEqual(occupancy(VALIDATION_SEEDS), [2, 2, 2, 2])

    def test_seat_assignment_is_prng_free_and_deterministic(self):
        for seed in ORDERED_SEEDS:
            index = seed - 360
            expected = (index // 2) % 4 if index % 2 == 0 else None
            self.assertEqual(coverage_seat_index(seed), expected)
        self.assertEqual(assignments(), assignments())
        self.assertEqual(plan_value(), plan_value())
        self.assertEqual(population_identity(), population_identity())

    def test_subsets_are_nested_prefixes_derived_from_seeds_only(self):
        self.assertEqual(train_seeds("S16"), tuple(range(360, 376)))
        self.assertEqual(train_seeds("S32"), tuple(range(360, 392)))
        self.assertEqual(train_seeds("S64"), tuple(range(360, 424)))
        self.assertEqual(train_seeds("S64"), TRAIN_SEEDS)
        for smaller, larger in (("S16", "S32"), ("S32", "S64")):
            self.assertLess(set(train_seeds(smaller)), set(train_seeds(larger)))
        for scale in SCALES:
            self.assertEqual(set(train_seeds(scale)) & set(VALIDATION_SEEDS), set())
        with self.assertRaises(ScaleError):
            train_seeds("S128")

    def test_policy_instances_are_fresh_per_game_and_seat(self):
        factories = seat_policy_factories_by_seed()
        self.assertEqual(sorted(factories), list(ORDERED_SEEDS))
        instances = [factory() for factory in factories[ORDERED_SEEDS[0]].values()] + [
            factory() for factory in factories[ORDERED_SEEDS[2]].values()
        ]
        self.assertEqual(len({id(value) for value in instances}), len(instances))

    def test_the_carry_forward_recipe_is_seed_free(self):
        assert_recipe_is_seed_free(recipe_value())
        with self.assertRaises(ScaleError):
            assert_recipe_is_seed_free({**recipe_value(), "seeds": list(ORDERED_SEEDS)})
        with self.assertRaises(ScaleError):
            assert_recipe_is_seed_free(
                {**recipe_value(), "split_policy_id": SPLIT_POLICY.value}
            )

    def test_the_plan_records_the_subsets_without_a_test_partition(self):
        plan = plan_value()
        self.assertIs(plan["test_partition_present"], False)
        self.assertEqual(
            plan["subsets"], {scale: list(train_seeds(scale)) for scale in SCALES}
        )


class ExecutionLockTest(unittest.TestCase):
    """source / runtime lockがinstalled provenance mismatchを拒否すること。"""

    def test_a_well_formed_lock_is_accepted(self):
        self.assertEqual(validate_lock(lock_value()), lock_value())

    def test_the_pinned_source_revisions_are_required(self):
        for revisions in (
            {"lisjong": "0" * 40},
            {"lisjong_engine": "0" * 40},
        ):
            broken = provenance_value(
                source_revisions={
                    **provenance_value()["source_revisions"],
                    **revisions,
                }
            )
            with self.assertRaises(ScaleError):
                validate_lock(lock_value(provenance=broken))

    def test_unresolved_provenance_is_rejected(self):
        with self.assertRaises(ScaleError):
            validate_lock(lock_value(provenance=provenance_value(fully_resolved=False)))

    def test_a_missing_arena_execution_revision_is_rejected(self):
        broken = provenance_value(
            source_revisions={
                **provenance_value()["source_revisions"],
                "lisjong_arena": None,
            }
        )
        with self.assertRaises(ScaleError):
            validate_lock(lock_value(provenance=broken))

    def test_the_runtime_is_cpu_only_single_threaded_and_deterministic(self):
        for override in (
            {"device": "cuda"},
            {"torch_threads": 4},
            {"deterministic_algorithms": False},
            {"free_threaded": True},
            {"torch": "2.12.0"},
            {"riichienv": "0.4.7"},
            {"python": "3.13.1"},
            {"platform": ""},
        ):
            with self.assertRaises(ScaleError):
                validate_lock(lock_value(runtime=runtime_value(**override)))

    def test_a_lock_must_be_pre_exposure_and_carry_a_seed_audit(self):
        with self.assertRaises(ScaleError):
            validate_lock(lock_value(result_exposed=True))
        with self.assertRaises(ScaleError):
            validate_lock(lock_value(seed_audit="  "))

    def test_a_tampered_population_plan_is_rejected(self):
        plan = _clone(plan_value())
        plan["ordered_seeds"] = list(range(354, 434))
        with self.assertRaises(ScaleError):
            validate_lock(lock_value(population_plan=plan))

    def test_extra_or_missing_lock_fields_are_rejected(self):
        extra = lock_value()
        extra["extra"] = 1
        with self.assertRaises(ScaleError):
            validate_lock(extra)
        missing = lock_value()
        del missing["runtime"]
        with self.assertRaises(ScaleError):
            validate_lock(missing)


class PopulationManifestTest(unittest.TestCase):
    """raw / dataset strict readbackとevidence re-derivation。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.lock = lock_value()
        cls.population, cls.raw, cls.dataset = population_manifest(cls.root, cls.lock)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_a_well_formed_manifest_is_accepted(self):
        validate_manifest(_clone(self.population), self.lock)

    def test_the_manifest_binds_to_the_locked_plan_and_execution_lock(self):
        self.assertEqual(self.population["population_plan"], plan_value())
        self.assertEqual(self.population["population_identity"], population_identity())
        self.assertEqual(
            self.population["execution_lock_identity"], identity(self.lock)
        )

    def test_evidence_covers_every_hanchan_without_a_test_partition(self):
        anchors = self.population["evidence"]["anchors_by_seed"]
        self.assertEqual(sorted(anchors), sorted(str(s) for s in ORDERED_SEEDS))
        self.assertTrue(all(rows for rows in anchors.values()))
        flattened = [row for seed in ORDERED_SEEDS for row in anchors[str(seed)]]
        self.assertEqual(len(set(flattened)), len(flattened))
        self.assertEqual(self.population["evidence"]["anchor_identities"], flattened)

    def test_train_and_validation_anchors_are_disjoint(self):
        anchors = self.population["evidence"]["anchors_by_seed"]
        train = {a for s in TRAIN_SEEDS for a in anchors[str(s)]}
        validation = {a for s in VALIDATION_SEEDS for a in anchors[str(s)]}
        self.assertEqual(train & validation, set())

    def test_evidence_is_re_derived_from_the_persisted_corpus(self):
        self.assertEqual(
            evidence_value(self.raw, self.dataset), self.population["evidence"]
        )

    def test_a_tampered_anchor_identity_is_rejected(self):
        broken = _clone(self.population)
        seed = str(ORDERED_SEEDS[0])
        broken["evidence"]["anchors_by_seed"][seed] = ["f" * 64]
        with self.assertRaises(ScaleError):
            validate_manifest(broken, self.lock)

    def test_a_manifest_that_renames_its_dataset_is_rejected_on_readback(self):
        root = self.root / "readback"
        population, raw, dataset = population_manifest(root, self.lock)
        (root / "population.json").write_bytes(canonical_json_bytes(population))
        self.assertEqual(load_population(root, self.lock)[0], population)
        tampered = _clone(population)
        tampered["dataset_identity"] = "b" * 64
        tampered["phase5"]["dataset_identity"] = "b" * 64
        tampered["evidence"]["inventory"]["dataset_identity"] = "b" * 64
        (root / "population.json").write_bytes(canonical_json_bytes(tampered))
        with self.assertRaises(ScaleError):
            load_population(root, self.lock)

    def test_non_canonical_manifest_bytes_are_rejected(self):
        root = self.root / "bytes"
        population, _raw, _dataset = population_manifest(root, self.lock)
        (root / "population.json").write_text(
            json.dumps(population, indent=2), encoding="utf-8"
        )
        with self.assertRaises(ScaleError):
            load_population(root, self.lock)

    def test_a_failed_or_unverified_generation_is_rejected(self):
        for override in ({"failure_count": 1}, {"phase2_equality_verified": False}):
            with self.assertRaises(ScaleError):
                validate_manifest({**_clone(self.population), **override}, self.lock)

    def test_manifest_fields_are_exact(self):
        extra = _clone(self.population)
        extra["extra"] = 1
        with self.assertRaises(ScaleError):
            validate_manifest(extra, self.lock)


class ModelArtifactTest(unittest.TestCase):
    """model artifactがexact TRAIN subsetとdatasetへbindされていること。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.lock = lock_value()
        cls.population, _raw, _dataset = population_manifest(cls.root, cls.lock)
        cls.models = model_manifests(cls.population, cls.lock)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_well_formed_manifests_are_accepted(self):
        for scale in SCALES:
            validate_model_manifest(
                _clone(self.models[scale]), self.population, self.lock
            )

    def test_the_model_binds_to_its_exact_train_subset(self):
        for scale in SCALES:
            manifest = self.models[scale]
            self.assertEqual(
                manifest["subset"],
                subset_binding(
                    scale,
                    raw_corpus_identity=self.population["raw_corpus_identity"],
                    dataset_identity=self.population["dataset_identity"],
                    provenance=self.lock["provenance"],
                ),
            )
            self.assertEqual(
                manifest["train_anchor_identities"],
                expected_train_anchors(scale, self.population),
            )
            self.assertEqual(
                len(manifest["subset"]["train_seeds"]),
                {"S16": 16, "S32": 32, "S64": 64}[scale],
            )

    def test_a_model_cannot_claim_another_scales_subset(self):
        broken = _clone(self.models["S16"])
        broken["subset"] = _clone(self.models["S32"]["subset"])
        with self.assertRaises(ScaleError):
            validate_model_manifest(broken, self.population, self.lock)

    def test_a_model_cannot_claim_another_scales_anchors(self):
        broken = _clone(self.models["S16"])
        broken["train_anchor_identities"] = _clone(
            self.models["S32"]["train_anchor_identities"]
        )
        with self.assertRaises(ScaleError):
            validate_model_manifest(broken, self.population, self.lock)

    def test_validation_anchors_may_not_leak_into_the_train_subset(self):
        broken = _clone(self.models["S16"])
        leaked = self.population["evidence"]["anchors_by_seed"][
            str(VALIDATION_SEEDS[0])
        ][0]
        broken["train_anchor_identities"] = sorted(
            broken["train_anchor_identities"] + [leaked]
        )
        with self.assertRaises(ScaleError):
            validate_model_manifest(broken, self.population, self.lock)

    def test_training_config_may_not_vary_by_scale(self):
        broken = _clone(self.models["S64"])
        broken["training_lock"]["training_config"]["learning_rate"] = 0.0005
        with self.assertRaises(ScaleError):
            validate_model_manifest(broken, self.population, self.lock)

    def test_the_bptt_policy_is_the_shared_full_population_inventory(self):
        for scale in SCALES:
            self.assertEqual(
                self.models[scale]["full_inventory"],
                self.population["evidence"]["inventory"],
            )
        broken = _clone(self.models["S16"])
        broken["full_inventory"]["bptt_policy"]["truncation_length"] = 3
        with self.assertRaises(ScaleError):
            validate_model_manifest(broken, self.population, self.lock)

    def test_the_selected_epoch_follows_the_locked_checkpoint_rule(self):
        history = [
            {"epoch": 1, "train_mse": 1.0, "validation_mae": 0.5},
            {"epoch": 2, "train_mse": 0.9, "validation_mae": 0.5},
            {"epoch": 3, "train_mse": 0.8, "validation_mae": 0.4},
        ]
        self.assertEqual(selected_epoch_from_history(history), 3)
        self.assertEqual(
            selected_epoch_from_history(history[:2]), 1, "ties keep the earlier epoch"
        )
        broken = _clone(self.models["S16"])
        broken["selected_epoch"] = 3
        with self.assertRaises(ScaleError):
            validate_model_manifest(broken, self.population, self.lock)

    def test_the_selected_epoch_mae_must_match_the_recorded_evaluation(self):
        broken = _clone(self.models["S16"])
        broken["loss_history"][1]["validation_mae"] = 0.01
        with self.assertRaises(ScaleError):
            validate_model_manifest(broken, self.population, self.lock)

    def test_the_model_runtime_must_be_the_locked_runtime(self):
        broken = _clone(self.models["S16"])
        broken["runtime"] = runtime_value(torch_threads=2)
        with self.assertRaises(ScaleError):
            validate_model_manifest(broken, self.population, self.lock)

    def test_nested_membership_is_enforced_across_the_scales(self):
        validate_nested_subsets(self.models, self.population)
        broken = dict(self.models)
        broken["S32"] = model_manifest("S32", self.population, self.lock)
        broken["S32"]["train_anchor_identities"] = _clone(
            self.models["S16"]["train_anchor_identities"]
        )
        with self.assertRaises(ScaleError):
            validate_nested_subsets(broken, self.population)


class EvaluationTest(unittest.TestCase):
    """shared fixed VALIDATION上のmeasurement contract。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.lock = lock_value()
        cls.population, _raw, _dataset = population_manifest(
            Path(cls._directory.name), cls.lock
        )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_a_well_formed_cell_passes_the_physical_gate(self):
        cell = evaluation_value(self.population)
        self.assertTrue(validate_evaluation(_clone(cell), self.population))

    def test_the_cell_covers_exactly_the_16_validation_hanchan(self):
        cell = evaluation_value(self.population)
        self.assertEqual(
            [row["game_seed"] for row in cell["per_game"]], list(VALIDATION_SEEDS)
        )
        self.assertEqual(len(cell["per_game"]), 16)

    def test_a_measurement_that_is_not_the_recorded_pooled_mae_is_rejected(self):
        cell = _clone(evaluation_value(self.population))
        cell["per_game"][0]["candidate_mae"] += 0.1
        with self.assertRaises(ScaleError):
            validate_evaluation(cell, self.population)

    def test_a_wrong_anchor_count_is_rejected(self):
        cell = _clone(evaluation_value(self.population))
        cell["per_game"][0]["sample_count"] += 1
        with self.assertRaises(ScaleError):
            validate_evaluation(cell, self.population)

    def test_a_forged_physical_gate_is_rejected(self):
        cell = _clone(evaluation_value(self.population))
        cell["physical_consistency"]["maximum_row_column_residual"] = 1.0
        with self.assertRaises(ScaleError):
            validate_evaluation(cell, self.population)

    def test_a_failing_physical_gate_is_reported_not_raised(self):
        cell = evaluation_value(self.population, physical_passed=False)
        self.assertFalse(validate_evaluation(_clone(cell), self.population))

    def test_depth_diagnostics_follow_the_inventory(self):
        cell = evaluation_value(self.population)
        buckets = self.population["evidence"]["inventory"]["partitions"]["validation"][
            "depth_bucket_counts"
        ]
        self.assertEqual(
            {row["bucket"]: row["sample_count"] for row in cell["depth_diagnostics"]},
            buckets,
        )
        broken = _clone(cell)
        broken["depth_diagnostics"][0]["sample_count"] += 1
        with self.assertRaises(ScaleError):
            validate_evaluation(broken, self.population)

    def test_inference_throughput_is_required(self):
        broken = _clone(evaluation_value(self.population))
        broken["inference"]["samples_per_second"] = 0.0
        with self.assertRaises(ScaleError):
            validate_evaluation(broken, self.population)


class ComparisonTest(unittest.TestCase):
    """deterministic bootstrapとexact interval threshold behaviour。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.lock = lock_value()
        cls.population, _raw, _dataset = population_manifest(
            Path(cls._directory.name), cls.lock
        )
        cls.cells = {
            scale: evaluation_value(cls.population, mae=SCALE_MAE[scale])
            for scale in SCALES
        }

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_the_bootstrap_constants_are_locked(self):
        self.assertEqual(BOOTSTRAP["replicates"], 10000)
        self.assertEqual(BOOTSTRAP["seed"], 148)
        self.assertEqual(BOOTSTRAP["lower_percentile"], 2.5)
        self.assertEqual(BOOTSTRAP["upper_percentile"], 97.5)
        self.assertEqual(BOOTSTRAP["order_statistic_indices"], [249, 9750])

    def test_the_bootstrap_is_deterministic(self):
        first = compare("S16", "S64", self.cells)
        second = compare("S16", "S64", self.cells)
        self.assertEqual(first, second)
        self.assertEqual(first["bootstrap"], dict(BOOTSTRAP))
        self.assertEqual(first["hanchan"], 16)

    def test_a_positive_delta_means_the_larger_population_is_better(self):
        row = compare("S16", "S64", self.cells)
        self.assertGreater(row["pooled_delta_mae"], 0)
        self.assertEqual(row["classification"], CLEAR_IMPROVEMENT)

    def test_interval_thresholds_are_exact(self):
        self.assertEqual(classify_interval(1e-12, 1.0), CLEAR_IMPROVEMENT)
        self.assertEqual(classify_interval(0.0, 1.0), INCONCLUSIVE)
        self.assertEqual(classify_interval(-1.0, 0.0), INCONCLUSIVE)
        self.assertEqual(classify_interval(-1.0, -1e-12), CLEAR_REGRESSION)
        self.assertEqual(classify_interval(-1.0, 1.0), INCONCLUSIVE)
        with self.assertRaises(ScaleError):
            classify_interval(1.0, -1.0)

    def test_the_curve_covers_the_primary_and_secondary_comparisons(self):
        rows = comparisons(self.cells)
        self.assertEqual([(r["smaller"], r["larger"]) for r in rows], list(CURVE))
        self.assertIn(PRIMARY_CURVE_PAIR, [(r["smaller"], r["larger"]) for r in rows])

    def test_unpaired_cells_are_rejected(self):
        broken = _clone(self.cells)
        broken["S64"]["validation_anchor_identities"] = ["f" * 64]
        with self.assertRaises(ScaleError):
            compare("S16", "S64", broken)

    def test_comparisons_outside_the_locked_curve_are_rejected(self):
        with self.assertRaises(ScaleError):
            compare("S64", "S16", self.cells)


class OutcomeTest(unittest.TestCase):
    """exhaustive Phase 10 outcomeがevidenceからだけ導かれること。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.lock = lock_value()
        cls.population, _raw, _dataset = population_manifest(
            Path(cls._directory.name), cls.lock
        )

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _result(self, **by_scale):
        models = model_manifests(self.population, self.lock, **by_scale)
        return result_value(self.population, models, self.lock)

    def test_a_monotone_curve_is_a_scale_signal(self):
        value = self._result()
        self.assertEqual(value["outcome"], SIGNAL)
        self.assertIn(value["outcome"], OUTCOMES)

    def test_a_worse_s64_is_a_scale_regression(self):
        value = self._result(S64={"mae": 0.44})
        self.assertEqual(value["outcome"], REGRESSION)

    def test_a_flat_curve_is_inconclusive_not_equivalent(self):
        value = self._result(S32={"mae": 0.40}, S64={"mae": 0.40})
        self.assertEqual(value["outcome"], BENEFIT_INCONCLUSIVE)
        self.assertNotIn("equivalent", " ".join(value["reasons"]))

    def test_a_failing_physical_gate_stops_the_child(self):
        value = self._result(S32={"physical_passed": False})
        self.assertEqual(value["outcome"], STOP_INVALID)
        self.assertFalse(value["gates"]["S32_physical_validity"])
        self.assertEqual(value["comparisons"], [])

    def test_a_self_rollout_failure_stops_the_child(self):
        value = self._result(S16={"self_rollout_failures": 2})
        self.assertEqual(value["outcome"], STOP_INVALID)
        self.assertFalse(value["gates"]["S16_self_rollout_complete"])

    def test_seed_plan_reformulate_is_never_derived_from_a_measurement(self):
        for gates, paired in (
            ({"a": False}, []),
            (
                {"a": True},
                [
                    {
                        "smaller": smaller,
                        "larger": larger,
                        "classification": INCONCLUSIVE,
                    }
                    for smaller, larger in CURVE
                ],
            ),
        ):
            outcome, _reasons = classify(gates, paired)
            self.assertIn(outcome, OUTCOMES)
            self.assertNotEqual(outcome, SEED_PLAN_REFORMULATE)

    def test_a_positive_outcome_does_not_extend_the_child(self):
        value = self._result()
        self.assertIs(value["formal_test"], False)
        self.assertIs(value["accumulated_with_historical_evidence"], False)
        self.assertEqual(
            value["population"]["population_plan"]["ordered_seeds"],
            list(ORDERED_SEEDS),
        )


class ResultArtifactTest(unittest.TestCase):
    """result artifactがraw evidenceから結論を再導出できること。"""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.lock = lock_value()
        cls.population, _raw, _dataset = population_manifest(cls.root, cls.lock)
        cls.models = model_manifests(cls.population, cls.lock)
        cls.value = result_value(cls.population, cls.models, cls.lock)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def test_a_well_formed_result_round_trips(self):
        destination = self.root / "result.json"
        save_result(destination, self.value, self.lock)
        loaded = load_result(destination, self.lock)
        self.assertEqual(
            {k: v for k, v in loaded.items() if k != "result_identity"}, self.value
        )
        with self.assertRaises(FileExistsError):
            save_result(destination, self.value, self.lock)

    def test_a_tampered_result_identity_is_rejected(self):
        destination = self.root / "identity.json"
        save_result(destination, self.value, self.lock)
        payload = json.loads(destination.read_bytes())
        payload["result_identity"] = "0" * 64
        destination.write_bytes(canonical_json_bytes(payload))
        with self.assertRaises(ScaleError):
            load_result(destination, self.lock)

    def test_a_freely_relabelled_outcome_is_rejected(self):
        broken = _clone(self.value)
        broken["outcome"] = BENEFIT_INCONCLUSIVE
        with self.assertRaises(ScaleError):
            validate_result(broken, self.lock)

    def test_a_self_consistent_comparison_block_is_rejected(self):
        """comparisonとclassificationだけを都合よく書き換えたresultを拒否する。

        符号もclassificationも互いに整合しているので、内部整合だけを見る
        validatorは通してしまう。per-hanchan measurementからの再導出だけが
        これを拒否できる。
        """
        flat = model_manifests(
            self.population, self.lock, S32={"mae": 0.40}, S64={"mae": 0.40}
        )
        broken = result_value(self.population, flat, self.lock)
        self.assertEqual(broken["outcome"], BENEFIT_INCONCLUSIVE)
        broken = _clone(broken)
        broken["comparisons"] = _clone(self.value["comparisons"])
        broken["outcome"] = SIGNAL
        broken["reasons"] = _clone(self.value["reasons"])
        with self.assertRaises(ScaleError):
            validate_result(broken, self.lock)

    def test_a_self_consistent_measurement_swap_is_rejected(self):
        """measurement blockを整合的に差し替えても、結論は追随しない。

        S16のmeasurementをS64と同じ水準へ書き換えると、recorded comparisonと
        outcomeはもう再導出できない。
        """
        broken = _clone(self.value)
        replacement = model_manifest(
            "S16", self.population, self.lock, mae=SCALE_MAE["S64"]
        )
        broken["models"]["S16"]["evaluation"] = _clone(replacement["evaluation"])
        broken["models"]["S16"]["loss_history"] = _clone(replacement["loss_history"])
        with self.assertRaises(ScaleError):
            validate_result(broken, self.lock)

    def test_a_self_consistent_population_swap_is_rejected(self):
        """locked planとは別のpopulationを、内部整合したresultとして拒否する。

        coverage seatを1つずらしてもslot数もbalanceも保たれるため、planと
        identityを一緒に書き換えれば内部整合する。locked planへのbindingだけが
        これを拒否できる。
        """
        broken = _clone(self.value)
        plan = broken["population"]["population_plan"]
        for row in plan["assignments"]:
            seats = row["seat_identities"]
            if AUGMENTATION_IDENTITY in seats:
                index = seats.index(AUGMENTATION_IDENTITY)
                seats[index] = PRIMARY_IDENTITY
                seats[(index + 1) % len(seats)] = AUGMENTATION_IDENTITY
        broken["population"]["population_identity"] = identity(plan)
        with self.assertRaises(ScaleError):
            validate_result(broken, self.lock)

    def test_the_carry_forward_recipe_does_not_leak_development_seeds(self):
        recipe = self.value["carry_forward_recipe"]
        encoded = canonical_json_bytes(recipe).decode("utf-8")
        self.assertNotIn(SPLIT_POLICY.value, encoded)
        for seed in ORDERED_SEEDS:
            self.assertNotIn(str(seed), encoded)
        broken = _clone(self.value)
        broken["carry_forward_recipe"]["seeds"] = list(ORDERED_SEEDS)
        with self.assertRaises(ScaleError):
            validate_result(broken, self.lock)

    def test_the_result_records_the_cost_accounting_scopes(self):
        cost = self.value["cost_accounting"]
        self.assertEqual(sorted(cost["training"]), sorted(SCALES))
        self.assertEqual(sorted(cost["inference"]), sorted(SCALES))
        for name in (
            "phase4_cpu_seconds",
            "phase4_wall_seconds",
            "phase5_cpu_seconds",
            "phase5_wall_seconds",
            "raw_compressed_bytes",
            "raw_uncompressed_bytes",
            "dataset_bytes",
            "anchor_count",
            "peak_process_ram_bytes",
        ):
            self.assertIn(name, cost["generation"])

    def test_the_result_requires_every_scale(self):
        with self.assertRaises(ScaleError):
            assemble_result(
                self.population,
                {scale: self.models[scale] for scale in ("S16", "S32")},
                self.lock,
            )

    def test_the_conditional_uniform_baseline_is_shared_across_scales(self):
        broken = _clone(self.value)
        for row in broken["models"]["S64"]["evaluation"]["per_game"]:
            row["snapshot_mae"] += 0.05
            row["delta_mae"] = row["snapshot_mae"] - row["candidate_mae"]
        evaluation = broken["models"]["S64"]["evaluation"]
        rows = evaluation["per_game"]
        count = sum(row["sample_count"] for row in rows)
        evaluation["conditional_uniform_mae"] = (
            sum(row["snapshot_mae"] * row["sample_count"] for row in rows) / count
        )
        with self.assertRaises(ScaleError):
            validate_result(broken, self.lock)

    def test_the_result_binds_to_its_execution_lock(self):
        self.assertEqual(self.value["execution_lock_identity"], identity(self.lock))
        other = lock_value(seed_audit="a different audit")
        with self.assertRaises(ScaleError):
            validate_result(self.value, other)


class DatasetBoundaryTest(unittest.TestCase):
    """Phase 10 datasetがwhole-hanchan partitionを守っていること。"""

    def test_the_dataset_validator_rejects_partition_leakage(self):
        from lisjong_arena.stage3_scale_learning_curve.experiment import (
            validate_dataset,
        )

        with tempfile.TemporaryDirectory() as directory:
            _raw, dataset, _report = scale_artifacts(Path(directory))
            validate_dataset(dataset)
            self.assertEqual(
                [a.game.game_seed for a in dataset.games], list(ORDERED_SEEDS)
            )
            self.assertEqual(
                [
                    a.game.game_seed
                    for a in dataset.games
                    if a.partition is DatasetPartition.VALIDATION
                ],
                list(VALIDATION_SEEDS),
            )
            self.assertFalse(
                any(
                    reference.partition is DatasetPartition.TEST
                    for reference in dataset.examples
                )
            )


class ImportContractTest(unittest.TestCase):
    """通常のimportとCLI構築でtorchを要求しないこと。"""

    def test_the_normal_phase10_import_and_cli_are_torch_free(self):
        """同一processのimport順に依存しないよう、独立したinterpreterで確認する。"""
        import subprocess
        import sys

        script = (
            "import sys\n"
            "from lisjong_arena.stage3_scale_learning_curve.__main__ import _parser\n"
            "from lisjong_arena.stage3_scale_learning_curve import training_lock\n"
            "_parser()\n"
            "training_lock()\n"
            "raise SystemExit(1 if 'torch' in sys.modules else 0)\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(completed.returncode, 0)

    def test_the_cli_offers_no_test_partition_or_rescue_option(self):
        from lisjong_arena.stage3_scale_learning_curve.__main__ import _parser

        parser = _parser()
        command = next(action for action in parser._actions if action.dest == "command")
        self.assertEqual(
            set(command.choices), {"plan", "lock", "generate", "train", "curve"}
        )
        help_text = parser.format_help().lower()
        for forbidden in ("test partition", "--seed", "--extend", "128"):
            self.assertNotIn(forbidden, help_text)


if __name__ == "__main__":
    unittest.main()
