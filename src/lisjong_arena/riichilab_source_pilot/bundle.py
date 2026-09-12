"""retained bundle全体のstrict readbackとcross-binding照合。

個々のfile（checkpoint manifest、seed plan、strength artifact、result）は
それぞれ自己整合性をstrict-readできるが、**互いに無関係なのに単体としては
valid**なfileを1つのbundleへ集めた状態はそれだけでは検出できない。この
moduleはbundleを1つのevidence unitとして扱い、次のcross-bindingを全件
照合する。

```text
checkpoints/ARM_R, ARM_Y
    arm            == directory name
    source         == Issue #211のlocked source identity（自己整合hashではない）
    policy識別子    == derive_policy_identity(arm, checkpoint_identity)
result.sources     == locked source identity block
result.gate0       == Arm R checkpointが束ねるGate 0 report
result.budget      == locked 9,116 / 2,555 と Arm R dataset row count
result.arms[arm]   == 各checkpointのidentity document（weights sha256まで）
seed-plan
    candidate / baseline == Arm R / Arm Y のpolicy identity
strength artifact
    candidate / baseline == seed planと同じidentity
    seeds / games / game mode / rotation / seed blockはlocked population
    summaryはraw game resultsから再集計したcanonical summaryと一致
result.strength    == 再集計したcanonical summary document
result.outcome     == classify_outcome(...)の再計算結果
```

どれか1つでも一致しなければfail closedする。近い値での代替、片側fallback、
欠損fileの黙認は行わない。
"""

from pathlib import Path

from lisjong_arena.single_round_artifact import load_single_round_artifact

from .artifact import (
    CHECKPOINTS_DIRNAME,
    RESULT_FILENAME,
    SEED_PLAN_FILENAME,
    STRENGTH_ARTIFACT_FILENAME,
    LoadedCheckpoint,
    load_checkpoint,
    load_result,
    load_seed_plan,
)
from .errors import SourcePilotArtifactError
from .evaluation import StrengthMeasurement, verify_strength_artifact
from .outcome import classify_outcome
from .protocol import (
    ARM_R,
    ARM_R_CORPUS_IDENTITY,
    ARM_R_MANIFEST_SHA256,
    ARM_SOURCE_IDENTITY,
    ARM_Y,
    ARM_Y_DATASET_IDENTITY,
    TRAIN_ROW_BUDGET,
    VALIDATION_ROW_BUDGET,
    Arm,
    SourcePilotOutcome,
    derive_policy_identity,
    source_identity_block,
)

#: bundle直下に存在しうるentry名。これ以外のentryがあれば、そのbundleは
#: Issue #211のevidence unitとして扱えない。
BUNDLE_ENTRIES = frozenset(
    {
        RESULT_FILENAME,
        SEED_PLAN_FILENAME,
        STRENGTH_ARTIFACT_FILENAME,
        CHECKPOINTS_DIRNAME,
    }
)

#: ABBB comparisonまで到達したoutcome。これらは完全なbundleを要求する。
COMPLETED_OUTCOMES = frozenset(
    {
        SourcePilotOutcome.RIICHILAB_SOURCE_SIGNAL,
        SourcePilotOutcome.YAKUHAI_CALL_SOURCE_SIGNAL,
        SourcePilotOutcome.SOURCE_PILOT_INCONCLUSIVE,
    }
)

#: comparison前に停止したoutcome。resultだけを持つ。
BLOCKED_OUTCOMES = frozenset(
    {
        SourcePilotOutcome.SOURCE_MATERIALIZATION_BLOCKED,
        SourcePilotOutcome.DATA_BUDGET_NOT_MATCHABLE,
    }
)


# outcomeごとに要求するfile集合が違うため、3分割がexhaustiveであることを
# import時に固定する。outcomeが増えたときに黙って「完全なbundle」扱いへ
# 落ちないようにする。
if COMPLETED_OUTCOMES | BLOCKED_OUTCOMES | {SourcePilotOutcome.STOP_INVALID} != set(
    SourcePilotOutcome
):
    raise RuntimeError("the bundle outcome partition is not exhaustive")


def _require(condition: object, message: str) -> None:
    if not condition:
        raise SourcePilotArtifactError(message)


def _verify_checkpoint_source(checkpoint: LoadedCheckpoint, arm: Arm) -> None:
    """checkpointがIssue #211のlocked source identityへbindしていることを確認する。

    manifest内部で整合しているhashであることでは足りない。locked value
    そのものと一致しなければ、そのcheckpointは別実験の成果物である。
    """
    source = checkpoint.manifest["source"]
    _require(
        source.get("arm") == arm.value,
        f"{arm.value} checkpoint source block is not bound to {arm.value}",
    )
    _require(
        source.get("source_identity") == ARM_SOURCE_IDENTITY[arm],
        f"{arm.value} checkpoint source identity is not the locked Issue #211 value",
    )
    if arm is ARM_R:
        corpus = source.get("corpus_source_identity")
        _require(
            type(corpus) is dict,
            "Arm R checkpoint does not carry a corpus source identity block",
        )
        _require(
            corpus.get("corpus_identity") == ARM_R_CORPUS_IDENTITY,
            "Arm R checkpoint corpus identity is not the locked #170 corpus",
        )
        _require(
            corpus.get("manifest_sha256") == ARM_R_MANIFEST_SHA256,
            "Arm R checkpoint manifest sha256 is not the locked #170 manifest",
        )
        _require(
            type(source.get("gate0")) is dict,
            "Arm R checkpoint does not carry the Gate 0 report it was built from",
        )
    else:
        _require(
            source.get("dataset_identity") == ARM_Y_DATASET_IDENTITY,
            "Arm Y checkpoint dataset identity is not the exact retained identity",
        )


def load_bundle_checkpoints(bundle: Path) -> dict[Arm, LoadedCheckpoint]:
    """両armのcheckpointをstrict-readし、locked source identityへbindする。"""
    root = bundle / CHECKPOINTS_DIRNAME
    _require(root.is_dir(), "bundle does not contain a checkpoints directory")
    names = {entry.name for entry in root.iterdir()}
    _require(
        names == {ARM_Y.value, ARM_R.value},
        "the checkpoints directory must contain exactly the two pilot arms",
    )
    checkpoints: dict[Arm, LoadedCheckpoint] = {}
    for arm in (ARM_Y, ARM_R):
        checkpoint = load_checkpoint(root / arm.value)
        _require(
            checkpoint.arm is arm,
            f"the checkpoint stored as {arm.value} declares a different arm",
        )
        _verify_checkpoint_source(checkpoint, arm)
        # policy identityはarm prefix + checkpoint identityから導出される。
        # manifestへ別途記録した値を信用せず、ここで導出し直す。
        _require(
            checkpoint.policy_identity
            == derive_policy_identity(arm, checkpoint.identity),
            f"{arm.value} policy identity is not derived from its checkpoint",
        )
        checkpoints[arm] = checkpoint
    _require(
        checkpoints[ARM_R].identity != checkpoints[ARM_Y].identity,
        "the two arms must not share one checkpoint identity",
    )
    return checkpoints


def _verify_result_shape(result: dict[str, object]) -> SourcePilotOutcome:
    outcome = SourcePilotOutcome(result["outcome"])
    _require(
        result.get("sources") == source_identity_block(),
        "result source identity block is not the locked Issue #211 block",
    )
    retention = result.get("retention")
    _require(
        type(retention) is dict and bool(retention.get("key")),
        "result does not record the retention target it was published under",
    )
    if outcome in BLOCKED_OUTCOMES or outcome is SourcePilotOutcome.STOP_INVALID:
        _require(
            bool(result.get("stop_reason")),
            "a non-completed result must record why it stopped",
        )
    else:
        _require(
            result.get("stop_reason") is None,
            "a completed result must not record a stop reason",
        )
    return outcome


def _verify_budget(result: dict[str, object], arm_r: LoadedCheckpoint) -> None:
    budget = result.get("budget")
    _require(type(budget) is dict, "a completed result must carry a budget block")
    _require(
        budget.get("train_rows") == TRAIN_ROW_BUDGET
        and budget.get("validation_rows") == VALIDATION_ROW_BUDGET,
        "result budget is not the matched 9,116 / 2,555 row budget",
    )
    source = arm_r.manifest["source"]
    _require(
        source.get("train_row_count") == TRAIN_ROW_BUDGET
        and source.get("validation_row_count") == VALIDATION_ROW_BUDGET,
        "the Arm R checkpoint was not trained on the matched row budget",
    )
    _require(
        result.get("gate0") == source.get("gate0"),
        "the result Gate 0 report differs from the Arm R checkpoint's own report",
    )


def _verify_arms(
    result: dict[str, object], checkpoints: dict[Arm, LoadedCheckpoint]
) -> None:
    arms = result.get("arms")
    _require(type(arms) is dict, "a completed result must carry an arms block")
    _require(
        arms.get("training_config_identity")
        == checkpoints[ARM_R].manifest["training_config_identity"],
        "result training config identity differs from the Arm R checkpoint",
    )
    for arm, checkpoint in checkpoints.items():
        recorded = arms.get(arm.value)
        _require(
            type(recorded) is dict,
            f"the result does not record the {arm.value} arm",
        )
        _require(
            recorded.get("checkpoint") == checkpoint.identity_document(),
            f"the result {arm.value} checkpoint record differs from the "
            f"retained {arm.value} checkpoint",
        )


def _verify_seed_plan(
    bundle: Path, checkpoints: dict[Arm, LoadedCheckpoint]
) -> dict[str, object]:
    path = bundle / SEED_PLAN_FILENAME
    _require(path.is_file(), "bundle does not contain the locked seed plan")
    plan = load_seed_plan(path)
    _require(
        plan["candidate_identity"] == checkpoints[ARM_R].policy_identity,
        "the seed plan candidate is not the retained Arm R policy",
    )
    _require(
        plan["baseline_identity"] == checkpoints[ARM_Y].policy_identity,
        "the seed plan baseline is not the retained Arm Y policy",
    )
    return plan


def verify_bundle(path: str | Path) -> dict[str, object]:
    """1つのretained bundleをevidence unitとしてstrict-readする。

    outcomeに応じて要求するfile集合が変わる。

    - comparisonまで到達したoutcomeは全fileとcross-bindingを要求する
    - Gate 0 / budgetで停止したoutcomeはresultのみを許す
    - `STOP / INVALID`は部分的な実行痕跡を許すが、存在するfileは
      すべてstrict-readし、両側が揃うcross-bindingは照合する
    """
    bundle = Path(path)
    _require(bundle.is_dir(), "bundle path is not a directory")
    entries = {entry.name for entry in bundle.iterdir()}
    unknown = sorted(entries - BUNDLE_ENTRIES)
    _require(not unknown, f"bundle contains unexpected entries: {unknown}")
    _require(
        RESULT_FILENAME in entries,
        "bundle does not contain a source-pilot result artifact",
    )

    result = load_result(bundle / RESULT_FILENAME)
    outcome = _verify_result_shape(result)
    document: dict[str, object] = {
        "bundle": str(bundle),
        "outcome": outcome.value,
        "result_identity": result["result_identity"],
        "stop_reason": result.get("stop_reason"),
    }

    if outcome in BLOCKED_OUTCOMES:
        _require(
            entries == {RESULT_FILENAME},
            "a result that stopped before training must not retain checkpoints, "
            "a seed plan or a strength artifact",
        )
        _require(
            result.get("arms") is None and result.get("strength") is None,
            "a result that stopped before training must not carry arms or strength",
        )
        document["verified"] = "result-only terminal outcome"
        return document

    if outcome is SourcePilotOutcome.STOP_INVALID:
        document["verified"] = _verify_partial_bundle(bundle, entries, result)
        return document

    _require(
        outcome in COMPLETED_OUTCOMES,
        "the recorded outcome does not belong to a completed comparison",
    )
    checkpoints = load_bundle_checkpoints(bundle)
    _verify_budget(result, checkpoints[ARM_R])
    _verify_arms(result, checkpoints)
    seed_plan = _verify_seed_plan(bundle, checkpoints)
    strength_path = bundle / STRENGTH_ARTIFACT_FILENAME
    _require(
        strength_path.is_file(), "bundle does not contain the ABBB strength artifact"
    )
    artifact = load_single_round_artifact(strength_path)
    summary = verify_strength_artifact(
        artifact,
        candidate_identity=seed_plan["candidate_identity"],
        baseline_identity=seed_plan["baseline_identity"],
    )
    regenerated = StrengthMeasurement(
        candidate_identity=seed_plan["candidate_identity"],
        baseline_identity=seed_plan["baseline_identity"],
        artifact=artifact,
        summary=summary,
    ).to_document()
    _require(
        result.get("strength") == regenerated,
        "the result strength block differs from the canonical summary regenerated "
        "from the raw game results",
    )
    recomputed = classify_outcome(
        protocol_valid=True,
        gate0_passed=True,
        budget_matched=True,
        interval_lower=regenerated["interval_lower"],
        interval_upper=regenerated["interval_upper"],
    )
    _require(
        recomputed is outcome,
        "the recorded outcome is not the outcome the decision order produces",
    )

    document["verified"] = "complete comparison bundle"
    document["seed_plan_identity"] = seed_plan["seed_plan_identity"]
    document["checkpoints"] = {
        arm.value: checkpoint.identity for arm, checkpoint in checkpoints.items()
    }
    document["policies"] = {
        "candidate": seed_plan["candidate_identity"],
        "baseline": seed_plan["baseline_identity"],
    }
    document["strength"] = {
        "games": regenerated["games"],
        "seed_blocks": regenerated["seed_blocks"],
        "interval_lower": regenerated["interval_lower"],
        "interval_upper": regenerated["interval_upper"],
    }
    return document


def _verify_partial_bundle(
    bundle: Path, entries: set[str], result: dict[str, object]
) -> str:
    """`STOP / INVALID`の実行痕跡を、存在するfileだけstrict-readする。

    `STOP / INVALID`はtrainingやevaluationの途中でも記録される。途中で
    止まったbundleに完全なfile集合を要求すると、durableなSTOP recordを
    読めなくなる。存在するfileはすべてstrict-readし、両側が揃うbinding
    だけを照合する。
    """
    _require(
        result.get("strength") is None,
        "a STOP / INVALID result must not publish a strength block",
    )
    verified = ["result"]
    checkpoints: dict[Arm, LoadedCheckpoint] = {}
    if CHECKPOINTS_DIRNAME in entries:
        root = bundle / CHECKPOINTS_DIRNAME
        for entry in sorted(root.iterdir()):
            try:
                arm = Arm(entry.name)
            except ValueError as error:
                raise SourcePilotArtifactError(
                    f"{entry.name} is not a pilot arm checkpoint"
                ) from error
            checkpoint = load_checkpoint(entry)
            _require(
                checkpoint.arm is arm,
                f"the checkpoint stored as {arm.value} declares a different arm",
            )
            _verify_checkpoint_source(checkpoint, arm)
            checkpoints[arm] = checkpoint
        verified.append(f"{len(checkpoints)} checkpoint(s)")
    if SEED_PLAN_FILENAME in entries:
        plan = load_seed_plan(bundle / SEED_PLAN_FILENAME)
        for arm, role in ((ARM_R, "candidate_identity"), (ARM_Y, "baseline_identity")):
            if arm in checkpoints:
                _require(
                    plan[role] == checkpoints[arm].policy_identity,
                    f"the seed plan {role} is not the retained {arm.value} policy",
                )
        verified.append("seed plan")
    if STRENGTH_ARTIFACT_FILENAME in entries:
        # artifactが存在する場合もSTOP / INVALIDはstrength claimを持たない。
        # protocol条件だけを確認し、resultへ昇格させない。
        artifact = load_single_round_artifact(bundle / STRENGTH_ARTIFACT_FILENAME)
        verify_strength_artifact(
            artifact,
            candidate_identity=artifact.plan.candidate_identity,
            baseline_identity=artifact.plan.baseline_identity,
        )
        verified.append("strength artifact")
    return "partial STOP / INVALID bundle: " + ", ".join(verified)


__all__ = [
    "BLOCKED_OUTCOMES",
    "BUNDLE_ENTRIES",
    "COMPLETED_OUTCOMES",
    "load_bundle_checkpoints",
    "verify_bundle",
]
