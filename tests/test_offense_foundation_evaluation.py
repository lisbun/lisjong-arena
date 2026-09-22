"""Synthetic contract tests for #331 offline evaluation and rollout boundaries."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisjong.action_vocabulary import encode_action
from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policy_contract import DecisionTraceRecorder, execute_policy_with_trace

from lisjong_arena import seed_registry
from lisjong_arena.offense_foundation import evaluation, rollout
from lisjong_arena.offense_foundation.fixtures import probes
from lisjong_arena.offense_foundation.learner import LoadedOffenseCheckpoint
from lisjong_arena.offense_foundation.qualification import seal
from lisjong_arena.offense_foundation.semantics import OffenseError, audit_trace
from lisjong_arena.offense_foundation.serving import InferenceDecision


def _probe(name):
    return next(probe for probe in probes() if probe.name == name)


def _teacher_trace(context):
    recorder = DecisionTraceRecorder()
    execute_policy_with_trace(TwoStepUkeirePolicy(), context, recorder)
    traces = recorder.snapshot()
    assert len(traces) == 1
    return traces[0]


def _inference(action, legal_actions):
    values = [-100.0] * 802
    values[encode_action(action)] = 0.0
    return InferenceDecision(
        action=action,
        action_index=encode_action(action),
        log_probabilities=values,
    )


class OfflineSupportTest(unittest.TestCase):
    def _checkpoint(self):
        return LoadedOffenseCheckpoint(
            path=Path("."),
            manifest={
                "scientific_corpus_identity": "c" * 64,
                "checkpoint_identity": "k" * 64,
            },
            model=object(),
        )

    def _manifest(self, per_game):
        games = [{"support": dict(per_game)} for _ in range(20)]
        return {
            "identity": "c" * 64,
            "lock": {"identity": "l" * 64},
            "games": games,
        }

    def test_offline_support_gate_uses_only_offline_summaries(self):
        per_game = {
            "choice_rows": 100,
            "winning_opportunities": 2,
            "riichi_opportunities": 3,
            "voluntary_call_opportunities": 5,
            "normal_discard_choice_rows": 50,
            "second_step_applicable_rows": 13,
        }
        ordered = tuple(("OFFLINE-EVAL", seed) for seed in range(20))
        with (
            patch.object(
                evaluation,
                "_validate_scientific_manifest_metadata",
                return_value=self._manifest(per_game),
            ),
            patch.object(evaluation, "ordered_games", return_value=ordered),
        ):
            result = evaluation.offline_support_document(
                "never-opened-corpus", self._checkpoint()
            )
        self.assertEqual(result["outcome"], evaluation.OFFLINE_SUPPORT_QUALIFIED)
        self.assertEqual(result["counts"]["winning_opportunities"], 40)
        self.assertEqual(result["counts"]["ukeire_stage_eligible_rows"], 1_000)
        self.assertEqual(result["counts"]["second_step_eligible_rows"], 260)

    def test_offline_support_shortfall_is_not_skill_failure(self):
        per_game = {
            "choice_rows": 100,
            "winning_opportunities": 1,
            "riichi_opportunities": 3,
            "voluntary_call_opportunities": 5,
            "normal_discard_choice_rows": 50,
            "second_step_applicable_rows": 13,
        }
        ordered = tuple(("OFFLINE-EVAL", seed) for seed in range(20))
        with (
            patch.object(
                evaluation,
                "_validate_scientific_manifest_metadata",
                return_value=self._manifest(per_game),
            ),
            patch.object(evaluation, "ordered_games", return_value=ordered),
        ):
            result = evaluation.offline_support_document(
                "never-opened-corpus", self._checkpoint()
            )
        self.assertEqual(result["outcome"], evaluation.OFFLINE_SUPPORT_INSUFFICIENT)
        self.assertEqual(
            result["failures"]["winning_opportunities"],
            {"observed": 20, "required": 30},
        )


class SemanticAccumulatorTest(unittest.TestCase):
    def test_exact_teacher_discard_is_semantically_successful(self):
        context = _probe("maximum_current_ukeire").context
        trace = _teacher_trace(context)
        stages = audit_trace(trace)
        accumulator = evaluation._SemanticAccumulator()
        accumulator.observe(
            context,
            trace,
            stages,
            _inference(trace.selected_action, trace.legal_actions),
        )
        result = accumulator.document()
        self.assertEqual(result["shanten"]["agreement"], 1.0)
        self.assertEqual(result["ukeire"]["conditional_agreement"], 1.0)
        self.assertEqual(result["secondary"]["all_choice_top1_agreement"], 1.0)

    def test_voluntary_call_is_counted_as_no_call_violation(self):
        context = _probe("pass_before_pon").context
        trace = _teacher_trace(context)
        call = next(
            action
            for action in context.legal_actions
            if action != trace.selected_action
        )
        accumulator = evaluation._SemanticAccumulator()
        accumulator.observe(
            context,
            trace,
            audit_trace(trace),
            _inference(call, trace.legal_actions),
        )
        result = accumulator.document()
        self.assertEqual(result["no_call"]["opportunity_count"], 1)
        self.assertEqual(result["no_call"]["violation_count"], 1)
        self.assertEqual(result["no_call"]["agreement"], 0.0)


class ShadowTeacherTest(unittest.TestCase):
    def test_contract_escape_is_audited_once_then_open_states_are_excluded(self):
        context = _probe("pass_before_pon").context
        call = next(
            action
            for action in context.legal_actions
            if action != _probe("pass_before_pon").expected
        )
        inference = _inference(call, context.legal_actions)
        accumulator = evaluation._SemanticAccumulator()
        support = {
            "choice_rows": 0,
            "winning_opportunities": 0,
            "riichi_opportunities": 0,
            "voluntary_call_opportunities": 0,
            "normal_discard_choice_rows": 0,
            "second_step_applicable_rows": 0,
        }
        runtime = SimpleNamespace(model=object())
        policy = rollout._ShadowAuditedLearnerPolicy(runtime, accumulator, support)
        with patch.object(rollout, "infer_decision", return_value=inference):
            first = policy.choose_action(context)
            second = policy.choose_action(context)

        self.assertEqual(first, call)
        self.assertEqual(second, call)
        self.assertTrue(policy.escaped)
        self.assertEqual(policy.post_escape_decisions, 1)
        self.assertTrue(policy.records[0].audited)
        self.assertTrue(policy.records[0].contract_escape)
        self.assertFalse(policy.records[1].audited)
        self.assertEqual(accumulator.document()["no_call"]["violation_count"], 1)


class RolloutLockTest(unittest.TestCase):
    def _checkpoint(self):
        return LoadedOffenseCheckpoint(
            path=Path("."),
            manifest={
                "checkpoint_identity": "d" * 64,
                "scientific_corpus_identity": "c" * 64,
                "train_seeds": [10, 11],
                "select_seeds": [12],
                "offline_eval_seeds": [13],
            },
            model=object(),
        )

    def _authority(self, seeds):
        ledger = seed_registry.new_ledger()
        ledger, record = seed_registry.reserve_allocation(
            ledger,
            owner_issue=rollout.ROLLOUT_OWNER_ISSUE,
            protocol=rollout.ROLLOUT_PROTOCOL,
            seed_domain=seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
            purpose="bounded #331 shadow-teacher rollout",
            population=rollout.ROLLOUT_POPULATION,
            split=rollout.ROLLOUT_SPLIT,
            seeds=seeds,
            arena_revision="a" * 40,
            protocol_revision="a" * 40,
            provenance_reference="lisbun/lisjong-arena#331",
            allocation_timestamp="2026-09-22T00:00:00Z",
        )
        return ledger, seed_registry.allocation_binding(
            ledger, record["allocation_identity"]
        )

    def test_rollout_lock_requires_fresh_authoritative_population(self):
        seeds = tuple(range(100, 120))
        ledger, binding = self._authority(seeds)
        offline = seal(
            {
                "outcome": evaluation.OFFLINE_SKILL_QUALIFIED,
                "checkpoint_identity": "d" * 64,
                "scientific_corpus_identity": "c" * 64,
            }
        )
        provenance = {"lisjong_arena_revision": "a" * 40}
        with (
            patch.object(
                rollout, "collect_execution_provenance", return_value=object()
            ),
            patch.object(
                rollout,
                "execution_provenance_to_dict",
                return_value=provenance,
            ),
        ):
            lock = rollout.make_rollout_lock(
                seeds=seeds,
                allocation_binding=binding,
                seed_ledger=ledger,
                checkpoint=self._checkpoint(),
                offline_result=offline,
            )
        self.assertEqual(lock["ordered_seeds"], list(seeds))
        self.assertEqual(lock["hanchan"], 80)

    def test_rollout_lock_rejects_scientific_seed_overlap(self):
        seeds = (10, *range(100, 119))
        ledger, binding = self._authority(seeds)
        offline = seal(
            {
                "outcome": evaluation.OFFLINE_SKILL_QUALIFIED,
                "checkpoint_identity": "d" * 64,
                "scientific_corpus_identity": "c" * 64,
            }
        )
        with self.assertRaisesRegex(OffenseError, "overlap scientific"):
            rollout.make_rollout_lock(
                seeds=seeds,
                allocation_binding=binding,
                seed_ledger=ledger,
                checkpoint=self._checkpoint(),
                offline_result=offline,
            )


if __name__ == "__main__":
    unittest.main()
