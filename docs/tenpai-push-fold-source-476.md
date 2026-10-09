# 聴牌PUSH/FOLDの対比較source producerとpilot（#476）

owner: lisbun/lisjong#254（設計5節）。wire契約は lisbun/lisjong#288 の
[tenpai-push-fold-source.md](https://github.com/lisbun/lisjong/blob/main/docs/tenpai-push-fold-source.md)。
Arenaは対局の実行と、engineが観測した局の結果の記録だけを担う。ゲート判定・`a_push`・`a_fold`の選択・
表のfit・報告はlisjongが所有する。**半荘の強さの評価ではない。**

入口は`scripts/generate_tenpai_push_fold_source_476.py`。このIssueはproducerとpilotまでで、
本番規模の実行はpilotの結果で規模とbucketの境界を事前登録してから別に行う。

## 生成の手順

1. **対照**: Champion（`PlacementAwareSpeedCallPolicy`）×4で半荘を実行する。全席・全判断について、
   Championの`PolicyDecision`をlisjongの`evaluate_tenpai_gate()`へ渡す。`None`でなければゲート判断で、
   半荘内の出現順に`ordinal`を付ける
2. **降りる側**: `has_fold_candidate`が真のゲート判断ごとに、同じseedで半荘を最初から実行する。
   判断者の席だけをlisjongの`TenpaiPushFoldPairPolicy.from_selection(path, fold_target=...)`にし、
   その局の精算が済んだ時点で止める
3. **照合**: 降りる側の実行では、指定した判断までの全判断について、入力（`DecisionContext`）と
   選んだ行動が対照と一致することを確認する。指定した判断では`a_fold`が選ばれたことを確認する。
   不一致、または指定した判断が現れないまま局が終わった場合は、何も書かずに止める
4. 全seedの完了後に3 fileを書き、lisjongの`read_source()`で読み戻す。その後に`generation.json`を書く

Arenaはゲートの条件を実装せず、合法手を直接差し替えない。Policyへ渡す`DecisionContext`は
記録用のwrapperを通しても変えない。

## 記録の決め方

`sequence`は半荘内の全席を通したselector呼び出しの通し番号（0始まり）で、#453 / #457のsourceと同じ数え方である。

局の結果はengineの`RoundCompletionFact`から取る。Arenaは点数を計算しない。

| field | 取り方 |
|---|---|
| `round_delta` | `point_deltas`の判断者の値（リーチ棒の供出、本場、供託の受け取りを含む。半荘終了時の精算は含まない） |
| `win` | 判断者が`winners`にいる場合。`points`は判断者が受け取った`settlement_transfers`のうち`HONBA`以外の合計 |
| `deal_in` | ロンで`source_seat`が判断者の場合。`points`は判断者が支払った`settlement_transfers`のうち`HONBA`以外の合計 |
| `exhaustive_draw_tenpai` | 荒牌流局の場合、判断者が`tenpai_seats`にいるか |
| `discard_passed` | 判断者の放銃で局が終わり、かつ今の打牌の後にその局で判断者のselector呼び出しがない場合だけ偽 |
| `declaration_discard` | (A)の押す側だけ。リーチ宣言の直後の、同じ席の打牌判断で選んだ打牌 |

- 今の打牌は、押す側の(B)(C)では`c0_action`、(A)では宣言牌、降りる側では`a_fold`である。
  その席のselector呼び出しがこの打牌でなければエラーにする
- **ダブロン**: wire契約の`deal_in`は和了者を1人しか持てない。判断者の打牌が2人にロンされた場合、
  `to`は判断者から見てツモ順で最も近い和了者、`points`は判断者の支払いの合計とする。
  件数を`generation.json`の`multiple_ron_deal_ins`に数える。lisjongの`L`は`to`がリーチ者の場合だけを
  数えるので、この決め方が表へ与える影響はpilotの件数を見てlisjong側で判断する
- ドラ・赤ドラ、待ちの枚数、役の有無は記録しない（lisjongが`policy_input`から計算する）
- `decisions.jsonl`は判断者の`PolicyInput`と合法手だけを持つ。他家の手牌・山はどのfileにも入れない

## 出力

```text
<output>/
    manifest.json     lisjong-tenpai-push-fold-source-manifest-v1
    decisions.jsonl   lisjong-tenpai-push-fold-decision-record-v1
    outcomes.jsonl    lisjong-tenpai-push-fold-outcome-record-v1
    generation.json   Arenaの記録（lisjongは読まない）
```

行の順序はseed昇順、`sequence`昇順、押す側→降りる側。3 fileはseed・分割・producerのrevisionだけで決まり、
同じ入力から生成し直すとbyte単位で一致する。時間などの実行ごとに変わる値は`generation.json`だけに書く。

`generation.json`は、(A)(B)(C)別のゲート判断の件数と降りる候補のある件数、(A)の宣言牌と`a_push`の
一致・不一致の件数、ダブロンの放銃の件数、降りる側の実行回数、半荘ごとの対照・降りる側の実行時間、
3 fileのbytes・行数・SHA-256、producer、runtime（Python、native moduleのSHA-256と`SOURCE_REVISION`）、
allocationを持つ。既存のoutputは上書きしない。失敗した実行は`generation.json`を持たない。

## pilotの固定値

`run`コマンドの値で、引数では変えられない。

| 項目 | 値 |
|---|---|
| seed | 938000..938015（engine domain `lisjong-engine-project-standard-v1-hanchan-v1`）。1 seed = 1半荘、rotationなし |
| 分割 | train 938000..938011（12半荘）、valid 938012..938015（4半荘） |
| 予約 | owner `lisbun/lisjong-arena#476`、protocol `tenpai-push-fold-source-pilot-v1`、population `tenpai-push-fold-source-pilot-16-hanchan`、split `TRAIN12-VALID4`。`arena_revision`は実行するmerge commit |
| 使用済み範囲 | 920000..920543、931000..931999、932000..932099、933000..933399、934000..934199、935000..935001、936000..936399、937000..937199 |
| Policy / rule | Champion ×4、`RuleSet.default()` |
| 待ちモデル | #245の`selection.json`（SHA-256 `14475264…ccc38`。lisjongの`load_selected_wait_model()`が照合する） |
| runtime | 通常版CPython 3.14、`LISJONG_SHANTEN_BACKEND=rust`、[現行pin](lisjong-native-wheel-current.md)のwheel、pin済みlisjong / lisjong-engine |

pilotのseedは本番の生成へ再利用しない。分割はlisjongの`report`コマンドを最後まで動かすためのもので、
pilotの数値で表やbucketを決めない。

## pilotの実行（ローカルのWSL）

1. 本producerをmergeし、merge commitへseed registry workflowでRESERVEDを予約する。

   ```text
   gh workflow run seed-registry.yml --repo lisbun/lisjong-arena \
     -f operation=reserve -f owner_issue=lisbun/lisjong-arena#476 \
     -f protocol=tenpai-push-fold-source-pilot-v1 \
     -f seed_domain=lisjong-engine-project-standard-v1-hanchan-v1 \
     -f purpose=tenpai-push-fold-source-pilot \
     -f population=tenpai-push-fold-source-pilot-16-hanchan \
     -f split=TRAIN12-VALID4 -f seeds=938000..938015 \
     -f arena_revision=<merge-sha> -f protocol_revision=tenpai-push-fold-source-pilot-v1 \
     -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/476
   ```

2. merge commitのcleanなcheckoutを新規venvへinstallし、現行pinのwheelを入れる。live ledgerを取得する。

   ```bash
   python -m pip install -e .
   python -m pip install --only-binary=:all: --no-index --no-deps --force-reinstall <wheel>
   git show origin/seed-registry:src/lisjong_arena/seed-ledger.json > <live-ledger.json>
   ```

3. 生成する。同じコマンドを別のoutputでもう1回実行し、3 fileのSHA-256が一致することを確認する
   （allocationはRESERVEDのまま2回とも通る）。

   ```bash
   LISJONG_SHANTEN_BACKEND=rust python scripts/generate_tenpai_push_fold_source_476.py run \
     --seed-ledger <live-ledger.json> --allocation-identity <identity> \
     --selection <selection.json> --workers 8 --output <new directory>
   ```

4. lisjongの`report`を最後まで動かす（境界とsupportの下限は動作確認用で、未登録）。

   ```bash
   python -m lisjong.learning.tenpai_push_fold_evaluation report --source <output> \
     --wall-upper-bounds 20,40 --count-upper-bounds 2,4 --minimum-support 1
   ```

5. #476へ結果を記録する: (A)(B)(C)別のゲート判断の件数、降りる候補のある件数、1半荘あたりの実行時間、
   manifestと各fileのSHA-256、lisjong・engine・arenaのrevision、wheelのSHA-256、
   `riichi_declaration_prediction`の件数。成功artifactを保持・hash確認してからallocationをCOMMITTEDにする。

対局を開始した後に失敗・中断した場合は、同じallocationで再実行せず、allocationをRETIREDにして
新しいseed範囲で予約し直す。生成物はGitへ入れない。

## 開発seedでの通し確認

`development`コマンドは、RETIREDな#385のseed（931000..931999）だけを実行できる。allocationは要らない。
経路の確認用で、出力はpilotの結果ではなく、表の作成・評価には使わない。

```bash
LISJONG_SHANTEN_BACKEND=rust python scripts/generate_tenpai_push_fold_source_476.py development \
  --train 931400..931400 --valid 931401..931401 \
  --selection <selection.json> --workers 2 --output <new directory>
```

## testの範囲

`tests/test_generate_tenpai_push_fold_source_476.py`は、実engineでRETIREDなseedを1半荘だけ実行する。
Champion・ゲート・降りる側のPolicy（lisjongが所有する境界）は軽い代役へ差し替え、lisjongの`read_source()`を
書いたfileの検査に使う。局の結果の取り方は合成した`RoundCompletionFact`で確認する。
実際のChampion・待ちモデル・native scorerを通した確認は、開発seedでの通し確認とpilotで行う。
