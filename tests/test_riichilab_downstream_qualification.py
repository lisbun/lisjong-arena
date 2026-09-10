"""Issue #203 offline downstream reconstruction qualificationのfocused tests。

すべてsynthetic fixtureだけを使う。Issue #170の実corpus bytesは読まない。
"""

from __future__ import annotations

import ast
import contextlib
import copy
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _riichilab_downstream_qualification_fixtures import (
    DEFAULT_HANDS,
    PLAIN_HAND_C,
    PLAIN_HAND_D,
    all_action_families_game,
    dahai,
    end_game,
    end_kyoku,
    gzip_jsonl,
    hora,
    ryukyoku,
    simple_game,
    start_game,
    start_kyoku,
    tsumo,
)
from lisjong.policy_contract.seat import Seat
from lisjong.policy_contract.tile import Tile, TileCategory, TileType, tile_sort_key

from lisjong_arena.phase2_training_anchor.training_labels import (
    OpponentIdentity,
    StructuralWaitUnavailableReason,
    structural_wait_for_hand,
)
from lisjong_arena.riichilab_corpus import __main__ as corpus_main
from lisjong_arena.riichilab_corpus.models import (
    API_BASE_URL,
    TARGET_BOTS,
    Participation,
    build_snapshot,
)
from lisjong_arena.riichilab_corpus.persistence import (
    build_manifest,
    group_participations,
    load_cache_index,
    persist_download,
    save_manifest,
    write_new_json,
)
from lisjong_arena.riichilab_downstream_qualification import qualification
from lisjong_arena.riichilab_downstream_qualification.behavior import ActionFamily
from lisjong_arena.riichilab_downstream_qualification.classification import (
    OverallOutcome,
    SurfaceClassification,
    classify_behavior_surface,
    classify_hidden_state_surface,
    combine_overall_outcome,
)
from lisjong_arena.riichilab_downstream_qualification.hidden_truth import (
    concealed_size_is_consistent,
)
from lisjong_arena.riichilab_downstream_qualification.mjai_events import (
    UnsupportedReason,
    VisibleEventKind,
    project_visible_event,
)
from lisjong_arena.riichilab_downstream_qualification.player_safe import (
    DecisionKind,
    PlayerSafeRoundState,
)
from lisjong_arena.riichilab_downstream_qualification.qualification import (
    qualify_local_corpus,
)
from lisjong_arena.riichilab_downstream_qualification.replay import replay_game
from lisjong_arena.riichilab_downstream_qualification.report import (
    REPORT_SCHEMA_ID,
    report_filename,
)

_PLAYED_AT = "2026-09-08T12:34:56Z"
_RETRIEVED_AT = "2026-09-09T00:00:00Z"
_ALL_SEATS = {Seat(index): 100 + index for index in range(4)}


def _replay(events, targets=None):
    return replay_game(
        events,
        game_id="synthetic",
        target_seats=_ALL_SEATS if targets is None else targets,
    )


def _first_decision(result, seat: Seat):
    return next(
        decision for decision in result.decisions if decision.viewer_seat == seat
    )


class PlayerSafeProjectionTests(unittest.TestCase):
    """player-safe projectionとleakage boundaryのtests。"""

    def test_opponent_initial_hands_are_not_projected(self) -> None:
        event = start_kyoku(hands=DEFAULT_HANDS)
        visible = project_visible_event(event, Seat(1))
        self.assertIs(visible.kind, VisibleEventKind.ROUND_START)
        self.assertEqual(
            list(visible.start.viewer_concealed_tiles),
            sorted(visible.start.viewer_concealed_tiles, key=tile_sort_key),
        )
        self.assertEqual(len(visible.start.viewer_concealed_tiles), 13)
        # seat1の手牌はpinzuと字牌だけであり、seat0のmanzu配牌は入らない。
        self.assertNotIn(
            Tile(TileType(TileCategory.MANZU, 1)),
            visible.start.viewer_concealed_tiles,
        )

    def test_opponent_draw_tile_is_not_projected(self) -> None:
        visible = project_visible_event(tsumo(0, "5mr"), Seat(2))
        self.assertIs(visible.kind, VisibleEventKind.OPPONENT_DRAW)
        self.assertIsNone(visible.tile)

    def test_terminal_payload_is_not_projected(self) -> None:
        for event in (hora(0, 1, "1m", ura=["9s"]), ryukyoku(tenpai_hands=[["1m"]])):
            with self.subTest(event=event["type"]):
                visible = project_visible_event(event, Seat(3))
                self.assertIs(visible.kind, VisibleEventKind.ROUND_TERMINAL)
                self.assertIsNone(visible.tile)
                self.assertEqual(visible.tiles, ())

    def test_same_prefix_reconstructs_the_same_snapshot(self) -> None:
        events = all_action_families_game()
        first = _replay(events)
        second = _replay(copy.deepcopy(events))
        self.assertEqual(
            [decision.snapshot for decision in first.decisions],
            [decision.snapshot for decision in second.decisions],
        )

    def test_future_events_do_not_change_earlier_snapshots(self) -> None:
        prefix = [
            start_game(),
            start_kyoku(hands=DEFAULT_HANDS),
            tsumo(0, "5mr"),
            dahai(0, "5mr", tsumogiri=True),
        ]
        short = _replay([*prefix, ryukyoku(), end_kyoku(), end_game()])
        long = _replay(
            [
                *prefix,
                tsumo(1, "1p"),
                dahai(1, "1p", tsumogiri=True),
                tsumo(2, "1s"),
                dahai(2, "1s", tsumogiri=True),
                ryukyoku(),
                end_kyoku(),
                end_game(),
            ]
        )
        self.assertEqual(short.decisions[0].snapshot, long.decisions[0].snapshot)

    def test_opponent_hidden_hand_change_leaves_viewer_snapshot_identical(self) -> None:
        def game(hands):
            return [
                start_game(),
                start_kyoku(hands=hands),
                tsumo(0, "5mr"),
                dahai(0, "5mr", tsumogiri=True),
                ryukyoku(),
                end_kyoku(),
                end_game(),
            ]

        viewer = {Seat(0): 126}
        # seat1 / seat2 / seat3のconcealed truthだけを入れ替える。seat0が
        # 観測できる情報は同一であり、player-safe snapshotも同一でなければ
        # ならない。
        rotated = [
            DEFAULT_HANDS[0],
            DEFAULT_HANDS[2],
            DEFAULT_HANDS[3],
            DEFAULT_HANDS[1],
        ]
        baseline = _first_decision(_replay(game(DEFAULT_HANDS), viewer), Seat(0))
        variant = _first_decision(_replay(game(rotated), viewer), Seat(0))
        self.assertEqual(baseline.snapshot, variant.snapshot)
        self.assertNotEqual(
            [row.expected_counts.counts for row in baseline.hidden.rows],
            [row.expected_counts.counts for row in variant.hidden.rows],
        )

    def test_own_visible_hand_change_is_reflected(self) -> None:
        viewer = {Seat(0): 126}
        baseline = _first_decision(
            _replay(simple_game(hands=DEFAULT_HANDS), viewer), Seat(0)
        )
        changed_hands = [
            ["2m", "2m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m", "W", "W", "N"],
            DEFAULT_HANDS[1],
            DEFAULT_HANDS[2],
            DEFAULT_HANDS[3],
        ]
        changed = _first_decision(
            _replay(simple_game(hands=changed_hands), viewer), Seat(0)
        )
        self.assertNotEqual(
            baseline.snapshot.own_concealed_tiles, changed.snapshot.own_concealed_tiles
        )

    def test_snapshot_fields_declare_the_information_boundary(self) -> None:
        names = {
            field.name
            for field in dataclasses.fields(
                _first_decision(
                    _replay(simple_game(), {Seat(0): 126}), Seat(0)
                ).snapshot
            )
        }
        self.assertEqual(
            names,
            {
                "viewer_seat",
                "decision_kind",
                "prevailing_wind",
                "hand_number",
                "honba",
                "dealer_seat",
                "seat_scores",
                "own_concealed_tiles",
                "own_drawn_tile",
                "public",
                "visible_event_index",
            },
        )

    def test_replay_reports_no_leakage_or_inconsistency(self) -> None:
        result = _replay(all_action_families_game())
        self.assertTrue(result.replayable)
        self.assertEqual(result.leakage_check_failures, 0)
        self.assertEqual(result.replay_consistency_failures, 0)


class BehaviorSupervisionTests(unittest.TestCase):
    """Surface A（observed action mapping）のtests。"""

    def test_all_required_action_families_are_measured(self) -> None:
        result = _replay(all_action_families_game())
        families = {decision.mapped.family for decision in result.decisions}
        self.assertEqual(
            families,
            {
                ActionFamily.DISCARD_TEDASHI,
                ActionFamily.DISCARD_TSUMOGIRI,
                ActionFamily.REACH,
                ActionFamily.CHI,
                ActionFamily.PON,
                ActionFamily.DAIMINKAN,
                ActionFamily.ANKAN,
                ActionFamily.KAKAN,
                ActionFamily.PASS,
                ActionFamily.RON,
                ActionFamily.TSUMO,
            },
        )

    def test_red_five_identity_is_preserved(self) -> None:
        result = _replay(simple_game(), {Seat(0): 126})
        decision = _first_decision(result, Seat(0))
        red_five = Tile(TileType(TileCategory.MANZU, 5), is_red=True)
        self.assertIn(red_five, decision.snapshot.own_concealed_tiles)
        self.assertEqual(decision.snapshot.own_drawn_tile, red_five)
        discard = result.decisions[0].mapped.action
        self.assertEqual(discard.tile, red_five)
        self.assertTrue(discard.tile.is_red)

    def test_call_and_kan_tile_accounting(self) -> None:
        result = _replay(all_action_families_game())
        pon_decision = next(
            decision
            for decision in result.decisions
            if decision.mapped.family is ActionFamily.PON
        )
        action = pon_decision.mapped.action
        self.assertEqual(action.target, Seat(0))
        self.assertEqual(len(action.consumed_tiles), 2)
        daiminkan = next(
            decision.mapped.action
            for decision in result.decisions
            if decision.mapped.family is ActionFamily.DAIMINKAN
        )
        self.assertEqual(len(daiminkan.consumed_tiles), 3)
        ankan = next(
            decision.mapped.action
            for decision in result.decisions
            if decision.mapped.family is ActionFamily.ANKAN
        )
        self.assertEqual(len(ankan.tiles), 4)
        # daiminkan後、seat3のconcealed sizeはopen-meld調整後も整合している。
        self.assertTrue(
            all(
                decision.hidden.concealed_size_consistent
                for decision in result.decisions
            )
        )

    def test_decision_kinds_are_bound_to_the_observed_context(self) -> None:
        result = _replay(all_action_families_game())
        reach_index = next(
            index
            for index, decision in enumerate(result.decisions)
            if decision.mapped.family is ActionFamily.REACH
        )
        self.assertIs(
            result.decisions[reach_index].snapshot.decision_kind, DecisionKind.TURN
        )
        self.assertIs(
            result.decisions[reach_index + 1].snapshot.decision_kind,
            DecisionKind.RIICHI_DISCARD,
        )
        chi_index = next(
            index
            for index, decision in enumerate(result.decisions)
            if decision.mapped.family is ActionFamily.CHI
        )
        self.assertIs(
            result.decisions[chi_index].snapshot.decision_kind,
            DecisionKind.CALL_RESPONSE,
        )
        self.assertIs(
            result.decisions[chi_index + 1].snapshot.decision_kind,
            DecisionKind.POST_CALL_DISCARD,
        )

    def test_kakan_resolves_a_unique_source_pon(self) -> None:
        result = _replay(all_action_families_game())
        kakan_action = next(
            decision.mapped.action
            for decision in result.decisions
            if decision.mapped.family is ActionFamily.KAKAN
        )
        self.assertEqual(kakan_action.from_seat, Seat(0))
        self.assertEqual(kakan_action.called_tile, kakan_action.added_tile)

    def test_kakan_without_a_matching_pon_fails_closed(self) -> None:
        events = all_action_families_game()
        index = next(
            position
            for position, event in enumerate(events)
            if event["type"] == "kakan"
        )
        events[index] = {
            "type": "kakan",
            "actor": 1,
            "pai": "2p",
            "consumed": ["2p", "2p", "9p"],
        }
        result = _replay(events)
        self.assertFalse(result.replayable)
        self.assertIs(
            result.unsupported_reason, UnsupportedReason.AMBIGUOUS_KAKAN_SOURCE_PON
        )
        self.assertEqual(result.decisions, ())

    def test_ron_and_tsumo_are_distinguished_by_trigger_context(self) -> None:
        result = _replay(all_action_families_game())
        ron = next(
            decision.mapped.action
            for decision in result.decisions
            if decision.mapped.family is ActionFamily.RON
        )
        tsumo_win = next(
            decision.mapped.action
            for decision in result.decisions
            if decision.mapped.family is ActionFamily.TSUMO
        )
        self.assertEqual(ron.actor, Seat(2))
        self.assertEqual(ron.target, Seat(3))
        self.assertEqual(tsumo_win.actor, Seat(2))

    def test_unsupported_decision_is_counted_not_dropped(self) -> None:
        events = [
            start_game(),
            start_kyoku(hands=DEFAULT_HANDS),
            tsumo(0, "5mr"),
            dahai(0, "5mr", tsumogiri=True),
            # 直前のdiscardと一致しない和了牌: trigger contextが解決できない。
            hora(1, 0, "9p"),
            end_kyoku(),
            end_game(),
        ]
        result = _replay(events)
        self.assertTrue(result.replayable)
        unsupported = [
            decision
            for decision in result.decisions
            if decision.mapped.family is ActionFamily.UNSUPPORTED
        ]
        self.assertEqual(len(unsupported), 1)
        self.assertIs(
            unsupported[0].mapped.unsupported_reason,
            UnsupportedReason.RON_TRIGGER_CONTEXT_UNRESOLVED,
        )

    def test_malformed_discard_fails_closed(self) -> None:
        events = simple_game()
        events[3] = {"type": "dahai", "actor": 0, "pai": "5mr"}
        result = _replay(events)
        self.assertFalse(result.replayable)
        self.assertIs(
            result.unsupported_reason, UnsupportedReason.MISSING_REQUIRED_FIELD
        )
        self.assertEqual(result.decisions, ())

    def test_unrecognized_tile_notation_fails_closed(self) -> None:
        result = _replay(simple_game(body=[tsumo(0, "0m"), dahai(0, "0m")]))
        self.assertFalse(result.replayable)
        self.assertIs(
            result.unsupported_reason, UnsupportedReason.UNRECOGNIZED_TILE_NOTATION
        )

    def test_masked_draw_fails_closed_for_every_seat(self) -> None:
        for actor in (0, 2):
            with self.subTest(actor=actor):
                result = _replay(
                    simple_game(
                        body=[
                            {"type": "tsumo", "actor": actor, "pai": "?"},
                            dahai(actor, "1m" if actor == 0 else "1s"),
                        ]
                    )
                )
                self.assertFalse(result.replayable)
                self.assertIs(
                    result.unsupported_reason,
                    UnsupportedReason.MASKED_OR_MISSING_DRAW,
                )

    def test_call_referencing_a_different_discard_fails_closed(self) -> None:
        events = all_action_families_game()
        index = next(
            position for position, event in enumerate(events) if event["type"] == "chi"
        )
        events[index] = {
            "type": "chi",
            "actor": 1,
            "target": 2,
            "pai": "4p",
            "consumed": ["3p", "5p"],
        }
        result = _replay(events)
        self.assertFalse(result.replayable)
        self.assertIs(
            result.unsupported_reason, UnsupportedReason.AMBIGUOUS_CALL_TARGET
        )

    def test_participation_join_binds_decisions_to_the_target_seat(self) -> None:
        result = _replay(all_action_families_game(), {Seat(2): 294})
        self.assertTrue(result.decisions)
        for decision in result.decisions:
            self.assertEqual(decision.viewer_seat, Seat(2))
            self.assertEqual(decision.bot_id, 294)
            self.assertEqual(decision.snapshot.viewer_seat, Seat(2))
            self.assertEqual(decision.hidden.viewer_seat, Seat(2))

    def test_shared_game_is_replayed_once_for_several_participations(self) -> None:
        shared = _replay(all_action_families_game(), {Seat(0): 126, Seat(2): 120})
        self.assertTrue(shared.replayable)
        seats = {decision.viewer_seat for decision in shared.decisions}
        self.assertEqual(seats, {Seat(0), Seat(2)})
        only_seat_0 = _replay(all_action_families_game(), {Seat(0): 126})
        only_seat_2 = _replay(all_action_families_game(), {Seat(2): 120})
        self.assertEqual(
            len(shared.decisions),
            len(only_seat_0.decisions) + len(only_seat_2.decisions),
        )
        self.assertEqual(shared.rounds, only_seat_0.rounds)


class HiddenStateSupervisionTests(unittest.TestCase):
    """Surface B（server-truth hidden-state supervision）のtests。"""

    def test_hidden_truth_is_joined_to_the_same_decision_identity(self) -> None:
        result = _replay(simple_game(), {Seat(0): 126})
        decision = _first_decision(result, Seat(0))
        self.assertEqual(decision.hidden.viewer_seat, decision.snapshot.viewer_seat)
        self.assertEqual(len(decision.hidden.rows), 3)
        self.assertEqual(
            [row.identity.seat for row in decision.hidden.rows],
            [Seat(1), Seat(2), Seat(3)],
        )
        opponent = decision.hidden.rows[0]
        self.assertEqual(opponent.expected_counts.concealed_size, 13)
        self.assertEqual(sum(opponent.expected_counts.counts), 13)

    def test_opponent_concealed_truth_is_not_in_the_player_safe_snapshot(self) -> None:
        result = _replay(simple_game(), {Seat(0): 126})
        decision = _first_decision(result, Seat(0))
        # seat1のconcealed truthは1pだけで構成される。player-safe snapshotの
        # 自手にもdiscard履歴にも現れない。
        one_pin = Tile(TileType(TileCategory.PINZU, 1))
        self.assertGreater(decision.hidden.rows[0].expected_counts.counts[9], 0)
        self.assertNotIn(one_pin, decision.snapshot.own_concealed_tiles)
        self.assertEqual(
            [
                discard
                for discards in decision.snapshot.public.discards
                for discard in discards
            ],
            [],
        )

    def test_red_five_truth_is_retained_in_the_hidden_channel(self) -> None:
        hands = [
            DEFAULT_HANDS[0],
            ["5pr", "2p", "3p", "4p", "5p", "6p", "7p", "8p", "9p", "E", "E", "S", "S"],
            DEFAULT_HANDS[2],
            DEFAULT_HANDS[3],
        ]
        result = _replay(simple_game(hands=hands), {Seat(0): 126})
        row = _first_decision(result, Seat(0)).hidden.rows[0]
        self.assertEqual(row.expected_counts.red_five_present, (False, True, False))
        self.assertTrue(row.holds_red_five)

    def test_tile_conservation_violation_is_detected(self) -> None:
        hands = [
            [
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
                "9m",
                "1p",
                "2p",
            ],
            ["1m", "1m", "1m", "3p", "4p", "5p", "6p", "7p", "8p", "9p", "E", "S", "W"],
            list(PLAIN_HAND_C),
            list(PLAIN_HAND_D),
        ]
        result = _replay(
            simple_game(
                hands=hands, body=[tsumo(0, "9p"), dahai(0, "9p", tsumogiri=True)]
            ),
            {Seat(0): 126},
        )
        self.assertTrue(result.replayable)
        decision = _first_decision(result, Seat(0))
        self.assertFalse(decision.hidden.tile_conservation_consistent)

    def test_concealed_size_consistency_detects_an_extra_tile(self) -> None:
        states = tuple(PlayerSafeRoundState(Seat(index)) for index in range(4))
        start = start_kyoku(hands=DEFAULT_HANDS)
        for state in states:
            state.apply(project_visible_event(start, state.viewer_seat))
        self.assertTrue(concealed_size_is_consistent(states))
        draw = tsumo(0, "5mr")
        for state in states:
            state.apply(project_visible_event(draw, state.viewer_seat))
        self.assertTrue(concealed_size_is_consistent(states))
        extra = tsumo(0, "5m")
        for state in states:
            state.apply(project_visible_event(extra, state.viewer_seat))
        self.assertFalse(concealed_size_is_consistent(states))

    def test_structural_wait_is_available_for_stable_hands(self) -> None:
        result = _replay(all_action_families_game())
        rows = [row for decision in result.decisions for row in decision.hidden.rows]
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row.structural_wait.is_available)
            self.assertIsNotNone(row.structural_tenpai)

    def test_structural_wait_stays_unsupported_for_unstable_hands(self) -> None:
        identity = OpponentIdentity(
            seat=Seat(1),
            wind=next(
                row.identity.wind
                for row in _first_decision(
                    _replay(simple_game(), {Seat(0): 126}), Seat(0)
                ).hidden.rows
                if row.identity.seat == Seat(1)
            ),
            viewer_relative_offset=1,
        )
        unstable = tuple(Tile(TileType(TileCategory.PINZU, 1)) for _ in range(14))
        result = structural_wait_for_hand(identity, unstable, ())
        self.assertFalse(result.is_available)
        self.assertIs(
            result.unavailable_reason,
            StructuralWaitUnavailableReason.UNSTABLE_HAND_SIZE,
        )


class ClassificationRuleTests(unittest.TestCase):
    """decision ruleがexactly one outcomeを返すことのtests。"""

    def test_behavior_rule(self) -> None:
        clean = {
            "player_safe_decision_points": 10,
            "exactly_mapped_actions": 10,
            "unsupported_actions": 0,
            "games_unsupported": 0,
            "leakage_check_failures": 0,
            "replay_consistency_failures": 0,
        }
        self.assertIs(
            classify_behavior_surface(**clean), SurfaceClassification.QUALIFIED
        )
        self.assertIs(
            classify_behavior_surface(
                **{**clean, "exactly_mapped_actions": 9, "unsupported_actions": 1}
            ),
            SurfaceClassification.PARTIAL,
        )
        self.assertIs(
            classify_behavior_surface(**{**clean, "games_unsupported": 1}),
            SurfaceClassification.PARTIAL,
        )
        self.assertIs(
            classify_behavior_surface(**{**clean, "leakage_check_failures": 1}),
            SurfaceClassification.NOT_QUALIFIED,
        )
        self.assertIs(
            classify_behavior_surface(
                **{**clean, "exactly_mapped_actions": 0, "unsupported_actions": 10}
            ),
            SurfaceClassification.NOT_QUALIFIED,
        )

    def test_hidden_state_rule(self) -> None:
        clean = {
            "player_safe_decision_points": 10,
            "decision_points_with_exact_opponent_truth": 10,
            "concealed_size_consistency_failures": 0,
            "tile_conservation_failures": 0,
            "games_unsupported": 0,
            "leakage_check_failures": 0,
            "replay_consistency_failures": 0,
        }
        self.assertIs(
            classify_hidden_state_surface(**clean), SurfaceClassification.QUALIFIED
        )
        self.assertIs(
            classify_hidden_state_surface(**{**clean, "tile_conservation_failures": 1}),
            SurfaceClassification.PARTIAL,
        )
        self.assertIs(
            classify_hidden_state_surface(
                **{**clean, "concealed_size_consistency_failures": 2}
            ),
            SurfaceClassification.PARTIAL,
        )
        self.assertIs(
            classify_hidden_state_surface(
                **{**clean, "decision_points_with_exact_opponent_truth": 0}
            ),
            SurfaceClassification.NOT_QUALIFIED,
        )
        self.assertIs(
            classify_hidden_state_surface(
                **{**clean, "replay_consistency_failures": 1}
            ),
            SurfaceClassification.NOT_QUALIFIED,
        )

    def test_every_combination_yields_exactly_one_outcome(self) -> None:
        expected = {
            (
                SurfaceClassification.QUALIFIED,
                SurfaceClassification.QUALIFIED,
            ): OverallOutcome.BOTH_SURFACES_TECHNICALLY_QUALIFIED,
            (
                SurfaceClassification.QUALIFIED,
                SurfaceClassification.NOT_QUALIFIED,
            ): OverallOutcome.BEHAVIOR_SUPERVISION_ONLY_QUALIFIED,
            (
                SurfaceClassification.NOT_QUALIFIED,
                SurfaceClassification.QUALIFIED,
            ): OverallOutcome.HIDDEN_STATE_SUPERVISION_ONLY_QUALIFIED,
            (
                SurfaceClassification.NOT_QUALIFIED,
                SurfaceClassification.NOT_QUALIFIED,
            ): OverallOutcome.NO_DOWNSTREAM_SURFACE_QUALIFIED,
        }
        seen = 0
        for behavior in SurfaceClassification:
            for hidden in SurfaceClassification:
                outcome = combine_overall_outcome(behavior, hidden)
                self.assertIsInstance(outcome, OverallOutcome)
                self.assertIsNot(outcome, OverallOutcome.STOP_INVALID)
                self.assertIs(
                    outcome,
                    expected.get(
                        (behavior, hidden),
                        OverallOutcome.DOWNSTREAM_RECONSTRUCTION_PARTIAL,
                    ),
                )
                seen += 1
        self.assertEqual(seen, 9)


def _snapshot_for(games: dict[str, list[tuple[int, int]]]):
    participations = tuple(
        Participation(game_id, bot_id, seat, _PLAYED_AT)
        for game_id, seats in games.items()
        for bot_id, seat in seats
    )
    return build_snapshot(
        retrieved_at=_RETRIEVED_AT,
        source_apis=tuple(f"{API_BASE_URL}/bots/{item[0]}" for item in TARGET_BOTS),
        target_bots=TARGET_BOTS,
        participations=participations,
    )


def _build_local_cache(directory: Path, payloads: dict[str, bytes], snapshot):
    grouped = group_participations(snapshot)
    for game_id, payload in payloads.items():
        persist_download(
            directory,
            participations=grouped[game_id],
            payload=payload,
            http_status=200,
            content_length=len(payload),
            retrieved_at=_RETRIEVED_AT,
        )
    manifest = build_manifest(snapshot, load_cache_index(directory))
    save_manifest(directory, manifest)
    return manifest


class CorpusQualificationTests(unittest.TestCase):
    """local corpusのidentity gateとreport artifactのtests。"""

    def test_identity_mismatch_stops_before_processing(self) -> None:
        snapshot = _snapshot_for({"game-a": [(126, 0)]})
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _build_local_cache(
                directory,
                {"game-a": gzip_jsonl(all_action_families_game())},
                snapshot,
            )
            report = qualify_local_corpus(snapshot, directory)
        self.assertIs(report.overall_outcome, OverallOutcome.STOP_INVALID)
        self.assertIsNotNone(report.stop_reason)
        self.assertIsNone(report.behavior)
        self.assertIsNone(report.hidden_state)
        self.assertEqual(
            report.expected_corpus_identity, qualification.EXPECTED_CORPUS_IDENTITY
        )

    def test_incomplete_cache_stops(self) -> None:
        snapshot = _snapshot_for({"game-a": [(126, 0)]})
        with tempfile.TemporaryDirectory() as raw:
            report = qualify_local_corpus(snapshot, Path(raw))
        self.assertIs(report.overall_outcome, OverallOutcome.STOP_INVALID)
        self.assertIn("cache", report.stop_reason)

    def test_matching_identity_produces_a_classified_report(self) -> None:
        snapshot = _snapshot_for({"game-a": [(126, 0), (120, 2)], "game-b": [(294, 1)]})
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            manifest = _build_local_cache(
                directory,
                {
                    "game-a": gzip_jsonl(all_action_families_game()),
                    "game-b": gzip_jsonl(simple_game()),
                },
                snapshot,
            )
            with (
                mock.patch.object(
                    qualification,
                    "EXPECTED_CORPUS_IDENTITY",
                    manifest["corpus_identity"],
                ),
                mock.patch.object(
                    qualification,
                    "EXPECTED_MANIFEST_SHA256",
                    manifest["manifest_sha256"],
                ),
            ):
                report = qualify_local_corpus(snapshot, directory)

        self.assertIs(
            report.overall_outcome, OverallOutcome.BOTH_SURFACES_TECHNICALLY_QUALIFIED
        )
        self.assertIs(report.behavior_classification, SurfaceClassification.QUALIFIED)
        self.assertIs(
            report.hidden_state_classification, SurfaceClassification.QUALIFIED
        )
        behavior = report.behavior
        self.assertEqual(behavior.games_processed, 2)
        self.assertEqual(behavior.games_replayable, 2)
        self.assertEqual(behavior.games_unsupported, 0)
        self.assertEqual(behavior.rounds_processed, 3)
        self.assertEqual(behavior.shared_games, 1)
        self.assertEqual(behavior.shared_game_participations, 2)
        self.assertEqual(behavior.unsupported_actions, 0)
        self.assertEqual(behavior.leakage_check_failures, 0)
        self.assertEqual(behavior.replay_consistency_failures, 0)
        self.assertEqual(behavior.legal_action_sets_exactly_reconstructed, 0)
        self.assertEqual(
            set(behavior.legal_action_set_unsupported_reasons),
            {qualification.LEGAL_ACTION_SET_UNSUPPORTED_REASON},
        )
        self.assertEqual(
            set(behavior.decision_points_per_target_bot), {"120", "126", "294"}
        )
        self.assertEqual(
            behavior.open_hand_decision_points + behavior.closed_hand_decision_points,
            behavior.player_safe_decision_points,
        )
        self.assertEqual(
            behavior.riichi_decision_points + behavior.non_riichi_decision_points,
            behavior.player_safe_decision_points,
        )
        hidden = report.hidden_state
        self.assertEqual(
            hidden.decision_points_with_exact_opponent_truth,
            behavior.player_safe_decision_points,
        )
        self.assertEqual(hidden.opponent_rows, 3 * hidden.decision_points)
        self.assertEqual(hidden.tile_conservation_failures, 0)
        self.assertEqual(hidden.concealed_size_consistency_failures, 0)
        self.assertTrue(report.coverage_limitations)

    def test_unsupported_game_downgrades_both_surfaces_to_partial(self) -> None:
        snapshot = _snapshot_for({"game-a": [(126, 0)], "game-b": [(294, 1)]})
        broken = all_action_families_game()
        broken[3] = {"type": "dahai", "actor": 0, "pai": "2p"}
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            manifest = _build_local_cache(
                directory,
                {
                    "game-a": gzip_jsonl(broken),
                    "game-b": gzip_jsonl(simple_game()),
                },
                snapshot,
            )
            with (
                mock.patch.object(
                    qualification,
                    "EXPECTED_CORPUS_IDENTITY",
                    manifest["corpus_identity"],
                ),
                mock.patch.object(
                    qualification,
                    "EXPECTED_MANIFEST_SHA256",
                    manifest["manifest_sha256"],
                ),
            ):
                report = qualify_local_corpus(snapshot, directory)
        self.assertIs(
            report.overall_outcome, OverallOutcome.DOWNSTREAM_RECONSTRUCTION_PARTIAL
        )
        self.assertEqual(report.behavior.games_unsupported, 1)
        self.assertEqual(
            report.behavior.game_unsupported_reasons,
            {UnsupportedReason.MISSING_REQUIRED_FIELD.value: 1},
        )

    def test_report_value_carries_only_aggregates(self) -> None:
        snapshot = _snapshot_for({"game-a": [(126, 0)]})
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            manifest = _build_local_cache(
                directory,
                {"game-a": gzip_jsonl(all_action_families_game())},
                snapshot,
            )
            with (
                mock.patch.object(
                    qualification,
                    "EXPECTED_CORPUS_IDENTITY",
                    manifest["corpus_identity"],
                ),
                mock.patch.object(
                    qualification,
                    "EXPECTED_MANIFEST_SHA256",
                    manifest["manifest_sha256"],
                ),
            ):
                report = qualify_local_corpus(snapshot, directory)
            value = report.to_value()
            self.assertEqual(value["schema"], REPORT_SCHEMA_ID)
            serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
            for forbidden in ("tehais", "5mr", "dahai", "consumed", "uradora"):
                self.assertNotIn(forbidden, serialized)


def _run_cli(argv: list[str]) -> int:
    """CLIのstdout出力を抑えたままexit codeだけを取得する。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return corpus_main.main(argv)


class OfflineCliTests(unittest.TestCase):
    """CLI subcommandのoffline boundary tests。"""

    def test_qualify_downstream_never_touches_the_network(self) -> None:
        snapshot = _snapshot_for({"game-a": [(126, 0)]})

        class ForbiddenTransport:
            def __init__(self, *args, **kwargs):
                raise AssertionError("qualification must not construct a transport")

        def forbidden(*args, **kwargs):
            raise AssertionError("qualification must not use the acquisition path")

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            manifest = _build_local_cache(
                directory,
                {"game-a": gzip_jsonl(all_action_families_game())},
                snapshot,
            )
            snapshot_path = directory / "snapshot.json"
            write_new_json(snapshot_path, snapshot.to_value())
            report_path = directory / report_filename(snapshot.snapshot_identity)
            with (
                mock.patch.object(
                    corpus_main, "StdlibHttpTransport", ForbiddenTransport
                ),
                mock.patch.object(corpus_main, "snapshot_recent_games", forbidden),
                mock.patch.object(corpus_main, "acquire_from_plan", forbidden),
                mock.patch.object(
                    qualification,
                    "EXPECTED_CORPUS_IDENTITY",
                    manifest["corpus_identity"],
                ),
                mock.patch.object(
                    qualification,
                    "EXPECTED_MANIFEST_SHA256",
                    manifest["manifest_sha256"],
                ),
            ):
                exit_code = _run_cli(
                    [
                        "qualify-downstream",
                        "--snapshot",
                        str(snapshot_path),
                        "--output-dir",
                        str(directory),
                        "--report-output",
                        str(report_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            written = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(written["schema"], REPORT_SCHEMA_ID)
            self.assertEqual(
                written["overall_outcome"],
                OverallOutcome.BOTH_SURFACES_TECHNICALLY_QUALIFIED.value,
            )

    def test_identity_mismatch_exits_non_zero(self) -> None:
        snapshot = _snapshot_for({"game-a": [(126, 0)]})
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            _build_local_cache(
                directory,
                {"game-a": gzip_jsonl(all_action_families_game())},
                snapshot,
            )
            snapshot_path = directory / "snapshot.json"
            write_new_json(snapshot_path, snapshot.to_value())
            report_path = directory / "stop-report.json"
            exit_code = _run_cli(
                [
                    "qualify-downstream",
                    "--snapshot",
                    str(snapshot_path),
                    "--output-dir",
                    str(directory),
                    "--report-output",
                    str(report_path),
                ]
            )
        self.assertEqual(exit_code, 1)

    def test_qualification_package_imports_no_network_module(self) -> None:
        package = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "lisjong_arena"
            / "riichilab_downstream_qualification"
        )
        forbidden = {
            "lisjong_arena.riichilab_corpus.http",
            "lisjong_arena.riichilab_corpus.api",
            "lisjong_arena.riichilab_corpus.acquisition",
            "urllib",
            "urllib.request",
            "http",
            "socket",
            "websockets",
        }
        modules = sorted(package.glob("*.py"))
        self.assertTrue(modules)
        for module in modules:
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name, forbidden, msg=module.name)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, forbidden, msg=module.name)


if __name__ == "__main__":
    unittest.main()
