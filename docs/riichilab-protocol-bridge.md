# RiichiLab protocol-facing decision bridge

この文書は、`src/lisjong_arena/riichilab/adapter.py`他が実装する
RiichiLab-specific protocol-facing decision bridgeの責務境界、公式protocol
との既知の差異、possible-action semantic validation contractを記録する。

[Arena Issue #27](https://github.com/lisbun/lisjong-arena/issues/27)で、
lisjong `src/lisjong/riichilab_adapter/`(lisjong Issue #38/#39で確立)から
behavior-preservingにcanonical physical migrationした。current contractは
本書を正本とする。Policy / `DecisionContext` / `InternalAction` /
`execute_policy()` / RiichiEnv legal Action mapping等のAI-side semantic
contractは引き続きlisjongが正本であり、本書では複製しない。

対象moduleと責務境界(`RiichiLabSeatAdapter`が`build_decision()` /
`execute_policy()` / `RiichiEnvActionMappingSession.resolve()`を
consumerとして利用する構成、WebSocket・token・`request_id` game内
lifecycle・timeout schedulerを対象外とすること)は
[Arena Issue #27本文](https://github.com/lisbun/lisjong-arena/issues/27)、
移管元の[lisjong Issue #38](https://github.com/lisbun/lisjong/issues/38) /
[Issue #39](https://github.com/lisbun/lisjong/issues/39)を正本とする。

## 責務境界

`lisjong_arena.riichilab`配下の以下のsubmoduleが、Arena-owned canonical
protocol-facing bridgeである。

- `adapter`: `RiichiLabSeatAdapter` / `SendReadyResponse`。1 game x 1 seat
  へbindされたstateful runtime。`SeatMaterializedState`と
  `RiichiEnvActionMappingSession`をconstructorで1回だけ生成し、
  `process_request_action()`呼び出しをまたいで継続保持する
- `request_action`: `ParsedRequestAction` / `parse_request_action()`。
  RiichiLab `request_action`入力境界
- `mjai_response`: `build_mjai_response()`。resolve済みRiichiEnv Actionの
  MJAI Bot-to-Server response正規化
- `possible_action_validation`: `validate_against_possible_actions()`。
  送信前server `possible_actions` semantic validation
- `adapter_errors`: `RiichiLabAdapterError`とそのsubclass

内部処理順は`build_decision()`(lisjong) → `execute_policy()`(lisjong) →
`mapping.resolve()`(lisjong) → MJAI response変換(Arena) →
`possible_actions`検証(Arena) → `request_id` bind(Arena)である。
`docs/architecture.md`の「Policy contract / Adapter boundary」節が定める
情報境界(Policyへ渡してよいのは`DecisionContext`だけ)はこのbridgeでも
維持する。

Arena Session(`lisjong_arena.riichilab.session`)は、WebSocket接続、
`start_game` / `action_ack` / `validation_result` / `end_game`、
`request_id`のgame内lifecycle管理を引き続き担当し、Policy判断・
Observation変換・Action mapping・`possible_actions` semantic validationは
この bridgeへ委譲する。詳細は
[`docs/riichilab-client.md`](riichilab-client.md)を参照。

## Adapter error hierarchy

`RiichiLabAdapterError`は直接`Exception`を継承し、Arena lower-level
client error hierarchy(`lisjong_arena.riichilab.errors.RiichiLabClientError`)
へは**reparentしない**。

```text
RiichiLabAdapterError(Exception)
├ MalformedRequestActionError
├ ObservationDeserializeError
├ SeatMismatchError
├ PossibleActionsValidationError
└ ProtocolConversionError
```

これはbehavior-preserving migrationのための意図的な分離である。現行
ranked / validation CLIは`RiichiLabClientError`だけをcatchするため、
統合するとAdapter / Policy boundary failureのuser-facing error behaviorが
変わってしまう。Sessionはこれらの例外もラップせずそのまま伝播させる。

## RiichiEnv 0.4.8実測: `Action.to_mjai()`の出力

| Action type | `to_mjai()`が含むfield | `to_mjai()`が含まないfield |
| --- | --- | --- |
| `dahai` | `type`, `actor`, `pai` | `tsumogiri` |
| `chi` / `pon` / `daiminkan` | `type`, `actor`, `pai`, `consumed` | `target` |
| `ankan` | `type`, `actor`, `consumed` | - |
| `kakan` | `type`, `actor`, `pai`, `consumed`(元Ponの構成牌) | - |
| `reach` | `type`, `actor` | - |
| `hora`(ron/tsumo共通) | `type`, `actor` | `pai`, `target` |
| `none`(Pass) | `type`, `actor` | - |
| `ryukyoku`(九種九牌) | `type`, `actor` | `pai`(constructorのtile省略時、`0`つまり`"1m"`相当が紛れ込み得るため、この経路では明示的に無視する) |

## MJAI response構築における必要最小限のnormalization

`build_mjai_response()`は`to_mjai()`の出力をbaseとして使用し、全variantの
独自再実装をしない。

- `actor`は、resolve済みcanonical `InternalAction.actor`から明示的に上書きする
- `dahai`には、`InternalAction.tsumogiri`から`tsumogiri`を追加する
- `chi` / `pon` / `daiminkan`には、`InternalAction.target`から`target`を追加する
- `ron`(`hora`)には、`InternalAction.target`から`target`を、
  `InternalAction.winning_tile`から`pai`を追加する
- `tsumo`(`hora`)には、`target = actor`を、`InternalAction.winning_tile`から
  `pai`を追加する
- `ankan` / `kakan` / `reach` / `none` / `ryukyoku`は、`to_mjai()`の出力を
  そのまま使用する(`actor`の上書きを除く)

tile文字列の生成には、`lisjong.riichienv_adapter.tile_to_mjai()`を使用する。

## possible_actions送信前semantic validation

`validate_against_possible_actions()`は、**これからserverへ送ろうとしている
Bot-to-Server response**と、**server提示`possible_actions`の各candidate**の
両方を、同一のcandidate semantic identityへprojectionしてから比較する。

```text
send-ready Bot response --projection--> candidate semantic identity
server candidate        --projection--> candidate semantic identity
                                        -> semantic equality
```

照合対象をcanonical `InternalAction`ではなく実際の送信内容にしているのは、
`KakanAction`のようにInternalAction側が保持しない外部semantic情報(元Pon
の`consumed`)を落とさずに検証するためである。

| Action type (mjai) | candidate必須identity(照合に使うfield) | candidateに存在する場合だけ整合確認するfield |
| --- | --- | --- |
| `dahai` | tile(`pai`) | actor, tsumogiri(要求しない) |
| `reach` | (type一致のみ) | actor |
| `chi` / `pon` / `daiminkan` | called tile(`pai`), consumed tile multiset(`consumed`) | actor, target |
| `ankan` | tile multiset(`consumed`、4枚) | actor |
| `kakan` | added tile(`pai`), 元Ponのtile multiset(`consumed`、3枚) | actor |
| `hora`(ron/tsumo共通) | (type一致のみ) | pai(和了牌), actor, target |
| `none` | (type一致のみ) | actor |
| `ryukyoku` | (type一致のみ) | actor |

`hora`だけは、公式`request_action`例が示す`{"type": "hora"}`という
minimal candidateを拒否しないために、`pai`をcandidate必須identityに
含めていない。

- 比較はraw dict完全一致ではなく、上記のsemantic identityの一致で行う
- list index、候補の列挙順には依存しない
- candidateへ`actor`/`target`/`tsumogiri`が存在しなくても拒否理由にしない
  (公式のminimal candidate形をそのまま受理する)
- tile文字列は`tile_from_mjai()`で正規化し、赤五と通常五、字牌表記の
  違いを保持する
- multiset field(`consumed`)は牌のcanonical順序でソートしてから比較し、
  入力側の順序差を無視する。枚数は`chi`/`pon` 2枚、`daiminkan`/`kakan`
  3枚、`ankan` 4枚を要求する
- semantic identity上、match件数が0件ならfail closed
  (`PossibleActionsValidationError`)、1件以上ならacceptする(同一semantic
  Actionへ2件以上candidateが一致してもaccept — lisjong Issue #39の実
  `/ws/validate`でduplicate candidateが実際に提示されることを確認済み)

### malformed / unknown candidateはfail closed

forward compatibilityとして許容するのは**既知Action typeのunknown追加
field**までであり、legal candidateそのもののunknown Action typeや
required field欠落までsilent ignoreはしない。

- 許容する: 既知typeのcandidateに`display_name`等の未知fieldが増えている
- fail closedする: candidateがmappingでない / `type`欠落 / 未知Action type /
  既知typeだがrequired field欠落・型不正・tile parse不能・`consumed`不正

これらが`possible_actions`内に1件でも存在する場合、他に一致candidateが
あるかどうかにかかわらずvalidation全体をfail closedする。個々のcandidateを
skipして残りだけで成功させない。送信予定response側をcandidate identityへ
projectionできない場合も、同様にpayloadを返さずfail closedする。

### candidateが任意で持つsemantic fieldとの整合

公式Protocolは`possible_actions`の具体例(minimal形)とAction別field表の
間に一部記述差があり、candidateが`actor` / `target`を持ち得ないとまでは
断言できない。そのため、

- candidateにこれらのfieldが**無ければ**identityだけで判定する
- candidateにこれらのfieldが**あれば**、送信予定responseの同名fieldと
  矛盾しないことも確認し、矛盾する場合はそのcandidateを非一致として扱う
  (結果として一致0件になればfail closedする。誤受理側には倒れない)

`tsumogiri`はこの整合確認の対象に含めない。打牌はcandidate identityの
`pai`で一意に定まり、公式のminimal candidate例も`tsumogiri`を持たないため、
仮にcandidate側へ付随していても識別材料にも矛盾判定材料にもしない。

### `hora`のminimal candidate対応

公式`request_action`例が示す`hora` candidateのminimal形
`{"type": "hora"}`を拒否しないため、`hora`の必須identityは`type`のみと
する。`pai`(和了牌)は、`actor` / `target`と同様に**candidate側に存在
する場合だけ**送信予定responseと矛盾しないことを確認する。

- `{"type": "hora"}` → 常にidentityが一致すれば受理する(`pai`での絞り込み
  なし)
- `{"type": "hora", "pai": "5m"}` → responseの`pai`と一致する場合だけ受理、
  不一致なら非一致として扱う(結果として一致0件ならfail closed)
- `{"type": "hora", "pai": "99z"}`のように`pai`が存在するのに牌として
  parseできない場合は、無視して非一致にするのではなく、candidate
  malformedとしてvalidation全体をfail closedする

## request_action入力境界

`parse_request_action()`は次の最低限の必須fieldだけを検証する。

- `type == "request_action"`
- `request_id`: `int`(`bool`は明示的に除外)。RiichiLab Protocol v2の
  `request_id`はgame内で一意なmonotonically increasing integerである。
  monotonicity検証、previous requestとの比較、stale/duplicate判定、
  `action_ack`との対応付けはこの境界の責務ではなく、Arena Session runtimeが
  扱う。この境界が行うのは、現在の`request_id`が仕様どおりの`int`である
  ことの確認と、その値をresponseへechoすることまでである
- `possible_actions`: list-likeなcollection(内容の検証はvalidation側で行う)
- `observation`: base64文字列。`riichienv.Observation.deserialize_from_base64()`で
  復元できない場合はfail closed
- `time`は存在すれば`ParsedRequestAction.time`として保持するが、
  `DecisionContext`やPolicyへは一切渡さない
- 上記以外の未知fieldは、それだけを理由に拒否しない(forward compatibility)

## 未確認事項・既知の前提(migration元から引き継ぎ)

- 公式`possible_actions`の具体例とAction別field表の記述差(`reach` /
  `hora`等でfield表の方が多い)については、実サーバーが実際にどこまでの
  fieldをcandidateへ付けるかが未確認である。現在の実装は「無ければ
  identityだけで判定、あれば矛盾だけ確認」という両対応にしてある
  (`hora`は`pai`まで、その他は`actor` / `target`まで)
- honor牌の文字列表記(`E`/`S`/`W`/`N`/`P`/`F`/`C`)は、RiichiEnv 0.4.8の
  event JSONに対して実測した表記であり、RiichiLab server側の
  `possible_actions`が同じ表記を使うことは未確認である。差異があれば、
  該当candidateはmalformedとして扱われ、fail closedする側に働く
- 本Issueのmigration実装時点でも、live RiichiLab接続はCI必須条件では
  ない。上記未確認事項の再確認は、lisjong Issue #38/#39で記録された経緯を
  踏まえ、必要になった時点でArena側から行う

## controlled migration状態

本migration PR時点では、lisjong側`src/lisjong/riichilab_adapter/`の
legacy physical copyがまだ残っている。「protocol-facing Adapter physical
duplicate完全解消済み」とは、lisjong cleanup PRのmergeとArenaのdependency
pin syncが完了するまで記録しない。詳細な段階は`docs/architecture.md`の
「RiichiLab protocol-facing decision bridge physical migration」節を参照。
