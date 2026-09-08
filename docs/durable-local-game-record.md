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

## Bundle v1

schema identityは`lisjong-arena-durable-local-game-record`、versionは`1`です。

| File | Meaning |
| --- | --- |
| `manifest.json` | schema、record identity、seed / game mode / max steps、backend、exact execution provenance、Seat順のPolicy identity、payload digest |
| `objective_trace.json` | #55と同じrunのlossless `GameTrace` event strings |
| `decisions.json` | step / event interval、Seat、実際のplayer-safe `PolicyInput`、legal / selected action、typed analysis |
| `result.json` | existing `LocalGameResult`と`SeatRoundStats` semantics |

すべてUTF-8 canonical JSONです。record identityはstorage pathやtimestampではなく、
schema、manifest semantic fields、3 payloadのSHA-256 digestへ結合されます。
loaderはpayload bytes、byte count、digest、record identityを再導出します。

Policy assignmentは`PolicySpec.identity`をSeat 0..3順で保持します。factoryや任意codeを
recordから復元せず、consumerはrecorded logical identityを明示的に扱います。

## Information boundary

`objective_trace.json`はobjective executionだけを保持します。AI analysisを
GameTrace eventへ埋め込みません。

`decisions.json`の`PolicyInput`は、そのdecisionで実際にPolicyへ渡されたplayer-safe
snapshotです。Arena persistence layerはhidden hand、wall、future event、shanten、
ukeire、hand value、HandBelief label等を再計算・合成しません。

schema v1はcurrent lisjong pinが提供する次のtyped `AnalysisTrace`だけを明示的に
round-tripします。

- `TwoStepUkeireAnalysis`
- `ValueAwareTwoStepUkeireAnalysis`
- `HandValueAwareTwoStepUkeireAnalysis`
- `FiniteHorizonCompletionAnalysis`

`analysis=None`も正常値として保持します。未知のsubtypeは文字列化、`repr`、pickle、
generic dataclass serializationへfallbackせず、write / load時にfail closedします。

## Completion and strict loading

writerはcompleted `LocalGameInspection`だけを受け取ります。3 payloadとmanifestを
destinationと同じparentのstaging directoryへ書き、public loaderでstrict readback
した後に、新規destination directoryをatomicに予約します。payloadを移動してから
manifestを最後にpublishするため、途中directoryはcompleted recordとしてloadできません。

次を拒否します。

- unknown schema id / version / backend
- missing / extra / corrupt / non-canonical payload
- byte count / digest / record identity mismatch
- existing target
- seed / game mode / step / decision countのsame-run不整合
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
```

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
