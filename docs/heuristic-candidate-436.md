# #436 1向聴守備候補対Championの正式評価

lisjong #235で追加した候補を、現Championとfresh 100 seed blocks / 400半荘で比較する。
[既存protocol](heuristic-candidate-aabb-half.md)のAABB・4 rotations・uma/oka・
95% interval・判定基準をそのまま使う。開発用80半荘の結果は正式な昇格根拠に含めない。
この実装PRは入口の準備であり、seed予約・正式実行・Champion変更を行った記録ではない。

| 項目 | 固定値 |
| --- | --- |
| owner | `lisbun/lisjong-arena#436` |
| protocol | `arena-heuristic-candidate-aabb-half-v1` |
| population / split | `heuristic-candidate-aabb-436` / `FORMAL-EVAL` |
| A | `one-shanten-defense-placement-aware-speed-call` |
| B | `placement-aware-speed-call` |
| A/B設定 | catalogの引数なしfactory |
| lisjong（両者） | `e6346ed2bb9e992138c05c4be367bd6a05ed00bc` |
| engine | `8735e89e1aea000ab59368d0368d476787827741` |
| RiichiEnv | `0.4.10` |
| native API / backend | `3` / 明示的 `rust` |
| wheel SHA-256 | `b14e53fea4161cb81c7912ead2c9d1206c2b95a1b19eb4999c6c2b7a056fdbe6` |
| 実行案 | local Linux x86_64 / 通常版CPython 3.14 / 8 workers |

wheelは [現行wheel手順](lisjong-native-wheel-current.md)で取得する。
#423のwheel・participant lock・allocationを流用しない。
#375 / #423の保存済みbundleは当時の固定契約で検証し、今回の条件へ改名しない。

## 実行前

1. この変更を含む**承認済みのclean merged main**をcheckoutする。
2. 新規venvへArenaをinstallし、`environment_verify --project pyproject.toml`でVCS依存を確認する。
3. revision別directoryへ取得したwheelをforce reinstallし、下記probeを実行する。
4. lisjongの`policy-status.md`でChampionが引き続きBであることを確認する。
5. live `seed-registry` branchを確認し、未使用100 seedsを下記workflowで予約する。

```bash
python -m lisjong_arena.environment_verify --project pyproject.toml
python -m lisjong_arena.shanten_backend_verification verify-wheel "$WHEEL"
python -m pip install --only-binary=:all: --no-index --no-deps --force-reinstall "$WHEEL"
export LISJONG_SHANTEN_BACKEND=rust
python -m lisjong_arena.shanten_backend_verification probe --backend rust --wheel "$WHEEL"
```

`$ARENA`は実行checkoutの完全なmerged commit SHA。`$SEEDS`はregistry書式の
`start..end`または列挙で100個。rangeはまだ予約済みではなく、live確認時に決める。
並列更新を直列化する**Arena Seed Registry workflowだけ**をauthority更新に使う。
local ledgerの編集・Git refの直接更新で予約を代替しない。

```bash
gh -R lisbun/lisjong-arena workflow run seed-registry.yml --ref main \
  -f operation=reserve -f owner_issue=lisbun/lisjong-arena#436 \
  -f protocol=arena-heuristic-candidate-aabb-half-v1 \
  -f seed_domain=riichienv-4p-red-half-hanchan-v1 \
  -f purpose='#436 one-shanten defense vs Champion formal AABB 400' \
  -f population=heuristic-candidate-aabb-436 -f split=FORMAL-EVAL \
  -f seeds="$SEEDS" -f arena_revision="$ARENA" \
  -f protocol_revision=arena-heuristic-candidate-aabb-half-v1 \
  -f provenance_reference=https://github.com/lisbun/lisjong-arena/issues/436
```

workflow完了後のlive ledgerと、`show`が返すbindingをそれぞれ`$LEDGER`、
`$ALLOCATION_BINDING`へ保存する。重複reserveをしない。lock/runにはCLI書式
`start:end`（または列挙）の`$EVAL_SEEDS`を使い、同じ100 seedsと順序を確認する。

## Lock・run・verify

`$BUNDLE`は空の新規出力directory、`$WHEEL`は絶対path。lock保存後はwheelの
位置・実行revision・worker数を変更しない。runは全400半荘が成功しない限り不成立とする。

```bash
python -m lisjong_arena.heuristic_candidate_aabb lock \
  --event 436 --wheel "$WHEEL" --out "$BUNDLE/candidate-lock.json" \
  --seeds "$EVAL_SEEDS" --workers 8 \
  --seed-ledger "$LEDGER" --allocation-binding "$ALLOCATION_BINDING" \
  --comparison-artifact "$BUNDLE/comparison.json" \
  --candidate-result "$BUNDLE/candidate-result.json" \
  --candidate-identity one-shanten-defense-placement-aware-speed-call \
  --candidate-factory lisjong_arena.policy_catalog:create_one_shanten_defense_placement_aware_speed_call \
  --candidate-source lisjong --candidate-revision e6346ed2bb9e992138c05c4be367bd6a05ed00bc \
  --incumbent-identity placement-aware-speed-call \
  --incumbent-factory lisjong_arena.policy_catalog:create_placement_aware_speed_call \
  --incumbent-source lisjong --incumbent-revision e6346ed2bb9e992138c05c4be367bd6a05ed00bc

python -m lisjong_arena.heuristic_candidate_aabb run \
  --lock "$BUNDLE/candidate-lock.json" --seed-ledger "$LEDGER" \
  --progress --progress-json "$BUNDLE/progress.json" --run-id "$RUN_ID"

python -m lisjong_arena.heuristic_candidate_aabb verify \
  --lock "$BUNDLE/candidate-lock.json" \
  --comparison "$BUNDLE/comparison.json" --result "$BUNDLE/candidate-result.json"
```

実行は#423と同じspawn seed-block executorを使う。親と各workerが前後に依存・
wheelの実byte・native identityを照合し、game中のnative呼出し、worker PID、
全seed分の順序・raw rows digestを証跡へ残す。verifyは保存済み証跡だけでも可能。
統計やPolicy判断ロジックを今回の入口へ複製しない。

成功したbundleを検証・保存後、allocationをworkflowの`operation=commit`で
COMMITTEDにする。途中失敗では完走分だけで判定せず、同じseedでの救済評価を行わない。
使用状況と失敗をIssueへ記録し、allocationを`operation=retire`でRETIREDにする。

```bash
gh -R lisbun/lisjong-arena workflow run seed-registry.yml --ref main \
  -f operation=commit -f allocation_identity="$ALLOCATION_IDENTITY"
```

開発用80半荘は同じ8 workersで771.8秒だったため、400半荘の単純外挿は約65分。
seed別の時間差・環境再構築で変わる参考値であり、完了時刻の保証ではない。
正式結果を見てから候補・判定条件を変えない。AWS実行・課金はこのlocal計画に含まれない。
