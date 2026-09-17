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
    StageA0SidecarError,
)
from lisjong_arena.stage_a0_tenpai_feasibility.fresh import (
    FRESH_LIVE_LABEL_PATH_QUALIFIED,
    qualify_fresh_live_label,
)
from lisjong_arena.stage_a0_tenpai_feasibility.labels import TargetAvailability
from lisjong_arena.stage_a0_tenpai_feasibility.report import (
    FeasibilityReport,
    QualificationCheck,
    build_checks,
)
from lisjong_arena.stage_a0_tenpai_feasibility.retained import (
    RETAINED_AUGMENTATION_NOT_QUALIFIED,
    RETAINED_AUGMENTATION_QUALIFIED,
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

    def test_a_provenance_revision_mismatch_is_rejected(self):
        drifted = dict(RETAINED_PROVENANCE)
        drifted["lisjong_arena_revision"] = "9" * 40
        result = self._qualify(current_provenance=drifted)
        self.assertEqual(result.outcome, RETAINED_AUGMENTATION_NOT_QUALIFIED)
        self.assertEqual(result.rejection_reason, "provenance-revision-mismatch")
        self.assertIn("lisjong_arena_revision", result.rejection_detail)

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
            "source_paths_examined": ("fixture",),
            "retained_corpus_identity": None,
            "retained_outcome": None,
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
