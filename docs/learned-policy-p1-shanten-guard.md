# Shanten-constrained Q serving diagnostic — G vs U (Issue #173)

`lisbun/lisjong-arena #152`は、同一player-safe state上でOffline QがBC /
behaviorよりpost-discard shantenを悪化させるdiscardへ系統的に偏ることを確認した
（`HAND-PROGRESSION DEGRADATION IDENTIFIED`）。`#158`はQへdeterministic
keep-shanten featureを足し、`#162`はそのexact P1 candidateをfresh
single-round rolloutでservingしたが、

```text
P1 GATE B INCONCLUSIVE
mean seed-block delta  +320.0
95% interval            [-47.1090, 687.1090]
```

だった。`#165`のFiniteHorizon curriculumもinconclusiveに終わった。

本Issueは**trainingを一切変更せず**、`#162`のexact P1 Q candidateについて、
serving時のaction-selection constraintだけを変更するbounded diagnosticを追加し、

> current Q valuesの中に、既知のhand-progression failureをselection-timeで
> 遮断すれば利用可能になるdecision signalが残っているか

を最小変更で切り分ける。

## これは何ではないか

```text
shanten guard
= bounded selection-time serving diagnostic

!= new Q algorithm
!= production Policy
!= final strength fix
```

guardはpost-discard shantenをpre-discard shantenと同じに保つ（＝悪化させない）
discardだけを候補に残す。

```text
guard preserves shanten
!= ukeire optimization
!= hand value optimization
!= full Mahjong utility optimization
```

牌効率（受け入れ枚数）、打点、押し引き、その他Mahjong utilityのいずれも
selectionへ持ち込まない。判定は「向聴数が変わるか変わらないか」という二値だけ
である。

この文書はcontractだけを持つ。個別runのnumeric resultとexhaustive outcomeは
Issue #173を正本とする。`#140` protocolは`docs/learned-policy-offline-q.md`、
`#152`の診断contractは`docs/learned-policy-offline-q-diagnosis.md`、P1 derived
representationは`docs/learned-policy-p1-gate-a.md`、exact candidate
materializationとGate B serving semanticsは`docs/learned-policy-p1-gate-b.md`が
引き続き正本である。本Issueはそれらを一切変更しない。

## Primary changed axis

変更してよいaxisは1つだけである。

```text
CHANGE
  learned Q ordinary-discard action-selection constraint

KEEP
  exact #162 model weights
  P1 8241 representation
  Q values
  TRAIN support set
  hybrid activation semantics
  legal action semantics
  action vocabulary
  yakuhai-call fallback
  checkpoint load semantics
  fresh Policy instance semantics
  evaluation backend / aggregation
```

new training / retraining / extra epoch / different initialization、
reward / gamma / target update / support変更、teacher変更、新しいfeature、
soft shanten penalty、Q値の補正はいずれも行わない。

## Arm design

```text
U — UNGUARDED control          exact #162 serving semantics（verbatim）
G — SHANTEN-GUARDED candidate  同じcandidate binding + selection guardだけ追加
```

両armは`#162`の同一serving checkpoint（同一model reference、同一TRAIN
support set）を共有する。違いはPolicy classだけである。

```text
U   src/.../serving.py            HybridPolicy                （無変更）
G   src/.../p1_shanten_guard.py   ShantenGuardedHybridPolicy   （selectionだけ変更）
```

`ShantenGuardedHybridPolicy`は`HybridPolicy`のsubclassであり、activation判定・
support gate・fallback・legal mask・canonical `resolve_legal_action()`は一切
overrideしない。overrideするのは、learned pathが実際に呼ばれたときの
`_learned_action()`だけである。

```text
original learned-path eligibility PASS   （HybridPolicy.choose_action()が判定、無変更）
    |
legal ordinary discard set L
    |
#152 / #158でlockされたcanonical shanten semanticsで
post-discard shanten == pre-discard shanten となるsubset K
（keep_shanten_tile_mask() を legal discard indexへ intersect するだけ）
    |
K non-empty  -> argmax Q over K   （masked_argmax_q()をKへ適用するだけ）
K empty      -> original argmax Q over L（fallbackしない）
```

重要な境界:

- K emptyを理由にyakuhai-call fallbackへ落とさない。fallback判定自体は
  `HybridPolicy.choose_action()`が変更前と同じ条件（`use_learned`）でしか
  行わない
- Q値へshanten bonus / penaltyを加えない。tie-breakは既存
  `masked_argmax_q()`のdeterministic argmax contractをsubsetへそのまま適用
  するだけである
- calls / riichi / kan / response decisions、fallback pathへguardを適用
  しない。guardが働くのは`use_learned`がTrueの決定だけである
- shanten semanticsを新しく実装しない。`hand_progression.py`の
  `keep_shanten_tile_mask()` / `hand_progression_for_row()`をsingle source
  of truthとしてそのまま再利用する（`reconstruct_concealed_tiles()` ->
  `lisjong.hand_evaluation.calculate_shanten()`という既存derivationの外側の
  内容には触れない）
- hidden opponent hand、wall truth、future state / outcome、teacher
  internal analysisは使わない。読むのは`decision.input`のplayer-safe own
  concealed handだけである
- 赤5 / 通常5は`#158`と同じ37-tile `TILE_AXIS`をそのまま使うため、分離した
  まま扱われる

## Guard candidate identity

Policy behaviorはweightsだけで決まらない。GのidentityはUのbase candidate
binding（weights digest / P1 feature / action vocabulary / support digest /
hybrid activation / fallback Policyを含む）へ、guard rule記述を足しただけの
binding documentから導出する。

```text
guard binding document
    binding_schema_version
    base_candidate_binding   （#162 exact candidate binding、そのまま）
    guard_rule                （selection semanticsの記述。次のsectionを参照）
        |
        v  canonical serialization -> sha256
learned-offlineq-p1-shanten-guard:<binding digest>
```

`base_candidate_binding`のいずれかのfieldが変わればGのidentityも変わる。
guard ruleが変わっても同様である。free-form aliasやweights digest単独は
identityとして受理しない（`require_guard_candidate_identity()`）。

Uのidentityは`#162`の`candidate_identity`をverbatimに使う（変更しない）。
`SingleRoundEvaluationPlan`はcandidate/baselineのidentityが異なることを
要求するため、GとUのidentityが必ず区別される。

### Guard rule

```json
{
  "semantics_id": "arena-learned-policy-offlineq-p1-shanten-guard-v1",
  "scope": "learned-ordinary-discard-selection-only",
  "activation_gate": "unchanged-from-base-candidate",
  "candidate_subset": "legal-ordinary-discard-actions-whose-tile-identity-keeps-shanten",
  "shanten_semantics_source": "lisjong_arena.learned_policy_offline_q.hand_progression.keep_shanten_tile_mask",
  "empty_subset_behavior": "original-legal-masked-argmax-q-no-fallback",
  "tie_break": "existing-masked-argmax-q-contract-restricted-to-subset",
  "q_value_modification": "none",
  "fallback_path_guarded": false,
  "non_discard_decisions_guarded": false
}
```

## Guard diagnostics

`ShantenGuardedHybridPolicy`は各learned-path decisionについて
`GuardDecisionSample`を1件記録する。

```text
keep_shanten_available        legal discardのうち少なくとも1件がkeep-shanten
unguarded_worsens_shanten     original（unguarded）argmax Qがshantenを悪化させるか
action_changed                guarded selectionがunguarded selectionと異なるか
guarded_worsens_shanten       guarded selectionがshantenを悪化させるか（構造上、
                               keep_shanten_availableならFalseで固定される）
baseline_post_discard_shanten unguarded selectionのpost-discard shanten
guarded_post_discard_shanten  guarded selectionのpost-discard shanten
```

`collect_guard_diagnostics()`はG armの全instanceからこれらを集計し、
`GuardDiagnostics`として次を保持する。

```text
learned_decision_count
keep_shanten_available_count / no_keep_shanten_available_count / rate
unguarded_worsen_count / unguarded_keep_count
action_change_count / rate
guarded_worsen_among_available_count            構造上つねに0（違反はfail closed）
mean_baseline_post_discard_shanten
mean_guarded_post_discard_shanten
paired_lower_count / paired_equal_count / paired_higher_count
```

`paired_higher_count`（guardedがbaselineよりshantenを悪化させたcase）も構造上
つねに0であり、`GuardDiagnostics.__post_init__()`がこれらの不変条件を
fail closedで検証する。

## Primary classification

primary metricは既存canonicalの1つだけである。

```text
seed-block G-vs-U score delta
normal-approx 95% interval
```

ただし、classificationはこのintervalだけからは決まらない。

```text
1. STOP / INVALID
2. SHANTEN GUARD EVIDENCE BLOCKED
3. SHANTEN GUARD INACTIVE           guard-induced action change count == 0
4. SHANTEN GUARD ROLLOUT SIGNAL     normal_approx_95_interval_lower > 0
5. SHANTEN GUARD ROLLOUT NEGATIVE   normal_approx_95_interval_upper < 0
6. SHANTEN GUARD ROLLOUT INCONCLUSIVE   intervalが0を跨ぐ
```

`guard_diagnostics.action_change_count == 0`は、intervalの符号より**優先**する。
guardが実際には1件もactionを変えなかった場合、score directionをpositiveにも
negativeにも解釈せず`SHANTEN GUARD INACTIVE`とする
(`derive_classification()`)。

evidence取得前のartifact / environment問題は`SHANTEN GUARD EVIDENCE BLOCKED`、
scientific identity mismatchやprotocol violationは`STOP / INVALID`であり、
どちらもresult documentが作られる前のpre-result stateである。

secondary Mahjong diagnosticsとserving diagnosticsはこのladderを一切通らない
（`classification_rule.secondary_metrics_may_alter_classification = false` /
`serving_diagnostics_may_alter_classification = false`）。

## Fresh development population

```text
game mode       4p-red-single
ordered seeds   522..546
seed blocks     25
rotations/seed  4
total games     100
workers         1  (serial)
role            DEVELOPMENT SHANTEN-GUARD DIAGNOSTIC
formal TEST     none
```

ABBB assignment（`[G, U, U, U]` -> `[U, G, U, U]` -> ...）は既存
`single_round_evaluation`のprotocol invariantであり、`candidate = G` /
`baseline = U`を割り当てるだけである。

`522..546`は`#165`のrollout population（`497..521`）直後のcontiguous range
である。`declared_allocated_seeds()`は`#162` Gate B / `#165`dataset・rollout
populationを含む既存constantsから宣言済みseedを集め、import時のshape検証と
`check_seed_freshness()` / `require_fresh_seed_plan()`の両方でcollisionを
fail closedに確認する。collisionがresult exposure前に判明した場合だけ、同じ
shapeのfresh contiguous rangeへ`SEED PLAN REFORMULATE`する。result exposure後
のseed変更 / extensionは行わない。

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

`run_shanten_guard_diagnostic()`はartifactを保存してから読み直し、raw game
resultsから canonical summaryを再導出して保存済みsummaryと一致することを
確認する。stdoutはmeasurementのsource of truthではない。

`require_diagnostic_artifact()`は、ordered seeds、game mode、rotation count、
100 games、G/U両方のcandidate identity、Gが各seatをちょうど25回担当した
ことをfail closedで確認する。

## Result document

```text
arena-learned-policy-offlineq-p1-shanten-guard-diagnostic-v1
```

保持するのは次である。

```text
schema version / diagnostic id / source / predecessor / parent Issue / protocol id
primary changed axis / locked unchanged axes
guarded_candidate（G binding, base_candidate_identityでUへbind）
unguarded_candidate（U binding, #162 exact identity）
plan（ordered seeds / seed blocks / rotations / games / game mode / workers）
strength artifact（schema / protocol / filename / sha256 / games / retention）
canonical summary
guard_diagnostics
secondary_diagnostics（guarded / unguarded）
serving_diagnostics（guarded / unguarded）
classification rule
limitations / interpretation boundary / next action boundary
provenance
result identity
classification（初期値 None）
```

`validate_diagnostic_result()`は、GとUが同じ`canonical_model_weights_digest`
/ `source_dataset_identity` / `supported_indices_digest` / `selected_epoch` /
`materialization_source` / `real_candidate_materialization`を共有し、かつ
`guarded_candidate.base_candidate_identity == unguarded_candidate.identity`
であることを要求する（`_validate_arms_share_base_identity()`）。これが
primary changed axisが本当にselection constraintだけであることの
machine-checkableな保証である。

`record_classification()`は`p1_gate_b.record_classification()`と同じ理由で、
渡された`LoadedP1ServingCheckpoint`を改めてdiskからstrict readbackし
（`bind_recorded_candidate()`）、fixture / substitute candidateの結果を
real Issue #173 evidenceとして記録できないようにする。

## Secondary diagnostics

classification thresholdには使わないが、必ず報告する。

```text
round count / mean round score delta
win count / win rate / mean win points
tenpai reached count / mean first tenpai turn
exhaustive draw count / exhaustive-draw tenpai rate
deal-in count / rate / mean deal-in loss
```

G側は既存canonical `candidate_metrics.mahjong_metrics`（母数100）をそのまま
写す。U側は`aggregate_seat_round_stats_metrics()`をU担当の3 seat分
（母数300）へ適用するだけであり、`#165`の`baseline_mahjong_metrics()`と同じ
パターンを再利用する。

serving diagnostics（G / Uそれぞれ）:

```text
policy instance count / total decisions
learned activation count / rate
scaffold fallback count / rate
support fallback count / rate

illegal selection = 0
non-finite model output = 0
resolve failure = 0
```

## Local execution

**real 100-game #173 experimentは、reviewed / merged machineryでのみ行う。**

`#162`のexact P1 serving checkpointはoperator-local durable storageにあり、
CI / cloud環境からはアクセスできない。cloud / CIにはcheckpointもretention
rootも存在しないため、そこで実行できるのはimplementation、fixture、
automated testsまでである。fixture candidateの結果は本Issueのevidenceに
ならない（`real_candidate_materialization = false`）。

operator実行は`__main__`のCLIサブコマンドではなく、既存`p1-materialize`で
retainした`#162`checkpointをPython APIから直接呼び出す。

```python
from lisjong_arena.learned_policy_offline_q.p1_candidate import (
    load_p1_serving_checkpoint,
)
from lisjong_arena.learned_policy_offline_q.p1_shanten_guard_diagnostic import (
    record_classification,
    require_fresh_seed_plan,
    run_shanten_guard_diagnostic,
)

require_fresh_seed_plan()  # real execution前にseed freshnessを再確認する

checkpoint = load_p1_serving_checkpoint(
    r"C:\Dev\lisjong-artifacts\offlineq-162-p1-gate-b\candidate"
)
measurement = run_shanten_guard_diagnostic(
    checkpoint,
    r"C:\Dev\lisjong-artifacts\offlineq-173-shanten-guard\artifact.json",
)
print(measurement.derived_outcome)  # review後にrecord_classification()で1件だけ記録する
```

`record_classification()`は`checkpoint`（strict readback済みの
`LoadedP1ServingCheckpoint`）を必須引数にする。`materialize_p1_serving_checkpoint()`
経由でのwrite-once retentionは`#162`と同じ`resolve_retention_target()`を使い、
Git work tree内やtemporary directory配下をfail closedで拒否する。generated
weights、strength artifact、result documentはいずれもGitへcommitしない。

## Follow-up boundary

どのoutcomeでも、本Issue内で次へ自動進行しない。`SHANTEN GUARD ROLLOUT
SIGNAL`でもGate C / hanchan / production adoptionを自動起票しない。
`NEGATIVE` / `INCONCLUSIVE` / `INACTIVE`でも、rescue seeds、soft shanten
penalty、ukeire tie-break、reward redesign、teacher変更、追加featureを本
Issueへ追加しない。次はparent `lisjong-project #45`でreviewし、P6 offline
support / IQL-CQL、P3 long-horizon value signal、P2 tile-structured
representation、追加P1 formulation、guarded candidateの次fidelity
evaluation等を現在のevidenceで再比較する。
