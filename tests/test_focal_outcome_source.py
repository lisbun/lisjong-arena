"""Unit tests for the #359 L0.3 B focal outcome source producer.

実RiichiEnvは起動しない。score boundaryはsynthetic MJAI event列、対局構成は
単一game実行境界（``run_focal_game`` / ``write_game``）の差し替えで固定する。
実対局とpinned lisjong consumerによるstrict readは
``test_focal_outcome_source_integration.py``が担当する。
"""

import json
import subprocess
import tempfile
import tomllib
import unittest
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _focal_outcome_source_fixtures import binding, source_contract
from lisjong.learning import (
    CONSTANT_RESIDUAL_RUNTIME_IDENTITY,
    SemanticEnvelopeOffensePolicy,
    select_residual_exploration,
)
from lisjong.learning import outcome_source as lisjong_outcome_source
from lisjong.policy_contract import DecisionTrace, Seat, Wind

from lisjong_arena.environment_identity import EnvironmentCheck, InstalledVcsIdentity
from lisjong_arena.focal_outcome_source import source
from lisjong_arena.focal_outcome_source.accounting import (
    FocalOutcomeSourceError,
    KyokuAccount,
    account_events,
    account_game,
    require_final_adjustment,
)
from lisjong_arena.focal_outcome_source.adapter import (
    FocalAdapterError,
    FocalExplorationPolicy,
)
from lisjong_arena.focal_outcome_source.exploration_token import (
    EXPLORATION_TOKEN_IDENTITY,
    exploration_token,
    exploration_token_payload,
)
from lisjong_arena.offense_foundation.fixtures import probes
from lisjong_arena.riichienv.local_game_runner import SeatDecisionObservation
from lisjong_arena.riichienv.round_result import RoundResultCollector

# test専用seed。seed ledgerのどのallocationとも重ならない範囲を使う。
TEST_SEEDS = tuple(range(900000, 900010))

START = [25000, 25000, 25000, 25000]


def start_kyoku(scores, *, kyoku=1, honba=0, kyotaku=0, oya=0, bakaze="E"):
    return {
        "bakaze": bakaze,
        "dora_marker": "1m",
        "honba": honba,
        "kyoku": kyoku,
        "kyotaku": kyotaku,
        "oya": oya,
        "scores": list(scores),
        "type": "start_kyoku",
    }


def reach(actor):
    return [{"actor": actor, "type": "reach"}, reach_accepted(actor)]


def reach_accepted(actor):
    return {"actor": actor, "type": "reach_accepted"}


def hora(actor, target, deltas):
    return {
        "actor": actor,
        "deltas": list(deltas),
        "target": target,
        "tsumo": actor == target,
        "type": "hora",
        "ura_markers": [],
    }


def ryukyoku(deltas, reason="exhaustive_draw"):
    return {"deltas": list(deltas), "reason": reason, "type": "ryukyoku"}


def game(*kyokus):
    events = [{"type": "start_game"}]
    for kyoku_events in kyokus:
        events.extend(kyoku_events)
        events.append({"type": "end_kyoku"})
    events.append({"type": "end_game"})
    return events


def fake_inspection(events, final_scores, final_sticks=0):
    """同じevent列から実``RoundResultCollector``でRoundResultを作る。"""
    env = SimpleNamespace(
        scores=lambda: list(final_scores),
        riichi_sticks=final_sticks,
        win_results=None,
    )
    collector = RoundResultCollector()
    collector.on_new_events(events, 0, env)
    return SimpleNamespace(
        game_trace=SimpleNamespace(
            events=tuple(
                SimpleNamespace(sequence=index, event=json.dumps(event))
                for index, event in enumerate(events)
            )
        ),
        round_results=collector.build(),
        result=SimpleNamespace(scores=tuple(final_scores)),
    )


class ScoreBoundaryTest(unittest.TestCase):
    def test_riichi_deposit_is_carried_into_the_next_kyoku(self):
        events = game(
            [
                start_kyoku(START),
                *reach(0),
                ryukyoku([1500, -1500, -1500, 1500]),
            ],
            [
                start_kyoku([25500, 23500, 23500, 26500], honba=1, kyotaku=1),
                hora(2, 1, [0, -3300, 4300, 0]),
            ],
        )
        first, second = account_events(events)
        self.assertEqual(first.points_after_kyoku, (25500, 23500, 23500, 26500))
        self.assertEqual(first.riichi_sticks_after, 1)
        self.assertEqual(second.riichi_sticks_before, 1)
        self.assertEqual(second.riichi_sticks_after, 0)
        self.assertEqual(second.points_after_kyoku, (25500, 20200, 27800, 26500))

    def test_riichi_and_noten_payment_in_one_kyoku_are_counted_exactly_once(self):
        # reach_acceptedの1000点控除はterminal deltasに含まれない。
        (account,) = account_events(
            game([start_kyoku(START), *reach(3), ryukyoku([-1000, -1000, -1000, 3000])])
        )
        self.assertEqual(account.points_after_kyoku, (24000, 24000, 24000, 27000))
        self.assertEqual(account.riichi_sticks_after, 1)

    # #364: RiichiEnv 0.4.10 emits riichi deposits already recorded by
    # reach_accepted again in ryukyoku.deltas on an all-four-tenpai exhaustive
    # draw (upstream smly/RiichiEnv#247). That event is inconsistent, and the
    # producer must keep rejecting it. The minimal kyoku mirrors the one found
    # by a diagnostic-only replay (not scientific data).
    ALL_TENPAI_START = [32600, 23500, 17400, 26500]

    def _all_tenpai_three_riichi_draw(self, deltas):
        return game(
            [
                start_kyoku(self.ALL_TENPAI_START, honba=2),
                *reach(3),
                *reach(2),
                *reach(0),
                ryukyoku(deltas),
            ]
        )

    def test_draw_deltas_repeating_accepted_riichi_deposits_fail_closed(self):
        with self.assertRaisesRegex(
            FocalOutcomeSourceError, "kyoku score/riichi-stick conservation is violated"
        ):
            account_events(self._all_tenpai_three_riichi_draw([-1000, 0, -1000, -1000]))

    def test_all_tenpai_draw_without_transfer_conserves_three_deposits(self):
        (account,) = account_events(self._all_tenpai_three_riichi_draw([0, 0, 0, 0]))
        self.assertEqual(account.points_after_kyoku, (31600, 23500, 16400, 25500))
        self.assertEqual(account.riichi_sticks_after, 3)

    def test_ron_on_the_riichi_declaration_tile_does_not_deduct_the_deposit(self):
        (account,) = account_events(
            game(
                [
                    start_kyoku(START),
                    {"actor": 1, "type": "reach"},
                    hora(0, 1, [3900, -3900, 0, 0]),
                ]
            )
        )
        self.assertEqual(account.points_after_kyoku, (28900, 21100, 25000, 25000))
        self.assertEqual(account.riichi_sticks_after, 0)

    def test_hanchan_ending_with_unclaimed_sticks_keeps_the_pre_final_boundary(self):
        events = game(
            [
                start_kyoku([30000, 20000, 25000, 24000], kyotaku=1),
                *reach(1),
                ryukyoku([0, 0, 0, 0], reason="exhaustive_draw"),
            ]
        )
        # backend end-game処理: 残存2本をtop seatへ帰属し、供託を0にする。
        final_scores = (32000, 19000, 25000, 24000)
        inspection = fake_inspection(events, final_scores, final_sticks=0)
        account = account_game(inspection)
        (kyoku,) = account.kyokus
        self.assertEqual(kyoku.points_after_kyoku, (30000, 19000, 25000, 24000))
        self.assertEqual(kyoku.riichi_sticks_after, 2)
        self.assertEqual(account.hanchan_final_scores, final_scores)
        self.assertEqual(account.hanchan_final_riichi_sticks, 0)
        # existing RoundResultはpost-final値を持ち続ける（変更しない）。
        self.assertEqual(inspection.round_results[-1].end_scores, final_scores)
        self.assertNotEqual(kyoku.points_after_kyoku, final_scores)

    def test_final_adjustment_must_be_exactly_one_seat_receiving_the_sticks(self):
        after = (30000, 19000, 25000, 24000)
        require_final_adjustment(after, 2, (30000, 19000, 25000, 26000), 0)
        for final_scores, final_sticks in (
            ((31000, 19000, 26000, 24000), 0),  # 2 seatへ分割
            ((31000, 19000, 25000, 24000), 0),  # 金額不足
            ((32000, 19000, 25000, 24000), 1),  # backendに供託が残る
            ((30000, 19000, 25000, 24000), 0),  # 帰属なし
        ):
            with self.subTest(final_scores=final_scores, final_sticks=final_sticks):
                with self.assertRaises(FocalOutcomeSourceError):
                    require_final_adjustment(after, 2, final_scores, final_sticks)
        require_final_adjustment(after, 0, after, 0)
        with self.assertRaises(FocalOutcomeSourceError):
            require_final_adjustment(after, 0, (30000, 18000, 25000, 25000), 0)

    def test_final_win_collects_the_sticks(self):
        events = game(
            [
                start_kyoku([30000, 20000, 25000, 23000], kyotaku=2),
                *reach(0),
                hora(3, 3, [-1000, -1000, -2000, 7000]),
            ]
        )
        final = (28000, 19000, 23000, 30000)
        account = account_game(fake_inspection(events, final))
        self.assertEqual(account.kyokus[-1].points_after_kyoku, final)
        self.assertEqual(account.kyokus[-1].riichi_sticks_after, 0)

    def test_honba_and_renchan_are_part_of_the_kyoku_identity(self):
        events = game(
            [
                start_kyoku(START, kyoku=1, honba=0),
                hora(0, 0, [6000, -2000, -2000, -2000]),
            ],
            [
                start_kyoku([31000, 23000, 23000, 23000], kyoku=1, honba=1),
                hora(0, 2, [6100, 0, -6100, 0]),
            ],
        )
        first, second = account_events(events)
        self.assertEqual((first.hand_number, first.honba), (1, 0))
        self.assertEqual((second.hand_number, second.honba), (1, 1))
        rows = [
            source.kyoku_row(item, game_ordinal=0, kyoku_ordinal=index, is_final=False)
            for index, item in enumerate((first, second))
        ]
        self.assertEqual([row["honba"] for row in rows], [0, 1])
        self.assertEqual(rows[1]["points_after_kyoku"], [37100, 23000, 16900, 23000])

    def test_double_ron_applies_each_hora_once_and_collects_sticks_once(self):
        (account,) = account_events(
            game(
                [
                    start_kyoku(START, kyotaku=1),
                    hora(1, 0, [-9000, 10000, 0, 0]),
                    hora(2, 0, [-2000, 0, 2000, 0]),
                ]
            )
        )
        self.assertEqual(account.winner_seats, (Seat(1), Seat(2)))
        self.assertEqual(account.points_after_kyoku, (14000, 35000, 27000, 25000))
        self.assertEqual(account.riichi_sticks_after, 0)
        row = source.kyoku_row(account, game_ordinal=0, kyoku_ordinal=0, is_final=True)
        self.assertEqual(row["end"], {"kind": "win", "winner_seats": [1, 2]})

    def test_abortive_draw_keeps_sticks_and_records_the_draw_kind(self):
        (account,) = account_events(
            game(
                [
                    start_kyoku(START),
                    *reach(2),
                    ryukyoku([0, 0, 0, 0], reason="four_riichi"),
                ]
            )
        )
        self.assertEqual(account.draw_kind, "four_riichi")
        self.assertEqual(account.riichi_sticks_after, 1)
        row = source.kyoku_row(account, game_ordinal=0, kyoku_ordinal=0, is_final=True)
        self.assertEqual(row["end"], {"kind": "draw", "draw_kind": "four_riichi"})

    def test_non_final_derivation_must_match_the_next_start_kyoku(self):
        events = game(
            [start_kyoku(START), *reach(0), ryukyoku([0, 0, 0, 0])],
            # kyotakuが1ではなく0
            [start_kyoku([24000, 25000, 25000, 25000]), ryukyoku([0, 0, 0, 0])],
        )
        with self.assertRaisesRegex(FocalOutcomeSourceError, "next start_kyoku"):
            account_events(events)

    def test_missing_duplicate_or_misordered_events_fail_closed(self):
        cases = {
            "reach_accepted without reach": game(
                [start_kyoku(START), reach_accepted(0), ryukyoku([0, 0, 0, 0])]
            ),
            "duplicate hora": game(
                [
                    start_kyoku(START),
                    hora(1, 0, [-1000, 1000, 0, 0]),
                    hora(1, 0, [-1000, 1000, 0, 0]),
                ]
            ),
            "hora after ryukyoku": game(
                [start_kyoku(START), ryukyoku([0, 0, 0, 0]), hora(1, 0, [-1, 1, 0, 0])]
            ),
            "reach after terminal": game(
                [start_kyoku(START), ryukyoku([0, 0, 0, 0]), *reach(1)]
            ),
            "kyoku without terminal": game([start_kyoku(START)]),
            "conservation": game([start_kyoku(START), hora(1, 0, [-1000, 2000, 0, 0])]),
            "missing end_game": game([start_kyoku(START), ryukyoku([0, 0, 0, 0])])[:-1],
            "missing start_game": game([start_kyoku(START), ryukyoku([0, 0, 0, 0])])[
                1:
            ],
            "event after end_game": game([start_kyoku(START), ryukyoku([0, 0, 0, 0])])
            + [ryukyoku([0, 0, 0, 0])],
        }
        for name, events in cases.items():
            with self.subTest(name), self.assertRaises(FocalOutcomeSourceError):
                account_events(events)

    def test_account_must_agree_with_the_captured_round_results(self):
        events = game([start_kyoku(START), hora(1, 0, [-1000, 1000, 0, 0])])
        inspection = fake_inspection(events, (24000, 26000, 25000, 25000))
        tampered = [json.loads(item.event) for item in inspection.game_trace.events]
        tampered[1]["honba"] = 3
        inspection.game_trace = SimpleNamespace(
            events=tuple(
                SimpleNamespace(sequence=index, event=json.dumps(event))
                for index, event in enumerate(tampered)
            )
        )
        with self.assertRaisesRegex(FocalOutcomeSourceError, "RoundResult"):
            account_game(inspection)


class ExplorationTokenTest(unittest.TestCase):
    def test_golden_vector(self):
        self.assertEqual(
            EXPLORATION_TOKEN_IDENTITY,
            "lisjong-arena-l0.3-focal-decision-token-sha256-v1",
        )
        self.assertEqual(
            exploration_token_payload(
                game_seed=900000, focal_seat=2, focal_decision_ordinal=17
            ),
            '{"focal_decision_ordinal":17,"focal_seat":2,"game_seed":900000,'
            '"identity":"lisjong-arena-l0.3-focal-decision-token-sha256-v1"}',
        )
        self.assertEqual(
            exploration_token(
                game_seed=900000, focal_seat=2, focal_decision_ordinal=17
            ),
            "d69a5293f147051d5dc956a9bbb2f39852e985567d684911102759a1fc698169",
        )

    def test_invalid_inputs_fail_closed(self):
        for kwargs in (
            {"game_seed": True, "focal_seat": 0, "focal_decision_ordinal": 0},
            {"game_seed": 1, "focal_seat": 4, "focal_decision_ordinal": 0},
            {"game_seed": 1, "focal_seat": 0, "focal_decision_ordinal": -1},
            {"game_seed": 1.0, "focal_seat": 0, "focal_decision_ordinal": 0},
        ):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                exploration_token(**kwargs)

    def test_consumer_identity_matches(self):
        self.assertEqual(
            EXPLORATION_TOKEN_IDENTITY,
            lisjong_outcome_source.EXPLORATION_TOKEN_IDENTITY,
        )
        self.assertEqual(
            source.FOCAL_ROTATION_RULE, lisjong_outcome_source.FOCAL_ROTATION_RULE
        )
        self.assertEqual(source.BEHAVIOR, lisjong_outcome_source.EXPECTED_BEHAVIOR)
        self.assertEqual(
            source.OUTCOME_SOURCE_SCHEMA, lisjong_outcome_source.OUTCOME_SOURCE_SCHEMA
        )
        self.assertEqual(
            source.OUTCOME_SOURCE_KIND, lisjong_outcome_source.OUTCOME_SOURCE_KIND
        )

    def test_pinned_lisjong_revision_matches_the_project_pin(self):
        project = Path(__file__).resolve().parents[1] / "pyproject.toml"
        dependencies = tomllib.loads(project.read_text(encoding="utf-8"))["project"][
            "dependencies"
        ]
        (lisjong,) = [item for item in dependencies if item.startswith("lisjong @")]
        self.assertTrue(lisjong.endswith("@" + source.PINNED_LISJONG_REVISION))


class CountingSelector:
    def __init__(self):
        self.calls = []

    def __call__(self, decision, token):
        self.calls.append((decision, token))
        return select_residual_exploration(decision, token)


class FocalAdapterTest(unittest.TestCase):
    def setUp(self):
        self.contexts = tuple(probe.context for probe in probes())

    def test_selector_is_called_once_per_decision_and_bound_to_it(self):
        selector = CountingSelector()
        adapter = FocalExplorationPolicy(
            game_seed=TEST_SEEDS[0], focal_seat=Seat(0), selector=selector
        )
        for context in self.contexts:
            action = adapter.choose_action(context)
            self.assertIn(action, context.legal_actions)
        self.assertEqual(len(selector.calls), len(self.contexts))
        for ordinal, (capture, (called_decision, called_token)) in enumerate(
            zip(adapter.captures, selector.calls, strict=True)
        ):
            self.assertEqual(capture.focal_decision_ordinal, ordinal)
            self.assertIs(capture.decision, self.contexts[ordinal])
            self.assertIs(called_decision, self.contexts[ordinal])
            self.assertEqual(capture.exploration_token, called_token)
            self.assertEqual(
                called_token,
                exploration_token(
                    game_seed=TEST_SEEDS[0],
                    focal_seat=0,
                    focal_decision_ordinal=ordinal,
                ),
            )

    def test_guard_and_single_survivor_decisions_advance_the_ordinal(self):
        adapter = FocalExplorationPolicy(game_seed=TEST_SEEDS[0], focal_seat=Seat(0))
        for context in self.contexts:
            adapter.choose_action(context)
        kinds = [capture.selection.kind.value for capture in adapter.captures]
        self.assertIn("win", kinds)
        self.assertIn("riichi", kinds)
        self.assertIn("response", kinds)
        single = [
            capture
            for capture in adapter.captures
            if capture.selection.survivors is not None
            and len(capture.selection.survivors) == 1
        ]
        self.assertTrue(single)
        self.assertEqual(
            [capture.focal_decision_ordinal for capture in adapter.captures],
            list(range(len(self.contexts))),
        )

    def test_rejects_another_seats_decision(self):
        adapter = FocalExplorationPolicy(game_seed=TEST_SEEDS[0], focal_seat=Seat(1))
        with self.assertRaises(FocalAdapterError):
            adapter.choose_action(self.contexts[0])
        self.assertEqual(adapter.captures, ())


def _execution(contexts, *, seed, non_focal_per_step):
    """focal seat 0のfake execution。各stepの前にnon-focal decisionを挟む。"""
    adapter = FocalExplorationPolicy(game_seed=seed, focal_seat=Seat(0))
    steps = []
    ordinal = 0
    for context in contexts:
        for _ in range(non_focal_per_step):
            steps.append(
                SimpleNamespace(
                    step_ordinal=ordinal,
                    event_sequence_start=10 + ordinal,
                    seat_decisions=(SimpleNamespace(seat=Seat(1)),),
                )
            )
            ordinal += 1
        action = adapter.choose_action(context)
        steps.append(
            SimpleNamespace(
                step_ordinal=ordinal,
                event_sequence_start=10 + ordinal,
                seat_decisions=(
                    SeatDecisionObservation(
                        seat=Seat(0),
                        policy_input=context.input,
                        decision_trace=DecisionTrace(
                            legal_actions=context.legal_actions,
                            selected_action=action,
                        ),
                    ),
                ),
            )
        )
        ordinal += 1
    return source.FocalGameExecution(
        seed=seed,
        focal_seat=Seat(0),
        inspection=SimpleNamespace(step_observations=tuple(steps)),
        captures=adapter.captures,
    )


KYOKU = KyokuAccount(
    start_event_sequence=0,
    round_wind=Wind.EAST,
    hand_number=1,
    honba=0,
    dealer_seat=Seat(0),
    riichi_sticks_before=0,
    riichi_sticks_after=0,
    points_before_kyoku=(25000,) * 4,
    points_after_kyoku=(25000,) * 4,
    winner_seats=(Seat(0),),
    draw_kind=None,
)


class FocalDecisionRowTest(unittest.TestCase):
    def setUp(self):
        self.contexts = tuple(probe.context for probe in probes())

    def test_other_seats_decision_count_does_not_change_focal_tokens(self):
        rows = {
            count: source.focal_decision_rows(
                _execution(self.contexts, seed=TEST_SEEDS[1], non_focal_per_step=count),
                (KYOKU,),
                game_ordinal=0,
            )
            for count in (0, 3)
        }
        self.assertEqual(
            [row["exploration_token"] for row in rows[0]],
            [row["exploration_token"] for row in rows[3]],
        )
        self.assertEqual(
            [row["focal_decision_ordinal"] for row in rows[3]],
            list(range(len(self.contexts))),
        )
        self.assertNotEqual(
            [row["step_ordinal"] for row in rows[0]],
            [row["step_ordinal"] for row in rows[3]],
        )

    def test_rows_record_survivors_only_for_discard_decisions(self):
        execution = _execution(self.contexts, seed=TEST_SEEDS[1], non_focal_per_step=0)
        rows = source.focal_decision_rows(execution, (KYOKU,), game_ordinal=0)
        for row, capture in zip(rows, execution.captures, strict=True):
            survivors = capture.selection.survivor_actions
            if capture.selection.kind.value == "discard":
                self.assertEqual(len(row["producer_survivor_actions"]), len(survivors))
            else:
                self.assertIsNone(row["producer_survivor_actions"])
            self.assertEqual(row["kyoku_ordinal"], 0)
            self.assertEqual(row["actor_seat"], 0)

    def test_capture_must_be_bound_to_the_same_decision(self):
        execution = _execution(self.contexts, seed=TEST_SEEDS[1], non_focal_per_step=0)
        swapped = source.FocalGameExecution(
            seed=execution.seed,
            focal_seat=execution.focal_seat,
            inspection=execution.inspection,
            captures=(execution.captures[1], execution.captures[0])
            + execution.captures[2:],
        )
        with self.assertRaisesRegex(FocalOutcomeSourceError, "not bound"):
            source.focal_decision_rows(swapped, (KYOKU,), game_ordinal=0)
        missing = source.FocalGameExecution(
            seed=execution.seed,
            focal_seat=execution.focal_seat,
            inspection=execution.inspection,
            captures=execution.captures[:-1],
        )
        with self.assertRaises(FocalOutcomeSourceError):
            source.focal_decision_rows(missing, (KYOKU,), game_ordinal=0)

    def test_policy_input_must_match_its_kyoku(self):
        execution = _execution(self.contexts, seed=TEST_SEEDS[1], non_focal_per_step=0)
        other = KyokuAccount(**{**_fields(KYOKU), "honba": 1})
        with self.assertRaisesRegex(FocalOutcomeSourceError, "kyoku"):
            source.focal_decision_rows(execution, (other,), game_ordinal=0)


def _fields(account):
    return {name: getattr(account, name) for name in account.__dataclass_fields__}


class SourceContractTest(unittest.TestCase):
    """``build_source_contract()``: 実git checkout + 差し替えた環境検証。"""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.checkout = Path(directory.name)
        (self.checkout / "src").mkdir()
        (self.checkout / "src" / "module.py").write_text("VALUE = 1\n")
        (self.checkout / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        for arguments in (
            ("init", "-q"),
            ("add", "-A"),
            (
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-q",
                "-m",
                "fixture",
            ),
        ):
            self._git(*arguments)
        self.check = EnvironmentCheck(
            identities=(
                InstalledVcsIdentity(
                    name="lisjong",
                    version="0.1.0",
                    repository_url="https://github.com/lisbun/lisjong.git",
                    revision=source.PINNED_LISJONG_REVISION,
                ),
            ),
            errors=(),
            pip_check_output="",
        )

    def _git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.checkout), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_clean_checkout_builds_a_valid_contract(self):
        with patch.object(source, "verify_environment", return_value=self.check):
            contract = source.build_source_contract(self.checkout / "pyproject.toml")
        self.assertEqual(contract["arena_revision"], self._git("rev-parse", "HEAD"))
        self.assertEqual(
            contract["dependencies"], {"lisjong": source.PINNED_LISJONG_REVISION}
        )
        self.assertEqual(
            contract["backend"],
            {"name": "riichienv", "version": metadata.version("riichienv")},
        )
        self.assertEqual(contract["game_mode"], source.GAME_MODE)
        self.assertEqual(source.validate_source_contract(contract), contract)

    def test_dirty_source_checkout_fails_closed(self):
        (self.checkout / "src" / "module.py").write_text("VALUE = 2\n")
        with (
            patch.object(source, "verify_environment", return_value=self.check),
            self.assertRaisesRegex(FocalOutcomeSourceError, "must be committed"),
        ):
            source.build_source_contract(self.checkout / "pyproject.toml")


class ConfigurationTest(unittest.TestCase):
    def test_exactly_one_exploring_seat_and_constant_baseline_elsewhere(self):
        previous_runtimes = []
        for focal in Seat:
            policies, adapter = source.build_game_policies(
                seed=TEST_SEEDS[2], focal_seat=focal
            )
            self.assertEqual(set(policies), set(Seat))
            exploring = [
                seat
                for seat, policy in policies.items()
                if isinstance(policy, FocalExplorationPolicy)
            ]
            self.assertEqual(exploring, [focal])
            self.assertIs(policies[focal], adapter)
            others = [policies[seat] for seat in Seat if seat != focal]
            for policy in others:
                self.assertIsInstance(policy, SemanticEnvelopeOffensePolicy)
                self.assertEqual(
                    policy.runtime.identity, CONSTANT_RESIDUAL_RUNTIME_IDENTITY
                )
            self.assertEqual(len({id(policy) for policy in others}), 3)
            # seatごとにfreshなruntime instance（seat間でもgame間でも共有しない）。
            runtimes = [policy.runtime for policy in others]
            for index, runtime in enumerate(runtimes):
                self.assertFalse(
                    any(runtime is other for other in runtimes[index + 1 :])
                )
                self.assertFalse(any(runtime is other for other in previous_runtimes))
            previous_runtimes.extend(runtimes)

    def test_rotation_and_global_ordinal_across_train_select(self):
        games = [
            (TEST_SEEDS[0], "TRAIN"),
            (TEST_SEEDS[1], "TRAIN"),
            (TEST_SEEDS[2], "TRAIN"),
            (TEST_SEEDS[3], "SELECT"),
            (TEST_SEEDS[4], "SELECT"),
        ]
        bindings = {
            "TRAIN": binding(TEST_SEEDS[:3]),
            "SELECT": binding(TEST_SEEDS[3:5]),
        }
        executed, written, documents = [], [], []

        def run_game(*, seed, focal_seat, max_steps):
            executed.append((seed, focal_seat))
            return (seed, focal_seat)

        def write(path, execution, *, game_ordinal, seed, split):
            path.mkdir()
            written.append((path.name, game_ordinal, seed, split, execution))
            return {"game_ordinal": game_ordinal}

        with (
            patch.object(source, "run_focal_game", side_effect=run_game),
            patch.object(source, "write_game", side_effect=write),
            patch.object(
                source,
                "write_document",
                side_effect=lambda path, value: documents.append(value),
            ),
            patch.object(source, "verify_focal_outcome_source", return_value="ok"),
            _tempdir() as root,
        ):
            result = source.generate_focal_outcome_source(
                root / "out",
                population_role="SCIENTIFIC",
                games=games,
                allocation_bindings=bindings,
                source_contract=source_contract(),
            )
            self.assertEqual(result, "ok")
            self.assertTrue((root / "out").is_dir())
        self.assertEqual(
            executed, [(seed, Seat(index % 4)) for index, (seed, _) in enumerate(games)]
        )
        self.assertEqual(
            [(name, ordinal, split) for name, ordinal, _, split, _ in written],
            [
                ("game-000", 0, "TRAIN"),
                ("game-001", 1, "TRAIN"),
                ("game-002", 2, "TRAIN"),
                ("game-003", 3, "SELECT"),
                ("game-004", 4, "SELECT"),
            ],
        )
        (manifest,) = documents
        self.assertEqual(manifest["population_role"], "SCIENTIFIC")
        self.assertEqual(manifest["behavior"], source.BEHAVIOR)
        self.assertEqual(
            [game["game_ordinal"] for game in manifest["games"]], [0, 1, 2, 3, 4]
        )

    def test_failed_game_publishes_nothing(self):
        with (
            patch.object(source, "run_focal_game", side_effect=RuntimeError("boom")),
            _tempdir() as root,
        ):
            with self.assertRaises(RuntimeError):
                source.generate_focal_outcome_source(
                    root / "out",
                    population_role="CALIBRATION",
                    games=[(TEST_SEEDS[0], "CALIBRATION")],
                    allocation_bindings={"CALIBRATION": binding(TEST_SEEDS[:1])},
                    source_contract=source_contract(),
                )
            self.assertEqual(list(root.iterdir()), [])

    def test_population_and_provenance_are_validated_before_execution(self):
        good = {
            "population_role": "CALIBRATION",
            "games": [(TEST_SEEDS[0], "CALIBRATION")],
            "allocation_bindings": {"CALIBRATION": binding(TEST_SEEDS[:1])},
        }
        wrong_domain = binding(TEST_SEEDS[:1]) | {
            "seed_domain": "riichienv-4p-red-single-v1"
        }
        cases = {
            "role": {"population_role": "PILOT"},
            "split": {"games": [(TEST_SEEDS[0], "TRAIN")]},
            "empty": {"games": [], "allocation_bindings": {}},
            "duplicate seed": {
                "games": [(TEST_SEEDS[0], "CALIBRATION")] * 2,
            },
            "binding split": {"allocation_bindings": {}},
            "binding membership": {
                "allocation_bindings": {"CALIBRATION": binding(TEST_SEEDS[1:2])}
            },
            "binding domain": {"allocation_bindings": {"CALIBRATION": wrong_domain}},
        }
        for name, change in cases.items():
            arguments = good | change
            with self.subTest(name), self.assertRaises(FocalOutcomeSourceError):
                source.validate_population(
                    arguments["population_role"],
                    arguments["games"],
                    arguments["allocation_bindings"],
                )
        for field, value in (
            ("dependencies", {"lisjong": "f" * 40}),
            ("game_mode", "4p-red-single"),
            ("backend", {"name": "other", "version": "1"}),
        ):
            with self.subTest(field), self.assertRaises(FocalOutcomeSourceError):
                source.validate_source_contract(source_contract() | {field: value})
        with patch.object(source, "run_focal_game") as run_game, _tempdir() as root:
            with self.assertRaises(FocalOutcomeSourceError):
                source.generate_focal_outcome_source(
                    root / "out",
                    **(good | {"population_role": "PILOT"}),
                    source_contract=source_contract(),
                )
            run_game.assert_not_called()


def _tempdir():
    return _TemporaryPath()


class _TemporaryPath(tempfile.TemporaryDirectory):
    def __enter__(self):
        return Path(super().__enter__())


if __name__ == "__main__":
    unittest.main()
