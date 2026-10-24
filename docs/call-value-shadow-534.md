# call-value-shadow-v1：鳴き直後の採用閾値割れshadow（#534の実装）

設計（正本）: [#533](https://github.com/lisbun/lisjong-arena/issues/533)。解析API:
[lisjong #350](https://github.com/lisbun/lisjong/issues/350)（`lisjong.policies.speed_call_analysis`、
[API文書](https://github.com/lisbun/lisjong/blob/7fc1130f9a6498d0de809f8128608b753c829415/docs/call-value-shadow-analysis-api.md)）。
本書は#534で実装した観測・plan・判定・strict verifyの契約である。**実行は後続の実行Issueで行う**
（本Issueでは新規対局・rehearsal・seed予約・課金を行っていない）。

Heuristic仮説のDEVELOPMENT診断で、強さ比較・S1/S2・正式評価ではない。

## 1. 問いと記録するもの

通常modeの門前から1向聴へ進むspeed-callで、採用閾値（2）を直後の実打牌が割るケースに、
検討に値する頻度と速度損失の範囲があるか（#533 §1）。

- 4席とも同じrevisionの`placement-aware-speed-call`（Champion、設定`{}`）。対局へ適用するのは常に
  Championの行動だけ。Policy・Championの挙動は変えない
- 主対象・分類・代替・速度損失はlisjong #350のAPIが返す値をそのまま使う。Arenaはroute・向聴・
  打点proxy・受入・completion mass・代替・分類を再計算しない（照合はlisjongの値どうしの整合と、
  保存contextからの同じAPIの再導出だけ）
- VALUE / SPEED mode、門前→聴牌、既副露の鳴き、BELOW_MAXは別集計（`secondary`）だけで、判定に使わない

## 2. call意図 → 成立 / 取消 → 直後打牌（観測）

`call_value_shadow_worker.CallValueObserver`（半荘ごと、Arena観測側だけのcross-decision状態）。

| 段階 | 内容 |
| --- | --- |
| call意図 | Championが合法なChi / Ponを選んだ判断。key = (seed, placement 0, round ordinal, actor, decision ordinal, exact call)。`response_ordinal`は局内のcall機会の番号。`analyze_speed_call_decision(public context, call)`を記録 |
| 成立 / 取消 | 局の終了（engine `on_round_evidence_complete`）で、**actor席のplayer-safe evidence**だけから決める（下表） |
| 直後打牌 | ESTABLISHEDかつ`SPEED_CALL`だけ、同じ局・同じ席の次の判断へ一対一に結び、`analyze_post_call_discard(その実際のcontext, call解析, 実打牌)`を記録 |

公開evidenceの読み方（鳴かれた打牌の`DiscardEvidence.order` = 判断時のtarget河の最後の`order`）:

| 打牌のresponse epochの公開outcome | 次のevidence | 結果 |
| --- | --- | --- |
| `CALL` | `MeldCalled`（同じorder、seat = actor、meldがcallと一致） | `ESTABLISHED` |
| `CALL` | `MeldCalled`（同じorder、別席） | `CANCELLED-PRIORITY-CALL` |
| `RON` | — | `CANCELLED-RON` |
| `NO_PUBLIC_RESPONSE` | `RoundEnded` | `CANCELLED-ROUND-END` |
| それ以外 / 打牌・epochがない / meldやorderが違う | — | `INVALID`（理由不明の取消・不整合） |

槓ドラ公開・立直成立 / 失敗（epoch解決後に保留から出るevidence）と宣言牌の立直宣言は読み飛ばす。
次の場合もINVALIDとして記録する（対局は続け、判定はSTOP-INVALID）:

- 成立したのに次の判断がない、別局の判断、同じ直後打牌への二重対応、直後contextの最後の副露が成立meldでない
- 局の終了時の局identity（場風・局・本場・親）がcall時のcontextと違う、半荘の終了時にpendingが残る
- lisjongがcall / post-call解析を拒否する（`SpeedCallAnalysisError`、tile conservation等）、保存contextが読み戻せない

情報境界: 成立 / 取消の判定に使う後続eventはArena観測側だけが読む。解析の入力はその判断の
public `DecisionContext`だけで、後続event・他席の情報・privileged observer data（`on_transaction_observation`）を
Policy・解析へ渡さない。単に「次に呼ばれた打牌」を対応させず、成立をevidenceで確かめてから結ぶ。

public contextは`durable_local_game_record`のPolicyInput v3（lossless）と合法手の元の順序で保存し、
読み戻した`DecisionContext`が元と等しいことを記録時に要求する。

## 3. 本番実行と判定（#533 §5・§6）

| 項目 | 固定値 |
| --- | --- |
| 本実行 | seeds 533000..533023（24 seeds）× 1半荘、4席Champion、配置の入替なし = 24半荘 / 96 seat-hanchans |
| seed authority | Seed Registryの既存engine hanchan domain、protocol `call-value-shadow-v1`、population `heuristic-candidate-engine-aabb-call-value-shadow-v1`、split DEVELOPMENT、owner = 後続の実行Issue |
| rehearsal | 既知dev seed 472005 / 472006（registry外）で観測あり / なしを別planで各1回（計4半荘）。本集計に入れない |
| 1半荘timeout | 600秒 |
| process tree | 4 GiB |
| stage wall | rehearsal 1時間 / 本実行4時間（max(monotonic, realtime)で判定） |
| worker | 1 |
| schema | plan `arena-call-value-shadow-v1-plan-v1`、shadow `arena-call-value-shadow-v1-shadow-v1`、hanchans `arena-call-value-shadow-v1-hanchans-v1`、summary `arena-call-value-shadow-v1-summary-v1`、verify `arena-call-value-shadow-v1-verify-v1`、compare `arena-call-value-shadow-v1-rehearsal-compare-v1`、stop `arena-call-value-shadow-v1-stop-v1` |

重複seedがある（全domainの他allocationと重なる）場合はplanを作れない。別seedへ移さず本versionを再設計する。

判定は次の順に1つだけ（allocatedのみ。rehearsalは判定なし）:

1. **STOP-INVALID**: INVALIDのcall記録、lisjongの値どうしの不整合（`0 <= M <= D`違反、`D <= 0`、
   `A > B`、`B = 0 < A`、速度損失と候補massの不一致、閾値を保たない代替、ツモ切りflagだけの差）が1件以上
2. **STOP-INCOMPLETE**: timeout / OOM / 上限超過 / 未完了。保存物を公開せず、summaryを作らない
3. **STOP-LOW-ACTIVATION**: 主対象の変更が12件未満、または変更のあるseedが6未満
4. **STOP-SPEED-TRADEOFF**: 主対象の全変更行でLの中央値 > 1/5、またはp90 > 2/5
5. **PROCEED-TO-CANDIDATE-DESIGN**: 上記に該当しない（棋力改善・候補採用・S1/S2通過ではない）

- 主対象 = lisjong `is_primary_target`（SPEED_CALL・NORMAL・門前・route 1向聴・閾値2）。
  主対象のBELOW_THRESHOLDでは代替は必ず実打牌と牌種か赤identityが違う（変更）
- `L = (B - A) / B`はlisjongの整数の分子 / 分母。`B = A = 0`は`L = 0`で`zero_mass`を数える
- 中央値は偶数のとき中央2値の平均、p90は昇順`ceil(0.9 n)`番目（nearest rank）。`fractions.Fraction`で厳密に比べ、floatで判定しない
- summaryは全判断数、call機会、call意図（route別）、成立 / 取消（理由別）、speed-call成立、主対象call、閾値割れ、
  代替あり、変更（赤のみ / 牌種）、zero mass、seed別・seat別、主対象の各変更行（B・A・N・D・horizon・差・proxy差・
  受入差・value差）を分子 / 分母付きで持つ。判断行を独立標本とした信頼区間・検定は出さない
- VALUE・BELOW_MAXの良い結果で主判定を救済しない。指標・seed・対象・代替ruleを結果を見て変えず、追加run・retry・延長なし

## 4. 保存物（write-once）とstrict verify

推奨layout（plan digestは保存先を含むので、directory名は実行Issueで決めるlabel）:

```text
<root>/call-value-shadow-v1/<label>/
  plan.json                   call-value-plan の出力（write-once）
  output/                     公開する保存物（下表、生成側で全件揃ったときだけ公開）
  diagnostics/                STOP-INCOMPLETEのstop記録（成功outputへ昇格しない）
  verify.json                 call-value-verify --output
  environment / ledger-before / allocation / gate・probe証拠、run.log（実行Issueで保存）
```

| file | 内容 |
| --- | --- |
| `shadow.jsonl` | 1行目header（schema `arena-call-value-shadow-v1-shadow-v1`、plan digest）、以降は半荘ごとに1行: 座席別の判断数・call機会数、call意図ごとの対応・取消・lisjong解析・public pre / post context。private truthなし |
| `hanchans.json` | 客観値: 局数・終了理由・final_points・`CompletedMatch` digest・呼び出し数・席ごとの本番Action列digest |
| `summary.json` | 固定分母・分類・分位・判定、`shadow.jsonl`と`hanchans.json`のdigest |
| `timings.json` | 半荘ごとのwall・peak RSS、stageの経過 |
| `SHA256SUMS` | 上の全fileのSHA-256 |

観測なしrehearsalは`shadow.jsonl`を持たない。analysis（shadow）と客観記録（hanchans）は別file。
shadowの判断は対局しないのでA/Bの勝率・収支は計算しない。

`call-value-verify`（別process、保存物だけを読む）は次をすべて要求する:

- `SHA256SUMS`と実fileの完全一致（欠落・追加・変更を拒否）、期待するfile集合
- full schedule（seed順・不足・重複・順序の入替を拒否）、判断数 = Policy呼び出し数、call / post ordinalの範囲と一対一
- 再導出に使うlisjong / native / Python / engineがplanと同じこと
- 保存したpublic contextから同じrevisionのlisjong解析を再導出し、記録したcall / post-call解析・主対象flagと完全一致
- summary（分類・分位・判定）の再導出と記録の完全一致、timingsの上限

生成側と回収先で`SHA256SUMS`・verify出力の`files`を照合する。回収できなければ完了としない。

## 5. CLI

```bash
python -m lisjong_arena.heuristic_candidate_engine_aabb call-value-plan \
  --run-kind {allocated,rehearsal} --observation {observed,unobserved} \
  --owner-issue lisbun/lisjong-arena#N [--allocation-identity ID] \
  --limit-enforcement "<OS / process treeの強制方法>" --wheel W \
  --run-output <label>/output --diagnostics <label>/diagnostics --output <label>/plan.json
python -m lisjong_arena.heuristic_candidate_engine_aabb call-value-run --plan P
python -m lisjong_arena.heuristic_candidate_engine_aabb call-value-verify --plan P --output V
python -m lisjong_arena.heuristic_candidate_engine_aabb call-value-rehearsal-compare \
  --observed P1 --unobserved P2 --output C
```

- seeds・timeout・RSS・wall・判定はprotocol invariantでCLIから変えられない。`--allocation-identity`はallocatedだけ
- planはclean merged mainのHEAD・実行環境（lisjong / native / engine）・RuleSet全field / digestを束縛する。
  run前後のlive gateでHEAD・環境・allocationを再照合する
- `call-value-rehearsal-compare`は観測あり / なしのrehearsal plan（同じ環境・HEAD）を両方verifyし、
  seedごとの本番Action列digest・`CompletedMatch` digest・終局・最終点・局数・呼び出し数の一致を要求する。
  不一致はSTOP-INVALID（照合記録は書く）

## 6. 実行Issueへの引継ぎ（未確定の実値）

実行前に次を実値で揃え、planへ束縛する（本書の時点では存在しない値を捏造しない）:

1. **revision**: Arena #534のmerge後main full SHAとclean checkout、lisjong pin `7fc1130f9a6498d0de809f8128608b753c829415`、
   engine `91af75e3aa11520c3b0543719dc74bb4c517ee06`
2. **wheel**: lisjong main CI run [40087058106](https://github.com/lisbun/lisjong/actions/runs/40087058106)の
   `lisjong-native-wheel-7fc1130…`、`lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（301,070 bytes、
   SHA-256 `02d11385688707bcda5b345499e07b637d981960cfb9a2894c2b7cba123cc634`）。実行環境で`sha256sum --strict -c SHA256SUMS`、
   `verify-wheel`、`aws_backend --backend rust --wheel`（SOURCE_REVISION / API 3 / numeric・R5 probe）を取り直す
3. **Rust-only採用gate**: この新pinで[Rust-only gate](engine-aabb-evaluation.md#84-rust-only-gaterust-gate-531)
   （`rust-gate-run` ×2 → `rust-gate-compare` → `rust-gate-verify`）をPASSさせる。#531の`89dd00d` PASSは流用しない
4. **環境**: AI作業環境のLinux x86_64 / 通常CPython 3.14 / native API 3。OS / glibc / CPU / memory / Python・
   実行機器identity、artifactのdurable回収方法。ユーザーPC / WSL / AWSへ自動移行しない
5. **上限の強制**: OS / process-tree上限（4 GiB、swapなし）と外側watchdog（max(realtime, monotonic)）、
   全実行工程6時間。kill / timeout / memoryの合成試験を実行環境で確認し、`--limit-enforcement`に方法を書く
6. **seed lock**: 全domainの既存allocation＋既知registry外seedと533000..533023を照合し、既存workflowで
   reserve / readback（owner = 実行Issue、protocol `call-value-shadow-v1`、domain engine hanchan）。
   reserve前に実行権限を確認する。使用後は既存規則でCOMMITTED、途中失敗もFREEへ戻さない
7. **順序**: rehearsal（observed / unobserved各plan → run → verify → compare、1時間以内）→ 本実行plan → run →
   verify → 回収とhash照合（4時間以内）。各段階と全体の上限を同時に適用する

## 7. testで固定したこと

[test_engine_aabb_call_value_shadow.py](../tests/test_engine_aabb_call_value_shadow.py):

- 公開evidenceの成立 / 優先鳴き / ロン / 局終了取消、理由不明の取消・別牌・別席・別meld・別order・閉じないepochの拒否
- 観測: 成立したspeed callが保存記録と一致、Champion行動不変、取消は直後打牌を取らない、次の判断なし・
  別局・pending残留・副露のない直後context・誤った打牌・解析拒否のINVALID、赤5を含むcontextの往復
- lisjongの値: R1 / 喰い替えの損失が整数比で一致、VALUE / BELOW_MAXは主対象外、損失・代替・ツモ切りflag・
  `M > D`・`B = 0 < A`・`D <= 0`の不整合拒否、保存contextからの再導出と改ざん検出
- 判定: 12件 / 6 seedsの境界、中央値1/5・p90 2/5の境界（含む）、偶数中央値、nearest rank、0 mass、INVALID優先
- plan: 固定seeds・上限・RuleSet・Champion設定、owner / protocol / 重複seed / 観測なしallocatedの拒否、drift検出、write-once
- run / verify: PROCEED / LOW / INVALID、部分実行の非公開とstop記録、別process verify、hash・欠落・追加・重複・
  順序・判断数・二重対応・解析値の改ざん検出、環境不一致の拒否、rehearsalのAction列照合

fixture `_call_value_shadow_fixtures.json`は、開発用smoke（registry外seed 102..107の実engine半荘、評価ではない）の
実call記録4件と、lisjong #348 / #350の局面fixture（R1、喰い替え、BELOW_MAX、VALUE、赤5）から生成した。
smoke（registry外seed 101..107の7半荘）ではINVALID 0件、優先鳴きによる取消を1件観測し、保存contextから
計90件の解析を再導出した。主対象のBELOW_THRESHOLDは出現しなかった（頻度・損失の推定ではない）。
