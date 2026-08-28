"""Phase 2 anchor pipelineのleakage / correctness contract tests。

lisjong-arena #81が要求するdeterministic targeted testsを固定する。

- anchor-time freeze / future append invariance / label attachment immutability
- same-public / different-hidden equivalence
- expected-count validityとstructural-wait availabilityの非結合
- opponent row identityのrotation safety
- known structural wait / non-tenpai vs unavailable
- red-five truth preservation
- effective rule provenance
- player-safe型へのhidden metadata混入禁止 / downstream type boundary
"""

import ast
import copy
import inspect
import unittest
from dataclasses import fields

from _phase2_anchor_fixtures import (
    halt_at_turn_anchor,
    hand,
    honor,
    manzu,
    opponent_identity,
    pinzu,
    pon,
    rules_with,
    run_game_with_recorder,
    souzu,
)
from lisjong.belief import (
    SCALE,
    TILE_TYPE_COUNT,
    exact_hand_belief_with_waits,
    tile_type_from_index,
    tile_type_index,
    wind_for_seat,
)
from lisjong.policy_contract import Seat, Wind
from lisjong_engine.match_state import MatchState
from lisjong_engine.observation import ObservationDecisionKind
from lisjong_engine.player_state import PlayerState
from lisjong_engine.public_state import public_tile
from lisjong_engine.round_evidence_builder import build_round_evidence
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.lisjong_engine.domain_conversion import (
    seat_from_engine_seat,
    tile_from_public_tile,
)
from lisjong_arena.lisjong_engine.policy_input import build_policy_input
from lisjong_arena.phase2_training_anchor import player_safe_anchor as anchor_module
from lisjong_arena.phase2_training_anchor.extraction import (
    Phase2AnchorRecorder,
    extract_phase2_game,
)
from lisjong_arena.phase2_training_anchor.pipeline_provenance import (
    ANCHOR_SEMANTICS_ID,
    EVIDENCE_CUTOFF_SEMANTICS_ID,
    LABEL_SEMANTICS_ID,
    SourceRevisions,
    collect_pipeline_provenance,
)
from lisjong_arena.phase2_training_anchor.player_safe_anchor import (
    AnchorKind,
    AnchorSourceIdentity,
    FrozenPlayerSafeAnchor,
    freeze_player_safe_anchor,
)
from lisjong_arena.phase2_training_anchor.rule_provenance import (
    effective_rule_provenance,
    normalize_effective_rules,
)
from lisjong_arena.phase2_training_anchor.training_labels import (
    OPPONENT_COUNT,
    ExactTrainingLabels,
    LabelAnchorIdentity,
    StructuralWaitUnavailableReason,
    build_exact_training_labels,
    expected_counts_for_concealed_hand,
    structural_wait_for_hand,
)
from lisjong_arena.phase2_training_anchor.training_sample import (
    anchor_identity_of,
    compose_training_sample,
)


def _source_for(game_seed: int) -> AnchorSourceIdentity:
    """anchorのsource identity。game_seedはそのgameのseedと一致させる。"""
    return AnchorSourceIdentity(source_class="test", game_seed=game_seed)


_SOURCE = _source_for(101)


def _provenance(rules: RuleSet | None = None):
    return collect_pipeline_provenance(rules or RuleSet.default())


def _freeze_from(halted, anchor_index: int = 0) -> FrozenPlayerSafeAnchor:
    return freeze_player_safe_anchor(
        source=_source_for(halted.match_state.match_seed),
        observation=halted.observation,
        evidence=build_round_evidence(
            halted.round_state, halted.observation.viewer_seat
        ),
        round_revision=halted.round_state.revision,
        anchor_index=anchor_index,
        rule_provenance=effective_rule_provenance(halted.match_state.rules),
    )


class AnchorTimeFreezeTest(unittest.TestCase):
    """TURN callback時点でSeatObservationとordered evidenceをfreezeできる。"""

    def test_turn_anchor_freezes_observation_and_ordered_evidence(self):
        halted = halt_at_turn_anchor(101, 4)
        frozen = _freeze_from(halted, anchor_index=4)

        self.assertIs(frozen.anchor_kind, AnchorKind.TURN)
        self.assertIs(frozen.observation, halted.observation)
        self.assertIs(frozen.observation.decision_kind, ObservationDecisionKind.TURN)
        self.assertEqual(frozen.anchor_index, 4)
        self.assertEqual(frozen.round_revision, halted.round_state.revision)
        self.assertEqual(frozen.viewer_seat, halted.observation.viewer_seat)
        self.assertEqual(frozen.hand_number, halted.observation.hand_number)
        self.assertEqual(frozen.honba, halted.observation.honba)
        self.assertIsInstance(frozen.evidence, tuple)
        self.assertGreater(len(frozen.evidence), 0)

    def test_anchor_eligibility_uses_only_the_turn_decision_kind(self):
        halted = halt_at_turn_anchor(101, 0)
        recorder = Phase2AnchorRecorder(halted.match_state, _SOURCE)

        for kind in ObservationDecisionKind:
            if kind is ObservationDecisionKind.TURN:
                continue
            # reaction observationはdrawn tileを公開しないため、kindの
            # 差し替えに合わせてdrawn_tileも外す。
            recorder.observe(
                copy.replace(halted.observation, decision_kind=kind, drawn_tile=None)
            )

        # TURN以外のdecision kindはanchorにならない。
        self.assertEqual(recorder.turn_anchors, 0)
        self.assertEqual(recorder.samples, [])

        recorder.observe(halted.observation)
        self.assertEqual(recorder.turn_anchors, 1)
        self.assertEqual(len(recorder.samples), 1)


class FutureAppendInvarianceTest(unittest.TestCase):
    """anchor capture後にgameが進んでも、frozen anchorは変化しない。"""

    def test_frozen_anchor_is_unchanged_by_later_progression(self):
        captured: list[FrozenPlayerSafeAnchor] = []
        snapshots: list[FrozenPlayerSafeAnchor] = []

        match_state = MatchState(seed=137, rules=RuleSet.default())
        source = _source_for(137)
        recorder = Phase2AnchorRecorder(match_state, source)

        class _CapturingRecorder(Phase2AnchorRecorder):
            def observe(self, observation):
                before = len(self.samples)
                super().observe(observation)
                if len(self.samples) > before and len(captured) < 3:
                    frozen = self.samples[-1].anchor
                    captured.append(frozen)
                    # capture時点のvalueをdeep copyで独立に保存する。
                    snapshots.append(copy.deepcopy(frozen))

        recorder = _CapturingRecorder(match_state, source)
        run_game_with_recorder(match_state, recorder)

        self.assertEqual(len(captured), 3)
        self.assertGreater(recorder.turn_anchors, len(captured))
        for frozen, snapshot in zip(captured, snapshots, strict=True):
            self.assertEqual(frozen, snapshot)
            self.assertEqual(frozen.observation, snapshot.observation)
            self.assertEqual(frozen.evidence, snapshot.evidence)

    def test_evidence_prefix_grows_only_for_later_anchors(self):
        early = halt_at_turn_anchor(101, 1)
        late = halt_at_turn_anchor(101, 5)
        early_anchor = _freeze_from(early, 1)
        late_anchor = _freeze_from(late, 5)

        # 同一局・同一viewerのanchorであることを先に固定する。evidenceは
        # 局ごとに独立し、かつviewer-relative（自分のツモ牌だけを保持する）
        # であるため、別局・別viewerのanchorと比較しても意味がない。
        self.assertEqual(early_anchor.hand_number, late_anchor.hand_number)
        self.assertEqual(early_anchor.honba, late_anchor.honba)
        self.assertEqual(early_anchor.viewer_seat, late_anchor.viewer_seat)

        # 後のanchorはより長いevidence prefixを持ち、前のanchorは後から
        # 発生したeventを含まない。
        self.assertLess(len(early_anchor.evidence), len(late_anchor.evidence))
        self.assertLess(early_anchor.round_revision, late_anchor.round_revision)
        self.assertEqual(
            late_anchor.evidence[: len(early_anchor.evidence)],
            early_anchor.evidence,
        )

    def test_honba_discriminates_repeated_hands(self):
        first = _freeze_from(halt_at_turn_anchor(101, 8), 8)
        repeated = _freeze_from(halt_at_turn_anchor(101, 20), 20)

        # 同じhand_numberでもhonbaが違えば別の局である。anchor identityは
        # この繰り返し局をrepeated-hand discriminatorで区別できる。
        self.assertEqual(first.hand_number, repeated.hand_number)
        self.assertNotEqual(first.honba, repeated.honba)


class LabelAttachmentImmutabilityTest(unittest.TestCase):
    """label生成 / composition前後でplayer-safe anchorが同一である。"""

    def test_label_build_and_composition_do_not_change_the_anchor(self):
        halted = halt_at_turn_anchor(101, 3)
        frozen = _freeze_from(halted, 3)
        before = copy.deepcopy(frozen)

        labels = build_exact_training_labels(
            halted.match_state, halted.observation.viewer_seat
        )
        self.assertEqual(frozen, before)

        sample = compose_training_sample(frozen, labels, _provenance())
        self.assertEqual(frozen, before)
        self.assertIs(sample.anchor, frozen)
        self.assertEqual(sample.anchor, before)


class SamePublicDifferentHiddenTest(unittest.TestCase):
    """public / viewer-private観測が同一でhidden handだけ違えばanchorは同一。"""

    def test_swapping_opponent_hidden_hands_keeps_the_anchor_identical(self):
        halted = halt_at_turn_anchor(101, 6)
        round_state = halted.round_state
        viewer = halted.observation.viewer_seat

        before_anchor = _freeze_from(halted, 6)
        before_labels = build_exact_training_labels(halted.match_state, viewer)

        opponents = [seat for seat in EngineSeat if seat is not viewer]
        first, second = opponents[0], opponents[1]

        # hidden truthだけを入れ替える。discards / melds / riichi / scoreは
        # そのまま維持するため、player-safe projectionは変化しないはずである。
        # engine内部stateへ直接触れるのは、public stateを固定したままhidden
        # stateだけを変える fixture が他に作れないためである。
        players = round_state._players  # noqa: SLF001
        first_hand = tuple(players[first].hand_tiles)
        second_hand = tuple(players[second].hand_tiles)
        self.assertNotEqual(first_hand, second_hand)

        def _swapped(seat, tiles):
            original = players[seat]
            return PlayerState(
                seat,
                tiles,
                original.discards,
                original.melds,
                riichi_status=original.riichi_status,
            )

        players[first] = _swapped(first, second_hand)
        players[second] = _swapped(second, first_hand)

        after_anchor = _freeze_from(halted, 6)
        after_labels = build_exact_training_labels(halted.match_state, viewer)

        # player-safe anchorは同一。
        self.assertEqual(before_anchor, after_anchor)
        self.assertEqual(before_anchor.observation, after_anchor.observation)
        self.assertEqual(before_anchor.evidence, after_anchor.evidence)

        # omniscient labelsは異なってよい。
        self.assertNotEqual(before_labels, after_labels)


class ExpectedCountIndependenceTest(unittest.TestCase):
    """expected-count validityはstructural-wait availabilityへcoupleしない。"""

    def test_unstable_hand_still_produces_expected_counts(self):
        identity = opponent_identity()
        # 12枚のtransient hand。stable 13-equivalent条件を満たさない。
        concealed = hand(
            manzu(1),
            manzu(2),
            manzu(3),
            pinzu(4),
            pinzu(5),
            pinzu(6),
            souzu(7),
            souzu(8),
            souzu(9),
            honor(1),
            honor(1),
            honor(2),
        )

        counts = expected_counts_for_concealed_hand(identity, concealed)
        wait = structural_wait_for_hand(identity, concealed, ())

        # expected-countは生成できる。
        self.assertEqual(counts.concealed_size, 12)
        self.assertEqual(sum(counts.counts), 12)
        self.assertEqual(counts.counts[tile_type_index(honor(1).tile_type)], 2)

        # waitだけがunavailable。
        self.assertFalse(wait.is_available)
        self.assertIsNone(wait.mask)
        self.assertIs(
            wait.unavailable_reason,
            StructuralWaitUnavailableReason.UNSTABLE_HAND_SIZE,
        )

    def test_public_meld_tiles_are_excluded_from_the_concealed_target(self):
        identity = opponent_identity()
        # concealed 10枚 + pon 1面子 = structural 13-equivalent。
        concealed = hand(
            manzu(2),
            manzu(3),
            manzu(4),
            pinzu(2),
            pinzu(3),
            pinzu(4),
            honor(5),
            honor(5),
            souzu(3),
            souzu(4),
        )
        melds = (pon(souzu(9)),)

        counts = expected_counts_for_concealed_hand(identity, concealed)
        wait = structural_wait_for_hand(identity, concealed, melds)

        # meld牌はconcealed countへ入らない。
        self.assertEqual(counts.concealed_size, 10)
        self.assertEqual(sum(counts.counts), 10)
        self.assertEqual(counts.counts[tile_type_index(souzu(9).tile_type)], 0)

        # 一方でmeldはstructural wait側では3-equivalentとして数えられる。
        self.assertTrue(wait.is_available)

    def test_one_unavailable_wait_does_not_drop_other_targets(self):
        stable = hand(
            *([manzu(1)] * 3),
            *([manzu(4)] * 3),
            *([pinzu(2)] * 3),
            *([souzu(6)] * 3),
            honor(1),
        )
        unstable = stable[:-1]
        identity = opponent_identity()

        self.assertTrue(structural_wait_for_hand(identity, stable, ()).is_available)
        self.assertFalse(structural_wait_for_hand(identity, unstable, ()).is_available)
        # どちらもexpected-countは生成される。
        self.assertEqual(
            expected_counts_for_concealed_hand(identity, unstable).concealed_size, 12
        )
        self.assertEqual(
            expected_counts_for_concealed_hand(identity, stable).concealed_size, 13
        )

    def test_extraction_never_silently_drops_an_anchor(self):
        extraction = extract_phase2_game(103)
        self.assertGreater(extraction.turn_anchors, 0)
        self.assertEqual(len(extraction.samples), extraction.turn_anchors)
        for sample in extraction.samples:
            self.assertEqual(len(sample.labels.expected_counts), OPPONENT_COUNT)
            self.assertEqual(len(sample.labels.structural_waits), OPPONENT_COUNT)


class OpponentIdentityRotationTest(unittest.TestCase):
    """dealer / viewer rotationでtarget opponent row identityがずれない。"""

    def test_row_identity_tracks_the_actual_seat_across_rotation(self):
        match_state = MatchState(seed=151, rules=RuleSet.default())
        source = _source_for(151)
        seen_dealers = set()
        seen_viewers = set()
        checked = 0

        class _VerifyingRecorder(Phase2AnchorRecorder):
            def observe(inner, observation):  # noqa: N805
                nonlocal checked
                before = len(inner.samples)
                super().observe(observation)
                if len(inner.samples) == before:
                    return
                round_state = match_state.active_round
                labels = inner.samples[-1].labels
                dealer_seat = seat_from_engine_seat(round_state.dealer_seat)
                seen_dealers.add(round_state.dealer_seat)
                seen_viewers.add(observation.viewer_seat)

                for row in labels.expected_counts:
                    # identityが指すseatの実concealed handを直接数え直し、
                    # そのrowのcountsと一致することを照合する。
                    engine_seat = next(
                        seat
                        for seat in EngineSeat
                        if seat_from_engine_seat(seat) == row.identity.seat
                    )
                    expected = [0] * TILE_TYPE_COUNT
                    for engine_tile in round_state.hand_tiles(engine_seat):
                        tile = tile_from_public_tile(public_tile(engine_tile))
                        expected[tile_type_index(tile.tile_type)] += 1

                    assert row.counts == tuple(expected), (
                        "opponent row identity does not match the counted hand"
                    )
                    assert row.identity.wind is wind_for_seat(
                        row.identity.seat, dealer_seat
                    )
                    assert row.identity.seat != labels.viewer_seat
                    checked += 1

        recorder = _VerifyingRecorder(match_state, source)
        run_game_with_recorder(match_state, recorder)

        self.assertGreater(checked, 100)
        self.assertEqual(seen_viewers, set(EngineSeat))
        self.assertGreater(len(seen_dealers), 1, "the fixture must rotate the dealer")

    def test_viewer_is_never_a_target_opponent(self):
        halted = halt_at_turn_anchor(101, 8)
        labels = build_exact_training_labels(
            halted.match_state, halted.observation.viewer_seat
        )
        viewer = seat_from_engine_seat(halted.observation.viewer_seat)
        self.assertEqual(labels.viewer_seat, viewer)
        self.assertNotIn(viewer, {row.identity.seat for row in labels.expected_counts})
        self.assertEqual(
            {row.identity.viewer_relative_offset for row in labels.expected_counts},
            {1, 2, 3},
        )


class StructuralWaitTest(unittest.TestCase):
    """structural wait maskがexisting exact builderと一致する。"""

    def test_known_ryanmen_wait_matches_the_exact_builder(self):
        identity = opponent_identity()
        # 234m 234p 234s 55z + 34m ryanmen -> 2m / 5m 待ち。
        concealed = hand(
            manzu(2),
            manzu(3),
            manzu(4),
            pinzu(2),
            pinzu(3),
            pinzu(4),
            souzu(2),
            souzu(3),
            souzu(4),
            honor(5),
            honor(5),
            manzu(3),
            manzu(4),
        )
        row = structural_wait_for_hand(identity, concealed, ())
        self.assertTrue(row.is_available)

        reference = exact_hand_belief_with_waits(concealed, ())
        expected = tuple(
            1 if reference.wait_probability_raw[index] == SCALE else 0
            for index in range(TILE_TYPE_COUNT)
        )
        self.assertEqual(row.mask, expected)

        waiting = {index for index, value in enumerate(row.mask) if value == 1}
        self.assertEqual(
            waiting,
            {
                tile_type_index(manzu(2).tile_type),
                tile_type_index(manzu(5).tile_type),
            },
        )

    def test_melded_hand_uses_existing_own_melds_semantics(self):
        identity = opponent_identity()
        # pon 1面子 + concealed 10枚 = structural 13-equivalent。
        concealed = hand(
            manzu(2),
            manzu(3),
            manzu(4),
            pinzu(2),
            pinzu(3),
            pinzu(4),
            honor(5),
            honor(5),
            souzu(3),
            souzu(4),
        )
        melds = (pon(souzu(9)),)
        row = structural_wait_for_hand(identity, concealed, melds)
        self.assertTrue(row.is_available)

        reference = exact_hand_belief_with_waits(concealed, melds)
        self.assertEqual(
            row.mask,
            tuple(
                1 if reference.wait_probability_raw[index] == SCALE else 0
                for index in range(TILE_TYPE_COUNT)
            ),
        )
        self.assertEqual(sum(row.mask), 2)

    def test_non_tenpai_all_zero_mask_is_distinct_from_unavailable(self):
        identity = opponent_identity()
        non_tenpai = hand(
            manzu(1),
            manzu(4),
            manzu(7),
            pinzu(1),
            pinzu(4),
            pinzu(7),
            souzu(1),
            souzu(4),
            souzu(7),
            honor(1),
            honor(3),
            honor(5),
            honor(7),
        )
        available = structural_wait_for_hand(identity, non_tenpai, ())
        self.assertTrue(available.is_available)
        self.assertEqual(available.mask, tuple([0] * TILE_TYPE_COUNT))
        self.assertIsNone(available.unavailable_reason)

        unavailable = structural_wait_for_hand(identity, non_tenpai[:-1], ())
        self.assertFalse(unavailable.is_available)
        self.assertIsNone(unavailable.mask)
        self.assertIsNotNone(unavailable.unavailable_reason)

        # all-zero validなmaskと、maskなしのunavailableは同じ値にならない。
        self.assertNotEqual(available, unavailable)


class RedFivePreservationTest(unittest.TestCase):
    """34-axisではred/normalを合算しつつ、red truthはlabel側で保持する。"""

    def test_red_five_is_aggregated_in_counts_but_preserved_separately(self):
        identity = opponent_identity()
        red_hand = hand(
            pinzu(5, is_red=True),
            pinzu(5),
            manzu(1),
            manzu(2),
            manzu(3),
            souzu(1),
            souzu(2),
            souzu(3),
            honor(1),
            honor(1),
            honor(2),
            honor(3),
            honor(4),
        )
        normal_hand = hand(
            pinzu(5),
            pinzu(5),
            manzu(1),
            manzu(2),
            manzu(3),
            souzu(1),
            souzu(2),
            souzu(3),
            honor(1),
            honor(1),
            honor(2),
            honor(3),
            honor(4),
        )

        red_row = expected_counts_for_concealed_hand(identity, red_hand)
        normal_row = expected_counts_for_concealed_hand(identity, normal_hand)

        # 34-axisでは赤5と通常5を合算するため、countsは区別できない。
        self.assertEqual(red_row.counts, normal_row.counts)
        self.assertEqual(red_row.counts[tile_type_index(pinzu(5).tile_type)], 2)

        # red truthはtraining-only side で保持される。
        self.assertEqual(red_row.red_five_present, (False, True, False))
        self.assertEqual(normal_row.red_five_present, (False, False, False))
        self.assertNotEqual(red_row, normal_row)

    def test_red_five_truth_survives_real_execution(self):
        extraction = extract_phase2_game(101)
        self.assertTrue(
            any(
                any(row.red_five_present)
                for sample in extraction.samples
                for row in sample.labels.expected_counts
            ),
            "the fixture game must contain at least one concealed red five",
        )


class EffectiveRuleProvenanceTest(unittest.TestCase):
    """name/version以上のeffective mechanics configurationをbindingする。"""

    def test_identical_rules_produce_identical_provenance(self):
        first = effective_rule_provenance(RuleSet.default())
        second = effective_rule_provenance(RuleSet.default())
        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)

    def test_same_name_and_version_but_changed_mechanics_changes_fingerprint(self):
        default = RuleSet.default()
        variant = rules_with(
            kokushi_ankan_chankan_enabled=(not default.kokushi_ankan_chankan_enabled)
        )

        self.assertEqual(variant.name, default.name)
        self.assertEqual(variant.version, default.version)

        base = effective_rule_provenance(default)
        changed = effective_rule_provenance(variant)
        self.assertEqual(base.name, changed.name)
        self.assertEqual(base.version, changed.version)
        self.assertNotEqual(base.fingerprint, changed.fingerprint)

    def test_each_effective_field_change_is_detected(self):
        default = RuleSet.default()
        base = effective_rule_provenance(default).fingerprint
        variants = (
            rules_with(
                riichi_minimum_live_wall_tiles=(
                    default.riichi_minimum_live_wall_tiles + 1
                )
            ),
            rules_with(
                double_wind_pair_fu=(2 if default.double_wind_pair_fu == 4 else 4)
            ),
            rules_with(nagashi_mangan_enabled=(not default.nagashi_mangan_enabled)),
            rules_with(starting_points=default.starting_points + 1000),
            rules_with(uma=(20, 10, -10, -20)),
        )
        fingerprints = {effective_rule_provenance(v).fingerprint for v in variants}
        self.assertEqual(len(fingerprints), len(variants))
        self.assertNotIn(base, fingerprints)

    def test_normalized_representation_is_deterministic(self):
        self.assertEqual(
            normalize_effective_rules(RuleSet.default()),
            normalize_effective_rules(RuleSet.default()),
        )

    def test_frozenset_fields_normalize_deterministically(self):
        # pao_yaku等のfrozenset fieldは反復順序が保証されないため、
        # 同じ内容なら常に同じ正規化表現になることを固定する。
        rules = RuleSet.default()
        self.assertEqual(
            normalize_effective_rules(rules),
            normalize_effective_rules(rules_with(pao_yaku=frozenset(rules.pao_yaku))),
        )


class NoHiddenMetadataInPlayerSafeInputTest(unittest.TestCase):
    """player-safe anchor型にlabel availability / reason / hidden truthがない。"""

    def test_frozen_anchor_fields_are_an_explicit_player_safe_allowlist(self):
        self.assertEqual(
            {field.name for field in fields(FrozenPlayerSafeAnchor)},
            {
                "source",
                "hand_number",
                "honba",
                "round_revision",
                "viewer_seat",
                "anchor_kind",
                "anchor_index",
                "observation",
                "evidence",
                "rule_provenance",
            },
        )

    def test_no_availability_or_hidden_truth_field_names(self):
        forbidden = (
            "label",
            "available",
            "availability",
            "reason",
            "exclusion",
            "hidden",
            "concealed",
            "wait",
            "expected_count",
            "red_five",
        )
        for field in fields(FrozenPlayerSafeAnchor):
            for token in forbidden:
                self.assertNotIn(
                    token,
                    field.name,
                    f"{field.name} looks like training-only metadata",
                )

    def test_player_safe_module_does_not_import_the_label_path(self):
        # docstringはtrusted declassifier chainの説明でこれらの名前へ言及
        # するため、raw textではなく実際のimport一覧を検証する。
        tree = ast.parse(inspect.getsource(anchor_module))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)

        for forbidden in (
            "training_labels",
            "ExactTrainingLabels",
            "MatchState",
            "RoundState",
            "lisjong_engine.match_state",
            "lisjong_engine.round_state",
        ):
            self.assertNotIn(
                forbidden,
                imported,
                f"{forbidden} must not be imported by the player-safe anchor module",
            )

    def test_player_safe_module_namespace_exposes_no_omniscient_type(self):
        for forbidden in ("MatchState", "RoundState", "ExactTrainingLabels"):
            self.assertFalse(
                hasattr(anchor_module, forbidden),
                f"{forbidden} must not be reachable from the player-safe module",
            )

    def test_label_availability_lives_only_on_the_training_only_type(self):
        halted = halt_at_turn_anchor(101, 2)
        labels = build_exact_training_labels(
            halted.match_state, halted.observation.viewer_seat
        )
        self.assertIsInstance(labels, ExactTrainingLabels)
        self.assertTrue(
            all(hasattr(row, "unavailable_reason") for row in labels.structural_waits)
        )
        frozen = _freeze_from(halted, 2)
        self.assertFalse(hasattr(frozen, "unavailable_reason"))
        self.assertFalse(hasattr(frozen, "labels"))


class DownstreamTypeBoundaryTest(unittest.TestCase):
    """player-safe feature/input helperがfull MatchState / RoundStateを取らない。"""

    def _annotation_names(self, func) -> set[str]:
        signature = inspect.signature(func)
        names = set()
        for parameter in signature.parameters.values():
            annotation = parameter.annotation
            names.add(
                annotation
                if isinstance(annotation, str)
                else getattr(annotation, "__name__", str(annotation))
            )
        return names

    def test_player_safe_builders_do_not_accept_omniscient_state(self):
        for func in (freeze_player_safe_anchor, build_policy_input):
            names = self._annotation_names(func)
            self.assertNotIn("MatchState", names)
            self.assertNotIn("RoundState", names)

    def test_freeze_takes_only_declassified_player_safe_values(self):
        parameters = set(inspect.signature(freeze_player_safe_anchor).parameters)
        self.assertEqual(
            parameters,
            {
                "source",
                "observation",
                "evidence",
                "round_revision",
                "anchor_index",
                "rule_provenance",
            },
        )

    def test_only_the_label_path_accepts_omniscient_state(self):
        names = self._annotation_names(build_exact_training_labels)
        self.assertIn("MatchState", names)


class SampleCompositionTest(unittest.TestCase):
    """compositionはmisaligned anchor / labelをfail closedする。"""

    def test_mismatched_viewer_is_rejected(self):
        halted = halt_at_turn_anchor(101, 5)
        frozen = _freeze_from(halted, 5)
        other_viewer = next(
            seat for seat in EngineSeat if seat is not halted.observation.viewer_seat
        )
        mismatched = build_exact_training_labels(halted.match_state, other_viewer)
        with self.assertRaises(ValueError):
            compose_training_sample(frozen, mismatched, _provenance())

    def test_same_viewer_and_round_but_different_revision_is_rejected(self):
        """same viewer / same round でもstate positionが違えばcomposeできない。

        viewer一致だけを検証していると、同じgame・同じ局・同じviewerの
        後続state positionのlabelsをearly anchorへ貼り付けられてしまう。
        これはpre / post action混同とoff-by-one alignment errorの本体である。
        """
        early = halt_at_turn_anchor(101, 1)
        late = halt_at_turn_anchor(101, 5)

        early_anchor = _freeze_from(early, 1)
        late_labels = build_exact_training_labels(
            late.match_state, late.observation.viewer_seat
        )

        # fixtureがsame viewer / same round / different revisionであることを固定する。
        self.assertEqual(early.observation.viewer_seat, late.observation.viewer_seat)
        self.assertEqual(early.observation.hand_number, late.observation.hand_number)
        self.assertEqual(early.observation.honba, late.observation.honba)
        self.assertNotEqual(early.round_state.revision, late.round_state.revision)

        with self.assertRaises(ValueError):
            compose_training_sample(early_anchor, late_labels, _provenance())

    def test_different_game_seed_with_identical_position_is_rejected(self):
        """position identifierが全一致でも、別gameのlabelsはcomposeできない。

        東1局最初のTURNは、seedが違っても`hand_number` / `honba` /
        `round_revision` / `viewer_seat` / `dealer_seat` / `prevailing_wind`が
        すべて一致する。position識別子だけではgameを跨いだ取り違えを検出
        できないため、game / match identityもalignmentへ含める。
        """
        first = halt_at_turn_anchor(101, 0)
        other = halt_at_turn_anchor(102, 0)

        # fixtureがposition identifier全一致であることを先に固定する。
        self.assertNotEqual(first.match_state.match_seed, other.match_state.match_seed)
        for attribute in (
            "hand_number",
            "honba",
            "viewer_seat",
            "dealer_seat",
            "prevailing_wind",
        ):
            self.assertEqual(
                getattr(first.observation, attribute),
                getattr(other.observation, attribute),
                f"fixture must share {attribute}",
            )
        self.assertEqual(first.round_state.revision, other.round_state.revision)

        first_anchor = _freeze_from(first, 0)
        other_labels = build_exact_training_labels(
            other.match_state, other.observation.viewer_seat
        )
        # hidden truthは当然異なる。
        self.assertNotEqual(
            other_labels.expected_counts,
            build_exact_training_labels(
                first.match_state, first.observation.viewer_seat
            ).expected_counts,
        )

        with self.assertRaises(ValueError):
            compose_training_sample(first_anchor, other_labels, _provenance())

    def test_label_game_identity_comes_from_the_engine_match_state(self):
        """label側のgame identityはanchorではなくengine authorityから来る。"""
        halted = halt_at_turn_anchor(102, 0)
        labels = build_exact_training_labels(
            halted.match_state, halted.observation.viewer_seat
        )
        self.assertEqual(
            labels.anchor_identity.game_seed, halted.match_state.match_seed
        )

    def test_same_hand_number_but_different_honba_is_rejected(self):
        """繰り返し局は`round_revision`が衝突し得るためhonbaでも区別する。"""
        first = halt_at_turn_anchor(101, 1)
        repeated = halt_at_turn_anchor(101, 20)

        self.assertEqual(
            first.observation.hand_number, repeated.observation.hand_number
        )
        self.assertNotEqual(first.observation.honba, repeated.observation.honba)

        first_anchor = _freeze_from(first, 1)
        repeated_labels = build_exact_training_labels(
            repeated.match_state, repeated.observation.viewer_seat
        )
        with self.assertRaises(ValueError):
            compose_training_sample(first_anchor, repeated_labels, _provenance())

    def test_label_identity_is_not_copied_from_the_anchor(self):
        """labelのanchor identityはprivileged stateから独立に導出される。"""
        halted = halt_at_turn_anchor(101, 5)
        labels = build_exact_training_labels(
            halted.match_state, halted.observation.viewer_seat
        )
        # anchor objectを一切渡していないbuilderが、anchor identityを持つ。
        self.assertEqual(
            labels.anchor_identity.round_revision, halted.round_state.revision
        )
        self.assertEqual(
            labels.anchor_identity.hand_number, halted.observation.hand_number
        )
        self.assertEqual(labels.anchor_identity.honba, halted.observation.honba)

        # そのidentityは、anchor側から独立に構成した期待値と一致する。
        frozen = _freeze_from(halted, 5)
        self.assertEqual(labels.anchor_identity, anchor_identity_of(frozen))

    def test_mismatched_rule_provenance_is_rejected(self):
        halted = halt_at_turn_anchor(101, 5)
        frozen = _freeze_from(halted, 5)
        labels = build_exact_training_labels(
            halted.match_state, halted.observation.viewer_seat
        )
        other_rules = rules_with(
            nagashi_mangan_enabled=(not RuleSet.default().nagashi_mangan_enabled)
        )
        with self.assertRaises(ValueError):
            compose_training_sample(
                frozen, labels, collect_pipeline_provenance(other_rules)
            )

    def test_matching_anchor_and_labels_compose(self):
        halted = halt_at_turn_anchor(101, 5)
        frozen = _freeze_from(halted, 5)
        labels = build_exact_training_labels(
            halted.match_state, halted.observation.viewer_seat
        )
        provenance = _provenance()
        sample = compose_training_sample(frozen, labels, provenance)
        self.assertIs(sample.anchor, frozen)
        self.assertIs(sample.labels, labels)
        self.assertIs(sample.provenance, provenance)
        self.assertEqual(
            sample.labels.viewer_seat, seat_from_engine_seat(frozen.viewer_seat)
        )


class PipelineProvenanceTest(unittest.TestCase):
    """#24が要求するsource / anchor / cutoff / label semantics provenance。"""

    def test_binds_semantics_identities_and_effective_rules(self):
        provenance = collect_pipeline_provenance(RuleSet.default())

        self.assertEqual(provenance.anchor_semantics_id, ANCHOR_SEMANTICS_ID)
        self.assertEqual(
            provenance.evidence_cutoff_semantics_id, EVIDENCE_CUTOFF_SEMANTICS_ID
        )
        self.assertEqual(provenance.label_semantics_id, LABEL_SEMANTICS_ID)
        self.assertEqual(
            provenance.effective_rules,
            effective_rule_provenance(RuleSet.default()),
        )

    def test_semantics_identities_are_independent_of_effective_rules(self):
        """rules差はfingerprintへ、semantics差はsemantics idへ現れる。"""
        default = collect_pipeline_provenance(RuleSet.default())
        variant = collect_pipeline_provenance(
            rules_with(
                kokushi_ankan_chankan_enabled=(
                    not RuleSet.default().kokushi_ankan_chankan_enabled
                )
            )
        )
        self.assertNotEqual(
            default.effective_rules.fingerprint, variant.effective_rules.fingerprint
        )
        self.assertEqual(default.label_semantics_id, variant.label_semantics_id)
        self.assertEqual(default.anchor_semantics_id, variant.anchor_semantics_id)

    def test_source_revisions_are_never_fabricated(self):
        revisions = collect_pipeline_provenance(RuleSet.default()).source_revisions
        for value in (
            revisions.lisjong,
            revisions.lisjong_engine,
            revisions.lisjong_arena,
        ):
            # 解決できた場合はfull commit ID、できない場合はNone。
            # 「それらしい」placeholderを作らない。
            if value is not None:
                self.assertRegex(value, r"\A[0-9a-f]{40}\Z")
        self.assertEqual(
            revisions.fully_resolved,
            None
            not in (
                revisions.lisjong,
                revisions.lisjong_engine,
                revisions.lisjong_arena,
            ),
        )

    def test_rejects_a_non_commit_revision(self):
        with self.assertRaises(ValueError):
            SourceRevisions(
                lisjong="not-a-commit", lisjong_engine=None, lisjong_arena=None
            )

    def test_every_sample_carries_pipeline_provenance(self):
        extraction = extract_phase2_game(101)
        first = extraction.samples[0].provenance
        for sample in extraction.samples:
            self.assertEqual(sample.provenance, first)
            self.assertEqual(
                sample.provenance.effective_rules, sample.anchor.rule_provenance
            )


class LabelValidationTest(unittest.TestCase):
    """label側のfail closed validation。"""

    def test_anchor_identity_rejects_invalid_positions(self):
        valid = {
            "game_seed": 101,
            "hand_number": 1,
            "honba": 0,
            "round_revision": 0,
            "viewer_seat": Seat.SEAT_0,
            "dealer_seat": Seat.SEAT_0,
            "prevailing_wind": Wind.EAST,
        }
        LabelAnchorIdentity(**valid)
        for field_name, bad in (
            ("hand_number", 0),
            ("honba", -1),
            ("round_revision", -1),
        ):
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    LabelAnchorIdentity(**{**valid, field_name: bad})
        with self.assertRaises(TypeError):
            LabelAnchorIdentity(**{**valid, "game_seed": "101"})

    def test_anchor_identity_distinguishes_games_at_an_identical_position(self):
        """position全一致でもgame_seedが違えばidentityは一致しない。"""
        base = {
            "game_seed": 101,
            "hand_number": 1,
            "honba": 0,
            "round_revision": 2,
            "viewer_seat": Seat.SEAT_0,
            "dealer_seat": Seat.SEAT_0,
            "prevailing_wind": Wind.EAST,
        }
        self.assertNotEqual(
            LabelAnchorIdentity(**base),
            LabelAnchorIdentity(**{**base, "game_seed": 102}),
        )

    def test_counts_row_rejects_a_wrong_length(self):
        from lisjong_arena.phase2_training_anchor.training_labels import (
            OpponentExpectedCounts,
        )

        with self.assertRaises(ValueError):
            OpponentExpectedCounts(
                identity=opponent_identity(),
                counts=(0,) * (TILE_TYPE_COUNT - 1),
                red_five_present=(False, False, False),
                concealed_size=0,
            )

    def test_wait_row_rejects_both_mask_and_reason(self):
        from lisjong_arena.phase2_training_anchor.training_labels import (
            OpponentStructuralWait,
        )

        with self.assertRaises(ValueError):
            OpponentStructuralWait(
                identity=opponent_identity(),
                mask=(0,) * TILE_TYPE_COUNT,
                unavailable_reason=(StructuralWaitUnavailableReason.UNSTABLE_HAND_SIZE),
            )
        with self.assertRaises(ValueError):
            OpponentStructuralWait(
                identity=opponent_identity(), mask=None, unavailable_reason=None
            )

    def test_expected_count_lookup_uses_logical_wind_identity(self):
        halted = halt_at_turn_anchor(101, 7)
        labels = build_exact_training_labels(
            halted.match_state, halted.observation.viewer_seat
        )
        row = labels.expected_counts[0]
        for index in range(TILE_TYPE_COUNT):
            self.assertEqual(
                labels.expected_count(row.identity.wind, tile_type_from_index(index)),
                row.counts[index],
            )
        viewer_wind = wind_for_seat(
            labels.viewer_seat, seat_from_engine_seat(halted.round_state.dealer_seat)
        )
        with self.assertRaises(KeyError):
            labels.expected_count(viewer_wind, manzu(1).tile_type)

    def test_anchor_rejects_a_non_turn_observation(self):
        halted = halt_at_turn_anchor(101, 1)
        with self.assertRaises(ValueError):
            FrozenPlayerSafeAnchor(
                source=_SOURCE,
                hand_number=1,
                honba=0,
                round_revision=0,
                viewer_seat=halted.observation.viewer_seat,
                anchor_kind=AnchorKind.TURN,
                anchor_index=0,
                observation=copy.replace(
                    halted.observation,
                    decision_kind=ObservationDecisionKind.DISCARD_REACTION,
                ),
                evidence=(),
                rule_provenance=effective_rule_provenance(RuleSet.default()),
            )


class RonLegalAuxiliaryDeferralTest(unittest.TestCase):
    """ron-legal auxiliaryをdeferした理由がcode上に残っている。"""

    def test_deferral_rationale_is_documented_in_the_label_module(self):
        from lisjong_arena.phase2_training_anchor import training_labels

        source = inspect.getsource(training_labels)
        self.assertIn("ron_legal_wait", source)
        self.assertIn("WinOrigin", source)
        self.assertIn("furiten", source)
        # context-freeな34-vectorを実装していないこと。
        self.assertNotIn("def build_ron_legal", source)


if __name__ == "__main__":
    unittest.main()
