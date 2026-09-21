import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lisjong_arena.wait_shape_qualification.pilot import (
    EXPECTED_PROTOCOL_LOCK_IDENTITY,
    F1_NOT_QUALIFIED,
    F1_QUALIFIED,
    F2_QUALIFIED,
    FINAL_QUALIFIED,
    LoadedPilotRaw,
    WaitShapePilotError,
    _validate_observation,
    build_execution_lock,
    qualification_document,
    summarize_f1,
    summarize_f2,
    write_raw_artifact,
)
from lisjong_arena.wait_shape_qualification.protocol import PILOT_SEEDS


def observation(index: int, *, projection=None, ordinary=True):
    seed = PILOT_SEEDS[index % len(PILOT_SEEDS)]
    selected = index % 20
    if projection is None:
        active = index % 5
        projection = {
            "TANKI": int(active == 0),
            "SHANPON": int(active == 1),
            "KANCHAN": int(active == 2),
            "PENCHAN": int(active == 3),
            "RYANMEN": int(active == 4),
            "KOKUSHI": 0,
        }
    return {
        "seed": seed,
        "round_wind": "E",
        "hand_number": 1,
        "honba": index,
        "step_ordinal": index,
        "decision_ordinal": index,
        "actor_seat": 0,
        "teacher_action_index": selected,
        "ordinary_discard_choice": ordinary,
        "legal_discard_indices": [selected, (selected + 1) % 20] if ordinary else [selected],
        "accepted_opponents": [
            {
                "opponent_seat": 1,
                "availability": "AVAILABLE",
                "projection": projection,
            }
        ],
    }


def raw_with(count=300):
    return LoadedPilotRaw(
        path=Path("."),
        manifest={"raw_identity": "a" * 64},
        observations=tuple(observation(index) for index in range(count)),
    )


def fake_execution():
    return {
        "execution_target": {
            "branch": "main",
            "revision": "1" * 40,
            "target_type": "reviewed-merged-main-v1",
        },
        "provenance": {
            "execution_environment": "riichienv",
            "lisjong_arena_version": "0.1.0",
            "lisjong_arena_revision": "1" * 40,
            "lisjong_version": "0.1.0",
            "lisjong_revision": "15799e5f0fe47f2e2b2c39060de804d99c51492d",
            "lisjong_engine_version": "0.1.0",
            "lisjong_engine_revision": "8735e89e1aea000ab59368d0368d476787827741",
            "riichienv_version": "0.4.10",
            "python_version": "3.14.6",
        },
        "runtime": {
            "python_implementation": "CPython",
            "python_version": "3.14.6",
            "sys_version": "3.14.6",
        },
    }


class WaitShapePilotTest(unittest.TestCase):
    def test_synthetic_locked_support_passes_f1_f2(self):
        raw = raw_with()
        f1 = summarize_f1(raw)
        f2 = summarize_f2(raw)
        self.assertEqual(f1["outcome"], F1_QUALIFIED)
        self.assertEqual(f2["outcome"], F2_QUALIFIED)
        self.assertEqual(
            f2["summary"]["riichi_exposed_discard_choice_decisions"], 300
        )
        self.assertGreaterEqual(f2["summary"]["unique_source_hanchan"], 48)
        self.assertGreaterEqual(f2["summary"]["unique_accepted_riichi_episodes"], 80)
        self.assertEqual(
            f2["summary"]["unique_selected_discard_vocabulary_indices"], 20
        )
        self.assertLessEqual(
            f2["summary"]["largest_selected_discard_index_share"], 0.20
        )

    def test_weak_shape_cannot_be_dropped_after_inspection(self):
        records = []
        for index in range(300):
            projection = {
                "TANKI": 0,
                "SHANPON": int(index % 4 == 0),
                "KANCHAN": int(index % 4 == 1),
                "PENCHAN": int(index % 4 == 2),
                "RYANMEN": int(index % 4 == 3),
                "KOKUSHI": 0,
            }
            records.append(observation(index, projection=projection))
        raw = LoadedPilotRaw(
            path=Path("."),
            manifest={"raw_identity": "b" * 64},
            observations=tuple(records),
        )
        f1 = summarize_f1(raw)
        self.assertEqual(f1["outcome"], F1_NOT_QUALIFIED)
        self.assertFalse(f1["summary"]["shapes"]["TANKI"]["qualified"])
        self.assertEqual(
            set(f1["summary"]["shapes"]),
            {"TANKI", "SHANPON", "KANCHAN", "PENCHAN", "RYANMEN"},
        )

    def test_unavailable_accepted_cell_is_stop_invalid(self):
        record = observation(0)
        record["accepted_opponents"][0] = {
            "opponent_seat": 1,
            "availability": "NO_STRUCTURAL_WAIT",
            "projection": None,
        }
        raw = LoadedPilotRaw(
            path=Path("."),
            manifest={"raw_identity": "c" * 64},
            observations=(record,),
        )
        f1 = summarize_f1(raw)
        self.assertEqual(f1["outcome"], "STOP / INVALID")
        self.assertEqual(
            f1["summary"]["unexpected_fail_closed_or_semantic_error_count"], 1
        )
        self.assertEqual(f1["summary"]["all_six_channel_zero_anchors"], 1)

    def test_final_route_requires_both_f1_and_f2(self):
        raw = raw_with()
        f1 = summarize_f1(raw)
        f2 = summarize_f2(raw)
        lock = {"lock_identity": "d" * 64}
        result = qualification_document(raw, f1, f2, lock)
        self.assertEqual(result["outcome"], FINAL_QUALIFIED)
        self.assertFalse(result["scientific_population_generated"])
        self.assertFalse(result["am_training_performed"])

    def test_raw_observation_rejects_scientific_seed_and_invalid_projection(self):
        bad_seed = observation(0)
        bad_seed["seed"] = 2100
        with self.assertRaises(WaitShapePilotError):
            _validate_observation(bad_seed, "record")

        all_zero = observation(0)
        all_zero["accepted_opponents"][0]["projection"] = {
            "TANKI": 0,
            "SHANPON": 0,
            "KANCHAN": 0,
            "PENCHAN": 0,
            "RYANMEN": 0,
            "KOKUSHI": 0,
        }
        with self.assertRaises(WaitShapePilotError):
            _validate_observation(all_zero, "record")

    def test_ordinary_discard_choice_requires_two_discard_candidates_and_selected_discard(self):
        fewer = observation(0)
        fewer["legal_discard_indices"] = [0]
        with self.assertRaises(WaitShapePilotError):
            _validate_observation(fewer, "record")

        non_discard_selected = observation(0)
        non_discard_selected["teacher_action_index"] = 50
        with self.assertRaises(WaitShapePilotError):
            _validate_observation(non_discard_selected, "record")

    def test_execution_lock_binds_preexposure_gates_and_exact_protocol(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch(
                "lisjong_arena.wait_shape_qualification.pilot._verify_locked_environment",
                return_value=fake_execution(),
            ):
                lock = build_execution_lock(
                    temp,
                    max_workers=2,
                    repository_collision_audit_pass=True,
                    private_collision_audit_pass=True,
                    no_prior_result_exposure_confirmed=True,
                )
        self.assertEqual(
            lock["protocol_lock_identity"], EXPECTED_PROTOCOL_LOCK_IDENTITY
        )
        self.assertEqual(lock["pilot"]["ordered_seeds"], list(PILOT_SEEDS))
        self.assertEqual(
            lock["pilot"]["teacher_identity"],
            "targeted-honor-release-terminal-progression",
        )
        self.assertFalse(lock["result_exposed"])
        self.assertTrue(lock["pilot"]["scientific_reuse_forbidden"])

    def test_execution_lock_requires_all_three_external_preexposure_gates(self):
        with tempfile.TemporaryDirectory() as temp:
            for field in (
                "repository_collision_audit_pass",
                "private_collision_audit_pass",
                "no_prior_result_exposure_confirmed",
            ):
                kwargs = {
                    "repository_collision_audit_pass": True,
                    "private_collision_audit_pass": True,
                    "no_prior_result_exposure_confirmed": True,
                }
                kwargs[field] = False
                with self.subTest(field=field), self.assertRaises(
                    WaitShapePilotError
                ):
                    build_execution_lock(temp, max_workers=1, **kwargs)

    def test_raw_artifact_is_write_once_and_strict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch(
                "lisjong_arena.wait_shape_qualification.pilot._verify_locked_environment",
                return_value=fake_execution(),
            ):
                lock = build_execution_lock(
                    root / "unused",
                    max_workers=1,
                    repository_collision_audit_pass=True,
                    private_collision_audit_pass=True,
                    no_prior_result_exposure_confirmed=True,
                )
            raw_path = root / "raw"
            loaded = write_raw_artifact(
                raw_path,
                observations=(observation(0), observation(1)),
                lock=lock,
            )
            self.assertEqual(len(loaded.observations), 2)
            with self.assertRaises(FileExistsError):
                write_raw_artifact(
                    raw_path,
                    observations=(observation(0),),
                    lock=lock,
                )

    def test_result_identity_changes_when_summary_is_tampered(self):
        f1 = summarize_f1(raw_with())
        tampered = copy.deepcopy(f1)
        tampered["summary"]["accepted_riichi_opponent_cells"] += 1
        self.assertNotEqual(tampered, f1)
        from lisjong_arena.wait_shape_qualification.pilot import _document_identity

        self.assertNotEqual(
            tampered["result_identity"],
            _document_identity(tampered, "result_identity"),
        )


if __name__ == "__main__":
    unittest.main()
