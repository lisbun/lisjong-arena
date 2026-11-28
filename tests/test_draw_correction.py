"""通常ツモ補正のplan・paired producer・射影・join・κの推定と凍結・分析（#598）。

実engineで、fixture用のplan（TRAIN 2 seed block、EVAL 1 seed block、A = ShantenPolicy、
B = MinimalPolicy）をpaired producerで実行し、同じseedを別のparticipantで実行した
paired artifactを「別の実行」として使う。κの規則と分析の算術は手計算できる合成値で固定する。
"""

import dataclasses
import json
import shutil
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from lisjong.draw_correction import draw_probabilities, unit_draw_corrections
from lisjong.policies import MinimalPolicy, ShantenPolicy

import lisjong_arena.draw_correction.join as join_module
from lisjong_arena.draw_correction import paired_run
from lisjong_arena.draw_correction.analysis import (
    CI_ESTABLISHED,
    CI_NOT_ESTABLISHED,
    R_ESTABLISHED,
    R_NOT_ESTABLISHED,
    analyze_correction,
    summarize_blocks,
)
from lisjong_arena.draw_correction.join import (
    BlockContrast,
    DrawCorrectionJoinError,
    join_paired,
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
    kappa_rule,
    read_frozen_kappa,
    sample_covariance,
    write_frozen_kappa,
)
from lisjong_arena.draw_correction.paired_run import (
    RESULTS_FILENAME,
    PairedRunError,
    produce_paired_split,
    read_paired_split,
)
from lisjong_arena.draw_correction.plan import (
    EVAL,
    TRAIN,
    DrawCorrectionPlanError,
    read_plan,
    write_plan,
)
from lisjong_arena.draw_correction.projection import (
    DrawProjectionError,
    project_pre_draw,
    project_record,
    tile_category,
)
from lisjong_arena.learning_candidate_engine_aabb.statistics import block_rank_totals

_TRAIN_SEEDS = (598101, 598102)
_EVAL_SEEDS = (598103,)
_PARTICIPANTS = {"candidate": "shanten-policy@test", "incumbent": "minimal-policy@test"}
_SWAPPED = {"candidate": "minimal-policy@test", "incumbent": "shanten-policy@test"}
_REVISIONS = {"arena": "a" * 40, "engine": "e" * 40, "lisjong": "1" * 40}
_FACTORIES = {"candidate": ShantenPolicy, "incumbent": MinimalPolicy}
_SWAPPED_FACTORIES = {"candidate": MinimalPolicy, "incumbent": ShantenPolicy}

_ROOT = None
_STATE = {}


def setUpModule():
    """fixture planの全splitを1回だけ実行する（module内の全testで共有する）。"""
    global _ROOT
    _ROOT = tempfile.TemporaryDirectory()
    root = Path(_ROOT.name)
    plan = write_plan(
        root / "plan.json",
        participants=_PARTICIPANTS,
        revisions=_REVISIONS,
        minimum_blocks=2,
        train_seeds=_TRAIN_SEEDS,
        evaluation_seeds=_EVAL_SEEDS,
    )
    other = write_plan(
        root / "other-plan.json",
        participants=_SWAPPED,
        revisions=_REVISIONS,
        minimum_blocks=2,
        train_seeds=(598901,),
        evaluation_seeds=_EVAL_SEEDS,
    )
    _STATE["plan"] = plan
    _STATE["other_plan"] = other
    for name, target_plan, role, factories in (
        ("train", plan, TRAIN, _FACTORIES),
        ("eval", plan, EVAL, _FACTORIES),
        ("other_eval", other, EVAL, _SWAPPED_FACTORIES),
    ):
        _STATE[name] = produce_paired_split(
            root / name,
            target_plan,
            role,
            policy_factories=factories,
            access="test-only",
            retention="test-only",
        )


def tearDownModule():
    _ROOT.cleanup()


def _root() -> Path:
    return Path(_ROOT.name)


def _copy(name: str) -> Path:
    target = Path(tempfile.mkdtemp(dir=_root())) / name
    shutil.copytree(_root() / name, target)
    return target


class PlanTest(unittest.TestCase):
    def test_round_trip_and_identity(self):
        plan = read_plan(_root() / "plan.json")
        self.assertEqual(plan, _STATE["plan"])
        self.assertEqual(plan.train_seeds, _TRAIN_SEEDS)
        self.assertEqual(plan.evaluation_seeds, _EVAL_SEEDS)
        self.assertNotEqual(plan.sha256, _STATE["other_plan"].sha256)

    def test_write_once(self):
        with self.assertRaises(FileExistsError):
            write_plan(
                _root() / "plan.json",
                participants=_PARTICIPANTS,
                revisions=_REVISIONS,
                minimum_blocks=2,
                train_seeds=_TRAIN_SEEDS,
                evaluation_seeds=_EVAL_SEEDS,
            )

    def test_invalid_plans_are_rejected(self):
        base = dict(
            participants=_PARTICIPANTS,
            revisions=_REVISIONS,
            minimum_blocks=2,
            train_seeds=[1, 2],
            evaluation_seeds=[3],
        )
        cases = {
            "overlap": {"evaluation_seeds": [2, 3]},
            "duplicate": {"train_seeds": [1, 1]},
            "empty": {"evaluation_seeds": []},
            "same_participants": {"participants": {"candidate": "x", "incumbent": "x"}},
            "bad_revision": {"revisions": {**_REVISIONS, "engine": "short"}},
            "minimum": {"minimum_blocks": 1},
        }
        for name, change in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(DrawCorrectionPlanError):
                    write_plan(Path(directory) / "plan.json", **{**base, **change})

    def test_non_canonical_plan_bytes_are_rejected(self):
        path = _root() / "crlf-plan.json"
        text = (_root() / "plan.json").read_bytes().replace(b"\n", b"\r\n")
        path.write_bytes(text)
        with self.assertRaises(DrawCorrectionPlanError):
            read_plan(path)


class PairedRunTest(unittest.TestCase):
    def test_read_back_matches_the_plan(self):
        split = read_paired_split(_root() / "train", _STATE["plan"])
        self.assertEqual(split.role, TRAIN)
        self.assertEqual(split.seeds, _TRAIN_SEEDS)
        self.assertEqual(len(split.rows), 12)

    def test_write_once(self):
        with self.assertRaises(FileExistsError):
            produce_paired_split(
                _root() / "train",
                _STATE["plan"],
                TRAIN,
                policy_factories=_FACTORIES,
                access="test-only",
                retention="test-only",
            )

    def test_results_from_another_run_with_the_same_seeds_are_rejected(self):
        # 同じseed・全6配置・同じ局identityでも、別のparticipantで実行した有効な結果row
        target = _copy("eval")
        shutil.copyfile(
            _root() / "other_eval" / RESULTS_FILENAME, target / RESULTS_FILENAME
        )
        with self.assertRaises(PairedRunError):
            read_paired_split(target, _STATE["plan"])

    def test_capture_from_another_run_with_the_same_seeds_is_rejected(self):
        target = _copy("eval")
        shutil.rmtree(target / "capture")
        shutil.copytree(_root() / "other_eval" / "capture", target / "capture")
        with self.assertRaises(PairedRunError):
            read_paired_split(target, _STATE["plan"])

    def test_consistent_artifact_with_another_participant_is_rejected(self):
        # manifestのdigestまで書き直した「内部では整合した」artifactでも、
        # 結果rowのparticipantがplanと違えば拒否する
        target = _copy("eval")
        manifest_path = target / paired_run.PAIRED_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = [
            json.loads(line)
            for line in (target / RESULTS_FILENAME)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        for row in rows:
            for seat in row["seats"]:
                if seat["role"] == "B":
                    seat["policy_identity"] = "another-policy@test"
        text = "".join(paired_run._line(row) for row in rows)
        (target / RESULTS_FILENAME).write_text(text, encoding="utf-8", newline="\n")
        data = text.encode("utf-8")
        manifest["results"].update(
            bytes=len(data), sha256=paired_run.sha256_bytes(data)
        )
        for entry, row in zip(manifest["hanchans"], rows, strict=True):
            entry["result_row_sha256"] = paired_run._digest_lines([row])
        manifest_path.write_text(
            paired_run.canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        with self.assertRaisesRegex(PairedRunError, "participant"):
            read_paired_split(target, _STATE["plan"])

    def test_artifact_of_another_plan_is_rejected(self):
        with self.assertRaises(PairedRunError):
            read_paired_split(_root() / "other_eval", _STATE["plan"])

    def test_tampered_manifest_field_is_rejected(self):
        for field, value in (
            ("role", TRAIN),
            ("seeds", list(_TRAIN_SEEDS)),
            ("revisions", {**_REVISIONS, "engine": "f" * 40}),
        ):
            with self.subTest(field=field):
                target = _copy("eval")
                path = target / paired_run.PAIRED_MANIFEST_FILENAME
                manifest = json.loads(path.read_text(encoding="utf-8"))
                manifest[field] = value
                path.write_text(
                    paired_run.canonical_json_text(manifest),
                    encoding="utf-8",
                    newline="\n",
                )
                with self.assertRaises(PairedRunError):
                    read_paired_split(target, _STATE["plan"])


class ProjectionTest(unittest.TestCase):
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
        for record in _STATE["train"].capture.records:
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
        record = _STATE["train"].capture.records[0]
        probability, value = project_pre_draw(record.pre_draw)
        self.assertEqual(project_record(record).probability, probability)
        self.assertEqual(project_record(record).value, value)
        with self.assertRaises(DrawProjectionError):
            project_record({"pre_draw": record.pre_draw})


class JoinTest(unittest.TestCase):
    def test_rank_contrast_and_correction_contrast_use_the_same_weights(self):
        split = _STATE["train"]
        joined = join_paired(split)
        self.assertEqual(
            (joined.role, joined.plan_sha256), (TRAIN, _STATE["plan"].sha256)
        )
        self.assertEqual(joined.seeds, _TRAIN_SEEDS)
        ranks = block_rank_totals(list(split.rows))
        for block, rank in zip(joined.blocks, ranks, strict=True):
            self.assertEqual(block.d, Fraction(rank["difference_ranks"], 12))

        # 手で再計算（最初のblock）: 席ごとの Σ_t c_t を role へ割り当て、(X_B − X_A) / 12
        seed = _TRAIN_SEEDS[0]
        totals = {"A": Fraction(0), "B": Fraction(0)}
        for row in split.rows[:6]:
            seat_sums = [Fraction(0)] * 4
            for record in split.capture.records:
                if (record.identity["seed"], record.identity["placement"]) != (
                    seed,
                    row["placement"],
                ):
                    continue
                projected = project_record(record)
                c = unit_draw_corrections(
                    projected.probability, projected.value, projected.realized_category
                )
                seat_sums = [a + b for a, b in zip(seat_sums, c, strict=True)]
            for seat, assignment in enumerate(row["seats"]):
                totals[assignment["role"]] += seat_sums[seat]
        self.assertEqual(joined.blocks[0].x, (totals["B"] - totals["A"]) / 12)
        self.assertNotEqual(joined.blocks[0].x, 0)

    def test_placement_weights_and_sign(self):
        # 全ツモで seat 0 にだけ +1 の補正 -> seat 0 の role は A,A,A,B,B,B
        split = _STATE["eval"]
        with patch.object(
            join_module, "unit_draw_corrections", return_value=(1, 0, 0, 0)
        ):
            (block,) = join_paired(split).blocks
        draws = [0] * 6
        for record in split.capture.records:
            draws[record.identity["placement"]] += 1
        self.assertEqual(block.x, Fraction(sum(draws[3:]) - sum(draws[:3]), 12))

    def test_zero_value_gives_zero_contrast(self):
        with patch.object(
            join_module, "unit_draw_corrections", return_value=(0, 0, 0, 0)
        ):
            (block,) = join_paired(_STATE["eval"]).blocks
        self.assertEqual(block.x, 0)

    def test_join_requires_a_paired_split(self):
        with self.assertRaises(DrawCorrectionJoinError):
            join_paired(_STATE["eval"].capture)

    def test_incomplete_or_mismatched_inputs_are_rejected(self):
        # 公開入口の手前の内部照合（paired artifactの読込を通った後の防御）
        split = _STATE["eval"]
        artifact, rows = split.capture, list(split.rows)
        partial = json.loads(json.dumps(artifact.manifest))
        partial["plan"] = partial["plan"][:5]
        records = list(artifact.records)
        pre_draw = json.loads(json.dumps(records[0].pre_draw))
        pre_draw["checkpoint"]["dealer_seat"] = (
            pre_draw["checkpoint"]["dealer_seat"] + 1
        ) % 4
        records[0] = dataclasses.replace(records[0], pre_draw=pre_draw)
        extra_round = json.loads(json.dumps(artifact.manifest))
        extra_round["hanchans"][0]["rounds"].append(
            {"live_draw_count": 0, "rinshan_draw_count": 0, "round_ordinal": 99}
        )
        cases = {
            "missing_placement": (
                dataclasses.replace(artifact, manifest=partial),
                rows,
            ),
            "extra_row": (artifact, [*rows, rows[0]]),
            "reordered": (artifact, [rows[1], rows[0], *rows[2:]]),
            "dealer": (dataclasses.replace(artifact, records=tuple(records)), rows),
            "round_count": (dataclasses.replace(artifact, manifest=extra_round), rows),
        }
        for name, (case_artifact, case_rows) in cases.items():
            with self.subTest(case=name), self.assertRaises(DrawCorrectionJoinError):
                join_module._join_blocks(case_artifact, case_rows)


class KappaRuleTest(unittest.TestCase):
    def test_rule_branches(self):
        # D = (1, 2, 3)、X = (1, 1, 4): Cov = 3/2、Var(X) = 3 -> κ = 1/2
        ds = [Fraction(v) for v in (1, 2, 3)]
        xs = [Fraction(v) for v in (1, 1, 4)]
        covariance, variance = sample_covariance(ds, xs), sample_covariance(xs, xs)
        self.assertEqual((covariance, variance), (Fraction(3, 2), 3))
        self.assertEqual(
            kappa_rule(3, 2, covariance, variance),
            (Fraction(1, 2), ESTABLISHED, BRANCH_RATIO),
        )
        self.assertEqual(
            kappa_rule(3, 2, Fraction(-1), Fraction(1)),
            (0, ESTABLISHED, BRANCH_NON_POSITIVE),
        )
        self.assertEqual(
            kappa_rule(3, 2, Fraction(0), Fraction(0)),
            (0, NOT_ESTABLISHED, BRANCH_ZERO_VARIANCE),
        )
        self.assertEqual(
            kappa_rule(3, 4, covariance, variance),
            (0, NOT_ESTABLISHED, BRANCH_INSUFFICIENT),
        )
        self.assertEqual(
            kappa_rule(1, 2, None, None), (0, NOT_ESTABLISHED, BRANCH_INSUFFICIENT)
        )


class KappaTest(unittest.TestCase):
    def setUp(self):
        self.plan = _STATE["plan"]
        self.train = join_paired(_STATE["train"])
        self.directory = Path(tempfile.mkdtemp(dir=_root()))

    def test_estimate_uses_plan_minimum_and_train_blocks(self):
        estimate = estimate_kappa(self.plan, self.train)
        self.assertEqual((estimate.k, estimate.minimum_blocks), (2, 2))
        ds = [block.d for block in self.train.blocks]
        xs = [block.x for block in self.train.blocks]
        self.assertEqual(
            (estimate.kappa, estimate.status, estimate.branch),
            kappa_rule(2, 2, sample_covariance(ds, xs), sample_covariance(xs, xs)),
        )

    def test_role_plan_and_seed_mismatches_are_rejected(self):
        evaluation = join_paired(_STATE["eval"])
        other = join_paired(_STATE["other_eval"])
        cases = {
            "evaluation_as_train": (self.plan, evaluation),
            "another_plan": (self.plan, other),
            "seed_subset": (
                self.plan,
                dataclasses.replace(self.train, blocks=self.train.blocks[:1]),
            ),
            "plan_of_other": (_STATE["other_plan"], self.train),
        }
        for name, (plan, blocks) in cases.items():
            with self.subTest(case=name), self.assertRaises(DrawCorrectionKappaError):
                estimate_kappa(plan, blocks)

    def test_non_finite_or_inexact_values_are_rejected(self):
        for bad in (float("nan"), float("inf"), 0.5):
            blocks = (
                dataclasses.replace(self.train.blocks[0], x=bad),
                self.train.blocks[1],
            )
            with self.subTest(bad=bad), self.assertRaises(DrawCorrectionKappaError):
                estimate_kappa(
                    self.plan, dataclasses.replace(self.train, blocks=blocks)
                )

    def test_frozen_round_trip_and_recorded_digest(self):
        estimate = estimate_kappa(self.plan, self.train)
        frozen = write_frozen_kappa(
            self.directory / "kappa.json", self.plan, self.train, estimate
        )
        self.assertEqual(frozen.estimate, estimate)
        self.assertEqual(frozen.train_seeds, _TRAIN_SEEDS)
        again = read_frozen_kappa(
            self.directory / "kappa.json", self.plan, expected_sha256=frozen.sha256
        )
        self.assertEqual(again, frozen)
        with self.assertRaises(DrawCorrectionKappaError):
            read_frozen_kappa(
                self.directory / "kappa.json", self.plan, expected_sha256="0" * 64
            )
        with self.assertRaises(DrawCorrectionKappaError):
            read_frozen_kappa(self.directory / "kappa.json", _STATE["other_plan"])
        with self.assertRaises(FileExistsError):
            write_frozen_kappa(
                self.directory / "kappa.json", self.plan, self.train, estimate
            )

    def test_estimate_must_match_train(self):
        estimate = dataclasses.replace(
            estimate_kappa(self.plan, self.train), kappa=Fraction(7)
        )
        with self.assertRaises(DrawCorrectionKappaError):
            write_frozen_kappa(
                self.directory / "k.json", self.plan, self.train, estimate
            )
        self.assertFalse((self.directory / "k.json").exists())

    def test_tampered_frozen_artifacts_are_rejected(self):
        estimate = estimate_kappa(self.plan, self.train)
        write_frozen_kappa(
            self.directory / "kappa.json", self.plan, self.train, estimate
        )
        original = json.loads(
            (self.directory / "kappa.json").read_text(encoding="utf-8")
        )
        tamperings = {
            "kappa": lambda d: d.update(kappa={"denominator": 1, "numerator": 99}),
            "status": lambda d: d.update(
                status=ESTABLISHED if d["status"] != ESTABLISHED else NOT_ESTABLISHED
            ),
            "train_seeds": lambda d: d.update(train_seeds=[598101]),
            "participants": lambda d: d.update(participants=_SWAPPED),
            "revisions": lambda d: d["revisions"].update(engine="f" * 40),
            "plan": lambda d: d.update(plan_sha256="0" * 64),
            "value_definition": lambda d: d["value_definition"].update(version=2),
            "unknown_field": lambda d: d.update(extra=1),
            "not_lowest_terms": lambda d: d.update(
                kappa={"denominator": 4, "numerator": 2}
            ),
        }
        for name, tamper in tamperings.items():
            with self.subTest(case=name):
                document = json.loads(json.dumps(original))
                tamper(document)
                path = self.directory / f"{name}.json"
                path.write_text(
                    paired_run.canonical_json_text(document),
                    encoding="utf-8",
                    newline="\n",
                )
                with self.assertRaises(DrawCorrectionKappaError):
                    read_frozen_kappa(path, self.plan)

    def test_non_canonical_bytes_are_rejected(self):
        estimate = estimate_kappa(self.plan, self.train)
        write_frozen_kappa(
            self.directory / "kappa.json", self.plan, self.train, estimate
        )
        path = self.directory / "crlf.json"
        path.write_bytes(
            (self.directory / "kappa.json").read_bytes().replace(b"\n", b"\r\n")
        )
        with self.assertRaises(DrawCorrectionKappaError):
            read_frozen_kappa(path, self.plan)


class AnalysisEntryTest(unittest.TestCase):
    """評価の入口: plan・凍結κ（記録したdigest）・EVALを自分で読み戻して照合する。"""

    @classmethod
    def setUpClass(cls):
        cls.directory = Path(tempfile.mkdtemp(dir=_root()))
        plan = _STATE["plan"]
        train = join_paired(_STATE["train"])
        cls.frozen = write_frozen_kappa(
            cls.directory / "kappa.json", plan, train, estimate_kappa(plan, train)
        )

    def _analyze(self, **overrides):
        arguments = dict(
            plan_path=_root() / "plan.json",
            frozen_path=self.directory / "kappa.json",
            frozen_sha256=self.frozen.sha256,
            evaluation_path=_root() / "eval",
            bootstrap_resamples=20,
            bootstrap_seed=1,
        )
        arguments.update(overrides)
        return analyze_correction(**arguments)

    def test_evaluation_of_the_plan_eval_split(self):
        result = self._analyze()
        self.assertEqual(result["plan_sha256"], _STATE["plan"].sha256)
        self.assertEqual(result["frozen_kappa_sha256"], self.frozen.sha256)
        self.assertEqual(result["evaluation_seeds"], list(_EVAL_SEEDS))
        # EVALは1 block（fixture）-> r は定義されない（正常な退化）
        self.assertEqual(result["ratio"]["status"], R_NOT_ESTABLISHED)
        json.dumps(result, allow_nan=False)

    def test_wrong_inputs_are_rejected(self):
        cases = {
            "frozen_digest": {"frozen_sha256": "0" * 64},
            "train_as_evaluation": {"evaluation_path": _root() / "train"},
            "evaluation_of_another_plan": {"evaluation_path": _root() / "other_eval"},
            "another_plan": {"plan_path": _root() / "other-plan.json"},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(
                    (DrawCorrectionPlanError, DrawCorrectionKappaError)
                ):
                    self._analyze(**change)


def _synthetic(pairs, *, first_seed=2000):
    return tuple(
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
    )


class SummaryArithmeticTest(unittest.TestCase):
    def test_kappa_zero_leaves_d_unchanged(self):
        blocks = _synthetic([(1, 5), (-2, 7), (Fraction(1, 12), -3)])
        result = summarize_blocks(
            Fraction(0), blocks, bootstrap_resamples=50, bootstrap_seed=1
        )
        self.assertEqual(result["ratio"]["r_exact"], "1/1")
        self.assertEqual(result["mean_d"], result["mean_d_adjusted"])

    def test_ratio_is_exact_and_bootstrap_is_deterministic(self):
        blocks = _synthetic([(1, 2), (-1, -2), (2, 2), (0, 0), (1, 0), (-2, -2)])
        kappa = Fraction(1, 2)
        first = summarize_blocks(
            kappa, blocks, bootstrap_resamples=200, bootstrap_seed=7
        )
        again = summarize_blocks(
            kappa, blocks, bootstrap_resamples=200, bootstrap_seed=7
        )
        self.assertEqual(first, again)
        ds = [Fraction(v) for v in (1, -1, 2, 0, 1, -2)]
        adjusted = [Fraction(v) for v in (0, 0, 1, 0, 1, -1)]
        mean, mean_a = sum(ds) / 6, sum(adjusted) / 6
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
        kappa = Fraction(1, 2)
        flat = summarize_blocks(
            kappa,
            _synthetic([(1, 1), (1, 2), (1, 3)]),
            bootstrap_resamples=20,
            bootstrap_seed=1,
        )
        self.assertEqual(flat["ratio"]["status"], R_NOT_ESTABLISHED)
        self.assertIsNone(flat["ratio"]["r"])
        self.assertEqual(flat["bootstrap"]["status"], CI_NOT_ESTABLISHED)
        single = summarize_blocks(
            kappa, _synthetic([(1, 1)]), bootstrap_resamples=20, bootstrap_seed=1
        )
        self.assertEqual(single["ratio"]["status"], R_NOT_ESTABLISHED)
        two = summarize_blocks(
            kappa,
            _synthetic([(1, 1), (2, 1)]),
            bootstrap_resamples=200,
            bootstrap_seed=1,
        )
        self.assertEqual(two["ratio"]["status"], R_ESTABLISHED)
        self.assertEqual(two["bootstrap"]["status"], CI_NOT_ESTABLISHED)
        for result in (flat, single, two):
            json.dumps(result, allow_nan=False)

    def test_extremes_and_correction_summary(self):
        result = summarize_blocks(
            Fraction(1, 2),
            _synthetic([(0, 4), (0, -6), (1, 2)]),
            bootstrap_resamples=10,
            bootstrap_seed=1,
            extreme_count=2,
        )
        self.assertEqual(
            result["corrections"]["extreme_blocks"],
            [{"kappa_x": "-3/1", "seed": 2001}, {"kappa_x": "2/1", "seed": 2000}],
        )
        self.assertEqual(result["corrections"]["block_kappa_x_mean"], "0/1")

    def test_inexact_or_negative_inputs_are_rejected(self):
        blocks = _synthetic([(1, 1), (2, 2)])
        bad_block = (dataclasses.replace(blocks[0], d=float("nan")), blocks[1])
        for kappa, case_blocks in (
            (0.5, blocks),
            (Fraction(-1), blocks),
            (Fraction(1), bad_block),
            (Fraction(1), ()),
        ):
            with self.subTest(kappa=kappa), self.assertRaises(DrawCorrectionKappaError):
                summarize_blocks(
                    kappa, case_blocks, bootstrap_resamples=10, bootstrap_seed=1
                )


if __name__ == "__main__":
    unittest.main()
