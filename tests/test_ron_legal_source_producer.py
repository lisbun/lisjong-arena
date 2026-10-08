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
    KakanAction,
    PassAction,
    PonAction,
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
    def __init__(self, riichi=False, ankan=False, kakan=False):
        self.riichi = riichi
        self.ankan = ankan
        self.kakan = kakan
        self.inputs = []

    def choose_action(self, decision):
        self.inputs.append(decision)
        kinds = (
            [AnkanAction]
            if self.ankan
            else [KakanAction, PonAction]
            if self.kakan
            else []
        )
        kinds += (
            []
            if self.kakan
            else [TsumoAction, RiichiAction]
            if self.riichi
            else [TsumoAction]
        )
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
        if layout == "kakan":
            east = [0] * 2 + [3, 6, 9, 12, 15, 18, 21, 24, 27, 28, 29]
        hands = [[take(kind) for kind in east]]
        draws = [
            take(kind)
            for kind in ([31, 0, 32, 33, 0] if layout == "kakan" else [first])
        ]
        for _ in range(3):
            hands.append([pool.pop(0) for _ in range(13)])
        pool[0:0] = draws
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


def fixture(riichi=False, ankan=False, kakan=False):
    match = MatchState(seed=0)
    with patch(
        "lisjong_engine.match_state.create_round_wall",
        return_value=fixed_wall(
            "ankan" if ankan else "kakan" if kakan else "riichi" if riichi else None
        ),
    ):
        state = match.start_round()
    state.enable_transaction_observation()
    recorder = producer.Recorder(0, match)
    recorder.observe(_round_start_observation(match, state))
    policies = [FixturePolicy(riichi, ankan, kakan) for _ in Seat]
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

    def test_kakan_has_exactly_one_reaction_before_confirmation(self):
        recorder, _ = fixture(kakan=True)
        events = [step["event"] for row in recorder.history for step in row["steps"]]
        position = next(
            i
            for i, e in enumerate(events)
            if e["kind"] == "progress" and e["action"]["kind"] == "kakan"
        )
        self.assertEqual(events[position + 1]["evidence"]["origin"], "kakan")
        self.assertEqual(events[position + 2]["kind"], "kan_confirmed")

    def test_context_projection_preserves_temporary_and_riichi_missingness(self):
        recorder, _ = fixture()
        value = recorder.raw_checkpoint.seats[0]
        from lisjong_engine.furiten import FuritenReason
        from lisjong_engine.win_context import RiichiStatus

        for reason, status, ippatsu in (
            (None, RiichiStatus.NONE, False),
            (FuritenReason.TEMPORARY, RiichiStatus.NONE, False),
            (FuritenReason.RIICHI, RiichiStatus.RIICHI, True),
        ):
            actual = producer.context(
                replace(
                    value,
                    missed_ron_furiten=reason,
                    riichi_status=status,
                    is_ippatsu=ippatsu,
                )
            )
            self.assertEqual(
                actual,
                {
                    "missed_ron_state": "none" if reason is None else reason.value,
                    "riichi_status": status.value,
                    "is_ippatsu": ippatsu,
                },
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
        others = [fixture(ankan=True)[0].finish(), fixture(kakan=True)[0].finish()]
        for seed, item in enumerate(others, 1):
            item[0][1][:] = [
                {**row, "key": {**row["key"], "seed": seed}} for row in item[0][1]
            ]
            item[0][2][:] = [
                {**row, "key": {**row["key"], "seed": seed}} for row in item[0][2]
            ]
            item[1][:] = [
                {**row, "key": {**row["key"], "seed": seed}} for row in item[1]
            ]
            item[2][:] = [{**row, "seed": seed} for row in item[2]]
            item[3][:] = [{**row, "seed": seed} for row in item[3]]
            others[seed - 1] = ((seed, *item[0][1:]), *item[1:])
        kinds = [step["event"]["kind"] for row in game[2] for step in row["steps"]]
        self.assertIn("riichi_established", kinds)
        self.assertIn("reaction", kinds)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            manifest = producer.write_population(
                output,
                [game, *others],
                {"train": [0], "valid": [1], "test": [2]},
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
            self.assertEqual(
                len(source.decisions), sum(len(g[0][1]) for g in [game, *others])
            )
            self.assertEqual(len(labelled), len(source.decisions))
            self.assertEqual(
                manifest["coverage"], [row for g in [game, *others] for row in g[3]]
            )
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
        committed = seed_registry.transition_allocation(
            ledger, identity, state=seed_registry.COMMITTED
        )
        with self.assertRaises(ValueError):
            producer.authorize(committed, identity, [9000000, 9000001], "a" * 40)
        ledger, identity = self.allocation(owner="lisbun/lisjong#259")
        with self.assertRaises(ValueError):
            producer.authorize(ledger, identity, [9000000, 9000001], "a" * 40)


if __name__ == "__main__":
    unittest.main()
