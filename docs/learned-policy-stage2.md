# Learned Policy Stage 2 — minimal behavior-cloning vertical slice

本書は`lisjong_arena.learned_policy_stage2`が実装するbounded experimentの契約を示す。
実験そのものの目的・acceptance criteria・結果は
[lisjong-arena #133](https://github.com/lisbun/lisjong-arena/issues/133)と
親roadmap [lisjong-project #40](https://github.com/lisbun/lisjong-project/issues/40)を正本とする。

## Scope

このpackageはStage 2 experiment 1本のためのexperiment-local harnessである。

```text
first-party teacher decision (yakuhai-call x4, 4p-red-half)
    -> actual DecisionContext
    -> arena-policy-input-feature-v1  (8204 float32)
    -> lisjong-action-vocabulary-1    (802 legal mask)
    -> versioned dataset artifact + whole-hanchan split
    -> fixed 1x128 MLP + masked cross-entropy
    -> frozen checkpoint
    -> one-shot TEST evaluation
```

次は導入しない。

- generic ML framework / generic trainer abstraction / generic dataset framework
- model registry / database / dashboard / generic artifact platform
- production Learned Policy class、serving Policy adapter（Stage 3のscope）
- HPO、architecture search、class weight、oversampling、label smoothing、
  dataset由来のmean/std normalization

`torch`はtraining / evaluation pathだけのlazy importである。dataset生成、artifact
readback、coverage集計はML runtimeを要求しない。

## Locked protocol

`protocol.py`がlocked valueをcodeとして固定する。結果を見てここを変更しない。

| 項目 | Locked value |
| --- | --- |
| protocol ID | `arena-learned-policy-stage2-v1` |
| teacher identity | `yakuhai-call` |
| teacher class | `YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy` |
| population | `yakuhai-call x4`（game / seatごとにfresh instance） |
| teacher source revision | lisjong `a0666d24e66179a45fd6e231a3cbd489b492d162` |
| game mode | `4p-red-half` |
| ordered seeds | `200..215`（16 hanchan） |
| split unit | whole hanchan |
| TRAIN / VALIDATION / TEST | `200..209` / `210..212` / `213..215` |
| feature semantics / tensor schema | `arena-policy-input-feature-v1` / `arena-policy-input-tensor-v1` |
| feature dimension / fingerprint | `8204` / `097dd99f…0abd0ed30` |
| action vocabulary / size / fingerprint | `lisjong-action-vocabulary-1` / `802` / `543c6bca…302d7952` |
| model | `Linear(8204,128) + ReLU + Linear(128,802)`、parameter count `1,153,698` |
| loss / optimizer | masked cross-entropy / Adam `lr=1e-3`, `weight_decay=0` |
| batch / epochs / patience | `256` / `20` / `4` |
| seeds / workers / threads | training `0`, dataloader `0`, workers `0`, torch threads `1` |
| checkpoint selection | lowest VALIDATION choice-row masked CE |

`verify_contract_identity()`は、installedのfeature schema fingerprintと
action vocabulary fingerprintを再計算し、上記locked valueと一致しない場合に
`Stage2ContractIdentityError`でfail closedする。vocabulary fingerprintは
`lisjong`のpublic `decode_action()`から
[`lisjong/docs/action-vocabulary.md`](https://github.com/lisbun/lisjong/blob/main/docs/action-vocabulary.md)
記載のcanonical semantics行を再構成してsha256したものであり、codec内部のkey表現には依存しない。

## Recording seam

`recording.py`は既存のRiichiEnv実行境界をそのまま使う。`LocalGameRunner`、
`GameTrace`、`LocalGameInspectionRecorder`は変更しない。

```text
LocalGameRunner(inspection_recorder=...)
    -> execute_policy_with_trace()      # execute_policy() と同じvalidation
    -> PolicyInput + DecisionTrace
    -> DecisionContext(input, legal_actions)
    -> build_policy_input_feature() -> tensor_values()
    -> build_legal_action_mask() / encode_action() / resolve_legal_action()
```

- teacher actionは必ずvalidated `InternalAction`から取得する。dataset生成のために
  `Policy.choose_action()`を直接呼び出してvalidationを迂回しない
- `DecisionTrace.analysis`は読まず、rowへも保存しない。objective execution
  observationへteacher-internal analysis（shanten、ukeire、danger、候補評価、選択理由）を
  混ぜない
- canonical順は`step_ordinal`昇順、step内は`actor_seat`昇順で固定する
- round identityは`PolicyInput.round`の`(round_wind, hand_number, honba)`という
  player-safeな値から導出する

## Dataset artifact

1 dataset = 1 immutable directoryとし、既存pathを上書きしない。

```text
<dataset>/
    manifest.json      canonical JSON identity / protocol / provenance / digests
    rows.jsonl         1行 = 1 decisionのplayer-safe metadata
    features.f32       N x 8204 little-endian float32 (row-major)
    legal_mask.u8      N x 802 uint8 (0 / 1) の fixed-size legal mask
```

dense featureとfixed legal maskをJSONへ展開しないのは容量のためだけであり、契約は
変わらない。`manifest.json`がdimension、row count、byte count、sha256を保持し、
readbackがそのすべてを照合する。

manifestは少なくとも次をbindする。

- source game identity（seed / game mode / scores / ranks / step count / round count）
- split membership（whole hanchan）
- Arena / lisjong / lisjong-engine revision、RiichiEnv version、Python version
  （既存の`collect_execution_provenance()`を再利用する）
- teacher Policy identity / class / population / source revision
- feature semantics ID、tensor schema version、dtype、dimension、schema fingerprint
- action vocabulary version、size、fingerprint
- `dataset_identity` = `dataset_identity`自身を除いたcanonical manifestのsha256

`collect_execution_provenance()`はsource treeがdirtyな場合にfail closedする。
そのためdataset生成はcommit後に実行する。`Stage2DatasetWriter(..., provenance=...)`の
明示指定はfixture / testのためだけの入口である。

### Hard invariants

writeとreadの双方でfail closedに検証する。

- feature dimension `== 8204`、legal mask dimension `== 802`
- non-finite feature `== 0`
- teacher action index is legal `== 100%`
- same-context encode / resolve round trip `== 100%`
- decision ordinalはgameごとに0起点で連続、rowはseed昇順にgroup化
- seed populationは`200..215`と完全一致し、splitはwhole hanchanで交差しない
- schema / vocabulary / manifest / digest mismatchはsilent fallbackせずfail closed

hidden opponent hand、wall truth、future state、future outcome、oracle informationは
rowへ入れない。featureはStage 1 encoderの出力をそのまま保持し、Stage 2側で再定義・
再計算しない。

## Metrics

forced decision（`len(legal_actions) == 1`）はmodel qualityを人工的に高く見せるため、
primary metricから分離する。

```text
primary    choice rows (len(legal_actions) >= 2)
             masked cross-entropy
             exact action agreement
             top-3 / top-5 agreement among legal actions
             conditional-uniform legal baseline
               CE reference        mean(log(number_of_legal_actions))
               agreement reference mean(1 / number_of_legal_actions)
             per-hanchan metrics
secondary  all-row masked CE / exact agreement
             forced-row count / share
             action-family agreement
             frequent / rare selected-index diagnostics
             TRAIN / VALIDATION / TEST gap
```

frequent / rare bucketはTRAIN頻度だけから決める（TESTを見て決めない）。

```text
teacher agreement
    != decision quality
    != game strength
```

## Safety checks

`serving_check.py`はTEST hanchanをlocked seedで再実行し、各decisionで
encode -> inference -> mask -> resolveのfull pathを通す。model出力はgameへ適用せず、
teacher x4のexecutionをそのまま観測する（学習modelがgameを駆動しない）。

- teacher label legal-mask membership
- masked argmax illegal selection
- `resolve_legal_action()` failure
- 再実行featureとfrozen dataset rowのbit一致、legal mask / teacher indexの一致
- same tensor + same weights -> same logits / same selected index

## Decision rule

```text
hard gate failed
    -> STOP / INVALID
no evaluable choice rows
    -> DATA COVERAGE INSUFFICIENT
TEST choice-row masked CE < conditional-uniform legal baseline
    -> VERTICAL SLICE VIABLE
otherwise
    -> MODEL CAPACITY INSUFFICIENT
```

`REPRESENTATION REFORMULATE`と`TEACHER COST TOO HIGH`はmeasurementだけでは自動
判定できないjudgement outcomeであり、機械的には発行しない。該当する場合はIssue上の
result recordで明示する。

model-learning gateはstrength claimではなく、
«このrepresentation / dataset / fixed modelでteacher action signalを学習できるか»
のfeasibility判定である。

## Running the experiment

generated dataset、trained weights、result artifactはrepository外へ出力し、Gitへ
commitしない。

```bash
python -m lisjong_arena.learned_policy_stage2 generate \
    --dataset /path/outside/git/dataset \
    --report  /path/outside/git/generation.json

python -m lisjong_arena.learned_policy_stage2 train \
    --dataset    /path/outside/git/dataset \
    --checkpoint /path/outside/git/checkpoint

python -m lisjong_arena.learned_policy_stage2 test \
    --dataset    /path/outside/git/dataset \
    --checkpoint /path/outside/git/checkpoint \
    --result     /path/outside/git/test-result.json
```

`generate`と`train`はTEST partitionのmetricを一切計算しない。`test`だけがfrozen
checkpointに対してTESTを1回評価する。`test`を再実行するとTEST one-shot disciplineが
破れるため、結果を見てからのcheckpoint / config変更は行わない。

`train`はTEST exposure前にcheckpoint identityと`weights_sha256`を出力する。この2値を
result recordへ残してからTESTを評価する。
