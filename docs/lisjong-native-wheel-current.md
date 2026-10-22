# 現行のlisjong pin・Rust wheel（#520）

リーチ宣言牌の位置（lisjong #301）、確定安全牌の基盤（lisjong #302）、`PolicyInput`の
保存形式v2（lisjong #305）、policy source reader v2（lisjong #307）、派生source v2（lisjong #309）、聴牌PUSH/FOLDの対比較source契約（lisjong #288）、
ロン合法source reader（lisjong #272）、役判定APIを含む組み合わせ。
確定安全牌の候補Policy（lisjong #312）、L2aの合成Policyと評価tool（lisjong #316 / #317）、
同点数牌paijia候補（lisjong #330）を含む。`200566f..0fff891`でnative（`native/`）のsourceは変わっていない。
Pythonがdefault、Rustは明示的opt-in。過去の#423の実験条件は
[#423時点の固定記録](lisjong-native-wheel-423.md)に保存し、変更しない。

| 項目 | 値 |
| --- | --- |
| lisjong pin / native SOURCE_REVISION | `0fff8918ec349eeec129907f6a723e2d7314b95a`（lisjong #330 / PR #331 merge） |
| wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（300,955 bytes） |
| wheel SHA-256 | `130929229b16b0508a61e77d04c0a219ab65a15f7e79a74d60c43725c0cb8fba` |
| native API_VERSION / SCORING_API_VERSION | `3` / `1` |
| main push CI | [39726880815](https://github.com/lisbun/lisjong/actions/runs/39726880815) / attempt 1 / success |
| artifact | `lisjong-native-wheel-0fff8918ec349eeec129907f6a723e2d7314b95a` |
| build | manylinux_2_28_x86_64、CPython 3.14.9、rustc 1.98.1、maturin 1.15.0、release / `--locked` |

対象はLinux x86_64 / 通常版CPython 3.14（AL2023対応タグ）。Windowsへこのwheelをinstallしない。
同名・同versionの旧wheelを上書きせず、revision別directoryで`SHA256SUMS`・`BUILD-INFO.txt`と一緒に保持する。
GitHub artifactの保存期間は90日。取得後の長期保持先としてprivate S3のrevision別objectを利用できる。

```bash
gh -R lisbun/lisjong run download 39726880815 \
  --name lisjong-native-wheel-0fff8918ec349eeec129907f6a723e2d7314b95a \
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

## 導入確認と正式評価

2026-10-22 JSTにcurrent pin `0fff891`のmain push CI成功、取得artifactの`sha256sum --strict -c SHA256SUMS`と
BUILD-INFO（`sha256`、`lisjong_revision` = pin、`github_event=push`、`refs/heads/main`）を確認した。
merge SHAから作られたwheelで、PR headのwheelではない。WSLでのinstall・`environment_verify`・Rust backend確認は
Issue #520の実行手順（[実行前計画](paijia-tie-screen-520.md)）で行う。#517の校正は旧pin `999553b`・旧wheel
`9aa0ca4e…`の組で実行済みで、新pinで再現したことにはしない。

以下は2026-10-20 JSTの旧pin `999553b`・旧wheel `9aa0ca4e…`（#507）の導入確認記録。
2026-10-20 JSTにpin `999553b`のmain push CI成功、取得artifactの実SHA-256とBUILD-INFOを確認した
（`sha256sum`の値と`SHA256SUMS`・`BUILD-INFO.txt`の`sha256`が一致、`lisjong_revision`がpinと一致）。
wheelのbyte列が`ee02748f…`と違うのは、build時に埋め込む`SOURCE_REVISION`が違うためである（native sourceは同一）。
Arenaは`SOURCE_REVISION`がpinと一致することを要求するので、`999553b`のpinには`9aa0ca4e…`を使う。
lisjong#318の学習・OFFLINE-EVALは同じlisjong `999553b`に旧wheel `ee02748f…`（`SOURCE_REVISION` `174bf48`）を入れて実行した。
WSL（Ubuntu 24.04）でのRust backend確認とL2a動作確認は[L2a合成Policyの動作確認](l2a-runtime-smoke-507.md)に記録する。

以下は2026-10-18 JSTの旧pin `174bf48`・旧wheel `ee02748f…`（#503）の導入確認記録。
main push CI 39047314657成功、取得artifactの実SHA-256とBUILD-INFOを確認した。

以下は2026-10-17 JSTの旧pin `200566f`・旧wheel `e2c27737…`（#499）の導入確認記録。
main push CI 39030086196成功、取得artifactの実SHA-256とBUILD-INFOを確認した。

以下は2026-10-17 JSTの旧pin `76844f5`・旧wheel `2eb9f7e6…`（PR #496）の導入確認記録。
main push CI 39017310949成功、取得artifactの実SHA-256とBUILD-INFOを確認した。

以下は2026-10-09 JSTの旧pin `6be9b90`・旧wheel `0c0e3dc4…`（#476）の導入確認記録。
main push CI成功、取得artifactの実SHA-256とBUILD-INFOを確認した。
#476の開発seedでの通し確認は[聴牌PUSH/FOLD対比較source](tenpai-push-fold-source-476.md)に記録する。

以下は2026-10-08 JSTの旧pin `994f529`・旧wheel `170ef348…`（#457）の導入確認記録。
main push CI成功、取得artifactの実SHA-256とBUILD-INFOを確認した。
#457の専用Linux CIで実engine合成fixture sourceのreader・正解builder全件受理を確認した。

以下は2026-10-01 JSTの旧pin `e6346ed`・旧wheel `b14e53fe…`（#436）の導入確認記録。
Linux x86_64 / CPython 3.14.7の新規venvでVCS依存整合、wheelとinstalled fileのbyte一致、
SOURCE_REVISION / API_VERSION 3、向聴およびR5の実native呼出し（`r5_probe_calls=1`）を確認した。
正式実行時にも親・各workerの検証を行う。AWS実機・RiichiLabでの時間適合はこの確認に含まない。
#436のseed予約・lock・比較手順は [1向聴守備候補の正式評価](heuristic-candidate-436.md) を参照。
