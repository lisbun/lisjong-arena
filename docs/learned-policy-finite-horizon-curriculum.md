# FiniteHorizon-teacher curriculum — Arm Y vs Arm F (Issue #165)

`lisbun/lisjong-arena #162`のGate Bは`P1 GATE B INCONCLUSIVE`で終わった。learned
activationは約78.5%、support fallbackは0%であり、「learned pathがほとんど使われ
なかったから決まらなかった」とは説明しにくい。candidate-only diagnosticsも
wins 4/100・tenpai reached 4/100で、basic offensive capabilityがまだ弱い。

`#165`はここでfeatureを積み上げず、**training teacher / behavior trajectory
distributionだけ**をprimary changed axisにするbounded experimentである。

```text
Arm Y — CONTROL      yakuhai-call x4
Arm F — CURRICULUM   finite-horizon x4
```

この文書はcontractだけを持つ。個別runのnumeric resultとexhaustive outcomeは
Issue #165を正本とする。`#140` protocolは`docs/learned-policy-offline-q.md`、
`#152`の診断contractは`docs/learned-policy-offline-q-diagnosis.md`、P1 derived
representationは`docs/learned-policy-p1-gate-a.md`、P1 hybrid servingと
candidate identityは`docs/learned-policy-p1-gate-b.md`が引き続き正本である。

## Primary changed axis

```text
CHANGE
teacher / behavior trajectory distribution
  yakuhai-call x4   (Arm Y)
      vs
  finite-horizon x4 (Arm F)
```

固定するaxis:

```text
P1 keep-shanten 37 feature / 8241 derived input
1 x 128 ReLU MLP / 802 output / 1,158,434 parameters
Offline Q reward semantics / gamma / Huber loss
optimizer / learning rate / weight decay / batch size
target-sync cadence / maximum epoch budget
training seed / dataloader seed / deterministic settings
fixed_final_iteration checkpoint selection
TRAIN support rule
hybrid activation / yakuhai-call serving fallback
action vocabulary / legal-mask semantics
ordered dataset seeds / split / game mode
```

`FiniteHorizonCompletionPolicy`のhorizon、evaluator、internal semanticsは変更
しない。新しいFiniteHorizon Policyも実装しない。両armとも
`lisjong_arena.policy_catalog`のcurated factoryをそのまま使う。

`maximum_epochs`は`learned_policy_stage2.protocol.MAXIMUM_EPOCHS`が唯一の
source of truthである。`#157`のHandBelief epoch-budget study（40 / 80 epoch）は
別experimentであり、`#165`はそれを持ち込まない。結果を見てepoch、learning rate、
patience、training seedを追加するpathも持たない。

## seed-alignedであってstate-pairedではない

両armは同じordered seedsと同じsplitを使うが、これはenvironment RNGの初期条件を
揃えるためである。teacherが最初に異なるactionを選んだ時点でtrajectoryは分岐する。

```text
same seed  !=  same state trajectory
```

したがってrow count、action-family distribution、TRAIN support set、reward
distribution、state distributionの差はcurriculum interventionのdownstream
consequenceとして受け入れ、人為的にmatchingしない。Arm FのTEST worsen-shanten
rateがArm Yより低いというだけではcausal primary evidenceにならない。

## Population

```text
dataset   465..496   32 hanchan per arm / 4p-red-half / whole-hanchan split
  TRAIN        465..484   20
  VALIDATION   485..490    6
  TEST         491..496    6

rollout   497..521   25 seed blocks x 4 rotations = 100 games / 4p-red-single
```

Arm Y / Arm Fはexactly同じseedsとsplitを使う。実行直前に
`fh_curriculum.check_seed_freshness()`でfreshnessを再確認し、collisionが
result exposure前に判明した場合だけ`SEED PLAN REFORMULATE`として同shapeのfresh
contiguous rangeへ再lockする。silent replacementもresult exposure後のrescueも
行わない。

## Dataset artifact

`arena-learned-policy-offlineq-dataset-v1`のsemanticsは変更しない。`#165`は
experiment-localなversioned schemaを別に持つ。

```text
arena-learned-policy-finite-horizon-curriculum-dataset-v1

<dataset>/
    manifest.json         canonical JSON identity / arm / teacher / digests
    rows.jsonl            1行 = 1 macro-transitionのplayer-safe metadata
    features.f32          N x 8204 float32
    legal_mask.u8         N x 802 uint8
    next_features.f32     N x 8204 float32（terminal rowはall-zero placeholder）
    next_legal_mask.u8    N x 802 uint8（同上）
```

row payloadのbinary layoutは`#140` macro-transition contractと同一であり、
`MacroTransitionFileWriters` / `verify_row_payloads()` / `read_validated_rows()`
をthin reuseする。manifestがbindするのは:

```text
schema version / experiment identity
arm identity / teacher identity / class / factory / population
source revisions（runtime provenance）
ordered seeds / split / game mode
feature identity / fingerprint（stored v1 8204）
derived feature identity / fingerprint（P1 8241）
action vocabulary identity / fingerprint
transition semantics / reward semantics
game records / row count / terminal count / teacher action-family counts
file digests / dataset identity
```

Arm Y / Arm Fは別path、write-once、strict readbackである。wrong arm、wrong
teacher、corrupted payload、editedなmanifestはいずれもfail closedする。
`require_dataset_pair()`は、2 armがseeds / split / feature / vocabulary /
transition / reward / source revisionsを共有し、teacherとdataset identityだけが
異なることを要求する。

## Support semantics

support setはarmごとに**自身のTRAIN datasetから**導出する。

```text
Arm Y support = support(Arm Y TRAIN)
Arm F support = support(Arm F TRAIN)
```

historical `#158` support、cross-arm shared support、他armのsupportを流用する
pathは存在しない。`build_arm_candidate`系のAPIはsupported indicesを引数で受け
取らず、渡されたdatasetから再導出する。checkpoint readbackも同じ再導出で照合
するため、cross-arm substitutionはfail closedする。

persistする値:

```text
supported action indices
support digest
support size
support coverage（TRAIN / VALIDATION support-complete rate）
```

## Candidate identity

candidate identityはweights digest単独ではない。

```text
candidate binding document
    arm / teacher identity
    source dataset identity
    canonical model weights digest
    P1 feature fingerprint
    action vocabulary fingerprint
    support digest
    model block / training block
    hybrid activation block / fallback Policy binding
    source revisions
        -> canonical JSON -> sha256
        -> learned-offlineq-fh-curriculum:<binding digest>
```

checkpoint bytesが変わればweights digestが変わり、candidate identityも変わる。
両armのcheckpointは別pathのwrite-once bundle（`manifest.json` + `weights.pt`）
であり、readbackはidentity / digest / semanticsをすべて再導出して照合する。
生成したweightsはGitへcommitしない。

## Serving semantics

両armとも`#162`のP1 hybrid serving semanticsをexact reuseする。

```text
eligible ordinary discard
+ own TRAIN support complete
    -> P1 Q model

otherwise
    -> yakuhai-call scaffold
```

**重要:**

```text
Arm F training teacher = finite-horizon
Arm F serving fallback = yakuhai-call
```

fallbackまでFiniteHorizonへ変えるとteacher axisだけでなくserving axisも変わり、
このcontrolled experimentが壊れる。`#165`では変更しない。

## Rollout protocol

既存`SingleRoundEvaluationPlan` / `run_single_round_evaluation()` /
`SingleRoundStrengthArtifact` / canonical aggregationをreuseし、新しいgame
runnerを作らない。

```text
ordered seeds        497..521
seed blocks          25
rotations / seed     4
total games          100
game mode            4p-red-single
workers              1
formal TEST          none
role                 DEVELOPMENT CURRICULUM SCREEN
```

ABBB:

```text
rotation 0  [F, Y, Y, Y]
rotation 1  [Y, F, Y, Y]
rotation 2  [Y, Y, F, Y]
rotation 3  [Y, Y, Y, F]
```

## Primary classification

```text
primary metric
  seed-block F-vs-Y score delta
  normal-approx 95% interval

normal_approx_95_interval_lower > 0   -> FINITEHORIZON CURRICULUM ROLLOUT SIGNAL
normal_approx_95_interval_upper < 0   -> FINITEHORIZON CURRICULUM ROLLOUT NEGATIVE
otherwise                             -> FINITEHORIZON CURRICULUM ROLLOUT INCONCLUSIVE
```

result exposure前にrequired evidenceが成立しない場合は
`CURRICULUM EVIDENCE BLOCKED`、protocol / artifact / candidate / result
integrityの違反は`STOP / INVALID`である。これら2つはresult documentが作られる
前のpre-result stateであり、intervalからは導出されない。

secondary Mahjong diagnostics、serving diagnostics、offline mechanism
diagnosticsは`derive_classification()`を一切通らない。

## Diagnostics

offline（両armのTRAIN / VALIDATION / TEST）:

```text
hanchan count / transition rows / terminal rows
teacher action-family counts
discard / riichi / call / winning decisions
TRAIN support size / digest / coverage
reward distribution summary
TEST behavior vs Q-selected hand progression（#152 / #158 thin reuse）
```

打牌は向聴数を下げないため、improve bucketは構造上0である（keepとworsenが
eligible decisionを分割する）。

rollout（両candidate）:

```text
round count / mean round score delta
win count / rate / mean win points
tenpai reached count / mean first tenpai turn
exhaustive draw count / exhaustive-draw tenpai rate
deal-in count / rate / mean deal-in loss

total decisions / learned activation count / rate
scaffold fallback count / rate / support fallback count / rate
illegal selection = 0 / non-finite output = 0 / resolve failure = 0
```

candidate側の母数はgame数（100）、control側はbaseline seat数（3 x 100 = 300）で
あり、母数が異なることをresult documentへ明記する。

## Result artifact

result documentはimmutable strength artifactを正本として参照し、canonical
summaryをraw gamesから再生成して照合する。`result_identity`は
classificationとidentity自身を除いたcanonical bytesのdigestであり、後からの
編集を拒否する。classificationは`record_classification()`が1件だけ記録でき、
その際にdiskの両checkpointをstrict readbackしてbindする。

## Pre-execution lock

real dataset generationの前に、実験条件を一度だけIssue #165へ記録する。

```bash
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-lock \
    --dataset-control   <retained path> \
    --dataset-curriculum <retained path> \
    --candidate-control  <retained path> \
    --candidate-curriculum <retained path> \
    --result-artifact    <retained path> \
    --lock    <lock json> \
    --comment <issue comment markdown>
```

lockはlocked constantとlive runtimeから機械的に組み立てられ、`result_exposed`は
常に`False`である。`validate_pre_execution_lock()`が同じconstantから期待値を
再導出するため、値を書き換えたlockは通らない。

## One-shot local execution

```bash
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-seed-freshness
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-lock ...
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-generate --arm Y ...
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-generate --arm F ...
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-train --arm Y ...
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-train --arm F ...
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-diagnose ...
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-rollout ...
python -m lisjong_arena.learned_policy_offline_q fh-curriculum-record-classification ...
```

real 32-hanchan generation、real two-arm training、real 100-game rolloutはCIへ
入れない。CIはcontract / validator / synthetic / focused regressionまでを見る。

## この実験が主張しないこと

```text
FiniteHorizon is the strongest teacher
FiniteHorizon should be the final Policy
curriculum simplicity alone caused the difference
P1 is universally required
the candidate passes Gate B vs tsumogiri
the candidate beats the Development Champion
hanchan strength improved
formal generalization is established
```

positive / negative / inconclusiveのいずれでも、同一Issue内でseed追加、epoch
追加、feature追加、support rule変更、reward redesign、rescue rolloutを行わず、
Gate B vs tsumogiri / Gate C / hanchanへも自動進行しない。next actionはparent
roadmap Issueが再選定する。
