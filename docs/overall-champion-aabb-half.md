# Overall Champion AABB half-game formal protocol v1

Issue #250。`lisjong-project` ADR 0005が定めたOverall Champion determinationを、
Arena-ownedの再現可能・fail-closed・strict-read可能なformal evaluation protocol
として実装したもののcontract文書である。

Protocol identity:

```text
arena-overall-champion-aabb-half-v1
```

実装は `src/lisjong_arena/overall_champion_aabb/`。

> Status: contract only. 本文書の時点でcurrent Heuristic / Learning Championの
> formal 400-hanchan runは実行していない。actual formal eventはmerge後の
> follow-up Issueでoperatorがlock -> one-shot execution -> strict verifyの順に行う。

## 1. Overall evaluationとは何か

Overall Champion determinationは、**cross-family formal AABB half-game evaluation**
である。

```text
A = current Heuristic Champion
B = current Learning Champion

game mode   4p-red-half
matchup     AABB
```

各半荘にA/Bが2 seatずつ参加し、locked populationの全体でA/Bが各seat positionへ
同数回配置される。

参照: `lisjong-project` ADR 0005 — Overall Champion determination by AABB half-game
(`docs/decisions/0005-overall-champion-aabb-half.md`)、ADR 0004 — Champion family
separation、`lisjong-project` #55。

## 2. family-internal promotion != Overall

family内promotionとcross-family Overall determinationは別のevaluation eventで
ある。

```text
new heuristic candidate vs current Heuristic Champion
new learning candidate  vs current Learning Champion
        family-internal promotion

Heuristic Champion vs Learning Champion
        cross-family Overall determination
```

family内で使ったdevelopment gate、track-specific diagnostic、training validation
metricはOverall判定へ持ち込まない。

```text
family Champion status
        !=
Overall superiority evidence
```

片方のfamilyが `Champion: not established` ならformal Overall runを開始しない。
相手不在を理由に他方をOverallへ自動昇格させない。

## 3. Primary metric = average-rank seed-block delta

Overallのprimary strength claimはhanchan-level final placementである。

seed `s` の4 rotationsから:

```text
H_rank(s) = Heuristic family 8 seat-resultsのmean final rank
L_rank(s) = Learning  family 8 seat-resultsのmean final rank

D(s) = L_rank(s) - H_rank(s)
```

符号の意味:

```text
D > 0  -> Heuristic advantage
D < 0  -> Learning advantage
```

rankは小さいほど強いため、`L - H` が正のときHeuristicが有利になる。

## 4. Statistical N = seed blocks

統計的単位は**seed block**である。

```text
seed blocks       100     <- statistical N
rotations / seed    4
total hanchan     400     <- N ではない
seat-results / family
                  800     <- N ではない
```

同一seedの4 rotationsは1 blockへ集約し、seat-resultやhanchanを独立標本として
扱わない。同一game内のseat resultは相関しており、それを独立Nとみなした区間は
uncertaintyを過小評価する(`lisjong-project` #62)。

95% intervalはv1では:

```text
mean D  ±  1.96 * standard error
standard error = sample SD (N-1) / sqrt(100)
```

とする。

100という数はbounded budgetであり、「100 blocksであらゆるstrength差を検出できる」
というpower proofではない。必要sample sizeはmetric / variance / detectable effect /
desired uncertainty / comparison designに依存する。

## 5. Secondary score diagnostics do not override primary

secondary diagnosticsとして少なくとも次を記録する。

```text
family average rank
1st / 2nd / 3rd / 4th counts
family average final score
mean final-score difference
seat-position-specific mean rank / score
```

これらは**primary classificationを一切overrideしない**。

```text
rank result       = INCONCLUSIVE
score diagnostic  = one side positive
        -> Overall classification は INCONCLUSIVE のまま
```

`mean_final_score_difference` は `Heuristic mean final score - Learning mean final
score` であり、正のときHeuristic advantageと読む(primaryと符号の向きが揃う)。
scoreをprimary classifierへ昇格させない。

## 6. Exhaustive classification

valid formal resultはexactly one:

```text
HEURISTIC CHAMPION SUPERIOR
LEARNING CHAMPION SUPERIOR
OVERALL INCONCLUSIVE
STOP / INVALID
```

Decision rule:

```text
protocol / provenance / artifact invalid
    -> STOP / INVALID

95% interval lower > 0
    -> HEURISTIC CHAMPION SUPERIOR

95% interval upper < 0
    -> LEARNING CHAMPION SUPERIOR

otherwise
    -> OVERALL INCONCLUSIVE
```

`lower == 0` / `upper == 0` はsuperiorityに含めず `OVERALL INCONCLUSIVE` とする。

`STOP / INVALID` はvalid resultではなくevidence rejectionであり、result artifactへ
書かれない。invalid evidenceは各boundaryがfail closedし、operator surfaceだけが
このlabelを表示する。invalid evidenceを通常のsuperiority resultへ変換しない。

### INCONCLUSIVE is a valid terminal outcome

```text
INCONCLUSIVE
    != equivalent
    != equally strong
    != permission to add seeds
```

`OVERALL INCONCLUSIVE` はcurrent pairについて `Overall Champion: not established`
を意味する、正常な終端結果である。

Arenaはevidenceとclassificationを所有し、project-levelの `Overall Champion`
designation更新は `lisjong-project` へhandoffする。

## 7. No result-driven rescue

result exposure後、同一formal event内で次を禁止する。

```text
add seeds
replace / drop seeds
change rotation plan
change primary metric
switch rank -> score
change CI method / threshold
rerun on another population to break a tie
partial result adoption
```

sample sizeやprecision targetを将来変える場合は、historical resultを救済せず
**new protocol revision**として扱う。

この境界はlockの `no_rescue_boundary` へmachine-readableに記録し、lock strict-read
時に一致を要求する。

## 8. Bundle

```text
<bundle>/
  overall-lock.json      pre-execution lock (write-once)
  comparison.json        既存 ComparisonArtifact そのもの
  overall-result.json    purpose-specific Overall result (write-once)
```

`comparison.json` は既存generic `ComparisonArtifact` をそのまま使う。Overall要件
だけを理由にgeneric artifact schemaを拡張しない。generic artifactが既に持つ
provenance fieldはlockとcross-checkし、Arena revision / lisjong-engine revisionの
ようなOverall固有の強いbindingはlock側が保持する。

`overall-lock.json` は最低限、protocol identity / version、Heuristic participant
binding、Learning participant binding、game mode、exact AABB rotation contract、
ordered 100 seeds、seed count、hanchan count、max_steps、primary statistic、
CI method、classification rule、Arena / lisjong / lisjong-engine revision、
RiichiEnv version、Python version、relevant ML runtime、`result_exposed=false`、
no-rescue boundary、artifact destinationsをbindする。

exact seed valuesはprotocolへhard-codeしない。v1 invariantはseed-block count
(100)だけであり、concrete populationは各formal eventのlockが選ぶ。

## 9. Participant binding

current Championをauto-discoverするregistryは持たない。formal eventのlockが
少なくとも次をbindする。

```text
family (heuristic / learning)
exact policy identity
exact factory / serving binding (module:qualname)
implementation revision
checkpoint / weights identity where applicable
```

execution APIはcallerから `PolicySpec` を受け取り、実行前にlock participant
bindingとのexact一致(identityとfactory binding)をfail-closedで確認する。

## 10. Merged-main execution discipline

formal executionはreview済みmerged implementationを対象とする。lock生成時と実行
直前の双方で、既存 `lisjong_arena._execution_safety` により

```text
clean Arena worktree
exact HEAD revision
main containment
write-once artifact destinations
```

を確認する。PR branch上でformal resultを生成してからmergeする運用にしない。

```text
implementation PR merge
    -> follow-up formal execution Issue
    -> pre-execution lock
    -> one-shot execution
    -> bundle strict verify
```

## 11. Bundle strict verification

`verify` boundaryは次の順に進み、どの段階でもfail closedする。

```text
lock strict read
    -> comparison strict read
    -> lock / comparison plan & provenance cross-binding
    -> raw seat-result validation
    -> seed-block rank statistics re-derive
    -> 95% interval re-derive
    -> secondary diagnostics re-derive
    -> classification re-derive
    -> result artifact comparison
    -> result identity validation
```

result artifactのrecorded statisticsは信用しない。検証は必ずstrict-readした
`comparison.json` のraw seat-resultsから再導出する。したがってresult fieldsだけを
自己整合的に改変してresult identityを再計算しても、raw evidenceとの
re-derivation mismatchで拒否される。

fail closed対象には参加者/family/seed/seed order/game mode/max_steps/rotation・
seat assignment mismatch、partial game、partial seed block、comparison digest
mismatch、provenance mismatch、statistics mismatch、secondary diagnostic mismatch、
classification mismatch、unknown schema/version、missing/unexpected JSON field、
bool-as-int、malformed numeric type、non-finite statistics、overwrite attemptを含む。

## 12. Operator surface

```text
python -m lisjong_arena.overall_champion_aabb lock   --out ... --seeds ... --workers ...
python -m lisjong_arena.overall_champion_aabb run    --lock ...
python -m lisjong_arena.overall_champion_aabb verify --lock ... --comparison ... --result ...
```

`run` はreal 400-hanchan executionを開始するpost-merge operator作業である。CIと
testはexecution境界を差し替え、actual formal evaluationも実RiichiEnvの半荘も
実行しない。

## 13. Non-goals

```text
current Champion pairのformal run (本実装では実行しない)
Overall designation更新
Heuristic / Learning family promotion redesign
Champion registry / auto-discovery
family promotion後のautomatic trigger
new hanchan runner / new backend
generic experiment framework / artifact registry
formal TEST / historical holdout reuse
sequential testing / Bayesian ranking / Elo / Glicko / SPRT
scoreをprimary classifierにすること
result-driven sample extension
```
