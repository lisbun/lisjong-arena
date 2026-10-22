# 凍結paijia候補の混成相手focal比較: 実行前計画（#522）

Issue: [lisbun/lisjong-arena#522](https://github.com/lisbun/lisjong-arena/issues/522)
（親 lisbun/lisjong-project#84、候補 lisbun/lisjong#330、前段 #520 / PR #521）。API・保存物・判定の正本は
[`engine-aabb-evaluation.md`](engine-aabb-evaluation.md) §10。

> Status: **計画（未承認・未実行）**。rehearsal・seed予約・本比較・AWSはどれも始めていない。
> Arena revisionは実装PRのmerge後に確定する。実行には個別の承認が要る（#520の承認は流用しない）。
> 結果はDEVELOPMENTで、採用・正式評価ではない。

## 1. 固定する入力

| 項目 | 値 |
| --- | --- |
| lisjong | `0fff8918ec349eeec129907f6a723e2d7314b95a`（全Policy。Arena pinは上げない） |
| A | `placement-aware-speed-call-suited-paijia-tie-v1`（#330の候補、変更・閾値調整なし） |
| B | `placement-aware-speed-call`（同revisionの現Champion。歴史的固定C0ではない） |
| O1 / O2 / O3 | `placement-aware-speed-call` / `targeted-honor-release-terminal-progression` / `placement-aware-speed-call-kobalab-0004-belief-paijia`（O3は未昇格の旧候補を固定相手としてだけ使う） |
| 設定・artifact | 全員 `{}`、学習artifactなし |
| lisjong-engine | `91af75e3aa11520c3b0543719dc74bb4c517ee06`、RuleSet全field・game mode・終了条件は#520と同じ（`rules_digest` `1393f502…a9626`） |
| native wheel | #520と同じ `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`、SHA-256 `130929229b16b0508a61e77d04c0a219ab65a15f7e79a74d60c43725c0cb8fba`（`focal-plan` はこの値とlisjong revisionが一致しなければplanを作らない） |
| Arena | 実装PRのmerge SHA（clean checkout、`main` に含まれること。planが記録し、live gateが実行前後に照合） |
| 実行 | WSL2 Ubuntu 24.04（#517 / #520と同じ機器）、CPython 3.14（uv venv）、Rust backend、worker 1 |
| 新規課金 | 0（AWSは使わない） |

相手の選定理由（Issue #522）: Champion系、速度鳴き追加前の系統、攻撃打牌が0004系の相手を混ぜ、AABBの
同系統だけの卓から条件を変える。結果を見て選び直さない。代表的な実戦相手分布の再現は主張しない。

### 1.1 槓・九種九牌を選ぶ経路の確認（source、lisjong `0fff891`）

A・B・O1・O2・O3はすべて `TwoStepUkeirePolicy._decide` を基底に持つ。基底の優先順は
和了 → 立直 → 打牌 → pass → 合法行動が1つだけの場合のそれ、で、槓・九種九牌は打牌かpassと同時にしか
提示されないので選ばれない。途中の `YakuhaiCallGenbutsuDefenseFiniteHorizonHandValueAwarePolicy._decide` は
親が槓を返した場合もpassへ置き換え、`PlacementAwareSpeedCallPolicy._decide` が追加で選ぶのはchi / ponだけ
である。O2（`TargetedHonorReleaseTerminalProgressionPolicy`）・O3
（`PlacementAwareSpeedCallKobalab0004BeliefPaijiaDiscardPolicy`、打牌判断だけを上書き）も同じ経路を通る。
したがって、この卓で槓・九種九牌が選ばれる経路はない（#472の「Champion系列は選ばない」の範囲内）。
相手の差替え・試行の拡大はしない。

## 2. seeds（提案。予約はしていない）

| 用途 | seeds | population | split |
| --- | --- | --- | --- |
| rehearsal | 472005（registry外の開発用seed、1 seed × 8半荘） | — | — |
| 本比較 | 522000..522031（32） | `heuristic-candidate-engine-focal-mixed-522` | `DEVELOPMENT` |

2026-10-22のlive ledger（`seed-registry` authority `eedb4e5`、58 allocations）で、全domainのどの
allocation（#520の520000..520007・520100..520131を含む）とも重ならないことを確認した。registry外の既知seed
（472000〜472006、513000）とも重ならない。予約は承認後に `seed-registry.yml`（`operation=reserve`、owner
`lisbun/lisjong-arena#522`、protocol `arena-heuristic-candidate-engine-focal-mixed-v1`、seed domain
`lisjong-engine-project-standard-v1-hanchan-v1`、`arena_revision` = merge SHA）で行い、実行前にlive ledgerで
readbackして照合する。標本の追加・途中判定はしない。

## 3. 上限と停止

| 実行 | wall | process tree | 1半荘 |
| --- | --- | --- | --- |
| rehearsal（8半荘） | 1時間（3,600 s） | 4 GiB | 600 s |
| 本比較（256半荘） | 6時間（21,600 s） | 4 GiB | 600 s |

上限の方式は#520と同じ三重（どれかが当たれば止まる）:

1. **OS（process tree）**: `systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 -p OOMPolicy=kill
   -p RuntimeMaxSec=<wall>`。scope終了時の `memory.peak` をprocess tree peakの正本とする
2. **realtimeの見張り**: scopeの外で `date +%s` を30秒ごとに見て、期限（開始realtime + wall）を過ぎたら
   `systemctl --user stop <unit>` で止めるwatchdog（#517でmonotonicがrealtimeより約401 s短かった。#520でも
   短い実行で5〜23 sの差があり、原因は未確定）
3. **code**: 1半荘timeout、1半荘ごとの「親 + workerのpeak RSS和」と「max(monotonic, realtime)経過」の確認。
   両clockの経過を `timings.json` に記録し、verifyの出力に差を示す（60 sを超えたら結果とともに報告する）

worst caseの256 × 600 s = 153,600 sは6時間を大きく超える。超えた場合は全体を止めて `STOP-INCOMPLETE` とし、
部分結果の採否判定をしない。K・timeoutを後から縮めない・伸ばさない。失敗・未対応なら停止し、自動再試行しない。

**STOP-BUDGET**（本比較の前に判断）: rehearsalの `report.json` の `projection`（256半荘への単純換算の
schedule秒 + 保存・summary秒）に、rehearsalで実測したverify・回収・hash照合の秒数を加えた見込みが
6時間の80%（17,280 s）を超える場合は、本比較を保留して報告する。Kやtimeoutを削って収めない。

## 4. 見込み

#520の段階2（同機器・同engine・同wheel、A/BだけのAABB 192半荘）は約50.7 s / 半荘だった。単純換算では
256半荘で約3.6時間（約13,000 s）だが、混成相手（特にO3の0004系打牌）の判断時間は未計測で、見込みは
rehearsalで更新する。rehearsalの8半荘は約7分の見込み（上限1時間）。容量は#520の実績から、raw約4 MB、
`policy-analysis.json` は判断秒の列で数MB、メモリ1 GB未満の見込み（上限4 GiB）。

## 5. 保存先

| 場所 | 用途 |
| --- | --- |
| WSL `~/fm-522/arena` | Arena merge SHAのclean checkout |
| WSL `~/fm-522/venv` | uv venv（Arena + pin + wheel） |
| WSL `~/fm-522/run/<run>/{plan.json, output/, diagnostics/}` | `<run>` = `rehearsal` / `compare` |
| `C:\Dev\lisjong-artifacts\issue-522-focal-mixed-recheck\` | scripts・log・plan・ledger snapshot・回収したoutput・SHA-256一覧・verify出力 |
| `C:\Dev\lisjong-artifacts\native\0fff8918…\` | wheel（取得済み、#520でSHA-256照合済み） |

#520の `~/ps-520` は変更せず、同時に重い実行をしない。

## 6. 手順（承認後）

1. setup: clean checkout、`pip install -e arena[dev]`、wheelを `--force-reinstall`、`environment_verify`
   （CONSISTENT）、`_lisjong_native.SOURCE_REVISION` = `0fff891`
2. OS上限の再確認（#520と同じ確認 + realtime watchdogの動作確認。同構成なら既存の停止証拠を再利用できる）
3. **rehearsal（1回だけ）**: `focal-plan --run-kind rehearsal` → `focal-run` → 別processの `focal-verify` →
   `focal-report` → `focal-verify`（reportも再導出）→ 回収 → hash照合 → 副指標の確認。判定には使わない。
   Python全対局照合は追加しない
4. rehearsalの実測から見込みを提示し、STOP-BUDGETに当たらないことを確認して、本比較の実行判断を受ける
5. seedsを予約・readback → `focal-plan --run-kind allocated --seeds 522000,..,522031 --allocation-identity ID`
   → `focal-run` → 別processの `focal-verify` → `focal-report` → `focal-verify` → 回収 → hash照合
6. 使ったallocationを既存workflowでCOMMITTEDにし、結果・停止理由を#522へ記録、#84 / #297へ次actionを渡す

## 7. 局単位記録チェック（AGENTS.md・[AABB仕様](heuristic-candidate-aabb-half.md)「新規評価の局単位記録チェック」）

| 項目 | 対応 |
| --- | --- |
| 同じ実行中に各局の客観結果を保存 | #513と同じ第1層・第2層の検査・I/O（両方 `VALID` を要求）を、このprotocolの半荘identity（`rotation` / `condition`）とseat role（`focal` / `O1`〜`O3`）で使う |
| seed・rotation・条件・seat・Policy identityへの結び付け | 第1層rowとscheduleの照合（件数・順序・seat role・identity）、bindings = plan digest・environment digest。Policy観測は別fileでseed・rotation・条件・seatが一致し、判断数がPolicy呼び出し数と一致 |
| 和了率・放銃率・平均和了打点・平均放銃失点の定義・母数 | #513の副指標をfocal席だけで数える（率の分母はfocal席の参加局。各seedの4 focal席を合算してA−B、seed等重み。打点・失点は本場を除く手の点、全seed合算の分子 / 分母、分母0はnull） |
| 集計単位 | seed block（局・seat-result・8半荘を独立標本にしない） |
| 既知development入力で取得→保存→回収→検証→集計 | 手順3のrehearsal（同じrunner・revision・backend、seed 472005） |
| 回収対象と完全性 | outputの全file（raw・analysis・summary・timings・report）のSHA-256一覧を生成側と回収先で照合し、別processのverifyで再導出が一致すること |

## 8. 範囲外

Champion昇格・稼働変更・他改修との束ね、正式評価の費用・標本数、#520のDとの合算、追加seed・第二候補、
閾値・K・相手の変更、AWS、Python全対局比較の自動追加、汎用backend・探索基盤。
