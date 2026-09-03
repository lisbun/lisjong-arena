"""Stage 4a testが共有するfixture。

実RiichiEnv hanchan、teacher recording、実trainingは1回あたり分単位のcost
がかかるため、testではGate 0の生成pathを再現せず、契約上有効なcheckpoint /
freeze recordを合成してretention、freeze binding、ABBB orchestrationの境界を
検証する。production側へgeneric backend abstractionは導入しない。
"""

from _learned_policy_stage3_fixtures import build_manifest
from _round_stats_fixtures import neutral_seat_round_stats_tuple

from lisjong_arena.learned_policy_stage3.artifact import ServingCheckpoint
from lisjong_arena.learned_policy_stage3.policy import ServingRuntime
from lisjong_arena.learned_policy_stage3.protocol import ArtifactClass
from lisjong_arena.learned_policy_stage4a.candidate import (
    RetentionTarget,
    build_freeze_document,
)
from lisjong_arena.learned_policy_stage4a.evaluation import Stage4aCandidate
from lisjong_arena.model import SINGLE_ROUND_GAME_MODE, PolicySpec
from lisjong_arena.riichienv.local_game_runner import LocalGameResult

WEIGHTS = b"stage4a-synthetic-weights"


def serving_checkpoint(path=None, **manifest_overrides) -> ServingCheckpoint:
    """torchを起動せず、manifestだけを持つ検証用``ServingCheckpoint``を作る。

    ``model``はここでは使わないsentinelである。checkpoint identityは合成
    manifestの内容から実際に導出されるため、manifestを変えればidentityも変わる。
    """
    return ServingCheckpoint(
        path=path,
        manifest=build_manifest(WEIGHTS, **manifest_overrides),
        model=object(),
        artifact_class=ArtifactClass.STAGE3_FIXTURE,
        artifact_bytes=len(WEIGHTS) + 1024,
        load_wall_clock_seconds=0.25,
        load_cpu_seconds=0.2,
    )


def retention_target(root, *, backend="operator-declared-store", key="stage4a/run-1"):
    """検証をbypassして組み立てるlogical retention target。"""
    return RetentionTarget(backend=backend, root=root, key=key)


def freeze_document(root, **manifest_overrides) -> dict:
    return build_freeze_document(
        serving_checkpoint(**manifest_overrides), target=retention_target(root)
    )


class StubServingPolicy:
    """Policy contractを満たすだけのstub。unit testでは実行しない。"""

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def choose_action(self, decision):
        raise AssertionError("unit tests must not execute the learned policy")


class StubRuntimeFactory:
    """``ServingRuntime.create_policy``相当の、instanceを記録するfactory。"""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.instances = []

    def __call__(self) -> StubServingPolicy:
        instance = StubServingPolicy(self.runtime)
        self.instances.append(instance)
        return instance


def stage4a_candidate(
    freeze, checkpoint
) -> tuple[Stage4aCandidate, StubRuntimeFactory]:
    """freeze recordとcheckpointへbindしたcandidateを、実modelなしで組み立てる。"""
    runtime = ServingRuntime(
        checkpoint=checkpoint,
        conditions={"device": "cpu", "inference_mode": True},
        peak_process_ram_bytes_after_load=1024,
    )
    factory = StubRuntimeFactory(runtime)
    candidate = Stage4aCandidate(
        freeze=freeze,
        runtime=runtime,
        spec=PolicySpec(identity=freeze.candidate_identity, factory=factory),
    )
    return candidate, factory


def local_game_result(seed: int, rotation: int) -> LocalGameResult:
    """candidateがrotation席で一定の差をつける決定的なfake game result。"""
    candidate_score = 31_000 + 100 * (seed % 7)
    others = (100_000 - candidate_score) // 3
    remainder = (100_000 - candidate_score) - 2 * others
    baseline_scores = [others, others, remainder]
    scores = tuple(
        candidate_score if seat == rotation else baseline_scores.pop()
        for seat in range(4)
    )
    ordered = sorted(range(4), key=lambda seat: -scores[seat])
    ranks = tuple(ordered.index(seat) + 1 for seat in range(4))
    return LocalGameResult(
        seed=seed,
        game_mode=SINGLE_ROUND_GAME_MODE,
        scores=scores,
        ranks=ranks,
        steps=10,
        decisions=10,
        seat_round_stats=neutral_seat_round_stats_tuple(scores),
    )


def fake_single_game(policies, *, seed: int, max_steps: int) -> LocalGameResult:
    """``single_round_evaluation._run_single_game``差し替え用のfake。

    candidate seatは、そのseatへ実際に割り当てられたPolicy instanceから判定
    する。rotation指定を別経路で受け取らないので、rotationとseat割り当てが
    食い違えばtest側で検出できる。
    """
    candidate_seats = [
        seat
        for seat, policy in policies.items()
        if isinstance(policy, StubServingPolicy)
    ]
    if len(candidate_seats) != 1:
        raise AssertionError("exactly one seat must hold the candidate policy")
    return local_game_result(seed, int(candidate_seats[0]))
