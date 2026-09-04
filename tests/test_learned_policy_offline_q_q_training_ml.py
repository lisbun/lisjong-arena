"""Arm B (support-restricted Offline Q) training and checkpoint tests (Issue #140)."""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _learned_policy_offline_q_artifact_fixtures import (
    FIXTURE_PROVENANCE,
    write_synthetic_dataset,
)

from lisjong_arena.learned_policy_offline_q import q_training
from lisjong_arena.learned_policy_offline_q.artifact import OfflineQDatasetWriter
from lisjong_arena.learned_policy_offline_q.errors import (
    OfflineQArtifactError,
    OfflineQProtocolError,
)
from lisjong_arena.learned_policy_offline_q.model import MacroTransitionRow
from lisjong_arena.learned_policy_offline_q.protocol import (
    DATASET_ORDERED_SEEDS,
    FEATURE_DIMENSION,
    GAMMA,
    MAXIMUM_EPOCHS,
    VOCABULARY_SIZE,
    Split,
    action_family,
    split_for_seed,
)
from lisjong_arena.learned_policy_offline_q.q_training import (
    compute_td_targets,
    load_checkpoint,
    save_checkpoint,
    train_q_model,
    train_support_mask,
)
from lisjong_arena.learned_policy_offline_q.split_tensors import (
    OfflineQSplitTensors,
    load_split_tensors,
)


def _mask(indices) -> tuple[bool, ...]:
    chosen = set(indices)
    return tuple(index in chosen for index in range(VOCABULARY_SIZE))


def _features(seed: int, ordinal: int) -> tuple[float, ...]:
    values = [0.0] * FEATURE_DIMENSION
    values[(seed * 31 + ordinal * 17) % FEATURE_DIMENSION] = 1.0
    return tuple(values)


def _row(
    seed: int,
    ordinal: int,
    *,
    legal_indices,
    behavior_index,
    terminal,
    next_legal_indices=None,
) -> MacroTransitionRow:
    kwargs = dict(
        seed=seed,
        split=split_for_seed(seed),
        round_ordinal=0,
        round_wind="east",
        hand_number=1,
        honba=0,
        actor_seat=0,
        step_ordinal=ordinal,
        decision_ordinal=ordinal,
        feature_values=_features(seed, ordinal),
        legal_mask=_mask(legal_indices),
        behavior_action_index=behavior_index,
        behavior_action_family=action_family(behavior_index),
        reward=0.01,
        terminal=terminal,
    )
    if terminal:
        kwargs.update(
            next_step_ordinal=None,
            next_decision_ordinal=None,
            next_feature_values=None,
            next_legal_mask=None,
        )
    else:
        kwargs.update(
            next_step_ordinal=ordinal + 1,
            next_decision_ordinal=ordinal + 1,
            next_feature_values=_features(seed, ordinal + 1),
            next_legal_mask=_mask(next_legal_indices),
        )
    return MacroTransitionRow(**kwargs)


def _build_dataset_with_a_coverage_gap(destination: Path, *, gap_next_legal_indices):
    """TRAINのbehavior supportは{0,1}に固定し、1つのTRAIN gameだけ
    ``gap_next_legal_indices``をnext legal indicesに持つnonterminal rowを持つ。

    呼び出し側は、TRAIN support {0,1}に対して全く重ならないcase（例: {2,3}）と、
    一部だけ重なるcase（例: {0,2}）の両方を渡せる。locked contractはどちらも
    fail closedを要求する -- 「1つでもTRAIN-unsupportedなnext legal actionが
    あればそのtransitionをbootstrapへ使わない」。
    """
    writer = OfflineQDatasetWriter(destination, provenance=FIXTURE_PROVENANCE)
    try:
        train_seeds = [
            seed
            for seed in DATASET_ORDERED_SEEDS
            if split_for_seed(seed) is Split.TRAIN
        ]
        gap_seed = train_seeds[0]
        for seed in DATASET_ORDERED_SEEDS:
            split = split_for_seed(seed)
            if seed == gap_seed:
                rows = (
                    _row(
                        seed,
                        0,
                        legal_indices=(0, 1),
                        behavior_index=0,
                        terminal=False,
                        next_legal_indices=gap_next_legal_indices,
                    ),
                )
            else:
                rows = (
                    _row(
                        seed,
                        0,
                        legal_indices=(0, 1),
                        behavior_index=0,
                        terminal=False,
                        next_legal_indices=(0, 1),
                    ),
                    _row(
                        seed, 1, legal_indices=(0, 1), behavior_index=1, terminal=True
                    ),
                )
            writer.add_game(
                seed=seed,
                split=split,
                scores=(25000, 25000, 25000, 25000),
                ranks=(1, 2, 3, 4),
                rows=rows,
            )
        return writer.finalize()
    except BaseException:
        writer.discard()
        raise


TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class QTrainingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.dataset = write_synthetic_dataset(self._tmp / "dataset", rows_per_game=6)
        self.checkpoint_path = self._tmp / "checkpoint"

    def test_train_support_mask_matches_the_train_behavior_actions(self):
        tensors = load_split_tensors(self.dataset)
        mask = train_support_mask(tensors[Split.TRAIN])
        expected = set(tensors[Split.TRAIN].behavior_action_index.tolist())
        actual = {index for index in range(VOCABULARY_SIZE) if bool(mask[index])}
        self.assertEqual(actual, expected)

    def test_training_takes_the_fixed_final_iteration_and_round_trips_it(self):
        run = train_q_model(self.dataset)
        # No cross-epoch VALIDATION selection: the checkpoint is always the
        # final of MAXIMUM_EPOCHS outer iterations, never a "best" epoch chosen
        # by comparing Huber loss against a moving bootstrap target.
        self.assertEqual(run.selected_epoch, MAXIMUM_EPOCHS)
        self.assertEqual(len(run.history), MAXIMUM_EPOCHS)
        self.assertEqual(
            run.final_validation_huber_loss, run.history[-1].validation_huber_loss
        )
        checkpoint = save_checkpoint(self.checkpoint_path, self.dataset, run)
        self.assertEqual(checkpoint.manifest["dataset_identity"], self.dataset.identity)
        self.assertEqual(checkpoint.manifest["selected_epoch"], MAXIMUM_EPOCHS)
        reloaded = load_checkpoint(self.checkpoint_path)
        self.assertEqual(reloaded.identity, checkpoint.identity)
        self.assertTrue(reloaded.supported_indices)

    def test_existing_checkpoint_destination_is_never_overwritten(self):
        run = train_q_model(self.dataset)
        save_checkpoint(self.checkpoint_path, self.dataset, run)
        with self.assertRaises(FileExistsError):
            save_checkpoint(self.checkpoint_path, self.dataset, run)

    def test_tampered_weights_fail_closed(self):
        run = train_q_model(self.dataset)
        checkpoint = save_checkpoint(self.checkpoint_path, self.dataset, run)
        weights_path = checkpoint.path / "weights.pt"
        payload = bytearray(weights_path.read_bytes())
        payload[-1] ^= 0xFF
        weights_path.write_bytes(bytes(payload))
        with self.assertRaises(OfflineQArtifactError):
            load_checkpoint(self.checkpoint_path)

    def test_tampered_supported_indices_with_a_stale_digest_fails_closed(self):
        """supported_indicesを書き換えてcanonical manifestを再保存すると、
        (raw list, digest)の食い違いでstrict loadがfail closedする。
        `checkpoint_identity`はdigestだけをhashするため、この照合が無いと
        raw listの改ざんは検出できない。
        """
        run = train_q_model(self.dataset)
        checkpoint = save_checkpoint(self.checkpoint_path, self.dataset, run)
        manifest_path = checkpoint.path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Widen supported_indices without updating supported_indices_digest or
        # checkpoint_identity -- this is exactly the kind of tamper that a
        # digest-less identity would silently accept.
        manifest["supported_indices"] = sorted(set(manifest["supported_indices"]) | {5})
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(OfflineQArtifactError):
            load_checkpoint(self.checkpoint_path)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class CoverageGapTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.destination = self._tmp / "dataset"

    def test_unsupported_next_actions_fail_closed_instead_of_silently_extrapolating(
        self,
    ):
        dataset = _build_dataset_with_a_coverage_gap(
            self.destination, gap_next_legal_indices=(2, 3)
        )
        with self.assertRaises(OfflineQProtocolError):
            train_q_model(dataset)

    def test_partially_unsupported_next_actions_fail_closed_too(self):
        """next legal {0,2}のうちTRAIN supportは{0,1}なので、action 0が
        supportedであっても action 2 がunsupportedである限りこのtransition
        全体をbootstrapへ使ってはいけない。intersectionが非空だからといって
        通してはならない。
        """
        dataset = _build_dataset_with_a_coverage_gap(
            self.destination, gap_next_legal_indices=(0, 2)
        )
        with self.assertRaises(OfflineQProtocolError):
            train_q_model(dataset)


def _stub_tensors(*, reward, terminal, next_legal_mask):
    """手計算で検証できる、小さく明示的なOfflineQSplitTensors。"""
    import torch

    count = len(reward)
    legal_mask = torch.zeros(count, VOCABULARY_SIZE, dtype=torch.bool)
    legal_mask[:, 0] = True
    legal_mask[:, 1] = True
    return OfflineQSplitTensors(
        split=Split.TRAIN,
        features=torch.zeros(count, FEATURE_DIMENSION),
        legal_mask=legal_mask,
        behavior_action_index=torch.zeros(count, dtype=torch.long),
        reward=torch.tensor(reward, dtype=torch.float32),
        terminal=torch.tensor(terminal, dtype=torch.bool),
        next_features=torch.zeros(count, FEATURE_DIMENSION),
        next_legal_mask=torch.tensor(next_legal_mask, dtype=torch.bool),
        row_indices=tuple(range(count)),
    )


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class TdTargetComputationTest(unittest.TestCase):
    """terminal / nonterminal targetの数値を、既知の出力を返すstub modelで直接検証する。"""

    def _stub_model(self, action_values: dict[int, float]):
        import torch

        class _StubQModel(torch.nn.Module):
            def forward(self, features):
                batch = features.shape[0]
                out = torch.full(
                    (batch, VOCABULARY_SIZE), float("-inf"), dtype=torch.float32
                )
                for index, value in action_values.items():
                    out[:, index] = value
                return out

        return _StubQModel()

    def test_terminal_target_equals_reward_exactly_and_ignores_the_target_model(self):
        import torch

        tensors = _stub_tensors(
            reward=[0.5, -0.3],
            terminal=[True, True],
            next_legal_mask=[[False] * VOCABULARY_SIZE] * 2,
        )
        support_mask = torch.zeros(VOCABULARY_SIZE, dtype=torch.bool)
        # A model that would blow up training if ever invoked for a terminal
        # row: it only defines Q-values for an index no terminal row could
        # legally reach.
        stub = self._stub_model({999: 1_000_000.0})
        targets = compute_td_targets(stub, tensors, support_mask)
        self.assertTrue(torch.allclose(targets, torch.tensor([0.5, -0.3])))

    def test_nonterminal_target_bootstraps_over_fully_supported_next_actions(self):
        import torch

        next_mask_row = [False] * VOCABULARY_SIZE
        next_mask_row[0] = True
        next_mask_row[1] = True
        tensors = _stub_tensors(
            reward=[1.0], terminal=[False], next_legal_mask=[next_mask_row]
        )
        support_mask = torch.zeros(VOCABULARY_SIZE, dtype=torch.bool)
        support_mask[0] = True
        support_mask[1] = True
        stub = self._stub_model({0: 2.0, 1: 5.0})
        targets = compute_td_targets(stub, tensors, support_mask)
        expected = 1.0 + GAMMA * 5.0
        self.assertTrue(torch.allclose(targets, torch.tensor([expected])))

    def test_nonterminal_target_fails_closed_when_any_next_legal_action_is_unsupported(
        self,
    ):
        """next legal {0,1,2}のうちTRAIN supportは{0,1}のみ。action 0, 1が
        supportedであっても action 2 がunsupportedである限り、locked contract
        はこのtransition全体をbootstrapへ使うことを禁じる。intersectionが
        非空（{0,1}）だからといって、その部分集合だけでmaxを取って通しては
        いけない -- 崩れた場合、次のstateで実際には選べたはずのunsupported
        actionへ暗黙にvalueが染み出す。
        """
        import torch

        next_mask_row = [False] * VOCABULARY_SIZE
        next_mask_row[0] = True
        next_mask_row[1] = True
        next_mask_row[2] = True  # legal but not TRAIN-supported
        tensors = _stub_tensors(
            reward=[1.0], terminal=[False], next_legal_mask=[next_mask_row]
        )
        support_mask = torch.zeros(VOCABULARY_SIZE, dtype=torch.bool)
        support_mask[0] = True
        support_mask[1] = True
        stub = self._stub_model({0: 2.0, 1: 5.0, 2: 100.0})
        with self.assertRaises(OfflineQProtocolError):
            compute_td_targets(stub, tensors, support_mask)


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class HardTargetSyncTest(unittest.TestCase):
    def test_target_network_is_frozen_within_an_epoch_and_resynced_between_epochs(
        self,
    ):
        import torch

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dataset = write_synthetic_dataset(tmp / "dataset", rows_per_game=6)
        tensors = load_split_tensors(dataset)

        captured: list[dict[str, torch.Tensor]] = []
        real_compute_td_targets = q_training.compute_td_targets

        def spy(target_model, tensors_arg, support_mask):
            captured.append(
                {
                    name: value.clone()
                    for name, value in target_model.state_dict().items()
                }
            )
            return real_compute_td_targets(target_model, tensors_arg, support_mask)

        with mock.patch.object(q_training, "compute_td_targets", side_effect=spy):
            q_training.train_from_split_tensors(tensors)

        # Two compute_td_targets calls per epoch: one for the TRAIN targets
        # (right after the hard sync) and one inside evaluate_huber_loss for
        # VALIDATION (same target_model, not yet re-synced).
        self.assertEqual(len(captured), MAXIMUM_EPOCHS * 2)

        def equal_state(a, b) -> bool:
            return all(torch.equal(a[name], b[name]) for name in a)

        # Frozen within epoch 1: the TRAIN-target snapshot and the
        # VALIDATION-target snapshot must be byte-identical (no online update
        # happened to the target network between them).
        self.assertTrue(equal_state(captured[0], captured[1]))

        # Re-synced between epoch 1 and epoch 2: epoch 2's target snapshot
        # must differ from epoch 1's, because the online model trained on
        # epoch 1's batches before epoch 2's sync copied its weights in.
        self.assertFalse(equal_state(captured[0], captured[2]))


if __name__ == "__main__":
    unittest.main()
