"""#562 participant bindingの作成・ML条件・load順・新しいPolicy・lockとの照合。

torch・実artifactを使わず、load境界を偽のobjectで差し替える。
"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _learning_engine_aabb_fixtures as fx

from lisjong_arena.learning_candidate_engine_aabb import participants, runtime
from lisjong_arena.learning_candidate_engine_aabb.participants import (
    ParticipantBindingError,
    candidate_document,
    incumbent_document,
)
from lisjong_arena.learning_candidate_engine_aabb.runtime import (
    LearningHanchanTask,
    LearningRuntimeError,
    LoadedParticipants,
    load_participants,
    play_loaded,
    require_ml_runtime,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _l2a_dir(root: Path, name: str, weights: bytes, *, recorded=None):
    path = root / name
    path.mkdir()
    (path / "weights.f32").write_bytes(weights)
    manifest = {
        "dataset": {"identity": "dataset-506"},
        "encoding": {
            "classification": "lisjong-l2a-decision-classification-v1",
            "identity": f"encoding-{name}",
        },
        "files": {"weights.f32": {"sha256": recorded or _sha(weights)}},
        "lisjong": {"package_version": "0.1.0", "source_digest": "4" * 64},
        "source": {"identity": "l2a-source-506"},
    }
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def _scorer(identity, manifest, head):
    return SimpleNamespace(
        artifact=SimpleNamespace(identity=identity, manifest=manifest),
        encoding=SimpleNamespace(head=SimpleNamespace(value=head)),
    )


class CandidateDocumentTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.discard_path, discard = _l2a_dir(self.root, "m1", b"discard")
        self.reaction_path, reaction = _l2a_dir(self.root, "r1", b"reaction")
        self.runtime = SimpleNamespace(
            discard=_scorer(fx.DISCARD_IDENTITY, discard, "discard"),
            reaction=_scorer(fx.REACTION_IDENTITY, reaction, "call"),
            identity=(fx.DISCARD_IDENTITY, fx.REACTION_IDENTITY),
        )

    def _document(self, runtime=None):
        return candidate_document(
            runtime or self.runtime,
            discard_path=self.discard_path,
            reaction_path=self.reaction_path,
            lisjong_revision=fx.LISJONG_REVISION,
        )

    def test_binding_hashes_the_files_on_disk(self):
        document = self._document()
        discard = document["artifacts"]["discard"]
        self.assertEqual(discard["weights_sha256"], _sha(b"discard"))
        self.assertEqual(
            discard["manifest_sha256"],
            _sha((self.discard_path / "manifest.json").read_bytes()),
        )
        self.assertEqual(discard["head"], "discard")
        self.assertEqual(document["artifacts"]["reaction"]["head"], "call")
        self.assertEqual(
            document["policy_identity"],
            f"lisjong-l2a:{fx.DISCARD_IDENTITY}:{fx.REACTION_IDENTITY}",
        )
        self.assertEqual(document["serving"]["lisjong_revision"], fx.LISJONG_REVISION)

    def test_weights_that_differ_from_the_manifest_are_rejected(self):
        (self.discard_path / "weights.f32").write_bytes(b"changed")
        with self.assertRaisesRegex(ParticipantBindingError, "discard weights"):
            self._document()

    def test_runtime_identity_must_match_the_artifacts(self):
        self.runtime.identity = (fx.REACTION_IDENTITY, fx.DISCARD_IDENTITY)
        with self.assertRaises(ParticipantBindingError):
            self._document()

    def test_swapped_heads_are_rejected(self):
        self.runtime.discard.encoding.head.value = "call"
        with self.assertRaises(ParticipantBindingError):
            self._document()


class IncumbentDocumentTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp())
        (self.path / "weights.pt").write_bytes(b"arm-y")
        (self.path / "manifest.json").write_text("{}", encoding="utf-8")
        self.digest = _sha(b"arm-y")
        identity = {**fx.INCUMBENT["checkpoint"], "weights_sha256": self.digest}
        self.checkpoint = SimpleNamespace(
            path=self.path,
            weights_sha256=self.digest,
            policy_identity=fx.INCUMBENT["policy_identity"],
            identity_document=lambda: identity,
        )

    def test_binding_hashes_the_exact_checkpoint(self):
        with mock.patch.object(participants, "INCUMBENT_WEIGHTS_SHA256", self.digest):
            document = incumbent_document(
                self.checkpoint, arena_revision=fx.ARENA_REVISION
            )
        self.assertEqual(document["weights_sha256"], self.digest)
        self.assertEqual(document["checkpoint_manifest_sha256"], _sha(b"{}"))
        self.assertEqual(document["serving"]["arena_revision"], fx.ARENA_REVISION)

    def test_other_weights_are_not_the_learning_champion(self):
        with self.assertRaisesRegex(ParticipantBindingError, "exact checkpoint"):
            incumbent_document(self.checkpoint, arena_revision=fx.ARENA_REVISION)

    def test_weights_file_must_match_the_checkpoint_manifest(self):
        (self.path / "weights.pt").write_bytes(b"changed")
        with self.assertRaisesRegex(ParticipantBindingError, "weights file"):
            incumbent_document(self.checkpoint, arena_revision=fx.ARENA_REVISION)


class MlRuntimeTest(unittest.TestCase):
    def test_fixed_conditions(self):
        self.assertEqual(require_ml_runtime(dict(fx.ML)), fx.ML)
        for change in (
            {"torch_threads": 8},
            {"deterministic_algorithms": False},
            {"cuda_available": True},
            {"device": "cuda"},
            {"torch_threads": 1.0},
            {"torch_version": ""},
        ):
            with self.subTest(change), self.assertRaises(LearningRuntimeError):
                require_ml_runtime({**fx.ML, **change})
        with self.assertRaises(LearningRuntimeError):
            require_ml_runtime({k: v for k, v in fx.ML.items() if k != "device"})

    def test_torch_version_str_subclass_is_measured_as_a_plain_str(self):
        """実torchの ``__version__`` はstrのsubclass（TorchVersion）。平のstrとして記録する（#569）。"""

        class TorchVersionLike(str):
            pass

        fake_torch = SimpleNamespace(
            __version__=TorchVersionLike("2.13.0+cpu"),
            cuda=SimpleNamespace(is_available=lambda: False),
            are_deterministic_algorithms_enabled=lambda: True,
            get_num_threads=lambda: 1,
        )
        with mock.patch.dict("sys.modules", {"torch": fake_torch}):
            measured = runtime.measured_ml_runtime()
        self.assertIs(type(measured["torch_version"]), str)
        self.assertEqual(measured["torch_version"], "2.13.0+cpu")
        self.assertEqual(require_ml_runtime(measured), measured)
        with self.assertRaises(LearningRuntimeError):
            require_ml_runtime(
                {**measured, "torch_version": TorchVersionLike("2.13.0")}
            )

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("torch"), "requires torch"
    )
    def test_real_torch_conditions_pass_the_check(self):
        import torch

        threads = torch.get_num_threads()
        deterministic = torch.are_deterministic_algorithms_enabled()
        self.addCleanup(torch.set_num_threads, threads)
        self.addCleanup(torch.use_deterministic_algorithms, deterministic)
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        measured = runtime.measured_ml_runtime()
        self.assertIs(type(measured["torch_version"]), str)
        self.assertEqual(require_ml_runtime(measured), measured)


class LoadParticipantsTest(unittest.TestCase):
    """ML条件を先に固定し、L2a -> Arm Yの順にloadして、条件が変わらないことを要求する。"""

    def _load(self, *, measures=(), served=None):
        calls = []
        sequence = iter(measures or (dict(fx.ML), dict(fx.ML)))

        def configure():
            calls.append("configure")
            return dict(fx.ML)

        def measure():
            calls.append("measure")
            return next(sequence)

        def load_candidate(discard, reaction):
            calls.append(("candidate", discard, reaction))
            return SimpleNamespace(create_policy=object)

        def load_incumbent(path):
            calls.append(("incumbent", path))
            conditions = served or {
                "deterministic_algorithms": True,
                "torch_threads": 1,
                "torch_version": fx.ML["torch_version"],
            }
            return (
                SimpleNamespace(conditions=conditions, checkpoint=None),
                object,
            )

        with (
            mock.patch.object(runtime, "candidate_document", return_value=fx.CANDIDATE),
            mock.patch.object(runtime, "incumbent_document", return_value=fx.INCUMBENT),
        ):
            loaded = load_participants(
                fx.ARTIFACT_PATHS,
                lisjong_revision=fx.LISJONG_REVISION,
                arena_revision=fx.ARENA_REVISION,
                configure=configure,
                measure=measure,
                load_candidate=load_candidate,
                load_incumbent=load_incumbent,
            )
        return loaded, calls

    def test_conditions_are_fixed_before_both_loads(self):
        loaded, calls = self._load()
        self.assertEqual(
            calls,
            [
                "configure",
                (
                    "candidate",
                    fx.ARTIFACT_PATHS["discard"],
                    fx.ARTIFACT_PATHS["reaction"],
                ),
                "measure",
                ("incumbent", fx.ARTIFACT_PATHS["champion"]),
                "measure",
            ],
        )
        self.assertEqual(loaded.candidate, fx.CANDIDATE)
        self.assertEqual(loaded.incumbent, fx.INCUMBENT)
        self.assertEqual(loaded.ml, fx.ML)

    def test_a_load_that_changes_the_conditions_stops(self):
        changed = {**fx.ML, "torch_threads": 8}
        for measures in ((changed, changed), (dict(fx.ML), changed)):
            with self.subTest(measures), self.assertRaises(LearningRuntimeError):
                self._load(measures=measures)

    def test_arm_y_runtime_must_report_the_same_conditions(self):
        served = {
            "deterministic_algorithms": True,
            "torch_threads": 8,
            "torch_version": fx.ML["torch_version"],
        }
        with self.assertRaisesRegex(LearningRuntimeError, "torch_threads"):
            self._load(served=served)

    def test_arm_y_path_must_be_the_configured_checkpoint(self):
        from lisjong_arena import initial_learning_champion as champion

        with (
            mock.patch.dict(
                os.environ, {champion.CHECKPOINT_ENV_VAR: os.path.abspath("other")}
            ),
            self.assertRaisesRegex(LearningRuntimeError, "locked Arm Y"),
        ):
            runtime._load_arm_y(fx.ARTIFACT_PATHS["champion"])


class _Policy:
    def choose_action(self, decision):
        return decision


class FreshPolicyTest(unittest.TestCase):
    def _task(self, placement=3):
        return LearningHanchanTask(
            seed=fx.SEEDS[0],
            placement=placement,
            candidate=fx.CANDIDATE,
            incumbent=fx.INCUMBENT,
            artifact_paths=fx.ARTIFACT_PATHS,
            wheel_path=fx.WHEEL_PATH,
        )

    def _play(self, factories, placement=3):
        seen = {}

        def fake_hanchan(policies, **kwargs):
            for seat, policy in policies.items():
                policy.choose_action("decision")
                kwargs["on_selector_call"](seat)
            seen.update(policies)
            return "match"

        with mock.patch.object(runtime, "run_policy_hanchan", fake_hanchan):
            capture, analysis = play_loaded(
                self._task(placement), factories, {"A": "policy-a", "B": "policy-b"}
            )
        return capture, analysis, seen

    def test_each_seat_gets_a_new_policy_every_hanchan(self):
        created = {"A": [], "B": []}

        def factory(role):
            def create():
                policy = _Policy()
                created[role].append(policy)
                return policy

            return create

        factories = {"A": factory("A"), "B": factory("B")}
        capture, analysis, _ = self._play(factories, placement=3)
        self._play(factories, placement=0)
        self.assertEqual((len(created["A"]), len(created["B"])), (4, 4))
        everything = created["A"] + created["B"]
        self.assertEqual(len({id(policy) for policy in everything}), 8)
        self.assertEqual([seat.role for seat in capture.seats], ["B", "A", "A", "B"])
        self.assertEqual(capture.policy_call_counts, (1, 1, 1, 1))
        self.assertEqual(capture.selector_call_counts, (1, 1, 1, 1))
        self.assertEqual(analysis["decisions"], [1, 1, 1, 1])
        self.assertEqual(len(analysis["decision_seconds"]), 4)

    def test_a_shared_policy_instance_is_rejected(self):
        shared = _Policy()
        with self.assertRaisesRegex(LearningRuntimeError, "shared"):
            self._play({"A": lambda: shared, "B": _Policy})

    def test_worker_refuses_participants_that_differ_from_the_lock(self):
        other = {**fx.CANDIDATE, "serving": {"lisjong_revision": "a" * 40}}
        loaded = LoadedParticipants(_Policy, _Policy, other, fx.INCUMBENT, dict(fx.ML))
        with (
            mock.patch.object(
                runtime, "environment_document", return_value=fx.ENVIRONMENT
            ),
            mock.patch.object(runtime, "_loaded_once", return_value=loaded),
            self.assertRaisesRegex(ParticipantBindingError, "differ from the lock"),
        ):
            runtime.play_learning_hanchan(self._task())

    def test_worker_refuses_ml_conditions_other_than_the_load(self):
        loaded = LoadedParticipants(
            _Policy,
            _Policy,
            fx.CANDIDATE,
            fx.INCUMBENT,
            {**fx.ML, "torch_version": "x"},
        )
        with (
            mock.patch.object(
                runtime, "environment_document", return_value=fx.ENVIRONMENT
            ),
            mock.patch.object(runtime, "_loaded_once", return_value=loaded),
            self.assertRaisesRegex(LearningRuntimeError, "ML conditions"),
        ):
            runtime.play_learning_hanchan(self._task())


if __name__ == "__main__":
    unittest.main()
