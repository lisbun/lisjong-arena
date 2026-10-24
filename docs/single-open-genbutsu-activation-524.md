# 単独副露者genbutsu候補のshadow変更頻度・時間gate: 実行前計画（#524）

Issue: [lisbun/lisjong-arena#524](https://github.com/lisbun/lisjong-arena/issues/524)
（親 lisbun/lisjong-project#84、候補 lisbun/lisjong#343 / PR #345）。API・保存物・判定の正本は
[`engine-aabb-evaluation.md`](engine-aabb-evaluation.md) §11。

> Status: **実装のみ（未承認・未実行）**。rehearsal・seed予約・本実行・192半荘screen・AWSはどれも始めていない。
> 実行は後続の実行Issueで個別に承認を得てから行う。下記のArena revisionは実装PRのmerge後に確定する。
> 結果はDEVELOPMENTで、採用・正式評価・強さの結果ではない。

## 1. 固定する入力（実行Issueへ引き継ぐ）

| 項目 | 値 |
| --- | --- |
| lisjong | `d2469f2519b947fb27bf8c1622a2854c200e0aae`（#343 / PR #345のsquash merge。Arena pin） |
| A | `placement-aware-speed-call-single-open-genbutsu-v1` = `lisjong.policies:SingleOpenGenbutsuPlacementAwareSpeedCallPolicy`（引数なし）、設定 `{}`、学習artifactなし。catalog factory `create_placement_aware_speed_call_single_open_genbutsu` |
| B | `placement-aware-speed-call` = 同じrevisionの `PlacementAwareSpeedCallPolicy`（Aのexact parent）、設定 `{}` |
| lisjong-engine | `91af75e3aa11520c3b0543719dc74bb4c517ee06`、RuleSet全field・game modeはengine AABB仕様のまま（`rules_digest` `1393f502…a9626`、`protocol_digest` `e60f75d7…adec2`。planが両digestを持ち、違えば拒否） |
| native wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`、301,077 bytes、SHA-256 `0157f3dc7fa631e834437e3f1f049cbfdab9ed1eadca1e8c73d6f8dec51d7046`、`SOURCE_REVISION` = lisjong pin、`API_VERSION` 3 / `SCORING_API_VERSION` 1、lisjong main push CI [40055297032](https://github.com/lisbun/lisjong/actions/runs/40055297032)（merge SHAのwheel） |
| Arena | 実装PRのmerge SHA（clean checkout、`main` に含まれること。planが記録し、live gateが実行前後に照合） |
| 実行 | WSL2 Ubuntu 24.04（`Ubuntu-24.04`）、CPython 3.14（uv venv）、Rust backend、worker 1（#517 / #520と同じ機器） |
| plan digest | `genbutsu-plan` の出力（実行Issueに記録する） |
| 新規課金 | 0（AWSは使わない） |

Bの挙動: `0fff891..d2469f2` でChampionのPolicy source（`placement_aware_speed_call.py` とその親）は変わっていない。
計算部分は変わった: 打牌候補の一括評価loopのRust化（lisjong #333、挙動維持）と、Python側の向聴artifact供給の分離
（lisjong #338）。lisjong側は回帰testで挙動維持を確認している。Arenaでは**Python全対局の再照合を自動追加しない**
（Issue指示）。#520 / #522の保存結果は旧pinの組で、新pinで再現したことにはしない。このgateはA・Bとも同じ
revision・同じwheelでの比較なので、pin間の差はgateの判定に入らない。

## 2. 判断の観測と判定

4席すべてB、各seed 1半荘、配置の入替なし。各判断でBの行動だけを対局へ適用する。Aは判断ごとに新しく作った
instanceで通常実行（`execute_policy`）とtrace実行（`execute_policy_with_trace`）を1回ずつ行い（別instance）、
2つの行動が違えば `trace-action-mismatch` とする。lisjong #343の `SingleOpenGenbutsuAnalysis` をそのまま集計し、
gate・現物集合・向聴・受入・価値proxyをArenaで再計算しない。

- 変更（分子）: `SWITCHED` で基礎牌種が違う、または赤identityだけが違う打牌。手出し / ツモ切りだけの違いは数えない
- 分母: 全32 seat-hanchans
- STOP-INVALID: `out-of-scope-difference`（`SWITCHED` 以外でA ≠ B）、`candidate-illegal-action`、
  `analysis-invariant-violation`（同向聴・受入・retained・yaku非減の条件と `qualifying_actions` の不一致、
  現物でない代替、parentの現物判定の矛盾、聴牌parentの比較など）、`trace-action-mismatch` が1件以上
- STOP-LOW-ACTIVATION: 変更 < 32件、または変更のあるseed < 4
- PROCEED: それ以外（予算配分の実務基準。改善・検出力の証拠ではない）
- 記録するもの: stage別件数（gate各段階）、対象副露者が1人の判断数、代替牌のある判断数、変更数、seed別・席別の分布、
  A（trace / 通常）とBの判断秒（中央値・p95・最大）、半荘wall、全schedule wall、親 + workerのpeak RSS、
  出力bytes（verifyの出力）。shadowの勝敗・点数は判定に使わない
- 低頻度のときも、追加seed・対象/閾値の緩和・候補の差替えを同じ実験内で行わない

## 3. seeds（提案。予約はしていない）

| seeds | population | split |
| --- | --- | --- |
| 524000..524007（8） | `heuristic-candidate-engine-aabb-single-open-genbutsu-activation-524` | `DEVELOPMENT` |

2026-10-24のlive ledger（`seed-registry` authority `09a385b`、59 allocations、SHA-256 `27530678…8f62`）で、
全domainのどのallocationとも重ならないことを確認した。registry外の既知seed（472000〜472006、513000）とも
重ならない。予約は承認後に `seed-registry.yml`（`operation=reserve`、owner `lisbun/lisjong-arena#524`、
protocol `arena-heuristic-candidate-engine-aabb-half-v1`、seed domain
`lisjong-engine-project-standard-v1-hanchan-v1`、`arena_revision` = merge SHA）で行い、実行前にlive ledgerで
readbackする。使用済みseedは後続の独立確認に再利用しない（planが全domainとの重なりを拒否する）。

## 4. 上限と停止（推奨案）

| 実行 | wall | process tree | 1半荘 |
| --- | --- | --- | --- |
| rehearsal（2半荘、registry外seed 472005・472006） | 1時間 | 4 GiB | 600 s |
| 本実行（8半荘） | 2時間（7,200 s） | 4 GiB | 600 s |

上限の方式は#520と同じ三重（[#520 §3](paijia-tie-screen-520.md)）: `systemd-run --user --scope -p MemoryMax=4G
-p MemorySwapMax=0 -p OOMPolicy=kill -p RuntimeMaxSec=<wall>`、scopeの外のrealtime watchdog（30秒ごと）、
codeの1半荘timeoutと半荘ごとの「親 + worker RSS」「max(monotonic, realtime)経過」の確認。
**計画承認前に**、実行に使うWSLで#517の `os-limit-check` と同じ3 caseとwatchdogの動作を再確認する
（#517で確認済みの方式だが、この実験の環境で適用できることを実行Issueで記録する）。
上限超過・timeout・analysisの不整合は `STOP-INCOMPLETE` で、`output` に何も出さず部分標本をPASS扱いしない。

## 5. 見込み（#520段階1の同機器・同engineでの実測から）

#520段階1（B + A各1回、8半荘）は762 s（半荘中央値97.8 s、最大115 s）、9,081判断、A・Bとも判断秒の中央値約2.7 ms・
p95約0.14 s、process tree peak（親 + worker）約367 MB、`shadow.json` 2.1 MBだった。#524のAは「Championの打牌判断 +
現物の代替打牌だけの比較値」で、判断ごとに2回（通常 / trace）実行するので、1半荘はB + 2A ≈ #520の約1.5倍と見込む。

| 実行 | 見込み | 上限 |
| --- | --- | --- |
| rehearsal（2半荘） | 約5分 | 1時間 |
| 本実行（8半荘） | 約20分（1半荘約140 s、最大約175 s） | 2時間 |
| verify / 回収 / hash照合 | 各数秒 | — |

容量: `shadow.json` は比較値を計算した判断のanalysisを含むので2〜5 MBの見込み。メモリ: 1 GB未満の見込み。
これらは見込みで、summaryの `screen_projection`（記述値）と実測で後続screenの費用を見積もる。

## 6. 保存先

| 場所 | 用途 |
| --- | --- |
| WSL `~/sog-524/arena` | Arena merge SHAのclean checkout |
| WSL `~/sog-524/venv` | uv venv（Arena + pin + wheel） |
| WSL `~/sog-524/run/<run>/{plan.json, output/, diagnostics/}` | `<run>` = `rehearsal` / `activation` |
| `C:\Dev\lisjong-artifacts\issue-524-single-open-genbutsu\` | scripts・log・plan・ledger snapshot・回収したoutput・SHA-256一覧・verify出力 |
| `C:\Dev\lisjong-artifacts\native\d2469f25…\` | wheel・`SHA256SUMS`・`BUILD-INFO.txt`（取得済み、SHA-256照合済み） |

#517 / #520 / #522のcheckout・pin・planは変更せず、同時に重い実行をしない。

## 7. 手順（承認後、実行Issueで）

1. setup: clean checkout、`pip install -e arena[dev]`、`verify-wheel`、wheelを `--force-reinstall`、
   `environment_verify --project pyproject.toml`（CONSISTENT）、`_lisjong_native.SOURCE_REVISION` = `d2469f2`、
   Rust backend probe（[現行wheel](lisjong-native-wheel-current.md)）
2. OS上限の再確認（§4）
3. **rehearsal**（既知の開発用seedだけ。未使用seedsは使わない）:

   ```bash
   python -m lisjong_arena.heuristic_candidate_engine_aabb genbutsu-plan --run-kind rehearsal \
     --timeout-seconds 600 --wall-limit-seconds 3600 --rss-limit-bytes 4294967296 \
     --limit-enforcement "<systemd-run scope + realtime watchdog>" \
     --wheel <wheel> --run-output ~/sog-524/run/rehearsal/output \
     --diagnostics ~/sog-524/run/rehearsal/diagnostics --output ~/sog-524/run/rehearsal/plan.json
   python -m lisjong_arena.heuristic_candidate_engine_aabb genbutsu-activation-run --plan <plan>
   python -m lisjong_arena.heuristic_candidate_engine_aabb genbutsu-activation-verify --plan <plan>   # 別process
   ```

   回収してSHA-256一覧を生成側と照合する。`trace-action-mismatch` 等の不正outcomeが出たら本実行へ進まない
4. seedsを予約・readback → `genbutsu-plan --run-kind allocated --seeds 524000,...,524007
   --allocation-identity <id> --wall-limit-seconds 7200 ...` → `genbutsu-activation-run` →
   別processの `genbutsu-activation-verify` → 回収・hash照合
5. 使ったallocationをCOMMITTEDにし、判定・頻度・時間・RSS・出力bytes・停止理由を実行Issueと#524へ記録、
   project #84とlisjong #343へ次actionを渡す。`PROCEED` の場合だけ、別の実行Issueで後続screen（§8）を事前登録する

## 8. 後続比較（この計画の範囲外。順序と判定案だけを引き継ぐ）

頻度・実行品質の通過後だけ、別の実行Issueで新32 seeds × 6配置 = 192半荘の固定DEVELOPMENT screenを事前登録する。
本実行の実測時間・RSSから保存・verifyを含む費用を見積もり、承認済み枠へ収まらなければ `STOP-BUDGET`。
主指標は現行engine AABBのseed等重みD。進行条件案はD > 0、素点差 >= 0、放銃率差 <= +1 pt、全件verify成功。
副露者への放銃だけで判断しない。条件は対局結果の取得前に実行Issueで確定し、その後変更しない。通過後も別seed /
別相手で凍結候補を再確認する。#517の別pairのSDをそのまま流用せず、正式評価はproject #83の候補固有計画へ分ける。
この計画で192半荘の実行・Champion更新は始めない。

## 9. 局単位記録チェック（AGENTS.md・[AABB仕様](heuristic-candidate-aabb-half.md)「新規評価の局単位記録チェック」）

この実行は強さ比較ではなく、Bだけが対局するshadow計数である。和了率・放銃率・平均和了打点・平均放銃失点の
A/B比較は成立しないので集計しない（shadowの勝敗も判定に使わない）。客観値として半荘ごとの局数・終了理由・
final_points・`CompletedMatch` digest・selector / Policy呼び出し数を `hanchans.json` に保存し、Policy側の観測
（`shadow.json`）と分ける。後続の192半荘screenは正式runと同じ局単位record（第1層・第2層）を同じ実行中に保存する
計画で、その定義・母数・保存先は後続の実行Issueに記載する。

| 項目 | 対応 |
| --- | --- |
| seed・seat・Policy identityへの結び付け | plan digest・environment digest、`shadow.json` と `hanchans.json` のseed・placement・seatが一致し、判断数がPolicy呼び出し数と一致 |
| 既知development入力で取得→保存→回収→検証→集計 | 手順3のrehearsal（同じrunner・revision・backend） |
| 回収対象と完全性 | outputの全file（shadow・hanchans・summary・timings）のSHA-256一覧を生成側と回収先で照合し、別processのverifyで再導出が一致すること。summaryは `shadow.json` / `hanchans.json` を内容のSHA-256で拘束する |

## 10. 範囲外

192半荘screenの実行、Python全対局比較の自動追加、AWS、追加seed・第二候補、閾値・Kの変更、#517のSD・必要Kの流用、
Champion採用・既存改修との束ね、汎用runner・探索基盤、lisjong #342の配布自動化・Python廃止・自律loopへの依存。
