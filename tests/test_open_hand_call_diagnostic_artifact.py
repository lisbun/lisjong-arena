"""Issue #196 diagnostic sidecar strict persistence tests。"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _round_stats_fixtures import neutral_seat_round_stats_tuple
from _single_round_artifact_fixtures import provenance
from lisjong.policy_contract import Seat

from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
    SingleRoundGameResult,
)
from lisjong_arena.open_hand_call_diagnostic_artifact import (
    OpenHandDiagnosticArtifactError,
    load_open_hand_diagnostic_artifact,
    save_open_hand_diagnostic_artifact,
)
from lisjong_arena.open_hand_call_diagnostics import (
    OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION,
    OpenHandDiagnosticEvaluationResult,
    OpenHandGameDiagnostics,
    aggregate_open_hand_diagnostics,
)
from lisjong_arena.single_round_artifact import save_single_round_artifact
from lisjong_arena.single_round_evaluation import aggregate_candidate_metrics


def _candidate_factory() -> object:
    return object()


def _baseline_factory() -> object:
    return object()


def _diagnostic_result() -> OpenHandDiagnosticEvaluationResult:
    plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(
            identity="open-hand-yaku-aware-call", factory=_candidate_factory
        ),
        baseline=PolicySpec(identity="yakuhai-call", factory=_baseline_factory),
        seeds=(101,),
    )
    scores = (25_000, 25_000, 25_000, 25_000)
    game_results = tuple(
        SingleRoundGameResult(
            seed=101,
            rotation=rotation,
            game_mode="4p-red-single",
            candidate_seat=Seat(rotation),
            scores=scores,
            seat_round_stats=neutral_seat_round_stats_tuple(scores),
        )
        for rotation in range(4)
    )
    evaluation = SingleRoundEvaluationResult(
        plan=plan,
        game_results=game_results,
        candidate_metrics=aggregate_candidate_metrics(
            "open-hand-yaku-aware-call", game_results
        ),
    )
    games = tuple(
        OpenHandGameDiagnostics(
            seed=101,
            rotation=rotation,
            candidate_seat=Seat(rotation),
            total_candidate_seat_decisions=10,
            same_action_decisions=9,
            divergent_action_decisions=1,
            shared_prefix_initial_call_opportunities=1,
            baseline_pass_to_candidate_chi=1 if rotation % 2 == 0 else 0,
            baseline_pass_to_candidate_pon=1 if rotation % 2 == 1 else 0,
            scaled_candidate_score_delta=0,
        )
        for rotation in range(4)
    )
    return OpenHandDiagnosticEvaluationResult(
        evaluation_result=evaluation,
        game_diagnostics=games,
        summary=aggregate_open_hand_diagnostics(games),
    )


def _save_pair(root: Path, suffix: str = "") -> tuple[Path, Path]:
    result = _diagnostic_result()
    strength_path = root / f"strength{suffix}.json"
    diagnostic_path = root / f"diagnostic{suffix}.json"
    with mock.patch(
        "lisjong_arena.single_round_artifact.collect_execution_provenance",
        return_value=replace(
            provenance(), lisjong_revision=OPEN_HAND_DIAGNOSTIC_LISJONG_REVISION
        ),
    ):
        save_single_round_artifact(result.evaluation_result, strength_path)
    save_open_hand_diagnostic_artifact(
        result,
        strength_artifact_path=strength_path,
        path=diagnostic_path,
    )
    return strength_path, diagnostic_path


class OpenHandDiagnosticArtifactTest(unittest.TestCase):
    def test_strict_round_trip_and_deterministic_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strength_a, diagnostic_a = _save_pair(root, "-a")
            strength_b, diagnostic_b = _save_pair(root, "-b")

            loaded = load_open_hand_diagnostic_artifact(
                diagnostic_a, strength_artifact_path=strength_a
            )

            self.assertEqual(loaded.summary.candidate_only_chi_count, 2)
            self.assertEqual(loaded.summary.candidate_only_pon_count, 2)
            self.assertEqual(strength_a.read_bytes(), strength_b.read_bytes())
            self.assertEqual(diagnostic_a.read_bytes(), diagnostic_b.read_bytes())

    def test_tampered_diagnostic_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strength, diagnostic = _save_pair(root)
            document = json.loads(diagnostic.read_text(encoding="utf-8"))
            document["game_diagnostics"][0]["scaled_candidate_score_delta"] = 1
            diagnostic.write_text(
                json.dumps(document, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(OpenHandDiagnosticArtifactError):
                load_open_hand_diagnostic_artifact(
                    diagnostic, strength_artifact_path=strength
                )

    def test_tampered_strength_artifact_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strength, diagnostic = _save_pair(root)
            strength.write_text(
                strength.read_text(encoding="utf-8") + " ", encoding="utf-8"
            )

            with self.assertRaisesRegex(OpenHandDiagnosticArtifactError, "SHA-256"):
                load_open_hand_diagnostic_artifact(
                    diagnostic, strength_artifact_path=strength
                )

    def test_unknown_sidecar_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            strength, diagnostic = _save_pair(root)
            document = json.loads(diagnostic.read_text(encoding="utf-8"))
            document["unexpected"] = True
            diagnostic.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaises(OpenHandDiagnosticArtifactError):
                load_open_hand_diagnostic_artifact(
                    diagnostic, strength_artifact_path=strength
                )


if __name__ == "__main__":
    unittest.main()
