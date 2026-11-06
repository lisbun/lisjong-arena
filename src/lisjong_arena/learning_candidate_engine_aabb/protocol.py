"""protocol ``arena-learning-candidate-engine-aabb-v1`` の固定値（#562、lisjong#368）。

Learning family専用の昇格評価である。``heuristic_candidate_engine_aabb``（Heuristic
family、主指標final_points、学習artifactを受理しない）へのoption追加ではなく、
独立したprotocol identity・lock・resultを持つ。engine・rules・6配置・局精算record・
副指標の部品だけを再利用する。

```text
engine        lisjong-engine 91af75e…（Heuristic engine AABBと同じrevision）
rules         RuleSet.default() の全field値（Heuristic engine AABBと同じrules digest）
schedule      lockのordered seeds の順 -> placement 0..5（AABB、ABAB、ABBA、BAAB、BABA、BBAA）
K             100 seed blocks = 600半荘（protocol invariant。callerは変えられない）
主指標        D(s) = mean_rank_B(s) - mean_rank_A(s) = (R_B(s) - R_A(s)) / 12
              R_x(s) = role x の12 seat-resultのengine最終rankの合計。正 = candidate有利
区間          mean D ± 1.96 × SD(K-1) / sqrt(K)
判定          lower > 0 -> CANDIDATE SUPERIOR、upper < 0 -> CHAMPION SUPERIOR、
              それ以外（0境界を含む）-> INCONCLUSIVE。無効な入力・実行 -> STOP / INVALID
```

K = 100の根拠はD(s) ∈ [-2, 2]から母SD ≤ 2とし、正規近似で約0.56順位差が
検出力80%の目安になることだけである（保証ではない）。配置間独立を仮定した
block SD 0.527は根拠にしない。

digestは既存artifactと同じcanonical JSONのSHA-256である。
"""

import re
from collections.abc import Sequence

from lisjong_arena import initial_learning_champion
from lisjong_arena.heuristic_candidate_engine_aabb import protocol as heuristic
from lisjong_arena.heuristic_candidate_engine_aabb.kyoku_record import (
    AABB_LAYOUT,
    PLACEMENT_CANDIDATE_SEATS,
    ROLE_CANDIDATE,
    ROLE_INCUMBENT,
    ExpectedHanchan,
    RecordLayout,
    SeatAssignment,
    placement_roles,
)
from lisjong_arena.overall_champion_aabb.lock import document_identity

PROTOCOL_ID = "arena-learning-candidate-engine-aabb-v1"
PROTOCOL_VERSION = 1
FORMAL_K = 100
"""正式eventのseed block数（lisjong#368でユーザー決定）。小さいKの正式eventはない。"""
SEAT_RESULTS_PER_ROLE_PER_BLOCK = 12
RANK_SUM_PER_BLOCK = 60
"""1 seed block（6半荘）の全seatのrank合計 = 6 × (1 + 2 + 3 + 4)。"""
INTERVAL_Z = 1.96
CLASSIFICATION_RULE_ID = (
    "learning-candidate-engine-seed-block-mean-rank-normal-approx-95-v1"
)
CANDIDATE_SUPERIOR = "CANDIDATE SUPERIOR"
CHAMPION_SUPERIOR = "CHAMPION SUPERIOR"
INCONCLUSIVE = "INCONCLUSIVE"

ENGINE_REVISION = heuristic.ENGINE_REVISION
EXPECTED_RULES = heuristic.EXPECTED_RULES
RULES_DIGEST = heuristic.RULES_DIGEST
SEED_DOMAIN = heuristic.SEED_DOMAIN
FORMAL_SPLIT = heuristic.FORMAL_SPLIT
BACKEND = heuristic.BACKEND
POPULATION_PREFIX = "learning-candidate-engine-aabb-"
EVALUATION_KIND = "FORMAL"

PARTICIPANT_FAMILY = "learning"
CANDIDATE_KIND = "lisjong-l2a-composite"
CANDIDATE_FACTORY = "lisjong.learning.l2a_policy:load_l2a_policy_factory"
CANDIDATE_CLASSIFICATION = "lisjong-l2a-decision-classification-v1"
INCUMBENT_KIND = "arena-source-pilot-arm-y"
INCUMBENT_FACTORY = (
    "lisjong_arena.initial_learning_champion:create_initial_learning_champion_policy"
)
INCUMBENT_POLICY_IDENTITY = initial_learning_champion.POLICY_IDENTITY
INCUMBENT_CHECKPOINT_IDENTITY = initial_learning_champion.CHECKPOINT_IDENTITY
INCUMBENT_WEIGHTS_SHA256 = initial_learning_champion.CHECKPOINT_DIGEST
"""current Learning Champion（#211 Arm Y、project #54）。既存bindingの値をそのまま使い、
guardは追加しない。"""

ML_RUNTIME = {
    "cuda_available": False,
    "deterministic_algorithms": True,
    "device": "cpu",
    "torch_threads": 1,
}
"""worker内で両runtimeに同じく適用するML条件（Arm Yの既存deterministic設定）。

torch versionとPython versionは実測値をlockの環境へ記録して照合する。"""

K_RATIONALE = (
    "D(s) is bounded in [-2, 2], so the population SD is at most 2; with the normal "
    "approximation (1.96 + 0.842) * 2 / sqrt(100) ~= 0.56 rank difference is the "
    "80% power guide. This is not a guarantee; smaller differences may end "
    "INCONCLUSIVE. The 0.527 block SD under exchangeability and independent "
    "placements is not used as a rationale."
)

NO_RESCUE_BOUNDARY = (
    "no seed is added, replaced or dropped after result exposure",
    "K, the primary statistic, interval, classification rule, RuleSet and "
    "participants are never changed after result exposure",
    "partial or failed execution is never partially adopted",
    "secondary metrics never override the primary classification",
    "INCONCLUSIVE is a valid terminal outcome, not permission to add seeds",
    "a STOP is never resumed automatically",
)

LAYOUT = RecordLayout(
    protocol_id=PROTOCOL_ID,
    kyoku_record_schema="arena-learning-candidate-engine-aabb-kyoku-record-v1",
    event_fact_schema="arena-learning-candidate-engine-aabb-event-fact-v1",
    manifest_schema="arena-learning-candidate-engine-aabb-record-manifest-v1",
    key_fields=AABB_LAYOUT.key_fields,
    validate_keys=AABB_LAYOUT.validate_keys,
    validate_seats=AABB_LAYOUT.validate_seats,
)
"""局単位recordのlayout。半荘key・seat検査はAABBと同じで、保存物のidentityだけ分ける。"""
SECONDARY_METRICS_SCHEMA = "arena-learning-candidate-engine-aabb-secondary-metrics-v1"

PROTOCOL_DIGEST = "a5c1cb73e3f2b93f6f22976dc1c78d7a0fbb02dcaa78e6338f9c646179fae529"
"""``protocol_document()`` のdigest。import時に一致を確かめる。"""

_HEX64 = re.compile(r"[0-9a-f]{64}")


class LearningEngineAabbError(heuristic.EngineAabbProtocolError):
    """Learning protocol / lock / resultの値が契約を満たさない場合。"""


def protocol_document() -> dict[str, object]:
    """protocol identityを構成する値（protocol_digestの対象）。"""
    return {
        "classification": {
            "interval": "mean D +/- z * SD(K-1) / sqrt(K), two-sided normal approx",
            "labels": {
                "lower_above_zero": CANDIDATE_SUPERIOR,
                "otherwise": INCONCLUSIVE,
                "upper_below_zero": CHAMPION_SUPERIOR,
            },
            "rule_id": CLASSIFICATION_RULE_ID,
            "z": INTERVAL_Z,
        },
        "engine_revision": ENGINE_REVISION,
        "family": PARTICIPANT_FAMILY,
        "game_mode": {
            "dealer_start_seat": 0,
            "match_format": "hanchan",
            "player_count": 4,
            "starting_points": 25000,
            "start": "MatchState(seed, rules) at East 1, no starting_scores",
        },
        "incumbent": {
            "checkpoint_identity": INCUMBENT_CHECKPOINT_IDENTITY,
            "factory_binding": INCUMBENT_FACTORY,
            "kind": INCUMBENT_KIND,
            "policy_identity": INCUMBENT_POLICY_IDENTITY,
            "weights_sha256": INCUMBENT_WEIGHTS_SHA256,
        },
        "k": FORMAL_K,
        "k_rationale": K_RATIONALE,
        "match_end_conditions": list(heuristic.MATCH_END_CONDITIONS),
        "ml_runtime": dict(ML_RUNTIME),
        "placements": [list(seats) for seats in PLACEMENT_CANDIDATE_SEATS],
        "primary_metric": {
            "block_difference": "D(s) = (R_B(s) - R_A(s)) / 12",
            "positive_means": "candidate advantage",
            "rank_source": "CompletedMatch final rank (-final points, seat order)",
            "seat_results_per_role_per_block": SEAT_RESULTS_PER_ROLE_PER_BLOCK,
        },
        "protocol_id": PROTOCOL_ID,
        "protocol_version": PROTOCOL_VERSION,
        "rules_digest": document_identity(EXPECTED_RULES),
    }


def require_formal_seeds(values: object) -> tuple[int, ...]:
    """正式eventのordered seeds。engine domainの検査に加え、件数がK = 100であること。"""
    seeds = heuristic.require_seeds(values)
    if len(seeds) != FORMAL_K:
        raise LearningEngineAabbError(
            f"a formal event needs exactly K = {FORMAL_K} seeds; got {len(seeds)}"
        )
    return seeds


def placement_schedule(seeds: Sequence[int]) -> list[dict[str, object]]:
    return heuristic.placement_schedule(seeds)


def schedule_document(ordered_seeds: Sequence[int]) -> dict[str, object]:
    """正式populationのschedule（schedule_digestの対象）。"""
    return {
        "hanchans": placement_schedule(require_formal_seeds(ordered_seeds)),
        "protocol_id": PROTOCOL_ID,
    }


def expected_schedule(
    hanchans: Sequence[dict], candidate_identity: str, incumbent_identity: str
) -> list[ExpectedHanchan]:
    """schedule itemからrecordのK1 / K2照合に渡す期待値を作る。"""
    identities = {
        ROLE_CANDIDATE: candidate_identity,
        ROLE_INCUMBENT: incumbent_identity,
    }
    return [
        ExpectedHanchan(
            seed=item["seed"],
            placement=item["placement"],
            seats=tuple(
                SeatAssignment(role, identities[role])
                for role in placement_roles(item["placement"])
            ),
        )
        for item in hanchans
    ]


def require_hex64(value: object, context: str) -> str:
    if type(value) is not str or not _HEX64.fullmatch(value):
        raise LearningEngineAabbError(f"{context} must be 64-hex")
    return value


def _self_check() -> None:
    if document_identity(protocol_document()) != PROTOCOL_DIGEST:
        raise LearningEngineAabbError("PROTOCOL_DIGEST does not match the document")


_self_check()

__all__ = [
    "BACKEND",
    "CANDIDATE_CLASSIFICATION",
    "CANDIDATE_FACTORY",
    "CANDIDATE_KIND",
    "CANDIDATE_SUPERIOR",
    "CHAMPION_SUPERIOR",
    "CLASSIFICATION_RULE_ID",
    "ENGINE_REVISION",
    "EVALUATION_KIND",
    "EXPECTED_RULES",
    "FORMAL_K",
    "FORMAL_SPLIT",
    "INCONCLUSIVE",
    "INCUMBENT_CHECKPOINT_IDENTITY",
    "INCUMBENT_FACTORY",
    "INCUMBENT_KIND",
    "INCUMBENT_POLICY_IDENTITY",
    "INCUMBENT_WEIGHTS_SHA256",
    "INTERVAL_Z",
    "K_RATIONALE",
    "LAYOUT",
    "ML_RUNTIME",
    "NO_RESCUE_BOUNDARY",
    "PARTICIPANT_FAMILY",
    "POPULATION_PREFIX",
    "PROTOCOL_DIGEST",
    "PROTOCOL_ID",
    "PROTOCOL_VERSION",
    "RANK_SUM_PER_BLOCK",
    "RULES_DIGEST",
    "SEAT_RESULTS_PER_ROLE_PER_BLOCK",
    "SECONDARY_METRICS_SCHEMA",
    "SEED_DOMAIN",
    "LearningEngineAabbError",
    "expected_schedule",
    "placement_schedule",
    "protocol_document",
    "require_formal_seeds",
    "require_hex64",
    "schedule_document",
]
