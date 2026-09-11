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

from _round_result_fixtures import neutral_round_result, scored_round_result
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
    ROUND_RESULTS_FILENAME,
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
from lisjong_arena.riichienv.round_result import RoundDrawFact, RoundResult
from lisjong_arena.riichienv.round_stats import SeatRoundStats
from lisjong_arena.single_round_artifact import SingleRoundExecutionProvenance

_MODULE = "lisjong_arena.durable_local_game_record"
_TILE = Tile(TileType(TileCategory.MANZU, 1))


_START_KYOKU = {
    "type": "start_kyoku",
    "bakaze": "E",
    "kyoku": 1,
    "honba": 0,
    "kyotaku": 0,
    "oya": 0,
    "scores": [25_000, 25_000, 25_000, 25_000],
    "dora_marker": "1m",
}
_RYUKYOKU = {
    "type": "ryukyoku",
    "reason": "exhaustive_draw",
    "deltas": [0, 0, 0, 0],
}
_HORA = {
    "type": "hora",
    "actor": 0,
    "target": 1,
    "deltas": [8_000, -8_000, 0, 0],
    "ura_markers": ["1m"],
}


def _trace(*events: dict) -> GameTrace:
    """``start_game`` / ``end_game``で挟んだobjective trace fixtureを作る。

    round-result factと同じrunを指すよう、``start_kyoku``やterminal eventは
    recorded factと一致する実際のfieldを持たせる。
    """
    return GameTrace(
        seed=7,
        game_mode="4p-red-single",
        events=tuple(
            GameTraceEvent(
                sequence=index,
                event=json.dumps(event, separators=(",", ":"), sort_keys=True),
            )
            for index, event in enumerate(
                ({"type": "start_game"}, *events, {"type": "end_game"})
            )
        ),
    )


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
    trace = _trace(_START_KYOKU, {"type": "dahai"}, _RYUKYOKU)
    return LocalGameInspection(
        result=result,
        game_trace=trace,
        round_results=(neutral_round_result(terminal_event_sequence=3),),
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
            round_results=inspection.round_results,
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
            ("schema_version", 3, "unsupported"),
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

    def test_version_1_records_are_rejected_with_an_explicit_reason(self):
        path = self._save("legacy-v1")
        _rewrite_json(
            path / MANIFEST_FILENAME,
            lambda row: row.__setitem__("schema_version", 1),
        )

        with self.assertRaisesRegex(
            DurableLocalGameRecordError, "schema version 1 records"
        ):
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


def _scored_inspection() -> LocalGameInspection:
    """backend-computed scoringまで揃ったround resultを持つinspection。"""
    base = _inspection()
    scores = (33_000, 17_000, 25_000, 25_000)
    result = LocalGameResult(
        seed=base.result.seed,
        game_mode=base.result.game_mode,
        scores=scores,
        ranks=(1, 4, 2, 3),
        steps=base.result.steps,
        decisions=base.result.decisions,
        seat_round_stats=tuple(
            SeatRoundStats(
                start_score=25_000,
                end_score=scores[seat],
                won=False,
                win_points=None,
                dealt_in=False,
                deal_in_loss=None,
                exhaustive_draw=False,
                tenpai_at_exhaustive_draw=None,
                first_tenpai_turn=None,
            )
            for seat in range(4)
        ),
    )
    return LocalGameInspection(
        result=result,
        game_trace=_trace(
            _START_KYOKU,
            {"type": "dahai"},
            {"type": "reach_accepted", "actor": 0},
            _HORA,
        ),
        step_observations=base.step_observations,
        round_results=(scored_round_result(terminal_event_sequence=4),),
    )


class DurableLocalGameRecordRoundResultTest(unittest.TestCase):
    """Issue #207で追加したper-round result payloadのpersistence contract。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _save(self, name: str = "record") -> Path:
        path = self.root / name
        save_local_game_record(
            _scored_inspection(),
            path,
            policy_identities=_identities(),
            max_steps=10,
            provenance=_provenance(),
        )
        return path

    def test_round_results_round_trip_preserves_typed_backend_facts(self):
        path = self._save()

        record = load_local_game_record(path)

        self.assertEqual(
            record.inspection.round_results,
            (scored_round_result(terminal_event_sequence=4),),
        )
        (round_result,) = record.inspection.round_results
        self.assertIs(round_result.round_wind, Wind.EAST)
        self.assertIs(round_result.dealer_seat, Seat.SEAT_0)
        self.assertEqual(round_result.riichi_seats, (Seat.SEAT_0,))
        self.assertEqual(round_result.dora_indicators, (_TILE,))
        (win,) = round_result.wins
        self.assertIs(win.winner_seat, Seat.SEAT_0)
        self.assertIs(win.loser_seat, Seat.SEAT_1)
        self.assertEqual(win.ura_indicators, (_TILE,))
        self.assertEqual(win.scoring.han, 4)
        self.assertEqual(win.scoring.fu, 30)
        self.assertEqual(win.scoring.yaku[0].name, "立直")
        self.assertTrue(round_result.win_scoring_available)

    def test_payload_is_listed_in_the_manifest_with_its_own_digest(self):
        path = self._save()

        record = load_local_game_record(path)

        reference = record.payloads["round_results"]
        self.assertEqual(reference.filename, ROUND_RESULTS_FILENAME)
        data = (path / ROUND_RESULTS_FILENAME).read_bytes()
        self.assertEqual(reference.byte_count, len(data))
        self.assertEqual(reference.sha256, hashlib.sha256(data).hexdigest())

    def test_missing_round_results_payload_is_rejected(self):
        path = self._save("missing")
        (path / ROUND_RESULTS_FILENAME).unlink()

        with self.assertRaisesRegex(DurableLocalGameRecordError, "missing or extra"):
            load_local_game_record(path)

    def test_tampered_round_results_payload_fails_the_integrity_check(self):
        path = self._save("tampered")
        _rewrite_json(
            path / ROUND_RESULTS_FILENAME,
            lambda row: row["rounds"][0]["wins"][0]["scoring"].__setitem__("han", 13),
        )

        with self.assertRaisesRegex(
            DurableLocalGameRecordError, "round_results payload"
        ):
            load_local_game_record(path)

    def test_equal_length_round_results_tamper_fails_the_digest(self):
        path = self._save("digest")
        _rewrite_json(
            path / ROUND_RESULTS_FILENAME,
            lambda row: row["rounds"][0]["wins"][0]["scoring"].__setitem__("fu", 40),
        )

        with self.assertRaisesRegex(DurableLocalGameRecordError, "digest mismatch"):
            load_local_game_record(path)

    def test_rehashed_round_results_tamper_fails_the_record_identity(self):
        path = self._save("rehashed")
        _rewrite_json(
            path / ROUND_RESULTS_FILENAME,
            lambda row: row["rounds"][0]["wins"][0]["scoring"].__setitem__("han", 13),
        )
        payload_data = (path / ROUND_RESULTS_FILENAME).read_bytes()

        def update_manifest(row):
            row["payloads"]["round_results"]["sha256"] = hashlib.sha256(
                payload_data
            ).hexdigest()
            row["payloads"]["round_results"]["byte_count"] = len(payload_data)

        _rewrite_json(path / MANIFEST_FILENAME, update_manifest)

        with self.assertRaisesRegex(DurableLocalGameRecordError, "identity"):
            load_local_game_record(path)

    def test_round_identity_that_contradicts_the_decisions_fails_closed(self):
        """round resultとGameTraceが一致しても、decision側と食い違えば拒否する。

        objective trace側の``start_kyoku``も同じhonbaへ書き換えるため、trace
        cross-checkは通過する。残るのはrecorded round identityと
        ``PolicyInput.round``のsame-run不整合だけである。
        """
        path = self._save("identity")
        _rewrite_json(
            path / ROUND_RESULTS_FILENAME,
            lambda row: row["rounds"][0].__setitem__("honba", 3),
        )
        _rewrite_json(
            path / OBJECTIVE_TRACE_FILENAME,
            lambda row: row["events"][1].__setitem__(
                "event",
                json.dumps(
                    {**_START_KYOKU, "honba": 3},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
        _refresh_payload_reference(path, "round_results")
        _refresh_payload_reference(path, "objective_trace")

        with self.assertRaisesRegex(
            DurableLocalGameRecordError, "decision round identity"
        ):
            load_local_game_record(path)

    def test_terminal_sequence_pointing_at_a_non_terminal_event_is_rejected(self):
        """terminal sequenceが``dahai``を指すround resultをreject する。"""
        path = self._save("non-terminal")
        _rewrite_json(
            path / ROUND_RESULTS_FILENAME,
            lambda row: row["rounds"][0]["wins"][0].__setitem__("event_sequence", 2),
        )
        _refresh_payload_reference(path, "round_results")

        with self.assertRaisesRegex(
            DurableLocalGameRecordError, "do not match the objective terminal sequence"
        ):
            load_local_game_record(path)

    def test_win_facts_that_contradict_the_objective_hora_are_rejected(self):
        """payload digestとrecord identityまで整えてもGameTrace mismatchで落ちる。"""
        mutations = (
            ("winner_seat", {"winner_seat": 2}, "winner seat mismatch"),
            ("loser_seat", {"loser_seat": 3}, "target seat mismatch"),
            # tsumoだけを立てるとRoundWinFact構築で落ちるため、それ自体は
            # 内部矛盾のないtamperにしてGameTrace cross-checkへ到達させる。
            ("tsumo", {"tsumo": True, "loser_seat": None}, "win method mismatch"),
            ("deltas", {"deltas": [1, -1, 0, 0]}, "score delta mismatch"),
            ("ura_indicators", {"ura_indicators": []}, "ura indicator mismatch"),
        )
        for field, changes, message in mutations:
            with self.subTest(field=field):
                path = self._save(f"hora-{field}")
                _rewrite_json(
                    path / ROUND_RESULTS_FILENAME,
                    lambda row, changes=changes: row["rounds"][0]["wins"][0].update(
                        changes
                    ),
                )
                _refresh_payload_reference(path, "round_results")

                with self.assertRaisesRegex(DurableLocalGameRecordError, message):
                    load_local_game_record(path)

    def test_start_kyoku_facts_that_contradict_the_objective_trace_are_rejected(self):
        mutations = (
            ("round_wind", "south", "round wind mismatch"),
            ("hand_number", 2, "hand number mismatch"),
            ("honba", 3, "honba mismatch"),
            ("dealer_seat", 1, "dealer seat mismatch"),
            ("riichi_sticks_before", 1, "riichi stick mismatch"),
            ("start_scores", [1, 2, 3, 99_994], "start score mismatch"),
            ("riichi_seats", [], "riichi seats do not match"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field):
                path = self._save(f"start-{field}")
                _rewrite_json(
                    path / ROUND_RESULTS_FILENAME,
                    lambda row, field=field, value=value: row["rounds"][0].__setitem__(
                        field, value
                    ),
                )
                _refresh_payload_reference(path, "round_results")

                with self.assertRaisesRegex(DurableLocalGameRecordError, message):
                    load_local_game_record(path)

    def test_dora_indicators_that_contradict_the_objective_trace_are_rejected(self):
        path = self._save("dora")
        _rewrite_json(
            path / ROUND_RESULTS_FILENAME,
            lambda row: row["rounds"][0].__setitem__(
                "dora_indicators",
                [
                    {"category": "pinzu", "is_red": False, "rank": 3},
                    {"category": "souzu", "is_red": False, "rank": 4},
                ],
            ),
        )
        _refresh_payload_reference(path, "round_results")

        with self.assertRaisesRegex(
            DurableLocalGameRecordError, "initial dora indicator mismatch"
        ):
            load_local_game_record(path)

    def test_seed_mismatch_in_the_round_payload_fails_closed(self):
        path = self._save("seed")
        _rewrite_json(
            path / ROUND_RESULTS_FILENAME, lambda row: row.__setitem__("seed", 99)
        )
        _refresh_payload_reference(path, "round_results")

        with self.assertRaisesRegex(DurableLocalGameRecordError, "seed"):
            load_local_game_record(path)

    def test_consumer_smoke_reads_round_facts_without_mahjong_rule_execution(self):
        path = self._save("smoke")

        def forbidden(*args, **kwargs):
            raise AssertionError("the strict loader must not evaluate Mahjong rules")

        with (
            patch("riichienv.HandEvaluator", forbidden),
            patch("riichienv.calculate_score", forbidden),
            patch(
                "lisjong_arena.riichienv.round_stats.HandEvaluator",
                forbidden,
            ),
        ):
            record = load_local_game_record(path)
            summary = summarize_local_game_record(record)

        self.assertEqual(summary.rounds, 1)
        self.assertEqual(summary.wins, 1)
        self.assertEqual(summary.wins_with_backend_scoring, 1)
        self.assertEqual(summary.draws, 0)

    def test_summary_counts_rounds_without_backend_scoring_separately(self):
        path = self.root / "unscored"
        save_local_game_record(
            _inspection(),
            path,
            policy_identities=_identities(),
            max_steps=10,
            provenance=_provenance(),
        )

        summary = summarize_local_game_record(load_local_game_record(path))

        self.assertEqual(summary.rounds, 1)
        self.assertEqual(summary.wins, 0)
        self.assertEqual(summary.wins_with_backend_scoring, 0)
        self.assertEqual(summary.draws, 1)
        self.assertEqual(neutral_round_result().draw.reason, "exhaustive_draw")


_ROUND_A_END_SCORES = (25_500, 23_500, 26_500, 23_500)
_ROUND_B_END_SCORES = (34_500, 15_500, 26_500, 23_500)


def _two_round_inspection() -> LocalGameInspection:
    """非最終局の境界を持つ、GameTraceとround resultsが一致するinspection。"""
    base = _inspection()
    result = LocalGameResult(
        seed=base.result.seed,
        game_mode=base.result.game_mode,
        scores=_ROUND_B_END_SCORES,
        ranks=(1, 4, 2, 3),
        steps=base.result.steps,
        decisions=base.result.decisions,
        seat_round_stats=tuple(
            SeatRoundStats(
                start_score=25_000,
                end_score=_ROUND_B_END_SCORES[seat],
                won=False,
                win_points=None,
                dealt_in=False,
                deal_in_loss=None,
                exhaustive_draw=False,
                tenpai_at_exhaustive_draw=None,
                first_tenpai_turn=None,
            )
            for seat in range(4)
        ),
    )
    first = RoundResult(
        round_wind=Wind.EAST,
        hand_number=1,
        honba=0,
        dealer_seat=Seat.SEAT_0,
        riichi_sticks_before=0,
        riichi_sticks_after=1,
        start_scores=(25_000, 25_000, 25_000, 25_000),
        end_scores=_ROUND_A_END_SCORES,
        dora_indicators=(_TILE,),
        riichi_seats=(Seat.SEAT_0,),
        start_event_sequence=1,
        wins=(),
        draw=RoundDrawFact(
            reason="exhaustive_draw",
            exhaustive=True,
            deltas=(1_500, -1_500, 1_500, -1_500),
            event_sequence=4,
        ),
    )
    second = scored_round_result(
        start_scores=_ROUND_A_END_SCORES,
        end_scores=_ROUND_B_END_SCORES,
        start_event_sequence=6,
        terminal_event_sequence=8,
    )
    second = RoundResult(
        round_wind=Wind.EAST,
        hand_number=2,
        honba=1,
        dealer_seat=Seat.SEAT_1,
        riichi_sticks_before=1,
        riichi_sticks_after=0,
        start_scores=second.start_scores,
        end_scores=second.end_scores,
        dora_indicators=second.dora_indicators,
        riichi_seats=(),
        start_event_sequence=second.start_event_sequence,
        wins=second.wins,
        draw=None,
    )
    trace = _trace(
        _START_KYOKU,
        {"type": "dahai"},
        {"type": "reach_accepted", "actor": 0},
        {
            "type": "ryukyoku",
            "reason": "exhaustive_draw",
            "deltas": [1_500, -1_500, 1_500, -1_500],
        },
        {"type": "end_kyoku"},
        {
            "type": "start_kyoku",
            "bakaze": "E",
            "kyoku": 2,
            "honba": 1,
            "kyotaku": 1,
            "oya": 1,
            "scores": list(_ROUND_A_END_SCORES),
            "dora_marker": "1m",
        },
        {"type": "dahai"},
        _HORA,
    )
    return LocalGameInspection(
        result=result,
        game_trace=trace,
        step_observations=base.step_observations,
        round_results=(first, second),
    )


class DurableLocalGameRecordTraceBindingTest(unittest.TestCase):
    """round-result factをobjective GameTraceへstrictにbindするcontract。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _save(self, inspection: LocalGameInspection, name: str) -> Path:
        path = self.root / name
        save_local_game_record(
            inspection,
            path,
            policy_identities=_identities(),
            max_steps=10,
            provenance=_provenance(),
        )
        return path

    def test_multi_round_record_round_trips_with_its_objective_trace(self):
        inspection = _two_round_inspection()

        path = self._save(inspection, "two-round")
        record = load_local_game_record(path)

        self.assertEqual(record.inspection, inspection)
        first, second = record.inspection.round_results
        self.assertEqual(first.end_scores, second.start_scores)
        self.assertEqual(first.riichi_sticks_after, second.riichi_sticks_before)
        self.assertEqual(second.end_scores, record.inspection.result.scores)

    def test_draw_facts_that_contradict_the_objective_ryukyoku_are_rejected(self):
        mutations = (
            (
                "reason",
                {"reason": "kyuushu_kyuuhai", "exhaustive": False},
                "draw reason",
            ),
            ("deltas", {"deltas": [0, 0, 0, 0]}, "score delta mismatch"),
        )
        for field, changes, message in mutations:
            with self.subTest(field=field):
                path = self._save(_two_round_inspection(), f"draw-{field}")
                _rewrite_json(
                    path / ROUND_RESULTS_FILENAME,
                    lambda row, changes=changes: row["rounds"][0]["draw"].update(
                        changes
                    ),
                )
                _refresh_payload_reference(path, "round_results")

                with self.assertRaisesRegex(DurableLocalGameRecordError, message):
                    load_local_game_record(path)

    def test_non_final_settlement_is_checked_against_the_next_start_kyoku(self):
        """次局``start_kyoku``と食い違うend scores / riichi sticksをrejectする。

        round間の連続性だけを保った改変では通らないよう、両局の対応fieldを
        同時に書き換えてもobjective trace側との不一致で落ちることを確認する。
        """
        mutations = (
            (
                "end_scores",
                {
                    0: {"end_scores": [1, 2, 3, 99_994]},
                    1: {"start_scores": [1, 2, 3, 99_994]},
                },
                "end scores do not match the next round",
            ),
            (
                "riichi_sticks",
                {0: {"riichi_sticks_after": 2}, 1: {"riichi_sticks_before": 2}},
                "riichi sticks do not match the next round",
            ),
        )
        for field, changes, message in mutations:
            with self.subTest(field=field):
                path = self._save(_two_round_inspection(), f"settlement-{field}")

                def edit(row, changes=changes):
                    for index, update in changes.items():
                        row["rounds"][index].update(update)

                _rewrite_json(path / ROUND_RESULTS_FILENAME, edit)
                _refresh_payload_reference(path, "round_results")

                with self.assertRaisesRegex(DurableLocalGameRecordError, message):
                    load_local_game_record(path)

    def test_a_dropped_round_is_rejected_against_the_objective_start_kyoku(self):
        """最終局だけを残して前局を落としても、``start_kyoku``の欠落で落ちる。

        最終局を落とす改変は既存のfinal score invariantが先に捕えるため、
        ここではstart_kyoku alignmentそのものを検証する。
        """
        path = self._save(_two_round_inspection(), "dropped")

        def edit(row):
            row["rounds"] = row["rounds"][1:]
            row["round_count"] = 1

        _rewrite_json(path / ROUND_RESULTS_FILENAME, edit)
        _refresh_payload_reference(path, "round_results")

        with self.assertRaisesRegex(
            DurableLocalGameRecordError, "do not match the objective start_kyoku"
        ):
            load_local_game_record(path)


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
            round_results=inspection.round_results,
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
