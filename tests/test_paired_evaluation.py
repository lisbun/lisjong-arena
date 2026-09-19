"""neutral paired-evaluation mechanicsのunit test。

実RiichiEnvを起動せず、既存``SingleRoundGameResult`` /
``SingleRoundStrengthArtifact``契約を満たす合成raw resultだけを使う。ここで
固定するのはmechanicsだけであり、どのseed populationが正しいか、intervalを
どのlabelへ写像するかはpurpose-specific experiment側のtestが所有する。
"""

import ast
import hashlib
import math
import tempfile
import unittest
from pathlib import Path

import _single_round_artifact_fixtures as artifact_fixtures
from _round_stats_fixtures import neutral_seat_round_stats_tuple
from lisjong.policy_contract import Seat

from lisjong_arena import paired_evaluation
from lisjong_arena.model import SINGLE_ROUND_ROTATION_COUNT, SingleRoundGameResult
from lisjong_arena.paired_evaluation import (
    INTERVAL_Z,
    SEED_BLOCK_ROTATION_COUNT,
    PairedEvaluationError,
    PairedSeedDelta,
    PairedSummary,
    arm_diagnostics,
    artifact_file_digest,
    focal_seed_block_means,
    load_arm_artifact,
    paired_deltas_from_block_means,
    summarize_paired_deltas,
)
from lisjong_arena.single_round_artifact import SingleRoundArtifactError

FORBIDDEN_IMPORT_ROOTS = (
    "lisjong_arena.progression_development",
    "lisjong_arena.targeted_honor_release_development",
    "lisjong_arena.targeted_honor_release_confirmation",
    "lisjong_arena.offensive_efficiency_diagnostic",
)


def game_result(
    seed: int,
    rotation: int,
    focal_score: int,
    *,
    focal_seat: int | None = None,
) -> SingleRoundGameResult:
    """focal seat scoreを直接指定できる1件のraw record。"""
    others = (100_000 - focal_score) // 3
    remainder = (100_000 - focal_score) - 2 * others
    remaining = [others, others, remainder]
    seat_of_focal = rotation if focal_seat is None else focal_seat
    scores = tuple(
        focal_score if seat == seat_of_focal else remaining.pop() for seat in range(4)
    )
    return SingleRoundGameResult(
        seed=seed,
        rotation=rotation,
        game_mode="4p-red-single",
        candidate_seat=Seat(seat_of_focal),
        scores=scores,
        seat_round_stats=neutral_seat_round_stats_tuple(scores),
    )


def seed_block(
    seed: int, focal_scores: tuple[int, int, int, int]
) -> tuple[SingleRoundGameResult, ...]:
    """rotation 0..3のcanonicalな1 seed block。"""
    return tuple(
        game_result(seed, rotation, focal_scores[rotation])
        for rotation in range(SEED_BLOCK_ROTATION_COUNT)
    )


def block_means(means_by_seed: dict[int, float]) -> tuple[tuple[int, float], ...]:
    """``focal_seed_block_means()``と同じ形のblock mean列。"""
    return tuple(means_by_seed.items())


class ArenaBoundaryTest(unittest.TestCase):
    """neutral layerがpurpose-specific experimentへ依存しないことを固定する。"""

    def test_rotation_count_comes_from_the_arena_core_contract(self) -> None:
        self.assertEqual(SEED_BLOCK_ROTATION_COUNT, SINGLE_ROUND_ROTATION_COUNT)
        self.assertEqual(SEED_BLOCK_ROTATION_COUNT, 4)

    def test_module_does_not_import_experiment_packages(self) -> None:
        source = Path(paired_evaluation.__file__).read_text(encoding="utf-8")
        imported: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "neutral layer has no relative import")
                imported.append(node.module or "")
        self.assertTrue(imported)
        for name in imported:
            for forbidden in FORBIDDEN_IMPORT_ROOTS:
                self.assertFalse(
                    name == forbidden or name.startswith(f"{forbidden}."),
                    f"{name} must not be imported by the neutral layer",
                )


class FocalSeedBlockMeansTest(unittest.TestCase):
    """4-rotation seed blockのdeterministic validationとmeanを固定する。"""

    def test_single_block_averages_the_four_rotations(self) -> None:
        results = seed_block(651, (30_000, 31_000, 32_000, 33_000))
        self.assertEqual(focal_seed_block_means(results), ((651, 31_500.0),))

    def test_multiple_blocks_keep_the_caller_supplied_seed_order(self) -> None:
        results = (
            seed_block(704, (25_000, 25_000, 25_000, 25_000))
            + seed_block(651, (30_000, 30_000, 30_000, 30_000))
            + seed_block(652, (10_000, 20_000, 30_000, 40_000))
        )
        self.assertEqual(
            focal_seed_block_means(results),
            ((704, 25_000.0), (651, 30_000.0), (652, 25_000.0)),
        )

    def test_partial_rotation_block_is_rejected(self) -> None:
        results = seed_block(651, (30_000, 30_000, 30_000, 30_000))[:3]
        with self.assertRaisesRegex(PairedEvaluationError, "four rotations"):
            focal_seed_block_means(results)

    def test_wrong_rotation_order_is_rejected(self) -> None:
        results = seed_block(651, (30_000, 31_000, 32_000, 33_000))
        with self.assertRaisesRegex(PairedEvaluationError, "rotations 0, 1, 2, 3"):
            focal_seed_block_means(results[::-1])

    def test_mixed_seeds_inside_a_block_are_rejected(self) -> None:
        results = (
            seed_block(651, (30_000, 30_000, 30_000, 30_000))[:3]
            + seed_block(652, (30_000, 30_000, 30_000, 30_000))[3:]
        )
        with self.assertRaisesRegex(PairedEvaluationError, "same seed"):
            focal_seed_block_means(results)

    def test_wrong_focal_seat_rotation_is_rejected(self) -> None:
        results = seed_block(651, (30_000, 30_000, 30_000, 30_000))[:3] + (
            game_result(651, 3, 30_000, focal_seat=0),
        )
        with self.assertRaisesRegex(PairedEvaluationError, "focal seat"):
            focal_seed_block_means(results)

    def test_malformed_containers_are_rejected(self) -> None:
        with self.assertRaisesRegex(PairedEvaluationError, "must be a tuple"):
            focal_seed_block_means(list(seed_block(651, (1, 1, 1, 1))))
        with self.assertRaisesRegex(PairedEvaluationError, "must not be empty"):
            focal_seed_block_means(())


class PairedDeltaTest(unittest.TestCase):
    """block meansからのpaired delta導出を固定する。"""

    def test_zero_positive_and_negative_deltas(self) -> None:
        deltas = paired_deltas_from_block_means(
            block_means({651: 30_000.0, 652: 31_000.0, 653: 29_500.0}),
            block_means({651: 30_000.0, 652: 30_000.0, 653: 30_000.0}),
        )
        self.assertEqual(tuple(item.seed for item in deltas), (651, 652, 653))
        self.assertEqual(deltas[0].delta, 0.0)
        self.assertEqual(deltas[1].delta, 1_000.0)
        self.assertEqual(deltas[2].delta, -500.0)
        for item in deltas:
            self.assertEqual(item.delta, item.candidate_mean - item.parent_mean)

    def test_deltas_are_derived_from_the_focal_block_means(self) -> None:
        candidate = focal_seed_block_means(
            seed_block(651, (30_000, 31_000, 32_000, 33_000))
        )
        parent = focal_seed_block_means(
            seed_block(651, (30_000, 30_000, 30_000, 30_000))
        )
        self.assertEqual(
            paired_deltas_from_block_means(candidate, parent),
            (
                PairedSeedDelta(
                    seed=651,
                    candidate_mean=31_500.0,
                    parent_mean=30_000.0,
                    delta=1_500.0,
                ),
            ),
        )

    def test_mismatched_ordered_seeds_are_rejected(self) -> None:
        with self.assertRaisesRegex(PairedEvaluationError, "same ordered seeds"):
            paired_deltas_from_block_means(
                block_means({651: 1.0, 652: 1.0}),
                block_means({652: 1.0, 651: 1.0}),
            )

    def test_mismatched_block_counts_are_rejected(self) -> None:
        with self.assertRaisesRegex(PairedEvaluationError, "same number"):
            paired_deltas_from_block_means(
                block_means({651: 1.0, 652: 1.0}), block_means({651: 1.0})
            )

    def test_empty_and_malformed_block_means_are_rejected(self) -> None:
        with self.assertRaisesRegex(PairedEvaluationError, "at least one seed block"):
            paired_deltas_from_block_means((), ())
        with self.assertRaisesRegex(PairedEvaluationError, "must be tuples"):
            paired_deltas_from_block_means([(651, 1.0)], ((651, 1.0),))

    def test_delta_document_field_names_are_stable(self) -> None:
        delta = PairedSeedDelta(
            seed=651, candidate_mean=31_500.0, parent_mean=30_000.0, delta=1_500.0
        )
        self.assertEqual(
            delta.to_document(),
            {
                "candidate_mean": 31_500.0,
                "delta": 1_500.0,
                "parent_mean": 30_000.0,
                "seed": 651,
            },
        )


class PairedSummaryTest(unittest.TestCase):
    """mean / sample SD / SE / normal-approx 95% intervalを固定する。"""

    def _deltas(self, values: tuple[float, ...]) -> tuple[PairedSeedDelta, ...]:
        return tuple(
            PairedSeedDelta(
                seed=651 + index,
                candidate_mean=30_000.0 + value,
                parent_mean=30_000.0,
                delta=value,
            )
            for index, value in enumerate(values)
        )

    def test_constant_deltas_have_a_degenerate_interval(self) -> None:
        summary = summarize_paired_deltas(self._deltas((1.0, 1.0, 1.0, 1.0)))
        self.assertEqual(
            summary,
            PairedSummary(
                block_count=4,
                mean_delta=1.0,
                sample_standard_deviation=0.0,
                standard_error=0.0,
                interval_lower=1.0,
                interval_upper=1.0,
            ),
        )

    def test_sample_standard_deviation_uses_n_minus_one(self) -> None:
        summary = summarize_paired_deltas(self._deltas((-1.0, 0.0, 1.0)))
        self.assertEqual(summary.block_count, 3)
        self.assertEqual(summary.mean_delta, 0.0)
        self.assertEqual(summary.sample_standard_deviation, 1.0)
        self.assertEqual(summary.standard_error, 1.0 / math.sqrt(3))
        self.assertEqual(summary.interval_lower, -INTERVAL_Z * summary.standard_error)
        self.assertEqual(summary.interval_upper, INTERVAL_Z * summary.standard_error)

    def test_interval_is_the_normal_approximation_of_the_mean(self) -> None:
        summary = summarize_paired_deltas(self._deltas((10.0, 20.0, 30.0, 40.0)))
        self.assertEqual(summary.mean_delta, 25.0)
        self.assertEqual(
            summary.interval_lower,
            summary.mean_delta - INTERVAL_Z * summary.standard_error,
        )
        self.assertEqual(
            summary.interval_upper,
            summary.mean_delta + INTERVAL_Z * summary.standard_error,
        )
        self.assertEqual(INTERVAL_Z, 1.96)

    def test_single_block_cannot_form_an_interval(self) -> None:
        with self.assertRaisesRegex(PairedEvaluationError, "at least two"):
            summarize_paired_deltas(self._deltas((1.0,)))

    def test_malformed_delta_containers_are_rejected(self) -> None:
        with self.assertRaisesRegex(PairedEvaluationError, "non-empty tuple"):
            summarize_paired_deltas(())
        with self.assertRaisesRegex(PairedEvaluationError, "non-empty tuple"):
            summarize_paired_deltas(list(self._deltas((1.0, 2.0))))

    def test_non_finite_deltas_stay_non_finite_without_being_classified(self) -> None:
        """non-finite intervalの拒否はscientific classificationの責務である。

        neutral layerはmechanical aggregationだけを行い、intervalの解釈や
        rejectionをここへ持ち込まない。各experimentのclassificationが
        non-finite intervalをfail closedする。
        """
        summary = summarize_paired_deltas(self._deltas((float("inf"), 1.0)))
        self.assertFalse(math.isfinite(summary.mean_delta))
        self.assertFalse(math.isfinite(summary.interval_lower))

    def test_summary_document_field_names_are_stable(self) -> None:
        summary = summarize_paired_deltas(self._deltas((1.0, 1.0)))
        self.assertEqual(
            summary.to_document(),
            {
                "block_count": 2,
                "interval_lower": 1.0,
                "interval_upper": 1.0,
                "mean_delta": 1.0,
                "sample_standard_deviation": 0.0,
                "standard_error": 0.0,
            },
        )


class ArmArtifactTransportTest(unittest.TestCase):
    """arm artifactのstrict read、digest、arm diagnosticsを固定する。"""

    def _saved_arm(self, directory: Path) -> Path:
        path = directory / "arm.json"
        artifact_fixtures.save(
            artifact_fixtures.evaluation_result(seeds=(20_200, 20_201)), path
        )
        return path

    def test_digest_is_the_byte_level_sha256_of_the_stored_file(self) -> None:
        with tempfile.TemporaryDirectory() as text:
            path = self._saved_arm(Path(text))
            self.assertEqual(
                artifact_file_digest(path),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                artifact_file_digest(str(path)), artifact_file_digest(path)
            )

            changed = Path(text) / "changed.json"
            changed.write_bytes(path.read_bytes() + b"\n")
            self.assertNotEqual(
                artifact_file_digest(changed), artifact_file_digest(path)
            )

    def test_load_arm_artifact_uses_the_existing_strict_readback(self) -> None:
        with tempfile.TemporaryDirectory() as text:
            directory = Path(text)
            path = self._saved_arm(directory)
            artifact = load_arm_artifact(path)
            self.assertEqual(artifact.plan.seeds, (20_200, 20_201))
            self.assertEqual(
                focal_seed_block_means(artifact.game_results),
                focal_seed_block_means(
                    artifact_fixtures.game_results((20_200, 20_201))
                ),
            )

            malformed = directory / "malformed.json"
            malformed.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(SingleRoundArtifactError):
                load_arm_artifact(malformed)

    def test_arm_diagnostics_reuse_the_canonical_summary_values(self) -> None:
        with tempfile.TemporaryDirectory() as text:
            artifact = load_arm_artifact(self._saved_arm(Path(text)))

        metrics = artifact.summary.candidate_metrics
        mahjong = metrics.mahjong_metrics
        diagnostics = arm_diagnostics(artifact)
        self.assertEqual(
            set(diagnostics),
            {
                "deal_in_count",
                "deal_in_rate",
                "exhaustive_draw_count",
                "exhaustive_draw_tenpai_count",
                "exhaustive_draw_tenpai_rate",
                "game_count",
                "mean_deal_in_loss",
                "mean_first_tenpai_turn",
                "mean_focal_seat_score",
                "mean_win_points",
                "round_count",
                "tenpai_reached_count",
                "tenpai_reached_rate",
                "win_count",
                "win_rate",
            },
        )
        self.assertEqual(diagnostics["game_count"], metrics.game_count)
        self.assertEqual(
            diagnostics["mean_focal_seat_score"], metrics.mean_candidate_score
        )
        self.assertEqual(diagnostics["round_count"], mahjong.round_count)
        self.assertEqual(diagnostics["win_count"], mahjong.win_count)
        self.assertEqual(diagnostics["win_rate"], mahjong.win_rate)
        self.assertEqual(diagnostics["deal_in_count"], mahjong.deal_in_count)
        self.assertEqual(diagnostics["deal_in_rate"], mahjong.deal_in_rate)
        self.assertEqual(
            diagnostics["exhaustive_draw_count"], mahjong.exhaustive_draw_count
        )
        self.assertEqual(
            diagnostics["exhaustive_draw_tenpai_count"],
            mahjong.exhaustive_draw_tenpai_count,
        )
        self.assertEqual(
            diagnostics["exhaustive_draw_tenpai_rate"],
            mahjong.exhaustive_draw_tenpai_rate,
        )
        self.assertEqual(diagnostics["mean_deal_in_loss"], mahjong.mean_deal_in_loss)
        self.assertEqual(
            diagnostics["mean_first_tenpai_turn"], mahjong.mean_first_tenpai_turn
        )
        self.assertEqual(diagnostics["mean_win_points"], mahjong.mean_win_points)
        self.assertEqual(
            diagnostics["tenpai_reached_count"], mahjong.tenpai_reached_count
        )
        self.assertEqual(
            diagnostics["tenpai_reached_rate"],
            mahjong.tenpai_reached_count / mahjong.round_count,
        )


if __name__ == "__main__":
    unittest.main()
