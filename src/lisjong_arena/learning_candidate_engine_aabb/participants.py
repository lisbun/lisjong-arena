"""participant binding document（#562）。

lockがbindするのはPolicy instanceやfactoryではなく、明示的なidentityと、
実際にloadしたartifactから作ったbinding documentである。workerはload後に同じ
documentを作り直し、lockと完全一致しなければ対局を始めない。

```text
A（candidate）  L2a合成Policy（lisjong ``load_l2a_policy_factory``）
                打牌・反応artifactごとに identity、manifest / weightsのSHA-256、
                encoding・分類・head、source / dataset identity、学習時lisjong
                serving: 実際にimportしたlisjong revision
B（incumbent）  current Learning Champion Arm Y（``initial_learning_champion``）
                checkpoint identity document（source・feature・vocabularyを含む）、
                manifest / weightsのSHA-256、policy identity
                serving: Arena revision（historical servingの再利用）
```

artifactのpathは実行環境の値なのでbindingに含めない（lockの ``execution`` が持つ）。
"""

import hashlib
import re
from pathlib import Path

from .protocol import (
    CANDIDATE_CLASSIFICATION,
    CANDIDATE_FACTORY,
    CANDIDATE_KIND,
    INCUMBENT_CHECKPOINT_IDENTITY,
    INCUMBENT_FACTORY,
    INCUMBENT_KIND,
    INCUMBENT_POLICY_IDENTITY,
    INCUMBENT_WEIGHTS_SHA256,
    PARTICIPANT_FAMILY,
    LearningEngineAabbError,
    require_hex64,
)

L2A_MANIFEST_FILENAME = "manifest.json"
L2A_WEIGHTS_FILENAME = "weights.f32"
INCUMBENT_ARM = "ARM_Y"
"""``riichilab_source_pilot.protocol.ARM_Y.value``（testで一致を固定する）。"""
HEADS = {"discard": "discard", "reaction": "call"}
"""L2a artifactの役割 -> ``L2aDecisionKind`` のhead値。"""

_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEAD_FIELDS = frozenset(
    {
        "classification",
        "dataset_identity",
        "encoding_identity",
        "head",
        "identity",
        "manifest_sha256",
        "source_identity",
        "training_lisjong",
        "weights_sha256",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "artifacts",
        "classification",
        "factory_binding",
        "family",
        "kind",
        "policy_identity",
        "serving",
    }
)
_INCUMBENT_FIELDS = frozenset(
    {
        "checkpoint",
        "checkpoint_manifest_sha256",
        "factory_binding",
        "family",
        "kind",
        "policy_identity",
        "serving",
        "weights_sha256",
    }
)


class ParticipantBindingError(LearningEngineAabbError):
    """participant bindingが作れない、または契約・lockと一致しない場合。"""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidate_policy_identity(discard_identity: str, reaction_identity: str) -> str:
    """candidateの明示的identity。class名からは導かない。"""
    return f"lisjong-l2a:{discard_identity}:{reaction_identity}"


def _require_revision(value: object, context: str) -> str:
    if type(value) is not str or not _HEX40.fullmatch(value):
        raise ParticipantBindingError(f"{context} must be a 40-hex revision")
    return value


# ---------------------------------------------------------------------------
# build（loadしたobjectから）
# ---------------------------------------------------------------------------


def l2a_head_binding(scorer: object, path: str | Path, role: str) -> dict:
    """strict loadしたL2a scorer 1つのbinding。fileのhashはdisk上の実体から計算する。"""
    artifact = scorer.artifact
    manifest = artifact.manifest
    root = Path(path)
    weights_sha256 = file_sha256(root / L2A_WEIGHTS_FILENAME)
    recorded = manifest["files"][L2A_WEIGHTS_FILENAME]["sha256"]
    if recorded != weights_sha256:
        raise ParticipantBindingError(
            f"{role} weights file hash differs from its manifest"
        )
    encoding = manifest["encoding"]
    document = {
        "classification": encoding["classification"],
        "dataset_identity": manifest["dataset"]["identity"],
        "encoding_identity": encoding["identity"],
        "head": scorer.encoding.head.value,
        "identity": artifact.identity,
        "manifest_sha256": file_sha256(root / L2A_MANIFEST_FILENAME),
        "source_identity": manifest["source"]["identity"],
        "training_lisjong": dict(manifest["lisjong"]),
        "weights_sha256": weights_sha256,
    }
    return _parse_head(document, role)


def candidate_document(
    runtime: object,
    *,
    discard_path: str | Path,
    reaction_path: str | Path,
    lisjong_revision: str,
) -> dict:
    """L2a runtime（打牌・反応scorer）からcandidate documentを作る。"""
    discard = l2a_head_binding(runtime.discard, discard_path, "discard")
    reaction = l2a_head_binding(runtime.reaction, reaction_path, "reaction")
    if tuple(runtime.identity) != (discard["identity"], reaction["identity"]):
        raise ParticipantBindingError("L2a runtime identity differs from its artifacts")
    document = {
        "artifacts": {"discard": discard, "reaction": reaction},
        "classification": CANDIDATE_CLASSIFICATION,
        "factory_binding": CANDIDATE_FACTORY,
        "family": PARTICIPANT_FAMILY,
        "kind": CANDIDATE_KIND,
        "policy_identity": candidate_policy_identity(
            discard["identity"], reaction["identity"]
        ),
        "serving": {"lisjong_revision": lisjong_revision},
    }
    return parse_candidate(document)


def incumbent_document(checkpoint: object, *, arena_revision: str) -> dict:
    """strict loadしたArm Y checkpointからincumbent documentを作る。"""
    from lisjong_arena.riichilab_source_pilot.artifact import (
        MANIFEST_FILENAME,
        WEIGHTS_FILENAME,
    )

    root = Path(checkpoint.path)
    weights_sha256 = file_sha256(root / WEIGHTS_FILENAME)
    if weights_sha256 != checkpoint.weights_sha256:
        raise ParticipantBindingError(
            "Arm Y weights file hash differs from its manifest"
        )
    document = {
        "checkpoint": dict(checkpoint.identity_document()),
        "checkpoint_manifest_sha256": file_sha256(root / MANIFEST_FILENAME),
        "factory_binding": INCUMBENT_FACTORY,
        "family": PARTICIPANT_FAMILY,
        "kind": INCUMBENT_KIND,
        "policy_identity": checkpoint.policy_identity,
        "serving": {"arena_revision": arena_revision},
        "weights_sha256": weights_sha256,
    }
    return parse_incumbent(document)


# ---------------------------------------------------------------------------
# parse（lockの読込・workerの照合）
# ---------------------------------------------------------------------------


def _fields(value: object, fields: frozenset[str], context: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise ParticipantBindingError(f"{context} fields do not match the schema")
    return value


def _hex64(value: object, context: str) -> str:
    try:
        return require_hex64(value, context)
    except LearningEngineAabbError as error:
        raise ParticipantBindingError(str(error)) from error


def _text(value: object, context: str) -> str:
    if type(value) is not str or not value:
        raise ParticipantBindingError(f"{context} must be a non-empty string")
    return value


def _parse_head(value: object, role: str) -> dict:
    context = f"candidate.artifacts.{role}"
    _fields(value, _HEAD_FIELDS, context)
    for name in ("identity", "manifest_sha256", "weights_sha256"):
        _hex64(value[name], f"{context}.{name}")
    if value["head"] != HEADS[role]:
        raise ParticipantBindingError(f"{context} must have the {HEADS[role]} head")
    if value["classification"] != CANDIDATE_CLASSIFICATION:
        raise ParticipantBindingError(f"{context} binds a different L2a classification")
    for name in ("dataset_identity", "encoding_identity", "source_identity"):
        _text(value[name], f"{context}.{name}")
    if type(value["training_lisjong"]) is not dict or not value["training_lisjong"]:
        raise ParticipantBindingError(f"{context}.training_lisjong must be an object")
    return value


def parse_candidate(value: object) -> dict:
    """candidate documentを厳密に検査する（live値は見ない）。"""
    _fields(value, _CANDIDATE_FIELDS, "candidate")
    if (
        value["family"] != PARTICIPANT_FAMILY
        or value["kind"] != CANDIDATE_KIND
        or value["factory_binding"] != CANDIDATE_FACTORY
        or value["classification"] != CANDIDATE_CLASSIFICATION
    ):
        raise ParticipantBindingError(
            "candidate must be the lisjong L2a composite Policy of the Learning family"
        )
    artifacts = value["artifacts"]
    if type(artifacts) is not dict or set(artifacts) != set(HEADS):
        raise ParticipantBindingError(
            "candidate.artifacts must be discard and reaction"
        )
    discard = _parse_head(artifacts["discard"], "discard")
    reaction = _parse_head(artifacts["reaction"], "reaction")
    if discard["identity"] == reaction["identity"]:
        raise ParticipantBindingError("discard and reaction artifacts must differ")
    if discard["source_identity"] != reaction["source_identity"]:
        raise ParticipantBindingError(
            "L2a artifacts were trained from different sources"
        )
    expected = candidate_policy_identity(discard["identity"], reaction["identity"])
    if value["policy_identity"] != expected:
        raise ParticipantBindingError(
            "candidate.policy_identity is not derived from M / R"
        )
    serving = _fields(
        value["serving"], frozenset({"lisjong_revision"}), "candidate.serving"
    )
    _require_revision(serving["lisjong_revision"], "candidate.serving.lisjong_revision")
    return value


def parse_incumbent(value: object) -> dict:
    """incumbent documentを厳密に検査する。current Learning Champion以外は受理しない。"""
    _fields(value, _INCUMBENT_FIELDS, "incumbent")
    if (
        value["family"] != PARTICIPANT_FAMILY
        or value["kind"] != INCUMBENT_KIND
        or value["factory_binding"] != INCUMBENT_FACTORY
    ):
        raise ParticipantBindingError("incumbent must be the Arm Y serving binding")
    if value["policy_identity"] != INCUMBENT_POLICY_IDENTITY:
        raise ParticipantBindingError("incumbent is not the current Learning Champion")
    if value["weights_sha256"] != INCUMBENT_WEIGHTS_SHA256:
        raise ParticipantBindingError("incumbent weights are not the exact checkpoint")
    _hex64(value["checkpoint_manifest_sha256"], "incumbent manifest hash")
    checkpoint = value["checkpoint"]
    if type(checkpoint) is not dict:
        raise ParticipantBindingError("incumbent.checkpoint must be an object")
    if (
        checkpoint.get("checkpoint_identity") != INCUMBENT_CHECKPOINT_IDENTITY
        or checkpoint.get("weights_sha256") != INCUMBENT_WEIGHTS_SHA256
        or checkpoint.get("arm") != INCUMBENT_ARM
    ):
        raise ParticipantBindingError(
            "incumbent checkpoint identity is not the current Learning Champion"
        )
    for name in ("feature", "source", "vocabulary"):
        if type(checkpoint.get(name)) is not dict:
            raise ParticipantBindingError(f"incumbent.checkpoint.{name} is missing")
    serving = _fields(
        value["serving"], frozenset({"arena_revision"}), "incumbent.serving"
    )
    _require_revision(serving["arena_revision"], "incumbent.serving.arena_revision")
    return value


def require_pair(candidate: dict, incumbent: dict) -> None:
    if candidate["policy_identity"] == incumbent["policy_identity"]:
        raise ParticipantBindingError("candidate and incumbent identities must differ")


__all__ = [
    "HEADS",
    "ParticipantBindingError",
    "candidate_document",
    "candidate_policy_identity",
    "file_sha256",
    "incumbent_document",
    "l2a_head_binding",
    "parse_candidate",
    "parse_incumbent",
    "require_pair",
]
