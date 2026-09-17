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
- `OTHER_FAIL_CLOSED`はprechecks通過後のcanonical builder failureという
  unexpected caseだけに割り当てる。countableであり、1件でもあればqualificationを
  通さない
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
same provenance / revision   bound Arena / lisjong / engine / RiichiEnv / Python
same decision identity       (seed, step_ordinal, decision_ordinal)
same acting seat             actor_seat
same PolicyInput semantics   byte-identical 8204 float32 feature row
same legal-action context    byte-identical 802 legal mask
same teacher action          behavior action index
same logical decision state  same-state binding（上記）が成立する
```

1つでも一致しなければ `RETAINED AUGMENTATION NOT QUALIFIED` とし、heuristic
replay fillingで埋めない。retained rowを黙って再生成して「retained dataset」と
呼ぶこともしない。

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
