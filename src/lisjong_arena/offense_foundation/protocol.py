"""Purpose-specific pre-execution locks for #331, shared by local and AWS runs."""

from lisjong_arena.seed_registry import (
    RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
    SeedRegistryError,
    load_ledger,
    require_allocation_binding,
    validate_binding_shape,
)

from .qualification import P2_PASS, SCHEMA, seal, unseal, validate_qualification
from .semantics import OffenseError

GAME_MODE = "4p-red-half"
ALLOCATION_OWNER_ISSUE = "lisbun/lisjong-arena#332"
ALLOCATION_PROTOCOL = "offense-foundation-v1"
ALLOCATION_SEED_DOMAIN = RIICHIENV_HALF_HANCHAN_SEED_DOMAIN
SUPPORT = {
    "choice_rows": 3000,
    "winning_opportunities": 50,
    "riichi_opportunities": 100,
    "voluntary_call_opportunities": 200,
    "normal_discard_choice_rows": 2000,
    "second_step_applicable_rows": 500,
}
ZERO_FAILURES = (
    "illegal_teacher_action",
    "vocabulary_encode_failure",
    "legal_mask_failure",
    "feature_materialization_failure",
    "semantic_audit_failure",
    "non_finite_feature",
)


def support_outcome(counts):
    if set(counts) != set(SUPPORT) or any(
        type(v) is not int or v < 0 for v in counts.values()
    ):
        raise OffenseError("invalid support counts")
    return (
        P2_PASS
        if all(counts[k] >= v for k, v in SUPPORT.items())
        else "OFFENSE SUPPORT NOT QUALIFIED"
    )


def _seeds(values, count=None):
    if type(values) is not list or any(
        type(s) is not int or not 0 <= s < 2**32 for s in values
    ):
        raise OffenseError("seeds must be explicit unsigned 32-bit integers")
    if len(set(values)) != len(values):
        raise OffenseError("duplicate seed")
    if count is not None and (
        len(values) != count or values != list(range(values[0], values[0] + count))
    ):
        raise OffenseError("exact contiguous ordered population required")


def validate_request(request):
    if type(request) is not dict or set(request) != {
        "allocation_bindings",
        "phase",
        "populations",
    }:
        raise OffenseError("invalid population request fields")
    phase = request["phase"]
    if phase not in ("P2", "SCIENTIFIC"):
        raise OffenseError("phase must be P2 or SCIENTIFIC")
    expected = (
        {"QUALIFICATION": 20}
        if phase == "P2"
        else {"TRAIN": 100, "SELECT": 20, "OFFLINE-EVAL": 20}
    )
    populations = request["populations"]
    if type(populations) is not dict or set(populations) != set(expected):
        raise OffenseError("unexpected population split")
    bindings = request["allocation_bindings"]
    if type(bindings) is not dict or set(bindings) != set(expected):
        raise OffenseError("allocation bindings must exactly match population splits")
    seen = set()
    for split, size in expected.items():
        _seeds(populations[split], size)
        if seen.intersection(populations[split]):
            raise OffenseError("cross-split seed leakage")
        seen.update(populations[split])
        try:
            binding = validate_binding_shape(bindings[split], seeds=populations[split])
        except SeedRegistryError as error:
            raise OffenseError(
                f"invalid {split} allocation binding: {error}"
            ) from error
        if binding["seed_domain"] != ALLOCATION_SEED_DOMAIN:
            raise OffenseError(
                "offense-foundation allocation uses the wrong seed_domain"
            )


def require_request_allocations(request, ledger=None):
    """Require current canonical Arena allocation authority for every split.

    This check is performed before lock creation. Historical lock readback
    validates the embedded binding without consulting a newer ledger, so later
    COMMITTED/RETIRED transitions do not rewrite old scientific evidence.
    """

    validate_request(request)
    current = load_ledger() if ledger is None else ledger
    try:
        for split, seeds in request["populations"].items():
            require_allocation_binding(
                current,
                request["allocation_bindings"][split],
                seeds=seeds,
                owner_issue=ALLOCATION_OWNER_ISSUE,
                protocol=ALLOCATION_PROTOCOL,
                seed_domain=ALLOCATION_SEED_DOMAIN,
                population="offense-foundation",
                split=split,
            )
    except SeedRegistryError as error:
        raise OffenseError(f"seed allocation authority invalid: {error}") from error


def make_lock(request, qualification, p2=None):
    validate_request(request)
    validate_qualification(qualification)
    p2_reference = None
    if request["phase"] == "SCIENTIFIC":
        if p2 is None:
            raise OffenseError("strict-read complete P2 corpus required")
        validate_p2_evidence(p2)
        if p2["lock"]["request"]["phase"] != "P2" or p2["p2_outcome"] != P2_PASS:
            raise OffenseError("final OFFENSE SUPPORT QUALIFIED required")
        if p2["lock"]["qualification"] != qualification:
            raise OffenseError("P2 teacher/runtime qualification mismatch")
        seeds = p2["lock"]["request"]["populations"]["QUALIFICATION"]
        if any(set(seeds).intersection(v) for v in request["populations"].values()):
            raise OffenseError("qualification population cannot be reused")
        p2_reference = {"identity": p2["identity"], "seeds": seeds}
    elif p2 is not None:
        raise OffenseError("P2 phase must not consume another P2 result")
    return seal(
        {
            "schema": SCHEMA,
            "kind": "protocol-lock",
            "game_mode": GAME_MODE,
            "teacher_seats": 4,
            "request": request,
            "qualification": qualification,
            "p2_reference": p2_reference,
        }
    )


def validate_lock(lock, p2=None):
    body = unseal(lock)
    if set(body) != {
        "schema",
        "kind",
        "game_mode",
        "teacher_seats",
        "request",
        "qualification",
        "p2_reference",
    }:
        raise OffenseError("unexpected lock fields")
    if lock != make_lock(lock["request"], lock["qualification"], p2):
        raise OffenseError("protocol lock or P2 reference differs")
    if type(lock["teacher_seats"]) is not int:
        raise OffenseError("teacher seat count must be an integer")


def ordered_games(lock):
    populations = lock["request"]["populations"]
    splits = (
        ("QUALIFICATION",)
        if lock["request"]["phase"] == "P2"
        else ("TRAIN", "SELECT", "OFFLINE-EVAL")
    )
    return tuple((split, seed) for split in splits for seed in populations[split])


def validate_p2_evidence(p2):
    """Validate the retained P2 manifest embedded in a scientific corpus.

    Generation additionally strict-reads the original P2 payload files.
    Embedded evidence is a provenance receipt, not permission to skip readback.
    """
    body = unseal(p2)
    if (
        set(body)
        != {
            "schema",
            "kind",
            "lock",
            "p2_evidence",
            "games",
            "support",
            "failures",
            "p2_outcome",
        }
        or p2["schema"] != SCHEMA
        or p2["kind"] != "corpus"
    ):
        raise OffenseError("invalid P2 evidence")
    if p2["p2_evidence"] is not None or p2["lock"]["request"]["phase"] != "P2":
        raise OffenseError("P2 evidence must be qualification-only")
    validate_lock(p2["lock"])
    games = ordered_games(p2["lock"])
    if type(p2["games"]) is not list or len(p2["games"]) != len(games):
        raise OffenseError("incomplete P2 evidence")
    counts = dict.fromkeys(SUPPORT, 0)
    for (split, seed), game in zip(games, p2["games"], strict=True):
        unseal(game)
        if (
            game["split"],
            game["seed"],
            game["game_mode"],
            game["lock_identity"],
        ) != (
            split,
            seed,
            GAME_MODE,
            p2["lock"]["identity"],
        ):
            raise OffenseError("P2 game membership/provenance mismatch")
        support_outcome(game["support"])
        for key in counts:
            counts[key] += game["support"][key]
    if (
        p2["support"] != counts
        or p2["p2_outcome"] != support_outcome(counts)
        or p2["failures"] != dict.fromkeys(ZERO_FAILURES, 0)
    ):
        raise OffenseError("P2 evidence summary mismatch")
