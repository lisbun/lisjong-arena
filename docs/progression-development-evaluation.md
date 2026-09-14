# Progression development evaluation (Issue #252)

`lisbun/lisjong #169 / PR #170` の exact adaptive candidate を、そのexact parent
baselineと同じ条件でreal gameへ送るためのbounded experiment contractである。

```text
P   TerminalShantenProgressionMechanismRiichiDefensePolicy   (#169 / #170)
C   MechanismRiichiDefenseYakuhaiCallPolicy                  (mechanism-riichi-defense)
T   PassiveTsumogiriPolicy                                   (既存comparatorをthin reuse)
```

個別runのnumeric resultと最終classificationは Issue #252 を正本とする。この文書は
contractだけを持ち、Issue本文を複製しない。

## 何をするIssueか

```text
Phase A   technical feasibility gate       647..650 / 4 rotations / 16 games
Phase B   paired development screen        651..750 / 4 rotations / 400 + 400 games
```

Aはexecution feasibilityのtechnical gate、Bはdevelopment strength screenである。
feasibility positiveはstrength positiveを意味せず、development positiveはbaseline
promotionもhanchan superiorityもformal generalizationも意味しない。

## exact adaptive semanticsを変更しない

candidate Pはlisjong側のexact #170 implementationをそのまま実行する。Arenaは
approximation、Monte Carlo、beam search、horizon変更、pruning追加を導入しない。

特に `lisjong #171` の draw-multiset / clairvoyant reduction は採用しない。#171は

```text
adaptive     = sum of min
clairvoyant  = min with future draws already known
```

が等価でないことが反例付きで確定してcloseされており、exact-safe optimizationとして
成立しない。`require_exact_candidate_semantics()` が、bound revision上で

- candidate class / module / horizon が #170 のものであること
- candidateがexact parent generationであること
- `lisjong.policies` にclairvoyant / draw-multiset moduleが存在しないこと

をfail closedで確認する。

## passive x3の2 arm design

同じordered seedsと同じrotation shapeで2 armを走らせる。

```text
Arm P   [P,T,T,T] [T,P,T,T] [T,T,P,T] [T,T,T,P]
Arm C   [C,T,T,T] [T,C,T,T] [T,T,C,T] [T,T,T,C]
```

直接 `P vs C x3` へは変更しない。#169が事前登録したtargetがpassive tsumogiri x3
に対する400 games / armであり、changed offensive decision axisをより素直に分離
できるためである。Tはweak fixed development comparatorであってstrength baseline
ではない。

## paired seed-block primary statistic

primary unitは1 ordered seedである。

```text
P_s = P armのfocal seat final scoreを4 rotationsで平均
C_s = C armの同じ量
D_s = P_s - C_s

primary = mean(D_s) と normal-approx 95% interval
```

400 P games と 400 C games を800 independent primary observationsとしては扱わない。
classificationは事前登録した1 ruleだけである。

```text
lower > 0   -> PROGRESSION DEVELOPMENT SIGNAL
upper < 0   -> PROGRESSION DEVELOPMENT NEGATIVE
otherwise   -> PROGRESSION DEVELOPMENT INCONCLUSIVE
```

secondary diagnostics（win rate、tenpai、deal-in、exhaustive draw等）はdescriptiveで
あり、`classify_paired_summary()` の引数にも入らない。classificationを後から書き換える
経路を型の形で持たない。

## 8時間のfeasibility gate

Phase Aは同じ16 gamesをworker設定だけ変えて実行し、以下を満たす場合にだけ
Phase Bへ進む。

```text
16/16 games完走
execution failureなし
parallel raw outcomes == serial raw outcomes
provenance valid
fastest valid worker settingが決まる
projected 400-game P-arm wall-clock <= 8 hours
```

`8 hours` はpre-registeredなpractical execution boundであってstrength thresholdでは
ない。満たさない場合は

```text
PROGRESSION DEVELOPMENT EVALUATION INFEASIBLE
```

と分類し、Phase Bを実行しない。P semanticsの変更、#171 approximation、easier seedsへの
差し替えでは応答しない。

worker数はexecution-performance settingであってscientific axisではない。
`select_fastest_valid_worker_count()` はwall-clockと正当性（serial一致 / 失敗なし /
16件完走）だけを見る。game scoreは引数にも入らない。Phase Bのworker数はPhase Aの
gate済みrecordからのみ供給され、callerは指定できない。

## progression diagnosticsのavailability

bound #170 candidateは typed `ProgressionDecisionAnalysis` をdecision内部で生成するが、
`_decide_discard()` は `PolicyDecision(action=selected, analysis=None)` を返し、public
decision contractへ載せない。したがってactivation / action-change / root post-discard
shantenはstable seamから観測できない。

Arenaはこれを埋めるために **progression DPを二度実行しない**。lisjongのprivate DP
internalsも再構築せず、public APIも広げない。`progression_diagnostics_availability()`
が理由付きで `available = false` を記録する。

```text
optional diagnostic unavailable != evaluation invalid
```

## artifact / one-shot discipline

既存Arena primitiveをそのまま使う。

```text
SingleRoundEvaluationPlan / run_single_round_evaluation[_parallel]()
SingleRoundStrengthArtifact / canonical aggregation / strict readback
_execution_safety の clean head / merged revision / write-once destination
```

新しいgame runnerもgeneric evaluation frameworkも作らない。残すlogical outputは

```text
technical feasibility record
P arm immutable strength artifact
C arm immutable strength artifact
paired result document（classification込み）
```

であり、いずれもwrite-once・content-addressed identity付き・strict readback可能で
ある。生成artifactはGit repositoryへcommitしない。

## 実行境界（重要）

real Phase A / Phase B executionは **merge後のoperator作業** である。testとCIでは
locked seedsのreal evaluationを実行しない。

pre-execution lockは `build_lock_document()` が生成し、

```text
worktree clean
HEAD == collected provenance revision
HEAD が merged long-lived branch に含まれる
artifact destinations が未作成
```

を満たさない限りfail closedする。したがってPR branchからreal lockもreal scientific
executionも開始できない。

## Operator CLI

```bash
python -m lisjong_arena.progression_development lock \
  --out LOCK.json \
  --feasibility-record FEASIBILITY.json \
  --candidate-artifact P.json \
  --parent-artifact C.json \
  --paired-result PAIRED.json

python -m lisjong_arena.progression_development phase-a --out FEASIBILITY.json

python -m lisjong_arena.progression_development phase-b \
  --feasibility-record FEASIBILITY.json \
  --candidate-artifact P.json \
  --parent-artifact C.json \
  --paired-result PAIRED.json
```

`phase-a` はgateを通らなければ非zeroで終了し、`phase-b` はgate済みrecordでなければ
実行しない。

## no-rescue boundary

Phase A result exposure後に、seed replacement / extension / rescueは行わない。
NEGATIVE / INCONCLUSIVEを同じIssue内でhorizon変更、objective変更、clairvoyant
approximation、comparator変更によって救済しない。bounded resultを記録し、#169 /
current heuristic-strength planningへ戻る。
