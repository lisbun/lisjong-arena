"""Synthetic contract tests only: no P2/scientific hanchan execution."""

import copy
import json
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policies import two_step_ukeire as teacher_module
from lisjong.policies.two_step_ukeire import TwoStepUkeireCandidateEvaluation as E
from lisjong.policy_contract import DecisionTraceRecorder, execute_policy_with_trace
from lisjong.policy_contract.action import AnkanAction, RiichiAction

from lisjong_arena.offense_foundation import corpus, qualification
from lisjong_arena.offense_foundation.__main__ import main
from lisjong_arena.offense_foundation.fixtures import discard, probes
from lisjong_arena.offense_foundation.protocol import (
    SUPPORT,
    ZERO_FAILURES,
    make_lock,
    ordered_games,
    support_outcome,
    validate_lock,
    validate_request,
)
from lisjong_arena.offense_foundation.qualification import (
    P0_PASS,
    P1_PASS,
    P2_PASS,
    SCHEMA,
    qualification_contract,
    qualify,
    read_document,
    require_matching_qualification_contract,
    seal,
    write_document,
)
from lisjong_arena.offense_foundation.semantics import (
    OffenseError,
    audit_discard,
    audit_trace,
    stage_sets,
)
from lisjong_arena.riichienv.local_game_runner import SeatDecisionObservation


def request(phase="P2"):
    # Only metadata for a replaced game boundary; these are not seed allocations.
    populations = (
        {"QUALIFICATION": list(range(20))}
        if phase == "P2"
        else {
            "TRAIN": list(range(100, 200)),
            "SELECT": list(range(200, 220)),
            "OFFLINE-EVAL": list(range(220, 240)),
        }
    )
    return {
        "phase": phase,
        "populations": populations,
        "known_used_seeds": [],
        "freshness_evidence": ["synthetic test fixture; not scientific evidence"],
    }


def observation(probe):
    recorder = DecisionTraceRecorder()
    execute_policy_with_trace(TwoStepUkeirePolicy(), probe.context, recorder)
    return SeatDecisionObservation(
        probe.context.input.self_seat, probe.context.input, recorder.snapshot()[0]
    )


class SemanticTest(unittest.TestCase):
    def test_real_qualification_and_determinism(self):
        first = qualify({})
        self.assertEqual((first["p0"], first["p1"]), (P0_PASS, P1_PASS))
        self.assertEqual(first, qualify({}))

    def test_class_name_does_not_qualify_wrong_own_turn_behavior(self):
        original = TwoStepUkeirePolicy._decide

        def bad(policy, context):
            from lisjong.policy_contract import PolicyDecision

            kan = next(
                (a for a in context.legal_actions if isinstance(a, AnkanAction)), None
            )
            return PolicyDecision(kan) if kan else original(policy, context)

        with patch.object(TwoStepUkeirePolicy, "_decide", bad):
            self.assertEqual(qualify({})["p0"], "OFFENSE TEACHER NOT QUALIFIED")

    def test_priority_failure_not_rescued(self):
        original = TwoStepUkeirePolicy._decide

        def bad(policy, context):
            from lisjong.policy_contract import PolicyDecision

            riichi = next(
                (a for a in context.legal_actions if isinstance(a, RiichiAction)), None
            )
            return PolicyDecision(riichi) if riichi else original(policy, context)

        with patch.object(TwoStepUkeirePolicy, "_decide", bad):
            self.assertEqual(qualify({})["p0"], "OFFENSE TEACHER NOT QUALIFIED")

    def test_missing_analysis_is_not_qualified(self):
        original = TwoStepUkeirePolicy.choose_action_with_analysis

        def bad(policy, context):
            from lisjong.policy_contract import PolicyDecision

            return PolicyDecision(original(policy, context).action)

        with patch.object(TwoStepUkeirePolicy, "choose_action_with_analysis", bad):
            report = qualify({})
        self.assertEqual(report["p0"], P0_PASS)
        self.assertEqual(report["p1"], "SEMANTIC AUDIT PATH NOT QUALIFIED")

    def test_stage_subsets_and_conditional_denominators(self):
        a, b, c, d = (discard(f"{n}m") for n in range(1, 5))
        stages = stage_sets(
            (E(a, 1, 10, 12), E(b, 1, 10, 9), E(c, 1, 8, None), E(d, 2, None, None))
        )
        self.assertEqual(stages.shanten, {a, b, c})
        self.assertEqual(stages.ukeire, {a, b})
        self.assertEqual(stages.second_step, {a})
        self.assertEqual(audit_discard(stages, b)["second_step_regret"], 3)
        self.assertEqual(audit_discard(stages, c)["ukeire_regret"], 2)
        self.assertIsNone(audit_discard(stages, c)["second_step_conditional_agreement"])
        self.assertEqual(audit_discard(stages, d)["shanten_regret"], 1)
        self.assertIsNone(audit_discard(stages, d)["ukeire_conditional_agreement"])

    def test_none_is_not_zero_and_ties_are_not_regret(self):
        a, b = discard("5m"), discard("0m")
        stages = stage_sets((E(a, 1, 0, 0), E(b, 1, 0, 0)))
        self.assertEqual(stages.second_step, {a, b})
        self.assertEqual(audit_discard(stages, b)["second_step_regret"], 0)
        self.assertTrue(audit_discard(stages, b)["second_step_conditional_agreement"])
        tenpai = stage_sets((E(a, 0, 0, None), E(b, 0, 0, None)))
        self.assertIsNone(tenpai.second_step)
        with self.assertRaises(OffenseError):
            stage_sets((E(a, 1, 0, None), E(b, 1, 0, 0)))
        with self.assertRaises(OffenseError):
            stage_sets((E(a, 1, 0, None), E(b, 2, 0, None)))

    def test_recording_reuses_one_evaluation_and_red_identity(self):
        probe = next(p for p in probes() if p.name == "stable_red_five_tie")
        with patch.object(
            teacher_module,
            "_evaluate_and_choose_discard",
            wraps=teacher_module._evaluate_and_choose_discard,
        ) as evaluate:
            obs = observation(probe)
            stages = audit_trace(obs.decision_trace)
            self.assertIs(
                stages.candidates[0],
                obs.decision_trace.analysis.candidate_evaluations[0],
            )
            row, features, mask, counts = corpus.encode_observation(obs, 0, 0)
            self.assertEqual(evaluate.call_count, 1)
        trace, roundtrip = corpus._read_row(row)
        self.assertEqual(roundtrip, stages)
        self.assertEqual(trace.selected_action, probe.expected)
        self.assertEqual(len(features), 8204 * 4)
        self.assertEqual(len(mask), 802)
        self.assertEqual(counts["choice_rows"], 1)


def synthetic_binding(**overrides):
    # Shape matches lisjong_arena.offense_foundation.qualification.runtime_binding().
    base = {
        "arena_revision": "a" * 40,
        "dependencies": {"lisjong": "b" * 40, "lisjong-engine": "c" * 40},
        "lisjong_source_digest": "d" * 64,
        "teacher": "lisjong.policies.TwoStepUkeirePolicy",
        "feature_fingerprint": "e" * 16,
        "vocabulary_fingerprint": "f" * 16,
        "feature_dimension": 8204,
        "vocabulary_size": 802,
        "python": "3.14.0",
        "riichienv": "0.4.10",
    }
    base.update(overrides)
    return base


class QualificationContractTest(unittest.TestCase):
    """#332 Blocker 1: cross-platform qualification identity equality removal."""

    def test_platform_only_differences_are_permitted(self):
        local = qualification_contract(qualify(synthetic_binding()))
        remote = qualification_contract(
            qualify(
                synthetic_binding(
                    python="3.14.1",
                    lisjong_source_digest="9" * 64,
                )
            )
        )
        self.assertEqual(local, remote)
        require_matching_qualification_contract(local, remote)  # must not raise

    def test_scientific_field_mismatches_are_rejected(self):
        local = qualification_contract(qualify(synthetic_binding()))
        mismatches = {
            "arena_revision": synthetic_binding(arena_revision="9" * 40),
            "lisjong_revision": synthetic_binding(
                dependencies={"lisjong": "9" * 40, "lisjong-engine": "c" * 40}
            ),
            "lisjong_engine_revision": synthetic_binding(
                dependencies={"lisjong": "b" * 40, "lisjong-engine": "9" * 40}
            ),
            "riichienv_version": synthetic_binding(riichienv="0.4.11"),
            "teacher": synthetic_binding(teacher="other.Teacher"),
            "feature_dimension": synthetic_binding(feature_dimension=1),
            "feature_fingerprint": synthetic_binding(feature_fingerprint="0" * 16),
            "vocabulary_size": synthetic_binding(vocabulary_size=1),
            "vocabulary_fingerprint": synthetic_binding(
                vocabulary_fingerprint="0" * 16
            ),
        }
        for field, altered_binding in mismatches.items():
            with self.subTest(field=field):
                remote = qualification_contract(qualify(altered_binding))
                self.assertNotEqual(local, remote)
                with self.assertRaises(OffenseError):
                    require_matching_qualification_contract(local, remote)

    def test_p0_or_p1_failure_is_rejected_before_comparison(self):
        original = TwoStepUkeirePolicy._decide

        def bad(policy, context):
            from lisjong.policy_contract import PolicyDecision

            kan = next(
                (a for a in context.legal_actions if isinstance(a, AnkanAction)), None
            )
            return PolicyDecision(kan) if kan else original(policy, context)

        with patch.object(TwoStepUkeirePolicy, "_decide", bad):
            failed = qualify(synthetic_binding())
        self.assertNotEqual(failed["p0"], P0_PASS)
        with self.assertRaises(OffenseError):
            qualification_contract(failed)


class CorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = qualify({})
        cls.observations = tuple(observation(p) for p in probes())
        cls.inspection = SimpleNamespace(
            step_observations=tuple(
                SimpleNamespace(step_ordinal=i, seat_decisions=(obs,))
                for i, obs in enumerate(cls.observations)
            )
        )

    def fake_game(self, seed):
        return SimpleNamespace(
            seed=seed,
            game_mode="4p-red-half",
            decisions=len(self.observations),
            steps=len(self.observations),
        ), self.inspection

    def generate_fixture(self, path, progress=None):
        lock = make_lock(request(), self.report)
        with (
            patch.object(corpus, "runtime_binding", return_value={}),
            patch.object(corpus, "_record_game", side_effect=self.fake_game),
        ):
            result = corpus.generate(lock, path, progress=progress)
        return lock, result

    class ImmediateExecutor:
        instances = []

        def __init__(self, max_workers):
            self.max_workers = max_workers
            self.futures = []
            self.shutdown_arguments = None
            self.__class__.instances.append(self)

        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)
            self.futures.append(future)
            return future

        def shutdown(self, *, wait, cancel_futures):
            self.shutdown_arguments = (wait, cancel_futures)

    def test_complete_fixture_deterministic_strict_read_and_choice_only_tensors(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "one", Path(tmp) / "two"
            progress = []
            lock, result = self.generate_fixture(first, lambda *v: progress.append(v))
            _, again = self.generate_fixture(second)
            self.assertEqual(result, again)
            self.assertEqual(corpus.read_corpus(first, expected_lock=lock), result)
            self.assertEqual(progress, [(i, 20) for i in range(1, 21)])
            self.assertEqual(result["p2_outcome"], "OFFENSE SUPPORT NOT QUALIFIED")
            for game in result["games"]:
                self.assertEqual(game["forced_rows"], 1)
                self.assertEqual(
                    game["files"]["features.f32"]["bytes"],
                    game["choice_rows"] * 8204 * 4,
                )
            with self.assertRaises(FileExistsError):
                self.generate_fixture(first)
            with self.assertRaises(OffenseError):
                make_lock(request("SCIENTIFIC"), self.report, result)

    def test_generation_requires_p0_p1_current_binding_before_runner(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(corpus, "runtime_binding", return_value={}),
            patch.object(corpus, "_record_game") as run,
        ):
            lock = make_lock(request(), self.report)
            lock["qualification"] = {
                **self.report,
                "p0": "OFFENSE TEACHER NOT QUALIFIED",
            }
            with self.assertRaises(OffenseError):
                corpus.generate(lock, Path(tmp) / "out")
            run.assert_not_called()

    def test_parallel_reverse_completion_matches_serial_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            serial = Path(tmp) / "serial"
            parallel = Path(tmp) / "parallel"
            lock, expected = self.generate_fixture(serial)
            progress = []
            self.ImmediateExecutor.instances.clear()
            with (
                patch.object(corpus, "runtime_binding", return_value={}),
                patch.object(corpus, "_record_game", side_effect=self.fake_game),
                patch.object(corpus, "ProcessPoolExecutor", self.ImmediateExecutor),
                patch.object(
                    corpus,
                    "as_completed",
                    side_effect=lambda futures: reversed(tuple(futures)),
                ),
            ):
                actual = corpus.generate(
                    lock,
                    parallel,
                    workers=4,
                    progress=lambda *values: progress.append(values),
                )
            self.assertEqual(expected, actual)
            self.assertEqual(
                [path.relative_to(serial) for path in sorted(serial.rglob("*"))],
                [path.relative_to(parallel) for path in sorted(parallel.rglob("*"))],
            )
            for serial_path in sorted(
                path for path in serial.rglob("*") if path.is_file()
            ):
                relative = serial_path.relative_to(serial)
                self.assertEqual(
                    serial_path.read_bytes(), (parallel / relative).read_bytes()
                )
            self.assertEqual(progress, [(i, 20) for i in range(1, 21)])
            executor = self.ImmediateExecutor.instances[-1]
            self.assertEqual(executor.max_workers, 4)
            self.assertEqual(executor.shutdown_arguments, (True, True))

    def test_worker_bounds_and_parallel_failure_publish_nothing(self):
        lock = make_lock(request(), self.report)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(corpus, "runtime_binding", return_value={}):
                for workers in (0, 21, 33, True):
                    with self.subTest(workers=workers), self.assertRaises(OffenseError):
                        corpus.generate(
                            lock, Path(tmp) / f"out-{workers}", workers=workers
                        )
            target = Path(tmp) / "parallel-failure"

            def fail_one_game(seed):
                if seed == 5:
                    raise RuntimeError("worker failed")
                return self.fake_game(seed)

            with (
                patch.object(corpus, "runtime_binding", return_value={}),
                patch.object(corpus, "_record_game", side_effect=fail_one_game),
                patch.object(corpus, "ProcessPoolExecutor", self.ImmediateExecutor),
                patch.object(corpus, "as_completed", side_effect=lambda values: values),
                self.assertRaises(RuntimeError),
            ):
                corpus.generate(lock, target, workers=2)
            self.assertFalse(target.exists())
            self.assertEqual(
                [path for path in Path(tmp).iterdir() if path.name == target.name], []
            )

    def test_p2_outcome_is_not_published_when_strict_readback_fails(self):
        lock = make_lock(request(), self.report)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out"
            with (
                patch.object(corpus, "runtime_binding", return_value={}),
                patch.object(corpus, "_record_game", side_effect=self.fake_game),
                patch.object(
                    corpus, "read_corpus", side_effect=OffenseError("readback failed")
                ),
                self.assertRaises(OffenseError),
            ):
                corpus.generate(lock, target)
            self.assertFalse(target.exists())

    def test_failed_game_publishes_no_partial_corpus(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(corpus, "runtime_binding", return_value={}),
            patch.object(
                corpus, "_record_game", side_effect=RuntimeError("execution failed")
            ),
        ):
            target = Path(tmp) / "out"
            with self.assertRaises(RuntimeError):
                corpus.generate(make_lock(request(), self.report), target)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_strict_reader_rejects_tampering_missing_extra_and_wrong_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out"
            lock, _ = self.generate_fixture(path)
            with self.assertRaises(OffenseError):
                corpus.read_corpus(path, expected_lock={})
            extra = path / "unexpected"
            extra.write_text("bad")
            with self.assertRaises(OffenseError):
                corpus.read_corpus(path)
            extra.unlink()
            payload = path / "game-000" / "features.f32"
            original = payload.read_bytes()
            payload.write_bytes(b"bad")
            with self.assertRaises(OffenseError):
                corpus.read_corpus(path)
            payload.write_bytes(original)
            manifest_path = path / "manifest.json"
            manifest = read_document(manifest_path)
            manifest["games"][1] = manifest["games"][0]
            manifest.pop("identity")
            manifest_path.unlink()
            write_document(manifest_path, seal(manifest))
            with self.assertRaises(OffenseError):
                corpus.read_corpus(path, expected_lock=lock)

    def test_bad_label_stage_mask_and_nonfinite_are_rejected(self):
        row, _, _, _ = corpus.encode_observation(self.observations[5], 0, 0)
        bad = copy.deepcopy(row)
        bad["teacher_action_index"] = 801
        with self.assertRaises(OffenseError):
            corpus._read_row(bad)
        bad = copy.deepcopy(row)
        bad["candidates"][0]["second_step_ukeire_score"] = None
        with self.assertRaises(OffenseError):
            corpus._read_row(bad)
        with self.assertRaises(OffenseError):
            corpus._feature_bytes([float("nan")] * 8204)


class ProtocolTest(unittest.TestCase):
    def test_scientific_pass_binding_and_population_order(self):
        # Protocol metadata fixture only. No game or scientific population is run.
        report = qualify({})
        p2_lock = make_lock(request(), report)
        games = [
            seal(
                {
                    "seed": seed,
                    "split": split,
                    "game_mode": "4p-red-half",
                    "lock_identity": p2_lock["identity"],
                    "support": dict(SUPPORT),
                }
            )
            for split, seed in ordered_games(p2_lock)
        ]
        p2 = seal(
            {
                "schema": SCHEMA,
                "kind": "corpus",
                "lock": p2_lock,
                "p2_evidence": None,
                "games": games,
                "support": {k: v * 20 for k, v in SUPPORT.items()},
                "failures": dict.fromkeys(ZERO_FAILURES, 0),
                "p2_outcome": P2_PASS,
            }
        )
        lock = make_lock(request("SCIENTIFIC"), report, p2)
        validate_lock(lock, p2)
        self.assertEqual(
            ordered_games(lock),
            tuple(
                [("TRAIN", s) for s in range(100, 200)]
                + [("SELECT", s) for s in range(200, 220)]
                + [("OFFLINE-EVAL", s) for s in range(220, 240)]
            ),
        )
        overlap = request("SCIENTIFIC")
        overlap["populations"]["SELECT"] = list(range(20))
        with self.assertRaises(OffenseError):
            make_lock(overlap, report, p2)
        with self.assertRaises(OffenseError):
            make_lock(request("SCIENTIFIC"), qualify({"other_runtime": True}), p2)
        broken = copy.deepcopy(p2)
        broken["support"]["choice_rows"] = 0
        broken.pop("identity")
        with self.assertRaises(OffenseError):
            make_lock(request("SCIENTIFIC"), report, seal(broken))

    def test_runner_gets_four_fresh_exact_teachers_per_game(self):
        with (
            patch.object(corpus, "LocalGameRunner") as runner,
            patch.object(corpus, "LocalGameInspectionRecorder"),
        ):
            corpus._record_game(10)
            corpus._record_game(11)
        policies = [
            policy for call in runner.call_args_list for policy in call.args[0].values()
        ]
        self.assertEqual(len({id(p) for p in policies}), 8)
        self.assertTrue(all(type(p) is TwoStepUkeirePolicy for p in policies))
        for call in runner.call_args_list:
            self.assertEqual(call.kwargs["game_mode"], "4p-red-half")
            self.assertIn("inspection_recorder", call.kwargs)
            self.assertNotIn("decision_point_observer", call.kwargs)

    def test_fixed_sizes_contiguity_prior_use_and_split_leakage(self):
        for mutate in (
            lambda r: r["populations"]["QUALIFICATION"].pop(),
            lambda r: r["populations"]["QUALIFICATION"].reverse(),
            lambda r: r["known_used_seeds"].append(3),
            lambda r: r.update(freshness_evidence=[]),
            lambda r: r["populations"]["QUALIFICATION"].__setitem__(0, False),
        ):
            value = request()
            mutate(value)
            with self.assertRaises(OffenseError):
                validate_request(value)
        value = request("SCIENTIFIC")
        value["populations"]["SELECT"] = list(range(110, 130))
        with self.assertRaises(OffenseError):
            validate_request(value)

    def test_scientific_requires_p2(self):
        report = qualify({})
        with self.assertRaises(OffenseError):
            make_lock(request("SCIENTIFIC"), report)
        self.assertEqual(support_outcome(dict(SUPPORT)), P2_PASS)
        for metric in SUPPORT:
            counts = dict(SUPPORT)
            counts[metric] -= 1
            self.assertNotEqual(support_outcome(counts), P2_PASS)

    def test_qualification_cli_retains_artifact_without_games(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(qualification, "runtime_binding", return_value={}),
        ):
            # CLI imports the function directly; no environment or seeds needed.
            with (
                patch(
                    "lisjong_arena.offense_foundation.__main__.runtime_binding",
                    return_value={},
                ),
                patch.object(corpus, "_record_game") as run,
            ):
                path = Path(tmp) / "qualification.json"
                self.assertEqual(main(["qualify", "--output", str(path)]), 0)
                report = read_document(path)
                self.assertEqual(report["p0"], P0_PASS)
                run.assert_not_called()

    def test_request_validation_cli_is_read_only_and_phase_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            request_path = Path(tmp) / "request.json"
            request_path.write_text(json.dumps(request()), encoding="utf-8")
            self.assertEqual(
                main(
                    [
                        "validate-request",
                        "--request",
                        str(request_path),
                        "--phase",
                        "P2",
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "validate-request",
                        "--request",
                        str(request_path),
                        "--phase",
                        "SCIENTIFIC",
                    ]
                ),
                2,
            )
            self.assertEqual([request_path], list(Path(tmp).iterdir()))


if __name__ == "__main__":
    unittest.main()
