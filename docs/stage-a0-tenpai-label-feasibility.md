# Stage A0 non-riichi Tenpai label path feasibility

本書は `lisjong_arena.stage_a0_tenpai_feasibility` が実装するfeasibility-only
qualification pathの契約を示す。実験そのものの目的・acceptance criteria・結果は
[lisjong-arena #258](https://github.com/lisbun/lisjong-arena/issues/258)、親の
[lisjong-arena #255](https://github.com/lisbun/lisjong-arena/issues/255)、
[lisjong-project #57](https://github.com/lisbun/lisjong-project/issues/57)を正本とする。

## Scope

このpackageが答えるのは1つの技術的問いだけである。

> current flat-BC decision rowに対応するhidden opponent stateから、canonicalな
> non-riichi structural-Tenpai targetを正確かつ再現可能に生成できるか。

```text
decision row
    -> same-state exact hidden opponent hand / melds
    -> canonical exact structural waits
    -> non-riichi mask
    -> T[j] = OR_t W[j, t]
```

次は**行わない**。

- A/T model training、Stage A0 VALIDATION learnability、Baseline 1 evaluation
- `lambda_tenpai` / class weighting / 3-seed model comparison / strength games
- Wait / Ron auxiliary、furiten reconstruction、Ron counterfactual
- Oracle Guiding / VLOG / privileged critic / Champion promotion
- formal TEST exposure、full Stage A0 scientific corpus generation

## Canonical Tenpai semantics

Tenpai判定をArenaへ新規実装しない。canonical authorityは常に次である。

```text
lisjong.belief.exact_wait_ground_truth.exact_hand_belief_with_waits()
```

Stage A0 targetはそのstructural wait maskの存在量化だけである。

```text
W[j, t] = canonical exact structural-completion-wait
T[j]    = OR_t W[j, t]
```

structural Tenpaiなので、furiten / yaku / remaining live copies / ron legality /
hand value / future action / future drawは含めない。

`phase2_training_anchor`が確立したbackend-neutralな
`OpponentIdentity` / `structural_wait_for_hand()`は
`phase2_training_anchor.structural_wait`へ分離して薄く再利用する
（`training_labels`は同じ名前を再exportし続ける）。`training_labels`が読む
lisjong-engine execution seamはStage A0では使わない。

## Same-state seam

public rowとprivileged labelは、同じpre-action decision pointから取得する。

```text
RiichiEnv (env.step() 直前の pre-action state)
    -> LocalGameRunner.decision_point_observer   (opt-in、default は None)
    -> DecisionPointHiddenState                  4 seat の concealed / meld / riichi
```

`DecisionPointObserver`はPolicy / `PolicyInput` / `GameTrace`へ接続されない。
observerを渡さない既存pathのbehaviorは変わらない。

同一stateであることは仮定せず、`require_same_decision_state()`が次をfail closedで
検証する。1つでも一致しなければlabelを作らずhard failureにする。

| 検証対象 | 内容 |
| --- | --- |
| step binding | privileged snapshotのstepがrowのstepと一致する |
| pending seat | labelするactor seatがその決定時点で実際にactionを求められている |
| actor own hand | privileged concealed multiset == public own-hand multiset |
| public melds | privileged meld snapshot == `PolicyInput.players[seat].melds` |
| riichi binding | public `RiichiState` と privileged `riichi_declared` の整合 |

後続stateからのheuristic reconstruction、future draw / action / resultの利用、
positionやseat attachmentの推測は行わない。

## Relative opponent mapping

```text
viewer / actor seat
    -> relative opponent slot (+1 / +2 / +3)
    -> canonical absolute seat = (actor + offset) mod 4
    -> seat wind (dealer 相対)
    -> concealed hand / public melds / public RiichiState
```

absolute seatをrow位置やclass名から暗黙導出しない。`OpponentIdentity`が相対
offset・absolute seat・seat windを同時にbindingするため、dealer / viewer
rotationが起きてもcellは正しいopponentへ追従する。

## Target availability / reason code

1 opponent cellは必ずexactly one classificationを持つ。評価順は固定である。

```text
SEAT_MAPPING_UNRESOLVED
RIICHI_EXCLUDED
HIDDEN_HAND_UNAVAILABLE
MELD_STATE_UNAVAILABLE
INVALID_PHYSICAL_INVENTORY
NOT_STABLE_13_EQUIVALENT
OTHER_FAIL_CLOSED
AVAILABLE
```

- `AVAILABLE`だけが`wait_mask`と`T`を持つ。非聴牌のall-zero maskは**valid label**
  であり、`T = 0`として扱う
- `AVAILABLE`以外はmaskedであり、`wait_mask` / `T`をいずれも持たない。
  **unavailableをT = 0へ丸めることはしない**
- `INVALID_PHYSICAL_INVENTORY`のstateはsilentに修復しない
- `OTHER_FAIL_CLOSED`はprechecks通過後のcanonical builder `ValueError` だけに
  割り当てる。countableであり、1件でもあればqualificationを通さない。
  `ValueError`以外の例外（programming error / infrastructure error）は
  cell-level reasonにせず、route-level hard failureとして伝播させる
- `RIICHI_EXCLUDED`はStage A0 primary targetの対象外という意味であり、
  negative labelではない

physical inventoryの上限値はlisjong `lisjong.belief.tile_inventory`が正本であり、
Arenaはその公開constantを参照するだけである。prechecksとcanonical builderが同じ
不変条件で一致することをtestで固定する。

## Public / private boundary

- public 8204 feature contractは変更しない
- `PublicDecisionRow`はfeature / legal mask / teacher action / row identityだけを
  持ち、privileged fieldを持たない
- `row_digest()`はprivileged annotationを入力に含まない。annotation attachment
  前後でdigestは変わらない
- hidden truthが異なり public stateが同じ2つのdecisionで、feature bytes /
  legal mask bytes / row digestが一致することをtestで固定する
- raw sidecar（concealed handを含む）はGitへcommitしない。Gitへ置くのは
  schema / code / tests / aggregate report / digestsだけである

## Minimal privileged sidecar

1 sidecar = 1 immutable directory。既存pathは上書きしない。

```text
<sidecar>/
    manifest.json   canonical JSON identity / provenance / totals / digests
    cells.jsonl     1行 = 1 opponent-target cell
```

cellが持つのは#258が要求する最小contractだけである。

```text
schema / version identity
row identity (source identity / seed / step / decision ordinal / actor seat)
public row digest
opponent canonical seat identity (相対 offset / absolute seat / seat wind)
opponent concealed tiles
opponent public melds
opponent public RiichiState + privileged riichi binding
target availability + reason code
source revision / rules provenance
```

意図的に保持しないもの: wall order、future action、future draw、final result、
Oracle Guiding用のfull hidden state、furiten reconstruction state、
Ron counterfactual branch state、future reward。

同じsidecar bytesとbound canonical implementationから `W[j, t]` と
`T[j] = OR_t W[j, t]` をdeterministicに再計算でき、storedな値と一致しなければ
fail closedする。

## Route A — retained #140/#190 corpus exact augmentation

第一優先はretained first-party flat-BC corpusのexact augmentationである。
`RETAINED AUGMENTATION QUALIFIED` とできるのは次がすべてexactに一致した場合だけ
である。

```text
same dataset identity        strict readback した manifest の dataset_identity
same source-semantic         SOURCE_SEMANTIC_PROVENANCE_FIELDS の完全一致
  provenance
same decision identity       (seed, step_ordinal, decision_ordinal)
same acting seat             actor_seat
same round identity          round_wind / hand_number / honba / round_ordinal
same PolicyInput semantics   byte-identical 8204 float32 feature row
same legal-action context    byte-identical 802 legal mask + legal_action_count
same teacher action          behavior action index
same logical decision state  same-state binding（上記）が成立する
```

`round_ordinal` は artifact が保持するderived valueなので、re-executionからも
`RoundOrdinals` の同じcontiguous grouping ruleで再導出して比較する。

1つでも一致しなければ `RETAINED AUGMENTATION NOT QUALIFIED` とし、heuristic
replay fillingで埋めない。retained rowを黙って再生成して「retained dataset」と
呼ぶこともしない。

### 2種類のprovenanceを区別する

| class | fields | 扱い |
| --- | --- | --- |
| source-semantic | `execution_environment`, `lisjong_version`, `lisjong_revision`, `lisjong_engine_version`, `lisjong_engine_revision`, `riichienv_version`, `python_version` | **完全一致を要求**。1 fieldでも違えば `provenance-revision-mismatch` でfail closed |
| instrumentation | `lisjong_arena_version`, `lisjong_arena_revision` | 一致を**要求しない**。両側を記録し、exact row alignmentで正当化する |

source-semantic provenanceは、retained rowのpublic semanticsとhidden stateの
exactnessを決めるdependency identityであり、operatorがhistorical execution
environmentを再現すれば一致させられる。current dependency driftを無視して
QUALIFIEDにはしない。

instrumentation provenance（Arena revision）を一致必須にできないのは、#258の
observer / qualification implementationがretained corpusを生成した
historical Arena revisionには存在しないためである。それを一致必須にすると、
exact row alignmentを一度も実証しないまま、instrumentation revisionが違うと
いう理由だけでretained routeが恒久的にNOT QUALIFIEDになる。代わりに、
decision identity / actor seat / round identity / feature bytes / legal mask
bytes / teacher action / same-state bindingのexact alignmentが、
instrumentationを含むreplayでも同じpublic row semanticsを再現したことを実証
する。両側のArena revisionはqualification resultとfeasibility artifactへ
記録する。

provenance documentのfield集合は`SOURCE_SEMANTIC_PROVENANCE_FIELDS`と
`INSTRUMENTATION_PROVENANCE_FIELDS`の和と一致しなければならない。未分類の
fieldはsilentに無視せずfail closedする。

feature schema fingerprintとaction vocabulary fingerprintは `load_dataset()`
がinstalled contractに対して既にfail closedで検証しており、重複検証しない。

### rejection は 2 class に分ける

retained routeの`NOT QUALIFIED`は、すべてが同じ意味ではない。

| class | reasons | fresh fallback |
| --- | --- | --- |
| `qualification-precondition-not-met` | `retained-artifact-unreadable`, `dataset-identity-mismatch`, `provenance-revision-mismatch`, `retained-row-missing` | **authorizeしない** |
| `exact-alignment-disqualified` | `decision-identity-mismatch`, `round-identity-mismatch`, `feature-row-mismatch`, `legal-mask-mismatch`, `teacher-action-mismatch`, `same-state-co-emission-failed` | authorizeする |

precondition classは、exact row alignmentを**正しい条件下でまだ試せていない**
状態である。特にsource-semantic provenance mismatchのままfresh fallbackへ
進めてしまうと、historical execution environmentを再現しないまま経路を
切り替えることになる。したがってこれらはoperator actionが残るpending state
として扱い、fresh fallbackをauthorizeしない。

`retained-row-missing`はそのseedのreplayを実行する前の判定なので、保守側
（authorizeしない）へ寄せてpreconditionに分類する。

分類のないrejection reasonはfail closedする。

### operatorに必要な追加step

retained corpus `69094c1b…` のrecorded source-semantic provenanceは、現在の
Arena pinとは一致しない。したがってretained routeを実測するには、historical
execution environmentを再現したうえでqualification CLIを実行する必要がある。

```text
lisjong          a0666d24e66179a45fd6e231a3cbd489b492d162
lisjong-engine   8735e89e1aea000ab59368d0368d476787827741
RiichiEnv        0.4.8
Python           retained manifest の python_version
execution env    retained manifest の execution_environment
```

Arena自身は#258 instrumentationを含むcurrent revisionのままでよい（それが
instrumentation provenanceである）。再現できないdependencyがある場合は、
`provenance-revision-mismatch` としてfail closedし続ける。

exposure boundary: このpathはTRAIN-side seed（245..264）しか実行・参照しない。
VALIDATION（265..270）とprotected TEST（271..276）のfeature rowは読まず、その
target behaviorも要約しない。

## Route B — fresh live-label fallback

retained augmentationが資格化できない場合にだけ、decision timeに

```text
public / player-safe current flat-BC row
+
training-only privileged Tenpai annotation
```

を同一row identityでco-emitできることを、boundedなtechnical smokeで確認する。

このpathはlisjong-engine execution seamではなく、retained flat-BCと同じ
RiichiEnv実行境界（`LocalGameRunner` + `yakuhai-call x4` + `4p-red-half`）を
使う。したがってPolicyInput semantics、8204 feature、legal actions、802 legal
mask、teacher action、game / rule semanticsはcurrent flat-BC contractそのもの
である。

technical smoke population（実行前にlock済み）:

```text
seeds       751, 752
identity    arena-stage-a0-tenpai-feasibility-smoke-751-752
role        DEVELOPMENT-ONLY TECHNICAL SMOKE
```

`100..750`はrepository内の既存populationが取得済みであり、`751..752`はその直後の
fresh contiguous rangeである。label prevalenceやmodel behaviorを見て選んでいない。
scientific evidenceへ再利用しない。full Stage A0 corpusは生成しない。

## Feasibility report / evidence chain

feasibility reportはversioned artifactであり、`report_identity`（自身を除いた
canonical JSONのsha256）を持つ。`load_feasibility_report()`がstrict readerで
あり、次をfail closedで検証する。

```text
exact report schema version
protocol id == #258 protocol
issue identity == lisbun/lisjong-arena#258
expected field set / JSON types
availability counts が全reason codeを網羅し cell count と一致する
hard_outcome / pending_reason の exactly-one invariant
hard outcome enum / retained route outcome enum
retained corpus identity（sha256 digest）
canonical JSON bytes と report_identity の整合
```

違反は `StageA0ReportError` としてfail closedする。

`routes.retained_augmentation` objectもexact field set / JSON typeで
strict validateし、`rejection_class` がその `rejection_reason` の分類と一致
することを要求する。さらに、top-levelの `sources.retained_corpus_identity`、
`routes.retained_augmentation.dataset_identity`、
`sources.retained_evidence.retained_corpus_identity` は、存在するものが
互いに一致しなければfail closedする。

fresh fallbackが `FRESH LIVE-LABEL PATH QUALIFIED` を作れるのは、strict reader
を通したうえで次をすべて満たすreportからだけである。

```text
retained route outcome == RETAINED AUGMENTATION NOT QUALIFIED
rejection class        == exact-alignment-disqualified
retained corpus        == RETAINED_DATASET_IDENTITY（#258 が対象とする corpus）
report 自身が hard outcome を主張していない
```

stale / malformed / unrelated / tampered / 別corpus / precondition failure /
既にhard outcomeを持つreportはすべてfail closedし、fresh fallbackとして扱わ
ない。retained reportを渡さずにfresh smokeを実行した場合は、hard outcomeを
作らず `pending_reason` のままにする。

final fresh reportには、入力したretained qualification reportの
`report_identity` / retained corpus identity / retained outcomeを
`sources.retained_evidence` として記録し、evidence chainを追跡可能にする。

## Unexpected exception は route outcome へ丸めない

qualification pathは、expectedなfailureとして明示的に分類できる例外だけを
catchする。

| path | catchする例外 | 意味 |
| --- | --- | --- |
| retained: artifact読み込み | `OfflineQError` / `OSError` / `ValueError` | `retained-artifact-unreadable`（precondition） |
| retained / fresh: co-emission | `StageA0AlignmentError` | same-state binding不成立 |
| CLI: deterministic recomputation | `StageA0SidecarError` | recomputation不一致 |
| labels: canonical builder | `ValueError` | `OTHER_FAIL_CLOSED`（countable） |

それ以外の例外（programming error / infrastructure error / canonical builderの
`RuntimeError`等）はcatchせずそのまま伝播させ、CLIをhard failさせる。
`RETAINED AUGMENTATION NOT QUALIFIED` / `FRESH LIVE-LABEL PATH NOT QUALIFIED` /
`TENPAI LABEL PATH BLOCKED` のいずれへも変換しない。broadな
`except Exception`はこのpackage内で使わない。

## Hard outcomes

#258が許すfinal route outcomeは次の4つだけである。

```text
RETAINED AUGMENTATION QUALIFIED
FRESH LIVE-LABEL PATH QUALIFIED
TENPAI LABEL PATH BLOCKED
STOP / INVALID
```

feasibility artifactは、まだ実測できていないhard outcomeを作らない。
`hard_outcome`と`pending_reason`はexactly oneだけが設定される。

### routing

`QualificationCheck`はどれも「routeが利用可能か」ではなく「evidenceと
internal invariantが壊れていないか」を表す。したがってcheck failureは
route unavailableではなくprotocol / evidence violationであり、必ず
`STOP / INVALID`へ写す。fresh fallbackの理由にも
`TENPAI LABEL PATH BLOCKED`にも変換しない。

```text
retained qualification precondition failure
    -> pending / operator action（hard outcomeにしない）

retained expected exact-alignment disqualification
    -> pending、fresh fallback eligible

retained exact alignment QUALIFIED + 全 check pass
    -> RETAINED AUGMENTATION QUALIFIED

retained exact alignment QUALIFIED + いずれかの check fail
    -> STOP / INVALID

fresh emission QUALIFIED + 全 check pass + valid retained evidence
    -> FRESH LIVE-LABEL PATH QUALIFIED

fresh emission QUALIFIED + 全 check pass + retained evidence なし
    -> pending（route選択はまだできない）

fresh expected same-state / path disqualification
    + valid retained exact-alignment disqualification
    -> TENPAI LABEL PATH BLOCKED

fresh emission は成立したが invariant / evidence / check が fail
    -> STOP / INVALID
```

`STOP / INVALID`へ写る具体例は、deterministic recomputation failure、
public/private boundary failure、seat mapping inconsistency、
`OTHER_FAIL_CLOSED > 0`、stable-13 / physical-inventory invariant failureである。

## Operator commands

```text
python -m lisjong_arena.stage_a0_tenpai_feasibility retained-qualify \
    --dataset <retained #140/#190 corpus> \
    --sidecar <scratch dir>/sidecar \
    --report  <scratch dir>/retained-report.json

python -m lisjong_arena.stage_a0_tenpai_feasibility fresh-smoke \
    --retained-report <scratch dir>/retained-report.json \
    --sidecar <scratch dir>/fresh-sidecar \
    --report  <scratch dir>/fresh-report.json

python -m lisjong_arena.stage_a0_tenpai_feasibility recompute \
    --sidecar <scratch dir>/sidecar
```

`--sidecar`と`--report`の出力先はrepository外のscratch領域を使う。
sidecarはconcealed handを含むためGitへcommitしない。

provenanceはclean merged mainのgit-installed packageから収集するため、
uncommitted changeやVCS metadataのないinstallではfail closedする。

## Limitations

- feasibility artifactのcoverage数値はmodel-quality resultではない
- このpathはStage A0のtraining / protocol lockを承認しない。承認するのは
  次のchild（Stage A0 protocol lock / power preflight）へ進むことだけである
