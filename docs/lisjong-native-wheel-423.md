# 現行のlisjong pin・Rust wheel（#423準備）

本書は#423時点の固定記録。「現行」は当時の意味。現在の組は[現行文書](lisjong-native-wheel-current.md)を参照する。

#423で最新0004＋Belief-paijia統合候補を使うための組み合わせ。
Pythonがdefault、Rustは明示的opt-in。候補の登録は強度評価・Champion昇格・稼働botの更新を意味しない。

## 固定する組

| 項目 | 値 |
| --- | --- |
| Arena | #423準備PRを含み、下記pinを持つrevision（実行時にfull SHAを記録） |
| lisjong pin / native SOURCE_REVISION | `58ef82aeb10ac77cb66290d54e67a42426919d5b`（lisjong #231 merge） |
| wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（234,334 bytes） |
| wheel SHA-256 | `a4480991f04bc2686c6790857fdbf467cd576aa9a910e05e344232a0c4a8086c` |
| native API_VERSION | `2` |
| main push CI | [36359700012](https://github.com/lisbun/lisjong/actions/runs/36359700012) / attempt 1 / success |
| artifact | `lisjong-native-wheel-58ef82aeb10ac77cb66290d54e67a42426919d5b` / id `10945396092` |
| build | manylinux_2_28_x86_64、CPython 3.14.7、rustc 1.98.1、maturin 1.15.0、release / `--locked` |

対象はLinux x86_64 / 通常版CPython 3.14（AL2023対応タグ）。Windowsにはこのwheelを入れない。
wheel・`SHA256SUMS`・`BUILD-INFO.txt`をrevision別directoryへ一緒に保持し、CI artifactの90日保持に依存しない。
同名・同versionの旧wheelを上書きしない。保存・検証結果の正本は#423の準備PRに記録する。

```bash
gh -R lisbun/lisjong run download 36359700012 \
  --name lisjong-native-wheel-58ef82aeb10ac77cb66290d54e67a42426919d5b \
  --dir <new-revision-specific-directory>
# 取得先で sha256sum --strict -c SHA256SUMS
```

## 導入・照合

新規venvを基本とし、Arenaをinstallしたあと、`python -m lisjong_arena.environment_verify --project pyproject.toml`でVCS revisionを確認する。
既存venvではlisjong自体も同versionのため通常installで旧revisionが残る場合がある。
その場合は環境を作り直すか、対象full SHAのVCS requirementを`pip install --force-reinstall --no-deps`で再導入し、再検証する。
その後、次を行う。

```bash
python -m lisjong_arena.shanten_backend_verification verify-wheel <wheel>
python -m pip install --only-binary=:all: --no-index --no-deps <wheel>
LISJONG_SHANTEN_BACKEND=rust python -m lisjong_arena.shanten_backend_verification \
  probe --backend rust --wheel <wheel>
```

既存venvで同versionのwheelを入れ替える場合は`--force-reinstall`を付ける。
`probe`はinstalled lisjongのVCS revision、SOURCE_REVISION、API_VERSION、native呼出し、
wheel内fileと実際にimportしたfileのbyte一致を検査する。不一致は拒否し、Pythonにfallbackしない。
Pythonへ戻す場合は`LISJONG_SHANTEN_BACKEND=python`とする。

## #423 participant

| 役割 | Arena identity | factory binding | lisjong class |
| --- | --- | --- | --- |
| candidate A | `placement-aware-speed-call-kobalab-0004-belief-paijia` | `lisjong_arena.policy_catalog:create_placement_aware_speed_call_kobalab_0004_belief_paijia` | `PlacementAwareSpeedCallKobalab0004BeliefPaijiaDiscardPolicy` |
| incumbent B | `placement-aware-speed-call` | `lisjong_arena.policy_catalog:create_placement_aware_speed_call` | `PlacementAwareSpeedCallPolicy` |

両者のimplementation_sourceは`lisjong`、implementation_revisionは同じfull pinを使う。
factoryの置き場所がArenaでも、Policy実装のrevisionにArena SHAを使わない。
Aは引数なしで生成し、lisjong-ownedの既定`KOBALAB_0004_BELIEF_PAIJIA_ESTIMATOR`を使う。
他家slotとfixed-point丸めもlisjongの実装を使い、Arenaで再定義しない。
旧合成版・単独Belief版・参照版は追加armとして登録しない。

既存`heuristic_candidate_aabb`はexplicit participant bindingを受け付ける。
ただし#375のseed owner/population・固定bootstrapと、#423の実行lockは同一視しない。
この準備変更ではseed予約・400半荘・AWS実行を行わない。後続で#423のallocation binding、
Rustの親/実worker検証を含む実行入口、校正・停止期限を整合し、cleanなmerged revisionでlockする。
既存AABB経路は`LocalGameRunner.run()`からcomparison artifactを保存し、
#415のdurable typed-analysis serializerを使わない。現行経路を使う限り#415は直接のblockerではない。

backend確認用の`shanten_backend_verification games`はcatalog aliasを受け付け、spawn workerごとに
revision/API/backendとnative呼出し・一括打牌評価counterを記録する。
これは自己対戦の導入smokeであり、#423のAABB強度評価や時間校正ではない。
0004一括経路は参照版だけでなく最新統合候補でも使う。実workerごとのcounter増分を確認する。

## 過去との境界・rollback

- [#409の固定記録](lisjong-native-wheel-409.md)は当時の組・検証結果を保持する。
- #400/#406等のhistorical plan・bootstrap・wheel hash・seed・artifactは変更しない。
- #409へのrollbackはArena `eb7baae889de63b0a94e21bd8ca7d518083ed5d1`（今回のbase）、
  lisjong `8bdfd3f942ced49830bcee1894aefe3d2e0acc3a`、wheel SHA-256
  `22ce171059416ba8e25c8801ec2ef425afeacfda792ae67d837865610269e7f8`の組で行う。
  そのArena commitと新規venv・対応wheelで`verify-wheel`/`probe`を実行する。最新候補はその組には存在しない。
- native sourceが同じでもSOURCE_REVISIONが異なる旧wheelを流用しない。
- `8bdfd3f..58ef82a`で`lisjong.learning`は不変。現行producerのconsumer pinは追随するが、
  過去のfrozen source/experimentのrevisionを書き換えない。
- 現Championの変更は候補制限helperの抽出。coreの#227/#231同値検証を参照し、
  Arena側では候補解決・同じinstalled revision・smokeの実行境界を確認する。

## 今回の導入検証（2026-09-28）

Linux x86_64 / 通常版CPython 3.14.6 / RiichiEnv 0.4.10で検証した。
wheelは上記main CI artifactをbinary限定で導入し、hash、SOURCE_REVISION、API_VERSION 2、
導入file一致、native probe呼出しを確認。`environment_verify`で両内部依存pinと`pip check`が一致した。
AL2023実機・Windows native buildは今回未実施。

- 候補登録、historical lazy import、AABB participant解決、backend拒否、旧#400 report、
  pure-offense backend、focal source、実comparisonのfocused 8 module：176 tests（wheel導入時1 skip）。
- 導入smoke：既知のDEVELOPMENT seed 0/1、`4p-red-single`、候補2局×Python/Rust、
  Champion seed 0の1局×Python/Rust。両者とも各backend間の全decision semantic digest・得点・順位・stepが一致。
- 候補は要求2/観測2 worker（各1局）。Rustの一括打牌評価は各worker 35/50回、合計85回。
  全workerのSOURCE_REVISION/API/backendを確認。Championの一括評価は0（想定どおり）。
- wheel未導入時のRust拒否は、別途wheelを外してbackend testを実行して確認。
- ruff 0.16.0のformat/checkと`git diff --check`が成功。full regressionの正本はArena PR CI。

これは導入・互換性の確認であり、#423のfresh AABB評価や本番worker数の校正、性能向上の証明ではない。
smoke seedを#423本評価のfresh allocationとして再利用しない。
