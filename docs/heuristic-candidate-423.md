# #423 最新候補対ChampionのRust実行入口

#426の依存・wheel・catalog整合に続く、実行入口の準備。
本評価・seed予約・AWS Launchは別のoperator作業である。
統計は [既存family-internal protocol](heuristic-candidate-aabb-half.md) を維持し、
100 fresh seed blocks / 400半荘、AABB、uma/oka、95% interval、判定を変更しない。

## 固定内容

| 項目 | #423の値 |
|---|---|
| owner | `lisbun/lisjong-arena#423` |
| population / split | `heuristic-candidate-aabb-423` / `FORMAL-EVAL` |
| protocol | `arena-heuristic-candidate-aabb-half-v1` |
| A | `placement-aware-speed-call-kobalab-0004-belief-paijia` |
| B | `placement-aware-speed-call` |
| A設定 | 引数なし、既定の条件付き一様Belief-paijia |
| lisjong（両者共通） | `58ef82aeb10ac77cb66290d54e67a42426919d5b` |
| engine | `8735e89e1aea000ab59368d0368d476787827741` |
| RiichiEnv | `0.4.10` |
| backend / native API | 明示的 `rust` / `2` |
| wheel SHA-256 | `a4480991f04bc2686c6790857fdbf467cd576aa9a910e05e344232a0c4a8086c` |

wheel取得・force reinstallは [current wheel手順](lisjong-native-wheel-current.md) を使う。
A/Bのfactoryはcatalogの対応する引数なしfactoryに限定し、旧候補・別設定への
差し替えは拒否する。過去比較の追加armは作らない。

## 実行入口

この変更をmergeしたclean mainで、Champion designationと評価済みrevisionからの
挙動継続性を確認し、校正と費用計画を確定した後に使用する。
`$SEEDS`はregistryで事前確保した100個の順序付きfresh seeds、`$WORKERS`は校正で
決めた値。以下は雛形であり、seed予約や実行済みの記録ではない。

```bash
export LISJONG_SHANTEN_BACKEND=rust
python -m lisjong_arena.heuristic_candidate_aabb lock \
  --event 423 --wheel "$WHEEL" \
  --out "$BUNDLE/candidate-lock.json" \
  --seeds "$SEEDS" --workers "$WORKERS" \
  --seed-ledger "$LEDGER" --allocation-binding "$ALLOCATION_BINDING" \
  --comparison-artifact "$BUNDLE/comparison.json" \
  --candidate-result "$BUNDLE/candidate-result.json" \
  --candidate-identity placement-aware-speed-call-kobalab-0004-belief-paijia \
  --candidate-factory lisjong_arena.policy_catalog:create_placement_aware_speed_call_kobalab_0004_belief_paijia \
  --candidate-source lisjong \
  --candidate-revision 58ef82aeb10ac77cb66290d54e67a42426919d5b \
  --incumbent-identity placement-aware-speed-call \
  --incumbent-factory lisjong_arena.policy_catalog:create_placement_aware_speed_call \
  --incumbent-source lisjong \
  --incumbent-revision 58ef82aeb10ac77cb66290d54e67a42426919d5b

python -m lisjong_arena.heuristic_candidate_aabb run \
  --lock "$BUNDLE/candidate-lock.json" --seed-ledger "$LEDGER" \
  --progress --progress-json "$BUNDLE/progress.json" --run-id "$RUN_ID"

python -m lisjong_arena.heuristic_candidate_aabb verify \
  --lock "$BUNDLE/candidate-lock.json" \
  --comparison "$BUNDLE/comparison.json" --result "$BUNDLE/candidate-result.json"
```

`lock_version=2`と`result_version=2`は#423専用のRust契約・実行証跡を必須とする。
#375のversion 1、bootstrap、既存証跡と既定CLI動作は維持する。
`--event 423`なしのlockに#423 allocationを渡すと停止する。
wheelの絶対pathはlockされ、実行時にも同じ位置に保持する。
保存後のverifyはwheelやnative moduleを必要とせず、収集したbundleを読める。

親processは実行前後、実workerは各seed blockの実行前後に、lisjong/engine/Arenaの
revision・依存version、native SOURCE_REVISION/API、wheel hashとロード済みファイルの
一致を検証する。各blockでgame中のnative呼出しが正であることを要求する。
親と異なるPID、seed順序、全block分の証跡、raw seat-resultのdigestを結果検証に含める。
証跡欠落・不一致・worker失敗時は比較全体を不成立とし、完走分だけを採用しない。

#423はworkers=1でもspawnを使う。1 jobは1 seedの4 rotationsで、内部は既存
`run_comparison` → `LocalGameRunner.run`。各game/seatのPolicyはfresh生成される。
完了順に関わらず結果をlockのseed順へ戻す。進捗は4半荘ずつ更新する。
失敗時には残りのworkerを終了する。typed-analysisのdurable trace保存は使わない。

## 校正・AWS実行への引継ぎ

次段階ではこのseed-block単位の実行経路を使い、遅いR5を残すBも含むAABB半荘を
DEVELOPMENT allocationで小規模校正する。formal allocation・既知結果のseedとは
分離する。worker数ごとのwall time、CPU/メモリ、throughputから400半荘の所要時間と
余裕を見積もり、instance・費用上限・停止期限を提示する。
新候補の固定decision速度比から全体時間を推定しない。

既存AWS runner / calibration / progress / Collect / fail-safeへ接続する#423用
bootstrapは、その校正計画とともに準備する。#375の固定bootstrapをそのまま起動しない。
本PRにはAWS起動・校正実行・formal seed予約・強さの判定は含めない。
Launchは具体的な費用計画へのユーザー承認後に行う。
