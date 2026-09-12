# Durable local game record

## Purpose

standard RiichiEnv `LocalGameRunner`のsuccessful completed gameを、process終了後も
training source、offline analysis、failure diagnosis、downstream replay consumerから
strictにreadbackするためのArena-owned local bundleです。

```text
LocalGameRunner
    -> completed LocalGameInspection
    -> validated staging bundle
    -> strict readback
    -> completed local record
```

このschemaはproject-wide canonical `GameRecord`ではなく、training datasetやviewer
presentation schemaでもありません。

```text
durable raw execution / decision record
    -> consumer-specific dataset / diagnostic / viewer projection
```

## Bundle v2

schema identityは`lisjong-arena-durable-local-game-record`、versionは`2`です。

| File | Meaning |
| --- | --- |
| `manifest.json` | schema、record identity、seed / game mode / max steps、backend、exact execution provenance、Seat順のPolicy identity、payload digest |
| `objective_trace.json` | #55と同じrunのlossless `GameTrace` event strings |
| `decisions.json` | step / event interval、Seat、実際のplayer-safe `PolicyInput`、legal / selected action、typed analysis |
| `result.json` | existing `LocalGameResult`と`SeatRoundStats` semantics |
| `round_results.json` | 完了した各局のauthoritative round-result fact(#207) |

すべてUTF-8 canonical JSONです。record identityはstorage pathやtimestampではなく、
schema、manifest semantic fields、4 payloadのSHA-256 digestへ結合されます。
loaderはpayload bytes、byte count、digest、record identityを再導出します。

Policy assignmentは`PolicySpec.identity`をSeat 0..3順で保持します。factoryや任意codeを
recordから復元せず、consumerはrecorded logical identityを明示的に扱います。

## Version history

```text
v1
  objective trace + decisions + aggregate/final result

v2
  + 完了した各局のauthoritative round-result facts
```

v1 bundleのsemanticsは変更していません。loaderはversion 2だけを受理し、
version 1 bundleは「per-round result factを持たない」という明示的な理由で
拒否します。version間のbest-effort補完はしません。既存のv1 bundleは書き換えず、
必要な場合はv2として作り直してください。

## Round result facts (v2)

`round_results.json`は、完了した各局のobjective terminal truthをGameTrace順に
保持します。`RoundResultCollector`が、RiichiEnvがまだauthoritativeなstateを
保持している実行時点でcaptureします。Arenaはここで麻雀ruleを評価しません。

各局が保持する値は次の通りです。

| Field | Source |
| --- | --- |
| `round_wind` / `hand_number` / `honba` / `dealer_seat` | `start_kyoku` event |
| `start_scores` / `riichi_sticks_before` | `start_kyoku` event |
| `end_scores` / `riichi_sticks_after` | 次局の`start_kyoku` event、最終局は`env.scores()` / `env.riichi_sticks` |
| `dora_indicators` | `start_kyoku.dora_marker`と局中の`dora` event |
| `riichi_seats` | `reach_accepted` event |
| `start_event_sequence` / `wins[].event_sequence` / `draw.event_sequence` | `GameTrace` event sequenceと同じ値 |
| `wins[]`: `winner_seat` / `tsumo` / `loser_seat` / `deltas` / `ura_indicators` | `hora` event |
| `wins[].scoring`: `han` / `fu` / `yakuman` / `yaku` / 支払い額 / `pao_payer` | `env.win_results`の`WinResult` |
| `draw`: `reason` / `exhaustive` / `deltas` | `ryukyoku` event |

1局で複数の`hora`が発生した場合、`wins`はevent順にすべて保持します。単一の
winnerへ畳み込みません。

### Objective trace binding

loaderは、round-result factを`objective_trace.json`のeventへstrictにbindします。
比較するのはrecorded objective value同士だけで、麻雀ruleの再計算はしません。

- 各`start_event_sequence`は実際の`start_kyoku`を指し、round wind / hand number /
  honba / dealer seat / start scores / riichi sticks / 初期dora markerが一致する
- 各`wins[].event_sequence`は実際の`hora`を指し、winner / tsumo・target semantics /
  deltas / ura markersが一致する
- `draw.event_sequence`は実際の`ryukyoku`を指し、reasonとdeltasが一致する
- 局範囲内の`dora` / `reach_accepted`から、recorded `dora_indicators` /
  `riichi_seats`が一意に一致する
- trace上の`start_kyoku`とterminal eventの件数・順序がrecorded factと完全に一致する
  (欠落・余分・順序不一致はfail closed)
- 非最終局の`end_scores` / `riichi_sticks_after`は次局の`start_kyoku`と一致する

`wins[].scoring`は`env.win_results`由来で、GameTraceに同値のfactが存在しません。
無理に再計算して照合することはせず、この照合対象から除外します。

### Capture timing and the `win_results` gap

pinned RiichiEnv 0.4.10でも、非最終局のterminal event(`hora` / `ryukyoku`)と
`end_kyoku`、そして次局の`start_kyoku`が同じ`env.step()`の中でまとめて発行され
ます。`env.step()`から戻った時点で`env.win_results` / `env.hands` / `env.melds` /
`env.dora_indicators` / `env._get_ura_markers()` / `env.scores()`はすでに次局の
stateへ置き換わっており、`env.step()`の内部へ割り込めるhookはありません。

そのためRiichiEnv 0.4.10でも、**backend-computed `WinResult`(han / fu / yaku /
yakuman / 支払い額 / pao)をcaptureできるのはgameの最終局だけ**です。それ以外の
局では`wins[].scoring`は`null`になります。`null`をnet deltaやGameTraceから
推測して埋めることはしません。`RoundResult.win_scoring_available`で判定できます。

### 現時点でcaptureできない値

次はRiichiEnv 0.4.10のexecution boundaryからauthoritativeに取得できないため、
このschemaには含めません。推測値でのfallbackも用意しません。

```text
非最終局のyaku / han / fu / yakuman / score limit
和了牌(winning tile)
和了時の手牌 / 面子構成
exhaustive drawのtenpai seat
  RiichiEnv 0.4.10のryukyoku eventはtenpais / tehaisを含まない
typed settlement decomposition
  (本供託の移動は riichi_sticks_before / riichi_sticks_after で表現する)
riichi宣言牌がriverのどれかというmarker(別Issue)
```

`wins[].ura_indicators`はRiichiEnvが`hora` eventへ無条件に載せる裏ドラ表示牌
です。和了者がriichiを宣言したことは意味しません。consumerは`riichi_seats`で
判断してください。

これらを埋めるには、RiichiEnv側が局終了時点のresult factを(次局へ進む前に)
明示的に公開するupstream contractが必要です。

## Information boundary

`objective_trace.json`はobjective executionだけを保持します。AI analysisを
GameTrace eventへ埋め込みません。

`decisions.json`の`PolicyInput`は、そのdecisionで実際にPolicyへ渡されたplayer-safe
snapshotです。Arena persistence layerはhidden hand、wall、future event、shanten、
ukeire、hand value、HandBelief label等を再計算・合成しません。

このschemaはcurrent lisjong pinが提供する次のtyped `AnalysisTrace`だけを明示的に
round-tripします。

- `TwoStepUkeireAnalysis`
- `ValueAwareTwoStepUkeireAnalysis`
- `HandValueAwareTwoStepUkeireAnalysis`
- `FiniteHorizonCompletionAnalysis`

`analysis=None`も正常値として保持します。未知のsubtypeは文字列化、`repr`、pickle、
generic dataclass serializationへfallbackせず、write / load時にfail closedします。

## Completion and strict loading

writerはcompleted `LocalGameInspection`だけを受け取ります。4 payloadとmanifestを
destinationと同じparentのstaging directoryへ書き、public loaderでstrict readback
した後に、新規destination directoryをatomicに予約します。payloadを移動してから
manifestを最後にpublishするため、途中directoryはcompleted recordとしてloadできません。

次を拒否します。

- unknown schema id / version / backend
- missing / extra / corrupt / non-canonical payload
- byte count / digest / record identity mismatch
- existing target
- seed / game mode / step / decision countのsame-run不整合
- round-result payloadのround連続性(start scores / riichi sticks)の破れ
- recorded round-result factとobjective GameTrace eventのsame-run不整合
- recorded round identityとdecision observationの`PolicyInput.round`不一致
- invalid GameTrace sequence、step ordinal、event interval
- Seatと`PolicyInput.self_seat`、selected action actorの不一致
- unsupported typed analysis

execution provenanceはexisting Arena collectorを再利用します。Arena、lisjong、
lisjong-engineのfull revision、package version、RiichiEnv / Python versionを確認できない
場合は推測してrecordを作りません。record acquisitionはcleanなArena source treeと
VCS provenanceを持つpinned dependenciesから実行してください。

## Operator CLI

operatorがdurabilityを満たすlocal output pathを選びます。Arenaはpathのretentionや
backupを管理しません。

```bash
python -m lisjong_arena.durable_local_game_record_cli record \
  --seed 12345 \
  --game-mode 4p-red-single \
  --policy 0=two-step \
  --policy 1=two-step \
  --policy 2=two-step \
  --policy 3=two-step \
  --output /durable/path/game-12345
```

curated alias以外のfirst-party Policyはexplicit referenceとlogical identityをSeatごとに
指定します。

```bash
python -m lisjong_arena.durable_local_game_record_cli record \
  --seed 12345 \
  --policy 0=lisjong.policies.some_policy:SomePolicy --policy-id 0=some-v1 \
  --policy 1=two-step --policy 2=two-step --policy 3=two-step \
  --output /durable/path/game-12345
```

fresh processからstrict loaderとbounded consumer summaryを確認できます。

```bash
python -m lisjong_arena.durable_local_game_record_cli summary \
  /durable/path/game-12345
```

Python consumerはArena-supported loaderを使用し、raw JSONを独自parseしません。

```python
from lisjong_arena import load_local_game_record

record = load_local_game_record("/durable/path/game-12345")
for step in record.inspection.step_observations:
    for decision in step.seat_decisions:
        policy_input = decision.policy_input
        legal_actions = decision.decision_trace.legal_actions
        selected_action = decision.decision_trace.selected_action
        analysis = decision.decision_trace.analysis

for round_result in record.inspection.round_results:
    round_result.round_wind, round_result.hand_number, round_result.honba
    round_result.start_scores, round_result.end_scores
    round_result.dora_indicators, round_result.riichi_seats
    for win in round_result.wins:
        win.winner_seat, win.tsumo, win.loser_seat, win.deltas
        win.ura_indicators
        win.scoring  # backendが公開していなければ None
    if round_result.draw is not None:
        round_result.draw.reason, round_result.draw.exhaustive
```

`lisjong-play #26`のReplay Viewerは、上記のtyped valueだけで局結果を描画でき
ます。`RiichiEnv`のimport、`HandEvaluator`、scorer、tenpai判定は不要です。
`wins[].scoring`が`null`の局については、scoring detailを表示できないことを
そのまま扱ってください(推測しない)。

## Non-goals

- failed / partial game public record
- canonical GameRecord / generic serializer / replay engine
- viewer / GUI / spectator mode
- training tensor、reward、Q target、label、dataset split
- historical experiment artifact migration
- database、registry、cloud upload、retention lifecycle
- Mortal、RiichiLab、first-party engineへの横展開

`lisjong-play #26`はこのstrict loaderのdownstream replay consumerですが、viewer固有の
projectionやGUI requirementをv1 schemaへ逆流させません。
