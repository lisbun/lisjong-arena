"""Stage 3 Entry Gate population, split, generation seam, and coverage tests."""

import ast
import hashlib
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from _stage3_entry_gate_fixtures import (
    STAGE3_BASE_SEEDS,
    stage3_corpus,
    stage3_population_artifacts,
)
from lisjong.policies import (
    GenbutsuDefenseTwoStepUkeirePolicy,
    HandValueAwareTwoStepUkeirePolicy,
    TwoStepUkeirePolicy,
    YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy,
)
from lisjong_engine.seat import Seat

from lisjong_arena.phase2_training_anchor.extraction import (
    normalized_seat_policy_factories,
)
from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase4_raw_corpus.extraction import extract_phase4_raw_game
from lisjong_arena.phase4_raw_corpus.generation import (
    generate_phase4_raw_corpus_for_seeds,
)
from lisjong_arena.phase4_raw_corpus.model import FIXED_SEEDS
from lisjong_arena.phase5_belief_dataset.builder import resolve_training_samples
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.split import (
    QUANTITATIVE_SEEDS,
    STAGE3_DEVELOPMENT_SEEDS,
    STAGE3_TRAIN_SEEDS,
    STAGE3_VALIDATION_SEEDS,
    FirstPartySplitPolicy,
    assign_first_party_games,
    partition_for_first_party_game,
)
from lisjong_arena.phase8_sequential.data import materialize_development_examples
from lisjong_arena.stage3_entry_gate.artifact import (
    RESULT_SCHEMA_VERSION,
    Stage3ArtifactError,
    load_result,
    save_result,
    validate_result_value,
)
from lisjong_arena.stage3_entry_gate.coverage import measure_population_coverage
from lisjong_arena.stage3_entry_gate.experiment import (
    Stage3ExperimentError,
    build_population_data,
    conditional_uniform_reference,
    validate_stage3_dataset,
)
from lisjong_arena.stage3_entry_gate.generation import (
    DATASET_DIRECTORY,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    RAW_DIRECTORY,
    GenerationCost,
    Stage3GenerationError,
    _peak_process_ram_bytes,
    _provenance_value,
    generate_population,
    load_population,
    load_population_manifest,
    validate_population_manifest,
)
from lisjong_arena.stage3_entry_gate.population import (
    MIXED_BASE_ORDER,
    GameSeatAssignment,
    PopulationPlan,
    SeatPolicyReference,
    Stage3PopulationError,
    plan_for_population_id,
    population_a_plan,
    population_b_plan,
    population_c_plan,
    stage3_population_plans,
)


class _HaltingPolicy:
    """最初のdecisionでseat identityつきsentinelを送出するstub Policy。"""

    class Halt(Exception):
        def __init__(self, tag: str) -> None:
            super().__init__(tag)
            self.tag = tag

    def __init__(self, tag: str) -> None:
        self._tag = tag

    def choose_action(self, context):
        raise _HaltingPolicy.Halt(self._tag)


def _halting_factory(tag: str):
    def factory():
        return _HaltingPolicy(tag)

    return factory


def _digest(seed: str) -> str:
    return (seed * 64)[:64]


def _valid_result_value() -> dict:
    identities = {
        "A": (_digest("a1"), _digest("a2"), _digest("a3")),
        "B": (_digest("b1"), _digest("b2"), _digest("b3")),
        "C": (_digest("c1"), _digest("c2"), _digest("c3")),
    }
    populations = {
        key: {
            "population_identity": value[0],
            "raw_corpus_identity": value[1],
            "dataset_identity": value[2],
        }
        for key, value in identities.items()
    }
    cells = []
    for training in ("A", "B", "C"):
        for validation in ("A", "B", "C"):
            cells.append(
                {
                    "training_population_id": training,
                    "training_population_identity": identities[training][0],
                    "validation_population_id": validation,
                    "validation_population_identity": identities[validation][0],
                    "validation_dataset_identity": identities[validation][2],
                    "sequential_validation_mae": 0.48,
                    "conditional_uniform_validation_mae": 0.49,
                    "delta_mae_vs_conditional_uniform": 0.49 - 0.48,
                    "physical_consistency": {"blocking_gate_passed": True},
                }
            )
    return {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "pilot_role": "development-only",
        "candidate": "S2",
        "reference_arm_id": "stage3-conditional-uniform-reference-arm-v1",
        "populations": populations,
        "cross_population_matrix": cells,
        "test_partition_evaluated": False,
        "accumulated_with_stage2_formal_holdout": False,
    }


class Stage3WindowsPortabilityTest(unittest.TestCase):
    def test_stage3_package_never_imports_resource_at_module_scope(self):
        """`resource`はUnix限定。Windowsでもplan CLIがimportできる必要がある。"""
        package = Path(generate_population.__module__.replace(".", "/")).parent
        root = Path(__file__).resolve().parent.parent / "src" / package
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                with self.subTest(module=path.name):
                    self.assertNotIn("resource", names)

    def test_peak_process_ram_is_best_effort(self):
        value = _peak_process_ram_bytes()
        self.assertTrue(value is None or (type(value) is int and value > 0))


class Stage3PopulationManifestValidationTest(unittest.TestCase):
    def _manifest(self, **overrides) -> dict:
        plan = population_a_plan()
        value = {
            "manifest_schema_version": ("stage3-entry-gate-population-manifest-v1"),
            "pilot_role": "development-only",
            "population_identity": plan.population_identity,
            "population_plan": plan.plan_value(),
            "raw_corpus_identity": _digest("a2"),
            "dataset_identity": _digest("a3"),
            "split_policy_id": FirstPartySplitPolicy.STAGE3_DEVELOPMENT.value,
            "provenance": {"fully_resolved": True},
            "generation_runtime": {},
            "coverage": {"events": {"hanchan": 12}},
            "cost": {},
            "conditional_uniform_baseline": {},
            "test_partition_present": False,
        }
        return value | overrides

    def test_valid_manifest_is_accepted(self):
        self.assertEqual(
            validate_population_manifest(self._manifest())["pilot_role"],
            "development-only",
        )

    def test_identity_must_be_the_hash_of_the_recorded_plan(self):
        with self.assertRaises(Stage3GenerationError):
            validate_population_manifest(
                self._manifest(population_identity=_digest("ff"))
            )

    def test_self_consistent_plan_tampering_is_rejected(self):
        """planを書き換えidentityを再計算しても、locked planと違えば拒否する。"""
        plan = population_a_plan()
        tampered_plan = plan.plan_value() | {"seat_assignment_semantics_id": "other"}
        identity = hashlib.sha256(canonical_json_bytes(tampered_plan)).hexdigest()
        with self.assertRaises(Stage3GenerationError):
            validate_population_manifest(
                self._manifest(
                    population_plan=tampered_plan, population_identity=identity
                )
            )

    def test_unknown_population_id_is_rejected(self):
        plan = population_a_plan()
        tampered_plan = plan.plan_value() | {"population_id": "D"}
        identity = hashlib.sha256(canonical_json_bytes(tampered_plan)).hexdigest()
        with self.assertRaises(Stage3GenerationError):
            validate_population_manifest(
                self._manifest(
                    population_plan=tampered_plan, population_identity=identity
                )
            )

    def test_unresolved_provenance_and_wrong_split_are_rejected(self):
        with self.assertRaises(Stage3GenerationError):
            validate_population_manifest(
                self._manifest(provenance={"fully_resolved": False})
            )
        with self.assertRaises(Stage3GenerationError):
            validate_population_manifest(
                self._manifest(split_policy_id=FirstPartySplitPolicy.QUANTITATIVE.value)
            )

    def test_wrong_hanchan_count_is_rejected(self):
        with self.assertRaises(Stage3GenerationError):
            validate_population_manifest(
                self._manifest(coverage={"events": {"hanchan": 11}})
            )


class Stage3PopulationProvenanceBindingTest(unittest.TestCase):
    """manifest provenanceが実体のcorpus / dataset provenanceへbindされること。

    このbindingが無いと、raw / datasetを一切触らずmanifestのsource revisionsや
    rules fingerprintだけを書き換えたpopulationがloadでき、その値がそのまま
    3 x 3 result artifactのpopulation provenanceへ転記されてしまう。
    """

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name) / "population"
        self.root.mkdir(parents=True)
        persisted_raw, dataset = stage3_population_artifacts(self.root)
        self.plan = population_a_plan()
        self.manifest = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "pilot_role": "development-only",
            "population_identity": self.plan.population_identity,
            "population_plan": self.plan.plan_value(),
            "raw_corpus_identity": persisted_raw.corpus_identity,
            "dataset_identity": dataset.dataset_identity,
            "split_policy_id": FirstPartySplitPolicy.STAGE3_DEVELOPMENT.value,
            "provenance": _provenance_value(dataset.provenance),
            "generation_runtime": {},
            "coverage": {"events": {"hanchan": 12}},
            "cost": {},
            "conditional_uniform_baseline": {},
            "test_partition_present": False,
        }
        self._write(self.manifest)

    def tearDown(self):
        self._directory.cleanup()

    def _write(self, manifest: dict) -> None:
        (self.root / MANIFEST_FILENAME).write_bytes(canonical_json_bytes(manifest))

    def test_directory_layout_matches_the_generator(self):
        self.assertTrue((self.root / RAW_DIRECTORY).is_dir())
        self.assertTrue((self.root / DATASET_DIRECTORY).is_dir())

    def test_matching_provenance_loads(self):
        manifest, persisted_raw, persisted_dataset = load_population(self.root)
        self.assertEqual(manifest["raw_corpus_identity"], persisted_raw.corpus_identity)
        self.assertEqual(
            manifest["provenance"],
            _provenance_value(persisted_dataset.dataset.provenance),
        )

    def test_self_consistent_source_revision_tampering_is_rejected(self):
        """revisionを別SHAへ書き換え、fully_resolvedもcanonical bytesも保つ改変。"""
        provenance = dict(self.manifest["provenance"])
        provenance["source_revisions"] = dict(provenance["source_revisions"]) | {
            "lisjong": "9" * 40
        }
        self.assertIs(provenance["fully_resolved"], True)
        self._write(self.manifest | {"provenance": provenance})
        # manifest単体としてはvalidであることを先に確認しておく。
        validate_population_manifest(load_population_manifest(self.root))
        with self.assertRaises(Stage3GenerationError):
            load_population(self.root)

    def test_self_consistent_rules_fingerprint_tampering_is_rejected(self):
        provenance = dict(self.manifest["provenance"])
        provenance["effective_rules"] = dict(provenance["effective_rules"]) | {
            "fingerprint": "b" * 64
        }
        self._write(self.manifest | {"provenance": provenance})
        with self.assertRaises(Stage3GenerationError):
            load_population(self.root)

    def test_label_semantics_tampering_is_rejected(self):
        provenance = dict(self.manifest["provenance"]) | {
            "label_semantics_id": "other-label-semantics-v9"
        }
        self._write(self.manifest | {"provenance": provenance})
        with self.assertRaises(Stage3GenerationError):
            load_population(self.root)


class Stage3ResultValidationTest(unittest.TestCase):
    def test_valid_three_by_three_result_round_trips(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "result.json"
            save_result(destination, _valid_result_value())
            loaded = load_result(destination)
            self.assertEqual(len(loaded["cross_population_matrix"]), 9)

    def test_empty_matrix_is_rejected(self):
        with self.assertRaises(Stage3ArtifactError):
            validate_result_value(
                _valid_result_value() | {"cross_population_matrix": []}
            )

    def test_incomplete_and_duplicated_matrix_pairs_are_rejected(self):
        value = _valid_result_value()
        with self.assertRaises(Stage3ArtifactError):
            validate_result_value(
                value
                | {"cross_population_matrix": value["cross_population_matrix"][:8]}
            )
        duplicated = list(value["cross_population_matrix"][:8]) + [
            dict(value["cross_population_matrix"][0])
        ]
        with self.assertRaises(Stage3ArtifactError):
            validate_result_value(value | {"cross_population_matrix": duplicated})

    def test_development_only_and_test_seal_are_enforced(self):
        for override in (
            {"pilot_role": "formal"},
            {"test_partition_evaluated": True},
            {"accumulated_with_stage2_formal_holdout": True},
            {"candidate": "S1"},
            {"reference_arm_id": "other"},
        ):
            with (
                self.subTest(override=override),
                self.assertRaises(Stage3ArtifactError),
            ):
                validate_result_value(_valid_result_value() | override)

    def test_cell_identity_mismatch_is_rejected(self):
        value = _valid_result_value()
        cells = [dict(cell) for cell in value["cross_population_matrix"]]
        cells[0]["validation_dataset_identity"] = _digest("ee")
        with self.assertRaises(Stage3ArtifactError):
            validate_result_value(value | {"cross_population_matrix": cells})

    def test_inconsistent_delta_is_rejected(self):
        value = _valid_result_value()
        cells = [dict(cell) for cell in value["cross_population_matrix"]]
        cells[0]["delta_mae_vs_conditional_uniform"] = 0.5
        with self.assertRaises(Stage3ArtifactError):
            validate_result_value(value | {"cross_population_matrix": cells})

    def test_duplicate_population_datasets_are_rejected(self):
        value = _valid_result_value()
        populations = {key: dict(entry) for key, entry in value["populations"].items()}
        populations["B"]["dataset_identity"] = populations["A"]["dataset_identity"]
        with self.assertRaises(Stage3ArtifactError):
            validate_result_value(value | {"populations": populations})


class Stage3PopulationPlanTest(unittest.TestCase):
    def test_locked_plans_use_the_development_only_seed_population(self):
        for plan in stage3_population_plans():
            with self.subTest(population=plan.population_id):
                self.assertEqual(
                    tuple(value.game_seed for value in plan.assignments),
                    STAGE3_DEVELOPMENT_SEEDS,
                )
                self.assertEqual(plan.train_seeds, STAGE3_TRAIN_SEEDS)
                self.assertEqual(plan.validation_seeds, STAGE3_VALIDATION_SEEDS)
                self.assertEqual(len(plan.assignments), 12)

    def test_mixed_population_rotates_one_seat_per_seed_and_is_balanced(self):
        plan = population_c_plan()
        base = tuple(value.identity for value in MIXED_BASE_ORDER)
        for index, assignment in enumerate(plan.assignments):
            with self.subTest(seed=assignment.game_seed):
                self.assertEqual(
                    assignment.seat_identities,
                    tuple(base[(seat - index) % 4] for seat in range(4)),
                )
        self.assertTrue(plan.is_seat_balanced)
        self.assertEqual(
            plan.seat_occupancy(),
            {identity: (3, 3, 3, 3) for identity in base},
        )

    def test_uniform_populations_seat_one_policy_everywhere(self):
        for plan, identity in (
            (population_a_plan(), "two-step"),
            (population_b_plan(), "yakuhai-call"),
        ):
            with self.subTest(population=plan.population_id):
                self.assertEqual(
                    {value.identity for value in plan.policies}, {identity}
                )
                self.assertTrue(
                    all(
                        set(value.seat_identities) == {identity}
                        for value in plan.assignments
                    )
                )

    def test_population_identity_includes_seat_assignment(self):
        plan = population_c_plan()
        rotated = replace(
            plan,
            assignments=(
                replace(
                    plan.assignments[0],
                    seat_identities=tuple(
                        reversed(plan.assignments[0].seat_identities)
                    ),
                ),
            )
            + plan.assignments[1:],
        )
        self.assertNotEqual(plan.population_identity, rotated.population_identity)
        self.assertEqual(
            plan.population_identity, population_c_plan().population_identity
        )

    def test_population_identities_are_distinct_per_population(self):
        identities = {
            plan.population_id: plan.population_identity
            for plan in stage3_population_plans()
        }
        self.assertEqual(len(set(identities.values())), 3)
        self.assertEqual(set(identities), {"A", "B", "C"})

    def test_plan_rejects_seeds_outside_the_locked_population(self):
        policy = SeatPolicyReference(identity="two-step", reference="two-step")
        with self.assertRaises(Stage3PopulationError):
            PopulationPlan(
                population_id="A",
                seat_assignment_semantics_id="fixed-single-policy-v1",
                policies=(policy,),
                assignments=(GameSeatAssignment(179, ("two-step",) * 4),)
                + tuple(
                    GameSeatAssignment(seed, ("two-step",) * 4)
                    for seed in STAGE3_DEVELOPMENT_SEEDS[1:]
                ),
            )

    def test_plan_rejects_undeclared_and_unseated_identities(self):
        declared = SeatPolicyReference(identity="two-step", reference="two-step")
        with self.assertRaises(Stage3PopulationError):
            PopulationPlan(
                population_id="A",
                seat_assignment_semantics_id="fixed-single-policy-v1",
                policies=(declared,),
                assignments=tuple(
                    GameSeatAssignment(seed, ("hand-value-aware",) * 4)
                    for seed in STAGE3_DEVELOPMENT_SEEDS
                ),
            )
        with self.assertRaises(Stage3PopulationError):
            PopulationPlan(
                population_id="A",
                seat_assignment_semantics_id="fixed-single-policy-v1",
                policies=(
                    declared,
                    SeatPolicyReference(
                        identity="hand-value-aware", reference="hand-value-aware"
                    ),
                ),
                assignments=tuple(
                    GameSeatAssignment(seed, ("two-step",) * 4)
                    for seed in STAGE3_DEVELOPMENT_SEEDS
                ),
            )

    def test_plan_factories_resolve_to_the_exact_pilot_policy_classes(self):
        expected = {
            "two-step": TwoStepUkeirePolicy,
            "genbutsu-defense-two-step": GenbutsuDefenseTwoStepUkeirePolicy,
            "hand-value-aware": HandValueAwareTwoStepUkeirePolicy,
            "yakuhai-call": (
                YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy
            ),
        }
        for reference in MIXED_BASE_ORDER:
            with self.subTest(identity=reference.identity):
                factory = reference.factory()
                first = factory()
                second = factory()
                self.assertIsInstance(first, expected[reference.identity])
                self.assertIsNot(first, second)

    def test_catalog_alias_identity_mismatch_fails_closed(self):
        reference = SeatPolicyReference(identity="two-step", reference="yakuhai-call")
        with self.assertRaises(Stage3PopulationError):
            reference.factory()

    def test_seat_factories_follow_the_planned_seat_assignment(self):
        plan = population_c_plan()
        factories = plan.seat_policy_factories_by_seed()
        self.assertEqual(set(factories), set(STAGE3_DEVELOPMENT_SEEDS))
        for assignment in plan.assignments:
            seat_factories = factories[assignment.game_seed]
            for index, seat in enumerate(Seat):
                with self.subTest(seed=assignment.game_seed, seat=seat.value):
                    instance = seat_factories[seat]()
                    self.assertEqual(
                        type(instance).__name__,
                        {
                            "two-step": "TwoStepUkeirePolicy",
                            "genbutsu-defense-two-step": (
                                "GenbutsuDefenseTwoStepUkeirePolicy"
                            ),
                            "hand-value-aware": "HandValueAwareTwoStepUkeirePolicy",
                            "yakuhai-call": (
                                "YakuhaiCallGenbutsuDefense"
                                "FiniteHorizonHandValueAwarePolicy"
                            ),
                        }[assignment.seat_identities[index]],
                    )

    def test_unknown_population_id_fails_closed(self):
        with self.assertRaises(Stage3PopulationError):
            plan_for_population_id("D")


class Stage3SplitTest(unittest.TestCase):
    def test_stage3_split_is_whole_hanchan_eight_four_without_test(self):
        for seed in STAGE3_TRAIN_SEEDS:
            self.assertIs(
                partition_for_first_party_game(
                    "first-party-bootstrap",
                    seed,
                    FirstPartySplitPolicy.STAGE3_DEVELOPMENT,
                ),
                DatasetPartition.TRAIN,
            )
        for seed in STAGE3_VALIDATION_SEEDS:
            self.assertIs(
                partition_for_first_party_game(
                    "first-party-bootstrap",
                    seed,
                    FirstPartySplitPolicy.STAGE3_DEVELOPMENT,
                ),
                DatasetPartition.VALIDATION,
            )

    def test_stage3_split_rejects_stage1_and_stage2_seeds(self):
        for seed in (149, 150, 179, 192, 1000):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                partition_for_first_party_game(
                    "first-party-bootstrap",
                    seed,
                    FirstPartySplitPolicy.STAGE3_DEVELOPMENT,
                )

    def test_stage3_split_requires_its_exact_locked_population(self):
        with self.assertRaises(ValueError):
            assign_first_party_games(
                stage3_corpus(), FirstPartySplitPolicy.QUANTITATIVE
            )

    def test_existing_split_policies_are_unchanged(self):
        self.assertEqual(
            FirstPartySplitPolicy.ACCEPTANCE.value,
            "first-party-seeds-1000-1007-all-test-v1",
        )
        self.assertEqual(
            FirstPartySplitPolicy.QUANTITATIVE.value,
            "first-party-seeds-100-159-40-10-10-v1",
        )
        self.assertIs(
            partition_for_first_party_game(
                "first-party-bootstrap", 150, FirstPartySplitPolicy.QUANTITATIVE
            ),
            DatasetPartition.TEST,
        )
        self.assertEqual(QUANTITATIVE_SEEDS[-1], 159)
        self.assertNotIn(180, QUANTITATIVE_SEEDS)

    def test_stage3_dataset_has_no_test_partition_and_is_game_atomic(self):
        with tempfile.TemporaryDirectory() as name:
            _raw, dataset = stage3_population_artifacts(Path(name))
        self.assertEqual(
            dataset.split_policy_id,
            FirstPartySplitPolicy.STAGE3_DEVELOPMENT.value,
        )
        self.assertEqual(
            {summary.partition for summary in dataset.partition_summaries},
            {DatasetPartition.TRAIN, DatasetPartition.VALIDATION},
        )
        by_partition = {
            partition: tuple(
                assignment.game.game_seed
                for assignment in dataset.games
                if assignment.partition is partition
            )
            for partition in DatasetPartition
        }
        self.assertEqual(by_partition[DatasetPartition.TRAIN], STAGE3_TRAIN_SEEDS)
        self.assertEqual(
            by_partition[DatasetPartition.VALIDATION], STAGE3_VALIDATION_SEEDS
        )
        self.assertEqual(by_partition[DatasetPartition.TEST], ())


class SeatPolicyFactorySeamTest(unittest.TestCase):
    def test_default_population_is_still_two_step_x4(self):
        factories = normalized_seat_policy_factories(None)
        self.assertEqual(set(factories), set(Seat))
        for seat in Seat:
            self.assertIsInstance(factories[seat](), TwoStepUkeirePolicy)

    def test_partial_or_non_callable_seat_factories_fail_closed(self):
        with self.assertRaises(ValueError):
            normalized_seat_policy_factories(
                {seat: TwoStepUkeirePolicy for seat in tuple(Seat)[:3]}
            )
        with self.assertRaises(TypeError):
            normalized_seat_policy_factories(
                {seat: TwoStepUkeirePolicy for seat in Seat} | {tuple(Seat)[0]: 1}
            )
        with self.assertRaises(TypeError):
            normalized_seat_policy_factories(["not", "a", "mapping", "x"])

    def test_explicit_seat_factories_reach_their_own_seat(self):
        tags = {seat: f"tag-{seat.value}" for seat in Seat}
        with self.assertRaises(_HaltingPolicy.Halt) as caught:
            extract_phase4_raw_game(
                FIXED_SEEDS[0],
                seat_policy_factories={
                    seat: _halting_factory(tags[seat]) for seat in Seat
                },
            )
        self.assertIn(caught.exception.tag, set(tags.values()))

    def test_generation_rejects_seat_factories_that_miss_a_seed(self):
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(ValueError):
                generate_phase4_raw_corpus_for_seeds(
                    Path(name) / "raw",
                    (180, 181),
                    seat_policy_factories_by_seed={
                        180: {seat: TwoStepUkeirePolicy for seat in Seat}
                    },
                )


class Stage3CoverageTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        self.persisted_raw, self.dataset = stage3_population_artifacts(root)
        self.samples = resolve_training_samples(self.dataset, self.persisted_raw)
        self.examples = materialize_development_examples(
            self.dataset.examples, self.samples
        )

    def tearDown(self):
        self._directory.cleanup()

    def test_coverage_counts_partitions_without_fabricating_absent_strata(self):
        coverage = measure_population_coverage(self.persisted_raw.corpus, self.examples)
        value = coverage.coverage_value()
        self.assertEqual(value["events"]["hanchan"], 12)
        self.assertEqual(value["events"]["stable_turn_anchors"], len(self.examples))
        self.assertEqual(value["events"]["opponent_rows"], 3 * len(self.examples))
        self.assertEqual(set(value["partitions"]), {"train", "validation"})
        self.assertEqual(
            value["partitions"]["train"]["anchors"]
            + value["partitions"]["validation"]["anchors"],
            len(self.examples),
        )
        for name in coverage.events.absent_event_strata:
            self.assertEqual(value["events"][name], 0)

    def test_coverage_opponent_row_strata_sum_to_the_opponent_rows(self):
        coverage = measure_population_coverage(self.persisted_raw.corpus, self.examples)
        for partition in coverage.partitions:
            with self.subTest(partition=partition.partition):
                self.assertEqual(
                    partition.opponent_rows_open + partition.opponent_rows_closed,
                    partition.anchors * 3,
                )
                self.assertEqual(
                    partition.opponent_rows_true_tenpai
                    + partition.opponent_rows_true_non_tenpai
                    + partition.opponent_rows_wait_unavailable,
                    partition.anchors * 3,
                )
                self.assertEqual(
                    sum(count for _name, count in partition.depth_bucket_counts),
                    partition.anchors,
                )

    def test_coverage_rejects_test_partition_examples(self):
        tampered = tuple(
            replace(
                value,
                example=replace(value.example, partition=DatasetPartition.TEST),
            )
            for value in self.examples
        )
        with self.assertRaises(ValueError):
            measure_population_coverage(self.persisted_raw.corpus, tampered)


class Stage3ExperimentContractTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        self.persisted_raw, self.dataset = stage3_population_artifacts(root)

    def tearDown(self):
        self._directory.cleanup()

    def test_validate_rejects_a_dataset_that_is_not_the_stage3_split(self):
        with self.assertRaises(Stage3ExperimentError):
            validate_stage3_dataset(
                replace(
                    self.dataset,
                    split_policy_id=FirstPartySplitPolicy.QUANTITATIVE.value,
                )
            )

    def test_population_data_binds_identities_and_seals_test(self):
        data = build_population_data(
            population_id="A",
            population_identity=population_a_plan().population_identity,
            persisted_raw=self.persisted_raw,
            dataset=self.dataset,
        )
        self.assertEqual(data.dataset_identity, self.dataset.dataset_identity)
        self.assertEqual(data.raw_corpus_identity, self.persisted_raw.corpus_identity)
        self.assertEqual(len(data.train_sequences), len(STAGE3_TRAIN_SEEDS))
        self.assertEqual(len(data.validation_sequences), len(STAGE3_VALIDATION_SEEDS))
        self.assertEqual(data.inventory.test_sequence_count, 0)

    def test_reference_arm_matches_the_direct_conditional_uniform_baseline(self):
        samples = resolve_training_samples(self.dataset, self.persisted_raw)
        examples = materialize_development_examples(self.dataset.examples, samples)
        validation = tuple(
            value
            for value in examples
            if value.example.partition is DatasetPartition.VALIDATION
        )
        reference = conditional_uniform_reference(
            self.dataset.dataset_identity, validation
        )
        self.assertEqual(reference.snapshot_metrics.sample_count, len(validation))
        self.assertEqual(len(reference.snapshot_predictions), len(validation))
        self.assertEqual(
            tuple(value.example for value in reference.examples),
            tuple(value.example for value in validation),
        )

    def test_reference_arm_requires_validation_examples(self):
        with self.assertRaises(Stage3ExperimentError):
            conditional_uniform_reference(self.dataset.dataset_identity, ())


class Stage3GenerationCostTest(unittest.TestCase):
    def _cost(self, **overrides) -> GenerationCost:
        values = {
            "hanchan": 12,
            "stable_turn_anchors": 600,
            "generation_wall_clock_seconds": 900.0,
            "generation_cpu_seconds": 890.0,
            "recording_wall_clock_seconds": 430.0,
            "readback_seconds": 1.0,
            "derivation_seconds": 2.0,
            "dataset_build_seconds": 3.0,
            "dataset_persistence_seconds": 0.5,
            "baseline_evaluation_seconds": 4.0,
            "peak_process_ram_bytes": 1024,
            "raw_uncompressed_bytes": 2048,
            "raw_compressed_bytes": 512,
            "dataset_bytes": 256,
        }
        return GenerationCost(**(values | overrides))

    def test_wall_clock_and_cpu_share_the_whole_generation_scope(self):
        value = self._cost().cost_value()
        self.assertEqual(value["wall_clock_seconds_per_hanchan"], 900.0 / 12)
        self.assertEqual(value["cpu_seconds_per_hanchan"], 890.0 / 12)
        self.assertEqual(value["wall_clock_seconds_per_anchor"], 900.0 / 600)
        self.assertEqual(value["cpu_seconds_per_anchor"], 890.0 / 600)
        self.assertEqual(value["recording_wall_clock_seconds_per_hanchan"], 430.0 / 12)
        self.assertIn("Phase 2 equality re-run", value["measurement_scope"])

    def test_recording_time_cannot_exceed_the_whole_generation_call(self):
        with self.assertRaises(Stage3GenerationError):
            self._cost(recording_wall_clock_seconds=901.0)


class Stage3GenerationContractTest(unittest.TestCase):
    def test_generate_population_refuses_an_existing_destination(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "population"
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                generate_population(population_a_plan(), destination)

    def test_manifest_loader_fails_closed_on_a_tampered_manifest(self):
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name)
            (destination / MANIFEST_FILENAME).write_bytes(
                b'{"manifest_schema_version":"other"}'
            )
            with self.assertRaises(Stage3GenerationError):
                load_population_manifest(destination)


class Stage3ImportContractTest(unittest.TestCase):
    def test_normal_stage3_import_and_cli_contract_are_torch_free(self):
        self.assertNotIn("torch", sys.modules)
        from lisjong_arena.stage3_entry_gate.__main__ import _parser

        parser = _parser()
        command_action = next(
            action for action in parser._actions if action.dest == "command"
        )
        self.assertEqual(
            set(command_action.choices), {"plan", "generate", "train", "matrix"}
        )
        help_text = parser.format_help().lower()
        self.assertNotIn("test partition", help_text)
        self.assertNotIn("torch", sys.modules)

    def test_distinct_base_games_produce_distinct_population_datasets(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            first_raw, first = stage3_population_artifacts(
                root / "first", STAGE3_BASE_SEEDS[0]
            )
            second_raw, second = stage3_population_artifacts(
                root / "second", STAGE3_BASE_SEEDS[1]
            )
            self.assertNotEqual(first_raw.corpus_identity, second_raw.corpus_identity)
            self.assertNotEqual(first.dataset_identity, second.dataset_identity)


if __name__ == "__main__":
    unittest.main()
