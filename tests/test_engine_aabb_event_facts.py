"""#513 第2層（公開event fact）の射影・検査と、第2層失敗の扱い（§10.6）。

公開evidenceはfixtureで合成した値であり、serializer / validatorの契約を試すだけ
である。実candidate経由の槓適用・槓factの実取得の確認にはならない（#472の
未確認事項は維持）。
"""

import copy
import dataclasses
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _engine_aabb_record_fixtures import (
    PRODUCER_REVISION,
    SCRIPT_X,
    SCRIPT_Y,
    E,
    N,
    S,
    W,
    capture,
    completion,
    completions_for,
    pon,
    riichi_evidence,
)
from lisjong_engine.round_event import DrawSource
from lisjong_engine.round_evidence import (
    DrawEvidence,
    KanConfirmedEvidence,
    RiichiFailedEvidence,
)

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb import (
    EventFactError,
    KyokuRecordError,
    build_records,
    read_kyoku_records,
    record_store,
    summarize_secondary_metrics,
    validate_event_fact_row,
    write_kyoku_records,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    EVENT_FACT_FILENAME,
    KYOKU_RECORD_FILENAME,
    MANIFEST_FILENAME,
)


def _build(captures, *, required=True):
    return build_records(captures, event_facts_required=required)


class ProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture = capture(21, 0, SCRIPT_X)
        rows, events, status = _build([cls.capture])
        cls.row, cls.events, cls.status = rows[0], events[0], status

    def test_public_facts_are_projected_in_order(self):
        self.assertTrue(self.status.valid)
        facts = [kyoku["facts"] for kyoku in self.events["kyokus"]]
        self.assertEqual(
            facts[0],
            [
                {
                    "declaration_discard_order": 8,
                    "seat": 0,
                    "self_discard_count": 3,
                    "type": "riichi_declared",
                },
                {"seat": 0, "type": "riichi_established"},
            ],
        )
        self.assertEqual(facts[1][0]["self_discard_count"], 7)
        self.assertEqual([fact["type"] for fact in facts[2]], [
            "meld_called", "kan_declared", "kan_confirmed",
        ])  # fmt: skip
        self.assertEqual(
            facts[2][0]["meld"],
            {
                "called_tile": {"is_red": True, "tile_type": 13},
                "from_seat": 2,
                "meld_type": "daiminkan",
                "tiles": [{"is_red": False, "tile_type": 13}] * 3
                + [{"is_red": True, "tile_type": 13}],
            },
        )
        self.assertEqual(
            facts[2][1]["meld"],
            {
                "called_tile": None,
                "from_seat": None,
                "meld_type": "ankan",
                "tiles": [{"is_red": False, "tile_type": 27}] * 4,
            },
        )
        self.assertEqual(facts[4][0]["meld"]["meld_type"], "pon")
        self.assertEqual(facts[3], [])
        self.assertEqual(
            [kyoku["kyoku_index"] for kyoku in self.events["kyokus"]], list(range(10))
        )

    def test_viewer_private_draws_are_not_stored(self):
        # fixtureの各viewerは自分のツモ牌（9p = tile_type 17）だけを見ている
        text = json.dumps(self.events)
        self.assertNotIn('"tile_type": 17', text)
        self.assertNotIn("draw", text)
        keys = set()

        def collect(value):
            if isinstance(value, dict):
                keys.update(value)
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(self.events)
        self.assertEqual(
            keys,
            {
                "called_discard_order", "called_tile", "dealer_seat",
                "declaration_discard_order", "facts", "from_seat", "hand_number",
                "honba", "is_red", "kyoku_index", "kyokus", "meld", "meld_type",
                "placement", "prevailing_wind", "seat", "seed", "self_discard_count",
                "tile_type", "tiles", "type",
            },
        )  # fmt: skip

    def _with_evidence(self, index, replacement):
        evidence = list(self.capture.round_evidence)
        evidence[index] = replacement
        return dataclasses.replace(self.capture, round_evidence=tuple(evidence))

    def test_viewer_disagreement_is_rejected(self):
        history = self.capture.match.history
        bad = completion(
            history[4],
            [pon(S, from_seat=W, order=2)],
            viewer_extra={N: [pon(E, from_seat=W, order=5)]},
        )
        with self.assertRaisesRegex(EventFactError, "differ between viewers"):
            _build([self._with_evidence(4, bad)])

    def test_kyoku_mapping_failures(self):
        evidence = self.capture.round_evidence
        missing = dataclasses.replace(self.capture, round_evidence=evidence[:-1])
        with self.assertRaisesRegex(EventFactError, "completions for"):
            _build([missing])
        swapped = list(evidence)
        swapped[5], swapped[6] = swapped[6], swapped[5]
        with self.assertRaisesRegex(EventFactError, "out of kyoku order"):
            _build([dataclasses.replace(self.capture, round_evidence=tuple(swapped))])
        foreign = completion(capture(22, 0, SCRIPT_Y).match.history[3])
        with self.assertRaisesRegex(EventFactError, "matches 0 kyoku"):
            _build([self._with_evidence(5, foreign)])
        with self.assertRaisesRegex(EventFactError, "not captured"):
            _build([dataclasses.replace(self.capture, round_evidence=None)])

    def test_riichi_must_match_layer1_contributions(self):
        history = self.capture.match.history
        # 第1層にK4の立直供託はない
        extra = completion(
            history[4], riichi_evidence(W, own_discards=4, first_order=1)
        )
        with self.assertRaisesRegex(EventFactError, "riichi contributions"):
            _build([self._with_evidence(4, extra)])
        # K0の立直（供託あり）が公開factにない
        with self.assertRaisesRegex(EventFactError, "riichi contributions"):
            _build([self._with_evidence(0, completion(history[0]))])
        failed = riichi_evidence(E, own_discards=3, first_order=0)
        failed[-1] = RiichiFailedEvidence(E)
        with self.assertRaisesRegex(EventFactError, "riichi contributions"):
            _build([self._with_evidence(0, completion(history[0], failed))])
        unresolved = riichi_evidence(E, own_discards=3, first_order=0)[:-1]
        with self.assertRaisesRegex(EventFactError, "never resolved"):
            _build([self._with_evidence(0, completion(history[0], unresolved))])

    def test_kan_confirmation_needs_a_declaration(self):
        history = self.capture.match.history
        kan = KanConfirmedEvidence(
            W, self.capture.round_evidence[2].projections[0].evidence[-1].meld
        )
        with self.assertRaisesRegex(EventFactError, "undeclared kan"):
            _build([self._with_evidence(3, completion(history[3], [kan]))])

    def test_validator_rejects_shape_violations(self):
        def rejected(mutate, pattern=""):
            row = copy.deepcopy(self.events)
            mutate(row)
            with self.assertRaisesRegex(EventFactError, pattern):
                validate_event_fact_row(row, self.row)

        rejected(lambda r: r.update(extra=1))
        rejected(lambda r: r.update(placement=1), "bound")
        rejected(lambda r: r["kyokus"].pop(), "count")
        rejected(lambda r: r["kyokus"][2].update(honba=0), "bound")
        rejected(lambda r: r["kyokus"][2]["facts"][0].update(type="meld_tsumo"))
        rejected(lambda r: r["kyokus"][2]["facts"][0]["meld"].update(meld_type="ankan"))
        rejected(lambda r: r["kyokus"][2]["facts"][0]["meld"].update(from_seat=3))
        rejected(lambda r: r["kyokus"][2]["facts"][0]["meld"]["tiles"].pop())
        rejected(
            lambda r: r["kyokus"][4]["facts"][0]["meld"]["tiles"][0].update(is_red=True)
        )
        rejected(lambda r: r["kyokus"][0]["facts"][0].update(self_discard_count=0))
        rejected(lambda r: r["kyokus"][0]["facts"].reverse(), "not declared")
        rejected(lambda r: r["kyokus"][0]["facts"][0].update(seat="0"))


class FailureHandlingTest(unittest.TestCase):
    """第2層の失敗は第2層全体の無効化。第1層の失敗は常に例外。"""

    @classmethod
    def setUpClass(cls):
        cls.good = capture(31, 0, SCRIPT_X)
        cls.plain = capture(31, 1, SCRIPT_Y)
        cls.broken = dataclasses.replace(cls.plain, round_evidence=None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = self.tmp / "record"

    def test_preflight_requires_layer2(self):
        with self.assertRaisesRegex(EventFactError, "not captured"):
            write_kyoku_records(
                self.path,
                [self.good, self.broken],
                producer_revision=PRODUCER_REVISION,
                event_facts_required=True,
            )
        self.assertFalse(self.path.exists())

    def test_formal_run_invalidates_all_layer2_with_reason(self):
        records = write_kyoku_records(
            self.path,
            [self.good, self.broken],
            producer_revision=PRODUCER_REVISION,
            event_facts_required=False,
        )
        self.assertFalse(records.event_fact_status.valid)
        self.assertIn("seed 31 placement 1", records.event_fact_status.reason)
        self.assertIsNone(records.event_facts)
        self.assertEqual(len(records.hanchans), 2)
        self.assertNotIn(EVENT_FACT_FILENAME, {p.name for p in self.path.iterdir()})
        manifest = json.loads((self.path / MANIFEST_FILENAME).read_text("utf-8"))
        self.assertEqual(manifest["event_facts"]["status"], "INVALID")
        self.assertIn("not captured", manifest["event_facts"]["reason"])
        read = read_kyoku_records(self.path, event_facts_required=False)
        self.assertEqual(read.event_fact_status, records.event_fact_status)
        with self.assertRaisesRegex(EventFactError, "required"):
            read_kyoku_records(self.path, event_facts_required=True)

    def test_layer1_failure_is_never_reclassified(self):
        bad_counts = dataclasses.replace(self.plain, policy_call_counts=(1, 2, 3, 4))
        with self.assertRaises(KyokuRecordError):
            write_kyoku_records(
                self.path,
                [self.good, bad_counts],
                producer_revision=PRODUCER_REVISION,
                event_facts_required=False,
            )
        self.assertFalse(self.path.exists())

    def _write_valid(self):
        return write_kyoku_records(
            self.path,
            [self.good, self.plain],
            producer_revision=PRODUCER_REVISION,
            event_facts_required=False,
        )

    def test_read_time_layer2_damage_invalidates_only_layer2(self):
        self._write_valid()
        (self.path / EVENT_FACT_FILENAME).write_text("{}\n", encoding="utf-8")
        read = read_kyoku_records(self.path, event_facts_required=False)
        self.assertFalse(read.event_fact_status.valid)
        self.assertIn("read-time verification failed", read.event_fact_status.reason)
        self.assertEqual(len(read.hanchans), 2)
        with self.assertRaises(EventFactError):
            read_kyoku_records(self.path, event_facts_required=True)
        (self.path / EVENT_FACT_FILENAME).unlink()
        missing = read_kyoku_records(self.path, event_facts_required=False)
        self.assertFalse(missing.event_fact_status.valid)

    def test_read_time_layer1_damage_still_raises(self):
        self._write_valid()
        payload = self.path / KYOKU_RECORD_FILENAME
        payload.write_bytes(payload.read_bytes().replace(b"25200", b"25300", 1))
        with self.assertRaises(KyokuRecordError):
            read_kyoku_records(self.path, event_facts_required=False)

    def test_layer2_content_tampering_with_matching_hash(self):
        self._write_valid()
        payload = self.path / EVENT_FACT_FILENAME
        old = '{"seat":0,"type":"riichi_established"}'
        text = payload.read_text("utf-8")
        self.assertIn(old, text)
        text = text.replace(old, '{"seat":1,"type":"riichi_established"}', 1)
        payload.write_text(text, encoding="utf-8", newline="\n")
        manifest_path = self.path / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text("utf-8"))
        data = text.encode("utf-8")
        manifest["event_facts"].update(
            bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
        )
        manifest_path.write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        # hashを付け直しても、作成時と同じ検査（立直の解決・供託との一致）で拒否する
        read = read_kyoku_records(self.path, event_facts_required=False)
        self.assertFalse(read.event_fact_status.valid)
        self.assertIsNone(read.event_facts)
        self.assertEqual(len(read.hanchans), 2)

    def test_invalid_layer2_hides_every_layer2_metric_but_zero_counts_are_valid(self):
        captures = [capture(41, p, SCRIPT_Y) for p in range(6)]
        valid = write_kyoku_records(
            self.tmp / "valid",
            captures,
            producer_revision=PRODUCER_REVISION,
            event_facts_required=True,
        )
        summary = summarize_secondary_metrics(valid)
        # 公開factが0件でも、第2層がVALIDなら分子0・分母ありで出す
        open_call = summary["event_fact_metrics"]["paired"]["open_call_rate"]
        self.assertEqual(open_call["per_seed"][0]["A"]["numerator"], 0)
        self.assertEqual(open_call["per_seed"][0]["A"]["denominator"], 96)
        riichi_turn = summary["event_fact_metrics"]["conditional"]["riichi_turn_mean"]
        self.assertIsNone(riichi_turn["A"]["value"])
        self.assertEqual(riichi_turn["A"]["denominator"], 0)

        broken = list(captures)
        broken[3] = dataclasses.replace(broken[3], round_evidence=None)
        invalid = write_kyoku_records(
            self.tmp / "invalid",
            broken,
            producer_revision=PRODUCER_REVISION,
            event_facts_required=False,
        )
        summary = summarize_secondary_metrics(invalid)
        self.assertIsNone(summary["event_fact_metrics"])
        self.assertEqual(summary["event_fact_status"]["status"], "INVALID")
        self.assertIn("not captured", summary["event_fact_status"]["reason"])
        # 第1層の副指標は第2層の失敗に左右されない
        self.assertEqual(
            summary["kyoku_metrics"],
            summarize_secondary_metrics(valid)["kyoku_metrics"],
        )

    def test_unknown_event_schema_is_a_layer2_failure(self):
        self._write_valid()
        manifest_path = self.path / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["event_facts"]["schema"] = "arena-other-event-fact-v9"
        manifest_path.write_text(
            canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        read = read_kyoku_records(self.path, event_facts_required=False)
        self.assertFalse(read.event_fact_status.valid)


class StorageFailureTest(unittest.TestCase):
    """保存・公開前検査の失敗を注入する（PR #515 review）。"""

    @classmethod
    def setUpClass(cls):
        cls.captures = [capture(81, 0, SCRIPT_X), capture(81, 1, SCRIPT_Y)]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = self.tmp / "record"

    def write(self, *, required):
        return write_kyoku_records(
            self.path,
            self.captures,
            producer_revision=PRODUCER_REVISION,
            event_facts_required=required,
        )

    def failing_write(self, filename):
        original = record_store.write_new_artifact_file

        def write(path, text):
            if path.name == filename:
                # 途中まで書いてから失敗する場合も、partial fileを残さない
                original(path, text[:10])
                path.unlink()
                raise OSError(f"injected failure for {filename}")
            return original(path, text)

        return mock.patch.object(record_store, "write_new_artifact_file", write)

    def assert_nothing_published(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_layer2_write_failure_keeps_layer1_in_a_formal_run(self):
        with self.failing_write(EVENT_FACT_FILENAME):
            records = self.write(required=False)
        self.assertFalse(records.event_fact_status.valid)
        self.assertIn("event fact write failed", records.event_fact_status.reason)
        self.assertEqual(len(records.hanchans), 2)
        self.assertEqual(
            sorted(child.name for child in self.path.iterdir()),
            [KYOKU_RECORD_FILENAME, MANIFEST_FILENAME],
        )
        read = read_kyoku_records(self.path, event_facts_required=False)
        self.assertEqual(read, records)
        self.assertIsNone(read.event_facts)

    def test_layer2_write_failure_stops_a_preflight(self):
        with self.failing_write(EVENT_FACT_FILENAME), self.assertRaises(OSError):
            self.write(required=True)
        self.assert_nothing_published()

    def test_layer1_and_manifest_write_failures_always_stop(self):
        for filename in (KYOKU_RECORD_FILENAME, MANIFEST_FILENAME):
            for required in (False, True):
                with (
                    self.subTest(filename=filename, required=required),
                    self.failing_write(filename),
                    self.assertRaises(OSError),
                ):
                    self.write(required=required)
                self.assert_nothing_published()

    def test_layer2_pre_publication_check_failure_keeps_layer1(self):
        def reject(row, hanchan):
            raise EventFactError("injected pre-publication failure")

        with mock.patch.object(record_store, "validate_event_fact_row", reject):
            records = self.write(required=False)
        self.assertFalse(records.event_fact_status.valid)
        self.assertIn("pre-publication check failed", records.event_fact_status.reason)
        self.assertIn("injected", records.event_fact_status.reason)
        self.assertEqual(
            sorted(child.name for child in self.path.iterdir()),
            [KYOKU_RECORD_FILENAME, MANIFEST_FILENAME],
        )
        manifest = json.loads((self.path / MANIFEST_FILENAME).read_text("utf-8"))
        self.assertEqual(manifest["event_facts"]["status"], "INVALID")
        # 公開後の読込は（差し替えなしでも）同じINVALIDと第1層を返す
        self.assertEqual(
            read_kyoku_records(self.path, event_facts_required=False), records
        )

    def missing_before_check(self):
        """公開前検査の直前に、staging上の第2層fileを消す。"""
        original = record_store.read_kyoku_records
        calls = []

        def read(path, *, event_facts_required):
            if not calls:
                (Path(path) / EVENT_FACT_FILENAME).unlink()
            calls.append(path)
            return original(path, event_facts_required=event_facts_required)

        return mock.patch.object(record_store, "read_kyoku_records", read)

    def test_layer2_file_missing_at_pre_publication_check_keeps_layer1(self):
        with self.missing_before_check():
            records = self.write(required=False)
        self.assertFalse(records.event_fact_status.valid)
        self.assertIn("pre-publication check failed", records.event_fact_status.reason)
        self.assertIn("missing", records.event_fact_status.reason)
        self.assertEqual(len(records.hanchans), 2)
        self.assertEqual(
            sorted(child.name for child in self.path.iterdir()),
            [KYOKU_RECORD_FILENAME, MANIFEST_FILENAME],
        )
        manifest = json.loads((self.path / MANIFEST_FILENAME).read_text("utf-8"))
        self.assertEqual(manifest["event_facts"]["status"], "INVALID")
        self.assertEqual(
            read_kyoku_records(self.path, event_facts_required=False), records
        )

    def test_layer2_file_missing_at_pre_publication_check_stops_a_preflight(self):
        with self.missing_before_check(), self.assertRaises(EventFactError):
            self.write(required=True)
        self.assert_nothing_published()

    def test_layer2_pre_publication_check_failure_stops_a_preflight(self):
        def reject(row, hanchan):
            raise EventFactError("injected pre-publication failure")

        with (
            mock.patch.object(record_store, "validate_event_fact_row", reject),
            self.assertRaisesRegex(EventFactError, "injected"),
        ):
            self.write(required=True)
        self.assert_nothing_published()


class InvalidTypeTest(unittest.TestCase):
    """不正型は辞書検索・同値比較の前に拒否し、EventFactErrorに統一する。"""

    @classmethod
    def setUpClass(cls):
        rows, events, _ = _build([capture(1, 0, SCRIPT_Y)])
        cls.row, cls.events = rows[0], events[0]

    def rejected(self, mutate):
        row = copy.deepcopy(self.events)
        mutate(row)
        with self.assertRaises(EventFactError):
            validate_event_fact_row(row, self.row)

    def test_unhashable_fact_type(self):
        self.rejected(lambda r: r["kyokus"][0]["facts"].append({"type": []}))
        self.rejected(lambda r: r["kyokus"][0]["facts"].append({"type": {}}))
        self.rejected(lambda r: r["kyokus"][0]["facts"].append([]))

    def test_bool_is_not_accepted_for_binding_ints(self):
        self.assertEqual((self.row["seed"], self.row["placement"]), (1, 0))
        self.rejected(lambda r: r.update(seed=True))
        self.rejected(lambda r: r.update(placement=False))
        self.rejected(lambda r: r.update(seed=1.0))
        self.rejected(lambda r: r["kyokus"][0].update(kyoku_index=False))
        self.rejected(lambda r: r["kyokus"][0].update(hand_number=True))
        self.rejected(lambda r: r["kyokus"][0].update(dealer_seat=False))
        self.rejected(lambda r: r["kyokus"][1].update(honba=True))

    def test_other_wrong_containers(self):
        self.rejected(lambda r: r.update(kyokus={}))
        self.rejected(lambda r: r["kyokus"].__setitem__(0, []))
        self.rejected(lambda r: r["kyokus"][0].update(facts={}))
        self.rejected(lambda r: r.update(seed=[1]))

    def test_valid_row_still_passes(self):
        validate_event_fact_row(copy.deepcopy(self.events), self.row)


class DrawEvidenceShapeTest(unittest.TestCase):
    def test_private_draw_differs_by_viewer_but_public_facts_match(self):
        cap = capture(51, 0, SCRIPT_Y)
        history = cap.match.history
        evidence = completions_for(cap.match, script_x=False)
        # viewer 0だけが自分のツモをもう1枚見る（private）。公開factは一致する
        extra = completion(
            history[0],
            viewer_extra={
                E: [DrawEvidence(seat=E, source=DrawSource.LIVE_WALL, tile=None)]
            },
        )
        cap = dataclasses.replace(cap, round_evidence=(extra, *evidence[1:]))
        _, events, status = _build([cap])
        self.assertTrue(status.valid)
        self.assertEqual(events[0]["kyokus"][0]["facts"], [])


if __name__ == "__main__":
    unittest.main()
