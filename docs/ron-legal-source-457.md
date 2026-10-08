# ロン合法source producer（#457）

lisjong #262の固定済み追加source契約をengine #61のprivileged transaction
observationから記録する。`scripts/generate_ron_legal_source_457.py`を入口とし、
通常のPolicy selectorとは別のoffline記録経路を使用する。

base v1は変更せず、同じselector連番から`base/`と`ron/`を生成する。
全commitとその内部stepを記録し、factsのsnapshot境界は行動適用前の半開prefix。
暗槓の機会なしstepは固定RuleSetでreactionへ変換せず、宣言と成立を同じ外側行に残す。
公開河のorderはengineの打牌eventから取得する。手牌はツモ牌を含んだcheckpointを
そのまま使い、見逃し状態・合法候補・解決結果はengineの観測factだけを投影する。

## 検証とpilotの状態

producer PRは実装と合成fixtureの接続検証を担当する。実対局pilotは未実施。
固定山fixtureは新規seed populationを実行したものではなく、実測の代替にしない。
通常CIではnative scorerがない場合のみreader接続testをskipし、専用
`ron-source-native` jobでは固定revisionのscorerを実際にbuildしてskipを禁止する。

pilotはproducerのマージ後に、seed registryで当該merge revisionへ予約する。
現在のregistry workflowは`arena_revision`がマージ済みcommitであることを確認するため、
PR headの実行を予約済みpilotとして扱わない。

## 実行前に固定する条件

- 2半荘、PlacementAwareSpeedCallPolicyを各席・各半荘で新規生成。
- `RuleSet.default()`（project-standard-v1）全体を固定。
- `ron-legal-source-pilot-v1`、`ron-legal-source-pilot-2-hanchan`、
  split `TRAIN1-VALID1-TEST0`。昇順2seedの先頭をtrain、後ろをvalid、testは空。
- domain `lisjong-engine-project-standard-v1-hanchan-v1`（既存registryで利用可能）。
- cleanなArena merge revision、pin済みlisjong/engine、通常CPython 3.14、
  Rust shanten、同じlisjong revisionからbuildしたnative scorer。

まずlive `seed-registry` branchのledgerを取得し、既存#259/#260・正式評価・
過去pilot/RETIREDを含む使用済みseedとの衝突がない新規2seedを選ぶ。
`main`のbootstrap ledgerや手編集したledgerは予約の代替ではない。
以下のworkflowへowner/protocol/population/splitと正確なmerge revisionを渡して予約する。

```text
gh workflow run seed-registry.yml --repo lisbun/lisjong-arena \
  -f operation=reserve -f owner_issue=lisbun/lisjong-arena#457 \
  -f protocol=ron-legal-source-pilot-v1 \
  -f seed_domain=lisjong-engine-project-standard-v1-hanchan-v1 \
  -f purpose=ron-source-pilot -f population=ron-legal-source-pilot-2-hanchan \
  -f split=TRAIN1-VALID1-TEST0 -f seeds=<first>..<second> \
  -f arena_revision=<merge-sha> -f protocol_revision=ron-legal-source-pilot-v1 \
  -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/457
```

workflow成功後、live ledgerを再取得し、allocation identityを指定して実行する。
`--seed-ledger`にはそのauthority snapshotを渡す。実行前にIssueへledger revision、
allocation binding、3 repository revision、runtime/backend、保存先を記録する。

```text
LISJONG_SHANTEN_BACKEND=rust python scripts/generate_ron_legal_source_457.py \
  --seed-ledger <live-ledger.json> --allocation-identity <identity> \
  --seeds <first> <second> --output <new-directory>
```

既存output・planは上書きしない。事前確認後、最初の対局より前にoutputの隣へ
`<output-name>.plan.json`を排他的に作り、同じ条件をstdoutへflushする。planも保持する。
全2半荘を完了した後にsourceを書き、base coverage、
`read_ron_source()`、`read_labelled_ron_source()`を全件通してから最後に
`complete.json`を作る。例外時は完了recordを作らず、partial sourceを採用しない。
再実行する場合は失敗seedを再利用せず、先にRETIREDへ遷移する。

完了recordにはallocation/provenance、Python/native/backend identity、全局counter、
source hashes/bytes/rows、reader/labelled判断数、リーチ/門前非リーチ/副露の
ロン正例snapshot・牌数、観測event数を保持する。得られなかった履歴ケースは
報告時に明示する。希少ケースの網羅や精度改善・Policy強さは主張しない。
成功artifactを保持・hash確認してからallocationをCOMMITTEDにし、生成物はGitへ入れない。

#259 select、#260学習、#262段階Cの正式baseline測定はこの実行に含めない。
#457はpilot報告までopen、lisjong #262も本producer PRではcloseしない。
