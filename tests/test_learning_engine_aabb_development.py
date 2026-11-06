"""#567 開発準備入口（固定18半荘・保存物verify・再現比較）。

実engine・実Policy・実artifactの代わりに、spawn workerで#513のfixture半荘と合成行動を
返す ``fake_dev_play*`` を使う。合成値であり、強さの結果・実測ではない。
"""

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _learning_engine_aabb_fixtures as fx

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb.executor import RunStopped
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    read_kyoku_records,
)
from lisjong_arena.learning_candidate_engine_aabb import __main__ as cli
from lisjong_arena.learning_candidate_engine_aabb import development as dev
from lisjong_arena.learning_candidate_engine_aabb import protocol, runtime
from lisjong_arena.learning_candidate_engine_aabb.lock import (
    LearningEngineAabbLockError,
    parse_lock_document,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity


def _redigest(plan):
    payload = {key: item for key, item in plan.items() if key != "plan_digest"}
    return {**payload, "plan_digest": document_identity(payload)}


def _resum(output: Path) -> None:
    for directory in (output / dev.DEVELOPMENT, output / dev.REPEAT, output):
        (directory / dev.SUMS_FILENAME).unlink()
        dev.write_sums(directory)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger_path = self.tmp / "ledger.json"
        seed_registry.write_ledger(self.ledger_path, fx.ledger())
        self.plan = fx.development_plan(self.tmp)
        self.plan_path = dev.save_plan(self.plan, self.tmp / "plan.json")
        markers = self.tmp / "markers"
        markers.mkdir()
        patcher = mock.patch.dict(os.environ, {fx.MARKER_ENV: str(markers)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_dev(self, play=fx.fake_dev_play, **kwargs):
        kwargs.setdefault("live_gate", fx.development_gate)
        kwargs.setdefault(
            "os_limits", lambda: fx.os_limits(self.plan["execution"]["limits"])
        )
        kwargs.setdefault("parent_peak_rss_kib", fx.parent_peak_rss_kib)
        kwargs.setdefault("ledger_path", self.ledger_path)
        return dev.run_development(self.plan_path, play=play, **kwargs)

    @property
    def output(self) -> Path:
        return Path(self.plan["output_dir"])

    def assert_nothing_published(self):
        self.assertFalse(self.output.exists())
        self.assertEqual(
            [p.name for p in self.tmp.iterdir() if "staging" in p.name], []
        )
        stops = list((self.tmp / "diagnostics").glob("stop-*.json"))
        self.assertEqual(len(stops), 1)
        stop = json.loads(stops[0].read_text("utf-8"))
        self.assertTrue(stop["not_a_result"])
        self.assertEqual(stop["status"], "STOP / INVALID")
        self.assertEqual(stop["plan_digest"], self.plan["plan_digest"])
        shutil.rmtree(self.tmp / "diagnostics")
        return stop


class FixedScheduleTest(_Case):
    """seed・配置・件数は定数で、外部入力では変えられない。"""

    def test_schedule_is_the_fixed_18_hanchans(self):
        runs = dev.schedule_document()["runs"]
        self.assertEqual(
            [(item["seed"], item["placement"]) for item in runs[dev.DEVELOPMENT]],
            [
                (seed, placement)
                for seed in (472_005, 472_006)
                for placement in range(6)
            ],
        )
        self.assertEqual(
            [(item["seed"], item["placement"]) for item in runs[dev.REPEAT]],
            [(472_005, placement) for placement in range(6)],
        )
        self.assertEqual(self.plan["schedule"], dev.schedule_document())

    def test_no_entry_point_takes_seeds_or_counts(self):
        for function in (dev.assemble_plan, dev.build_plan, dev.run_development):
            parameters = set(inspect.signature(function).parameters)
            self.assertFalse(
                parameters & {"seeds", "ordered_seeds", "schedule", "k", "runs"},
                function.__name__,
            )
        for argument in ("--seeds", "--k"):
            with self.subTest(argument=argument):
                with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                    cli._parser().parse_args(
                        ["dev-plan", argument, "1", "--plan", "p.json"]
                    )

    def test_a_changed_schedule_is_rejected_even_with_a_new_digest(self):
        def changed(mutate):
            plan = json.loads(json.dumps(self.plan))
            mutate(plan["schedule"]["runs"])
            plan["schedule_digest"] = document_identity(plan["schedule"])
            return _redigest(plan)

        for name, mutate in (
            ("other seed", lambda r: r[dev.DEVELOPMENT][0].update(seed=472_004)),
            ("drop", lambda r: r[dev.REPEAT].pop()),
            ("add", lambda r: r[dev.REPEAT].append(dict(r[dev.REPEAT][0]))),
            ("order", lambda r: r[dev.DEVELOPMENT].reverse()),
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(dev.LearningDevelopmentError, "18"):
                    dev.parse_plan(changed(mutate))

    def test_plan_is_not_a_formal_lock(self):
        self.assertIs(self.plan["not_a_strength_result"], True)
        self.assertEqual(self.plan["evaluation_kind"], "DEVELOPMENT")
        text = json.dumps(self.plan)
        for word in ("FORMAL-EVAL", "allocation_binding", "ordered_seeds"):
            self.assertNotIn(word, text)
        with self.assertRaises(LearningEngineAabbLockError):
            parse_lock_document(self.plan)

    def test_plan_tampering_is_rejected(self):
        for name, mutate in (
            ("not a strength result", lambda p: p.update(not_a_strength_result=False)),
            ("kind", lambda p: p.update(evaluation_kind="FORMAL")),
            ("limits", lambda p: p["execution"]["limits"].update(wall_seconds=1)),
            ("digest only", None),
        ):
            with self.subTest(name=name):
                plan = json.loads(json.dumps(self.plan))
                if mutate is None:
                    plan["plan_digest"] = "0" * 64
                    expected = "digest"
                else:
                    mutate(plan)
                    plan = _redigest(plan)
                    expected = "."
                with self.assertRaisesRegex(dev.LearningDevelopmentError, expected):
                    dev.parse_plan(plan)


class SeedRegistryTest(_Case):
    def collide(self, domain):
        allocation = fx.allocation(
            event=990, seeds=(472_006, 990_000), domain=domain, protocol="other"
        )
        return fx.ledger(allocation)

    def test_seeds_allocated_in_any_domain_are_rejected(self):
        for domain in (protocol.SEED_DOMAIN, fx.OTHER_DOMAIN):
            with self.subTest(domain=domain):
                with self.assertRaisesRegex(dev.LearningDevelopmentError, "472006"):
                    fx.development_plan(self.tmp, ledger=self.collide(domain))

    def test_live_gate_rejects_a_later_allocation(self):
        seed_registry.write_ledger(
            self.ledger_path.with_name("late.json"), self.collide(fx.OTHER_DOMAIN)
        )
        with self.assertRaisesRegex(dev.LearningDevelopmentError, "collide"):
            self.run_dev(ledger_path=self.ledger_path.with_name("late.json"))
        self.assertEqual(self.assert_nothing_published()["completed_hanchans"], 0)

    def test_run_does_not_touch_the_ledger(self):
        before = self.ledger_path.read_bytes()
        self.run_dev()
        self.assertEqual(self.ledger_path.read_bytes(), before)


class SuccessfulRunTest(unittest.TestCase):
    """完走した18半荘（class単位で1回だけ実行し、改ざんはcopyに対して行う）。"""

    @classmethod
    def setUpClass(cls):
        case = _Case()
        case.setUp()
        cls._case = case
        cls.report = case.run_dev()
        cls.source = case.output
        cls.plan = case.plan

    @classmethod
    def tearDownClass(cls):
        cls._case.doCleanups()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.output = self.tmp / "output"
        shutil.copytree(self.source, self.output)

    def test_report_is_facts_only(self):
        report = self.report
        self.assertEqual(report["evaluation_kind"], "DEVELOPMENT")
        self.assertIs(report["not_a_strength_result"], True)
        for key in ("statistic", "classification", "blocks", "result_digest"):
            self.assertNotIn(key, report)
        self.assertEqual(report["runs"][dev.DEVELOPMENT]["hanchan_count"], 12)
        self.assertEqual(report["runs"][dev.REPEAT]["hanchan_count"], 6)
        self.assertEqual(report["reproduction"]["status"], dev.MATCH)
        self.assertEqual(len(report["reproduction"]["hanchans"]), 6)
        metrics = report["runs"][dev.DEVELOPMENT]["secondary_metrics"]
        self.assertEqual(metrics["schema"], dev.SECONDARY_METRICS_SCHEMA)

    def test_stored_files_and_sums(self):
        names = sorted(
            path.relative_to(self.source).as_posix()
            for path in self.source.rglob("*")
            if path.is_file()
        )
        for expected in (
            "SHA256SUMS",
            "plan.json",
            "report.json",
            "observations.json",
            "development/SHA256SUMS",
            "development/actions.json",
            "development/raw/manifest.json",
            "repeat/SHA256SUMS",
            "repeat/actions.json",
        ):
            self.assertIn(expected, names)
        observations = json.loads((self.source / "observations.json").read_text())
        self.assertEqual(len(observations["runs"][dev.DEVELOPMENT]), 12)
        self.assertEqual(observations["parent_peak_rss_kib"], fx.PARENT_PEAK_RSS_KIB)

    def test_raw_cannot_be_read_as_formal_records(self):
        raw = self.source / dev.DEVELOPMENT / dev.RAW_DIRNAME
        read_kyoku_records(raw, event_facts_required=False, layout=dev.LAYOUT)
        with self.assertRaises(Exception):
            read_kyoku_records(raw, event_facts_required=False, layout=protocol.LAYOUT)

    def test_verify_does_not_start_the_engine_or_a_policy(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("verify started the engine or a Policy")

        with (
            mock.patch.object(runtime, "run_policy_hanchan", forbidden),
            mock.patch.object(runtime, "load_participants", forbidden),
            mock.patch.object(runtime, "_load_l2a", forbidden),
            mock.patch.object(runtime, "_load_arm_y", forbidden),
            mock.patch.object(dev, "run_tasks", forbidden),
            mock.patch.object(dev, "live_values", forbidden),
        ):
            self.assertEqual(dev.verify_development(self.output), self.report)

    def test_verify_in_a_separate_process(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "lisjong_arena.learning_candidate_engine_aabb",
                "dev-verify",
                "--output-dir",
                str(self.output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        printed = json.loads(completed.stdout)
        self.assertEqual(printed["report_digest"], self.report["report_digest"])
        self.assertEqual(printed["reproduction"], dev.MATCH)

    def rewrite(self, name, mutate):
        path = self.output / name
        document = json.loads(path.read_text("utf-8"))
        mutate(document)
        path.write_text(
            dev.canonical_json_text(document), encoding="utf-8", newline="\n"
        )

    def test_changed_files_fail_the_sums(self):
        (self.output / "report.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(dev.LearningDevelopmentError, "SHA256SUMS"):
            dev.verify_development(self.output)

    def test_missing_or_extra_files_fail_the_sums(self):
        for name, damage in (
            ("extra", lambda o: (o / "repeat" / "extra.txt").write_text("x")),
            ("missing", lambda o: (o / "development" / "actions.json").unlink()),
        ):
            with self.subTest(name=name):
                shutil.rmtree(self.output)
                shutil.copytree(self.source, self.output)
                damage(self.output)
                with self.assertRaisesRegex(dev.LearningDevelopmentError, "SHA256SUMS"):
                    dev.verify_development(self.output)

    def test_consistent_tampering_is_caught_by_the_rederivation(self):
        """SHA256SUMSまで書き直した改ざんも、保存物からの再導出で拒否する。"""

        def metric(report):
            metrics = report["runs"][dev.DEVELOPMENT]["secondary_metrics"]
            metrics["schema"] = "tampered"

        def first_action(document):
            document["hanchans"][0]["actions"][0][1]["index"] = 99

        def action_sha(document):
            entry = document["hanchans"][0]
            entry["actions"].pop()
            entry["actions_sha256"] = dev._sha256_text(
                dev.canonical_json_text(entry["actions"])
            )

        cases = (
            ("report.json", metric, "re-derivation"),
            (
                "report.json",
                lambda r: r["reproduction"].update(status=dev.MISMATCH),
                "re-derivation",
            ),
            ("report.json", lambda r: r.update(not_a_strength_result=False), "re-der"),
            ("development/actions.json", first_action, "hash mismatch"),
            ("repeat/actions.json", action_sha, "Policy call counts"),
            (
                "observations.json",
                lambda o: o["summary"][dev.REPEAT].update(max_peak_rss_kib=1),
                "summary",
            ),
            (
                "observations.json",
                lambda o: o["runs"][dev.REPEAT].pop(),
                "every hanchan",
            ),
            ("plan.json", lambda p: p.update(owner_issue="x"), "drifted"),
        )
        for name, mutate, pattern in cases:
            with self.subTest(name=name, pattern=pattern):
                shutil.rmtree(self.output)
                shutil.copytree(self.source, self.output)
                self.rewrite(name, mutate)
                _resum(self.output)
                with self.assertRaisesRegex(dev.LearningDevelopmentError, pattern):
                    dev.verify_development(self.output)

    def test_raw_tampering_is_caught(self):
        path = self.output / dev.DEVELOPMENT / dev.RAW_DIRNAME / "kyoku-records.jsonl"
        lines = path.read_text("utf-8").splitlines(keepends=True)
        row = json.loads(lines[0])
        row["final_score"][0]["rank"], row["final_score"][1]["rank"] = (
            row["final_score"][1]["rank"],
            row["final_score"][0]["rank"],
        )
        lines[0] = dev.canonical_json_text(row)
        path.write_text("".join(lines), encoding="utf-8", newline="\n")
        _resum(self.output)
        with self.assertRaisesRegex(dev.LearningDevelopmentError, "raw record"):
            dev.verify_development(self.output)


class ReproductionTest(_Case):
    """再実行の差はSTOPではなく、reportにMISMATCHとして記録し、verifyでも再導出する。"""

    def assert_mismatch(self, play, placement, field):
        report = self.run_dev(play)
        reproduction = report["reproduction"]
        self.assertEqual(reproduction["status"], dev.MISMATCH)
        by_placement = {item["placement"]: item for item in reproduction["hanchans"]}
        self.assertEqual(by_placement[placement]["status"], dev.MISMATCH)
        self.assertTrue(
            any(
                name.startswith(field) for name in by_placement[placement]["differing"]
            ),
            by_placement[placement]["differing"],
        )
        others = [item for key, item in by_placement.items() if key != placement]
        self.assertTrue(all(item["status"] == dev.MATCH for item in others))
        self.assertEqual(dev.verify_development(self.output), report)
        with mock.patch("sys.stdout"):
            code = cli.main(["dev-verify", "--output-dir", str(self.output)])
        self.assertEqual(code, 3)

    def test_action_difference(self):
        self.assert_mismatch(fx.fake_dev_play_action_drift, 2, "actions")

    def test_settlement_difference(self):
        self.assert_mismatch(fx.fake_dev_play_record_drift, 0, "kyoku_record.")

    def test_time_and_rss_differences_are_ignored(self):
        """時間・RSSは決定的payloadではない（各半荘の観測値は毎回違ってよい）。"""
        report = self.run_dev()
        self.assertEqual(report["reproduction"]["status"], dev.MATCH)
        observations = json.loads((self.output / "observations.json").read_text())
        walls = [row["wall_seconds"] for row in observations["runs"][dev.REPEAT]]
        self.assertTrue(all(wall >= 0 for wall in walls))


class StopTest(_Case):
    """失敗・上限超過は全体STOPで、成功report・部分出力を残さない。"""

    def test_worker_failures(self):
        for play, pattern in (
            (fx.fake_dev_play_error, "synthetic Policy failure"),
            (fx.fake_dev_play_without_actions, "selected actions"),
        ):
            with self.subTest(play=play.__name__):
                with self.assertRaisesRegex(
                    (RunStopped, dev.LearningDevelopmentError), pattern
                ):
                    self.run_dev(play)
                self.assert_nothing_published()

    def test_timeout(self):
        plan = fx.development_plan(self.tmp, limits={**fx.LIMITS, "timeout_seconds": 3})
        self.plan = plan
        self.plan_path = dev.save_plan(plan, self.tmp / "short-plan.json")
        with self.assertRaisesRegex(RunStopped, "locked timeout"):
            self.run_dev(fx.fake_dev_play_slow)
        self.assertEqual(self.assert_nothing_published()["completed_hanchans"], 7)

    def test_memory_in_the_repeat_run(self):
        with self.assertRaisesRegex(RunStopped, "peak RSS"):
            self.run_dev(fx.fake_dev_play_large_rss)
        self.assertEqual(self.assert_nothing_published()["completed_hanchans"], 16)

    def test_wall_in_the_post_run_live_gate(self):
        now = {"value": 0.0}
        calls = []

        def gate(plan, *, ledger):
            fx.development_gate(plan, ledger=ledger)
            calls.append(1)
            if len(calls) == 2:
                now["value"] = fx.LIMITS["wall_seconds"] + 1

        with self.assertRaisesRegex(RunStopped, "post-run live gate"):
            self.run_dev(
                live_gate=gate, clock=lambda: now["value"], realtime=lambda: 0.0
            )
        self.assertEqual(self.assert_nothing_published()["completed_hanchans"], 18)

    def test_limits_while_saving(self):
        now = {"value": 0.0}
        original = dev.build_report

        def slow_report(*args, **kwargs):
            now["value"] = fx.LIMITS["wall_seconds"] + 1
            return original(*args, **kwargs)

        with mock.patch.object(dev, "build_report", side_effect=slow_report):
            with self.assertRaisesRegex(RunStopped, r"wall time .*before publishing"):
                self.run_dev(clock=lambda: now["value"], realtime=lambda: 0.0)
        self.assertEqual(self.assert_nothing_published()["completed_hanchans"], 18)

        calls = []

        def parent():
            calls.append(1)
            # 18半荘と観測値の記録までは小さく、公開直前に上限を超える
            return 1024 if len(calls) <= 19 else fx.LIMITS["peak_rss_kib"]

        with self.assertRaisesRegex(RunStopped, r"peak RSS .*before publishing"):
            self.run_dev(parent_peak_rss_kib=parent)
        self.assertEqual(self.assert_nothing_published()["completed_hanchans"], 18)

    def test_write_failure_while_saving(self):
        with mock.patch.object(
            dev, "write_sums", side_effect=OSError("injected write failure")
        ):
            with self.assertRaisesRegex(RunStopped, "injected write failure"):
                self.run_dev()
        self.assertEqual(self.assert_nothing_published()["completed_hanchans"], 18)
        # 何も公開していないので、同じplanはそのまま実行できる
        self.assertEqual(self.run_dev()["reproduction"]["status"], dev.MATCH)

    def test_os_limits_and_live_gate(self):
        observed = {**fx.os_limits(), "memory_swap_max_bytes": None}
        with self.assertRaisesRegex(RunStopped, "memory.swap.max"):
            self.run_dev(os_limits=lambda: observed)
        self.assertEqual(self.assert_nothing_published()["completed_hanchans"], 0)

        def gate(plan, *, ledger):
            raise dev.LearningDevelopmentError("live binding differs")

        with self.assertRaisesRegex(dev.LearningDevelopmentError, "live binding"):
            self.run_dev(live_gate=gate)
        self.assertEqual(self.assert_nothing_published()["completed_hanchans"], 0)

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        with self.assertRaisesRegex(dev.LearningDevelopmentError, "write-once"):
            self.run_dev()


if __name__ == "__main__":
    unittest.main()
