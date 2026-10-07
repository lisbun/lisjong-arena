"""lisjong#255 HandBelief hand-truth source producer (lisjong-arena#453).

Pins what Arena records (decision-point hands of the three opponents, no
label), the same-state checks, the runner-side coverage check that catches a
decision dropped from both files, the coverage evidence re-check, the seed
guard, and that the Policy never receives the hands.  Hanchan execution is not
run here.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from lisjong.policy_contract.action import DiscardAction, PassAction
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.meld import MeldKind, PublicMeld
from lisjong.policy_contract.own_hand_state import OwnHandState
from lisjong.policy_contract.player_state import PlayerPublicState
from lisjong.policy_contract.policy_input import PolicyInput
from lisjong.policy_contract.riichi import RiichiState
from lisjong.policy_contract.round_state import RoundState
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile, TileCategory, TileType
from lisjong.policy_contract.wind import Wind

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import generate_hand_belief_source_255 as generator  # noqa: E402

_CATEGORIES = {
    "m": TileCategory.MANZU,
    "p": TileCategory.PINZU,
    "s": TileCategory.SOUZU,
    "z": TileCategory.HONOR,
}
SEED = 931400


def tiles(spec: str) -> tuple[Tile, ...]:
    out, ranks = [], ""
    for character in spec:
        if character.isdigit():
            ranks += character
            continue
        out += [Tile(TileType(_CATEGORIES[character], int(rank))) for rank in ranks]
        ranks = ""
    return tuple(out)


PON = PublicMeld(
    kind=MeldKind.PON,
    tiles=tiles("777z"),
    from_seat=Seat.SEAT_0,
    called_tile=tiles("7z")[0],
)
HANDS = (
    generator.SeatHand(tiles("112345678999m5z"), ()),
    generator.SeatHand(tiles("123456789p1234z"), ()),
    generator.SeatHand(tiles("123456789s1166z"), ()),
    generator.SeatHand(tiles("2345p66788s"), (PON,)),
)


def _player(melds=()):
    return PlayerPublicState(
        score=25000, discards=(), melds=tuple(melds), riichi=RiichiState.NONE
    )


def _decision(seat: int, legal, hands=HANDS, public_melds=None):
    melds = public_melds or tuple(hand.melds for hand in hands)
    policy_input = PolicyInput(
        self_seat=Seat(seat),
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1,
            dealer_seat=Seat.SEAT_0,
            honba=0,
            riichi_sticks=0,
            dora_indicators=(),
            live_wall_tiles_remaining=60,
        ),
        players=tuple(_player(m) for m in melds),
        own_hand=OwnHandState(
            concealed_tiles=tuple(hands[seat].concealed_tiles), drawn_tile=None
        ),
    )
    return DecisionContext(input=policy_input, legal_actions=tuple(legal))


def _discard(seat, tile_spec):
    return DiscardAction(actor=Seat(seat), tile=tiles(tile_spec)[0], tsumogiri=False)


def _events(hands=HANDS):
    observer_discard = _discard(0, "5z")
    passing = PassAction(actor=Seat.SEAT_3)
    return [
        generator.RecordedDecision(
            _decision(0, [observer_discard, _discard(0, "9m")], hands),
            observer_discard,
            hands,
        ),
        # out of scope: no legal discard
        generator.RecordedDecision(_decision(3, [passing], hands), passing, hands),
        generator.RecordedDecision(
            _decision(1, [_discard(1, "4z")], hands), _discard(1, "4z"), hands
        ),
    ]


SCOPE_KEYS = [(0, 0), (1, 2)]


class ExtractRecordsTest(unittest.TestCase):
    def test_records_decision_point_hands_of_the_three_opponents(self):
        decisions, facts = generator.extract_records(SEED, _events())
        self.assertEqual(
            [d["key"] for d in decisions],
            [
                {"seat": 0, "seed": SEED, "sequence": 0},
                {"seat": 1, "seed": SEED, "sequence": 2},
            ],
        )
        self.assertEqual([f["key"] for f in facts], [d["key"] for d in decisions])
        first = facts[0]
        self.assertEqual(first["schema"], generator.HAND_FACT_SCHEMA)
        self.assertEqual([o["seat"] for o in first["opponents"]], [1, 2, 3])
        # Same sequence as the decision: the pre-action snapshot (v1 contract).
        self.assertEqual({o["sequence"] for o in first["opponents"]}, {0})
        self.assertEqual(len(first["opponents"][2]["melds"]), 1)
        self.assertEqual(len(first["opponents"][0]["concealed_tiles"]), 13)
        # Facts only: no label and no hands in the player-safe record.
        self.assertEqual(set(first), {"key", "opponents", "schema"})
        self.assertNotIn("opponents", decisions[0])
        self.assertNotIn("wait", json.dumps(first))
        self.assertEqual([o["seat"] for o in facts[1]["opponents"]], [0, 2, 3])

    def test_observer_hand_must_match_policy_input(self):
        events = _events()
        other = list(HANDS)
        other[0] = generator.SeatHand(tiles("112345678999m6z"), ())
        events[0] = generator.RecordedDecision(
            events[0].decision, events[0].action, tuple(other)
        )
        with self.assertRaisesRegex(
            generator.HandBeliefSourceProducerError, "observer"
        ):
            generator.extract_records(SEED, events)

    def test_melds_must_match_the_public_state(self):
        events = _events()
        public = ((), (), (), ())  # PON missing from the public state
        events[0] = generator.RecordedDecision(
            _decision(0, [_discard(0, "5z")], HANDS, public),
            _discard(0, "5z"),
            HANDS,
        )
        with self.assertRaisesRegex(generator.HandBeliefSourceProducerError, "melds"):
            generator.extract_records(SEED, events)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.decisions, self.facts = generator.extract_records(SEED, _events())

    def test_full_coverage_passes(self):
        generator.verify_coverage(SEED, SCOPE_KEYS, self.decisions, self.facts)

    def test_decision_dropped_from_both_files_fails(self):
        with self.assertRaisesRegex(
            generator.HandBeliefSourceProducerError, "1 decision rows for 2"
        ):
            generator.verify_coverage(
                SEED, SCOPE_KEYS, self.decisions[1:], self.facts[1:]
            )

    def test_extra_or_swapped_row_fails(self):
        with self.assertRaises(generator.HandBeliefSourceProducerError):
            generator.verify_coverage(SEED, [(0, 0)], self.decisions, self.facts)
        with self.assertRaisesRegex(
            generator.HandBeliefSourceProducerError, "keys do not match"
        ):
            generator.verify_coverage(
                SEED, [(0, 0), (2, 2)], self.decisions, self.facts
            )

    def test_duplicate_row_fails(self):
        with self.assertRaisesRegex(
            generator.HandBeliefSourceProducerError, "duplicate"
        ):
            generator.verify_coverage(
                SEED,
                SCOPE_KEYS,
                [self.decisions[0], self.decisions[0]],
                self.facts,
            )

    def test_opponents_must_be_the_other_three_seats_once(self):
        for opponents in (
            self.facts[0]["opponents"][:2],
            self.facts[0]["opponents"][:2] + self.facts[0]["opponents"][:1],
        ):
            facts = [dict(self.facts[0], opponents=opponents), self.facts[1]]
            with self.assertRaisesRegex(
                generator.HandBeliefSourceProducerError, "exactly once"
            ):
                generator.verify_coverage(SEED, SCOPE_KEYS, self.decisions, facts)

    def test_stale_opponent_hand_fails(self):
        opponents = [dict(o) for o in self.facts[0]["opponents"]]
        opponents[0]["sequence"] = 99
        facts = [dict(self.facts[0], opponents=opponents), self.facts[1]]
        with self.assertRaisesRegex(
            generator.HandBeliefSourceProducerError, "snapshot"
        ):
            generator.verify_coverage(SEED, SCOPE_KEYS, self.decisions, facts)


PRODUCER = {
    "arena_revision": "a",
    "lisjong_engine_revision": "e",
    "lisjong_revision": "l",
    "policy": generator.POLICY,
}


class WriteSourceTest(unittest.TestCase):
    def _write(self, root: Path) -> Path:
        decisions, facts = generator.extract_records(SEED, _events())
        output = root / "source"
        generator.write_source(
            output,
            splits={"train": [SEED], "valid": [], "test": []},
            games=[(SEED, decisions, facts, SCOPE_KEYS)],
            producer=PRODUCER,
        )
        return output

    def test_layout_and_coverage_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self._write(Path(directory))
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["schema"], generator.MANIFEST_SCHEMA)
            self.assertEqual(manifest["scope"], generator.SCOPE)
            self.assertEqual(set(manifest["files"]), {"decisions", "hand_facts"})
            self.assertEqual(manifest["files"]["hand_facts"]["rows"], 2)
            coverage = json.loads((output / "coverage.json").read_text())
            self.assertEqual(coverage["total_in_scope_decisions"], 2)
            self.assertEqual(generator.verify_source_coverage(output), 2)

    def test_refuses_incomplete_games_before_writing(self):
        decisions, facts = generator.extract_records(SEED, _events())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(generator.HandBeliefSourceProducerError):
                generator.write_source(
                    Path(directory) / "source",
                    splits={"train": [SEED], "valid": [], "test": []},
                    games=[(SEED, decisions[:1], facts[:1], SCOPE_KEYS)],
                    producer=PRODUCER,
                )

    def test_decision_removed_from_both_files_after_writing_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self._write(Path(directory))
            for name in ("decisions.jsonl", "hand_facts.jsonl"):
                path = output / name
                path.write_text(path.read_text().splitlines(keepends=True)[0])
            with self.assertRaisesRegex(
                generator.HandBeliefSourceProducerError, "runner coverage"
            ):
                generator.verify_source_coverage(output)

    def test_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            self._write(Path(directory))
            with self.assertRaises(FileExistsError):
                self._write(Path(directory))


class RecorderTest(unittest.TestCase):
    def test_policy_receives_only_the_decision_and_coverage_is_collected(self):
        from lisjong_engine.match_state import MatchState

        received = []

        class Inner:
            def choose_action(self, decision):
                received.append(decision)
                return decision.legal_actions[0]

        match_state = MatchState(seed=SEED, rules=None)
        match_state.start_round()
        events, scope_keys = [], []
        recorder = generator._Recorder(Inner(), match_state, events, scope_keys)
        for decision in (
            _decision(0, [_discard(0, "5z")]),
            _decision(3, [PassAction(actor=Seat.SEAT_3)]),
        ):
            recorder.choose_action(decision)
        self.assertEqual(received, [events[0].decision, events[1].decision])
        self.assertEqual(scope_keys, [(0, 0)])
        self.assertTrue(all(len(e.hands) == 4 for e in events))

    def test_capture_hands_reads_all_seats_from_the_engine(self):
        from lisjong_engine.match_state import MatchState

        match_state = MatchState(seed=SEED, rules=None)
        match_state.start_round()
        hands = generator.capture_hands(match_state.active_round)
        self.assertEqual([len(h.concealed_tiles) for h in hands], [13, 13, 13, 13])
        self.assertTrue(all(h.melds == () for h in hands))
        self.assertTrue(
            all(isinstance(t, Tile) for h in hands for t in h.concealed_tiles)
        )


class SeedGuardTest(unittest.TestCase):
    def test_pilot_seeds_stay_in_the_unused_retired_range(self):
        self.assertEqual(
            generator._seed_range("931400..931402"), [931400, 931401, 931402]
        )
        for text in ("930999..931000", "931999..932000", "931399..931400", "931100"):
            with self.assertRaises(Exception):
                generator._seed_range(text)

    def test_measurement_seeds_stay_in_the_257_range(self):
        self.assertEqual(generator._seed_range("933000..933001"), [933000, 933001])
        self.assertEqual(generator._seed_range("933399"), [933399])
        for text in ("932999..933000", "933399..933400", "932000..932099"):
            with self.assertRaises(Exception):
                generator._seed_range(text)

    def test_a_run_does_not_mix_pilot_and_measurement_seeds(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                generator.main(
                    [
                        "--train=931400",
                        "--valid=933000",
                        "--test=933001",
                        f"--output={directory}/out",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
