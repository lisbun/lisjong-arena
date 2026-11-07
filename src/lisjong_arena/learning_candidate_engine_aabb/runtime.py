"""worker内のML runtime・participant load・1半荘実行（#562）。

```text
worker（spawn、1 process。全半荘で同じprocessを使う）
  environment_document()      Rust backend・wheel・provenance・Python
                              + ML条件（Arm Yの既存deterministic設定を先に適用して実測）
  load_participants()         1 processにつき1回だけ
    ML条件を適用・実測  -> L2a M / R をstrict load -> 実測
                       -> Arm Y checkpointをstrict load（既存binding） -> 実測
    3回の実測が同じでなければ止める（load順で条件が変わらないこと）
    loadしたobjectからbinding documentを作り直す（lockと照合する値）
  play_learning_hanchan()     lockのbindingと完全一致を要求 -> seatごとに新しいPolicy
                              -> run_policy_hanchan -> capture・判断時間・peak RSS
```

共有するのはimmutableなeval-mode modelだけで、Policy instanceは半荘・seatごとに
factoryから新しく作る。判断時間は実行観測であり、Policy入力・recordへは流さない。
"""

import os
import platform
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lisjong_engine.seat import Seat as EngineSeat

from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    PlayedHanchan,
    current_peak_rss_kib,
)
from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    environment_document as engine_environment_document,
)
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    ROLE_CANDIDATE,
    SeatAssignment,
    placement_roles,
)
from lisjong_arena.heuristic_candidate_engine_aabb.protocol import locked_rules
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import HanchanCapture
from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

from .participants import (
    ParticipantBindingError,
    candidate_document,
    incumbent_document,
)
from .protocol import ML_RUNTIME, LearningEngineAabbError

ARTIFACT_ROLES = ("champion", "discard", "reaction")


class LearningRuntimeError(LearningEngineAabbError):
    """ML runtime・participantのloadや照合が契約を満たさない場合。"""


# ---------------------------------------------------------------------------
# ML conditions
# ---------------------------------------------------------------------------


def measured_ml_runtime() -> dict[str, object]:
    """このprocessで今有効なML条件（設定はしない）。"""
    import torch

    return {
        "cuda_available": bool(torch.cuda.is_available()),
        "deterministic_algorithms": bool(torch.are_deterministic_algorithms_enabled()),
        "device": "cpu",
        "python_version": platform.python_version(),
        "torch_threads": torch.get_num_threads(),
        "torch_version": torch.__version__,
    }


def configure_ml_runtime() -> dict[str, object]:
    """Arm Yの既存deterministic CPU条件を適用し、実測値を返す。"""
    from lisjong_arena.learned_policy_stage2.training import (
        configure_deterministic_runtime,
    )

    configure_deterministic_runtime()
    return require_ml_runtime(measured_ml_runtime())


def require_ml_runtime(conditions: object) -> dict[str, object]:
    """固定したML条件（threads・determinism・CPU）を満たすことを要求する。"""
    if type(conditions) is not dict or set(conditions) != {
        *ML_RUNTIME,
        "python_version",
        "torch_version",
    }:
        raise LearningRuntimeError("ML conditions do not match the schema")
    for name, value in ML_RUNTIME.items():
        if conditions[name] != value or type(conditions[name]) is not type(value):
            raise LearningRuntimeError(
                f"ML condition {name}={conditions[name]!r} differs from {value!r}"
            )
    for name in ("python_version", "torch_version"):
        if type(conditions[name]) is not str or not conditions[name]:
            raise LearningRuntimeError(f"ML condition {name} is missing")
    return conditions


def environment_document(wheel_path: str) -> dict[str, object]:
    """lockに記録し、各workerの値と完全一致を要求する実行環境。"""
    environment = engine_environment_document(wheel_path)
    return {**environment, "ml": configure_ml_runtime()}


# ---------------------------------------------------------------------------
# participants
# ---------------------------------------------------------------------------


def require_artifact_paths(value: object) -> dict[str, str]:
    """3 artifactの絶対path（champion / discard / reaction）。"""
    if type(value) is not dict or set(value) != set(ARTIFACT_ROLES):
        raise LearningRuntimeError(f"artifact paths must be exactly {ARTIFACT_ROLES}")
    for role, path in value.items():
        if type(path) is not str or not Path(path).is_absolute():
            raise LearningRuntimeError(f"artifact path {role} must be absolute")
    if len({Path(path) for path in value.values()}) != len(ARTIFACT_ROLES):
        raise LearningRuntimeError("artifact paths must differ")
    return value


@dataclass(frozen=True, slots=True)
class LoadedParticipants:
    """1 processで1回だけloadしたruntimeと、そこから作り直したbinding。"""

    candidate_factory: Callable[[], object]
    incumbent_factory: Callable[[], object]
    candidate: dict
    incumbent: dict
    ml: dict


def _load_l2a(discard: str, reaction: str):
    from lisjong.learning.l2a_policy import load_l2a_policy_factory

    return load_l2a_policy_factory(discard, reaction)


def _load_arm_y(path: str):
    """既存bindingのstrict loadを使う。pathはbindingの環境変数と一致しなければならない。"""
    from lisjong_arena import initial_learning_champion as champion

    configured = os.environ.get(champion.CHECKPOINT_ENV_VAR)
    if configured is None or Path(configured) != Path(path):
        raise LearningRuntimeError(
            f"{champion.CHECKPOINT_ENV_VAR} must name the locked Arm Y checkpoint {path}"
        )
    runtime = champion.initial_learning_champion_runtime()
    return runtime, champion.create_initial_learning_champion_policy


def load_participants(
    paths: Mapping[str, str],
    *,
    lisjong_revision: str,
    arena_revision: str,
    configure: Callable[[], dict] = configure_ml_runtime,
    measure: Callable[[], dict] = measured_ml_runtime,
    load_candidate: Callable = _load_l2a,
    load_incumbent: Callable = _load_arm_y,
) -> LoadedParticipants:
    """ML条件を固定してから両runtimeをloadし、load前後の条件が同じことを要求する。

    ``configure`` / ``measure`` / ``load_*`` はtestでload境界を差し替えるseamで、
    正式実行では渡さない。
    """
    paths = require_artifact_paths(dict(paths))
    before = require_ml_runtime(configure())
    candidate_runtime = load_candidate(paths["discard"], paths["reaction"])
    after_candidate = measure()
    incumbent_runtime, incumbent_factory = load_incumbent(paths["champion"])
    after_incumbent = measure()
    if not before == after_candidate == after_incumbent:
        raise LearningRuntimeError(
            "ML conditions changed while loading the participants: "
            f"{before} / {after_candidate} / {after_incumbent}"
        )
    served = incumbent_runtime.conditions
    for name in ("deterministic_algorithms", "torch_threads", "torch_version"):
        if served.get(name) != before[name]:
            raise LearningRuntimeError(
                f"Arm Y serving runtime reports a different {name}"
            )
    try:
        candidate = candidate_document(
            candidate_runtime,
            discard_path=paths["discard"],
            reaction_path=paths["reaction"],
            lisjong_revision=lisjong_revision,
        )
        incumbent = incumbent_document(
            incumbent_runtime.checkpoint, arena_revision=arena_revision
        )
    except (KeyError, TypeError, AttributeError) as error:
        raise ParticipantBindingError(
            f"loaded artifacts cannot be bound: {type(error).__name__}: {error}"
        ) from error
    return LoadedParticipants(
        candidate_factory=candidate_runtime.create_policy,
        incumbent_factory=incumbent_factory,
        candidate=candidate,
        incumbent=incumbent,
        ml=before,
    )


_LOADED: dict[tuple, LoadedParticipants] = {}


def _loaded_once(paths: dict, lisjong_revision: str, arena_revision: str):
    key = (tuple(sorted(paths.items())), lisjong_revision, arena_revision)
    if key not in _LOADED:
        if _LOADED:
            raise LearningRuntimeError(
                "a worker must serve exactly one participant set"
            )
        _LOADED[key] = load_participants(
            paths, lisjong_revision=lisjong_revision, arena_revision=arena_revision
        )
    return _LOADED[key]


# ---------------------------------------------------------------------------
# 1 hanchan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LearningHanchanTask:
    """workerへ渡す1半荘分の入力（picklableなdocumentだけを持つ）。"""

    seed: int
    placement: int
    candidate: dict
    incumbent: dict
    artifact_paths: dict
    wheel_path: str
    record_actions: bool = False
    """開発準備入口（#567）の再現比較だけが ``True`` にし、選択行動の列を返させる。
    正式runは ``False``（従来どおり ``selected_actions = None``）。"""


class _TimedPolicy:
    """``choose_action`` の呼び出し数と所要時間を数えるproxy。判断には触れない。

    ``actions`` を渡すと、返した行動を正規化して ``[seat名, 値]`` で追記する（観測だけ）。
    """

    __slots__ = ("_policy", "_counts", "_seconds", "_seat", "_actions")

    def __init__(
        self, policy, counts: Counter, seconds: dict, seat: EngineSeat, actions=None
    ):
        self._policy = policy
        self._counts = counts
        self._seconds = seconds
        self._seat = seat
        self._actions = actions

    def choose_action(self, decision):
        self._counts[self._seat] += 1
        started = time.perf_counter()
        try:
            action = self._policy.choose_action(decision)
        finally:
            self._seconds[self._seat] += time.perf_counter() - started
        if self._actions is not None:
            from lisjong_arena.heuristic_candidate_engine_aabb.backend_check import (
                canonical_engine_value,
            )

            self._actions.append([self._seat.name, canonical_engine_value(action)])
        return action


def play_loaded(
    task: LearningHanchanTask,
    factories: Mapping[str, Callable[[], object]],
    identities: Mapping[str, str],
    *,
    actions: list | None = None,
) -> tuple[HanchanCapture, dict]:
    """roleごとのfactoryから、seatごとに新しいPolicyを作って1半荘を実行する。

    ``actions`` を渡すと、全seatの選択行動を判断順に追記する（#567の再現比較）。
    """
    roles = placement_roles(task.placement)
    counts: Counter = Counter()
    seconds = dict.fromkeys(EngineSeat, 0.0)
    selector_calls: Counter = Counter()
    created = []
    policies = {}
    for index, seat in enumerate(EngineSeat):
        policy = factories[roles[index]]()
        created.append(policy)
        policies[seat] = _TimedPolicy(policy, counts, seconds, seat, actions)
    if len({id(policy) for policy in created}) != len(created):
        raise LearningRuntimeError("a Policy instance was shared between seats")
    completions = []

    def count_selector(seat: EngineSeat) -> None:
        selector_calls[seat] += 1

    match = run_policy_hanchan(
        policies,
        seed=task.seed,
        rules=locked_rules(),
        on_round_evidence_complete=completions.append,
        on_selector_call=count_selector,
    )
    capture = HanchanCapture(
        match=match,
        seed=task.seed,
        placement=task.placement,
        seats=tuple(SeatAssignment(role, identities[role]) for role in roles),
        selector_call_counts=tuple(selector_calls[seat] for seat in EngineSeat),
        policy_call_counts=tuple(counts[seat] for seat in EngineSeat),
        round_evidence=tuple(completions),
    )
    analysis = {
        "decision_seconds": [seconds[seat] for seat in EngineSeat],
        "decisions": [counts[seat] for seat in EngineSeat],
    }
    return capture, analysis


def play_learning_hanchan(task: LearningHanchanTask) -> PlayedHanchan:
    """worker内で1半荘を実行する（実engine・実Policy）。"""
    environment = environment_document(task.wheel_path)
    provenance = environment["provenance"]
    # serving revisionはこのworkerが実際にimportした値から作り、lockの値と比べる
    loaded = _loaded_once(
        require_artifact_paths(task.artifact_paths),
        provenance["lisjong_revision"],
        provenance["lisjong_arena_revision"],
    )
    if loaded.candidate != task.candidate or loaded.incumbent != task.incumbent:
        raise ParticipantBindingError("loaded participants differ from the lock")
    if loaded.ml != environment["ml"]:
        raise LearningRuntimeError("ML conditions differ from the participant load")
    actions = [] if task.record_actions else None
    capture, analysis = play_loaded(
        task,
        {
            ROLE_CANDIDATE: loaded.candidate_factory,
            "B": loaded.incumbent_factory,
        },
        {
            ROLE_CANDIDATE: loaded.candidate["policy_identity"],
            "B": loaded.incumbent["policy_identity"],
        },
        actions=actions,
    )
    if measured_ml_runtime() != loaded.ml:
        raise LearningRuntimeError("ML conditions changed during the hanchan")
    return PlayedHanchan(
        capture,
        environment,
        os.getpid(),
        current_peak_rss_kib(),
        selected_actions=None if actions is None else tuple(actions),
        policy_analysis=analysis,
    )


__all__ = [
    "ARTIFACT_ROLES",
    "LearningHanchanTask",
    "LearningRuntimeError",
    "LoadedParticipants",
    "configure_ml_runtime",
    "environment_document",
    "load_participants",
    "measured_ml_runtime",
    "play_learning_hanchan",
    "play_loaded",
    "require_artifact_paths",
    "require_ml_runtime",
]
