"""#513 第1層（局精算record）の射影・検査・保存・読込・schedule照合。

期待値は ``_engine_aabb_record_fixtures`` のscript X / Yを手計算した値であり、
実行結果に合わせて書いていない（script docstring参照）。
"""

import copy
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from _engine_aabb_record_fixtures import (
    CANDIDATE_IDENTITY,
    INCUMBENT_IDENTITY,
    PRODUCER_REVISION,
    SCRIPT_X,
    SCRIPT_Y,
    capture,
    seats_for,
)
from lisjong_engine.rules import RuleSet

from lisjong_arena.heuristic_candidate_engine_aabb import (
    ExpectedHanchan,
    KyokuRecordError,
    RecordBindings,
    SeatAssignment,
    build_records,
    kyoku_record,
    read_kyoku_records,
    require_bindings,
    validate_hanchan_row,
    verify_schedule,
    write_kyoku_records,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import (
    KYOKU_RECORD_FILENAME,
    MANIFEST_FILENAME,
)


def _row(script=SCRIPT_X, *, seed=513001, placement=0):
    rows, _, _ = build_records(
        [capture(seed, placement, script)], event_facts_required=True
    )
    return rows[0]


class RuleValuesTest(unittest.TestCase):
    def test_arithmetic_inputs_match_the_locked_rule_set(self):
        rules = RuleSet.default()
        self.assertEqual(
            (rules.name, rules.version),
            (
                kyoku_record.RULES_IDENTITY["name"],
                kyoku_record.RULES_IDENTITY["version"],
            ),
        )
        self.assertEqual(rules.starting_points, kyoku_record.STARTING_POINTS)
        self.assertEqual(rules.return_points, kyoku_record.RETURN_POINTS)
        self.assertEqual(tuple(rules.uma), kyoku_record.UMA)
        self.assertEqual(rules.riichi_stick_points, kyoku_record.RIICHI_STICK_POINTS)
        self.assertEqual(rules.bankruptcy_threshold, kyoku_record.BANKRUPTCY_THRESHOLD)

    def test_placement_table(self):
        self.assertEqual(
            [kyoku_record.placement_roles(p) for p in range(6)],
            [
                ("A", "A", "B", "B"),
                ("A", "B", "A", "B"),
                ("A", "B", "B", "A"),
                ("B", "A", "A", "B"),
                ("B", "A", "B", "A"),
                ("B", "B", "A", "A"),
            ],
        )


class ProjectionTest(unittest.TestCase):
    """script X / Yの射影を手計算値と照合する。"""

    @classmethod
    def setUpClass(cls):
        cls.x = _row(SCRIPT_X)
        cls.y = _row(SCRIPT_Y, placement=1)

    def test_hanchan_summary_x(self):
        row = self.x
        self.assertEqual(row["end_reason"], "target_reached")
        self.assertEqual(row["final_raw_scores"], [25_200, 32_600, 15_700, 26_500])
        self.assertEqual(
            row["final_riichi_stick_awards"], [{"amount": 1000, "seat": 1}]
        )
        self.assertEqual(
            [
                (s["rank"], s["base_points"], s["uma_points"], s["oka_points"])
                for s in row["final_score"]
            ],
            [
                (3, -40, -100, 0),
                (1, 10, 300, 200),
                (4, -140, -300, 0),
                (2, -30, 100, 0),
            ],
        )
        self.assertEqual(
            [s["final_points"] for s in row["final_score"]], [-140, 510, -440, 70]
        )
        self.assertEqual(
            row["seats"][0], {"policy_identity": CANDIDATE_IDENTITY, "role": "A"}
        )
        self.assertEqual(
            row["seats"][2], {"policy_identity": INCUMBENT_IDENTITY, "role": "B"}
        )
        self.assertEqual(len(row["kyokus"]), 10)

    def test_single_ron_with_riichi_stick(self):
        kyoku = self.x["kyokus"][0]
        self.assertEqual(
            kyoku["result"],
            {
                "kind": "win",
                "win": {
                    "is_last_tile": False,
                    "method": "ron",
                    "origin": "discard",
                    "source_seat": 2,
                    "winner_seats": [1],
                },
            },
        )
        self.assertEqual(
            kyoku["transfers"],
            [
                {
                    "amount": 1600,
                    "payer": 2,
                    "reason": "ron",
                    "recipient": 1,
                    "winner_seat": 1,
                }
            ],
        )
        self.assertEqual(kyoku["riichi_contributions"], [{"points": 1000, "seat": 0}])
        self.assertEqual(kyoku["riichi_stick_awards"], [{"amount": 1000, "seat": 1}])
        self.assertEqual(kyoku["point_deltas"], [-1000, 2600, -1600, 0])
        self.assertEqual(kyoku["points_before"], [25_000] * 4)
        self.assertEqual(kyoku["points_after"], [24_000, 27_600, 23_400, 25_000])
        self.assertEqual(
            (kyoku["round_ordinal"], kyoku["dealer_continues"]), (1, False)
        )
        self.assertEqual(
            kyoku["next_position"],
            {
                "dealer_seat": 1,
                "hand_number": 2,
                "honba": 0,
                "prevailing_wind": "east",
                "riichi_sticks": 0,
            },
        )

    def test_exhaustive_draw_carries_the_stick(self):
        kyoku = self.x["kyokus"][1]
        self.assertEqual(
            kyoku["result"],
            {
                "exhaustive_draw": {"nagashi_mangan_seats": [], "tenpai_seats": [1, 3]},
                "kind": "exhaustive_draw",
            },
        )
        self.assertEqual(
            sorted(
                (t["payer"], t["recipient"], t["amount"]) for t in kyoku["transfers"]
            ),
            [(0, 1, 750), (0, 3, 750), (2, 1, 750), (2, 3, 750)],
        )
        self.assertTrue(all(t["reason"] == "noten_penalty" for t in kyoku["transfers"]))
        self.assertEqual(kyoku["point_deltas"], [-1500, 1500, -1500, 500])
        self.assertEqual(kyoku["riichi_sticks_after"], 1)
        self.assertTrue(kyoku["dealer_continues"])
        self.assertEqual(kyoku["next_position"]["honba"], 1)

    def test_double_ron_honba_and_stick_to_nearest_winner(self):
        kyoku = self.x["kyokus"][2]
        self.assertEqual(kyoku["result"]["win"]["winner_seats"], [0, 3])
        self.assertEqual(kyoku["result"]["win"]["source_seat"], 2)
        self.assertEqual(
            sorted(
                (t["payer"], t["recipient"], t["amount"], t["reason"])
                for t in kyoku["transfers"]
            ),
            [(2, 0, 1600, "ron"), (2, 3, 300, "honba"), (2, 3, 1600, "ron")],
        )
        self.assertEqual(kyoku["riichi_stick_awards"], [{"amount": 1000, "seat": 3}])
        self.assertEqual(kyoku["point_deltas"], [1600, 0, -3500, 2900])
        self.assertEqual(
            (kyoku["riichi_sticks_before"], kyoku["riichi_sticks_after"]), (1, 0)
        )

    def test_abortive_draw_and_tsumo_with_honba(self):
        abortive, tsumo = self.x["kyokus"][3], self.x["kyokus"][4]
        self.assertEqual(
            abortive["result"],
            {"abortive_draw": {"reason": "four_winds"}, "kind": "abortive_draw"},
        )
        self.assertEqual(abortive["point_deltas"], [0, 0, 0, 0])
        self.assertTrue(abortive["dealer_continues"])
        self.assertEqual(tsumo["result"]["win"]["method"], "tsumo")
        self.assertIsNone(tsumo["result"]["win"]["source_seat"])
        self.assertEqual(
            sorted((t["payer"], t["amount"], t["reason"]) for t in tsumo["transfers"]),
            [
                (0, 100, "honba"),
                (0, 800, "tsumo"),
                (2, 100, "honba"),
                (2, 1600, "tsumo"),
                (3, 100, "honba"),
                (3, 800, "tsumo"),
            ],
        )
        self.assertEqual(tsumo["point_deltas"], [-900, 3500, -1700, -900])

    def test_final_kyoku_leaves_a_stick_for_the_final_award(self):
        final = self.x["kyokus"][-1]
        self.assertEqual(
            (final["prevailing_wind"], final["hand_number"], final["honba"]),
            ("south", 4, 4),
        )
        self.assertEqual(final["point_deltas"], [2000, -1000, -1000, -1000])
        self.assertEqual(final["points_after"], [25_200, 31_600, 15_700, 26_500])
        self.assertEqual(final["riichi_sticks_after"], 1)
        self.assertIsNone(final["next_position"])

    def test_bankruptcy_hanchan(self):
        row = self.y
        self.assertEqual(row["end_reason"], "bankruptcy")
        self.assertEqual(row["final_raw_scores"], [52_600, 25_000, 25_000, -2_600])
        self.assertEqual(
            [
                (s["rank"], s["base_points"], s["bankruptcy_points"], s["final_points"])
                for s in row["final_score"]
            ],
            [
                (1, 220, 100, 820),
                (2, -50, 0, 50),
                (3, -50, 0, -150),
                (4, -320, -100, -720),
            ],
        )
        self.assertEqual(len(row["kyokus"]), 8)
        self.assertEqual([k["honba"] for k in row["kyokus"]], list(range(8)))
        self.assertEqual(
            row["kyokus"][-1]["transfers"],
            [
                {
                    "amount": 2400,
                    "payer": 3,
                    "reason": "ron",
                    "recipient": 0,
                    "winner_seat": 0,
                },
                {
                    "amount": 2100,
                    "payer": 3,
                    "reason": "honba",
                    "recipient": 0,
                    "winner_seat": 0,
                },
            ],
        )

    def test_only_whitelisted_fields_are_stored(self):
        # 手牌・牌山・和了牌・ドラ表示牌・翻符役・Policy analysisのkeyを持たない
        keys = set()

        def collect(value):
            if isinstance(value, dict):
                keys.update(value)
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect([self.x, self.y])
        self.assertEqual(
            keys,
            {
                "abortive_draw", "amount", "bankruptcy_points", "base_points",
                "dealer_continues", "dealer_seat", "end_reason", "exhaustive_draw",
                "final_points", "final_raw_scores", "final_riichi_stick_awards",
                "final_score", "hand_number", "honba", "is_last_tile", "kind",
                "kyoku_index", "kyokus", "method", "nagashi_mangan_seats",
                "next_position", "oka_points", "origin", "payer", "placement",
                "point_deltas", "points", "points_after", "points_before",
                "policy_call_counts", "policy_identity", "prevailing_wind", "rank",
                "reason", "recipient", "result", "riichi_contributions",
                "riichi_stick_awards", "riichi_sticks", "riichi_sticks_after",
                "riichi_sticks_before", "role", "round_ordinal", "round_seed",
                "score", "seat", "seats", "seed", "selector_call_counts",
                "source_seat", "tenpai_seats", "transfers", "uma_points", "win",
                "winner_seat", "winner_seats",
            },
        )  # fmt: skip


class RowRejectionTest(unittest.TestCase):
    """同じvalidatorが作成時・読込時に改変を拒否する。"""

    @classmethod
    def setUpClass(cls):
        cls.base = _row(SCRIPT_X)

    def assert_rejected(self, mutate, pattern=None):
        row = copy.deepcopy(self.base)
        mutate(row)
        with self.assertRaisesRegex(KyokuRecordError, pattern or ""):
            validate_hanchan_row(row)

    def test_base_row_passes(self):
        validate_hanchan_row(copy.deepcopy(self.base))

    def test_unknown_and_missing_fields(self):
        self.assert_rejected(lambda r: r.update(extra=1), "unknown")
        self.assert_rejected(lambda r: r.pop("end_reason"), "missing")
        self.assert_rejected(lambda r: r["kyokus"][0].update(winning_tile=1), "unknown")
        self.assert_rejected(lambda r: r["kyokus"][0]["result"]["win"].update(han=2))

    def test_wrong_types(self):
        self.assert_rejected(lambda r: r.update(seed="513001"))
        self.assert_rejected(lambda r: r.update(placement=True))
        self.assert_rejected(lambda r: r["kyokus"][0].update(honba=0.0))
        self.assert_rejected(lambda r: r["final_score"][0].update(final_points=-140.0))
        self.assert_rejected(lambda r: r["kyokus"][0]["transfers"][0].update(payer=4))

    def test_k3_kyoku_order_and_round_seed(self):
        def swap(row):
            row["kyokus"][5], row["kyokus"][6] = row["kyokus"][6], row["kyokus"][5]

        self.assert_rejected(swap, "kyoku_index|K7")
        self.assert_rejected(lambda r: r["kyokus"].pop(3), "kyoku_index|K7")
        self.assert_rejected(lambda r: r["kyokus"].pop(), "K7")

        def duplicate(row):
            row["kyokus"].insert(4, copy.deepcopy(row["kyokus"][3]))

        self.assert_rejected(duplicate, "kyoku_index|K7|K3")
        self.assert_rejected(lambda r: r.update(kyokus=[]), "K3")
        self.assert_rejected(
            lambda r: r["kyokus"][2].update(
                round_seed=r["kyokus"][2]["round_seed"] + 1
            ),
            "round_seed",
        )

    def test_k4_k5_k8_settlement_tampering(self):
        def delta(row):
            row["kyokus"][0]["point_deltas"][1] += 100
            row["kyokus"][0]["points_after"][1] += 100

        self.assert_rejected(delta)
        self.assert_rejected(
            lambda r: r["kyokus"][0]["points_after"].__setitem__(0, 24_100),
            "points_before_kyoku",
        )

        def transfer(row):
            row["kyokus"][0]["transfers"][0]["amount"] = 1500

        self.assert_rejected(transfer, "transfers")
        self.assert_rejected(lambda r: r["kyokus"][0].update(riichi_contributions=[]))
        self.assert_rejected(lambda r: r["kyokus"][2].update(riichi_stick_awards=[]))

    def test_k6_k7_continuity(self):
        def start(row):
            row["kyokus"][0]["points_before"] = [24_000, 26_000, 25_000, 25_000]
            row["kyokus"][0]["points_after"] = [23_000, 28_600, 23_400, 25_000]

        self.assert_rejected(start, "K6")

        def gap(row):
            row["kyokus"][4]["points_before"][0] += 100
            row["kyokus"][4]["points_after"][0] += 100

        self.assert_rejected(gap, "continue")
        self.assert_rejected(
            lambda r: r["kyokus"][3]["next_position"].update(honba=2),
            "K7|next_position",
        )
        self.assert_rejected(
            lambda r: r["kyokus"][-1].update(
                next_position=r["kyokus"][0]["next_position"]
            ),
            "K7",
        )

    def test_k9_dealer_continuation(self):
        self.assert_rejected(
            lambda r: r["kyokus"][1].update(dealer_continues=False), "K9|dealer"
        )

    def test_k10_final_values(self):
        self.assert_rejected(
            lambda r: r["final_raw_scores"].__setitem__(1, 32_500), "C3|C5"
        )
        self.assert_rejected(lambda r: r.update(final_riichi_stick_awards=[]), "C3")

        def points(row):
            row["final_score"][1]["final_points"] += 10
            row["final_score"][0]["final_points"] -= 10

        self.assert_rejected(points, "C2")

        def rank(row):
            row["final_score"][0]["rank"], row["final_score"][3]["rank"] = 2, 3

        self.assert_rejected(rank, "C6")
        self.assert_rejected(lambda r: r.update(end_reason="bankruptcy"), "C9")
        self.assert_rejected(lambda r: r.update(end_reason="manual"), "end_reason")

    def test_k11_result_shape(self):
        def mixed(row):
            row["kyokus"][0]["result"]["exhaustive_draw"] = {
                "nagashi_mangan_seats": [],
                "tenpai_seats": [],
            }

        self.assert_rejected(mixed)
        self.assert_rejected(
            lambda r: r["kyokus"][0]["result"]["win"].update(source_seat=1),
            "source_seat",
        )
        self.assert_rejected(
            lambda r: r["kyokus"][4]["result"]["win"].update(source_seat=0), "tsumo"
        )

    def test_k12_call_counts(self):
        self.assert_rejected(
            lambda r: r["policy_call_counts"].__setitem__(2, 41), "K12"
        )

    def test_k2_roles_and_identities(self):
        self.assert_rejected(lambda r: r.update(placement=1), "K2")
        self.assert_rejected(
            lambda r: r["seats"][1].update(policy_identity="other"), "K2"
        )

        def same(row):
            for seat in row["seats"]:
                seat["policy_identity"] = CANDIDATE_IDENTITY

        self.assert_rejected(same, "K2")


class ScheduleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, _, _ = build_records(
            [capture(7, p, SCRIPT_Y) for p in (0, 1)], event_facts_required=True
        )
        cls.rows = rows

    def expected(self, keys):
        return [ExpectedHanchan(seed, p, seats_for(p)) for seed, p in keys]

    def test_exact_schedule_passes(self):
        verify_schedule(self.rows, self.expected([(7, 0), (7, 1)]))

    def test_missing_duplicate_order_and_unexpected(self):
        with self.assertRaisesRegex(KyokuRecordError, "missing"):
            verify_schedule(self.rows, self.expected([(7, 0), (7, 1), (7, 2)]))
        with self.assertRaisesRegex(KyokuRecordError, "duplicated"):
            verify_schedule(self.rows + self.rows[:1], self.expected([(7, 0), (7, 1)]))
        with self.assertRaisesRegex(KyokuRecordError, "order"):
            verify_schedule(self.rows[::-1], self.expected([(7, 0), (7, 1)]))
        with self.assertRaisesRegex(KyokuRecordError, "unexpected"):
            verify_schedule(self.rows, self.expected([(7, 0)]))

    def test_identity_mismatch(self):
        seats = list(seats_for(1))
        seats[0] = SeatAssignment("A", "different-candidate")
        expected = [
            ExpectedHanchan(7, 0, seats_for(0)),
            ExpectedHanchan(7, 1, tuple(seats)),
        ]
        with self.assertRaisesRegex(KyokuRecordError, "K2"):
            verify_schedule(self.rows, expected)

    def test_expected_schedule_must_follow_the_placement_table(self):
        with self.assertRaisesRegex(KyokuRecordError, "placement table"):
            verify_schedule(self.rows, [ExpectedHanchan(7, 0, seats_for(1))])

    def test_duplicate_hanchan_is_rejected_on_build(self):
        with self.assertRaisesRegex(KyokuRecordError, "more than once"):
            build_records(
                [capture(7, 0, SCRIPT_Y), capture(7, 0, SCRIPT_Y)],
                event_facts_required=True,
            )

    def test_match_seed_must_match_the_record_seed(self):
        wrong = dataclasses.replace(capture(8, 0, SCRIPT_Y), seed=9)
        with self.assertRaisesRegex(KyokuRecordError, "match seed"):
            build_records([wrong], event_facts_required=True)


class StoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.captures = [capture(11, 0, SCRIPT_X), capture(11, 1, SCRIPT_Y)]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.path = self.tmp / "record"

    def write(self, **kwargs):
        return write_kyoku_records(
            self.path,
            self.captures,
            producer_revision=PRODUCER_REVISION,
            event_facts_required=kwargs.pop("event_facts_required", True),
            **kwargs,
        )

    def test_write_read_roundtrip(self):
        bindings = RecordBindings(lock_digest="1" * 64, environment_digest="2" * 64)
        written = self.write(bindings=bindings)
        read = read_kyoku_records(self.path, event_facts_required=True)
        self.assertEqual(written, read)
        self.assertEqual(len(read.hanchans), 2)
        self.assertTrue(read.event_fact_status.valid)
        self.assertEqual(read.producer_revision, PRODUCER_REVISION)
        require_bindings(read, bindings)
        with self.assertRaises(KyokuRecordError):
            require_bindings(read, RecordBindings(lock_digest="3" * 64))
        self.assertEqual(
            sorted(child.name for child in self.path.iterdir()),
            ["event-facts.jsonl", "kyoku-records.jsonl", "manifest.json"],
        )
        manifest = json.loads((self.path / MANIFEST_FILENAME).read_text("utf-8"))
        self.assertEqual(manifest["kyoku_records"]["rows"], 2)
        self.assertNotIn("PASS", json.dumps(manifest))

    def test_write_once(self):
        self.write()
        with self.assertRaises(FileExistsError):
            self.write()

    def test_failed_build_publishes_nothing(self):
        bad = list(self.captures) + [self.captures[0]]
        with self.assertRaises(KyokuRecordError):
            write_kyoku_records(
                self.path,
                bad,
                producer_revision=PRODUCER_REVISION,
                event_facts_required=True,
            )
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_invalid_producer_revision(self):
        with self.assertRaises(KyokuRecordError):
            write_kyoku_records(
                self.path,
                self.captures,
                producer_revision="main",
                event_facts_required=True,
            )

    def _rewrite_kyoku_file(self, mutate_text, *, fix_manifest):
        self.write()
        payload = self.path / KYOKU_RECORD_FILENAME
        text = mutate_text(payload.read_text("utf-8"))
        payload.write_text(text, encoding="utf-8", newline="\n")
        if fix_manifest:
            import hashlib

            manifest_path = self.path / MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text("utf-8"))
            data = text.encode("utf-8")
            manifest["kyoku_records"].update(
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                rows=text.count("\n"),
            )
            manifest["hanchan_count"] = text.count("\n")
            from lisjong_arena._artifact_io import canonical_json_text

            manifest_path.write_text(
                canonical_json_text(manifest), encoding="utf-8", newline="\n"
            )

    def test_hash_mismatch_is_rejected(self):
        self._rewrite_kyoku_file(
            lambda t: t.replace("25200", "25300", 1), fix_manifest=False
        )
        with self.assertRaisesRegex(KyokuRecordError, "sha256"):
            read_kyoku_records(self.path, event_facts_required=False)

    def test_value_tampering_with_matching_hash_is_rejected(self):
        # hashを付け直しても、作成時と同じrow検査で拒否される
        self._rewrite_kyoku_file(
            lambda t: t.replace("25200", "25300", 1), fix_manifest=True
        )
        with self.assertRaises(KyokuRecordError):
            read_kyoku_records(self.path, event_facts_required=False)

    def test_dropped_hanchan_with_matching_hash_fails_the_schedule(self):
        self._rewrite_kyoku_file(lambda t: t.split("\n")[0] + "\n", fix_manifest=True)
        read = read_kyoku_records(self.path, event_facts_required=False)
        # 欠落は期待scheduleとの照合（K1）で拒否される
        with self.assertRaisesRegex(KyokuRecordError, "K1: missing"):
            verify_schedule(
                read.hanchans,
                [ExpectedHanchan(11, p, seats_for(p)) for p in (0, 1)],
            )
        # 第2層は第1層と行数が合わないので無効になり、欠損を0件に見せない
        self.assertFalse(read.event_fact_status.valid)
        self.assertIsNone(read.event_facts)

    def test_unknown_schema_and_fields_in_manifest(self):
        self.write()
        manifest_path = self.path / MANIFEST_FILENAME
        original = json.loads(manifest_path.read_text("utf-8"))
        from lisjong_arena._artifact_io import canonical_json_text

        def rewrite(manifest):
            manifest_path.write_text(
                canonical_json_text(manifest), encoding="utf-8", newline="\n"
            )

        rewrite(original)
        read_kyoku_records(self.path, event_facts_required=True)
        for mutate, pattern in (
            (
                lambda m: m.update(
                    schema="arena-heuristic-candidate-engine-aabb-record-manifest-v2"
                ),
                "schema",
            ),
            (lambda m: m.update(extra=True), "fields"),
            (lambda m: m["kyoku_records"].update(schema="other"), "schema"),
            (lambda m: m["kyoku_records"].update(rows=3), "rows"),
            (lambda m: m.update(hanchan_count=3), "rows"),
            (
                lambda m: m.update(protocol="arena-heuristic-candidate-aabb-half-v1"),
                "protocol",
            ),
            (lambda m: m["rules"].update(version=2), "rules"),
            (lambda m: m["producer"].update(arena_revision="x"), "revision"),
            (lambda m: m["bindings"].update(lock_digest="short"), "digest"),
            (lambda m: m["kyoku_records"].update(rows="2"), "rows"),
        ):
            manifest = copy.deepcopy(original)
            mutate(manifest)
            rewrite(manifest)
            with self.assertRaisesRegex(KyokuRecordError, pattern):
                read_kyoku_records(self.path, event_facts_required=False)

    def test_unexpected_file_is_rejected(self):
        self.write()
        (self.path / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(KyokuRecordError, "unexpected"):
            read_kyoku_records(self.path, event_facts_required=False)

    def test_non_canonical_line_is_rejected(self):
        self._rewrite_kyoku_file(
            lambda t: t.replace('{"end_reason"', '{ "end_reason"', 1), fix_manifest=True
        )
        with self.assertRaisesRegex(KyokuRecordError, "canonical"):
            read_kyoku_records(self.path, event_facts_required=False)


if __name__ == "__main__":
    unittest.main()
