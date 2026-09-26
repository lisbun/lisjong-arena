# L0.3 outcome-Q paired-strength protocol v1 (#385)

Issue: [lisbun/lisjong-arena#385](https://github.com/lisbun/lisjong-arena/issues/385)
（parent [lisbun/lisjong-project#79](https://github.com/lisbun/lisjong-project/issues/79) Step F）

Protocol identity:

```text
arena-l0.3-outcome-q-paired-strength-v1
```

実装は `src/lisjong_arena/l03_paired_strength/`。protocol invariantは
`protocol.py`、lockは`lock.py`、単一game境界とschedule executorは
`execution.py`、統計・terminal interpretation・result verifyは`result.py`を正本とする。

> Status: protocol freeze + pre-execution lock tooling（Step F）。Step Gのfresh
> paired executionはlock strict readback PASS後のoperator作業であり、このPRでは
> strength seedを予約・実行せず、paired outcomeを一切exposeしていない。

## 1. Comparison

```text
candidate focal   SemanticEnvelopeOffensePolicy + frozen outcome-Q residual runtime
baseline focal    SemanticEnvelopeOffensePolicy + ConstantResidualRuntime（canonical-first）
other 3 seats     ConstantResidualRuntime（両armで同一、game / seatごとにfresh）
```

paired arm間の差はfocal seatのresidual runtimeだけである。frozen identity:

| item | value |
| --- | --- |
| outcome-Q artifact identity | `30874a8eae2c248d1c45a31fe273306fb52cdaa664e8e61e8b4652b5b5bbb7d3` |
| artifact `manifest.json` sha256 | `8cdb44aa1f7dc0e2c5a86dcba2342b51f4c2a14f7a489e179ecb1d485dde1393` |
| artifact `weights.f32` sha256 | `07e72778def521fe1dd19113ff7c88c1a7614201c0c2d7a1a10ff597891157c8` |
| candidate runtime identity | `4982511841c5bf5748a14f6905ab6bd9d00c23f1980b9ce61bb23afba238c2f3` |
| baseline / opponent runtime identity | `725ed52560bed62934b477d35bfc4d6b7805909a95d2c095b280cd6e1d5fc257` |
| selection policy | `lisjong-offense-l0.2-semantic-envelope-v1` |
| training source（provenanceのみ） | `230606f9304f77843d11efc65874ea04b2319ed236d81445265332d26b380cc4` |

## 2. Backend / rules / revisions

```text
backend         lisjong-engine（RiichiEnv strength executionは混ぜない）
rules           {"constructor": "RuleSet.default", "name": "project-standard-v1", "version": 1}
lisjong         c6ab5d68c51b50494cbfed45a2ba699cddab1256   Step D training / Step E qualification revision
lisjong-engine  96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b   C0 / C2 source lineage revision
torch           2.13.0（local version suffixは許容）
Arena           lockが記録するmerged-main clean HEAD（seed allocationのarena_revisionと一致必須）
```

lisjong / lisjong-engineはrepository全体の`pyproject.toml` pinではなく、このprotocolが
installed `direct_url.json`のexact VCS revisionとして照合する（#370 / #372と同じ方式）。

## 3. Population / pairing

```text
seed domain     lisjong-engine-project-standard-v1-hanchan-v1
owner           lisbun/lisjong-arena#385
protocol        arena-l0.3-outcome-q-paired-strength-v1
population      l0.3-outcome-q-paired-strength
split           STRENGTH-EVAL
seeds           931000..931999（1,000 seed blocks）
```

- freshness: lockはlive ledgerの**全domain**の他allocationと、registry外diagnostic seed
  `910000..910399`との非重複を要求する。CALIBRATION `920000..920015`、TRAIN
  `920016..920435`、SELECT `920436..920543`とも重ならない
- schedule: seed昇順の各blockで focal seat 0, 1, 2, 3、各paired unit内は candidate →
  baseline。global game ordinalはこのschedule上のindex（8,000 hanchan）。lockは
  schedule全体のcanonical JSON digestをbindする
- 各paired unitのcandidate / baselineは同じgame seed・同じfocal seat・同じopponent runtime
- 開発中の実engine smokeはdiagnostic seedだけで行った。唯一の例外として、当初候補だった
  seed `930000`をbaseline armだけで1 hanchan実行した（candidate armなし、paired outcomeなし）
  ため、populationを`931000..931999`へずらし`930000`は使わない

## 4. Budget

```text
seed blocks      1,000（statistical N）
paired units     4,000
hanchan          8,000
```

rationale（result exposure前に固定）: lisjong-engineで約50 s / hanchan / worker
（C2 generation 528 hanchan / 24,939 sにStep Eのoutcome-Q discard runtimeを加味）、
約110 CPU-hour。seed-block SD 8–10 ptを計画上の仮定とすると、SEは約0.25–0.32 pt、
80% powerのminimum detectable effectは約0.7–0.9 pt / hanchan。SDは公開済みdataの
推定値ではなく計画仮定であり、result exposure後にbudgetを変更しない。

## 5. Primary endpoint / uncertainty

```text
final score    lisjong-engine CompletedMatch.final_score.for_seat(focal).final_points
               （RuleSet.default()のuma / oka / 箱下を含むengine精算値。protocolで再計算しない。
                 内部単位 1 = 0.1 pt）
paired unit    Δ(s, f) = candidate focal final_points - baseline focal final_points
seed block     D(s) = mean_{f=0..3} Δ(s, f)
point estimate mean D
interval       two-sided normal-approximation 95%: mean D ± 1.96 · SD(N-1) / sqrt(N)、N = 1,000
```

statistical Nは1,000 seed blocksであり、4,000 paired unitや8,000 hanchanではない。

## 6. Invalid pair / minimum support

- engine / Policy / runtime例外、またはstrict validationに失敗したgame recordが1件でも
  あればevent全体を`STOP / INVALID`とする
- invalid pairはdrop・skip・replace・re-seed・selective rerunしない。partial resultは採用しない
- 必要なvalid paired unitは4,000 / 4,000
- result artifactを書く前にabortした実行はoutcomeをexposeしない。再実行はlock・seed・
  destinationを変えずにlocked schedule全体をやり直す場合だけ許す
- 実行前後のlive target（Arena / lisjong / lisjong-engine / torch / Python、artifact bytes /
  identity、seed allocation state）がlockと異なれば`STOP / INVALID`

## 7. Terminal interpretation

```text
valid event かつ interval lower > 0    -> OUTCOME-AWARE RESIDUAL OFFENSE IMPROVED
valid event かつ interval lower <= 0   -> OUTCOME-AWARE RESIDUAL OFFENSE NOT ESTABLISHED
invalid evidence                        -> STOP / INVALID（result artifactを書かない）
```

secondary diagnostics（armごとのfocal平均final pt、平均順位・順位分布、平均素点、
win / deal-in / riichi供託 / kyoku数、final_points vectorが同一のpaired unit数）は
primaryを置き換えず、retraining / HPO / seed追加 / envelope変更の根拠にしない。

## 8. Lock / result

```text
paired-strength-lock.json   pre-execution lock（write-once、result_exposed=false）
result.json                 raw 8,000 game records + seed blocks + summary + classification（write-once）
```

lockはprotocol block全体、schedule digest、live seed allocation binding（+ allocationの
`arena_revision` / `protocol_revision`）、execution target、installed environment、
candidate artifact digest / identity、baseline runtime identity、result destination、
no-rescue boundary、`result_exposed=false`をbindし、canonical JSON digestを
`lock_identity`とする。

`verify-lock`（strict readback）は同じinputからlive環境でpayloadを再構成し、lockとの
完全一致とresult destination未作成を要求する。`verify`はrecorded statisticsを信用せず、
raw recordから再導出したresultとの完全一致だけを成功とする。

## 9. Operator runbook

前提: 本実装がmergeされたArena `main`のclean checkout（HEAD = merge commit）。

1. **Seed allocation**（Arena Seed Registry workflow）

   ```text
   gh workflow run seed-registry.yml \
     -f operation=reserve \
     -f owner_issue=lisbun/lisjong-arena#385 \
     -f protocol=arena-l0.3-outcome-q-paired-strength-v1 \
     -f seed_domain=lisjong-engine-project-standard-v1-hanchan-v1 \
     -f purpose="#385 L0.3 outcome-Q vs canonical-first paired strength" \
     -f population=l0.3-outcome-q-paired-strength \
     -f split=STRENGTH-EVAL \
     -f seeds=931000..931999 \
     -f arena_revision=<merged main sha> \
     -f protocol_revision=arena-l0.3-outcome-q-paired-strength-v1 \
     -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/385
   ```

   `operation=show`でallocation bindingを取得してJSON fileへ保存し、live ledger
   （`seed-registry` branchの`src/lisjong_arena/seed-ledger.json`）も保存する。

2. **実行環境**（実行host上、Python 3.14）

   ```text
   python -m pip install -e <arena checkout>
   python -m pip install --force-reinstall --no-deps \
     "lisjong @ git+https://github.com/lisbun/lisjong.git@c6ab5d68c51b50494cbfed45a2ba699cddab1256" \
     "lisjong-engine @ git+https://github.com/lisbun/lisjong-engine.git@96b9796c76ef5db8f3968f689a1ca6f3dfc9aa3b"
   python -m pip install torch==2.13.0
   ```

   candidate artifact directory（`manifest.json` / `weights.f32`のみ）を配置する。

3. **Lock**（実行hostで作る）

   ```text
   python -m lisjong_arena.l03_paired_strength lock \
     --out <dir>/paired-strength-lock.json --artifact <artifact dir> \
     --seed-ledger <live ledger> --allocation-binding <binding.json> \
     --result <dir>/result.json
   ```

4. **Strict readback**（`LOCK STRICT READBACK PASS`、`result_exposed=false`を確認）

   ```text
   python -m lisjong_arena.l03_paired_strength verify-lock \
     --lock <dir>/paired-strength-lock.json --artifact <artifact dir> \
     --seed-ledger <live ledger>
   ```

   lock fileのSHA-256と`lock_identity`を#385へ記録し、reviewを受けてからStep Gへ進む。

5. **Step G**（別Issueでauthorizeされた後だけ）

   ```text
   python -m lisjong_arena.l03_paired_strength run \
     --lock <lock> --artifact <artifact dir> --seed-ledger <live ledger> \
     --workers <N> --progress
   python -m lisjong_arena.l03_paired_strength verify --lock <lock> --result <result>
   ```

   `--progress`は件数だけを表示する。worker数はresultに影響しない（各hanchanは
   seed / seat / runtimeだけで決まる）。完了後、allocationを`commit`する。
