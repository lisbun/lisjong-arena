"""#562 Learning engine AABB protocol / lock / runnerのtest用builder。

実artifact・torch・Rust wheel・実Policyを使わずに、lock・実行・result・verifyの
契約を試す。``fake_play*`` はspawn workerへ渡すため、module-levelの関数として置く。
半荘の中身は#513のfixture（実engineの ``MatchState`` で進めた手計算済みscript）である。

```text
SCRIPT_Y    seat 0が連続ロンでseat 3を飛ばす -> rank = seat + 1
            AABBの6配置では R_A = R_B = 30 -> D = 0
SCRIPT_X    placement 0（Aはseat 0 / 1）だけで使う -> blockのDが0から動く
X_SEEDS     placement 0にSCRIPT_Xを使うseed（それ以外はすべてSCRIPT_Y）
```
"""

import os
import time
from dataclasses import replace

from _engine_aabb_record_fixtures import SCRIPT_X, SCRIPT_Y, completions_for, play

from lisjong_arena import seed_registry
from lisjong_arena.heuristic_candidate_engine_aabb.executor import (
    ExecutedHanchan,
    PlayedHanchan,
)
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    SeatAssignment,
    placement_roles,
)
from lisjong_arena.heuristic_candidate_engine_aabb.record_store import HanchanCapture
from lisjong_arena.learning_candidate_engine_aabb.lock import (
    assemble_lock_document,
    require_live_target,
)
from lisjong_arena.learning_candidate_engine_aabb.memory_guard import (
    memory_guard_document,
)
from lisjong_arena.learning_candidate_engine_aabb.participants import (
    candidate_policy_identity,
)
from lisjong_arena.learning_candidate_engine_aabb.protocol import (
    CANDIDATE_CLASSIFICATION,
    CANDIDATE_FACTORY,
    CANDIDATE_KIND,
    ENGINE_REVISION,
    FORMAL_K,
    FORMAL_SPLIT,
    INCUMBENT_CHECKPOINT_IDENTITY,
    INCUMBENT_FACTORY,
    INCUMBENT_KIND,
    INCUMBENT_POLICY_IDENTITY,
    INCUMBENT_WEIGHTS_SHA256,
    POPULATION_PREFIX,
    PROTOCOL_ID,
    SEED_DOMAIN,
)

LISJONG_REVISION = "b" * 40
ARENA_REVISION = "c" * 40
EVENT = 962
SEEDS = tuple(562_000 + index for index in range(FORMAL_K))
X_SEEDS = frozenset(SEEDS[::3])
WHEEL_PATH = os.path.abspath("lisjong_native-test.whl")
ARTIFACT_PATHS = {
    "champion": os.path.abspath("arm-y"),
    "discard": os.path.abspath("m1"),
    "reaction": os.path.abspath("r1"),
}
LIMITS = {"peak_rss_kib": 4_000_000, "timeout_seconds": 60, "wall_seconds": 3_600}
DISCARD_IDENTITY = "1" * 64
REACTION_IDENTITY = "2" * 64


def head(identity, *, head_value, encoding):
    return {
        "classification": CANDIDATE_CLASSIFICATION,
        "dataset_identity": "dataset-" + identity[:8],
        "encoding_identity": encoding,
        "head": head_value,
        "identity": identity,
        "manifest_sha256": "3" * 64,
        "source_identity": "l2a-source-506",
        "training_lisjong": {"package_version": "0.1.0", "source_digest": "4" * 64},
        "weights_sha256": "5" * 64,
    }


CANDIDATE = {
    "artifacts": {
        "discard": head(
            DISCARD_IDENTITY,
            head_value="discard",
            encoding="lisjong-l2a-discard-candidate-encoding-m1-v1",
        ),
        "reaction": head(
            REACTION_IDENTITY,
            head_value="call",
            encoding="lisjong-l2a-reaction-candidate-encoding-r1-v1",
        ),
    },
    "classification": CANDIDATE_CLASSIFICATION,
    "factory_binding": CANDIDATE_FACTORY,
    "family": "learning",
    "kind": CANDIDATE_KIND,
    "policy_identity": candidate_policy_identity(DISCARD_IDENTITY, REACTION_IDENTITY),
    "serving": {"lisjong_revision": LISJONG_REVISION},
}
INCUMBENT = {
    "checkpoint": {
        "arm": "ARM_Y",
        "checkpoint_identity": INCUMBENT_CHECKPOINT_IDENTITY,
        "feature": {"identity": "arena-policy-input-feature-v1"},
        "source": {"identity": "source-pilot"},
        "vocabulary": {"version": "v1"},
        "weights_sha256": INCUMBENT_WEIGHTS_SHA256,
    },
    "checkpoint_manifest_sha256": "6" * 64,
    "factory_binding": INCUMBENT_FACTORY,
    "family": "learning",
    "kind": INCUMBENT_KIND,
    "policy_identity": INCUMBENT_POLICY_IDENTITY,
    "serving": {"arena_revision": ARENA_REVISION},
    "weights_sha256": INCUMBENT_WEIGHTS_SHA256,
}
ML = {
    "cuda_available": False,
    "deterministic_algorithms": True,
    "device": "cpu",
    "python_version": "3.14.7",
    "torch_threads": 1,
    "torch_version": "2.13.0+cpu",
}
ENVIRONMENT = {
    "ml": ML,
    "native": {
        "api_version": 3,
        "source_revision": LISJONG_REVISION,
        "wheel_bytes": 1,
        "wheel_file": "lisjong_native-test.whl",
        "wheel_sha256": "d" * 64,
    },
    "provenance": {
        "lisjong_arena_revision": ARENA_REVISION,
        "lisjong_engine_revision": ENGINE_REVISION,
        "lisjong_revision": LISJONG_REVISION,
    },
    "python": {"implementation": "CPython", "version": "3.14.7"},
    "shanten_backend": {"backend": "rust", "lisjong_revision": LISJONG_REVISION},
}


OTHER_DOMAIN = "riichienv-4p-red-half-hanchan-v1"


def allocation(
    *,
    event=EVENT,
    seeds=SEEDS,
    protocol=PROTOCOL_ID,
    split=FORMAL_SPLIT,
    domain=SEED_DOMAIN,
):
    return seed_registry.build_allocation_record(
        owner_issue=f"lisbun/lisjong-arena#{event}",
        protocol=protocol,
        seed_domain=domain,
        purpose="test",
        population=f"{POPULATION_PREFIX}{event}",
        split=split,
        seeds=seeds,
        arena_revision="e" * 40,
        protocol_revision="test",
        provenance_reference="test",
        allocation_timestamp="2026-11-06T00:00:00Z",
    )


def ledger(*extra, main=None):
    document = seed_registry.new_ledger()
    document["allocations"] = [allocation() if main is None else main, *extra]
    document["allocations"].sort(key=seed_registry._sort_key)
    return seed_registry.validate_ledger(document)


def allocation_binding(document=None, *, event=EVENT):
    document = ledger() if document is None else document
    record = next(
        item
        for item in document["allocations"]
        if item["owner_issue"] == f"lisbun/lisjong-arena#{event}"
    )
    return seed_registry.allocation_binding(document, record["allocation_identity"])


def destinations(tmp):
    return {
        "diagnostics": str(tmp / "diagnostics"),
        "observations": str(tmp / "observations.json"),
        "raw": str(tmp / "raw"),
        "result": str(tmp / "result.json"),
    }


def lock_document(tmp, **overrides):
    document = overrides.pop("ledger", None) or ledger()
    arguments = {
        "event": EVENT,
        "candidate": CANDIDATE,
        "incumbent": INCUMBENT,
        "ordered_seeds": SEEDS,
        "allocation_binding": allocation_binding(document),
        "limits": LIMITS,
        "wheel_path": WHEEL_PATH,
        "artifact_paths": ARTIFACT_PATHS,
        "destinations": destinations(tmp),
        "environment": ENVIRONMENT,
        "arena_revision": ARENA_REVISION,
        "ledger": document,
    }
    arguments.update(overrides)
    return assemble_lock_document(**arguments)


LIMITS_V2 = {"timeout_seconds": 60, "wall_seconds": 3_600}


def lock_document_v2(tmp, **overrides):
    """#580のlock v2（cgroup memory guard、8 worker）。"""
    overrides.setdefault("limits", LIMITS_V2)
    overrides.setdefault("workers", 8)
    overrides.setdefault("memory_guard", memory_guard_document())
    return lock_document(tmp, **overrides)


def gate(lock, *, ledger):
    """live値をfixtureの値として照合する（正式実行のgateと同じ関数）。"""
    require_live_target(
        lock,
        ledger=ledger,
        live=(ENVIRONMENT, CANDIDATE, INCUMBENT),
        head=ARENA_REVISION,
    )


PARENT_PEAK_RSS_KIB = 1024


def os_limits(limits=LIMITS):
    """lockの上限どおりに設定したscopeのOS上限（#565）。"""
    return {
        "memory_max_bytes": limits["peak_rss_kib"] * 1024,
        "memory_swap_max_bytes": 0,
        "runtime_max_seconds": float(limits["wall_seconds"]),
        "unit": "run-u565.scope",
    }


def parent_peak_rss_kib():
    return PARENT_PEAK_RSS_KIB


# ---------------------------------------------------------------------------
# captures（in-processでも、spawn workerでも使う）
# ---------------------------------------------------------------------------

COUNTS = (40, 41, 42, 43)
ANALYSIS = {"decision_seconds": [0.5, 0.25, 0.125, 1.0], "decisions": list(COUNTS)}


def script_for(seed, placement):
    return SCRIPT_X if (seed in X_SEEDS and placement == 0) else SCRIPT_Y


def capture(seed, placement, *, evidence=True, counts=COUNTS, script=None):
    script = script_for(seed, placement) if script is None else script
    match = play(seed, script)
    identities = {"A": CANDIDATE["policy_identity"], "B": INCUMBENT["policy_identity"]}
    return HanchanCapture(
        match=match,
        seed=seed,
        placement=placement,
        seats=tuple(
            SeatAssignment(role, identities[role])
            for role in placement_roles(placement)
        ),
        selector_call_counts=counts,
        policy_call_counts=counts,
        round_evidence=completions_for(match, script_x=script is SCRIPT_X)
        if evidence
        else None,
    )


def executed_schedule():
    """全600半荘のExecutedHanchan（in-process。result / verifyのtest用）。"""
    return [
        ExecutedHanchan(capture(seed, placement), 0.5, 2048, None, dict(ANALYSIS))
        for seed in SEEDS
        for placement in range(6)
    ]


# ---------------------------------------------------------------------------
# fake play（spawn worker内で実行される）
# ---------------------------------------------------------------------------


def fake_play(task):
    return PlayedHanchan(
        capture(task.seed, task.placement),
        ENVIRONMENT,
        os.getpid(),
        2048,
        policy_analysis=dict(ANALYSIS),
    )


def fake_play_policy_error(task):
    if (task.seed, task.placement) == (SEEDS[1], 2):
        raise RuntimeError("synthetic Policy failure")
    return fake_play(task)


def fake_play_slow(task):
    if (task.seed, task.placement) == (SEEDS[0], 1):
        time.sleep(30)
    return fake_play(task)


def fake_play_other_environment(task):
    played = fake_play(task)
    if (task.seed, task.placement) == (SEEDS[0], 3):
        environment = {**ENVIRONMENT, "ml": {**ML, "torch_threads": 8}}
        return replace(played, environment=environment)
    return played


def fake_play_large_rss(task):
    played = fake_play(task)
    if (task.seed, task.placement) == (SEEDS[0], 4):
        return replace(played, peak_rss_kib=LIMITS["peak_rss_kib"] + 1)
    return played


def fake_play_wrong_placement(task):
    played = fake_play(task)
    if (task.seed, task.placement) == (SEEDS[0], 1):
        return replace(played, capture=replace(played.capture, placement=2))
    return played


# ---------------------------------------------------------------------------
# 開発準備入口（#567）
# ---------------------------------------------------------------------------

DEVELOPMENT_SEEDS = (472_005, 472_006)
SEAT_NAMES = ("EAST", "SOUTH", "WEST", "NORTH")
MARKER_ENV = "LISJONG_ARENA_TEST_DEVELOPMENT_MARKERS"
"""再実行（2回目）の半荘だけ結果を変えるfakeが、1回目を覚えておくdirectory。"""


def fake_actions(seed, placement, counts=COUNTS):
    """seatごとにPolicy呼び出し数と同じ数の、決定的な合成行動。"""
    return tuple(
        [name, {"__type": "SyntheticAction", "index": index, "seed": seed}]
        for name, count in zip(SEAT_NAMES, counts, strict=True)
        for index in range(count)
    )


def fake_dev_play(task):
    played = fake_play(task)
    return replace(played, selected_actions=fake_actions(task.seed, task.placement))


def _second_time(task):
    """同じ(seed, placement)を2回目に実行したときだけTrue（spawn workerをまたぐ）。"""
    marker = os.path.join(os.environ[MARKER_ENV], f"{task.seed}-{task.placement}.seen")
    if os.path.exists(marker):
        return True
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("seen")
    return False


def fake_dev_play_action_drift(task):
    played = fake_dev_play(task)
    if (task.seed, task.placement) == (472_005, 2) and _second_time(task):
        actions = list(played.selected_actions)
        actions[0] = ["EAST", {"__type": "SyntheticAction", "index": -1, "seed": 0}]
        return replace(played, selected_actions=tuple(actions))
    return played


def fake_dev_play_record_drift(task):
    played = fake_dev_play(task)
    if (task.seed, task.placement) == (472_005, 0) and _second_time(task):
        drifted = capture(task.seed, task.placement, script=SCRIPT_X)
        return replace(played, capture=drifted)
    return played


def fake_dev_play_without_actions(task):
    return fake_play(task)


def fake_dev_play_error(task):
    if (task.seed, task.placement) == (472_006, 3):
        raise RuntimeError("synthetic Policy failure")
    return fake_dev_play(task)


def fake_dev_play_slow(task):
    if (task.seed, task.placement) == (472_006, 1):
        time.sleep(30)
    return fake_dev_play(task)


def fake_dev_play_large_rss(task):
    played = fake_dev_play(task)
    if (task.seed, task.placement) == (472_005, 4) and _second_time(task):
        return replace(played, peak_rss_kib=LIMITS["peak_rss_kib"] + 1)
    return played


def development_plan(tmp, **overrides):
    from lisjong_arena.learning_candidate_engine_aabb.development import (
        assemble_plan,
    )

    arguments = {
        "candidate": CANDIDATE,
        "incumbent": INCUMBENT,
        "limits": LIMITS,
        "wheel_path": WHEEL_PATH,
        "artifact_paths": ARTIFACT_PATHS,
        "output_dir": str(tmp / "output"),
        "diagnostics_dir": str(tmp / "diagnostics"),
        "environment": ENVIRONMENT,
        "arena_revision": ARENA_REVISION,
        "ledger": ledger(),
    }
    arguments.update(overrides)
    return assemble_plan(**arguments)


def development_gate(plan, *, ledger):
    from lisjong_arena.learning_candidate_engine_aabb.development import (
        require_live_development,
    )

    require_live_development(
        plan,
        ledger=ledger,
        live=(ENVIRONMENT, CANDIDATE, INCUMBENT),
        head=ARENA_REVISION,
    )
