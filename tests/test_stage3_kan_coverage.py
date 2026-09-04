"""Arena #146 kan coverage-source qualification tests。

24 hanchanのformal executionはここでは実行しない。protocol identity、
opportunity accounting、evidence accounting、dataset compatibility、
classification ruleの境界だけをdeterministicに固定する。
"""

import json
import tempfile
import unittest
from pathlib import Path

from _stage3_entry_gate_fixtures import stage3_corpus
from _stage3_kan_coverage_fixtures import (
    ankan_action,
    daiminkan_action,
    daiminkan_stream,
    decision_with,
    declared_kan_stream,
    discard_action,
    kakan_action,
    kan_accounting_totals,
    kan_coverage_artifacts,
    kan_coverage_corpus,
    kan_opportunity_diagnostic_value,
    pass_action,
    population_manifest_value,
    ron_action,
    tsumo_action,
)
from lisjong.policies.kan_coverage_yakuhai_call import KanCoverageYakuhaiCallPolicy
from lisjong_engine.public_state import PublicMeldType
from lisjong_engine.round_evidence import DrawEvidence, RoundEndedEvidence, RoundEndKind
from lisjong_engine.seat import Seat

from lisjong_arena.phase4_raw_corpus.codec import canonical_json_bytes
from lisjong_arena.phase5_belief_dataset.model import DatasetPartition
from lisjong_arena.phase5_belief_dataset.split import (
    STAGE3_DEVELOPMENT_SEEDS,
    STAGE3_TRAIN_SEEDS,
    STAGE3_VALIDATION_SEEDS,
    FirstPartySplitPolicy,
    assign_first_party_games,
    partition_for_first_party_game,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.policy_reference import resolve_policy_reference
from lisjong_arena.stage3_entry_gate.population import (
    PopulationPlan,
    Stage3PopulationError,
    stage3_population_plans,
)
from lisjong_arena.stage3_kan_coverage.accounting import (
    CONFIRMED,
    NON_CONFIRM,
    UNACCOUNTED,
    KanAccountingError,
    account_selected_kans,
    classify_selected_kan,
)
from lisjong_arena.stage3_kan_coverage.generation import (
    KanCoverageGenerationError,
    KanEventRow,
    dataset_retention_value,
    kan_event_inventory,
    validate_population_manifest,
)
from lisjong_arena.stage3_kan_coverage.opportunity import (
    KanOpportunityError,
    KanOpportunityObserver,
)
from lisjong_arena.stage3_kan_coverage.population import (
    KanCoveragePopulationError,
    KanCoveragePopulationPlan,
    kan_coverage_population_plan,
)
from lisjong_arena.stage3_kan_coverage.protocol import (
    ACCOUNTING_REFORMULATE,
    INSUFFICIENT,
    ORDERED_SEEDS,
    PILOT_HANCHAN,
    POLICY_IDENTITY,
    POLICY_IMPORT_REFERENCE,
    QUALIFIED,
    STOP_INVALID,
    TRAIN_SEEDS,
    VALIDATION_SEEDS,
)
from lisjong_arena.stage3_kan_coverage.result import (
    CONTRACT_VIOLATION,
    OBSERVED,
    UNMEASURED,
    KanCoverageResultError,
    classify,
    kind_interpretation,
    load_result,
    result_value,
    save_result,
    validate_result_value,
)


def _observe(*decisions, policy=None, seed: int = 306, seat: Seat = Seat.EAST):
    """observerでwrapしたPolicyへdecisionを順に流し、diagnosticを返す。"""
    observer = KanOpportunityObserver()
    factory = policy or KanCoverageYakuhaiCallPolicy
    wrapped = observer.wrap_factories_by_seed({seed: {seat: factory}})
    observed = wrapped[seed][seat]()
    for decision in decisions:
        observed.choose_action(decision)
    return observer.resolve()


class KanCoveragePopulationTest(unittest.TestCase):
    def test_explicit_import_reference_resolves_to_the_coverage_policy(self):
        spec = resolve_policy_reference(
            POLICY_IMPORT_REFERENCE, explicit_identity=POLICY_IDENTITY
        )
        self.assertEqual(spec.identity, POLICY_IDENTITY)
        self.assertIs(spec.factory, KanCoverageYakuhaiCallPolicy)

    def test_no_catalog_registration_is_required(self):
        self.assertNotIn(POLICY_IDENTITY, POLICY_CATALOG)

    def test_every_seat_and_game_builds_a_fresh_policy_instance(self):
        plan = kan_coverage_population_plan()
        factories = plan.seat_policy_factories_by_seed()
        self.assertEqual(tuple(factories), ORDERED_SEEDS)
        instances = [
            factories[seed][seat]() for seed in ORDERED_SEEDS[:2] for seat in Seat
        ]
        self.assertEqual(len(instances), 8)
        self.assertEqual(len({id(value) for value in instances}), 8)
        self.assertTrue(
            all(isinstance(value, KanCoverageYakuhaiCallPolicy) for value in instances)
        )

    def test_the_population_is_the_same_policy_in_every_seat(self):
        plan = kan_coverage_population_plan()
        for assignment in plan.assignments:
            self.assertEqual(assignment.seat_identities, (POLICY_IDENTITY,) * 4)

    def test_exact_ordered_seed_membership(self):
        plan = kan_coverage_population_plan()
        self.assertEqual(plan.ordered_seeds, tuple(range(306, 330)))
        self.assertEqual(len(plan.ordered_seeds), PILOT_HANCHAN)
        self.assertEqual(plan.train_seeds, tuple(range(306, 324)))
        self.assertEqual(plan.validation_seeds, tuple(range(324, 330)))
        self.assertEqual(len(plan.train_seeds), 18)
        self.assertEqual(len(plan.validation_seeds), 6)

    def test_plan_rejects_a_different_seed_population(self):
        plan = kan_coverage_population_plan()
        with self.assertRaises(KanCoveragePopulationError):
            KanCoveragePopulationPlan(
                policy=plan.policy, assignments=plan.assignments[:-1]
            )

    def test_plan_rejects_a_non_uniform_seat_assignment(self):
        from lisjong_arena.stage3_entry_gate.population import GameSeatAssignment

        plan = kan_coverage_population_plan()
        broken = (
            GameSeatAssignment(
                ORDERED_SEEDS[0], (POLICY_IDENTITY, POLICY_IDENTITY, "two-step", "x")
            ),
            *plan.assignments[1:],
        )
        with self.assertRaises(KanCoveragePopulationError):
            KanCoveragePopulationPlan(policy=plan.policy, assignments=broken)

    def test_population_identity_is_the_hash_of_the_plan(self):
        plan = kan_coverage_population_plan()
        import hashlib

        self.assertEqual(
            plan.population_identity,
            hashlib.sha256(canonical_json_bytes(plan.plan_value())).hexdigest(),
        )
        self.assertIs(plan.plan_value()["test_partition_present"], False)

    def test_split_assigns_whole_games_and_seals_the_test_partition(self):
        for seed in TRAIN_SEEDS:
            self.assertIs(
                partition_for_first_party_game(
                    "first-party-bootstrap",
                    seed,
                    FirstPartySplitPolicy.KAN_COVERAGE_DEVELOPMENT,
                ),
                DatasetPartition.TRAIN,
            )
        for seed in VALIDATION_SEEDS:
            self.assertIs(
                partition_for_first_party_game(
                    "first-party-bootstrap",
                    seed,
                    FirstPartySplitPolicy.KAN_COVERAGE_DEVELOPMENT,
                ),
                DatasetPartition.VALIDATION,
            )
        assignments = assign_first_party_games(
            kan_coverage_corpus(), FirstPartySplitPolicy.KAN_COVERAGE_DEVELOPMENT
        )
        self.assertEqual(len(assignments), PILOT_HANCHAN)
        self.assertNotIn(
            DatasetPartition.TEST, {value.partition for value in assignments}
        )


class HistoricalIsolationTest(unittest.TestCase):
    def test_stage3_historical_constants_are_unchanged(self):
        self.assertEqual(STAGE3_TRAIN_SEEDS, tuple(range(180, 188)))
        self.assertEqual(STAGE3_VALIDATION_SEEDS, tuple(range(188, 192)))
        self.assertEqual(STAGE3_DEVELOPMENT_SEEDS, tuple(range(180, 192)))
        self.assertEqual(
            FirstPartySplitPolicy.STAGE3_DEVELOPMENT.value,
            "first-party-seeds-180-191-8-4-development-only-v1",
        )

    def test_stage3_population_plans_remain_locked_to_their_seeds(self):
        for plan in stage3_population_plans():
            self.assertEqual(plan.plan_value()["ordered_seeds"], list(range(180, 192)))
            self.assertEqual(plan.plan_value()["pilot_role"], "development-only")

    def test_stage3_plans_reject_the_successor_seed_population(self):
        from lisjong_arena.stage3_entry_gate.population import (
            GameSeatAssignment,
            SeatPolicyReference,
        )

        policy = SeatPolicyReference(identity="two-step", reference="two-step")
        with self.assertRaises(Stage3PopulationError):
            PopulationPlan(
                population_id="A",
                seat_assignment_semantics_id="fixed-single-policy-v1",
                policies=(policy,),
                assignments=tuple(
                    GameSeatAssignment(seed, ("two-step",) * 4)
                    for seed in ORDERED_SEEDS
                ),
            )

    def test_successor_seeds_are_disjoint_from_every_historical_population(self):
        self.assertTrue(set(ORDERED_SEEDS).isdisjoint(STAGE3_DEVELOPMENT_SEEDS))
        self.assertTrue(set(ORDERED_SEEDS).isdisjoint(range(100, 180)))

    def test_each_split_policy_rejects_the_other_population(self):
        with self.assertRaises(ValueError):
            partition_for_first_party_game(
                "first-party-bootstrap", 306, FirstPartySplitPolicy.STAGE3_DEVELOPMENT
            )
        with self.assertRaises(ValueError):
            partition_for_first_party_game(
                "first-party-bootstrap",
                180,
                FirstPartySplitPolicy.KAN_COVERAGE_DEVELOPMENT,
            )

    def test_historical_stage3_corpus_still_measures_its_own_events(self):
        from lisjong_arena.stage3_entry_gate.coverage import _event_coverage

        coverage = _event_coverage(stage3_corpus(), 12)
        self.assertEqual(coverage.hanchan, 12)
        self.assertEqual(coverage.daiminkan, 0)
        self.assertEqual(coverage.ankan, 0)
        self.assertEqual(coverage.kakan, 0)
        self.assertEqual(coverage.rinshan_draw, 0)


class KanOpportunityAccountingTest(unittest.TestCase):
    def test_winning_action_with_a_legal_kan_is_not_a_no_win_opportunity(self):
        for winning, kan in (
            (ron_action(), daiminkan_action()),
            (tsumo_action(), ankan_action()),
        ):
            with self.subTest(winning=type(winning).__name__):
                diagnostic = _observe(decision_with(winning, kan, pass_action()))
                value = diagnostic.diagnostic_value()
                self.assertEqual(value["kan_opportunity_decisions"], 1)
                self.assertEqual(value["eligible_no_win_opportunity_decisions"], 0)
                self.assertEqual(value["winning_action_also_legal_decisions"], 1)
                self.assertEqual(value["selected_kan_decisions"], 0)
                self.assertEqual(value["selection_contract_violations"], 0)

    def test_eligible_no_win_opportunities_are_converted_to_kan(self):
        cases = (
            ("daiminkan", decision_with(daiminkan_action(), pass_action())),
            ("ankan", decision_with(ankan_action(), discard_action())),
            ("kakan", decision_with(kakan_action(), discard_action())),
        )
        for kind, decision in cases:
            with self.subTest(kind=kind):
                diagnostic = _observe(decision)
                value = diagnostic.diagnostic_value()
                self.assertEqual(value["eligible_no_win_opportunity_decisions"], 1)
                self.assertEqual(value["selected_kan_decisions"], 1)
                self.assertEqual(value["selection_contract_violations"], 0)
                self.assertEqual(
                    value["by_kind"][kind],
                    {
                        "legal_opportunities": 1,
                        "legal_candidate_actions": 1,
                        "legal_opportunities_with_winning_action": 0,
                        "eligible_no_win_opportunities": 1,
                        "selected": 1,
                    },
                )

    def test_multiple_kan_candidates_are_accounted(self):
        decision = decision_with(
            discard_action(),
            ankan_action(4),
            ankan_action(6),
            kakan_action(5),
            daiminkan_action(3),
        )
        diagnostic = _observe(decision)
        value = diagnostic.diagnostic_value()
        self.assertEqual(value["multiple_kan_candidate_decisions"], 1)
        self.assertEqual(value["multiple_kan_kind_decisions"], 1)
        self.assertEqual(value["by_kind"]["ankan"]["legal_candidate_actions"], 2)
        self.assertEqual(value["by_kind"]["kakan"]["legal_candidate_actions"], 1)
        self.assertEqual(value["by_kind"]["daiminkan"]["legal_candidate_actions"], 1)
        self.assertEqual(value["selected_kan_decisions"], 1)
        record = diagnostic.records[0]
        self.assertEqual(record.selected_kind, "daiminkan")
        self.assertEqual(len(record.candidates), 4)

    def test_legal_action_input_order_does_not_change_the_diagnostic(self):
        actions = (
            discard_action(),
            ankan_action(4),
            kakan_action(5),
            daiminkan_action(3),
        )
        first = _observe(decision_with(*actions)).diagnostic_value()
        second = _observe(decision_with(*reversed(actions))).diagnostic_value()
        self.assertEqual(first, second)

    def test_decisions_without_a_kan_candidate_are_counted_but_not_recorded(self):
        class _FirstLegalPolicy:
            """kanの無いdecisionでも実手牌を必要としないstub delegate。"""

            def choose_action(self, decision):
                return decision.legal_actions[0]

        diagnostic = _observe(
            decision_with(discard_action(), discard_action(2)),
            decision_with(ankan_action(), discard_action()),
            policy=_FirstLegalPolicy,
        )
        self.assertEqual(diagnostic.total_decisions, 2)
        self.assertEqual(len(diagnostic.records), 1)
        self.assertEqual(diagnostic.records[0].decision_index, 1)

    def test_a_policy_that_skips_an_eligible_kan_is_a_contract_violation(self):
        class _DiscardOnlyPolicy:
            def choose_action(self, decision):
                return next(
                    action
                    for action in decision.legal_actions
                    if type(action).__name__ == "DiscardAction"
                )

        diagnostic = _observe(
            decision_with(ankan_action(), discard_action()), policy=_DiscardOnlyPolicy
        )
        value = diagnostic.diagnostic_value()
        self.assertEqual(value["eligible_no_win_opportunity_decisions"], 1)
        self.assertEqual(value["selected_kan_decisions"], 0)
        self.assertEqual(value["selection_contract_violations"], 1)
        self.assertEqual(value["by_kind"]["ankan"]["selected"], 0)

    def test_repeated_instances_of_one_seat_must_agree(self):
        observer = KanOpportunityObserver()
        wrapped = observer.wrap_factories_by_seed(
            {306: {Seat.EAST: KanCoverageYakuhaiCallPolicy}}
        )
        wrapped[306][Seat.EAST]().choose_action(
            decision_with(ankan_action(), discard_action())
        )
        wrapped[306][Seat.EAST]().choose_action(
            decision_with(kakan_action(), discard_action())
        )
        with self.assertRaises(KanOpportunityError):
            observer.resolve()

    def test_repeated_identical_passes_resolve_to_one_canonical_pass(self):
        observer = KanOpportunityObserver()
        wrapped = observer.wrap_factories_by_seed(
            {306: {Seat.EAST: KanCoverageYakuhaiCallPolicy}}
        )
        for _ in range(2):
            wrapped[306][Seat.EAST]().choose_action(
                decision_with(ankan_action(), discard_action())
            )
        diagnostic = observer.resolve()
        self.assertEqual(diagnostic.passes_per_seat, (2,))
        self.assertEqual(len(diagnostic.records), 1)
        self.assertEqual(diagnostic.total_decisions, 1)

    def test_an_observed_policy_returns_the_delegate_action_unchanged(self):
        decision = decision_with(ankan_action(), discard_action())
        observer = KanOpportunityObserver()
        wrapped = observer.wrap_factories_by_seed(
            {306: {Seat.EAST: KanCoverageYakuhaiCallPolicy}}
        )
        observed = wrapped[306][Seat.EAST]().choose_action(decision)
        direct = KanCoverageYakuhaiCallPolicy().choose_action(decision)
        self.assertEqual(observed, direct)
        self.assertIn(observed, decision.legal_actions)


class KanEvidenceAccountingTest(unittest.TestCase):
    def _classify(self, stream, **kwargs):
        return classify_selected_kan(stream, 0, **kwargs)

    def test_confirmed_declared_kan_binds_to_its_rinshan_draw(self):
        for kind, meld_type in (
            ("ankan", PublicMeldType.ANKAN),
            ("kakan", PublicMeldType.KAKAN),
        ):
            with self.subTest(kind=kind):
                outcome, _detail, expected, observed = self._classify(
                    declared_kan_stream(meld_type),
                    kind=kind,
                    actor=Seat.EAST,
                    target=None,
                )
                self.assertEqual(outcome, CONFIRMED)
                self.assertTrue(expected)
                self.assertTrue(observed)

    def test_chankan_ron_is_an_explicit_non_confirm_path(self):
        outcome, detail, expected, observed = self._classify(
            declared_kan_stream(PublicMeldType.KAKAN, chankan_ron=True),
            kind="kakan",
            actor=Seat.EAST,
            target=None,
        )
        self.assertEqual(outcome, NON_CONFIRM)
        self.assertIn("ron", detail)
        self.assertFalse(expected)
        self.assertFalse(observed)

    def test_four_kans_abortive_draw_is_not_a_missing_rinshan(self):
        outcome, detail, expected, observed = self._classify(
            declared_kan_stream(PublicMeldType.ANKAN, abortive_after_confirm=True),
            kind="ankan",
            actor=Seat.EAST,
            target=None,
        )
        self.assertEqual(outcome, CONFIRMED)
        self.assertIn("four_kans", detail)
        self.assertFalse(expected)
        self.assertFalse(observed)

    def test_a_confirmed_kan_without_a_rinshan_draw_is_reported_as_missing(self):
        outcome, _detail, expected, observed = self._classify(
            declared_kan_stream(PublicMeldType.ANKAN, rinshan=False)
            + (
                DrawEvidence(
                    Seat.SOUTH,
                    __import__(
                        "lisjong_engine.round_event", fromlist=["DrawSource"]
                    ).DrawSource.LIVE_WALL,
                ),
            ),
            kind="ankan",
            actor=Seat.EAST,
            target=None,
        )
        self.assertEqual(outcome, CONFIRMED)
        self.assertTrue(expected)
        self.assertFalse(observed)

    def test_daiminkan_confirms_through_its_called_meld(self):
        outcome, _detail, expected, observed = self._classify(
            daiminkan_stream(), kind="daiminkan", actor=Seat.EAST, target=Seat.SOUTH
        )
        self.assertEqual(outcome, CONFIRMED)
        self.assertTrue(expected)
        self.assertTrue(observed)

    def test_daiminkan_lost_to_a_ron_is_an_explicit_non_confirm_path(self):
        outcome, detail, _expected, _observed = self._classify(
            daiminkan_stream(ron=True),
            kind="daiminkan",
            actor=Seat.EAST,
            target=Seat.SOUTH,
        )
        self.assertEqual(outcome, NON_CONFIRM)
        self.assertIn("ron", detail)

    def test_daiminkan_lost_to_another_call_is_an_explicit_non_confirm_path(self):
        outcome, detail, _expected, _observed = self._classify(
            daiminkan_stream(called_by=Seat.WEST),
            kind="daiminkan",
            actor=Seat.EAST,
            target=Seat.SOUTH,
        )
        self.assertEqual(outcome, NON_CONFIRM)
        self.assertIn("another seat", detail)

    def test_an_unresolved_selected_kan_is_unaccounted(self):
        for kind, target in (("ankan", None), ("daiminkan", Seat.SOUTH)):
            with self.subTest(kind=kind):
                outcome, _detail, _expected, _observed = self._classify(
                    (), kind=kind, actor=Seat.EAST, target=target
                )
                self.assertEqual(outcome, UNACCOUNTED)

    def test_a_confirmation_without_its_declaration_is_unaccounted(self):
        stream = declared_kan_stream(PublicMeldType.ANKAN)[2:]
        outcome, _detail, _expected, _observed = self._classify(
            stream, kind="ankan", actor=Seat.EAST, target=None
        )
        self.assertEqual(outcome, UNACCOUNTED)

    def test_a_terminal_round_without_a_declaration_is_unaccounted(self):
        outcome, _detail, _expected, _observed = self._classify(
            (RoundEndedEvidence(kind=RoundEndKind.EXHAUSTIVE_DRAW),),
            kind="kakan",
            actor=Seat.EAST,
            target=None,
        )
        self.assertEqual(outcome, UNACCOUNTED)

    def test_a_daiminkan_account_requires_its_target_seat(self):
        with self.assertRaises(KanAccountingError):
            self._classify(
                daiminkan_stream(), kind="daiminkan", actor=Seat.EAST, target=None
            )

    def test_an_unknown_kan_kind_fails_closed(self):
        with self.assertRaises(KanAccountingError):
            self._classify((), kind="pon", actor=Seat.EAST, target=None)

    def test_decision_count_mismatch_is_not_silently_accepted(self):
        diagnostic = _observe(decision_with(ankan_action(), discard_action()))
        with self.assertRaises(KanAccountingError):
            account_selected_kans(kan_coverage_corpus(), diagnostic)


class KanCoverageDatasetTest(unittest.TestCase):
    def test_kan_containing_games_survive_the_dataset_build(self):
        with tempfile.TemporaryDirectory() as directory:
            persisted_raw, dataset = kan_coverage_artifacts(Path(directory))
        inventory = kan_event_inventory(persisted_raw.corpus)
        self.assertTrue(any(row.kind == "ankan" for row in inventory))
        self.assertTrue(any(row.kind == "rinshan_draw" for row in inventory))
        retention = dataset_retention_value(dataset, inventory)
        self.assertEqual(retention["kan_containing_games_retained"], PILOT_HANCHAN)
        self.assertEqual(retention["kan_containing_games_dropped"], 0)
        self.assertEqual(retention["dataset_game_seeds"], list(ORDERED_SEEDS))
        self.assertTrue(
            all(
                count > 0
                for count in retention["anchors_by_kan_containing_game"].values()
            )
        )

    def test_a_dropped_kan_containing_game_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            _persisted_raw, dataset = kan_coverage_artifacts(Path(directory))
        inventory = (KanEventRow(999_999, 0, Seat.EAST, "ankan"),)
        with self.assertRaises(KanCoverageGenerationError):
            dataset_retention_value(dataset, inventory)

    def test_the_dataset_keeps_the_whole_hanchan_split_without_a_test_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            _persisted_raw, dataset = kan_coverage_artifacts(Path(directory))
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
        self.assertNotIn(
            DatasetPartition.TEST, {value.partition for value in dataset.games}
        )

    def test_kan_containing_anchors_keep_the_player_safe_boundary(self):
        from lisjong_arena.phase5_belief_dataset.builder import (
            resolve_training_samples,
        )
        from lisjong_arena.phase8_sequential.data import (
            materialize_development_examples,
        )

        with tempfile.TemporaryDirectory() as directory:
            persisted_raw, dataset = kan_coverage_artifacts(Path(directory))
            samples = resolve_training_samples(dataset, persisted_raw)
        examples = materialize_development_examples(dataset.examples, samples)
        self.assertEqual(len(examples), PILOT_HANCHAN)
        for value in examples:
            anchor = value.sample.anchor
            for evidence in anchor.evidence:
                if isinstance(evidence, DrawEvidence):
                    if evidence.seat is not anchor.viewer_seat:
                        self.assertIsNone(evidence.tile)
            for row in value.sample.labels.expected_counts:
                self.assertEqual(sum(row.counts), row.concealed_size)


class KanCoverageManifestTest(unittest.TestCase):
    def test_a_well_formed_manifest_validates(self):
        self.assertIsInstance(
            validate_population_manifest(population_manifest_value()), dict
        )

    def test_a_tampered_population_plan_fails_closed(self):
        manifest = population_manifest_value()
        manifest["population_plan"]["ordered_seeds"] = list(range(180, 192))
        with self.assertRaises(KanCoverageGenerationError):
            validate_population_manifest(manifest)

    def test_a_self_consistent_but_foreign_plan_fails_closed(self):
        import hashlib

        manifest = population_manifest_value()
        manifest["population_plan"]["population_id"] = "B"
        manifest["population_identity"] = hashlib.sha256(
            canonical_json_bytes(manifest["population_plan"])
        ).hexdigest()
        with self.assertRaises(KanCoverageGenerationError):
            validate_population_manifest(manifest)

    def test_unresolved_provenance_fails_closed(self):
        with self.assertRaises(KanCoverageGenerationError):
            validate_population_manifest(
                population_manifest_value(fully_resolved=False)
            )

    def test_a_different_hanchan_count_fails_closed(self):
        with self.assertRaises(KanCoverageGenerationError):
            validate_population_manifest(population_manifest_value(hanchan=36))

    def test_a_missing_diagnostic_section_fails_closed(self):
        manifest = population_manifest_value()
        del manifest["kan_accounting"]
        with self.assertRaises(KanCoverageGenerationError):
            validate_population_manifest(manifest)


class KanCoverageResultTest(unittest.TestCase):
    def test_a_complete_pilot_is_qualified(self):
        outcome, reasons = classify(population_manifest_value())
        self.assertEqual(outcome, QUALIFIED)
        self.assertTrue(reasons)

    def test_a_zero_opportunity_kind_is_unmeasured_not_a_failure(self):
        manifest = population_manifest_value()
        diagnostic = manifest["kan_opportunity_diagnostic"]
        self.assertEqual(kind_interpretation(diagnostic, "kakan"), UNMEASURED)
        self.assertEqual(kind_interpretation(diagnostic, "ankan"), OBSERVED)
        self.assertEqual(classify(manifest)[0], QUALIFIED)

    def test_an_unconverted_eligible_opportunity_is_a_contract_violation(self):
        diagnostic = kan_opportunity_diagnostic_value(kakan=(3, 0), violations=3)
        self.assertEqual(kind_interpretation(diagnostic, "kakan"), CONTRACT_VIOLATION)
        manifest = population_manifest_value()
        manifest["kan_opportunity_diagnostic"] = diagnostic
        self.assertEqual(classify(manifest)[0], ACCOUNTING_REFORMULATE)

    def test_an_unaccounted_selected_kan_reformulates_the_accounting(self):
        manifest = population_manifest_value()
        manifest["kan_accounting"]["totals"] = kan_accounting_totals(unaccounted=1)
        self.assertEqual(classify(manifest)[0], ACCOUNTING_REFORMULATE)

    def test_a_missing_rinshan_reformulates_the_accounting(self):
        manifest = population_manifest_value()
        manifest["kan_accounting"]["totals"] = kan_accounting_totals(rinshan_missing=1)
        self.assertEqual(classify(manifest)[0], ACCOUNTING_REFORMULATE)

    def test_no_observed_opportunity_is_empirically_insufficient(self):
        manifest = population_manifest_value()
        manifest["kan_opportunity_diagnostic"] = kan_opportunity_diagnostic_value(
            daiminkan=(0, 0), ankan=(0, 0), kakan=(0, 0)
        )
        manifest["kan_accounting"]["totals"] = kan_accounting_totals(
            selected=0, confirmed=0
        )
        manifest["coverage"]["events"]["rinshan_draw"] = 0
        self.assertEqual(classify(manifest)[0], INSUFFICIENT)

    def test_unresolved_provenance_is_stop_invalid(self):
        self.assertEqual(
            classify(population_manifest_value(fully_resolved=False))[0], STOP_INVALID
        )

    def test_a_dropped_kan_game_is_stop_invalid(self):
        manifest = population_manifest_value()
        manifest["dataset_retention"]["kan_containing_games_dropped"] = 1
        self.assertEqual(classify(manifest)[0], STOP_INVALID)

    def test_result_round_trips_through_canonical_json(self):
        value = result_value(population_manifest_value())
        with tempfile.TemporaryDirectory() as directory:
            path = save_result(directory, value)
            self.assertEqual(json.loads(path.read_bytes())["outcome"], value["outcome"])
            self.assertEqual(load_result(directory), value)
            with self.assertRaises(FileExistsError):
                save_result(directory, value)

    def test_an_unknown_outcome_fails_closed(self):
        value = result_value(population_manifest_value())
        value["outcome"] = "PROBABLY FINE"
        with self.assertRaises(KanCoverageResultError):
            validate_result_value(value)

    def test_a_qualified_result_cannot_carry_a_contract_violation(self):
        value = result_value(population_manifest_value())
        value["kind_interpretation"]["kakan"] = CONTRACT_VIOLATION
        with self.assertRaises(KanCoverageResultError):
            validate_result_value(value)

    def test_a_malformed_result_fails_closed(self):
        with self.assertRaises(KanCoverageResultError):
            validate_result_value([])
        value = result_value(population_manifest_value())
        del value["coverage"]
        with self.assertRaises(KanCoverageResultError):
            validate_result_value(value)

    def test_the_result_keeps_the_next_step_boundary(self):
        value = result_value(population_manifest_value())
        self.assertIn(
            "not a final training population lock", value["next_step_boundary"]
        )
        self.assertNotIn("kan_opportunity_records", value["kan_opportunity_summary"])


if __name__ == "__main__":
    unittest.main()
