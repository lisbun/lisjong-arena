"""`lisbun/lisjong-arena #258` Stage A0 Tenpai label path feasibilityのtest。

`lisjong.belief.exact_wait_ground_truth`のsemantic suiteはArenaへ複製しない。
ここで固定するのはArena integration pathだけである。

- same-state seam（public row state == privileged label state）
- relative slot -> canonical seat -> hidden truthのbinding
- target availability reason code contract（unavailable != negative label）
- public / private leakage boundary
- retained augmentation qualificationのfail-closed境界
- sidecarのstrict readbackとdeterministic recomputation
"""

import hashlib
import json
import unittest
from array import array
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _stage_a0_tenpai_fixtures import (
    FIXTURE_PROVENANCE,
    FOURTEEN_EQUIVALENT_HAND,
    INVALID_INVENTORY_HAND,
    NON_TENPAI_HAND,
    OPEN_TENPAI_CONCEALED,
    RETAINED_PROVENANCE,
    TENPAI_HAND,
    ankan_meld,
    build_retained_dataset,
    hidden_state_for,
    observed_decision,
    policy_input_for,
    pon_meld,
    seat_plan,
    tiles,
    uniform_plan,
)
from lisjong.action_vocabulary import build_legal_action_mask, encode_action
from lisjong.belief import exact_hand_belief_with_waits
from lisjong.policy_contract import DecisionContext, Seat, Wind
from lisjong.policy_contract.action import DiscardAction
from lisjong.policy_contract.riichi import RiichiState

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_input import (
    build_policy_input_feature,
    tensor_values,
)
from lisjong_arena.learned_policy_offline_q import protocol as offline_q
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATASET_TEST_SEEDS,
    DATASET_VALIDATION_SEEDS,
)
from lisjong_arena.phase2_training_anchor import structural_wait
from lisjong_arena.stage_a0_tenpai_feasibility import labels, protocol, public_row
from lisjong_arena.stage_a0_tenpai_feasibility.emission import (
    ObservedDecision,
    emit_decision,
)
from lisjong_arena.stage_a0_tenpai_feasibility.errors import (
    StageA0AlignmentError,
    StageA0ProtocolError,
    StageA0ReportError,
    StageA0SidecarError,
)
from lisjong_arena.stage_a0_tenpai_feasibility.fresh import (
    FRESH_LIVE_LABEL_PATH_QUALIFIED,
    qualify_fresh_live_label,
)
from lisjong_arena.stage_a0_tenpai_feasibility.labels import TargetAvailability
from lisjong_arena.stage_a0_tenpai_feasibility.protocol import RETAINED_DATASET_IDENTITY
from lisjong_arena.stage_a0_tenpai_feasibility.report import (
    REPORT_SCHEMA_VERSION,
    RETAINED_AUGMENTATION_NOT_QUALIFIED,
    STOP_INVALID,
    TENPAI_LABEL_PATH_BLOCKED,
    FeasibilityReport,
    LoadedFeasibilityReport,
    QualificationCheck,
    build_checks,
    load_feasibility_report,
    report_identity,
    require_retained_not_qualified,
)
from lisjong_arena.stage_a0_tenpai_feasibility.retained import (
    EXACT_ALIGNMENT_DISQUALIFIED,
    PRECONDITION_NOT_MET,
    REJECTION_CLASS_BY_REASON,
    RETAINED_AUGMENTATION_QUALIFIED,
    compare_source_semantic_provenance,
    instrumentation_provenance_delta,
    qualify_retained_augmentation,
)
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import (
    CELLS_FILENAME,
    MANIFEST_FILENAME,
    ROUTE_FRESH,
    availability_counts,
    cell_from_document,
    load_sidecar,
    sidecar_identity,
    verify_deterministic_recomputation,
    write_sidecar,
)


def retained_route_document(
    *,
    outcome,
    rejection_reason=None,
    dataset_identity=RETAINED_DATASET_IDENTITY,
):
    """strict reader が要求する retained route object の完全な field set。"""
    return {
        "outcome": outcome,
        "dataset_identity": dataset_identity,
        "examined_seeds": [245, 246],
        "examined_row_count": 2,
        "aligned_row_count": 0 if rejection_reason else 2,
        "instrumentation_provenance": None,
        "rejection_reason": rejection_reason,
        "rejection_class": (
            None
            if rejection_reason is None
            else REJECTION_CLASS_BY_REASON[rejection_reason]
        ),
        "rejection_detail": None if rejection_reason is None else "fixture",
    }


def _cells_by_seat(emission):
    return {int(cell.identity.seat): cell for cell in emission.cells}


class TargetAvailabilityContractTest(unittest.TestCase):
    """reason code contractと`unavailable != negative label`を固定する。"""

    def test_non_riichi_closed_opponent_is_available(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        for cell in emission.cells:
            self.assertIs(cell.availability, TargetAvailability.AVAILABLE)
            self.assertEqual(cell.melds, ())

    def test_non_riichi_open_opponent_is_available(self):
        plan = seat_plan(
            actor_seat=Seat.SEAT_0,
            opponent_hands={
                Seat.SEAT_1: OPEN_TENPAI_CONCEALED,
                Seat.SEAT_2: TENPAI_HAND,
                Seat.SEAT_3: TENPAI_HAND,
            },
            opponent_melds={Seat.SEAT_1: (pon_meld(from_seat=Seat.SEAT_2),)},
        )
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        cell = _cells_by_seat(emission)[1]
        self.assertIs(cell.availability, TargetAvailability.AVAILABLE)
        self.assertEqual(len(cell.melds), 1)
        self.assertEqual(cell.target.tenpai, 1)

    def test_structural_non_tenpai_is_an_all_zero_wait_label(self):
        plan = uniform_plan(NON_TENPAI_HAND, actor_seat=Seat.SEAT_2)
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_2),
            source_identity="fixture",
            seed=1,
        )
        for cell in emission.cells:
            self.assertIs(cell.availability, TargetAvailability.AVAILABLE)
            self.assertEqual(cell.target.tenpai, 0)
            self.assertEqual(sum(cell.target.wait_mask), 0)

    def test_structural_tenpai_matches_the_canonical_wait_mask(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_3)
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_3),
            source_identity="fixture",
            seed=1,
        )
        belief = exact_hand_belief_with_waits(tiles(TENPAI_HAND), ())
        expected = tuple(
            1 if raw == labels.SCALE else 0 for raw in belief.wait_probability_raw
        )
        for cell in emission.cells:
            self.assertEqual(cell.target.wait_mask, expected)
            self.assertEqual(cell.target.tenpai, 1)
            self.assertGreaterEqual(sum(cell.target.wait_mask), 1)

    def test_riichi_opponent_is_excluded_and_never_a_negative_label(self):
        plan = seat_plan(
            actor_seat=Seat.SEAT_0,
            opponent_hands={
                Seat.SEAT_1: TENPAI_HAND,
                Seat.SEAT_2: TENPAI_HAND,
                Seat.SEAT_3: TENPAI_HAND,
            },
            riichi={Seat.SEAT_2: RiichiState.ACCEPTED},
        )
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        cell = _cells_by_seat(emission)[2]
        self.assertIs(cell.availability, TargetAvailability.RIICHI_EXCLUDED)
        self.assertIsNone(cell.target.tenpai)
        self.assertIsNone(cell.target.wait_mask)

    def test_declared_riichi_is_also_excluded(self):
        plan = seat_plan(
            actor_seat=Seat.SEAT_0,
            opponent_hands={
                Seat.SEAT_1: TENPAI_HAND,
                Seat.SEAT_2: TENPAI_HAND,
                Seat.SEAT_3: TENPAI_HAND,
            },
            riichi={Seat.SEAT_1: RiichiState.DECLARED},
        )
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        self.assertIs(
            _cells_by_seat(emission)[1].availability,
            TargetAvailability.RIICHI_EXCLUDED,
        )

    def test_unstable_thirteen_equivalent_is_masked_with_a_reason(self):
        plan = seat_plan(
            actor_seat=Seat.SEAT_0,
            opponent_hands={
                Seat.SEAT_1: FOURTEEN_EQUIVALENT_HAND,
                Seat.SEAT_2: TENPAI_HAND,
                Seat.SEAT_3: TENPAI_HAND,
            },
        )
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        cell = _cells_by_seat(emission)[1]
        self.assertIs(cell.availability, TargetAvailability.NOT_STABLE_13_EQUIVALENT)
        self.assertIsNone(cell.target.tenpai)

    def test_invalid_physical_inventory_fails_closed(self):
        plan = seat_plan(
            actor_seat=Seat.SEAT_0,
            opponent_hands={
                Seat.SEAT_1: INVALID_INVENTORY_HAND,
                Seat.SEAT_2: TENPAI_HAND,
                Seat.SEAT_3: TENPAI_HAND,
            },
        )
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        cell = _cells_by_seat(emission)[1]
        self.assertIs(cell.availability, TargetAvailability.INVALID_PHYSICAL_INVENTORY)
        self.assertIsNone(cell.target.tenpai)
        self.assertIsNone(cell.target.wait_mask)

    def test_inventory_precheck_agrees_with_the_canonical_builder(self):
        invalid = tiles(INVALID_INVENTORY_HAND)
        self.assertTrue(labels.violates_physical_inventory(invalid, ()))
        with self.assertRaises(ValueError):
            exact_hand_belief_with_waits(invalid, ())
        valid = tiles(TENPAI_HAND)
        self.assertFalse(labels.violates_physical_inventory(valid, ()))
        exact_hand_belief_with_waits(valid, ())

    def test_missing_hidden_hand_and_meld_state_are_distinct_reasons(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0, drop_hand_for=Seat.SEAT_1),
            source_identity="fixture",
            seed=1,
        )
        self.assertIs(
            _cells_by_seat(emission)[1].availability,
            TargetAvailability.HIDDEN_HAND_UNAVAILABLE,
        )

    def test_missing_meld_state_is_an_alignment_failure(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        with self.assertRaises(StageA0AlignmentError):
            emit_decision(
                observed_decision(
                    plan, actor_seat=Seat.SEAT_0, drop_melds_for=Seat.SEAT_1
                ),
                source_identity="fixture",
                seed=1,
            )

    def test_meld_state_unavailable_is_reachable_without_alignment(self):
        identity = labels.opponent_identity(Seat.SEAT_0, 1, Seat.SEAT_0)
        target = labels.build_opponent_target(
            identity,
            seat_resolved=True,
            public_riichi=RiichiState.NONE,
            privileged_riichi_declared=False,
            concealed_tiles=tiles(TENPAI_HAND),
            melds=None,
        )
        self.assertIs(target.availability, TargetAvailability.MELD_STATE_UNAVAILABLE)

    def test_seat_mapping_unresolved_is_reachable(self):
        identity = labels.opponent_identity(Seat.SEAT_0, 1, Seat.SEAT_0)
        target = labels.build_opponent_target(
            identity,
            seat_resolved=False,
            public_riichi=None,
            privileged_riichi_declared=None,
            concealed_tiles=None,
            melds=None,
        )
        self.assertIs(target.availability, TargetAvailability.SEAT_MAPPING_UNRESOLVED)

    def test_every_reason_code_is_distinct_and_exhaustive(self):
        values = [availability.value for availability in TargetAvailability]
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(
            set(values),
            {
                "AVAILABLE",
                "RIICHI_EXCLUDED",
                "NOT_STABLE_13_EQUIVALENT",
                "HIDDEN_HAND_UNAVAILABLE",
                "MELD_STATE_UNAVAILABLE",
                "SEAT_MAPPING_UNRESOLVED",
                "INVALID_PHYSICAL_INVENTORY",
                "OTHER_FAIL_CLOSED",
            },
        )

    def test_an_unavailable_target_can_never_carry_a_label(self):
        identity = labels.opponent_identity(Seat.SEAT_0, 1, Seat.SEAT_0)
        with self.assertRaises(Exception):
            labels.OpponentTenpaiTarget(
                identity=identity,
                availability=TargetAvailability.RIICHI_EXCLUDED,
                wait_mask=(0,) * 34,
                tenpai=0,
            )


class MeldHandlingTest(unittest.TestCase):
    """meld（槓を含む）がstructural equivalentとして扱われることを固定する。"""

    def test_ankan_containing_state_is_a_stable_thirteen_equivalent_target(self):
        plan = seat_plan(
            actor_seat=Seat.SEAT_0,
            opponent_hands={
                Seat.SEAT_1: OPEN_TENPAI_CONCEALED,
                Seat.SEAT_2: TENPAI_HAND,
                Seat.SEAT_3: TENPAI_HAND,
            },
            opponent_melds={Seat.SEAT_1: (ankan_meld(),)},
        )
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        cell = _cells_by_seat(emission)[1]
        self.assertIs(cell.availability, TargetAvailability.AVAILABLE)
        self.assertEqual(len(cell.melds[0].tiles), 4)
        expected = exact_hand_belief_with_waits(
            tiles(OPEN_TENPAI_CONCEALED), (ankan_meld(),)
        )
        self.assertEqual(
            cell.target.wait_mask,
            tuple(
                1 if raw == labels.SCALE else 0 for raw in expected.wait_probability_raw
            ),
        )


class RelativeSeatMappingTest(unittest.TestCase):
    """固定actor seatの偶然で通らないよう、全seat / rotationでbindingを検証する。"""

    def test_every_actor_seat_binds_distinct_canonical_opponents(self):
        for actor_seat in Seat:
            for dealer_seat in Seat:
                plan = uniform_plan(TENPAI_HAND, actor_seat=actor_seat)
                emission = emit_decision(
                    observed_decision(
                        plan, actor_seat=actor_seat, dealer_seat=dealer_seat
                    ),
                    source_identity="fixture",
                    seed=1,
                )
                seats = [int(cell.identity.seat) for cell in emission.cells]
                self.assertEqual(len(set(seats)), 3)
                self.assertNotIn(int(actor_seat), seats)
                for cell in emission.cells:
                    offset = cell.identity.viewer_relative_offset
                    self.assertEqual(
                        int(cell.identity.seat), (int(actor_seat) + offset) % 4
                    )

    def test_seat_wind_follows_the_dealer_rotation(self):
        for dealer_seat in Seat:
            plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_1)
            emission = emit_decision(
                observed_decision(
                    plan, actor_seat=Seat.SEAT_1, dealer_seat=dealer_seat
                ),
                source_identity="fixture",
                seed=1,
            )
            for cell in emission.cells:
                expected_index = (int(cell.identity.seat) - int(dealer_seat)) % 4
                self.assertIs(cell.identity.wind, tuple(Wind)[expected_index])

    def test_a_permuted_opponent_hand_changes_the_attached_label(self):
        """seat attachmentが入れ替わればlabelも入れ替わることを実測する。"""
        base = seat_plan(
            actor_seat=Seat.SEAT_0,
            opponent_hands={
                Seat.SEAT_1: TENPAI_HAND,
                Seat.SEAT_2: NON_TENPAI_HAND,
                Seat.SEAT_3: NON_TENPAI_HAND,
            },
        )
        permuted = seat_plan(
            actor_seat=Seat.SEAT_0,
            opponent_hands={
                Seat.SEAT_1: NON_TENPAI_HAND,
                Seat.SEAT_2: TENPAI_HAND,
                Seat.SEAT_3: NON_TENPAI_HAND,
            },
        )
        first = _cells_by_seat(
            emit_decision(
                observed_decision(base, actor_seat=Seat.SEAT_0),
                source_identity="fixture",
                seed=1,
            )
        )
        second = _cells_by_seat(
            emit_decision(
                observed_decision(permuted, actor_seat=Seat.SEAT_0),
                source_identity="fixture",
                seed=1,
            )
        )
        self.assertEqual((first[1].target.tenpai, first[2].target.tenpai), (1, 0))
        self.assertEqual((second[1].target.tenpai, second[2].target.tenpai), (0, 1))

    def test_a_cell_cannot_claim_a_seat_it_did_not_derive(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        document = emission.cells[0].to_document()
        document["opponent_seat"] = (document["opponent_seat"] + 1) % 4
        with self.assertRaises(StageA0SidecarError):
            cell_from_document(document, "cell[0]")


class SameStateSeamTest(unittest.TestCase):
    """public row stateとprivileged label stateの同一性をfail closedで固定する。"""

    def test_a_snapshot_from_another_step_is_rejected(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        with self.assertRaises(StageA0AlignmentError):
            observed_decision(
                plan,
                actor_seat=Seat.SEAT_0,
                step_ordinal=3,
                hidden_step_ordinal=4,
            )

    def test_a_divergent_actor_hand_is_rejected(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        decision = observed_decision(plan, actor_seat=Seat.SEAT_0)
        divergent_plan = dict(plan)
        divergent_plan[Seat.SEAT_0] = (tiles(TENPAI_HAND), (), RiichiState.NONE)
        broken = hidden_state_for(divergent_plan, actor_seat=Seat.SEAT_0)
        with self.assertRaises(StageA0AlignmentError):
            emit_decision(
                ObservedDecision(
                    step_ordinal=decision.step_ordinal,
                    decision_ordinal=decision.decision_ordinal,
                    actor_seat=decision.actor_seat,
                    context=decision.context,
                    selected_action=decision.selected_action,
                    hidden=broken,
                ),
                source_identity="fixture",
                seed=1,
            )

    def test_a_divergent_public_meld_snapshot_is_rejected(self):
        plan = seat_plan(
            actor_seat=Seat.SEAT_0,
            opponent_hands={
                Seat.SEAT_1: OPEN_TENPAI_CONCEALED,
                Seat.SEAT_2: TENPAI_HAND,
                Seat.SEAT_3: TENPAI_HAND,
            },
            opponent_melds={Seat.SEAT_1: (pon_meld(from_seat=Seat.SEAT_2),)},
        )
        decision = observed_decision(plan, actor_seat=Seat.SEAT_0)
        stripped = dict(plan)
        stripped[Seat.SEAT_1] = (tiles(OPEN_TENPAI_CONCEALED), (), RiichiState.NONE)
        with self.assertRaises(StageA0AlignmentError):
            emit_decision(
                ObservedDecision(
                    step_ordinal=decision.step_ordinal,
                    decision_ordinal=decision.decision_ordinal,
                    actor_seat=decision.actor_seat,
                    context=decision.context,
                    selected_action=decision.selected_action,
                    hidden=hidden_state_for(stripped, actor_seat=Seat.SEAT_0),
                ),
                source_identity="fixture",
                seed=1,
            )

    def test_row_identity_mismatch_is_a_hard_failure(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        decision = observed_decision(plan, actor_seat=Seat.SEAT_0)
        identity = public_row.DecisionRowIdentity(
            source_identity="fixture",
            seed=1,
            step_ordinal=0,
            decision_ordinal=0,
            actor_seat=2,
        )
        with self.assertRaises(StageA0AlignmentError):
            public_row.build_public_decision_row(
                identity, decision.context, decision.selected_action
            )


class PublicPrivateBoundaryTest(unittest.TestCase):
    """hidden truthがpublic 8204 inputへ入らないことを固定する。"""

    def test_public_row_uses_the_canonical_flat_bc_builders(self):
        self.assertIs(public_row.build_policy_input_feature, build_policy_input_feature)
        self.assertIs(public_row.tensor_values, tensor_values)
        self.assertIs(public_row.build_legal_action_mask, build_legal_action_mask)
        self.assertIs(public_row.encode_action, encode_action)
        self.assertEqual(protocol.FEATURE_DIMENSION, 8204)
        self.assertEqual(protocol.VOCABULARY_SIZE, 802)

    def test_different_hidden_truth_leaves_the_public_row_bytes_identical(self):
        first = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        second = uniform_plan(NON_TENPAI_HAND, actor_seat=Seat.SEAT_0)
        left = emit_decision(
            observed_decision(first, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        right = emit_decision(
            observed_decision(second, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        self.assertEqual(left.row.feature_bytes(), right.row.feature_bytes())
        self.assertEqual(left.row.legal_mask_bytes(), right.row.legal_mask_bytes())
        self.assertEqual(left.row.row_digest(), right.row.row_digest())
        self.assertNotEqual(
            [cell.target.tenpai for cell in left.cells],
            [cell.target.tenpai for cell in right.cells],
        )

    def test_the_public_row_carries_no_privileged_field(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        self.assertEqual(
            set(public_row.PublicDecisionRow.__dataclass_fields__),
            {
                "identity",
                "split",
                "round_wind",
                "hand_number",
                "honba",
                "feature_values",
                "legal_mask",
                "teacher_action_index",
                "teacher_action_family",
            },
        )
        self.assertEqual(
            emission.row.feature_values,
            tensor_values(
                build_policy_input_feature(
                    emission_context(plan, Seat.SEAT_0),
                )
            ),
        )


def emission_context(plan, actor_seat):
    return policy_input_for(plan, actor_seat=actor_seat, dealer_seat=Seat.SEAT_0)


class SidecarTest(unittest.TestCase):
    """最小privileged sidecar contractのwrite / strict readback / recomputation。"""

    def _emissions(self):
        plans = (
            (Seat.SEAT_0, uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)),
            (Seat.SEAT_1, uniform_plan(NON_TENPAI_HAND, actor_seat=Seat.SEAT_1)),
            (Seat.SEAT_2, uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_2)),
        )
        cells = []
        for ordinal, (actor_seat, plan) in enumerate(plans):
            emission = emit_decision(
                observed_decision(
                    plan,
                    actor_seat=actor_seat,
                    step_ordinal=ordinal,
                    decision_ordinal=ordinal,
                ),
                source_identity="stage-a0-fixture",
                seed=751,
            )
            cells.extend(emission.cells)
        return tuple(cells)

    def test_write_and_strict_readback_round_trip(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "sidecar"
            sidecar = write_sidecar(
                destination,
                self._emissions(),
                route=ROUTE_FRESH,
                source_identity="stage-a0-fixture",
                provenance=FIXTURE_PROVENANCE,
            )
            self.assertEqual(sidecar.route, ROUTE_FRESH)
            self.assertEqual(len(sidecar.cells), 9)
            self.assertEqual(sidecar.manifest["totals"]["row_count"], 3)
            reloaded = load_sidecar(destination)
            self.assertEqual(reloaded.identity, sidecar.identity)
            self.assertEqual(reloaded.cells, sidecar.cells)

    def test_deterministic_recomputation_from_the_same_bytes(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "sidecar"
            sidecar = write_sidecar(
                destination,
                self._emissions(),
                route=ROUTE_FRESH,
                source_identity="stage-a0-fixture",
                provenance=FIXTURE_PROVENANCE,
            )
            self.assertEqual(verify_deterministic_recomputation(sidecar), 9)
            self.assertEqual(
                verify_deterministic_recomputation(load_sidecar(destination)), 9
            )

    def test_a_corrupted_cell_payload_is_rejected(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "sidecar"
            write_sidecar(
                destination,
                self._emissions(),
                route=ROUTE_FRESH,
                source_identity="stage-a0-fixture",
                provenance=FIXTURE_PROVENANCE,
            )
            payload = (destination / CELLS_FILENAME).read_text("utf-8")
            (destination / CELLS_FILENAME).write_text(
                payload.replace('"tenpai":1', '"tenpai":0', 1), encoding="utf-8"
            )
            with self.assertRaises(StageA0SidecarError):
                load_sidecar(destination)

    def test_a_tampered_label_that_keeps_the_digest_fails_recomputation(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "sidecar"
            write_sidecar(
                destination,
                self._emissions(),
                route=ROUTE_FRESH,
                source_identity="stage-a0-fixture",
                provenance=FIXTURE_PROVENANCE,
            )
            manifest = json.loads((destination / MANIFEST_FILENAME).read_text("utf-8"))
            lines = (destination / CELLS_FILENAME).read_text("utf-8").splitlines(True)
            for index, line in enumerate(lines):
                document = json.loads(line)
                if document["availability"] == "AVAILABLE":
                    document["wait_mask"] = [0] * 34
                    document["tenpai"] = 0
                    lines[index] = (
                        json.dumps(
                            document,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    break
            payload = "".join(lines).encode("utf-8")
            (destination / CELLS_FILENAME).write_bytes(payload)
            manifest["files"]["cells"] = {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            manifest.pop("sidecar_identity")
            manifest["sidecar_identity"] = sidecar_identity(manifest)
            (destination / MANIFEST_FILENAME).write_text(
                canonical_json_text(manifest), encoding="utf-8", newline="\n"
            )
            with self.assertRaises(StageA0SidecarError):
                verify_deterministic_recomputation(load_sidecar(destination))

    def test_an_existing_destination_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "sidecar"
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                write_sidecar(
                    destination,
                    self._emissions(),
                    route=ROUTE_FRESH,
                    source_identity="stage-a0-fixture",
                    provenance=FIXTURE_PROVENANCE,
                )


class RetainedAugmentationTest(unittest.TestCase):
    """retained corpus exact augmentationのfail-closed境界を固定する。"""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.seeds = (245, 246)
        self.decisions = {}
        emissions = {}
        for ordinal, seed in enumerate(self.seeds):
            actor_seat = Seat(ordinal % 4)
            plan = uniform_plan(TENPAI_HAND, actor_seat=actor_seat)
            decision = observed_decision(
                plan,
                actor_seat=actor_seat,
                step_ordinal=ordinal,
                decision_ordinal=ordinal,
            )
            self.decisions[seed] = (decision,)
            emissions[seed] = (
                (
                    decision,
                    emit_decision(decision, source_identity="pending", seed=seed),
                ),
            )
        self.emissions = emissions
        self.dataset_path = Path(self.directory.name) / "retained"
        self.dataset = build_retained_dataset(self.dataset_path, emissions)

    def _source(self, seed):
        return self.decisions.get(seed, ())

    def _qualify(self, **overrides):
        arguments = {
            "seeds": self.seeds,
            "expected_dataset_identity": self.dataset.identity,
            "observed_decision_source": self._source,
            "current_provenance": RETAINED_PROVENANCE,
        }
        arguments.update(overrides)
        return qualify_retained_augmentation(self.dataset_path, **arguments)

    def test_exact_alignment_qualifies(self):
        result = self._qualify()
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_QUALIFIED)
        self.assertEqual(result.aligned_row_count, len(self.seeds))
        self.assertEqual(len(result.cells), 3 * len(self.seeds))
        for cell in result.cells:
            self.assertEqual(cell.row_identity.source_identity, self.dataset.identity)

    def test_qualification_does_not_full_read_retained_payload_files(self):
        """#258 qualification must not materialize VALIDATION / protected TEST payloads."""
        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError(
                "retained qualification must use bounded prefix reads"
            ),
        ):
            result = self._qualify()
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_QUALIFIED)
        self.assertEqual(result.aligned_row_count, len(self.seeds))

    def test_a_wrong_decision_ordinal_is_rejected(self):
        def source(seed):
            for decision in self._source(seed):
                yield ObservedDecision(
                    step_ordinal=decision.step_ordinal,
                    decision_ordinal=decision.decision_ordinal + 7,
                    actor_seat=decision.actor_seat,
                    context=decision.context,
                    selected_action=decision.selected_action,
                    hidden=decision.hidden,
                )

        result = self._qualify(observed_decision_source=source)
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertEqual(result.rejection_reason, "decision-identity-mismatch")
        self.assertEqual(result.cells, ())

    def test_a_wrong_actor_seat_is_rejected(self):
        def source(seed):
            for decision in self._source(seed):
                actor_seat = Seat((int(decision.actor_seat) + 1) % 4)
                plan = uniform_plan(TENPAI_HAND, actor_seat=actor_seat)
                yield observed_decision(
                    plan,
                    actor_seat=actor_seat,
                    step_ordinal=decision.step_ordinal,
                    decision_ordinal=decision.decision_ordinal,
                )

        result = self._qualify(observed_decision_source=source)
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertEqual(result.rejection_reason, "decision-identity-mismatch")

    def test_a_missing_seed_is_rejected(self):
        result = self._qualify(observed_decision_source=lambda seed: ())
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertEqual(result.rejection_reason, "decision-identity-mismatch")

    def test_a_feature_mismatch_is_rejected(self):
        def source(seed):
            for decision in self._source(seed):
                actor_seat = decision.actor_seat
                plan = uniform_plan(
                    TENPAI_HAND,
                    actor_seat=actor_seat,
                    actor_hand="9m 9m 9p 9p 1s 2s 3s 4s 5s 6s 7s 8s 9s 1m",
                )
                yield observed_decision(
                    plan,
                    actor_seat=actor_seat,
                    step_ordinal=decision.step_ordinal,
                    decision_ordinal=decision.decision_ordinal,
                )

        result = self._qualify(observed_decision_source=source)
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertIn(
            result.rejection_reason,
            ("feature-row-mismatch", "legal-mask-mismatch"),
        )

    def test_a_legal_mask_mismatch_is_rejected(self):
        def source(seed):
            for decision in self._source(seed):
                policy_input = decision.context.input
                concealed = policy_input.own_hand.concealed_tiles
                actions = (
                    decision.selected_action,
                    DiscardAction(
                        actor=policy_input.self_seat,
                        tile=concealed[4],
                        tsumogiri=False,
                    ),
                    DiscardAction(
                        actor=policy_input.self_seat,
                        tile=concealed[2],
                        tsumogiri=False,
                    ),
                )
                yield ObservedDecision(
                    step_ordinal=decision.step_ordinal,
                    decision_ordinal=decision.decision_ordinal,
                    actor_seat=decision.actor_seat,
                    context=DecisionContext(input=policy_input, legal_actions=actions),
                    selected_action=decision.selected_action,
                    hidden=decision.hidden,
                )

        result = self._qualify(observed_decision_source=source)
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertEqual(result.rejection_reason, "legal-mask-mismatch")

    def test_a_same_state_failure_is_reported_not_raised(self):
        def source(seed):
            for decision in self._source(seed):
                other = uniform_plan(NON_TENPAI_HAND, actor_seat=Seat.SEAT_3)
                yield ObservedDecision(
                    step_ordinal=decision.step_ordinal,
                    decision_ordinal=decision.decision_ordinal,
                    actor_seat=decision.actor_seat,
                    context=decision.context,
                    selected_action=decision.selected_action,
                    hidden=hidden_state_for(
                        other,
                        actor_seat=decision.actor_seat,
                        step_ordinal=decision.step_ordinal,
                    ),
                )

        result = self._qualify(observed_decision_source=source)
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertEqual(result.rejection_reason, "same-state-co-emission-failed")
        self.assertEqual(result.cells, ())

    def test_instrumentation_revision_drift_still_allows_qualification(self):
        """#258 の instrumentation は historical Arena revision には存在しない。

        source semantics が一致し全 row が exact に align するなら、Arena
        revision が違うだけで retained route を落としてはいけない。
        """
        drifted = dict(RETAINED_PROVENANCE)
        drifted["lisjong_arena_revision"] = "9" * 40
        drifted["lisjong_arena_version"] = "0.2.0"
        result = self._qualify(current_provenance=drifted)
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_QUALIFIED)
        self.assertEqual(result.aligned_row_count, len(self.seeds))
        recorded = result.instrumentation_provenance
        self.assertFalse(recorded["lisjong_arena_revision"]["matches"])
        self.assertEqual(
            recorded["lisjong_arena_revision"]["retained"],
            RETAINED_PROVENANCE["lisjong_arena_revision"],
        )
        self.assertEqual(recorded["lisjong_arena_revision"]["qualification"], "9" * 40)

    def test_a_teacher_source_semantic_revision_mismatch_is_rejected(self):
        for field in ("lisjong_revision", "lisjong_version"):
            with self.subTest(field=field):
                drifted = dict(RETAINED_PROVENANCE)
                drifted[field] = "9" * 40
                result = self._qualify(current_provenance=drifted)
                self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
                self.assertEqual(
                    result.rejection_reason, "provenance-revision-mismatch"
                )
                self.assertIn(field, result.rejection_detail)

    def test_an_execution_provenance_mismatch_is_rejected(self):
        """hidden-state exactness に必要な dependency の drift は reject する。"""
        for field in (
            "riichienv_version",
            "lisjong_engine_revision",
            "lisjong_engine_version",
            "python_version",
            "execution_environment",
        ):
            with self.subTest(field=field):
                drifted = dict(RETAINED_PROVENANCE)
                drifted[field] = "drifted"
                result = self._qualify(current_provenance=drifted)
                self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
                self.assertEqual(
                    result.rejection_reason, "provenance-revision-mismatch"
                )
                self.assertIn(field, result.rejection_detail)
                self.assertEqual(result.cells, ())

    def test_an_unclassified_provenance_field_fails_closed(self):
        extended = dict(RETAINED_PROVENANCE)
        extended["future_field"] = "value"
        with self.assertRaises(StageA0ProtocolError):
            self._qualify(current_provenance=extended)
        reduced = dict(RETAINED_PROVENANCE)
        del reduced["python_version"]
        with self.assertRaises(StageA0ProtocolError):
            self._qualify(current_provenance=reduced)

    def test_the_provenance_split_covers_every_recorded_field(self):
        self.assertEqual(
            set(protocol.SOURCE_SEMANTIC_PROVENANCE_FIELDS)
            | set(protocol.INSTRUMENTATION_PROVENANCE_FIELDS),
            set(RETAINED_PROVENANCE),
        )
        self.assertNotIn(
            "lisjong_arena_revision", protocol.SOURCE_SEMANTIC_PROVENANCE_FIELDS
        )
        self.assertIn("lisjong_revision", protocol.SOURCE_SEMANTIC_PROVENANCE_FIELDS)
        self.assertIn("riichienv_version", protocol.SOURCE_SEMANTIC_PROVENANCE_FIELDS)
        self.assertEqual(
            compare_source_semantic_provenance(
                RETAINED_PROVENANCE, RETAINED_PROVENANCE
            ),
            (),
        )
        delta = instrumentation_provenance_delta(
            RETAINED_PROVENANCE, RETAINED_PROVENANCE
        )
        self.assertTrue(all(entry["matches"] for entry in delta.values()))

    def test_precondition_failures_do_not_authorize_the_fresh_fallback(self):
        """exact alignment を正しい条件で試せていない失敗は precondition。"""
        cases = {
            "dataset-identity-mismatch": self._qualify(
                expected_dataset_identity="0" * 64
            ),
            "provenance-revision-mismatch": self._qualify(
                current_provenance={**RETAINED_PROVENANCE, "riichienv_version": "9.9"}
            ),
            "retained-artifact-unreadable": qualify_retained_augmentation(
                Path(self.directory.name) / "missing",
                seeds=self.seeds,
                expected_dataset_identity=self.dataset.identity,
                observed_decision_source=self._source,
                current_provenance=RETAINED_PROVENANCE,
            ),
        }
        for reason, result in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
                self.assertEqual(result.rejection_reason, reason)
                self.assertEqual(result.rejection_class, PRECONDITION_NOT_MET)
                self.assertFalse(result.authorizes_fresh_fallback)

    def test_exact_alignment_failures_authorize_the_fresh_fallback(self):
        """正しい条件下で exact join を試して失敗した場合だけ technical NOT QUALIFIED。"""

        def feature_drift(seed):
            for decision in self._source(seed):
                actor_seat = decision.actor_seat
                plan = uniform_plan(
                    TENPAI_HAND,
                    actor_seat=actor_seat,
                    actor_hand="9m 9m 9p 9p 1s 2s 3s 4s 5s 6s 7s 8s 9s 1m",
                )
                yield observed_decision(
                    plan,
                    actor_seat=actor_seat,
                    step_ordinal=decision.step_ordinal,
                    decision_ordinal=decision.decision_ordinal,
                )

        def decision_drift(seed):
            for decision in self._source(seed):
                yield ObservedDecision(
                    step_ordinal=decision.step_ordinal,
                    decision_ordinal=decision.decision_ordinal + 7,
                    actor_seat=decision.actor_seat,
                    context=decision.context,
                    selected_action=decision.selected_action,
                    hidden=decision.hidden,
                )

        for name, source in (
            ("feature", feature_drift),
            ("decision", decision_drift),
        ):
            with self.subTest(case=name):
                result = self._qualify(observed_decision_source=source)
                self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
                self.assertEqual(result.rejection_class, EXACT_ALIGNMENT_DISQUALIFIED)
                self.assertTrue(result.authorizes_fresh_fallback)

    def test_round_and_legal_mask_failures_are_alignment_disqualifications(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "retained"
        dataset = build_retained_dataset(
            path, self.emissions, row_overrides={"round_ordinal": 5}
        )
        result = qualify_retained_augmentation(
            path,
            seeds=self.seeds,
            expected_dataset_identity=dataset.identity,
            observed_decision_source=self._source,
            current_provenance=RETAINED_PROVENANCE,
        )
        self.assertEqual(result.rejection_class, EXACT_ALIGNMENT_DISQUALIFIED)
        self.assertTrue(result.authorizes_fresh_fallback)

    def test_every_rejection_reason_is_classified(self):
        reasons = set(REJECTION_CLASS_BY_REASON.values())
        self.assertEqual(reasons, {PRECONDITION_NOT_MET, EXACT_ALIGNMENT_DISQUALIFIED})
        self.assertEqual(
            REJECTION_CLASS_BY_REASON["same-state-co-emission-failed"],
            EXACT_ALIGNMENT_DISQUALIFIED,
        )
        self.assertEqual(
            REJECTION_CLASS_BY_REASON["retained-row-missing"], PRECONDITION_NOT_MET
        )

    def test_an_unexpected_exception_is_not_converted_to_not_qualified(self):
        """programming error を retained NOT QUALIFIED へ丸めない。"""

        def failing(identity, concealed_tiles, own_melds):
            raise RuntimeError("programming error")

        original = labels.structural_wait_for_hand
        labels.structural_wait_for_hand = failing
        self.addCleanup(setattr, labels, "structural_wait_for_hand", original)
        with self.assertRaises(RuntimeError):
            self._qualify()

    def test_a_round_identity_mismatch_is_rejected(self):
        """artifact が保持する derived round identity も明示比較する。"""
        for field, value in (
            ("round_wind", "south"),
            ("hand_number", 2),
            ("honba", 1),
            ("round_ordinal", 5),
        ):
            with self.subTest(field=field):
                directory = TemporaryDirectory()
                self.addCleanup(directory.cleanup)
                path = Path(directory.name) / "retained"
                dataset = build_retained_dataset(
                    path, self.emissions, row_overrides={field: value}
                )
                result = qualify_retained_augmentation(
                    path,
                    seeds=self.seeds,
                    expected_dataset_identity=dataset.identity,
                    observed_decision_source=self._source,
                    current_provenance=RETAINED_PROVENANCE,
                )
                self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
                self.assertEqual(result.rejection_reason, "round-identity-mismatch")
                self.assertIn(field, result.rejection_detail)

    def test_a_legal_action_count_mismatch_is_rejected(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "retained"
        dataset = build_retained_dataset(
            path, self.emissions, legal_action_count_drift=True
        )
        result = qualify_retained_augmentation(
            path,
            seeds=self.seeds,
            expected_dataset_identity=dataset.identity,
            observed_decision_source=self._source,
            current_provenance=RETAINED_PROVENANCE,
        )
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertEqual(result.rejection_reason, "legal-mask-mismatch")

    def test_a_dataset_identity_mismatch_is_rejected(self):
        result = self._qualify(expected_dataset_identity="0" * 64)
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertEqual(result.rejection_reason, "dataset-identity-mismatch")

    def test_an_unreadable_artifact_is_rejected_not_assumed(self):
        missing = Path(self.directory.name) / "missing"
        result = qualify_retained_augmentation(
            missing,
            seeds=self.seeds,
            expected_dataset_identity=self.dataset.identity,
            observed_decision_source=self._source,
            current_provenance=RETAINED_PROVENANCE,
        )
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertEqual(result.rejection_reason, "retained-artifact-unreadable")

    def test_protected_test_and_validation_seeds_are_refused(self):
        for seed in (DATASET_TEST_SEEDS[0], DATASET_VALIDATION_SEEDS[0]):
            with self.assertRaises(StageA0ProtocolError):
                self._qualify(seeds=(seed,))

    def test_the_qualification_path_never_reads_protected_test_rows(self):
        read_indices = []
        original = type(self.dataset).feature_row

        def recording_feature_row(dataset, index):
            read_indices.append(index)
            return original(dataset, index)

        self.addCleanup(setattr, type(self.dataset), "feature_row", original)
        type(self.dataset).feature_row = recording_feature_row
        result = self._qualify()
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_QUALIFIED)
        protected = {
            index
            for index, row in enumerate(self.dataset.rows)
            if row.seed in DATASET_TEST_SEEDS or row.seed in DATASET_VALIDATION_SEEDS
        }
        self.assertTrue(protected)
        self.assertFalse(protected.intersection(read_indices))


class FreshLiveLabelTest(unittest.TestCase):
    """bounded technical smokeのco-emission qualificationを固定する。"""

    def _source(self, seed):
        actor_seat = Seat(seed % 4)
        plan = uniform_plan(TENPAI_HAND, actor_seat=actor_seat)
        return (
            observed_decision(
                plan, actor_seat=actor_seat, step_ordinal=0, decision_ordinal=0
            ),
        )

    def test_the_locked_smoke_population_is_fresh_and_bounded(self):
        self.assertEqual(protocol.SMOKE_SEEDS, (751, 752))
        self.assertLessEqual(
            len(protocol.SMOKE_SEEDS), protocol.MAXIMUM_SMOKE_GAME_COUNT
        )
        with self.assertRaises(StageA0ProtocolError):
            protocol.require_smoke_seed(245)

    def test_co_emission_qualifies_on_the_locked_population(self):
        result = qualify_fresh_live_label(observed_decision_source=self._source)
        self.assertEqual(result.outcome, FRESH_LIVE_LABEL_PATH_QUALIFIED)
        self.assertEqual(result.row_count, len(protocol.SMOKE_SEEDS))
        self.assertEqual(result.cell_count, 3 * len(protocol.SMOKE_SEEDS))

    def test_an_alignment_failure_never_becomes_a_label(self):
        def broken(seed):
            decision = self._source(seed)[0]
            other = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
            yield ObservedDecision(
                step_ordinal=decision.step_ordinal,
                decision_ordinal=decision.decision_ordinal,
                actor_seat=decision.actor_seat,
                context=decision.context,
                selected_action=decision.selected_action,
                hidden=hidden_state_for(other, actor_seat=decision.actor_seat),
            )

        result = qualify_fresh_live_label(observed_decision_source=broken)
        self.assertFalse(result.is_qualified)
        self.assertEqual(result.cells, ())


class FeasibilityReportTest(unittest.TestCase):
    """artifactが測っていないhard outcomeを作らないことを固定する。"""

    def _cells(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        return emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=751,
        ).cells

    def _report(self, **overrides):
        cells = self._cells()
        arguments = {
            "provenance": FIXTURE_PROVENANCE,
            "instrumentation_provenance": None,
            "source_paths_examined": ("fixture",),
            "retained_corpus_identity": None,
            "retained_outcome": None,
            "retained_evidence": None,
            "smoke_population_identity": protocol.SMOKE_POPULATION_IDENTITY,
            "fresh_outcome": None,
            "sidecar_identity": None,
            "row_count": 1,
            "cell_count": len(cells),
            "availability_counts": availability_counts(cells),
            "checks": build_checks(cells, deterministic_recomputation=None),
            "hard_outcome": None,
            "pending_reason": "operator-local retained corpus is required",
            "recommended_route": None,
        }
        arguments.update(overrides)
        return FeasibilityReport(**arguments)

    def test_all_reason_codes_are_counted_even_when_zero(self):
        document = self._report().to_document()
        self.assertEqual(
            set(document["counts"]["availability"]),
            {availability.value for availability in TargetAvailability},
        )

    def test_exactly_one_of_outcome_or_pending_reason_is_set(self):
        with self.assertRaises(StageA0ProtocolError):
            self._report(hard_outcome=RETAINED_AUGMENTATION_QUALIFIED)
        with self.assertRaises(StageA0ProtocolError):
            self._report(pending_reason=None)
        self._report(hard_outcome=RETAINED_AUGMENTATION_QUALIFIED, pending_reason=None)

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(StageA0ProtocolError):
            self._report(hard_outcome="TENPAI LEARNABILITY PASS", pending_reason=None)

    def test_the_artifact_never_carries_a_learnability_metric(self):
        document = self._report().to_document()
        serialized = json.dumps(document).lower()
        for forbidden in (
            "cross_entropy",
            "learnability",
            "accuracy",
            "strength",
            "prevalence",
            "baseline_loss",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_qualification_checks_cover_every_required_axis(self):
        names = {check.name for check in self._report().checks}
        self.assertEqual(
            names,
            {
                "seat_mapping",
                "stable_13_equivalent",
                "physical_inventory",
                "public_private_boundary",
                "fail_closed",
            },
        )
        with_recomputation = self._report(
            checks=build_checks(
                self._cells(),
                deterministic_recomputation=QualificationCheck(
                    name="deterministic_recomputation",
                    qualified=True,
                    detail="fixture",
                ),
            )
        )
        self.assertIn(
            "deterministic_recomputation",
            with_recomputation.to_document()["qualifications"],
        )

    def test_the_report_is_written_as_canonical_json_and_never_overwritten(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            self._report().write(path)
            self.assertTrue(path.is_file())
            with self.assertRaises(Exception):
                self._report().write(path)


class FeasibilityReportStrictReaderTest(unittest.TestCase):
    """fresh fallback を authorize できる retained evidence を絞る。"""

    def _cells(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        return emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=751,
        ).cells

    def _retained_report(
        self,
        *,
        outcome=RETAINED_AUGMENTATION_NOT_QUALIFIED,
        rejection_reason="feature-row-mismatch",
        corpus_identity=RETAINED_DATASET_IDENTITY,
        **over,
    ):
        cells = self._cells()
        arguments = {
            "provenance": FIXTURE_PROVENANCE,
            "instrumentation_provenance": None,
            "source_paths_examined": ("fixture",),
            "retained_corpus_identity": corpus_identity,
            "retained_outcome": retained_route_document(
                outcome=outcome,
                rejection_reason=rejection_reason,
                dataset_identity=corpus_identity,
            ),
            "retained_evidence": None,
            "smoke_population_identity": None,
            "fresh_outcome": None,
            "sidecar_identity": None,
            "row_count": 1,
            "cell_count": len(cells),
            "availability_counts": availability_counts(cells),
            "checks": build_checks(cells, deterministic_recomputation=None),
            "hard_outcome": None,
            "pending_reason": "retained route measured; fresh fallback required",
            "recommended_route": None,
        }
        arguments.update(over)
        return FeasibilityReport(**arguments)

    def _write(self, report, directory, name="report.json"):
        path = Path(directory) / name
        report.write(path)
        return path

    def test_a_written_report_round_trips_through_the_strict_reader(self):
        with TemporaryDirectory() as directory:
            path = self._write(self._retained_report(), directory)
            loaded = load_feasibility_report(path)
            self.assertIsInstance(loaded, LoadedFeasibilityReport)
            self.assertEqual(
                loaded.document["report_schema_version"], REPORT_SCHEMA_VERSION
            )
            self.assertEqual(
                loaded.retained_outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED
            )
            self.assertEqual(loaded.retained_corpus_identity, RETAINED_DATASET_IDENTITY)
            self.assertEqual(loaded.identity, report_identity(loaded.document))

    def test_a_not_qualified_retained_report_authorizes_the_fallback(self):
        with TemporaryDirectory() as directory:
            path = self._write(self._retained_report(), directory)
            evidence = require_retained_not_qualified(load_feasibility_report(path))
            self.assertEqual(
                evidence["retained_outcome"], RETAINED_AUGMENTATION_NOT_QUALIFIED
            )
            self.assertEqual(
                evidence["retained_corpus_identity"], RETAINED_DATASET_IDENTITY
            )
            self.assertEqual(len(evidence["report_identity"]), 64)

    def test_a_qualified_retained_report_never_authorizes_the_fallback(self):
        with TemporaryDirectory() as directory:
            path = self._write(
                self._retained_report(
                    outcome=RETAINED_AUGMENTATION_QUALIFIED,
                    rejection_reason=None,
                    hard_outcome=RETAINED_AUGMENTATION_QUALIFIED,
                    pending_reason=None,
                    recommended_route="retained-augmentation",
                ),
                directory,
            )
            with self.assertRaises(StageA0ReportError):
                require_retained_not_qualified(load_feasibility_report(path))

    def test_a_report_without_a_measured_retained_route_is_refused(self):
        with TemporaryDirectory() as directory:
            path = self._write(
                self._retained_report(
                    retained_outcome=None, retained_corpus_identity=None
                ),
                directory,
            )
            with self.assertRaises(StageA0ReportError):
                require_retained_not_qualified(load_feasibility_report(path))

    def test_a_tampered_report_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = self._write(self._retained_report(), directory)
            document = json.loads(path.read_text("utf-8"))
            document["routes"]["retained_augmentation"]["rejection_class"] = (
                PRECONDITION_NOT_MET
            )
            path.write_text(canonical_json_text(document), encoding="utf-8")
            with self.assertRaises(StageA0ReportError):
                load_feasibility_report(path)

    def test_a_foreign_protocol_or_issue_is_rejected(self):
        for pointer, value in (
            (("protocol", "protocol_id"), "arena-some-other-protocol-v1"),
            (("protocol", "issue"), "lisbun/lisjong-arena#255"),
            (("report_schema_version",), "arena-stage-a0-report-v0"),
        ):
            with self.subTest(pointer=pointer):
                with TemporaryDirectory() as directory:
                    path = self._write(self._retained_report(), directory)
                    document = json.loads(path.read_text("utf-8"))
                    target = document
                    for key in pointer[:-1]:
                        target = target[key]
                    target[pointer[-1]] = value
                    document.pop("report_identity")
                    document["report_identity"] = report_identity(document)
                    path.write_text(canonical_json_text(document), encoding="utf-8")
                    with self.assertRaises(StageA0ReportError):
                        load_feasibility_report(path)

    def test_a_malformed_or_unrelated_document_is_rejected(self):
        with TemporaryDirectory() as directory:
            broken = Path(directory) / "broken.json"
            broken.write_text("{ not json", encoding="utf-8")
            with self.assertRaises(StageA0ReportError):
                load_feasibility_report(broken)
            unrelated = Path(directory) / "unrelated.json"
            unrelated.write_text('{"hello":"world"}', encoding="utf-8")
            with self.assertRaises(StageA0ReportError):
                load_feasibility_report(unrelated)
            missing = Path(directory) / "missing.json"
            with self.assertRaises(StageA0ReportError):
                load_feasibility_report(missing)

    def test_a_non_canonical_byte_layout_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = self._write(self._retained_report(), directory)
            document = json.loads(path.read_text("utf-8"))
            path.write_text(json.dumps(document, indent=4), encoding="utf-8")
            with self.assertRaises(StageA0ReportError):
                load_feasibility_report(path)

    def test_a_fresh_qualified_outcome_requires_retained_evidence(self):
        cells = self._cells()
        with self.assertRaises(StageA0ProtocolError):
            FeasibilityReport(
                provenance=FIXTURE_PROVENANCE,
                instrumentation_provenance=None,
                source_paths_examined=("fixture",),
                retained_corpus_identity=None,
                retained_outcome=None,
                retained_evidence=None,
                smoke_population_identity=protocol.SMOKE_POPULATION_IDENTITY,
                fresh_outcome={"outcome": FRESH_LIVE_LABEL_PATH_QUALIFIED},
                sidecar_identity=None,
                row_count=1,
                cell_count=len(cells),
                availability_counts=availability_counts(cells),
                checks=build_checks(cells, deterministic_recomputation=None),
                hard_outcome=FRESH_LIVE_LABEL_PATH_QUALIFIED,
                pending_reason=None,
                recommended_route="fresh-live-label",
            )

    def test_the_evidence_chain_is_recorded_in_the_fresh_report(self):
        cells = self._cells()
        evidence = {
            "report_identity": "b" * 64,
            "retained_corpus_identity": RETAINED_DATASET_IDENTITY,
            "retained_outcome": RETAINED_AUGMENTATION_NOT_QUALIFIED,
        }
        report = FeasibilityReport(
            provenance=FIXTURE_PROVENANCE,
            instrumentation_provenance=None,
            source_paths_examined=("fixture",),
            retained_corpus_identity=RETAINED_DATASET_IDENTITY,
            retained_outcome=retained_route_document(
                outcome=RETAINED_AUGMENTATION_NOT_QUALIFIED,
                rejection_reason="feature-row-mismatch",
            ),
            retained_evidence=evidence,
            smoke_population_identity=protocol.SMOKE_POPULATION_IDENTITY,
            fresh_outcome={"outcome": FRESH_LIVE_LABEL_PATH_QUALIFIED},
            sidecar_identity=None,
            row_count=1,
            cell_count=len(cells),
            availability_counts=availability_counts(cells),
            checks=build_checks(cells, deterministic_recomputation=None),
            hard_outcome=FRESH_LIVE_LABEL_PATH_QUALIFIED,
            pending_reason=None,
            recommended_route="fresh-live-label",
        )
        document = report.to_document()
        self.assertEqual(document["sources"]["retained_evidence"], evidence)
        with TemporaryDirectory() as directory:
            path = self._write(report, directory)
            self.assertEqual(
                load_feasibility_report(path).document["sources"]["retained_evidence"],
                evidence,
            )


class FreshCommandEvidenceGateTest(unittest.TestCase):
    """CLI の fresh handler が validated retained evidence 以外を受けない。"""

    def _run(self, directory, retained_report):
        from lisjong_arena.stage_a0_tenpai_feasibility import __main__ as cli

        def source(seed):
            actor_seat = Seat(seed % 4)
            plan = uniform_plan(TENPAI_HAND, actor_seat=actor_seat)
            return (observed_decision(plan, actor_seat=actor_seat),)

        original_provenance = cli.provenance_document
        original_qualify = cli.qualify_fresh_live_label
        cli.provenance_document = lambda: dict(FIXTURE_PROVENANCE)
        cli.qualify_fresh_live_label = lambda **kwargs: original_qualify(
            observed_decision_source=source, **kwargs
        )
        self.addCleanup(setattr, cli, "provenance_document", original_provenance)
        self.addCleanup(setattr, cli, "qualify_fresh_live_label", original_qualify)

        arguments = ["fresh-smoke", "--sidecar", str(Path(directory) / "sidecar")]
        if retained_report is not None:
            arguments += ["--retained-report", str(retained_report)]
        arguments += ["--report", str(Path(directory) / "fresh-report.json")]
        cli.main(arguments)
        return load_feasibility_report(Path(directory) / "fresh-report.json")

    def _retained_report(
        self,
        directory,
        *,
        outcome,
        rejection_reason="feature-row-mismatch",
        corpus_identity=RETAINED_DATASET_IDENTITY,
        **over,
    ):
        cells = emit_decision(
            observed_decision(
                uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0),
                actor_seat=Seat.SEAT_0,
            ),
            source_identity="fixture",
            seed=751,
        ).cells
        arguments = {
            "provenance": FIXTURE_PROVENANCE,
            "instrumentation_provenance": None,
            "source_paths_examined": ("fixture",),
            "retained_corpus_identity": corpus_identity,
            "retained_outcome": retained_route_document(
                outcome=outcome,
                rejection_reason=rejection_reason,
                dataset_identity=corpus_identity,
            ),
            "retained_evidence": None,
            "smoke_population_identity": None,
            "fresh_outcome": None,
            "sidecar_identity": None,
            "row_count": 1,
            "cell_count": len(cells),
            "availability_counts": availability_counts(cells),
            "checks": build_checks(cells, deterministic_recomputation=None),
            "hard_outcome": None,
            "pending_reason": "retained route measured",
            "recommended_route": None,
        }
        arguments.update(over)
        path = Path(directory) / "retained-report.json"
        FeasibilityReport(**arguments).write(path)
        return path

    def test_without_retained_evidence_the_fresh_run_stays_pending(self):
        with TemporaryDirectory() as directory:
            report = self._run(directory, None)
            self.assertIsNone(report.hard_outcome)
            self.assertIsNotNone(report.document["outcome"]["pending_reason"])
            self.assertIsNone(report.document["sources"]["retained_evidence"])

    def test_a_not_qualified_retained_report_yields_the_fresh_outcome(self):
        with TemporaryDirectory() as directory:
            retained = self._retained_report(
                directory, outcome=RETAINED_AUGMENTATION_NOT_QUALIFIED
            )
            report = self._run(directory, retained)
            self.assertEqual(report.hard_outcome, FRESH_LIVE_LABEL_PATH_QUALIFIED)
            evidence = report.document["sources"]["retained_evidence"]
            self.assertEqual(
                evidence["retained_outcome"], RETAINED_AUGMENTATION_NOT_QUALIFIED
            )
            self.assertEqual(
                evidence["retained_corpus_identity"], RETAINED_DATASET_IDENTITY
            )

    def test_a_qualified_retained_report_cannot_authorize_the_fresh_outcome(self):
        with TemporaryDirectory() as directory:
            retained = self._retained_report(
                directory,
                outcome=RETAINED_AUGMENTATION_QUALIFIED,
                rejection_reason=None,
                hard_outcome=RETAINED_AUGMENTATION_QUALIFIED,
                pending_reason=None,
                recommended_route="retained-augmentation",
            )
            with self.assertRaises(StageA0ReportError):
                self._run(directory, retained)

    def test_a_precondition_failure_cannot_authorize_the_fresh_outcome(self):
        for reason in (
            "dataset-identity-mismatch",
            "provenance-revision-mismatch",
            "retained-artifact-unreadable",
            "retained-row-missing",
        ):
            with self.subTest(reason=reason), TemporaryDirectory() as directory:
                retained = self._retained_report(
                    directory,
                    outcome=RETAINED_AUGMENTATION_NOT_QUALIFIED,
                    rejection_reason=reason,
                )
                with self.assertRaises(StageA0ReportError):
                    self._run(directory, retained)

    def test_a_foreign_retained_corpus_cannot_authorize_the_fresh_outcome(self):
        with TemporaryDirectory() as directory:
            retained = self._retained_report(
                directory,
                outcome=RETAINED_AUGMENTATION_NOT_QUALIFIED,
                corpus_identity="c" * 64,
            )
            with self.assertRaises(StageA0ReportError):
                self._run(directory, retained)

    def test_an_inconsistent_corpus_identity_is_rejected(self):
        with TemporaryDirectory() as directory:
            retained = self._retained_report(
                directory, outcome=RETAINED_AUGMENTATION_NOT_QUALIFIED
            )
            document = json.loads(retained.read_text("utf-8"))
            document["routes"]["retained_augmentation"]["dataset_identity"] = "d" * 64
            document.pop("report_identity")
            document["report_identity"] = report_identity(document)
            retained.write_text(canonical_json_text(document), encoding="utf-8")
            with self.assertRaises(StageA0ReportError):
                self._run(directory, retained)

    def test_an_unexpected_exception_is_not_converted_to_a_route_outcome(self):
        def failing(identity, concealed_tiles, own_melds):
            raise RuntimeError("programming error")

        original = labels.structural_wait_for_hand
        labels.structural_wait_for_hand = failing
        self.addCleanup(setattr, labels, "structural_wait_for_hand", original)
        with TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                self._run(directory, None)

    def test_a_tampered_retained_report_cannot_authorize_the_fresh_outcome(self):
        with TemporaryDirectory() as directory:
            retained = self._retained_report(
                directory, outcome=RETAINED_AUGMENTATION_NOT_QUALIFIED
            )
            document = json.loads(retained.read_text("utf-8"))
            document["counts"]["row_count"] = 999
            retained.write_text(canonical_json_text(document), encoding="utf-8")
            with self.assertRaises(StageA0ReportError):
                self._run(directory, retained)


class HardOutcomeRoutingTest(unittest.TestCase):
    """post-qualification invariant failure を STOP / INVALID へ写す。"""

    def _cli(self):
        from lisjong_arena.stage_a0_tenpai_feasibility import __main__ as cli

        original = cli.provenance_document
        cli.provenance_document = lambda: dict(FIXTURE_PROVENANCE)
        self.addCleanup(setattr, cli, "provenance_document", original)
        return cli

    def _break_recomputation(self, cli):
        def failing(sidecar):
            raise StageA0SidecarError("cells do not recompute")

        original = cli.verify_deterministic_recomputation
        cli.verify_deterministic_recomputation = failing
        self.addCleanup(setattr, cli, "verify_deterministic_recomputation", original)

    def _break_boundary(self):
        from lisjong_arena.stage_a0_tenpai_feasibility import report as report_module

        def failing(cells):
            return QualificationCheck(
                name="public_private_boundary",
                qualified=False,
                detail="privileged truth reached the public row",
            )

        original = report_module.check_public_private_boundary
        report_module.check_public_private_boundary = failing
        self.addCleanup(
            setattr, report_module, "check_public_private_boundary", original
        )

    def _break_fail_closed(self):
        def failing(identity, concealed_tiles, own_melds):
            raise ValueError("canonical contract drifted")

        original = labels.structural_wait_for_hand
        labels.structural_wait_for_hand = failing
        self.addCleanup(setattr, labels, "structural_wait_for_hand", original)

    # --- retained route --------------------------------------------------

    def _qualified_retained(self, cells):
        from lisjong_arena.stage_a0_tenpai_feasibility.retained import (
            RetainedQualification,
        )

        return RetainedQualification(
            outcome=RETAINED_AUGMENTATION_QUALIFIED,
            dataset_identity=RETAINED_DATASET_IDENTITY,
            examined_seeds=(245,),
            examined_row_count=1,
            aligned_row_count=1,
            instrumentation_provenance=None,
            rejection_reason=None,
            rejection_class=None,
            rejection_detail=None,
            cells=cells,
        )

    def _run_retained(self, cli, directory, cells):
        original = cli.qualify_retained_augmentation
        cli.qualify_retained_augmentation = lambda dataset: self._qualified_retained(
            cells
        )
        self.addCleanup(setattr, cli, "qualify_retained_augmentation", original)
        cli.main(
            [
                "retained-qualify",
                "--dataset",
                str(Path(directory) / "corpus"),
                "--sidecar",
                str(Path(directory) / "sidecar"),
                "--report",
                str(Path(directory) / "retained-report.json"),
            ]
        )
        return load_feasibility_report(Path(directory) / "retained-report.json")

    def _retained_cells(self, source_identity=RETAINED_DATASET_IDENTITY):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        return emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity=source_identity,
            seed=245,
        ).cells

    def test_retained_alignment_qualified_but_recomputation_fails_is_stop_invalid(
        self,
    ):
        cli = self._cli()
        self._break_recomputation(cli)
        with TemporaryDirectory() as directory:
            report = self._run_retained(cli, directory, self._retained_cells())
            self.assertEqual(report.hard_outcome, STOP_INVALID)
            self.assertIsNone(report.document["outcome"]["pending_reason"])
            self.assertIsNone(
                report.document["routes"]["recommended_stage_a0_corpus_route"]
            )
            self.assertFalse(
                report.document["qualifications"]["deterministic_recomputation"][
                    "qualified"
                ]
            )

    def test_retained_alignment_qualified_but_fail_closed_cells_is_stop_invalid(self):
        cli = self._cli()
        self._break_fail_closed()
        with TemporaryDirectory() as directory:
            report = self._run_retained(cli, directory, self._retained_cells())
            self.assertEqual(report.hard_outcome, STOP_INVALID)
            self.assertFalse(
                report.document["qualifications"]["fail_closed"]["qualified"]
            )
            self.assertEqual(
                report.document["counts"]["availability"]["OTHER_FAIL_CLOSED"], 3
            )

    def test_retained_alignment_qualified_but_leakage_is_stop_invalid(self):
        cli = self._cli()
        self._break_boundary()
        with TemporaryDirectory() as directory:
            report = self._run_retained(cli, directory, self._retained_cells())
            self.assertEqual(report.hard_outcome, STOP_INVALID)
            self.assertFalse(
                report.document["qualifications"]["public_private_boundary"][
                    "qualified"
                ]
            )

    def test_a_clean_retained_run_still_qualifies(self):
        cli = self._cli()
        with TemporaryDirectory() as directory:
            report = self._run_retained(cli, directory, self._retained_cells())
            self.assertEqual(report.hard_outcome, RETAINED_AUGMENTATION_QUALIFIED)
            self.assertEqual(
                report.document["routes"]["recommended_stage_a0_corpus_route"],
                "retained-augmentation",
            )

    # --- fresh route -----------------------------------------------------

    def _fresh_source(self, seed):
        actor_seat = Seat(seed % 4)
        plan = uniform_plan(TENPAI_HAND, actor_seat=actor_seat)
        return (observed_decision(plan, actor_seat=actor_seat),)

    def _broken_fresh_source(self, seed):
        decision = self._fresh_source(seed)[0]
        other = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        yield ObservedDecision(
            step_ordinal=decision.step_ordinal,
            decision_ordinal=decision.decision_ordinal,
            actor_seat=decision.actor_seat,
            context=decision.context,
            selected_action=decision.selected_action,
            hidden=hidden_state_for(other, actor_seat=decision.actor_seat),
        )

    def _run_fresh(self, cli, directory, source, retained_report):
        original = cli.qualify_fresh_live_label
        cli.qualify_fresh_live_label = lambda **kwargs: original(
            observed_decision_source=source, **kwargs
        )
        self.addCleanup(setattr, cli, "qualify_fresh_live_label", original)
        arguments = [
            "fresh-smoke",
            "--sidecar",
            str(Path(directory) / "fresh-sidecar"),
        ]
        if retained_report is not None:
            arguments += ["--retained-report", str(retained_report)]
        arguments += ["--report", str(Path(directory) / "fresh-report.json")]
        cli.main(arguments)
        return load_feasibility_report(Path(directory) / "fresh-report.json")

    def _retained_evidence_report(self, directory):
        cells = self._retained_cells(source_identity="fixture")
        path = Path(directory) / "retained-evidence.json"
        FeasibilityReport(
            provenance=FIXTURE_PROVENANCE,
            instrumentation_provenance=None,
            source_paths_examined=("fixture",),
            retained_corpus_identity=RETAINED_DATASET_IDENTITY,
            retained_outcome=retained_route_document(
                outcome=RETAINED_AUGMENTATION_NOT_QUALIFIED,
                rejection_reason="feature-row-mismatch",
            ),
            retained_evidence=None,
            smoke_population_identity=None,
            fresh_outcome=None,
            sidecar_identity=None,
            row_count=1,
            cell_count=len(cells),
            availability_counts=availability_counts(cells),
            checks=build_checks(cells, deterministic_recomputation=None),
            hard_outcome=None,
            pending_reason="retained route disqualified by exact alignment",
            recommended_route=None,
        ).write(path)
        return path

    def test_fresh_qualified_but_recomputation_fails_is_stop_invalid(self):
        cli = self._cli()
        self._break_recomputation(cli)
        with TemporaryDirectory() as directory:
            retained = self._retained_evidence_report(directory)
            report = self._run_fresh(cli, directory, self._fresh_source, retained)
            self.assertEqual(report.hard_outcome, STOP_INVALID)
            self.assertIsNone(report.document["outcome"]["pending_reason"])
            self.assertFalse(
                report.document["qualifications"]["deterministic_recomputation"][
                    "qualified"
                ]
            )

    def test_fresh_qualified_but_leakage_is_stop_invalid(self):
        cli = self._cli()
        self._break_boundary()
        with TemporaryDirectory() as directory:
            retained = self._retained_evidence_report(directory)
            report = self._run_fresh(cli, directory, self._fresh_source, retained)
            self.assertEqual(report.hard_outcome, STOP_INVALID)

    def test_an_expected_fresh_path_failure_with_retained_evidence_is_blocked(self):
        cli = self._cli()
        with TemporaryDirectory() as directory:
            retained = self._retained_evidence_report(directory)
            report = self._run_fresh(
                cli, directory, self._broken_fresh_source, retained
            )
            self.assertEqual(report.hard_outcome, TENPAI_LABEL_PATH_BLOCKED)
            self.assertEqual(
                report.document["routes"]["fresh_live_label"]["rejection_reason"],
                "same-state-co-emission-failed",
            )

    def test_a_stop_invalid_report_round_trips_through_the_strict_reader(self):
        cli = self._cli()
        self._break_recomputation(cli)
        with TemporaryDirectory() as directory:
            report = self._run_retained(cli, directory, self._retained_cells())
            self.assertEqual(report.hard_outcome, STOP_INVALID)
            reloaded = load_feasibility_report(report.path)
            self.assertEqual(reloaded.identity, report.identity)
            self.assertEqual(reloaded.hard_outcome, STOP_INVALID)


class FailClosedContractTest(unittest.TestCase):
    """`OTHER_FAIL_CLOSED` の契約を実装と一致させる。"""

    def _identity(self):
        return labels.opponent_identity(Seat.SEAT_0, 1, Seat.SEAT_0)

    def _build(self):
        return labels.build_opponent_target(
            self._identity(),
            seat_resolved=True,
            public_riichi=RiichiState.NONE,
            privileged_riichi_declared=False,
            concealed_tiles=tiles(TENPAI_HAND),
            melds=(),
        )

    def test_a_canonical_builder_value_error_is_countable_and_blocking(self):
        def failing(identity, concealed_tiles, own_melds):
            raise ValueError("canonical contract drifted")

        original = labels.structural_wait_for_hand
        labels.structural_wait_for_hand = failing
        self.addCleanup(setattr, labels, "structural_wait_for_hand", original)
        target = self._build()
        self.assertIs(target.availability, TargetAvailability.OTHER_FAIL_CLOSED)
        self.assertIsNone(target.tenpai)
        self.assertIsNone(target.wait_mask)
        check = build_checks((), deterministic_recomputation=None)
        self.assertTrue({entry.name for entry in check} >= {"fail_closed"})

    def test_a_non_value_error_is_a_route_level_hard_failure(self):
        def failing(identity, concealed_tiles, own_melds):
            raise RuntimeError("programming error")

        original = labels.structural_wait_for_hand
        labels.structural_wait_for_hand = failing
        self.addCleanup(setattr, labels, "structural_wait_for_hand", original)
        with self.assertRaises(RuntimeError):
            self._build()

    def test_the_fresh_route_does_not_convert_unexpected_exceptions(self):
        def failing(identity, concealed_tiles, own_melds):
            raise RuntimeError("programming error")

        def source(seed):
            actor_seat = Seat(seed % 4)
            plan = uniform_plan(TENPAI_HAND, actor_seat=actor_seat)
            return (observed_decision(plan, actor_seat=actor_seat),)

        original = labels.structural_wait_for_hand
        labels.structural_wait_for_hand = failing
        self.addCleanup(setattr, labels, "structural_wait_for_hand", original)
        with self.assertRaises(RuntimeError):
            qualify_fresh_live_label(observed_decision_source=source)

    def test_a_non_zero_fail_closed_count_blocks_qualification(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        cells = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=751,
        ).cells

        def failing(identity, concealed_tiles, own_melds):
            raise ValueError("canonical contract drifted")

        original = labels.structural_wait_for_hand
        labels.structural_wait_for_hand = failing
        self.addCleanup(setattr, labels, "structural_wait_for_hand", original)
        failed = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=751,
        ).cells
        self.assertTrue(
            all(
                cell.availability is TargetAvailability.OTHER_FAIL_CLOSED
                for cell in failed
            )
        )
        checks = {
            entry.name: entry
            for entry in build_checks(failed, deterministic_recomputation=None)
        }
        self.assertFalse(checks["fail_closed"].qualified)
        healthy = {
            entry.name: entry
            for entry in build_checks(cells, deterministic_recomputation=None)
        }
        self.assertTrue(healthy["fail_closed"].qualified)


class CanonicalSemanticsTest(unittest.TestCase):
    """canonical authorityがlisjong側であることを固定する。"""

    def test_the_exact_wait_builder_identity_is_recorded_and_stable(self):
        identity = protocol.exact_wait_implementation_identity()
        self.assertEqual(len(identity), 64)
        self.assertEqual(identity, protocol.exact_wait_implementation_identity())

    def test_arena_does_not_define_a_second_tenpai_implementation(self):
        self.assertIs(
            structural_wait.exact_hand_belief_with_waits, exact_hand_belief_with_waits
        )
        self.assertEqual(
            protocol.CANONICAL_WAIT_BUILDER, "lisjong.belief.exact_wait_ground_truth"
        )

    def test_the_target_is_the_existential_or_of_the_canonical_wait_mask(self):
        for hand in (TENPAI_HAND, NON_TENPAI_HAND):
            identity = labels.opponent_identity(Seat.SEAT_0, 1, Seat.SEAT_0)
            target = labels.build_opponent_target(
                identity,
                seat_resolved=True,
                public_riichi=RiichiState.NONE,
                privileged_riichi_declared=False,
                concealed_tiles=tiles(hand),
                melds=(),
            )
            self.assertEqual(target.tenpai, 1 if any(target.wait_mask) else 0)

    def test_the_flat_bc_contract_identity_matches_the_retained_corpus(self):
        self.assertEqual(protocol.GAME_MODE, offline_q.GAME_MODE)
        self.assertEqual(protocol.TEACHER_IDENTITY, offline_q.TEACHER_IDENTITY)
        self.assertEqual(
            protocol.TEACHER_SOURCE_REVISION, offline_q.TEACHER_SOURCE_REVISION
        )
        self.assertEqual(protocol.FEATURE_DIMENSION, offline_q.FEATURE_DIMENSION)
        self.assertEqual(protocol.VOCABULARY_SIZE, offline_q.VOCABULARY_SIZE)
        self.assertIs(
            protocol.verify_contract_identity, offline_q.verify_contract_identity
        )


class PublicRowDigestTest(unittest.TestCase):
    def test_the_digest_covers_the_feature_and_mask_payload(self):
        plan = uniform_plan(TENPAI_HAND, actor_seat=Seat.SEAT_0)
        emission = emit_decision(
            observed_decision(plan, actor_seat=Seat.SEAT_0),
            source_identity="fixture",
            seed=1,
        )
        row = emission.row
        self.assertEqual(
            len(array("f", row.feature_values).tobytes()),
            protocol.FEATURE_DIMENSION * 4,
        )
        mutated = public_row.PublicDecisionRow(
            identity=row.identity,
            split=row.split,
            round_wind=row.round_wind,
            hand_number=row.hand_number,
            honba=row.honba,
            feature_values=(row.feature_values[0] + 1.0,) + row.feature_values[1:],
            legal_mask=row.legal_mask,
            teacher_action_index=row.teacher_action_index,
            teacher_action_family=row.teacher_action_family,
        )
        self.assertNotEqual(row.row_digest(), mutated.row_digest())


if __name__ == "__main__":
    unittest.main()
