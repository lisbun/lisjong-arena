# 現行のlisjong pin・Rust wheel（#434）

RiichiLabでChampionのR5探索までRust化する組み合わせ。
Pythonがdefault、Rustは明示的opt-in。過去の#423の実験条件は
[#423時点の固定記録](lisjong-native-wheel-423.md)に保存し、変更しない。

| 項目 | 値 |
| --- | --- |
| lisjong pin / native SOURCE_REVISION | `51e832e50a0ee71eac65e4a017f46590e7438c04`（lisjong #233 merge） |
| wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（252,815 bytes） |
| wheel SHA-256 | `802d19e4c2f8cfb52f133e8b0666a9225a745e1a85da2600a6343433ab919c79` |
| native API_VERSION | `3` |
| main push CI | [36725918036](https://github.com/lisbun/lisjong/actions/runs/36725918036) / attempt 1 / success |
| artifact | `lisjong-native-wheel-51e832e50a0ee71eac65e4a017f46590e7438c04` / id `11102344881` |
| build | manylinux_2_28_x86_64、CPython 3.14.7、rustc 1.98.1、maturin 1.15.0、release / `--locked` |

対象はLinux x86_64 / 通常版CPython 3.14（AL2023対応タグ）。Windowsへこのwheelをinstallしない。
同名・同versionの旧wheelを上書きせず、revision別directoryで`SHA256SUMS`・`BUILD-INFO.txt`と一緒に保持する。
GitHub artifactの保存期間は90日。取得後の長期保持先としてprivate S3のrevision別objectを利用できる。

```bash
gh -R lisbun/lisjong run download 36725918036 \
  --name lisjong-native-wheel-51e832e50a0ee71eac65e4a017f46590e7438c04 \
  --dir <new-revision-specific-directory>
# 取得先で sha256sum --strict -c SHA256SUMS
```

新規venvへこのArena revisionをinstallし、`environment_verify --project pyproject.toml`で依存を確認する。
既存環境では同versionの旧packageが残らないよう、明示的なVCS revisionで再導入して確認する。

```bash
python -m lisjong_arena.shanten_backend_verification verify-wheel <wheel>
python -m pip install --only-binary=:all: --no-index --no-deps --force-reinstall <wheel>
LISJONG_SHANTEN_BACKEND=rust python -m lisjong_arena.riichilab.aws_backend \
  --backend rust --wheel <wheel>
```

最後のprobeは、installed lisjong revision / native SOURCE_REVISION / API_VERSION / wheelとimport済みfileの一致、
向聴計算のnative呼出し、lisjongの本番factory経由のR5 native呼出しを確認する。
R5の小入力probeは配線確認であり、Champion全体の速度や強さの評価ではない。
出力の`r5_probe_calls >= 1`、`native_api_version == 3`を確認する。
Pythonへ戻す場合は`LISJONG_SHANTEN_BACKEND=python`を明示する。

AWSでの起動・停止・回収は [Champion 2bot Rust試運転](aws-riichilab-rust-434.md) を参照。
既存のAABB candidate / incumbentのfactory bindingは維持するが、#423等のfrozen runを新pinで再現したことにはしない。
過去runは当時のArena commitとwheelの組を使う。

## 導入確認（2026-09-30）

Linux x86_64 / 通常版CPython 3.14.7の作業環境で、上記CI wheelのSHA-256・SOURCE_REVISION・API_VERSION 3・
installed file一致と、向聴 / R5の実native呼出しを確認した。AWS実機・RiichiLabでの時間適合は未確認。
