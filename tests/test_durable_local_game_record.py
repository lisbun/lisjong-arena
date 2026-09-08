"""Issue #155 durable local game recordのstrict persistence contract tests。"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from lisjong.policies.finite_horizon_completion import (
    FiniteHorizonCandidateEvaluation,
    FiniteHorizonCompletionAnalysis,
)
from lisjong.policies.hand_value_aware_two_step_ukeire import (
    HandValueAwareTwoStepUkeireAnalysis,
    HandValueCandidateEvaluation,
)
from lisjong.policies.two_step_ukeire import (
    TwoStepUkeireAnalysis,
    TwoStepUkeireCandidateEvaluation,
)
from lisjong.policies.value_aware_two_step_ukeire import (
    ValueAwareTwoStepUkeireAnalysis,
    ValueAwareTwoStepUkeireCandidateEvaluation,
)
from lisjong.policy_contract import (
    AnalysisTrace,
    AnkanAction,
    ChiAction,
    DaiminkanAction,
    DecisionTrace,
    DiscardAction,
    KakanAction,
    KyuushuKyuuhaiAction,
    OwnHandState,
    PassAction,
    PlayerPublicState,
    PolicyInput,
    PonAction,
    RiichiAction,
    RiichiState,
    RonAction,
    RoundState,
    Seat,
    Tile,
    TileCategory,
    TileType,
    TsumoAction,
    Wind,
)

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.durable_local_game_record import (
    DECISIONS_FILENAME,
    MANIFEST_FILENAME,
    OBJECTIVE_TRACE_FILENAME,
    RESULT_FILENAME,
    DurableLocalGameRecordError,
    load_local_game_record,
    run_and_save_local_game_record,
    save_local_game_record,
    summarize_local_game_record,
)
from lisjong_arena.game_trace import GameTrace, GameTraceEvent
from lisjong_arena.model import PolicySpec
from lisjong_arena.riichienv.local_game_runner import (
    LocalGameInspection,
    LocalGameResult,
    SeatDecisionObservation,
    StepDecisionObservation,
)
from lisjong_arena.riichienv.round_stats import SeatRoundStats
from lisjong_arena.single_round_artifact import SingleRoundExecutionProvenance

_MODULE = "lisjong_arena.durable_local_game_record"
_TILE = Tile(TileType(TileCategory.MANZU, 1))


def _provenance() -> SingleRoundExecutionProvenance:
    return SingleRoundExecutionProvenance(
        execution_environment="riichienv",
        lisjong_arena_version="0.1.0",
        lisjong_arena_revision="a" * 40,
        lisjong_version="0.1.0",
        lisjong_revision="b" * 40,
        lisjong_engine_version="0.1.0",
        lisjong_engine_revision="c" * 40,
        riichienv_version="0.4.8",
        python_version="3.14.6",
    )


def _player() -> PlayerPublicState:
    return PlayerPublicState(
        score=25_000,
        discards=(),
        melds=(),
        riichi=RiichiState.NONE,
    )


def _policy_input(seat: Seat) -> PolicyInput:
    return PolicyInput(
        self_seat=seat,
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1,
            dealer_seat=Seat.SEAT_0,
            honba=0,
            riichi_sticks=0,
            dora_indicators=(_TILE,),
            live_wall_tiles_remaining=70,
        ),
        players=(_player(), _player(), _player(), _player()),
        own_hand=OwnHandState(concealed_tiles=(_TILE,), drawn_tile=_TILE),
    )


def _discard(seat: Seat) -> DiscardAction:
    return DiscardAction(actor=seat, tile=_TILE, tsumogiri=True)


def _all_actions():
    one = _TILE
    two = Tile(TileType(TileCategory.MANZU, 2))
    three = Tile(TileType(TileCategory.MANZU, 3))
    return (
        _discard(Seat.SEAT_0),
        RiichiAction(actor=Seat.SEAT_0),
        ChiAction(
            actor=Seat.SEAT_0,
            target=Seat.SEAT_3,
            called_tile=two,
            consumed_tiles=(one, three),
        ),
        PonAction(
            actor=Seat.SEAT_0,
            target=Seat.SEAT_1,
            called_tile=one,
            consumed_tiles=(one, one),
        ),
        DaiminkanAction(
            actor=Seat.SEAT_0,
            target=Seat.SEAT_1,
            called_tile=one,
            consumed_tiles=(one, one, one),
        ),
        AnkanAction(actor=Seat.SEAT_0, tiles=(one, one, one, one)),
        KakanAction(
            actor=Seat.SEAT_0,
            added_tile=one,
            from_seat=Seat.SEAT_1,
            called_tile=one,
        ),
        RonAction(actor=Seat.SEAT_0, target=Seat.SEAT_1, winning_tile=one),
        TsumoAction(actor=Seat.SEAT_0, winning_tile=one),
        PassAction(actor=Seat.SEAT_0),
        KyuushuKyuuhaiAction(actor=Seat.SEAT_0),
    )


def _decision(seat: Seat, analysis: AnalysisTrace | None) -> SeatDecisionObservation:
    action = PassAction(actor=seat) if analysis is None else _discard(seat)
    return SeatDecisionObservation(
        seat=seat,
        policy_input=_policy_input(seat),
        decision_trace=DecisionTrace(
            legal_actions=(action,),
            selected_action=action,
            analysis=analysis,
        ),
    )


def _inspection() -> LocalGameInspection:
    actions = tuple(_discard(seat) for seat in Seat)
    two_step = TwoStepUkeireAnalysis(
        (TwoStepUkeireCandidateEvaluation(actions[0], 1, 4, None),)
    )
    value_aware = ValueAwareTwoStepUkeireAnalysis(
        (ValueAwareTwoStepUkeireCandidateEvaluation(actions[1], 1, 4, 1, None),)
    )
    hand_value = HandValueAwareTwoStepUkeireAnalysis(
        (HandValueCandidateEvaluation(actions[2], 1, 4, 2, 1, None),)
    )
    finite = FiniteHorizonCompletionAnalysis(
        horizon=3,
        hidden_tile_count=70,
        sequence_denominator=10,
        candidate_evaluations=(FiniteHorizonCandidateEvaluation(actions[3], 5),),
        two_step_tiebreak_analysis=TwoStepUkeireAnalysis(
            (TwoStepUkeireCandidateEvaluation(actions[3], 1, 4, None),)
        ),
    )
    stats = tuple(
        SeatRoundStats(
            start_score=25_000,
            end_score=25_000,
            won=False,
            win_points=None,
            dealt_in=False,
            deal_in_loss=None,
            exhaustive_draw=False,
            tenpai_at_exhaustive_draw=None,
            first_tenpai_turn=None,
        )
        for _ in Seat
    )
    result = LocalGameResult(
        seed=7,
        game_mode="4p-red-single",
        scores=(25_000, 25_000, 25_000, 25_000),
        ranks=(1, 2, 3, 4),
        steps=2,
        decisions=5,
        seat_round_stats=stats,
    )
    trace = GameTrace(
        seed=7,
        game_mode="4p-red-single",
        events=tuple(
            GameTraceEvent(
                sequence=index,
                event=json.dumps({"type": event_type}, separators=(",", ":")),
            )
            for index, event_type in enumerate(
                ("start_game", "start_kyoku", "dahai", "none", "end_game")
            )
        ),
    )
    return LocalGameInspection(
        result=result,
        game_trace=trace,
        step_observations=(
            StepDecisionObservation(
                step_ordinal=0,
                event_sequence_start=2,
                event_sequence_end=3,
                seat_decisions=(
                    _decision(Seat.SEAT_0, two_step),
                    _decision(Seat.SEAT_1, value_aware),
                    _decision(Seat.SEAT_2, hand_value),
                    _decision(Seat.SEAT_3, finite),
                ),
            ),
            StepDecisionObservation(
                step_ordinal=1,
                event_sequence_start=3,
                event_sequence_end=4,
                seat_decisions=(_decision(Seat.SEAT_0, None),),
            ),
        ),
    )


def _identities() -> dict[Seat, str]:
    return {seat: f"policy-{int(seat)}" for seat in Seat}


def _rewrite_json(path: Path, edit) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    edit(value)
    path.write_text(canonical_json_text(value), encoding="utf-8", newline="\n")


def _refresh_payload_reference(record_path: Path, payload_name: str) -> None:
    payload_path = record_path / f"{payload_name}.json"
    data = payload_path.read_bytes()

    def update_manifest(row):
        row["payloads"][payload_name]["sha256"] = hashlib.sha256(data).hexdigest()
        row["payloads"][payload_name]["byte_count"] = len(data)
        without_identity = dict(row)
        without_identity.pop("record_identity")
        row["record_identity"] = hashlib.sha256(
            canonical_json_text(without_identity).encode("utf-8")
        ).hexdigest()

    _rewrite_json(record_path / MANIFEST_FILENAME, update_manifest)


class DurableLocalGameRecordRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _save(self, name: str = "record") -> Path:
        path = self.root / name
        save_local_game_record(
            _inspection(),
            path,
            policy_identities=_identities(),
            max_steps=10,
            provenance=_provenance(),
        )
        return path

    def test_round_trip_preserves_trace_result_decisions_and_supported_analysis(self):
        path = self._save()

        record = load_local_game_record(path)

        self.assertEqual(record.inspection, _inspection())
        self.assertEqual(record.policy_identities, tuple(_identities().values()))
        self.assertEqual(record.max_steps, 10)
        self.assertEqual(record.provenance, _provenance())
        manifest = json.loads((path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        recorded_identity = manifest.pop("record_identity")
        self.assertEqual(
            recorded_identity,
            hashlib.sha256(canonical_json_text(manifest).encode("utf-8")).hexdigest(),
        )
        analyses = tuple(
            decision.decision_trace.analysis
            for step in record.inspection.step_observations
            for decision in step.seat_decisions
        )
        self.assertEqual(
            tuple(type(value) if value is not None else None for value in analyses),
            (
                TwoStepUkeireAnalysis,
                ValueAwareTwoStepUkeireAnalysis,
                HandValueAwareTwoStepUkeireAnalysis,
                FiniteHorizonCompletionAnalysis,
                None,
            ),
        )
        self.assertEqual(
            [
                (
                    step.event_sequence_start,
                    step.event_sequence_end,
                    tuple(decision.seat for decision in step.seat_decisions),
                )
                for step in record.inspection.step_observations
            ],
            [(2, 3, tuple(Seat)), (3, 4, (Seat.SEAT_0,))],
        )
        summary = summarize_local_game_record(record)
        self.assertEqual(summary.decisions, 5)
        self.assertEqual(summary.decisions_with_analysis, 4)

    def test_identity_is_path_independent_and_deterministic(self):
        first = load_local_game_record(self._save("first"))
        moved_path = self.root / "moved"
        shutil.copytree(self.root / "first", moved_path)
        moved = load_local_game_record(moved_path)
        second = load_local_game_record(self._save("second"))

        self.assertEqual(first.record_identity, moved.record_identity)
        self.assertEqual(first.record_identity, second.record_identity)

    def test_all_current_internal_action_variants_round_trip(self):
        inspection = _inspection()
        original_step = inspection.step_observations[0]
        original_decision = original_step.seat_decisions[0]
        actions = _all_actions()
        changed_decision = SeatDecisionObservation(
            seat=original_decision.seat,
            policy_input=original_decision.policy_input,
            decision_trace=DecisionTrace(
                legal_actions=actions,
                selected_action=actions[0],
                analysis=None,
            ),
        )
        changed_step = StepDecisionObservation(
            step_ordinal=original_step.step_ordinal,
            event_sequence_start=original_step.event_sequence_start,
            event_sequence_end=original_step.event_sequence_end,
            seat_decisions=(changed_decision, *original_step.seat_decisions[1:]),
        )
        changed_inspection = LocalGameInspection(
            result=inspection.result,
            game_trace=inspection.game_trace,
            step_observations=(changed_step, inspection.step_observations[1]),
        )
        path = self.root / "all-actions"
        save_local_game_record(
            changed_inspection,
            path,
            policy_identities=_identities(),
            max_steps=None,
            provenance=_provenance(),
        )

        loaded = load_local_game_record(path)

        self.assertEqual(
            loaded.inspection.step_observations[0]
            .seat_decisions[0]
            .decision_trace.legal_actions,
            actions,
        )

    def test_completed_record_is_strictly_readable_in_a_fresh_process(self):
        path = self._save()

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "lisjong_arena.durable_local_game_record_cli",
                "summary",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        summary = json.loads(completed.stdout)
        self.assertEqual(summary["seed"], 7)
        self.assertEqual(summary["steps"], 2)
        self.assertEqual(summary["decisions"], 5)
        self.assertEqual(summary["decisions_with_analysis"], 4)

    def test_rejects_existing_target_without_overwrite(self):
        path = self._save()
        original = (path / MANIFEST_FILENAME).read_bytes()

        with self.assertRaises(FileExistsError):
            save_local_game_record(
                _inspection(),
                path,
                policy_identities=_identities(),
                max_steps=10,
                provenance=_provenance(),
            )

        self.assertEqual((path / MANIFEST_FILENAME).read_bytes(), original)

    def test_unknown_version_and_tampered_manifest_fail_closed(self):
        for field, value, message in (
            ("schema_id", "future-schema", "unsupported"),
            ("schema_version", 2, "unsupported"),
            ("seed", 8, "identity"),
            ("record_identity", "0" * 64, "identity"),
        ):
            with self.subTest(field=field):
                path = self._save(field)
                _rewrite_json(
                    path / MANIFEST_FILENAME, lambda row: row.__setitem__(field, value)
                )
                with self.assertRaisesRegex(DurableLocalGameRecordError, message):
                    load_local_game_record(path)

    def test_wrong_manifest_field_type_uses_the_dedicated_error(self):
        path = self._save()
        _rewrite_json(
            path / MANIFEST_FILENAME,
            lambda row: row.__setitem__("schema_version", True),
        )

        with self.assertRaises(DurableLocalGameRecordError):
            load_local_game_record(path)

    def test_missing_truncated_and_digest_mismatch_fail_closed(self):
        missing = self._save("missing")
        (missing / DECISIONS_FILENAME).unlink()
        with self.assertRaisesRegex(DurableLocalGameRecordError, "missing or extra"):
            load_local_game_record(missing)

        truncated = self._save("truncated")
        payload = truncated / OBJECTIVE_TRACE_FILENAME
        payload.write_bytes(payload.read_bytes()[:20])
        with self.assertRaisesRegex(DurableLocalGameRecordError, "byte count"):
            load_local_game_record(truncated)

        corrupt = self._save("corrupt")
        result_path = corrupt / RESULT_FILENAME
        data = bytearray(result_path.read_bytes())
        data[-2] = ord(" ")
        result_path.write_bytes(data)
        with self.assertRaisesRegex(DurableLocalGameRecordError, "digest"):
            load_local_game_record(corrupt)

    def test_payload_tamper_with_updated_digest_still_fails_same_run_consistency(self):
        path = self._save()
        decisions = path / DECISIONS_FILENAME
        _rewrite_json(decisions, lambda row: row.__setitem__("seed", 99))
        _refresh_payload_reference(path, "decisions")
        with self.assertRaisesRegex(DurableLocalGameRecordError, "seed fields"):
            load_local_game_record(path)

    def test_unknown_analysis_type_is_rejected_even_after_rehashing(self):
        path = self._save()
        decisions = path / DECISIONS_FILENAME

        def replace_analysis_type(row):
            row["steps"][0]["seat_decisions"][0]["decision_trace"]["analysis"][
                "type"
            ] = "future.package:UnknownAnalysis"

        _rewrite_json(decisions, replace_analysis_type)
        _refresh_payload_reference(path, "decisions")

        with self.assertRaisesRegex(DurableLocalGameRecordError, "unsupported"):
            load_local_game_record(path)

    def test_incomplete_bundle_is_rejected(self):
        path = self.root / "incomplete"
        path.mkdir()
        (path / MANIFEST_FILENAME).write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(DurableLocalGameRecordError, "missing or extra"):
            load_local_game_record(path)

    def test_readback_validation_failure_publishes_no_final_record(self):
        path = self.root / "record"
        with (
            patch(
                f"{_MODULE}.load_local_game_record",
                side_effect=DurableLocalGameRecordError("readback failed"),
            ),
            self.assertRaisesRegex(DurableLocalGameRecordError, "readback failed"),
        ):
            save_local_game_record(
                _inspection(),
                path,
                policy_identities=_identities(),
                max_steps=None,
                provenance=_provenance(),
            )
        self.assertFalse(path.exists())
        self.assertEqual(tuple(self.root.iterdir()), ())


class DurableLocalGameRecordAcquisitionTest(unittest.TestCase):
    def test_failed_runner_does_not_finalize_a_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record"
            policies = {
                seat: PolicySpec(f"policy-{int(seat)}", lambda: object())
                for seat in Seat
            }
            with (
                patch(
                    f"{_MODULE}.LocalGameRunner.run",
                    side_effect=RuntimeError("game failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "game failed"),
            ):
                run_and_save_local_game_record(policies, seed=7, path=path)
            self.assertFalse(path.exists())

    def test_rejects_unknown_analysis_subtype_before_creating_target(self):
        @dataclass(frozen=True, slots=True)
        class UnsupportedAnalysis(AnalysisTrace):
            value: int

        inspection = _inspection()
        original = inspection.step_observations[0]
        seat = original.seat_decisions[0]
        changed = SeatDecisionObservation(
            seat=seat.seat,
            policy_input=seat.policy_input,
            decision_trace=DecisionTrace(
                legal_actions=seat.decision_trace.legal_actions,
                selected_action=seat.decision_trace.selected_action,
                analysis=UnsupportedAnalysis(1),
            ),
        )
        changed_step = StepDecisionObservation(
            step_ordinal=original.step_ordinal,
            event_sequence_start=original.event_sequence_start,
            event_sequence_end=original.event_sequence_end,
            seat_decisions=(changed, *original.seat_decisions[1:]),
        )
        changed_inspection = LocalGameInspection(
            result=inspection.result,
            game_trace=inspection.game_trace,
            step_observations=(changed_step, inspection.step_observations[1]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record"
            with self.assertRaisesRegex(DurableLocalGameRecordError, "unsupported"):
                save_local_game_record(
                    changed_inspection,
                    path,
                    policy_identities=_identities(),
                    max_steps=None,
                    provenance=_provenance(),
                )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
