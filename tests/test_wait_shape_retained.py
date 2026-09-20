import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from _stage_a0_tenpai_fixtures import (
    INVALID_INVENTORY_HAND,
    NON_TENPAI_HAND,
    RETAINED_PROVENANCE,
    TENPAI_HAND,
    observed_decision,
    uniform_plan,
)
from lisjong.policy_contract import Seat
from lisjong.policy_contract.riichi import RiichiState

from lisjong_arena.stage_a0_tenpai_feasibility.emission import emit_decision
from lisjong_arena.stage_a0_tenpai_feasibility.errors import StageA0SidecarError
from lisjong_arena.stage_a0_tenpai_feasibility.protocol import RETAINED_DATASET_IDENTITY
from lisjong_arena.stage_a0_tenpai_feasibility.sidecar import (
    ROUTE_FRESH,
    ROUTE_RETAINED,
    write_sidecar,
)
from lisjong_arena.wait_shape_qualification.__main__ import main
from lisjong_arena.wait_shape_qualification.labels import (
    WaitShapeAvailability as A,
)
from lisjong_arena.wait_shape_qualification.labels import (
    WaitShapeQualificationError,
    WaitShapeTarget,
)
from lisjong_arena.wait_shape_qualification.protocol import (
    LISJONG_ENGINE_REVISION,
    TEACHER_LISJONG_REVISION,
)
from lisjong_arena.wait_shape_qualification.retained import (
    NOT_QUALIFIED,
    QUALIFIED,
    audit_retained_sidecar,
    load_f0_report,
    write_f0_report,
)

EXECUTION = {
    **RETAINED_PROVENANCE,
    "lisjong_revision": TEACHER_LISJONG_REVISION,
    "lisjong_engine_revision": LISJONG_ENGINE_REVISION,
}


def cells(hand=TENPAI_HAND, state=RiichiState.ACCEPTED, seed=245):
    plan = uniform_plan(
        hand,
        actor_seat=Seat.SEAT_0,
        riichi={Seat.SEAT_1: state, Seat.SEAT_2: state, Seat.SEAT_3: state},
    )
    return emit_decision(
        observed_decision(plan, actor_seat=Seat.SEAT_0),
        source_identity=RETAINED_DATASET_IDENTITY,
        seed=seed,
    ).cells


class RetainedAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provenance = patch(
            "lisjong_arena.wait_shape_qualification.retained.provenance_document",
            return_value=EXECUTION,
        )
        self.provenance.start()
        self.addCleanup(self.provenance.stop)

    def sidecar(self, records, route=ROUTE_RETAINED):
        path = self.root / f"sidecar-{len(list(self.root.iterdir()))}"
        write_sidecar(
            path,
            records,
            route=route,
            source_identity=RETAINED_DATASET_IDENTITY,
            provenance=RETAINED_PROVENANCE,
        )
        return path

    def test_real_canonical_labels_and_immutable_roundtrip_cli(self):
        path = self.sidecar(cells())
        original = (path / "cells.jsonl").read_bytes()
        report = audit_retained_sidecar(path)
        self.assertEqual(report["outcome"], QUALIFIED)
        self.assertEqual(report["counts"]["canonical_labels_produced"], 3)
        self.assertEqual(report["counts"]["same_state_cells"], 3)
        self.assertEqual(report["counts"]["retained_riichi_excluded_cells_examined"], 3)
        self.assertEqual(report["source"]["provenance"], RETAINED_PROVENANCE)
        self.assertEqual(
            report["execution"]["lisjong_revision"], TEACHER_LISJONG_REVISION
        )
        output = self.root / "report.json"
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--sidecar", str(path), "--output", str(output)]), 0)
        self.assertEqual(stdout.getvalue().strip(), QUALIFIED)
        self.assertEqual(load_f0_report(output), report)
        with self.assertRaises(FileExistsError):
            write_f0_report(output, report)
        self.assertEqual((path / "cells.jsonl").read_bytes(), original)

    def test_kokushi_only_qualifies(self):
        report = audit_retained_sidecar(
            self.sidecar(cells("1m 9m 1p 9p 1s 9s E S W N P F C"))
        )
        self.assertEqual(report["outcome"], QUALIFIED)
        self.assertEqual(
            report["counts"]["ordinary_five_all_zero_kokushi_positive_count"], 3
        )
        self.assertEqual(report["counts"]["kokushi_descriptive_count"], 3)
        self.assertEqual(report["counts"]["all_six_channel_zero_count"], 0)

    def test_multi_positive_real_hand(self):
        report = audit_retained_sidecar(
            self.sidecar(cells("1m 1m 1m 2m 3m 4m 5m 6m 7m 8m 9m 9m 9m"))
        )
        self.assertEqual(report["counts"]["multi_positive_count"], 3)

    def test_empty_and_non_riichi_are_not_qualified(self):
        for records in ((), cells(state=RiichiState.NONE)):
            with self.subTest(records=len(records)):
                report = audit_retained_sidecar(self.sidecar(records))
                self.assertEqual(report["outcome"], NOT_QUALIFIED)
                self.assertEqual(
                    report["counts"]["retained_riichi_excluded_cells_examined"], 0
                )

    def test_unavailable_reason_counts(self):
        cases = (
            (cells(state=RiichiState.DECLARED), A.NOT_ACCEPTED_RIICHI),
            (
                tuple(replace(c, concealed_tiles=None) for c in cells()),
                A.HIDDEN_HAND_UNAVAILABLE,
            ),
            (tuple(replace(c, melds=None) for c in cells()), A.MELD_STATE_UNAVAILABLE),
            (cells(INVALID_INVENTORY_HAND), A.INVALID_PHYSICAL_INVENTORY),
            (cells(TENPAI_HAND + " N"), A.NOT_STABLE_13_EQUIVALENT),
            (cells(NON_TENPAI_HAND), A.NO_STRUCTURAL_WAIT),
            (
                tuple(replace(c, privileged_riichi_declared=False) for c in cells()),
                A.RIICHI_BINDING_MISMATCH,
            ),
        )
        for records, reason in cases:
            with self.subTest(reason=reason):
                report = audit_retained_sidecar(self.sidecar(records))
                self.assertEqual(report["outcome"], NOT_QUALIFIED)
                self.assertEqual(report["unavailable_reason_counts"][reason.value], 3)
                self.assertEqual(
                    report["counts"]["all_six_channel_zero_count"],
                    3 if reason is A.NO_STRUCTURAL_WAIT else 0,
                )

    def test_failure_gates_override_success(self):
        path = self.sidecar(cells())
        from lisjong_arena.wait_shape_qualification.labels import (
            build_wait_shape_target_from_retained_cell,
        )

        success = build_wait_shape_target_from_retained_cell(cells()[0])
        for reason in (A.CANONICAL_BUILDER_FAILURE, A.INVALID_SEMANTIC_PROJECTION):
            with (
                self.subTest(reason=reason),
                patch(
                    "lisjong_arena.wait_shape_qualification.retained.build_wait_shape_target_from_retained_cell",
                    side_effect=[success, WaitShapeTarget(reason, None), success],
                ),
            ):
                report = audit_retained_sidecar(path)
                self.assertEqual(report["outcome"], NOT_QUALIFIED)
                self.assertEqual(report["counts"]["canonical_labels_produced"], 2)
        mixed = cells()
        report = audit_retained_sidecar(
            self.sidecar(
                (replace(mixed[0], privileged_riichi_declared=False), *mixed[1:])
            )
        )
        self.assertEqual(report["outcome"], NOT_QUALIFIED)
        self.assertEqual(report["counts"]["same_state_binding_failures"], 1)

    def test_malformed_incompatible_sidecars_fail_without_output(self):
        base = cells()
        cases = (
            base[:2],
            (*base, base[0]),
            (replace(base[0], public_row_digest="f" * 64), *base[1:]),
            cells(seed=2000),
            cells(seed=271),
        )
        for records in cases:
            with self.subTest(records=len(records)):
                with self.assertRaises(WaitShapeQualificationError):
                    audit_retained_sidecar(self.sidecar(records))
        with self.assertRaises(WaitShapeQualificationError):
            audit_retained_sidecar(self.sidecar(base, route=ROUTE_FRESH))
        path = self.sidecar(base)
        (path / "cells.jsonl").write_text("{}\n")
        output = self.root / "missing.json"
        with self.assertRaises(StageA0SidecarError):
            main(["--sidecar", str(path), "--output", str(output)])
        self.assertFalse(output.exists())

    def test_runtime_drift_fails_before_builder(self):
        path = self.sidecar(cells())
        with (
            patch(
                "lisjong_arena.wait_shape_qualification.retained.provenance_document",
                return_value={**EXECUTION, "lisjong_revision": "f" * 40},
            ),
            patch(
                "lisjong_arena.wait_shape_qualification.retained.build_wait_shape_target_from_retained_cell"
            ) as builder,
        ):
            with self.assertRaises(WaitShapeQualificationError):
                audit_retained_sidecar(path)
            builder.assert_not_called()

    def test_expected_unavailable_cells_do_not_add_unlocked_pass_gates(self):
        base = cells()
        report = audit_retained_sidecar(
            self.sidecar(
                (
                    replace(base[0], concealed_tiles=None),
                    *base[1:],
                )
            )
        )
        self.assertEqual(report["outcome"], QUALIFIED)
        self.assertEqual(report["counts"]["same_state_cells"], 2)
        self.assertEqual(
            report["unavailable_reason_counts"][A.HIDDEN_HAND_UNAVAILABLE.value], 1
        )

    def test_unexpected_exception_does_not_emit_partial_report(self):
        path = self.sidecar(cells())
        output = self.root / "partial.json"
        with patch(
            "lisjong_arena.wait_shape_qualification.labels.exact_hand_belief_with_waits",
            side_effect=RuntimeError("unexpected"),
        ):
            with self.assertRaises(RuntimeError):
                main(["--sidecar", str(path), "--output", str(output)])
        self.assertFalse(output.exists())

    def test_strict_report_rejects_tampering(self):
        report = audit_retained_sidecar(self.sidecar(cells()))
        changes = (
            ("schema_version", "unknown"),
            ("outcome", NOT_QUALIFIED),
            ("extra", 1),
            ("counts", {**report["counts"], "canonical_labels_produced": True}),
            ("counts", {**report["counts"], "same_state_cells": 0}),
            ("unavailable_reason_counts", {}),
            ("identity", {**report["identity"], "protocol_lock_identity": "0" * 64}),
        )
        for key, value in changes:
            bad = copy.deepcopy(report)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(WaitShapeQualificationError):
                write_f0_report(self.root / "bad.json", bad)
            tampered = self.root / "tampered.json"
            tampered.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(WaitShapeQualificationError):
                load_f0_report(tampered)
        self.assertFalse((self.root / "bad.json").exists())


if __name__ == "__main__":
    unittest.main()
