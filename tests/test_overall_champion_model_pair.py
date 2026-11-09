"""#583 Overall v2 dual-artifact lock tests; never run a real half-game."""

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from _overall_champion_aabb_fixtures import (
    destinations,
    heuristic_binding,
    learning_binding,
    merged_main_execution,
    seeds,
)

import lisjong_arena.overall_champion_aabb.lock as lock_module
import lisjong_arena.overall_champion_aabb.model_pair as model_pair
from lisjong_arena.overall_champion_aabb.lock import (
    OverallChampionLockError,
    build_lock_document,
    document_identity,
    parse_lock_document,
    require_live_execution_target,
    save_lock_document,
)
from lisjong_arena.overall_champion_aabb.model_pair import (
    BINDING,
    EXPECTED_COMPONENTS,
    FACTORY,
    POLICY_IDENTITY,
    PAIR_VERSION,
    OverallModelPairError,
    require_live_pair,
    require_pair_document,
)


def _l2a_binding():
    return learning_binding(
        factory_binding=FACTORY,
        policy_identity=POLICY_IDENTITY,
        checkpoint_binding=None,
        checkpoint_identity=None,
        checkpoint_digest=None,
    )


def _pair():
    return {
        "pair_version": PAIR_VERSION,
        "binding": BINDING,
        "components": copy.deepcopy(EXPECTED_COMPONENTS),
    }


class ModelPairBindingTest(unittest.TestCase):
    def test_four_exact_frozen_digests_are_required(self) -> None:
        self.assertEqual(require_pair_document(_pair()), _pair())
        for role in EXPECTED_COMPONENTS:
            for key in ("identity", "manifest_sha256", "weights_sha256"):
                modified = _pair()
                modified["components"][role][key] = "f" * 64
                with self.subTest(role=role, key=key):
                    with self.assertRaises(OverallModelPairError):
                        require_pair_document(modified)

    def test_missing_unknown_or_wrong_typed_pair_field_rejected(self) -> None:
        for modified in (
            {"pair_version": PAIR_VERSION, "binding": BINDING},
            {**_pair(), "unknown": True},
            {**_pair(), "pair_version": True},
            {**_pair(), "binding": "some.other.module:alias"},
            {**_pair(), "components": []},
        ):
            with self.subTest(modified=modified):
                with self.assertRaises(OverallModelPairError):
                    require_pair_document(modified)

    def test_live_attestation_is_rechecked_not_caller_trusted(self) -> None:
        with mock.patch.object(
            model_pair,
            "resolve_binding_callable",
            return_value=lambda: copy.deepcopy(EXPECTED_COMPONENTS),
        ):
            require_live_pair(_pair())
        changed = copy.deepcopy(EXPECTED_COMPONENTS)
        changed["reaction"]["weights_sha256"] = "e" * 64
        with mock.patch.object(
            model_pair,
            "resolve_binding_callable",
            return_value=lambda: changed,
        ):
            with self.assertRaises(OverallModelPairError):
                require_live_pair(_pair())


class OverallV2LockTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)

    def _build(self):
        with (
            merged_main_execution(),
            mock.patch.object(
                lock_module, "collect_ml_runtime", return_value={"torch": "2.13.0+cpu"}
            ),
            mock.patch.object(lock_module, "live_pair_document", return_value=_pair()),
        ):
            return build_lock_document(
                destinations=destinations(self.path),
                heuristic=heuristic_binding(),
                learning=_l2a_binding(),
                seeds=seeds(),
                max_workers=8,
                ml_runtime_packages=("torch",),
                learning_artifact_binding=BINDING,
            )

    def test_v1_historical_lock_still_has_exact_schema(self) -> None:
        from _overall_champion_aabb_fixtures import install_checkpoint, lock_document

        install_checkpoint(self.path)
        self.addCleanup(__import__("_overall_champion_aabb_fixtures").clear_checkpoint)
        doc = lock_document(self.path)
        self.assertEqual(parse_lock_document(doc), doc)
        self.assertEqual(doc["lock_version"], 1)
        self.assertNotIn("learning_artifacts", doc)

    def test_v2_roundtrip_portable_with_both_models_bound(self) -> None:
        doc = self._build()
        self.assertEqual(doc["lock_version"], 2)
        self.assertEqual(doc["learning_artifacts"], _pair())
        self.assertEqual(parse_lock_document(doc), doc)
        out = self.path / "overall-lock.json"
        save_lock_document(doc, out)
        self.assertEqual(
            parse_lock_document(__import__("json").loads(out.read_text())), doc
        )

    def test_malformed_pair_rejected_even_if_lock_digest_recomputed(self) -> None:
        for field in ("manifest_sha256", "weights_sha256"):
            doc = copy.deepcopy(self._build())
            doc["learning_artifacts"]["components"]["discard"][field] = "f" * 64
            payload = {k: v for k, v in doc.items() if k != "lock_identity"}
            doc["lock_identity"] = document_identity(payload)
            with self.subTest(field=field):
                with self.assertRaises(OverallChampionLockError):
                    parse_lock_document(doc)

    def test_v1_cannot_claim_current_l2a_without_attesting_pair(self) -> None:
        with merged_main_execution():
            with self.assertRaisesRegex(OverallChampionLockError, "requires"):
                build_lock_document(
                    destinations=destinations(self.path),
                    heuristic=heuristic_binding(),
                    learning=_l2a_binding(),
                    seeds=seeds(),
                    max_workers=1,
                )

    def test_v2_requires_torch_and_exact_champion_factory(self) -> None:
        with (
            merged_main_execution(),
            mock.patch.object(lock_module, "collect_ml_runtime", return_value={}),
        ):
            with self.assertRaisesRegex(OverallChampionLockError, "torch"):
                build_lock_document(
                    destinations=destinations(self.path),
                    heuristic=heuristic_binding(),
                    learning=_l2a_binding(),
                    seeds=seeds(),
                    max_workers=8,
                    learning_artifact_binding=BINDING,
                )
        with merged_main_execution():
            with self.assertRaises(OverallChampionLockError):
                build_lock_document(
                    destinations=destinations(self.path),
                    heuristic=heuristic_binding(),
                    learning=_l2a_binding(),
                    seeds=seeds(),
                    max_workers=8,
                    learning_artifact_binding="wrong.module:binding",
                )

    def test_live_preflight_rejects_ended_model_bytes(self) -> None:
        doc = self._build()
        with (
            merged_main_execution(),
            mock.patch.object(
                lock_module, "collect_ml_runtime", return_value={"torch": "2.13.0+cpu"}
            ),
            mock.patch.object(
                lock_module,
                "require_live_pair",
                side_effect=OverallModelPairError("changed"),
            ),
        ):
            with self.assertRaisesRegex(OverallChampionLockError, "changed"):
                require_live_execution_target(doc)


if __name__ == "__main__":
    unittest.main()
