# P1 Gate A — keep-shanten discard feature bounded evaluation (Issue #158)

`lisbun/lisjong-arena #158`は、`#152`が到達した

```text
FINAL OUTCOME
HAND-PROGRESSION DEGRADATION IDENTIFIED
```

に対して、**deterministic Mahjong featureをちょうど1 family**だけcurrent simple
Offline Qへ追加したときにhand-progression degradationが改善するかを、`#140`が
retainしたartifactだけでboundedに検証するmachineryを定義する。個別runのnumeric
resultとexhaustive outcomeはIssue #158を正本とし、この文書はcontractだけを持つ。

`#140` protocolそのもの（seed population、reward、gamma、target cadence、
support rule、model hidden width、serving semantics）はこの文書では変更しない。
`docs/learned-policy-offline-q.md`が引き続き正本であり、`#152`の診断contractは
`docs/learned-policy-offline-q-diagnosis.md`が持つ。

## Primary changed axis

変更してよいaxisは1つだけである。

```text
CHANGE
  deterministic keep-shanten discard 37-tile feature family
  （およびその不可避なfirst-layer parameter増加とinput dimension）

KEEP
  retained transition rows / TRAIN / VALIDATION / TEST membership / row order /
  row identity / behavior action / reward / terminal / legal mask /
  action vocabulary / reward semantics / gamma / target update cadence /
  support restriction semantics / Huber loss / optimizer / learning rate /
  batch size / hidden width 128 / ReLU / maximum epochs / training seed /
  dataloader seed / Torch deterministic settings /
  fixed_final_iteration checkpoint selection / evaluation rows
```

`GENERATION_BUDGET`はこのIssueのcost guardrailをresult schemaへ固定する。

```text
new game generation  0
new seed allocation  0
new hanchan          0
new TEST exposure    0
```

result validatorはこの4つが0でないdocumentを拒否する。

## Source artifact identities

`#152`と同じ4 artifactを対象にするため、strict retained-artifact bindingは
再実装せず`diagnosis.bind_diagnosis_inputs()`をそのまま共有する
（`p1_gate_a.bind_gate_a_inputs()`）。

```text
dataset                   69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4
BC checkpoint             17a31fc8aa0edcdd3834da7075abe37bd9554d47f4efe94afb31050bad20ac3b
retained Q-v1 checkpoint  31545d6bde3da4fd7ee6152bf3183e5be82302d8a5cee70ccf35923781382b94
replacement TEST artifact fe7a4455b775cbc23568b0d9c7489593c0859bce28e0529e3e400a816cf7fccd
supported_indices_digest  230b2f07dc95d169ebfb85b9deb6174f22909b52025979ae64da541bd9481d9e
```

exact artifactが利用不能な場合、regenerateもreplacement seed払い出しも行わない。
その状況は`P1 EVIDENCE INSUFFICIENT`であり、実行しないだけである。

## P1 derived representation

現行v1 (`arena-policy-input-feature-v1` / 8204 /
`097dd99f...abd0ed30`) を**breaking changeしない**。P1はexperiment-localな
派生representationとして別のidentityを持つ。

```text
derived feature semantics id   arena-learned-policy-offlineq-p1-keep-shanten-feature-v1
derived tensor schema version  arena-learned-policy-offlineq-p1-keep-shanten-tensor-v1
dtype                          float32
base feature dimension         8204
appended feature dimension       37
dimension                      8241
schema fingerprint             beae3eb8d3dc79b4f837ec98ed3b90eb7530e6f931102b8cdf4854bcf0a15409
```

```text
index 0 .. 8203     v1の値をverbatimに保持する（derivationはbase valueを書き換えない）
index 8204 .. 8240  keep-shanten discard mask、canonical TILE_AXIS順の37 entry
```

appended descriptorは`p1_keep_shanten_discard.tile[<label>]:binary`であり、
labelはv1 `TILE_AXIS_LABELS`と同一である。赤5と通常5は37軸上で別entryのままで
ある（`5m` と `5m-red` は独立index）。

fingerprintはv1 `learned_policy_input.tensor.schema_fingerprint()`と同じ構成
（`"\n".join(lines) + "\n"`のUTF-8 sha256）で、headerだけをderived schema用に
拡張し、**base v1 fingerprintを取り込む**。したがってbase schemaが変わればP1
fingerprintも必ず変わる。import時に`p1_schema_fingerprint()`とlocked値の一致を
確認し、drift時はfail closedする。

### keep-shanten mask semantics

各tile entryのvalueは次のとおりである。

```text
1.0   own concealed handにそのexact tile identityが1枚以上あり、
      exactly 1枚discardしたpost-discard shantenがpre-discard shantenと等しい
0.0   それ以外（handに無い場合を含む）
```

derivationが読む情報は**自seatの`own_hand`グループだけ**である。

```text
使わない
  legal mask / opponent hidden hand / wall truth / future state / outcome /
  teacher internal analysis
追加しない
  ukeire / waits / value / defense / HandBelief / current-shanten one-hot /
  その他のfeature family
```

向聴semanticsをArenaで再実装しない。`#152`が確立した

```text
locked v1 own_hand feature
  -> reconstruct_concealed_tiles()   exact整数枚数への逆写像 / 曖昧ならfail closed
  -> lisjong.hand_evaluation.calculate_shanten()
```

を`hand_progression.keep_shanten_tile_mask()`としてsingle source of truthのまま
再利用する。赤5と通常5の同一視も確定面子数の判断もlisjongが所有し、Arenaは
37軸上のtile identityを渡すだけである。

### Derived data/view

新しいgame、seed、hanchanを生成しない。locked retained transition rowsから
in-memory derived viewを作るだけであり、次は一切変更しない。

```text
row order / row identity / split membership / behavior action /
reward / terminal / legal mask / next legal mask
```

`next_features`にも同じderivationを適用し、source / nextのschemaを一致させる。
**terminal rowの`next_features`は既存contract上all-zero placeholderであり
（有効性は`terminal`が決める）、P1でも新semanticsを発明せずappended 37 entryも
all-zeroにする。** この規約は`terminal_next_state_padding=all-zero`として
fingerprintへ含める。

`verify_derived_alignment()`はrole評価の前に、derived viewがbase 8204列を
verbatimに保持し、legal mask / behavior / reward / terminalを変えていないこと、
terminal placeholderにderived値が入っていないことをfail closedで確認する。

### 一意に復元できない場合

`own_hand.tile_counts`が整数枚数へ戻らない、0..4の範囲外、復元枚数が
`calculate_shanten()`の有効枚数でない、打牌後向聴数が打牌前より小さい、の
いずれかに当たるrowは推測で埋めず`OfflineQAmbiguousStateError`でfail closedする。
derived coverageは`imputed_row_count: 0`をschema上固定し、validatorが強制する。

coverage不足でprimary comparisonを成立させられない場合は
`P1 EVIDENCE INSUFFICIENT`である。

## P1 Q-v2 training

`#140` / current merged implementationのsimple Offline Q formulationを意図的に
そのまま再利用する。目的はQ formulationを改善することではなく、
**representation 1 familyだけの差**を見ることである。

```text
8241 -> Linear(8241, 128) -> ReLU -> Linear(128, 802)
```

現行`q_training.create_model()` pathは8204入力へ固定されているため、
existing v1 behaviorを変えずに`p1_q_training.create_p1_model()`が
experiment-localな8241入力modelを構築する。training loop自体は
`q_training.train_from_split_tensors()`を`model_factory` seam経由で再利用し、
実装を複製しない。したがってloss、optimizer、learning rate、weight decay、
batch size、gamma、target sync cadence、support restriction、maximum epochs、
training seed、dataloader seed、deterministic settings、checkpoint selectionは
すべて`#140`とliteralに同一である。

`verify_locked_q_protocol_delta()`は、`q_training.locked_model_block()` /
`locked_training_block()`をsource of truthとして、P1 blockとの差が
`input_dimension`（とその帰結として記録するparameter count情報）だけであることを
fail closedで確認する。

### Parameter count

input dimension増加に伴うfirst-layer parameter増加は、feature追加の不可避な
帰結として受け入れる。**hidden widthを縮めてparameter countを合わせない。**

```text
8204-input model   8204*128 + 128 + 128*802 + 802 = 1,153,698
8241-input model   8241*128 + 128 + 128*802 + 802 = 1,158,434
delta                                     37 * 128 =    +4,736
```

### checkpoint selection

`fixed_final_iteration`を維持する。fitted-Qのouter iterationごとにbootstrap
targetそのものが変わるため、VALIDATION lossのepoch間比較でcheckpointを選ばない
（理由は`q_training`のmodule docstringが正本）。

このIssueはP1 Q-v2のcheckpoint artifactを別途永続化しない。result documentが
model block、training block、`weights_digest`（state_dict tensor bytesの
deterministicなsha256）、`selected_epoch`、`epoch_history`、
`supported_indices_digest`を持ち、監査と再導出はそこから行う。

## Gate A evaluation

full strength evaluationへ進まない。roleは`#152`と同じ4つで、**primary roleは
2つだけ**である。

| role | source artifact | seeds | 位置づけ |
|---|---|---|---|
| `dataset-train` | dataset | `245..264` | training distribution diagnostic。primaryではない |
| `dataset-validation` | dataset | `265..270` | 同上 |
| `dataset-test` | dataset | `271..276` | **primary**。`#140` / `#152`でexposure済み |
| `replacement-test` | replacement TEST | `354..359` | **primary**。同上 |

どのroleも`is_generalization_evidence: false`であり、新しいTEST claimを
構成しない。

母数は`#152`と同じeligibility（`select_eligible_rows()`）である。

```text
legal action count >= 2                        forced decisionを除外
全legal actionがordinary discard blockに属する    riichi / call / agari等を除外
全legal actionがTRAIN-supported                  support fallback rowを除外
```

同一row上で4 armを比較する。

```text
q_v2       P1 candidate（8241入力）
q_v1       retained Q-v1 checkpoint（8204入力）
bc         retained BC checkpoint（8204入力）
behavior   記録済みbehavior action
```

### Required metrics

```text
action_agreement
    q_v2 vs q_v1 / q_v2 vs bc / q_v2 vs behavior /
    q_v1 vs bc / q_v1 vs behavior / bc vs behavior の
    disagreement count と rate

hand_progression.arms（4 arm）
    post-discard shanten の固定quantile summary（mean / distribution）
    keep-shanten count / rate
    worsen-shanten count / rate

hand_progression.pairs（5 pair）
    lower / equal / higher post-discard shanten count
    higher rate
    worsening rate差

hand_progression.per_seed
    seed（hanchan）単位のpaired diagnostic
```

per-seed blockは記述的であり、row独立性を仮定したp-value / confidence interval
claimは行わない。

## Result artifact

`p1-gate-a`は小さなJSON documentを1つ書く。

```text
p1_gate_a_schema_version / p1_gate_a_id / source issue / predecessor issues /
parent issue / protocol id / retention
input_artifact_identities（+ real_artifact_execution）/ locked_source_identities
feature（v1）/ derived_feature（P1）/ vocabulary
candidate   model / training / weights_digest / selected_epoch /
            final_validation_huber_loss / epoch_history / supported_indices_digest
changed_axis / generation_budget / fixed_quantiles / primary_roles
roles[]
    role / source_artifact / split / is_primary_role / is_generalization_evidence
    row_counts / derived_coverage
    action_agreement
    hand_progression   status / arms / pairs / per_seed / outcome_conditions
limitations / interpretation_boundary
classification   （常にnullで作られる）
```

`validate_gate_a_result()`は、documentが自己申告するflagやlabelを一切
authorityにせず、次をfail closedで検証する。

```text
schema version / gate A id / source・predecessor・parent issue / protocol id
retention backend・key、v1 feature identity、derived feature identity、
    vocabulary identity、fixed quantile set、primary role集合
changed_axis / generation_budget / limitations / interpretation_boundary が
    locked constant と verbatim 一致

input_artifact_identities の5 identity を LOCKED_SOURCE_IDENTITIES と exact 比較し、
    real_artifact_execution がその比較結果と一致すること

candidate: model block / training block が locked 値、
    epoch_history が maximum_epochs 件、selected_epoch が最終iteration、
    real execution では supported_indices_digest が retained 値と一致

role集合が4件ちょうど、各 role の source artifact / split / primary role /
    generalization semantics が locked 値
row_counts / derived_coverage の分割と imputed_row_count = 0
action_agreement: rate = count / row_count の再導出
hand_progression: 4 arm と 5 pair が同じ母数、counts が分割、
    rate と worsening rate差 が counts / arm rate から再導出できること、
    per_seed が eligible rows を分割
outcome_conditions: 記録済み metrics から再導出した値と exact 一致

classification: ladder 内の値であり、かつ derive_classification() の導出結果と一致
```

Generated dataset / weights / result artifactはGitへcommitしない。

## Interpretation ladder

`#158`がresult exposure前に固定したexhaustive outcomeは`P1GateAOutcome`に
そのまま入っている。

```text
P1 HAND-PROGRESSION SIGNAL
P1 HAND-PROGRESSION REGRESSION
P1 HAND-PROGRESSION INCONCLUSIVE
P1 EVIDENCE INSUFFICIENT
STOP / INVALID
```

`#152`のladderと違い、`#158`のladderは**数値からdeterministicに決まる**。
したがって`derive_classification()`が機械的に導出し、`record_classification()`は
その導出結果と一致するoutcomeだけを記録できる。結果を見てthreshold、role、
feature familyを変更する余地をcode上に残さない。

primary role（`dataset-test`と`replacement-test`）**両方**で次の3条件が
すべて成立したとき`P1 HAND-PROGRESSION SIGNAL`である。

```text
1  q_v2 worsen-shanten rate < q_v1 worsen-shanten rate
2  paired (q_v2 vs q_v1) lower post-discard shanten rows > higher rows
3  |q_v2 worsen rate - bc worsen rate| < |q_v1 worsen rate - bc worsen rate|
```

両primary roleで上記の逆方向（`>` / `higher > lower` / gap拡大）がすべて成立
したとき`P1 HAND-PROGRESSION REGRESSION`である。valid evidenceはあるが
directionが混在する、またはどちらの条件も満たさない場合は
`P1 HAND-PROGRESSION INCONCLUSIVE`である。

primary roleのhand progressionが`UNAVAILABLE`、またはeligible rowが0で
valid primary comparisonが成立しない場合は`P1 EVIDENCE INSUFFICIENT`である。

`STOP / INVALID`はresult documentへ記録しない。identity mismatch、schema /
provenance不整合、leakage、TEST discipline violationはstrict bindingと
validationがdocument生成前にfail closedするため、**result documentが存在する
時点でこのoutcomeは到達不能**である。`record_classification()`はこれを明示的に
拒否する。

TRAIN / VALIDATIONはtraining / diagnosticとして報告してよいが、positive claimの
正本にしない。ladderはprimary roleだけを読む。

## Interpretation boundary

positiveでも次までしか言わない。

```text
explicit keep-shanten structure
helped this bounded simple Offline Q formulation
preserve hand progression on retained Gate A evidence
```

次は言わない。

```text
P1 is universally required
Q is now strong
hanchan strength improved
Mortal-like representation is proven superior
```

negativeでもP1全体を永久REJECTしない。`keep-shanten only` formulationの
negative evidenceとしてparent `lisjong-project #45`へ返す。

この境界は`interpretation_boundary`としてresult documentへ埋め込み、validatorが
locked constantとの一致を強制する。

## Limitations

- Gate Aはretained behavior-distribution rows上のoffline decision comparisonで
  あり、rollout distributionを観測しない。strength / hanchan improvement claimを
  構成しない
- `dataset-test 271..276`と`replacement-test 354..359`は`#140` / `#152`で
  exposure済みであり、新しいTEST claim / generalization evidenceではない
- TRAIN / VALIDATIONはpositive claimの正本ではない
- rowは独立samplingではない（同じhanchan・同じ手牌から複数row）。per-seed blockは
  記述的であり、p-value / confidence intervalを主張しない
- 変更したのはkeep-shanten feature familyだけであり、reward / gamma / target
  cadence / support restriction / loss / optimizer / hidden width / activation /
  seed / deterministic settings / checkpoint selectionは`#140`の値である
- 新しいhanchan、seed、training data、TEST exposureを作っていない

## Local execution

retained artifactはoperator-local durable storageにあり、CI / online環境からは
アクセスできない。実行はoperator環境で行う。

```powershell
$Artifacts = "C:\Dev\lisjong-artifacts"

python -m lisjong_arena.learned_policy_offline_q p1-gate-a `
    --bundle           "$Artifacts\offlineq-140-rebuild\candidate-pair" `
    --dataset          "<retained dataset directory>" `
    --replacement-test "<retained replacement TEST directory>" `
    --result           "$Artifacts\offlineq-158-p1-gate-a\gate-a.json"
```

`--dataset`と`--replacement-test`はbundleと同じ場所に無くてよく、operator pathを
codeへhard-codeしない。`--result`は既存fileを上書きしない。

期待される出力:

```text
dataset-train:      eligible=<n>/<n> q_v2_vs_q_v1=<n> hand_progression=<AVAILABLE|UNAVAILABLE> worsen q_v2=<r> q_v1=<r> signal=<bool> regression=<bool>
dataset-validation: ...
dataset-test:       ...
replacement-test:   ...
derived_classification=<outcome>
classification=None
```

identity binding、feature / vocabulary identity、supported_indices digest、
derived schema fingerprintのいずれかが合わなければ、この時点でfail closedして
何も書かない。

result artifactをreviewしたうえで、exhaustive outcomeを1件だけ記録する。
記録できるのは`derive_classification()`が導出したoutcomeと一致するものだけで
ある。

```powershell
python -m lisjong_arena.learned_policy_offline_q p1-record-classification `
    --result            "$Artifacts\offlineq-158-p1-gate-a\gate-a.json" `
    --classified-result "$Artifacts\offlineq-158-p1-gate-a\gate-a-classified.json" `
    --outcome           <HAND_PROGRESSION_SIGNAL|HAND_PROGRESSION_REGRESSION|HAND_PROGRESSION_INCONCLUSIVE|EVIDENCE_INSUFFICIENT>
```

**final classificationはこのlocal executionの後にだけ行う。** result artifactを
取得していない状態でoutcomeを記録することはできない。

## Follow-up boundary

`P1 HAND-PROGRESSION SIGNAL`でも、本Issue内で自動的にGate Bへ進まない。次は
parent `lisjong-project #45` / `#44`でreviewし、必要なら別bounded Issueとして
設計する。
