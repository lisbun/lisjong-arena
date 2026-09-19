"""Issue #251 RiichiLab disagreement diagnostic tests."""

import unittest
from unittest import mock

from _riichilab_source_pilot_fixtures import normal_discard_log, riichi_log
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichilab_disagreement_diagnostic import analysis as analysis_module
from lisjong_arena.riichilab_disagreement_diagnostic.analysis import (
    DiagnosticBlockedError,
    DisagreementAnalyzer,
)
from lisjong_arena.riichilab_source_pilot.materialization import materialize_game

GAME_MODE = "4p-red-single"
TARGET_SEATS = {Seat(index): 120 for index in range(4)}


def _materialize(log, observer=None):
    return materialize_game(
        log,
        game_id="fixture",
        target_seats=TARGET_SEATS,
        game_mode=GAME_MODE,
        decision_observer=observer,
    )


def _captured(log):
    observations = []
    result = _materialize(log, observations.append)
    return result, observations


def _discard_choice():
    _result, observations = _captured(normal_discard_log())
    for observation in observations:
        if not isinstance(observation.teacher_action, DiscardAction):
            continue
        alternatives = tuple(
            action
            for action in observation.decision.legal_actions
            if isinstance(action, DiscardAction)
            and action != observation.teacher_action
        )
        if observation.legal_action_count >= 2 and alternatives:
            return observation, alternatives[0]
    raise AssertionError("fixture did not expose a discard-vs-discard choice")


class _FixedPolicy:
    def __init__(self, selected, seen):
        self._selected = selected
        self._seen = seen

    def choose_action(self, decision):
        self._seen.append(decision)
        return self._selected


class MaterializationObserverTests(unittest.TestCase):
    def test_observer_is_behavior_preserving_for_gate0_materialization(self):
        expected = _materialize(normal_discard_log())
        observations = []
        actual = _materialize(normal_discard_log(), observations.append)

        self.assertEqual(actual, expected)
        self.assertEqual(
            sum(item.legal_action_count >= 2 for item in observations),
            len(actual.rows),
        )
        self.assertEqual(
            sum(item.legal_action_count == 1 for item in observations),
            actual.forced_rows,
        )
        for item in observations:
            self.assertIn(item.teacher_action, item.decision.legal_actions)

    def test_forced_decision_is_observed_for_coverage(self):
        result, observations = _captured(riichi_log())
        forced = [item for item in observations if item.legal_action_count == 1]
        self.assertEqual(len(forced), result.forced_rows)
        self.assertGreater(len(forced), 0)


class DisagreementAnalyzerTests(unittest.TestCase):
    def test_same_decision_context_and_exact_action_agree(self):
        observation, _alternative = _discard_choice()
        seen = []
        analyzer = DisagreementAnalyzer(
            policy_factory=lambda: _FixedPolicy(observation.teacher_action, seen)
        )

        analyzer.observe(observation)
        document = analyzer.aggregate_document()

        self.assertIs(seen[0], observation.decision)
        self.assertEqual(document["agreement"]["total"], 1)
        self.assertEqual(document["agreement"]["agreements"], 1)
        self.assertEqual(document["agreement"]["disagreements"], 0)

    def test_different_exact_action_is_disagreement_and_is_attributed(self):
        observation, alternative = _discard_choice()
        analyzer = DisagreementAnalyzer(
            policy_factory=lambda: _FixedPolicy(alternative, [])
        )

        analyzer.observe(observation)
        document = analyzer.aggregate_document()

        teacher_family = analysis_module.action_family(observation.teacher_action)
        baseline_family = analysis_module.action_family(alternative)
        self.assertEqual(document["agreement"]["disagreements"], 1)
        self.assertEqual(document["per_bot"]["Mortal-v4b"]["total"], 1)
        self.assertEqual(
            document["per_decision_kind"][observation.decision_kind.value]["total"],
            1,
        )
        self.assertEqual(
            document["action_family_confusion"][teacher_family][baseline_family],
            1,
        )
        hand_key = "open" if observation.is_open_hand else "closed"
        riichi_key = "riichi" if observation.is_riichi_declared else "non_riichi"
        self.assertEqual(document["hand_state"][hand_key]["total"], 1)
        self.assertEqual(document["riichi_state"][riichi_key]["total"], 1)
        self.assertEqual(
            document["legal_action_count"]["exact"][
                str(observation.legal_action_count)
            ]["total"],
            1,
        )

    def test_forced_decision_is_excluded_from_primary_agreement(self):
        _result, observations = _captured(riichi_log())
        forced = next(item for item in observations if item.legal_action_count == 1)

        def fail_if_called():
            raise AssertionError("forced decisions must not execute the baseline")

        analyzer = DisagreementAnalyzer(policy_factory=fail_if_called)
        analyzer.observe(forced)
        document = analyzer.aggregate_document()

        self.assertEqual(document["coverage"]["forced_decisions"], 1)
        self.assertEqual(document["coverage"]["choice_decisions"], 0)
        self.assertEqual(document["agreement"]["total"], 0)

    def test_illegal_baseline_result_fails_closed(self):
        observation, _alternative = _discard_choice()

        class BadPolicy:
            def choose_action(self, decision):
                return object()

        analyzer = DisagreementAnalyzer(policy_factory=BadPolicy)
        with self.assertRaises(DiagnosticBlockedError):
            analyzer.observe(observation)

    def test_discard_shanten_uses_only_visible_concealed_hand_minus_one_tile(self):
        observation, alternative = _discard_choice()
        analyzer = DisagreementAnalyzer(
            policy_factory=lambda: _FixedPolicy(alternative, [])
        )
        concealed = list(observation.decision.input.own_hand.concealed_tiles)
        expected_teacher = list(concealed)
        expected_teacher.remove(observation.teacher_action.tile)
        expected_baseline = list(concealed)
        expected_baseline.remove(alternative.tile)

        calls = []

        def fake_shanten(tiles):
            calls.append(tuple(tiles))
            return 0 if len(calls) == 1 else 1

        with mock.patch.object(
            analysis_module, "calculate_shanten", side_effect=fake_shanten
        ):
            analyzer.observe(observation)

        document = analyzer.aggregate_document()
        self.assertEqual(calls[0], tuple(expected_teacher))
        self.assertEqual(calls[1], tuple(expected_baseline))
        self.assertEqual(len(calls[0]), len(concealed) - 1)
        self.assertEqual(len(calls[1]), len(concealed) - 1)
        self.assertEqual(
            document["discard_shanten_disagreement"]["baseline_shanten_worse"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
