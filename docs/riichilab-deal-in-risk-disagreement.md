# RiichiLab 放銃の危険がある局面の打牌不一致 diagnostic

Issue #455（#441 段階B）は、exact #170 corpusの強豪bot 3体が実際に選んだ打牌と、
同じ局面で現Championが選ぶ打牌の不一致を、放銃の危険がある局面に絞って記述する。

Issue #251の[strong-bot disagreement diagnostic](riichilab-disagreement-diagnostic.md)
と同じcorpus・同じobserver seamを使うが、comparatorが違う別identityのrunである。
#251のlocked inputs・結果は変更せず、同じ表に混ぜない。

強さ評価・因果推定・改修の採否判断ではない。強豪botの選択を正解として扱わない。

## 固定した入力

- corpus identity: 064b949733b13026bdbe951c9f69b90653c5977e894449d23f99ce830718b865
- manifest SHA-256: 378edce0a3a117abd47e1f400e92c59de52be2fb990947865c08d815b2f9b0ca
- 比較先bot: FuuroMaster 126、Mortal-v4b 120、zero-test2 294（bot別を必ず併記する）
- comparator alias: placement-aware-speed-call
- comparator class: PlacementAwareSpeedCallPolicy
- lisjong revision: 51e832e50a0ee71eac65e4a017f46590e7438c04
  （#441の対象期間にChampionが稼働していたrevision）
- shanten backend: rust（`LISJONG_SHANTEN_BACKEND=rust`。稼働時と同じ）
- RiichiEnv: 0.4.10
- 期待coverage: 112対局 / 判断機会 19,874 / 選択行 18,992 / 強制行 882

installされたlisjongのcommit、shanten backend、RiichiEnv versionが上と一致しない
場合は、corpusを読む前に`STOP / INVALID`で停止する。coverageが一致しない場合は
`DIAGNOSTIC BLOCKED`で停止し、集計を出さない。

## 対象局面

対象は、強豪bot席の打牌の選択行（合法手2以上、教師の行動が打牌）のうち、自席の
リーチが`NONE`で、打牌直前のplayer-visibleな情報が次のいずれかに当たるもの。

| 区分 | 条件 |
|---|---|
| R1 | 他家のリーチ成立者（`ACCEPTED`）がちょうど1人 |
| R2 | 他家のリーチ成立者が2人以上 |
| F | 他家のリーチ成立者が0人で、他家の少なくとも1人がチー・ポン・大明槓（加槓したポンを含む）を持つ。暗槓だけの手は含めない |

次は主集計に入れず、件数だけを`counts_only`へ出す。

- 強制行、打牌以外の判断（鳴き・立直宣言・和了など）
- 自席リーチ成立後の打牌
- 自席のリーチ宣言牌の選択（自席が`DECLARED`。#441 追加診断Eと同じく対象外）
- どの区分にも当たらない打牌

comparatorは#251と同じく全選択行で実行し、Policy instanceは対局×席ごとに作る。

## 集計項目

区分ごとに、bot別と3体合算で次を出す。率は分子・分母とともに出す。

1. 選択行数、一致数、不一致数。Championが打牌以外を選んだ不一致は
   `comparator_non_discard_disagreements`へ分ける。
2. 打牌同士の不一致の、打牌後の向聴数の比較（#251と同じ公開`calculate_shanten`）。
   `teacher_shanten_worse`は強豪botの打牌後の向聴数がChampionより大きい件数。
3. 区分R1・R2の打牌同士の不一致について、現物かどうかの組合せ。
4. 巡目帯（自席の何打目か。1–6 / 7–12 / 13以降）と自席の副露有無の内訳。
5. 強豪botの実際の打牌がロンされた件数（公開の`hora` event。槍槓は含めない）。
   Championの代替打牌が放銃になったかどうかは評価しない。

### 現物の判定

1人のリーチ者に対する現物は、そのリーチ者自身の捨て牌と、リーチ宣言牌より後に
他の席が切った牌である。R2は全リーチ者に対する現物だけを現物とする。筋・壁などの
危険度推定は使わない。

`PolicyInput`は宣言牌の位置を持たないので、同じ席の連続する判断の間で他家の
リーチ状態が変わった区間から宣言牌の位置を求める。鳴きで自席の手番が飛び、
位置が1つに決まらず、かつその違いで現物かどうかが変わる牌は`unavailable`へ数える。

## 実行

実装PRのmerge後、記録するrevisionのclean checkoutから実行する。rust backendの
native wheelがLinux向けなので、local環境のWSLで実行する。AWSは使わない。

    export LISJONG_SHANTEN_BACKEND=rust
    python -m lisjong_arena.riichilab_deal_in_risk_disagreement \
        --snapshot <path-to-170-snapshot.json> \
        --output-dir <path-to-170-corpus-directory> \
        --arena-revision "$(git rev-parse HEAD)" \
        > <outside-git-artifact-dir>/issue-455-aggregate.json

lisjongは上のrevisionを`git+https`のdirect URLでinstallし、同じrevisionの
native wheelを入れる。出力JSONはlocalの集計reportであり、repositoryの外へ置く。

## 情報・公開の境界

- 入力は公開MJAI eventと、replay時点のplayer-safeな`DecisionContext`に限る。
  他家の伏せ牌、山、将来の状態、強豪botの内部推定は使わない。
- Champion側は選んだ行動だけを使い、候補評価や選択理由を出力へ混ぜない。
- raw game log、変換済みの判断行、第三者botの個別判断は保存・commit・掲載しない。
  GitHubへは集計値と簡潔な解釈だけを記録する。
- 対象行は同じ対局・同じbot内で依存するので独立標本として扱わず、区間推定・検定は
  行わない。3体の合算値を上位bot群の代表として読まない。
