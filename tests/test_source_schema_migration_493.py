"""Development-only round trips for the #493 producer wire migration."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_generate_hand_belief_source_255 as hand_fixture
import test_generate_riichi_deal_in_source_237 as riichi_fixture
import test_policy_source_record as policy_fixture
import test_ron_legal_source_producer as ron_fixture
from lisjong.learning import hand_belief_source, riichi_deal_in_source, ron_legal_source
from lisjong.learning.errors import SourceRecordError
from lisjong.learning.source_record import read_source_record as read_lisjong_source
from lisjong.policy_contract import (
    DecisionContext,
    Discard,
    DiscardAction,
    RiichiState,
)

from lisjong_arena.offense_foundation import source_record as offense
from lisjong_arena.offense_foundation.qualification import seal, unseal
from lisjong_arena.policy_source_record import (
    archive,
    binding,
    generation,
    record,
    replay,
)


def declared(view, seat):
    players = list(view.players)
    player = players[seat]
    players[seat] = replace(
        player,
        riichi=RiichiState.ACCEPTED,
        discards=player.discards
        or (Discard(hand_fixture.tiles("4m")[0], False, 0, None),),
        riichi_declaration_discard_order=player.discards[-1].order
        if player.discards
        else 0,
    )
    return replace(view, players=tuple(players))


class MigrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def policy_source(self, name, *, with_order, schema):
        view = policy_fixture.policy_input("123m456p789s1122z5z")
        if with_order:
            view = declared(view, 1)
        action = DiscardAction(view.self_seat, view.own_hand.concealed_tiles[0], False)
        observation = policy_fixture.observe(
            "two-step", DecisionContext(view, (action,))
        )
        result = SimpleNamespace(
            seed=11, game_mode=record.GAME_MODE, decisions=1, steps=1
        )
        inspection = SimpleNamespace(
            step_observations=(
                SimpleNamespace(
                    step_ordinal=0,
                    seat_decisions=(observation,),
                ),
            )
        )
        population = record.population_document(record.DEVELOPMENT, {"TRAIN": [11]})
        with (
            patch.object(
                binding, "runtime_binding", return_value=policy_fixture.RUNTIME
            ),
            patch.object(
                generation, "run_recorded_game", return_value=(result, inspection)
            ),
        ):
            manifest = generation.generate(
                population, self.root / name, teacher="two-step", schema=schema
            )
        return self.root / name, manifest, view, observation

    def test_policy_v2_survives_both_readers_replay_and_archive(self):
        path, manifest, view, _ = self.policy_source(
            "new", with_order=True, schema=record.SCHEMA_V2
        )
        self.assertEqual(record.read_source_record(path), manifest)
        self.assertEqual(next(read_lisjong_source(path).decisions()).policy_input, view)
        with patch.object(
            binding, "runtime_binding", return_value=policy_fixture.RUNTIME
        ):
            evidence = replay.replay_verify(path)
        archive.archive_source_record(path, self.root / "archive", replay=evidence)
        archive.restore_source_record(
            self.root / "archive",
            self.root / "restored",
            expected_identity=manifest["identity"],
        )
        self.assertEqual(
            next(read_lisjong_source(self.root / "restored").decisions()).policy_input,
            view,
        )

    def test_policy_v1_bytes_and_explicit_rejection_are_preserved(self):
        path, manifest, _, observation = self.policy_source(
            "old", with_order=False, schema=record.SCHEMA
        )
        expected = offense.encode_observation(
            observation,
            game_ordinal=0,
            split="TRAIN",
            seed=11,
            step_ordinal=0,
            decision_ordinal=0,
        )
        self.assertEqual(
            (path / "game-000" / record.SOURCE_FILENAME).read_bytes(),
            offense._canonical_line(expected).encode(),
        )
        self.assertEqual(record.read_source_record(path), manifest)
        read_lisjong_source(path)
        with self.assertRaises(ValueError):
            self.policy_source("refused", with_order=True, schema=record.SCHEMA)
        # The standalone O0 encoder still rejects supplied declaration positions.
        _, _, _, marked = self.policy_source(
            "marked", with_order=True, schema=record.SCHEMA_V2
        )
        with self.assertRaises(ValueError):
            offense.encode_observation(
                marked,
                game_ordinal=0,
                split="TRAIN",
                seed=11,
                step_ordinal=0,
                decision_ordinal=0,
            )

    def test_policy_schemas_mutually_reject_payloads(self):
        for written, relabelled in (
            (record.SCHEMA, record.SCHEMA_V2),
            (record.SCHEMA_V2, record.SCHEMA),
        ):
            path, manifest, _, _ = self.policy_source(
                written, with_order=False, schema=written
            )
            body = unseal(manifest)
            body["schema"] = relabelled
            (path / "manifest.json").write_bytes(
                (
                    json.dumps(
                        seal(body),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n"
                ).encode()
            )
            with self.assertRaises(record.PolicySourceRecordError):
                record.read_source_record(path)
            with self.assertRaises(SourceRecordError):
                read_lisjong_source(path)

    def test_hand_belief_producer_preserves_orders_through_labelled_read(self):
        hands = (
            *hand_fixture.HANDS[:3],
            hand_fixture.generator.SeatHand(
                hand_fixture.tiles("2345p667889s"), (hand_fixture.PON,)
            ),
        )
        events = [
            replace(
                event,
                decision=replace(
                    event.decision, input=declared(event.decision.input, 1)
                ),
            )
            for event in hand_fixture._events(hands)
        ]
        rows, facts = hand_fixture.generator.extract_records(hand_fixture.SEED, events)
        output = self.root / "hand"
        hand_fixture.generator.write_source(
            output,
            splits={"train": [hand_fixture.SEED], "valid": [], "test": []},
            games=[(hand_fixture.SEED, rows, facts, hand_fixture.SCOPE_KEYS)],
            producer={
                "arena_revision": "a",
                "lisjong_engine_revision": "b",
                "lisjong_revision": "c",
                "policy": "fixture",
            },
        )
        _, decisions = hand_belief_source.read_decisions(output)
        _, labelled = hand_belief_source.read_labelled_source(output)
        self.assertEqual(decisions[0].policy_input, events[0].decision.input)
        self.assertEqual(labelled[0].decision, decisions[0])
        self.assertTrue(
            all(row["schema"] == hand_belief_source.DECISION_SCHEMA_V2 for row in rows)
        )

    def test_riichi_deal_in_producer_preserves_orders_through_labelled_read(self):
        context, action = riichi_fixture.s_discards()
        view = replace(
            context.input,
            own_hand=replace(
                context.input.own_hand,
                concealed_tiles=riichi_fixture.tiles("134m456p789s1122z5z"),
            ),
        )
        context = replace(context, input=declared(view, 1))
        rows, facts = riichi_fixture.generator.extract_records(
            931000, [riichi_fixture.r_declares(), (context, action)], {}
        )
        output = self.root / "riichi"
        riichi_fixture.generator.write_source(
            output,
            splits={"train": [931000], "valid": [], "test": []},
            games=[(931000, rows, facts)],
            producer={
                "arena_revision": "a",
                "lisjong_engine_revision": "b",
                "lisjong_revision": "c",
                "policy": "fixture",
            },
        )
        _, decisions = riichi_deal_in_source.read_decisions(output)
        _, labelled = riichi_deal_in_source.read_labelled_source(output)
        self.assertEqual(decisions[0].policy_input, context.input)
        self.assertEqual(labelled[0].decision, decisions[0])
        self.assertEqual(rows[0]["schema"], riichi_deal_in_source.DECISION_SCHEMA_V2)

    def test_engine_ron_checkpoint_writer_preserves_supplied_positions(self):
        recorder, _ = ron_fixture.fixture(riichi=True)
        # The live Adapter intentionally supplies None in this migration stage.
        for row in recorder.history:
            self.assertEqual(row["schema"], ron_legal_source.HISTORY_SCHEMA_V2)
        value = next(
            step["checkpoint"]
            for row in recorder.history
            for step in row["steps"]
            if any(
                p["riichi"] == "accepted"
                for p in step["checkpoint"]["views"][0]["players"]
            )
        )
        checkpoint = ron_legal_source._checkpoint(
            value, "development", schema=ron_legal_source.HISTORY_SCHEMA_V2
        )
        marked = tuple(
            replace(
                view,
                players=tuple(
                    replace(p, riichi_declaration_discard_order=p.discards[-1].order)
                    if p.riichi is RiichiState.ACCEPTED
                    else p
                    for p in view.players
                ),
            )
            for view in checkpoint.views
        )
        written = ron_fixture.producer.checkpoint_to_value(
            marked, copy.deepcopy(value["contexts"])
        )
        path = self.root / "checkpoint.json"
        path.write_bytes(json.dumps(written).encode())
        read = ron_legal_source._checkpoint(
            json.loads(path.read_bytes()),
            "saved",
            schema=ron_legal_source.HISTORY_SCHEMA_V2,
        )
        self.assertEqual(read.views, marked)
        self.assertTrue(
            any(
                p.riichi_declaration_discard_order is not None
                for p in read.views[0].players
            )
        )
