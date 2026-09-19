"""Issue #250 Overall Champion AABB half-game protocol v1 contract tests.

CIでactual 400-hanchan formal evaluationも実RiichiEnvの半荘も実行しない。
raw seat rowsとartifactはすべてsyntheticに合成する。
"""

from __future__ import annotations

import dataclasses
import unittest
from collections.abc import Mapping
from unittest import mock

from _overall_champion_aabb_fixtures import (
    HEURISTIC_ADVANTAGE,
    HEURISTIC_IDENTITY,
    LEARNING_ADVANTAGE,
    LEARNING_IDENTITY,
    TIE,
    alternating_profile,
    comparison_artifact,
    game_ranks,
    heuristic_binding,
    learning_binding,
    other_learning_policy,
    seat_results,
    seeds,
    stub_heuristic_policy,
    tampered_comparison_artifact,
    uniform_profile,
)
from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

import lisjong_arena.comparison as comparison_module
from lisjong_arena.comparison import ROTATION_COUNT as GENERIC_ROTATION_COUNT
from lisjong_arena.comparison import aggregate_policy_metrics, run_comparison
from lisjong_arena.model import ComparisonPlan, PolicySpec, SeatResult
from lisjong_arena.overall_champion_aabb.protocol import (
    CLASSIFICATION_LABELS,
    FAMILY_SEAT_EXPOSURE_PER_BLOCK,
    FAMILY_SEAT_RESULTS_PER_BLOCK,
    GAME_MODE,
    HANCHAN_COUNT,
    HEURISTIC_SLOT,
    HEURISTIC_SUPERIOR_LABEL,
    INCONCLUSIVE_LABEL,
    INTERVAL_Z,
    LEARNING_SLOT,
    LEARNING_SUPERIOR_LABEL,
    MAX_STEPS,
    PROTOCOL_ID,
    ROTATION_COUNT,
    ROTATION_PLAN,
    SEAT_RESULT_COUNT,
    SEED_BLOCK_COUNT,
    STOP_INVALID_LABEL,
    OverallChampionProtocolError,
    ParticipantBinding,
    factory_binding_of,
    protocol_document,
    require_overall_population,
    require_participants,
    require_protocol_document,
)
from lisjong_arena.overall_champion_aabb.statistics import (
    OverallChampionStatisticsError,
    OverallSeedBlock,
    block_sign_counts,
    classify,
    derive_secondary_diagnostics,
    derive_seed_blocks,
    summarize_seed_blocks,
)
from lisjong_arena.paired_evaluation import PairedSummary
from lisjong_arena.riichienv.local_game_runner import LocalGameResult

SEEDS = seeds()
_SCORE_BY_RANK = {1: 40_000, 2: 30_000, 3: 20_000, 4: 10_000}


def _blocks(profile_of=None, population: tuple[int, ...] = SEEDS):
    return derive_seed_blocks(
        comparison_artifact(
            population, profile_of or uniform_profile(HEURISTIC_ADVANTAGE)
        ),
        heuristic=heuristic_binding(),
        learning=learning_binding(),
        seeds=population,
    )


class ProtocolInvariantTest(unittest.TestCase):
    def test_protocol_identity_and_v1_budget_are_fixed(self) -> None:
        self.assertEqual(PROTOCOL_ID, "arena-overall-champion-aabb-half-v1")
        self.assertEqual(GAME_MODE, "4p-red-half")
        self.assertEqual(SEED_BLOCK_COUNT, 100)
        self.assertEqual(ROTATION_COUNT, 4)
        self.assertEqual(HANCHAN_COUNT, 400)
        self.assertEqual(SEAT_RESULT_COUNT, 1_600)
        self.assertEqual(FAMILY_SEAT_RESULTS_PER_BLOCK, 8)
        self.assertEqual(FAMILY_SEAT_EXPOSURE_PER_BLOCK, 2)
        self.assertEqual(INTERVAL_Z, 1.96)

    def test_v1_rotation_plan_matches_the_generic_execution_substrate(self) -> None:
        """Arena-owned v1 planは既存generic comparisonの実挙動と一致する。"""
        self.assertEqual(ROTATION_COUNT, GENERIC_ROTATION_COUNT)
        observed: list[tuple[str, str, str, str]] = []

        def fake_game(
            policies: Mapping[Seat, object],
            *,
            seed: int,
            game_mode: str,
            max_steps: int,
        ) -> LocalGameResult:
            observed.append(
                tuple(policies[seat].identity for seat in Seat)  # type: ignore[union-attr]
            )
            scores = (40_000, 30_000, 20_000, 10_000)
            return LocalGameResult(
                seed=seed,
                game_mode=game_mode,
                scores=scores,
                ranks=(1, 2, 3, 4),
                steps=1,
                decisions=4,
                seat_round_stats=neutral_seat_round_stats_tuple(scores),
            )

        class _Slot:
            def __init__(self, identity: str) -> None:
                self.identity = identity

            def choose_action(self, decision: object) -> object:
                raise AssertionError("rotation binding test must not execute policies")

        plan = ComparisonPlan(
            policy_a=PolicySpec(
                identity=HEURISTIC_SLOT, factory=lambda: _Slot(HEURISTIC_SLOT)
            ),
            policy_b=PolicySpec(
                identity=LEARNING_SLOT, factory=lambda: _Slot(LEARNING_SLOT)
            ),
            seeds=(7,),
            game_mode=GAME_MODE,
            max_steps=MAX_STEPS,
        )
        with mock.patch.object(comparison_module, "_run_single_game", fake_game):
            run_comparison(plan)
        self.assertEqual(tuple(observed), ROTATION_PLAN)

    def test_four_rotations_give_both_families_exact_symmetric_seat_exposure(
        self,
    ) -> None:
        heuristic_seats = [0, 0, 0, 0]
        learning_seats = [0, 0, 0, 0]
        for rotation in range(ROTATION_COUNT):
            for seat, slot in enumerate(ROTATION_PLAN[rotation]):
                if slot == HEURISTIC_SLOT:
                    heuristic_seats[seat] += 1
                else:
                    learning_seats[seat] += 1
        self.assertEqual(heuristic_seats, [FAMILY_SEAT_EXPOSURE_PER_BLOCK] * 4)
        self.assertEqual(learning_seats, [FAMILY_SEAT_EXPOSURE_PER_BLOCK] * 4)

    def test_formal_population_requires_exactly_100_unique_ordered_seeds(self) -> None:
        self.assertEqual(require_overall_population(SEEDS), SEEDS)
        with self.assertRaises(OverallChampionProtocolError):
            require_overall_population(SEEDS[:-1])
        with self.assertRaises(OverallChampionProtocolError):
            require_overall_population(SEEDS + (SEEDS[-1] + 1,))
        duplicated = SEEDS[:-1] + (SEEDS[0],)
        with self.assertRaises(OverallChampionProtocolError):
            require_overall_population(duplicated)
        with self.assertRaises(TypeError):
            require_overall_population(set(SEEDS))
        with self.assertRaises(TypeError):
            require_overall_population(("90000",) + SEEDS[1:])

    def test_seed_order_is_part_of_the_population_identity(self) -> None:
        reordered = (SEEDS[1], SEEDS[0]) + SEEDS[2:]
        self.assertNotEqual(require_overall_population(reordered), SEEDS)

    def test_exact_seed_values_are_not_hard_coded_in_the_protocol(self) -> None:
        elsewhere = seeds(start=123_456)
        self.assertEqual(require_overall_population(elsewhere), elsewhere)
        self.assertEqual(protocol_document(elsewhere)["ordered_seeds"], list(elsewhere))

    def test_participant_binding_requires_exact_family_and_identity(self) -> None:
        require_participants(heuristic_binding(), learning_binding())
        with self.assertRaises(OverallChampionProtocolError):
            require_participants(learning_binding(), learning_binding())
        with self.assertRaises(OverallChampionProtocolError):
            require_participants(heuristic_binding(), heuristic_binding())
        with self.assertRaises(OverallChampionProtocolError):
            ParticipantBinding(
                family="hybrid",
                policy_identity="x",
                factory_binding="m:f",
                implementation_source="lisjong",
                implementation_revision="a" * 40,
            )
        with self.assertRaisesRegex(
            OverallChampionProtocolError, "implementation_source"
        ):
            ParticipantBinding(
                family="heuristic",
                policy_identity="x",
                factory_binding="m:f",
                implementation_source="somewhere-else",
                implementation_revision="a" * 40,
            )

    def test_same_identity_on_both_sides_is_rejected(self) -> None:
        with self.assertRaises(OverallChampionProtocolError):
            require_participants(
                heuristic_binding(policy_identity="same"),
                learning_binding(policy_identity="same"),
            )

    def test_binding_rejects_malformed_revision_and_checkpoint(self) -> None:
        with self.assertRaises(OverallChampionProtocolError):
            heuristic_binding(implementation_revision="ABC")
        with self.assertRaises(OverallChampionProtocolError):
            learning_binding(checkpoint_digest="0" * 63)
        with self.assertRaises(OverallChampionProtocolError):
            learning_binding(checkpoint_identity=None, checkpoint_digest="f" * 64)
        with self.assertRaises(OverallChampionProtocolError):
            heuristic_binding(factory_binding="not-a-binding")

    def test_factory_binding_of_names_the_exact_callable(self) -> None:
        self.assertEqual(
            factory_binding_of(stub_heuristic_policy),
            "_overall_champion_aabb_fixtures:stub_heuristic_policy",
        )
        self.assertNotEqual(
            factory_binding_of(other_learning_policy),
            factory_binding_of(stub_heuristic_policy),
        )
        with self.assertRaises(OverallChampionProtocolError):
            factory_binding_of(lambda: None)

    def test_protocol_document_round_trip_rejects_drift(self) -> None:
        document = protocol_document(SEEDS)
        self.assertEqual(require_protocol_document(document, "protocol"), SEEDS)
        drifted = dict(document)
        drifted["game_mode"] = "4p-red-east"
        with self.assertRaises(OverallChampionProtocolError):
            require_protocol_document(drifted, "protocol")
        missing = {key: value for key, value in document.items() if key != "max_steps"}
        with self.assertRaises(OverallChampionProtocolError):
            require_protocol_document(missing, "protocol")
        extra = dict(document)
        extra["unexpected"] = 1
        with self.assertRaises(OverallChampionProtocolError):
            require_protocol_document(extra, "protocol")

    def test_classification_label_table_is_exhaustive(self) -> None:
        self.assertEqual(
            set(CLASSIFICATION_LABELS),
            {
                "HEURISTIC_CHAMPION_SUPERIOR",
                "LEARNING_CHAMPION_SUPERIOR",
                "OVERALL_INCONCLUSIVE",
                "STOP_INVALID",
            },
        )
        self.assertEqual(
            set(CLASSIFICATION_LABELS.values()),
            {
                HEURISTIC_SUPERIOR_LABEL,
                LEARNING_SUPERIOR_LABEL,
                INCONCLUSIVE_LABEL,
                STOP_INVALID_LABEL,
            },
        )

    def test_classification_labels_are_exhaustive(self) -> None:
        document = protocol_document(SEEDS)
        classification = document["classification"]
        assert isinstance(classification, dict)
        self.assertIs(classification["secondary_metrics_override_primary"], False)
        self.assertIn("STOP / INVALID", classification["rule"])
        self.assertEqual(STOP_INVALID_LABEL, "STOP / INVALID")


class SeedBlockStatisticsTest(unittest.TestCase):
    def test_clear_heuristic_advantage(self) -> None:
        blocks = _blocks(uniform_profile(HEURISTIC_ADVANTAGE))
        self.assertEqual(len(blocks), SEED_BLOCK_COUNT)
        self.assertEqual(blocks[0].heuristic_mean_rank, 1.5)
        self.assertEqual(blocks[0].learning_mean_rank, 3.5)
        self.assertEqual(blocks[0].delta, 2.0)
        summary = summarize_seed_blocks(blocks)
        self.assertEqual(summary.mean_delta, 2.0)
        self.assertGreater(summary.interval_lower, 0.0)
        self.assertEqual(classify(summary)["label"], HEURISTIC_SUPERIOR_LABEL)
        self.assertEqual(
            block_sign_counts(blocks),
            {
                "negative_block_count": 0,
                "positive_block_count": SEED_BLOCK_COUNT,
                "zero_block_count": 0,
            },
        )

    def test_clear_learning_advantage(self) -> None:
        blocks = _blocks(uniform_profile(LEARNING_ADVANTAGE))
        self.assertEqual(blocks[0].delta, -2.0)
        summary = summarize_seed_blocks(blocks)
        self.assertLess(summary.interval_upper, 0.0)
        self.assertEqual(classify(summary)["label"], LEARNING_SUPERIOR_LABEL)
        self.assertEqual(block_sign_counts(blocks)["negative_block_count"], 100)

    def test_exact_tie(self) -> None:
        blocks = _blocks(uniform_profile(TIE))
        self.assertEqual(blocks[0].heuristic_mean_rank, 2.5)
        self.assertEqual(blocks[0].learning_mean_rank, 2.5)
        self.assertEqual(blocks[0].delta, 0.0)
        summary = summarize_seed_blocks(blocks)
        self.assertEqual(summary.mean_delta, 0.0)
        self.assertEqual(summary.interval_lower, 0.0)
        self.assertEqual(summary.interval_upper, 0.0)
        self.assertEqual(classify(summary)["label"], INCONCLUSIVE_LABEL)
        self.assertEqual(block_sign_counts(blocks)["zero_block_count"], 100)

    def test_mixed_evidence_is_inconclusive(self) -> None:
        blocks = _blocks(alternating_profile(SEEDS))
        summary = summarize_seed_blocks(blocks)
        self.assertEqual(summary.mean_delta, 0.0)
        self.assertLess(summary.interval_lower, 0.0)
        self.assertGreater(summary.interval_upper, 0.0)
        self.assertEqual(classify(summary)["label"], INCONCLUSIVE_LABEL)
        self.assertEqual(
            block_sign_counts(blocks),
            {
                "negative_block_count": 50,
                "positive_block_count": 50,
                "zero_block_count": 0,
            },
        )

    def test_block_delta_formula_is_learning_minus_heuristic(self) -> None:
        blocks = _blocks(alternating_profile(SEEDS))
        for block in blocks:
            self.assertEqual(
                block.delta, block.learning_mean_rank - block.heuristic_mean_rank
            )

    def test_statistical_n_is_seed_blocks_not_seats_or_hanchan(self) -> None:
        blocks = _blocks(alternating_profile(SEEDS))
        summary = summarize_seed_blocks(blocks)
        self.assertEqual(summary.block_count, SEED_BLOCK_COUNT)
        self.assertNotEqual(summary.block_count, HANCHAN_COUNT)
        self.assertNotEqual(
            summary.block_count, SEED_BLOCK_COUNT * FAMILY_SEAT_RESULTS_PER_BLOCK
        )

    def test_summary_uses_sample_sd_standard_error_and_z_interval(self) -> None:
        blocks = _blocks(alternating_profile(SEEDS))
        values = [block.delta for block in blocks]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        deviation = variance**0.5
        error = deviation / len(values) ** 0.5
        summary = summarize_seed_blocks(blocks)
        self.assertEqual(summary.sample_standard_deviation, deviation)
        self.assertEqual(summary.standard_error, error)
        self.assertEqual(summary.interval_lower, mean - INTERVAL_Z * error)
        self.assertEqual(summary.interval_upper, mean + INTERVAL_Z * error)

    def test_summary_requires_exactly_the_locked_block_count(self) -> None:
        blocks = _blocks(uniform_profile(TIE))
        with self.assertRaises(OverallChampionStatisticsError):
            summarize_seed_blocks(blocks[:-1])

    def test_secondary_diagnostics_are_rederived_from_raw_rows(self) -> None:
        artifact = comparison_artifact(SEEDS, uniform_profile(HEURISTIC_ADVANTAGE))
        diagnostics = derive_secondary_diagnostics(
            artifact, heuristic=heuristic_binding(), learning=learning_binding()
        )
        self.assertEqual(diagnostics.heuristic.average_rank, 1.5)
        self.assertEqual(diagnostics.learning.average_rank, 3.5)
        self.assertEqual(diagnostics.heuristic.seat_result_count, 800)
        self.assertEqual(diagnostics.learning.seat_result_count, 800)
        self.assertEqual(diagnostics.heuristic.game_count, HANCHAN_COUNT)
        self.assertEqual(diagnostics.heuristic.first_count, 400)
        self.assertEqual(diagnostics.heuristic.second_count, 400)
        self.assertEqual(diagnostics.heuristic.third_count, 0)
        self.assertEqual(diagnostics.learning.fourth_count, 400)
        self.assertEqual(len(diagnostics.heuristic.seat_mean_ranks), 4)
        self.assertEqual(len(diagnostics.learning.seat_mean_scores), 4)
        self.assertEqual(
            diagnostics.mean_final_score_difference,
            diagnostics.heuristic.average_score - diagnostics.learning.average_score,
        )
        self.assertIs(
            diagnostics.to_document()["overrides_primary_classification"], False
        )

    def test_recorded_generic_metrics_must_agree_with_raw_rows(self) -> None:
        rows = seat_results(SEEDS, uniform_profile(HEURISTIC_ADVANTAGE))
        drifted = dataclasses.replace(
            aggregate_policy_metrics(HEURISTIC_IDENTITY, rows), average_rank=1.25
        )
        artifact = tampered_comparison_artifact(rows, metrics_a=drifted)
        with self.assertRaisesRegex(
            OverallChampionStatisticsError, "re-derived from raw seat-results"
        ):
            derive_secondary_diagnostics(
                artifact, heuristic=heuristic_binding(), learning=learning_binding()
            )


class ClassificationTest(unittest.TestCase):
    def _summary(self, lower: float, upper: float) -> PairedSummary:
        return PairedSummary(
            block_count=SEED_BLOCK_COUNT,
            mean_delta=(lower + upper) / 2.0,
            sample_standard_deviation=1.0,
            standard_error=0.1,
            interval_lower=lower,
            interval_upper=upper,
        )

    def test_interval_lower_above_zero_is_heuristic_superior(self) -> None:
        self.assertEqual(
            classify(self._summary(0.0001, 2.0))["label"], HEURISTIC_SUPERIOR_LABEL
        )

    def test_interval_upper_below_zero_is_learning_superior(self) -> None:
        self.assertEqual(
            classify(self._summary(-2.0, -0.0001))["label"], LEARNING_SUPERIOR_LABEL
        )

    def test_interval_crossing_zero_is_inconclusive(self) -> None:
        self.assertEqual(
            classify(self._summary(-0.5, 0.5))["label"], INCONCLUSIVE_LABEL
        )

    def test_interval_lower_exactly_zero_is_inconclusive(self) -> None:
        self.assertEqual(classify(self._summary(0.0, 2.0))["label"], INCONCLUSIVE_LABEL)

    def test_interval_upper_exactly_zero_is_inconclusive(self) -> None:
        self.assertEqual(
            classify(self._summary(-2.0, 0.0))["label"], INCONCLUSIVE_LABEL
        )

    def test_invalid_evidence_is_never_converted_to_a_superiority_result(self) -> None:
        with self.assertRaises(OverallChampionStatisticsError):
            classify(self._summary(float("nan"), 1.0))
        with self.assertRaises(OverallChampionStatisticsError):
            classify(self._summary(1.0, -1.0))
        with self.assertRaises(OverallChampionStatisticsError):
            classify(
                PairedSummary(
                    block_count=SEED_BLOCK_COUNT - 1,
                    mean_delta=1.0,
                    sample_standard_deviation=1.0,
                    standard_error=0.1,
                    interval_lower=0.5,
                    interval_upper=1.5,
                )
            )
        with self.assertRaises(OverallChampionStatisticsError):
            classify({"interval_lower": 1.0, "interval_upper": 2.0})

    def test_secondary_score_diagnostics_cannot_override_the_primary_rule(self) -> None:
        """rankがinconclusiveなら、scoreが一方へ振れてもINCONCLUSIVEのまま。"""
        population = seeds()
        rows = list(seat_results(population, alternating_profile(population)))
        boosted = []
        for row in rows:
            score = row.score + (
                100_000 if row.policy_identity == HEURISTIC_IDENTITY else 0
            )
            boosted.append(
                SeatResult(
                    seed=row.seed,
                    rotation=row.rotation,
                    game_mode=row.game_mode,
                    seat=row.seat,
                    policy_identity=row.policy_identity,
                    score=score,
                    rank=row.rank,
                )
            )
        artifact = comparison_artifact(population, rows=tuple(boosted))
        blocks = derive_seed_blocks(
            artifact,
            heuristic=heuristic_binding(),
            learning=learning_binding(),
            seeds=population,
        )
        diagnostics = derive_secondary_diagnostics(
            artifact, heuristic=heuristic_binding(), learning=learning_binding()
        )
        self.assertGreater(diagnostics.mean_final_score_difference, 0.0)
        self.assertEqual(
            classify(summarize_seed_blocks(blocks))["label"], INCONCLUSIVE_LABEL
        )


class RawSeatResultValidationTest(unittest.TestCase):
    def _derive(self, artifact, *, population=SEEDS, **overrides):
        bindings = {
            "heuristic": heuristic_binding(),
            "learning": learning_binding(),
        }
        bindings.update(overrides)
        return derive_seed_blocks(artifact, seeds=population, **bindings)

    def test_wrong_game_mode_is_rejected(self) -> None:
        artifact = comparison_artifact(
            SEEDS, uniform_profile(TIE), game_mode="4p-red-east"
        )
        with self.assertRaisesRegex(OverallChampionStatisticsError, "game_mode"):
            self._derive(artifact)

    def test_wrong_max_steps_is_rejected(self) -> None:
        artifact = comparison_artifact(SEEDS, uniform_profile(TIE), max_steps=999)
        with self.assertRaisesRegex(OverallChampionStatisticsError, "max_steps"):
            self._derive(artifact)

    def test_participant_identity_mismatch_is_rejected(self) -> None:
        artifact = comparison_artifact(SEEDS, uniform_profile(TIE))
        with self.assertRaisesRegex(OverallChampionStatisticsError, "Heuristic"):
            self._derive(artifact, heuristic=heuristic_binding(policy_identity="other"))
        with self.assertRaisesRegex(OverallChampionStatisticsError, "Learning"):
            self._derive(artifact, learning=learning_binding(policy_identity="other"))

    def test_swapped_families_are_rejected(self) -> None:
        artifact = comparison_artifact(
            SEEDS,
            uniform_profile(TIE),
            heuristic_identity=LEARNING_IDENTITY,
            learning_identity=HEURISTIC_IDENTITY,
        )
        with self.assertRaises(OverallChampionStatisticsError):
            self._derive(artifact)

    def test_seed_order_mismatch_is_rejected(self) -> None:
        artifact = comparison_artifact(SEEDS, uniform_profile(TIE))
        reordered = (SEEDS[1], SEEDS[0]) + SEEDS[2:]
        with self.assertRaisesRegex(OverallChampionStatisticsError, "ordered seeds"):
            self._derive(artifact, population=reordered)

    def test_partial_block_is_rejected(self) -> None:
        population = seeds(SEED_BLOCK_COUNT - 1)
        artifact = comparison_artifact(population, uniform_profile(TIE))
        with self.assertRaises(OverallChampionProtocolError):
            self._derive(artifact, population=population)

    def test_partial_game_inside_a_block_is_rejected(self) -> None:
        rows = seat_results(SEEDS, uniform_profile(TIE))
        artifact = tampered_comparison_artifact(rows[:-4])
        with self.assertRaisesRegex(OverallChampionStatisticsError, "seat-results"):
            self._derive(artifact)

    def test_wrong_rotation_seat_assignment_is_rejected(self) -> None:
        rows = list(seat_results(SEEDS, uniform_profile(TIE)))
        swapped = rows[0]
        rows[0] = SeatResult(
            seed=swapped.seed,
            rotation=swapped.rotation,
            game_mode=swapped.game_mode,
            seat=swapped.seat,
            policy_identity=LEARNING_IDENTITY,
            score=swapped.score,
            rank=swapped.rank,
        )
        other = rows[2]
        rows[2] = SeatResult(
            seed=other.seed,
            rotation=other.rotation,
            game_mode=other.game_mode,
            seat=other.seat,
            policy_identity=HEURISTIC_IDENTITY,
            score=other.score,
            rank=other.rank,
        )
        artifact = tampered_comparison_artifact(tuple(rows))
        with self.assertRaisesRegex(OverallChampionStatisticsError, "rotation plan"):
            self._derive(artifact)

    def test_malformed_block_rank_permutation_is_rejected(self) -> None:
        rows = list(seat_results(SEEDS, uniform_profile(TIE)))
        first = rows[0]
        rows[0] = SeatResult(
            seed=first.seed,
            rotation=first.rotation,
            game_mode=first.game_mode,
            seat=first.seat,
            policy_identity=first.policy_identity,
            score=first.score,
            rank=rows[1].rank,
        )
        artifact = tampered_comparison_artifact(tuple(rows))
        with self.assertRaisesRegex(OverallChampionStatisticsError, "permutation"):
            self._derive(artifact)

    def test_family_swap_inside_a_block_is_rejected(self) -> None:
        """1 rowだけfamilyを入れ替えてもrotation plan validationで落ちる。"""
        rows = list(seat_results(SEEDS, uniform_profile(TIE)))
        first = rows[0]
        rows[0] = SeatResult(
            seed=first.seed,
            rotation=first.rotation,
            game_mode=first.game_mode,
            seat=first.seat,
            policy_identity=LEARNING_IDENTITY,
            score=first.score,
            rank=first.rank,
        )
        artifact = tampered_comparison_artifact(tuple(rows))
        with self.assertRaises(OverallChampionStatisticsError):
            self._derive(artifact)

    def test_non_artifact_input_is_rejected(self) -> None:
        with self.assertRaises(OverallChampionStatisticsError):
            self._derive({"seat_results": []})


class SharedPairedLayerBoundaryTest(unittest.TestCase):
    """#268 reusable layerへOverall固有contractを逆流させていないこと。"""

    def test_paired_evaluation_stays_neutral(self) -> None:
        import lisjong_arena.paired_evaluation as paired

        source = set(paired.__all__)
        for name in (
            "heuristic",
            "learning",
            "HEURISTIC",
            "LEARNING",
            "overall",
            "champion",
        ):
            self.assertFalse(
                any(name in entry for entry in source),
                f"{name!r} leaked into the neutral paired layer",
            )
        self.assertNotIn("SEED_BLOCK_COUNT", source)

    def test_overall_reuses_shared_summary_mechanics(self) -> None:
        from lisjong_arena.paired_evaluation import summarize_paired_deltas

        blocks = _blocks(alternating_profile(SEEDS))
        self.assertEqual(
            summarize_seed_blocks(blocks),
            summarize_paired_deltas(tuple(block.to_paired_delta() for block in blocks)),
        )

    def test_block_projection_preserves_the_overall_sign_convention(self) -> None:
        block = OverallSeedBlock(
            seed=1,
            heuristic_mean_rank=2.0,
            learning_mean_rank=3.0,
            delta=1.0,
        )
        paired = block.to_paired_delta()
        self.assertEqual(paired.candidate_mean, block.learning_mean_rank)
        self.assertEqual(paired.parent_mean, block.heuristic_mean_rank)
        self.assertEqual(paired.delta, 1.0)


class GenericSubstrateRegressionTest(unittest.TestCase):
    def test_generic_comparison_plan_defaults_are_unchanged(self) -> None:
        plan = ComparisonPlan(
            policy_a=PolicySpec(identity="a", factory=stub_heuristic_policy),
            policy_b=PolicySpec(identity="b", factory=other_learning_policy),
            seeds=(1, 2),
        )
        self.assertEqual(plan.game_mode, "4p-red-half")
        self.assertEqual(plan.max_steps, 10_000)
        self.assertEqual(plan.seeds, (1, 2))

    def test_generic_artifact_accepts_the_v1_rotation_plan(self) -> None:
        artifact = comparison_artifact(seeds(2), uniform_profile(TIE))
        self.assertEqual(artifact.plan.game_mode, GAME_MODE)
        self.assertEqual(len(artifact.seat_results), 2 * ROTATION_COUNT * 4)

    def test_generic_artifact_rejects_a_plan_that_is_not_the_v1_rotation(self) -> None:
        population = seeds(2)
        rows = list(seat_results(population, uniform_profile(TIE)))
        first = rows[0]
        rows[0] = SeatResult(
            seed=first.seed,
            rotation=first.rotation,
            game_mode=first.game_mode,
            seat=first.seat,
            policy_identity=LEARNING_IDENTITY,
            score=first.score,
            rank=first.rank,
        )
        with self.assertRaises(ValueError):
            comparison_artifact(population, rows=tuple(rows))

    def test_single_round_abbb_protocol_is_untouched(self) -> None:
        from lisjong_arena.model import (
            SINGLE_ROUND_GAME_MODE,
            SINGLE_ROUND_ROTATION_COUNT,
        )

        self.assertEqual(SINGLE_ROUND_GAME_MODE, "4p-red-single")
        self.assertEqual(SINGLE_ROUND_ROTATION_COUNT, 4)
        self.assertNotEqual(SINGLE_ROUND_GAME_MODE, GAME_MODE)

    def test_fixture_rank_plan_is_a_permutation_per_hanchan(self) -> None:
        for profile in (HEURISTIC_ADVANTAGE, LEARNING_ADVANTAGE, TIE):
            for rotation in range(ROTATION_COUNT):
                self.assertEqual(
                    sorted(game_ranks(rotation, profile)), [1, 2, 3, 4], profile
                )
                for seat, rank in enumerate(game_ranks(rotation, profile)):
                    self.assertIn(_SCORE_BY_RANK[rank], _SCORE_BY_RANK.values())
                    self.assertIsInstance(seat, int)


if __name__ == "__main__":
    unittest.main()
