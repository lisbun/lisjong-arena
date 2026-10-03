"""lisjong#236 stage-1 diagnostic (scripts/diagnose_single_riichi_defense_236.py).

Pins the aggregation scope (exactly one opponent in riichi, self not in riichi,
>= 2 legal discards), the per-branch denominators, and the RETIRED-seed guard.
Hanchan execution is not run here.
"""

import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import diagnose_single_riichi_defense_236 as diagnostic  # noqa: E402


def _row(branch, *, riichi=(1,), own=False, legal=3, dealt=0, to_riichi=True):
    return {
        "round": ("EAST", 1, 0),
        "own_riichi": own,
        "riichi_opponents": riichi,
        "legal_discards": legal,
        "branch": branch,
        "dealt_in": dealt > 0,
        "deal_in_to_riichi": dealt > 0 and to_riichi,
        "deal_in_points": dealt,
    }


class AggregateTest(unittest.TestCase):
    def test_scope_excludes_other_states(self):
        games = [
            {
                "seed": 931000,
                "rounds": 9,
                "rows": [
                    _row("PUSH_TENPAI"),
                    _row("PUSH_TENPAI", riichi=()),
                    _row("PUSH_TENPAI", riichi=(1, 2)),
                    _row("PUSH_TENPAI", own=True),
                    _row("PUSH_TENPAI", legal=1),
                ],
            }
        ]
        summary = diagnostic._aggregate(games)
        self.assertEqual(summary["decisions"], 1)
        self.assertEqual(summary["by_branch"]["PUSH_TENPAI"]["decisions"], 1)

    def test_branch_denominators_and_points(self):
        games = [
            {
                "seed": 931000,
                "rounds": 8,
                "rows": [
                    _row("MECHANISM_DEFENSE_FILTERED"),
                    _row("MECHANISM_DEFENSE_FILTERED", dealt=8000),
                    _row("FALLBACK_ALL_LEGAL_1_SHANTEN", dealt=2000, to_riichi=False),
                    _row("FOLD_COMMON_GENBUTSU"),
                ],
            }
        ]
        summary = diagnostic._aggregate(games)
        mechanism = summary["by_branch"]["MECHANISM_DEFENSE_FILTERED"]
        self.assertEqual(mechanism["decisions"], 2)
        self.assertEqual(mechanism["deal_ins"], 1)
        self.assertEqual(mechanism["deal_in_rate_per_decision"], 0.5)
        self.assertEqual(mechanism["deal_in_points_share"], 0.8)
        fallback = summary["by_branch"]["FALLBACK_ALL_LEGAL_1_SHANTEN"]
        self.assertEqual(fallback["deal_ins_to_riichi"], 0)
        self.assertEqual(summary["deal_in_points_total"], 10000)
        self.assertIsNone(
            summary["by_branch"]["NO_ANALYSIS"]["mean_points_per_deal_in"]
        )
        self.assertEqual(summary["rounds"], 8)

    def test_seed_range_must_stay_retired(self):
        with self.assertRaises(SystemExit):
            diagnostic.main(["--games", "2", "--first-seed", "931999", "--output", "x"])
        with self.assertRaises(SystemExit):
            diagnostic.main(["--games", "1", "--first-seed", "930999", "--output", "x"])


if __name__ == "__main__":
    unittest.main()
