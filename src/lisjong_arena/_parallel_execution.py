"""AABB / ABBBで共通な、(seed, rotation)単位のlocal process並列executionの
最小限のprivate orchestration。

``lisjong_arena.comparison``のAABB comparisonと
``lisjong_arena.single_round_evaluation``のABBB single-round評価は、
seat assignment、raw result構築、validation、aggregationという意味で異なる
protocolであり、それぞれの所有物として各moduleへ残す。ここへ集約するのは、
両者で明らかに同一な「1 (seed, rotation) gameをworker process内部でfresh
Policyから実行する」というprocess orchestrationだけである。

Python標準libraryの``concurrent.futures.ProcessPoolExecutor``と明示的な
``multiprocessing``のspawn contextだけを使い、外部parallelism dependencyは
追加しない。forkで偶然継承されるglobal state、parent processのmutable
stateには依存しない。

``GenericEvaluationExecutor`` / ``GameBackend`` / ``EvaluationBackend`` /
``PluginExecutor``のような汎用backend abstractionはここでも導入しない。
"""

from __future__ import annotations

import multiprocessing
import pickle
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass

from lisjong.policy_contract import Seat

from lisjong_arena.model import PolicySpec
from lisjong_arena.riichienv.local_game_runner import LocalGameResult, LocalGameRunner

_SPAWN_CONTEXT = multiprocessing.get_context("spawn")
"""fork依存のglobal state継承に頼らないための明示的なspawn context。"""


class PolicyFactoryNotSerializableError(Exception):
    """並列execution向けにPolicySpec.factoryがprocess間serialize不能な場合。

    既存``PolicySpec``のpublic contractはserial evaluationのため一般の
    callableを許容し続ける（``PolicySpec.__post_init__``は変更しない）。
    この制約はparallel APIを利用する場合だけに適用し、lambdaやlocal
    closureのような、spawn workerへ安全に渡せないfactoryをsilentに
    serial実行へfallbackせず、ここで明示的にfail closedする。
    """

    __slots__ = ("identity",)

    def __init__(self, message: str, *, identity: str) -> None:
        super().__init__(f"policy identity {identity!r}: {message}")
        self.identity = identity


def validate_max_workers(max_workers: object) -> int:
    """``max_workers``がpositive intであることをfail closedで検証する。

    ``max_workers``はexecution performance設定であり、comparison /
    evaluationの意味を決めるdomain inputではないので、resultのどのfieldへも
    含めない。
    """
    if type(max_workers) is not int:
        raise TypeError("max_workers must be an int")
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")
    return max_workers


def check_policy_spec_serializable(spec: PolicySpec) -> None:
    """parallel実行前に``spec.factory``のspawn互換性をpreflightする。

    ここでの``pickle.dumps()``成功はspawn互換性の証明ではない。明らかに
    壊れているfactory（lambdaやlocal closure等）を、実際にworker process群を
    起動する前に安価に検出するための最小限のgateにすぎない。実際にspawn
    worker内でmodule import / factory resolution / Policy instance生成まで
    成功することは、これとは別にintegration testで検証する。
    """
    try:
        pickle.dumps(spec.factory)
    except Exception as exc:
        raise PolicyFactoryNotSerializableError(
            "factory is not process-serializable for parallel execution "
            "(e.g. a lambda or local closure); use an importable top-level "
            "callable instead",
            identity=spec.identity,
        ) from exc


@dataclass(frozen=True, slots=True)
class GameJob:
    """1 (seed, rotation) gameをworker processへ渡す最小のpicklable value。

    ``assignment``はSeat 0..3順のPolicySpec割り当てである。Policy instance
    そのものは含まない。worker process内部でfactoryから生成する。
    """

    seed: int
    rotation: int
    assignment: tuple[PolicySpec, PolicySpec, PolicySpec, PolicySpec]
    game_mode: str
    max_steps: int


@dataclass(frozen=True, slots=True)
class GameJobOutcome:
    """1 ``GameJob``の実行結果。

    成功時は``result``が入り``error_text``は``None``、失敗時はその逆になる。
    ``error_text``は元例外のtype / message / tracebackを含むplain textで
    あり、worker <-> parent間で必ずpickleできる形へ失敗を落とし込む。呼び出し
    側はこれを``ComparisonExecutionError`` / ``SingleRoundEvaluationError``
    のような既存domain exceptionの``__cause__``へ変換する。
    """

    seed: int
    rotation: int
    result: LocalGameResult | None
    error_text: str | None


def _run_game_job(job: GameJob) -> GameJobOutcome:
    """worker process内部でfresh Policyを生成し1 gameを実行する。

    ``ProcessPoolExecutor``へ提出するtop-level関数はこれだけである。parent
    processで生成したPolicy instanceは一切参照せず、``job.assignment``の
    factoryをここで初めて呼び出す。Policy factory failureとgame execution
    failureのどちらも例外を外へ伝播させず``GameJobOutcome.error_text``へ
    変換する。任意のPolicy実装が送出しうる例外はworker <-> parent間で
    pickleできるとは限らないため、ここでplain textへ変換してから返す。
    """
    policies = {}
    for seat in Seat:
        spec = job.assignment[seat]
        try:
            policies[seat] = spec.factory()
        except Exception:
            return GameJobOutcome(
                seed=job.seed,
                rotation=job.rotation,
                result=None,
                error_text=(
                    f"policy factory for identity {spec.identity!r} failed "
                    f"at {seat!r}:\n{traceback.format_exc()}"
                ),
            )

    try:
        result = LocalGameRunner(
            policies,
            seed=job.seed,
            game_mode=job.game_mode,
            max_steps=job.max_steps,
        ).run()
    except Exception:
        return GameJobOutcome(
            seed=job.seed,
            rotation=job.rotation,
            result=None,
            error_text=f"single game execution failed:\n{traceback.format_exc()}",
        )

    return GameJobOutcome(
        seed=job.seed, rotation=job.rotation, result=result, error_text=None
    )


def run_game_jobs(
    jobs: Sequence[GameJob],
    *,
    max_workers: int,
    game_runner: Callable[[GameJob], GameJobOutcome] = _run_game_job,
) -> dict[tuple[int, int], GameJobOutcome]:
    """独立した``(seed, rotation)`` job群をlocal process poolで実行する。

    完了順序は``dict``のkeyである``(seed, rotation)``でlookupする前提であり、
    呼び出し側がcanonical orderへ組み直す責務を持つ（AABB / ABBBで並び順の
    意味が異なるため、canonical order化はprotocol固有moduleの責務として残す）。

    workerの完了を待ち切ってから返すため、1 job失敗時にまだ実行中の他jobを
    強制terminateすることはない。呼び出し側はcanonical order上で最初に
    見つかったfailureを報告することで、失敗の報告自体もprocess completion
    順序に依存しない決定的な挙動にできる。

    ``game_runner``はtestのためだけの差し替え口であり、既定値は実際に
    ``LocalGameRunner``を実行する``_run_game_job``である。public parallel
    entry point（``run_comparison_parallel`` / ``run_single_round_evaluation_parallel``）
    はこれを上書きしない。
    """
    validate_max_workers(max_workers)

    outcomes: dict[tuple[int, int], GameJobOutcome] = {}
    if not jobs:
        return outcomes

    with ProcessPoolExecutor(
        max_workers=max_workers, mp_context=_SPAWN_CONTEXT
    ) as executor:
        futures = {executor.submit(game_runner, job): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                outcome = future.result()
            except BaseException as exc:
                outcome = GameJobOutcome(
                    seed=job.seed,
                    rotation=job.rotation,
                    result=None,
                    error_text=(
                        "worker process failed before returning a result:\n"
                        + "".join(
                            traceback.format_exception(
                                type(exc), exc, exc.__traceback__
                            )
                        )
                    ),
                )
            outcomes[(job.seed, job.rotation)] = outcome

    return outcomes


__all__ = [
    "GameJob",
    "GameJobOutcome",
    "PolicyFactoryNotSerializableError",
    "check_policy_spec_serializable",
    "run_game_jobs",
    "validate_max_workers",
]
