# Strength Loop開始前guard（project #100の準備）

実装owner: Arena。横断条件: [project #100](https://github.com/lisbun/lisjong-project/issues/100)、
権限: [ADR 0011](https://github.com/lisbun/lisjong-project/blob/main/docs/decisions/0011-autonomous-strength-loop.md)。

`python -m lisjong_arena.strength_loop_guard input.json --output check.json`は、
信頼されたcontrollerが取得・照合したsnapshotに対して開始前の資源条件を判定する。
外部サービス取得・scheduler・停止スイッチ設置・通知送信は行わない。
`RESOURCE_CHECK_PASSED`は承認でも実行許可証でもない。#98の未決事項を解消しない。

## 入力

全field必須、余分なfieldを拒否。実行例ではなくfield定義である。

| object | fields / 意味 |
| --- | --- |
| root | `schema="arena-strength-loop-guard-v1"`, `policy`, `snapshot`, `request` |
| policy | `enabled` bool、`month` UTC請求月YYYY-MM、`budget_microusd`、`max_age_seconds`正整数、`stagnation_limit`正整数、`weekly_session_limit` / `weekly_pr_limit`非負整数 |
| snapshot | `as_of` UTC RFC3339、`paused` bool、`billing_complete` bool、`billed_microusd`、`runs`配列、`cycles_without_promotion`非負整数、`iso_week` YYYY-Www、`sessions_this_week` / `prs_this_week`非負整数 |
| request | `action`は`development_run` / `llm_session` / `candidate_pr`のみ。`estimated_microusd`は次の操作全体の上限見積り |
| runs[] | `run_id`一意、`estimated_microusd`当月全体の上限見積り、`billed_microusd`既反映分、`finalized`最終請求との照合完了bool |

金額objectはすべて`{"aws": 非負整数, "llm": 非負整数}`で、単位はUSDの百万分の一。
float、bool、nullを金額としない。円等からの換算が必要ならcontrollerで出典と換算時点を固定する。
未確定のLLM予算を0支出や無制限に置換しない。未決設定は入力を完成できず停止する。

AWS上限は既存承認に従い2026-10だけUSD 30、他月はUSD 20。これは総額上限であり、
loop配分や個別Launch承認ではない。実運用設定のbudgetは承認されたloop配分以下にする。

## 判定

- 無効化、pause、請求照合未完、停滞上限到達でSTOP。
- 古い/未来のsnapshot、月/週不一致、未知schema、重複run、請求内訳矛盾はINVALID。
- 請求総額はcredit適用前で、loop外の消費も含める。
  未確定runの未反映額は`上限見積り − 既反映分`。総請求には既反映分が既に含まれるため二重加算しない。
  未確定runで請求が見積りを超えたら残額0と仮定せず停止し、再見積り・照合を要求する。
- `finalized=true`は最終請求照合済みのrunだけ。単にprocessが終わったこととは異なる。
  全runの請求内訳合計が総請求を超えれば拒否する。
- `総請求 + 未反映額 + 次操作見積り`が上限以上でSTOP、80%以上ならwarning。
  予算0はそのserviceを含む開始を閉じる保守的設定で、無料扱いの迂回路にしない。
- LLM session / PR作成は対応する週上限到達でもSTOP。UTC ISO週を使う。
- S3・merge・昇格はこのguardの対象actionとして受理しない。別の承認・正式gateが必要。

出力は機械集計した金額・停滞/週count・理由/警告。write-onceで保存し再読込する。
STOP/INVALIDのexit codeは2、資源チェック通過は0。月次ダイジェストの数値欄に再利用できるが、
月の全イベントを収集したダイジェストそのものではない。LLM所見や昇格判断を生成しない。

## controllerへ接続する際の必須条件

1. #98で予算・停止スイッチの正本・運用条件を確定する。候補/LLM sessionにpolicy/snapshotの編集権を渡さない。
2. authoritative sourceからpauseと費用を取得。取得不能をpaused=falseや支出0にしない。
   `as_of`は作成時刻の付替えでなく、必要sourceのうち最古の観測時点とする。
3. 月をまたぐrunは当月分と将来の予約を別途正しく管理し、欠落する場合は`billing_complete=false`で止める。
   Cost Explorerの反映遅延は未反映runの上限見積りで埋める。
4. 各開始前に再判定し、controllerの単一writer内で新runの費用予約と開始を直列化する。
   このpure functionだけでは並行開始による予算超過を防げない。
5. CLI非zero/例外なら起動しない。検査後の古いPASS fileを次の操作へ使い回さない。
   pauseは新規開始を止める条件であり、実行中runの強制killやseed返却を意味しない。
6. warning/STOPの通知と月次artifact集約を接続し、候補loopが必ずguardを通るintegration testを加える。

現時点は合成入力による判定部の検証まで。実請求取得、停止スイッチ取得、通知、月次集約、
実controllerの迂回不能性は未検証。#100/#97の完了・無人運用開始とは扱わない。
