"""Tenpai PUSH/FOLD paired source producer (lisjong-arena#476).

The real engine plays one RETIRED lisjong-arena#385 seed with a cheap stand-in
for the Champion, the gate and the fold-side Policy (the lisjong-owned boundary
``LisjongRuntime``).  The stand-in gate keeps the structural conditions that
lisjong's strict reader checks, so ``read_source()`` is the oracle for the
written files.  No Champion game, wait model or native scorer is used.

The control hanchan is played once and shared; fold-side runs are real replays
of its first rounds.  Regenerating real Champion games byte for byte is checked
by the pilot, not here.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lisjong.learning import tenpai_push_fold_source as wire
from lisjong.learning.ron_legal_estimator import in_riichi_scope
from lisjong.learning.tenpai_push_fold import GateKind
from lisjong.policies.shanten import ShantenPolicy
from lisjong.policy_contract import (
    DiscardAction,
    MeldKind,
    PolicyDecision,
    RiichiAction,
    Seat,
    Tile,
    TileCategory,
    TileType,
)
from lisjong.policy_contract import Wind as PolicyWind
from lisjong_engine.public_state import SeatPointDelta, SeatScore
from lisjong_engine.round_completion import (
    RoundCompletionFact,
    RoundCompletionSettlementTransfer,
    RoundCompletionWinner,
    RoundOutcomeKind,
)
from lisjong_engine.round_result import AbortiveDrawReason
from lisjong_engine.seat import Seat as EngineSeat
from lisjong_engine.settlement import TransferReason
from lisjong_engine.win_context import WinMethod
from lisjong_engine.wind import Wind

from lisjong_arena import seed_registry

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "generate_tenpai_push_fold_source_476",
    _ROOT / "scripts" / "generate_tenpai_push_fold_source_476.py",
)
producer = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = producer
_SPEC.loader.exec_module(producer)

ARENA = "a" * 40
SEED = 931400
"""A RETIRED lisjong-arena#385 seed; no unused population seed is played."""
SPLITS = {"train": (SEED,), "valid": ()}
PRODUCER = {
    "arena_revision": ARENA,
    "lisjong_engine_revision": "b" * 40,
    "lisjong_revision": "c" * 40,
    "policy": "PlacementAwareSpeedCallPolicy",
    "wait_model_sha256": wire.SELECTED_WAIT_MODEL_SHA256,
}
E, S, W, N = EngineSeat


class _Champion:
    """Riichi when legal, otherwise the shanten-minimizing baseline."""

    def choose_action_with_analysis(self, decision):
        for action in decision.legal_actions:
            if isinstance(action, RiichiAction):
                return PolicyDecision(action=action)
        return PolicyDecision(action=ShantenPolicy().choose_action(decision))


class _Tsumogiri(_Champion):
    """Differs from ``_Champion`` on hand discards."""

    def choose_action_with_analysis(self, decision):
        for action in decision.legal_actions:
            if isinstance(action, DiscardAction) and action.tsumogiri:
                return PolicyDecision(action=action)
        return super().choose_action_with_analysis(decision)


class _FoldPolicy:
    def __init__(self, runtime, fold_target):
        self._runtime = runtime
        self._fold_target = fold_target

    def choose_action_with_analysis(self, decision):
        decided = _Champion().choose_action_with_analysis(decision)
        if decision != self._fold_target:
            return decided
        return PolicyDecision(action=self._runtime.gate(decision, decided).fold_action)


class FakeRuntime:
    """Stand-in for ``LisjongRuntime``: one riichi opponent and two discards."""

    def champion(self):
        return _Champion()

    def fold_policy(self, fold_target):
        return _FoldPolicy(self, fold_target)

    def gate(self, decision, c0_decision):
        policy_input = decision.input
        seat = policy_input.self_seat
        riichi = [
            other
            for other in Seat
            if other is not seat and in_riichi_scope(policy_input, other)
        ]
        discards = [a for a in decision.legal_actions if isinstance(a, DiscardAction)]
        c0 = c0_decision.action
        if len(riichi) != 1 or len(discards) < 2:
            return None
        if isinstance(c0, RiichiAction):
            kind, push = GateKind.RIICHI, discards[0]
        elif not isinstance(c0, DiscardAction):
            return None
        elif all(m.kind is MeldKind.ANKAN for m in policy_input.players[seat].melds):
            kind, push = GateKind.CLOSED_DISCARD, c0
        else:
            kind, push = GateKind.OPEN_DISCARD, c0
        fold = next(action for action in reversed(discards) if action != push)
        return SimpleNamespace(
            kind=kind,
            riichi_seat=riichi[0],
            c0_action=c0,
            push_action=push,
            fold_action=fold,
            push_ron_legal_raw=100,
            fold_ron_legal_raw=50 if self._has_candidate(policy_input, c0) else 100,
        )

    @staticmethod
    def _has_candidate(policy_input, c0) -> bool:
        """A few decisions of the first rounds, so fold-side replays stay short."""
        state = policy_input.round
        return (
            state.round_wind is PolicyWind.EAST
            and state.hand_number <= 2
            and (
                isinstance(c0, RiichiAction) or state.live_wall_tiles_remaining % 6 == 0
            )
        )


@functools.cache
def _control():
    """The shared control hanchan: ``(hanchan, gates)`` of ``play_control()``."""
    return producer.play_control(SEED, FakeRuntime())


def _records():
    control, gates = _control()
    return producer.gate_records(SEED, control.calls, gates)


def _generate(output: Path) -> dict[str, object]:
    with mock.patch.object(producer, "play_control", lambda seed, runtime: _control()):
        return producer.generate(
            output,
            splits=SPLITS,
            producer=PRODUCER,
            runtime=FakeRuntime(),
            workers=1,
            evidence={"protocol": "test"},
        )


class GenerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        cls.document = _generate(cls.root / "first")
        cls.manifest, cls.pairs = wire.read_source(cls.root / "first")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    def test_the_strict_reader_accepts_every_kind_and_both_sides(self) -> None:
        self.assertEqual(self.manifest.producer, PRODUCER)
        self.assertEqual(self.manifest.splits, SPLITS)
        self.assertEqual({pair.decision.key.seed for pair in self.pairs}, {SEED})
        self.assertIn(GateKind.RIICHI, {pair.decision.kind for pair in self.pairs})
        self.assertGreater(
            len({pair.decision.kind for pair in self.pairs} - {GateKind.RIICHI}), 0
        )
        self.assertTrue(any(pair.fold is None for pair in self.pairs))
        self.assertTrue(any(pair.fold is not None for pair in self.pairs))

    def test_the_counts_are_those_of_the_written_rows(self) -> None:
        total = self.document["total"]
        for kind in GateKind:
            rows = [pair for pair in self.pairs if pair.decision.kind is kind]
            self.assertEqual(
                total["gate_decisions"][kind.value],
                {
                    "gate_decisions": len(rows),
                    "with_fold_candidate": sum(p.fold is not None for p in rows),
                },
            )
        riichi = [p for p in self.pairs if p.decision.kind is GateKind.RIICHI]
        matched = sum(
            p.push.declaration_discard == p.decision.push_action for p in riichi
        )
        self.assertEqual(
            total["riichi_declaration_prediction"],
            {"match": matched, "mismatch": len(riichi) - matched},
        )
        self.assertEqual(
            total["fold_runs"], sum(pair.fold is not None for pair in self.pairs)
        )
        self.assertEqual(total["hanchan"], 1)
        written = json.loads(
            (self.root / "first" / producer.GENERATION_FILENAME).read_text("utf-8")
        )
        self.assertEqual(written, self.document)

    def test_rows_are_in_key_order_with_push_before_fold(self) -> None:
        lines = (self.root / "first" / wire.OUTCOMES_FILENAME).read_text("utf-8")
        rows = [json.loads(line) for line in lines.splitlines()]
        self.assertEqual(
            [(row["key"]["sequence"], row["side"]) for row in rows],
            [
                (pair.decision.key.sequence, side)
                for pair in self.pairs
                for side in (wire.PUSH, wire.FOLD)[: 1 + (pair.fold is not None)]
            ],
        )

    def test_an_existing_output_is_refused(self) -> None:
        with self.assertRaises(producer.TenpaiPushFoldProducerError):
            _generate(self.root / "first")

    def test_games_must_be_the_split_seeds(self) -> None:
        game = {"seed": SEED, "decisions": [], "outcomes": []}
        for games in ([], [game, {**game, "seed": 931999}], [game, game]):
            with self.assertRaises(producer.TenpaiPushFoldProducerError):
                producer.write_source(
                    self.root / "mismatch",
                    splits=SPLITS,
                    games=games,
                    producer=PRODUCER,
                )
        self.assertFalse((self.root / "mismatch").exists())


class FoldRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = FakeRuntime()
        cls.control, _ = _control()
        cls.record = next(
            record
            for record in _records()
            if record.has_fold_candidate
            and cls.control.calls[record.key.sequence].round_index > 0
        )
        cls.target = cls.record.key.sequence

    def test_the_run_is_the_control_up_to_the_target_and_stops_at_the_round(
        self,
    ) -> None:
        fold = producer.play_fold(self.record, self.control.calls, self.runtime)
        self.assertEqual(fold.calls[: self.target], self.control.calls[: self.target])
        self.assertEqual(fold.calls[self.target].action, self.record.fold_action)
        self.assertEqual(
            fold.calls[self.target].context, self.control.calls[self.target].context
        )
        round_index = self.control.calls[self.target].round_index
        self.assertEqual(len(fold.rounds), round_index + 1)
        self.assertEqual(fold.rounds[:round_index], self.control.rounds[:round_index])
        self.assertTrue(all(c.round_index <= round_index for c in fold.calls))
        self.assertGreater(len(self.control.rounds), round_index + 1)

    def test_gate_ordinals_follow_the_sequence(self) -> None:
        records = _records()
        self.assertEqual([r.ordinal for r in records], list(range(len(records))))
        sequences = [r.key.sequence for r in records]
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertTrue(any(not r.has_fold_candidate for r in records))

    def test_a_policy_that_does_not_fold_at_the_target_fails(self) -> None:
        runtime = FakeRuntime()
        runtime.fold_policy = lambda fold_target: _Champion()
        with self.assertRaisesRegex(producer.TenpaiPushFoldProducerError, "chose"):
            producer.play_fold(self.record, self.control.calls, runtime)

    def test_a_run_that_leaves_the_control_before_the_target_fails(self) -> None:
        runtime = FakeRuntime()
        runtime.champion = lambda: _Tsumogiri()
        with self.assertRaisesRegex(
            producer.TenpaiPushFoldProducerError, "call \\d+ (input differs|chose)"
        ):
            producer.play_fold(self.record, self.control.calls, runtime)

    def test_a_target_that_never_appears_fails(self) -> None:
        calls = list(self.control.calls)
        target = calls[self.target]
        calls[self.target] = producer.Call(target.context, target.action, 0)
        with self.assertRaisesRegex(
            producer.TenpaiPushFoldProducerError, "did not appear"
        ):
            producer.play_fold(self.record, calls, self.runtime)

    def test_a_gate_whose_c0_is_not_the_played_action_is_rejected(self) -> None:
        gate = SimpleNamespace(c0_action=RiichiAction(actor=Seat(0)))
        calls = [self.control.calls[self.target]]
        with self.assertRaisesRegex(producer.TenpaiPushFoldProducerError, "C0"):
            producer.gate_records(SEED, calls, [(0, gate)])


def _tile(index: int) -> Tile:
    return Tile(TileType(tuple(TileCategory)[index // 9], index % 9 + 1))


def _discard(seat: int, index: int) -> DiscardAction:
    return DiscardAction(actor=Seat(seat), tile=_tile(index), tsumogiri=False)


def _call(seat: int, action, round_index: int = 0):
    context = SimpleNamespace(input=SimpleNamespace(self_seat=Seat(seat)))
    return producer.Call(context, action, round_index)


def _fact(outcome=RoundOutcomeKind.ABORTIVE_DRAW, *, deltas=(0, 0, 0, 0), **fields):
    if outcome is RoundOutcomeKind.ABORTIVE_DRAW:
        fields.setdefault("abortive_reason", next(iter(AbortiveDrawReason)))
    return RoundCompletionFact(
        prevailing_wind=Wind.EAST,
        hand_number=1,
        dealer_seat=E,
        honba=1,
        outcome=outcome,
        point_deltas=tuple(
            SeatPointDelta(seat, delta)
            for seat, delta in zip(EngineSeat, deltas, strict=True)
        ),
        scores_after=tuple(SeatScore(seat, 25000) for seat in EngineSeat),
        dealer_continues=False,
        has_next_round=True,
        **fields,
    )


def _ron(source, payments: dict, *, deltas=(0, 0, 0, 0)):
    """``payments``: winner -> base points; honba goes to the first winner."""
    transfers = [
        RoundCompletionSettlementTransfer(
            source, winner, points, TransferReason.RON, winner
        )
        for winner, points in payments.items()
    ]
    first = next(iter(payments))
    transfers.append(
        RoundCompletionSettlementTransfer(
            source, first, 300, TransferReason.HONBA, first
        )
    )
    return _fact(
        RoundOutcomeKind.WIN,
        winners=tuple(
            RoundCompletionWinner(winner, WinMethod.RON)
            for winner in EngineSeat
            if winner in payments
        ),
        source_seat=source,
        settlement_transfers=tuple(transfers),
        deltas=deltas,
    )


def _record(kind=GateKind.CLOSED_DISCARD, *, sequence=1, seat=0):
    c0 = (
        RiichiAction(actor=Seat(seat)) if kind is GateKind.RIICHI else _discard(seat, 3)
    )
    return SimpleNamespace(
        key=wire.GateDecisionKey(seed=931400, sequence=sequence, seat=seat),
        kind=kind,
        c0_action=c0,
        fold_action=_discard(seat, 9),
    )


class RoundOutcomeTest(unittest.TestCase):
    """Synthetic completion facts; no game is played."""

    def test_a_ron_on_the_discard_did_not_pass(self) -> None:
        calls = [_call(1, _discard(1, 0)), _call(0, _discard(0, 3)), _call(2, None, 1)]
        fact = _ron(E, {W: 7700}, deltas=(-8000, 0, 9000, -1000))
        outcome = producer.round_outcome(_record(), wire.PUSH, calls, fact)
        self.assertFalse(outcome.discard_passed)
        self.assertEqual((outcome.deal_in_to, outcome.deal_in_points), (Seat(2), 7700))
        self.assertEqual(outcome.round_delta, -8000)
        self.assertIsNone(outcome.win_method)
        self.assertIsNone(outcome.exhaustive_draw_tenpai)
        self.assertIsNone(outcome.declaration_discard)

    def test_a_later_deal_in_is_not_this_discard(self) -> None:
        calls = [
            _call(1, _discard(1, 0)),
            _call(0, _discard(0, 3)),
            _call(1, _discard(1, 5)),
            _call(0, _discard(0, 4)),
        ]
        outcome = producer.round_outcome(
            _record(), wire.PUSH, calls, _ron(E, {S: 2000})
        )
        self.assertTrue(outcome.discard_passed)
        self.assertEqual((outcome.deal_in_to, outcome.deal_in_points), (Seat(1), 2000))

    def test_another_seats_deal_in_is_no_result_of_the_decider(self) -> None:
        calls = [_call(1, _discard(1, 0)), _call(0, _discard(0, 3))]
        outcome = producer.round_outcome(
            _record(), wire.PUSH, calls, _ron(S, {W: 2000}, deltas=(0, -2300, 2300, 0))
        )
        self.assertTrue(outcome.discard_passed)
        self.assertIsNone(outcome.deal_in_to)
        self.assertIsNone(outcome.win_method)
        self.assertEqual(outcome.round_delta, 0)

    def test_a_multiple_ron_names_the_nearest_winner_and_the_whole_payment(
        self,
    ) -> None:
        calls = [_call(1, _discard(1, 0)), _call(2, _discard(2, 3))]
        record = _record(seat=2)
        record.c0_action = _discard(2, 3)
        outcome = producer.round_outcome(
            record, wire.PUSH, calls, _ron(W, {E: 1000, S: 3900})
        )
        self.assertEqual((outcome.deal_in_to, outcome.deal_in_points), (Seat(0), 4900))
        self.assertFalse(outcome.discard_passed)

    def test_win_points_exclude_honba(self) -> None:
        calls = [_call(1, _discard(1, 0)), _call(0, _discard(0, 3))]
        ron = producer.round_outcome(_record(), wire.PUSH, calls, _ron(S, {E: 5800}))
        self.assertEqual((ron.win_method, ron.win_points), ("ron", 5800))
        self.assertTrue(ron.discard_passed)
        tsumo = _fact(
            RoundOutcomeKind.WIN,
            winners=(RoundCompletionWinner(E, WinMethod.TSUMO),),
            settlement_transfers=tuple(
                RoundCompletionSettlementTransfer(payer, E, amount, reason, E)
                for payer in (S, W, N)
                for amount, reason in (
                    (2000, TransferReason.TSUMO),
                    (100, TransferReason.HONBA),
                )
            ),
            deltas=(7300, -2100, -2100, -2100),
        )
        outcome = producer.round_outcome(_record(), wire.PUSH, calls, tsumo)
        self.assertEqual((outcome.win_method, outcome.win_points), ("tsumo", 6000))
        self.assertEqual(outcome.round_delta, 7300)

    def test_draws(self) -> None:
        calls = [_call(1, _discard(1, 0)), _call(0, _discard(0, 9))]
        for tenpai in (True, False):
            fact = _fact(
                RoundOutcomeKind.EXHAUSTIVE_DRAW, tenpai_seats=(E,) if tenpai else (S,)
            )
            outcome = producer.round_outcome(_record(), wire.FOLD, calls, fact)
            self.assertIs(outcome.exhaustive_draw_tenpai, tenpai)
        abortive = producer.round_outcome(_record(), wire.FOLD, calls, _fact())
        self.assertIsNone(abortive.exhaustive_draw_tenpai)
        self.assertTrue(abortive.discard_passed)

    def test_the_riichi_push_side_records_the_declaration_discard(self) -> None:
        record = _record(GateKind.RIICHI)
        calls = [
            _call(1, _discard(1, 0)),
            _call(0, record.c0_action),
            _call(0, _discard(0, 7)),
        ]
        outcome = producer.round_outcome(record, wire.PUSH, calls, _ron(E, {N: 8000}))
        self.assertEqual(outcome.declaration_discard, _discard(0, 7))
        self.assertFalse(outcome.discard_passed)
        fold = [_call(1, _discard(1, 0)), _call(0, record.fold_action)]
        outcome = producer.round_outcome(record, wire.FOLD, fold, _fact())
        self.assertIsNone(outcome.declaration_discard)

    def test_calls_that_are_not_the_sides_discard_are_rejected(self) -> None:
        riichi = _record(GateKind.RIICHI)
        cases = {
            "another discard": (_record(), wire.PUSH, [None, _call(0, _discard(0, 4))]),
            "not the fold": (_record(), wire.FOLD, [None, _call(0, _discard(0, 3))]),
            "another seat": (_record(), wire.FOLD, [None, _call(1, _discard(0, 9))]),
            "no declaration": (riichi, wire.PUSH, [None, _call(0, riichi.c0_action)]),
            "declaration of the next round": (
                riichi,
                wire.PUSH,
                [None, _call(0, riichi.c0_action), _call(0, _discard(0, 7), 1)],
            ),
            "no riichi": (
                riichi,
                wire.PUSH,
                [None, _call(0, _discard(0, 3)), _call(0, _discard(0, 7))],
            ),
        }
        for name, (record, side, calls) in cases.items():
            with (
                self.subTest(name),
                self.assertRaises(producer.TenpaiPushFoldProducerError),
            ):
                producer.round_outcome(record, side, calls, _fact())


def reserved_ledger(**overrides):
    parameters = {
        "owner_issue": producer.OWNER_ISSUE,
        "protocol": producer.PROTOCOL,
        "seed_domain": seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        "purpose": "test",
        "population": producer.POPULATION,
        "split": producer.SPLIT,
        "seeds": producer.PILOT_SEEDS,
        "arena_revision": ARENA,
        "protocol_revision": producer.PROTOCOL,
        "provenance_reference": "https://example.invalid/provenance",
        "allocation_timestamp": "2026-10-09T00:00:00.000000Z",
    }
    parameters.update(overrides)
    return seed_registry.reserve_allocation(seed_registry.load_ledger(), **parameters)


class PilotPopulationTest(unittest.TestCase):
    def test_the_pilot_is_fixed_and_outside_every_used_range(self) -> None:
        self.assertEqual(producer.PILOT_SEEDS, tuple(range(938000, 938016)))
        self.assertEqual(
            producer.PILOT_SPLITS,
            {
                "train": tuple(range(938000, 938012)),
                "valid": tuple(range(938012, 938016)),
            },
        )
        producer.guard.check_population(producer.allocation_preset())
        for seeds in (
            range(931000, 932000),
            range(932000, 932100),
            range(937000, 937200),
        ):
            self.assertTrue(
                all(any(s in used for used in producer.USED_RANGES) for s in seeds)
            )

    def _check(self, ledger, identity, revision=ARENA):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text(json.dumps(ledger), encoding="utf-8")
            return producer.check_allocation(path, identity, revision)

    def test_only_the_fresh_allocation_of_the_revision_is_authorized(self) -> None:
        ledger, record = reserved_ledger()
        identity = record["allocation_identity"]
        self.assertEqual(self._check(ledger, identity)["allocation"], record)
        with self.assertRaises(producer.TenpaiPushFoldProducerError):
            self._check(ledger, identity, "d" * 40)
        with self.assertRaises(producer.TenpaiPushFoldProducerError):
            self._check(ledger, identity, ARENA + "-dirty")
        other, other_record = reserved_ledger(seeds=producer.PILOT_SEEDS[:-1])
        with self.assertRaises(producer.TenpaiPushFoldProducerError):
            self._check(other, other_record["allocation_identity"])

    def test_development_plays_only_retired_seeds(self) -> None:
        arguments = SimpleNamespace(train=[931400], valid=[938012])
        with mock.patch.object(producer, "generate") as generate:
            with self.assertRaisesRegex(
                producer.TenpaiPushFoldProducerError, "RETIRED"
            ):
                producer.run_development(arguments)
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
