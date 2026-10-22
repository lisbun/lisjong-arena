# engine版AABB評価のprotocol・lock・実行・result・verify（#514）

Issue: [lisbun/lisjong-arena#514](https://github.com/lisbun/lisjong-arena/issues/514)
（parent [lisbun/lisjong-project#83](https://github.com/lisbun/lisjong-project/issues/83)）
仕様: [`heuristic-candidate-engine-aabb-half.md`](heuristic-candidate-engine-aabb-half.md) §2〜§5、§7、§9〜§12
局単位record（#513）: [`engine-aabb-kyoku-record.md`](engine-aabb-kyoku-record.md)

> Status: **実装のみ**。正式eventは実行していない。seed予約、校正、K・timeoutの決定（J1 / J2）、
> AWS入口、Champion変更は行っていない。開始条件（§7）がそろうまで正式評価は始めない。

`lisjong_arena.heuristic_candidate_engine_aabb` は、独立protocol
`arena-heuristic-candidate-engine-aabb-half-v1` の固定値、実行前lock、6配置executor、
主統計、result、保存物からのverifyを持つ。旧RiichiEnv版（`heuristic_candidate_aabb`）への
backend option追加ではなく、旧版の100 block・4 rotation・得点式を持ち込まない。

```text
lock      protocol / rules / participants / population / K根拠 / timeout / 環境 をbind
run       live gate -> 6K半荘（seed順 -> placement 0..5） -> live gate
          -> 第1層・schedule・主統計を書込前に検査 -> raw record -> result
verify    保存済みlock・raw・resultだけから再導出し、完全一致だけを成功とする（offline）
dev-check §10.7の記録経路確認（開発用seed・別document、正式resultにならない）
```

## 1. Module

| module | 役割 |
| --- | --- |
| `protocol` | 固定値、rules / protocol / schedule document、digest、participant binding |
| `statistics` | `S_A` / `S_B` / 差（内部単位の整数）、`D`、区間、classification |
| `lock` | lockの組立・保存・読込、Seed Registryとの照合、live gate |
| `executor` | 1 workerでの逐次実行、半荘ごと・seatごとの新しいPolicy、呼び出し数、timeout |
| `result` | result document、offline verify |
| `runner` | 正式eventの実行入口、STOP時の診断証跡 |
| `development` | §10.7の開発用確認（plan / raw / summary / timings） |
| `calibration` | 限定校正（#517）のplan / run / verify / report（§8） |
| `backend_check` | 校正前のPython/Rust照合P2（#517、§8.2） |
| `paijia_tie_plan` / `paijia_tie_worker` / `paijia_tie_activation` / `paijia_tie_screen` | #520 同点数牌paijia候補の開発比較（DEVELOPMENT、§9） |
| `__main__` | `lock` / `run` / `verify` / `dev-check` / `dev-verify`、校正用の6 command（§8）、#520の6 command（§9） |

依存は `protocol / lock / executor / result → 局単位record（#513）` の一方向で、record側は
protocol・lockを知らない。bridgeの `run_policy_hanchan` には、`on_round_evidence_complete`
（そのままengineへ渡す）とselector呼び出しの観測hookをoptionalで加えた。既定の動作は変わらない。

## 2. 固定値とdigest（§5.6、N11）

| 項目 | 値 |
| --- | --- |
| protocol | `arena-heuristic-candidate-engine-aabb-half-v1` version 1 |
| engine | `91af75e3aa11520c3b0543719dc74bb4c517ee06`（installed `direct_url.json` と照合） |
| `rules_digest` | `1393f5021bd332ca6efe733c12fd79e011b55706ae559db894b0619cd0fa9626` |
| `protocol_digest` | `e60f75d7c32695e1c79225cad409334c3233dc8dd8b1cd0c6c7995789c9adec2` |
| classification rule | `heuristic-candidate-engine-seed-block-final-points-normal-approx-95-v1` |

- canonical JSONは既存artifactと同じ（`_artifact_io.canonical_json_text`: key sort、indent 2）で、
  digestはそのUTF-8 bytesのSHA-256。enumは `.value`、集合はsortしたlist、tupleはlist
- rules documentは `dataclasses.fields(RuleSet)` を列挙して作る。field集合と値が§5.2の表
  （`protocol.EXPECTED_RULES`）と一致しなければ、lock・実行のどちらも止まる
- protocol documentは、ID・version・配置表・主指標・classification・rules digest・engine revision・
  game mode・終局条件を持つ。digestはmodule importのたびに再計算して照合する
- schedule digestは `{"hanchans": [{seed, placement, roles}...], "protocol_id"}` のdigest。
  lock digestは `lock_digest` を除いたlock全体のdigest

## 3. Lock

`python -m lisjong_arena.heuristic_candidate_engine_aabb lock ...` で、Seed Registryの
allocation、Kの根拠のJSON、timeout、wheel、出力先を明示して書く。defaultはない。

| bind | 内容 |
| --- | --- |
| participants | A / Bの `policy_identity`、`factory_binding`（`lisjong_arena.policy_catalog:create_*` だけ）、`family = heuristic`、同じ `lisjong_revision`、`behavior_configuration` とそのdigest、`backend = rust`、`artifact = null`。factory・callable自体は保存しない |
| `incumbent_role` | `current-champion` または `fixed-c0`（resultにも残す） |
| population | owner Issue、`heuristic-candidate-engine-aabb-<event>`、`FORMAL-EVAL`、seed domain、ordered seeds、K、schedule digest、allocation binding |
| `k_rationale` | `target_effect` / `sd_estimate` / `power` / `cost_estimate`（空でない文字列） |
| execution | `timeout_seconds`（1半荘あたり。正の整数を明示）、`backend = rust`、wheel path、worker 1 |
| environment | Python、`collect_execution_provenance()`（Arena / lisjong / lisjong-engine / riichienv）、shanten backend、native wheelのfilename・SHA-256・bytes・`SOURCE_REVISION`・`API_VERSION`。digestも記録 |
| execution target | clean worktreeで、`main` に含まれるArena revision |
| destinations | `raw`（局単位record directory）、`result`（result JSON）、`diagnostics`（STOP時の証跡）。lock作成時に未作成であること |
| 固定 | protocol document / digest、rules / digest、no-rescue boundary、`result_exposed = false` |

検査:

- K = ordered seedsの件数、`K >= 2`（算術上の下限。正式評価の最小KはJ1で決める）、seedの重複なし
- allocation bindingを `require_allocation_binding()` でlive ledgerへ照合する（owner Issue・protocol・
  domain・population・split・membership・active state）。加えて、**全domain**の他allocationと
  1 seedでも重なれば拒否する。registry外で使った既知seed（#472の472000〜472006、#513 / #514の
  実engine testの513000）も拒否する
- ledgerが読めない・壊れている場合は止まる。fallbackはない
- A / Bはidentityとfactoryが異なり、同じlisjong revisionで、installed lisjongと一致する。
  学習artifact、`policy_catalog` 以外のfactory、factoryに合わない設定は受理しない
- 保存はwrite-once。読込時はschema・field集合・各値・各digestを再検査する

## 4. 実行（`run`）

1. lockを読み、`raw` / `result` が既にあれば開始しない（written resultは再実行しない。§4.3）
2. live gate: clean HEAD（`main` に含まれる）、実行環境、allocationがlockと一致
3. 1 worker（spawn）で、seed順 → placement 0..5の6K半荘を逐次に実行する。workerは半荘ごとに
   実行環境を検査し、seatごとにfactoryから新しいPolicyを作り、`run_policy_hanchan(rules=<lockのRuleSet>)`
   を `on_round_evidence_complete` 付きで呼ぶ。selector呼び出し数とPolicy呼び出し数をseatごとに数える
4. live gateをもう一度通す
5. 書込前に、第1層の全検査（#513）、期待schedule・seat・identity（K1 / K2）、`S_A + S_B = 0`、
   主統計が成り立つことを確かめる
6. raw record（`event_facts_required = False`、bindingsはlock digestと環境digest）とresultを
   同じparentのstagingへ書き、resultの保存まで成功してから両方を公開する（rawを先に公開して
   resultで失敗した場合はrawを戻す）。途中で失敗しても公開先には何も残らない

STOP / INVALID（resultを書かない）: Policy・engine・bridge・mapping（`PolicyActionValidationError`、
`UnmappedActionError` 等）の例外、1半荘がlockのtimeoutを超える、workerの環境がlockと違う、
workerが別の半荘を返す、呼び出し数の不一致、第1層・schedule・ゼロサムの不一致、ledger・HEAD・
環境のlive照合の失敗。成功分のresult、seedの差し替え、skip、fallbackはない。
`diagnostics/stop-<UTC>.json`（`not_a_result: true`）に理由と完了半荘数を残す。resultを公開する前に
止まった場合は、同じlock・seed・destinationでschedule全体をやり直せる。timeoutや失敗のときは
workerを強制終了する（`terminate_workers()`。止まらずに残るworkerを作らない）。

第2層だけの失敗は§10.6のとおり、第2層を `INVALID` にして理由を残し、第2層の副指標を出さない。
主指標と第1層の副指標は第1層の検査だけで判断する。

## 5. Resultとverify

result（`arena-heuristic-candidate-engine-aabb-result-v1`、`evaluation_kind = FORMAL`）:
`blocks`（seedごとの `s_a_internal` / `s_b_internal` / `difference_internal`）、`statistic`
（`k`、`mean_d`、`sd`、`lower`、`upper`、`z`、`classification`、rule ID）、`secondary_metrics`
（#513の集計。記述値）、`event_fact_status`、`incumbent_role`、`lock_digest`、`protocol_digest`、
`raw_manifest_sha256`、`aggregation_revision`、`result_digest`。

- `D(s) = (S_A - S_B) / 120`（pt / seat-result）。割り算は集計時に1回だけ
- `mean ± 1.96 × SD(K-1) / sqrt(K)`。lower > 0 → `CANDIDATE SUPERIOR`、upper < 0 →
  `CHAMPION SUPERIOR`、それ以外（境界0を含む）→ `INCONCLUSIVE`
- 副指標は主指標の区間・classificationを変えない

`verify --lock <lock>`（`verify_result()`）は、保存済みlock・raw record・resultだけを読み、
recordの全検査、期待schedule、bindings、主統計、副指標を再導出して、記録値との完全一致を
求める。engineの再実行・Policyの生成はしない。live ledger・実行環境の照合はしない
（それは実行時のgateである）。

## 6. 開発用の記録経路確認（`dev-check`）

§10.7とAGENTS.md「対局評価の実行前確認」のための入口。正式lockのFORMAL-EVAL・ledger検査を
無効にして使うのではなく、別のplan document（`evaluation_kind = DEVELOPMENT`）を使う。

| 項目 | 値 |
| --- | --- |
| seeds | 472005、472006（#472の開発用、registry外）× 6配置 = 12半荘 |
| participants | A = `placement-aware-speed-call`（C0）、B = `one-shanten-defense-placement-aware-speed-call` |
| 第2層 | 必須（`event_facts_required = True`） |
| 出力 | `plan.json`、`raw/`、`summary.json`（block合計・副指標。区間・classificationなし、`not_a_formal_result`）、`timings.json`（再導出対象外） |
| HEAD | clean worktree（`main` への包含は求めない） |

`dev-verify` は保存物だけからsummaryを再導出して完全一致を確かめる。この12半荘は強さの比較でも、
J3（P2の規模）の決定でも、固定C0（J6）の代替証拠でもない。

実施（2026-10-21、Arena `92fedd2`、WSL、Rust）: 12半荘・143局、第2層 `VALID`、別processの
`dev-verify` で完全一致、1半荘最大59.4秒、worker peak RSS 274 MiB。12半荘とも `final_points` と
局数が#472の同じ半荘と一致した。実行条件・指標・hashは
[#514のcomment](https://github.com/lisbun/lisjong-arena/issues/514#issuecomment-6320600309)にある。

## 7. 残る開始条件

- J1（Kと最小値）、J2（timeoutとfail-safe）は校正後に決める。未決のまま正式eventを始めない
- J6（固定C0の固定方法）は対C0 eventの計画時に決める
- 槓・九種九牌を選ぶcandidateは、その適用・記録をcandidateごとに確認する（#472の範囲を一般化しない）
- §10.7の確認（本Issueで開発用入力により1回）、P2 / P3の正式構成照合、校正、project #83のgate、
  承認済みのevent計画、seed予約がそろうまで正式評価は始めない
- worker数は1に固定した。並列化する場合は、worker間の環境一致を含めて別に設計する

## 8. 限定校正（#517）

Issue: [lisbun/lisjong-arena#517](https://github.com/lisbun/lisjong-arena/issues/517)

> Status: **実装のみ**。P2 / P3、seed予約、校正の実行はしていない（実行対象・上限の承認後に行う）。

仕様§11の校正を、固定した1組・1構成に限って行う入口。正式lock（`FORMAL-EVAL`）と
dev-check（既知12半荘）の検査は緩めず、別のplan document（`evaluation_kind = CALIBRATION`）を使う。
executor・局単位record・`block_totals`・I/O検査は正式runと同じcodeである。

| 項目 | 値 |
| --- | --- |
| A（candidate） | `one-shanten-defense-placement-aware-speed-call` |
| B（incumbent、`current-champion`） | `placement-aware-speed-call`（§6 dev-checkとはA / Bが逆） |
| 規模 | 32 seed blocks × 6配置 = 192半荘、Rust、worker 1、1回（`CALIBRATION_K`。正式の最小Kではない） |
| population | owner `lisbun/lisjong-arena#517`、`heuristic-candidate-engine-aabb-calibration-517`、split `CALIBRATION`、engine domain |
| 設計値（J1） | δ = 1.0 pt / seat-result、両側α = .05、power = .80、`K_plan = ceil(((1.959964 + 0.841621) × σ / δ)²)` |
| bootstrap | seed block単位、10,000回、乱数seed 514、SDの片側95%上限（nearest-rank） |

### 8.1 Command

```text
calibration-plan   --seeds S1,..,S32 --allocation-identity ID --timeout-seconds T
                   --wall-limit-seconds W --rss-limit-bytes R --limit-enforcement TEXT
                   --wheel WHL --calibration-output DIR --diagnostics DIR --output plan.json
calibration-run    --plan plan.json
calibration-verify --plan plan.json      保存物だけからsummary（とreport）を再導出、file一覧とSHA-256を出力
calibration-report --plan plan.json      verifyに通った保存物からreport.jsonをwrite-onceで書く
```

- plan: pair、seeds（allocation binding。全domainの他allocation・registry外の既知seedと重なれば拒否）、
  設計値、timeout、資源上限（stageごとのwall秒、process tree RSS bytes、OS側の強制方法の記述）、
  実行環境（正式lockと同じ `environment_document`）、clean で `main` に含まれるArena revision、
  保存先（`output` / `diagnostics`、未作成）。defaultはない。write-onceで、読込時に全fieldとdigestを再検査する
- run: live gate（HEAD・環境・allocationがactive）→ 192半荘 → live gate → 第1層・第2層・scheduleを
  書込前に検査 → stagingに `raw/`・`summary.json`・`timings.json` を書いてから `output` へ公開する
- 上限: 1半荘timeoutは既存executor。stage全体のwall秒と、親 + workerのpeak RSSの和は1半荘ごとに確かめ、
  超えたらworkerを止めてSTOPする。これはcode側の二重化で、process tree全体の強制はOS側
  （例: `systemd-run --user --scope -p MemoryMax=… -p RuntimeMaxSec=…`）で行い、その方法をplanに書く
- STOP / INCOMPLETE: 例外、timeout、上限超過、環境・呼び出し数・第1層・第2層・scheduleの不一致。
  `output` には何も公開せず、`diagnostics/stop-<UTC>.json`（`not_a_result: true`）だけを残す。
  成功分だけのsummary、seedの差し替え・追加・再試行、条件の緩和はしない。`output` があれば再実行しない

### 8.2 保存物

| file | 内容 | 再導出 |
| --- | --- | --- |
| `raw/` | 局単位record（第1層・第2層とも `VALID` を要求、bindingsはplan digest） | — |
| `summary.json` | blockごとの `S_A` / `S_B` / 差 / `D`、K、SD(K-1)、bootstrap SD上限、leave-one-block-outのSDと各blockの寄与、各σでの `K_plan` と半荘数、副指標（#513、seed単位）、`status = CALIBRATION COMPLETE; NOT A STRENGTH RESULT` | plan + raw |
| `timings.json` | 半荘ごとのwall秒・局数・worker peak RSS・親peak RSS、schedule・保存・summaryの秒数 | 計測値 |
| `report.json` | 1半荘 / 6配置blockの時間（平均・中央値・最大）、局数、RSS最大、file容量とSHA-256、J1の各σでの所要時間（観測平均、暫定timeout・2倍案timeoutでのworst case）、J2の観測最大と `ceil(2 × 最大 / 60) × 60` 案 | plan + summary + timings + file |

`timings.json` は再導出しないが、verify・reportの前に毎回検査する（reportの有無によらない）:
時間は有限の非負数、局数は正の整数で保存済みraw recordの局数と一致、RSSは正の整数か欠測（`null`）。
boolは拒否する。RSSの欠測は0として扱わず、reportでは最大値の対象から外して欠測件数を記録する。

summaryとreportはD平均・区間・classificationを出さない（校正のD平均の符号・区間を採否に使わない）。
正式の最小K・timeoutは校正値から自動決定せず、別のevent計画で決める。

### 8.3 P2 / P3（`backend-check` / `backend-compare`）

```text
LISJONG_SHANTEN_BACKEND=python ... backend-check --backend python --timeout-seconds T --output python.json
LISJONG_SHANTEN_BACKEND=rust   ... backend-check --backend rust --wheel WHL --timeout-seconds T --output rust.json
... backend-compare --python python.json --rust rust.json --output p2.json      MATCHでだけ終了コード0
```

- 入力は#472の開発用seed 472005・472006 × 6配置（12半荘）を、上表のpairでbackendごとに1回。
  backendはprocessごとに選ばれるので別processで実行する。正式runnerのRust固定は変えない
- 半荘ごとに局数、`end_reason`、`final_points`、`CompletedMatch` 全体（各局の `CompletedRound` の結果・
  精算・transfer・本場・供託、`final_score`）の正規化表現、Policyが返した全選択行動の列を比べる。
  許容誤差はなく、1つでも違えば `MISMATCH`（Rustを補正しない、Pythonへ自動で切り替えない）
- 正規化（`canonical_engine_value`）は#472のP2と同じ規則: enumは `型名.名前`、dataclass / slotsは
  field名と値、集合・dictはsortしたlist
- P3: rustの `backend-check` と `calibration-run` は、親とworkerの両方で `environment_document`
  （wheel filename・bytes・SHA-256、installed native filesと wheelの一致、`SOURCE_REVISION`、
  `API_VERSION`、backend、provenance）を取り、planの値との完全一致を要求する。指定WSL構成についての
  確認であり、別のimage（AWS等）のPASSではない

## 9. 同点数牌paijia候補の開発比較（#520）

Issue: [lisbun/lisjong-arena#520](https://github.com/lisbun/lisjong-arena/issues/520)
（候補: lisbun/lisjong#330、PR lisbun/lisjong#331、merge `0fff891`）。実行前計画は
[`paijia-tie-screen-520.md`](paijia-tie-screen-520.md)。

> Status: **実装のみ**。rehearsal・seed予約・段階1・段階2は実行していない（実行は別承認）。

`evaluation_kind = DEVELOPMENT` の、この実験専用のplan / run / verify。正式lock（`FORMAL-EVAL`）・
校正（`CALIBRATION`、固定pair / K）・dev-check（既知12半荘・#472 pair）の検査は変えない。
executor・局単位record・`block_totals`・副指標・I/O検査は正式runと同じcodeで、汎用runner・探索基盤は作らない。
正式のclassification（`CANDIDATE SUPERIOR` 等）は出さない。

| 項目 | 値 |
| --- | --- |
| A | `placement-aware-speed-call-suited-paijia-tie-v1`（catalog factory `create_placement_aware_speed_call_suited_paijia_tie` → `lisjong.policies:PlacementAwareSpeedCallSuitedPaijiaTiePolicy`、引数なし） |
| B | `placement-aware-speed-call`（同じlisjong revisionの現Champion。歴史的固定C0ではない） |
| 設定・artifact | 両者とも `{}`、学習artifactなし |
| 実行 | Rust、worker 1、1半荘timeoutはplanで明示（defaultなし） |
| allocation | owner `lisbun/lisjong-arena#520`、split `DEVELOPMENT`、engine domain、population `heuristic-candidate-engine-aabb-paijia-tie-activation-520`（段階1、8 seeds）/ `…-paijia-tie-screen-520`（段階2、32 seeds）。全domainの他allocation・registry外の既知seedと重なれば拒否 |

### 9.1 Command

```text
paijia-plan --stage activation|screen --run-kind allocated|rehearsal
            [--seeds S1,.. --allocation-identity ID]            allocatedだけ
            --timeout-seconds T --wall-limit-seconds W --rss-limit-bytes R --limit-enforcement TEXT
            [--screen-wall-limit-seconds W2 --screen-rss-limit-bytes R2 --screen-limit-enforcement TEXT2]
                                                                 activationだけ（段階2の上限を先に固定）
            [--activation-plan P1]                               allocatedのscreenだけ
            --wheel WHL --stage-output DIR --diagnostics DIR --output plan.json
paijia-activation-run    --plan P1     paijia-activation-verify --plan P1
paijia-screen-run        --plan P2     paijia-screen-verify     --plan P2
paijia-screen-report     --plan P2
```

- plan: write-once。pair、seeds（allocation binding、またはrehearsalの固定seed 472005・472006）、
  timeout、資源上限（stage wall秒、process tree RSS bytes、OS側の強制方法の記述）、判定条件（下記、
  定数と一致しなければ拒否）、実行環境（正式lockと同じ `environment_document`。native `SOURCE_REVISION` は
  installed lisjongと一致が必要）、cleanで `main` に含まれるArena revision、保存先（未作成）を持つ
- 段階2（allocated）のplanは、作成時に段階1の保存物をverifyで再導出し、判定が `PROCEED` であることと、
  段階1のplanで固定した段階2の上限と同じ値であることを要求する（段階1のplan / summary digestを拘束）
- rehearsal: 既知の開発用seed（registry外）で同じrunner・保存・verifyを通す動作確認。判定・進行条件は
  出さない（`gate` / `progression` は `null`）。allocationは使わない
- run: live gate（HEAD・環境・allocationがactive）→ schedule → live gate → 書込前検査 → stagingに書いてから
  `output` へ公開。失敗・timeout・上限超過・analysisの不整合は `STOP-INCOMPLETE` で、`output` に何も出さず
  `diagnostics/stop-<UTC>.json`（`not_a_result: true`）だけを残す。`output` があれば再実行しない
- stage wall上限はmonotonicとrealtimeの両方で1半荘ごとに確かめ、長い方で判定する（#517で約401 sの差）。
  両方の経過を `timings.json` に記録し、verifyの出力に差を示す。process tree全体の強制はOS側で行う

### 9.2 段階1（activation）

B×4席を各seed 1半荘（配置の入替なし）。各判断で、Bの行動を実行経路から1回だけ取得して対局に適用し、
同じ `DecisionContext` で判断ごとに新しく作ったAを lisjongの `execute_policy_with_trace` で1回だけ採点する
（Aの行動は適用しない）。判断ごとに次へ分類する。

| outcome | 意味 |
| --- | --- |
| `same` | A = B |
| `tile-type-change` / `red-only-change` | `PAIJIA_SWITCH` で基礎牌種が違う / 赤・通常だけが違う（**変更**として数える） |
| `tsumogiri-only` | 同じ牌の手出し・ツモ切りだけの違い（変更に数えない） |
| `out-of-scope-difference` | `PAIJIA_SWITCH` 以外でA ≠ B |
| `candidate-illegal-action` | Aの行動が合法候補に一致しない |
| `analysis-invariant-violation` | 打牌なのに `SuitedPaijiaTieAnalysis` がない、analysisの `champion_action` が同じ入力のBの行動と違う、Aの行動が `selected_action` と違う、`PAIJIA_SWITCH` ⇔ 行動の変更、選択がpaijia最小でない、Championが最小なのに切替、数牌でない、評価対象外の行動 |

Arenaはpaijiaや評価値を再計算しない。照合はanalysisが報告する値どうしと、実際の行動との整合だけである。
Aの判断が例外を送出した場合は分類せず、その半荘でSTOP（`STOP-INCOMPLETE`）とする。

保存物: `shadow.json`（判断ごとのoutcome・stage・A/B判断秒。差のある判断とgate通過判断だけ
lisjongのanalysisと両行動）、`hanchans.json`（客観値: 局数、終了理由、final_points、`CompletedMatch` の
digest、selector / Policy呼び出し数）、`summary.json`、`timings.json`。判断数はseatごとのPolicy呼び出し数と
一致しなければならない。勝敗は判定に使わない。

判定（実行前に固定、上から最初に当たったもの）:

1. `STOP-INVALID`: 対象外差分・合法性違反・不変条件違反が1件以上
2. `STOP-LOW-ACTIVATION`: 変更 < 32件（分母は全32 seat-hanchans）、または変更のあるseed < 4
3. `STOP-BUDGET`: `192 × mean(半荘wall − (A判断秒 + B判断秒) / 2)` > 0.8 × 段階2のwall上限
4. `PROCEED`

summaryはgate通過数（`CHAMPION_MINIMUM_PAIJIA` + `PAIJIA_SWITCH`）、stage別の件数（除外理由）、outcome別の件数、
seed・seat別の変更数、A/B判断秒（中央値・p95・最大）、半荘wall・局数・RSS、段階2の見込み（timeout到達時の
worst caseも）を持つ。変更頻度は予算配分の基準で、検出力や改善の証拠ではない。

### 9.3 段階2（screen）

正式runと同じ `play_task` で32 seeds × 6配置を1回。A席は `execute_policy_with_trace` で1回だけ判断し
（行動は同じ）、stage別件数と `PAIJIA_SWITCH`（実発動、analysis付き）を数える。B席は判断秒だけを計る。
analysisが整合しなければその場でSTOPする。

保存物: `raw/`（第1層・第2層、両方 `VALID` が必要、bindingsはplan digest）、`policy-analysis.json`（Policy側の
観測。seed・placement・seatで客観recordと対応、判断数はPolicy呼び出し数と一致）、`summary.json`、
`timings.json`、`report.json`（`paijia-screen-report`。時間・RSS・容量・時計差。強さの値は持たない）。

| 項目 | 内容 |
| --- | --- |
| 主指標 | `D_s = (S_A − S_B) / 120`、seed等重みの平均D、SD(K−1)、`D ± 1.96 SD / √32`（記述用。小Kの正規近似・選択後の偏りの限界があり、検出力保証・昇格判定に使わない） |
| 副指標 | #513の副指標（seed別の分子・分母、和了率・放銃率、本場を除く平均和了打点・平均放銃失点、素点・順位成分、流局聴牌、立直・副露）。分母0はnull |
| 発動 | A判断数、stage別件数、実発動（`PAIJIA_SWITCH`）数と種類、seed別 |
| 判断時間 | A / B別の中央値・p95（nearest-rank）・最大・件数 |

進行条件（実行前に固定。すべて満たすときだけ `PROMISING-DEVELOPMENT`、それ以外は `NOT-PROMISING-DEVELOPMENT`）:

1. D平均 > 0
2. 素点成分（`base_points`）のA−B、seed等重み平均 >= 0（pt / seat-result）
3. seed等重みの放銃率差 A−B <= +0.01（nullなら不成立）
4. 完走・第1層 / 第2層VALID・analysis整合・上限内（summaryが書かれた時点で成立。回収後のhash照合と
   別processのverifyで確かめる）

`PROMISING-DEVELOPMENT` は採用ではなく、別seed・別相手構成での独立した開発再確認の設計へ進む条件である。
途中のDによる追加・早期採用、結果を見てからの閾値変更はしない。
