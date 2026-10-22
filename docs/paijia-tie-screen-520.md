# 同点数牌paijia候補の開発比較: 実行前計画（#520）

Issue: [lisbun/lisjong-arena#520](https://github.com/lisbun/lisjong-arena/issues/520)
（親 lisbun/lisjong-project#84、候補 lisbun/lisjong#330 / PR #331）。API・保存物・判定の正本は
[`engine-aabb-evaluation.md`](engine-aabb-evaluation.md) §9。

> Status: **計画（未承認・未実行）**。rehearsal・seed予約・段階1・段階2・AWSはどれも始めていない。
> 下記のArena revisionは実装PRのmerge後に確定する。実行には個別の承認が要る。結果はDEVELOPMENTで、
> 採用・正式評価ではない。

## 1. 固定する入力

| 項目 | 値 |
| --- | --- |
| lisjong | `0fff8918ec349eeec129907f6a723e2d7314b95a`（#330 / PR #331のsquash merge。Arena pin） |
| A | `placement-aware-speed-call-suited-paijia-tie-v1` = `lisjong.policies:PlacementAwareSpeedCallSuitedPaijiaTiePolicy`（引数なし）、設定 `{}`、学習artifactなし |
| B | `placement-aware-speed-call` = 同じrevisionの `PlacementAwareSpeedCallPolicy`、設定 `{}` |
| lisjong-engine | `91af75e3aa11520c3b0543719dc74bb4c517ee06`、RuleSet全field・game mode・6配置はengine AABB仕様のまま（`rules_digest` `1393f502…a9626`、`protocol_digest` `e60f75d7…adec2`） |
| native wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`、300,955 bytes、SHA-256 `130929229b16b0508a61e77d04c0a219ab65a15f7e79a74d60c43725c0cb8fba`、`SOURCE_REVISION` = lisjong pin、lisjong main push CI [39726880815](https://github.com/lisbun/lisjong/actions/runs/39726880815)（merge SHAのwheel。PR headのwheelではない） |
| Arena | 実装PRのmerge SHA（clean checkout、`main` に含まれること。planが記録し、live gateが実行前後に照合） |
| 実行 | WSL2 Ubuntu 24.04（`Ubuntu-24.04`）、CPython 3.14（uv venv）、Rust backend、worker 1。Intel Core Ultra 7 258V、8 vCPU、メモリ約16.5 GB（#517で確認した同じ機器） |
| 新規課金 | 0（AWSは使わない） |

Bの挙動: 基準revision `ef90245` から `0fff891` までのsource変更は#331だけで、Bが通るのは
`targeted_honor_release_terminal_progression.py` のhelper切り出し（同じ1回の計算からHVA snapshotも返す）である。
lisjong側は選択・analysis・例外が変わらないことをPR #331の回帰testで確認している。Arenaでは
Python全対局の再照合を追加しない（Issue指示）。段階1の「analysisの `champion_action` = 同じ入力でのBの行動」の
照合は、AとBが同じrevisionで整合することの確認で、`ef90245` との行動一致の証明ではない。
旧pin `999553b..ef90245` でPolicy・契約のsourceは変わっていない（学習系だけ）。nativeのsourceも
`999553b..0fff891` で不変で、wheelのbyte列の違いは埋め込む `SOURCE_REVISION` による。

## 2. seeds（提案。予約はしていない）

| 段階 | seeds | population | split |
| --- | --- | --- | --- |
| 段階1 | 520000..520007（8） | `heuristic-candidate-engine-aabb-paijia-tie-activation-520` | `DEVELOPMENT` |
| 段階2 | 520100..520131（32） | `heuristic-candidate-engine-aabb-paijia-tie-screen-520` | `DEVELOPMENT` |

2026-10-22のlive ledger（`seed-registry` authority `59884a2`、56 allocations、revision `c76b3d87…`）で、
全domainのどのallocationとも重ならないことを確認した。registry外の既知seed（472000〜472006、513000）とも
重ならない。予約は承認後に `seed-registry.yml`（`operation=reserve`、owner `lisbun/lisjong-arena#520`、
protocol `arena-heuristic-candidate-engine-aabb-half-v1`、seed domain
`lisjong-engine-project-standard-v1-hanchan-v1`、`arena_revision` = merge SHA）で行い、番号はlive ledgerで
readbackする。段階2のallocationは段階1が `PROCEED` の場合だけ予約する。

## 3. 上限と停止

| 段階 | wall | process tree | 1半荘 |
| --- | --- | --- | --- |
| rehearsal（2 + 12半荘） | 各1時間 | 4 GiB | 600 s |
| 段階1（8半荘） | 2時間（7,200 s） | 4 GiB | 600 s |
| 段階2（192半荘） | 4時間（14,400 s） | 4 GiB | 600 s |

全体の科学計算上限は段階1 + 段階2で6時間。worst caseの192 × 600 s = 115,200 sは4時間を大きく超える。
4時間で全件が終わる保証はなく、超えた場合は全体を止めて `STOP-INCOMPLETE` とし、部分結果を使わない。
K・timeoutを後から縮めない・伸ばさない。

上限の方式（三重。どれかが当たれば止まる）:

1. **OS（process tree）**: `systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 -p OOMPolicy=kill
   -p RuntimeMaxSec=<wall>`。#517で親・spawnした子ともscopeごとkillされることを確認済み（cgroup v2、
   user managerへ `memory` 委譲）。scope終了時の `memory.peak` をprocess tree peakの正本とする
2. **realtimeの見張り**: #517でmonotonicがrealtimeより約401 s短かった（原因未特定、WSL VMの時計差が疑われる）。
   `RuntimeMaxSec` やPythonのmonotonicはVMの停止中に進まない可能性があるので、scopeの外で
   `date +%s`（realtime）を30秒ごとに見て、期限を過ぎたら `systemctl --user stop <unit>` でscopeを止める
   watchdogを併用する（期限 = 開始realtime + wall）
3. **code**: executorの1半荘timeout（600 s）、1半荘ごとの「親 + workerのpeak RSS和」と
   「max(monotonic経過, realtime経過)」の確認（§9.1）。両clockの経過を `timings.json` に記録し、
   verifyの出力に差を示す。差が60 sを超えたら結果とともに報告する（判定は変えない）

段階1の `STOP-BUDGET` は、段階1の計測から段階2を `192 × mean(半荘wall − (A + B判断秒) / 2)` と見込み、
14,400 sの0.8倍（11,520 s）を超える場合である（段階1は4席でA・Bの両方を計算するので、AABBの2席ずつへ換算する）。

## 4. 見込み（#517の同機器・同engineでの実測から）

実測（#517、C0系2 Policy、Rust、192半荘）は平均42.65 s / 半荘（最大70.44 s）、2,068局、cgroup peak 825 MB、
raw 15.9 KB / 半荘だった。Aは「Bの打牌判断 + 最終tieの数牌のpaijia」なので、判断時間はBとほぼ同じと見込む。

| 実行 | 見込み（Policy時間が支配的と仮定） | 上限 |
| --- | --- | --- |
| rehearsal 段階1（2半荘、4席B + shadow A） | 約3分 | 1時間 |
| rehearsal 段階2（12半荘） | 約9分 | 1時間 |
| 段階1（8半荘） | 約11分（1半荘はshadowで約2倍の約85 s） | 2時間 |
| 段階2（192半荘） | 約2.3時間（約8,200 s）。`STOP-BUDGET` の線は11,520 s | 4時間 |
| verify / report / 回収 / hash照合 | 各数秒 | — |

容量: raw約3 MB、`policy-analysis.json` は判断秒の列で数MB、段階1の `shadow.json` は1 MB前後の見込み。
メモリ: 1 GB未満の見込み（上限4 GiB）。これらは見込みで、段階1の結果で段階2の見込みを更新する。

## 5. 保存先

| 場所 | 用途 |
| --- | --- |
| WSL `~/ps-520/arena` | Arena merge SHAのclean checkout |
| WSL `~/ps-520/venv` | uv venv（Arena + pin + wheel） |
| WSL `~/ps-520/run/<run>/{plan.json, output/, diagnostics/}` | `<run>` = `rehearsal-activation` / `rehearsal-screen` / `activation` / `screen` |
| `C:\Dev\lisjong-artifacts\issue-520-paijia-tie-screen\` | scripts・log・plan・ledger snapshot・回収したoutput・SHA-256一覧・verify出力 |
| `C:\Dev\lisjong-artifacts\native\0fff8918…\` | wheel・`SHA256SUMS`・`BUILD-INFO.txt`（取得済み、SHA-256照合済み） |

校正（#517）の `~/cal-517` checkout・pin・planは変更せず、同時に重い実行をしない。

## 6. 手順（承認後）

1. setup: clean checkout、`pip install -e arena[dev]`、`verify-wheel`、wheelを `--force-reinstall`、
   `environment_verify --project pyproject.toml`（CONSISTENT）、`_lisjong_native.SOURCE_REVISION` = `0fff891`
2. OS上限の再確認（#517の `os-limit-check` と同じ3 case + realtime watchdogの動作確認）
3. **記録経路の確認（rehearsal）**: `paijia-plan --run-kind rehearsal` で段階1・段階2を作り、
   それぞれ `run` → 別processの `verify` → 回収 → hash照合 → 段階2は `report`。
   既知の開発用seed 472005・472006だけを使い、未使用seedsは使わない（下のチェックリスト）
4. 段階1のseedsを予約・readback → `paijia-plan --stage activation --run-kind allocated`
   （段階2の上限もここで固定）→ `paijia-activation-run` → 別processの `paijia-activation-verify` → 回収・hash照合
5. 判定が `PROCEED` の場合だけ: 段階2のseedsを予約・readback → `paijia-plan --stage screen
   --run-kind allocated --activation-plan <段階1のplan>` → `paijia-screen-run` → verify → `paijia-screen-report` →
   verify（reportも再導出）→ 回収・hash照合。`STOP-*` なら段階2を行わない
6. 使ったallocationをCOMMITTEDにし、結果・停止理由を#520へ記録、project #84とlisjong #297へ次actionを渡す

## 7. 局単位記録チェック（AGENTS.md・[AABB仕様](heuristic-candidate-aabb-half.md)「新規評価の局単位記録チェック」）

| 項目 | 対応 |
| --- | --- |
| 同じ実行中に各局の客観結果を保存 | 段階2は正式runと同じ `write_kyoku_records`（第1層・第2層、両方 `VALID` を要求）。段階1は強さ比較でなく、局数・終了理由・final_points・`CompletedMatch` digestを `hanchans.json` に保存 |
| seed・配置・局・seat・Policy identityへの結び付け | 第1層のK1 / K2（schedule照合）、bindings = plan digest・environment digest。Policy観測は別fileでseed・placement・seatが一致し、判断数がPolicy呼び出し数と一致 |
| 和了率・放銃率・平均和了打点・平均放銃失点の定義・母数 | #513の副指標（率の分母は参加seat局、seed blockで合算、seed等重み。打点・失点は本場を除く手の点、全seed合算の分子 / 分母、分母0はnull） |
| 集計単位 | seed block（局・seat-resultを独立標本にしない） |
| 既知development入力で取得→保存→回収→検証→集計 | 手順3のrehearsal（同じrunner・revision・backend） |
| 回収対象と完全性 | outputの全file（raw・analysis・summary・timings・report）のSHA-256一覧を生成側と回収先で照合し、別processのverifyで再導出が一致すること |

## 8. 範囲外

Python全対局比較の自動追加、AWS、追加seed・第二候補、閾値・Kの変更、#517の流用（SD・必要K）、
Champion採用・既存改修との束ね、汎用runner・探索基盤。
