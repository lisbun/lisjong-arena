"""Project #54 current Learning Championのexact Overall serving binding。

#211 Arm Yのcheckpoint / inference semanticsは再実装せず、既存のstrict loaderと
SourcePilotRuntimeへ薄くbindする。checkpointはrepository外のoperator-owned artifact
であり、1つの明示的environment variableだけで指定する。
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

from lisjong_arena.overall_champion_aabb.protocol import ServedCheckpoint
from lisjong_arena.riichilab_source_pilot.artifact import (
    WEIGHTS_FILENAME,
    LoadedCheckpoint,
    load_checkpoint,
)
from lisjong_arena.riichilab_source_pilot.protocol import ARM_Y
from lisjong_arena.riichilab_source_pilot.serving import (
    SourcePilotRuntime,
    SourcePilotServingPolicy,
    create_serving_runtime,
)

CHECKPOINT_ENV_VAR = "LISJONG_ARENA_INITIAL_LEARNING_CHAMPION_CHECKPOINT"

POLICY_IDENTITY = (
    "learned-source-pilot-y:"
    "ecb31fc6954095041423fcfc312d0b07f952ce550f5b05f5e94238bcce7bb45e"
)
CHECKPOINT_IDENTITY = "ecb31fc6954095041423fcfc312d0b07f952ce550f5b05f5e94238bcce7bb45e"
CHECKPOINT_DIGEST = "746ef5ada2cc83d2aefbfa94cac464f03d27edb8082d22e0e455ea916dde385f"


class InitialLearningChampionBindingError(ValueError):
    """current Learning Champion serving bindingをexactに構成できない。"""


def _configured_checkpoint_path() -> Path:
    raw = os.environ.get(CHECKPOINT_ENV_VAR)
    if raw is None or not raw.strip():
        raise InitialLearningChampionBindingError(
            f"{CHECKPOINT_ENV_VAR} must name the exact #211 Arm Y checkpoint directory"
        )
    return Path(raw)


def _require_exact_checkpoint(checkpoint: LoadedCheckpoint) -> LoadedCheckpoint:
    if not isinstance(checkpoint, LoadedCheckpoint):
        raise TypeError("checkpoint must be a LoadedCheckpoint")
    if checkpoint.arm is not ARM_Y:
        raise InitialLearningChampionBindingError(
            "configured checkpoint is not the exact Arm Y source-pilot arm"
        )
    if checkpoint.identity != CHECKPOINT_IDENTITY:
        raise InitialLearningChampionBindingError(
            "configured checkpoint identity is not the current Learning Champion"
        )
    if checkpoint.policy_identity != POLICY_IDENTITY:
        raise InitialLearningChampionBindingError(
            "configured checkpoint policy identity is not the current Learning Champion"
        )
    if checkpoint.weights_sha256 != CHECKPOINT_DIGEST:
        raise InitialLearningChampionBindingError(
            "configured checkpoint weights digest is not the current Learning Champion"
        )
    return checkpoint


@cache
def _loaded_checkpoint() -> LoadedCheckpoint:
    """explicit pathの#211 artifactをstrict-loadし、exact Champion identityを要求する。"""
    return _require_exact_checkpoint(load_checkpoint(_configured_checkpoint_path()))


@cache
def _runtime() -> SourcePilotRuntime:
    """1 processにつき1回だけexact checkpointからimmutable serving runtimeを作る。"""
    return create_serving_runtime(ARM_Y, _loaded_checkpoint())


def create_initial_learning_champion_policy() -> SourcePilotServingPolicy:
    """Overall participant factory: game / seatごとにfresh Policyを返す。"""
    return _runtime().create_policy()


def served_initial_learning_champion_checkpoint() -> ServedCheckpoint:
    """factoryが実際に読むweights fileを#250 preflightへ申告する。"""
    checkpoint = _loaded_checkpoint()
    return ServedCheckpoint(
        identity=checkpoint.identity,
        path=checkpoint.path / WEIGHTS_FILENAME,
    )


__all__ = [
    "CHECKPOINT_DIGEST",
    "CHECKPOINT_ENV_VAR",
    "CHECKPOINT_IDENTITY",
    "POLICY_IDENTITY",
    "InitialLearningChampionBindingError",
    "create_initial_learning_champion_policy",
    "served_initial_learning_champion_checkpoint",
]
