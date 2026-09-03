"""Synthetic Stage 3 serving-checkpoint fixtures.

実RiichiEnv hanchanとteacher recordingは1局あたり分単位のcostがかかるため、
unit testではPath B fixtureの生成経路を再現せず、契約上有効なcheckpoint
artifactを合成してloader / adapter boundaryを検証する。production側へgeneric
backend abstractionは導入しない。
"""

import hashlib

from lisjong.policy_contract import DecisionContext, Seat
from lisjong.policy_contract.action import DiscardAction

from lisjong_arena._artifact_io import canonical_json_text
from lisjong_arena.learned_policy_stage2.artifact import feature_block, vocabulary_block
from lisjong_arena.learned_policy_stage2.training import (
    CHECKPOINT_SCHEMA_VERSION,
    MANIFEST_FILENAME,
    WEIGHTS_FILENAME,
    checkpoint_identity,
    locked_model_block,
    locked_training_block,
)
from lisjong_arena.learned_policy_stage3.artifact import (
    FIXTURE_CHECKPOINT_SCHEMA_VERSION,
)
from lisjong_arena.learned_policy_stage3.fixture import FIXTURE_NOTE, FIXTURE_ORIGIN
from lisjong_arena.learned_policy_stage3.protocol import (
    EXCLUDED_STAGE2_TEST_SEEDS,
    FIXTURE_TRAIN_SEEDS,
    FIXTURE_VALIDATION_SEEDS,
    PROTOCOL_ID,
)

FIXTURE_PROVENANCE = {
    "execution_environment": "riichienv",
    "lisjong_arena_version": "0.1.0",
    "lisjong_arena_revision": "0" * 40,
    "lisjong_version": "0.1.0",
    "lisjong_revision": "a0666d24e66179a45fd6e231a3cbd489b492d162",
    "lisjong_engine_version": "0.1.0",
    "lisjong_engine_revision": "2" * 40,
    "riichienv_version": "0.4.8",
    "python_version": "3.14.0",
}


def fixture_block(**overrides) -> dict:
    block = {
        "origin": FIXTURE_ORIGIN,
        "protocol_id": PROTOCOL_ID,
        "train_seeds": list(FIXTURE_TRAIN_SEEDS),
        "validation_seeds": list(FIXTURE_VALIDATION_SEEDS),
        "excluded_stage2_test_seeds": list(EXCLUDED_STAGE2_TEST_SEEDS),
        "row_count": 1234,
        "teacher_identity": "yakuhai-call",
        "teacher_source_revision": "a0666d24e66179a45fd6e231a3cbd489b492d162",
        "stage2_checkpoint_identity": None,
        "note": FIXTURE_NOTE,
    }
    block.update(overrides)
    return block


def build_manifest(weights: bytes, **overrides) -> dict:
    """locked contractを満たすStage 3 fixture manifestを組み立てる。"""
    manifest: dict = {
        "checkpoint_schema_version": FIXTURE_CHECKPOINT_SCHEMA_VERSION,
        "dataset_identity": "f" * 64,
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "model": locked_model_block(),
        "training": locked_training_block(),
        "parameter_count": 1_153_698,
        "selected_epoch": 3,
        "selected_validation_choice_masked_ce": 1.5,
        "epoch_history": [],
        "weights_bytes": len(weights),
        "weights_sha256": hashlib.sha256(weights).hexdigest(),
        "fixture": fixture_block(),
        "provenance": dict(FIXTURE_PROVENANCE),
        "runtime": {"torch_threads": 1},
    }
    manifest.update(overrides)
    manifest["checkpoint_identity"] = checkpoint_identity(manifest)
    return manifest


def write_checkpoint(
    path, *, manifest_edit=None, weights_edit=None, rehash_weights=False
):
    """合成checkpointをdirectoryへ書き出す。

    `manifest_edit`はcheckpoint_identity確定後のmanifestを、`weights_edit`は
    weights bytesを、それぞれfail-closed pathの検証のために壊すためのhookで
    ある。`rehash_weights=True`のときはmanifestのbyte count / sha256を壊した
    weightsに対して計算し直し、digestが一致したままpayloadだけが壊れている
    artifactを作る。
    """
    import torch

    from lisjong_arena.learned_policy_stage2.network import create_model

    path.mkdir(parents=True, exist_ok=True)
    weights_path = path / WEIGHTS_FILENAME
    torch.manual_seed(0)
    torch.save(create_model().state_dict(), weights_path)
    weights = weights_path.read_bytes()
    if weights_edit is not None and rehash_weights:
        weights = weights_edit(weights)
        weights_path.write_bytes(weights)
        weights_edit = None

    manifest = build_manifest(weights)
    if manifest_edit is not None:
        manifest = manifest_edit(manifest)
    if weights_edit is not None:
        weights_path.write_bytes(weights_edit(weights))
    (path / MANIFEST_FILENAME).write_text(
        canonical_json_text(manifest), encoding="utf-8", newline="\n"
    )
    return path


def build_stage2_schema_manifest(weights: bytes) -> dict:
    """fixture blockを持たない、Stage 2 schemaのretained checkpoint manifest。"""
    manifest = build_manifest(weights)
    manifest.pop("fixture")
    manifest.pop("provenance")
    manifest["checkpoint_schema_version"] = CHECKPOINT_SCHEMA_VERSION
    manifest["checkpoint_identity"] = checkpoint_identity(manifest)
    return manifest


def write_stage2_schema_checkpoint(path):
    """Path A相当（Stage 2 schema）のcheckpointを合成する。"""
    import torch

    from lisjong_arena.learned_policy_stage2.network import create_model

    path.mkdir(parents=True, exist_ok=True)
    weights_path = path / WEIGHTS_FILENAME
    torch.manual_seed(0)
    torch.save(create_model().state_dict(), weights_path)
    (path / MANIFEST_FILENAME).write_text(
        canonical_json_text(build_stage2_schema_manifest(weights_path.read_bytes())),
        encoding="utf-8",
        newline="\n",
    )
    return path


def discard_decision(tiles) -> DecisionContext:
    """打牌候補だけからなる、単純で合法なDecisionContextを作る。"""
    from _learned_policy_input_fixtures import minimal_policy_input

    return DecisionContext(
        input=minimal_policy_input(own_tiles=tuple(tiles)),
        legal_actions=tuple(DiscardAction(Seat.SEAT_0, item, False) for item in tiles),
    )
