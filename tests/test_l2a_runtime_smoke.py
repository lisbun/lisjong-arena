"""Issue #507 L2a runtime smoke tests (no RiichiEnv, no torch, no real artifact)."""

import json
import sys
import tempfile
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest import mock

from lisjong.policy_contract import Seat

from lisjong_arena.l2a_runtime_smoke import smoke
from lisjong_arena.model import PolicySpec

DISCARD_IDENTITY = "a" * 64
REACTION_IDENTITY = "b" * 64


class _Kind(Enum):
    DISCARD = "discard"
    CALL = "call"
    FORCED = "forced"


class _Result:
    def __init__(self, kind, action, timings_ms=None):
        self.kind = kind
        self.action = action
        self.timings_ms = timings_ms


class _Decision:
    def __init__(self, legal_actions):
        self.legal_actions = legal_actions


class _FakeL2aPolicy:
    def __init__(self, runtime):
        self.runtime = runtime

    def decide(self, decision):
        action = decision.legal_actions[0]
        if self.runtime.illegal:
            action = object()
        if len(decision.legal_actions) == 1:
            return _Result(_Kind.FORCED, action)
        return _Result(
            _Kind.DISCARD,
            action,
            {"candidate_encoding": 2.0, "shared_context": 1.0, "forward": 0.5},
        )


class _FakeRuntime:
    def __init__(self, *, illegal=False):
        self.identity = (DISCARD_IDENTITY, REACTION_IDENTITY)
        self.illegal = illegal
        self.created = []

    def __call__(self):
        policy = _FakeL2aPolicy(self)
        self.created.append(policy)
        return policy


class _FakeC0:
    def choose_action(self, decision):
        return decision.legal_actions[0]


class _GameRunner:
    """Each game asks the L2a seat for one scored and one forced decision."""

    def __init__(self, *, fail_on_seed=None):
        self.calls = []
        self.fail_on_seed = fail_on_seed

    def __call__(self, policies, seed):
        self.calls.append((dict(policies), seed))
        if seed == self.fail_on_seed:
            raise RuntimeError("engine failure")
        for policy in policies.values():
            if isinstance(policy, smoke.RecordingL2aPolicy):
                policy.choose_action(_Decision((object(), object())))
                policy.choose_action(_Decision((object(),)))
        return types.SimpleNamespace(
            scores=(25000, 25000, 25000, 25000),
            ranks=(1, 2, 3, 4),
            steps=10,
            decisions=12,
        )


class L2aRuntimeSmokeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "run"
        patcher = mock.patch.object(
            smoke, "C0_SPEC", PolicySpec(identity=smoke.C0_IDENTITY, factory=_FakeC0)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_smoke(self, runtime, runner, **kwargs):
        return smoke.run_smoke(
            runtime,
            output=self.output,
            environment={"arena_revision": "c" * 40},
            load_seconds=0.25,
            game_runner=runner,
            **kwargs,
        )

    def test_protocol_constants_are_fixed(self):
        self.assertEqual(smoke.SEEDS, tuple(range(506900, 506916)))
        self.assertEqual(len(smoke.SEEDS), 16)
        self.assertEqual(
            smoke.ALLOCATION_IDENTITY,
            "f536a79cfb9c02d382e809795fee54d49f13620e7df0234499897f56bd1cbba7",
        )
        self.assertEqual(smoke.PROTOCOL, "l2a-runtime-smoke-v1")
        self.assertEqual(
            [smoke.l2a_seat(ordinal) for ordinal in range(6)],
            [Seat(0), Seat(1), Seat(2), Seat(3), Seat(0), Seat(1)],
        )

    def test_full_run_passes_and_rotates_the_l2a_seat(self):
        runtime = _FakeRuntime()
        runner = _GameRunner()
        report = self.run_smoke(runtime, runner)

        self.assertEqual(report["outcome"], smoke.OUTCOME_PASS)
        self.assertEqual([seed for _, seed in runner.calls], list(smoke.SEEDS))
        seen_policies = set()
        for ordinal, (policies, _) in enumerate(runner.calls):
            self.assertEqual(set(policies), set(Seat))
            l2a = [
                seat
                for seat, policy in policies.items()
                if isinstance(policy, smoke.RecordingL2aPolicy)
            ]
            self.assertEqual(l2a, [Seat(ordinal % 4)])
            c0 = [policy for policy in policies.values() if isinstance(policy, _FakeC0)]
            self.assertEqual(len(c0), 3)
            for policy in policies.values():
                self.assertNotIn(id(policy), seen_policies)
                seen_policies.add(id(policy))
        self.assertEqual(len(runtime.created), 16)
        self.assertEqual(len({id(policy) for policy in runtime.created}), 16)

        totals = report["totals"]
        self.assertEqual(totals["games_completed"], 16)
        self.assertEqual(totals["l2a_decisions"], 32)
        self.assertEqual(totals["l2a_by_kind"], {"discard": 16, "forced": 16})
        self.assertEqual(totals["l2a_scored_by_kind"], {"discard": 16})
        self.assertEqual((totals["illegal"], totals["exceptions"]), (0, 0))
        self.assertEqual(
            [game["l2a_seat"] for game in report["games"]],
            [ordinal % 4 for ordinal in range(16)],
        )
        self.assertEqual(
            report["policies"]["l2a"]["discard_artifact_identity"], "a" * 64
        )
        self.assertEqual(
            report["policies"]["l2a"]["identity"],
            smoke.l2a_policy_identity(DISCARD_IDENTITY, REACTION_IDENTITY),
        )
        self.assertIsNone(report["failure"])

        timings = report["timings_ms"]
        self.assertEqual(timings["all"]["decide"]["count"], 32)
        self.assertEqual(timings["discard"]["forward"]["count"], 16)
        self.assertEqual(timings["discard"]["forward"]["median"], 0.5)
        self.assertNotIn("forced", timings)

        saved = json.loads((self.output / smoke.REPORT_FILE).read_text("utf-8"))
        self.assertEqual(saved["outcome"], smoke.OUTCOME_PASS)
        lines = (self.output / smoke.DECISIONS_FILE).read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 32)
        first = json.loads(lines[0])
        self.assertEqual(first["game_ordinal"], 0)
        self.assertNotIn("scores", first)

    def test_illegal_action_stops_the_run(self):
        runner = _GameRunner()
        report = self.run_smoke(_FakeRuntime(illegal=True), runner)

        self.assertEqual(report["outcome"], smoke.OUTCOME_FAIL)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(report["totals"]["games_completed"], 0)
        self.assertEqual(report["totals"]["illegal"], 1)
        self.assertEqual(report["totals"]["exceptions"], 1)
        self.assertEqual(report["failure"]["error_type"], "IllegalL2aActionError")
        self.assertEqual(report["games"], [])

    def test_game_exception_fails_closed_without_partial_pass(self):
        runner = _GameRunner(fail_on_seed=506902)
        report = self.run_smoke(_FakeRuntime(), runner)

        self.assertEqual(report["outcome"], smoke.OUTCOME_FAIL)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(report["totals"]["games_completed"], 2)
        self.assertEqual(report["totals"]["illegal"], 0)
        self.assertEqual(report["totals"]["exceptions"], 1)
        self.assertEqual(report["failure"]["seed"], 506902)
        self.assertEqual(report["failure"]["l2a_seat"], 2)
        self.assertEqual(report["failure"]["message"], "engine failure")

    def test_other_seeds_never_pass(self):
        report = self.run_smoke(_FakeRuntime(), _GameRunner(), seeds=(1, 2))

        self.assertEqual(report["totals"]["games_completed"], 2)
        self.assertEqual(report["outcome"], smoke.OUTCOME_FAIL)

    def test_existing_output_is_refused(self):
        self.output.mkdir()
        with self.assertRaises(FileExistsError):
            self.run_smoke(_FakeRuntime(), _GameRunner())


class LoadL2aRuntimeTest(unittest.TestCase):
    def _stub(self, runtime):
        module = types.ModuleType("lisjong.learning.l2a_policy")
        module.load_l2a_policy_factory = mock.Mock(return_value=runtime)
        import lisjong.learning as package

        return (
            module,
            mock.patch.dict(sys.modules, {module.__name__: module}),
            (mock.patch.object(package, "l2a_policy", module, create=True)),
        )

    def test_identity_must_match(self):
        module, modules, attribute = self._stub(_FakeRuntime())
        with modules, attribute:
            runtime = smoke.load_l2a_runtime(
                "m1",
                "r1",
                expected_identity=(DISCARD_IDENTITY, REACTION_IDENTITY),
            )
            self.assertEqual(runtime.identity, (DISCARD_IDENTITY, REACTION_IDENTITY))
            module.load_l2a_policy_factory.assert_called_once_with("m1", "r1")
            with self.assertRaises(smoke.L2aRuntimeSmokeError):
                smoke.load_l2a_runtime(
                    "m1",
                    "r1",
                    expected_identity=(REACTION_IDENTITY, DISCARD_IDENTITY),
                )


class SummarizeTest(unittest.TestCase):
    def test_nearest_rank_percentile(self):
        values = [float(value) for value in range(1, 21)]
        summary = smoke.summarize(values)
        self.assertEqual(summary["count"], 20)
        self.assertEqual(summary["median"], 10.5)
        self.assertEqual(summary["p95"], 19.0)
        self.assertEqual(summary["max"], 20.0)

    def test_empty(self):
        self.assertEqual(
            smoke.summarize([]),
            {"count": 0, "median": None, "p95": None, "max": None},
        )


if __name__ == "__main__":
    unittest.main()
