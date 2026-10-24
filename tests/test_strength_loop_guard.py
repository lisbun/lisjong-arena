"""Project #100: deterministic resource checks on synthetic accounting only."""

import unittest
from datetime import datetime, timezone

from lisjong_arena import strength_loop_guard as guard

NOW = datetime(2026, 10, 24, 6, tzinfo=timezone.utc)


def fixture():
    return {
        "schema": guard.SCHEMA,
        "policy": {
            "enabled": True,
            "month": "2026-10",
            "budget_microusd": {"aws": 20_000_000, "llm": 1_000_000},
            "max_age_seconds": 300,
            "stagnation_limit": 3,
            "weekly_session_limit": 2,
            "weekly_pr_limit": 2,
        },
        "snapshot": {
            "as_of": "2026-10-24T06:00:00Z",
            "paused": False,
            "billing_complete": True,
            "billed_microusd": {"aws": 2_000_000, "llm": 100_000},
            "runs": [],
            "cycles_without_promotion": 0,
            "iso_week": "2026-W43",
            "sessions_this_week": 0,
            "prs_this_week": 0,
        },
        "request": {
            "action": "development_run",
            "estimated_microusd": {"aws": 1_000_000, "llm": 0},
        },
    }


class LoopGuardTest(unittest.TestCase):
    def test_pass_is_only_a_resource_check(self):
        result = guard.evaluate(fixture(), now=NOW)
        self.assertEqual(result["status"], "RESOURCE_CHECK_PASSED")
        self.assertEqual(
            result["projected_microusd"], {"aws": 3_000_000, "llm": 100_000}
        )
        self.assertNotIn("authorized", result)

    def test_disabled_pause_unreconciled_billing_and_stagnation_each_stop(self):
        for section, field, value, reason in (
            ("policy", "enabled", False, "DISABLED"),
            ("snapshot", "paused", True, "PAUSED"),
            ("snapshot", "billing_complete", False, "BILLING_UNRECONCILED"),
            ("snapshot", "cycles_without_promotion", 3, "STAGNATION_LIMIT"),
        ):
            doc = fixture()
            doc[section][field] = value
            with self.subTest(reason=reason):
                result = guard.evaluate(doc, now=NOW)
                self.assertEqual(result["status"], "STOP")
                self.assertIn(reason, result["reasons"])

    def test_partially_billed_runs_are_not_double_counted(self):
        doc = fixture()
        doc["snapshot"]["runs"] = [
            {
                "run_id": "r1",
                "estimated_microusd": {"aws": 5_000_000, "llm": 0},
                "billed_microusd": {"aws": 2_000_000, "llm": 0},
                "finalized": False,
            }
        ]
        result = guard.evaluate(doc, now=NOW)
        self.assertEqual(result["outstanding_microusd"]["aws"], 3_000_000)
        self.assertEqual(result["projected_microusd"]["aws"], 6_000_000)
        doc["snapshot"]["runs"][0]["finalized"] = True
        self.assertEqual(
            guard.evaluate(doc, now=NOW)["projected_microusd"]["aws"], 3_000_000
        )

    def test_pending_and_next_run_cost_stop_at_limit_and_warn_at_eighty_percent(self):
        doc = fixture()
        for estimate, status, warning in (
            (13_999_999, "RESOURCE_CHECK_PASSED", False),
            (14_000_000, "RESOURCE_CHECK_PASSED", True),
            (18_000_000, "STOP", False),
        ):
            doc["request"]["estimated_microusd"]["aws"] = estimate
            result = guard.evaluate(doc, now=NOW)
            with self.subTest(estimate=estimate):
                self.assertEqual(result["status"], status)
                self.assertEqual("AWS_BUDGET_80_PERCENT" in result["warnings"], warning)
        doc = fixture()
        doc["policy"]["budget_microusd"]["llm"] = 0
        self.assertIn("LLM_BUDGET_LIMIT", guard.evaluate(doc, now=NOW)["reasons"])

    def test_unknown_budget_and_bad_numbers_fail_closed(self):
        for number in (None, True, -1, 1.5, float("nan"), float("inf")):
            doc = fixture()
            doc["policy"]["budget_microusd"]["aws"] = number
            with self.subTest(number=number), self.assertRaises(ValueError):
                guard.evaluate(doc, now=NOW)

    def test_stale_future_wrong_month_and_wrong_week_are_invalid(self):
        for field, value in (
            ("as_of", "2026-10-24T05:54:59Z"),
            ("as_of", "2026-10-24T06:00:01Z"),
            ("as_of", "invalid"),
            ("iso_week", "2026-W42"),
        ):
            doc = fixture()
            doc["snapshot"][field] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                guard.evaluate(doc, now=NOW)
        doc = fixture()
        doc["policy"]["month"] = "2026-11"
        with self.assertRaises(ValueError):
            guard.evaluate(doc, now=NOW)

    def test_october_exception_expires_in_november(self):
        doc = fixture()
        doc["policy"]["budget_microusd"]["aws"] = 30_000_000
        guard.evaluate(doc, now=NOW)
        doc["policy"]["month"] = "2026-11"
        doc["snapshot"].update(as_of="2026-11-01T00:00:00Z", iso_week="2026-W44")
        november = datetime(2026, 11, 1, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "month cap"):
            guard.evaluate(doc, now=november)
        doc["policy"]["budget_microusd"]["aws"] = 20_000_000
        guard.evaluate(doc, now=november)

    def test_weekly_limits_are_checked_before_the_corresponding_action(self):
        for action, counter, reason in (
            ("llm_session", "sessions_this_week", "WEEKLY_SESSION_LIMIT"),
            ("candidate_pr", "prs_this_week", "WEEKLY_PR_LIMIT"),
        ):
            doc = fixture()
            doc["request"]["action"] = action
            doc["snapshot"][counter] = 2
            self.assertIn(reason, guard.evaluate(doc, now=NOW)["reasons"])

    def test_duplicate_or_inconsistent_billing_is_rejected(self):
        doc = fixture()
        row = {
            "run_id": "r1",
            "estimated_microusd": {"aws": 3_000_000, "llm": 0},
            "billed_microusd": {"aws": 1_000_000, "llm": 0},
            "finalized": False,
        }
        doc["snapshot"]["runs"] = [row, row.copy()]
        with self.assertRaisesRegex(ValueError, "unique"):
            guard.evaluate(doc, now=NOW)
        doc["snapshot"]["runs"] = [row]
        row["billed_microusd"]["aws"] = 3_000_000
        with self.assertRaisesRegex(ValueError, "exceeds billed total"):
            guard.evaluate(doc, now=NOW)

    def test_open_run_overrun_requires_reconciliation(self):
        doc = fixture()
        doc["snapshot"]["runs"] = [
            {
                "run_id": "overrun",
                "estimated_microusd": {"aws": 1, "llm": 0},
                "billed_microusd": {"aws": 2, "llm": 0},
                "finalized": False,
            }
        ]
        with self.assertRaisesRegex(ValueError, "exceeded its estimate"):
            guard.evaluate(doc, now=NOW)

    def test_formal_and_unknown_fields_are_rejected(self):
        for action in ("s3", "promote", "merge"):
            doc = fixture()
            doc["request"]["action"] = action
            with self.assertRaises(ValueError):
                guard.evaluate(doc, now=NOW)
        doc = fixture()
        doc["snapshot"]["ignore_pause"] = True
        with self.assertRaises(ValueError):
            guard.evaluate(doc, now=NOW)
