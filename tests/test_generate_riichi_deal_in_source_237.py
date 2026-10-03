"""lisjong#237 S1 data generator (scripts/generate_riichi_deal_in_source_237.py).

Pins what Arena records (observed facts only), the timing window of the riichi
player's hand and win options, engine facts for the selected discard only, the
13-equivalent hand after an ankan, the scope, the canonical file layout and
the RETIRED-seed guard.  Hanchan execution is not run here.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from lisjong.policy_contract.action import (
    AnkanAction,
    DiscardAction,
    PassAction,
    RonAction,
    TsumoAction,
)
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.discard import Discard
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
import generate_riichi_deal_in_source_237 as generator  # noqa: E402

_CATEGORIES = {
    "m": TileCategory.MANZU,
    "p": TileCategory.PINZU,
    "s": TileCategory.SOUZU,
    "z": TileCategory.HONOR,
}
S, R, X = Seat.SEAT_0, Seat.SEAT_1, Seat.SEAT_2
KEY = ("EAST", 1, 0)


def tiles(spec: str) -> tuple[Tile, ...]:
    out, ranks = [], ""
    for character in spec:
        if character.isdigit():
            ranks += character
            continue
        out += [Tile(TileType(_CATEGORIES[character], int(rank))) for rank in ranks]
        ranks = ""
    return tuple(out)


def t(spec: str) -> Tile:
    return tiles(spec)[0]


R_HAND = tiles("123m456p789s11z23m")  # waits 1m / 4m


def _player(riichi=RiichiState.NONE, discards="", melds=()):
    return PlayerPublicState(
        score=25000,
        discards=tuple(
            Discard(tile=tile, tsumogiri=False, order=order, called_by=None)
            for order, tile in enumerate(tiles(discards))
        ),
        melds=tuple(melds),
        riichi=riichi,
    )


def decision(
    seat,
    concealed,
    legal,
    *,
    r_state=RiichiState.ACCEPTED,
    r_melds=(),
    s_state=RiichiState.NONE,
    x_state=RiichiState.NONE,
    r_discards="9p",
):
    players = (
        _player(s_state),
        _player(r_state, r_discards, r_melds),
        _player(x_state),
        _player(),
    )
    policy_input = PolicyInput(
        self_seat=seat,
        round=RoundState(
            round_wind=Wind.EAST,
            hand_number=1,
            dealer_seat=Seat.SEAT_0,
            honba=0,
            riichi_sticks=1,
            dora_indicators=(),
            live_wall_tiles_remaining=40,
        ),
        players=players,
        own_hand=OwnHandState(concealed_tiles=tuple(concealed), drawn_tile=None),
    )
    return DecisionContext(input=policy_input, legal_actions=tuple(legal))


def discard(seat, spec):
    return DiscardAction(actor=seat, tile=t(spec), tsumogiri=False)


def r_declares():
    return (
        decision(
            R,
            R_HAND + (t("9p"),),
            [discard(R, "9p")],
            r_state=RiichiState.DECLARED,
            r_discards="",
        ),
        discard(R, "9p"),
    )


def s_discards(chosen="5z", **state):
    legal = [discard(S, "1m"), discard(S, "4m"), discard(S, "5z")]
    return decision(S, tiles("14m5z"), legal, **state), discard(S, chosen)


class ExtractRecordsTest(unittest.TestCase):
    def test_records_facts_without_judging_the_label(self):
        decisions, facts = generator.extract_records(
            931000, [r_declares(), s_discards()], {}
        )
        self.assertEqual(len(decisions), 1)
        fact = facts[0]
        self.assertEqual(fact["key"], {"seat": 0, "seed": 931000, "sequence": 1})
        self.assertEqual(decisions[0]["key"], fact["key"])
        self.assertEqual(fact["riichi_seat"], 1)
        self.assertEqual(fact["riichi_declared_sequence"], 0)
        self.assertEqual(fact["riichi_hand"]["sequence"], 0)
        self.assertEqual(len(fact["riichi_hand"]["concealed_tiles"]), 13)
        self.assertEqual(fact["win_options"], [])
        self.assertEqual(
            fact["selected_outcome"], {"dealt_in": False, "ron_offered": False}
        )
        # Arena records facts only: no label, furiten flag or per-candidate value.
        self.assertEqual(
            set(fact),
            {
                "key",
                "riichi_declared_sequence",
                "riichi_hand",
                "riichi_seat",
                "schema",
                "selected_outcome",
                "win_options",
            },
        )
        self.assertNotIn("furiten", json.dumps(fact))
        self.assertNotIn("riichi_hand", decisions[0])

    def test_engine_facts_are_for_the_selected_discard(self):
        ron = RonAction(actor=R, target=S, winning_tile=t("1m"))
        events = [
            r_declares(),
            s_discards("1m"),
            (decision(R, R_HAND, [ron, PassAction(actor=R)]), ron),
        ]
        _, facts = generator.extract_records(931000, events, {KEY: (0, {1})})
        self.assertEqual(
            facts[0]["selected_outcome"], {"dealt_in": True, "ron_offered": True}
        )

    def test_win_options_stay_inside_the_riichi_to_decision_window(self):
        ron_on_x = RonAction(actor=R, target=X, winning_tile=t("4m"))
        ron_on_s = RonAction(actor=R, target=S, winning_tile=t("1m"))
        events = [
            r_declares(),
            s_discards(),  # sequence 1: no option yet
            (decision(R, R_HAND, [ron_on_x, PassAction(actor=R)]), PassAction(actor=R)),
            s_discards("1m"),  # sequence 3: sees the pass at 2
            (decision(R, R_HAND, [ron_on_s, PassAction(actor=R)]), PassAction(actor=R)),
            s_discards(),  # sequence 5: sees passes at 2 and 4
        ]
        _, facts = generator.extract_records(931000, events, {})
        self.assertEqual(
            [[o["sequence"] for o in f["win_options"]] for f in facts],
            [[], [2], [2, 4]],
        )
        self.assertEqual(facts[1]["win_options"][0]["selected_action"]["kind"], "pass")

    def test_tsumo_option_is_recorded_with_the_chosen_discard(self):
        drawn = R_HAND + (t("1m"),)
        tsumo = TsumoAction(actor=R, winning_tile=t("1m"))
        events = [
            r_declares(),
            (decision(R, drawn, [tsumo, discard(R, "1m")]), discard(R, "1m")),
            s_discards(),
        ]
        _, facts = generator.extract_records(931000, events, {})
        (option,) = facts[0]["win_options"]
        self.assertEqual(option["selected_action"]["kind"], "discard")
        self.assertEqual(facts[0]["riichi_hand"]["sequence"], 1)

    def test_ankan_after_riichi_is_kept_in_the_13_equivalent_hand(self):
        before = tiles("123m456p777s11z23m")
        ankan_tiles = tiles("7777s")
        meld = PublicMeld(
            kind=MeldKind.ANKAN, tiles=ankan_tiles, from_seat=None, called_tile=None
        )
        events = [
            (
                decision(
                    R,
                    before + (t("9p"),),
                    [discard(R, "9p")],
                    r_state=RiichiState.DECLARED,
                    r_discards="",
                ),
                discard(R, "9p"),
            ),
            (
                decision(
                    R,
                    before + (t("7s"),),
                    [AnkanAction(actor=R, tiles=ankan_tiles), discard(R, "7s")],
                ),
                AnkanAction(actor=R, tiles=ankan_tiles),
            ),
            (
                decision(
                    R, tiles("123m456p11z23m9m"), [discard(R, "9m")], r_melds=(meld,)
                ),
                discard(R, "9m"),
            ),
            s_discards(r_melds=(meld,), r_discards="9p9m"),
        ]
        _, facts = generator.extract_records(931000, events, {})
        hand = facts[0]["riichi_hand"]
        self.assertEqual(len(hand["concealed_tiles"]), 10)
        self.assertEqual(len(hand["melds"]), 1)
        self.assertEqual(hand["sequence"], 2)
        self.assertEqual(facts[0]["riichi_declared_sequence"], 0)

    def test_scope_and_missing_hand(self):
        decisions, _ = generator.extract_records(
            931000,
            [
                r_declares(),
                s_discards(x_state=RiichiState.ACCEPTED),
                s_discards(s_state=RiichiState.ACCEPTED),
            ],
            {},
        )
        self.assertEqual(decisions, [])
        with self.assertRaises(RuntimeError):
            generator.extract_records(931000, [s_discards()], {})


class WriteSourceTest(unittest.TestCase):
    def test_canonical_files_and_manifest_digests(self):
        decisions, facts = generator.extract_records(
            931000, [r_declares(), s_discards()], {}
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source"
            manifest = generator.write_source(
                output,
                splits={"train": [931000], "valid": [], "test": []},
                games=[(931000, decisions, facts)],
                producer={
                    "arena_revision": "a",
                    "lisjong_engine_revision": "b",
                    "lisjong_revision": "c",
                    "policy": "P",
                },
            )
            for name, filename in (
                ("decisions", "decisions.jsonl"),
                ("label_facts", "label_facts.jsonl"),
            ):
                data = (output / filename).read_bytes()
                self.assertEqual(
                    manifest["files"][name]["sha256"], hashlib.sha256(data).hexdigest()
                )
                for line in data.decode().splitlines(keepends=True):
                    self.assertEqual(generator.canonical_line(json.loads(line)), line)
            self.assertEqual(manifest["schema"], generator.MANIFEST_SCHEMA)
            with self.assertRaises(FileExistsError):
                generator.write_source(output, splits={}, games=[], producer={})


class SeedGuardTest(unittest.TestCase):
    def test_seeds_must_stay_retired_and_disjoint(self):
        with self.assertRaises(SystemExit):
            generator.main(
                [
                    "--train",
                    "930999..931000",
                    "--valid",
                    "931001",
                    "--test",
                    "931002",
                    "--output",
                    "x",
                ]
            )
        with self.assertRaises(SystemExit):
            generator.main(
                [
                    "--train",
                    "931000..931001",
                    "--valid",
                    "931001",
                    "--test",
                    "931002",
                    "--output",
                    "x",
                ]
            )


if __name__ == "__main__":
    unittest.main()
