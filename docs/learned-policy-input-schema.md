# Learned Policy experiment-local PolicyInput feature schema

## Purpose and ownership

本書は、Issue #129で確立した最初のfeed-forward Learned Policy実験向け入力表現の
正本である。

```text
lisjong PolicyInput
    -> arena-policy-input-feature-v1
    -> arena-policy-input-tensor-v1 (8204 float32 values)
```

このschemaは`lisjong-arena`が所有する**experiment-local representation**である。
`PolicyInput`のfieldとvisibility semanticsは引き続き`lisjong`が所有し、本schemaを
canonical Learned Policy input、汎用encoder base class、または恒久production contractとは
扱わない。production昇格は、後続のbehavior-cloning vertical sliceによる実測後の別Decisionである。

encoderが受け取るdomain inputはexact typeのcurrent `PolicyInput` 1個だけである。
`DecisionContext.legal_actions`、action vocabulary legal mask、selected Action、engine state、
raw observation / log、HandBelief、teacher label、future outcomeは入力しない。

```text
PolicyInput -> feature -> tensor

DecisionContext.legal_actions -> lisjong action vocabulary legal mask
                                  # separate path
```

Issue #129 preflight時のArena mainは`35ef28484fd0cecfba7bbe9c53b0c5c5b2f515fa`、
Arenaのlisjong pinは`84e905d252d65eb37b722f195f2774fd5661d5af`である。
`lisjong` Issue #149のaction vocabulary merge revision
`a0666d24e66179a45fd6e231a3cbd489b492d162`までに`PolicyInput` contractの変更がないことも
確認した。feature pathはaction vocabularyをimportせず、今回のschema追加のためだけに
Arenaのpinを更新しない。

## Public API and identities

public moduleは`lisjong_arena.learned_policy_input`である。

| Item | Locked v1 value |
| --- | --- |
| Feature semantics ID | `arena-policy-input-feature-v1` |
| Tensor schema version | `arena-policy-input-tensor-v1` |
| Dimension | `8204` |
| Flat dtype | pure Pythonでは`tuple[float, ...]`、adapterでは`torch.float32` |
| Tensor shape | `(8204,)` |
| Tile axis | 37 semantic tile values |
| Relative-seat axis | self, shimocha, toimen, kamicha |
| Fingerprint | `cb02f8ec43861d277deaed0a0592f3d08cc4f26e351d8e27550b173f9b2059de` |

主要APIは次のとおりである。

| API | Meaning |
| --- | --- |
| `build_policy_input_feature(policy_input, *, version=...)` | exact `PolicyInput`からimmutable semantic featureを構築 |
| `tensor_values(feature, *, version=...)` | featureをfixed-size Python float tupleへflatten |
| `to_tensor(feature, *, version=...)` | torchをlazy importし、1本のfloat32 tensorへ変換 |
| `schema_fingerprint(*, version=...)` | 全index semanticsとschema headerのSHA-256 |
| `FEATURE_GROUPS` | top-level offset、length、logical shape |
| `FEATURE_INDEX_DESCRIPTORS` | index順の一意な全8204 semantic descriptor |

`to_tensor()`はmodel、batching、device placement、lossを所有しない単一sample adapterである。
package importおよびpure-Python pathではtorchをimportしない。

## Preflight feature inventory

すべてのsourceはcurrent `PolicyInput`内にあり、lisjongの許可リスト型によって当該seatから
観測可能なsnapshotである。表中の`player-safe`はそのvisibility根拠を表す。

| Field / group | Exact source | Visibility | Representation / logical shape | Flat range | Value domain / normalization | Seat, tile, order, missing semantics | Maximum and failure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Round wind | `round.round_wind` | player-safe public | 4-way one-hot `(4,)` | `[0, 4)` | EAST, SOUTH, WEST, NORTH; 0/1 | absolute round meaning; rotationしない | non-`Wind`を拒否 |
| Hand number | `round.hand_number` | player-safe public | 4-way one-hot `(4,)` | `[4, 8)` | 1..4; 0/1 | 1-based | 範囲外を拒否 |
| Dealer relation | `round.dealer_seat`, `self_seat` | player-safe public | relative-seat one-hot `(4,)` | `[8, 12)` | self/shimocha/toimen/kamicha; 0/1 | `(dealer - self) mod 4` | non-`Seat`を拒否 |
| Self wind | `round.dealer_seat`, `self_seat` | player-safe derived structure | wind one-hot `(4,)` | `[12, 16)` | EAST..NORTH; 0/1 | `(self - dealer) mod 4`; dealer relationと独立に明示 | relationとの矛盾を拒否 |
| Honba | `round.honba` | player-safe public | scalar `(1,)` | `[16, 17)` | non-negative exact int / `10` | scale超過値もclipせず保持 | negative / non-finite float32化を拒否 |
| Riichi sticks | `round.riichi_sticks` | player-safe public | scalar `(1,)` | `[17, 18)` | non-negative exact int / `10` | carry-overを含み上限を推測しない | negative / non-finite float32化を拒否 |
| Live wall | `round.live_wall_tiles_remaining` | player-safe public | scalar `(1,)` | `[18, 19)` | 0..84 / `84` | current contractのcounter上限 | `>84`を拒否 |
| Dora indicators | `round.dora_indicators` | player-safe public | 5 ordered slots x 38 | `[19, 209)` | presence + 37-tile one-hot | 公開順を保持、空slotはall-zero payload | `>5`を拒否、truncateなし |
| Player scores | `players[absolute].score` | player-safe public | 1 scalar / relative player | player block内 | exact int / `100000` | playersをself-relative順へrotate; negativeを保持 | scale超過をclipせず、non-finite float32化を拒否 |
| Player riichi | `players[absolute].riichi` | player-safe public | 3-way one-hot / relative player | player block内 | NONE, DECLARED, ACCEPTED; 0/1 | self-relative player axis | unknown enumを拒否 |
| Public melds | `players[absolute].melds` | player-safe public | 4 ordered slots x 86 / relative player | player block内 | presence, kind, tile counts, source, called tile | player axis / `from_seat`をself-relative化; meld sequenceを保持; tile multisetは37 counts | `>4/player`を拒否、truncateなし |
| Discard history | all `players[*].discards` | player-safe public | 136 global-order slots x 48 | `[1601, 8129)` | presence, discarder, tile, tsumogiri, called-by | `Discard.order`をslot indexとして保持; seatsをself-relative化 | `>136`、gap、duplicate、per-seat順序違反を拒否 |
| Own concealed tiles | `own_hand.concealed_tiles` | player-safe own-private | 37 tile counts | `[8129, 8166)` | each semantic tile count 0..4 / `4` | unordered multiset; redを独立axisで保持 | total `>14`を拒否 |
| Drawn tile | `own_hand.drawn_tile` | player-safe own-private | presence + 37 one-hot | `[8166, 8204)` | Noneまたは37-tile value | `None`はpresence 0 + all-zero payload | concealed tilesにない値を拒否 |

tile countsやseat rotationはsource valueの構造変換である。shanten、ukeire、remaining public
inventory、hand value、danger、wait prediction、HandBelief等のderived featureは追加しない。

## Complete top-level layout

flat rangeは0-based half-openである。group間にholeまたはoverlapはない。

| Group | Logical shape | Range | Length |
| --- | ---: | ---: | ---: |
| `round_wind` | `(4,)` | `[0, 4)` | 4 |
| `hand_number` | `(4,)` | `[4, 8)` | 4 |
| `dealer_relative_seat` | `(4,)` | `[8, 12)` | 4 |
| `self_wind` | `(4,)` | `[12, 16)` | 4 |
| `honba` | `(1,)` | `[16, 17)` | 1 |
| `riichi_sticks` | `(1,)` | `[17, 18)` | 1 |
| `live_wall_tiles_remaining` | `(1,)` | `[18, 19)` | 1 |
| `dora_indicators` | `(5, 38)` | `[19, 209)` | 190 |
| `players` | `(4, 348)` | `[209, 1601)` | 1392 |
| `discards` | `(136, 48)` | `[1601, 8129)` | 6528 |
| `own_hand` | `(75,)` | `[8129, 8204)` | 75 |

row-majorに、表のgroup順、slot番号昇順、各slotのsubfield順でflattenする。

## Fixed axes

### Relative seat axis

```text
index 0 = self
index 1 = shimocha
index 2 = toimen
index 3 = kamicha

relative seat = (absolute seat - self_seat) mod 4
```

`players`、dealer、discarder、`Discard.called_by`、`PublicMeld.from_seat`はすべて同じ
mappingを使う。absolute seat labelだけを全体回転した入力は同じfeatureになる。

### Wind and categorical axes

| Axis | Exact order |
| --- | --- |
| Wind | EAST, SOUTH, WEST, NORTH |
| Riichi | NONE, DECLARED, ACCEPTED |
| Meld kind | CHI, PON, DAIMINKAN, ANKAN, KAKAN |

round windは場の意味なのでseat label rotationの対象にしない。self windはdealer relationから
独立featureとして導出し、absolute seat labelを残さない。

### 37-tile axis

```text
1m 2m 3m 4m 5m 6m 7m 8m 9m
1p 2p 3p 4p 5p 6p 7p 8p 9p
1s 2s 3s 4s 5s 6s 7s 8s 9s
1z 2z 3z 4z 5z 6z 7z
5m-red 5p-red 5s-red
```

normal fiveとred fiveを別axisへ置く。34-type countだけへ落とさず、RiichiEnvの136-IDや
physical copy identityも導入しない。unordered multisetはこのaxisのcountへ変換し、
dora / discard / called tile / drawn tileのsequenceまたは単一値はone-hotにする。

## Slot layouts

### Dora slot: 38 values

| Local range | Meaning |
| ---: | --- |
| `[0, 1)` | presence |
| `[1, 38)` | 37-tile one-hot |

5 slotを公開順に置く。padding slotはpresenceとpayloadがすべて0である。valid tile slotは
presence 1かつtile one-hotがexactly 1なので、paddingと衝突しない。

### Relative player row: 348 values

| Local range | Meaning |
| ---: | --- |
| `[0, 1)` | score / 100000 |
| `[1, 4)` | riichi one-hot |
| `[4, 348)` | 4 meld slots x 86 |

player row順はself、shimocha、toimen、kamichaである。

### Meld slot: 86 values

| Local range | Meaning |
| ---: | --- |
| `[0, 1)` | presence |
| `[1, 6)` | meld kind one-hot |
| `[6, 43)` | 37 tile counts / 4 |
| `[43, 44)` | `from_seat` presence |
| `[44, 48)` | self-relative `from_seat` one-hot |
| `[48, 49)` | `called_tile` presence |
| `[49, 86)` | called tile 37-way one-hot |

CHI / PON / DAIMINKAN / KAKANはsourceとcalled tileを持つ。ANKANは両presenceが0である。
padding meldは全86値が0であり、valid ANKANとはmeld presenceで区別する。

### Global discard slot: 48 values

| Local range | Meaning |
| ---: | --- |
| `[0, 1)` | presence |
| `[1, 5)` | self-relative discarder one-hot |
| `[5, 42)` | discarded tile 37-way one-hot |
| `[42, 43)` | tsumogiri |
| `[43, 44)` | `called_by` presence |
| `[44, 48)` | self-relative `called_by` one-hot |

slot番号そのものがglobal `Discard.order`である。各seatのtuple順を勝手にsortして修復せず、
per-seat orderが昇順であること、および全seatを合わせたorderが`0..N-1`で一意・連続であることを
検証してから配置する。したがってordered public historyをcountへ集約するlossはない。

### Own-hand block: 75 values

| Local range | Flat range | Meaning |
| ---: | ---: | --- |
| `[0, 37)` | `[8129, 8166)` | concealed 37-tile counts / 4 |
| `[37, 38)` | `[8166, 8167)` | drawn tile presence |
| `[38, 75)` | `[8167, 8204)` | drawn tile one-hot |

`drawn_tile`は追加枚数でなくconcealed multiset内のmetadataである。

## Bounds, normalization, and no clipping

variable-length boundsはcorpus実測値でなく4人麻雀の構造・物理制約から決める。

| Value | v1 bound / scale | Basis |
| --- | --- | --- |
| Dora indicators | maximum 5 | initial indicator + maximum four kan indicators |
| Melds | maximum 4 per player | four hand groups |
| Global discards | maximum 136 | one entry cannot exceed the 136 physical tiles in a 4-player set |
| Concealed tiles | maximum 14 | maximum current concealed hand size |
| Live wall counter | 0..84 | current `PolicyInput` counter contract (`84 - tsumo events`) |
| Tile count channel | 0..4, divide by 4 | four physical copies per base kind; red is a separate semantic channel |
| Honba | divide by 10 | stable experiment scale; no semantic upper bound assumed |
| Riichi sticks | divide by 10 | carry-over may exceed one hand's declarations; no clipping |
| Score | divide by 100000 | initial four-player point-pool scale; negative and magnitude >1 retained |

honba、riichi sticks、scoreがscaleを超えた場合、`1.0`へclipしない。正規化後の値をそのまま
保持する。ただしPython floatまたはfloat32でfiniteに表現できない値はfail closedとする。

`PublicMeld.tiles`と`own_hand.concealed_tiles`はunordered multisetなのでinput tuple orderへ
依存しない。`dora_indicators`、meld slots、global discard orderはsequenceなのでsortしない。

## Exact input and fail-closed boundary

`PolicyInput` subclassを含め、top-levelはexact `PolicyInput`だけを受理する。構成dataclass、Enum、
tuple、int、bool、Tileも同様にcurrent contractのexact typeを要求する。これは将来fieldや意味を
追加したsubclassをv1が黙って一部だけencodeすることを防ぐためである。

次をfallback、truncate、clip、補完せず例外にする。

- unsupported feature semantics ID / tensor schema version
- wrong top-level typeまたはsubclass
- component type / enum / scalar typeの不一致
- tile axis外の値
- fixed physical length bound超過
- global discard orderのgap、duplicate、per-seat sequence違反
- discarder自身を指す`called_by`、meld owner自身を指す`from_seat`
- drawn tileがconcealed multisetに存在しない
- live wall counterの0..84逸脱
- scalarをfinite float32へ変換できない値
- padding後のnon-padding slot
- output dimension drift

`PolicyInput` contract自身が保証しないdeep tile conservation、called discardとmeldの完全照合、
ruleset-dependent red tile枚数まではencoderが再判定しない。schemaが配置のために依存する
order、type、fixed boundだけをfail-closed validation対象にする。

top-level type errorはPython `TypeError`である。schema固有の失敗は
`PolicyInputFeatureError`を基底とする次の階層で伝播する。

| Error | Condition |
| --- | --- |
| `UnsupportedFeatureSemanticsError` | unknown feature semantics ID |
| `UnsupportedTensorSchemaVersionError` | unknown tensor schema version |
| `PolicyInputFeatureValidationError` | representability、domain、finite validation failure |
| `FeatureDimensionError` | emitted length differs from 8204 |

## Compatibility fingerprint and version rule

fingerprint inputはUTF-8 textで、次を改行区切りにし、末尾にも改行を付ける。

```text
feature_semantics_id=<literal>
tensor_schema_version=<literal>
dtype=<literal>
feature_dim=<literal>
0:<index-0 semantic descriptor>
...
8203:<index-8203 semantic descriptor>
```

全descriptorはaxis value、slot番号、normalization、presence semanticsを含み、一意である。
testは`version -> expected SHA-256`をliteralとして保持し、主要group boundaryと代表indexもliteralで
固定する。

同一versionでは次を変更しない。

- dimension、group order、offset、length、logical shape、flatten order
- categorical / tile / relative-seat axis order
- slot count、presence、padding semantics
- normalization constants、dtype
- individual index semantics

breaking changeでは既存fingerprintを更新せず、新しいfeature semantics IDとtensor schema versionを
追加する。v1がexperiment-localであることと、v1 artifactの再現性を守ることは両立する。

## Relationship to other contracts

### PolicyInput

`PolicyInput`がonline visibility boundaryの正本である。本schemaはfieldを追加・変更せず、
self-relative tensor placementだけを行う。

### Action vocabulary and legal mask

`lisjong.action_vocabulary`はmodel output sideの独立contractである。legal maskをfeatureへ重複格納せず、
feature packageからaction vocabularyへ依存しない。後続vertical sliceが両者をmodelの前後でcomposeする。

### Phase 6 HandBelief feature

`phase6-history-snapshot-v1` / 919 dimensionsは
`FrozenPlayerSafeAnchor + ordered RoundEvidence prefix`を入力とするHandBelief estimator専用schemaである。
本schemaのbase class、互換schema、canonical tensor layoutではない。本Issueはそのidentity、dimension、
artifactを変更しない。

## Usage

```python
from lisjong_arena.learned_policy_input import (
    build_policy_input_feature,
    tensor_values,
)

feature = build_policy_input_feature(policy_input)
values = tensor_values(feature)
assert len(values) == 8204
```

torch extraを明示的に導入した実験では次を使用できる。

```python
from lisjong_arena.learned_policy_input import to_tensor

input_tensor = to_tensor(feature)  # shape (8204,), dtype torch.float32
```

model architecture、batching、training dataset、teacher generation、action logits、legal mask適用、
decode、artifact format、throughput、strength evaluationは後続Issueのscopeである。
