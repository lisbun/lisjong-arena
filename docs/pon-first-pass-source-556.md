# pon-first-pass-source-v1：P1学習のTRAIN / TEST source（#556の実装）

設計: [lisjong #365](https://github.com/lisbun/lisjong/issues/365)（DESIGN-READY、[設計案](https://github.com/lisbun/lisjong/issues/365#issuecomment-6599819983)・
[予算と配分](https://github.com/lisbun/lisjong/issues/365#issuecomment-6600146234)）。
consumer: [lisjong #366](https://github.com/lisbun/lisjong/issues/366)（reader・10列encoding・ridge・E2 / E3、`docs/p1-value-learning.md`）。
観測・照合・field分離・D armの測定値・strict verifyは[#553のpilot](pon-first-pass-branch-553.md)と同じ実装を使う。
**実行は後続の実行Issueで行う**（本Issueでは対局・rehearsal・seed予約・課金をしていない）。DEVELOPMENTのsourceで、強さの結果ではない。

## 1. pilot（#553）から変えた点

| 項目 | pilot `pon-first-pass-branch-v1` | source `pon-first-pass-source-v1` |
| --- | --- | --- |
| seed | 553000..553023（1 run） | 6ブロック×40 seeds（下表）。splitごとのallocation |
| verifyで再実行 | 範囲の最小・最大 | **各ブロックの最小・最大** |
| header | pilotのfield | ＋ `allocation_identity`・`artifact_identity`・`block`・`split`（lisjong #366の契約） |
| summary | 分散・配分判定・記述のΔ | **Δの統計を持たない**（品質・INVALID・候補 / 成立 / 取消の件数・時間・分岐前の割合だけ）。判定はSOURCE-COMPLETE / STOP-INVALID |
| 開始条件 | 3時間・全工程4時間 | ブロックごとに3時間・session 4時間（#553の`block_start_condition`）＋ **累積の収集14時間** |
| TEST | — | 凍結artifactのidentityがないとplanを作れない |

候補（門前の最初のPon）・成立 / ロン取消の扱い・D armの介入（P1 wrapper）・Δの定義・INVALID・`decision_input` / `selection`の
field分離・全席の行動記録・2 seed再実行での完全一致はpilotと同じ。実装はpilotの`pon_first_pass`を`ProtocolSpec`で
protocolごとに切り替え（schema・plan・header・summary）、pilotの契約・保存物は変えない（pilotのtestは変更なしでPASS）。

## 2. ブロック・split・seed

| block | split | seeds | verifyで再実行 |
| --- | --- | --- | --- |
| TRAIN-1 | TRAIN | 556000..556039 | 556000・556039 |
| TRAIN-2 | TRAIN | 556040..556079 | 556040・556079 |
| TRAIN-3 | TRAIN | 556080..556119 | 556080・556119 |
| TEST-1 | TEST | 556200..556239 | 556200・556239 |
| TEST-2 | TEST | 556240..556279 | 556240・556279 |
| TEST-3 | TEST | 556280..556319 | 556280・556319 |

- allocationはsplitごと（TRAIN 556000..556119、TEST 556200..556319）。domain `lisjong-engine-project-standard-v1-hanchan-v1`、
  protocol `pon-first-pass-source-v1`、population `heuristic-candidate-engine-aabb-pon-first-pass-source-v1`、split `TRAIN` / `TEST`。
  予約直前に全domainのallocationと照合し、重複があれば開始しない（別seedへ移さない）
- **順序**: TRAIN-1 → TRAIN-2 → TRAIN-3 → （lisjong #366でfit・artifact凍結）→ TEST-1 → TEST-2 → TEST-3。各ブロックのplanは、
  先行する全ブロックのplanを順に渡し、同じ環境・revisionで次の両方を満たすことを要求する
  1. runの出力がSOURCE-COMPLETE（SHA256SUMS一致）
  2. そのplan・出力に対応する**strict verifyの成功記録**（planのdiagnosticsの`verify.json`）がある。記録の欠落、別planの記録、
     出力file・summary・verify seed・実行上限との不一致、verifyの失敗記録（`verify-failed.json`）のいずれでも次のplanを作らない
- allocated blockのverifyは1回だけ: 開始記録`verify-started.json`を排他的に作ってから行う。成功なら`verify.json`、最小・最大seedの
  再実行の不一致・保存物の不整合・run + verifyの上限超過なら`verify-failed.json`を残して止まり、再実行しない（結果を見て救済しない）
- TESTのplanは`--artifact-identity`（lisjong #366で凍結したartifactのidentity）を必須にし、headerへ束縛する。TRAINのplanは
  artifactを持たない。**TEST-2・TEST-3はTEST-1と同じartifact**を要求する（先行ブロックの記録が引き継ぐidentityと照合し、
  違えばplanを作らない）
- rehearsalは472005 / 472006（registry外、動作確認だけ）

## 3. 時間・開始条件

| 項目 | 規則 |
| --- | --- |
| 見積もり | c = R̄ + r̂·D̄、r̂ = 4 × max(見積もり元の成立分岐 / seat-hanchan, 116/96)。ブロック = (40 + 2)c + 900秒（本実行40 seeds＋verify 2 seeds＋回収） |
| 率の下限 | 116/96はP1 pilot（#555）の成立率。rehearsal（2 seeds）の少ない分岐で時間を過小に見積もらないため |
| 見積もり元 | TRAIN-1はrehearsal（`pon-first-source-estimate --observed --unobserved`）。以後は**直前ブロック**の実測（`--from-block`） |
| ブロック・session | 見積もり ≤ min(3時間, 4時間 − そのsessionの経過)（#553の`block_start_condition`、`--session-elapsed-seconds`） |
| 累積 | 先行ブロックの収集時間の合計（`--collected-seconds`、watchdog記録の和）＋ 見積もり ≤ **14時間**（50,400秒）。合計は先行ブロックのrun経過＋verify経過（記録値）の和以上でなければ拒否 |
| 判定 | 満たさなければNOT-STARTED-BUDGETでplanを作らない。**seed数（40）を縮めない** |
| 実行上限 | 生成・verify・回収を合わせた上限 = **min(3時間, 4時間 − sessionの経過, 14時間 − 累積)**（planの`start_condition.limit_seconds`・`resource_limits.wall_seconds`）。例: 累積11時間45分なら、新しいsessionでも上限は2時間15分。runはこの上限で止まり、verifyはrun経過＋verify経過がこの上限を超えたら失敗記録を残す |
| 費用の記録 | gate・rehearsalは準備費用として別に記録する。累積の記録はplanの`start_condition`（先行ブロックのplan digest・run経過・合計）にwrite-onceで残る |

## 4. 保存物（ブロックごと、write-once）

`hanchans.json`・`actions.jsonl`・`branches.jsonl`・`summary.json`・`timings.json`・`SHA256SUMS`。schemaは
`arena-pon-first-pass-source-v1-*`。branches（actionsも同じfield）のheader:

```json
{"allocation_identity": "<split allocation>", "artifact_identity": null | "<frozen artifact>",
 "block": "TRAIN-1", "call_analysis_version": "call-value-shadow-v1", "kind": "header",
 "plan_digest": "...", "protocol_version": "pon-first-pass-source-v1",
 "schema": "arena-pon-first-pass-source-v1-branches-v1", "split": "TRAIN",
 "wrapper_version": "pon-first-pass-branch-wrapper-v1"}
```

候補行はpilotと同じfield（`decision_input`は判断時点の値、`selection`は標本の選択だけ）。summaryは`schema`・`decision`
（SOURCE-COMPLETE / STOP-INVALID）・`plan_digest`・`block`・`split`・`invalid`・`population`（候補 / 成立 / ロン取消）・
`pre_branch`（D armの分岐前の割合）・品質（分岐後の鳴き）・時間を持ち、Δの統計・分散・分岐ごとのΔ・配分判定を持たない。
lisjong #366のreaderはこのblockを直接読む（testで確認）。未完了時はpilotと同じく、outputを公開せずstop記録（完了済みtaskを含む）を
残し、正式な集計・判定はしない。

## 5. CLI

```bash
python -m lisjong_arena.heuristic_candidate_engine_aabb pon-first-source-plan \
  --run-kind {allocated,rehearsal} --observation {observed,unobserved} --owner-issue lisbun/lisjong-arena#N \
  [--block TRAIN-1|...|TEST-3 --allocation-identity ID [--artifact-identity A] \
   --time-estimate E --session-elapsed-seconds S --collected-seconds C [--previous-plan P ...]] \
  --limit-enforcement "<OS / process treeの強制方法>" --wheel W \
  --run-output <dir>/output --diagnostics <dir>/diagnostics --output <dir>/plan.json
python -m lisjong_arena.heuristic_candidate_engine_aabb pon-first-source-run --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb pon-first-source-verify --plan P   # allocatedはdiagnostics/verify.jsonへ。rehearsalは[--output V]
python -m lisjong_arena.heuristic_candidate_engine_aabb pon-first-source-rehearsal-compare --observed P1 --unobserved P2 --output C
python -m lisjong_arena.heuristic_candidate_engine_aabb pon-first-source-estimate \
  (--observed P1 --unobserved P2 | --from-block P) --output E
```

## 6. testで固定したこと

[test_engine_aabb_pon_first_pass_source.py](../tests/test_engine_aabb_pon_first_pass_source.py)（実engineの対局は使わない。#553のfake play）:
6ブロックの固定値（seed範囲・各40 seeds・verify seed）、splitのallocation、TEST planのartifact必須とTRAINでの拒否、drift、owner、
率の下限と見積もりの再導出、3時間・session 4時間・累積14時間のそれぞれの境界とseed数を縮めないこと、実行上限への累積残りの反映
（累積11時間45分で2時間15分）、見積もり元（rehearsal / 直前ブロック）とブロック順、収集時間の下限（run＋verify）、先行planとoutputの束縛、
先行ブロックのverify成功記録（欠落・別planの記録・再実行不一致・上限超過で拒否、verifyの再実行拒否）、TEST-2・TEST-3のartifact一致
（A→A受理、A→B拒否）、header・summaryの契約（Δの統計なし）とstrict verify、
TEST blockのartifact束縛、STOP-INVALID、未完了時のstop記録、直前ブロックからの見積もり、rehearsalの照合と見積もり（pilotの
見積もりはsourceのplanを拒否）、**pinしたlisjong #366のreaderがblockを読めること**（schema・block・seedの定数の一致を含む）、CLI。

## 7. 実行Issueへの引継ぎ（未確定の実値）

1. revision: 本Issueのmerge後Arena main full SHA、lisjong pin `a341257f199e284a3519e17b458e06ced9f04c67`（#366 / PR #367）、wheel: lisjong main CI run 41655510786の
   `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（SHA-256 `01d2476cf433f1a3be3d336679366364929d84299f439b19a83f07a087ebd1d5`、[現行の組み合わせ](lisjong-native-wheel-current.md)）。新pinのRust-only gateを取り直す（過去のPASSを流用しない）
2. 環境: WSL2 Ubuntu-24.04の専用user scope（`MemoryMax=4G`・`MemorySwapMax=0`・`OOMPolicy=kill`・`RuntimeMaxSec`）と外側の
   watchdog。新規venvで同revisionのsource・wheel・install identityを確認する。クラウドの追加課金はなし
3. 順序: 準備session（gate・rehearsal・見積もり）→ TRAIN allocationの照合・予約 → TRAIN-1 → TRAIN-2 → TRAIN-3 → lisjong #366で
   fit・artifact凍結（identityを記録）→ TEST allocationの照合・予約 → TEST-1 → TEST-2 → TEST-3 → lisjong #366の評価1回。
   各ブロックは開始条件を満たすときだけ開始し、満たさなければ停止して報告する（完了分だけで正式判定しない）
4. 保存先: `C:\Dev\lisjong-artifacts\issue-<実行Issue>-p1-learning-source\`（write-once）
