# riichi-first-dama-source-v1：R1学習のTRAIN / TEST source（#548の実装）

設計: [lisjong #356](https://github.com/lisbun/lisjong/issues/356)（[改訂・引継ぎ版](https://github.com/lisbun/lisjong/issues/356#issuecomment-6418811229)）。
consumer: [lisjong #360](https://github.com/lisbun/lisjong/issues/360)（reader・ridge・gate Policy・E2 / E3、`docs/r1-value-learning.md`）。
観測・照合・strict verifyは[#544のpilot](riichi-first-dama-branch-544.md)と同じ実装を使う。**実行は後続の実行Issueで行う**
（本Issueでは対局・rehearsal・seed予約・課金をしていない）。DEVELOPMENTのsourceで、強さの結果ではない。

## 1. pilot（#544）から変えた点

| 項目 | pilot `riichi-first-dama-branch-v1` | source `riichi-first-dama-source-v1` |
| --- | --- | --- |
| seed | 544000..544015（1 run） | 4ブロック×22 seeds（下表）。splitごとのallocation |
| verifyで再実行 | 範囲の最小・最大 | **各ブロックの最小・最大** |
| header | pilotのfield | ＋ `allocation_identity`・`artifact_identity`・`block`・`split`（lisjong #360の契約） |
| summary | 分散・σ_u・費用判定・記述のΔ | **Δの統計を持たない**（品質・INVALID・時間だけ）。判定はSOURCE-COMPLETE / STOP-INVALID |
| 開始条件 | 3時間・全工程4時間 | ＋ **累積の収集12時間**。見積もりはTRAIN-1がrehearsal、以後は直前ブロックの実測 |
| TEST | — | 凍結artifactのidentityがないとplanを作れない |

分岐点・D armの介入（R1 wrapper）・Δの定義・INVALID・全席の行動記録・2 seed再実行での完全一致はpilotと同じ。
実装はpilotの`riichi_first_dama`を`ProtocolSpec`でprotocolごとに切り替え（schema・plan・header・summary）、pilotの契約・
保存物は変えない。

## 2. ブロック・split・seed

| block | split | seeds | verifyで再実行 |
| --- | --- | --- | --- |
| TRAIN-1 | TRAIN | 547000..547021 | 547000・547021 |
| TRAIN-2 | TRAIN | 547022..547043 | 547022・547043 |
| TEST-1 | TEST | 547100..547121 | 547100・547121 |
| TEST-2 | TEST | 547122..547143 | 547122・547143 |

- allocationはsplitごと（TRAIN 547000..547043、TEST 547100..547143）。domain `lisjong-engine-project-standard-v1-hanchan-v1`、
  protocol `riichi-first-dama-source-v1`、population `heuristic-candidate-engine-aabb-riichi-first-dama-source-v1`、split `TRAIN` / `TEST`
- **順序**: TRAIN-1 → TRAIN-2 → （lisjong #360でfit・artifact凍結）→ TEST-1 → TEST-2。各ブロックのplanは、先行する全ブロックの
  planを順に渡し、同じ環境・revisionでSOURCE-COMPLETE（SHA256SUMS一致）であることを要求する
- TEST-1 / TEST-2のplanは`--artifact-identity`（lisjong #360で凍結したartifactのidentity）を必須にし、headerへ束縛する。
  TRAINのplanはartifactを持たない
- rehearsalは472005 / 472006（registry外、動作確認だけ）

## 3. 時間・開始条件

| 項目 | 規則 |
| --- | --- |
| 見積もり | 22 × Rの平均wall + n̂ × D armの平均wall（本実行）＋ 2 × Rの平均wall + n̂_verify × D armの平均wall（verify）＋ 900秒。n̂ = max(見積もり元の分岐率, 203/64) × 88、n̂_verify = 同じ率 × 8 |
| 見積もり元 | TRAIN-1はrehearsal（`riichi-first-source-estimate --observed --unobserved`）。以後は**直前ブロック**の実測（`--from-block`） |
| session | 見積もり ≤ min(3時間, 4時間 − そのsessionの経過)（`--session-elapsed-seconds`） |
| 累積 | 先行ブロックの収集時間の合計（`--collected-seconds`、watchdog記録の和）＋ 見積もり ≤ 12時間。合計は先行ブロックのtimingsのrun経過の和以上でなければ拒否 |
| 判定 | 満たさなければNOT-STARTED-BUDGETでplanを作らない（seedを使わない）。planの`resource_limits.wall_seconds`はsessionの上限 |
| 費用の記録 | gate・rehearsalは準備費用として別に記録する。累積の記録はplanの`start_condition`（先行ブロックのplan digest・run経過・合計）にwrite-onceで残る |

## 4. 保存物（ブロックごと、write-once）

`hanchans.json`・`actions.jsonl`・`branches.jsonl`・`features.jsonl`・`summary.json`・`timings.json`・`SHA256SUMS`。schemaは
`arena-riichi-first-dama-source-v1-*`。branchesのheader:

```json
{"allocation_identity": "<split allocation>", "artifact_identity": null | "<frozen artifact>",
 "block": "TRAIN-1", "features_version": "riichi-dama-features-v2", "kind": "header",
 "plan_digest": "...", "protocol_version": "riichi-first-dama-source-v1",
 "schema": "arena-riichi-first-dama-source-v1-branches-v1", "split": "TRAIN",
 "wrapper_version": "riichi-first-dama-branch-wrapper-v1"}
```

summaryは`schema`・`decision`（SOURCE-COMPLETE / STOP-INVALID）・`plan_digest`・`block`・`split`・`invalid`・品質・時間を持ち、
Δの統計・分散・分岐ごとのΔを持たない。lisjong #360のreaderはこのblockを直接読む（testで確認）。
未完了時はpilotと同じく、outputを公開せずstop記録（完了済みtaskを含む）を残し、正式な集計・判定はしない。

## 5. CLI

```bash
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-source-plan \
  --run-kind {allocated,rehearsal} --observation {observed,unobserved} --owner-issue lisbun/lisjong-arena#N \
  [--block TRAIN-1|TRAIN-2|TEST-1|TEST-2 --allocation-identity ID [--artifact-identity A] \
   --time-estimate E --session-elapsed-seconds S --collected-seconds C [--previous-plan P ...]] \
  --limit-enforcement "<OS / process treeの強制方法>" --wheel W \
  --run-output <dir>/output --diagnostics <dir>/diagnostics --output <dir>/plan.json
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-source-run --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-source-verify --plan P --output V
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-source-rehearsal-compare --observed P1 --unobserved P2 --output C
python -m lisjong_arena.heuristic_candidate_engine_aabb riichi-first-source-estimate \
  (--observed P1 --unobserved P2 | --from-block P) --output E
```

## 6. testで固定したこと

[test_engine_aabb_riichi_first_source.py](../tests/test_engine_aabb_riichi_first_source.py)（実engineの対局は使わない。#544のfake play）:
ブロック・split・seed・verify seed、TEST planのartifact必須とTRAINでの拒否、drift、owner、sessionと累積12時間の開始条件の境界、
見積もり元（rehearsal / 直前ブロック）とブロック順、収集時間の下限、header・summaryの契約（Δの統計なし）とstrict verify、
TEST blockのartifact束縛、STOP-INVALID、未完了時のstop記録、直前ブロックからの見積もり、rehearsalの照合と見積もり、
**pinしたlisjong #360のreaderがblockを読めること**（schema・blockの定数の一致を含む）、CLI。pilot（#544）のtestは変更なしでPASS。

## 7. 実行Issueへの引継ぎ（未確定の実値）

1. revision: 本Issueのmerge後Arena main full SHA、lisjong pin `1d14c09baee554e26fcea9865eddca3944cb447f`（#360 / PR #361）、
   engine `91af75e3aa11520c3b0543719dc74bb4c517ee06`、wheel: lisjong main CI run 40526929107の
   `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（301,070 bytes、SHA-256
   `b3473370042a9f6570464ded6f6e4457918395779092c429844f09bacd31477c`、[現行の組み合わせ](lisjong-native-wheel-current.md)）。
   新pinのRust-only gateを取り直す（過去のPASSを流用しない）
2. 環境: WSL2 Ubuntu-24.04の専用user scope（`MemoryMax=4G`・`MemorySwapMax=0`・`OOMPolicy=kill`・`RuntimeMaxSec`）と外側の
   watchdog。新規venvで同revisionのsource・wheel・install identityを確認する。クラウドの追加課金はなし
3. 順序: 準備session（gate・rehearsal・見積もり）→ TRAIN allocationの照合・予約 → TRAIN-1 → TRAIN-2 → lisjong #360でfit・
   artifact凍結（identityを記録）→ TEST allocationの照合・予約 → TEST-1 → TEST-2 → lisjong #360の評価1回。各ブロックは開始条件を
   満たすときだけ開始し、満たさなければ停止して報告する（完了分だけで正式判定しない）
4. 保存先: `C:\Dev\lisjong-artifacts\issue-<実行Issue>-r1-learning-source\`（write-once）
