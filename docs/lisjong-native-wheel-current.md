# 現行のlisjong pin・Rust wheel（#544）

lisjong #357（PR #358）でR1介入のD arm wrapper（`RiichiFirstDamaBranchPolicy`、`riichi-first-dama-branch-wrapper-v1`）を追加した組み合わせ。#346（`89dd00d`）からのRust必須はそのままで、Pythonへのfallbackや切替はない。`84e9193..d22c4c3`は`lisjong.policies.riichi_dama_branch`・test・文書の変更だけで、native source・入口（`API_VERSION` 3）・特徴（`riichi-dama-features-v2`）は変わらないが、埋め込む`SOURCE_REVISION`が違うのでwheelは別物である。
互換契約は[Rust-only移行のconsumer互換契約](rust-only-consumer-compatibility.md)を正本とする。
過去の#423の実験条件は[#423時点の固定記録](lisjong-native-wheel-423.md)に保存し、変更しない。
#542（`84e9193`、wheel SHA-256 `ed439a9d…cfc6`、CI run 40156527731）の組は#542の`gate-2/`・`rehearsal-3/`・本計測の記録として残る。
#540（`a2b2dc9`、wheel SHA-256 `edd8dd0e…2e6a`、CI run 40114544361）の組は#542のgate（`gate/`）とrehearsal-2の記録として残る。
#534（`7fc1130`、wheel SHA-256 `02d11385…c634`、CI run 40087058106）の組は[call-value-shadow-v1](call-value-shadow-534.md)の記録として残る。

| 項目 | 値 |
| --- | --- |
| lisjong pin / native SOURCE_REVISION | `d22c4c3e3c00a72b35705bda3220d5598884ffa1`（lisjong #357 / PR #358 merge） |
| wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（301,069 bytes） |
| wheel SHA-256 | `69bf2efff99d6e87cf57d78c25f5e74c0a004a317cc774fdada43b70296f440f` |
| source archive SHA-256 | `3121c2d23fdddbd883f993c93ebdc67db87fa8cc47b2e3f322936c20aad2571d`（`git archive --format=tar --prefix=lisjong/ d22c4c3…`。旧pinの記録値`6d8c8a48…`を再現する同じ方法（Windows checkoutのgit）で計算） |
| native API_VERSION / SCORING_API_VERSION | `3` / `1` |
| main push CI | [40184471486](https://github.com/lisbun/lisjong/actions/runs/40184471486) / attempt 1 / success |
| artifact | `lisjong-native-wheel-d22c4c3e3c00a72b35705bda3220d5598884ffa1` |
| build | manylinux_2_28_x86_64、CPython 3.14.9、rustc 1.98.1、maturin 1.15.0、release / `--locked` |
| 取得先 | `C:\Dev\lisjong-artifacts\native\d22c4c3e3c00a72b35705bda3220d5598884ffa1\` |
| 導入確認 | 取得artifactの`sha256sum --strict -c SHA256SUMS`とBUILD-INFOだけを確認。新規venvへの導入・`verify-wheel`・probe・Rust-only gateはR1 pilotの実行Issueで行う |

対象はLinux x86_64 / 通常版CPython 3.14（AL2023対応タグ）。Windowsへこのwheelをinstallしない。
同名・同versionの旧wheelを上書きせず、revision別directoryで`SHA256SUMS`・`BUILD-INFO.txt`と一緒に保持する。
GitHub artifactの保存期間は90日。取得後の長期保持先としてprivate S3のrevision別objectを利用できる。

```bash
gh -R lisbun/lisjong run download 40184471486 \
  --name lisjong-native-wheel-d22c4c3e3c00a72b35705bda3220d5598884ffa1 \
  --dir <new-revision-specific-directory>
# 取得先で sha256sum --strict -c SHA256SUMS
```

新規venvへこのArena revisionをinstallし、`environment_verify --project pyproject.toml`で依存を確認する。
既存環境では同versionの旧packageが残らないよう、明示的なVCS revisionで再導入して確認する。

```bash
python -m lisjong_arena.shanten_backend_verification verify-wheel <wheel>
python -m pip install --only-binary=:all: --no-index --no-deps --force-reinstall <wheel>
python -m lisjong_arena.riichilab.aws_backend --backend rust --wheel <wheel>
```

最後のprobeは、installed lisjong revision / native SOURCE_REVISION / API_VERSION / wheelとimport済みfileの一致、
向聴計算のnative呼出し、lisjongの本番factory経由のR5 native呼出しを確認する。
R5の小入力probeは配線確認であり、Champion全体の速度や強さの評価ではない。
出力の`r5_probe_calls >= 1`、`native_api_version == 3`を確認する。

### backendの指定（#531）

- `LISJONG_SHANTEN_BACKEND`は未設定か`rust`。どちらもnativeを使う。
- `python`と未知値は、Arenaのpackage import時（lisjongを読み込む前）と各実行CLIの`--backend`で、
  移行案内付きで拒否する。黙ってRustへ読み替えない。
- native拡張がない・`API_VERSION`が違う・tableが壊れている場合は、lisjongが
  `lisjong.hand_evaluation.shanten`のimport時に失敗する。lisjongを読むArena module（実行・保存物の読取・
  verifyのほとんど）はnative wheelが必要。#537以降、`lisjong_arena`のpackage import自体は環境変数の検査だけで、
  再exportは遅延読込なので、lisjongを使わない`lisjong_arena.seed_registry`はnativeなしで動く。
- Pythonへ戻す方法はない。旧Python計算の再実行は、記録済みの旧Arena / lisjong revision・wheel・環境の組で行う
  （[互換契約](rust-only-consumer-compatibility.md)の「旧環境の復元」）。

### CI

Arena CIは、pinと同じlisjong revisionのnative sourceからwheelを1回buildし（`native-wheel` job、
`LISJONG_NATIVE_SOURCE_REVISION`をpinと照合）、全test jobへbinary onlyでinstallして`SOURCE_REVISION`を確かめる。
このwheelはmain CIのwheelとbyte一致しない場合がある。wheel SHA-256の照合はmockで検査し、
実行環境では上表のmain CI wheelを使う。

AWSでの起動・停止・回収は [Champion 2bot Rust試運転](aws-riichilab-rust-434.md) を参照。
既存のAABB candidate / incumbentのfactory bindingは維持するが、#423等のfrozen runを新pinで再現したことにはしない。
過去runは当時のArena commitとwheelの組を使う。

## 導入確認と正式評価

2026-10-25 JSTにpin `d22c4c3`のmain push CI成功、取得artifactの`sha256sum --strict -c SHA256SUMS`とBUILD-INFO（`sha256`、`lisjong_revision` = pin、`github_event=push`、`refs/heads/main`、CPython 3.14.9、
rustc 1.98.1、maturin 1.15.0）を確認した（lisjong #357・Arena #544、取得先は上表）。merge SHAから作られたwheelで、PR headのwheelではない。
Arena #544のfocused testは、WSLの既存venv（native `84e9193`のwheel、`API_VERSION` 3）にlisjong `d22c4c3`のsourceを重ねて実行した（nativeは不変）。このwheel自体の新規venvへの導入確認はまだしていない。
**この新pinのRust-only採用gateはまだPASSしていない。**`84e9193`のgate PASS（#542 `gate-2/`）を流用せず、R1 pilotの実行Issueで取り直す。

以下は2026-10-25 JSTの旧pin `84e9193`・旧wheel `ed439a9d…cfc6`（#542）の記録。新pinで再現したことにはしない。

2026-10-25 JSTにpin `84e9193`のmain push CI成功、取得artifactの`sha256sum --strict -c SHA256SUMS`とBUILD-INFO（`sha256`、`lisjong_revision` = pin、`github_event=push`、`refs/heads/main`、CPython 3.14.9、
rustc 1.98.1、maturin 1.15.0）を確認した（lisjong #354・Arena #542、取得先は上表）。merge SHAから作られたwheelで、PR headのwheelではない。
WSL2 Ubuntu 24.04 / uv CPython 3.14.7の新規venvへ`tools/native_bundle.py`で導入し、`_lisjong_native.SOURCE_REVISION` = pin・`API_VERSION` 3と
`riichi_dama_branch`（`riichi-dama-features-v2`）のimportを確かめた（開発・test用。実行環境での照合ではない）。
**この新pinのRust-only採用gateはまだPASSしていない。**`a2b2dc9`のgate PASS（#542 `gate/`）を流用せず、#542で取り直す。

以下は2026-10-25 JSTの旧pin `a2b2dc9`・旧wheel `edd8dd0e…2e6a`（#540）の記録。新pinで再現したことにはしない。

2026-10-25 JSTにpin `a2b2dc9`のmain push CI成功、取得artifactの`SHA256SUMS`とBUILD-INFO（`sha256`、`lisjong_revision` = pin、`github_event=push`、`refs/heads/main`、CPython 3.14.9、
rustc 1.98.1、maturin 1.15.0）を確認した（lisjong #352・Arena #540、取得先 `C:\Dev\lisjong-artifacts\native\a2b2dc97ff1e6657dbf34b50b29a7e8936613e11\`）。merge SHAから作られたwheelで、PR headのwheelではない。
WSL2 Ubuntu 24.04 / uv CPython 3.14.7の新規venvへ`tools/native_bundle.py`で導入し、`_lisjong_native.SOURCE_REVISION` = pin・`API_VERSION` 3と
`riichi_dama_branch`のimportを確かめた（開発・test用。実行環境での照合ではない）。
このpinのRust-only採用gateは#542でPASSした（gate digest `e2119d44…02bf`、`gate/`）。その後pinを`84e9193`へ更新したため、新pinでは取り直す（[引継ぎ](riichi-dama-branch-539.md#9-実行issueへの引継ぎ未確定の実値)）。

以下は2026-10-24 JSTの旧pin `7fc1130`・旧wheel `02d11385…c634`（#534）の記録。新pinで再現したことにはしない。

2026-10-24 JSTにpin `7fc1130`のmain push CI成功、取得artifactの`sha256sum --strict -c SHA256SUMS`と
BUILD-INFO（`sha256`、`lisjong_revision` = pin、`github_event=push`、`refs/heads/main`、CPython 3.14.9、
rustc 1.98.1、maturin 1.15.0）を確認した（Arena #534、取得先 `C:\Dev\lisjong-artifacts\native\7fc1130f9a6498d0de809f8128608b753c829415\`）。merge SHAから作られたwheelで、PR headのwheelではない。
WSL2 Ubuntu 24.04 / uv CPython 3.14.7の新規venvで導入し、`_lisjong_native.SOURCE_REVISION` = pin・`API_VERSION` 3と
`speed_call_analysis`のimportを確かめた（開発・test用。実行環境での照合ではない）。
当時、このpinのRust-only採用gateはPASSしていなかった（#531の`89dd00d` PASSは流用しない）。
call-value-shadow-v1の実行Issueで実行環境（AI作業環境Linux）のgateを取り直す（[引継ぎ](call-value-shadow-534.md#6-実行issueへの引継ぎ未確定の実値)）。

以下は2026-10-24 JSTの旧pin `89dd00d`・旧wheel `65979c5c…`（#531）の記録。Rust必須化の組み合わせで、
Rust-only gateは`2c2107c`でPASSした。新pinで再現したことにはしない。

| 項目 | 値 |
| --- | --- |
| lisjong pin / native SOURCE_REVISION | `89dd00d54d3651c1a8a720b2bea3933bb2ca0bd5`（lisjong #346 / PR #347 merge） |
| wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（301,068 bytes） |
| wheel SHA-256 | `65979c5cfcbbdf01db61efbf99cd5d698db1454e553c983f0eb2cfcdcb198c00` |
| source archive SHA-256 | `46196b436726e0f2fae32904451af2b4fbc7f34f6e7d09b5769d5de3ad9b1048`（13,035,520 bytes） |
| main push CI | [40070934802](https://github.com/lisbun/lisjong/actions/runs/40070934802) / attempt 1 / success |
| 保存済みbundle | `C:\Dev\lisjong-artifacts\issue-342-native-bundle\89dd00d54d3651c1a8a720b2bea3933bb2ca0bd5\`（manifest SHA-256 `10643302…8336`） |

2026-10-24 JSTにpin `89dd00d`のmain push CI成功と、保存済みbundleのwheel SHA-256・BUILD-INFO
（`sha256`、`lisjong_revision` = pin、`github_event=push`、`refs/heads/main`、CPython 3.14.9、rustc 1.98.1、
maturin 1.15.0）を確認した。merge SHAから作られたwheelで、PR headのwheelではない。
WSL2 Ubuntu 24.04 / uv CPython 3.14.7の新規venvでの導入確認（Arena #531）の結果は#531のcommentに記録する。
Rust-only pinでのengine AABB正式eventの開始条件は、P2の代わりに
[Rust-only gate](engine-aabb-evaluation.md#84-rust-only-gaterust-gate-531)を使う。

以下は2026-10-24 JSTの旧pin `d2469f2`・旧wheel `0157f3dc…`（#524）の記録。Python backendがdefault、
Rustは明示的opt-inだった組み合わせで、#524・#526はこの組で実行済み。新pinで再現したことにはしない。

| 項目 | 値 |
| --- | --- |
| lisjong pin / native SOURCE_REVISION | `d2469f2519b947fb27bf8c1622a2854c200e0aae`（lisjong #343 / PR #345 merge） |
| wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（301,077 bytes） |
| wheel SHA-256 | `0157f3dc7fa631e834437e3f1f049cbfdab9ed1eadca1e8c73d6f8dec51d7046` |
| native API_VERSION / SCORING_API_VERSION | `3` / `1` |
| main push CI | [40055297032](https://github.com/lisbun/lisjong/actions/runs/40055297032) / attempt 1 / success |
| 取得先 | `C:\Dev\lisjong-artifacts\native\d2469f25…\` |

2026-10-24 JSTにpin `d2469f2`のmain push CI成功、取得artifactの`sha256sum --strict -c SHA256SUMS`と
BUILD-INFO（`sha256`、`lisjong_revision` = pin、`github_event=push`、`refs/heads/main`、CPython 3.14.9、
rustc 1.98.1、maturin 1.15.0）を確認した（取得先 `C:\Dev\lisjong-artifacts\native\d2469f25…\`）。
merge SHAから作られたwheelで、PR headのwheelではない。WSLでのinstall・`environment_verify`・Rust backend確認は
Issue #524の実行手順（[実行前計画](single-open-genbutsu-activation-524.md)）で行う。#520 / #522は旧pin
`0fff891`・旧wheel `13092922…`の組で実行済みで、新pinで再現したことにはしない。

以下は2026-10-22 JSTの旧pin `0fff891`・旧wheel `13092922…`（#520）の導入確認記録。
2026-10-22 JSTにpin `0fff891`のmain push CI成功、取得artifactの`sha256sum --strict -c SHA256SUMS`と
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
