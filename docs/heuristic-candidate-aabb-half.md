# Heuristic candidate AABB half-game protocol v1 (#375)

Issue: [lisbun/lisjong-arena#375](https://github.com/lisbun/lisjong-arena/issues/375)

Protocol identity:

```text
arena-heuristic-candidate-aabb-half-v1
```

実装は `src/lisjong_arena/heuristic_candidate_aabb/`、AWS実行は
`scripts/aws/run-heuristic-candidate-aabb-375.ps1` +
`scripts/aws/bootstrap-heuristic-candidate-aabb-375.sh`。

> Status: contract + executor only。formal 400-hanchan eventはmerge後に
> operatorがseed allocation → AWS preflight → launch → collectの順で1回だけ実行する。

## 1. 位置づけ

Heuristic candidateとcurrent Heuristic Championを半荘で比べる、**family-internal**
なcandidate評価である。cross-family Overall判定
([`arena-overall-champion-aabb-half-v1`](overall-champion-aabb-half.md))とは
participant family・primary metric・classificationの意味が異なるため、optionを
追加して流用せず独立したprotocol identityを持つ。

実行substrateは既存generic AABB comparison (`ComparisonPlan` /
`run_comparison(_parallel)` / `ComparisonArtifact`)、participant bindingは
Overall protocolの `ParticipantBinding`、paired統計は `paired_evaluation`、
seed authorityは [Seed Registry](seed-registry.md) をreuseする。

## 2. Fixed shape

```text
A = candidate   PlacementAwareSpeedCallPolicy               (lisjong 2a9debe…)
B = incumbent   TargetedHonorReleaseTerminalProgressionPolicy (same revision)
game mode       4p-red-half
matchup         AABB, 4 rotations / seed (A/Bが各seatを2回ずつ担当)
seed blocks     100 (statistical N)
hanchan         400
max_steps       10,000
```

participantのidentity / factory / revisionはprotocolへhard-codeせず、lockがbindする
(#375のbootstrapは上記2つをbindする)。checkpointを持つparticipantはv1では受理しない。

## 3. Primary metric — uma/oka final score

各seat-resultのfinal scoreはprotocol invariantとして固定する。

```text
配給原点 25000 / 返し 30000
final_score = (final_points - 30000) / 1000 + uma[rank] + oka[rank]
uma = (+30, +10, -10, -30)
oka = (+20, 0, 0, 0)
```

rankと最終素点はengine (`LocalGameResult`) の結果をそのまま使い、同点処理や供託の
扱いをprotocol側で再定義しない。計算は整数(1/1000単位)で行う。

seed `s` の4 rotationsについて:

```text
A(s) = candidate 8 seat-resultsのmean final score
B(s) = incumbent 8 seat-resultsのmean final score
D(s) = A(s) - B(s)          (> 0 -> candidate advantage)
```

95% interval は `mean D ± 1.96 * SD(N-1) / sqrt(N)`、N = 100 seed blocks。

## 4. Classification

```text
interval lower > 0   -> CANDIDATE SUPERIOR
interval upper < 0   -> CHAMPION SUPERIOR
otherwise            -> INCONCLUSIVE   (境界0を含む)
invalid evidence     -> STOP / INVALID (result artifactへ書かない)
```

secondary diagnostics (平均順位とその差、1〜4着回数、平均素点、seat別成績) は
primary classificationを上書きしない。Champion昇格判断はこのprotocolの外
(lisjong-project governance) で扱う。

## 5. Seed allocation / lock / no-rescue

- populationはSeed Registryの `riichienv-4p-red-half-hanchan-v1` domainで、
  owner `lisbun/lisjong-arena#375`、protocol `arena-heuristic-candidate-aabb-half-v1`、
  population `heuristic-candidate-aabb-375`、split `FORMAL-EVAL` のallocationで
  なければならない。lockはallocation bindingをlive ledgerへ照合してbindし、
  runの前後でも再照合する
- lockはmerged-main clean worktree、internal VCS dependency environment、
  execution provenance、participant implementation revision、write-once
  destinationをfail closedで確認する
- result exposure後のseed追加・差し替え、metric / uma / oka / CI方法の変更、
  partial adoptionはしない(`no_rescue_boundary`)

## 6. Bundle

```text
candidate-lock.json     pre-execution lock (write-once)
comparison.json         既存 ComparisonArtifact
candidate-result.json   purpose-specific result (write-once)
```

`verify` はrecorded statisticsを信用せず、raw seat-resultsから再導出した結果との
完全一致だけを成功とする。

## 7. Operator runbook (AWS)

前提: 本実装がmergeされたArena `main` のclean checkout、`.venv` にその
revisionをinstall済み、AWS CLI v2 / PowerShell 7、既存の
`lisjong-riichilab-smoke-ec2` role と `lisjong-riichilab-smoke-305` security group。

1. **Seed allocation** (Arena Seed Registry workflow、ユーザー操作)

   ```text
   gh workflow run seed-registry.yml \
     -f operation=reserve \
     -f owner_issue=lisbun/lisjong-arena#375 \
     -f protocol=arena-heuristic-candidate-aabb-half-v1 \
     -f seed_domain=riichienv-4p-red-half-hanchan-v1 \
     -f purpose="#375 heuristic candidate AABB half formal event" \
     -f population=heuristic-candidate-aabb-375 \
     -f split=FORMAL-EVAL \
     -f seeds=<START>..<END> \
     -f arena_revision=<merged main sha> \
     -f protocol_revision=arena-heuristic-candidate-aabb-half-v1 \
     -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/375
   ```

   その後 `operation=show` でallocation bindingを取得し、JSON fileへ保存する。

2. **Preflight (課金なし)** — local merged revision / published bootstrap digest /
   live ledgerに対するallocation binding / AWS discovery / Pricing API / quota /
   dry-run / cost planを確認し、`plan.json` を書く

   ```powershell
   .\scripts\aws\run-heuristic-candidate-aabb-375.ps1 -Action Preflight `
     -ArenaRevision <merged main sha> -Seeds <START>:<END> `
     -AllocationBindingPath <binding.json> -AwsProfile <profile>
   ```

3. **Launch (課金あり)** — `plan.json` の予測コスト・worst-case exposureを確認して
   から実行する。bucket → instance → fail-safe arm → SSM submit → Collect

   ```powershell
   .\scripts\aws\run-heuristic-candidate-aabb-375.ps1 -Action Launch `
     -ArenaRevision <same sha> -Seeds <START>:<END> `
     -AllocationBindingPath <binding.json> -AwsProfile <profile>
   ```

4. **Progress / reattach**

   ```powershell
   .\scripts\aws\status-run.ps1 -RunId <run-id> -AwsProfile <profile>
   .\scripts\aws\run-heuristic-candidate-aabb-375.ps1 -Action Collect -RunId <run-id> -AwsProfile <profile>
   ```

   Collectはinstanceをterminateし、bundleをdownloadしてremote completion recordの
   SHA-256と照合し、**localで `verify` を再実行**してclassification / result
   identityがremoteと一致することを確認する。その後bucketを削除し、残存resource
   sweepを行う。

5. 結果(primary / 95% interval / classification / secondary / result identity /
   実際のruntime・cost)を#375へ記録し、allocationを `commit` する。

### Defaults and cost bound

```text
region            ap-northeast-1
instance          c7i.8xlarge On-Demand (32 vCPU), 32 workers
planning basis    ~530 s / hanchan / worker (#202 local), slowdown [0.6, 2.0]
fail-safe         23,400 s (6.5 h) from launch, boot timer + SSM timer
budget            USD 15 (worst-case exposure + margin)
```

Preflightは現在のAWS Pricing APIでinstance / gp3を価格付けし、fail-safe window全体の
worst-case exposure + marginがbudget以下であること、fail-safeが予測billable最大の
1.5倍以上であること、worker当たりmemoryが1,536 MiB以上であることを要求する。
