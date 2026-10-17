"""lisjong-arena #493: durable local game record schema version 3（リーチ宣言牌の位置）。

新規保存の既定はversion 3。明示version 2のbyte列と位置の拒否を維持する。
読み込みは記録のschema versionに応じて厳密に行う。
"""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from test_durable_local_game_record import (
    _identities,
    _inspection,
    _provenance,
    _refresh_payload_reference,
    _rewrite_json,
)
from test_durable_local_game_record_declaration_order import _with_declaration

from lisjong_arena.durable_local_game_record import (
    DECISIONS_FILENAME,
    DEFAULT_LOCAL_GAME_RECORD_SCHEMA_VERSION,
    LOCAL_GAME_RECORD_SCHEMA_VERSION,
    LOCAL_GAME_RECORD_SCHEMA_VERSION_WITH_RIICHI_DECLARATION,
    MANIFEST_FILENAME,
    DurableLocalGameRecordError,
    load_local_game_record,
    save_local_game_record,
)

FIELD = "riichi_declaration_discard_order"
V2 = LOCAL_GAME_RECORD_SCHEMA_VERSION
V3 = LOCAL_GAME_RECORD_SCHEMA_VERSION_WITH_RIICHI_DECLARATION


def _inspection_with_declaration():
    """step 0のseat 1の判断で、seat 3がorder 0の宣言牌でリーチしている記録。"""
    inspection = _inspection()
    step = inspection.step_observations[0]
    decisions = list(step.seat_decisions)
    decisions[1] = replace(
        decisions[1], policy_input=_with_declaration(decisions[1].policy_input, 3)
    )
    steps = list(inspection.step_observations)
    steps[0] = replace(step, seat_decisions=tuple(decisions))
    return replace(inspection, step_observations=tuple(steps))


def _first_players(decisions):
    return decisions["steps"][0]["seat_decisions"][1]["policy_input"]["players"]


class _RecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _save(self, inspection, *, name="record", **kwargs):
        path = self.root / name
        record = save_local_game_record(
            inspection,
            path,
            policy_identities=_identities(),
            max_steps=10,
            provenance=_provenance(),
            **kwargs,
        )
        return path, record

    def _decisions(self, path):
        return json.loads((path / DECISIONS_FILENAME).read_text(encoding="utf-8"))

    def _edit_decisions(self, path, edit):
        _rewrite_json(path / DECISIONS_FILENAME, edit)
        _refresh_payload_reference(path, "decisions")


class SchemaVersion3RoundTripTest(_RecordTest):
    def test_round_trip_keeps_declaration_orders(self) -> None:
        inspection = _inspection_with_declaration()

        path, saved = self._save(inspection)
        loaded = load_local_game_record(path)

        self.assertEqual(DEFAULT_LOCAL_GAME_RECORD_SCHEMA_VERSION, V3)
        self.assertEqual(saved.schema_version, V3)
        self.assertEqual(loaded.schema_version, V3)
        self.assertEqual(loaded.inspection, inspection)
        players = (
            loaded.inspection.step_observations[0]
            .seat_decisions[1]
            .policy_input.players
        )
        self.assertEqual(players[3].riichi_declaration_discard_order, 0)
        manifest = json.loads((path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], V3)

    def test_default_and_explicit_version_3_write_identical_bytes(self) -> None:
        inspection = _inspection_with_declaration()
        default_path, _ = self._save(inspection, name="default")
        explicit_path, _ = self._save(inspection, name="explicit", schema_version=V3)
        self.assertEqual(
            {item.name: item.read_bytes() for item in default_path.iterdir()},
            {item.name: item.read_bytes() for item in explicit_path.iterdir()},
        )

    def test_every_player_carries_the_field_in_version_3(self) -> None:
        path, _ = self._save(_inspection_with_declaration(), schema_version=V3)

        for step in self._decisions(path)["steps"]:
            for decision in step["seat_decisions"]:
                for player in decision["policy_input"]["players"]:
                    self.assertIn(FIELD, player)
        self.assertEqual(
            [p[FIELD] for p in _first_players(self._decisions(path))],
            [None, None, None, 0],
        )

    def test_version_3_also_stores_inputs_without_orders(self) -> None:
        path, _ = self._save(_inspection())

        loaded = load_local_game_record(path)

        self.assertEqual(loaded.inspection, _inspection())
        self.assertEqual(loaded.schema_version, V3)


class SchemaVersion2Test(_RecordTest):
    def test_explicit_version_2_keeps_the_old_output(self) -> None:
        path, saved = self._save(_inspection(), schema_version=V2)

        manifest = json.loads((path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], V2)
        self.assertEqual(saved.schema_version, V2)
        for player in _first_players(self._decisions(path)):
            self.assertEqual(set(player), {"discards", "melds", "riichi", "score"})

    def test_explicit_version_2_keeps_the_pre_switch_bundle_bytes(self) -> None:
        # Captured from #500 main before changing the default; fixed provenance.
        expected = {
            "decisions.json": "4a633b288fd1b041d9395bcd7915e1e84313f8d8be11384b1fea40cbe14fb76e",
            "manifest.json": "f4b523c72bdbca5100fe92f649f6209c5ca0050fe6d484155a17a72cc008e1ac",
            "objective_trace.json": "31a63f547b9fb1b70906137c7b3183452cbd1044e76420dd2a4f92ecfb697df6",
            "result.json": "f05a2e77810e7cf3988dcf31fd6aa331c7b67b4655579e56c0f771f3bee38f20",
            "round_results.json": "dc76e4b2eff674c927b6212b3bf56eb510acefdc4316487a192bd38add931f58",
        }
        path, _ = self._save(_inspection(), schema_version=V2)
        self.assertEqual(
            {
                item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                for item in path.iterdir()
            },
            expected,
        )

    def test_version_2_load_restores_none(self) -> None:
        path, _ = self._save(_inspection(), schema_version=V2)

        loaded = load_local_game_record(path)

        for step in loaded.inspection.step_observations:
            for decision in step.seat_decisions:
                for player in decision.policy_input.players:
                    self.assertIsNone(player.riichi_declaration_discard_order)

    def test_version_2_rejects_an_input_with_a_value_and_does_not_switch(self) -> None:
        path = self.root / "record"

        with self.assertRaises(DurableLocalGameRecordError):
            save_local_game_record(
                _inspection_with_declaration(),
                path,
                policy_identities=_identities(),
                max_steps=10,
                provenance=_provenance(),
                schema_version=V2,
            )

        self.assertFalse(path.exists())


class StrictReadTest(_RecordTest):
    def test_version_3_rejects_a_player_without_the_field(self) -> None:
        path, _ = self._save(_inspection_with_declaration(), schema_version=V3)
        self._edit_decisions(path, lambda value: _first_players(value)[2].pop(FIELD))

        with self.assertRaises(DurableLocalGameRecordError):
            load_local_game_record(path)

    def test_version_2_rejects_a_player_with_the_field(self) -> None:
        path, _ = self._save(_inspection(), schema_version=V2)

        def add(value):
            _first_players(value)[0][FIELD] = None

        self._edit_decisions(path, add)

        with self.assertRaises(DurableLocalGameRecordError):
            load_local_game_record(path)

    def test_version_3_rejects_non_integer_orders(self) -> None:
        for bad in (True, 0.0, "0"):
            with self.subTest(bad=bad):
                path, _ = self._save(
                    _inspection_with_declaration(),
                    name=f"record-{type(bad).__name__}",
                    schema_version=V3,
                )

                def set_bad(value, bad=bad):
                    _first_players(value)[3][FIELD] = bad

                self._edit_decisions(path, set_bad)
                with self.assertRaises(DurableLocalGameRecordError):
                    load_local_game_record(path)

    def test_version_3_rejects_values_the_contract_rejects(self) -> None:
        cases = {
            "missing-discard": lambda players: players[3].__setitem__(FIELD, 9),
            "not-in-riichi": lambda players: players[0].__setitem__(FIELD, 0),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                path, _ = self._save(
                    _inspection_with_declaration(), name=name, schema_version=V3
                )
                self._edit_decisions(path, lambda value: mutate(_first_players(value)))
                with self.assertRaises(DurableLocalGameRecordError):
                    load_local_game_record(path)

    def test_version_3_rejects_unknown_player_fields(self) -> None:
        path, _ = self._save(_inspection_with_declaration(), schema_version=V3)

        def add(value):
            _first_players(value)[1]["extra"] = 1

        self._edit_decisions(path, add)

        with self.assertRaises(DurableLocalGameRecordError):
            load_local_game_record(path)

    def test_manifest_version_decides_the_parser(self) -> None:
        # version 3の中身を持つ記録のmanifestをversion 2へ書き換えると、読めない。
        path, _ = self._save(_inspection_with_declaration(), schema_version=V3)

        def downgrade(manifest):
            manifest["schema_version"] = V2

        _rewrite_json(path / MANIFEST_FILENAME, downgrade)
        # manifestのrecord_identityを再計算し、identityの不一致で止まらないようにする。
        _refresh_payload_reference(path, "decisions")

        with self.assertRaises(DurableLocalGameRecordError) as raised:
            load_local_game_record(path)

        # identityの不一致ではなく、version 2のparserが宣言牌の位置のfieldを持つplayerを
        # 受理しないことで拒否された。loaderは具体的な理由を原因として連鎖させる。
        messages = []
        error = raised.exception
        while error is not None:
            messages.append(str(error))
            error = error.__cause__ or error.__context__
        self.assertNotIn("record identity mismatch", messages)
        self.assertIn(
            "decisions.steps[0].seat_decisions[0].policy_input.players[0] "
            "fields are invalid",
            messages,
        )


class SchemaVersionSelectionTest(_RecordTest):
    def test_rejects_unsupported_schema_versions(self) -> None:
        for bad, error in (
            (1, ValueError),
            (4, ValueError),
            ("3", TypeError),
            (True, TypeError),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(error):
                    self._save(_inspection(), name=f"bad-{bad!r}", schema_version=bad)

    def test_version_1_records_stay_unreadable(self) -> None:
        path, _ = self._save(_inspection())

        def to_v1(manifest):
            manifest["schema_version"] = 1

        _rewrite_json(path / MANIFEST_FILENAME, to_v1)

        with self.assertRaisesRegex(DurableLocalGameRecordError, "schema version 1"):
            load_local_game_record(path)

    def test_seat_of_the_rejected_value_is_reported_in_version_2(self) -> None:
        with self.assertRaisesRegex(
            DurableLocalGameRecordError, rf"players\[3\]\.{FIELD}"
        ):
            self._save(_inspection_with_declaration(), schema_version=V2)


if __name__ == "__main__":
    unittest.main()
