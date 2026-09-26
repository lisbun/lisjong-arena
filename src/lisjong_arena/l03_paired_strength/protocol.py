"""L0.3 Step F outcome-Q vs canonical-first paired-strength protocol v1（#385）。

parent lisjong-project#79 Step F。Step E（lisjong#206）で
``OUTCOME-Q SERVING QUALIFICATION PASS``となった不変のoutcome-Q artifactを、
同じ``SemanticEnvelopeOffensePolicy`` familyのconstant-zero（canonical-first）
residual runtimeとfocal seatだけで比較する。

```text
candidate focal   SemanticEnvelopeOffensePolicy + frozen outcome-Q residual runtime
baseline focal    SemanticEnvelopeOffensePolicy + constant-zero residual runtime
other 3 seats     ConstantResidualRuntime（両armで同一、game / seatごとにfresh）
backend / rules   lisjong-engine / project-standard-v1（RuleSet.default()）

seed block        fresh seed 1個
paired unit       (seed, focal seat)、focal seat 0..3を各blockで1回ずつ
arm               candidate / baseline（同じseed・同じfocal seat）
budget            1,000 seed blocks = 4,000 paired units = 8,000 hanchan
```

primary endpointはpaired focal-seat final score差
（candidate focal final score - baseline focal final score）である。final scoreは
engineの``CompletedMatch.final_score.for_seat(focal).final_points``
（RuleSet.default()のuma / oka込み、内部単位 1 = 0.1pt）をそのまま使い、
protocol側で順位・uma・okaを再計算しない。

protocol identityを構成する条件（seed population、seat schedule、budget、
endpoint、区間法、invalid handling、terminal rule、frozen identity / revision）は
すべてこのmoduleのinvariantであり、caller-configurableにしない。
"""

from __future__ import annotations

from dataclasses import dataclass

from lisjong_arena._artifact_io import ArtifactValidationError, expect_object
from lisjong_arena.focal_outcome_source.engine_source import (
    BACKEND_NAME,
    PINNED_LISJONG_ENGINE_REVISION,
    RULES,
)
from lisjong_arena.paired_evaluation import INTERVAL_Z
from lisjong_arena.seed_registry import LISJONG_ENGINE_HANCHAN_SEED_DOMAIN

PROTOCOL_ID = "arena-l0.3-outcome-q-paired-strength-v1"
PROTOCOL_VERSION = 1

PARENT_ISSUE = "lisbun/lisjong-project#79"
OWNER_ISSUE = "lisbun/lisjong-arena#385"
"""seed allocationのowner issue。lockはこのownerのallocationだけを受理する。"""

# ---------------------------------------------------------------------------
# frozen comparison identities（Step D / E evidence）
# ---------------------------------------------------------------------------

SELECTION_POLICY_IDENTITY = "lisjong-offense-l0.2-semantic-envelope-v1"
CANDIDATE_ARTIFACT_IDENTITY = (
    "30874a8eae2c248d1c45a31fe273306fb52cdaa664e8e61e8b4652b5b5bbb7d3"
)
CANDIDATE_ARTIFACT_FILE_SHA256 = {
    "manifest.json": "8cdb44aa1f7dc0e2c5a86dcba2342b51f4c2a14f7a489e179ecb1d485dde1393",
    "weights.f32": "07e72778def521fe1dd19113ff7c88c1a7614201c0c2d7a1a10ff597891157c8",
}
"""lisjong#203のchecksum bookkeepingで記録したimmutable artifact file digest。"""
CANDIDATE_RUNTIME_IDENTITY = (
    "4982511841c5bf5748a14f6905ab6bd9d00c23f1980b9ce61bb23afba238c2f3"
)
"""#79 A2 digest ``{outcome_q_artifact, selection_policy}``（Step Eで記録）。"""
BASELINE_RUNTIME_IDENTITY = (
    "725ed52560bed62934b477d35bfc4d6b7805909a95d2c095b280cd6e1d5fc257"
)
"""#79 A2 digest ``{residual_scorer: constant-zero, selection_policy}``。"""
TRAINING_SOURCE_IDENTITY = (
    "230606f9304f77843d11efc65874ea04b2319ed236d81445265332d26b380cc4"
)
"""artifactを学習したC2 SCIENTIFIC source（#374）。provenanceとしてだけ記録する。"""

LISJONG_REVISION = "c6ab5d68c51b50494cbfed45a2ba699cddab1256"
"""Step D training / Step E serving qualificationのlisjong revision。"""
LISJONG_ENGINE_REVISION = PINNED_LISJONG_ENGINE_REVISION
"""C0 / C2 source lineageと同じlisjong-engine revision（#366 / #367 PASS）。"""
TORCH_VERSION = "2.13.0"
"""Step D / Eと同じtorch release。local version suffix（``+cpu`` 等）は許容する。"""

# ---------------------------------------------------------------------------
# population / pairing
# ---------------------------------------------------------------------------

SEED_DOMAIN = LISJONG_ENGINE_HANCHAN_SEED_DOMAIN
ALLOCATION_POPULATION = "l0.3-outcome-q-paired-strength"
ALLOCATION_SPLIT = "STRENGTH-EVAL"

FIRST_SEED = 931_000
SEED_BLOCK_COUNT = 1_000
ORDERED_SEEDS = tuple(range(FIRST_SEED, FIRST_SEED + SEED_BLOCK_COUNT))
EXCLUDED_DIAGNOSTIC_SEEDS = range(910_000, 910_400)
"""#366 / #367 / #370のregistry外diagnostic seed。populationと重なってはならない。"""

SEAT_COUNT = 4
FOCAL_SEATS = (0, 1, 2, 3)
CANDIDATE_ARM = "candidate"
BASELINE_ARM = "baseline"
ARMS = (CANDIDATE_ARM, BASELINE_ARM)
PAIRED_UNITS_PER_BLOCK = len(FOCAL_SEATS)
HANCHAN_PER_BLOCK = PAIRED_UNITS_PER_BLOCK * len(ARMS)
PAIRED_UNIT_COUNT = SEED_BLOCK_COUNT * PAIRED_UNITS_PER_BLOCK
HANCHAN_COUNT = SEED_BLOCK_COUNT * HANCHAN_PER_BLOCK

FINAL_POINTS_PER_PT = 10
"""engine ``FinalPlayerScore.final_points``の内部単位（1 = 0.1pt）。"""

BUDGET_RATIONALE = (
    "1,000 fresh seed blocks (4,000 paired units, 8,000 hanchan) were chosen "
    "before any paired outcome existed. Planning basis: about 50 s per hanchan "
    "per worker on lisjong-engine (C2 generation 528 hanchan / 24,939 s plus "
    "Step E outcome-Q discard runtime), i.e. about 110 CPU-hours; an assumed "
    "8-10 pt seed-block SD of the paired uma/oka final-score difference gives "
    "a standard error of about 0.25-0.32 pt and an 80%-power minimum "
    "detectable effect of about 0.7-0.9 pt per hanchan. The SD is a planning "
    "assumption, not an estimate from exposed data, and the budget is never "
    "resized after result exposure."
)

ROTATION_RULE = (
    "for each locked seed in ascending order, focal seat 0, 1, 2, 3 in that "
    "order; within each paired unit the candidate arm is listed before the "
    "baseline arm; the global game ordinal is the index in this schedule"
)

# ---------------------------------------------------------------------------
# endpoint / terminal interpretation
# ---------------------------------------------------------------------------

IMPROVED_LABEL = "OUTCOME-AWARE RESIDUAL OFFENSE IMPROVED"
NOT_ESTABLISHED_LABEL = "OUTCOME-AWARE RESIDUAL OFFENSE NOT ESTABLISHED"
STOP_INVALID_LABEL = "STOP / INVALID"
IMPROVED_KIND = "IMPROVED"
NOT_ESTABLISHED_KIND = "NOT_ESTABLISHED"

CLASSIFICATION_RULE_ID = "l0.3-paired-focal-final-points-seed-block-normal-95-v1"

MINIMUM_VALID_PAIRED_UNITS = PAIRED_UNIT_COUNT
"""全4,000 paired unitがvalidであることを要求する（部分採用はしない）。"""


class PairedStrengthProtocolError(ValueError):
    """#385 protocol v1を満たせない場合。"""


@dataclass(frozen=True, slots=True)
class GameAssignment:
    """frozen scheduleの1 hanchan。"""

    game_ordinal: int
    block_index: int
    seed: int
    focal_seat: int
    arm: str

    def to_document(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "block_index": self.block_index,
            "focal_seat": self.focal_seat,
            "game_ordinal": self.game_ordinal,
            "seed": self.seed,
        }


def game_schedule() -> tuple[GameAssignment, ...]:
    """``ROTATION_RULE``に従うdeterministicな8,000 hanchan schedule。"""
    assignments: list[GameAssignment] = []
    for block_index, seed in enumerate(ORDERED_SEEDS):
        for focal_seat in FOCAL_SEATS:
            for arm in ARMS:
                assignments.append(
                    GameAssignment(
                        game_ordinal=len(assignments),
                        block_index=block_index,
                        seed=seed,
                        focal_seat=focal_seat,
                        arm=arm,
                    )
                )
    return tuple(assignments)


def endpoint_document() -> dict[str, object]:
    """primary endpoint / uncertainty / invalid handling / terminal rule block。"""
    return {
        "classification_rule_id": CLASSIFICATION_RULE_ID,
        "final_score": {
            "source": "lisjong-engine CompletedMatch.final_score.for_seat(focal)"
            ".final_points",
            "includes": "RuleSet.default() base points, uma, oka and bankruptcy "
            "points exactly as settled by the engine; never recomputed",
            "unit": "engine internal final_points, 1 = 0.1 pt",
            "units_per_pt": FINAL_POINTS_PER_PT,
        },
        "interval_method": (
            "two-sided normal-approximation 95% interval over seed blocks, "
            f"mean D +/- {INTERVAL_Z} * SD(N-1) / sqrt(N)"
        ),
        "interval_z": INTERVAL_Z,
        "invalid_handling": [
            "any engine / Policy / runtime exception, or a result record failing "
            "strict validation, in any scheduled hanchan makes the whole event "
            "STOP / INVALID",
            "invalid pairs are never dropped, skipped, replaced, re-seeded or "
            "selectively rerun; no partial result is adopted",
            "an execution aborted before the result artifact is written exposes "
            "no outcome; only the complete locked schedule may be executed "
            "again, with identical lock, seeds and destinations",
            "live target drift (Arena / lisjong / lisjong-engine / torch "
            "revision, artifact bytes or identity, seed allocation state) before "
            "or after execution is STOP / INVALID",
        ],
        "minimum_valid_paired_units": MINIMUM_VALID_PAIRED_UNITS,
        "paired_unit": (
            "(seed, focal seat); candidate and baseline use the same game seed, "
            "the same focal seat and identical opponent runtimes"
        ),
        "point_estimate": "mean of D(s) over all locked seed blocks",
        "primary_statistic": (
            "for each locked seed s, D(s) = mean over focal seats 0..3 of "
            "(candidate focal final_points - baseline focal final_points)"
        ),
        "primary_unit": "seed block",
        "secondary_diagnostics": [
            "per-arm focal mean final_points, mean rank and rank counts",
            "per-arm focal raw final score mean",
            "per-arm focal win / deal-in / riichi-deposit / kyoku counts",
            "paired units whose candidate and baseline final_points vectors "
            "are identical",
        ],
        "secondary_diagnostics_override_primary": False,
        "sign_interpretation": "D > 0 -> candidate focal advantage",
        "statistical_n": (
            f"{SEED_BLOCK_COUNT} seed blocks, never {PAIRED_UNIT_COUNT} paired "
            f"units or {HANCHAN_COUNT} hanchan"
        ),
        "terminal_rule": {
            IMPROVED_LABEL: "valid event and interval lower bound > 0",
            NOT_ESTABLISHED_LABEL: "valid event and interval lower bound <= 0",
            STOP_INVALID_LABEL: "any invalid source, artifact, provenance, "
            "protocol or execution evidence",
        },
    }


def comparison_document() -> dict[str, object]:
    """両armとopponentのfrozen identity block。"""
    return {
        "baseline_focal": {
            "policy_class": "lisjong.learning.SemanticEnvelopeOffensePolicy",
            "residual_runtime": "lisjong.learning.ConstantResidualRuntime",
            "runtime_identity": BASELINE_RUNTIME_IDENTITY,
        },
        "candidate_focal": {
            "artifact_file_sha256": dict(CANDIDATE_ARTIFACT_FILE_SHA256),
            "artifact_identity": CANDIDATE_ARTIFACT_IDENTITY,
            "policy_class": "lisjong.learning.SemanticEnvelopeOffensePolicy",
            "residual_runtime": "lisjong.learning.load_outcome_q_policy_factory",
            "runtime_identity": CANDIDATE_RUNTIME_IDENTITY,
            "training_source_identity": TRAINING_SOURCE_IDENTITY,
        },
        "difference": "focal residual runtime only",
        "opponents": {
            "policy_class": "lisjong.learning.SemanticEnvelopeOffensePolicy",
            "residual_runtime": "lisjong.learning.ConstantResidualRuntime",
            "runtime_identity": BASELINE_RUNTIME_IDENTITY,
            "seats": "the three non-focal seats in both arms, fresh per game and seat",
        },
        "selection_policy_identity": SELECTION_POLICY_IDENTITY,
    }


def protocol_document() -> dict[str, object]:
    """lockとresultが共有するprotocol v1 block（完全にinvariant）。"""
    return {
        "backend": BACKEND_NAME,
        "budget": {
            "hanchan_count": HANCHAN_COUNT,
            "hanchan_per_block": HANCHAN_PER_BLOCK,
            "paired_unit_count": PAIRED_UNIT_COUNT,
            "paired_units_per_block": PAIRED_UNITS_PER_BLOCK,
            "rationale": BUDGET_RATIONALE,
            "seed_block_count": SEED_BLOCK_COUNT,
        },
        "comparison": comparison_document(),
        "dependencies": {
            "lisjong": LISJONG_REVISION,
            "lisjong-engine": LISJONG_ENGINE_REVISION,
            "torch": TORCH_VERSION,
        },
        "endpoint": endpoint_document(),
        "pairing": {
            "arms": list(ARMS),
            "focal_seats": list(FOCAL_SEATS),
            "rotation_rule": ROTATION_RULE,
        },
        "parent_issue": PARENT_ISSUE,
        "protocol_id": PROTOCOL_ID,
        "protocol_version": PROTOCOL_VERSION,
        "rules": dict(RULES),
        "seed_allocation": {
            "excluded_diagnostic_seeds": [
                EXCLUDED_DIAGNOSTIC_SEEDS.start,
                EXCLUDED_DIAGNOSTIC_SEEDS.stop - 1,
            ],
            "freshness": "disjoint from every other Arena Seed Registry "
            "allocation in any seed domain and from the unregistered "
            "diagnostic seeds",
            "owner_issue": OWNER_ISSUE,
            "population": ALLOCATION_POPULATION,
            "seed_domain": SEED_DOMAIN,
            "seed_membership": {
                "first": ORDERED_SEEDS[0],
                "kind": "range",
                "last": ORDERED_SEEDS[-1],
            },
            "split": ALLOCATION_SPLIT,
        },
    }


def require_protocol_document(value: object, context: str) -> dict[str, object]:
    """persisted protocol blockがv1 invariantと完全一致することを要求する。"""
    try:
        raw = expect_object(value, set(protocol_document()), context)
    except ArtifactValidationError as exc:
        raise PairedStrengthProtocolError(str(exc)) from exc
    if dict(raw) != protocol_document():
        raise PairedStrengthProtocolError(f"{context} differs from locked protocol v1")
    return dict(raw)


if set(ORDERED_SEEDS) & set(EXCLUDED_DIAGNOSTIC_SEEDS):
    raise AssertionError("strength seeds must not overlap diagnostic seeds")


__all__ = [
    "ALLOCATION_POPULATION",
    "ALLOCATION_SPLIT",
    "ARMS",
    "BASELINE_ARM",
    "BASELINE_RUNTIME_IDENTITY",
    "BUDGET_RATIONALE",
    "CANDIDATE_ARM",
    "CANDIDATE_ARTIFACT_FILE_SHA256",
    "CANDIDATE_ARTIFACT_IDENTITY",
    "CANDIDATE_RUNTIME_IDENTITY",
    "CLASSIFICATION_RULE_ID",
    "EXCLUDED_DIAGNOSTIC_SEEDS",
    "FINAL_POINTS_PER_PT",
    "FOCAL_SEATS",
    "HANCHAN_COUNT",
    "HANCHAN_PER_BLOCK",
    "IMPROVED_KIND",
    "IMPROVED_LABEL",
    "LISJONG_ENGINE_REVISION",
    "LISJONG_REVISION",
    "MINIMUM_VALID_PAIRED_UNITS",
    "NOT_ESTABLISHED_KIND",
    "NOT_ESTABLISHED_LABEL",
    "ORDERED_SEEDS",
    "OWNER_ISSUE",
    "PAIRED_UNIT_COUNT",
    "PARENT_ISSUE",
    "PROTOCOL_ID",
    "PROTOCOL_VERSION",
    "RULES",
    "SEAT_COUNT",
    "SEED_BLOCK_COUNT",
    "SEED_DOMAIN",
    "SELECTION_POLICY_IDENTITY",
    "STOP_INVALID_LABEL",
    "TORCH_VERSION",
    "TRAINING_SOURCE_IDENTITY",
    "GameAssignment",
    "PairedStrengthProtocolError",
    "comparison_document",
    "endpoint_document",
    "game_schedule",
    "protocol_document",
    "require_protocol_document",
]
