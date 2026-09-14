"""Issue #252 progression development evaluationのunit test。

実RiichiEnvを起動せず、locked seeds(647..650 / 651..750)のreal evaluationも
実行しない。executionは``execute``差し替えの合成fakeであり、raw game resultは
fixtureが決めた値である。real Phase A / Phase Bはmerge後のoperator作業である。
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _progression_development_fixtures import (
    ARENA_REVISION,
    arm_plan,
    constant_focal_score,
    evaluation_result,
    game_results,
    lock_destinations,
    locked_environment,
    provenance,
    recording_execute,
    save_arm_artifact,
    seed_offset_focal_score,
    write_lock,
)

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_offline_q.p1_gate_b_comparator import (
    PassiveTsumogiriPolicy,
)
from lisjong_arena.policy_catalog import POLICY_CATALOG
from lisjong_arena.progression_development import experiment, lock, paired
from lisjong_arena.progression_development import protocol as protocol_module
from lisjong_arena.progression_development.feasibility import (
    FeasibilityError,
    FeasibilityGate,
    WorkerMeasurement,
    build_feasibility_record,
    collect_machine_profile,
    evaluate_feasibility_gate,
    load_feasibility_record,
    parse_feasibility_record,
    project_phase_b_arm_wall_clock_hours,
    require_passed_gate,
    run_phase_a_feasibility,
    save_feasibility_record,
    select_fastest_valid_worker_count,
    summarize_per_game_elapsed,
)
from lisjong_arena.progression_development.lock import ProgressionLockError
from lisjong_arena.progression_development.paired import (
    PairedResultError,
    PairedSeedDelta,
    build_paired_result,
    classify_paired_summary,
    derive_paired_deltas,
    focal_seed_block_means,
    load_arm_artifact,
    load_paired_result,
    parse_paired_result,
    save_paired_result,
    summarize_paired_deltas,
    verify_paired_result,
)
from lisjong_arena.progression_development.protocol import (
    CANDIDATE_CLASS_NAME,
    CANDIDATE_IDENTITY,
    CANDIDATE_SOURCE_MODULE,
    COMPARATOR_IDENTITY,
    FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
    INCONCLUSIVE_LABEL,
    INFEASIBLE_LABEL,
    NEGATIVE_LABEL,
    PARENT_IDENTITY,
    PHASE_A_GAME_COUNT,
    PHASE_A_SEEDS,
    PHASE_B_GAMES_PER_ARM,
    PHASE_B_SEEDS,
    PHASE_B_TOTAL_GAMES,
    ROTATION_COUNT,
    SIGNAL_LABEL,
    ProgressionProtocolError,
    candidate_spec,
    comparator_spec,
    parent_spec,
    progression_diagnostics_availability,
    require_disjoint_populations,
    require_exact_candidate_semantics,
    require_exact_comparator,
    require_phase_a_population,
    require_phase_b_population,
    supported_worker_sweep,
)


def measurement(
    worker_count: int,
    *,
    seconds: float,
    failed: bool = False,
    matches: bool = True,
    games: int = PHASE_A_GAME_COUNT,
) -> WorkerMeasurement:
    """timingと正当性だけを指定したworker measurement。"""
    return WorkerMeasurement(
        worker_count=worker_count,
        wall_clock_seconds=seconds,
        games_completed=games,
        execution_failed=failed,
        failure_text="boom" if failed else None,
        raw_results_digest=None if failed else "0" * 64,
        matches_serial_raw_results=matches,
        games_per_hour=0.0 if seconds <= 0 else games * 3600.0 / seconds,
        speedup_vs_serial=None if failed else 1.0,
    )


def passing_measurements() -> tuple[WorkerMeasurement, ...]:
    """gateを通る決定的なsweep(projected wall-clockは8h未満)。"""
    return (
        measurement(1, seconds=400.0),
        measurement(4, seconds=120.0),
        measurement(8, seconds=90.0),
    )


class IdentityTests(unittest.TestCase):
    """P / C / Tのexact identityとbound semanticsを固定する。"""

    def test_candidate_resolves_to_the_exact_170_implementation(self):
        spec = candidate_spec()
        self.assertEqual(spec.identity, CANDIDATE_IDENTITY)
        policy = spec.factory()
        self.assertEqual(type(policy).__name__, CANDIDATE_CLASS_NAME)
        self.assertEqual(type(policy).__module__, CANDIDATE_SOURCE_MODULE)

    def test_parent_resolves_to_the_curated_baseline(self):
        spec = parent_spec()
        self.assertEqual(spec.identity, PARENT_IDENTITY)
        self.assertIs(spec, POLICY_CATALOG[PARENT_IDENTITY])

    def test_comparator_reuses_the_existing_passive_tsumogiri_implementation(self):
        spec = comparator_spec()
        self.assertEqual(spec.identity, COMPARATOR_IDENTITY)
        self.assertIs(type(spec.factory()), PassiveTsumogiriPolicy)
        binding = require_exact_comparator()
        self.assertEqual(binding["identity"], COMPARATOR_IDENTITY)
        self.assertIn("PassiveTsumogiriPolicy", binding["reused_implementation"])

    def test_candidate_binding_records_adaptive_generation(self):
        binding = require_exact_candidate_semantics()
        self.assertEqual(binding.identity, CANDIDATE_IDENTITY)
        self.assertEqual(binding.class_name, CANDIDATE_CLASS_NAME)
        self.assertEqual(
            binding.parent_class_name, "MechanismRiichiDefenseYakuhaiCallPolicy"
        )
        self.assertEqual(binding.horizon, 3)
        self.assertEqual(binding.adaptive_analysis_type, "ProgressionDecisionAnalysis")

    def test_bound_revision_has_no_clairvoyant_semantics(self):
        binding = require_exact_candidate_semantics()
        self.assertFalse(binding.clairvoyant_semantics_present)
        self.assertEqual(protocol_module._forbidden_approximation_modules(), ())

    def test_candidate_is_an_exact_generation_of_its_parent(self):
        candidate = type(candidate_spec().factory())
        parent = type(parent_spec().factory())
        # parent-preserving behaviour自体はlisjongが所有する。Arenaはgeneration
        # identityだけを確認し、同じbehaviour testを複製しない。
        self.assertTrue(issubclass(candidate, parent))
        self.assertIsNot(candidate, parent)

    def test_candidate_is_not_registered_in_the_curated_catalog(self):
        self.assertNotIn(CANDIDATE_IDENTITY, POLICY_CATALOG)

    def test_progression_diagnostics_are_recorded_as_unavailable(self):
        availability = progression_diagnostics_availability()
        self.assertFalse(availability["available"])
        self.assertIsNone(availability["activation_rate"])
        self.assertIn("analysis=None", str(availability["reason"]))

    def test_diagnostics_never_execute_the_progression_dp_a_second_time(self):
        """diagnosticsのためにPolicyを生成も実行もしない。

        bound candidateは``PolicyDecision.analysis=None``を返すため、typed
        analysisはstable seamから観測できない。Arenaはそこを埋めるために
        DPを再実行せず、availability documentだけを残す。
        """
        with mock.patch.object(
            protocol_module,
            "create_terminal_shanten_progression",
            side_effect=AssertionError("diagnostics must not run the policy"),
        ) as factory:
            first = progression_diagnostics_availability()
            second = progression_diagnostics_availability()
        factory.assert_not_called()
        self.assertEqual(first, second)

    def test_unavailable_diagnostics_keep_the_selected_action_untouched(self):
        """diagnosticsがunavailableでも、選択されたactionは何も変わらない。

        production selectionはparentと同じ``_decide_discard()``経路のままで
        あり、Arenaはdecision pathへ介入しない。
        """
        candidate = type(candidate_spec().factory())
        self.assertIs(
            candidate._decide_discard,
            candidate.__dict__["_decide_discard"],
        )
        self.assertNotIn("choose_action", candidate.__dict__)


class PopulationTests(unittest.TestCase):
    """locked seed populationとgame数を固定する。"""

    def test_phase_a_population_is_exact(self):
        self.assertEqual(PHASE_A_SEEDS, (647, 648, 649, 650))
        self.assertEqual(PHASE_A_GAME_COUNT, 16)
        self.assertEqual(require_phase_a_population(PHASE_A_SEEDS), PHASE_A_SEEDS)

    def test_phase_b_population_is_exact(self):
        self.assertEqual(len(PHASE_B_SEEDS), 100)
        self.assertEqual(PHASE_B_SEEDS[0], 651)
        self.assertEqual(PHASE_B_SEEDS[-1], 750)
        self.assertEqual(PHASE_B_GAMES_PER_ARM, 400)
        self.assertEqual(PHASE_B_TOTAL_GAMES, 800)
        self.assertEqual(require_phase_b_population(PHASE_B_SEEDS), PHASE_B_SEEDS)

    def test_populations_are_disjoint(self):
        require_disjoint_populations()
        self.assertFalse(set(PHASE_A_SEEDS) & set(PHASE_B_SEEDS))

    def test_technical_seeds_are_rejected_as_development_population(self):
        with self.assertRaises(ProgressionProtocolError):
            require_phase_b_population(PHASE_A_SEEDS)

    def test_extended_or_reordered_population_is_rejected(self):
        with self.assertRaises(ProgressionProtocolError):
            require_phase_b_population((*PHASE_B_SEEDS, 751))
        with self.assertRaises(ProgressionProtocolError):
            require_phase_b_population(tuple(reversed(PHASE_B_SEEDS)))
        with self.assertRaises(ProgressionProtocolError):
            require_phase_a_population(PHASE_A_SEEDS[:-1])


class WorkerSweepTests(unittest.TestCase):
    """worker sweepはmachineが出せる範囲だけを使う。"""

    def test_full_sweep_when_sixteen_logical_cpus_exist(self):
        self.assertEqual(supported_worker_sweep(16), (1, 4, 8, 16))
        self.assertEqual(supported_worker_sweep(32), (1, 4, 8, 16))

    def test_sixteen_is_replaced_with_the_highest_supported_count(self):
        self.assertEqual(supported_worker_sweep(8), (1, 4, 8))
        self.assertEqual(supported_worker_sweep(12), (1, 4, 8, 12))
        self.assertEqual(supported_worker_sweep(2), (1, 2))
        self.assertEqual(supported_worker_sweep(1), (1,))

    def test_invalid_cpu_count_is_rejected(self):
        with self.assertRaises(ProgressionProtocolError):
            supported_worker_sweep(0)
        with self.assertRaises(ProgressionProtocolError):
            supported_worker_sweep(True)


class WorkerSelectionTests(unittest.TestCase):
    """worker選択はtiming / correctnessだけに依存する。"""

    def test_selects_the_fastest_valid_setting(self):
        self.assertEqual(select_fastest_valid_worker_count(passing_measurements()), 8)

    def test_failed_setting_is_never_selected(self):
        measurements = (
            measurement(1, seconds=400.0),
            measurement(8, seconds=1.0, failed=True),
        )
        self.assertEqual(select_fastest_valid_worker_count(measurements), 1)

    def test_divergent_setting_is_never_selected(self):
        measurements = (
            measurement(1, seconds=400.0),
            measurement(8, seconds=1.0, matches=False),
        )
        self.assertEqual(select_fastest_valid_worker_count(measurements), 1)

    def test_incomplete_setting_is_never_selected(self):
        measurements = (
            measurement(1, seconds=400.0),
            measurement(8, seconds=1.0, games=PHASE_A_GAME_COUNT - 1),
        )
        self.assertEqual(select_fastest_valid_worker_count(measurements), 1)

    def test_tie_breaks_on_the_smaller_worker_count(self):
        measurements = (
            measurement(4, seconds=100.0),
            measurement(8, seconds=100.0),
        )
        self.assertEqual(select_fastest_valid_worker_count(measurements), 4)

    def test_no_valid_setting_returns_none(self):
        measurements = (measurement(1, seconds=10.0, failed=True),)
        self.assertIsNone(select_fastest_valid_worker_count(measurements))

    def test_measurement_carries_no_game_outcome(self):
        fields = set(measurement(1, seconds=1.0).to_document())
        self.assertFalse(
            fields & {"scores", "candidate_score", "seed", "game_results", "delta"}
        )


class FeasibilityGateTests(unittest.TestCase):
    """事前登録したtechnical gateの判定を固定する。"""

    def test_gate_passes_on_a_clean_sweep(self):
        gate = evaluate_feasibility_gate(passing_measurements())
        self.assertTrue(gate.gate_passed)
        self.assertEqual(gate.failure_reasons, ())
        self.assertEqual(gate.selected_worker_count, 8)
        self.assertIsNone(gate.label)
        self.assertLess(
            gate.projected_phase_b_arm_wall_clock_hours,
            FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
        )
        self.assertAlmostEqual(
            gate.projected_phase_b_arm_cpu_hours,
            gate.projected_phase_b_arm_wall_clock_hours * 8,
        )

    def test_execution_failure_fails_the_gate(self):
        gate = evaluate_feasibility_gate(
            (measurement(1, seconds=10.0), measurement(8, seconds=5.0, failed=True))
        )
        self.assertFalse(gate.gate_passed)
        self.assertEqual(gate.label, INFEASIBLE_LABEL)
        self.assertIsNone(gate.selected_worker_count)
        self.assertIn(
            "at least one worker setting failed to execute", gate.failure_reasons
        )

    def test_divergent_raw_outcomes_fail_the_gate(self):
        gate = evaluate_feasibility_gate(
            (measurement(1, seconds=10.0), measurement(8, seconds=5.0, matches=False))
        )
        self.assertFalse(gate.gate_passed)
        self.assertIn(
            "parallel raw outcomes differ from serial raw outcomes",
            gate.failure_reasons,
        )

    def test_incomplete_population_fails_the_gate(self):
        gate = evaluate_feasibility_gate(
            (measurement(1, seconds=10.0, games=PHASE_A_GAME_COUNT - 1),)
        )
        self.assertFalse(gate.gate_passed)
        self.assertIn(
            f"a worker setting did not complete {PHASE_A_GAME_COUNT} technical games",
            gate.failure_reasons,
        )

    def test_projection_above_the_bound_fails_the_gate(self):
        # 16 gamesで2時間 -> 400 gamesで50時間。
        slow = measurement(1, seconds=2.0 * 3600.0)
        self.assertGreater(
            project_phase_b_arm_wall_clock_hours(slow),
            FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
        )
        gate = evaluate_feasibility_gate((slow,))
        self.assertFalse(gate.gate_passed)
        self.assertEqual(gate.label, INFEASIBLE_LABEL)
        self.assertIsNone(gate.selected_worker_count)
        self.assertTrue(
            any(
                "exceeds the pre-registered" in reason
                for reason in gate.failure_reasons
            )
        )

    def test_empty_sweep_fails_the_gate(self):
        gate = evaluate_feasibility_gate(())
        self.assertFalse(gate.gate_passed)
        self.assertIn("no worker measurement was recorded", gate.failure_reasons)


class PhaseAExecutionTests(unittest.TestCase):
    """Phase Aのsweep実行を合成executionで固定する。"""

    def _run(self, execute, *, logical_cpu_count: int = 8):
        counter = iter(float(value) for value in range(0, 100_000))
        with TemporaryDirectory() as directory:
            base = Path(directory)
            destinations = lock_destinations(base)
            lock_path = base / "lock.json"
            write_lock(lock_path, destinations)
            with locked_environment():
                return run_phase_a_feasibility(
                    lock_path=lock_path,
                    destination=destinations["feasibility_record"],
                    logical_cpu_count=logical_cpu_count,
                    execute=execute,
                    clock=lambda: next(counter),
                )

    def test_sweep_runs_the_locked_population_at_each_worker_setting(self):
        execute = recording_execute(constant_focal_score(30_000))
        record = self._run(execute)
        self.assertEqual(execute.calls, [1, 4, 8])
        self.assertEqual(record.ordered_seeds, PHASE_A_SEEDS)
        self.assertEqual(len(record.measurements), 3)
        for item in record.measurements:
            self.assertEqual(item.games_completed, PHASE_A_GAME_COUNT)

    def test_parallel_raw_outcomes_match_serial(self):
        record = self._run(recording_execute(constant_focal_score(30_000)))
        self.assertTrue(
            all(item.matches_serial_raw_results for item in record.measurements)
        )
        digests = {item.raw_results_digest for item in record.measurements}
        self.assertEqual(len(digests), 1)
        self.assertTrue(record.gate.gate_passed)

    def test_divergent_worker_result_fails_the_gate(self):
        execute = recording_execute(
            constant_focal_score(30_000), divergent_workers=frozenset({8})
        )
        record = self._run(execute)
        self.assertFalse(record.gate.gate_passed)
        self.assertIn(
            "parallel raw outcomes differ from serial raw outcomes",
            record.gate.failure_reasons,
        )

    def test_worker_failure_yields_no_valid_measurement_and_no_fallback(self):
        execute = recording_execute(
            constant_focal_score(30_000), failing_workers=frozenset({4})
        )
        record = self._run(execute)
        failed = [item for item in record.measurements if item.worker_count == 4]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].execution_failed)
        self.assertEqual(failed[0].games_completed, 0)
        self.assertFalse(failed[0].valid)
        self.assertFalse(record.gate.gate_passed)
        self.assertIsNone(record.gate.selected_worker_count)

    def test_per_game_elapsed_is_summarised_from_the_serial_run(self):
        record = self._run(recording_execute(constant_focal_score(30_000)))
        self.assertIsNotNone(record.per_game_elapsed)
        self.assertEqual(record.per_game_elapsed.game_count, PHASE_A_GAME_COUNT)

    def test_record_binds_machine_and_diagnostics(self):
        record = self._run(recording_execute(constant_focal_score(30_000)))
        self.assertEqual(record.machine.logical_cpu_count, 8)
        self.assertFalse(record.progression_diagnostics["available"])
        self.assertEqual(record.parent_identity, PARENT_IDENTITY)

    def test_record_is_written_to_the_locked_destination(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            destinations = lock_destinations(base)
            lock_path = base / "lock.json"
            write_lock(lock_path, destinations)
            counter = iter(float(value) for value in range(0, 100_000))
            with locked_environment():
                record = run_phase_a_feasibility(
                    lock_path=lock_path,
                    destination=destinations["feasibility_record"],
                    logical_cpu_count=8,
                    execute=recording_execute(constant_focal_score(30_000)),
                    clock=lambda: next(counter),
                )
            self.assertTrue(destinations["feasibility_record"].exists())
            reloaded = load_feasibility_record(destinations["feasibility_record"])
        self.assertEqual(reloaded.record_identity, record.record_identity)


class PhaseALockGateTests(unittest.TestCase):
    """Phase Aはlockをconsumeして初めてreal executionへ進める。"""

    def _attempt(
        self,
        base: Path,
        *,
        lock_path: Path | None = None,
        destination: Path | None = None,
        live_head: str = ARENA_REVISION,
        live_provenance=None,
    ):
        """runner呼び出し回数を観測しながらPhase Aを試行する。"""
        destinations = lock_destinations(base)
        actual_lock = base / "lock.json" if lock_path is None else lock_path
        if not actual_lock.exists() and lock_path is None:
            write_lock(actual_lock, destinations)
        execute = recording_execute(constant_focal_score(30_000))
        counter = iter(float(value) for value in range(0, 100_000))
        with locked_environment(head=live_head, execution_provenance=live_provenance):
            with self.assertRaises(Exception) as raised:
                run_phase_a_feasibility(
                    lock_path=actual_lock,
                    destination=(
                        destinations["feasibility_record"]
                        if destination is None
                        else destination
                    ),
                    logical_cpu_count=8,
                    execute=execute,
                    clock=lambda: next(counter),
                )
        return execute, raised.exception

    def test_a_missing_lock_runs_no_game(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            execute, _ = self._attempt(base, lock_path=base / "absent.json")
        self.assertEqual(execute.calls, [])

    def test_an_unlocked_destination_runs_no_game(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            execute, error = self._attempt(base, destination=base / "elsewhere.json")
        self.assertEqual(execute.calls, [])
        self.assertIsInstance(error, ProgressionLockError)

    def test_a_different_live_execution_target_runs_no_game(self):
        """未mergeのPR branch(HEADがlocked revisionと異なる)では開始できない。"""
        with TemporaryDirectory() as directory:
            base = Path(directory)
            execute, error = self._attempt(base, live_head="f" * 40)
        self.assertEqual(execute.calls, [])
        self.assertIsInstance(error, ProgressionLockError)
        self.assertIn("locked execution target", str(error))

    def test_drifted_live_provenance_runs_no_game(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            execute, error = self._attempt(
                base, live_provenance=provenance(lisjong_revision="9" * 40)
            )
        self.assertEqual(execute.calls, [])
        self.assertIsInstance(error, ProgressionLockError)

    def test_an_existing_record_destination_runs_no_game(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            destinations = lock_destinations(base)
            write_lock(base / "lock.json", destinations)
            destinations["feasibility_record"].write_text("{}", encoding="utf-8")
            execute, _ = self._attempt(base)
        self.assertEqual(execute.calls, [])

    def test_a_tampered_lock_runs_no_game(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            destinations = lock_destinations(base)
            document = write_lock(base / "lock.json", destinations)
            document["phase_b"]["ordered_seeds"] = list(range(1, 101))
            payload = {
                name: item for name, item in document.items() if name != "lock_identity"
            }
            document["lock_identity"] = protocol_module.document_identity(payload)
            tampered = base / "tampered-lock.json"
            tampered.write_text(canonical_json_text(document), encoding="utf-8")
            execute, error = self._attempt(base, lock_path=tampered)
        self.assertEqual(execute.calls, [])
        self.assertIsInstance(error, ProgressionLockError)


class PerGameElapsedTests(unittest.TestCase):
    def test_percentiles_use_nearest_rank(self):
        summary = summarize_per_game_elapsed([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(summary.game_count, 4)
        self.assertEqual(summary.min_seconds, 1.0)
        self.assertEqual(summary.max_seconds, 4.0)
        self.assertEqual(summary.p50_seconds, 2.0)
        self.assertEqual(summary.p95_seconds, 4.0)
        self.assertEqual(summary.mean_seconds, 2.5)

    def test_empty_elapsed_is_rejected(self):
        with self.assertRaises(FeasibilityError):
            summarize_per_game_elapsed([])


class FeasibilityRecordPersistenceTests(unittest.TestCase):
    """feasibility recordのwrite-once / strict readbackを固定する。"""

    def _record(self, measurements=None):
        items = passing_measurements() if measurements is None else measurements
        return build_feasibility_record(
            ordered_seeds=PHASE_A_SEEDS,
            provenance=provenance(),
            machine=collect_machine_profile(8),
            measurements=items,
            per_game_elapsed=summarize_per_game_elapsed([1.0] * PHASE_A_GAME_COUNT),
            gate=evaluate_feasibility_gate(items),
        )

    def test_round_trip_preserves_identity(self):
        record = self._record()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "feasibility.json"
            save_feasibility_record(record, path)
            reloaded = load_feasibility_record(path)
        self.assertEqual(reloaded.record_identity, record.record_identity)
        self.assertEqual(reloaded.gate.selected_worker_count, 8)
        self.assertEqual(reloaded.ordered_seeds, PHASE_A_SEEDS)

    def test_existing_destination_is_never_overwritten(self):
        record = self._record()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "feasibility.json"
            save_feasibility_record(record, path)
            with self.assertRaises(Exception):
                save_feasibility_record(record, path)

    def test_tampered_record_is_rejected(self):
        record = self._record()
        document = record.to_document()
        document["gate"]["gate_passed"] = True
        document["gate"]["selected_worker_count"] = 64
        with self.assertRaises(FeasibilityError):
            parse_feasibility_record(document)

    def test_wrong_seed_population_is_rejected(self):
        record = self._record()
        document = record.to_document()
        document["ordered_seeds"] = [1, 2, 3, 4]
        with self.assertRaises(FeasibilityError):
            parse_feasibility_record(document)

    def test_unsupported_version_is_rejected(self):
        document = self._record().to_document()
        document["record_version"] = 2
        with self.assertRaises(FeasibilityError):
            parse_feasibility_record(document)

    def test_bool_is_not_accepted_where_an_int_is_required(self):
        document = self._record().to_document()
        document["measurements"][0]["worker_count"] = True
        with self.assertRaises(Exception):
            parse_feasibility_record(document)

    def test_int_is_not_accepted_where_a_float_is_required(self):
        document = self._record().to_document()
        document["measurements"][0]["wall_clock_seconds"] = 400
        with self.assertRaises(Exception):
            parse_feasibility_record(document)

    def test_missing_key_is_rejected(self):
        document = self._record().to_document()
        del document["machine"]
        with self.assertRaises(Exception):
            parse_feasibility_record(document)

    def _resealed(self, document: dict) -> dict:
        """documentを書き換えたうえでrecord identityを再計算する。"""
        payload = {
            name: item for name, item in document.items() if name != "record_identity"
        }
        document["record_identity"] = protocol_module.document_identity(payload)
        return document

    def test_a_resealed_passing_gate_is_rejected(self):
        """stored gate_passedをauthorityにしない。

        計測がgateを通っていないrecordのgateだけを書き換え、identityまで
        再計算しても、measurementsから再導出したgateと違えば拒否する。
        """
        measurements = slow_measurements()
        record = self._record(measurements)
        self.assertFalse(record.gate.gate_passed)
        document = record.to_document()
        document["gate"]["gate_passed"] = True
        document["gate"]["label"] = None
        document["gate"]["failure_reasons"] = []
        document["gate"]["selected_worker_count"] = 8
        document["gate"]["projected_phase_b_arm_wall_clock_hours"] = 1.0
        document["gate"]["projected_phase_b_arm_cpu_hours"] = 8.0
        with self.assertRaises(FeasibilityError) as raised:
            parse_feasibility_record(self._resealed(document))
        self.assertIn("re-derived", str(raised.exception))

    def test_a_resealed_faster_worker_choice_is_rejected(self):
        document = self._record().to_document()
        document["gate"]["selected_worker_count"] = 4
        with self.assertRaises(FeasibilityError):
            parse_feasibility_record(self._resealed(document))

    def test_measurements_must_match_the_recorded_machine_sweep(self):
        document = self._record().to_document()
        document["measurements"] = document["measurements"][:2]
        with self.assertRaises(FeasibilityError) as raised:
            parse_feasibility_record(self._resealed(document))
        self.assertIn("locked sweep", str(raised.exception))

    def test_a_resealed_wrong_parent_identity_is_rejected(self):
        document = self._record().to_document()
        document["parent_identity"] = "two-step"
        with self.assertRaises(FeasibilityError):
            parse_feasibility_record(self._resealed(document))

    def test_a_resealed_candidate_binding_is_rejected(self):
        document = self._record().to_document()
        document["candidate_binding"]["clairvoyant_semantics_present"] = True
        with self.assertRaises(FeasibilityError):
            parse_feasibility_record(self._resealed(document))

    def test_a_resealed_diagnostics_claim_is_rejected(self):
        """unavailableな診断を「available」と書き換えたrecordを受理しない。"""
        document = self._record().to_document()
        document["progression_diagnostics"]["available"] = True
        document["progression_diagnostics"]["activation_rate"] = 0.82
        with self.assertRaises(FeasibilityError):
            parse_feasibility_record(self._resealed(document))

    def test_a_resealed_wall_clock_bound_is_rejected(self):
        document = self._record().to_document()
        document["gate"]["wall_clock_limit_hours"] = 100.0
        with self.assertRaises(FeasibilityError):
            parse_feasibility_record(self._resealed(document))


class PassedGateTests(unittest.TestCase):
    def test_failed_gate_blocks_phase_b(self):
        record = build_feasibility_record(
            ordered_seeds=PHASE_A_SEEDS,
            provenance=provenance(),
            machine=collect_machine_profile(8),
            measurements=(measurement(1, seconds=2.0 * 3600.0),),
            per_game_elapsed=None,
            gate=evaluate_feasibility_gate((measurement(1, seconds=2.0 * 3600.0),)),
        )
        with self.assertRaises(FeasibilityError) as raised:
            require_passed_gate(record)
        self.assertIn(INFEASIBLE_LABEL, str(raised.exception))

    def test_passed_gate_returns_the_selected_worker_count(self):
        record = build_feasibility_record(
            ordered_seeds=PHASE_A_SEEDS,
            provenance=provenance(),
            machine=collect_machine_profile(8),
            measurements=passing_measurements(),
            per_game_elapsed=None,
            gate=evaluate_feasibility_gate(passing_measurements()),
        )
        self.assertEqual(require_passed_gate(record), 8)

    def test_a_gate_without_a_worker_count_is_rejected(self):
        record = build_feasibility_record(
            ordered_seeds=PHASE_A_SEEDS,
            provenance=provenance(),
            machine=collect_machine_profile(8),
            measurements=passing_measurements(),
            per_game_elapsed=None,
            gate=FeasibilityGate(
                gate_passed=True,
                label=None,
                failure_reasons=(),
                selected_worker_count=None,
                projected_phase_b_arm_wall_clock_hours=1.0,
                projected_phase_b_arm_cpu_hours=1.0,
                wall_clock_limit_hours=FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
            ),
        )
        with self.assertRaises(FeasibilityError):
            require_passed_gate(record)


class PairedDerivationTests(unittest.TestCase):
    """paired primary statisticをraw evidenceから再導出する。"""

    def test_focal_means_use_the_four_rotations_of_each_seed(self):
        results = game_results((651, 652), constant_focal_score(32_000))
        blocks = focal_seed_block_means(results)
        self.assertEqual(blocks, ((651, 32_000.0), (652, 32_000.0)))

    def test_focal_means_average_over_rotations(self):
        def focal(seed: int, rotation: int) -> int:
            del seed
            return 30_000 + 1_000 * rotation

        blocks = focal_seed_block_means(game_results((651,), focal))
        self.assertEqual(blocks, ((651, 31_500.0),))

    def test_partial_seed_block_is_rejected(self):
        results = game_results((651,), constant_focal_score(30_000))[:3]
        with self.assertRaises(PairedResultError):
            focal_seed_block_means(results)

    def test_paired_deltas_are_exact(self):
        deltas = _paired_deltas_from_offsets(lambda seed: seed - 651)
        self.assertEqual(len(deltas), 100)
        self.assertEqual(deltas[0].seed, 651)
        self.assertEqual(deltas[0].delta, 0.0)
        self.assertEqual(deltas[99].delta, 99.0)
        for item in deltas:
            self.assertEqual(item.delta, item.candidate_mean - item.parent_mean)

    def test_summary_matches_the_paired_deltas(self):
        deltas = tuple(
            PairedSeedDelta(seed=seed, candidate_mean=1.0, parent_mean=0.0, delta=1.0)
            for seed in PHASE_B_SEEDS
        )
        summary = summarize_paired_deltas(deltas)
        self.assertEqual(summary.block_count, 100)
        self.assertEqual(summary.mean_delta, 1.0)
        self.assertEqual(summary.sample_standard_deviation, 0.0)
        self.assertEqual(summary.standard_error, 0.0)
        self.assertEqual(summary.interval_lower, 1.0)
        self.assertEqual(summary.interval_upper, 1.0)

    def test_single_block_cannot_form_an_interval(self):
        with self.assertRaises(PairedResultError):
            summarize_paired_deltas(
                (
                    PairedSeedDelta(
                        seed=651, candidate_mean=1.0, parent_mean=0.0, delta=1.0
                    ),
                )
            )


def _paired_deltas_from_offsets(offset):
    """candidate armだけにseed依存offsetを与えたpaired deltasを作る。"""
    candidate = evaluation_result(
        arm_plan(candidate_spec(), PHASE_B_SEEDS),
        seed_offset_focal_score(30_000, offset),
    )
    parent = evaluation_result(
        arm_plan(parent_spec(), PHASE_B_SEEDS), constant_focal_score(30_000)
    )
    with TemporaryDirectory() as directory:
        candidate_path = Path(directory) / "candidate.json"
        parent_path = Path(directory) / "parent.json"
        save_arm_artifact(candidate, candidate_path)
        save_arm_artifact(parent, parent_path)
        return derive_paired_deltas(
            load_arm_artifact(candidate_path), load_arm_artifact(parent_path)
        )


class ClassificationTests(unittest.TestCase):
    """事前登録したclassification boundaryを固定する。"""

    def _summary(self, lower: float, upper: float):
        mean = (lower + upper) / 2
        standard_error = (upper - mean) / paired.INTERVAL_Z
        return paired.PairedSummary(
            block_count=100,
            mean_delta=mean,
            sample_standard_deviation=standard_error * 10.0,
            standard_error=standard_error,
            interval_lower=lower,
            interval_upper=upper,
        )

    def test_strictly_positive_lower_bound_is_signal(self):
        classification = classify_paired_summary(self._summary(0.5, 2.0))
        self.assertEqual(classification["kind"], "SIGNAL")
        self.assertEqual(classification["label"], SIGNAL_LABEL)

    def test_strictly_negative_upper_bound_is_negative(self):
        classification = classify_paired_summary(self._summary(-2.0, -0.5))
        self.assertEqual(classification["kind"], "NEGATIVE")
        self.assertEqual(classification["label"], NEGATIVE_LABEL)

    def test_interval_containing_zero_is_inconclusive(self):
        classification = classify_paired_summary(self._summary(-1.0, 1.0))
        self.assertEqual(classification["kind"], "INCONCLUSIVE")
        self.assertEqual(classification["label"], INCONCLUSIVE_LABEL)

    def test_zero_bounds_are_inconclusive(self):
        self.assertEqual(
            classify_paired_summary(self._summary(0.0, 2.0))["kind"], "INCONCLUSIVE"
        )
        self.assertEqual(
            classify_paired_summary(self._summary(-2.0, 0.0))["kind"], "INCONCLUSIVE"
        )

    def test_classification_only_receives_the_primary_summary(self):
        import inspect

        signature = inspect.signature(classify_paired_summary)
        self.assertEqual(list(signature.parameters), ["summary"])

    def test_non_finite_interval_is_rejected(self):
        with self.assertRaises(PairedResultError):
            classify_paired_summary(
                paired.PairedSummary(
                    block_count=100,
                    mean_delta=float("nan"),
                    sample_standard_deviation=1.0,
                    standard_error=1.0,
                    interval_lower=float("nan"),
                    interval_upper=float("nan"),
                )
            )


class PairedArmValidationTests(unittest.TestCase):
    """arm artifactのidentity / 件数 / seed orderをfail closedする。"""

    def _artifacts(self, directory: Path, *, candidate=None, parent=None, seeds=None):
        seeds = PHASE_B_SEEDS if seeds is None else seeds
        candidate_result = evaluation_result(
            arm_plan(candidate or candidate_spec(), seeds),
            constant_focal_score(31_000),
        )
        parent_result = evaluation_result(
            arm_plan(parent or parent_spec(), seeds), constant_focal_score(30_000)
        )
        candidate_path = directory / "candidate.json"
        parent_path = directory / "parent.json"
        save_arm_artifact(candidate_result, candidate_path)
        save_arm_artifact(parent_result, parent_path)
        return (
            load_arm_artifact(candidate_path),
            load_arm_artifact(parent_path),
            candidate_path,
            parent_path,
        )

    def test_locked_arms_derive_one_hundred_paired_units(self):
        with TemporaryDirectory() as directory:
            candidate, parent, _, _ = self._artifacts(Path(directory))
            deltas = derive_paired_deltas(candidate, parent)
        self.assertEqual(len(deltas), len(PHASE_B_SEEDS))
        self.assertEqual(tuple(item.seed for item in deltas), PHASE_B_SEEDS)
        self.assertTrue(all(item.delta == 1_000.0 for item in deltas))

    def test_each_arm_contains_four_hundred_games(self):
        with TemporaryDirectory() as directory:
            candidate, parent, _, _ = self._artifacts(Path(directory))
        self.assertEqual(len(candidate.game_results), PHASE_B_GAMES_PER_ARM)
        self.assertEqual(len(parent.game_results), PHASE_B_GAMES_PER_ARM)
        self.assertEqual(
            len(candidate.game_results) + len(parent.game_results),
            PHASE_B_TOTAL_GAMES,
        )

    def test_wrong_candidate_identity_is_rejected(self):
        with TemporaryDirectory() as directory:
            candidate, parent, _, _ = self._artifacts(
                Path(directory), candidate=POLICY_CATALOG["yakuhai-call"]
            )
            with self.assertRaises(PairedResultError):
                derive_paired_deltas(candidate, parent)

    def test_wrong_parent_identity_is_rejected(self):
        with TemporaryDirectory() as directory:
            candidate, parent, _, _ = self._artifacts(
                Path(directory), parent=POLICY_CATALOG["two-step"]
            )
            with self.assertRaises(PairedResultError):
                derive_paired_deltas(candidate, parent)

    def test_technical_seeds_are_rejected_as_development_evidence(self):
        with TemporaryDirectory() as directory:
            candidate, parent, _, _ = self._artifacts(
                Path(directory), seeds=PHASE_A_SEEDS
            )
            with self.assertRaises(PairedResultError):
                derive_paired_deltas(candidate, parent)

    def test_partial_arm_is_rejected(self):
        with TemporaryDirectory() as directory:
            candidate, parent, _, _ = self._artifacts(
                Path(directory), seeds=PHASE_B_SEEDS[:-1]
            )
            with self.assertRaises(PairedResultError):
                derive_paired_deltas(candidate, parent)

    def test_a_wrong_comparator_is_rejected(self):
        from lisjong_arena.model import SingleRoundEvaluationPlan

        plan = SingleRoundEvaluationPlan(
            candidate=candidate_spec(),
            baseline=POLICY_CATALOG["two-step"],
            seeds=PHASE_B_SEEDS,
            max_steps=10_000,
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            save_arm_artifact(
                evaluation_result(plan, constant_focal_score(31_000)), path
            )
            candidate = load_arm_artifact(path)
            parent_path = Path(directory) / "parent.json"
            save_arm_artifact(
                evaluation_result(
                    arm_plan(parent_spec(), PHASE_B_SEEDS),
                    constant_focal_score(30_000),
                ),
                parent_path,
            )
            parent = load_arm_artifact(parent_path)
            with self.assertRaises(PairedResultError):
                derive_paired_deltas(candidate, parent)


class PairedResultDocumentTests(unittest.TestCase):
    """paired resultのbinding / write-once / strict readbackを固定する。"""

    def _build(self, directory: Path):
        candidate_result = evaluation_result(
            arm_plan(candidate_spec(), PHASE_B_SEEDS), constant_focal_score(31_000)
        )
        parent_result = evaluation_result(
            arm_plan(parent_spec(), PHASE_B_SEEDS), constant_focal_score(30_000)
        )
        candidate_path = directory / "candidate.json"
        parent_path = directory / "parent.json"
        save_arm_artifact(candidate_result, candidate_path)
        save_arm_artifact(parent_result, parent_path)
        document = build_paired_result(
            candidate_artifact=load_arm_artifact(candidate_path),
            candidate_artifact_path=candidate_path,
            parent_artifact=load_arm_artifact(parent_path),
            parent_artifact_path=parent_path,
            worker_count=8,
        )
        return document, candidate_path, parent_path

    def test_document_binds_identities_revisions_and_evidence(self):
        with TemporaryDirectory() as directory:
            document, _, _ = self._build(Path(directory))
        self.assertEqual(document["candidate_binding"]["identity"], CANDIDATE_IDENTITY)
        self.assertEqual(
            document["comparator_binding"]["identity"], COMPARATOR_IDENTITY
        )
        self.assertEqual(document["worker_count"], 8)
        self.assertEqual(len(document["paired_deltas"]), 100)
        self.assertEqual(document["arms"]["candidate"]["game_count"], 400)
        self.assertEqual(document["arms"]["parent"]["game_count"], 400)
        self.assertIn("lisjong_revision", document["provenance"])
        self.assertEqual(document["classification"]["label"], SIGNAL_LABEL)
        self.assertTrue(document["arms"]["candidate"]["artifact_digest"])

    def test_secondary_diagnostics_do_not_change_the_classification(self):
        with TemporaryDirectory() as directory:
            document, _, _ = self._build(Path(directory))
            classification = dict(document["classification"])
            document["arms"]["candidate"]["diagnostics"]["win_rate"] = 0.99
            document["arms"]["parent"]["diagnostics"]["win_rate"] = 0.01
            summary = paired.PairedSummary(
                block_count=document["primary_summary"]["block_count"],
                mean_delta=document["primary_summary"]["mean_delta"],
                sample_standard_deviation=document["primary_summary"][
                    "sample_standard_deviation"
                ],
                standard_error=document["primary_summary"]["standard_error"],
                interval_lower=document["primary_summary"]["interval_lower"],
                interval_upper=document["primary_summary"]["interval_upper"],
            )
        self.assertEqual(classify_paired_summary(summary), classification)

    def test_round_trip_preserves_identity(self):
        with TemporaryDirectory() as directory:
            document, _, _ = self._build(Path(directory))
            path = Path(directory) / "paired.json"
            save_paired_result(document, path)
            reloaded = load_paired_result(path)
        self.assertEqual(reloaded["result_identity"], document["result_identity"])

    def test_unavailable_diagnostics_do_not_invalidate_the_strength_evidence(self):
        """optional diagnosticがunavailableでもpaired evidenceは有効である。"""
        with TemporaryDirectory() as directory:
            document, _, _ = self._build(Path(directory))
            path = Path(directory) / "paired.json"
            save_paired_result(document, path)
            reloaded = load_paired_result(path)
        self.assertFalse(reloaded["progression_diagnostics"]["available"])
        self.assertEqual(reloaded["classification"]["label"], SIGNAL_LABEL)
        self.assertEqual(len(reloaded["paired_deltas"]), 100)

    def test_existing_destination_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            document, _, _ = self._build(Path(directory))
            path = Path(directory) / "paired.json"
            save_paired_result(document, path)
            with self.assertRaises(Exception):
                save_paired_result(document, path)

    def test_tampered_classification_is_rejected(self):
        with TemporaryDirectory() as directory:
            document, _, _ = self._build(Path(directory))
        document["classification"]["label"] = NEGATIVE_LABEL
        document["classification"]["kind"] = "NEGATIVE"
        with self.assertRaises(PairedResultError):
            parse_paired_result(document)

    def test_tampered_summary_is_rejected(self):
        with TemporaryDirectory() as directory:
            document, _, _ = self._build(Path(directory))
        document["primary_summary"]["mean_delta"] = 12_345.0
        with self.assertRaises(PairedResultError):
            parse_paired_result(document)

    def test_tampered_delta_is_rejected(self):
        with TemporaryDirectory() as directory:
            document, _, _ = self._build(Path(directory))
        document["paired_deltas"][0]["delta"] = 99_999.0
        with self.assertRaises(PairedResultError):
            parse_paired_result(document)

    def test_malformed_numeric_type_is_rejected(self):
        with TemporaryDirectory() as directory:
            document, _, _ = self._build(Path(directory))
        document["paired_deltas"][0]["delta"] = 0
        with self.assertRaises(Exception):
            parse_paired_result(document)

    def test_full_verification_accepts_a_faithful_result(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            document, candidate_path, parent_path = self._build(base)
            path = base / "paired.json"
            save_paired_result(document, path)
            verified = verify_paired_result(
                path,
                candidate_artifact_path=candidate_path,
                parent_artifact_path=parent_path,
            )
        self.assertEqual(verified["result_identity"], document["result_identity"])

    def test_a_resealed_result_that_contradicts_raw_evidence_is_rejected(self):
        """documentを整合的に書き換えidentityまで再計算しても拒否する。

        deltas / summary / classification / result identityをすべて自己整合
        させても、arm artifactのraw game resultsから再導出した値と違えば
        final verificationはfail closedする。
        """
        with TemporaryDirectory() as directory:
            base = Path(directory)
            document, candidate_path, parent_path = self._build(base)

            # 全D_sへ +5000 を足し、summary / classification / identityを
            # その値へ自己整合させる。
            shifted = []
            for item in document["paired_deltas"]:
                entry = dict(item)
                entry["candidate_mean"] = entry["candidate_mean"] + 5_000.0
                entry["delta"] = entry["candidate_mean"] - entry["parent_mean"]
                shifted.append(entry)
            document["paired_deltas"] = shifted
            deltas = tuple(
                PairedSeedDelta(
                    seed=item["seed"],
                    candidate_mean=item["candidate_mean"],
                    parent_mean=item["parent_mean"],
                    delta=item["delta"],
                )
                for item in shifted
            )
            summary = summarize_paired_deltas(deltas)
            document["primary_summary"] = summary.to_document()
            document["classification"] = classify_paired_summary(summary)
            payload = {
                name: item
                for name, item in document.items()
                if name != "result_identity"
            }
            document["result_identity"] = protocol_module.document_identity(payload)

            # self-consistencyだけのreadbackは通る。
            parse_paired_result(document)

            path = base / "resealed.json"
            path.write_text(canonical_json_text(document), encoding="utf-8")
            with self.assertRaises(PairedResultError) as raised:
                verify_paired_result(
                    path,
                    candidate_artifact_path=candidate_path,
                    parent_artifact_path=parent_path,
                )
        self.assertIn("raw game results", str(raised.exception))

    def test_a_tampered_arm_artifact_is_rejected_by_full_verification(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            document, candidate_path, parent_path = self._build(base)
            path = base / "paired.json"
            save_paired_result(document, path)
            artifact = json.loads(candidate_path.read_text(encoding="utf-8"))
            artifact["game_results"][0]["scores"][0] += 1
            candidate_path.write_text(json.dumps(artifact), encoding="utf-8")
            with self.assertRaises(Exception):
                verify_paired_result(
                    path,
                    candidate_artifact_path=candidate_path,
                    parent_artifact_path=parent_path,
                )

    def test_a_swapped_arm_artifact_is_rejected_by_full_verification(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            document, candidate_path, parent_path = self._build(base)
            path = base / "paired.json"
            save_paired_result(document, path)
            with self.assertRaises(PairedResultError):
                verify_paired_result(
                    path,
                    candidate_artifact_path=parent_path,
                    parent_artifact_path=candidate_path,
                )

    def test_mismatched_arm_provenance_is_rejected(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            candidate_path = base / "candidate.json"
            parent_path = base / "parent.json"
            save_arm_artifact(
                evaluation_result(
                    arm_plan(candidate_spec(), PHASE_B_SEEDS),
                    constant_focal_score(31_000),
                ),
                candidate_path,
            )
            save_arm_artifact(
                evaluation_result(
                    arm_plan(parent_spec(), PHASE_B_SEEDS),
                    constant_focal_score(30_000),
                ),
                parent_path,
                execution_provenance=provenance(lisjong_revision="4" * 40),
            )
            with self.assertRaises(PairedResultError):
                build_paired_result(
                    candidate_artifact=load_arm_artifact(candidate_path),
                    candidate_artifact_path=candidate_path,
                    parent_artifact=load_arm_artifact(parent_path),
                    parent_artifact_path=parent_path,
                    worker_count=8,
                )


def slow_measurements() -> tuple[WorkerMeasurement, ...]:
    """sweep contractは満たすが、projectionが8hを超える計測。"""
    return (
        measurement(1, seconds=2.0 * 3600.0),
        measurement(4, seconds=1.5 * 3600.0),
        measurement(8, seconds=1.2 * 3600.0),
    )


class PhaseBGateIntegrationTests(unittest.TestCase):
    """Phase Bはlockとgate済みrecordの両方をstrict-readして初めて実行できる。"""

    def _prepare(self, directory: Path, *, passing: bool = True):
        """lockとfeasibility recordを用意し、destinationsを返す。"""
        destinations = lock_destinations(directory)
        lock_path = directory / "lock.json"
        write_lock(lock_path, destinations)
        measurements = passing_measurements() if passing else slow_measurements()
        record = build_feasibility_record(
            ordered_seeds=PHASE_A_SEEDS,
            provenance=provenance(),
            machine=collect_machine_profile(8),
            measurements=measurements,
            per_game_elapsed=None,
            gate=evaluate_feasibility_gate(measurements),
        )
        save_feasibility_record(record, destinations["feasibility_record"])
        return lock_path, destinations

    def _run(self, lock_path, destinations, execute, *, live_provenance=None):
        with locked_environment(execution_provenance=live_provenance):
            with mock.patch(
                "lisjong_arena.single_round_artifact.collect_execution_provenance",
                return_value=provenance()
                if live_provenance is None
                else live_provenance,
            ):
                return experiment.run_phase_b_development(
                    lock_path=lock_path,
                    feasibility_record_path=destinations["feasibility_record"],
                    candidate_artifact_path=destinations["candidate_artifact"],
                    parent_artifact_path=destinations["parent_artifact"],
                    paired_result_path=destinations["paired_result"],
                    execute=execute,
                )

    def test_failed_gate_blocks_phase_b_execution(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            lock_path, destinations = self._prepare(base, passing=False)
            execute = recording_execute(constant_focal_score(30_000))
            with self.assertRaises(FeasibilityError):
                self._run(lock_path, destinations, execute)
            self.assertEqual(execute.calls, [])
            self.assertFalse(destinations["candidate_artifact"].exists())

    def test_missing_record_blocks_phase_b_execution(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            destinations = lock_destinations(base)
            lock_path = base / "lock.json"
            write_lock(lock_path, destinations)
            execute = recording_execute(constant_focal_score(30_000))
            with self.assertRaises(Exception):
                self._run(lock_path, destinations, execute)
            self.assertEqual(execute.calls, [])

    def test_missing_lock_blocks_phase_b_execution(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            _, destinations = self._prepare(base)
            execute = recording_execute(constant_focal_score(30_000))
            with self.assertRaises(Exception):
                self._run(base / "absent.json", destinations, execute)
            self.assertEqual(execute.calls, [])

    def test_unlocked_destination_blocks_phase_b_execution(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            lock_path, destinations = self._prepare(base)
            elsewhere = dict(destinations)
            elsewhere["paired_result"] = base / "elsewhere.json"
            execute = recording_execute(constant_focal_score(30_000))
            with self.assertRaises(ProgressionLockError):
                self._run(lock_path, elsewhere, execute)
            self.assertEqual(execute.calls, [])

    def test_a_different_live_execution_target_blocks_phase_b(self):
        """未mergeのPR branchからはPhase Bを開始できない。"""
        with TemporaryDirectory() as directory:
            base = Path(directory)
            lock_path, destinations = self._prepare(base)
            execute = recording_execute(constant_focal_score(30_000))
            with locked_environment(head="f" * 40):
                with self.assertRaises(ProgressionLockError):
                    experiment.run_phase_b_development(
                        lock_path=lock_path,
                        feasibility_record_path=destinations["feasibility_record"],
                        candidate_artifact_path=destinations["candidate_artifact"],
                        parent_artifact_path=destinations["parent_artifact"],
                        paired_result_path=destinations["paired_result"],
                        execute=execute,
                    )
            self.assertEqual(execute.calls, [])

    def test_drifted_live_provenance_blocks_phase_b_before_game_one(self):
        """revision driftは400局を走らせる前にfail closedする。"""
        with TemporaryDirectory() as directory:
            base = Path(directory)
            lock_path, destinations = self._prepare(base)
            execute = recording_execute(constant_focal_score(30_000))
            with self.assertRaises(ProgressionLockError):
                self._run(
                    lock_path,
                    destinations,
                    execute,
                    live_provenance=provenance(lisjong_revision="9" * 40),
                )
            self.assertEqual(execute.calls, [])
            self.assertFalse(destinations["candidate_artifact"].exists())
            self.assertFalse(destinations["paired_result"].exists())

    def test_phase_a_provenance_mismatch_blocks_phase_b_before_game_one(self):
        """Phase A recordのprovenanceがliveと違えばrunnerを呼ばない。"""
        with TemporaryDirectory() as directory:
            base = Path(directory)
            destinations = lock_destinations(base)
            lock_path = base / "lock.json"
            write_lock(lock_path, destinations)
            measurements = passing_measurements()
            record = build_feasibility_record(
                ordered_seeds=PHASE_A_SEEDS,
                provenance=provenance(lisjong_revision="8" * 40),
                machine=collect_machine_profile(8),
                measurements=measurements,
                per_game_elapsed=None,
                gate=evaluate_feasibility_gate(measurements),
            )
            save_feasibility_record(record, destinations["feasibility_record"])
            execute = recording_execute(constant_focal_score(30_000))
            with self.assertRaises(PairedResultError):
                self._run(lock_path, destinations, execute)
            self.assertEqual(execute.calls, [])
            self.assertFalse(destinations["candidate_artifact"].exists())

    def test_passed_gate_runs_both_arms_with_the_selected_worker_count(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            lock_path, destinations = self._prepare(base)
            execute = recording_execute(constant_focal_score(30_000))
            outcome = self._run(lock_path, destinations, execute)
            self.assertEqual(execute.calls, [8, 8])
            self.assertEqual(outcome.worker_count, 8)
            self.assertEqual(outcome.classification["label"], INCONCLUSIVE_LABEL)
            self.assertEqual(len(outcome.paired_result["paired_deltas"]), 100)
            self.assertTrue(destinations["candidate_artifact"].exists())
            self.assertTrue(destinations["parent_artifact"].exists())

    def test_existing_artifact_destination_blocks_phase_b(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            lock_path, destinations = self._prepare(base)
            destinations["candidate_artifact"].write_text("{}", encoding="utf-8")
            execute = recording_execute(constant_focal_score(30_000))
            with self.assertRaises(Exception):
                self._run(lock_path, destinations, execute)
            self.assertEqual(execute.calls, [])

    def test_no_entry_point_accepts_a_custom_or_extended_population(self):
        """result exposure後にseedを差し替え / 追加する経路を型として持たない。"""
        import inspect

        for callable_object in (
            experiment.build_arm_plan,
            experiment.run_phase_b_development,
        ):
            parameters = set(inspect.signature(callable_object).parameters)
            self.assertFalse(
                parameters & {"seeds", "ordered_seeds", "extra_seeds", "population"},
                f"{callable_object.__name__} must not accept a caller-chosen population",
            )

    def test_arm_plans_share_seeds_and_rotation_shape(self):
        candidate_plan = experiment.build_arm_plan(candidate_arm=True, max_steps=10_000)
        parent_plan = experiment.build_arm_plan(candidate_arm=False, max_steps=10_000)
        self.assertEqual(candidate_plan.seeds, parent_plan.seeds)
        self.assertEqual(candidate_plan.seeds, PHASE_B_SEEDS)
        self.assertEqual(candidate_plan.candidate.identity, CANDIDATE_IDENTITY)
        self.assertEqual(parent_plan.candidate.identity, PARENT_IDENTITY)
        self.assertEqual(candidate_plan.baseline.identity, COMPARATOR_IDENTITY)
        self.assertEqual(parent_plan.baseline.identity, COMPARATOR_IDENTITY)
        self.assertEqual(ROTATION_COUNT, 4)


class LockTests(unittest.TestCase):
    """pre-execution lockのschemaとfail-closed境界を固定する。"""

    def _document(self, directory: Path) -> dict:
        with (
            mock.patch.object(
                lock, "collect_execution_provenance", return_value=provenance()
            ),
            mock.patch.object(lock, "require_clean_arena_head", return_value="1" * 40),
            mock.patch.object(
                lock, "require_merged_arena_revision", return_value="1" * 40
            ),
        ):
            return lock.build_lock_document(
                {
                    "feasibility_record": directory / "feasibility.json",
                    "candidate_artifact": directory / "candidate.json",
                    "parent_artifact": directory / "parent.json",
                    "paired_result": directory / "paired.json",
                }
            )

    def test_lock_binds_the_required_conditions(self):
        with TemporaryDirectory() as directory:
            document = self._document(Path(directory))
        self.assertIs(document["result_exposed"], False)
        self.assertEqual(document["candidate_binding"]["identity"], CANDIDATE_IDENTITY)
        self.assertIs(
            document["candidate_binding"]["clairvoyant_semantics_present"], False
        )
        self.assertEqual(document["parent_identity"], PARENT_IDENTITY)
        self.assertEqual(
            document["comparator_binding"]["identity"], COMPARATOR_IDENTITY
        )
        self.assertEqual(document["phase_a"]["ordered_seeds"], list(PHASE_A_SEEDS))
        self.assertEqual(document["phase_b"]["ordered_seeds"], list(PHASE_B_SEEDS))
        self.assertEqual(document["phase_b"]["games_per_arm"], 400)
        self.assertEqual(document["phase_b"]["total_games"], 800)
        self.assertEqual(document["protocol"]["game_mode"], "4p-red-single")
        self.assertIs(document["protocol"]["formal_test"], False)
        self.assertEqual(
            document["phase_a"]["wall_clock_limit_hours"],
            FEASIBILITY_WALL_CLOCK_LIMIT_HOURS,
        )
        self.assertTrue(document["no_rescue_boundary"])
        self.assertIn("lisjong_revision", document["provenance"])
        self.assertIn("python_version", document["runtime"])

    def test_lock_round_trips_through_strict_readback(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            document = self._document(base)
            path = base / "lock.json"
            lock.save_lock_document(document, path)
            reloaded = lock.load_lock_document(path)
        self.assertEqual(reloaded["lock_identity"], document["lock_identity"])

    def test_tampered_lock_is_rejected(self):
        with TemporaryDirectory() as directory:
            document = self._document(Path(directory))
        document["phase_b"]["ordered_seeds"] = list(range(1, 101))
        with self.assertRaises(lock.ProgressionLockError):
            lock.parse_lock_document(document)

    def _resealed(self, document: dict) -> dict:
        payload = {
            name: item for name, item in document.items() if name != "lock_identity"
        }
        document["lock_identity"] = protocol_module.document_identity(payload)
        return document

    def test_resealed_locks_are_rebound_to_the_locked_contract(self):
        """identityを再計算しても、locked contractと違うlockは拒否する。"""
        cases = {
            "phase A population": lambda doc: doc["phase_a"].__setitem__(
                "ordered_seeds", [1, 2, 3, 4]
            ),
            "phase B population": lambda doc: doc["phase_b"].__setitem__(
                "ordered_seeds", list(range(1, 101))
            ),
            "phase B games per arm": lambda doc: doc["phase_b"].__setitem__(
                "games_per_arm", 40
            ),
            "wall clock bound": lambda doc: doc["phase_a"].__setitem__(
                "wall_clock_limit_hours", 100.0
            ),
            "worker sweep": lambda doc: doc["phase_a"].__setitem__(
                "worker_sweep", [1, 2]
            ),
            "classification rule": lambda doc: doc["classification"].__setitem__(
                "rule", "always SIGNAL"
            ),
            "no rescue boundary": lambda doc: doc.__setitem__("no_rescue_boundary", []),
            "parent identity": lambda doc: doc.__setitem__(
                "parent_identity", "two-step"
            ),
            "candidate binding": lambda doc: doc["candidate_binding"].__setitem__(
                "clairvoyant_semantics_present", True
            ),
            "comparator binding": lambda doc: doc["comparator_binding"].__setitem__(
                "identity", "other-comparator"
            ),
            "execution target type": lambda doc: doc["execution_target"].__setitem__(
                "target_type", "anything-goes"
            ),
            "protocol block": lambda doc: doc["protocol"].__setitem__(
                "formal_test", True
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(field=name), TemporaryDirectory() as directory:
                document = self._document(Path(directory))
                mutate(document)
                with self.assertRaises(lock.ProgressionLockError):
                    lock.parse_lock_document(self._resealed(document))

    def test_a_provenance_that_contradicts_the_execution_target_is_rejected(self):
        with TemporaryDirectory() as directory:
            document = self._document(Path(directory))
        document["execution_target"]["revision"] = "e" * 40
        with self.assertRaises(lock.ProgressionLockError):
            lock.parse_lock_document(self._resealed(document))

    def test_exposed_result_flag_is_rejected(self):
        with TemporaryDirectory() as directory:
            document = self._document(Path(directory))
        document["result_exposed"] = True
        with self.assertRaises(lock.ProgressionLockError):
            lock.parse_lock_document(document)

    def test_dirty_worktree_blocks_lock_generation(self):
        from lisjong_arena._execution_safety import ExecutionSafetyError

        with TemporaryDirectory() as directory:
            base = Path(directory)
            with (
                mock.patch.object(
                    lock, "collect_execution_provenance", return_value=provenance()
                ),
                mock.patch.object(
                    lock,
                    "require_clean_arena_head",
                    side_effect=ExecutionSafetyError("dirty"),
                ),
            ):
                with self.assertRaises(lock.ProgressionLockError):
                    lock.build_lock_document(
                        {
                            "feasibility_record": base / "feasibility.json",
                            "candidate_artifact": base / "candidate.json",
                            "parent_artifact": base / "parent.json",
                            "paired_result": base / "paired.json",
                        }
                    )

    def test_unmerged_revision_blocks_lock_generation(self):
        from lisjong_arena._execution_safety import ExecutionSafetyError

        with TemporaryDirectory() as directory:
            base = Path(directory)
            with (
                mock.patch.object(
                    lock, "collect_execution_provenance", return_value=provenance()
                ),
                mock.patch.object(
                    lock, "require_clean_arena_head", return_value="1" * 40
                ),
                mock.patch.object(
                    lock,
                    "require_merged_arena_revision",
                    side_effect=ExecutionSafetyError("not merged"),
                ),
            ):
                with self.assertRaises(lock.ProgressionLockError):
                    lock.build_lock_document(
                        {
                            "feasibility_record": base / "feasibility.json",
                            "candidate_artifact": base / "candidate.json",
                            "parent_artifact": base / "parent.json",
                            "paired_result": base / "paired.json",
                        }
                    )


class CanonicalJsonTests(unittest.TestCase):
    def test_documents_are_canonical_json(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            record = build_feasibility_record(
                ordered_seeds=PHASE_A_SEEDS,
                provenance=provenance(),
                machine=collect_machine_profile(8),
                measurements=passing_measurements(),
                per_game_elapsed=None,
                gate=evaluate_feasibility_gate(passing_measurements()),
            )
            path = base / "feasibility.json"
            save_feasibility_record(record, path)
            text = path.read_text(encoding="utf-8")
        parsed = json.loads(text)
        self.assertEqual(parsed["record_identity"], record.record_identity)
        self.assertEqual(list(parsed), sorted(parsed))


if __name__ == "__main__":
    unittest.main()
