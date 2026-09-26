"""Issue #389 arm artifactとsummaryのunit test。

単一game境界を差し替えた決定的なarmを保存・strict readbackし、手計算した
記述値・paired統計（seed block単位、unconditional metricのみ）・sample-size
tableと、binding / pairingのfail closedを固定する。
"""

import json
import tempfile
import unittest
from pathlib import Path

from _pure_offense_benchmark_fixtures import (
    OTHER,
    OTHER_ARM,
    REFERENCE,
    SEEDS,
    allocation_ledger,
    save_arm,
)
from lisjong.policies import MinimalPolicy

from lisjong_arena import seed_registry
from lisjong_arena.model import PolicySpec
from lisjong_arena.pure_offense_benchmark.artifact import (
    OFFENSE_FILE,
    STRENGTH_FILE,
    PureOffenseArtifactError,
    load_benchmark_arm,
    resolve_seed_allocation,
)
from lisjong_arena.pure_offense_benchmark.protocol import (
    PAIRED_METRICS,
    SEED_DOMAIN,
)
from lisjong_arena.pure_offense_benchmark.summary import (
    PureOffenseSummaryError,
    build_summary,
    format_summary,
    required_seed_blocks,
    save_summary,
)


class PureOffenseSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.reference_dir = save_arm(
            self.root, "reference", REFERENCE, lambda seed, focal: ("draw", None)
        )
        self.other_dir = save_arm(
            self.root, "other", OTHER, lambda seed, focal: OTHER_ARM[(seed, focal)]
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _summary(self):
        return build_summary(
            [load_benchmark_arm(self.reference_dir), load_benchmark_arm(self.other_dir)]
        )

    def test_arm_profile_descriptive_values(self) -> None:
        profile = self._summary()["arms"][1]["profile"]
        self.assertEqual(profile["kyoku_count"], 8)
        self.assertEqual(profile["seed_block_count"], 2)
        self.assertEqual(profile["mean_score_delta"], (3000 * 3 - 2000 * 2) / 8)

        tenpai = profile["formal_tenpai"]
        self.assertEqual(tenpai["reached_count"], 5)
        self.assertEqual(tenpai["reached_rate"], 5 / 8)
        self.assertEqual(
            tenpai["mean_first_turn_among_reached"], (3 + 9 + 12 + 3 + 12) / 5
        )
        self.assertEqual(tenpai["by_turn"], {"5": 2 / 8, "8": 2 / 8, "12": 5 / 8})
        curve = tenpai["cumulative_by_turn"]
        self.assertEqual(len(curve), 19)
        self.assertEqual(curve[2], 0.0)
        self.assertEqual(curve[3], 2 / 8)
        self.assertEqual(curve[-1], tenpai["reached_rate"])

        win = profile["win"]
        self.assertEqual((win["count"], win["rate"]), (3, 3 / 8))
        self.assertEqual(win["mean_turn_among_wins"], 6.0)
        self.assertEqual(win["mean_points_among_wins"], 3000.0)
        self.assertEqual((win["tsumo_count"], win["ron_count"]), (3, 0))
        self.assertEqual(win["by_turn"], {"5": 2 / 8, "8": 2 / 8, "12": 3 / 8})
        self.assertEqual(win["cumulative_by_turn"][-1], win["rate"])

        self.assertEqual(
            profile["deal_in"],
            {
                "count": 2,
                "mean_loss_among_deal_ins": 2000.0,
                "rate": 2 / 8,
            },
        )
        self.assertEqual(
            profile["termination"],
            {
                "abortive_draw": 0,
                "abortive_draw_reasons": {},
                "exhaustive_draw": 3,
                "focal_win": 3,
                "opponent_win": 2,
            },
        )
        self.assertEqual(profile["exhaustive_draw_tenpai"]["count"], 2)
        self.assertEqual(
            profile["exhaustive_draw_tenpai"]["rate_among_exhaustive_draws"], 2 / 3
        )
        dealer = profile["dealer_split"]["dealer"]
        self.assertEqual((dealer["kyoku_count"], dealer["win_rate"]), (2, 1.0))
        self.assertEqual(profile["dealer_split"]["non_dealer"]["kyoku_count"], 6)

    def test_paired_metrics_use_seed_blocks_and_unconditional_values(self) -> None:
        summary = self._summary()
        (comparison,) = summary["paired_comparisons"]
        self.assertEqual(comparison["difference"], "other - reference")
        self.assertEqual(tuple(comparison["metrics"]), PAIRED_METRICS)
        for forbidden in ("mean_first_turn_among_reached", "mean_turn_among_wins"):
            self.assertNotIn(forbidden, comparison["metrics"])

        score = comparison["metrics"]["score_delta"]
        self.assertEqual(score["n_seed_blocks"], 2)
        self.assertAlmostEqual(score["mean_difference"], (1000 + 250) / 2)
        self.assertAlmostEqual(score["paired_sd"], 750 / 2**0.5)
        self.assertAlmostEqual(
            score["ci95_lower"], 625 - 1.96 * (750 / 2**0.5) / 2**0.5
        )
        metrics = comparison["metrics"]
        self.assertAlmostEqual(metrics["win"]["mean_difference"], (0.5 + 0.25) / 2)
        self.assertAlmostEqual(metrics["win_by_turn_5"]["mean_difference"], 0.25)
        self.assertEqual(metrics["win_by_turn_5"]["paired_sd"], 0.0)
        self.assertAlmostEqual(metrics["win_by_turn_12"]["mean_difference"], 0.375)
        self.assertAlmostEqual(
            metrics["formal_tenpai_by_turn_8"]["mean_difference"], 0.25
        )
        self.assertAlmostEqual(
            metrics["formal_tenpai_by_turn_12"]["mean_difference"], (0.75 + 0.5) / 2
        )

        sizes = comparison["sample_size_by_practical_difference"]
        self.assertEqual(
            [row["delta"] for row in sizes["score_delta"]], [50, 100, 200, 300]
        )
        self.assertEqual([row["delta"] for row in sizes["win"]], [1, 2, 5])
        self.assertEqual(
            sizes["score_delta"][0]["ci_half_width_seed_blocks"],
            required_seed_blocks(750 / 2**0.5, 50.0, 1.96),
        )
        self.assertIsNone(summary["terminal_classification"])

    def test_summary_is_write_once_and_formats(self) -> None:
        summary = self._summary()
        path = self.root / "summary.json"
        save_summary(summary, path)
        self.assertEqual(json.loads(path.read_text())["summary_version"], 1)
        with self.assertRaises(FileExistsError):
            save_summary(summary, path)
        text = format_summary(summary)
        self.assertIn("no terminal label", text)
        self.assertIn("paired: other - reference", text)

    def test_arms_with_different_seeds_are_not_paired(self) -> None:
        third = save_arm(
            self.root,
            "third",
            PolicySpec(identity="third", factory=MinimalPolicy),
            lambda seed, focal: ("draw", None),
            seeds=(1, 3),
        )
        with self.assertRaises(PureOffenseSummaryError):
            build_summary(
                [load_benchmark_arm(self.reference_dir), load_benchmark_arm(third)]
            )

    def test_duplicate_focal_identity_is_rejected(self) -> None:
        arm = load_benchmark_arm(self.reference_dir)
        with self.assertRaises(PureOffenseSummaryError):
            build_summary([arm, arm])


class PureOffenseArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.arm_dir = save_arm(
            self.root, "arm", OTHER, lambda seed, focal: OTHER_ARM[(seed, focal)]
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trip_keeps_existing_strength_artifact_and_records(self) -> None:
        arm = load_benchmark_arm(self.arm_dir)
        self.assertEqual(arm.focal_identity, "other")
        self.assertEqual(arm.seeds, SEEDS)
        self.assertEqual(len(arm.records), 8)
        self.assertEqual(arm.strength.evaluation_protocol, "abbb-single-round-v1")
        self.assertEqual(arm.seed_allocation["split"], "DEVELOPMENT")

    def test_destination_is_write_once(self) -> None:
        with self.assertRaises(FileExistsError):
            save_arm(self.root, "arm", OTHER, lambda seed, focal: ("draw", None))

    def test_tampering_fails_closed(self) -> None:
        offense_path = self.arm_dir / OFFENSE_FILE
        original = offense_path.read_text()
        document = json.loads(original)

        cases = {}
        changed = json.loads(original)
        changed["strength_artifact"]["sha256"] = "0" * 64
        cases["digest"] = changed
        changed = json.loads(original)
        changed["benchmark"]["turn_checkpoints"] = [5, 8]
        cases["manifest"] = changed
        changed = json.loads(original)
        changed["records"][0]["seats"][0]["won"] = False
        changed["records"][0]["seats"][0]["win_turn"] = None
        changed["records"][0]["seats"][0]["win_tsumo"] = None
        changed["records"][0]["termination"] = "exhaustive_draw"
        changed["records"][0]["draw_reason"] = "exhaustive_draw"
        cases["record vs round stats"] = changed
        changed = json.loads(original)
        changed["records"] = changed["records"][:-1]
        cases["missing record"] = changed
        changed = json.loads(original)
        changed["focal"]["identity"] = "renamed"
        cases["focal identity"] = changed
        self.assertEqual(document, json.loads(original))

        for name, content in cases.items():
            with self.subTest(name):
                offense_path.write_text(json.dumps(content))
                with self.assertRaises(PureOffenseArtifactError):
                    load_benchmark_arm(self.arm_dir)
        offense_path.write_text(original)
        load_benchmark_arm(self.arm_dir)

        strength_path = self.arm_dir / STRENGTH_FILE
        strength_path.write_text(strength_path.read_text() + " ")
        with self.assertRaises(PureOffenseArtifactError):
            load_benchmark_arm(self.arm_dir)


class SeedAllocationTest(unittest.TestCase):
    def test_allocation_must_be_owned_by_389_in_single_round_domain(self) -> None:
        ledger, record = allocation_ledger()
        allocation = resolve_seed_allocation(ledger, record["allocation_identity"])
        self.assertEqual(allocation.seeds, SEEDS)
        self.assertEqual(allocation.binding["seed_domain"], SEED_DOMAIN)

        other_ledger, other_record = allocation_ledger(
            owner_issue="lisbun/lisjong-arena#1"
        )
        with self.assertRaises(PureOffenseArtifactError):
            resolve_seed_allocation(other_ledger, other_record["allocation_identity"])
        with self.assertRaises(PureOffenseArtifactError):
            resolve_seed_allocation(ledger, "0" * 64)

    def test_retired_allocation_is_not_usable(self) -> None:
        ledger, record = allocation_ledger()
        retired = seed_registry.transition_allocation(
            ledger, record["allocation_identity"], state=seed_registry.RETIRED
        )
        with self.assertRaises(PureOffenseArtifactError):
            resolve_seed_allocation(retired, record["allocation_identity"])


class RequiredSeedBlocksTest(unittest.TestCase):
    def test_formula(self) -> None:
        self.assertEqual(required_seed_blocks(100.0, 50.0, 1.96), 16)
        self.assertEqual(required_seed_blocks(100.0, 50.0, 1.96 + 0.8416), 32)
        self.assertEqual(required_seed_blocks(0.0, 50.0, 1.96), 2)
        with self.assertRaises(ValueError):
            required_seed_blocks(1.0, 0.0, 1.96)


if __name__ == "__main__":
    unittest.main()
