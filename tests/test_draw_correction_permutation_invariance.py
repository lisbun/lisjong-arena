"""未公開牌の置換不変性fixture（pのgate、lisjong#390 §2、#598 Scope 7）。

engineの既存pin（`91af75e`）を使うintegration test。同じツモ直前・反応windowの状態で、
**未公開の牌**（未来の山、未公開の表示牌・裏表示牌、未使用の嶺上牌・補充牌）だけを、
合法な136枚の完全集合を保ったまま入れ替え、その時点の

- 全4席の合法手集合（`RoundState.legal_actions`）
- decisionを求められている席の観測（`build_seat_observation`）と、Policyへ渡る選択肢の射影
- 全席の公開面と各席の既知情報（手牌・河・副露・立直・フリテン・公開表示牌・残り枚数等）

が変わらないことを比べる。終局の精算は比べない（一致する必要はない）。

方法:
1. 山Wで局を進め、各decision時点tの選択列を記録する
2. 時点tで未公開の牌の集合Hを求め、Hの上の巡回置換σ（どの牌も別の位置へ動く）で
   初期の山を写したW'を作る（H以外の位置は変えない）
3. W'で同じ選択列をtまで再生し、Wの時点tと比べる

遅延槓ドラ（大明槓、`DELAY_OPEN_KAN_DORA`）は、公開されるまでは未公開の表示牌としてHに入り、
公開後はHから外れる（公開タイミングを守る）。

これは条件の検査であって、pの不偏性の完全な証明ではない（#390の仮定は文書に記録する）。
"""

import random
import unittest
from unittest.mock import patch

from lisjong_engine.action_projection import project_legal_actions
from lisjong_engine.dora import dora_tile_type
from lisjong_engine.legal_action import (
    AnkanLegalAction,
    ChiLegalAction,
    DaiminkanLegalAction,
    DiscardLegalAction,
    KakanLegalAction,
    NineTerminalsLegalAction,
    PassLegalAction,
    PonLegalAction,
    RiichiLegalAction,
    RonLegalAction,
    TsumoLegalAction,
)
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation_builder import build_seat_observation
from lisjong_engine.round_phase import RoundPhase
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat
from lisjong_engine.tile import STANDARD_TILES
from lisjong_engine.wall import DEAD_WALL_SIZE, Wall

E, S, W, N = Seat.EAST, Seat.SOUTH, Seat.WEST, Seat.NORTH
_TURN_PHASES = (RoundPhase.AWAITING_DISCARD, RoundPhase.AWAITING_RIICHI_DISCARD)
_REACTION_PHASES = (
    RoundPhase.AWAITING_REACTIONS,
    RoundPhase.AWAITING_KAKAN_REACTIONS,
    RoundPhase.AWAITING_ANKAN_REACTIONS,
)
_PRIORITY = (
    TsumoLegalAction,
    RonLegalAction,
    DaiminkanLegalAction,
    AnkanLegalAction,
    KakanLegalAction,
    PonLegalAction,
    ChiLegalAction,
    RiichiLegalAction,
    DiscardLegalAction,
    PassLegalAction,
    NineTerminalsLegalAction,
)
_DEAL_ORDER = (E, S, W, N)


# ---------------------------------------------------------------------------
# 局の駆動（MatchStateの山だけをtestで差し替える。productionには何も追加しない）
# ---------------------------------------------------------------------------


def _start(live, dead):
    match = MatchState(seed=1, rules=RuleSet.default())
    wall = Wall([STANDARD_TILES[i] for i in live], [STANDARD_TILES[i] for i in dead])
    with patch("lisjong_engine.match_state.create_round_wall", return_value=wall):
        state = match.start_round()
    return match, state


def _chooser(discard_preference=()):
    preferred = list(discard_preference)

    def choose(seat, actions, state):
        for kind in _PRIORITY:
            offered = [action for action in actions if type(action) is kind]
            if not offered:
                continue
            if kind is DiscardLegalAction and preferred:
                for action in offered:
                    if action.tile_id == preferred[0]:
                        preferred.pop(0)
                        return action
            return offered[0]
        raise AssertionError(f"{seat} has no legal action")

    return choose


def _riichi_chooser(riichi_seat, riichi_discard):
    """立直する席だけ立直し、他の席は反応をすべてPass・打牌はツモ切りにする。"""

    def choose(seat, actions, state):
        kinds = {type(action): action for action in actions}
        for kind in (TsumoLegalAction, RonLegalAction):
            if kind in kinds:
                return kinds[kind]
        if seat is riichi_seat and RiichiLegalAction in kinds:
            return kinds[RiichiLegalAction]
        discards = [a for a in actions if type(a) is DiscardLegalAction]
        if discards:
            for wanted in (riichi_discard, state.drawn_tile_id):
                for action in discards:
                    if action.tile_id == wanted:
                        return action
            return discards[0]
        return kinds[PassLegalAction]

    return choose


def _is_decision(state):
    return state.phase in _TURN_PHASES or state.phase in _REACTION_PHASES


def _advance(state, choose):
    """1 transactionを進め、selectorの選択（再生用）を返す。"""
    phase = state.phase
    if phase is RoundPhase.AWAITING_DRAW:
        state.draw(state.current_seat)
        return ("draw",)
    if phase is RoundPhase.AWAITING_RINSHAN_DRAW:
        state.draw_rinshan(state.current_seat)
        return ("rinshan",)
    if phase in _TURN_PHASES:
        seat = state.current_seat
        action = choose(seat, state.legal_actions(seat).actions, state)
        state.apply(seat, action, expected_revision=state.revision)
        return ("turn", seat, action)
    if phase in _REACTION_PHASES:
        choices = {
            seat: choose(seat, state.legal_actions(seat).actions, state)
            for seat in state.reacting_seats
        }
        state.resolve_reactions(choices, expected_revision=state.revision)
        return ("reactions", choices)
    if phase is RoundPhase.AWAITING_WIN_FINALIZATION:
        state.finalize_pending_win(expected_revision=state.revision)
        return ("finalize",)
    raise AssertionError(f"unexpected phase {phase}")


def _replay(state, steps):
    for step in steps:
        if step[0] == "draw":
            state.draw(state.current_seat)
        elif step[0] == "rinshan":
            state.draw_rinshan(state.current_seat)
        elif step[0] == "turn":
            state.apply(step[1], step[2], expected_revision=state.revision)
        elif step[0] == "reactions":
            state.resolve_reactions(step[1], expected_revision=state.revision)
        else:
            state.finalize_pending_win(expected_revision=state.revision)


# ---------------------------------------------------------------------------
# 比較
# ---------------------------------------------------------------------------


def _hidden_ids(state):
    """時点tで誰にも見えていない物理牌（未来の山 + 王牌のうち未公開のもの）。"""
    revealed = {tile.id for tile in state.revealed_dora_indicators}
    hidden = [tile.id for tile in state.remaining_tiles]
    hidden += [tile.id for tile in state.dead_wall_tiles if tile.id not in revealed]
    return hidden


def _unrevealed_dead_slots(state):
    revealed = {tile.id for tile in state.revealed_dora_indicators}
    return [tile.id for tile in state.dead_wall_tiles if tile.id not in revealed]


def _seat_facts(state, seat):
    return (
        state.hand_tiles(seat),
        state.discards(seat),
        state.melds(seat),
        state.riichi_status(seat),
        state.is_ippatsu(seat),
        state.furiten_reasons(seat),
        state.seat_wind(seat),
    )


def _public_facts(state):
    return (
        state.phase,
        state.revision,
        state.current_seat,
        state.revealed_dora_indicators,
        state.remaining_count,
        state.remaining_rinshan_count,
        state.drawn_tile,
        state.drawn_tile_source,
        state.pending_discard,
        state.pending_kan_dora_reveals,
        state.riichi_finalizations,
        state.reacting_seats if state.phase in _REACTION_PHASES else (),
    )


def _deciding_seats(state):
    if state.phase in _TURN_PHASES:
        return (state.current_seat,)
    return state.reacting_seats


def _snapshot(match, state):
    return {
        "public": _public_facts(state),
        "seats": tuple(_seat_facts(state, seat) for seat in Seat),
        "legal": tuple(state.legal_actions(seat) for seat in Seat),
        "observations": tuple(
            build_seat_observation(match, seat) for seat in _deciding_seats(state)
        ),
        "options": tuple(
            project_legal_actions(state.legal_actions(seat), state).options
            for seat in _deciding_seats(state)
        ),
    }


def _permuted_wall(live, dead, hidden, rng):
    """Hの上の巡回置換（固定点なし）で初期の山を写す。H以外の位置は変えない。"""
    order = list(hidden)
    rng.shuffle(order)
    sigma = {order[i]: order[(i + 1) % len(order)] for i in range(len(order))}
    return [sigma.get(i, i) for i in live], [sigma.get(i, i) for i in dead]


class _Coverage:
    """1 scenario内で実際に比べた時点の記録（scenarioごとにassertする）。"""

    def __init__(self):
        self.points = 0
        self.kinds = set()
        self.pending_kan_dora = 0
        self.unrevealed_slot_changed = 0
        self.riichi_tsumo = 0


def _check_round(test, live, dead, *, choose, rng, limit=None):
    """局を進め、各decision時点で置換したW'と比べる。比べた時点の記録を返す。"""
    match, state = _start(live, dead)
    coverage = _Coverage()
    steps = []
    while state.phase is not RoundPhase.FINISHED:
        if _is_decision(state) and (limit is None or coverage.points < limit):
            hidden = _hidden_ids(state)
            test.assertGreaterEqual(len(hidden), 2)
            live2, dead2 = _permuted_wall(live, dead, hidden, rng)
            test.assertEqual(sorted(live2 + dead2), list(range(136)))
            match2, state2 = _start(live2, dead2)
            _replay(state2, steps)
            context = f"step {len(steps)} phase {state.phase.value}"
            test.assertEqual(
                _snapshot(match, state), _snapshot(match2, state2), context
            )
            # 置換が自明でないこと（未公開の牌は実際に動いている）
            test.assertNotEqual(
                (state.remaining_tiles, _unrevealed_dead_slots(state)),
                (state2.remaining_tiles, _unrevealed_dead_slots(state2)),
                context,
            )
            if _unrevealed_dead_slots(state) != _unrevealed_dead_slots(state2):
                coverage.unrevealed_slot_changed += 1
            for seat in Seat:
                kinds = {type(a) for a in state.legal_actions(seat).actions}
                coverage.kinds |= kinds
                if TsumoLegalAction in kinds and state.is_riichi_established(seat):
                    coverage.riichi_tsumo += 1
            if state.pending_kan_dora_reveals:
                coverage.pending_kan_dora += 1
            coverage.points += 1
        steps.append(_advance(state, choose))
    return match, state, coverage


# ---------------------------------------------------------------------------
# scriptした山
# ---------------------------------------------------------------------------


class _Pool:
    def __init__(self):
        self.free = set(range(136))

    def take(self, kind, count=1, *, red=False):
        candidates = [kind * 4 + c for c in (0, 1, 2, 3)]
        if not red:
            candidates = [i for i in candidates if not STANDARD_TILES[i].is_red] + [
                i for i in candidates if STANDARD_TILES[i].is_red
            ]
        picked = [i for i in candidates if i in self.free][:count]
        if len(picked) != count:
            raise AssertionError(f"kind {kind} is exhausted")
        self.free -= set(picked)
        return picked


def _m(rank):
    return rank - 1


def _p(rank):
    return 8 + rank


def _s(rank):
    return 17 + rank


def _z(rank):
    return 26 + rank


def _scripted(pool, hands, draws, dead=None):
    """配牌（親から4枚×3巡 + 1枚）・ツモ順・王牌を指定した山を作る。残りは山の後ろへ。"""
    for seat in _DEAL_ORDER:
        if len(hands.get(seat, ())) > 13:
            raise AssertionError("a hand holds at most 13 tiles")
    full = {}
    for seat in _DEAL_ORDER:
        given = list(hands.get(seat, ()))
        filler = sorted(pool.free)[: 13 - len(given)]
        pool.free -= set(filler)
        full[seat] = given + filler
    live = []
    for block in range(3):
        for seat in _DEAL_ORDER:
            live += full[seat][block * 4 : block * 4 + 4]
    for seat in _DEAL_ORDER:
        live.append(full[seat][12])
    live += draws
    if dead is None:
        dead = sorted(pool.free)[:DEAD_WALL_SIZE]
    pool.free -= set(dead)
    live += sorted(pool.free)
    pool.free = set()
    assert len(dead) == DEAD_WALL_SIZE and sorted(live + dead) == list(range(136))
    return live, dead


def _ids(pool, kinds):
    return [pool.take(kind)[0] for kind in kinds]


class PermutationInvarianceTest(unittest.TestCase):
    """各scenarioで、未公開の牌を入れ替えても非終局の合法手・観測が変わらない。

    各scenarioは、自分の中で実際に比べた時点にprobeが現れたことをassertする
    （test順やskipに依存しない）。
    """

    def _run(
        self, live, dead, *, discard_preference=(), limit=None, seed=0, choose=None
    ):
        _, state, coverage = _check_round(
            self,
            live,
            dead,
            choose=choose or _chooser(discard_preference),
            rng=random.Random(seed),
            limit=limit,
        )
        self.assertGreater(coverage.points, 0)
        self.assertGreater(coverage.unrevealed_slot_changed, 0)
        return state, coverage

    def test_nine_terminals_probe(self):
        pool = _Pool()
        east = _ids(
            pool,
            [
                _m(1),
                _m(9),
                _p(1),
                _p(9),
                _s(1),
                _s(9),
                _z(1),
                _z(2),
                _z(3),
                _m(2),
                _m(3),
                _m(4),
                _p(5),
            ],
        )
        draw = _ids(pool, [_p(6)])
        live, dead = _scripted(pool, {E: east}, draw)
        match, state = _start(live, dead)
        state.draw(E)
        self.assertIn(
            NineTerminalsLegalAction, {type(a) for a in state.legal_actions(E).actions}
        )
        _, coverage = self._run(live, dead, limit=6, seed=1)
        self.assertIn(NineTerminalsLegalAction, coverage.kinds)

    def test_tsumo_probe_with_hidden_ura_and_kan_indicators(self):
        hand = [
            _m(1),
            _m(2),
            _m(3),
            _m(4),
            _m(6),
            _m(7),
            _m(8),
            _m(9),
            _p(1),
            _p(2),
            _p(3),
            _p(5),
            _m(5),
        ]
        for ura_kind in (_p(4), _z(7)):  # 裏表示牌が4p（5pが裏ドラ）/ 中
            with self.subTest(ura=ura_kind):
                pool = _Pool()
                east = _ids(pool, hand)
                draw = _ids(pool, [_p(5)])
                indicator = _ids(pool, [_z(5)])
                ura = _ids(pool, [ura_kind])
                rest = sorted(pool.free - set(draw))[:12]
                pool.free -= set(rest)
                # 王牌: 嶺上4枚、表示牌・裏表示牌の5組（先頭の組が公開中の表示牌と裏）
                dead = rest[:4] + indicator + ura + rest[4:12]
                live, dead = _scripted(pool, {E: east}, draw, dead)
                match, state = _start(live, dead)
                state.draw(E)
                self.assertIn(
                    TsumoLegalAction, {type(a) for a in state.legal_actions(E).actions}
                )
                _, coverage = self._run(live, dead, limit=3, seed=2)
                self.assertIn(TsumoLegalAction, coverage.kinds)

    def test_ron_probe_without_yaku_is_not_made_legal_by_hidden_dora(self):
        # 南: 234m 678m 345p 678p + 9s単騎（役なし）。東の初打9s。
        # 未公開の表示牌・裏表示牌に8s（9sがドラ）を置いても、ロンは合法にならない。
        for hidden_indicator in (_s(8), _z(6)):
            with self.subTest(hidden_indicator=hidden_indicator):
                pool = _Pool()
                south = _ids(
                    pool,
                    [
                        _m(2),
                        _m(3),
                        _m(4),
                        _m(6),
                        _m(7),
                        _m(8),
                        _p(3),
                        _p(4),
                        _p(5),
                        _p(6),
                        _p(7),
                        _p(8),
                        _s(9),
                    ],
                )
                east_nine = _ids(pool, [_s(9)])
                east = east_nine + _ids(
                    pool,
                    [
                        _z(1),
                        _z(2),
                        _z(3),
                        _z(4),
                        _z(5),
                        _z(6),
                        _m(1),
                        _m(1),
                        _p(1),
                        _p(1),
                        _s(1),
                        _s(1),
                    ],
                )
                draw = _ids(pool, [_z(7)])
                dora8 = _ids(pool, [hidden_indicator, hidden_indicator])
                rest = sorted(pool.free)[:12]
                pool.free -= set(rest)
                dead = rest[:4] + rest[4:5] + [dora8[0]] + [dora8[1]] + rest[5:12]
                live, dead = _scripted(pool, {E: east, S: south}, draw, dead)
                self._run(live, dead, discard_preference=east_nine, limit=4, seed=3)
                match, state = _start(live, dead)
                state.draw(E)
                state.apply(
                    E,
                    DiscardLegalAction(east_nine[0]),
                    expected_revision=state.revision,
                )
                self.assertIs(state.phase, RoundPhase.AWAITING_REACTIONS)
                self.assertNotIn(
                    RonLegalAction, {type(a) for a in state.legal_actions(S).actions}
                )

    def test_ron_probe_with_yaku(self):
        # 南: 234m 678m 345p 678p + 5s単騎（断么九）。東の初打5s -> ロンが合法。
        pool = _Pool()
        south = _ids(
            pool,
            [
                _m(2),
                _m(3),
                _m(4),
                _m(6),
                _m(7),
                _m(8),
                _p(3),
                _p(4),
                _p(5),
                _p(6),
                _p(7),
                _p(8),
                _s(5),
            ],
        )
        east_five = _ids(pool, [_s(5)])
        east = east_five + _ids(
            pool,
            [
                _z(1),
                _z(2),
                _z(3),
                _z(4),
                _z(5),
                _z(6),
                _m(1),
                _m(1),
                _p(1),
                _p(1),
                _s(1),
                _s(1),
            ],
        )
        draw = _ids(pool, [_z(7)])
        live, dead = _scripted(pool, {E: east, S: south}, draw)
        match, state = _start(live, dead)
        state.draw(E)
        state.apply(
            E, DiscardLegalAction(east_five[0]), expected_revision=state.revision
        )
        self.assertIn(RonLegalAction, {type(a) for a in state.legal_actions(S).actions})
        _, coverage = self._run(
            live, dead, discard_preference=east_five, limit=3, seed=4
        )
        self.assertIn(RonLegalAction, coverage.kinds)

    def test_delayed_open_kan_dora_is_hidden_until_revealed(self):
        # 東の初打7p、南が7p×3で大明槓。槓ドラは南の打牌がロン以外で解決するまで未公開。
        pool = _Pool()
        south = _ids(pool, [_p(7), _p(7), _p(7)])
        east_seven = _ids(pool, [_p(7)])
        east = east_seven + _ids(
            pool,
            [
                _z(1),
                _z(1),
                _z(2),
                _z(2),
                _z(3),
                _z(3),
                _z(4),
                _z(4),
                _m(9),
                _m(9),
                _s(9),
                _s(9),
            ],
        )
        draw = _ids(pool, [_z(5)])
        live, dead = _scripted(pool, {E: east, S: south}, draw)
        _, coverage = self._run(
            live, dead, discard_preference=east_seven, limit=8, seed=5
        )
        self.assertIn(DaiminkanLegalAction, coverage.kinds)
        self.assertGreater(coverage.pending_kan_dora, 0)

    def test_random_walls_full_round(self):
        for wall_seed in (598001, 598002):
            with self.subTest(wall_seed=wall_seed):
                order = list(range(136))
                random.Random(wall_seed).shuffle(order)
                live, dead = order[:-DEAD_WALL_SIZE], order[-DEAD_WALL_SIZE:]
                state, coverage = self._run(live, dead, seed=wall_seed)
                self.assertIs(state.phase, RoundPhase.FINISHED)
                self.assertGreater(coverage.points, 20)

    def test_riichi_tsumo_probe_with_effective_ura_dora(self):
        # 東: 123m 456m 789m 23p 55p。第1ツモ北を立直宣言牌にして1p-4p待ちで立直、
        # 他家はツモ切り・全員Pass、東の第2ツモ1pでツモ（立直成立後の和了probe）。
        # 公開中の表示牌の裏を4p（5pが裏ドラ2枚）/ 中（裏ドラ0枚）にした2つの合法な状態で、
        # どちらもツモがofferされ、裏ドラ枚数が実際に変わることを確かめる。
        hand = [_m(r) for r in range(1, 10)] + [_p(2), _p(3), _p(5), _p(5)]
        ura_counts = {}
        for ura_kind in (_p(4), _z(7)):
            with self.subTest(ura=ura_kind):
                pool = _Pool()
                east = _ids(pool, hand)
                riichi_discard = _ids(pool, [_z(4)])
                draws = riichi_discard + _ids(pool, [_z(3), _z(2), _z(1), _p(1)])
                indicator = _ids(pool, [_z(5)])
                ura = _ids(pool, [ura_kind])
                rest = sorted(pool.free)[:12]
                pool.free -= set(rest)
                dead = rest[:4] + indicator + ura + rest[4:12]
                live, dead = _scripted(pool, {E: east}, draws, dead)
                choose = _riichi_chooser(E, riichi_discard[0])

                match, state = _start(live, dead)
                # 配牌52枚のあと、東・南・西・北・東の5回ツモした東の手番まで進める
                while not (
                    state.phase is RoundPhase.AWAITING_DISCARD
                    and state.current_seat is E
                    and state.remaining_count == len(live) - 52 - 5
                ):
                    _advance(state, choose)
                self.assertTrue(state.is_riichi_established(E))
                self.assertIn(
                    TsumoLegalAction, {type(a) for a in state.legal_actions(E).actions}
                )
                ura_types = [
                    dora_tile_type(tile)
                    for tile in state.corresponding_ura_dora_indicators
                ]
                ura_counts[ura_kind] = sum(
                    tile.tile_type in ura_types for tile in state.hand_tiles(E)
                )

                _, coverage = self._run(live, dead, seed=6, choose=choose)
                self.assertGreater(coverage.riichi_tsumo, 0)
        self.assertEqual(ura_counts, {_p(4): 2, _z(7): 0})


if __name__ == "__main__":
    unittest.main()
