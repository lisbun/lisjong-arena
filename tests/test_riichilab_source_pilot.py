"""Issue #211 RiichiLab source pilot — non-ML tests。

このtestは実#170 corpusを読まない。すべてsynthetic / public-format
fixtureとArena自身のlocal RiichiEnv実行だけを使う。
"""

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _riichilab_source_pilot_fixtures import (
    ankan_log,
    chi_log,
    daiminkan_log,
    explicit_pass_log,
    future_divergent_logs,
    generated_game_log,
    hidden_variant_logs,
    kakan_log,
    kyuushu_log,
    multi_ron_log,
    normal_discard_log,
    pon_log,
    red_five_log,
    riichi_duplicate_tile_ankan_log,
    riichi_duplicate_tile_tsumo_log,
    riichi_log,
    riichi_stale_ron_offer_log,
    riichi_stick_log,
    ron_log,
    tile_counts,
    tsumo_win_log,
    unknown_event_log,
)
from _source_pilot_strength_fixtures import save_strength_artifact
from lisjong.action_vocabulary import (
    build_legal_action_mask,
    decode_action,
    encode_action,
    resolve_legal_action,
)
from lisjong.policy_contract.action import AnkanAction, DiscardAction, TsumoAction
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.seat import Seat
from riichienv import ActionType, RiichiEnv

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_input import (
    build_policy_input_feature,
    tensor_values,
)
from lisjong_arena.riichienv.adapter import (
    RiichiEnvActionMappingSession,
    SeatMaterializedState,
    build_decision,
    tile_from_mjai,
)
from lisjong_arena.riichilab_corpus.models import (
    CorpusError,
    Participation,
    RecentGamesSnapshot,
)
from lisjong_arena.riichilab_corpus.persistence import ensure_outside_git_worktree
from lisjong_arena.riichilab_corpus.validation import parse_jsonl_gzip
from lisjong_arena.riichilab_source_pilot import __main__ as cli_module
from lisjong_arena.riichilab_source_pilot import dataset as dataset_module
from lisjong_arena.riichilab_source_pilot import (
    materialization as materialization_module,
)
from lisjong_arena.riichilab_source_pilot import protocol as protocol_module
from lisjong_arena.riichilab_source_pilot.artifact import (
    RESULT_FILENAME,
    SEED_PLAN_FILENAME,
    STRENGTH_ARTIFACT_FILENAME,
    load_result,
    result_identity,
    save_result,
    seed_plan_document,
    validate_result,
    validate_seed_plan,
)
from lisjong_arena.riichilab_source_pilot.bundle import verify_bundle
from lisjong_arena.riichilab_source_pilot.dataset import (
    Gate0Report,
    build_row_budget,
    build_source,
    canonical_game_order,
    rows_identity,
)
from lisjong_arena.riichilab_source_pilot.errors import (
    BudgetNotMatchableError,
    MaterializationError,
    SourcePilotArtifactError,
    SourcePilotProtocolError,
)
from lisjong_arena.riichilab_source_pilot.experiment import (
    persist_stop_invalid,
    run_source_pilot,
)
from lisjong_arena.riichilab_source_pilot.materialization import (
    DecisionKind,
    GameMaterialization,
    GameUnsupportedReason,
    MaterializedRow,
    RowUnresolvedReason,
    materialize_game,
    pack_feature_values,
)
from lisjong_arena.riichilab_source_pilot.outcome import classify_outcome
from lisjong_arena.riichilab_source_pilot.protocol import (
    ARM_R,
    ARM_Y,
    ARM_Y_TEST_SEEDS_NEVER_READ,
    EVALUATION_GAME_COUNT,
    EVALUATION_SEEDS,
    MINIMUM_LEGAL_ACTION_COUNT,
    TRAIN_ROW_BUDGET,
    VALIDATION_ROW_BUDGET,
    SourcePilotOutcome,
    derive_policy_identity,
    plan_document,
    require_evaluation_seeds,
    validate_plan,
)

GAME_MODE = "4p-red-single"
ALL_SEATS = {Seat(index): index for index in range(4)}


class _FakeRawObservation:
    """`_repair_discard_identity`が読む4 fieldだけを持つ最小限のtest double。"""

    def __init__(self, *, hand, drawn_tile, player_id, riichi_declared):
        self.hand = hand
        self.drawn_tile = drawn_tile
        self.player_id = player_id
        self.riichi_declared = riichi_declared


_GENERATED_CACHE: dict[tuple[int, str], tuple] = {}


def generated(seed: int, *, game_mode: str = GAME_MODE):
    """1 gameのgeneration結果をtest module内で一度だけ計算する。"""
    key = (seed, game_mode)
    if key not in _GENERATED_CACHE:
        _GENERATED_CACHE[key] = generated_game_log(seed, game_mode=game_mode)
    return _GENERATED_CACHE[key]


def materialize(log, *, seats=None, game_id="fixture"):
    return materialize_game(
        log,
        game_id=game_id,
        target_seats=ALL_SEATS if seats is None else seats,
        game_mode=GAME_MODE,
    )


def live_legal_mask_at_second_action(log, *, seat: Seat, action_type: ActionType):
    """productionのlive-play adapter（`build_decision`）だけを使い、
    materializationのalias repairを一切経由しないlegal action maskを
    独立に構築する。

    `log`を最初からfresh `RiichiEnv`へ`apply_event()`で再生し、`seat`が
    `action_type`をlegal_actionsへ持つ最初のdecisionで
    `build_decision()`（`LocalGameRunner`が実際のlive対局で使うのと同じ
    関数）を呼ぶ。materialization moduleの`_repair_discard_identity`や
    replay-decision authorityには一切触れないため、materialized rowの
    legal maskと比較すればMJAI replayの物理ID collapseを経由しない
    独立した確認になる。
    """
    env = RiichiEnv(game_mode=GAME_MODE)
    tracker = SeatMaterializedState(seat)
    mapping_session = RiichiEnvActionMappingSession(seat)
    seat_events: list[str] = []
    consumed = 0
    for event in log:
        env.apply_event(event)
        observation = env.get_observations().get(int(seat))
        if observation is None:
            continue
        seat_events.extend(observation.new_events())
        if not observation.legal_actions():
            continue
        new_events = seat_events[consumed:]
        consumed = len(seat_events)
        decision = build_decision(
            tracker, observation, mapping_session, new_events=new_events
        )
        if any(
            action.action_type == action_type for action in observation.legal_actions()
        ):
            return build_legal_action_mask(decision.context)
    raise AssertionError(f"seat {seat} never had {action_type} among its legal actions")


class LockedProtocolTests(unittest.TestCase):
    def test_row_budget_is_the_locked_190_s20_population(self):
        self.assertEqual(TRAIN_ROW_BUDGET, 9_116)
        self.assertEqual(VALIDATION_ROW_BUDGET, 2_555)
        self.assertEqual(
            protocol_module.ARM_Y_PREFIX_BINDING["row_count"],
            TRAIN_ROW_BUDGET + VALIDATION_ROW_BUDGET,
        )

    def test_locked_source_identities(self):
        sources = protocol_module.source_identity_block()
        self.assertEqual(
            sources["ARM_Y"]["dataset_identity"],
            "69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4",
        )
        self.assertEqual(
            sources["ARM_R"]["corpus_identity"],
            "064b949733b13026bdbe951c9f69b90653c5977e894449d23f99ce830718b865",
        )
        self.assertEqual(
            sources["ARM_R"]["manifest_sha256"],
            "378edce0a3a117abd47e1f400e92c59de52be2fb990947865c08d815b2f9b0ca",
        )

    def test_locked_student_contract(self):
        student = plan_document()["student"]
        self.assertEqual(student["feature"]["dimension"], 8204)
        self.assertEqual(
            student["feature"]["semantics_id"], "arena-policy-input-feature-v1"
        )
        self.assertEqual(student["vocabulary"]["size"], 802)
        self.assertEqual(
            student["vocabulary"]["version"], "lisjong-action-vocabulary-1"
        )
        self.assertEqual(student["model"]["hidden_width"], 128)
        self.assertEqual(student["model"]["activation"], "relu")
        self.assertEqual(student["training"]["batch_size"], 256)
        self.assertEqual(student["training"]["maximum_epochs"], 20)
        self.assertEqual(student["training"]["early_stop_patience"], 4)
        self.assertEqual(student["training"]["learning_rate"], 1e-3)
        self.assertEqual(student["training"]["weight_decay"], 0.0)
        self.assertEqual(student["training"]["training_seed"], 0)
        self.assertEqual(student["training"]["dataloader_seed"], 0)
        self.assertEqual(student["training"]["dataloader_workers"], 0)
        self.assertEqual(student["training"]["torch_threads"], 1)
        self.assertEqual(
            student["training"]["checkpoint_selection"],
            "lowest_validation_choice_row_masked_ce",
        )
        self.assertEqual(
            student["training"]["loss"], "masked_cross_entropy_over_legal_actions"
        )
        self.assertTrue(student["same_trainer_for_both_arms"])

    def test_excluded_components_cover_the_forbidden_interventions(self):
        excluded = set(plan_document()["excluded_components"])
        for name in (
            "p1_8241_features",
            "p2_tile_structured_architecture",
            "p3_reward_change",
            "p4_auxiliary_head",
            "p6_conservative_q",
            "hand_belief",
            "class_weighting",
            "oversampling",
            "label_smoothing",
            "hpo",
            "multiple_training_seeds",
            "unmasked_cross_entropy_fallback",
            "source_mixing",
        ):
            self.assertIn(name, excluded)

    def test_locked_evaluation_population(self):
        evaluation = plan_document()["evaluation"]
        self.assertEqual(evaluation["seeds"], list(range(23000, 23100)))
        self.assertEqual(evaluation["seed_blocks"], 100)
        self.assertEqual(evaluation["rotations_per_seed"], 4)
        self.assertEqual(evaluation["games"], EVALUATION_GAME_COUNT)
        self.assertEqual(evaluation["games"], 400)
        self.assertEqual(evaluation["game_mode"], "4p-red-single")
        self.assertEqual(evaluation["candidate_arm"], ARM_R.value)
        self.assertEqual(evaluation["baseline_arm"], ARM_Y.value)
        self.assertFalse(evaluation["formal_test_exposure"])
        self.assertFalse(evaluation["post_exposure_extension_allowed"])

    def test_evaluation_population_does_not_touch_the_140_test_seeds(self):
        self.assertEqual(ARM_Y_TEST_SEEDS_NEVER_READ, tuple(range(271, 277)))
        self.assertFalse(set(EVALUATION_SEEDS) & set(ARM_Y_TEST_SEEDS_NEVER_READ))

    def test_seed_population_cannot_silently_expand(self):
        require_evaluation_seeds(EVALUATION_SEEDS)
        for rejected in (
            EVALUATION_SEEDS + (23100,),
            EVALUATION_SEEDS[:-1],
            tuple(range(22800, 22900)),
            tuple(reversed(EVALUATION_SEEDS)),
        ):
            with self.assertRaises(SourcePilotProtocolError):
                require_evaluation_seeds(rejected)

    def test_plan_drift_is_rejected(self):
        document = plan_document()
        validate_plan(document)
        drifted = plan_document()
        drifted["budget"]["train_rows"] = 9_117
        with self.assertRaises(SourcePilotProtocolError):
            validate_plan(drifted)

    def test_policy_identities_differ_per_arm(self):
        digest = "a" * 64
        self.assertNotEqual(
            derive_policy_identity(ARM_R, digest),
            derive_policy_identity(ARM_Y, digest),
        )
        self.assertTrue(
            derive_policy_identity(ARM_R, digest).startswith("learned-source-pilot-r:")
        )
        with self.assertRaises(SourcePilotProtocolError):
            derive_policy_identity(ARM_R, "short")

    def test_outcomes_are_exhaustive_and_unique(self):
        values = [outcome.value for outcome in SourcePilotOutcome]
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(
            set(values),
            {
                "RIICHILAB SOURCE SIGNAL",
                "YAKUHAI-CALL SOURCE SIGNAL",
                "SOURCE PILOT INCONCLUSIVE",
                "SOURCE MATERIALIZATION BLOCKED",
                "DATA BUDGET NOT MATCHABLE",
                "STOP / INVALID",
            },
        )


class OutcomeDecisionOrderTests(unittest.TestCase):
    def test_decision_order(self):
        self.assertIs(
            classify_outcome(
                protocol_valid=False,
                gate0_passed=False,
                budget_matched=False,
                interval_lower=None,
                interval_upper=None,
            ),
            SourcePilotOutcome.STOP_INVALID,
        )
        self.assertIs(
            classify_outcome(
                protocol_valid=True,
                gate0_passed=False,
                budget_matched=False,
                interval_lower=None,
                interval_upper=None,
            ),
            SourcePilotOutcome.SOURCE_MATERIALIZATION_BLOCKED,
        )
        self.assertIs(
            classify_outcome(
                protocol_valid=True,
                gate0_passed=True,
                budget_matched=False,
                interval_lower=None,
                interval_upper=None,
            ),
            SourcePilotOutcome.DATA_BUDGET_NOT_MATCHABLE,
        )
        self.assertIs(
            classify_outcome(
                protocol_valid=True,
                gate0_passed=True,
                budget_matched=True,
                interval_lower=0.5,
                interval_upper=2.0,
            ),
            SourcePilotOutcome.RIICHILAB_SOURCE_SIGNAL,
        )
        self.assertIs(
            classify_outcome(
                protocol_valid=True,
                gate0_passed=True,
                budget_matched=True,
                interval_lower=-2.0,
                interval_upper=-0.5,
            ),
            SourcePilotOutcome.YAKUHAI_CALL_SOURCE_SIGNAL,
        )
        self.assertIs(
            classify_outcome(
                protocol_valid=True,
                gate0_passed=True,
                budget_matched=True,
                interval_lower=-1.0,
                interval_upper=1.0,
            ),
            SourcePilotOutcome.SOURCE_PILOT_INCONCLUSIVE,
        )

    def test_interval_bounds_are_required_for_a_valid_result(self):
        with self.assertRaises(SourcePilotProtocolError):
            classify_outcome(
                protocol_valid=True,
                gate0_passed=True,
                budget_matched=True,
                interval_lower=None,
                interval_upper=1.0,
            )
        with self.assertRaises(SourcePilotProtocolError):
            classify_outcome(
                protocol_valid=True,
                gate0_passed=True,
                budget_matched=True,
                interval_lower=1.0,
                interval_upper=-1.0,
            )


class FixtureHygieneTests(unittest.TestCase):
    def test_fixtures_respect_physical_tile_limits(self):
        logs = {
            "normal_discard": normal_discard_log(),
            "riichi": riichi_log(),
            "chi": chi_log(),
            "pon": pon_log(),
            "daiminkan": daiminkan_log(),
            "ankan": kakan_log(with_chankan_ron=False),
            "kakan_chankan": kakan_log(with_chankan_ron=True),
            "tsumo_win": tsumo_win_log(),
            "ron": ron_log(),
            "multi_ron": multi_ron_log(),
            "kyuushu": kyuushu_log(),
            "explicit_pass": explicit_pass_log(),
            "red_five": red_five_log(),
        }
        for name, log in logs.items():
            with self.subTest(fixture=name):
                for tile, count in tile_counts(log).items():
                    limit = (
                        1
                        if tile.endswith("r")
                        else 3
                        if tile in ("5m", "5p", "5s")
                        else 4
                    )
                    self.assertLessEqual(count, limit, f"{name}:{tile}")


class MaterializationCoverageTests(unittest.TestCase):
    def test_normal_discard_covers_tsumogiri_and_tedashi(self):
        result = materialize(normal_discard_log())
        self.assertTrue(result.supported)
        discards = [
            row for row in result.rows if row.teacher_action_family == "discard"
        ]
        self.assertEqual(len(discards), 2)
        selected = []
        for row in discards:
            action = decode_action(row.teacher_action_index, actor=Seat(row.actor_seat))
            selected.append(action.tsumogiri)
        self.assertEqual(sorted(selected), [False, True])

    def test_riichi_declaration_and_riichi_discard(self):
        result = materialize(riichi_log())
        self.assertTrue(result.supported)
        families = [row.teacher_action_family for row in result.rows]
        self.assertIn("reach", families)
        # riichi宣言の直後のdiscardはforced row（legal actionが1つ）である。
        self.assertEqual(result.forced_rows, 1)

    def test_forced_discard_still_validates_teacher_tsumogiri(self):
        log = riichi_log()
        log[4]["tsumogiri"] = False
        result = materialize(log)
        self.assertTrue(result.supported)
        self.assertEqual(result.forced_rows, 0)
        self.assertIn(
            (RowUnresolvedReason.TEACHER_ACTION_NOT_IN_EXACT_LEGAL_SET.value, 1),
            result.unresolved_reasons,
        )

    def test_riichi_duplicate_drawn_tile_ankan_choice_is_exact_tsumogiri(self):
        """riichi中に4枚目の同種牌を自摸したAnkan choice rowはunresolvedにしない。

        drawn tileと同じsemantic tileが既に3枚hand中にあり、MJAI replayの
        physical ID再構成は本来ambiguousになる
        (`RowUnresolvedReason.DRAWN_TILE_SLOTS_RESTRICTED`)。RiichiEnv 0.4.10は
        riichi_declared中のDiscard legal actionを常に`drawn_tile`だけから
        構築するため、残り1件のdiscard candidateはtsumogiriで確定している。
        """
        log = riichi_duplicate_tile_ankan_log()
        result = materialize(log)
        self.assertTrue(result.supported, result.unsupported_reason)
        self.assertEqual(result.unresolved_reasons, ())
        ankan_rows = [
            row
            for row in result.rows
            if row.teacher_action_family == "ankan" and row.is_riichi_declared
        ]
        self.assertEqual(len(ankan_rows), 1)
        row = ankan_rows[0]
        self.assertEqual(row.legal_action_count, 2)

        actions = self._decode_legal_actions(row)
        discards = [a for a in actions if isinstance(a, DiscardAction)]
        second_actions = [a for a in actions if isinstance(a, AnkanAction)]
        self.assertEqual(len(discards), 1)
        self.assertEqual(len(second_actions), 1)
        self.assertTrue(discards[0].tsumogiri)
        # 自摸した4枚目の"1m"がsemantic drawn tileであり、discard candidateは
        # exactにそれと一致する（他のcopyへtedashiで逃げる余地はない）。
        self.assertEqual(discards[0].tile, tile_from_mjai("1m"))

        live_mask = live_legal_mask_at_second_action(
            log, seat=Seat(0), action_type=ActionType.ANKAN
        )
        self.assertEqual(live_mask, row.legal_mask)

    def test_riichi_duplicate_drawn_tile_tsumo_choice_is_exact_tsumogiri(self):
        """同じduplicate-tile alias patternの、第2 actionがTsumoである版。"""
        log = riichi_duplicate_tile_tsumo_log()
        result = materialize(log)
        self.assertTrue(result.supported, result.unsupported_reason)
        self.assertEqual(result.unresolved_reasons, ())
        tsumo_rows = [
            row
            for row in result.rows
            if row.teacher_action_family == "tsumo" and row.is_riichi_declared
        ]
        self.assertEqual(len(tsumo_rows), 1)
        row = tsumo_rows[0]
        self.assertEqual(row.legal_action_count, 2)

        actions = self._decode_legal_actions(row)
        discards = [a for a in actions if isinstance(a, DiscardAction)]
        second_actions = [a for a in actions if isinstance(a, TsumoAction)]
        self.assertEqual(len(discards), 1)
        self.assertEqual(len(second_actions), 1)
        self.assertTrue(discards[0].tsumogiri)
        # tanki待ちの2枚目の"5s"を自摸しており、discard candidateはexactに
        # その自摸牌と一致する。
        self.assertEqual(discards[0].tile, tile_from_mjai("5s"))

        live_mask = live_legal_mask_at_second_action(
            log, seat=Seat(0), action_type=ActionType.TSUMO
        )
        self.assertEqual(live_mask, row.legal_mask)

    def _decode_legal_actions(self, row: MaterializedRow):
        actor = Seat(row.actor_seat)
        return [
            decode_action(index, actor=actor)
            for index, bit in enumerate(row.legal_mask)
            if bit
        ]

    def test_unrelated_physical_alias_ambiguity_still_fails_closed(self):
        """riichi修正はriichi_declaredの場合だけに限定される。

        RiichiEnv 0.4.10のriichi以外のDiscard legal action生成は常に
        hand配列の各slotを直接反復するため（`legal_actions.rs`）、
        drawn tile と同じsemantic tileが複数枚あればoffered_countは必ず
        物理slot数と一致し、この`_repair_discard_identity`のambiguous
        分岐(offered_countが1件だけ提示される場合)はriichi_declared以外
        では到達しない。したがって既存fixtureで非riichiの反例を作れず、
        `_repair_discard_identity`自体をriichi_declared=Falseで直接
        呼び出し、既存のfail-closed経路がそのまま残っていることを確認する。
        """
        hand = [0, 0, 4, 8]  # 同じidへcollapseされた2枚の"1m" + 2m + 3m
        legal_actions = (
            materialization_module._ReplayAction(
                action_type=ActionType.DISCARD,
                actor=0,
                tile=0,
                consume_tiles=(),
            ),
        )
        non_riichi_raw = _FakeRawObservation(
            hand=hand,
            drawn_tile=0,
            player_id=0,
            riichi_declared=[False, False, False, False],
        )
        _, _, _, ambiguous = materialization_module._repair_discard_identity(
            non_riichi_raw, legal_actions
        )
        self.assertTrue(ambiguous)

        riichi_raw = _FakeRawObservation(
            hand=hand,
            drawn_tile=0,
            player_id=0,
            riichi_declared=[True, False, False, False],
        )
        _, _, _, riichi_ambiguous = materialization_module._repair_discard_identity(
            riichi_raw, legal_actions
        )
        self.assertFalse(riichi_ambiguous)

    def test_riichi_stale_ron_offer_after_permanent_furiten_is_exact_forced_pass(self):
        """riichi中にRonを2回見送っても、2回目はexact forced Pass rowになる。

        1回目の見送りはKyoku.steps()がRon付きのriichi Pass observationとして
        記録し、seat 0はその局で以後permanent riichi furitenになる。
        `RiichiEnv.apply_event()`駆動のforward stateはこの遷移を独立に再現
        しないため、2回目の見送りでも(誤って)Ronをlegalとして提示し続ける。
        Kyoku.steps()自身の記録済みPass observationだけを根拠に、この既知の
        stale Ron提示だけを取り除き、gameをfail closedにしない。
        """
        result = materialize(riichi_stale_ron_offer_log())
        self.assertTrue(result.supported, result.unsupported_reason)
        self.assertEqual(result.unresolved_reasons, ())
        pass_choice_rows = [
            row
            for row in result.rows
            if row.actor_seat == 0
            and row.teacher_action_family == "pass"
            and row.is_riichi_declared
        ]
        # 1回目の見送りはRon/Passの本物のchoice rowとしてexact materializeされる。
        self.assertEqual(len(pass_choice_rows), 1)
        self.assertEqual(pass_choice_rows[0].legal_action_count, 2)
        # 2回目の見送り(stale Ron提示)はforced row(legal actionが1つ)として
        # 消費され、REPLAY_DECISION_ALIGNMENT_FAILEDでgame全体をfail closed
        # にしない。
        self.assertEqual(result.forced_rows, 2)

    def test_call_families_are_materialized(self):
        for name, log, family in (
            ("chi", chi_log(), "chi"),
            ("pon", pon_log(), "pon"),
            ("daiminkan", daiminkan_log(), "daiminkan"),
            ("ankan", kakan_log(with_chankan_ron=False), "kakan"),
        ):
            with self.subTest(fixture=name):
                result = materialize(log)
                self.assertTrue(result.supported, result.unsupported_reason)
                self.assertIn(
                    family, [row.teacher_action_family for row in result.rows]
                )

    def test_pon_teacher_uses_exact_two_tile_candidate_and_iterator_pid(self):
        log = pon_log()
        replay_by_prefix, _ = materialization_module._replay_decisions(log)
        replay_pon = next(
            step
            for step in replay_by_prefix.values()
            if step.action_type == ActionType.PON
        )
        self.assertEqual(replay_pon.seat, 2)
        raw_pon = next(
            action
            for action in replay_pon.observation.legal_actions()
            if action.action_type == ActionType.PON
        )
        self.assertIsNone(raw_pon.actor)
        self.assertEqual(len(raw_pon.consume_tiles), 3)
        result = materialize(log)
        row = next(row for row in result.rows if row.teacher_action_family == "pon")
        canonical = decode_action(row.teacher_action_index, actor=Seat(row.actor_seat))
        self.assertEqual(canonical.actor, Seat(2))
        self.assertEqual(canonical.target, Seat(0))
        self.assertEqual(len(canonical.consumed_tiles), 2)

    def test_claim_teacher_targets_match_the_public_trigger(self):
        for log, family in (
            (chi_log(), "chi"),
            (pon_log(), "pon"),
            (daiminkan_log(), "daiminkan"),
            (ron_log(), "ron"),
        ):
            with self.subTest(family=family):
                result = materialize(log)
                row = next(
                    row for row in result.rows if row.teacher_action_family == family
                )
                action = decode_action(
                    row.teacher_action_index, actor=Seat(row.actor_seat)
                )
                self.assertEqual(action.target, Seat(0))

    def test_replay_alias_collision_keeps_exact_public_call_target(self):
        """0.4.10 replayの同一ID重複でも公開打牌者でcallを帰属させる。"""
        log = kakan_log(with_chankan_ron=False)
        env = RiichiEnv(game_mode=GAME_MODE)
        saw_alias_collision = False
        for event in log:
            env.apply_event(event)
            for player_id, observation in env.get_observations().items():
                if observation.last_discard is None or not any(
                    action.action_type in (ActionType.CHI, ActionType.PON)
                    for action in observation.legal_actions()
                ):
                    continue
                matching_opponents = [
                    seat
                    for seat, discards in enumerate(observation.discards)
                    if seat != player_id
                    and discards
                    and discards[-1] == observation.last_discard
                ]
                saw_alias_collision |= len(matching_opponents) > 1

        self.assertTrue(saw_alias_collision)
        result = materialize(log)
        self.assertTrue(result.supported, result.unsupported_reason)
        self.assertEqual(result.unresolved_reasons, ())

    def test_post_call_discard_is_its_own_decision_kind(self):
        result = materialize(chi_log())
        kinds = [row.decision_kind for row in result.rows]
        self.assertIn(DecisionKind.POST_CALL_DISCARD, kinds)
        self.assertIn(DecisionKind.CALL_RESPONSE, kinds)
        self.assertIn(DecisionKind.TURN, kinds)

    def test_tsumo_and_ron_are_materialized(self):
        self.assertIn(
            "tsumo",
            [row.teacher_action_family for row in materialize(tsumo_win_log()).rows],
        )
        self.assertIn(
            "ron", [row.teacher_action_family for row in materialize(ron_log()).rows]
        )

    def test_multi_ron_materializes_every_winner_from_the_same_prefix(self):
        result = materialize(multi_ron_log())
        self.assertTrue(result.supported)
        rons = [row for row in result.rows if row.teacher_action_family == "ron"]
        self.assertEqual(sorted(row.actor_seat for row in rons), [1, 3])
        # 同じtriggerへのresponseなので、両者のround stateは一致する。
        self.assertEqual(
            {(row.round_wind, row.hand_number, row.honba) for row in rons},
            {("EAST", 1, 0)},
        )
        for omitted in (1, 3):
            single = materialize(
                [
                    event
                    for event in multi_ron_log()
                    if not (
                        event.get("type") == "hora" and event.get("actor") == omitted
                    )
                ]
            )
            winner = 3 if omitted == 1 else 1
            multi_row = next(row for row in rons if row.actor_seat == winner)
            single_row = next(
                row for row in single.rows if row.teacher_action_family == "ron"
            )
            self.assertEqual(multi_row.feature_payload, single_row.feature_payload)
            self.assertEqual(
                multi_row.legal_mask_payload, single_row.legal_mask_payload
            )
            self.assertEqual(
                multi_row.teacher_action_index, single_row.teacher_action_index
            )

    def test_ankan_and_kan_dora_do_not_break_the_following_turn(self):
        from _riichilab_source_pilot_fixtures import ankan_log

        result = materialize(ankan_log())
        self.assertTrue(result.supported)
        self.assertIn("ankan", [row.teacher_action_family for row in result.rows])
        self.assertIn("discard", [row.teacher_action_family for row in result.rows])

    def test_current_score_riichi_sticks_and_live_wall_are_exact(self):
        """round開始時点の値ではなく、decision時点のpublic stateを使う。

        feature indexは`docs/learned-policy-input-schema.md`のlocked layout
        である（riichi sticks `[17,18)` / live wall `[18,19)` / relative
        player row `209 + 348 * relative seat`の先頭がscore）。
        """
        result = materialize(riichi_stick_log())
        self.assertTrue(result.supported, result.unsupported_reason)
        rows = {
            (row.actor_seat, row.decision_kind): row.feature_values
            for row in result.rows
        }

        before = rows[(0, DecisionKind.TURN)]
        self.assertEqual(before[17], 0.0)
        self.assertAlmostEqual(before[18], 83 / 84, places=6)
        for relative in range(4):
            self.assertAlmostEqual(before[209 + 348 * relative], 0.25, places=6)

        after = rows[(1, DecisionKind.TURN)]
        # riichi stick 1本がcurrent valueとして入る。
        self.assertAlmostEqual(after[17], 0.1, places=6)
        # tsumo 2件ぶんliveが減っている。
        self.assertAlmostEqual(after[18], 82 / 84, places=6)
        # 宣言者 seat 0 は seat 1 から見て kamicha (relative seat 3) であり、
        # そのscoreは25000ではなく24000である。
        self.assertAlmostEqual(after[209], 0.25, places=6)
        self.assertAlmostEqual(after[209 + 348 * 3], 0.24, places=6)

        later = rows[(2, DecisionKind.TURN)]
        self.assertAlmostEqual(later[18], 81 / 84, places=6)
        self.assertAlmostEqual(later[209 + 348 * 2], 0.24, places=6)

    def test_red_five_identity_is_preserved(self):
        result = materialize(red_five_log())
        self.assertTrue(result.supported)
        row = result.rows[0]
        action = decode_action(row.teacher_action_index, actor=Seat(row.actor_seat))
        self.assertTrue(action.tile.is_red)
        self.assertEqual(action.tile.tile_type.rank, 5)

    def test_explicit_pass_is_materialized_once(self):
        result = materialize(explicit_pass_log())
        self.assertTrue(result.supported)
        passes = [row for row in result.rows if row.teacher_action_family == "pass"]
        explicit = [row for row in passes if not row.implicit_pass]
        self.assertEqual(len(explicit), 1)
        self.assertEqual(explicit[0].actor_seat, 1)
        # 同じdecisionをimplicit passとして二重計上しない。
        self.assertEqual(len([row for row in passes if row.actor_seat == 1]), 1)

    def test_implicit_pass_requires_an_exact_legal_response_opportunity(self):
        result = materialize(normal_discard_log())
        implicit = [row for row in result.rows if row.implicit_pass]
        self.assertEqual(len(implicit), 1)
        row = implicit[0]
        self.assertIs(row.decision_kind, DecisionKind.CALL_RESPONSE)
        self.assertGreaterEqual(row.legal_action_count, MINIMUM_LEGAL_ACTION_COUNT)
        self.assertTrue(row.legal_mask[row.teacher_action_index])
        action = decode_action(row.teacher_action_index, actor=Seat(row.actor_seat))
        self.assertEqual(type(action).__name__, "PassAction")

    def test_pass_after_a_competing_claim_requires_replay_step(self):
        result = materialize(ron_log())
        self.assertTrue(result.supported)
        self.assertEqual(result.unresolved_reasons, ())
        passes = [row for row in result.rows if row.teacher_action_family == "pass"]
        self.assertEqual(
            [(row.actor_seat, row.implicit_pass) for row in passes], [(1, True)]
        )

    def test_unknown_event_type_fails_closed(self):
        result = materialize(unknown_event_log())
        self.assertFalse(result.supported)
        self.assertIs(
            result.unsupported_reason, GameUnsupportedReason.UNRECOGNIZED_EVENT_TYPE
        )
        self.assertEqual(result.rows, ())

    def test_malformed_event_fails_closed(self):
        log = normal_discard_log()
        log[2] = {"type": ["tsumo"], "actor": 0}
        result = materialize(log)
        self.assertFalse(result.supported)
        self.assertIs(result.unsupported_reason, GameUnsupportedReason.MALFORMED_EVENT)

    def test_malformed_mjai_is_rejected_before_either_replay_seam(self):
        cases = []

        log = normal_discard_log()
        log[2]["pai"] = "INVALID"
        cases.append(("invalid draw tile", log))

        log = normal_discard_log()
        log[3]["pai"] = "1mgarbage"
        cases.append(("valid tile prefix", log))

        log = normal_discard_log()
        log[2]["actor"] = 4
        cases.append(("invalid state actor", log))

        log = pon_log()
        next(event for event in log if event["type"] == "pon")["target"] = 4
        cases.append(("invalid call target", log))

        log = pon_log()
        next(event for event in log if event["type"] == "pon")["consumed"] = ["E"]
        cases.append(("short pon consumed", log))

        log = normal_discard_log()
        log[1]["scores"].pop()
        cases.append(("short start scores", log))

        log = normal_discard_log()
        log[1]["tehais"].pop()
        cases.append(("short start hands", log))

        log = normal_discard_log()
        log[1]["tehais"][0][0] = "INVALID"
        cases.append(("invalid initial hand tile", log))

        log = normal_discard_log()
        log[1]["dora_marker"] = "1mgarbage"
        cases.append(("invalid first dora", log))

        log = normal_discard_log()
        log.insert(4, {"type": "dora", "dora_marker": "INVALID"})
        cases.append(("invalid later dora", log))

        log = chi_log()
        next(event for event in log if event["type"] == "chi")["consumed"] = ["1m"]
        cases.append(("short chi consumed", log))

        log = daiminkan_log()
        next(event for event in log if event["type"] == "daiminkan")["consumed"] = [
            "E",
            "E",
        ]
        cases.append(("short daiminkan consumed", log))

        log = ankan_log()
        next(event for event in log if event["type"] == "ankan")["consumed"].pop()
        cases.append(("short ankan consumed", log))

        log = ron_log()
        next(event for event in log if event["type"] == "hora")["pai"] = "INVALID"
        cases.append(("invalid winning tile", log))

        log = normal_discard_log()
        log[3]["tsumogiri"] = "false"
        cases.append(("invalid discard shape", log))

        for name, malformed in cases:
            with self.subTest(name=name):
                with mock.patch.object(
                    materialization_module,
                    "_replay_decisions",
                    side_effect=AssertionError("invalid input reached MjaiReplay"),
                ):
                    result = materialize(malformed)
                self.assertFalse(result.supported)
                self.assertIs(
                    result.unsupported_reason, GameUnsupportedReason.MALFORMED_EVENT
                )
                self.assertEqual(result.rows, ())

    def test_chankan_ron_uses_replay_response_before_future_result(self):
        result = materialize(kakan_log(with_chankan_ron=True))
        self.assertTrue(result.supported, result.unsupported_reason)
        ron = next(row for row in result.rows if row.teacher_action_family == "ron")
        self.assertEqual(ron.actor_seat, 3)
        variant = kakan_log(with_chankan_ron=True)
        hora_event = next(event for event in variant if event.get("type") == "hora")
        hora_event["ura_markers"] = ["9m"]
        variant_ron = next(
            row
            for row in materialize(variant).rows
            if row.teacher_action_family == "ron"
        )
        self.assertEqual(ron.feature_payload, variant_ron.feature_payload)
        self.assertEqual(ron.legal_mask_payload, variant_ron.legal_mask_payload)

    def test_chankan_pass_is_not_inferred_without_replay_step(self):
        log = kakan_log(with_chankan_ron=False)
        kakan_index = next(i for i, event in enumerate(log) if event["type"] == "kakan")
        log.insert(kakan_index + 1, {"type": "none", "actor": 3})
        result = materialize(log)
        self.assertFalse(result.supported)
        self.assertIs(
            result.unsupported_reason,
            GameUnsupportedReason.REPLAY_DECISION_ALIGNMENT_FAILED,
        )

    def test_kyuushu_exact_legal_candidate_is_materialized(self):
        result = materialize(kyuushu_log())
        self.assertTrue(result.supported, result.unsupported_reason)
        self.assertEqual(result.decision_opportunities, 1)
        self.assertEqual(
            [row.teacher_action_family for row in result.rows], ["kyuushu_kyuuhai"]
        )

    def test_kyuushu_teacher_reason_must_be_explicit(self):
        log = kyuushu_log()
        log[3]["reason"] = "exhaustive_draw"
        result = materialize(log)
        self.assertFalse(result.supported)
        self.assertIs(
            result.unsupported_reason,
            GameUnsupportedReason.REPLAY_DECISION_ALIGNMENT_FAILED,
        )

    def test_non_dealer_first_draw_and_later_draw_kyuushu_legality(self):
        from _riichilab_source_pilot_fixtures import (
            dahai,
            start_game,
            start_kyoku,
            tsumo,
        )

        hands = kyuushu_log()[1]["tehais"]
        hands[0], hands[1] = hands[1], hands[0]
        events = [
            start_game(),
            start_kyoku(hands),
            tsumo(0, "2m"),
            dahai(0, "2m", tsumogiri=True),
            tsumo(1, "3m"),
            dahai(1, "3m", tsumogiri=True),
            tsumo(2, "4m"),
            dahai(2, "4m", tsumogiri=True),
            tsumo(3, "5m"),
            dahai(3, "5m", tsumogiri=True),
            tsumo(0, "6m"),
            dahai(0, "6m", tsumogiri=True),
            tsumo(1, "7m"),
        ]
        env = RiichiEnv(game_mode=GAME_MODE)
        for index, event in enumerate(events):
            env.apply_event(event)
            if index in (4, 12):
                legal = env.get_observations()[1].legal_actions()
                present = any(
                    action.action_type == ActionType.KYUSHU_KYUHAI for action in legal
                )
                self.assertEqual(present, index == 4)

    def test_every_retained_row_is_an_exact_choice_row(self):
        for log in (
            normal_discard_log(),
            chi_log(),
            pon_log(),
            daiminkan_log(),
            kakan_log(with_chankan_ron=False),
            multi_ron_log(),
            explicit_pass_log(),
        ):
            result = materialize(log)
            for row in result.rows:
                self.assertEqual(len(row.feature_values), 8204)
                self.assertEqual(len(row.legal_mask), 802)
                self.assertEqual(sum(row.legal_mask), row.legal_action_count)
                self.assertGreaterEqual(
                    row.legal_action_count, MINIMUM_LEGAL_ACTION_COUNT
                )
                self.assertTrue(row.legal_mask[row.teacher_action_index])

    def test_decision_opportunities_account_for_every_row(self):
        for log in (normal_discard_log(), chi_log(), pon_log(), explicit_pass_log()):
            result = materialize(log)
            unresolved = sum(count for _, count in result.unresolved_reasons)
            self.assertEqual(
                result.decision_opportunities,
                len(result.rows) + result.forced_rows + unresolved,
            )

    def test_non_target_seats_produce_no_rows(self):
        result = materialize(chi_log(), seats={Seat(1): 7})
        self.assertTrue(result.supported)
        self.assertEqual({row.actor_seat for row in result.rows}, {1})
        self.assertEqual({row.bot_id for row in result.rows}, {7})
        self.assertEqual(result.decision_opportunities, 2)
        self.assertEqual(
            result.decision_opportunities,
            len(result.rows)
            + result.forced_rows
            + sum(count for _, count in result.unresolved_reasons),
        )

    def test_a_game_needs_at_least_one_target_seat(self):
        with self.assertRaises(MaterializationError):
            materialize_game(
                normal_discard_log(),
                game_id="g",
                target_seats={},
                game_mode=GAME_MODE,
            )


class InformationBoundaryTests(unittest.TestCase):
    def test_opponent_concealed_hands_do_not_change_student_features(self):
        variant_a, variant_b = hidden_variant_logs()
        rows_a = materialize(variant_a, seats={Seat(0): 0}, game_id="a").rows
        rows_b = materialize(variant_b, seats={Seat(0): 0}, game_id="b").rows
        self.assertTrue(rows_a)
        self.assertEqual(len(rows_a), len(rows_b))
        for left, right in zip(rows_a, rows_b, strict=True):
            self.assertEqual(left.feature_payload, right.feature_payload)
            self.assertEqual(left.legal_mask_payload, right.legal_mask_payload)
            self.assertEqual(left.teacher_action_index, right.teacher_action_index)

    def test_future_events_do_not_change_earlier_rows(self):
        short, long = future_divergent_logs()
        rows_short = materialize(short, seats={Seat(0): 0}, game_id="s").rows
        rows_long = materialize(long, seats={Seat(0): 0}, game_id="l").rows
        self.assertTrue(rows_short)
        for left, right in zip(rows_short, rows_long, strict=False):
            self.assertEqual(left.feature_payload, right.feature_payload)
            self.assertEqual(left.legal_mask_payload, right.legal_mask_payload)
            self.assertEqual(left.teacher_action_index, right.teacher_action_index)

    def test_rows_carry_only_player_safe_metadata(self):
        row = materialize(normal_discard_log()).rows[0]
        fields = set(MaterializedRow.__dataclass_fields__)
        for forbidden in (
            "opponent_hand",
            "wall",
            "ura_dora",
            "final_scores",
            "terminal_result",
            "hand_belief",
            "analysis",
            "shanten",
            "ukeire",
        ):
            self.assertNotIn(forbidden, fields)
        self.assertEqual(row.round_wind, "EAST")


class LiveReplayEquivalenceTests(unittest.TestCase):
    """同じevent prefixに対して、live semanticsとreplay semanticsが一致する。"""

    GENERATED_CASES = (
        ("4p-red-single", 245),
        ("4p-red-single", 246),
        ("4p-red-half", 245),
    )

    def test_generated_games_match_live_semantics_exactly(self):
        for game_mode, seed in self.GENERATED_CASES:
            with self.subTest(game_mode=game_mode, seed=seed):
                events, live = generated(seed, game_mode=game_mode)
                result = materialize_game(
                    events,
                    game_id=f"{game_mode}-{seed}",
                    target_seats=ALL_SEATS,
                    game_mode=game_mode,
                )
                self.assertTrue(result.supported, result.unsupported_reason)
                # replay側が観測したdecision opportunityはlive decision数と一致する。
                self.assertEqual(result.decision_opportunities, len(live))

                live_index: dict[tuple, list] = {}
                for seat, policy_input, legal_actions, selected in live:
                    if len(legal_actions) < MINIMUM_LEGAL_ACTION_COUNT:
                        continue
                    key = (
                        seat,
                        pack_feature_values(
                            tensor_values(build_policy_input_feature(policy_input))
                        ),
                    )
                    live_index.setdefault(key, []).append(
                        (policy_input, legal_actions, selected)
                    )

                self.assertTrue(result.rows)
                for row in result.rows:
                    key = (row.actor_seat, row.feature_payload)
                    self.assertIn(key, live_index)
                    policy_input, legal_actions, selected = live_index[key][0]
                    live_mask = build_legal_action_mask(
                        DecisionContext(input=policy_input, legal_actions=legal_actions)
                    )
                    self.assertEqual(row.legal_mask, live_mask)
                    self.assertEqual(row.legal_action_count, len(legal_actions))
                    if not row.implicit_pass:
                        self.assertEqual(
                            row.teacher_action_index, encode_action(selected)
                        )

    def test_materialization_is_deterministic(self):
        events, _ = generated(245)
        first = materialize(events, game_id="seed-245")
        second = materialize(events, game_id="seed-245")
        self.assertEqual(
            [
                (row.feature_payload, row.legal_mask_payload, row.teacher_action_index)
                for row in first.rows
            ],
            [
                (row.feature_payload, row.legal_mask_payload, row.teacher_action_index)
                for row in second.rows
            ],
        )
        self.assertEqual(rows_identity(first.rows), rows_identity(second.rows))

    def test_action_vocabulary_round_trip_for_every_row(self):
        events, live = generated(245)
        result = materialize(events, game_id="seed-245")
        live_index = {}
        for seat, policy_input, legal_actions, selected in live:
            if len(legal_actions) < MINIMUM_LEGAL_ACTION_COUNT:
                continue
            key = (
                seat,
                pack_feature_values(
                    tensor_values(build_policy_input_feature(policy_input))
                ),
            )
            live_index.setdefault(key, []).append((policy_input, legal_actions))
        for row in result.rows:
            policy_input, legal_actions = live_index[
                (row.actor_seat, row.feature_payload)
            ][0]
            decision = DecisionContext(input=policy_input, legal_actions=legal_actions)
            action = resolve_legal_action(row.teacher_action_index, decision)
            self.assertIn(action, legal_actions)
            self.assertEqual(encode_action(action), row.teacher_action_index)


_ROW_TEMPLATE: list = []


def _row_template() -> MaterializedRow:
    if not _ROW_TEMPLATE:
        _ROW_TEMPLATE.append(materialize(normal_discard_log()).rows[0])
    return _ROW_TEMPLATE[0]


def _synthetic_game(
    game_id: str, rows: int, *, supported: bool = True
) -> GameMaterialization:
    template = _row_template()
    made = tuple(
        MaterializedRow(
            game_id=game_id,
            decision_ordinal=index,
            round_ordinal=0,
            round_wind=template.round_wind,
            hand_number=template.hand_number,
            honba=template.honba,
            actor_seat=index % 4,
            bot_id=index % 2,
            decision_kind=template.decision_kind,
            legal_action_count=template.legal_action_count,
            teacher_action_index=template.teacher_action_index,
            teacher_action_family=template.teacher_action_family,
            implicit_pass=False,
            is_open_hand=False,
            is_riichi_declared=False,
            feature_payload=template.feature_payload,
            legal_mask_payload=template.legal_mask_payload,
        )
        for index in range(rows)
    )
    return GameMaterialization(
        game_id=game_id,
        supported=supported,
        unsupported_reason=None
        if supported
        else GameUnsupportedReason.UNRECOGNIZED_EVENT_TYPE,
        rounds=1,
        decision_opportunities=rows,
        rows=made if supported else (),
        forced_rows=0,
        unresolved_reasons=(),
    )


def _source(games, *, identity: str = "c" * 64):
    return build_source(
        tuple(games),
        corpus_identity=identity,
        manifest_sha256="m" * 64,
        snapshot_identity="s" * 64,
        target_seat_counts={game.game_id: 1 for game in games},
    )


class LocalCorpusOutputPathTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = RecentGamesSnapshot(
            retrieved_at="2026-09-12T00:00:00Z",
            source_apis=(),
            target_bots=(),
            participations=(),
            snapshot_identity="s" * 64,
        )

    def test_shared_boundary_accepts_string_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            for output_dir in (directory, Path(directory)):
                with self.subTest(type=type(output_dir).__name__):
                    with (
                        mock.patch.object(
                            dataset_module,
                            "ensure_outside_git_worktree",
                            wraps=ensure_outside_git_worktree,
                        ) as ensure,
                        mock.patch.object(
                            dataset_module,
                            "load_cache_index",
                            side_effect=CorpusError("synthetic cache stop"),
                        ) as load_index,
                    ):
                        with self.assertRaisesRegex(
                            dataset_module.SourceIdentityError,
                            "local cache index is invalid",
                        ):
                            dataset_module.materialize_local_corpus(
                                self.snapshot, output_dir
                            )
                    ensure.assert_called_once_with(Path(directory))
                    load_index.assert_called_once_with(Path(directory).resolve())

    def test_materialize_cli_passes_argparse_string_to_shared_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    cli_module, "_load_snapshot", return_value=self.snapshot
                ),
                mock.patch.object(
                    dataset_module,
                    "load_cache_index",
                    side_effect=CorpusError("synthetic cache stop"),
                ) as load_index,
                mock.patch.object(cli_module, "_emit") as emit,
            ):
                result = cli_module.main(
                    [
                        "materialize",
                        "--snapshot",
                        "synthetic.json",
                        "--output-dir",
                        directory,
                    ]
                )
            self.assertEqual(result, 2)
            load_index.assert_called_once_with(Path(directory).resolve())
            self.assertFalse(emit.call_args.args[0]["authoritative"])

    def test_run_cli_reaches_the_same_shared_boundary_without_running(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    cli_module, "resolve_retention_target", return_value=object()
                ),
                mock.patch.object(
                    cli_module, "_load_snapshot", return_value=self.snapshot
                ),
                mock.patch.object(
                    dataset_module,
                    "load_cache_index",
                    side_effect=CorpusError("synthetic cache stop"),
                ) as load_index,
                mock.patch.object(
                    cli_module, "_stopped", return_value={"synthetic": True}
                ),
                mock.patch.object(cli_module, "load_retained_arm_y_source") as arm_y,
                mock.patch.object(cli_module, "run_source_pilot") as run_pilot,
                mock.patch.object(cli_module, "_emit"),
            ):
                result = cli_module.main(
                    [
                        "run",
                        "--snapshot",
                        "synthetic.json",
                        "--output-dir",
                        directory,
                        "--arm-y-dataset",
                        "unused",
                        "--retention-backend",
                        "unused",
                        "--retention-root",
                        "unused",
                        "--retention-key",
                        "unused",
                    ]
                )
            self.assertEqual(result, 2)
            load_index.assert_called_once_with(Path(directory).resolve())
            arm_y.assert_not_called()
            run_pilot.assert_not_called()


class LocalCorpusParserBoundaryTests(unittest.TestCase):
    def test_real_parser_tuple_reaches_materializer_as_list(self):
        game_id = "synthetic-game"
        events = normal_discard_log()
        payload = gzip.compress(
            "\n".join(json.dumps(event) for event in events).encode("utf-8")
        )
        parsed = parse_jsonl_gzip(payload)
        self.assertIsInstance(parsed, tuple)
        self.assertEqual(parsed, tuple(events))
        snapshot = RecentGamesSnapshot(
            retrieved_at="2026-09-12T00:00:00Z",
            source_apis=(),
            target_bots=(),
            participations=(Participation(game_id, 1, 0, "2026-09-12T00:00:00Z"),),
            snapshot_identity="s" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    dataset_module, "load_cache_index", return_value={game_id: {}}
                ),
                mock.patch.object(
                    dataset_module,
                    "validate_manifest_file",
                    return_value={
                        "corpus_identity": dataset_module.ARM_R_CORPUS_IDENTITY,
                        "manifest_sha256": dataset_module.ARM_R_MANIFEST_SHA256,
                    },
                ),
                mock.patch.object(
                    dataset_module,
                    "_verify_every_cached_game",
                    return_value={game_id: ({Seat(0): 1}, payload)},
                ),
                mock.patch.object(
                    dataset_module,
                    "materialize_game",
                    wraps=materialization_module.materialize_game,
                ) as materialize,
            ):
                source = dataset_module.materialize_local_corpus(snapshot, directory)
        materialize.assert_called_once()
        self.assertIsInstance(materialize.call_args.args[0], list)
        self.assertEqual(materialize.call_args.args[0], list(parsed))
        self.assertTrue(source.games[0].supported)


class Gate0ReportTests(unittest.TestCase):
    def test_gate_fails_when_any_game_is_unsupported(self):
        source = _source(
            [_synthetic_game("g1", 100), _synthetic_game("g2", 0, supported=False)]
        )
        self.assertEqual(source.report.games_unsupported, 1)
        self.assertFalse(source.report.gate_passed)

    def test_gate_fails_when_any_decision_is_unresolved(self):
        game = _synthetic_game("g1", 10)
        unresolved = GameMaterialization(
            game_id=game.game_id,
            supported=True,
            unsupported_reason=None,
            rounds=game.rounds,
            decision_opportunities=game.decision_opportunities + 1,
            rows=game.rows,
            forced_rows=0,
            unresolved_reasons=(
                (RowUnresolvedReason.DRAWN_TILE_SLOTS_RESTRICTED.value, 1),
            ),
        )
        source = _source([unresolved])
        self.assertEqual(source.report.unresolved_rows, 1)
        self.assertFalse(source.report.gate_passed)

    def test_gate_passes_only_on_an_exactly_materialized_population(self):
        source = _source([_synthetic_game("g1", 10)])
        self.assertTrue(source.report.gate_passed)
        document = source.report.to_document()
        self.assertTrue(document["gate_passed"])
        self.assertEqual(document["unresolved_rows"], 0)
        self.assertEqual(document["games_unsupported"], 0)
        self.assertEqual(document["leakage_check_failures"], 0)
        self.assertIn("apply_event", document["replay_seam"])
        self.assertIn("MjaiReplay.Kyoku.steps", document["replay_seam"])

    def test_empty_population_does_not_pass_the_gate(self):
        self.assertFalse(_source([_synthetic_game("g1", 0)]).report.gate_passed)

    def test_report_is_a_plain_document(self):
        report = _source([_synthetic_game("g1", 10)]).report
        self.assertIsInstance(report, Gate0Report)
        self.assertEqual(
            report.to_document()["minimum_legal_action_count"],
            MINIMUM_LEGAL_ACTION_COUNT,
        )


_SUFFICIENT_SOURCES: dict[str, object] = {}
_BUDGETS: dict[str, object] = {}


def _sufficient_games():
    return [_synthetic_game(f"g{index:03d}", 400) for index in range(30)]


class RowBudgetTests(unittest.TestCase):
    def _sufficient_source(self, *, identity: str = "c" * 64):
        if identity not in _SUFFICIENT_SOURCES:
            _SUFFICIENT_SOURCES[identity] = _source(
                _sufficient_games(), identity=identity
            )
        return _SUFFICIENT_SOURCES[identity]

    def _budget(self, *, identity: str = "c" * 64):
        if identity not in _BUDGETS:
            _BUDGETS[identity] = build_row_budget(
                self._sufficient_source(identity=identity)
            )
        return _BUDGETS[identity]

    def test_exact_budget_is_formed_when_rows_are_sufficient(self):
        budget = self._budget()
        self.assertEqual(len(budget.train_rows), TRAIN_ROW_BUDGET)
        self.assertEqual(len(budget.validation_rows), VALIDATION_ROW_BUDGET)

    def test_a_raw_game_never_crosses_the_partition(self):
        budget = self._budget()
        self.assertFalse(set(budget.train_game_ids) & set(budget.validation_game_ids))
        self.assertFalse(
            {row.game_id for row in budget.train_rows}
            & {row.game_id for row in budget.validation_rows}
        )

    def test_partition_and_selection_are_deterministic(self):
        first = self._budget()
        second = build_row_budget(self._sufficient_source())
        self.assertEqual(first.train_game_ids, second.train_game_ids)
        self.assertEqual(first.validation_game_ids, second.validation_game_ids)
        self.assertEqual(
            rows_identity(first.train_rows), rows_identity(second.train_rows)
        )
        self.assertEqual(
            rows_identity(first.validation_rows), rows_identity(second.validation_rows)
        )

    def test_canonical_game_order_is_bound_to_the_source_identity(self):
        left = canonical_game_order(self._sufficient_source(identity="c" * 64))
        right = canonical_game_order(self._sufficient_source(identity="d" * 64))
        self.assertNotEqual(
            [game.game_id for game in left], [game.game_id for game in right]
        )

    def test_canonical_game_order_ignores_row_counts(self):
        games = _sufficient_games()
        shuffled = list(reversed(games))
        ordered_a = canonical_game_order(_source(games))
        ordered_b = canonical_game_order(_source(shuffled))
        self.assertEqual(
            [game.game_id for game in ordered_a],
            [game.game_id for game in ordered_b],
        )

    def test_insufficient_rows_cannot_be_rescued(self):
        games = [_synthetic_game(f"g{index:03d}", 100) for index in range(20)]
        with self.assertRaises(BudgetNotMatchableError):
            build_row_budget(_source(games))

    def test_budget_is_not_formed_before_gate0_passes(self):
        games = _sufficient_games()
        games.append(_synthetic_game("bad", 0, supported=False))
        with self.assertRaises(MaterializationError):
            build_row_budget(_source(games))

    def test_distribution_document_is_descriptive_only(self):
        budget = self._budget()
        document = budget.distribution_document()
        self.assertEqual(document["train"]["rows"], TRAIN_ROW_BUDGET)
        self.assertEqual(document["validation"]["rows"], VALIDATION_ROW_BUDGET)
        for partition in ("train", "validation"):
            for key in (
                "rows_per_target_bot",
                "action_family_counts",
                "decision_kind_counts",
                "legal_action_count_distribution",
                "open_hand_rows",
                "closed_hand_rows",
                "riichi_rows",
                "non_riichi_rows",
                "implicit_pass_rows",
                "explicit_action_rows",
            ):
                self.assertIn(key, document[partition])

    def test_budget_value_rejects_wrong_row_counts(self):
        budget = self._budget()
        with self.assertRaises(BudgetNotMatchableError):
            dataset_module.RowBudget(
                train_game_ids=budget.train_game_ids,
                validation_game_ids=budget.validation_game_ids,
                train_rows=budget.train_rows[:-1],
                validation_rows=budget.validation_rows,
            )

    def test_budget_value_rejects_a_shared_game(self):
        budget = self._budget()
        with self.assertRaises(BudgetNotMatchableError):
            dataset_module.RowBudget(
                train_game_ids=budget.train_game_ids + budget.validation_game_ids,
                validation_game_ids=budget.validation_game_ids,
                train_rows=budget.train_rows,
                validation_rows=budget.validation_rows,
            )


class SeedPlanArtifactTests(unittest.TestCase):
    def test_seed_plan_binds_the_locked_population(self):
        document = seed_plan_document(
            candidate_identity="learned-source-pilot-r:" + "a" * 64,
            baseline_identity="learned-source-pilot-y:" + "b" * 64,
        )
        validate_seed_plan(document)
        self.assertEqual(document["evaluation"]["seeds"], list(EVALUATION_SEEDS))

    def test_seed_plan_cannot_be_edited_after_the_fact(self):
        document = seed_plan_document(
            candidate_identity="learned-source-pilot-r:" + "a" * 64,
            baseline_identity="learned-source-pilot-y:" + "b" * 64,
        )
        document["evaluation"]["seeds"].append(23100)
        with self.assertRaises(SourcePilotArtifactError):
            validate_seed_plan(document)

    def test_result_must_carry_a_locked_outcome_and_identity(self):
        with self.assertRaises(SourcePilotArtifactError):
            validate_result({"result_schema_version": "wrong"})
        with self.assertRaises(SourcePilotArtifactError):
            validate_result(
                {
                    "result_schema_version": protocol_module.RESULT_SCHEMA_VERSION,
                    "protocol_id": protocol_module.PROTOCOL_ID,
                    "plan": plan_document(),
                    "outcome": "NOT AN OUTCOME",
                    "result_identity": "0" * 64,
                }
            )


class LeakageCounterTests(unittest.TestCase):
    """hidden-truth leakageは0固定のcounterにしない。"""

    def _leaking_game(self) -> GameMaterialization:
        return GameMaterialization(
            game_id="leaky",
            supported=False,
            unsupported_reason=(
                GameUnsupportedReason.SEAT_VISIBLE_EVENT_LEAKS_HIDDEN_TRUTH
            ),
            rounds=0,
            decision_opportunities=0,
            rows=(),
            forced_rows=0,
            unresolved_reasons=(),
        )

    def test_leakage_failures_count_the_games_that_leaked(self):
        source = _source([_synthetic_game("g1", 10), self._leaking_game()])
        self.assertEqual(source.report.leakage_failures, 1)
        self.assertEqual(source.report.to_document()["leakage_check_failures"], 1)
        self.assertFalse(source.report.gate_passed)

    def test_other_unsupported_reasons_are_not_counted_as_leakage(self):
        source = _source([_synthetic_game("g1", 0, supported=False)])
        self.assertEqual(source.report.games_unsupported, 1)
        self.assertEqual(source.report.leakage_failures, 0)
        self.assertFalse(source.report.gate_passed)

    def test_an_unmasked_seat_visible_event_reaches_the_counter(self):
        """detection pathからreport counterまでが繋がっていることを確認する。

        current RiichiEnvは他家のconcealed handとdrawを実際にmaskする。
        そのためmasking sentinelを別の値へ差し替え、`"?"`でmaskされた
        seat-visible eventをleakとして観測させる。確認対象はleakage判定の
        結線であり、engineのmasking動作そのものではない。
        """
        with mock.patch.object(materialization_module, "_MASKED_TILE", "!"):
            result = materialize(normal_discard_log(), game_id="leaky")
        self.assertFalse(result.supported)
        self.assertIs(
            result.unsupported_reason,
            GameUnsupportedReason.SEAT_VISIBLE_EVENT_LEAKS_HIDDEN_TRUTH,
        )
        source = _source([result])
        self.assertEqual(source.report.leakage_failures, 1)


class CounterConsistencyTests(unittest.TestCase):
    """decision opportunityはrows + forced + unresolvedで説明し切る。"""

    def test_unaccounted_decision_opportunities_fail_closed(self):
        game = _synthetic_game("g1", 4)
        with self.assertRaises(MaterializationError):
            GameMaterialization(
                game_id=game.game_id,
                supported=True,
                unsupported_reason=None,
                rounds=game.rounds,
                decision_opportunities=game.decision_opportunities + 1,
                rows=game.rows,
                forced_rows=0,
                unresolved_reasons=(),
            )

    def test_forced_and_unresolved_decisions_are_accounted(self):
        game = _synthetic_game("g1", 4)
        accounted = GameMaterialization(
            game_id=game.game_id,
            supported=True,
            unsupported_reason=None,
            rounds=game.rounds,
            decision_opportunities=6,
            rows=game.rows,
            forced_rows=1,
            unresolved_reasons=(
                (RowUnresolvedReason.DRAWN_TILE_SLOTS_RESTRICTED.value, 1),
            ),
        )
        self.assertEqual(accounted.unresolved_rows, 1)
        source = _source([accounted])
        document = source.report.to_document()
        self.assertEqual(
            document["decision_opportunities"],
            document["eligible_rows"] + document["forced_rows"] + 1,
        )

    def test_an_unsupported_game_must_not_carry_rows(self):
        game = _synthetic_game("g1", 4)
        with self.assertRaises(MaterializationError):
            GameMaterialization(
                game_id=game.game_id,
                supported=False,
                unsupported_reason=GameUnsupportedReason.MALFORMED_EVENT,
                rounds=0,
                decision_opportunities=0,
                rows=game.rows,
                forced_rows=0,
                unresolved_reasons=(),
            )

    def test_materialized_games_account_for_every_opportunity(self):
        events, _ = generated(246)
        result = materialize(events, game_id="seed-246")
        self.assertTrue(result.supported)
        self.assertEqual(
            result.decision_opportunities,
            len(result.rows) + result.forced_rows + result.unresolved_rows,
        )


BACKEND = "local-directory"
KEY = "riichilab-source-pilot-211/non-ml-test"
FOREIGN_CANDIDATE = "learned-source-pilot-r:" + "a" * 64
FOREIGN_BASELINE = "learned-source-pilot-y:" + "b" * 64


class TerminalOutcomeDurabilityTests(unittest.TestCase):
    """実行開始後のterminal outcomeは必ずwrite-once artifactとして残る。"""

    def _destination(self, directory: str) -> Path:
        return Path(directory) / "bundle"

    def _blocked_run(self, directory: str, source):
        return run_source_pilot(
            arm_y_source=None,
            arm_r_source=source,
            destination=self._destination(directory),
            backend=BACKEND,
            key=KEY,
        )

    def _sufficient_source(self):
        identity = "c" * 64
        if identity not in _SUFFICIENT_SOURCES:
            _SUFFICIENT_SOURCES[identity] = _source(
                _sufficient_games(), identity=identity
            )
        return _SUFFICIENT_SOURCES[identity]

    def test_gate0_failure_leaves_a_durable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            source = _source([_synthetic_game("bad", 0, supported=False)])
            run = self._blocked_run(directory, source)
            self.assertIs(
                run.outcome, SourcePilotOutcome.SOURCE_MATERIALIZATION_BLOCKED
            )
            persisted = load_result(run.path / RESULT_FILENAME)
            self.assertEqual(persisted, run.result)
            self.assertTrue(persisted["stop_reason"])
            self.assertEqual(
                verify_bundle(run.path)["outcome"],
                SourcePilotOutcome.SOURCE_MATERIALIZATION_BLOCKED.value,
            )

    def test_budget_failure_leaves_a_durable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            source = _source([_synthetic_game(f"g{index}", 10) for index in range(3)])
            run = self._blocked_run(directory, source)
            self.assertIs(run.outcome, SourcePilotOutcome.DATA_BUDGET_NOT_MATCHABLE)
            self.assertEqual(
                verify_bundle(run.path)["outcome"],
                SourcePilotOutcome.DATA_BUDGET_NOT_MATCHABLE.value,
            )

    def test_a_failure_after_the_gate_leaves_a_durable_stop_invalid(self):
        class _CorruptedArmYSource:
            @property
            def identity(self):
                raise RuntimeError("simulated retained artifact corruption")

        with tempfile.TemporaryDirectory() as directory:
            destination = self._destination(directory)
            with self.assertRaises(RuntimeError):
                run_source_pilot(
                    arm_y_source=_CorruptedArmYSource(),
                    arm_r_source=self._sufficient_source(),
                    destination=destination,
                    backend=BACKEND,
                    key=KEY,
                )
            result = load_result(destination / RESULT_FILENAME)
            self.assertEqual(result["outcome"], SourcePilotOutcome.STOP_INVALID.value)
            self.assertIn(
                "simulated retained artifact corruption", result["stop_reason"]
            )
            self.assertIsNone(result["strength"])
            document = verify_bundle(destination)
            self.assertEqual(document["outcome"], SourcePilotOutcome.STOP_INVALID.value)
            self.assertIn("partial STOP / INVALID bundle", document["verified"])

    def test_a_recorded_terminal_outcome_blocks_a_convenient_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            source = _source([_synthetic_game("bad", 0, supported=False)])
            self._blocked_run(directory, source)
            with self.assertRaises(FileExistsError):
                self._blocked_run(directory, source)

    def test_the_first_terminal_record_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = self._destination(directory)
            first = persist_stop_invalid(
                destination, backend=BACKEND, key=KEY, stop_reason="first"
            )
            second = persist_stop_invalid(
                destination, backend=BACKEND, key=KEY, stop_reason="second"
            )
            self.assertEqual(first, second)
            self.assertEqual(first["stop_reason"], "first")


class BundleCrossBindingTests(unittest.TestCase):
    """bundle全体のstrict readback（comparison前のterminal outcome）。"""

    def _blocked_bundle(self, directory: str) -> Path:
        destination = Path(directory) / "bundle"
        run_source_pilot(
            arm_y_source=None,
            arm_r_source=_source([_synthetic_game("bad", 0, supported=False)]),
            destination=destination,
            backend=BACKEND,
            key=KEY,
        )
        return destination

    def test_a_blocked_bundle_verifies_with_only_its_result(self):
        with tempfile.TemporaryDirectory() as directory:
            document = verify_bundle(self._blocked_bundle(directory))
            self.assertEqual(document["verified"], "result-only terminal outcome")
            self.assertTrue(document["stop_reason"])

    def test_an_unexpected_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._blocked_bundle(directory)
            (bundle / "notes.txt").write_text("operator note", encoding="utf-8")
            with self.assertRaises(SourcePilotArtifactError):
                verify_bundle(bundle)

    def test_a_blocked_bundle_must_not_retain_evaluation_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._blocked_bundle(directory)
            # 単体としてvalidなseed planでも、comparison前に停止した
            # bundleへ同梱されていればevidenceとして成立しない。
            plan = seed_plan_document(
                candidate_identity="learned-source-pilot-r:" + "a" * 64,
                baseline_identity="learned-source-pilot-y:" + "b" * 64,
            )
            (bundle / SEED_PLAN_FILENAME).write_text(
                canonical_json_text(plan), encoding="utf-8", newline="\n"
            )
            with self.assertRaises(SourcePilotArtifactError):
                verify_bundle(bundle)

    def test_a_missing_result_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.mkdir()
            with self.assertRaises(SourcePilotArtifactError):
                verify_bundle(empty)

    def _rehashed(self, bundle: Path, directory: str, mutate) -> Path:
        """resultをmutateし、result identityを再計算して別bundleへ保存する。"""
        result = load_result(bundle / RESULT_FILENAME)
        mutate(result)
        del result["result_identity"]
        result["result_identity"] = result_identity(result)
        reissued = Path(directory) / "reissued"
        reissued.mkdir()
        save_result(reissued / RESULT_FILENAME, result)
        return reissued

    def test_a_blocked_outcome_must_match_the_decision_order(self):
        """Gate 0 reportと矛盾するblocked outcomeを受け付けない。"""
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._blocked_bundle(directory)
            self.assertEqual(
                load_result(bundle / RESULT_FILENAME)["outcome"],
                SourcePilotOutcome.SOURCE_MATERIALIZATION_BLOCKED.value,
            )

            def _pass_the_gate(result):
                result["gate0"]["gate_passed"] = True

            reissued = self._rehashed(bundle, directory, _pass_the_gate)
            with self.assertRaises(SourcePilotArtifactError):
                verify_bundle(reissued)

    def test_a_budget_outcome_requires_a_passed_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bundle"
            run_source_pilot(
                arm_y_source=None,
                arm_r_source=_source(
                    [_synthetic_game(f"g{index}", 10) for index in range(3)]
                ),
                destination=destination,
                backend=BACKEND,
                key=KEY,
            )
            self.assertEqual(
                load_result(destination / RESULT_FILENAME)["outcome"],
                SourcePilotOutcome.DATA_BUDGET_NOT_MATCHABLE.value,
            )

            def _fail_the_gate(result):
                result["gate0"]["gate_passed"] = False

            reissued = self._rehashed(destination, directory, _fail_the_gate)
            with self.assertRaises(SourcePilotArtifactError):
                verify_bundle(reissued)

    def test_a_blocked_outcome_must_not_carry_post_gate_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._blocked_bundle(directory)

            def _claim_a_budget(result):
                result["budget"] = {"train_rows": 9116, "validation_rows": 2555}

            reissued = self._rehashed(bundle, directory, _claim_a_budget)
            with self.assertRaises(SourcePilotArtifactError):
                verify_bundle(reissued)

    def test_a_strength_artifact_without_a_seed_plan_fails_closed(self):
        """STOP bundleでも実行順序（seed plan -> strength artifact）を固定する。"""
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bundle"
            persist_stop_invalid(
                destination,
                backend=BACKEND,
                key=KEY,
                stop_reason="simulated interruption",
            )
            self.assertIn(
                "partial STOP / INVALID bundle", verify_bundle(destination)["verified"]
            )
            save_strength_artifact(
                destination / STRENGTH_ARTIFACT_FILENAME,
                FOREIGN_CANDIDATE,
                FOREIGN_BASELINE,
            )
            with self.assertRaises(SourcePilotArtifactError):
                verify_bundle(destination)

    def test_a_non_directory_bundle_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._blocked_bundle(directory)
            with self.assertRaises(SourcePilotArtifactError):
                verify_bundle(bundle / RESULT_FILENAME)


if __name__ == "__main__":
    unittest.main()
