"""Arena #148 population-mix pilotのtorch非依存contract tests。

72 hanchanのformal pilotはここでは実行しない。population construction、seed /
split discipline、source attribution、accounting、dataset retention、paired
comparison、exhaustive classificationの境界だけを固定する。
"""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from _stage3_mix_pilot_fixtures import (
    ARM_DATASET_IDENTITIES,
    accounting_totals,
    arm_manifest_value,
    arm_manifests,
    comparison_row_value,
    comparison_rows,
    evaluation_cell_value,
    matrix_cells,
    mix_artifacts,
    mix_corpus,
    opportunity_diagnostic_value,
    result_value,
    self_consistent_swapped_arm_result,
)

from lisjong_arena.phase2_training_anchor.extraction import FIRST_PARTY_SOURCE_CLASS
from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.split import (
    KAN_COVERAGE_DEVELOPMENT_SEEDS,
    MIX_PILOT_DEVELOPMENT_SEEDS,
    MIX_PILOT_TRAIN_SEEDS,
    MIX_PILOT_VALIDATION_SEEDS,
    QUANTITATIVE_SEEDS,
    STAGE3_DEVELOPMENT_SEEDS,
    FirstPartySplitPolicy,
    partition_for_first_party_game,
)
from lisjong_arena.phase9_confirmatory.protocol import HOLDOUT_SEEDS
from lisjong_arena.stage3_entry_gate.population import (
    population_a_plan,
    population_b_plan,
    population_c_plan,
)
from lisjong_arena.stage3_kan_coverage.opportunity import (
    KanDecisionRecord,
    KanOpportunityDiagnostic,
)
from lisjong_arena.stage3_kan_coverage.population import kan_coverage_population_plan
from lisjong_arena.stage3_mix_pilot import __main__ as cli
from lisjong_arena.stage3_mix_pilot.artifact import (
    MixArtifactError,
    load_result,
    save_result,
    validate_result_value,
)
from lisjong_arena.stage3_mix_pilot.attribution import (
    MixAttributionError,
    attribute_sources,
    complement_slots,
    primary_source_summary,
    restrict_accounts,
    restrict_diagnostic,
)
from lisjong_arena.stage3_mix_pilot.comparison import (
    MixComparisonError,
    PairedHanchanCluster,
    build_clusters,
    classify_regression,
    compare_against_control,
    paired_hanchan_bootstrap,
    pooled_delta,
)
from lisjong_arena.stage3_mix_pilot.experiment import (
    MixExperimentError,
    validate_mix_dataset,
)
from lisjong_arena.stage3_mix_pilot.generation import (
    MANIFEST_FILENAME,
    MixGenerationError,
    load_population,
    load_population_manifest,
    validate_population_manifest,
)
from lisjong_arena.stage3_mix_pilot.population import (
    MixPopulationError,
    coverage_seat_index,
    mix_arm_plan,
    mix_arm_plans,
)
from lisjong_arena.stage3_mix_pilot.protocol import (
    ARM_IDS,
    AUGMENTATION_IDENTITY,
    AUGMENTATION_REFERENCE,
    AUGMENTATION_SLOTS_BY_ARM,
    CLEAR_REGRESSION,
    CONTRACT_VIOLATION,
    CONTROL_ARM_ID,
    COVERAGE_INSUFFICIENT,
    INCONCLUSIVE,
    MIX_LOCKED_LOW,
    MIX_LOCKED_MEDIUM,
    NO_CLEAR_REGRESSION,
    OBSERVED,
    OPPORTUNITY_OBSERVED,
    ORDERED_SEEDS,
    OUTCOMES,
    PILOT_HANCHAN_PER_ARM,
    PRIMARY_IDENTITY,
    PRIMARY_REFERENCE,
    QUALITY_TRADEOFF,
    SEAT_SLOTS_PER_ARM,
    SPLIT_POLICY,
    STOP_INVALID,
    TRAIN_SEEDS,
    UNMEASURED,
    VALIDATION_SEEDS,
)
from lisjong_arena.stage3_mix_pilot.result import (
    MixResultError,
    classify,
    coverage_accounting,
    hard_validity,
    kind_interpretation,
    regression_status,
    selected_recipe,
)


class SeedAndSplitTest(unittest.TestCase):
    def test_ordered_seeds_are_the_locked_fresh_contiguous_range(self):
        self.assertEqual(ORDERED_SEEDS, tuple(range(330, 354)))
        self.assertEqual(len(ORDERED_SEEDS), 24)
        self.assertEqual(PILOT_HANCHAN_PER_ARM, 24)
        self.assertEqual(SEAT_SLOTS_PER_ARM, 96)

    def test_train_and_validation_partitions_are_exact(self):
        self.assertEqual(TRAIN_SEEDS, tuple(range(330, 348)))
        self.assertEqual(VALIDATION_SEEDS, tuple(range(348, 354)))
        self.assertEqual(len(TRAIN_SEEDS), 18)
        self.assertEqual(len(VALIDATION_SEEDS), 6)
        self.assertEqual(TRAIN_SEEDS + VALIDATION_SEEDS, ORDERED_SEEDS)

    def test_the_pilot_population_has_no_test_partition(self):
        for seed in ORDERED_SEEDS:
            partition = partition_for_first_party_game(
                FIRST_PARTY_SOURCE_CLASS,
                seed,
                FirstPartySplitPolicy.MIX_PILOT_DEVELOPMENT,
            )
            self.assertIn(
                partition, (DatasetPartition.TRAIN, DatasetPartition.VALIDATION)
            )

    def test_split_policy_rejects_seeds_outside_the_locked_population(self):
        for seed in (329, 354, 180, 306):
            with self.assertRaises(ValueError):
                partition_for_first_party_game(
                    FIRST_PARTY_SOURCE_CLASS,
                    seed,
                    FirstPartySplitPolicy.MIX_PILOT_DEVELOPMENT,
                )

    def test_no_historical_population_seed_is_reused(self):
        historical = (
            set(QUANTITATIVE_SEEDS)
            | set(HOLDOUT_SEEDS)
            | set(STAGE3_DEVELOPMENT_SEEDS)
            | set(KAN_COVERAGE_DEVELOPMENT_SEEDS)
        )
        self.assertFalse(historical.intersection(MIX_PILOT_DEVELOPMENT_SEEDS))

    def test_split_module_exposes_the_locked_mix_pilot_seeds(self):
        self.assertEqual(MIX_PILOT_TRAIN_SEEDS, TRAIN_SEEDS)
        self.assertEqual(MIX_PILOT_VALIDATION_SEEDS, VALIDATION_SEEDS)
        self.assertEqual(MIX_PILOT_DEVELOPMENT_SEEDS, ORDERED_SEEDS)

    def test_all_three_arms_share_the_same_ordered_seeds_intentionally(self):
        plans = mix_arm_plans()
        for plan in plans:
            self.assertEqual(
                tuple(value.game_seed for value in plan.assignments), ORDERED_SEEDS
            )
            self.assertEqual(plan.train_seeds, TRAIN_SEEDS)
            self.assertEqual(plan.validation_seeds, VALIDATION_SEEDS)

    def test_arms_are_distinguished_by_construction_not_by_seeds(self):
        identities = {plan.population_identity for plan in mix_arm_plans()}
        self.assertEqual(len(identities), len(ARM_IDS))


class PopulationConstructionTest(unittest.TestCase):
    def test_exact_augmentation_seat_slot_fractions(self):
        expected = {"A": (0, 0.0), "B": (12, 0.125), "C": (24, 0.25)}
        for plan in mix_arm_plans():
            slots, fraction = expected[plan.arm_id]
            self.assertEqual(plan.augmentation_slot_count, slots)
            self.assertEqual(plan.augmentation_seat_slot_fraction, fraction)
            self.assertEqual(slots, AUGMENTATION_SLOTS_BY_ARM[plan.arm_id])

    def test_control_arm_is_pure_primary_source(self):
        plan = mix_arm_plan("A")
        for assignment in plan.assignments:
            self.assertEqual(assignment.seat_identities, (PRIMARY_IDENTITY,) * 4)

    def test_low_augmentation_places_one_coverage_seat_in_half_the_hanchan(self):
        plan = mix_arm_plan("B")
        augmented = [
            value
            for value in plan.assignments
            if AUGMENTATION_IDENTITY in value.seat_identities
        ]
        self.assertEqual(len(augmented), 12)
        for assignment in augmented:
            self.assertEqual(assignment.seat_identities.count(AUGMENTATION_IDENTITY), 1)
        self.assertEqual(
            [value.game_seed for value in augmented],
            [seed for seed in ORDERED_SEEDS if (seed - ORDERED_SEEDS[0]) % 2 == 0],
        )

    def test_medium_augmentation_places_one_coverage_seat_in_every_hanchan(self):
        plan = mix_arm_plan("C")
        for assignment in plan.assignments:
            self.assertEqual(assignment.seat_identities.count(AUGMENTATION_IDENTITY), 1)
            self.assertEqual(assignment.seat_identities.count(PRIMARY_IDENTITY), 3)

    def test_coverage_actor_seat_is_exactly_balanced(self):
        self.assertEqual(mix_arm_plan("A").coverage_seat_occupancy(), (0, 0, 0, 0))
        self.assertEqual(mix_arm_plan("B").coverage_seat_occupancy(), (3, 3, 3, 3))
        self.assertEqual(mix_arm_plan("C").coverage_seat_occupancy(), (6, 6, 6, 6))
        for plan in mix_arm_plans():
            self.assertTrue(plan.is_coverage_seat_balanced)

    def test_seat_assignment_is_deterministic_and_prng_free(self):
        for arm_id in ARM_IDS:
            first = mix_arm_plan(arm_id)
            second = mix_arm_plan(arm_id)
            self.assertEqual(first.plan_value(), second.plan_value())
            self.assertEqual(first.population_identity, second.population_identity)

    def test_coverage_seat_index_rule_is_explicit(self):
        self.assertIsNone(coverage_seat_index("A", 0))
        self.assertEqual(coverage_seat_index("B", 0), 0)
        self.assertIsNone(coverage_seat_index("B", 1))
        self.assertEqual(coverage_seat_index("B", 2), 1)
        self.assertEqual(coverage_seat_index("C", 0), 0)
        self.assertEqual(coverage_seat_index("C", 5), 1)

    def test_coverage_seat_index_rejects_unknown_arm_and_index(self):
        with self.assertRaises(MixPopulationError):
            coverage_seat_index("D", 0)
        with self.assertRaises(MixPopulationError):
            coverage_seat_index("B", 24)

    def test_policy_references_resolve_to_fresh_instances_per_game_and_seat(self):
        plan = mix_arm_plan("C")
        factories = plan.seat_policy_factories_by_seed()
        self.assertEqual(tuple(factories), ORDERED_SEEDS)
        instances = [
            factory()
            for seat_factories in factories.values()
            for factory in seat_factories.values()
        ]
        self.assertEqual(len({id(value) for value in instances}), len(instances))

    def test_the_coverage_source_uses_the_locked_explicit_import_reference(self):
        plan = mix_arm_plan("B")
        coverage = next(
            value for value in plan.policies if value.identity == AUGMENTATION_IDENTITY
        )
        self.assertEqual(coverage.reference, AUGMENTATION_REFERENCE)
        instance = coverage.factory()()
        self.assertEqual(type(instance).__name__, "KanCoverageYakuhaiCallPolicy")

    def test_the_primary_source_uses_the_curated_catalog_alias(self):
        plan = mix_arm_plan("B")
        primary = next(
            value for value in plan.policies if value.identity == PRIMARY_IDENTITY
        )
        self.assertEqual(primary.reference, PRIMARY_REFERENCE)
        self.assertTrue(callable(primary.factory()))

    def test_plan_value_records_the_augmentation_semantics(self):
        plan = mix_arm_plan("B")
        value = plan.plan_value()
        self.assertEqual(value["augmentation_seat_slots"], 12)
        self.assertEqual(value["seat_slots"], SEAT_SLOTS_PER_ARM)
        self.assertEqual(value["augmented_hanchan"], 12)
        self.assertTrue(value["coverage_seat_balanced"])
        self.assertIs(value["test_partition_present"], False)
        self.assertEqual(value["split_policy_id"], SPLIT_POLICY.value)

    def test_population_identity_covers_the_seat_assignment(self):
        """同じarm / 同じseeds / 同じslot数でも、座る席が違えばidentityは違う。"""
        plan = mix_arm_plan("C")
        shifted = replace(
            plan,
            assignments=tuple(
                replace(
                    value,
                    seat_identities=tuple(
                        AUGMENTATION_IDENTITY
                        if seat_index == (index + 1) % 4
                        else PRIMARY_IDENTITY
                        for seat_index in range(4)
                    ),
                )
                for index, value in enumerate(plan.assignments)
            ),
        )
        self.assertEqual(shifted.augmentation_slot_count, 24)
        self.assertTrue(shifted.is_coverage_seat_balanced)
        self.assertNotEqual(shifted.population_identity, plan.population_identity)

    def test_unbalanced_or_wrong_sized_augmentation_is_rejected(self):
        plan = mix_arm_plan("C")
        collapsed = tuple(
            replace(
                value,
                seat_identities=(
                    AUGMENTATION_IDENTITY,
                    PRIMARY_IDENTITY,
                    PRIMARY_IDENTITY,
                    PRIMARY_IDENTITY,
                ),
            )
            for value in plan.assignments
        )
        with self.assertRaises(MixPopulationError):
            replace(plan, assignments=collapsed)

    def test_two_coverage_seats_in_one_hanchan_are_rejected(self):
        plan = mix_arm_plan("C")
        doubled = list(plan.assignments)
        doubled[0] = replace(
            doubled[0],
            seat_identities=(
                AUGMENTATION_IDENTITY,
                AUGMENTATION_IDENTITY,
                PRIMARY_IDENTITY,
                PRIMARY_IDENTITY,
            ),
        )
        with self.assertRaises(MixPopulationError):
            replace(plan, assignments=tuple(doubled))

    def test_unknown_arm_is_rejected(self):
        with self.assertRaises(MixPopulationError):
            mix_arm_plan("D")


class HistoricalIsolationTest(unittest.TestCase):
    def test_stage3_entry_gate_population_identities_are_unchanged(self):
        self.assertEqual(
            population_a_plan().plan_value()["ordered_seeds"], list(range(180, 192))
        )
        for plan in (population_a_plan(), population_b_plan(), population_c_plan()):
            self.assertEqual(plan.plan_value()["pilot_role"], "development-only")
            self.assertNotIn(330, plan.plan_value()["ordered_seeds"])

    def test_kan_coverage_population_identity_is_unchanged(self):
        plan = kan_coverage_population_plan()
        self.assertEqual(plan.ordered_seeds, tuple(range(306, 330)))
        self.assertEqual(plan.population_id, "kan-coverage")

    def test_mix_pilot_identities_differ_from_every_historical_identity(self):
        historical = {
            population_a_plan().population_identity,
            population_b_plan().population_identity,
            population_c_plan().population_identity,
            kan_coverage_population_plan().population_identity,
        }
        for plan in mix_arm_plans():
            self.assertNotIn(plan.population_identity, historical)


def _record(seed: int, seat, *, selected: str | None, eligible: bool = True):
    from lisjong_arena.stage3_kan_coverage.opportunity import action_descriptor

    descriptor = None if selected is None else canonical_json_bytes({"kind": selected})
    return KanDecisionRecord(
        game_seed=seed,
        viewer_seat=seat,
        decision_index=0,
        winning_action_legal=not eligible,
        winning_kinds=() if eligible else ("ron",),
        candidates=(canonical_json_bytes(action_descriptor(object())),),
        candidate_kinds=("ankan",),
        selected_kind=selected,
        selected_descriptor=descriptor,
        policy_input=None,
    )


class SourceAttributionTest(unittest.TestCase):
    def setUp(self):
        from lisjong_engine.seat import Seat

        self.seats = tuple(Seat)
        self.slots = frozenset({(330, self.seats[0])})
        self.diagnostic = KanOpportunityDiagnostic(
            records=(
                _record(330, self.seats[0], selected="ankan"),
                _record(330, self.seats[1], selected=None),
            ),
            decision_counts=tuple((330, seat.value, 5) for seat in self.seats),
            passes_per_seat=(2,),
        )

    def test_restriction_keeps_only_the_declared_seats(self):
        restricted = restrict_diagnostic(self.diagnostic, self.slots)
        self.assertEqual(len(restricted.records), 1)
        self.assertEqual(restricted.records[0].viewer_seat, self.seats[0])
        self.assertEqual(len(restricted.decision_counts), 1)
        self.assertEqual(restricted.total_decisions, 5)

    def test_complement_is_every_other_observed_seat(self):
        primary = complement_slots(self.diagnostic, self.slots)
        self.assertEqual(len(primary), 3)
        self.assertNotIn((330, self.seats[0]), primary)

    def test_primary_decisions_never_count_as_contract_violations(self):
        attribution = attribute_sources("B", self.diagnostic, (), self.slots)
        self.assertEqual(
            attribution.coverage_diagnostic.selection_contract_violations, ()
        )
        # primary seat declined a legal kan, which is expected behaviour.
        self.assertEqual(
            len(attribution.primary_diagnostic.selection_contract_violations), 1
        )
        summary = primary_source_summary(attribution.primary_diagnostic)
        self.assertNotIn("selection_contract_violations", summary)
        self.assertIn("descriptive only", summary["contract_role"])

    def test_a_seat_the_observer_never_saw_fails_closed(self):
        with self.assertRaises(MixAttributionError):
            restrict_diagnostic(self.diagnostic, frozenset({(999, self.seats[0])}))

    def test_control_arm_restriction_is_empty_but_well_formed(self):
        restricted = restrict_diagnostic(self.diagnostic, frozenset())
        self.assertEqual(restricted.records, ())
        self.assertEqual(restricted.total_decisions, 0)
        value = restricted.diagnostic_value()
        self.assertEqual(value["selection_contract_violations"], 0)
        for kind in ("daiminkan", "ankan", "kakan"):
            self.assertEqual(value["by_kind"][kind]["eligible_no_win_opportunities"], 0)

    def test_accounts_are_restricted_by_seat(self):
        from lisjong_arena.stage3_kan_coverage.accounting import SelectedKanAccount

        accounts = tuple(
            SelectedKanAccount(
                game_seed=330,
                viewer_seat=seat,
                decision_index=0,
                round_index=0,
                checkpoint_index=0,
                kind="ankan",
                outcome="confirmed",
                detail="",
                rinshan_expected=True,
                rinshan_observed=True,
            )
            for seat in self.seats
        )
        self.assertEqual(len(restrict_accounts(accounts, self.slots)), 1)


class DatasetCompatibilityTest(unittest.TestCase):
    def test_the_locked_population_materializes_a_whole_hanchan_split(self):
        with tempfile.TemporaryDirectory() as name:
            _persisted_raw, dataset = mix_artifacts(Path(name))
            seeds = tuple(value.game.game_seed for value in dataset.games)
            self.assertEqual(seeds, ORDERED_SEEDS)
            train = tuple(
                value.game.game_seed
                for value in dataset.games
                if value.partition is DatasetPartition.TRAIN
            )
            validation = tuple(
                value.game.game_seed
                for value in dataset.games
                if value.partition is DatasetPartition.VALIDATION
            )
            self.assertEqual(train, TRAIN_SEEDS)
            self.assertEqual(validation, VALIDATION_SEEDS)
            self.assertFalse(
                [
                    value
                    for value in dataset.games
                    if value.partition is DatasetPartition.TEST
                ]
            )

    def test_kan_containing_games_are_retained_by_materialization(self):
        from lisjong_arena.stage3_kan_coverage.generation import (
            dataset_retention_value,
            kan_event_inventory,
        )

        with tempfile.TemporaryDirectory() as name:
            persisted_raw, dataset = mix_artifacts(Path(name), kan=True)
            inventory = kan_event_inventory(persisted_raw.corpus)
            self.assertTrue(inventory)
            retention = dataset_retention_value(dataset, inventory)
            self.assertEqual(retention["kan_containing_games_dropped"], 0)
            self.assertEqual(
                retention["kan_containing_games_retained"], len(ORDERED_SEEDS)
            )

    def test_the_mix_dataset_validator_rejects_a_foreign_split(self):
        from _stage3_kan_coverage_fixtures import kan_coverage_artifacts

        with tempfile.TemporaryDirectory() as name:
            _persisted_raw, dataset = kan_coverage_artifacts(Path(name))
            with self.assertRaises(MixExperimentError):
                validate_mix_dataset(dataset)

    def test_the_mix_dataset_validator_accepts_the_locked_population(self):
        with tempfile.TemporaryDirectory() as name:
            _persisted_raw, dataset = mix_artifacts(Path(name))
            validate_mix_dataset(dataset)

    def test_the_corpus_covers_the_locked_hanchan_count(self):
        self.assertEqual(len(mix_corpus().games), PILOT_HANCHAN_PER_ARM)


class DistributionEffectTest(unittest.TestCase):
    """measurement C — armごとのdistribution statisticsを実artifactから測る。"""

    def _measure(self, arm_id: str, *, kan: bool):
        from lisjong_arena.phase5_belief_dataset.builder import (
            resolve_training_samples,
        )
        from lisjong_arena.phase8_sequential.data import (
            materialize_development_examples,
        )
        from lisjong_arena.stage3_entry_gate.coverage import (
            measure_population_coverage,
        )
        from lisjong_arena.stage3_kan_coverage.generation import kan_event_inventory
        from lisjong_arena.stage3_mix_pilot.generation import distribution_value

        with tempfile.TemporaryDirectory() as name:
            persisted_raw, dataset = mix_artifacts(Path(name), kan=kan)
            samples = resolve_training_samples(dataset, persisted_raw)
            examples = materialize_development_examples(dataset.examples, samples)
            coverage = measure_population_coverage(persisted_raw.corpus, examples)
            inventory = kan_event_inventory(persisted_raw.corpus)
            return distribution_value(mix_arm_plan(arm_id), coverage, inventory, ())

    def test_the_reported_seat_slot_fraction_is_the_locked_arm_fraction(self):
        for arm_id, fraction in (("A", 0.0), ("B", 0.125), ("C", 0.25)):
            value = self._measure(arm_id, kan=True)
            self.assertEqual(value["coverage_source_seat_slot_fraction"], fraction)
            self.assertEqual(
                value["coverage_source_seat_slots"],
                AUGMENTATION_SLOTS_BY_ARM[arm_id],
            )
            self.assertEqual(value["seat_slots"], SEAT_SLOTS_PER_ARM)

    def test_a_kan_free_corpus_reports_zero_kan_strata_without_fabrication(self):
        value = self._measure("A", kan=False)
        self.assertEqual(value["kan_containing_hanchan"], 0)
        self.assertEqual(value["kan_containing_hanchan_fraction"], 0.0)
        self.assertEqual(value["kan_containing_rounds"], 0)
        self.assertEqual(value["ankan_per_hanchan"], 0.0)

    def test_a_kan_bearing_corpus_reports_the_kan_strata(self):
        value = self._measure("C", kan=True)
        self.assertEqual(value["kan_containing_hanchan"], PILOT_HANCHAN_PER_ARM)
        self.assertEqual(value["kan_containing_hanchan_fraction"], 1.0)
        self.assertGreater(value["ankan_per_hanchan"], 0.0)

    def test_distribution_reports_the_required_anchor_ratios(self):
        value = self._measure("B", kan=True)
        for name in (
            "anchors_per_hanchan",
            "open_row_ratio",
            "closed_row_ratio",
            "call_related_anchor_ratio",
            "riichi_related_anchor_ratio",
            "rinshan_draw_per_hanchan",
            "confirmed_kan_per_hanchan",
        ):
            self.assertIn(name, value)
        self.assertAlmostEqual(
            value["open_row_ratio"] + value["closed_row_ratio"], 1.0, places=12
        )

    def test_the_estimate_is_labelled_descriptive(self):
        value = self._measure("B", kan=True)
        self.assertIn("descriptive", value["estimate_role"])
        self.assertIn("not a claim about the true kan rate", value["estimate_role"])


class SeatSlotAccountingTest(unittest.TestCase):
    """coverage slotとprimary slotが96 seat slotsをexactに分割することを固定する。"""

    def test_every_seat_slot_is_attributed_to_exactly_one_source(self):
        for plan in mix_arm_plans():
            coverage = plan.coverage_slots()
            self.assertEqual(len(coverage), AUGMENTATION_SLOTS_BY_ARM[plan.arm_id])
            primary = SEAT_SLOTS_PER_ARM - len(coverage)
            self.assertEqual(
                primary,
                sum(
                    1
                    for value in plan.assignments
                    for identity in value.seat_identities
                    if identity == PRIMARY_IDENTITY
                ),
            )

    def test_coverage_slots_name_real_seeds_and_seats(self):
        from lisjong_engine.seat import Seat

        plan = mix_arm_plan("B")
        for seed, seat in plan.coverage_slots():
            self.assertIn(seed, ORDERED_SEEDS)
            self.assertIsInstance(seat, Seat)


class PopulationManifestTest(unittest.TestCase):
    def test_a_well_formed_manifest_validates(self):
        for arm_id in ARM_IDS:
            validate_population_manifest(arm_manifest_value(arm_id))

    def test_a_tampered_population_plan_is_rejected(self):
        manifest = arm_manifest_value("B")
        manifest["population_plan"]["augmentation_seat_slots"] = 24
        with self.assertRaises(MixGenerationError):
            validate_population_manifest(manifest)

    def test_an_identity_that_is_not_the_plan_hash_is_rejected(self):
        manifest = arm_manifest_value("C")
        manifest["population_identity"] = "0" * 64
        with self.assertRaises(MixGenerationError):
            validate_population_manifest(manifest)

    def test_unresolved_source_revisions_are_rejected(self):
        with self.assertRaises(MixGenerationError):
            validate_population_manifest(arm_manifest_value("A", fully_resolved=False))

    def test_a_wrong_hanchan_count_is_rejected(self):
        with self.assertRaises(MixGenerationError):
            validate_population_manifest(arm_manifest_value("A", hanchan=12))

    def test_a_declared_test_partition_is_rejected(self):
        manifest = arm_manifest_value("A")
        manifest["test_partition_present"] = True
        with self.assertRaises(MixGenerationError):
            validate_population_manifest(manifest)

    def test_mismatched_attributed_seat_slots_are_rejected(self):
        manifest = arm_manifest_value("B")
        manifest["source_attribution"]["coverage_source"]["seat_slots"] = 24
        with self.assertRaises(MixGenerationError):
            validate_population_manifest(manifest)

    def test_a_manifest_from_another_arm_id_is_rejected(self):
        manifest = arm_manifest_value("B")
        manifest["arm_id"] = "C"
        with self.assertRaises(MixGenerationError):
            validate_population_manifest(manifest)

    def test_non_canonical_manifest_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / MANIFEST_FILENAME).write_text(
                json.dumps(arm_manifest_value("A"), indent=2)
            )
            with self.assertRaises(MixGenerationError):
                load_population_manifest(directory)

    def test_load_population_requires_the_manifest(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(FileNotFoundError):
                load_population(Path(name))


class PairedComparisonTest(unittest.TestCase):
    def _clusters(self, control: float, candidate: float):
        return tuple(
            PairedHanchanCluster(
                game_seed=seed, weight=40, control_mae=control, candidate_mae=candidate
            )
            for seed in VALIDATION_SEEDS
        )

    def test_delta_is_control_minus_candidate(self):
        clusters = self._clusters(0.42, 0.40)
        self.assertAlmostEqual(pooled_delta(clusters), 0.02, places=12)
        self.assertAlmostEqual(clusters[0].delta_mae, 0.02, places=12)

    def test_pooling_is_weighted_by_anchor_count(self):
        clusters = (
            PairedHanchanCluster(
                game_seed=VALIDATION_SEEDS[0],
                weight=90,
                control_mae=0.5,
                candidate_mae=0.5,
            ),
            PairedHanchanCluster(
                game_seed=VALIDATION_SEEDS[1],
                weight=10,
                control_mae=0.5,
                candidate_mae=0.0,
            ),
        )
        self.assertAlmostEqual(pooled_delta(clusters), 0.05, places=12)

    def test_the_bootstrap_is_deterministic(self):
        clusters = self._clusters(0.42, 0.40)
        self.assertEqual(
            paired_hanchan_bootstrap(clusters), paired_hanchan_bootstrap(clusters)
        )

    def test_classification_is_exhaustive_and_threshold_exact(self):
        self.assertEqual(classify_regression((-0.03, -0.01)), CLEAR_REGRESSION)
        self.assertEqual(classify_regression((-0.03, 0.0)), NO_CLEAR_REGRESSION)
        self.assertEqual(classify_regression((-0.03, 0.01)), NO_CLEAR_REGRESSION)
        self.assertEqual(classify_regression((0.01, 0.02)), NO_CLEAR_REGRESSION)

    def test_a_uniformly_worse_candidate_is_a_clear_regression(self):
        control = evaluation_cell_value("A", "A", mae=0.40)
        candidate = evaluation_cell_value("B", "A", mae=0.50)
        row = compare_against_control(
            candidate_arm_id="B",
            validation_arm_id="A",
            control_cell=control,
            candidate_cell=candidate,
        )
        self.assertEqual(row["classification"], CLEAR_REGRESSION)
        self.assertLess(row["interval_upper"], 0)
        self.assertAlmostEqual(row["pooled_delta_mae"], -0.10, places=12)

    def test_a_uniformly_better_candidate_is_not_a_regression(self):
        control = evaluation_cell_value("A", "A", mae=0.50)
        candidate = evaluation_cell_value("B", "A", mae=0.40)
        row = compare_against_control(
            candidate_arm_id="B",
            validation_arm_id="A",
            control_cell=control,
            candidate_cell=candidate,
        )
        self.assertEqual(row["classification"], NO_CLEAR_REGRESSION)
        self.assertEqual(row["positive_hanchan_count"], len(VALIDATION_SEEDS))

    def test_cells_from_different_evaluation_populations_fail_closed(self):
        with self.assertRaises(MixComparisonError):
            build_clusters(
                evaluation_cell_value("A", "A"), evaluation_cell_value("B", "B")
            )

    def test_mismatched_anchor_counts_fail_closed(self):
        control = evaluation_cell_value("A", "A")
        candidate = evaluation_cell_value("B", "A", anchors=41)
        with self.assertRaises(MixComparisonError):
            build_clusters(control, candidate)

    def test_a_foreign_validation_hanchan_population_fails_closed(self):
        control = evaluation_cell_value("A", "A")
        candidate = evaluation_cell_value("B", "A")
        control["per_game"] = control["per_game"][:-1]
        candidate["per_game"] = candidate["per_game"][:-1]
        with self.assertRaises(MixComparisonError):
            build_clusters(control, candidate)

    def test_the_control_arm_is_never_compared_against_itself(self):
        with self.assertRaises(MixComparisonError):
            compare_against_control(
                candidate_arm_id=CONTROL_ARM_ID,
                validation_arm_id="A",
                control_cell=evaluation_cell_value("A", "A"),
                candidate_cell=evaluation_cell_value("A", "A"),
            )

    def test_a_non_control_baseline_cell_fails_closed(self):
        with self.assertRaises(MixComparisonError):
            compare_against_control(
                candidate_arm_id="C",
                validation_arm_id="A",
                control_cell=evaluation_cell_value("B", "A"),
                candidate_cell=evaluation_cell_value("C", "A"),
            )


class KindInterpretationTest(unittest.TestCase):
    def test_zero_opportunity_is_unmeasured_not_a_failure(self):
        diagnostic = opportunity_diagnostic_value(kakan=(0, 0))
        self.assertEqual(kind_interpretation(diagnostic, "kakan"), UNMEASURED)

    def test_a_selected_kind_is_observed(self):
        diagnostic = opportunity_diagnostic_value(ankan=(5, 5))
        self.assertEqual(kind_interpretation(diagnostic, "ankan"), OBSERVED)

    def test_an_unselected_but_available_kind_is_not_a_violation(self):
        diagnostic = opportunity_diagnostic_value(daiminkan=(6, 0))
        self.assertEqual(
            kind_interpretation(diagnostic, "daiminkan"), OPPORTUNITY_OBSERVED
        )

    def test_an_eligible_decision_without_any_kan_selection_is_a_violation(self):
        diagnostic = opportunity_diagnostic_value(
            daiminkan=(6, 0), unconverted={"daiminkan": 1}
        )
        self.assertEqual(
            kind_interpretation(diagnostic, "daiminkan"), CONTRACT_VIOLATION
        )


class ClassificationTest(unittest.TestCase):
    def test_a_healthy_pilot_locks_the_lowest_augmentation_fraction(self):
        outcome, reasons, gates = classify(
            arm_manifests(), matrix_cells(), comparison_rows()
        )
        self.assertEqual(outcome, MIX_LOCKED_LOW)
        self.assertTrue(reasons)
        self.assertTrue(gates["hard_validity"]["A"]["passed"])
        self.assertTrue(gates["coverage_source_accounting"]["B"]["passed"])

    def test_arm_c_is_locked_only_when_arm_b_is_not_eligible(self):
        manifests = arm_manifests(
            B=arm_manifest_value(
                "B",
                diagnostic=opportunity_diagnostic_value(
                    daiminkan=(0, 0), ankan=(0, 0), kakan=(0, 0)
                ),
                totals=accounting_totals(selected=0, confirmed=0),
            )
        )
        outcome, _reasons, _gates = classify(
            manifests, matrix_cells(), comparison_rows()
        )
        self.assertEqual(outcome, MIX_LOCKED_MEDIUM)

    def test_no_coverage_anywhere_is_coverage_insufficient(self):
        empty = opportunity_diagnostic_value(
            daiminkan=(0, 0), ankan=(0, 0), kakan=(0, 0)
        )
        manifests = arm_manifests(
            B=arm_manifest_value(
                "B", diagnostic=empty, totals=accounting_totals(selected=0, confirmed=0)
            ),
            C=arm_manifest_value(
                "C", diagnostic=empty, totals=accounting_totals(selected=0, confirmed=0)
            ),
        )
        outcome, reasons, _gates = classify(
            manifests, matrix_cells(), comparison_rows()
        )
        self.assertEqual(outcome, COVERAGE_INSUFFICIENT)
        self.assertTrue(reasons)

    def test_both_candidates_regressing_is_a_quality_tradeoff(self):
        outcome, _reasons, _gates = classify(
            arm_manifests(), matrix_cells(), comparison_rows(regressed=("B", "C"))
        )
        self.assertEqual(outcome, QUALITY_TRADEOFF)

    def test_one_regressing_candidate_still_locks_the_other(self):
        outcome, _reasons, _gates = classify(
            arm_manifests(), matrix_cells(), comparison_rows(regressed=("B",))
        )
        self.assertEqual(outcome, MIX_LOCKED_MEDIUM)

    def test_a_regression_on_a_single_evaluation_population_is_enough(self):
        rows = comparison_rows()
        rows = [
            comparison_row_value("B", "C", pooled_delta=-0.02, lower=-0.03, upper=-0.01)
            if row["candidate_arm_id"] == "B" and row["validation_population_id"] == "C"
            else row
            for row in rows
        ]
        outcome, _reasons, _gates = classify(arm_manifests(), matrix_cells(), rows)
        self.assertEqual(outcome, MIX_LOCKED_MEDIUM)

    def test_the_remaining_combination_is_inconclusive(self):
        empty = opportunity_diagnostic_value(
            daiminkan=(0, 0), ankan=(0, 0), kakan=(0, 0)
        )
        manifests = arm_manifests(
            C=arm_manifest_value(
                "C", diagnostic=empty, totals=accounting_totals(selected=0, confirmed=0)
            )
        )
        outcome, _reasons, _gates = classify(
            manifests, matrix_cells(), comparison_rows(regressed=("B",))
        )
        self.assertEqual(outcome, INCONCLUSIVE)

    def test_unresolved_provenance_stops_the_pilot(self):
        manifests = arm_manifests(A=arm_manifest_value("A", fully_resolved=False))
        outcome, reasons, _gates = classify(
            manifests, matrix_cells(), comparison_rows()
        )
        self.assertEqual(outcome, STOP_INVALID)
        self.assertTrue(any("fully resolved" in reason for reason in reasons))

    def test_a_dropped_kan_containing_game_stops_the_pilot(self):
        manifests = arm_manifests(C=arm_manifest_value("C", dropped_kan_games=1))
        outcome, _reasons, _gates = classify(
            manifests, matrix_cells(), comparison_rows()
        )
        self.assertEqual(outcome, STOP_INVALID)

    def test_a_failed_physical_gate_stops_the_pilot(self):
        cells = [
            evaluation_cell_value(
                training_id,
                validation_id,
                physical_passed=not (training_id == "B" and validation_id == "C"),
            )
            for training_id in ARM_IDS
            for validation_id in ARM_IDS
        ]
        outcome, _reasons, _gates = classify(arm_manifests(), cells, comparison_rows())
        self.assertEqual(outcome, STOP_INVALID)

    def test_a_non_finite_model_output_stops_the_pilot(self):
        cells = matrix_cells()
        cells[0]["sequential_validation_mae"] = float("nan")
        outcome, _reasons, _gates = classify(arm_manifests(), cells, comparison_rows())
        self.assertEqual(outcome, STOP_INVALID)

    def test_a_contract_violation_blocks_the_coverage_gate(self):
        manifests = arm_manifests(
            B=arm_manifest_value(
                "B",
                diagnostic=opportunity_diagnostic_value(
                    violations=1, unconverted={"ankan": 1}
                ),
            )
        )
        gate = coverage_accounting(manifests["B"])
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["kind_interpretation"]["ankan"], CONTRACT_VIOLATION)

    def test_unaccounted_and_missing_rinshan_block_the_coverage_gate(self):
        for totals in (
            accounting_totals(unaccounted=1),
            accounting_totals(rinshan_missing=1),
        ):
            gate = coverage_accounting(arm_manifest_value("C", totals=totals))
            self.assertFalse(gate["passed"])

    def test_every_outcome_this_module_can_return_is_exhaustive(self):
        produced = {
            classify(*arguments)[0]
            for arguments in (
                (arm_manifests(), matrix_cells(), comparison_rows()),
                (
                    arm_manifests(),
                    matrix_cells(),
                    comparison_rows(regressed=("B", "C")),
                ),
                (
                    arm_manifests(A=arm_manifest_value("A", fully_resolved=False)),
                    matrix_cells(),
                    comparison_rows(),
                ),
            )
        }
        self.assertTrue(produced.issubset(set(OUTCOMES)))

    def test_classification_requires_every_arm(self):
        manifests = arm_manifests()
        del manifests["C"]
        with self.assertRaises(MixResultError):
            classify(manifests, matrix_cells(), comparison_rows())

    def test_regression_status_requires_every_evaluation_population(self):
        rows = [
            row for row in comparison_rows() if row["validation_population_id"] != "C"
        ]
        with self.assertRaises(MixResultError):
            regression_status("B", rows)

    def test_hard_validity_reports_the_measured_gates(self):
        gate = hard_validity(arm_manifest_value("B"), matrix_cells())
        self.assertTrue(gate["passed"])
        self.assertTrue(gate["runtime_measured"])
        self.assertTrue(gate["storage_measured"])
        self.assertTrue(gate["physical_validity_passed"])
        self.assertEqual(gate["hanchan_generated"], PILOT_HANCHAN_PER_ARM)


def _scalars(value):
    """dict / listを再帰的に降りてscalarだけを列挙する。"""
    if isinstance(value, dict):
        for item in value.values():
            yield from _scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from _scalars(item)
    else:
        yield value


class SelectedRecipeTest(unittest.TestCase):
    def test_no_recipe_is_locked_for_a_reformulate_outcome(self):
        self.assertIsNone(selected_recipe(INCONCLUSIVE, arm_manifests()))
        self.assertIsNone(selected_recipe(STOP_INVALID, arm_manifests()))

    def test_the_recipe_locks_sources_and_fraction(self):
        recipe = selected_recipe(MIX_LOCKED_LOW, arm_manifests())
        self.assertEqual(recipe["selected_arm_id"], "B")
        self.assertEqual(recipe["augmentation_seat_slot_fraction"], 0.125)
        self.assertEqual(recipe["primary_source"]["identity"], PRIMARY_IDENTITY)
        self.assertEqual(
            recipe["augmentation_source"]["identity"], AUGMENTATION_IDENTITY
        )
        self.assertIs(recipe["development_seeds_reused_for_phase10"], False)

    def test_the_recipe_carries_no_pilot_seed_bound_identity(self):
        """recipeはPhase 10へ渡すものなので、pilot seedsをどこにも残さない。

        `ordered_seeds`のようなkeyが無いことだけでは足りない。split policyの
        **value** は`first-party-seeds-330-353-...`であり、名前そのものがpilot
        seed rangeへbindされている。recipeのscalarを再帰的に走査して、seed値も
        seed-bound identifierも残っていないことを固定する。
        """
        for outcome in (MIX_LOCKED_LOW, MIX_LOCKED_MEDIUM):
            recipe = selected_recipe(outcome, arm_manifests())
            with self.subTest(outcome=outcome):
                self.assertNotIn("ordered_seeds", recipe)
                self.assertNotIn("split_policy_id", recipe)
                for scalar in _scalars(recipe):
                    if isinstance(scalar, bool):
                        continue
                    if isinstance(scalar, int):
                        self.assertNotIn(scalar, ORDERED_SEEDS)
                    if isinstance(scalar, str):
                        self.assertNotIn(SPLIT_POLICY.value, scalar)
                        self.assertNotIn("330-353", scalar)
                        self.assertNotIn("330..353", scalar)

    def test_the_recipe_keeps_seed_independent_split_semantics(self):
        recipe = selected_recipe(MIX_LOCKED_LOW, arm_manifests())
        self.assertEqual(
            recipe["split_semantics"],
            {
                "unit": "whole hanchan",
                "partitions": ["TRAIN", "VALIDATION"],
                "test_partition_present": False,
            },
        )

    def test_the_medium_recipe_records_the_higher_fraction(self):
        recipe = selected_recipe(MIX_LOCKED_MEDIUM, arm_manifests())
        self.assertEqual(recipe["selected_arm_id"], "C")
        self.assertEqual(recipe["augmentation_seat_slot_fraction"], 0.25)


class ResultArtifactTest(unittest.TestCase):
    def test_a_well_formed_result_validates_and_round_trips(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "result.json"
            save_result(path, result_value())
            loaded = load_result(path)
            self.assertEqual(loaded["outcome"], MIX_LOCKED_LOW)
            self.assertEqual(len(loaded["cross_population_matrix"]), 9)
            self.assertEqual(len(loaded["paired_comparisons"]), 6)
            self.assertTrue(loaded["gates"]["hard_validity"]["A"]["passed"])
            self.assertEqual(loaded["selected_recipe"]["selected_arm_id"], "B")

    def test_an_outcome_that_the_recorded_evidence_does_not_support_is_rejected(self):
        """outcomeはfreeなstringではなく、recorded evidenceから再導出される。"""
        value = result_value()
        self.assertEqual(value["outcome"], MIX_LOCKED_LOW)
        value["outcome"] = MIX_LOCKED_MEDIUM
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_a_locked_outcome_without_gates_or_recipe_is_rejected(self):
        """`MIX LOCKED` を名乗りながら証拠が空のartifactを通さない。"""
        for field, empty in (("gates", {}), ("selected_recipe", None)):
            value = result_value()
            value[field] = empty
            with self.subTest(field=field), self.assertRaises(MixArtifactError):
                validate_result_value(value)

    def test_a_recipe_that_the_selection_rule_did_not_derive_is_rejected(self):
        value = result_value()
        value["selected_recipe"] = dict(value["selected_recipe"]) | {
            "augmentation_seat_slot_fraction": 0.25
        }
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_tampered_gate_detail_is_rejected(self):
        value = result_value()
        gates = json.loads(json.dumps(value["gates"]))
        gates["coverage_source_accounting"]["B"]["confirmed_kan"] = 999
        value["gates"] = gates
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_a_self_consistent_swap_of_the_locked_population_is_rejected(self):
        """内部整合していても、locked planと違うpopulationのresultは通さない。

        plan / population_identity / matrix identity / gates / selected_recipe を
        すべてtampered planへ揃えているので、内部整合だけを見るvalidatorは通す。
        最終outputはPhase 10へ渡すrecipeのlockそのものなので、artifactが
        「実際に実行したlocked planから来た」ことを証明できなければならない。
        """
        for tamper in ("source", "seats"):
            value = self_consistent_swapped_arm_result("B", tamper)
            with self.subTest(tamper=tamper):
                # tamperしたresultは内部整合している。
                entry = value["arms"]["B"]
                self.assertEqual(
                    entry["population_identity"],
                    hashlib.sha256(
                        canonical_json_bytes(entry["population_plan"])
                    ).hexdigest(),
                )
                self.assertEqual(
                    entry["population_plan"]["augmentation_seat_slots"], 12
                )
                self.assertIs(entry["population_plan"]["coverage_seat_balanced"], True)
                for cell in value["cross_population_matrix"]:
                    if cell["validation_population_id"] == "B":
                        self.assertEqual(
                            cell["validation_population_identity"],
                            entry["population_identity"],
                        )
                # それでもlocked planへbindできないので拒否される。
                with self.assertRaises(MixArtifactError):
                    validate_result_value(value)

    def test_a_population_identity_that_is_not_its_plan_hash_is_rejected(self):
        value = result_value()
        plan = json.loads(json.dumps(value["arms"]["C"]["population_plan"]))
        plan["augmented_hanchan"] = 23
        value["arms"]["C"]["population_plan"] = plan
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_arm_entries_must_carry_the_evidence_the_outcome_rests_on(self):
        for field in ("source_attribution", "dataset_retention", "provenance"):
            value = result_value()
            del value["arms"]["B"][field]
            with self.subTest(field=field), self.assertRaises(MixArtifactError):
                validate_result_value(value)

    def test_a_self_consistent_but_fabricated_comparison_is_rejected(self):
        """符号だけ整合した偽comparisonを、matrixからの再導出で拒否する。

        `pooled_delta` / interval / classification は互いに整合しているが、
        recorded per-hanchan measurementからは導出できない値である。
        """
        value = result_value()
        rows = json.loads(json.dumps(value["paired_comparisons"]))
        rows[0]["pooled_delta_mae"] = 0.05
        rows[0]["interval_lower"] = 0.04
        rows[0]["interval_upper"] = 0.06
        rows[0]["classification"] = NO_CLEAR_REGRESSION
        value["paired_comparisons"] = rows
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_a_comparison_whose_interval_was_widened_is_rejected(self):
        value = result_value()
        rows = json.loads(json.dumps(value["paired_comparisons"]))
        rows[0]["interval_lower"] = rows[0]["interval_lower"] - 1.0
        value["paired_comparisons"] = rows
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_the_recorded_outcome_follows_the_recorded_measurements(self):
        """matrixを本当に悪化させると、再導出でregressionが立つ。"""
        cells = matrix_cells({"A": 0.40, "B": 0.50, "C": 0.50})
        value = result_value(cells=cells)
        validate_result_value(value)
        self.assertEqual(value["outcome"], QUALITY_TRADEOFF)
        self.assertIsNone(value["selected_recipe"])
        for row in value["paired_comparisons"]:
            self.assertEqual(row["classification"], CLEAR_REGRESSION)

    def test_an_incomplete_matrix_is_rejected(self):
        value = result_value()
        value["cross_population_matrix"] = value["cross_population_matrix"][:-1]
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_a_duplicated_matrix_pair_is_rejected(self):
        value = result_value()
        cells = json.loads(json.dumps(value["cross_population_matrix"]))
        cells[1] = cells[0]
        value["cross_population_matrix"] = cells
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_missing_paired_comparisons_are_rejected(self):
        value = result_value()
        value["paired_comparisons"] = value["paired_comparisons"][:-1]
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_a_classification_that_contradicts_the_interval_is_rejected(self):
        value = result_value()
        rows = json.loads(json.dumps(value["paired_comparisons"]))
        rows[0]["classification"] = CLEAR_REGRESSION
        value["paired_comparisons"] = rows
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_an_unknown_outcome_is_rejected(self):
        value = result_value()
        value["outcome"] = "LOOKS FINE"
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_accumulating_with_historical_evidence_is_rejected(self):
        value = result_value()
        value["accumulated_with_historical_evidence"] = True
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_evaluating_a_test_partition_is_rejected(self):
        value = result_value()
        value["test_partition_evaluated"] = True
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_arms_sharing_a_dataset_identity_are_rejected(self):
        value = result_value()
        value["arms"]["B"]["dataset_identity"] = ARM_DATASET_IDENTITIES["A"]
        with self.assertRaises(MixArtifactError):
            validate_result_value(value)

    def test_a_tampered_result_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "result.json"
            save_result(path, result_value())
            value = json.loads(path.read_bytes())
            value["outcome_reasons"] = ["tampered"]
            path.write_bytes(canonical_json_bytes(value))
            with self.assertRaises(MixArtifactError):
                load_result(path)

    def test_an_existing_result_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "result.json"
            save_result(path, result_value())
            with self.assertRaises(FileExistsError):
                save_result(path, result_value())


class ImportContractTest(unittest.TestCase):
    def test_the_normal_mix_pilot_import_and_cli_are_torch_free(self):
        """通常のimportとCLI構築でtorchを要求しない。

        同一processのimport順に依存しないよう、独立したinterpreterで確認する。
        """
        import subprocess
        import sys

        script = (
            "import sys\n"
            "from lisjong_arena.stage3_mix_pilot.__main__ import _parser\n"
            "_parser()\n"
            "raise SystemExit(1 if 'torch' in sys.modules else 0)\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(completed.returncode, 0)


class CommandLineTest(unittest.TestCase):
    def test_the_plan_command_prints_the_locked_identities(self):
        value = cli._plan_command()
        self.assertEqual(value["ordered_seeds"], list(ORDERED_SEEDS))
        self.assertEqual(tuple(sorted(value["population_identities"])), ARM_IDS)
        self.assertEqual(len(value["arms"]), 3)

    def test_the_cli_has_no_seed_or_fraction_option(self):
        parser = cli._parser()
        text = parser.format_help()
        for forbidden in ("--seed", "--fraction", "--augmentation", "--epochs"):
            self.assertNotIn(forbidden, text)

    def test_generate_only_accepts_a_locked_arm_id(self):
        with self.assertRaises(SystemExit):
            cli._parser().parse_args(["generate", "--arm", "D", "--output", "x"])

    def test_matrix_requires_every_arm_exactly_once(self):
        with self.assertRaises(SystemExit):
            cli._keyed_paths([("A", Path("a")), ("B", Path("b"))], "--population")


if __name__ == "__main__":
    unittest.main()
