"""Issue #256 offensive-efficiency diagnostic unit/contract tests.

No real #252 replay or R5 search is executed in CI.  Locked population constants,
transport semantics, deterministic sampling, trajectory rejection and artifact
re-derivation are exercised with synthetic first-party values.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from _progression_development_fixtures import (
    constant_focal_score,
    evaluation_result,
    provenance,
    save_arm_artifact,
)
from lisjong.policies.mechanism_riichi_defense_offensive_efficiency_diagnostic import (
    OffensiveEfficiencyBranch,
)
from lisjong.policy_contract import DecisionContext, DiscardAction, Seat
from lisjong.policy_contract.own_hand_state import OwnHandState
from lisjong.policy_contract.player_state import PlayerPublicState
from lisjong.policy_contract.policy_input import PolicyInput
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.round_state import RoundState
from lisjong.policy_contract.tile import Tile, TileCategory, TileType
from lisjong.policy_contract.wind import Wind

import lisjong_arena.offensive_efficiency_diagnostic.analysis as analysis_module
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.offensive_efficiency_diagnostic.analysis import (
    PHASE2_SAMPLE_LIMIT,
    DecisionIdentity,
    DecisionKind,
    OffensiveEfficiencyDiagnosticError,
    Phase1DecisionRecord,
    Phase1EvaluationResult,
    Phase1GameDiagnostics,
    Phase2DecisionRecord,
    Phase2Sample,
    TerminalUniverseResult,
    TurnBucket,
    _Phase1Recorder,
    _turn_bucket,
    aggregate_phase1,
    aggregate_phase2,
    build_cluster_summaries,
    build_phase1_plan,
    require_trajectory_identity,
    select_phase2_samples,
)
from lisjong_arena.offensive_efficiency_diagnostic.artifact import (
    OffensiveEfficiencyArtifactError,
    build_artifact,
    load_artifact,
    save_artifact,
)
from lisjong_arena.progression_development.protocol import (
    COMPARATOR_IDENTITY,
    MAX_STEPS,
    PARENT_IDENTITY,
    PHASE_B_GAMES_PER_ARM,
    PHASE_B_SEEDS,
    ROTATION_COUNT,
    document_identity,
)

_LISJONG_256_REVISION = "b06b83fb9b3acbe3fc2acb601d364e17e11aba80"
_TILE_1M = Tile(TileType(TileCategory.MANZU, 1))
_TILE_2M = Tile(TileType(TileCategory.MANZU, 2))


def _player() -> PlayerPublicState:
    return PlayerPublicState(
        score=25000, discards=(), melds=(), riichi=RiichiState.NONE
    )


def _policy_input(*, drawn: bool = True) -> PolicyInput:
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
        own_hand=OwnHandState(
            concealed_tiles=(_TILE_1M, _TILE_2M),
            drawn_tile=_TILE_2M if drawn else None,
        ),
    )


def _discard(tile: Tile) -> DiscardAction:
    return DiscardAction(actor=Seat.SEAT_0, tile=tile, tsumogiri=False)


def _record(
    *,
    seed: int = 651,
    rotation: int = 0,
    ordinal: int = 0,
    shanten: int = 1,
    open_hand: bool = False,
    full_all_zero: bool = False,
    eligible_all_zero: bool = False,
    full_ukeire: int | None = 0,
    eligible_ukeire: int | None = 0,
    full_second: int | None = None,
    eligible_second: int | None = None,
    full_completion: int = 0,
    eligible_completion: int = 0,
) -> Phase1DecisionRecord:
    return Phase1DecisionRecord(
        identity=DecisionIdentity(seed, rotation, ordinal),
        candidate_seat=Seat(rotation),
        decision_kind=DecisionKind.NORMAL_TURN,
        open_hand=open_hand,
        self_riichi=False,
        turn_bucket=TurnBucket.EARLY,
        branch=OffensiveEfficiencyBranch.PUSH,
        legal_discard_count=2,
        baseline_eligible_discard_count=2,
        selected_action_repr="DiscardAction(test)",
        selected_post_discard_shanten=shanten,
        full_shanten_regret=0,
        eligible_shanten_regret=0,
        full_ukeire_regret=full_ukeire,
        eligible_ukeire_regret=eligible_ukeire,
        full_second_step_regret=full_second,
        eligible_second_step_regret=eligible_second,
        full_completion_regret=full_completion,
        eligible_completion_regret=eligible_completion,
        full_completion_all_zero=full_all_zero,
        eligible_completion_all_zero=eligible_all_zero,
    )


def _game_diagnostics(
    records_by_game: dict[tuple[int, int], tuple[Phase1DecisionRecord, ...]]
    | None = None,
) -> tuple[Phase1GameDiagnostics, ...]:
    records_by_game = records_by_game or {}
    games = []
    for seed in PHASE_B_SEEDS:
        for rotation in range(ROTATION_COUNT):
            records = records_by_game.get((seed, rotation), ())
            choice = len(records)
            games.append(
                Phase1GameDiagnostics(
                    seed=seed,
                    rotation=rotation,
                    candidate_seat=Seat(rotation),
                    focal_decision_count=choice,
                    discard_decision_count=choice,
                    choice_discard_decision_count=choice,
                    forced_discard_decision_count=0,
                    records=records,
                )
            )
    return tuple(games)


def _phase1_result(
    *, focal_score: int = 25000, with_record: bool = False
) -> Phase1EvaluationResult:
    plan = build_phase1_plan()
    evaluation = evaluation_result(plan, constant_focal_score(focal_score))
    records_by_game = {}
    if with_record:
        records_by_game[(651, 0)] = (
            _record(full_all_zero=True, eligible_all_zero=True),
        )
    games = _game_diagnostics(records_by_game)
    records = tuple(record for game in games for record in game.records)
    return Phase1EvaluationResult(
        evaluation_result=evaluation,
        game_diagnostics=games,
        records=records,
        aggregate=aggregate_phase1(games),
        clusters=build_cluster_summaries(records),
    )


def _phase2_record(sample: Phase2Sample) -> Phase2DecisionRecord:
    full = TerminalUniverseResult(
        selected_terminal_shanten_mass=20,
        best_terminal_shanten_mass=10,
        regret_mass=10,
        sequence_denominator=100,
        best_tie_count=1,
        selected_is_best=False,
        best_action_reprs=("DiscardAction(best)",),
    )
    eligible = TerminalUniverseResult(
        selected_terminal_shanten_mass=20,
        best_terminal_shanten_mass=20,
        regret_mass=0,
        sequence_denominator=100,
        best_tie_count=2,
        selected_is_best=True,
        best_action_reprs=("DiscardAction(a)", "DiscardAction(b)"),
    )
    return Phase2DecisionRecord(
        sample=sample, full_legal=full, baseline_eligible=eligible
    )


class LockedPopulationTest(unittest.TestCase):
    def test_phase1_plan_reuses_exact_252_parent_population(self) -> None:
        plan = build_phase1_plan()
        self.assertEqual(plan.candidate.identity, PARENT_IDENTITY)
        self.assertEqual(plan.baseline.identity, COMPARATOR_IDENTITY)
        self.assertEqual(plan.seeds, tuple(range(651, 751)))
        self.assertEqual(plan.seeds, PHASE_B_SEEDS)
        self.assertEqual(len(plan.seeds) * ROTATION_COUNT, PHASE_B_GAMES_PER_ARM)
        self.assertEqual(plan.max_steps, MAX_STEPS)

    def test_arena_does_not_import_private_lisjong_metric_helpers(self) -> None:
        source = Path(analysis_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        policy_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("lisjong.policies.")
        }
        self.assertEqual(
            policy_imports,
            {
                "lisjong.policies.mechanism_riichi_defense_offensive_efficiency_diagnostic"
            },
        )


class DecisionPopulationTest(unittest.TestCase):
    def test_turn_bucket_boundaries_are_fixed(self) -> None:
        self.assertIs(_turn_bucket(0), TurnBucket.EARLY)
        self.assertIs(_turn_bucket(5), TurnBucket.EARLY)
        self.assertIs(_turn_bucket(6), TurnBucket.MIDDLE)
        self.assertIs(_turn_bucket(11), TurnBucket.MIDDLE)
        self.assertIs(_turn_bucket(12), TurnBucket.LATE)

    def test_forced_discard_is_counted_but_never_calls_172(self) -> None:
        action = _discard(_TILE_1M)
        decision = DecisionContext(input=_policy_input(), legal_actions=(action,))
        recorder = _Phase1Recorder(seed=651, rotation=0, candidate_seat=Seat.SEAT_0)
        with patch.object(
            analysis_module,
            "analyze_mechanism_riichi_defense_offensive_efficiency",
            side_effect=AssertionError("#172 must not run for forced discard"),
        ):
            recorder.record(decision, action)
        snapshot = recorder.snapshot()
        self.assertEqual(snapshot.discard_decision_count, 1)
        self.assertEqual(snapshot.choice_discard_decision_count, 0)
        self.assertEqual(snapshot.forced_discard_decision_count, 1)
        self.assertEqual(snapshot.records, ())

    def test_choice_discard_calls_172_once_with_r5_disabled_and_transports_values(
        self,
    ) -> None:
        action = _discard(_TILE_1M)
        other = _discard(_TILE_2M)
        decision = DecisionContext(input=_policy_input(), legal_actions=(action, other))
        fake = SimpleNamespace(
            baseline_selected_action=action,
            branch=OffensiveEfficiencyBranch.PUSH,
            legal_discard_actions=(action, other),
            baseline_eligible_actions=(action, other),
            candidate_evaluations=(
                SimpleNamespace(action=action, post_discard_shanten=1),
                SimpleNamespace(action=other, post_discard_shanten=1),
            ),
            full_legal_summary=SimpleNamespace(
                shanten_regret=0,
                ukeire_regret=None,
                second_step_regret=0,
                completion_regret=7,
                completion_all_zero=False,
            ),
            baseline_eligible_summary=SimpleNamespace(
                shanten_regret=0,
                ukeire_regret=0,
                second_step_regret=None,
                completion_regret=3,
                completion_all_zero=True,
            ),
        )
        recorder = _Phase1Recorder(seed=651, rotation=0, candidate_seat=Seat.SEAT_0)
        with patch.object(
            analysis_module,
            "analyze_mechanism_riichi_defense_offensive_efficiency",
            return_value=fake,
        ) as analyzer:
            recorder.record(decision, action)
        analyzer.assert_called_once_with(
            decision.input,
            (action, other),
            include_terminal_progression=False,
        )
        record = recorder.snapshot().records[0]
        self.assertIsNone(record.full_ukeire_regret)
        self.assertEqual(record.eligible_ukeire_regret, 0)
        self.assertEqual(record.full_completion_regret, 7)
        self.assertEqual(record.eligible_completion_regret, 3)
        self.assertFalse(record.full_completion_all_zero)
        self.assertTrue(record.eligible_completion_all_zero)


class Phase1AggregationTest(unittest.TestCase):
    def test_not_applicable_and_zero_remain_distinct_in_distribution(self) -> None:
        first = _record(seed=651, full_ukeire=None, eligible_ukeire=0)
        second = _record(seed=652, full_ukeire=4, eligible_ukeire=0)
        games = _game_diagnostics({(651, 0): (first,), (652, 0): (second,)})
        aggregate = aggregate_phase1(games)
        summaries = {
            (item.metric, item.universe): item for item in aggregate.metric_summaries
        }
        full = summaries[("R2_UKEIRE", "FULL_LEGAL")].distribution
        eligible = summaries[("R2_UKEIRE", "BASELINE_ELIGIBLE")].distribution
        self.assertEqual(full.applicable_count, 1)
        self.assertEqual(full.nonzero_count, 1)
        self.assertEqual(eligible.applicable_count, 2)
        self.assertEqual(eligible.nonzero_count, 0)

    def test_clusters_include_frequency_and_magnitude(self) -> None:
        record = _record(full_completion=9, eligible_completion=4)
        clusters = build_cluster_summaries((record,))
        target = next(
            item
            for item in clusters
            if item.metric == "R4_COMPLETION_MASS"
            and item.universe == "FULL_LEGAL"
            and item.dimension == "branch"
        )
        self.assertEqual(target.population_count, 1)
        self.assertEqual(target.population_share, 1.0)
        self.assertEqual(target.distribution.nonzero_incidence, 1.0)
        self.assertEqual(target.distribution.maximum, 9)


class Phase2SamplingTest(unittest.TestCase):
    def test_sample_is_deterministic_round_robin_and_bounded_to_64(self) -> None:
        records = []
        index = 0
        for round_index in range(10):
            for shanten in range(4):
                for open_hand in (False, True):
                    records.append(
                        _record(
                            seed=651 + index,
                            rotation=index % 4,
                            ordinal=round_index,
                            shanten=shanten,
                            open_hand=open_hand,
                            full_all_zero=True,
                            eligible_all_zero=True,
                        )
                    )
                    index += 1
        samples = select_phase2_samples(tuple(reversed(records)))
        self.assertEqual(len(samples), PHASE2_SAMPLE_LIMIT)
        self.assertEqual(
            len({sample.identity for sample in samples}), PHASE2_SAMPLE_LIMIT
        )
        self.assertEqual(
            tuple((sample.shanten_bucket, sample.open_hand) for sample in samples[:8]),
            (
                ("0", False),
                ("0", True),
                ("1", False),
                ("1", True),
                ("2", False),
                ("2", True),
                ("3+", False),
                ("3+", True),
            ),
        )
        self.assertEqual(samples, select_phase2_samples(tuple(reversed(records))))


class TrajectoryIdentityTest(unittest.TestCase):
    def test_mismatched_replay_is_rejected(self) -> None:
        historical = _phase1_result(focal_score=25000)
        replay = _phase1_result(focal_score=25001)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "parent.json"
            save_arm_artifact(historical.evaluation_result, path)
            from lisjong_arena.paired_evaluation import load_arm_artifact

            parent = load_arm_artifact(path)
            with self.assertRaisesRegex(
                OffensiveEfficiencyDiagnosticError, "TRAJECTORY IDENTITY FAILURE"
            ):
                require_trajectory_identity(replay, parent)


class ArtifactRoundTripTest(unittest.TestCase):
    def _write_valid(self, directory: Path):
        phase1 = _phase1_result(with_record=True)
        parent_path = directory / "parent.json"
        result_path = directory / "diagnostic.json"
        save_arm_artifact(phase1.evaluation_result, parent_path)
        samples = select_phase2_samples(phase1.records)
        self.assertEqual(len(samples), 1)
        phase2_records = (_phase2_record(samples[0]),)
        artifact = build_artifact(
            phase1=phase1,
            phase2_records=phase2_records,
            parent_artifact_path=parent_path,
            provenance=provenance(lisjong_revision=_LISJONG_256_REVISION),
        )
        save_artifact(artifact, result_path)
        return parent_path, result_path, artifact

    def test_strict_round_trip_rederives_aggregates_and_sample(self) -> None:
        with TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            parent_path, result_path, artifact = self._write_valid(directory)
            loaded = load_artifact(result_path, parent_artifact_path=parent_path)
        self.assertEqual(loaded.result_identity, artifact.result_identity)
        self.assertEqual(loaded.phase1_aggregate, artifact.phase1_aggregate)
        self.assertEqual(loaded.clusters, artifact.clusters)
        self.assertEqual(loaded.phase2_samples, artifact.phase2_samples)
        self.assertEqual(
            loaded.phase2_aggregate, aggregate_phase2(loaded.phase2_records)
        )

    def test_resealed_tampered_aggregate_is_rejected_by_rederivation(self) -> None:
        with TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            parent_path, result_path, _ = self._write_valid(directory)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            document["phase1"]["aggregate"]["choice_discard_decision_count"] += 1
            payload = {
                key: value
                for key, value in document.items()
                if key != "result_identity"
            }
            document["result_identity"] = document_identity(payload)
            result_path.write_text(canonical_json_text(document), encoding="utf-8")
            with self.assertRaises(OffensiveEfficiencyArtifactError):
                load_artifact(result_path, parent_artifact_path=parent_path)

    def test_resealed_wrong_parent_digest_is_rejected(self) -> None:
        with TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            parent_path, result_path, _ = self._write_valid(directory)
            document = json.loads(result_path.read_text(encoding="utf-8"))
            document["source"]["parent_artifact_digest"] = "0" * 64
            payload = {
                key: value
                for key, value in document.items()
                if key != "result_identity"
            }
            document["result_identity"] = document_identity(payload)
            result_path.write_text(canonical_json_text(document), encoding="utf-8")
            with self.assertRaises(OffensiveEfficiencyArtifactError):
                load_artifact(result_path, parent_artifact_path=parent_path)


if __name__ == "__main__":
    unittest.main()
