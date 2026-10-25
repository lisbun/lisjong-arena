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
| `backend_check` | 校正前のPython/Rust照合P2（#517、§8.3）。#531以降は保存記録の照合だけ |
| `rust_only_gate` | Rust必須lisjong用の新version gate（#531、§8.4）。P2の代わり |
| `paijia_tie_plan` / `paijia_tie_worker` / `paijia_tie_activation` / `paijia_tie_screen` | #520 同点数牌paijia候補の開発比較（DEVELOPMENT、§9） |
| `focal_mixed_plan` / `focal_mixed_worker` / `focal_mixed_compare` | #522 凍結paijia候補の混成相手focal比較（DEVELOPMENT、§10） |
| `__main__` | `lock` / `run` / `verify` / `dev-check` / `dev-verify`、校正用の6 command（§8）、#520の6 command（§9）、#522の4 command（§10） |

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
  承認済みのevent計画、seed予約がそろうまで正式評価は始めない。Rust必須のlisjong pin（`89dd00d`以降）では
  P2を実行できないので、P2の代わりにRust-only gate（§8.4）のPASSを使う。P3・校正・seed予約は省略しない
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
- #531以降（lisjong `89dd00d`、Python backend削除）は `backend-check --backend python` を実行前に移行案内付きで
  拒否する。保存済みのP2記録（`backend-check-v1` / `backend-comparison-v1`）は `backend-compare` で
  読み直せる。旧Python側の再実行は、記録が指定する旧Arena / lisjong revision・wheel・環境の組で行う。
  P2のschema・protocol digest・#517の証拠は変更しない

### 8.4 Rust-only gate（`rust-gate-*`、#531）

Rust必須のlisjong pinで、P2（Python全対局比較）の代わりに使う新version gate。
schemaは `arena-heuristic-candidate-engine-aabb-rust-only-gate-run-v1`（1回分）と
`arena-heuristic-candidate-engine-aabb-rust-only-gate-v1`（判定）で、P2 v1・protocol v1を上書きしない。

```text
rust-gate-run --run first  --wheel WHL --timeout-seconds T --output first.json
rust-gate-run --run second --wheel WHL --timeout-seconds T --output second.json   （別process）
rust-gate-compare --first first.json --second second.json --output gate.json      PASSでだけ終了コード0
rust-gate-verify  --first first.json --second second.json --gate gate.json        保存物から再導出
```

| 項目 | 内容 |
| --- | --- |
| 入力 | P2と同じ#472の開発用seed 472005・472006 × 6配置（12半荘）、校正の固定pair。registry外 |
| 実行 | clean で `main` に含まれるArena revision、Rust、worker 1、1半荘timeoutはCLIで明示 |
| P3 | 親と各workerの `environment_document` が一致し、実行時のpin・wheelが現行の固定値（`EXPECTED_*`） |
| native counter | 各半荘でnumeric向聴のnative呼び出し ≥ 1、親で本番factory経由のR5 native呼び出し ≥ 1。打牌候補一括評価・R5の増分も記録 |
| 記録整合性 | 第1層・第2層の局単位recordとscheduleが成り立つ（正式runと同じ検査） |
| 決定性 | 2回のrunで局数・`end_reason`・`final_points`・`CompletedMatch` 全体・選択行動の列・native counterが全半荘で完全一致 |
| 再導出 | `rust-gate-verify` は保存済みの2つのrun documentだけからgate documentを作り直し、完全一致を求める。engine・Policy・backend probeは呼ばない |

- 仕様fixture・合法手・情報境界の回帰は、gateがbindするrevisionのlisjong CIとArena CIを正本とし、
  gateはそれを再実行しない。強さ・過去結果との同一性・Champion昇格の根拠ではない
- 不一致は `FAIL` として記録する。Rustを補正せず、別のbackendへ切り替えない
- gateはその記録のlisjong revision・wheel SHA-256にだけ有効。正式eventのlockの環境と一致させる。
  pinを変えたら新しいgateを取り直す

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
- verify・report・段階2の前提の作成は、保存した `timings.json` にも実行時と同じ上限を再確認する
  （schedule wallとmax(monotonic, realtime)の経過 <= wall上限、半荘ごとの「親 + worker」RSSの既知値の和 <= 上限。
  RSSの欠測は欠測のまま扱う）
- summaryはPolicy側の保存物（段階1の `shadow.json` / `hanchans.json`、段階2の `policy-analysis.json`）を内容の
  SHA-256で拘束する（`inputs`）。summaryに現れない詳細（analysis・行動）だけを書き換えても再導出が一致しない。
  詳細の形（outcomeに応じたanalysis・行動・理由の有無、`PAIJIA_SWITCH` のanalysisの行動と記録した行動の一致、
  変化の種類と牌の一致）も読込時に確かめる。paijia・評価値は再計算しない

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

## 10. 凍結paijia候補の混成相手focal比較（#522）

Issue: [lisbun/lisjong-arena#522](https://github.com/lisbun/lisjong-arena/issues/522)
（親 lisbun/lisjong-project#84、候補 lisbun/lisjong#330、前段 #520）。実行前計画は
[`focal-mixed-recheck-522.md`](focal-mixed-recheck-522.md)。

> Status: **実装のみ**。rehearsal・seed予約・本比較は実行していない（実行は別承認）。

AABB（§1〜§7）・#520（§9）とは別のcontract（protocol `arena-heuristic-candidate-engine-focal-mixed-v1`）。
同じ卓にA/Bを2席ずつ置くのではなく、focal 1席と固定の相手3席を置き、focalだけをAとBで入れ替えた
2半荘を、同じseed・同じ相手identity・同じ席で比べる。AABBの配置表・role検査・除数120・SDは流用しない。
#520のDとは別のendpointで、合算・同一母集団の追試とは扱わない。

```text
seed s（seed順） -> rotation r = 0..3 -> 条件 A, B（1 seed 8半荘、K = 32で256半荘、1回だけ）
  seat r = focal（条件AではA、条件BではB）   seat r+1 = O1   seat r+2 = O2   seat r+3 = O3（mod 4）
D_s = sum_r (F_A(s, r) - F_B(s, r)) / 40      F = focal席のengine final_points（内部単位 1 = 0.1 pt）
```

| 項目 | 値 |
| --- | --- |
| A | `placement-aware-speed-call-suited-paijia-tie-v1` |
| B | `placement-aware-speed-call`（同じrevisionの現Champion） |
| O1 / O2 / O3 | `placement-aware-speed-call` / `targeted-honor-release-terminal-progression` / `placement-aware-speed-call-kobalab-0004-belief-paijia` |
| 設定・artifact | 全員 `{}`、学習artifactなし。factoryは `policy_catalog` の明示binding |
| 凍結値 | live環境から作るplanは lisjong `0fff891`・wheel SHA-256 `13092922…8fba` と一致しなければ拒否 |
| allocation | owner `lisbun/lisjong-arena#522`、protocol上記、split `DEVELOPMENT`、population `heuristic-candidate-engine-focal-mixed-522`、engine domain。全domainの他allocation（#520の8 + 32 seedsを含む）・registry外の既知seedと重なれば拒否 |
| rehearsal | registry外の開発用seed 472005だけ（8半荘）。判定なし |

- **focalの抽出**: 第1層rowのseatのrole `focal`（= seat r）だけで行う。条件BではfocalとO1が同じ
  identityなので、identityでは選ばない。O1のBはfocal Bへ含めない（判断時間も `focal-B` と `O1` を分ける）
- **局記録**: 局・終局の検査（K3〜K11）、第2層、manifestのI/Oは#513と同じcodeで、半荘identity
  （`rotation` / `condition`）とseat roleの検査、schema名（`…-focal-mixed-kyoku-record-v1` /
  `…-event-fact-v1` / `…-record-manifest-v1`）だけを `focal_mixed_plan.LAYOUT` が持つ（`kyoku_record.RecordLayout`）。
  AABBのrecordは `AABB_LAYOUT` のままで、互いのreaderでは読めない
- **Policy観測**: Aは `execute_policy_with_trace` で1回だけ判断し、#520と同じ検査でstage別件数と
  `PAIJIA_SWITCH` を数える。他の席は判断秒だけ。`policy-analysis.json` に分けて保存し、summaryが内容の
  SHA-256で拘束する
- 実行枠（write-once plan、live gate、1 worker、1半荘timeout、max(monotonic, realtime)のwall、親 + worker RSS、
  `STOP-INCOMPLETE` で何も公開しない、verify / reportでtimingsの上限を再確認）は#520と同じ規則

```text
focal-plan --run-kind allocated|rehearsal [--seeds S1,.. --allocation-identity ID]
           --timeout-seconds T --wall-limit-seconds W --rss-limit-bytes R --limit-enforcement TEXT
           --wheel WHL --run-output DIR --diagnostics DIR --output plan.json
focal-run --plan P     focal-verify --plan P     focal-report --plan P
```

summary: seed blockごとのfocal F_A / F_B（4 rotation）と差・D、平均・SD(K−1)・記述用区間（0を跨ぐかを明示）、
全K通りのleave-one-seed-out平均（最小値とseed、最大の正のblockを除いた値）、focal席だけの副指標
（#513の定義。率は各seedの4 focal席を合算してA−B、seed等重み。分母0はnull。条件付きの値は全seed合算）、
Aの発動数、役割別の判断秒。reportは時間・RSS・容量・時計差と、rehearsalでは256半荘への単純換算の見込み。

進行条件（実行前に固定。すべて満たすときだけ `RECONFIRMED-DEVELOPMENT`、それ以外は `NOT-RECONFIRMED-DEVELOPMENT`）:

1. D平均 > 0
2. focal素点成分（`base_points`）のA−B、seed等重み平均 >= 0
3. focal放銃率のseed等重み差 A−B <= +0.01（nullなら不成立）
4. 全32通りのleave-one-seed-out平均の最小値 > 0
5. 完走・第1層 / 第2層VALID・analysis整合・上限内（回収後のhash照合と別processのverifyで確かめる）

境界の0は内部単位の整数の和で判定する（丸めで0を正にしない）。4は開発上の予算配分条件で、有意性・
非劣性・裾の頑健性の証明ではない。`NOT-RECONFIRMED` ならv1の追加対局をせず、条件緩和・seed追加・
相手入替で救済しない。`RECONFIRMED` でもChampion昇格・稼働変更・他改修との束ねは自動で行わない。

## 11. 単独副露者genbutsu候補のshadow変更頻度gate（#524）

Issue: [lisbun/lisjong-arena#524](https://github.com/lisbun/lisjong-arena/issues/524)
（候補: lisbun/lisjong#343、PR lisbun/lisjong#345、merge `d2469f2`）。実行前計画は
[`single-open-genbutsu-activation-524.md`](single-open-genbutsu-activation-524.md)。

> Status: **実装のみ**。rehearsal・seed予約・本実行は行っていない（実行は別Issue・別承認）。

#520（§9）のplan・protocol・digestは変えず、別schemaのplan（`…-single-open-genbutsu-plan-v1`、owner #524、
population `heuristic-candidate-engine-aabb-single-open-genbutsu-activation-524`）を使う。stageは段階1
（activation）だけで、後続の192半荘screenは別の実行Issueで別planとして事前登録する。互いのplan readerは
相手のplanを受理しない。実行枠（write-once plan、live gate、1 worker、1半荘timeout、max(monotonic, realtime)の
wall、親 + worker RSS、`STOP-INCOMPLETE` で何も公開しない、verifyでtimingsの上限を再確認）と半荘の実行
（§9.2の `activation_task`）は#520と同じcodeで、判断ごとの採点proxyだけを差し替える。

| 項目 | 値 |
| --- | --- |
| A | `placement-aware-speed-call-single-open-genbutsu-v1`（catalog factory `create_placement_aware_speed_call_single_open_genbutsu` → `lisjong.policies:SingleOpenGenbutsuPlacementAwareSpeedCallPolicy`、引数なし） |
| B | `placement-aware-speed-call`（同じlisjong revisionの現Champion、Aのexact parent） |
| 設定・artifact | 両者とも `{}`、学習artifactなし |
| 規模 | 8 seeds × 1半荘（4席B、配置の入替なし）= 32 seat-hanchans。K・seedの追加はしない |
| rehearsal | registry外の開発用seed 472005・472006。判定なし（`gate` は `null`） |

```text
genbutsu-plan --run-kind allocated|rehearsal [--seeds S1,.. --allocation-identity ID]
              --timeout-seconds T --wall-limit-seconds W --rss-limit-bytes R --limit-enforcement TEXT
              --wheel WHL --run-output DIR --diagnostics DIR --output plan.json
genbutsu-activation-run --plan P     genbutsu-activation-verify --plan P
```

各判断で、Bの行動を実行経路から1回だけ取得して対局へ適用する。Aは判断ごとに新しく作った2つのinstanceで
通常実行（`execute_policy`）とtrace実行（`execute_policy_with_trace`）を1回ずつ行い、適用しない。
lisjong #343の `SingleOpenGenbutsuAnalysis` を集計し、gate・現物集合・向聴・受入・価値proxyを再計算しない。

| outcome | 意味 |
| --- | --- |
| `same` | A = B |
| `tile-type-change` / `red-only-change` | `SWITCHED` で基礎牌種が違う / 赤・通常だけが違う（**変更**として数える） |
| `tsumogiri-only` | 同じ牌の手出し・ツモ切りだけの違い（変更に数えない） |
| `out-of-scope-difference` | `SWITCHED` 以外でA ≠ B |
| `candidate-illegal-action` | Aの行動が合法候補に一致しない |
| `trace-action-mismatch` | Aの通常実行とtrace実行の行動が違う |
| `analysis-invariant-violation` | 打牌なのに `SingleOpenGenbutsuAnalysis` がない、`parent_action` が同じ入力のBの行動と違う、Aの行動が `selected_action` と違う、`SWITCHED` ⇔ 行動の変更、stageと記録fieldの有無（副露者席・現物判定・比較値）の矛盾、`parent_in_genbutsu` と現物集合の矛盾、現物でない代替、聴牌parentの比較、`qualifying_actions` が「同向聴・受入・retained・yaku非減」を満たす代替と一致しない、選択が `qualifying_actions` にない |

照合はanalysisが報告する値どうしと、実際の行動との整合だけである（代替の選び方の順位は照合しない）。
Aの判断が例外を送出した場合は分類せず、その半荘でSTOP（`STOP-INCOMPLETE`）とする。

保存物: `shadow.json`（判断ごとのoutcome・stage・A trace / A通常 / Bの判断秒。差のある判断と比較値を計算した
判断（`NO_QUALIFYING_ALTERNATIVE` / `SWITCHED`）だけlisjongのanalysisと3つの行動・理由）、`hanchans.json`
（客観値、#520と同じ形）、`summary.json`、`timings.json`。判断数はseatごとのPolicy呼び出し数と一致しなければ
ならない。summaryは `shadow.json` / `hanchans.json` を内容のSHA-256で拘束し、詳細の形も読込時に確かめる。

判定（実行前に固定、上から最初に当たったもの）:

1. `STOP-INVALID`: 上表の不正outcomeが1件以上
2. `STOP-LOW-ACTIVATION`: 変更 < 32件（分母は全32 seat-hanchans）、または変更のあるseed < 4
3. `PROCEED`

予算の判定（`STOP-BUDGET`）はここでは出さない。summaryの `screen_projection` は
`192 × mean(半荘wall − A通常秒 − (A trace秒 + B秒) / 2)` の記述値で、後続の実行Issueが承認済み枠と比べる。
summaryはstage別の件数（gate各段階）、対象副露者が1人の判断数、代替牌のある判断数、`SWITCHED` 数、outcome別の件数、
seed・seat別の変更数・副露者1人の判断数・代替のある判断数、A（trace / 通常）とBの判断秒（中央値・p95・最大）、
半荘wall・局数・RSSを持つ。verifyは出力fileのbytes・SHA-256と合計bytesを返す。変更頻度は予算配分の基準で、
検出力や改善の証拠ではない。

## 12. call-value-shadow-v1（#534）

Issue: [lisbun/lisjong-arena#534](https://github.com/lisbun/lisjong-arena/issues/534)（設計#533、解析API
lisbun/lisjong#350、merge `7fc1130`）。契約・CLI・引継ぎは[`call-value-shadow-534.md`](call-value-shadow-534.md)を正本とする。

> Status: **実装のみ**。rehearsal・seed予約・本実行は行っていない（実行は後続の実行Issue）。

#520 / #524のplan・protocol・digestは変えず、別schemaのplan（`arena-call-value-shadow-v1-plan-v1`）を使う。
実行枠（write-once、live gate、1 worker、1半荘timeout、max(monotonic, realtime)のwall、RSS、`STOP-INCOMPLETE`で
何も公開しない）は§9の`execute_plan`を共有し、#534で追加した引数（実行順・participant・`SHA256SUMS`を書く
finalize hook）だけを渡す。既定値では#520 / #524の動作は変わらない。半荘は4席Champion（`placement-aware-speed-call`）で、
call意図の成立 / 取消はactor席のplayer-safe round evidence（`on_round_evidence_complete`）だけから決める。

## 13. riichi-dama-branch-v1（#540）

Issue: [lisbun/lisjong-arena#540](https://github.com/lisbun/lisjong-arena/issues/540)（設計#539、特徴API・D arm wrapper
lisbun/lisjong#352、merge `a2b2dc9`）。契約・CLI・引継ぎは[`riichi-dama-branch-539.md`](riichi-dama-branch-539.md)を正本とする。

> Status: **実装のみ**。rehearsal・seed予約・本実行は行っていない（実行は後続の実行Issue）。

既存のplan・protocol・digestは変えず、別schemaのplan（`arena-riichi-dama-branch-v1-plan-v1`）を使う。
D armがRの結果に依存するため、実行枠（write-once、live gate、1 worker、1半荘 / 1 arm timeout、max(monotonic, realtime)の
wall、RSS、`STOP-INCOMPLETE`で何も公開しない）は§9の`run_tasks`を2段（R → D arm）で使う専用の枠にした。
Rの局記録は#513の`project_row`（専用layout、placement 0・4席Champion）で検査する。D armは`run_policy_hanchan`へ
追加した`on_delivery`の受け渡しで対象局の精算後に打ち切る（engineは変更しない。既定値では従来の呼び出しと同じ）。
