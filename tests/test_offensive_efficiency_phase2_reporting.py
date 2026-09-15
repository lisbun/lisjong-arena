"""Focused tests for Issue #256 Phase-2 required breakdowns."""

import unittest

from lisjong_arena.offensive_efficiency_diagnostic.analysis import (
    DecisionIdentity,
    Phase2DecisionRecord,
    Phase2Sample,
    TerminalUniverseResult,
)
from lisjong_arena.offensive_efficiency_diagnostic.reporting import (
    derive_phase2_breakdowns,
)


def _terminal(regret: int) -> TerminalUniverseResult:
    return TerminalUniverseResult(
        selected_terminal_shanten_mass=10 + regret,
        best_terminal_shanten_mass=10,
        regret_mass=regret,
        sequence_denominator=100,
        best_tie_count=1,
        selected_is_best=regret == 0,
        best_action_reprs=("best",),
    )


def _record(
    *, seed: int, shanten: str, open_hand: bool, full_regret: int, eligible_regret: int
) -> Phase2DecisionRecord:
    return Phase2DecisionRecord(
        sample=Phase2Sample(
            identity=DecisionIdentity(seed, 0, 0),
            shanten_bucket=shanten,
            open_hand=open_hand,
        ),
        full_legal=_terminal(full_regret),
        baseline_eligible=_terminal(eligible_regret),
    )


class Phase2BreakdownTest(unittest.TestCase):
    def test_shanten_and_open_closed_breakdowns_are_derived(self) -> None:
        records = (
            _record(
                seed=651,
                shanten="1",
                open_hand=False,
                full_regret=10,
                eligible_regret=0,
            ),
            _record(
                seed=652,
                shanten="1",
                open_hand=True,
                full_regret=20,
                eligible_regret=10,
            ),
            _record(
                seed=653,
                shanten="2",
                open_hand=False,
                full_regret=0,
                eligible_regret=0,
            ),
        )
        breakdowns = derive_phase2_breakdowns(records)
        dimensions = {(item.dimension, item.value) for item in breakdowns}
        self.assertEqual(
            dimensions,
            {
                ("selected_shanten", "1"),
                ("selected_shanten", "2"),
                ("open_closed", "closed"),
                ("open_closed", "open"),
            },
        )

        full_shanten_one = next(
            item
            for item in breakdowns
            if item.universe == "FULL_LEGAL"
            and item.dimension == "selected_shanten"
            and item.value == "1"
        )
        self.assertEqual(full_shanten_one.population_count, 2)
        self.assertEqual(full_shanten_one.applicable_count, 2)
        self.assertEqual(full_shanten_one.nonzero_count, 2)
        self.assertEqual(full_shanten_one.nonzero_incidence, 1.0)
        self.assertEqual(full_shanten_one.mean_expected_regret, 0.15)
        self.assertEqual(full_shanten_one.max_expected_regret, 0.2)

        eligible_closed = next(
            item
            for item in breakdowns
            if item.universe == "BASELINE_ELIGIBLE"
            and item.dimension == "open_closed"
            and item.value == "closed"
        )
        self.assertEqual(eligible_closed.population_count, 2)
        self.assertEqual(eligible_closed.selected_best_count, 2)
        self.assertEqual(eligible_closed.selected_best_rate, 1.0)
        self.assertEqual(eligible_closed.nonzero_count, 0)

    def test_empty_population_has_no_breakdowns(self) -> None:
        self.assertEqual(derive_phase2_breakdowns(()), ())


if __name__ == "__main__":
    unittest.main()
