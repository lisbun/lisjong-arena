"""sample construction contractのprovenance value。

`lisjong-project #24` Phase 2は、effective rule configurationだけでなく、

```text
source / revision / anchor / cutoff / label semantics provenance
```

をbindingできることをsuccess criteriaにしている。Phase 3でcorpusを生成した
あとに、そのsampleが「どのcode revisionの、どのanchor / cutoff / label
semanticsで作られたものか」をsample contractから追えなければ、
`exact_hand_belief_with_waits()`等の意味が後日変わったときにlabelの意味を
確定できない。

本moduleはそのための最小valueだけを持つ。persistent corpus metadata schema、
generic provenance framework、dataset registryは作らない。

## Semantics identity

anchor / evidence cutoff / label semanticsは、code revisionとは別に、
**contractとして意図的に変更したとき**に上げる明示的なidentityとして持つ。
revisionだけでは、無関係なrefactorとsemantic changeを区別できないためである。

## Source revision

revisionはinstall済みdistributionのVCS metadata（`direct_url.json`の
`vcs_info.commit_id`）から取得する。既存`lisjong_arena.artifact`と同じ
情報源であり、独自のrevision解決経路を増やさない。

取得できない場合（local editable install等）は`None`とし、**値を捏造しない**。
Phase 3のcorpus generationは、resolve済みrevisionを要求する側で判定する。
Phase 2ではcontractのbinding可能性を固定することが目的であり、生成時の
enforcement policyはここで先取りしない。
"""

import json
import re
from dataclasses import dataclass
from importlib import metadata

from lisjong_engine.rules import RuleSet

from lisjong_arena.phase2_training_anchor.rule_provenance import (
    EffectiveRuleProvenance,
    effective_rule_provenance,
)

ANCHOR_SEMANTICS_ID = "turn-pre-action-frozen-anchor-v1"
"""anchor semanticsのidentity。

v1: anchor eligibilityは`SeatObservation.decision_kind == TURN`のみ。anchorは
到達時にfreezeし、終了後stateからの再構成をprimary pathにしない。
"""

EVIDENCE_CUTOFF_SEMANTICS_ID = "anchor-time-round-evidence-prefix-v1"
"""evidence cutoff semanticsのidentity。

v1: anchor時点で`build_round_evidence(active_round, viewer)`が返したprefixを
そのままfreezeする。anchor後に発生したeventは含めない。
"""

LABEL_SEMANTICS_ID = "exact-concealed-count-red-structural-wait-v1"
"""label builder semanticsのidentity。

v1: 3 opponents x 34 base kindのexact concealed copy count（normal/red 5は
34-axisで合算、meld牌は除外）、per-suit exact red-five presence、stable
13-equivalent handに限定したper-opponent 34-kind binary structural wait mask。
ron-legal auxiliaryは含まない。
"""

_FULL_COMMIT_ID = re.compile(r"\A[0-9a-f]{40}\Z").fullmatch

_TRACKED_DISTRIBUTIONS = ("lisjong", "lisjong-engine", "lisjong-arena")


class PipelineProvenanceError(Exception):
    """provenance解決そのものが不正な場合。"""


def _distribution_revision(distribution_name: str) -> str | None:
    """installされたdistributionのVCS commit idを返す。未解決なら`None`。

    値を推測・捏造しない。metadataが無い、VCS installでない、commit idが
    full commit IDでない場合はいずれも`None`とする。malformedなmetadataは
    `None`へ丸めず、解決経路自体の不正としてfail closedする。
    """
    try:
        direct_url_text = metadata.distribution(distribution_name).read_text(
            "direct_url.json"
        )
    except metadata.PackageNotFoundError:
        return None
    if direct_url_text is None:
        return None

    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError as exc:
        raise PipelineProvenanceError(
            f"{distribution_name} direct_url.json is malformed"
        ) from exc
    if type(direct_url) is not dict:
        raise PipelineProvenanceError(
            f"{distribution_name} direct_url.json is malformed"
        )

    vcs_info = direct_url.get("vcs_info")
    if type(vcs_info) is not dict:
        # local path / editable installはVCS revisionを持たない。
        return None
    if vcs_info.get("vcs") != "git":
        return None
    revision = vcs_info.get("commit_id")
    if type(revision) is not str or _FULL_COMMIT_ID(revision) is None:
        return None
    return revision


@dataclass(frozen=True, slots=True)
class SourceRevisions:
    """sample構成が依存する3 repositoryのcode revision。

    `None`は「この実行環境では解決できなかった」を明示する値であり、
    「revisionが無い」でも「任意のrevisionでよい」でもない。
    """

    lisjong: str | None
    lisjong_engine: str | None
    lisjong_arena: str | None

    def __post_init__(self) -> None:
        for name in ("lisjong", "lisjong_engine", "lisjong_arena"):
            value = getattr(self, name)
            if value is None:
                continue
            if type(value) is not str or _FULL_COMMIT_ID(value) is None:
                raise ValueError(
                    f"{name} revision must be a full git commit ID or None"
                )

    @property
    def fully_resolved(self) -> bool:
        """3 repository全てのrevisionが解決できているか。"""
        return None not in (self.lisjong, self.lisjong_engine, self.lisjong_arena)


@dataclass(frozen=True, slots=True)
class TrainingPipelineProvenance:
    """1回のsample構成が依存したcode revisionとsemantics identityの束。

    effective rulesもここへ含め、`TrainingSample`がanchorのrule provenanceと
    一致することを検証する。
    """

    source_revisions: SourceRevisions
    anchor_semantics_id: str
    evidence_cutoff_semantics_id: str
    label_semantics_id: str
    effective_rules: EffectiveRuleProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.source_revisions, SourceRevisions):
            raise TypeError("source_revisions must be a SourceRevisions")
        for name in (
            "anchor_semantics_id",
            "evidence_cutoff_semantics_id",
            "label_semantics_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty str")
        if not isinstance(self.effective_rules, EffectiveRuleProvenance):
            raise TypeError("effective_rules must be an EffectiveRuleProvenance")


def collect_pipeline_provenance(rules: RuleSet) -> TrainingPipelineProvenance:
    """current実行環境から、Phase 2 pipeline provenanceを構成する。

    semantics identityはこのcode revisionが実装しているcontractのidentityで
    あり、実行環境からは推定しない。
    """
    return TrainingPipelineProvenance(
        source_revisions=SourceRevisions(
            **{
                name.replace("-", "_"): _distribution_revision(name)
                for name in _TRACKED_DISTRIBUTIONS
            }
        ),
        anchor_semantics_id=ANCHOR_SEMANTICS_ID,
        evidence_cutoff_semantics_id=EVIDENCE_CUTOFF_SEMANTICS_ID,
        label_semantics_id=LABEL_SEMANTICS_ID,
        effective_rules=effective_rule_provenance(rules),
    )
