"""Synthetic, fixed-wall engine fixtures; never a retained pilot population."""

import importlib.util
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from lisjong.policy_contract import (
    AnkanAction,
    DiscardAction,
    PassAction,
    RiichiAction,
    TsumoAction,
)
from lisjong_engine.driver import _advance_round, _round_start_observation
from lisjong_engine.match_state import MatchState
from lisjong_engine.round_phase import RoundPhase
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat
from lisjong_engine.tile import STANDARD_TILES
from lisjong_engine.wall import Wall

from lisjong_arena import seed_registry
from lisjong_arena.lisjong_engine.policy_selector import build_seat_selectors

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "generate_ron_legal_source_457.py"
)
spec = importlib.util.spec_from_file_location("ron_producer_457", SCRIPT)
producer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = producer
spec.loader.exec_module(producer)


class FixturePolicy:
    def __init__(self, riichi=False, ankan=False):
        self.riichi = riichi
        self.ankan = ankan
        self.inputs = []

    def choose_action(self, decision):
        self.inputs.append(decision)
        kinds = [AnkanAction] if self.ankan else []
        kinds += [TsumoAction, RiichiAction] if self.riichi else [TsumoAction]
        for kind in kinds:
            for action in decision.legal_actions:
                if isinstance(action, kind):
                    return action
        for action in decision.legal_actions:
            if isinstance(action, PassAction):
                return action
        discards = [a for a in decision.legal_actions if isinstance(a, DiscardAction)]
        if not discards:
            raise AssertionError("fixture lacks a discard or pass")
        return next((a for a in discards if a.tsumogiri), discards[0])


def fixed_wall(layout=None):
    # Physical deterministic fixture: 4 round-robin hands, then the rest of
    # STANDARD_TILES. No MatchState-derived shuffle or new population seed.
    hands = [list(STANDARD_TILES[i::4][:13]) for i in range(4)]
    if layout is not None:
        pool = list(STANDARD_TILES)

        def take(kind):
            found = next(t for t in pool if t.tile_type.id == kind)
            pool.remove(found)
            return found

        east = [0] * 3 + [1] * 3 + [2] * 3 + [12] * 3 + [31]
        first = 14
        if layout == "ankan":
            east = [0] * 3 + [11] * 3 + [22] * 3 + [31] * 3 + [27]
            first = 0
        hands = [[take(kind) for kind in east]]
        draw = take(first)
        for _ in range(3):
            hands.append([pool.pop(0) for _ in range(13)])
        pool.insert(0, draw)
    used = {t.id for hand in hands for t in hand}
    dealt = []
    for block in range(3):
        for hand in hands:
            dealt.extend(hand[block * 4 : block * 4 + 4])
    dealt.extend(hand[12] for hand in hands)
    remainder = (
        [t for t in STANDARD_TILES if t.id not in used] if layout is None else pool
    )
    return Wall(dealt + remainder[:-14], remainder[-14:])


def fixture(riichi=False, ankan=False):
    match = MatchState(seed=0)
    with patch(
        "lisjong_engine.match_state.create_round_wall",
        return_value=fixed_wall("ankan" if ankan else "riichi" if riichi else None),
    ):
        state = match.start_round()
    state.enable_transaction_observation()
    recorder = producer.Recorder(0, match)
    recorder.observe(_round_start_observation(match, state))
    policies = [FixturePolicy(riichi, ankan) for _ in Seat]
    selectors = build_seat_selectors(
        {s: recorder.wrap(policies[i]) for i, s in enumerate(Seat)}
    )
    while state.phase is not RoundPhase.FINISHED:
        _advance_round(
            match, state, selectors, on_transaction_observation=recorder.observe
        )
    return recorder, policies


class ProjectionTests(unittest.TestCase):
    def test_fixed_wall_covers_commit_boundaries_and_hidden_data_stays_off_policy(self):
        recorder, policies = fixture()
        game, facts, history, coverage = recorder.finish()
        self.assertEqual(coverage[0]["selectors"], len(recorder.events))
        self.assertEqual(coverage[0]["decisions"], len(game[1]))
        self.assertEqual(coverage[0]["transitions"], len(history))
        self.assertEqual([r["index"] for r in history], list(range(len(history))))
        self.assertEqual(history[-1]["steps"][0]["event"]["kind"], "round_end")
        for fact in facts:
            row = history[fact["history_boundary"]]
            self.assertEqual(
                row["steps"][0]["event"]["sequence"], fact["key"]["sequence"]
            )
            previous = history[fact["history_boundary"] - 1]["steps"][-1]["checkpoint"]
            decision = game[1][[r["key"] for r in game[1]].index(fact["key"])]
            self.assertEqual(
                previous["views"][fact["key"]["seat"]], decision["policy_input"]
            )
        for policy in policies:
            self.assertTrue(policy.inputs)
            for decision in policy.inputs:
                self.assertFalse(hasattr(decision, "checkpoint"))
                self.assertFalse(hasattr(decision.input, "contexts"))

    def test_missing_or_repeated_transactions_and_incomplete_game_rejected(self):
        recorder, _ = fixture()
        with self.assertRaises(producer.RonProducerError):
            recorder.observe(
                type("Transaction", (), {"steps": [], "round_ordinal": 1})()
            )
        recorder.terminal = False
        with self.assertRaises(producer.RonProducerError):
            recorder.finish()
        match = MatchState(seed=0)
        with patch(
            "lisjong_engine.match_state.create_round_wall", return_value=fixed_wall()
        ):
            state = match.start_round()
        state.enable_transaction_observation()
        watcher = producer.Recorder(0, match)
        started = _round_start_observation(match, state)
        watcher.observe(started)
        state.draw(Seat.EAST)
        from lisjong_engine.transaction_observation import TransactionObservation

        draw = TransactionObservation(
            round_ordinal=1,
            revision=state.revision,
            phase=state.phase,
            current_seat=state.current_seat,
            steps=state.last_transaction_steps,
            selector_decision=None,
        )
        with self.assertRaises(producer.RonProducerError):
            watcher.observe(replace(draw, revision=draw.revision + 1))
        watcher.observe(draw)
        with self.assertRaises(producer.RonProducerError):
            watcher.observe(draw)

    def test_ankan_declaration_confirmation_and_rinshan_stay_in_commit_order(self):
        recorder, _ = fixture(ankan=True)
        events = [s["event"] for row in recorder.history for s in row["steps"]]
        declarations = [
            e
            for e in events
            if e["kind"] == "progress" and e["action"]["kind"] == "ankan"
        ]
        self.assertTrue(declarations)
        self.assertIn("kan_confirmed", [e["kind"] for e in events])
        self.assertTrue(any(e.get("draw_kind") == "rinshan" for e in events))
        self.assertFalse(
            any(e.get("evidence", {}).get("origin") == "ankan" for e in events)
        )

    def test_rules_are_fixed(self):
        from lisjong.learning.ron_legal_source import RULES

        self.assertEqual(producer.rules_projection(RuleSet.default()), RULES)
        with self.assertRaises(producer.RonProducerError):
            producer.rules_projection(
                replace(RuleSet.default(), triple_ron_abortive_draw=False)
            )

    def test_native_reader_accepts_complete_fixture_and_rejects_missing_fact(self):
        from lisjong.belief.ron_legal_ground_truth import require_scoring_backend
        from lisjong.learning.ron_legal_source import (
            read_labelled_ron_source,
            read_ron_source,
        )

        try:
            require_scoring_backend()
        except Exception as error:
            if os.environ.get("LISJONG_REQUIRE_NATIVE") == "1":
                raise
            self.skipTest(f"native scoring unavailable: {error}")
        recorder, _ = fixture(riichi=True)
        game = recorder.finish()
        kinds = [step["event"]["kind"] for row in game[2] for step in row["steps"]]
        self.assertIn("riichi_established", kinds)
        self.assertIn("reaction", kinds)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            manifest = producer.write_population(
                output,
                [game],
                {"train": [0], "valid": [], "test": []},
                {
                    "arena_revision": "a" * 40,
                    "lisjong_revision": "b" * 40,
                    "lisjong_engine_revision": "c" * 40,
                    "policy": producer.base.POLICY,
                },
                producer.rules_projection(RuleSet.default()),
            )
            source = read_ron_source(output / "ron", base_directory=output / "base")
            _, labelled = read_labelled_ron_source(
                output / "ron", base_directory=output / "base"
            )
            self.assertEqual(len(source.decisions), len(game[0][1]))
            self.assertEqual(len(labelled), len(source.decisions))
            self.assertEqual(manifest["coverage"], game[3])
            (output / "ron" / "ron_facts.jsonl").write_bytes(b"")
            with self.assertRaises(ValueError):
                read_ron_source(output / "ron", base_directory=output / "base")


class AllocationTests(unittest.TestCase):
    def allocation(
        self, seeds=(9000000, 9000001), revision="a" * 40, owner=producer.OWNER
    ):
        ledger = seed_registry.new_ledger()
        ledger, record = seed_registry.reserve_allocation(
            ledger,
            owner_issue=owner,
            protocol=producer.PROTOCOL,
            seed_domain=seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
            allocation_timestamp="2026-10-08T00:00:00Z",
            purpose="synthetic fixture",
            population=producer.POPULATION,
            split=producer.SPLIT,
            seeds=seeds,
            arena_revision=revision,
            protocol_revision=producer.PROTOCOL,
            provenance_reference="https://github.com/lisbun/lisjong-arena/issues/457",
        )
        return ledger, record["allocation_identity"]

    def test_authorized_and_mismatched_revision_population(self):
        ledger, identity = self.allocation()
        self.assertEqual(
            producer.authorize(ledger, identity, [9000000, 9000001], "a" * 40)[
                "allocation_identity"
            ],
            identity,
        )
        for seeds, revision in (
            ([9000000, 9000000], "a" * 40),
            ([9000001, 9000000], "a" * 40),
            ([9000000, 9000001], "b" * 40),
            ([9000000, 9000001], "a" * 40 + "-dirty"),
        ):
            with self.assertRaises(ValueError):
                producer.authorize(ledger, identity, seeds, revision)
        ledger, identity = self.allocation(owner="lisbun/lisjong#259")
        with self.assertRaises(ValueError):
            producer.authorize(ledger, identity, [9000000, 9000001], "a" * 40)


if __name__ == "__main__":
    unittest.main()
