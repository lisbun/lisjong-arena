"""Issue #211 RiichiLab source pilot — non-ML tests。

このtestは実#170 corpusを読まない。すべてsynthetic / public-format
fixtureとArena自身のlocal RiichiEnv実行だけを使う。
"""

import unittest

from _riichilab_source_pilot_fixtures import (
    chi_log,
    daiminkan_log,
    explicit_pass_log,
    future_divergent_logs,
    generated_game_log,
    hidden_variant_logs,
    kakan_log,
    multi_ron_log,
    normal_discard_log,
    pon_log,
    red_five_log,
    riichi_log,
    riichi_stick_log,
    ron_log,
    tile_counts,
    tsumo_win_log,
    unknown_event_log,
)
from lisjong.action_vocabulary import (
    build_legal_action_mask,
    decode_action,
    encode_action,
    resolve_legal_action,
)
from lisjong.policy_contract.decision_context import DecisionContext
from lisjong.policy_contract.seat import Seat
from riichienv import ActionType, RiichiEnv

from lisjong_arena.learned_policy_input import (
    build_policy_input_feature,
    tensor_values,
)
from lisjong_arena.riichilab_source_pilot import dataset as dataset_module
from lisjong_arena.riichilab_source_pilot import protocol as protocol_module
from lisjong_arena.riichilab_source_pilot.artifact import (
    seed_plan_document,
    validate_result,
    validate_seed_plan,
)
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

    def test_ambiguous_pass_after_a_competing_claim_stays_unsupported(self):
        result = materialize(ron_log())
        self.assertTrue(result.supported)
        self.assertIn(
            (RowUnresolvedReason.AMBIGUOUS_PASS_AFTER_COMPETING_CLAIM.value, 1),
            result.unresolved_reasons,
        )
        # ambiguousなseatのrowは作らない。
        self.assertNotIn(1, [row.actor_seat for row in result.rows])

    def test_unknown_event_type_fails_closed(self):
        result = materialize(unknown_event_log())
        self.assertFalse(result.supported)
        self.assertIs(
            result.unsupported_reason, GameUnsupportedReason.UNRECOGNIZED_EVENT_TYPE
        )
        self.assertEqual(result.rows, ())

    def test_chankan_ron_is_not_silently_skipped(self):
        """replay seamが槍槓response windowを提示しないことをfail closedで扱う。"""
        result = materialize(kakan_log(with_chankan_ron=True))
        self.assertFalse(result.supported)
        self.assertIs(
            result.unsupported_reason,
            GameUnsupportedReason.OBSERVED_ACTION_WITHOUT_DECISION_OPPORTUNITY,
        )
        self.assertEqual(result.rows, ())

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

    GENERATED_SEEDS = (245, 246)

    def test_generated_games_match_live_semantics_exactly(self):
        for seed in self.GENERATED_SEEDS:
            with self.subTest(seed=seed):
                events, live = generated(seed)
                result = materialize(events, game_id=f"seed-{seed}")
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


if __name__ == "__main__":
    unittest.main()
