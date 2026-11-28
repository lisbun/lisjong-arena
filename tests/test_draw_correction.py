"""通常ツモ補正の射影・join・rank contrast・κの推定と凍結・分析（#598）。

実engineの1 seed block（A = ShantenPolicy、B = MinimalPolicy、6配置）をcaptureし、
局recordの第1層rowと結ぶ。κ・splitの規則は手計算できる合成blockで固定する。
"""

import dataclasses
import functools
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from lisjong.draw_correction import (
    draw_probabilities,
    unit_draw_corrections,
    v1_definition,
)
from lisjong.policies import MinimalPolicy, ShantenPolicy
from lisjong_engine.seat import Seat

import lisjong_arena.draw_correction.join as join_module
from lisjong_arena.draw_correction.analysis import (
    CI_ESTABLISHED,
    CI_NOT_ESTABLISHED,
    R_ESTABLISHED,
    R_NOT_ESTABLISHED,
    analyze_correction,
    corrected_differences,
)
from lisjong_arena.draw_correction.join import (
    BlockContrast,
    DrawCorrectionJoinError,
    JoinedBlocks,
    join_capture_with_hanchans,
)
from lisjong_arena.draw_correction.kappa import (
    BRANCH_INSUFFICIENT,
    BRANCH_NON_POSITIVE,
    BRANCH_RATIO,
    BRANCH_ZERO_VARIANCE,
    ESTABLISHED,
    NOT_ESTABLISHED,
    DrawCorrectionKappaError,
    estimate_kappa,
    read_frozen_kappa,
    require_disjoint_split,
    write_frozen_kappa,
)
from lisjong_arena.draw_correction.projection import (
    DrawProjectionError,
    project_pre_draw,
    project_record,
    tile_category,
)
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    SeatAssignment,
    placement_roles,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    HanchanCapture,
    build_records,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import PROTOCOL_ID
from lisjong_arena.learning_candidate_engine_aabb.statistics import block_rank_totals
from lisjong_arena.lisjong_engine.live_draw_capture import capture_live_draw_hanchan
from lisjong_arena.lisjong_engine.live_draw_store import (
    read_live_draw_capture,
    write_live_draw_capture,
)

_SEED = 598101
_REVISION = "0" * 40
_PARTICIPANTS = {"candidate": "shanten-policy@test", "incumbent": "minimal-policy@test"}


def _policies(placement):
    return {
        seat: ShantenPolicy() if role == "A" else MinimalPolicy()
        for seat, role in zip(Seat, placement_roles(placement), strict=True)
    }


@functools.cache
def _engine_block():
    """1 seed block（6配置）のcaptureと、同じ実行の`CompletedMatch`。"""
    results = []
    for placement in range(6):
        completed, captured = capture_live_draw_hanchan(
            _policies(placement), seed=_SEED, placement=placement
        )
        results.append((placement, completed, captured))
    return tuple(results)


def _hanchan_rows(block=None):
    captures = [
        HanchanCapture(
            match=completed,
            seed=_SEED,
            placement=placement,
            seats=tuple(
                SeatAssignment(
                    role, _PARTICIPANTS["candidate" if role == "A" else "incumbent"]
                )
                for role in placement_roles(placement)
            ),
            selector_call_counts=(1, 1, 1, 1),
            policy_call_counts=(1, 1, 1, 1),
            round_evidence=None,
        )
        for placement, completed, _ in (block or _engine_block())
    ]
    rows, _, _ = build_records(captures, event_facts_required=False)
    return rows


def _write_capture(directory, placements=range(6)):
    destination = Path(directory) / "capture"
    write_live_draw_capture(
        destination,
        [captured for p, _, captured in _engine_block() if p in placements],
        plan=[(_SEED, p) for p in placements],
        arena_revision=_REVISION,
        engine_revision=_REVISION,
        rules="RuleSet.default()",
        access="test-only",
        retention="test-only",
    )
    return read_live_draw_capture(destination)


class _EngineBlockFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.artifact = _write_capture(cls._tmp.name)
        cls.rows = _hanchan_rows()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class ProjectionTest(_EngineBlockFixture):
    def test_tile_category_folds_copies_and_separates_red_fives(self):
        self.assertEqual(tile_category(16), 34)  # 赤5m（5mのcopy 0）
        self.assertEqual(tile_category(52), 35)  # 赤5p
        self.assertEqual(tile_category(88), 36)  # 赤5s
        self.assertEqual([tile_category(i) for i in (17, 18, 19)], [4, 4, 4])
        self.assertEqual(tile_category(0), 0)
        self.assertEqual(tile_category(135), 33)
        for bad in (-1, 136, True, 1.0):
            with self.subTest(bad=bad), self.assertRaises(DrawProjectionError):
                tile_category(bad)

    def test_projection_matches_board_and_realized_tile_is_in_support(self):
        called_seen = False
        for record in self.artifact.records:
            projected = project_record(record)
            checkpoint = record.pre_draw["checkpoint"]
            drawer = record.pre_draw["current_seat"]
            self.assertEqual(projected.value.drawer_seat, drawer)
            self.assertEqual(
                projected.value.drawer_hand_counts,
                projected.probability.hand_counts[drawer],
            )
            # 鳴かれた河の牌は河から除き、副露の側で数える
            for seat_index, seat in enumerate(checkpoint["seats"]):
                uncalled = [
                    d["tile"] for d in seat["discards"] if d["called_by"] is None
                ]
                called_seen |= len(uncalled) != len(seat["discards"])
                self.assertEqual(
                    sum(projected.probability.uncalled_discard_counts[seat_index]),
                    len(uncalled),
                )
            support = draw_probabilities(projected.probability)
            self.assertIn(projected.realized_category, support)
        self.assertTrue(called_seen, "the fixture should contain a called discard")

    def test_projection_uses_pre_draw_only(self):
        record = self.artifact.records[0]
        probability, value = project_pre_draw(record.pre_draw)
        self.assertEqual(project_record(record).probability, probability)
        self.assertEqual(project_record(record).value, value)
        with self.assertRaises(DrawProjectionError):
            project_record({"pre_draw": record.pre_draw})


class JoinTest(_EngineBlockFixture):
    def test_rank_contrast_and_correction_contrast_use_the_same_weights(self):
        joined = join_capture_with_hanchans(self.artifact, self.rows)
        self.assertEqual(joined.protocol_id, PROTOCOL_ID)
        self.assertEqual(joined.value_definition, v1_definition())
        (block,) = joined.blocks
        rank = block_rank_totals(self.rows)[0]
        self.assertEqual(block.d, Fraction(rank["difference_ranks"], 12))

        # 手で再計算: 席ごとの Σ_t c_t を role へ割り当て、(X_B − X_A) / 12
        totals = {"A": Fraction(0), "B": Fraction(0)}
        for row in self.rows:
            seat_sums = [Fraction(0)] * 4
            for record in self.artifact.records:
                if record.identity["placement"] != row["placement"]:
                    continue
                projected = project_record(record)
                c = unit_draw_corrections(
                    projected.probability, projected.value, projected.realized_category
                )
                seat_sums = [a + b for a, b in zip(seat_sums, c, strict=True)]
            for seat, assignment in enumerate(row["seats"]):
                totals[assignment["role"]] += seat_sums[seat]
        self.assertEqual(block.x, (totals["B"] - totals["A"]) / 12)
        self.assertEqual(block.draw_count, len(self.artifact.records))
        self.assertNotEqual(block.x, 0)

    def test_placement_weights_and_sign(self):
        # 全ツモで seat 0 にだけ +1 の補正 -> seat 0 の role は A,A,A,B,B,B
        with patch.object(
            join_module, "unit_draw_corrections", return_value=(1, 0, 0, 0)
        ):
            (block,) = join_capture_with_hanchans(self.artifact, self.rows).blocks
        draws = [0] * 6
        for record in self.artifact.records:
            draws[record.identity["placement"]] += 1
        self.assertEqual(block.x, Fraction(sum(draws[3:]) - sum(draws[:3]), 12))

    def test_zero_value_gives_zero_contrast(self):
        with patch.object(
            join_module, "unit_draw_corrections", return_value=(0, 0, 0, 0)
        ):
            (block,) = join_capture_with_hanchans(self.artifact, self.rows).blocks
        self.assertEqual(block.x, 0)

    def test_missing_placement_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            partial = _write_capture(directory, placements=range(5))
        with self.assertRaises(DrawCorrectionJoinError):
            join_capture_with_hanchans(partial, self.rows[:5])

    def test_seed_split_across_artifacts_is_rejected(self):
        # 同じseedのplacement 0..2だけをTRAIN側のartifactへ置いた場合（残りは別split）
        with tempfile.TemporaryDirectory() as directory:
            first_half = _write_capture(directory, placements=range(3))
        with self.assertRaises(DrawCorrectionJoinError):
            join_capture_with_hanchans(first_half, self.rows[:3])

    def test_unplanned_or_reordered_hanchans_are_rejected(self):
        for name, rows in {
            "extra": [*self.rows, self.rows[0]],
            "missing": self.rows[:5],
            "reordered": [self.rows[1], self.rows[0], *self.rows[2:]],
        }.items():
            with self.subTest(case=name), self.assertRaises(DrawCorrectionJoinError):
                join_capture_with_hanchans(self.artifact, rows)

    def test_result_from_another_run_is_rejected(self):
        rows = json.loads(json.dumps(self.rows))
        other = dict(rows[2])
        other["seed"] = _SEED + 1
        rows[2] = other
        with self.assertRaises(DrawCorrectionJoinError):
            join_capture_with_hanchans(self.artifact, rows)

    def test_round_identity_mismatch_is_rejected(self):
        manifest = json.loads(json.dumps(self.artifact.manifest))
        manifest["hanchans"][0]["rounds"].append(
            {"live_draw_count": 0, "rinshan_draw_count": 0, "round_ordinal": 99}
        )
        tampered = dataclasses.replace(self.artifact, manifest=manifest)
        with self.assertRaises(DrawCorrectionJoinError):
            join_capture_with_hanchans(tampered, self.rows)

    def test_capture_dealer_mismatch_is_rejected(self):
        records = list(self.artifact.records)
        first = records[0]
        pre_draw = json.loads(json.dumps(first.pre_draw))
        pre_draw["checkpoint"]["dealer_seat"] = (
            pre_draw["checkpoint"]["dealer_seat"] + 1
        ) % 4
        records[0] = dataclasses.replace(first, pre_draw=pre_draw)
        tampered = dataclasses.replace(self.artifact, records=tuple(records))
        with self.assertRaises(DrawCorrectionJoinError):
            join_capture_with_hanchans(tampered, self.rows)


# ---------------------------------------------------------------------------
# 合成block（κ・split・分析）
# ---------------------------------------------------------------------------


def _blocks(pairs, *, first_seed=1000):
    return JoinedBlocks(
        protocol_id=PROTOCOL_ID,
        blocks=tuple(
            BlockContrast(
                seed=first_seed + index,
                d=Fraction(d),
                x=Fraction(x),
                rank_sum_a=30,
                rank_sum_b=30,
                draw_count=1,
                drawer_corrections=(Fraction(x),),
            )
            for index, (d, x) in enumerate(pairs)
        ),
        capture_payload_sha256="0" * 64,
        capture_producer={"arena_revision": _REVISION, "engine_revision": _REVISION},
        value_definition=v1_definition(),
    )


class KappaRuleTest(unittest.TestCase):
    def test_positive_covariance_gives_exact_ratio(self):
        # D = (1, 2, 3)、X = (1, 1, 4): Cov = 3/2、Var(X) = 3 -> κ = 1/2
        estimate = estimate_kappa(_blocks([(1, 1), (2, 1), (3, 4)]), minimum_blocks=2)
        self.assertEqual(estimate.covariance, Fraction(3, 2))
        self.assertEqual(estimate.variance, 3)
        self.assertEqual(estimate.kappa, Fraction(1, 2))
        self.assertEqual(
            (estimate.status, estimate.branch), (ESTABLISHED, BRANCH_RATIO)
        )

    def test_negative_covariance_gives_zero(self):
        estimate = estimate_kappa(_blocks([(1, 3), (2, 2), (3, 1)]), minimum_blocks=2)
        self.assertEqual(estimate.kappa, 0)
        self.assertEqual(
            (estimate.status, estimate.branch), (ESTABLISHED, BRANCH_NON_POSITIVE)
        )

    def test_zero_variance_is_not_established(self):
        estimate = estimate_kappa(_blocks([(1, 2), (3, 2), (5, 2)]), minimum_blocks=2)
        self.assertEqual(estimate.kappa, 0)
        self.assertEqual(
            (estimate.status, estimate.branch), (NOT_ESTABLISHED, BRANCH_ZERO_VARIANCE)
        )

    def test_insufficient_blocks_is_not_established(self):
        estimate = estimate_kappa(_blocks([(1, 1), (2, 3), (4, 4)]), minimum_blocks=4)
        self.assertEqual(
            (estimate.kappa, estimate.status, estimate.branch),
            (0, NOT_ESTABLISHED, BRANCH_INSUFFICIENT),
        )
        single = estimate_kappa(_blocks([(1, 1)]), minimum_blocks=2)
        self.assertIsNone(single.covariance)
        self.assertEqual(single.status, NOT_ESTABLISHED)

    def test_minimum_blocks_must_be_at_least_two(self):
        for bad in (0, 1, 2.0, True):
            with self.subTest(bad=bad), self.assertRaises(DrawCorrectionKappaError):
                estimate_kappa(_blocks([(1, 1), (2, 2)]), minimum_blocks=bad)

    def test_non_finite_or_inexact_values_are_rejected(self):
        blocks = _blocks([(1, 1), (2, 2)])
        for bad in (float("nan"), float("inf"), 0.5):
            tampered = dataclasses.replace(
                blocks,
                blocks=(dataclasses.replace(blocks.blocks[0], x=bad), blocks.blocks[1]),
            )
            with self.subTest(bad=bad), self.assertRaises(DrawCorrectionKappaError):
                estimate_kappa(tampered, minimum_blocks=2)

    def test_duplicate_train_blocks_are_rejected(self):
        blocks = _blocks([(1, 1), (2, 2)])
        duplicated = dataclasses.replace(
            blocks, blocks=(blocks.blocks[0], blocks.blocks[0])
        )
        with self.assertRaises(DrawCorrectionKappaError):
            estimate_kappa(duplicated, minimum_blocks=2)


class SplitTest(unittest.TestCase):
    def test_overlapping_or_duplicate_seeds_are_rejected(self):
        require_disjoint_split([1, 2], [3, 4])
        for train, evaluation in (
            ([1, 2], [2, 3]),
            ([1, 1], [3]),
            ([1], [3, 3]),
            ([], [1]),
        ):
            with self.subTest(train=train, evaluation=evaluation):
                with self.assertRaises(DrawCorrectionKappaError):
                    require_disjoint_split(train, evaluation)


class FrozenKappaTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "kappa.json"
        self.train = _blocks([(1, 1), (2, 1), (3, 4)])
        self.estimate = estimate_kappa(self.train, minimum_blocks=2)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self):
        return write_frozen_kappa(
            self.path, self.train, self.estimate, participants=_PARTICIPANTS
        )

    def test_strict_readback_round_trip(self):
        frozen = self._write()
        self.assertEqual(frozen.estimate, self.estimate)
        self.assertEqual(frozen.train_seeds, (1000, 1001, 1002))
        self.assertEqual(frozen.value_definition, v1_definition())
        self.assertEqual(read_frozen_kappa(self.path).document, frozen.document)
        self.assertEqual(frozen.document["participants"], _PARTICIPANTS)

    def test_write_once(self):
        self._write()
        with self.assertRaises(FileExistsError):
            self._write()

    def test_estimate_must_match_train(self):
        other = estimate_kappa(_blocks([(1, 3), (2, 2), (3, 1)]), minimum_blocks=2)
        with self.assertRaises(DrawCorrectionKappaError):
            write_frozen_kappa(self.path, self.train, other, participants=_PARTICIPANTS)
        self.assertFalse(self.path.exists())

    def test_tampered_artifacts_are_rejected(self):
        self._write()
        original = json.loads(self.path.read_text(encoding="utf-8"))
        tamperings = {
            "kappa": lambda d: d.update(kappa={"denominator": 1, "numerator": 1}),
            "status": lambda d: d.update(status=NOT_ESTABLISHED),
            "seeds": lambda d: d["split"].update(train_seeds=[1000, 1001, 9999]),
            "k": lambda d: d.update(k=4),
            "value_definition": lambda d: d["value_definition"].update(version=2),
            "unknown_field": lambda d: d.update(extra=1),
            "protocol": lambda d: d.update(protocol_id="other"),
            "not_lowest_terms": lambda d: d.update(
                kappa={"denominator": 4, "numerator": 2}
            ),
        }
        for name, tamper in tamperings.items():
            with self.subTest(case=name):
                document = json.loads(json.dumps(original))
                tamper(document)
                path = Path(self._tmp.name) / f"{name}.json"
                path.write_text(
                    json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False)
                    + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(DrawCorrectionKappaError):
                    read_frozen_kappa(path)

    def test_non_canonical_bytes_are_rejected(self):
        self._write()
        path = Path(self._tmp.name) / "compact.json"
        path.write_text(json.dumps(json.loads(self.path.read_text())), encoding="utf-8")
        with self.assertRaises(DrawCorrectionKappaError):
            read_frozen_kappa(path)


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.train = _blocks([(1, 1), (2, 1), (3, 4)])

    def tearDown(self):
        self._tmp.cleanup()

    def _frozen(self, train=None, name="kappa.json"):
        train = train or self.train
        estimate = estimate_kappa(train, minimum_blocks=2)
        return write_frozen_kappa(
            Path(self._tmp.name) / name, train, estimate, participants=_PARTICIPANTS
        )

    def test_kappa_zero_leaves_d_unchanged(self):
        frozen = self._frozen(_blocks([(1, 3), (2, 2), (3, 1)]))  # κ = 0
        evaluation = _blocks([(1, 5), (-2, 7), (Fraction(1, 12), -3)], first_seed=2000)
        self.assertEqual(
            corrected_differences(frozen, evaluation), [b.d for b in evaluation.blocks]
        )
        result = analyze_correction(
            frozen, evaluation, bootstrap_resamples=50, bootstrap_seed=1
        )
        self.assertEqual(result["ratio"]["r_exact"], "1/1")

    def test_ratio_is_exact_and_bootstrap_is_deterministic(self):
        frozen = self._frozen()  # κ = 1/2
        evaluation = _blocks(
            [(1, 2), (-1, -2), (2, 2), (0, 0), (1, 0), (-2, -2)], first_seed=2000
        )
        adjusted = [
            Fraction(0),
            Fraction(0),
            Fraction(1),
            Fraction(0),
            Fraction(1),
            Fraction(-1),
        ]
        self.assertEqual(corrected_differences(frozen, evaluation), adjusted)
        first = analyze_correction(
            frozen, evaluation, bootstrap_resamples=200, bootstrap_seed=7
        )
        again = analyze_correction(
            frozen, evaluation, bootstrap_resamples=200, bootstrap_seed=7
        )
        self.assertEqual(first, again)
        ds = [Fraction(v) for v in (1, -1, 2, 0, 1, -2)]
        mean = sum(ds) / 6
        mean_a = sum(adjusted) / 6
        expected = sum((a - mean_a) ** 2 for a in adjusted) / sum(
            (d - mean) ** 2 for d in ds
        )
        self.assertEqual(first["ratio"]["status"], R_ESTABLISHED)
        self.assertEqual(
            first["ratio"]["r_exact"], f"{expected.numerator}/{expected.denominator}"
        )
        if first["bootstrap"]["status"] == CI_ESTABLISHED:
            self.assertLessEqual(
                first["bootstrap"]["lower"], first["bootstrap"]["upper"]
            )
        json.dumps(first, allow_nan=False)

    def test_degenerate_cases_are_reported_without_nan(self):
        frozen = self._frozen()
        flat = analyze_correction(
            frozen,
            _blocks([(1, 1), (1, 2), (1, 3)], first_seed=2000),
            bootstrap_resamples=20,
            bootstrap_seed=1,
        )
        self.assertEqual(flat["ratio"]["status"], R_NOT_ESTABLISHED)
        self.assertIsNone(flat["ratio"]["r"])
        self.assertEqual(flat["bootstrap"]["status"], CI_NOT_ESTABLISHED)
        single = analyze_correction(
            frozen,
            _blocks([(1, 1)], first_seed=2000),
            bootstrap_resamples=20,
            bootstrap_seed=1,
        )
        self.assertEqual(single["ratio"]["status"], R_NOT_ESTABLISHED)
        two = analyze_correction(
            frozen,
            _blocks([(1, 1), (2, 1)], first_seed=2000),
            bootstrap_resamples=200,
            bootstrap_seed=1,
        )
        self.assertEqual(two["ratio"]["status"], R_ESTABLISHED)
        self.assertEqual(two["bootstrap"]["status"], CI_NOT_ESTABLISHED)
        for result in (flat, single, two):
            json.dumps(result, allow_nan=False)

    def test_evaluation_requires_frozen_kappa_and_disjoint_seeds(self):
        frozen = self._frozen()
        with self.assertRaises(DrawCorrectionKappaError):
            analyze_correction(
                frozen.estimate,
                _blocks([(1, 1), (2, 2)], first_seed=2000),
                bootstrap_resamples=10,
                bootstrap_seed=1,
            )
        with self.assertRaises(DrawCorrectionKappaError):
            analyze_correction(
                frozen,
                _blocks([(1, 1), (2, 2)], first_seed=1001),  # TRAINのseedを含む
                bootstrap_resamples=10,
                bootstrap_seed=1,
            )

    def test_evaluation_with_another_value_definition_is_rejected(self):
        frozen = self._frozen()
        evaluation = dataclasses.replace(
            _blocks([(1, 1), (2, 2)], first_seed=2000),
            value_definition={**v1_definition(), "version": 2},
        )
        with self.assertRaises(DrawCorrectionKappaError):
            analyze_correction(
                frozen, evaluation, bootstrap_resamples=10, bootstrap_seed=1
            )

    def test_extremes_and_correction_summary(self):
        frozen = self._frozen()  # κ = 1/2
        evaluation = _blocks([(0, 4), (0, -6), (1, 2)], first_seed=2000)
        result = analyze_correction(
            frozen,
            evaluation,
            bootstrap_resamples=10,
            bootstrap_seed=1,
            extreme_count=2,
        )
        self.assertEqual(
            result["corrections"]["extreme_blocks"],
            [{"kappa_x": "-3/1", "seed": 2001}, {"kappa_x": "2/1", "seed": 2000}],
        )
        self.assertEqual(result["corrections"]["block_kappa_x_mean"], "0/1")


if __name__ == "__main__":
    unittest.main()
