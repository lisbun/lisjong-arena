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
| `__main__` | `lock` / `run` / `verify` / `dev-check` / `dev-verify` |

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
