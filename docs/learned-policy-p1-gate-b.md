# P1 Gate B — exact #158 candidate vs passive tsumogiri x3 (Issue #162)

`lisbun/lisjong-arena #158`のGate Aは、retained same-state rows上で

```text
FINAL OUTCOME
P1 HAND-PROGRESSION SIGNAL
```

に到達した。ただしeffectは小さく、degradationは解消していない。

`#162`のGate Bは、そこからさらにfeatureを足すのではなく、**`#158`で得たexact P1
hybrid candidateを一切変更せずserveし**、fresh development single-round rollout上で
passiveなtsumogiri x3に対して最低限のoffensive capabilityを示すかだけを見る。

```text
Gate A                              Gate B
retained same-state rows      ->    fresh single-round rollout
offline decision comparison         candidate vs passive tsumogiri x3
                                    4p-red-single / 25 seed blocks / 4 rotations
```

この文書はcontractだけを持つ。個別runのnumeric resultとexhaustive outcomeは
Issue #162を正本とする。`#140` protocolは`docs/learned-policy-offline-q.md`、
`#152`の診断contractは`docs/learned-policy-offline-q-diagnosis.md`、P1 derived
representationは`docs/learned-policy-p1-gate-a.md`が引き続き正本である。

## Gate Bが主張しないこと

Gate Bはone-way viability filterであり、strength confirmationではない。

```text
Gate B positive
!= strength confirmation
!= P1 causal effect の証明
!= Q-v1 との比較
!= BC との比較
!= Development Champion との比較
!= hanchan strength improvement
!= formal generalization
!= final Policy candidate
```

特に本Issueでは同じGate B populationでQ-v1をpaired comparatorとして走らせない。
したがって`P1 > Q-v1 in rollout`というclaimは行わない。

candidateは`#140`以来のhybridであり、learned pathを使わないdecisionでは
`yakuhai-call` scaffoldへfallbackする。一方comparatorはpassive tsumogiriである。
この非対称性は意図的であり、**Gate BはP1 feature単独ではなく`#158` exact hybrid
candidate全体のviabilityを測る**。

これらの境界は`p1_gate_b.GATE_B_LIMITATIONS` /
`p1_gate_b.INTERPRETATION_BOUNDARY`としてresult documentへ埋め込まれ、validatorが
locked constantとの一致を強制する。

## Primary changed axis

学習側のaxisは変更しない。

```text
MODEL / TRAINING CHANGE
  none

EVALUATION CHANGE
  retained same-state Gate A  ->  fresh interactive single-round Gate B
```

## Gate 0 — exact P1 candidate materialization

`src/lisjong_arena/learned_policy_offline_q/p1_candidate.py`が所有する。

### Locked candidate identities

```text
canonical model weights digest  f8108bf1e671007f22a8b36295ff545b3198479f74d83bb48da8a45df194461f
source dataset identity         69094c1b82f2aaedfed57cb3021b90d44642c3978a2368d4d1e2d927c5a7b2f4
TRAIN support digest            230b2f07dc95d169ebfb85b9deb6174f22909b52025979ae64da541bd9481d9e
selected epoch                  20 / 20  (fixed_final_iteration)
parameter count                 1,158,434
P1 feature semantics            arena-learned-policy-offlineq-p1-keep-shanten-feature-v1
P1 tensor schema                arena-learned-policy-offlineq-p1-keep-shanten-tensor-v1
P1 feature dimension            8241
P1 feature fingerprint          beae3eb8d3dc79b4f837ec98ed3b90eb7530e6f931102b8cdf4854bcf0a15409
action vocabulary               lisjong-action-vocabulary-1  size 802
action vocabulary fingerprint   543c6bca832069dd88b22554b8546ddcd958840a7be7ed291b4ebab6302d7952
```

`LOCKED_P1_CANDIDATE`はweights digest / dataset identity / support digestを保持し、
dataset identityとsupport digestは`#152` / `#158`の`LOCKED_SOURCE_IDENTITIES`から
そのまま引く（import時に一致をfail closedで確認する）。

### Materialization path

```text
A. --weights   exact retained #158 weights -> strict state_dict load -> digest verify
B. --dataset   exact deterministic reconstruction -> digest verify
```

Path Bは新しいresearch trainingではなく、`#158` weightsのexact reconstruction
だけを目的とする。`train_p1_q_model()`（`#140`と同一のloss / optimizer / batch
size / gamma / target cadence / support restriction / seeds / deterministic
settings / `fixed_final_iteration`）をそのまま再利用する。

digestがexact一致しなければ`P1CandidateError`でfail closedする。

```text
digest mismatch
-> seed変更            なし
-> epoch追加            なし
-> optimizer変更        なし
-> alternate config retry なし
-> tolerance acceptance なし
```

該当pathはcodeとして存在しない。mismatchは`STOP / INVALID`である。

### Candidate logical identity — weights-onlyではない

Policy behaviorはweightsだけで決まらない。candidate identityはbinding document
のcanonical serializationから導出する。

```text
candidate binding document
    binding_schema_version
    canonical model weights digest
    P1 feature block（semantics id / tensor schema / dimension / fingerprint）
    action vocabulary block（version / size / fingerprint）
    TRAIN support set digest
    hybrid activation semantics block
    fallback Policy identity / class / source revision
        |
        v  canonical serialization -> sha256
learned-offlineq-p1-gateb:<binding digest>
```

いずれか1つでも変われば別candidate identityになる。free-form aliasや
weights digest単独はidentityとして受理しない
（`require_candidate_identity()`）。

### Serving checkpoint

```text
arena-learned-policy-offlineq-p1-serving-checkpoint-v1
```

experiment-localなwrite-once bundle（`manifest.json` + `weights.pt`）である。
manifestはschema version、source / predecessor / parent Issue、protocol id、
candidate identity、binding document、canonical weights digest、serialized
weights digest / bytes、source dataset identity、P1 feature block、action
vocabulary block、supported indices（と digest）、model block、training block、
parameter count、selected epoch、hybrid activation block、fallback Policy block、
materialization source、source revisions、runtime provenance、そして
`strength_claim = null`を持つ。

`load_p1_serving_checkpoint()`は自己申告値をauthorityにしない。candidate
identityはbinding documentから、weights digestは実際にloadしたweightsから、
`real_candidate_materialization`はlocked constantとの比較から、それぞれ**再導出**
して照合する。

retention先の解決は`learned_policy_stage4a.candidate.resolve_retention_target()`
をそのまま再利用する。したがってGit work tree内やtemporary directory配下は
fail closedであり、**generated weightsはrepositoryへ入らない**。

### fixture candidateはreal Gate B evidenceにならない

`ExpectedCandidateIdentities`をcallerが差し替えられるのは、synthetic fixtureで
contract自体をtestできるようにするためだけである。

`real_candidate_materialization`は**どの境界でも自己申告値にしない**。
checkpoint manifest（`load_p1_serving_checkpoint()`）とGate B result document
（`validate_gate_b_result()`）の両方が、記録された`expected_identities`から
同じ規則で再導出して照合する。

```text
expected_identities == LOCKED_P1_CANDIDATE  ->  true
otherwise                                   ->  false
```

result documentのcandidate blockは`expected_identities`を保持し、validatorは
さらに

```text
candidate.canonical_model_weights_digest == expected_identities.canonical_model_weights_digest
candidate.source_dataset_identity        == expected_identities.source_dataset_identity
candidate.supported_indices_digest       == expected_identities.support_set_digest
```

を要求する。したがって`true`は必ず「exact #158 candidate identitiesである」
ことを含意し、checkpoint loaderを経由せず組み立てたresult documentでも、
fixture / substitute candidateのbindingを自己整合的に作って
`real_candidate_materialization = true`と`result_identity`を再計算する経路は
fail closedになる。

Gate B resultはこのflagが`true`のcandidateでしかexhaustive outcomeを記録
できない（`record_classification()`）。

### identity claimとactual checkpointのbinding

digest文字列の自己整合性は「exact #158 checkpointを実際にservingした」ことを
証明しない。既知のlocked digestをdocumentへ書き並べ、matching bindingと
identityを再生成し、`result_identity`を再計算するだけで整合したdocumentは
作れてしまう。

そこで`record_classification()`はstrict-loadedな
`LoadedP1ServingCheckpoint`を**必須引数**にし、`bind_recorded_candidate()`が

```text
checkpoint.path のbundleを locked expectations の下で読み直す
    -> weights bytes を読む
    -> actual bytes から canonical model weights digest を再導出
    -> re-readback した checkpoint の candidate block が
       result document の candidate block と exact 一致することを要求
```

を行う。`load_p1_serving_checkpoint()`のexpectationsはこの経路では
callerが差し替えられない`LOCKED_P1_CANDIDATE`である。したがってこの境界を
通れるのは、実際にexact #158 weightsを持つbundleだけであり、

- weights bytesを持たないsnapshot
- in-processで組み立てた`LoadedP1ServingCheckpoint` dataclass
- locked identity文字列だけを並べたsynthetic result
- bundleが存在しないresult

はいずれもclassificationできない。

## Candidate serving semantics

`src/lisjong_arena/learned_policy_offline_q/p1_serving.py`が所有する。

```text
DecisionContext
    |
    +-- eligible ordinary discard + TRAIN support complete
    |       -> locked v1 8204 encoding
    |       -> #158 keep-shanten 37 mask
    |       -> P1 8241 feature
    |       -> Q-v2
    |       -> legal masked argmax
    |
    +-- otherwise
            -> yakuhai-call scaffold
```

`#140`のactivation / fallback / legal mask / canonical `resolve_legal_action()`
semanticsは**複製しない**。既存`HybridPolicy`をそのまま使い、v1 8204 encodingの
あとへ`#158`の`derive_p1_row()`を挟むminimum encoder seam
（`HybridRuntime.derive_features` / `HybridRuntime.feature_dimension`）だけを
追加した。

default v1 pathは`derive_features=None` / `feature_dimension=8204`であり、
8204入力BC/Q hybridのbehaviorは変わらない。`derive_features`が`None`のまま
`feature_dimension`だけを変えることはfail closedで拒否する。

serving側の情報境界:

- legal maskはsupport gateとaction selectionにだけ使い、feature derivationへ
  渡さない（`derive_p1_row()`の入力はencodeされたv1 feature rowだけである）
- hidden opponent hand、wall truth、future state / outcomeを読まない
- 向聴semanticsはArenaで再実装せず、canonical
  `lisjong.hand_evaluation.calculate_shanten()`を`#158`の
  `keep_shanten_tile_mask()`経由でそのまま使う
- checkpointはruntime構築時に1回だけloadし、decisionごとにreloadしない
- Policy instanceは各game・各seatごとに`create_policy()`から新規生成する

## Locked Gate B comparator

`arena-p1-gate-b-passive-tsumogiri-v1`
（`p1_gate_b_comparator.PassiveTsumogiriPolicy`）。

```text
Ron / Tsumoがlegal
    -> deterministic winning action（canonical vocabulary index順の総順序）

winning actionが無く Passがlegal
    -> Pass

otherwise（own-turn）
    -> tsumogiri == True かつ tile == own_hand.drawn_tile のlegal DiscardAction
       をexactly 1件
```

chi / pon / daiminkan / riichi / ankan / kakan / kyuushu kyuuhaiは選ばない。
unique tsumogiri discardを解決できなければarbitrary fallbackせず
`PassiveTsumogiriError`でfail closedする。

決定は`DecisionContext`だけの関数であり、`legal_actions`の入力順に依存しない。
PRNG、hidden mutable state、instance stateを持たない（`__slots__ = ()`）。

これはArena-owned / Gate-B-specificなexperiment comparatorであり、curated
`lisjong_arena.policy_catalog.POLICY_CATALOG`へ**恒久登録しない**。production
Policy、strength baseline、Development Championのいずれでもない。

## Gate B plan

```text
game mode       4p-red-single
ordered seeds   440..464
seed blocks     25
rotations/seed  4
total games     100
workers         1  (serial)
role            DEVELOPMENT-ONLY P1 GATE B
formal TEST     none
```

ABBB assignment（`[P1, T, T, T]` -> `[T, P1, T, T]` -> ...）は既存
`single_round_evaluation`のprotocol invariantであり、Gate Bはそれを再実装しない。

seed populationはimport時に、`100..359`（historical prefix）と`360..439`
（Phase 10 scale learning curve）に対する非交差をfail closedで確認する。
collisionが判明した場合は**result exposure前**にだけ`SEED PLAN REFORMULATE`として
同じshapeのfresh rangeへre-lockする。resultを見た後のseed変更 / extensionは行わない。

Learned runtimeのprocess serializationを本Issueへ持ち込まないため、実行は
serial（`workers = 1`）である。parallel Learned runtimeはGate Bのために新設しない。

## Execution / artifact

新しいgame runner / seat rotation / score aggregationを作らず、既存contractを
thin reuseする。

```text
SingleRoundEvaluationPlan
run_single_round_evaluation()
save_single_round_artifact()      immutable / write-once
load_single_round_artifact()      strict readback
summarize_single_round_strength() canonical re-derivation
```

`run_gate_b()`はartifactを保存してから読み直し、raw game resultsから
canonical summaryを再導出して保存済みsummaryと一致することを確認する。
stdoutはmeasurementのsource of truthではない。

`require_gate_b_artifact()`は、ordered seeds、game mode、rotation count、100
games、candidate identity、comparator identity、candidateが各seatをちょうど25回
担当したことをfail closedで確認する。partial run、corrupt artifact、seed
mismatch、comparator substitutionはここで止まる。

## Result document

```text
arena-learned-policy-offlineq-p1-gate-b-v1
```

保持するのは次である。

```text
schema version / gate id / source / predecessor / parent Issue / protocol id
candidate（logical identity / binding document / canonical weights digest /
           checkpoint schema / materialization source / selected epoch /
           source dataset identity / support digest / expected identities /
           real_candidate_materialization / retention target）
comparator（identity / semantics / catalog registration = false）
plan（ordered seeds / seed blocks / rotations / games / game mode / workers）
strength artifact（schema / protocol / filename / sha256 / games / retention）
canonical summary（既存canonical aggregationの結果そのもの）
serving diagnostics
classification rule
limitations / interpretation boundary
provenance
result identity
classification（初期値 None）
```

`result_identity`は`classification`と自身を除いたcanonical bytesのsha256であり、
outcomeを記録しても変わらない。validatorはこれを再導出して、記録済み
measurementの後編集を検出する。ただしdocument全体を作り直せば
`result_identity`も再計算できるため、これは改竄検出であってidentity保証では
ない。scientific identityの保証は上記の`real_candidate_materialization`
再導出とcandidate binding再導出が担う。

## Primary classification

primary metricは既存canonicalの1つだけである。

```text
seed-block candidate-vs-baseline score delta
normal-approx 95% interval
```

```text
normal_approx_95_interval_lower > 0   -> P1 GATE B POSITIVE SIGNAL
normal_approx_95_interval_upper < 0   -> P1 GATE B NEGATIVE SIGNAL
otherwise                             -> P1 GATE B INCONCLUSIVE
```

evidence取得前のartifact / environment問題は`P1 GATE B EVIDENCE BLOCKED`、
scientific identity mismatchやprotocol violationは`STOP / INVALID`であり、
どちらもresult documentが作られる前のpre-result stateである。したがって
`record_classification()`はこの2つを記録できない。

`derive_classification()`はcanonical intervalからのみoutcomeを導出する。
secondary Mahjong metricsとserving diagnosticsはこのpathを通らず、
result documentの`classification_rule`にもその旨がlocked constantとして入る。

## Secondary diagnostics

classification thresholdには使わないが、必ず報告する。canonical summaryが
既に保持しているため、別式で再計算しない。

```text
round count / mean round score delta
win count / win rate / mean win points
tenpai reached count / mean first tenpai turn
exhaustive draw count / exhaustive-draw tenpai rate
deal-in count / rate / mean deal-in loss
```

serving diagnostics:

```text
policy instance count / total decisions
learned activation count / rate
ineligible scaffold fallback count / rate
support fallback count / rate

illegal selection = 0
non-finite model output = 0
resolve failure = 0
```

後者3つはserving path上でfail closedであり、1件でも起きればrunがabortして
result documentは作られない。validatorはこれらが0であることを要求する。
activation / scaffold fallback / support fallbackの3 countは
total decisionsを完全に分割していなければならない。

## Local execution

**real Gate B executionは、reviewed / merged machineryでのみ行う。**

`#158`のretained artifactとexact P1 candidateはoperator-local durable storageに
あり、CI / cloud環境からはアクセスできない。cloud / CIには`#158` candidateも
retention rootも存在しないため、そこで実行できるのはimplementation、fixture、
automated testsまでである。fixture candidateの結果はGate B evidenceにならない
（`real_candidate_materialization = false`）。

```powershell
$Artifacts = "C:\Dev\lisjong-artifacts"

# Gate 0 -- exact candidate materialization（A: retained weights）
python -m lisjong_arena.learned_policy_offline_q p1-materialize `
    --bundle         "$Artifacts\offlineq-140-rebuild\candidate-pair" `
    --weights        "<retained #158 P1 weights.pt>" `
    --retention-root "$Artifacts"

# Gate 0 -- exact deterministic reconstruction（B: retained dataset）
python -m lisjong_arena.learned_policy_offline_q p1-materialize `
    --bundle         "$Artifacts\offlineq-140-rebuild\candidate-pair" `
    --dataset        "<retained dataset directory>" `
    --retention-root "$Artifacts"
```

`--retention-backend`（既定`operator-local-durable`）と`--retention-key`
（既定`offlineq-162-p1-gate-b/candidate`）は明示もできる。`--retention-root`は
実在するabsolute directoryで、temporary directory配下でもGit work tree内でも
あってはならない。宣言できるnon-ephemeral rootが無い場合は
`P1 GATE B EVIDENCE BLOCKED`として終了し、Gate Bへ進まない。

期待される出力:

```text
materialization_source=<exact-retained-158-weights|exact-158-deterministic-reconstruction>
canonical_model_weights_digest=f8108bf1...
candidate_identity=learned-offlineq-p1-gateb:<binding digest>
selected_epoch=20
real_candidate_materialization=True
retention_key=offlineq-162-p1-gate-b/candidate
```

`real_candidate_materialization=False`が出た場合、それはlocked #158 candidateでは
ない。Gate Bへ進まない。

pre-execution lock commentを投稿したうえで、Gate Bを1回だけ実行する。

```powershell
python -m lisjong_arena.learned_policy_offline_q p1-gate-b `
    --checkpoint "$Artifacts\offlineq-162-p1-gate-b\candidate" `
    --artifact   "$Artifacts\offlineq-162-p1-gate-b\gate-b-artifact.json" `
    --result     "$Artifacts\offlineq-162-p1-gate-b\gate-b.json"
```

`--artifact`と`--result`は既存fileを上書きしない。

result artifactをreviewしたうえで、exhaustive outcomeを1件だけ記録する。
記録できるのは`derive_classification()`が導出したoutcomeと一致するものだけで
ある。

```powershell
python -m lisjong_arena.learned_policy_offline_q p1-gate-b-record-classification `
    --result            "$Artifacts\offlineq-162-p1-gate-b\gate-b.json" `
    --classified-result "$Artifacts\offlineq-162-p1-gate-b\gate-b-classified.json" `
    --checkpoint        "$Artifacts\offlineq-162-p1-gate-b\candidate" `
    --outcome           <POSITIVE_SIGNAL|NEGATIVE_SIGNAL|INCONCLUSIVE>
```

`--checkpoint`は必須である。classificationはresult documentが並べたidentity
digestではなく、そこでstrict readbackしたexact #158 serving checkpointへ
bindされる。

generated weights、strength artifact、result documentはいずれもGitへcommitしない。

## Follow-up boundary

`P1 GATE B POSITIVE SIGNAL`でも、本Issue内でGate Cへ自動進行しない。
`P1 GATE B NEGATIVE SIGNAL` / `P1 GATE B INCONCLUSIVE`でも、seed追加、comparator
変更、second P1 feature、retraining、rescue Gate Bを同じIssueで行わない。次は
parent `lisjong-project #45` / `#44`でreviewし、必要なら別bounded Issueとして
設計する。
