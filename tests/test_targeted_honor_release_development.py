"""Issue #263 targeted honor-release development contract tests.

No real 651..750 replay or fresh Phase-B population is executed in CI.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _progression_development_fixtures import (
    constant_focal_score,
    evaluation_result,
    provenance,
    save_arm_artifact,
)
from lisjong.policies.targeted_honor_release_terminal_progression import (
    HandValueDecisiveStage,
    TargetedHonorReleaseActivationStage,
    TargetedHonorReleaseAnalysis,
    TargetedHonorReleaseBranch,
)
from lisjong.policies.terminal_shanten_progression_mechanism_riichi_defense import (
    ProgressionCandidateEvaluation,
)
from lisjong.policy_contract import DecisionContext, DecisionTrace, DiscardAction, Seat
from lisjong.policy_contract.own_hand_state import OwnHandState
from lisjong.policy_contract.player_state import PlayerPublicState
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.round_state import RoundState
from lisjong.policy_contract.tile import Tile, TileCategory, TileType
from lisjong.policy_contract.wind import Wind

import lisjong_arena.targeted_honor_release_development.diagnostic as diagnostic
import lisjong_arena.targeted_honor_release_development.lock as lock_module
from lisjong_arena.targeted_honor_release_development.artifact import (
    build_artifact,
    load_artifact,
    save_artifact,
)
from lisjong_arena.targeted_honor_release_development.diagnostic import (
    DecisionIdentity,
    DecisionRecord,
    GameDiagnostics,
    PhaseADiagnosticResult,
    aggregate_diagnostics,
    build_phase_a_plan,
    classify_gate,
    require_trajectory_identity,
)
from lisjong_arena.targeted_honor_release_development.lock import (
    TargetedHonorReleaseLockError,
    build_lock_document,
    load_lock_document,
    save_lock_document,
)
from lisjong_arena.targeted_honor_release_development.paired import (
    build_classified_result,
    build_paired_result,
    save_classified_result,
    save_paired_result,
    verify_classified_result,
    verify_paired_result,
)
from lisjong_arena.targeted_honor_release_development.protocol import (
    CANDIDATE_IDENTITY,
    DIAGNOSTIC_COMPLETE_LABEL,
    INCONCLUSIVE_LABEL,
    LISJONG_REVISION,
    OPPORTUNITY_NOT_OBSERVED_LABEL,
    PARENT_IDENTITY,
    PHASE_A_GAME_COUNT,
    PHASE_A_SEEDS,
    SIGNAL_LABEL,
    TargetedHonorReleaseProtocolError,
    candidate_spec,
    comparator_spec,
    parent_spec,
    require_exact_candidate_semantics,
    require_phase_b_population,
    seed_freshness_block,
)

ARENA_REVISION = "a" * 40
ENGINE_REVISION = "b" * 40
FRESH_SEEDS = tuple(range(751, 851))

M3 = Tile(TileType(TileCategory.MANZU, 3))
M4 = Tile(TileType(TileCategory.MANZU, 4))
EAST = Tile(TileType(TileCategory.HONOR, 1))
A_M3 = DiscardAction(Seat.SEAT_0, M3, False)
A_EAST = DiscardAction(Seat.SEAT_0, EAST, False)


def _live_provenance():
    return provenance(lisjong_revision=LISJONG_REVISION).__class__(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision=ARENA_REVISION,
        lisjong_version="0.1.0",
        lisjong_revision=LISJONG_REVISION,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision=ENGINE_REVISION,
        riichienv_version="0.4.10",
        python_version="3.14.6",
    )


def _player() -> PlayerPublicState:
    return PlayerPublicState(25000, (), (), RiichiState.NONE)


def _policy_input() -> object:
    from lisjong.policy_contract.policy_input import PolicyInput

    return PolicyInput(
        self_seat=Seat.SEAT_0,
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1,
            dealer_seat=Seat.SEAT_0,
            honba=0,
            riichi_sticks=0,
            dora_indicators=(),
            live_wall_tiles_remaining=70,
        ),
        players=(_player(), _player(), _player(), _player()),
        own_hand=OwnHandState((M3, M4, EAST), M4),
    )


def _progression(action: DiscardAction, mass: int) -> ProgressionCandidateEvaluation:
    counts = [0] * 9
    counts[1] = mass
    return ProgressionCandidateEvaluation(
        action=action,
        completion_mass=0,
        root_post_discard_shanten=3,
        terminal_shanten_mass=mass,
        terminal_shanten_counts=tuple(counts),
    )


def _active_analysis() -> TargetedHonorReleaseAnalysis:
    return TargetedHonorReleaseAnalysis(
        activation_stage=TargetedHonorReleaseActivationStage.R5_HONOR_ONLY_SWITCH,
        parent_action=A_M3,
        selected_action=A_EAST,
        action_changed=True,
        branch=TargetedHonorReleaseBranch.PUSH,
        closed_hand=True,
        parent_post_discard_shanten=3,
        parent_current_ukeire=8,
        parent_retained_real_value=0,
        eligible_candidate_count=2,
        target_candidate_count=2,
        honor_target_candidate_count=1,
        hva_decisive_stage=HandValueDecisiveStage.YAKU_ROUTE,
        horizon=3,
        hidden_tile_count=70,
        sequence_denominator=328440,
        progression_evaluations=(
            _progression(A_M3, 2),
            _progression(A_EAST, 1),
        ),
        r5_best_count=1,
    )


def _decision_record(
    *, seed: int = 651, rotation: int = 0, elapsed: float = 0.01
) -> DecisionRecord:
    return DecisionRecord(
        identity=DecisionIdentity(seed, rotation, 0),
        candidate_seat=Seat(rotation),
        parent_action_repr=repr(A_M3),
        candidate_action_repr=repr(A_EAST),
        action_changed=True,
        activation_stage=TargetedHonorReleaseActivationStage.R5_HONOR_ONLY_SWITCH,
        branch=TargetedHonorReleaseBranch.PUSH,
        closed_hand=True,
        parent_post_discard_shanten=3,
        parent_current_ukeire=8,
        parent_retained_real_value=0,
        eligible_candidate_count=2,
        target_candidate_count=2,
        honor_target_candidate_count=1,
        hva_decisive_stage=HandValueDecisiveStage.YAKU_ROUTE,
        r5_activated=True,
        r5_best_count=1,
        sequence_denominator=328440,
        terminal_shanten_masses=(2, 1),
        candidate_elapsed_seconds=elapsed,
    )


def _game_diagnostics() -> tuple[GameDiagnostics, ...]:
    games = []
    for seed in PHASE_A_SEEDS:
        for rotation in range(4):
            records = (
                (_decision_record(seed=seed, rotation=rotation),)
                if seed == PHASE_A_SEEDS[0] and rotation == 0
                else ()
            )
            games.append(
                GameDiagnostics(
                    seed=seed,
                    rotation=rotation,
                    candidate_seat=Seat(rotation),
                    focal_decision_count=len(records),
                    discard_decision_count=len(records),
                    choice_discard_decision_count=len(records),
                    forced_discard_decision_count=0,
                    game_wall_clock_seconds=0.1,
                    candidate_runtime_total_seconds=float(
                        sum(record.candidate_elapsed_seconds for record in records)
                    ),
                    records=records,
                )
            )
    return tuple(games)


def _phase_a_result() -> PhaseADiagnosticResult:
    plan = build_phase_a_plan()
    evaluation = evaluation_result(plan, constant_focal_score(25000))
    games = _game_diagnostics()
    aggregate = aggregate_diagnostics(games, replay_wall_clock_seconds=40.0)
    gate = classify_gate(
        trajectory_identity_passed=True,
        game_count=PHASE_A_GAME_COUNT,
        execution_failure_count=0,
        aggregate=aggregate,
    )
    return PhaseADiagnosticResult(
        evaluation_result=evaluation,
        game_diagnostics=games,
        records=tuple(record for game in games for record in game.records),
        aggregate=aggregate,
        gate=gate,
    )


def _source_parent_artifact(directory: Path) -> Path:
    path = directory / "issue-252-parent.json"
    save_arm_artifact(
        evaluation_result(build_phase_a_plan(), constant_focal_score(25000)),
        path,
        execution_provenance=provenance(
            lisjong_revision="b06b83fb9b3acbe3fc2acb601d364e17e11aba80"
        ),
    )
    return path


class ProtocolTest(unittest.TestCase):
    def test_exact_candidate_parent_and_trace_binding(self) -> None:
        binding = require_exact_candidate_semantics()
        self.assertEqual(binding.identity, CANDIDATE_IDENTITY)
        self.assertEqual(binding.analysis_class_name, "TargetedHonorReleaseAnalysis")
        self.assertEqual(parent_spec().identity, PARENT_IDENTITY)
        self.assertNotEqual(candidate_spec().identity, comparator_spec().identity)

    def test_phase_b_requires_exact_contiguous_100_seed_shape(self) -> None:
        self.assertEqual(require_phase_b_population(FRESH_SEEDS), FRESH_SEEDS)
        with self.assertRaises(TargetedHonorReleaseProtocolError):
            require_phase_b_population(tuple(range(751, 850)))
        with self.assertRaises(TargetedHonorReleaseProtocolError):
            require_phase_b_population(tuple(range(700, 800)))

    def test_external_freshness_confirmation_is_mandatory(self) -> None:
        with self.assertRaisesRegex(TargetedHonorReleaseProtocolError, "local/private"):
            seed_freshness_block(
                FRESH_SEEDS,
                external_freshness_confirmed=False,
            )

    def test_external_collision_requires_reformulation(self) -> None:
        with self.assertRaisesRegex(
            TargetedHonorReleaseProtocolError, "SEED PLAN REFORMULATE"
        ):
            seed_freshness_block(
                FRESH_SEEDS,
                external_freshness_confirmed=True,
                additional_allocated_seeds=(800,),
            )


class ParentTrajectoryObservationTest(unittest.TestCase):
    def test_candidate_is_observed_once_but_parent_action_drives_trajectory(
        self,
    ) -> None:
        class Parent:
            def choose_action(self, decision):
                del decision
                return A_M3

        recorder = diagnostic._Recorder(
            seed=651, rotation=0, candidate_seat=Seat.SEAT_0
        )
        wrapper = diagnostic._ObservedParentPolicy(Parent(), recorder)
        decision = DecisionContext(
            input=_policy_input(),
            legal_actions=(A_M3, A_EAST),
        )

        def traced(candidate, observed_decision, sink):
            del candidate
            sink.on_decision(
                DecisionTrace(
                    legal_actions=observed_decision.legal_actions,
                    selected_action=A_EAST,
                    analysis=_active_analysis(),
                )
            )
            return A_EAST

        with mock.patch.object(
            diagnostic, "execute_policy_with_trace", side_effect=traced
        ) as candidate_call:
            selected = wrapper.choose_action(decision)

        self.assertIs(selected, A_M3)
        candidate_call.assert_called_once()
        (record,) = recorder.records
        self.assertTrue(record.action_changed)
        self.assertTrue(record.r5_activated)

    def test_forced_discard_executes_shadow_candidate_once_for_runtime(self) -> None:
        recorder = diagnostic._Recorder(
            seed=651, rotation=0, candidate_seat=Seat.SEAT_0
        )
        decision = DecisionContext(input=_policy_input(), legal_actions=(A_M3,))

        def traced(candidate, observed_decision, sink):
            del candidate
            sink.on_decision(
                DecisionTrace(
                    legal_actions=observed_decision.legal_actions,
                    selected_action=A_M3,
                    analysis=None,
                )
            )
            return A_M3

        with mock.patch.object(
            diagnostic, "execute_policy_with_trace", side_effect=traced
        ) as candidate_call:
            recorder.observe(decision, A_M3)
        candidate_call.assert_called_once()
        self.assertEqual(recorder.forced_discard_decision_count, 1)
        self.assertEqual(recorder.records, [])
        self.assertGreaterEqual(recorder.candidate_runtime_total_seconds, 0.0)


class DiagnosticGateTest(unittest.TestCase):
    def test_gate_passes_only_with_observed_r5_change_and_bounded_runtime(self) -> None:
        result = _phase_a_result()
        self.assertTrue(result.gate.passed)
        self.assertEqual(result.gate.label, DIAGNOSTIC_COMPLETE_LABEL)

    def test_zero_action_change_blocks_phase_b(self) -> None:
        games = tuple(
            GameDiagnostics(
                seed=seed,
                rotation=rotation,
                candidate_seat=Seat(rotation),
                focal_decision_count=0,
                discard_decision_count=0,
                choice_discard_decision_count=0,
                forced_discard_decision_count=0,
                game_wall_clock_seconds=0.01,
                candidate_runtime_total_seconds=0.0,
                records=(),
            )
            for seed in PHASE_A_SEEDS
            for rotation in range(4)
        )
        aggregate = aggregate_diagnostics(games, replay_wall_clock_seconds=1.0)
        gate = classify_gate(
            trajectory_identity_passed=True,
            game_count=PHASE_A_GAME_COUNT,
            execution_failure_count=0,
            aggregate=aggregate,
        )
        self.assertFalse(gate.passed)
        self.assertEqual(gate.label, OPPORTUNITY_NOT_OBSERVED_LABEL)


class TrajectoryIdentityTest(unittest.TestCase):
    def test_replay_mismatch_is_rejected(self) -> None:
        with TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            parent_path = _source_parent_artifact(directory)
            from lisjong_arena.progression_development.paired import load_arm_artifact

            parent = load_arm_artifact(parent_path)
            replay = evaluation_result(
                build_phase_a_plan(), constant_focal_score(25001)
            )
            with self.assertRaisesRegex(
                diagnostic.TargetedHonorReleaseDiagnosticError,
                "TRAJECTORY IDENTITY FAILURE",
            ):
                require_trajectory_identity(replay, parent)


class PhaseAArtifactTest(unittest.TestCase):
    def test_roundtrip_rederives_summary_and_gate(self) -> None:
        with TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            parent_path = _source_parent_artifact(directory)
            path = directory / "phase-a.json"
            result = _phase_a_result()
            document = build_artifact(
                phase_a=result,
                parent_artifact_path=parent_path,
                max_workers=8,
                provenance=_live_provenance(),
            )
            save_artifact(document, path)
            loaded = load_artifact(path, parent_artifact_path=parent_path)
        self.assertEqual(loaded["result_identity"], document["result_identity"])
        self.assertEqual(loaded["gate"]["label"], DIAGNOSTIC_COMPLETE_LABEL)


class LockTest(unittest.TestCase):
    def _destinations(self, directory: Path) -> dict[str, Path]:
        return {
            "phase_a_diagnostic": directory / "phase-a.json",
            "candidate_artifact": directory / "candidate.json",
            "parent_artifact": directory / "parent.json",
            "paired_result": directory / "paired.json",
            "classified_result": directory / "classified.json",
        }

    def test_lock_binds_dynamic_fresh_population_and_exact_revision(self) -> None:
        with TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            source = _source_parent_artifact(directory)
            destinations = self._destinations(directory)
            lock_path = directory / "lock.json"
            live = _live_provenance()
            with (
                mock.patch.object(
                    lock_module, "collect_execution_provenance", return_value=live
                ),
                mock.patch.object(
                    lock_module, "require_clean_arena_head", return_value=ARENA_REVISION
                ),
                mock.patch.object(
                    lock_module,
                    "require_merged_arena_revision",
                    return_value=ARENA_REVISION,
                ),
                mock.patch.object(
                    lock_module,
                    "_runtime_document",
                    return_value={
                        "python_implementation": "CPython",
                        "python_version": "3.14.6",
                        "sys_version": "3.14.6",
                    },
                ),
            ):
                document = build_lock_document(
                    parent_artifact_path=source,
                    destinations=destinations,
                    phase_b_seeds=FRESH_SEEDS,
                    max_workers=8,
                    external_freshness_confirmed=True,
                )
                save_lock_document(document, lock_path)
                loaded = load_lock_document(lock_path)
        self.assertEqual(loaded["phase_b"]["ordered_seeds"], list(FRESH_SEEDS))
        self.assertEqual(loaded["provenance"]["lisjong_revision"], LISJONG_REVISION)


class PairedResultTest(unittest.TestCase):
    def _save_arm(self, directory: Path, *, candidate: bool, score: int) -> Path:
        seeds = FRESH_SEEDS
        spec = candidate_spec() if candidate else parent_spec()
        plan = diagnostic.SingleRoundEvaluationPlan(
            candidate=spec,
            baseline=comparator_spec(),
            seeds=seeds,
            max_steps=10_000,
        )
        result = evaluation_result(plan, constant_focal_score(score))
        path = directory / ("candidate.json" if candidate else "parent.json")
        save_arm_artifact(result, path, execution_provenance=_live_provenance())
        return path

    def test_positive_paired_result_uses_100_seed_blocks(self) -> None:
        from lisjong_arena.progression_development.paired import load_arm_artifact

        with TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            h_path = self._save_arm(directory, candidate=True, score=25100)
            c_path = self._save_arm(directory, candidate=False, score=25000)
            h = load_arm_artifact(h_path)
            c = load_arm_artifact(c_path)
            paired_path = directory / "paired.json"
            classified_path = directory / "classified.json"
            document = build_paired_result(
                candidate_artifact=h,
                candidate_artifact_path=h_path,
                parent_artifact=c,
                parent_artifact_path=c_path,
                seeds=FRESH_SEEDS,
                worker_count=8,
            )
            save_paired_result(document, paired_path)
            verified = verify_paired_result(
                paired_path,
                candidate_artifact_path=h_path,
                parent_artifact_path=c_path,
            )
            classified = build_classified_result(
                paired_result=verified, paired_result_path=paired_path
            )
            save_classified_result(classified, classified_path)
            verify_classified_result(classified_path, paired_result_path=paired_path)
        self.assertEqual(verified["primary_summary"]["block_count"], 100)
        self.assertEqual(verified["classification"]["label"], SIGNAL_LABEL)

    def test_equal_arms_are_inconclusive(self) -> None:
        from lisjong_arena.progression_development.paired import load_arm_artifact

        with TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            h_path = self._save_arm(directory, candidate=True, score=25000)
            c_path = self._save_arm(directory, candidate=False, score=25000)
            document = build_paired_result(
                candidate_artifact=load_arm_artifact(h_path),
                candidate_artifact_path=h_path,
                parent_artifact=load_arm_artifact(c_path),
                parent_artifact_path=c_path,
                seeds=FRESH_SEEDS,
                worker_count=8,
            )
        self.assertEqual(document["classification"]["label"], INCONCLUSIVE_LABEL)


if __name__ == "__main__":
    unittest.main()
