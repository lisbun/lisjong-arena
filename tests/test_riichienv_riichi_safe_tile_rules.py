"""lisjong #302: RiichiEnvが確定安全牌の前提とする2つの規則を実装していることの確認。

`lisjong.belief.riichi_safe_tiles`は次の規則を前提にする。

1. リーチ後の見逃しは、その局の終わりまでフリテンになる
2. リーチ後は待ちが変わらない（暗槓は待ちの牌種を変えない場合にだけ許される）

ここでは、固定versionのRiichiEnv（`riichienv.MjaiReplay`の`Kyoku.steps()`）が各判断時点で
提示する合法手を、合成した局面で直接確かめる。Arena側のreplay補正
（`_confirmed_permanent_riichi_furiten`）を通さない。
"""

import json
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
    tile_counts,
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


def _decisions(log):
    """logが物理枚数の上限を超えていないことを確かめてから、replayの判断列を返す。"""
    over = {tile: count for tile, count in tile_counts(log).items() if count > 4}
    if over:
        raise AssertionError(f"fixture uses more than four copies: {over}")
    by_prefix, _by_teacher = _replay_decisions(log)
    return by_prefix


def _decision_after(by_prefix, *, seat, event_index):
    """`event_index`番目のeventの直後に`seat`へ提示された判断。提示されなければ`None`。"""
    found = [
        decision
        for (_round, decision_seat, prefix), decision in by_prefix.items()
        if decision_seat == seat and prefix == event_index
    ]
    if not found:
        return None
    (decision,) = found
    return decision


def _legal_types(decision):
    return frozenset(
        action.action_type for action in decision.observation.legal_actions()
    )


def _replayed_past(by_prefix, event_index):
    """replayが`event_index`より後の判断まで進んだか。"""
    return any(prefix > event_index for (_round, _seat, prefix) in by_prefix)


class RiichiMissedWinFuritenTest(unittest.TestCase):
    """待ちが3p・6pのリーチ者（seat 0）への、見逃しの前後のロンの提示。

    RiichiEnvは、応答できる合法手がない席へ判断を提示しない。そのため「ロンが提示されない」は
    「その打牌の直後にseat 0の判断がない」として現れる。これを「局面を取得できなかった」と
    区別するため、どのtestも次を確かめる。

    - replayが対象の打牌より後の判断まで進んでいる
    - 見逃しのない対照局面では、同じ打牌の直後にseat 0へロンが提示される
    """

    def setUp(self) -> None:
        self.assertEqual(version("riichienv"), "0.4.10")

    def _log(self, *discards):
        events = []
        for actor, tile in discards:
            events.extend([tsumo(actor, tile), dahai(actor, tile, tsumogiri=True)])
        return _riichi_log(TWO_SIDED_WAIT_HAND, [OTHER_1, OTHER_2, OTHER_3], *events)

    def _ron_offered(self, discards, target):
        """`target`の打牌の直後に、seat 0へロンが提示されたか。

        対象の打牌の直後へもう1巡ぶんの打牌を足し、replayが対象より後まで進んだことを
        確かめる。対象の打牌を選んだ席の判断（直前のツモ）が存在することも確かめる。
        """
        actor, tile = target
        trailing = ((actor + 1) % 4, "9m")
        log = self._log(*discards, target, trailing)
        by_prefix = _decisions(log)
        draw_index = len(log) - 1 - log[::-1].index(tsumo(actor, tile))
        discard_index = draw_index + 1
        self.assertEqual(log[discard_index], dahai(actor, tile, tsumogiri=True))
        self.assertIsNotNone(
            _decision_after(by_prefix, seat=actor, event_index=draw_index)
        )
        self.assertTrue(_replayed_past(by_prefix, discard_index))

        decision = _decision_after(by_prefix, seat=0, event_index=discard_index)
        if decision is None:
            return False
        legal = _legal_types(decision)
        # 判断が提示されるなら、ロンか見送りの選択である。
        self.assertIn(ActionType.PASS, legal)
        return ActionType.RON in legal

    def test_winning_tile_is_ronnable_before_any_missed_win(self) -> None:
        self.assertTrue(self._ron_offered((), (1, "3p")))
        self.assertTrue(self._ron_offered((), (1, "6p")))

    def test_same_tile_type_is_not_ronnable_after_a_missed_win(self) -> None:
        # 対照：先に切られるのが待ち牌でなければ、seat 2の3pにロンが提示される。
        self.assertTrue(self._ron_offered(((1, "9s"),), (2, "3p")))

        self.assertFalse(self._ron_offered(((1, "3p"),), (2, "3p")))

    def test_other_wait_is_not_ronnable_after_a_missed_win(self) -> None:
        self.assertTrue(self._ron_offered(((1, "9s"),), (2, "6p")))

        self.assertFalse(self._ron_offered(((1, "3p"),), (2, "6p")))

    def test_furiten_continues_after_the_riichi_players_own_draw(self) -> None:
        # 見逃しの後、リーチ者自身のツモ番（seat 0の9mツモ切り）を挟む。
        between = ((2, "9s"), (3, "9s"), (0, "9m"))
        # 対照：見逃しがなければ、ツモ番を挟んだ後の6pにロンが提示される。
        self.assertTrue(self._ron_offered(((1, "9s"), *between), (1, "6p")))

        self.assertFalse(self._ron_offered(((1, "3p"), *between), (1, "6p")))

    def test_the_missed_win_itself_was_offered(self) -> None:
        # 上のtestで「見逃した」とする1回目の3pに、実際にロンが提示されている。
        log = self._log((1, "3p"), (2, "3p"), (3, "9m"))
        by_prefix = _decisions(log)
        first = log.index(dahai(1, "3p", tsumogiri=True))

        decision = _decision_after(by_prefix, seat=0, event_index=first)

        self.assertIsNotNone(decision)
        self.assertIn(ActionType.RON, _legal_types(decision))


class RiichiAnkanWaitInvarianceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(version("riichienv"), "0.4.10")

    def _decision_after_drawing(self, hand, drawn):
        log = _riichi_log(
            hand,
            [NEUTRAL_1, NEUTRAL_2, NEUTRAL_3],
            tsumo(1, "E"),
            dahai(1, "E", tsumogiri=True),
            tsumo(2, "E"),
            dahai(2, "E", tsumogiri=True),
            tsumo(3, "E"),
            dahai(3, "E", tsumogiri=True),
            tsumo(0, drawn),
            dahai(0, drawn, tsumogiri=True),
        )
        by_prefix = _decisions(log)
        decision = _decision_after(
            by_prefix, seat=0, event_index=log.index(tsumo(0, drawn))
        )
        self.assertIsNotNone(decision)
        return decision

    def test_wait_preserving_ankan_is_legal_after_riichi(self) -> None:
        decision = self._decision_after_drawing(WAIT_PRESERVING_ANKAN_HAND, "1m")

        self.assertIn(ActionType.ANKAN, _legal_types(decision))

    def test_wait_changing_ankan_is_not_legal_after_riichi(self) -> None:
        decision = self._decision_after_drawing(WAIT_CHANGING_ANKAN_HAND, "1m")

        legal = _legal_types(decision)
        self.assertIn(ActionType.DISCARD, legal)
        self.assertNotIn(ActionType.ANKAN, legal)

    def test_only_the_drawn_tile_can_be_discarded_after_riichi(self) -> None:
        decision = self._decision_after_drawing(TWO_SIDED_WAIT_HAND, "S")

        discards = [
            json.loads(action.to_mjai())
            for action in decision.observation.legal_actions()
            if action.action_type == ActionType.DISCARD
        ]

        # 打牌の合法手は1つで、それはツモったSである。手牌の13枚は選べない。
        self.assertEqual(len(discards), 1)
        self.assertEqual(discards[0]["type"], "dahai")
        self.assertEqual(discards[0]["actor"], 0)
        self.assertEqual(discards[0]["pai"], "S")
        self.assertNotIn("S", TWO_SIDED_WAIT_HAND)


if __name__ == "__main__":
    unittest.main()
