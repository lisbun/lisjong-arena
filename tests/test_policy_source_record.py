"""Contract tests for the teacher-selectable policy source record (#442)."""

import copy
import functools
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lisjong.policies import MinimalPolicy
from lisjong.policy_contract import (
    DecisionContext,
    DecisionTraceRecorder,
    execute_policy_with_trace,
)
from lisjong.policy_contract.action import PassAction, PonAction

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.model import PolicySpec
from lisjong_arena.offense_foundation.fixtures import OTHER, S, hand, policy_input
from lisjong_arena.offense_foundation.fixtures import probes as offense_probes
from lisjong_arena.offense_foundation.qualification import seal
from lisjong_arena.policy_source_record import (
    DEVELOPMENT,
    SCIENTIFIC,
    PolicySourceRecordError,
    binding,
    generation,
    record,
    replay,
)
from lisjong_arena.policy_source_record.__main__ import main
from lisjong_arena.riichienv.local_game_runner import SeatDecisionObservation

C0 = "placement-aware-speed-call"
TWO_STEP = "two-step"

RUNTIME = {
    "arena_revision": "a" * 40,
    "dependencies": {"lisjong": "b" * 40, "lisjong-engine": "c" * 40},
    "lisjong_source_digest": "d" * 64,
    "shanten_backend": {"name": "python", "native": None},
    "python": "3.14.0",
    "riichienv": "0.4.10",
}


def yakuhai_pon_context():
    """C0 pons the yakuhai pair here; TwoStepUkeire passes."""
    return DecisionContext(
        policy_input("13m456p78s99p11z55z"),
        (PonAction(S, OTHER, hand("5z")[0], hand("55z")), PassAction(S)),
    )


_SLOW_PROBES = frozenset(
    {"minimum_shanten", "maximum_current_ukeire", "tenpai_stable_tie"}
)
"""Probes whose C0 evaluation is slow on the Python shanten backend."""


def contexts():
    return (
        *(
            probe.context
            for probe in offense_probes()
            if probe.name not in _SLOW_PROBES
        ),
        yakuhai_pon_context(),
    )


def observe(teacher, context):
    recorder = DecisionTraceRecorder()
    policy = binding.resolve_teacher(teacher).factory()
    execute_policy_with_trace(policy, context, recorder)
    return SeatDecisionObservation(
        context.input.self_seat, context.input, recorder.snapshot()[0]
    )


@functools.cache
def observations_for(teacher):
    return tuple(observe(teacher, context) for context in contexts())


def fake_game(teacher, seed):
    """Stand-in for one hanchan: one step per fixed context, executed by the teacher."""
    observations = observations_for(teacher)
    inspection = SimpleNamespace(
        step_observations=tuple(
            SimpleNamespace(step_ordinal=index, seat_decisions=(observation,))
            for index, observation in enumerate(observations)
        )
    )
    result = SimpleNamespace(
        seed=seed,
        game_mode=record.GAME_MODE,
        decisions=len(observations),
        steps=len(observations),
    )
    return result, inspection


def development_population():
    return record.population_document(
        DEVELOPMENT, {"TRAIN": [11, 12], "SELECT": [13], "OFFLINE-EVAL": [14]}
    )


def scientific_population():
    populations = {"TRAIN": [21, 22], "SELECT": [23]}
    bindings = {
        split: {
            "allocation_identity": str(index) * 64,
            "ledger_revision": "9" * 64,
            "owner_repository": seed_registry.OWNER_REPOSITORY,
            "seed_domain": seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
            "seed_membership_identity": seed_registry.seed_membership_identity(seeds),
        }
        for index, (split, seeds) in enumerate(populations.items(), 1)
    }
    return record.population_document(SCIENTIFIC, populations, bindings)


class PolicySourceRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = patch.object(binding, "runtime_binding", return_value=RUNTIME)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def generate(self, teacher=C0, *, name="source", recorded_teacher=None):
        recorded = teacher if recorded_teacher is None else recorded_teacher
        with patch.object(
            generation,
            "run_recorded_game",
            side_effect=lambda _teacher, seed: fake_game(recorded, seed),
        ):
            manifest = generation.generate(
                development_population(), self.root / name, teacher=teacher
            )
        return manifest, self.root / name

    def selected_actions(self, path):
        manifest = record.read_source_record(path)
        game = manifest["games"][0]
        return [
            selected for _, _, _, selected in record.iter_rows(path / "game-000", game)
        ]

    # --- population -------------------------------------------------------

    def test_population_validation_fails_closed(self):
        valid = development_population()
        cases = {
            "binding on development": {
                **valid,
                "allocation_bindings": scientific_population()["allocation_bindings"],
            },
            "overlapping splits": {
                **valid,
                "populations": {"TRAIN": [1], "SELECT": [1]},
            },
            "duplicate seed": {**valid, "populations": {"TRAIN": [1, 1]}},
            "unknown split": {**valid, "populations": {"VALID": [1]}},
            "empty split": {**valid, "populations": {"TRAIN": []}},
            "unknown purpose": {**valid, "purpose": "PILOT"},
            "unknown schema": {**valid, "schema": "other"},
            "scientific without bindings": {**valid, "purpose": SCIENTIFIC},
        }
        for name, document in cases.items():
            with self.subTest(name), self.assertRaises(PolicySourceRecordError):
                record.validate_population(document)
        record.validate_population(scientific_population())

    def test_scientific_binding_membership_must_match_split_seeds(self):
        population = copy.deepcopy(scientific_population())
        population["populations"]["TRAIN"] = [21, 99]
        with self.assertRaises(PolicySourceRecordError):
            record.validate_population(population)

    def test_scientific_generation_requires_ledger_authority(self):
        with self.assertRaisesRegex(PolicySourceRecordError, "requires the ledger"):
            generation.generate(scientific_population(), self.root / "s", teacher=C0)
        with self.assertRaisesRegex(PolicySourceRecordError, "not authorized"):
            generation.generate(
                scientific_population(),
                self.root / "s",
                teacher=C0,
                ledger=seed_registry.new_ledger(),
            )
        self.assertFalse((self.root / "s").exists())

    # --- generation / readback -------------------------------------------

    def test_roundtrip_binds_teacher_population_and_rows(self):
        manifest, path = self.generate()
        self.assertEqual(record.read_source_record(path), manifest)
        self.assertEqual(manifest["schema"], record.SCHEMA)
        self.assertEqual(manifest["purpose"], DEVELOPMENT)
        self.assertIsNone(manifest["allocation_bindings"])
        contract = manifest["source_contract"]
        self.assertEqual(
            contract["teacher"]["policy_class"],
            "lisjong.policies.placement_aware_speed_call.PlacementAwareSpeedCallPolicy",
        )
        self.assertEqual(contract["seat_policies"], [C0] * 4)
        self.assertEqual(contract["runtime"], RUNTIME)
        self.assertEqual(len(manifest["games"]), 4)
        self.assertEqual(
            [(g["split"], g["seed"]) for g in manifest["games"]],
            [("TRAIN", 11), ("TRAIN", 12), ("SELECT", 13), ("OFFLINE-EVAL", 14)],
        )
        row = json.loads(
            (path / "game-000" / record.SOURCE_FILENAME).read_text().splitlines()[0]
        )
        self.assertEqual(
            set(row),
            {
                "game_ordinal",
                "seed",
                "split",
                "step_ordinal",
                "decision_ordinal",
                "actor_seat",
                "policy_input",
                "legal_actions",
                "teacher_selected_action",
            },
        )

    def test_teacher_selection_changes_recorded_actions(self):
        c0_manifest, c0_path = self.generate(C0, name="c0")
        two_manifest, two_path = self.generate(TWO_STEP, name="two")
        self.assertNotEqual(
            c0_manifest["source_contract"]["teacher"],
            two_manifest["source_contract"]["teacher"],
        )
        c0_actions = self.selected_actions(c0_path)
        two_actions = self.selected_actions(two_path)
        self.assertIsInstance(c0_actions[-1], PonAction)
        self.assertIsInstance(two_actions[-1], PassAction)
        self.assertEqual(c0_actions[:-1], two_actions[:-1])

    def test_existing_destination_is_never_overwritten(self):
        self.generate()
        with self.assertRaises(FileExistsError):
            self.generate()

    def test_tampering_is_rejected(self):
        _, path = self.generate()
        payload = path / "game-001" / record.SOURCE_FILENAME
        original = payload.read_text()
        lines = original.splitlines(keepends=True)
        payload.write_text("".join(lines[:-1]))
        with self.assertRaisesRegex(PolicySourceRecordError, "checksum"):
            record.read_source_record(path)
        payload.write_text(original)
        record.read_source_record(path)

        (path / "extra.json").write_text("{}")
        with self.assertRaisesRegex(PolicySourceRecordError, "unexpected"):
            record.read_source_record(path)

    def test_resealed_manifest_mismatches_are_rejected(self):
        _, path = self.generate()
        manifest_path = path / "manifest.json"
        original = json.loads(manifest_path.read_text())

        def body(value):
            return {k: v for k, v in value.items() if k != "identity"}

        cases = {
            "unknown schema": lambda m: m.update(
                schema="arena-policy-source-record-v0"
            ),
            "seat policies": lambda m: m["source_contract"].update(
                seat_policies=[C0, C0, C0, TWO_STEP]
            ),
            "split seed": lambda m: m["populations"].update(TRAIN=[11, 99]),
            "duplicate seed": lambda m: m["populations"].update(SELECT=[11]),
            "claimed binding": lambda m: m.update(
                allocation_bindings=scientific_population()["allocation_bindings"]
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                tampered = copy.deepcopy(body(original))
                mutate(tampered)
                manifest_path.unlink()
                manifest_path.write_text(
                    canonical_json_text(seal(tampered)), encoding="utf-8"
                )
                with self.assertRaises(PolicySourceRecordError):
                    record.read_source_record(path)
        manifest_path.unlink()
        manifest_path.write_text(canonical_json_text(original))
        record.read_source_record(path)

    def test_expected_population_must_match(self):
        _, path = self.generate()
        other = record.population_document(DEVELOPMENT, {"TRAIN": [11, 12, 13, 14]})
        with self.assertRaisesRegex(PolicySourceRecordError, "expected population"):
            record.read_source_record(path, expected_population=other)

    # --- replay -----------------------------------------------------------

    def test_replay_matches_every_decision(self):
        manifest, path = self.generate()
        summary = replay.replay_verify(path)
        self.assertEqual(summary["mismatches"], 0)
        self.assertEqual(
            summary["decisions"], sum(g["decision_count"] for g in manifest["games"])
        )

    def test_replay_detects_a_teacher_that_was_only_renamed(self):
        _, path = self.generate(C0, recorded_teacher=TWO_STEP)
        with self.assertRaisesRegex(PolicySourceRecordError, "replay mismatch: 4 of"):
            replay.replay_verify(path)

    def test_replay_requires_the_bound_runtime(self):
        _, path = self.generate()
        manifest = record.read_source_record(path)
        teacher = binding.teacher_identity(C0)
        for field in binding.REPLAY_RUNTIME_FIELDS:
            with self.subTest(field):
                runtime = {**RUNTIME, field: "changed"}
                with self.assertRaisesRegex(PolicySourceRecordError, field):
                    replay.require_replay_runtime(manifest, teacher, runtime)
        replay.require_replay_runtime(
            manifest, teacher, {**RUNTIME, "arena_revision": "e" * 40}
        )
        with self.assertRaisesRegex(PolicySourceRecordError, "teacher"):
            replay.require_replay_runtime(
                manifest, binding.teacher_identity(TWO_STEP), RUNTIME
            )

    # --- CLI --------------------------------------------------------------

    def test_cli_population_readback_and_failure(self):
        population = self.root / "population.json"
        self.assertEqual(
            main(
                [
                    "population",
                    "--train",
                    "100..101",
                    "--offline-eval",
                    "102",
                    "--output",
                    str(population),
                ]
            ),
            0,
        )
        document = record.load_population(population)
        self.assertEqual(
            document["populations"], {"TRAIN": [100, 101], "OFFLINE-EVAL": [102]}
        )
        _, path = self.generate()
        self.assertEqual(main(["readback", "--source", str(path)]), 0)
        (path / "game-000" / record.SOURCE_FILENAME).write_text("")
        self.assertEqual(main(["readback", "--source", str(path)]), 1)


class TeacherIdentityTest(unittest.TestCase):
    def test_unknown_teacher_is_rejected(self):
        with self.assertRaises(PolicySourceRecordError):
            binding.resolve_teacher("C0")
        with self.assertRaises(PolicySourceRecordError):
            binding.resolve_teacher(
                C0, {C0: PolicySpec(identity="other", factory=MinimalPolicy)}
            )

    def test_teacher_identity_names_class_and_factory(self):
        identity = binding.teacher_identity(C0)
        self.assertEqual(identity["catalog_identity"], C0)
        self.assertEqual(
            identity["configuration"]["factory"],
            "lisjong_arena.policy_catalog.create_placement_aware_speed_call",
        )
        self.assertEqual(
            identity["configuration_digest"], binding.digest(identity["configuration"])
        )


class RecordingNonInterferenceIntegrationTest(unittest.TestCase):
    """Real RiichiEnv: recording on/off must not change the executed hanchan."""

    def test_recorded_and_unrecorded_hanchan_are_identical(self):
        spec = PolicySpec(identity="minimal", factory=MinimalPolicy)
        with patch.object(binding, "resolve_teacher", return_value=spec):
            recorded, inspection = generation.run_recorded_game("minimal", 944500123)
            unrecorded = generation.run_unrecorded_game("minimal", 944500123)
        self.assertEqual(recorded, unrecorded)
        self.assertEqual(
            sum(len(step.seat_decisions) for step in inspection.step_observations),
            recorded.decisions,
        )


if __name__ == "__main__":
    unittest.main()
