"""lisjong #302: RiichiEnvが確定安全牌の前提とする2つの規則を実装していることの確認。

`lisjong.belief.riichi_safe_tiles`は次の規則を前提にする。

1. リーチ後の見逃しは、その局の終わりまでフリテンになる
2. リーチ後は待ちが変わらない（暗槓は待ちの牌種を変えない場合にだけ許される）

ここでは、固定versionのRiichiEnv（`riichienv.MjaiReplay`の`Kyoku.steps()`）が各判断時点で
提示する合法手を、合成した局面で直接確かめる。Arena側のreplay補正
（`_confirmed_permanent_riichi_furiten`）を通さない。
"""

import unittest
from importlib.metadata import version

from _riichilab_source_pilot_fixtures import (
    END_EVENTS,
    NEUTRAL_1,
    NEUTRAL_2,
    NEUTRAL_3,
    RYUKYOKU,
    dahai,
    reach,
    reach_accepted,
    start_game,
    start_kyoku,
    tsumo,
)
from riichienv import ActionType

from lisjong_arena.riichilab_source_pilot.materialization import _replay_decisions

#: 111m 234m 567m 88m + 45p。待ちは3pと6p。
TWO_SIDED_WAIT_HAND = [
    "1m",
    "1m",
    "1m",
    "2m",
    "3m",
    "4m",
    "5m",
    "6m",
    "7m",
    "8m",
    "8m",
    "4p",
    "5p",
]

#: 11123m 456p 789p 99s。待ちは1m・4m・9s。1mを暗槓すると待ちが変わる。
WAIT_CHANGING_ANKAN_HAND = [
    "1m",
    "1m",
    "1m",
    "2m",
    "3m",
    "4p",
    "5p",
    "6p",
    "7p",
    "8p",
    "9p",
    "9s",
    "9s",
]

#: 111m 234p 456p 789p + 9s。待ちは9sだけ。1mの暗槓は待ちを変えない。
WAIT_PRESERVING_ANKAN_HAND = [
    "1m",
    "1m",
    "1m",
    "2p",
    "3p",
    "4p",
    "4p",
    "5p",
    "6p",
    "7p",
    "8p",
    "9p",
    "9s",
]

#: 待ち牌を持たない他家の手牌。
OTHER_1 = ["1s", "2s", "3s", "4s", "5s", "6s", "7s", "8s", "E", "E", "S", "S", "W"]
OTHER_2 = ["1s", "2s", "3s", "4s", "5s", "6s", "7s", "8s", "W", "W", "N", "N", "C"]
OTHER_3 = ["P", "P", "F", "F", "C", "C", "N", "N", "S", "S", "E", "E", "W"]


def _riichi_log(hand, others, *after_riichi):
    """seat 0が9pをツモ切ってリーチした後へ、eventを続けるlog。"""
    return [
        start_game(),
        start_kyoku([hand, *others]),
        tsumo(0, "9p") if "9p" not in hand else tsumo(0, "P"),
        reach(0),
        dahai(0, "9p" if "9p" not in hand else "P", tsumogiri=True),
        reach_accepted(0),
        *after_riichi,
        RYUKYOKU,
        *END_EVENTS,
    ]


def _legal_types_at(log, *, seat, after_event):
    """`after_event`番目のeventの直後に`seat`へ提示される合法手の種類。判断がなければ空。"""
    by_prefix, _by_teacher = _replay_decisions(log)
    found = [
        decision
        for (_round, decision_seat, prefix), decision in by_prefix.items()
        if decision_seat == seat and prefix == after_event
    ]
    if not found:
        return frozenset()
    (decision,) = found
    return frozenset(
        action.action_type for action in decision.observation.legal_actions()
    )


class RiichiMissedWinFuritenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(version("riichienv"), "0.4.10")

    def _log(self, *discards):
        events = []
        for actor, tile in discards:
            events.extend([tsumo(actor, tile), dahai(actor, tile, tsumogiri=True)])
        return _riichi_log(TWO_SIDED_WAIT_HAND, [OTHER_1, OTHER_2, OTHER_3], *events)

    def test_winning_tile_is_ronnable_before_any_missed_win(self) -> None:
        log = self._log((1, "6p"))
        discard_index = log.index(dahai(1, "6p", tsumogiri=True))

        legal = _legal_types_at(log, seat=0, after_event=discard_index)

        self.assertIn(ActionType.RON, legal)

    def test_same_tile_type_is_not_ronnable_after_a_missed_win(self) -> None:
        log = self._log((1, "3p"), (2, "3p"))
        second = log.index(dahai(2, "3p", tsumogiri=True))

        self.assertIn(
            ActionType.RON,
            _legal_types_at(
                log, seat=0, after_event=log.index(dahai(1, "3p", tsumogiri=True))
            ),
        )
        self.assertNotIn(
            ActionType.RON, _legal_types_at(log, seat=0, after_event=second)
        )

    def test_other_wait_is_not_ronnable_after_a_missed_win(self) -> None:
        log = self._log((1, "3p"), (2, "6p"))
        second = log.index(dahai(2, "6p", tsumogiri=True))

        self.assertNotIn(
            ActionType.RON, _legal_types_at(log, seat=0, after_event=second)
        )

    def test_furiten_continues_after_the_riichi_players_own_draw(self) -> None:
        # 見逃しの後、リーチ者自身のツモ番を挟んでも解除されない。
        log = self._log((1, "3p"), (2, "E"), (3, "E"), (0, "N"), (1, "6p"))
        later = log.index(dahai(1, "6p", tsumogiri=True))

        self.assertNotIn(
            ActionType.RON, _legal_types_at(log, seat=0, after_event=later)
        )


class RiichiAnkanWaitInvarianceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(version("riichienv"), "0.4.10")

    def _legal_after_drawing_fourth_tile(self, hand):
        log = _riichi_log(
            hand,
            [NEUTRAL_1, NEUTRAL_2, NEUTRAL_3],
            tsumo(1, "E"),
            dahai(1, "E", tsumogiri=True),
            tsumo(2, "E"),
            dahai(2, "E", tsumogiri=True),
            tsumo(3, "E"),
            dahai(3, "E", tsumogiri=True),
            tsumo(0, "1m"),
            dahai(0, "1m", tsumogiri=True),
        )
        draw = log.index(tsumo(0, "1m"))
        return _legal_types_at(log, seat=0, after_event=draw)

    def test_wait_preserving_ankan_is_legal_after_riichi(self) -> None:
        legal = self._legal_after_drawing_fourth_tile(WAIT_PRESERVING_ANKAN_HAND)

        self.assertIn(ActionType.ANKAN, legal)

    def test_wait_changing_ankan_is_not_legal_after_riichi(self) -> None:
        legal = self._legal_after_drawing_fourth_tile(WAIT_CHANGING_ANKAN_HAND)

        self.assertTrue(legal)
        self.assertNotIn(ActionType.ANKAN, legal)

    def test_discard_after_riichi_is_limited_to_the_drawn_tile(self) -> None:
        log = _riichi_log(
            TWO_SIDED_WAIT_HAND,
            [OTHER_1, OTHER_2, OTHER_3],
            tsumo(1, "E"),
            dahai(1, "E", tsumogiri=True),
            tsumo(2, "S"),
            dahai(2, "S", tsumogiri=True),
            tsumo(3, "W"),
            dahai(3, "W", tsumogiri=True),
            tsumo(0, "N"),
            dahai(0, "N", tsumogiri=True),
        )
        draw = log.index(tsumo(0, "N"))
        by_prefix, _by_teacher = _replay_decisions(log)
        (decision,) = [
            item
            for (_round, seat, prefix), item in by_prefix.items()
            if seat == 0 and prefix == draw
        ]
        discards = [
            action
            for action in decision.observation.legal_actions()
            if action.action_type == ActionType.DISCARD
        ]

        self.assertEqual(len(discards), 1)


if __name__ == "__main__":
    unittest.main()
