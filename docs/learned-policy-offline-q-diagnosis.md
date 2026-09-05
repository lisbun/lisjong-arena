# Offline Q failure diagnosis — artifact-only bounded diagnostic (Issue #152)

`lisbun/lisjong-arena #152`は、`#140`が到達した

```text
FINAL OUTCOME
VALUE/Q OBJECTIVE NEGATIVE

mean candidate game delta  -488.67
95% interval               [-844.19, -133.15]
```

について、**新しい学習・game生成・strength evidenceを作らず**、`#140`が
retainしたartifactだけでfailure mechanismをboundedに切り分けるためのdiagnostic
machineryを定義する。個別runのnumeric resultとexhaustive outcomeはIssue #152を
正本とし、この文書はcontractだけを持つ。

`#140` protocolそのもの（seed population、reward、gamma、target cadence、
support rule、model capacity、serving semantics）はこの文書では変更しない。
`docs/learned-policy-offline-q.md`が引き続き正本である。

## 目的

`#140`のcandidate-only Mahjong diagnosticsは

```text
tenpai reached          2 / 100
mean first tenpai turn  19.0
```

という記述的signalを残した。これは**baselineとの差ではなくQ hybrid自身の値**で
あり、原因の特定でもない。`#152`の目的は「次に変更すべきaxisを1つに絞れるだけの
failure-mechanism evidenceを得ること」であり、次のalgorithmを選ぶことではない。

## Artifact-only boundary

このdiagnosticが行わないことを機械的に固定する。

```text
new hanchan generation / new seed allocation
retraining / replacement TEST regeneration
new strength evidence / new TEST exposure
reward / gamma / target cadence / support rule変更
CQL / AWR / ResNet / model width / feature expansion
HandBelief integration / 外部data追加 / production Policy変更
```

`diagnose` CLIはretained artifactのstrict readbackだけを入力に取り、
generation pathを一切呼ばない。artifactがoperator環境に無い状況は
`DIAGNOSTIC EVIDENCE INSUFFICIENT`ではなく、単に**real diagnostic execution
未実施**である。

## Source artifact identities

`#152`がsource of truthとしてlockした`#140` retained artifactは
`diagnosis.LOCKED_SOURCE_IDENTITIES`に固定されている。

```text
dataset                   69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4
BC checkpoint             17a31fc8aa0edcdd3834da7075abe37bd9554d47f4efe94afb31050bad20ac3b
Q checkpoint              31545d6bde3da4fd7ee6152bf3183e5be82302d8a5cee70ccf35923781382b94
replacement TEST artifact fe7a4455b775cbc23568b0d9c7489593c0859bce28e0529e3e400a816cf7fccd
supported_indices_digest  230b2f07dc95d169ebfb85b9deb6174f22909b52025979ae64da541bd9481d9e
```

```text
retention backend  operator-local-durable
retention key      offlineq-140-rebuild/candidate-pair
```

`bind_diagnosis_inputs()`は次をfail closedで確認する。

- BC / Q checkpointの`dataset_identity`が一致し、渡されたdatasetと同一であること
- dataset / BC / Q / replacement TESTのfeature identityとvocabulary identityが
  locked値と一致すること
- Q checkpointの`supported_indices`がその`supported_indices_digest`と一致すること
- 4 artifactのidentityが上記lock値と一致すること

いずれか1つでも一致しなければ分析へ進まない。

## Eligible rows

Measurement A-Dの母数は、serving hybridがlearned modelを実際に使う条件と同一で
ある（`HybridPolicy.choose_action()`と同じ判定）。

```text
legal action count >= 2                     forced decisionを除外
全legal actionがordinary discard blockに属する  riichi / call / agari等を除外
全legal actionがTRAIN-supported              support fallback rowを除外
```

除外したrow数は`row_counts`へ残り、母数を後から動かせないようにしている。

## Roles

TRAINとTESTのroleは分離し、混ぜない。

| role | source artifact | seeds | 位置づけ |
|---|---|---|---|
| `dataset-train` | dataset | `245..264` | behavior distribution。generalization evidenceではない |
| `dataset-validation` | dataset | `265..270` | 同上 |
| `dataset-test` | dataset | `271..276` | `#140`以前にexposure済み。新しいTEST claimにしない |
| `replacement-test` | replacement TEST | `354..359` | `#140`で一度だけexposureしたoffline diagnostic population |

どのroleも`is_generalization_evidence: false`であり、このdiagnosticは
新しいstrength / TEST claimを構成しない。

## Measurement A — same-state disagreement

同じplayer-safe stateとsame legal maskへBC / Qを適用し、top-1選択を比較する。

```text
Q vs BC / Q vs behavior / BC vs behavior のdisagreement count / rate
stratification: legal action count / terminal・nonterminal / round / decision depth
```

stratification bandは`LEGAL_ACTION_COUNT_BUCKETS`と
`DECISION_DEPTH_BAND_EDGES`としてcodeに固定してあり、結果を見てから変更しない。

## Measurement B — Q ranking stability

各eligible rowについて次を求め、固定quantile
（`0 / 0.05 / 0.25 / 0.5 / 0.75 / 0.95 / 1`）でsummaryする。

```text
Q top-1 value / Q top-2 value / Q margin = top1 - top2
Q value of BC-selected action / Q value of behavior action
Q-selected vs BC-selected gap / Q-selected vs behavior gap
```

summaryは`all eligible rows` / `Q・BC agree rows` / `Q・BC disagree rows`の
3 scopeで並べる。threshold判定は行わない（「安定 / 不安定」の線引きを数値を見て
から作らない）。

## Measurement C — reward / bootstrap structure

```text
immediate reward           terminal / nonterminal / agree / disagree
TD target                  all / agree / disagree / terminal / nonterminal
predicted selected-Q       同上
absolute Bellman residual  同上
```

TD targetは既存の`compute_td_targets()`（gamma = 1.0、support-restricted
bootstrap）をそのまま使う。**retained artifactはfitted-Q最終iterationのonline
networkだけを含み、そのepochのtarget network snapshotを含まない。** したがって
このdiagnosticのTD targetは`final_q_checkpoint_as_its_own_target`であり、
training中に実際に使われたtargetの再現ではない。TD target / residualは
next legal actionがすべてTRAIN-supportedなrow（terminal rowを含む）でだけ
定義し、それ以外は`unsupported_bootstrap_row_count`として数える。

これはdescriptive diagnosticであり、causal proofではない。

## Measurement D — hand progression

**player-safeかつ一意に導出できる範囲だけ**実施する。

```text
feature row own_hand.tile_counts   自seatのconcealed hand（count / 4.0のexact float32）
    + ordinary discard vocabulary index
        -> lisjong.hand_evaluation.calculate_shanten()
```

読むのは自seatの`own_hand`グループだけであり、opponent hand、wall、future
state、teacher-internal analysisは読まない。向聴semanticsはlisjongの公開契約が
所有し、Arenaは`Tile`列を渡すだけである。

出力は Q / BC / behavior それぞれの

```text
post-discard shanten の固定summary
keep-shanten / worsen-shanten count / rate
Q vs BC / Q vs behavior の lower / equal / higher count と worsening rate差
```

である。

### 一意に復元できない場合

次のいずれかに当たるrowが1つでもあれば、推測で埋めずそのroleの
Measurement Dを`UNAVAILABLE`にする。

- `own_hand.tile_counts`が整数枚数へ戻らない、または0..4の範囲外
- 復元したconcealed hand枚数が`calculate_shanten()`の有効枚数でない
- legalなdiscard候補牌が復元した手牌に存在しない
- 打牌後向聴数が打牌前より小さい（構造上あり得ない）

**Measurement Dが`UNAVAILABLE`でもMeasurement A-Cは成立する。**

### ukeireは`UNAVAILABLE`

既存のukeire semanticsは`lisjong.policies.ukeire.UkeirePolicy`が
`PolicyInput`束縛のprivate helperとして所有しており、Arenaから一意に再利用できる
公開契約が存在しない。Arena側で同等物を書き直すことは「既存semanticsと異なる
ukeire定義」の新規導入になるため行わず、常に`UNAVAILABLE`として理由を記録する。

## Measurement E — retained strength context

`#140`のcanonical strength resultは再計算・再samplingせず、
`retained_strength_context`としてcontextだけを持つ。candidate-only Mahjong
metricsは`is_baseline_difference: false`でschema上明示し、validatorがそれを
強制する。

## Result artifact

`diagnose`は小さなJSON documentを1つ書く。

```text
diagnosis_schema_version / diagnosis_id / source issue / protocol id
input_artifact_identities（+ real_artifact_execution）
locked_source_identities / feature / vocabulary / fixed_quantiles
roles[]
    role / source_artifact / split / is_generalization_evidence
    row_counts
    measurement_a / measurement_b / measurement_c / measurement_d
retained_strength_context
limitations
classification   （常にnullで作られる）
```

`validate_diagnosis_result()`はfield集合、role重複、
`rate = count / row_count`の再導出、stratificationがeligible rowsを分割すること、
terminal / bootstrap countsの分割、Measurement Dのstatus整合、
candidate-only labelを検証する。

Generated dataset / weights / result artifactはGitへcommitしない。

## Interpretation ladder

`#152`が result exposure前に固定したexhaustive outcomeは`DiagnosisOutcome`に
そのまま入っている。

```text
HAND-PROGRESSION DEGRADATION IDENTIFIED
Q-RANKING INSTABILITY IDENTIFIED
FAILURE MECHANISM INCONCLUSIVE
DIAGNOSTIC EVIDENCE INSUFFICIENT
STOP / INVALID
```

このladderは「明確なdescriptive patternがあるか」というqualitative judgementで
あり、**codeは数値からoutcomeを自動生成しない**。閾値を後付けで発明しないことが
`#152`の明示要件だからである。`record_classification()`が機械的に強制するのは
次の3点だけである。

1. outcomeがexhaustive集合に属すること
2. 実artifactをstrict readbackした実行結果にだけ付与できること
3. 一度記録したoutcomeを上書きできないこと

## Limitations

- Measurement A-Cはoffline behavior-distribution states上のdiagnosticであり、
  Q policy自身が作るrollout distributionを直接観測しない。
  `ROLLOUT DISTRIBUTION SHIFT PROVEN`とはしない
- offline disagreementはstrength regressionのcausal proofではない
- `#140`のcandidate-only Mahjong metricsはbaselineとの差ではない
- TD targetは最終checkpointを自分自身のtargetとした評価であり、training中の
  targetの再現ではない
- Measurement Dは自seat concealed handだけを復元する。ukeireは導出しない
- TRAIN / VALIDATION上のagreementはgeneralization evidenceではない

## Local execution

retained artifactはoperator-local durable storageにあり、CI / online環境からは
アクセスできない。実行はoperator環境で行う。

```powershell
$Artifacts = "C:\Dev\lisjong-artifacts"

python -m lisjong_arena.learned_policy_offline_q diagnose `
    --bundle           "$Artifacts\offlineq-140-rebuild\candidate-pair" `
    --dataset          "<retained dataset directory>" `
    --replacement-test "<retained replacement TEST directory>" `
    --result           "$Artifacts\offlineq-152-diagnosis\diagnosis.json"
```

`--dataset`と`--replacement-test`はbundleと同じ場所に無くてよく、operator pathを
codeへhard-codeしない。`--result`は既存fileを上書きしない。

期待される出力:

```text
dataset-train:        eligible=<n>/<n> q_vs_bc=<n> q_vs_behavior=<n> bc_vs_behavior=<n> measurement_d=<AVAILABLE|UNAVAILABLE>
dataset-validation:   ...
dataset-test:         ...
replacement-test:     ...
classification=None
```

identity binding、feature / vocabulary identity、supported_indices digestの
いずれかが合わなければ、この時点でfail closedして何も書かない。

result artifactをreviewしたうえで、exhaustive outcomeを1件だけ記録する。

```powershell
python -m lisjong_arena.learned_policy_offline_q record-classification `
    --result            "$Artifacts\offlineq-152-diagnosis\diagnosis.json" `
    --classified-result "$Artifacts\offlineq-152-diagnosis\diagnosis-classified.json" `
    --outcome           <HAND_PROGRESSION_DEGRADATION_IDENTIFIED|Q_RANKING_INSTABILITY_IDENTIFIED|FAILURE_MECHANISM_INCONCLUSIVE|DIAGNOSTIC_EVIDENCE_INSUFFICIENT|STOP_INVALID>
```

**final classificationはこのlocal executionの後にだけ行う。** result artifactを
取得していない状態でoutcomeを記録することはできない。
