"""Executable P0/P1 qualification of the exact installed first-party teacher."""

import hashlib
import itertools
import subprocess
import sys
from dataclasses import replace
from importlib import metadata
from pathlib import Path

import lisjong
from lisjong.action_vocabulary import ACTION_VOCABULARY_SIZE, encode_action
from lisjong.policies import TwoStepUkeirePolicy
from lisjong.policies.two_step_ukeire import TwoStepUkeireCandidateEvaluation
from lisjong.policy_contract import (
    DecisionTraceRecorder,
    execute_policy,
    execute_policy_with_trace,
)

from lisjong_arena._artifact_io import (
    canonical_json_text,
    parse_json_text,
    write_new_artifact_file,
)
from lisjong_arena.environment_identity import verify_environment
from lisjong_arena.learned_policy_input import FEATURE_DIM, schema_fingerprint
from lisjong_arena.learned_policy_stage2.protocol import (
    LOCKED_VOCABULARY_FINGERPRINT,
    verify_contract_identity,
)

from .fixtures import discard, probes
from .semantics import OffenseError, audit_discard, audit_trace, stage_sets

SCHEMA = "arena-offense-o0-prerequisites-v1"
TEACHER = "lisjong.policies.TwoStepUkeirePolicy"
P0_PASS = "OFFENSE TEACHER QUALIFIED"
P1_PASS = "SEMANTIC AUDIT PATH QUALIFIED"
P2_PASS = "OFFENSE SUPPORT QUALIFIED"


def digest(value):
    return hashlib.sha256(canonical_json_text(value).encode()).hexdigest()


def seal(value):
    return {**value, "identity": digest(value)}


def unseal(value):
    if type(value) is not dict or "identity" not in value:
        raise OffenseError("artifact identity missing")
    body = {k: v for k, v in value.items() if k != "identity"}
    if value["identity"] != digest(body):
        raise OffenseError("artifact identity mismatch")
    return body


def read_document(path):
    text = Path(path).read_text(encoding="utf-8")
    value = parse_json_text(text)
    unseal(value)
    if text != canonical_json_text(value):
        raise OffenseError("artifact is not canonical JSON")
    return value


def write_document(path, value):
    unseal(value)
    write_new_artifact_file(Path(path), canonical_json_text(value))
    if read_document(path) != value:
        raise OffenseError("artifact readback mismatch")


def runtime_binding(project="pyproject.toml"):
    project = Path(project).resolve()
    if (
        project.parent != Path(__file__).resolve().parents[3]
        or project.name != "pyproject.toml"
    ):
        raise OffenseError(
            "STOP / INVALID: project must own the imported Arena checkout"
        )
    check = verify_environment(project)
    if check.errors:
        raise OffenseError("STOP / INVALID: " + "; ".join(check.errors))
    verify_contract_identity()
    if FEATURE_DIM != 8204 or ACTION_VOCABULARY_SIZE != 802:
        raise OffenseError("STUDENT CONTRACT STALE")

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(project.parent), *args], text=True
        ).strip()

    if git("status", "--porcelain", "--untracked-files=no") or git(
        "status", "--porcelain", "--untracked-files=normal", "--", "src"
    ):
        raise OffenseError("STOP / INVALID: tracked source changes must be committed")
    # Bind the actually imported source as well as package-manager VCS metadata.
    root = Path(lisjong.__file__).parent
    sources = {
        str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in sorted(root.rglob("*.py"))
    }
    return {
        "arena_revision": git("rev-parse", "HEAD"),
        "dependencies": {i.name: i.revision for i in check.identities},
        "lisjong_source_digest": digest(sources),
        "teacher": TEACHER,
        "feature_fingerprint": schema_fingerprint(),
        "vocabulary_fingerprint": LOCKED_VOCABULARY_FINGERPRINT,
        "feature_dimension": 8204,
        "vocabulary_size": 802,
        "python": ".".join(map(str, sys.version_info[:3])),
        "riichienv": metadata.version("riichienv"),
    }


def candidate_records(stages):
    return (
        []
        if stages is None
        else [
            {
                "action_index": encode_action(e.action),
                "post_discard_shanten": e.post_discard_shanten,
                "current_ukeire_count": e.current_ukeire_count,
                "second_step_ukeire_score": e.second_step_ukeire_score,
            }
            for e in stages.candidates
        ]
    )


def qualify(binding):
    """No game/seed execution. Failing behavior is recorded, never corrected."""
    p0, p1, evidence = [], [], []
    for probe in probes():
        behavior_ok, semantic_ok, baseline = True, True, None
        for actions in itertools.permutations(probe.context.legal_actions):
            context = replace(probe.context, legal_actions=actions)
            recorder = DecisionTraceRecorder()
            try:
                selected = execute_policy_with_trace(
                    TwoStepUkeirePolicy(), context, recorder
                )
                behavior_ok &= selected == probe.expected
                # Separate qualification calls establish parity and repeatability;
                # corpus generation consumes only the one actual execution trace.
                policy = TwoStepUkeirePolicy()
                behavior_ok &= execute_policy(policy, context) == selected
                behavior_ok &= execute_policy(policy, context) == selected
            except Exception as error:
                behavior_ok = False
                evidence.append({"probe": probe.name, "error": type(error).__name__})
                continue
            try:
                stages = audit_trace(recorder.snapshot()[0])
                rows = candidate_records(stages)
                if baseline is None:
                    baseline = rows
                semantic_ok &= rows == baseline
                if probe.name == "maximum_second_step":
                    semantic_ok &= [
                        (
                            r["post_discard_shanten"],
                            r["current_ukeire_count"],
                            r["second_step_ukeire_score"],
                        )
                        for r in rows
                    ] == [(1, 21, 122), (1, 21, 126)]
                if probe.name == "evaluated_zero_current_ukeire":
                    semantic_ok &= rows[0]["current_ukeire_count"] == 0
                if probe.name == "stable_red_five_tie":
                    semantic_ok &= len({r["action_index"] for r in rows}) == 2
                    for e in stages.candidates:
                        result = audit_discard(stages, e.action)
                        semantic_ok &= result["second_step_regret"] == 0
            except Exception as error:
                semantic_ok = False
                evidence.append({"probe": probe.name, "error": type(error).__name__})
        p0.append({"probe": probe.name, "pass": bool(behavior_ok)})
        p1.append({"probe": probe.name, "pass": bool(semantic_ok and behavior_ok)})
        evidence.append({"probe": probe.name, "candidates": baseline})
    # Typed-value adapter probes supplement actual lisjong execution: evaluated
    # zero at second-step must remain eligible; a shanten miss is not ukeire success.
    try:
        a, b, c = discard("1m"), discard("2m"), discard("3m")
        stages = stage_sets(
            (
                TwoStepUkeireCandidateEvaluation(a, 1, 0, 0),
                TwoStepUkeireCandidateEvaluation(b, 1, 0, 0),
                TwoStepUkeireCandidateEvaluation(c, 2, None, None),
            )
        )
        passed = (
            audit_discard(stages, b)["second_step_conditional_agreement"] is True
            and audit_discard(stages, b)["second_step_regret"] == 0
            and audit_discard(stages, c)["ukeire_conditional_agreement"] is None
            and audit_discard(stages, c)["second_step_conditional_agreement"] is None
        )
    except Exception:
        passed = False
    p1.append({"probe": "typed_zero_and_conditional_denominators", "pass": passed})
    return seal(
        {
            "schema": SCHEMA,
            "kind": "qualification",
            "binding": binding,
            "p0": P0_PASS
            if all(c["pass"] for c in p0)
            else "OFFENSE TEACHER NOT QUALIFIED",
            "p1": P1_PASS
            if all(c["pass"] for c in p1)
            else "SEMANTIC AUDIT PATH NOT QUALIFIED",
            "p0_checks": p0,
            "p1_checks": p1,
            "evidence": evidence,
        }
    )


def validate_qualification(report):
    body = unseal(report)
    if (
        set(body)
        != {
            "schema",
            "kind",
            "binding",
            "p0",
            "p1",
            "p0_checks",
            "p1_checks",
            "evidence",
        }
        or report["schema"] != SCHEMA
        or report["kind"] != "qualification"
    ):
        raise OffenseError("invalid qualification artifact")
    names = [p.name for p in probes()]
    for field, expected in (
        ("p0_checks", names),
        ("p1_checks", [*names, "typed_zero_and_conditional_denominators"]),
    ):
        checks = report[field]
        if type(checks) is not list or checks != [
            {"probe": name, "pass": True} for name in expected
        ]:
            raise OffenseError("all declared qualification checks must pass")
    if report["p0"] != P0_PASS or report["p1"] != P1_PASS:
        raise OffenseError("P0/P1 PASS required")


def require_qualification(report, binding):
    # Re-run bounded fixtures instead of trusting caller-supplied PASS strings.
    if report != qualify(binding):
        raise OffenseError(
            "qualification is stale, altered, or bound to another runtime"
        )
    if report["p0"] != P0_PASS or report["p1"] != P1_PASS:
        raise OffenseError(f"{report['p0']}; {report['p1']}")
