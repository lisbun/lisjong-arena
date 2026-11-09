"""#586 Overall lock v2 kyoku records; never runs a real RiichiEnv game."""

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from _overall_champion_aabb_fixtures import (
    HEURISTIC_ADVANTAGE,
    HEURISTIC_IDENTITY,
    clear_checkpoint,
    comparison_provenance,
    comparison_result,
    destinations,
    heuristic_binding,
    heuristic_spec,
    install_checkpoint,
    learning_binding,
    lock_document,
    merged_main_execution,
    save_synthetic_comparison,
    seeds,
    stub_heuristic_policy,
    stub_learning_policy,
    synthetic_kyoku_records,
    uniform_profile,
    v2_destinations,
)

import lisjong_arena.overall_champion_aabb.kyoku as kyoku_module
import lisjong_arena.overall_champion_aabb.lock as lock_module
import lisjong_arena.overall_champion_aabb.model_pair as model_pair
from lisjong_arena._parallel_execution import GameJob
from lisjong_arena.comparison import ComparisonExecutionError
from lisjong_arena.current_learning_champion import (
    create_current_learning_champion_policy,
)
from lisjong_arena.model import ComparisonPlan, PolicySpec
from lisjong_arena.overall_champion_aabb.experiment import run_overall_evaluation
from lisjong_arena.overall_champion_aabb.kyoku import (
    OverallKyokuRecordError,
    _KyokuJobOutcome,
    _run_kyoku_game_job,
    execute_with_kyoku_records,
    save_kyoku_files,
    summarize_kyoku_records,
    validate_kyoku_records,
    verify_kyoku_files,
)
from lisjong_arena.overall_champion_aabb.lock import (
    OverallChampionLockError,
    build_lock_document,
    document_identity,
    parse_lock_document,
    save_lock_document,
)
from lisjong_arena.overall_champion_aabb.model_pair import (
    BINDING,
    EXPECTED_COMPONENTS,
    FACTORY,
    PAIR_VERSION,
    POLICY_IDENTITY,
)
from lisjong_arena.overall_champion_aabb.protocol import GAME_MODE, MAX_STEPS
from lisjong_arena.overall_champion_aabb.result import (
    OverallChampionResultError,
    build_overall_result,
    load_bundle_comparison,
    save_overall_result,
    verify_overall_bundle,
)

H, L = HEURISTIC_IDENTITY, POLICY_IDENTITY
SMALL_SEEDS = (11, 12)


def _pair():
    return {
        "pair_version": PAIR_VERSION,
        "binding": BINDING,
        "components": copy.deepcopy(EXPECTED_COMPONENTS),
    }


def _l2a_binding():
    return learning_binding(
        factory_binding=FACTORY,
        policy_identity=POLICY_IDENTITY,
        checkpoint_binding=None,
        checkpoint_identity=None,
        checkpoint_digest=None,
    )


def _v2_patches():
    return (
        mock.patch.object(
            lock_module, "collect_ml_runtime", return_value={"torch": "2.13.0+cpu"}
        ),
        mock.patch.object(lock_module, "live_pair_document", return_value=_pair()),
    )


def _small(seed_population=SMALL_SEEDS):
    result = comparison_result(
        seed_population,
        uniform_profile(HEURISTIC_ADVANTAGE),
        heuristic_identity=H,
        learning_identity=L,
    )
    return result, synthetic_kyoku_records(result.seat_results)


def _validate(records, result, seed_population=SMALL_SEEDS):
    return validate_kyoku_records(
        records,
        seeds=seed_population,
        heuristic_identity=H,
        learning_identity=L,
        seat_results=result.seat_results,
    )


class ValidateKyokuRecordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.result, self.records = _small()

    def _rejects(self, mutate, pattern):
        records = copy.deepcopy(list(self.records))
        mutate(records)
        with self.assertRaisesRegex(OverallKyokuRecordError, pattern):
            _validate(records, self.result)

    def test_consistent_records_pass(self) -> None:
        self.assertEqual(len(_validate(self.records, self.result)), 8)

    def test_missing_and_duplicate_records_rejected(self) -> None:
        self._rejects(lambda r: r.pop(), "expected 8")
        self._rejects(lambda r: r.__setitem__(1, copy.deepcopy(r[0])), "order")

    def test_reordered_records_rejected(self) -> None:
        self._rejects(lambda r: r.reverse(), "order")

    def test_participant_swap_rejected(self) -> None:
        def swap(records):
            ids = records[0]["seat_identities"]
            ids[0], ids[2] = ids[2], ids[0]

        self._rejects(swap, "seat identities")

    def test_score_or_rank_mismatch_with_comparison_rejected(self) -> None:
        self._rejects(
            lambda r: r[3]["scores"].__setitem__(0, r[3]["scores"][0] + 100),
            "comparison",
        )
        self._rejects(lambda r: r[3]["ranks"].reverse(), "comparison")

    def test_point_components_must_reconcile(self) -> None:
        def bump(records):
            records[2]["kyokus"][0]["seats"][1]["tsumo_loss"] += 100

        self._rejects(bump, "reconcile")

    def test_kyoku_chain_must_end_at_final_score(self) -> None:
        def second_kyoku(records):
            first = copy.deepcopy(records[0]["kyokus"][0])
            first["index"] = 1
            records[0]["kyokus"].append(first)

        self._rejects(second_kyoku, "continue")

    def test_win_flags_must_match_counts(self) -> None:
        def flag(records):
            for seat in records[0]["kyokus"][0]["seats"]:
                if not seat["won"]:
                    seat["won"] = True
                    return

        self._rejects(flag, "won/win_count")

    def test_unknown_field_or_outcome_rejected(self) -> None:
        self._rejects(lambda r: r[0].__setitem__("extra", 1), "fields")
        self._rejects(
            lambda r: r[0]["kyokus"][0].__setitem__("outcome", "unknown"), "outcome"
        )


class SummarizeTest(unittest.TestCase):
    def test_direction_and_denominators(self) -> None:
        result, records = _small()
        summary = summarize_kyoku_records(
            records, heuristic_identity=H, learning_identity=L
        )
        self.assertEqual(summary["status"], "DIAGNOSTIC_ONLY")
        self.assertEqual(summary["seed_count"], 2)
        win = summary["paired"]["win_rate"]
        # 1 kyoku x 4 rotations x 2 seats per family per seed, 2 seeds.
        self.assertEqual(win["heuristic"]["seat_kyokus"], 16)
        self.assertEqual(win["learning"]["seat_kyokus"], 16)
        expected = win["learning"]["pooled"] - win["heuristic"]["pooled"]
        self.assertAlmostEqual(win["learning_minus_heuristic"]["mean"], expected)
        self.assertNotIn("primary_d", summary)


class KyokuFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.result, self.records = _small()
        self.records_path = self.path / "kyoku-records.json"
        self.summary_path = self.path / "kyoku-summary.json"
        save_kyoku_files(
            records=self.records,
            summary=summarize_kyoku_records(
                self.records, heuristic_identity=H, learning_identity=L
            ),
            lock_identity="lock-1",
            records_path=self.records_path,
            summary_path=self.summary_path,
        )

    def _verify(self, lock_identity="lock-1"):
        return verify_kyoku_files(
            records_path=self.records_path,
            summary_path=self.summary_path,
            lock_identity=lock_identity,
            seeds=SMALL_SEEDS,
            heuristic_identity=H,
            learning_identity=L,
            seat_results=self.result.seat_results,
        )

    def test_roundtrip_verifies(self) -> None:
        self.assertEqual(self._verify()["lock_identity"], "lock-1")

    def test_files_are_write_once(self) -> None:
        with self.assertRaises(Exception):
            save_kyoku_files(
                records=self.records,
                summary={},
                lock_identity="lock-1",
                records_path=self.records_path,
                summary_path=self.path / "other.json",
            )

    def test_other_lock_rejected(self) -> None:
        with self.assertRaisesRegex(OverallKyokuRecordError, "another lock"):
            self._verify("lock-2")

    def test_tampered_summary_rejected(self) -> None:
        summary = json.loads(self.summary_path.read_text(encoding="utf-8"))
        summary["paired"]["win_rate"]["learning"]["pooled"] += 0.01
        self.summary_path.write_text(json.dumps(summary), encoding="utf-8")
        with self.assertRaisesRegex(OverallKyokuRecordError, "re-derived"):
            self._verify()

    def test_tampered_records_rejected(self) -> None:
        document = json.loads(self.records_path.read_text(encoding="utf-8"))
        document["records"][0]["scores"][0] += 100
        self.records_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(OverallKyokuRecordError):
            self._verify()


class ExecuteWithKyokuRecordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = ComparisonPlan(
            policy_a=PolicySpec(H, stub_heuristic_policy),
            policy_b=PolicySpec(L, stub_learning_policy),
            seeds=SMALL_SEEDS,
            game_mode=GAME_MODE,
            max_steps=MAX_STEPS,
        )
        self.expected, records = _small()
        self.records = {(r["seed"], r["rotation"]): r for r in records}

    def _outcomes(self, jobs, *, fail=None, drop=None):
        outcomes = {}
        for job in reversed(list(jobs)):  # completion order must not matter
            key = (job.seed, job.rotation)
            record = self.records[key]
            result = SimpleNamespace(
                seed=job.seed,
                game_mode=job.game_mode,
                scores=tuple(record["scores"]),
                ranks=tuple(record["ranks"]),
            )
            outcomes[key] = _KyokuJobOutcome(
                seed=job.seed,
                rotation=job.rotation,
                result=None if key == fail else result,
                error_text="boom" if key == fail else None,
                record=None if key == drop else record,
            )
        return outcomes

    def _run(self, **kwargs):
        def fake(jobs, *, max_workers, game_runner, progress_callback):
            self.assertIs(game_runner, _run_kyoku_game_job)
            return self._outcomes(jobs, **kwargs)

        with mock.patch.object(kyoku_module, "run_game_jobs", side_effect=fake):
            return execute_with_kyoku_records(self.plan, max_workers=2)

    def test_same_comparison_as_generic_and_canonical_record_order(self) -> None:
        result, records = self._run()
        self.assertEqual(result.seat_results, self.expected.seat_results)
        self.assertEqual(result.metrics_a, self.expected.metrics_a)
        self.assertEqual(result.metrics_b, self.expected.metrics_b)
        self.assertEqual(
            [(r["seed"], r["rotation"]) for r in records],
            [(s, r) for s in SMALL_SEEDS for r in range(4)],
        )

    def test_one_failed_game_fails_everything(self) -> None:
        with self.assertRaises(ComparisonExecutionError):
            self._run(fail=(12, 2))

    def test_missing_record_fails_everything(self) -> None:
        with self.assertRaisesRegex(ComparisonExecutionError, "kyoku record"):
            self._run(drop=(11, 1))


class _FakeRunner:
    complete = True

    def __init__(self, policies, *, seed, game_mode, max_steps, trace_sink):
        self.seed, self.game_mode, self.sink = seed, game_mode, trace_sink

    def run(self):
        self.sink.on_start(seed=self.seed, game_mode=self.game_mode)
        for index, event in enumerate(({"type": "start_game"}, {"type": "end_game"})):
            self.sink.on_event(SimpleNamespace(sequence=index, event=json.dumps(event)))
        if self.complete:
            self.sink.on_complete()
        return SimpleNamespace(
            seed=self.seed,
            game_mode=self.game_mode,
            scores=(40_000, 30_000, 20_000, 10_000),
            ranks=(1, 2, 3, 4),
        )


class KyokuGameJobTest(unittest.TestCase):
    def setUp(self) -> None:
        h, l_ = (
            PolicySpec(H, stub_heuristic_policy),
            PolicySpec(L, stub_learning_policy),
        )
        self.job = GameJob(
            seed=5,
            rotation=1,
            assignment=(l_, h, h, l_),
            game_mode=GAME_MODE,
            max_steps=MAX_STEPS,
        )
        self.account = SimpleNamespace(kyokus=(), final_award_seat=None, final_award=0)

    def test_records_events_from_the_same_game(self) -> None:
        with (
            mock.patch.object(kyoku_module, "LocalGameRunner", _FakeRunner),
            mock.patch.object(
                kyoku_module, "account_game", return_value=self.account
            ) as account,
        ):
            outcome = _run_kyoku_game_job(self.job)
        self.assertIsNone(outcome.error_text)
        events, scores = account.call_args.args
        self.assertEqual([e["type"] for e in events], ["start_game", "end_game"])
        self.assertEqual(scores, (40_000, 30_000, 20_000, 10_000))
        self.assertEqual(outcome.record["seat_identities"], [L, H, H, L])
        self.assertEqual(outcome.record["raw_event_count"], 2)

    def test_incomplete_trace_or_accounting_error_is_a_failure(self) -> None:
        class Incomplete(_FakeRunner):
            complete = False

        with (
            mock.patch.object(kyoku_module, "LocalGameRunner", Incomplete),
            mock.patch.object(kyoku_module, "account_game", return_value=self.account),
        ):
            self.assertIn("did not complete", _run_kyoku_game_job(self.job).error_text)
        with (
            mock.patch.object(kyoku_module, "LocalGameRunner", _FakeRunner),
            mock.patch.object(
                kyoku_module, "account_game", side_effect=ValueError("bad deltas")
            ),
        ):
            outcome = _run_kyoku_game_job(self.job)
        self.assertIn("bad deltas", outcome.error_text)
        self.assertIsNone(outcome.record)


class LockDestinationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)

    def _build(self, dests):
        ml, pair = _v2_patches()
        with merged_main_execution(), ml, pair:
            return build_lock_document(
                destinations=dests,
                heuristic=heuristic_binding(),
                learning=_l2a_binding(),
                seeds=seeds(),
                max_workers=8,
                ml_runtime_packages=("torch",),
                learning_artifact_binding=BINDING,
            )

    def test_v2_requires_kyoku_destinations(self) -> None:
        doc = self._build(v2_destinations(self.path))
        self.assertEqual(
            set(doc["artifact_destinations"]),
            {"comparison_artifact", "overall_result", "kyoku_records", "kyoku_summary"},
        )
        with self.assertRaisesRegex(OverallChampionLockError, "destinations"):
            self._build(destinations(self.path))

    def test_resealed_v2_without_kyoku_destinations_rejected(self) -> None:
        doc = self._build(v2_destinations(self.path))
        for name in ("kyoku_records", "kyoku_summary"):
            forged = copy.deepcopy(doc)
            del forged["artifact_destinations"][name]
            payload = {k: v for k, v in forged.items() if k != "lock_identity"}
            forged["lock_identity"] = document_identity(payload)
            with self.subTest(name=name), self.assertRaises(OverallChampionLockError):
                parse_lock_document(forged)

    def test_v1_rejects_kyoku_destinations(self) -> None:
        install_checkpoint(self.path)
        self.addCleanup(clear_checkpoint)
        with merged_main_execution():
            with self.assertRaisesRegex(OverallChampionLockError, "destinations"):
                lock_module.build_lock_document(
                    destinations=v2_destinations(self.path),
                    heuristic=heuristic_binding(),
                    learning=learning_binding(),
                    seeds=seeds(),
                    max_workers=1,
                )
        doc = lock_document(self.path)
        self.assertEqual(
            set(doc["artifact_destinations"]),
            {"comparison_artifact", "overall_result"},
        )


class V2RunTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.dests = v2_destinations(self.path)
        ml, pair = _v2_patches()
        with merged_main_execution(), ml, pair:
            self.lock = build_lock_document(
                destinations=self.dests,
                heuristic=heuristic_binding(),
                learning=_l2a_binding(),
                seeds=seeds(),
                max_workers=8,
                ml_runtime_packages=("torch",),
                learning_artifact_binding=BINDING,
            )
        self.lock_path = self.path / "overall-lock.json"
        save_lock_document(self.lock, self.lock_path)
        self.result = comparison_result(
            seeds(),
            uniform_profile(HEURISTIC_ADVANTAGE),
            heuristic_identity=H,
            learning_identity=L,
        )
        self.records = synthetic_kyoku_records(self.result.seat_results)

    def _run(self, execute_kyoku):
        ml, pair = _v2_patches()
        with (
            merged_main_execution(),
            ml,
            pair,
            mock.patch.object(model_pair, "live_pair_document", return_value=_pair()),
            mock.patch(
                "lisjong_arena.artifact._collect_execution_provenance",
                return_value=comparison_provenance(),
            ),
        ):
            return run_overall_evaluation(
                lock_path=self.lock_path,
                heuristic_spec=heuristic_spec(),
                learning_spec=PolicySpec(L, create_current_learning_champion_policy),
                execute=lambda *a, **k: self.fail("v2 must not use the v1 executor"),
                execute_kyoku=execute_kyoku,
            )

    def _nothing_written(self):
        for name, path in self.dests.items():
            self.assertFalse(path.exists(), name)

    def test_v2_run_writes_and_verifies_kyoku_files(self) -> None:
        outcome = self._run(lambda plan, *, max_workers: (self.result, self.records))
        self.assertEqual(outcome.kyoku_records_path, self.dests["kyoku_records"])
        for path in self.dests.values():
            self.assertTrue(path.exists())
        summary = json.loads(self.dests["kyoku_summary"].read_text(encoding="utf-8"))
        self.assertEqual(summary["lock_identity"], self.lock["lock_identity"])
        verify_overall_bundle(
            lock_path=self.lock_path,
            comparison_path=self.dests["comparison_artifact"],
            result_path=self.dests["overall_result"],
            kyoku_records_path=self.dests["kyoku_records"],
            kyoku_summary_path=self.dests["kyoku_summary"],
        )
        with self.assertRaisesRegex(OverallChampionResultError, "requires both"):
            verify_overall_bundle(
                lock_path=self.lock_path,
                comparison_path=self.dests["comparison_artifact"],
                result_path=self.dests["overall_result"],
                kyoku_records_path=self.dests["kyoku_records"],
            )

    def test_inconsistent_kyoku_records_write_nothing(self) -> None:
        broken = list(copy.deepcopy(self.records))
        broken.pop()
        with self.assertRaisesRegex(OverallChampionResultError, "kyoku records"):
            self._run(lambda plan, *, max_workers: (self.result, tuple(broken)))
        self._nothing_written()

    def test_v2_executor_must_return_kyoku_records(self) -> None:
        with self.assertRaisesRegex(OverallChampionResultError, "kyoku records"):
            self._run(lambda plan, *, max_workers: self.result)
        self._nothing_written()


class V1BundleTest(unittest.TestCase):
    def test_v1_verify_rejects_kyoku_paths(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name)
        install_checkpoint(path)
        self.addCleanup(clear_checkpoint)
        lock = lock_document(path)
        lock_path = path / "overall-lock.json"
        save_lock_document(lock, lock_path)
        comparison_path = path / "comparison.json"
        result_path = path / "overall-result.json"
        save_synthetic_comparison(
            comparison_result(seeds(), uniform_profile(HEURISTIC_ADVANTAGE)),
            comparison_path,
        )
        save_overall_result(
            build_overall_result(
                lock_document=lock,
                comparison_artifact=load_bundle_comparison(comparison_path),
                comparison_artifact_path=comparison_path,
            ),
            result_path,
        )
        verify_overall_bundle(
            lock_path=lock_path,
            comparison_path=comparison_path,
            result_path=result_path,
        )
        with self.assertRaisesRegex(OverallChampionResultError, "v1 bundle"):
            verify_overall_bundle(
                lock_path=lock_path,
                comparison_path=comparison_path,
                result_path=result_path,
                kyoku_records_path=path / "k.json",
                kyoku_summary_path=path / "s.json",
            )


if __name__ == "__main__":
    unittest.main()
