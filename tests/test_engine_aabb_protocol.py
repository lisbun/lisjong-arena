"""#514 protocol固定値・主統計・lockの検査。"""

import copy
import dataclasses
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _engine_aabb_protocol_fixtures import (
    ARENA_REVISION,
    CANDIDATE,
    ENVIRONMENT,
    EVENT,
    INCUMBENT,
    LISJONG_REVISION,
    SEED_A,
    SEED_B,
    SEEDS,
    _allocation,
    allocation_binding,
    ledger,
    lock_document,
)
from _engine_aabb_record_fixtures import two_block_captures
from lisjong_engine.rules import RuleSet

from lisjong_arena import seed_registry
from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.heuristic_candidate_engine_aabb import protocol, statistics
from lisjong_arena.heuristic_candidate_engine_aabb.development import (
    catalog_participant,
)
from lisjong_arena.heuristic_candidate_engine_aabb.lock import (
    EngineAabbLockError,
    load_lock,
    parse_lock_document,
    require_live_target,
    save_lock,
)
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import (
    EngineAabbProtocolError,
    Participant,
    parse_participant,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import build_records
from lisjong_arena.overall_champion_aabb.lock import document_identity


class ProtocolConstantsTest(unittest.TestCase):
    def test_digests_are_pinned(self):
        self.assertEqual(
            document_identity(protocol.EXPECTED_RULES),
            "1393f5021bd332ca6efe733c12fd79e011b55706ae559db894b0619cd0fa9626",
        )
        self.assertEqual(
            document_identity(protocol.protocol_document()),
            "e60f75d7c32695e1c79225cad409334c3233dc8dd8b1cd0c6c7995789c9adec2",
        )
        self.assertEqual(
            protocol.protocol_document()["engine_revision"], protocol.ENGINE_REVISION
        )
        self.assertEqual(
            protocol.ENGINE_REVISION, "91af75e3aa11520c3b0543719dc74bb4c517ee06"
        )

    def test_rules_document_enumerates_every_rule_set_field(self):
        document = protocol.rules_document(protocol.locked_rules())
        self.assertEqual(
            set(document), {field.name for field in dataclasses.fields(RuleSet)}
        )
        self.assertEqual(document, protocol.EXPECTED_RULES)
        self.assertEqual(document["pao_yaku"], ["daisangen", "daisuushii"])
        self.assertEqual(document["uma"], [30, 10, -10, -30])

    def test_drifted_rule_set_is_rejected(self):
        changed = dataclasses.replace(RuleSet.default(), uma=(20, 10, -10, -20))
        with mock.patch.object(RuleSet, "default", return_value=changed):
            with self.assertRaisesRegex(EngineAabbProtocolError, "§5.2"):
                protocol.locked_rules()

    def test_schedule_order_and_role_exposure(self):
        hanchans = protocol.schedule_document(SEEDS)["hanchans"]
        self.assertEqual(
            [(item["seed"], item["placement"]) for item in hanchans],
            [(SEED_A, p) for p in range(6)] + [(SEED_B, p) for p in range(6)],
        )
        block = hanchans[:6]
        # 各seatはblock内で3回ずつA、roleごとに12 seat-results
        for seat in range(4):
            self.assertEqual(sum(item["roles"][seat] == "A" for item in block), 3)
        self.assertEqual(
            sum(role == "A" for item in block for role in item["roles"]), 12
        )

    def test_population_seed_rules(self):
        with self.assertRaisesRegex(EngineAabbProtocolError, "at least 2"):
            protocol.require_seeds([SEED_A])
        with self.assertRaisesRegex(EngineAabbProtocolError, "repeat"):
            protocol.require_seeds([SEED_A, SEED_A])
        with self.assertRaisesRegex(EngineAabbProtocolError, "outside the registry"):
            protocol.require_seeds([SEED_A, 472_005])
        with self.assertRaises(EngineAabbProtocolError):
            protocol.require_seeds([SEED_A, True])
        with self.assertRaises(EngineAabbProtocolError):
            protocol.require_seeds("514001,514002")


class ParticipantTest(unittest.TestCase):
    def document(self, **changes):
        return {**CANDIDATE.to_document(), **changes}

    def test_round_trip(self):
        self.assertEqual(parse_participant(CANDIDATE.to_document(), "A"), CANDIDATE)

    def test_rejected_participants(self):
        for changes, pattern in (
            ({"artifact": {"checkpoint": "x"}}, "learned artifacts"),
            ({"family": "learned"}, "heuristic"),
            ({"backend": "python"}, "rust"),
            ({"lisjong_revision": "main"}, "40-hex"),
            ({"policy_identity": "Two Step"}, "policy_identity"),
            ({"behavior_configuration_digest": "0" * 64}, "digest"),
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(EngineAabbProtocolError, pattern):
                    parse_participant(self.document(**changes), "A")

    def test_factory_binding_must_be_the_catalog_factory(self):
        for binding in (
            "lisjong.learning:create_policy",
            "lisjong_arena.policy_catalog:create_two_step",
            "lisjong_arena.policy_catalog:POLICY_CATALOG",
        ):
            with self.subTest(binding=binding):
                with self.assertRaises(EngineAabbProtocolError):
                    protocol.resolve_factory(
                        dataclasses.replace(CANDIDATE, factory_binding=binding)
                    )

    def test_behavior_configuration_must_fit_the_factory(self):
        configured = dataclasses.replace(CANDIDATE, behavior_configuration={"depth": 3})
        with self.assertRaisesRegex(EngineAabbProtocolError, "factory"):
            protocol.resolve_factory(configured)

    def test_pair_rules(self):
        with self.assertRaisesRegex(EngineAabbProtocolError, "identities"):
            protocol.require_pair(CANDIDATE, CANDIDATE)
        other_revision = catalog_participant("placement-aware-speed-call", "f" * 40)
        with self.assertRaisesRegex(EngineAabbProtocolError, "same lisjong"):
            protocol.require_pair(CANDIDATE, other_revision)
        same_factory = Participant(
            "alias", INCUMBENT.factory_binding, LISJONG_REVISION, {}
        )
        with self.assertRaisesRegex(EngineAabbProtocolError, "factories"):
            protocol.require_pair(same_factory, INCUMBENT)


class StatisticsTest(unittest.TestCase):
    def test_block_totals_from_records(self):
        hanchans, _, _ = build_records(two_block_captures(), event_facts_required=True)
        self.assertEqual(
            statistics.block_totals(hanchans),
            [
                {
                    "difference_internal": -1000,
                    "s_a_internal": -500,
                    "s_b_internal": 500,
                    "seed": 513001,
                },
                {
                    "difference_internal": 0,
                    "s_a_internal": 0,
                    "s_b_internal": 0,
                    "seed": 513002,
                },
            ],
        )

    def test_interval_by_hand(self):
        # D = 1, -2, 3（内部単位 120, -240, 360）
        summary = statistics.summarize_blocks([120, -240, 360])
        mean = 2 / 3
        sd = math.sqrt(((1 - mean) ** 2 + (-2 - mean) ** 2 + (3 - mean) ** 2) / 2)
        self.assertAlmostEqual(summary["mean_d"], mean)
        self.assertAlmostEqual(summary["sd"], sd)
        self.assertAlmostEqual(summary["lower"], mean - 1.96 * sd / math.sqrt(3))
        self.assertEqual(summary["k"], 3)
        self.assertEqual(summary["classification"], "INCONCLUSIVE")

    def test_classification_boundaries(self):
        self.assertEqual(
            statistics.summarize_blocks([120, 120])["classification"],
            "CANDIDATE SUPERIOR",
        )
        self.assertEqual(
            statistics.summarize_blocks([-120, -120])["classification"],
            "CHAMPION SUPERIOR",
        )
        # 区間の境界が0ちょうど（lower = upper = 0）はINCONCLUSIVE
        zero = statistics.summarize_blocks([0, 0])
        self.assertEqual((zero["lower"], zero["upper"]), (0.0, 0.0))
        self.assertEqual(zero["classification"], "INCONCLUSIVE")

    def test_invalid_inputs(self):
        with self.assertRaises(EngineAabbProtocolError):
            statistics.summarize_blocks([120])
        with self.assertRaises(EngineAabbProtocolError):
            statistics.summarize_blocks([120, 1.5])
        hanchans, _, _ = build_records(two_block_captures(), event_facts_required=True)
        with self.assertRaisesRegex(EngineAabbProtocolError, "6K"):
            statistics.block_totals(hanchans[:-1])
        broken = copy.deepcopy(hanchans)
        broken[0]["final_score"][0]["final_points"] += 10
        with self.assertRaisesRegex(EngineAabbProtocolError, "S_A \\+ S_B"):
            statistics.block_totals(broken)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_lock_binds_everything(self):
        lock = lock_document(self.tmp)
        self.assertFalse(lock["result_exposed"])
        self.assertEqual(lock["population"]["ordered_seeds"], list(SEEDS))
        self.assertEqual(lock["population"]["k"], 2)
        self.assertEqual(
            lock["population"]["owner_issue"], f"lisbun/lisjong-arena#{EVENT}"
        )
        self.assertEqual(lock["protocol_digest"], protocol.PROTOCOL_DIGEST)
        self.assertEqual(lock["rules_digest"], protocol.RULES_DIGEST)
        self.assertEqual(lock["execution"]["timeout_seconds"], 60)
        self.assertEqual(lock["environment"], ENVIRONMENT)
        self.assertIsNone(lock["participants"]["A"]["artifact"])

    def test_write_once_and_round_trip(self):
        lock = lock_document(self.tmp)
        path = self.tmp / "lock.json"
        save_lock(lock, path)
        self.assertEqual(load_lock(path), lock)
        with self.assertRaises(FileExistsError):
            save_lock(lock, path)

    def test_rejected_inputs(self):
        cases = (
            ({"k": 3}, "K must equal"),
            ({"ordered_seeds": (SEED_A,), "k": 1}, "at least 2"),
            ({"incumbent": CANDIDATE}, "identities"),
            ({"incumbent_role": "champion"}, "incumbent_role"),
            ({"timeout_seconds": 0}, "timeout"),
            ({"timeout_seconds": None}, "timeout"),
            ({"k_rationale": {"power": "x"}}, "k_rationale"),
            ({"k_rationale": {**{k: "x" for k in ("cost_estimate", "power", "sd_estimate")}, "target_effect": " "}}, "non-empty"),
            ({"wheel_path": "relative.whl"}, "absolute"),
            ({"arena_revision": "main"}, "40-hex"),
            ({"environment": {**ENVIRONMENT, "shanten_backend": {"backend": "python", "lisjong_revision": LISJONG_REVISION}}}, "rust"),
            ({"environment": {**ENVIRONMENT, "provenance": {**ENVIRONMENT["provenance"], "lisjong_engine_revision": "8" * 40}}}, "lisjong-engine"),
            ({"candidate": catalog_participant("two-step", "f" * 40)}, "same lisjong|lisjong revision"),
        )  # fmt: skip
        for overrides, pattern in cases:
            with self.subTest(overrides=sorted(overrides)):
                with self.assertRaisesRegex(EngineAabbProtocolError, pattern):
                    lock_document(self.tmp, **overrides)

    def test_seed_allocation_authority(self):
        # 別eventのallocation（他domainを含む）と1 seedでも重なれば拒否
        other = _allocation(
            event=901,
            seeds=(SEED_B, 514_100),
            domain="riichienv-4p-red-half-hanchan-v1",
        )
        with self.assertRaisesRegex(EngineAabbLockError, "collide"):
            lock_document(self.tmp, ledger=ledger(other))
        # population / split が違うallocationは使えない
        document = ledger()
        binding = allocation_binding(document)
        with self.assertRaisesRegex(EngineAabbLockError, "authority"):
            lock_document(
                self.tmp, ledger=document, event=902, allocation_binding=binding
            )
        calibration = seed_registry.new_ledger()
        calibration["allocations"] = [_allocation(split="CALIBRATION")]
        with self.assertRaisesRegex(EngineAabbLockError, "split"):
            lock_document(self.tmp, ledger=seed_registry.validate_ledger(calibration))
        # allocationと順序だけ違うordered seedsは、membershipが同じでもscheduleが変わる
        reordered = lock_document(self.tmp, ordered_seeds=(SEED_B, SEED_A))
        self.assertNotEqual(
            reordered["population"]["schedule_digest"],
            lock_document(self.tmp)["population"]["schedule_digest"],
        )
        retired = seed_registry.transition_allocation(
            document, binding["allocation_identity"], state="RETIRED"
        )
        with self.assertRaisesRegex(EngineAabbLockError, "active"):
            lock_document(self.tmp, ledger=retired, allocation_binding=binding)

    def test_parse_rejects_tampering(self):
        lock = lock_document(self.tmp)
        for mutate, pattern in (
            (lambda d: d.update(result_exposed=True), "result_exposed"),
            (lambda d: d.update(schema="arena-heuristic-candidate-engine-aabb-lock-v2"), "schema"),
            (lambda d: d.update(extra=1), "fields"),
            (lambda d: d["rules"].update(uma=[20, 10, -10, -20]), "rules"),
            (lambda d: d["protocol"].update(protocol_version=2), "protocol"),
            (lambda d: d["population"].update(k=3), "K"),
            (lambda d: d["population"]["ordered_seeds"].reverse(), "schedule digest"),
            (lambda d: d["population"].update(split="CALIBRATION"), "population"),
            (lambda d: d["execution"].update(timeout_seconds=61), "lock digest"),
            (lambda d: d["execution"].update(timeout_seconds="60"), "timeout"),
            (lambda d: d["environment"]["python"].update(version="3.14.8"), "environment digest"),
            (lambda d: d["participants"]["B"].update(policy_identity="two-step"), "bound"),
            (lambda d: d["artifact_destinations"].update(result=d["artifact_destinations"]["raw"]), "differ"),
            (lambda d: d["no_rescue_boundary"].pop(), "no-rescue"),
        ):  # fmt: skip
            document = copy.deepcopy(lock)
            mutate(document)
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(EngineAabbProtocolError, pattern):
                    parse_lock_document(document)

    def test_live_gate(self):
        lock = lock_document(self.tmp)
        document = ledger()
        require_live_target(
            lock, ledger=document, environment=ENVIRONMENT, head=ARENA_REVISION
        )
        with self.assertRaisesRegex(EngineAabbLockError, "HEAD"):
            require_live_target(
                lock, ledger=document, environment=ENVIRONMENT, head="9" * 40
            )
        drifted = json.loads(json.dumps(ENVIRONMENT))
        drifted["native"]["wheel_sha256"] = "0" * 64
        with self.assertRaisesRegex(EngineAabbLockError, "environment"):
            require_live_target(
                lock, ledger=document, environment=drifted, head=ARENA_REVISION
            )
        retired = seed_registry.transition_allocation(
            document,
            lock["population"]["allocation_binding"]["allocation_identity"],
            state="RETIRED",
        )
        with self.assertRaisesRegex(EngineAabbLockError, "active"):
            require_live_target(
                lock, ledger=retired, environment=ENVIRONMENT, head=ARENA_REVISION
            )

    def test_lock_file_must_be_canonical_json(self):
        lock = lock_document(self.tmp)
        path = self.tmp / "lock.json"
        path.write_text(
            canonical_json_text(lock).replace('"k": 2', '"k": 2, "k": 3'),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaises(EngineAabbLockError):
            load_lock(path)


if __name__ == "__main__":
    unittest.main()
