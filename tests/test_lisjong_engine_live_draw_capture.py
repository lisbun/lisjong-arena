"""通常ツモopt-in capture（#596）のfixture test。

実際のlisjong-engineで1半荘を進め、その観測列を共有して検査する。scripted
selector（和了・槓・鳴き・立直を優先）は、鳴き・3種の槓・嶺上・流局直前の
最後のツモ・赤牌を1半荘の中で起こすためのtest用入力であり、評価runではない
（registry外の固定seed）。異常系は、実際の観測列を人工的に改ざんして作る。

Policyへの情報境界（captureの有無で`DecisionContext`・選択・結果が変わらないこと）
は、実`MinimalPolicy`の半荘で別に固定する。
"""

import copy
import dataclasses
import functools
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from lisjong.policies.minimal import MinimalPolicy
from lisjong_engine import action_descriptor as descriptors
from lisjong_engine.driver import run_hanchan
from lisjong_engine.match_state import MatchState
from lisjong_engine.round_event import DrawSource, TileDrawnEvent
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.tile import STANDARD_TILES
from lisjong_engine.transaction_observation import TransactionStepKind

from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan
from lisjong_arena.lisjong_engine.live_draw_capture import (
    TOTAL_TILE_COUNT,
    LiveDrawCapture,
    LiveDrawCaptureError,
    capture_live_draw_hanchan,
    validate_row,
    visible_tile_ids,
)
from lisjong_arena.lisjong_engine.live_draw_store import (
    MANIFEST_FILENAME,
    ROWS_FILENAME,
    read_live_draw_capture,
    write_live_draw_capture,
)

# registry外のtest用seed。この半荘で鳴き・3種の槓・嶺上・最後の通常ツモ・赤牌が起きる。
_SEED = 1
_REVISION = "0" * 40

_PRIORITY = (
    descriptors.TsumoActionDescriptor,
    descriptors.RonActionDescriptor,
    descriptors.DaiminkanActionDescriptor,
    descriptors.AnkanActionDescriptor,
    descriptors.KakanActionDescriptor,
    descriptors.PonActionDescriptor,
    descriptors.ChiActionDescriptor,
    descriptors.RiichiActionDescriptor,
    descriptors.DiscardActionDescriptor,
    descriptors.PassActionDescriptor,
    descriptors.NineTerminalsActionDescriptor,
)


def _scripted_selector(log=None):
    def select(observation, options):
        for kind in _PRIORITY:
            for option in options:
                if type(option) is kind:
                    if log is not None:
                        log.append((observation, tuple(options), option))
                    return option
        raise AssertionError("no known descriptor was offered")

    return select


def _run_engine(seed, *, observer=None, evidence=None, log=None):
    callbacks = {}
    if observer is not None:
        callbacks["on_transaction_observation"] = observer
    if evidence is not None:
        callbacks["on_round_evidence_complete"] = evidence
    selector = _scripted_selector(log)
    return run_hanchan(
        MatchState(seed=seed, rules=RuleSet.default()),
        {seat: selector for seat in EngineSeat},
        **callbacks,
    )


def _replay(observations, completions, completed, *, placement=0):
    capture = LiveDrawCapture(seed=_SEED, placement=placement)
    for observation in observations:
        capture.on_transaction_observation(observation)
    for completion in completions:
        capture.on_round_evidence_complete(completion)
    return capture.finish(completed)


@functools.cache
def _shared_run():
    """1半荘の観測列・evidence・結果・captureを、test class間で1回だけ作る。"""
    observations = []
    completions = []
    completed = _run_engine(
        _SEED, observer=observations.append, evidence=completions.append
    )
    captured = _replay(observations, completions, completed)
    return tuple(observations), tuple(completions), completed, captured


class _EngineCaptureFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        observations, completions, cls.completed, cls.captured = _shared_run()
        cls.observations = list(observations)
        cls.completions = list(completions)
        cls.rows = cls.captured.rows


class CaptureCompletenessTest(_EngineCaptureFixture):
    def test_rows_match_independent_draw_events(self) -> None:
        live = rinshan = 0
        for observation in self.observations:
            for step in observation.steps:
                for event in step.events:
                    if isinstance(event, TileDrawnEvent):
                        if event.source is DrawSource.LIVE_WALL:
                            live += 1
                        else:
                            rinshan += 1
        self.assertEqual(len(self.rows), live)
        self.assertEqual(
            sum(item.rinshan_draw_count for item in self.captured.rounds), rinshan
        )
        self.assertGreater(rinshan, 0)
        self.assertEqual(len(self.captured.rounds), len(self.completed.history))

    def test_identity_is_contiguous_per_round(self) -> None:
        for round_entry in self.captured.rounds:
            rows = [
                row
                for row in self.rows
                if row["identity"]["round_ordinal"] == round_entry.round_ordinal
            ]
            self.assertEqual(len(rows), round_entry.live_draw_count)
            self.assertEqual(
                [row["identity"]["draw_index"] for row in rows],
                list(range(len(rows))),
            )

    def test_every_row_passes_shared_validation(self) -> None:
        for row in self.rows:
            validate_row(row)

    def test_unseen_distribution_is_normalized_and_contains_realized_tile(self) -> None:
        for row in self.rows:
            checkpoint = row["pre_draw"]["checkpoint"]
            visible = set(visible_tile_ids(checkpoint))
            unseen = Counter(
                (STANDARD_TILES[tile].tile_type.id, STANDARD_TILES[tile].is_red)
                for tile in range(TOTAL_TILE_COUNT)
                if tile not in visible
            )
            total = sum(unseen.values())
            self.assertEqual(
                total,
                checkpoint["live_tiles_remaining"]
                + 14
                - len(checkpoint["revealed_dora_indicators"]),
            )
            realized = STANDARD_TILES[row["realized"]["tile"]]
            self.assertGreaterEqual(unseen[(realized.tile_type.id, realized.is_red)], 1)


class CaptureBoundaryCaseTest(_EngineCaptureFixture):
    def test_first_dealer_draw_is_a_live_wall_row(self) -> None:
        first = self.rows[0]
        checkpoint = first["pre_draw"]["checkpoint"]
        self.assertEqual(first["identity"]["round_ordinal"], 1)
        self.assertEqual(first["identity"]["draw_index"], 0)
        self.assertEqual(first["identity"]["drawer_seat"], checkpoint["dealer_seat"])
        self.assertEqual(
            [len(seat["hand_tiles"]) for seat in checkpoint["seats"]], [13] * 4
        )
        self.assertEqual(checkpoint["live_tiles_remaining"], 70)
        self.assertEqual(first["identity"]["revision"], 2)
        self.assertEqual(first["pre_draw"]["revision"], 1)

    def test_draw_after_call_skips_turn_order(self) -> None:
        skipped = False
        for previous, row in zip(self.rows, self.rows[1:]):
            same_round = (
                previous["identity"]["round_ordinal"]
                == row["identity"]["round_ordinal"]
            )
            if (
                same_round
                and row["identity"]["drawer_seat"]
                != (previous["identity"]["drawer_seat"] + 1) % 4
            ):
                skipped = True
        self.assertTrue(skipped)

    def test_all_meld_types_and_post_rinshan_draws_appear(self) -> None:
        meld_types = {
            meld["meld_type"]
            for row in self.rows
            for seat in row["pre_draw"]["checkpoint"]["seats"]
            for meld in seat["melds"]
        }
        self.assertEqual(meld_types, {"pon", "chi", "daiminkan", "kakan", "ankan"})
        self.assertTrue(
            any(
                row["pre_draw"]["checkpoint"]["rinshan_tiles_remaining"] < 4
                for row in self.rows
            )
        )

    def test_red_tiles_and_last_live_draw_appear(self) -> None:
        self.assertTrue(
            any(STANDARD_TILES[row["realized"]["tile"]].is_red for row in self.rows)
        )
        self.assertTrue(
            any(
                row["pre_draw"]["checkpoint"]["live_tiles_remaining"] == 1
                for row in self.rows
            )
        )

    def test_called_discards_are_counted_once(self) -> None:
        called = 0
        for row in self.rows:
            checkpoint = row["pre_draw"]["checkpoint"]
            visible = visible_tile_ids(checkpoint)
            self.assertEqual(len(visible), len(set(visible)))
            called += sum(
                discard["called_by"] is not None
                for seat in checkpoint["seats"]
                for discard in seat["discards"]
            )
        self.assertGreater(called, 0)


class CaptureRejectsTamperedStreamTest(_EngineCaptureFixture):
    def _first_draw_index(self, source=DrawSource.LIVE_WALL, skip=0):
        for index, observation in enumerate(self.observations):
            event = observation.steps[0].events
            if (
                observation.steps[0].kind is TransactionStepKind.DRAW
                and event[0].source is source
            ):
                if skip == 0:
                    return index
                skip -= 1
        raise AssertionError("draw not found")

    def assert_rejected(self, observations, completions=None):
        with self.assertRaises(LiveDrawCaptureError):
            _replay(
                observations,
                self.completions if completions is None else completions,
                self.completed,
            )

    def test_missing_observation_is_rejected(self) -> None:
        index = self._first_draw_index(skip=3)
        self.assert_rejected(self.observations[:index] + self.observations[index + 1 :])

    def test_duplicated_observation_is_rejected(self) -> None:
        index = self._first_draw_index(skip=3)
        observations = list(self.observations)
        observations.insert(index, observations[index])
        self.assert_rejected(observations)

    def test_reordered_observations_are_rejected(self) -> None:
        index = self._first_draw_index(skip=3)
        observations = list(self.observations)
        observations[index], observations[index + 1] = (
            observations[index + 1],
            observations[index],
        )
        self.assert_rejected(observations)

    def test_stream_starting_mid_round_is_rejected(self) -> None:
        self.assert_rejected(self.observations[1:])

    def test_observation_from_another_round_is_rejected(self) -> None:
        index = self._first_draw_index(skip=3)
        observations = list(self.observations)
        observations[index] = dataclasses.replace(
            observations[index], round_ordinal=observations[index].round_ordinal + 1
        )
        self.assert_rejected(observations)

    def test_post_draw_inconsistency_is_rejected(self) -> None:
        index = self._first_draw_index(skip=3)
        observations = list(self.observations)
        draw = observations[index]
        stale = dataclasses.replace(
            draw.steps[0], checkpoint=observations[index - 1].steps[-1].checkpoint
        )
        observations[index] = dataclasses.replace(draw, steps=(stale,))
        self.assert_rejected(observations)

    def test_wrong_realized_tile_is_rejected(self) -> None:
        index = self._first_draw_index(skip=3)
        observations = list(self.observations)
        draw = observations[index]
        event = draw.steps[0].events[0]
        other = next(
            tile
            for tile in STANDARD_TILES
            if tile != event.tile and tile.tile_type != event.tile.tile_type
        )
        forged = dataclasses.replace(
            draw.steps[0], events=(dataclasses.replace(event, tile=other),)
        )
        observations[index] = dataclasses.replace(draw, steps=(forged,))
        self.assert_rejected(observations)

    def test_missing_round_evidence_is_rejected(self) -> None:
        self.assert_rejected(self.observations, self.completions[:-1])

    def test_mismatched_round_evidence_is_rejected(self) -> None:
        completions = list(self.completions)
        completions[0], completions[1] = completions[1], completions[0]
        self.assert_rejected(self.observations, completions)

    def test_unfinished_round_is_rejected(self) -> None:
        last_round_start = max(
            index
            for index, observation in enumerate(self.observations)
            if observation.steps[0].kind is TransactionStepKind.ROUND_STARTED
        )
        self.assert_rejected(self.observations[: last_round_start + 3])

    def test_capture_cannot_be_reused_after_finish(self) -> None:
        capture = LiveDrawCapture(seed=_SEED, placement=0)
        for observation in self.observations:
            capture.on_transaction_observation(observation)
        for completion in self.completions:
            capture.on_round_evidence_complete(completion)
        capture.finish(self.completed)
        with self.assertRaises(LiveDrawCaptureError):
            capture.on_transaction_observation(self.observations[0])


class RowValidationTest(_EngineCaptureFixture):
    def _row(self, index=40):
        return copy.deepcopy(self.rows[index])

    def assert_invalid(self, row):
        with self.assertRaises(LiveDrawCaptureError):
            validate_row(row)

    def test_unknown_or_missing_field_is_rejected(self) -> None:
        row = self._row()
        row["identity"]["extra"] = 1
        self.assert_invalid(row)
        row = self._row()
        del row["realized"]["tile"]
        self.assert_invalid(row)

    def test_duplicate_physical_tile_is_rejected(self) -> None:
        row = self._row()
        seats = row["pre_draw"]["checkpoint"]["seats"]
        seats[1]["hand_tiles"][0] = seats[0]["hand_tiles"][0]
        self.assert_invalid(row)

    def test_realized_tile_already_visible_is_rejected(self) -> None:
        row = self._row()
        row["realized"]["tile"] = row["pre_draw"]["checkpoint"]["seats"][0][
            "hand_tiles"
        ][0]
        self.assert_invalid(row)

    def test_broken_count_invariant_is_rejected(self) -> None:
        row = self._row()
        row["pre_draw"]["checkpoint"]["live_tiles_remaining"] -= 1
        self.assert_invalid(row)

    def test_drawn_tile_before_draw_is_rejected(self) -> None:
        row = self._row()
        seat = row["pre_draw"]["checkpoint"]["seats"][0]
        seat["drawn_tile"] = seat["hand_tiles"][0]
        seat["drawn_tile_source"] = "live_wall"
        self.assert_invalid(row)

    def test_wrong_phase_or_drawer_is_rejected(self) -> None:
        row = self._row()
        row["pre_draw"]["phase"] = "awaiting_discard"
        self.assert_invalid(row)
        row = self._row()
        row["pre_draw"]["current_seat"] = (row["identity"]["drawer_seat"] + 1) % 4
        self.assert_invalid(row)

    def test_called_discard_without_meld_is_rejected(self) -> None:
        for index, original in enumerate(self.rows):
            seats = original["pre_draw"]["checkpoint"]["seats"]
            if any(
                meld["meld_type"] == "pon" for seat in seats for meld in seat["melds"]
            ):
                row = self._row(index)
                for seat in row["pre_draw"]["checkpoint"]["seats"]:
                    seat["melds"] = [
                        meld for meld in seat["melds"] if meld["meld_type"] != "pon"
                    ]
                self.assert_invalid(row)
                return
        self.fail("no row with a pon")


class CaptureObserverFailureTest(unittest.TestCase):
    def test_observer_exception_fails_the_whole_run(self) -> None:
        def explode(observation):
            raise RuntimeError("capture failed")

        with self.assertRaises(RuntimeError):
            _run_engine(_SEED, observer=explode)


class CaptureDoesNotChangeExecutionTest(unittest.TestCase):
    """engine scripted selector: captureの有無で観測・選択・結果が一致する。"""

    def test_engine_execution_is_identical_with_and_without_capture(self) -> None:
        plain_log = []
        plain = _run_engine(_SEED, log=plain_log)
        captured_log = []
        capture = LiveDrawCapture(seed=_SEED, placement=0)
        observed = _run_engine(
            _SEED,
            observer=capture.on_transaction_observation,
            evidence=capture.on_round_evidence_complete,
            log=captured_log,
        )
        capture.finish(observed)
        self.assertEqual(plain, observed)
        self.assertEqual(plain_log, captured_log)


class _RecordingPolicy:
    def __init__(self) -> None:
        self._policy = MinimalPolicy()
        self.decisions = []

    def choose_action(self, decision):
        self.decisions.append(decision)
        action = self._policy.choose_action(decision)
        self.decisions.append(action)
        return action


class PolicyInformationBoundaryTest(unittest.TestCase):
    """実Policy 4体: captureの有無で4席の`DecisionContext`・行動・結果が一致する。"""

    _POLICY_SEED = 20260824

    def test_policy_inputs_and_results_are_unchanged_by_capture(self) -> None:
        plain_policies = {seat: _RecordingPolicy() for seat in EngineSeat}
        plain = run_policy_hanchan(plain_policies, seed=self._POLICY_SEED)
        captured_policies = {seat: _RecordingPolicy() for seat in EngineSeat}
        captured, hanchan = capture_live_draw_hanchan(
            captured_policies, seed=self._POLICY_SEED, placement=0
        )
        self.assertEqual(plain, captured)
        for seat in EngineSeat:
            self.assertEqual(
                plain_policies[seat].decisions, captured_policies[seat].decisions
            )
        self.assertGreater(len(hanchan.rows), 0)
        for row in hanchan.rows:
            self.assertEqual(row["identity"]["seed"], self._POLICY_SEED)

    def test_wrapper_rejects_non_callable_observer(self) -> None:
        with self.assertRaises(TypeError):
            run_policy_hanchan(
                {seat: MinimalPolicy() for seat in EngineSeat},
                seed=self._POLICY_SEED,
                on_transaction_observation=object(),
            )


class LiveDrawStoreTest(_EngineCaptureFixture):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.destination = self.root / "capture"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, destination=None, hanchans=None, **overrides):
        arguments = {
            "arena_revision": _REVISION,
            "engine_revision": _REVISION,
            "rules": "project-standard-v1",
            "access": "test fixture only",
            "retention": "deleted with the temporary directory",
        }
        arguments.update(overrides)
        return write_live_draw_capture(
            self.destination if destination is None else destination,
            [self.captured] if hanchans is None else hanchans,
            **arguments,
        )

    def test_round_trip_splits_identity_pre_draw_and_realized(self) -> None:
        artifact = self._write()
        self.assertEqual(len(artifact.records), len(self.rows))
        for record, row in zip(artifact.records, self.rows):
            self.assertEqual(record.identity, row["identity"])
            self.assertEqual(record.pre_draw, row["pre_draw"])
            self.assertEqual(record.realized, row["realized"])
            self.assertNotIn("tile", record.pre_draw)
        self.assertEqual(
            artifact.manifest["information_class"], "privileged-offline-only"
        )
        self.assertEqual(read_live_draw_capture(self.destination), artifact)

    def test_existing_destination_is_not_overwritten(self) -> None:
        self._write()
        with self.assertRaises(FileExistsError):
            self._write()

    def test_access_and_retention_are_required(self) -> None:
        with self.assertRaises(LiveDrawCaptureError):
            self._write(access=" ")
        with self.assertRaises(LiveDrawCaptureError):
            self._write(retention="")
        self.assertFalse(self.destination.exists())

    def test_duplicate_hanchan_identity_is_rejected(self) -> None:
        with self.assertRaises(LiveDrawCaptureError):
            self._write(hanchans=[self.captured, self.captured])
        self.assertFalse(self.destination.exists())

    def test_tampered_payload_is_rejected(self) -> None:
        self._write()
        rows = self.destination / ROWS_FILENAME
        data = bytearray(rows.read_bytes())
        data[10] = ord("X") if data[10] != ord("X") else ord("Y")
        rows.write_bytes(bytes(data))
        with self.assertRaises(LiveDrawCaptureError):
            read_live_draw_capture(self.destination)

    def test_extra_file_is_rejected(self) -> None:
        self._write()
        (self.destination / "extra.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(LiveDrawCaptureError):
            read_live_draw_capture(self.destination)

    def test_manifest_count_mismatch_is_rejected(self) -> None:
        self._write()
        manifest_path = self.destination / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["hanchans"][0]["rounds"][0]["live_draw_count"] -= 1
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaises(LiveDrawCaptureError):
            read_live_draw_capture(self.destination)


if __name__ == "__main__":
    unittest.main()
