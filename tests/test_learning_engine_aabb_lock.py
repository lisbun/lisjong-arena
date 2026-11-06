"""#562 protocol固定値・participant binding document・lock・live gate。"""

import copy
import tempfile
import unittest
from pathlib import Path

import _learning_engine_aabb_fixtures as fx

from lisjong_arena import initial_learning_champion
from lisjong_arena.heuristic_candidate_engine_aabb import protocol as heuristic
from lisjong_arena.learning_candidate_engine_aabb import protocol
from lisjong_arena.learning_candidate_engine_aabb.lock import (
    LearningEngineAabbLockError,
    load_lock,
    parse_lock_document,
    require_live_target,
    save_lock,
)
from lisjong_arena.learning_candidate_engine_aabb.participants import (
    INCUMBENT_ARM,
    ParticipantBindingError,
    parse_candidate,
    parse_incumbent,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity
from lisjong_arena.riichilab_source_pilot.protocol import ARM_Y


def _resealed(lock):
    payload = {key: value for key, value in lock.items() if key != "lock_digest"}
    return {**payload, "lock_digest": document_identity(payload)}


class ProtocolTest(unittest.TestCase):
    def test_protocol_identity_is_separate_from_heuristic(self) -> None:
        self.assertNotEqual(
            protocol.PROTOCOL_ID, "arena-heuristic-candidate-engine-aabb-half-v1"
        )
        self.assertNotEqual(protocol.PROTOCOL_DIGEST, heuristic.PROTOCOL_DIGEST)
        self.assertEqual(
            document_identity(protocol.protocol_document()), protocol.PROTOCOL_DIGEST
        )
        self.assertNotEqual(
            protocol.LAYOUT.kyoku_record_schema,
            "arena-heuristic-candidate-engine-aabb-kyoku-record-v1",
        )

    def test_engine_rules_and_placements_are_the_heuristic_ones(self) -> None:
        document = protocol.protocol_document()
        self.assertEqual(document["engine_revision"], heuristic.ENGINE_REVISION)
        self.assertEqual(document["rules_digest"], heuristic.RULES_DIGEST)
        self.assertEqual(
            document["placements"], [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]]
        )
        self.assertEqual(document["k"], 100)

    def test_incumbent_is_the_existing_learning_champion_binding(self) -> None:
        self.assertEqual(
            protocol.INCUMBENT_POLICY_IDENTITY,
            initial_learning_champion.POLICY_IDENTITY,
        )
        self.assertEqual(
            protocol.INCUMBENT_WEIGHTS_SHA256,
            "746ef5ada2cc83d2aefbfa94cac464f03d27edb8082d22e0e455ea916dde385f",
        )
        self.assertEqual(
            protocol.INCUMBENT_CHECKPOINT_IDENTITY,
            "ecb31fc6954095041423fcfc312d0b07f952ce550f5b05f5e94238bcce7bb45e",
        )
        self.assertEqual(INCUMBENT_ARM, ARM_Y.value)

    def test_formal_seeds_must_be_exactly_k(self) -> None:
        self.assertEqual(len(protocol.require_formal_seeds(fx.SEEDS)), 100)
        for seeds in (fx.SEEDS[:99], (*fx.SEEDS, 563_000), fx.SEEDS[:2]):
            with (
                self.subTest(len(seeds)),
                self.assertRaises(protocol.LearningEngineAabbError),
            ):
                protocol.require_formal_seeds(seeds)
        external = (472_000, *fx.SEEDS[1:])
        with self.assertRaises(heuristic.EngineAabbProtocolError):
            protocol.require_formal_seeds(external)


class ParticipantDocumentTest(unittest.TestCase):
    def test_fixture_documents_parse(self) -> None:
        self.assertIs(parse_candidate(fx.CANDIDATE), fx.CANDIDATE)
        self.assertIs(parse_incumbent(fx.INCUMBENT), fx.INCUMBENT)

    def test_candidate_tampering_is_rejected(self) -> None:
        def changed(mutate):
            value = copy.deepcopy(fx.CANDIDATE)
            mutate(value)
            return value

        cases = {
            "family": lambda v: v.update(family="heuristic"),
            "factory": lambda v: v.update(factory_binding="x:create"),
            "classification": lambda v: v.update(classification="other"),
            "policy identity": lambda v: v.update(policy_identity="l2a"),
            "extra field": lambda v: v.update(extra=1),
            "head swapped": lambda v: v["artifacts"]["discard"].update(head="call"),
            "head classification": lambda v: v["artifacts"]["reaction"].update(
                classification="other"
            ),
            "weights hash": lambda v: v["artifacts"]["discard"].update(
                weights_sha256="X" * 64
            ),
            "identity type": lambda v: v["artifacts"]["discard"].update(identity=1),
            "different sources": lambda v: v["artifacts"]["reaction"].update(
                source_identity="other"
            ),
            "same artifacts": lambda v: v["artifacts"].update(
                reaction={**v["artifacts"]["discard"], "head": "call"}
            ),
            "serving revision": lambda v: v["serving"].update(lisjong_revision="b"),
            "missing artifact": lambda v: v["artifacts"].pop("reaction"),
        }
        for name, mutate in cases.items():
            with self.subTest(name), self.assertRaises(ParticipantBindingError):
                parse_candidate(changed(mutate))

    def test_incumbent_must_be_the_exact_arm_y(self) -> None:
        def changed(mutate):
            value = copy.deepcopy(fx.INCUMBENT)
            mutate(value)
            return value

        cases = {
            "policy identity": lambda v: v.update(policy_identity="learned-x"),
            "weights": lambda v: v.update(weights_sha256="7" * 64),
            "checkpoint identity": lambda v: v["checkpoint"].update(
                checkpoint_identity="7" * 64
            ),
            "arm": lambda v: v["checkpoint"].update(arm="ARM_R"),
            "factory": lambda v: v.update(factory_binding="x:y"),
            "missing vocabulary": lambda v: v["checkpoint"].pop("vocabulary"),
            "manifest hash": lambda v: v.update(checkpoint_manifest_sha256="z"),
        }
        for name, mutate in cases.items():
            with self.subTest(name), self.assertRaises(ParticipantBindingError):
                parse_incumbent(changed(mutate))


class LockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_lock_binds_protocol_participants_population_and_limits(self) -> None:
        lock = fx.lock_document(self.tmp)
        self.assertEqual(lock["schema"], "arena-learning-candidate-engine-aabb-lock-v1")
        self.assertIs(lock["result_exposed"], False)
        self.assertEqual(lock["population"]["k"], 100)
        self.assertEqual(lock["population"]["ordered_seeds"], list(fx.SEEDS))
        self.assertEqual(lock["participants"]["A"], fx.CANDIDATE)
        self.assertEqual(lock["participants"]["B"], fx.INCUMBENT)
        self.assertEqual(lock["execution"]["limits"], fx.LIMITS)
        self.assertEqual(lock["execution"]["workers"], 1)
        path = save_lock(lock, self.tmp / "lock.json")
        self.assertEqual(load_lock(path), lock)
        with self.assertRaises(FileExistsError):
            save_lock(lock, path)

    def test_assembly_rejects_invalid_inputs(self) -> None:
        development = fx.ledger(main=fx.allocation(split="DEVELOPMENT"))
        heuristic_protocol = fx.ledger(
            main=fx.allocation(protocol="arena-heuristic-candidate-engine-aabb-half-v1")
        )
        colliding = fx.ledger(
            fx.allocation(
                event=963, seeds=(fx.SEEDS[5], 563_001), domain=fx.OTHER_DOMAIN
            )
        )
        cases = {
            "K 99": {"ordered_seeds": fx.SEEDS[:99]},
            "candidate": {"candidate": {**fx.CANDIDATE, "kind": "other"}},
            "incumbent": {"incumbent": {**fx.INCUMBENT, "policy_identity": "x"}},
            "limits missing": {"limits": {"timeout_seconds": 1, "wall_seconds": 2}},
            "limits zero": {"limits": {**fx.LIMITS, "peak_rss_kib": 0}},
            "timeout above wall": {"limits": {**fx.LIMITS, "timeout_seconds": 4_000}},
            "relative wheel": {"wheel_path": "wheel.whl"},
            "relative artifact": {
                "artifact_paths": {**fx.ARTIFACT_PATHS, "discard": "m1"}
            },
            "same artifact path": {
                "artifact_paths": {
                    **fx.ARTIFACT_PATHS,
                    "reaction": fx.ARTIFACT_PATHS["discard"],
                }
            },
            "torch threads": {
                "environment": {**fx.ENVIRONMENT, "ml": {**fx.ML, "torch_threads": 8}}
            },
            "nondeterministic": {
                "environment": {
                    **fx.ENVIRONMENT,
                    "ml": {**fx.ML, "deterministic_algorithms": False},
                }
            },
            "ml python": {
                "environment": {
                    **fx.ENVIRONMENT,
                    "ml": {**fx.ML, "python_version": "3.14.8"},
                }
            },
            "engine revision": {
                "environment": {
                    **fx.ENVIRONMENT,
                    "provenance": {
                        **fx.ENVIRONMENT["provenance"],
                        "lisjong_engine_revision": "0" * 40,
                    },
                }
            },
            "candidate serving revision": {
                "candidate": {
                    **fx.CANDIDATE,
                    "serving": {"lisjong_revision": "a" * 40},
                }
            },
            "incumbent serving revision": {
                "incumbent": {**fx.INCUMBENT, "serving": {"arena_revision": "a" * 40}}
            },
            "arena revision": {"arena_revision": "a" * 40},
            "destinations": {"destinations": {"raw": "a", "result": "b"}},
            "development allocation": {"ledger": development},
            "heuristic allocation": {"ledger": heuristic_protocol},
            "collision in another domain": {"ledger": colliding},
        }
        for name, overrides in cases.items():
            with (
                self.subTest(name),
                self.assertRaises(protocol.LearningEngineAabbError),
            ):
                fx.lock_document(self.tmp, **overrides)

    def test_parse_rejects_tampered_locks(self) -> None:
        lock = fx.lock_document(self.tmp)

        def changed(mutate, *, reseal=True):
            value = copy.deepcopy(lock)
            mutate(value)
            return _resealed(value) if reseal else value

        cases = {
            "digest": changed(
                lambda v: v.update(protocol_digest="0" * 64), reseal=False
            ),
            "result exposed": changed(lambda v: v.update(result_exposed=True)),
            "K": changed(lambda v: v["population"].update(k=99)),
            "K type": changed(lambda v: v["population"].update(k=100.0)),
            "protocol": changed(lambda v: v["protocol"].update(k=50)),
            "rules": changed(lambda v: v["rules"].update(uma=[20, 10, -10, -20])),
            "schedule digest": changed(
                lambda v: v["population"].update(schedule_digest="0" * 64)
            ),
            "split": changed(lambda v: v["population"].update(split="DEVELOPMENT")),
            "workers": changed(lambda v: v["execution"].update(workers=2)),
            "participant head": changed(
                lambda v: v["participants"]["A"]["artifacts"]["discard"].update(
                    head="call"
                )
            ),
            "participants swapped": changed(
                lambda v: v.update(
                    participants={
                        "A": v["participants"]["B"],
                        "B": v["participants"]["A"],
                    }
                )
            ),
            "environment digest": changed(
                lambda v: v["environment"]["python"].update(version="3.14.8")
            ),
            "branch": changed(lambda v: v["execution_target"].update(branch="dev")),
            "no rescue": changed(lambda v: v["no_rescue_boundary"].pop()),
            "schema": changed(lambda v: v.update(schema="other")),
        }
        self.assertEqual(parse_lock_document(copy.deepcopy(lock)), lock)
        for name, value in cases.items():
            with (
                self.subTest(name),
                self.assertRaises(protocol.LearningEngineAabbError),
            ):
                parse_lock_document(value)


class LiveGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = fx.lock_document(Path(tempfile.mkdtemp()))
        self.ledger = fx.ledger()

    def _gate(self, **overrides):
        live = {
            "environment": fx.ENVIRONMENT,
            "candidate": fx.CANDIDATE,
            "incumbent": fx.INCUMBENT,
        }
        live.update(overrides.pop("live", {}))
        require_live_target(
            self.lock,
            ledger=overrides.pop("ledger", self.ledger),
            live=(live["environment"], live["candidate"], live["incumbent"]),
            head=overrides.pop("head", fx.ARENA_REVISION),
        )

    def test_matching_live_values_pass(self) -> None:
        self._gate()

    def test_mismatches_stop_before_execution(self) -> None:
        other = copy.deepcopy(fx.CANDIDATE)
        other["artifacts"]["discard"]["manifest_sha256"] = "9" * 64
        cases = {
            "head": {"head": "a" * 40},
            "environment": {
                "live": {
                    "environment": {
                        **fx.ENVIRONMENT,
                        "ml": {**fx.ML, "torch_version": "2.12"},
                    }
                }
            },
            "candidate": {"live": {"candidate": other}},
            "incumbent": {
                "live": {
                    "incumbent": {
                        **fx.INCUMBENT,
                        "checkpoint_manifest_sha256": "a" * 64,
                    }
                }
            },
            "ledger": {
                "ledger": fx.ledger(
                    fx.allocation(
                        event=963, seeds=(fx.SEEDS[0],), domain=fx.OTHER_DOMAIN
                    )
                )
            },
        }
        for name, overrides in cases.items():
            with self.subTest(name), self.assertRaises(LearningEngineAabbLockError):
                self._gate(**overrides)


if __name__ == "__main__":
    unittest.main()
