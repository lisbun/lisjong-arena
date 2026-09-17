"""retained #140/#190 flat-BC corpusのexact augmentation qualification。

retained rowへhidden truthを付けてよいのは、retained rowとprivileged stateが
次のすべてでexactに一致することを実証できた場合だけである。

```text
same dataset identity        strict readbackしたmanifestのdataset_identity
same source-semantic         lisjong / lisjong-engine / RiichiEnv / Python /
  provenance                 execution environment（`SOURCE_SEMANTIC_PROVENANCE_FIELDS`）
same decision identity       (seed, step_ordinal, decision_ordinal)
same acting seat             actor_seat
same round identity          round_wind / hand_number / honba / round_ordinal
same PolicyInput semantics   byte-identical 8204 float32 feature row
same legal-action context    byte-identical 802 legal mask + legal_action_count
same teacher action          behavior action index
same logical decision state  privileged snapshotがpublic own-hand / meld /
                             riichi stateを再現する（`require_same_decision_state`）
```

1つでも一致しなければ`RETAINED AUGMENTATION NOT QUALIFIED`であり、heuristic
replay fillingで埋めない。retained rowを黙って再生成して「retained dataset」と
呼ぶこともしない。

## instrumentation provenanceを一致必須にしない理由

#258のobserver / qualification implementationは、retained corpusを生成した
historical Arena revisionには存在しない。したがって
`lisjong_arena_revision == <historical>`を満たしながらこのqualificationを
実行することは論理的に不可能であり、それを一致必須にすると、exact row
alignmentを一度も実証しないままinstrumentation revisionが違うという理由
だけでretained routeが恒久的にNOT QUALIFIEDになる。

そこでArena revisionはinstrumentation provenanceとして両側を記録し、
一致は要求しない。代わりに上記のexact alignmentが、instrumentationを含む
replayでもretained public row semanticsを再現したことを実証する。

hidden stateのexactnessを決めるsource-semantic dependency（teacher / Policy
semanticsを持つlisjong、rulesを持つlisjong-engine、dealとstate表現を持つ
RiichiEnv、interpreter）は引き続き完全一致を要求してfail closedする。
current dependency driftを無視してQUALIFIEDにはしない。operatorは
historical execution environmentを再現する必要がある
（`docs/stage-a0-tenpai-label-feasibility.md`を参照）。

exposure boundary: このpathはTRAIN-side seedしか実行・参照しない。
protected TEST（271..276）とVALIDATION（265..270）のrow featureは読まず、
そのtarget behaviorも要約しない。
"""

from array import array
from dataclasses import dataclass

from lisjong_arena.learned_policy_offline_q.artifact import (
    LoadedOfflineQDataset,
    load_dataset,
    provenance_document,
)
from lisjong_arena.learned_policy_stage2.recording import RoundOrdinals

from .emission import emit_decision
from .errors import StageA0ProtocolError
from .execution import observed_decisions_for_seed
from .protocol import (
    EXCLUDED_QUALIFICATION_SEEDS,
    INSTRUMENTATION_PROVENANCE_FIELDS,
    RETAINED_DATASET_IDENTITY,
    RETAINED_TRAIN_SEEDS,
    SOURCE_SEMANTIC_PROVENANCE_FIELDS,
    Split,
    require_qualification_seed,
)

RETAINED_AUGMENTATION_QUALIFIED = "RETAINED AUGMENTATION QUALIFIED"
RETAINED_AUGMENTATION_NOT_QUALIFIED = "RETAINED AUGMENTATION NOT QUALIFIED"


@dataclass(frozen=True, slots=True)
class RetainedQualification:
    """retained augmentation qualificationの機械可読な結果。

    `cells`はqualifiedな場合にだけ非空になる。not qualifiedのときは理由を
    `rejection_reason`と`rejection_detail`で必ず説明し、部分的なlabelを
    「retained augmentation」として返さない。
    """

    outcome: str
    dataset_identity: str | None
    examined_seeds: tuple[int, ...]
    examined_row_count: int
    aligned_row_count: int
    instrumentation_provenance: dict | None
    rejection_reason: str | None
    rejection_detail: str | None
    cells: tuple

    def __post_init__(self) -> None:
        if self.outcome not in (
            RETAINED_AUGMENTATION_QUALIFIED,
            RETAINED_AUGMENTATION_NOT_QUALIFIED,
        ):
            raise StageA0ProtocolError("unsupported retained qualification outcome")
        if self.outcome == RETAINED_AUGMENTATION_QUALIFIED:
            if self.rejection_reason is not None:
                raise StageA0ProtocolError(
                    "a qualified retained route must not carry a rejection reason"
                )
            if self.aligned_row_count != self.examined_row_count:
                raise StageA0ProtocolError(
                    "a qualified retained route must align every examined row"
                )
        elif self.rejection_reason is None:
            raise StageA0ProtocolError(
                "a rejected retained route must state its rejection reason"
            )

    @property
    def is_qualified(self) -> bool:
        return self.outcome == RETAINED_AUGMENTATION_QUALIFIED

    def to_document(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "dataset_identity": self.dataset_identity,
            "examined_seeds": list(self.examined_seeds),
            "examined_row_count": self.examined_row_count,
            "aligned_row_count": self.aligned_row_count,
            "instrumentation_provenance": self.instrumentation_provenance,
            "rejection_reason": self.rejection_reason,
            "rejection_detail": self.rejection_detail,
        }


def _rejected(
    reason: str,
    detail: str,
    *,
    dataset_identity: str | None = None,
    examined_seeds: tuple[int, ...] = (),
    examined_row_count: int = 0,
    aligned_row_count: int = 0,
    instrumentation_provenance: dict | None = None,
) -> RetainedQualification:
    return RetainedQualification(
        outcome=RETAINED_AUGMENTATION_NOT_QUALIFIED,
        dataset_identity=dataset_identity,
        examined_seeds=examined_seeds,
        examined_row_count=examined_row_count,
        aligned_row_count=aligned_row_count,
        instrumentation_provenance=instrumentation_provenance,
        rejection_reason=reason,
        rejection_detail=detail,
        cells=(),
    )


def _require_classified_provenance(provenance: dict, context: str) -> None:
    """provenance documentが既知のfield集合そのものであることを確認する。

    未分類のfieldをsilentに無視すると、後からprovenance schemaが増えたときに
    source-semantic driftを見逃す。過不足はfail closedにする。
    """
    known = set(SOURCE_SEMANTIC_PROVENANCE_FIELDS) | set(
        INSTRUMENTATION_PROVENANCE_FIELDS
    )
    actual = set(provenance)
    if actual != known:
        missing = sorted(known - actual)
        unknown = sorted(actual - known)
        raise StageA0ProtocolError(
            f"{context} provenance fields are not the classified set; "
            f"missing={missing!r} unclassified={unknown!r}"
        )


def compare_source_semantic_provenance(
    manifest_provenance: dict, current_provenance: dict
) -> tuple[str, ...]:
    """hidden-state exactnessに必要なprovenance fieldの相違だけを返す。

    instrumentation provenance（Arena revision）はここに含めない。
    """
    return tuple(
        sorted(
            name
            for name in SOURCE_SEMANTIC_PROVENANCE_FIELDS
            if manifest_provenance[name] != current_provenance[name]
        )
    )


def instrumentation_provenance_delta(
    manifest_provenance: dict, current_provenance: dict
) -> dict[str, object]:
    """retained側と現在のinstrumentation provenanceを両方記録する。

    一致は要求しないが、どのArena revisionがreplayを実行したかは必ず残す。
    """
    return {
        name: {
            "retained": manifest_provenance[name],
            "qualification": current_provenance[name],
            "matches": manifest_provenance[name] == current_provenance[name],
        }
        for name in INSTRUMENTATION_PROVENANCE_FIELDS
    }


def _train_row_indices(dataset: LoadedOfflineQDataset, seeds) -> dict[int, list[int]]:
    """TRAIN-side seedのrow indexだけを集める。

    VALIDATION / protected TESTのrowはindexも収集しない。
    """
    wanted = set(seeds)
    grouped: dict[int, list[int]] = {seed: [] for seed in wanted}
    for index, row in enumerate(dataset.rows):
        if row.seed not in wanted:
            continue
        if row.split is not Split.TRAIN:
            raise StageA0ProtocolError(
                f"seed {row.seed} is not a TRAIN-side row in the retained dataset"
            )
        grouped[row.seed].append(index)
    return grouped


def qualify_retained_augmentation(
    dataset_path,
    *,
    seeds=RETAINED_TRAIN_SEEDS,
    expected_dataset_identity: str = RETAINED_DATASET_IDENTITY,
    observed_decision_source=observed_decisions_for_seed,
    current_provenance=None,
) -> RetainedQualification:
    """retained corpusのexact augmentationをfail closedで資格判定する。

    `observed_decision_source`は`seed -> Iterable[ObservedDecision]`のcallable
    である。defaultは現行revisionでのdeterministic re-executionであり、testでは
    このseamへ決定的なfixtureを渡す。heuristic replay fillingは行わない。
    """
    seeds = tuple(seeds)
    for seed in seeds:
        require_qualification_seed(seed)
    forbidden = set(seeds).intersection(EXCLUDED_QUALIFICATION_SEEDS)
    if forbidden:
        raise StageA0ProtocolError(
            "the #258 qualification path must not examine VALIDATION or "
            f"protected TEST seeds: {sorted(forbidden)!r}"
        )

    try:
        dataset = load_dataset(dataset_path)
    except Exception as error:  # strict readback failure is a hard rejection
        return _rejected(
            "retained-artifact-unreadable",
            f"{type(error).__name__}: {error}",
        )

    identity = dataset.identity
    if identity != expected_dataset_identity:
        return _rejected(
            "dataset-identity-mismatch",
            f"expected {expected_dataset_identity!r}; got {identity!r}",
            dataset_identity=identity,
        )

    manifest_provenance = dataset.manifest["provenance"]
    if current_provenance is None:
        current_provenance = provenance_document()
    _require_classified_provenance(manifest_provenance, "retained corpus")
    _require_classified_provenance(current_provenance, "qualification run")
    instrumentation = instrumentation_provenance_delta(
        manifest_provenance, current_provenance
    )
    differing = compare_source_semantic_provenance(
        manifest_provenance, current_provenance
    )
    if differing:
        return _rejected(
            "provenance-revision-mismatch",
            "the retained corpus was executed under different source semantics; "
            "reproduce the historical execution environment before qualifying. "
            f"differing source-semantic provenance fields: {list(differing)!r}",
            dataset_identity=identity,
            instrumentation_provenance=instrumentation,
        )

    grouped = _train_row_indices(dataset, seeds)
    examined_row_count = sum(len(indices) for indices in grouped.values())
    aligned_row_count = 0
    cells = []

    for seed in seeds:
        indices = grouped[seed]
        if not indices:
            return _rejected(
                "retained-row-missing",
                f"seed {seed} contributes no retained TRAIN row",
                dataset_identity=identity,
                examined_seeds=seeds,
                examined_row_count=examined_row_count,
                aligned_row_count=aligned_row_count,
                instrumentation_provenance=instrumentation,
            )

        rounds = RoundOrdinals()
        emissions = {}
        round_ordinals = {}
        for decision in observed_decision_source(seed):
            try:
                emission = emit_decision(
                    decision, source_identity=identity, seed=seed, split=Split.TRAIN
                )
            except Exception as error:
                return _rejected(
                    "same-state-co-emission-failed",
                    f"seed {seed}: {type(error).__name__}: {error}",
                    dataset_identity=identity,
                    examined_seeds=seeds,
                    examined_row_count=examined_row_count,
                    aligned_row_count=aligned_row_count,
                    instrumentation_provenance=instrumentation,
                )
            key = (
                decision.step_ordinal,
                decision.decision_ordinal,
                int(decision.actor_seat),
            )
            emissions[key] = emission
            # `round_ordinal`はretained artifactが保持するderived valueなので、
            # 同じcontiguous grouping ruleでre-executionからも導出して比較する。
            round_state = decision.context.input.round
            round_ordinals[key] = rounds.resolve(
                (
                    round_state.round_wind.value,
                    round_state.hand_number,
                    round_state.honba,
                )
            )

        for index in indices:
            row = dataset.rows[index]
            key = (row.step_ordinal, row.decision_ordinal, row.actor_seat)
            emission = emissions.get(key)

            def reject(reason: str, detail: str) -> RetainedQualification:
                return _rejected(
                    reason,
                    f"seed {seed} row {index}: {detail}",
                    dataset_identity=identity,
                    examined_seeds=seeds,
                    examined_row_count=examined_row_count,
                    aligned_row_count=aligned_row_count,
                    instrumentation_provenance=instrumentation,
                )

            if emission is None:
                return reject(
                    "decision-identity-mismatch",
                    f"no re-executed decision at {key!r}",
                )
            for name, retained_value, replayed_value in (
                ("round_wind", row.round_wind, emission.row.round_wind),
                ("hand_number", row.hand_number, emission.row.hand_number),
                ("honba", row.honba, emission.row.honba),
                ("round_ordinal", row.round_ordinal, round_ordinals[key]),
            ):
                if retained_value != replayed_value:
                    return reject(
                        "round-identity-mismatch",
                        f"{name} differs: retained {retained_value!r} != "
                        f"replayed {replayed_value!r}",
                    )
            if (
                array("f", dataset.feature_row(index)).tobytes()
                != emission.row.feature_bytes()
            ):
                return reject(
                    "feature-row-mismatch",
                    "feature bytes differ from the re-executed decision",
                )
            if dataset.legal_mask_row(index) != emission.row.legal_mask:
                return reject(
                    "legal-mask-mismatch",
                    "legal mask differs from the re-executed decision",
                )
            if row.legal_action_count != emission.row.legal_action_count:
                return reject(
                    "legal-mask-mismatch",
                    f"legal_action_count differs: retained "
                    f"{row.legal_action_count} != replayed "
                    f"{emission.row.legal_action_count}",
                )
            if row.behavior_action_index != emission.row.teacher_action_index:
                return reject(
                    "teacher-action-mismatch",
                    "behavior action differs from the re-executed decision",
                )
            aligned_row_count += 1
            cells.extend(emission.cells)

    return RetainedQualification(
        outcome=RETAINED_AUGMENTATION_QUALIFIED,
        dataset_identity=identity,
        examined_seeds=seeds,
        examined_row_count=examined_row_count,
        aligned_row_count=aligned_row_count,
        instrumentation_provenance=instrumentation,
        rejection_reason=None,
        rejection_detail=None,
        cells=tuple(cells),
    )


__all__ = [
    "RETAINED_AUGMENTATION_NOT_QUALIFIED",
    "RETAINED_AUGMENTATION_QUALIFIED",
    "RetainedQualification",
    "compare_source_semantic_provenance",
    "instrumentation_provenance_delta",
    "qualify_retained_augmentation",
]
