"""Issue #211 bundle testが共有するABBB strength artifact fixture。

400 gameをplayせず、既存``SingleRoundEvaluationResult``契約を満たすraw game
resultsだけを組み立ててlocked seed populationのartifactを保存する。identityは
callerが与えるため、同じ形で「別比較のartifact」も作れる。
"""

from pathlib import Path

from _single_round_artifact_fixtures import game_results, save

from lisjong_arena.model import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    SingleRoundEvaluationResult,
)
from lisjong_arena.riichilab_source_pilot.protocol import EVALUATION_SEEDS
from lisjong_arena.single_round_evaluation import aggregate_candidate_metrics


def _no_policy():  # pragma: no cover - gameをplayしないためのplaceholder
    raise AssertionError("the strength artifact fixture never plays a game")


def save_strength_artifact(
    path: str | Path, candidate_identity: str, baseline_identity: str
) -> None:
    """locked populationのstrength artifactを保存する。"""
    plan = SingleRoundEvaluationPlan(
        candidate=PolicySpec(identity=candidate_identity, factory=_no_policy),
        baseline=PolicySpec(identity=baseline_identity, factory=_no_policy),
        seeds=EVALUATION_SEEDS,
    )
    results = game_results(EVALUATION_SEEDS)
    save(
        SingleRoundEvaluationResult(
            plan=plan,
            game_results=results,
            candidate_metrics=aggregate_candidate_metrics(candidate_identity, results),
        ),
        Path(path),
    )
