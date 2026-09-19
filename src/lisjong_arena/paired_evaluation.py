"""paired seed-block evaluationのneutral Arena mechanics。

このmoduleは複数のpurpose-specific experimentが既に共有している

```text
transport     保存済みarm artifactのstrict readとbyte-level digest
validation    4-rotation seed blockのdeterministicな形式検証
aggregation   block mean -> paired delta -> paired summary statistics
```

だけを所有する。hypothesis、participant identity、ordered seed population、
Phase semantics、protocol ID、classification label / rule、evidence role、
experiment artifact schemaといったscientific contractは各purpose packageが
所有し、このlayerへ持ち込まない。

## shared primitive != shared statistical protocol

このmoduleが共有するのはmechanicsだけである。何個のseed blockを主単位に
するか、その母数でnormal-approx intervalを解釈してよいか、どのlabelへ
写像するかはexperimentごとのscientific decisionであり、共有primitiveが
存在することはすべてのexperimentが同じstatistical protocolを使うという
意味ではない。

## dependency direction

```text
purpose-specific experiment
    -> lisjong_arena.paired_evaluation
    -> existing Arena model / artifact primitives
```

このmoduleはexperiment packageをimportしない。error contextを実験固有に
保ちたいcallerは、``PairedEvaluationError``を自分のerror typeで包める。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.model import SINGLE_ROUND_ROTATION_COUNT, SingleRoundGameResult
from lisjong_arena.single_round_artifact import (
    SingleRoundStrengthArtifact,
    load_single_round_artifact,
)

SEED_BLOCK_ROTATION_COUNT = SINGLE_ROUND_ROTATION_COUNT
"""1 seed blockのrotation数(既存4-player seat rotation contractと同じ)。

現在のconsumerはいずれも4-player / 4-rotation seed blockを使う。N-player /
N-rotationへ一般化する必要は現時点で存在しないため、ここはcaller-configurable
にせずArena coreのrotation contractへ固定する。
"""

INTERVAL_Z = 1.96
"""normal-approx 95% intervalの係数(既存seed-block statisticsと同じ)。"""


class PairedEvaluationError(ValueError):
    """paired mechanicsがdeterministic validationまたは導出に失敗した場合。"""


@dataclass(frozen=True, slots=True)
class PairedSeedDelta:
    """1 ordered seed blockのpaired primary observation。

    field名は既存のstable public structureであり、``candidate_mean`` /
    ``parent_mean``はneutral namingとして完璧でなくてもrenameしない。既存
    artifact schemaのserialized field名と既存consumerの契約をそのまま保つ。
    """

    seed: int
    candidate_mean: float
    parent_mean: float
    delta: float

    def to_document(self) -> dict[str, object]:
        return {
            "candidate_mean": self.candidate_mean,
            "delta": self.delta,
            "parent_mean": self.parent_mean,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class PairedSummary:
    """paired seed blocksのprimary summary。

    何個のblockを主単位とするか、その母数でこのintervalを解釈してよいかは
    各experimentのprotocolが決める。
    """

    block_count: int
    mean_delta: float
    sample_standard_deviation: float
    standard_error: float
    interval_lower: float
    interval_upper: float

    def to_document(self) -> dict[str, object]:
        return {
            "block_count": self.block_count,
            "interval_lower": self.interval_lower,
            "interval_upper": self.interval_upper,
            "mean_delta": self.mean_delta,
            "sample_standard_deviation": self.sample_standard_deviation,
            "standard_error": self.standard_error,
        }


def focal_seed_block_means(
    game_results: tuple[SingleRoundGameResult, ...],
) -> tuple[tuple[int, float], ...]:
    """raw game resultsから``(seed, focal seat mean score)``を導出する。

    1 seed blockはrotation 0..3の4件ちょうどであり、順序と件数をここで
    fail closedする。summaryではなくraw evidenceから再導出する。どのseed
    populationが正しいかはcaller側のprotocolが決める。
    """
    if not isinstance(game_results, tuple):
        raise PairedEvaluationError("game_results must be a tuple")
    if not game_results:
        raise PairedEvaluationError("game_results must not be empty")
    if len(game_results) % SEED_BLOCK_ROTATION_COUNT != 0:
        raise PairedEvaluationError(
            "game_results must contain exactly four rotations per seed block"
        )

    blocks: list[tuple[int, float]] = []
    for offset in range(0, len(game_results), SEED_BLOCK_ROTATION_COUNT):
        block = game_results[offset : offset + SEED_BLOCK_ROTATION_COUNT]
        seed = block[0].seed
        if any(item.seed != seed for item in block):
            raise PairedEvaluationError(
                "each seed block must contain records for the same seed"
            )
        if tuple(item.rotation for item in block) != tuple(
            range(SEED_BLOCK_ROTATION_COUNT)
        ):
            raise PairedEvaluationError(
                "each seed block must contain rotations 0, 1, 2, 3 in order"
            )
        if tuple(int(item.candidate_seat) for item in block) != tuple(
            range(SEED_BLOCK_ROTATION_COUNT)
        ):
            raise PairedEvaluationError(
                "each seed block must place the focal seat in rotations 0, 1, 2, 3"
            )
        blocks.append(
            (
                seed,
                sum(item.candidate_score for item in block) / SEED_BLOCK_ROTATION_COUNT,
            )
        )
    return tuple(blocks)


def paired_deltas_from_block_means(
    candidate_blocks: tuple[tuple[int, float], ...],
    parent_blocks: tuple[tuple[int, float], ...],
) -> tuple[PairedSeedDelta, ...]:
    """2 armのblock meansを同じseed順で突き合わせてpaired deltaを作る。

    ここが確認するのはmechanicalな対応関係、すなわち2 armが同じ件数の
    同じordered seedsを提出していることだけである。そのseed列がexperiment
    のlocked populationかどうかはcaller側のprotocolが判定する。
    """
    if not isinstance(candidate_blocks, tuple) or not isinstance(parent_blocks, tuple):
        raise PairedEvaluationError("block means must be tuples")
    if not candidate_blocks:
        raise PairedEvaluationError("paired deltas need at least one seed block")
    if len(candidate_blocks) != len(parent_blocks):
        raise PairedEvaluationError(
            "both arms must contribute the same number of paired seed blocks"
        )
    if tuple(seed for seed, _ in candidate_blocks) != tuple(
        seed for seed, _ in parent_blocks
    ):
        raise PairedEvaluationError("both arms must use the same ordered seeds")
    return tuple(
        PairedSeedDelta(
            seed=seed,
            candidate_mean=candidate_mean,
            parent_mean=parent_mean,
            delta=candidate_mean - parent_mean,
        )
        for (seed, candidate_mean), (_, parent_mean) in zip(
            candidate_blocks, parent_blocks, strict=True
        )
    )


def summarize_paired_deltas(
    deltas: tuple[PairedSeedDelta, ...],
) -> PairedSummary:
    """paired ``D_s``のmeanとnormal-approx 95% intervalを導出する。"""
    if not isinstance(deltas, tuple) or not deltas:
        raise PairedEvaluationError("deltas must be a non-empty tuple")
    if len(deltas) < 2:
        raise PairedEvaluationError(
            "a normal-approx interval needs at least two paired seed blocks"
        )
    values = [item.delta for item in deltas]
    count = len(values)
    mean_delta = sum(values) / count
    variance = sum((value - mean_delta) ** 2 for value in values) / (count - 1)
    sample_standard_deviation = math.sqrt(variance)
    standard_error = sample_standard_deviation / math.sqrt(count)
    return PairedSummary(
        block_count=count,
        mean_delta=mean_delta,
        sample_standard_deviation=sample_standard_deviation,
        standard_error=standard_error,
        interval_lower=mean_delta - INTERVAL_Z * standard_error,
        interval_upper=mean_delta + INTERVAL_Z * standard_error,
    )


def artifact_file_digest(path: str | Path) -> str:
    """保存済みarm artifact fileのsha256 digestを返す。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def arm_diagnostics(artifact: SingleRoundStrengthArtifact) -> dict[str, object]:
    """既存canonical summaryからsecondary diagnosticsを抜き出す。

    値はすべて既存canonical aggregationの結果であり、新しい統計semanticsは
    導入しない。``tenpai_reached_rate``だけはcanonical metricsが持たないため
    同じ母数(``round_count``)でここで割るが、これはarm-localなdescriptive
    derivationであり、共有aggregationへは足さない。scientific classificationは
    このdocumentを参照しない。
    """
    metrics = artifact.summary.candidate_metrics
    mahjong = metrics.mahjong_metrics
    return {
        "deal_in_count": mahjong.deal_in_count,
        "deal_in_rate": mahjong.deal_in_rate,
        "exhaustive_draw_count": mahjong.exhaustive_draw_count,
        "exhaustive_draw_tenpai_count": mahjong.exhaustive_draw_tenpai_count,
        "exhaustive_draw_tenpai_rate": mahjong.exhaustive_draw_tenpai_rate,
        "game_count": metrics.game_count,
        "mean_deal_in_loss": mahjong.mean_deal_in_loss,
        "mean_first_tenpai_turn": mahjong.mean_first_tenpai_turn,
        "mean_focal_seat_score": metrics.mean_candidate_score,
        "mean_win_points": mahjong.mean_win_points,
        "round_count": mahjong.round_count,
        "tenpai_reached_count": mahjong.tenpai_reached_count,
        "tenpai_reached_rate": (
            None
            if mahjong.round_count == 0
            else mahjong.tenpai_reached_count / mahjong.round_count
        ),
        "win_count": mahjong.win_count,
        "win_rate": mahjong.win_rate,
    }


def load_arm_artifact(path: str | Path) -> SingleRoundStrengthArtifact:
    """arm artifactを既存strict readbackで読み戻す。"""
    return load_single_round_artifact(path)


__all__ = [
    "INTERVAL_Z",
    "SEED_BLOCK_ROTATION_COUNT",
    "PairedEvaluationError",
    "PairedSeedDelta",
    "PairedSummary",
    "arm_diagnostics",
    "artifact_file_digest",
    "focal_seed_block_means",
    "load_arm_artifact",
    "paired_deltas_from_block_means",
    "summarize_paired_deltas",
]
