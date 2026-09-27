# 現行のlisjong pin・Rust wheelの組み合わせ（#409）

Issue：#409。lisjong側の変更はlisbun/lisjong#224 / lisbun/lisjong#225（0004構造評価の一括native化）。
wheelの配布契約はlisbun/lisjong#216 / lisbun/lisjong#217、Arena側の導入検査は[#400](rust-shanten-backend-400.md)を正本とする。

本書は、現行のArenaが使う **Arena revision・lisjong revision・Rust wheel** の組と、その実行入口、rollback手順を扱う。
defaultのbackendはPythonのままで、Rustは明示的なopt-inである。
推定器・Policyの選択は変えない。0004参照Policyのidentity（`kobalab-0004-tile-efficiency-reference-v1`）も変えない。
強さや対局速度の改善を新たに主張するものではない。
lisjong#218 / lisjong#219の「536→121 ms」は、固定630 decision再生でのPolicy計算の測定値であり、対局全体の速度向上率ではない。

## 1. 組み合わせ

rollbackは、次の表の **1列を単位** として行う。wheelだけを入れ替えない。

| 項目 | 現行（#409） | 前の組（#400 / #406） |
| --- | --- | --- |
| Arena revision | 本Issue（#409）のPRのmerge revision以降 | `438bf5a`（#409直前のmain）。#406の実行は`106b50e` |
| lisjong（`pyproject.toml`のpin） | `8bdfd3f942ced49830bcee1894aefe3d2e0acc3a`（lisjong#225 merge） | `2553c1b9f22545bb2fcb914adce1879d15cdc58d`（lisjong#217 merge） |
| wheel file | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（234,339 bytes） | 同名（229,050 bytes） |
| wheel SHA-256 | `22ce171059416ba8e25c8801ec2ef425afeacfda792ae67d837865610269e7f8` | `ff8aaa400de5b58e4bb61d040dce596b7875047cda2bf2daa15b1a0e3e90166c` |
| `_lisjong_native.SOURCE_REVISION` | `8bdfd3f…` | `2553c1b…` |
| `_lisjong_native.API_VERSION` | `2` | 属性なし（lisjongは1として扱う） |
| CI run（`push` / `refs/heads/main`） | [36296356964](https://github.com/lisbun/lisjong/actions/runs/36296356964)（attempt 1、success） | [36257082986](https://github.com/lisbun/lisjong/actions/runs/36257082986) |
| CI artifact | `lisjong-native-wheel-8bdfd3f…`（id 10923957785） | `lisjong-native-wheel-2553c1b…` |
| 保持（正本、ユーザー管理） | `C:\Dev\lisjong-artifacts\issue-409-rust-wheel-8bdfd3f\wheel\` | `C:\Dev\lisjong-artifacts\issue-216-rust-wheel\wheel\` |
| Arena側の値 | `shanten_backend_verification.backend.EXPECTED_*` | `shanten_backend_verification.plan`（`LISJONG_REVISION` / `WHEEL_*`） |

現行wheelのbuild情報（CI artifactの`BUILD-INFO.txt`のまま）：
`quay.io/pypa/manylinux_2_28_x86_64`、CPython 3.14.7、rustc 1.98.1、maturin 1.15.0、release profile、`--locked`。
対象はAmazon Linux 2023 / x86_64 / 通常版CPython 3.14である。このLinux wheelをWindowsのvenvへ入れない。
Windowsのlocal native buildとvenv更新は、必要になったときの別作業とする。

新旧のwheelは **file名もpackage version（0.1.0）も同じ** である。
そのため、保持directoryをrevisionごとに分け、既存のwheelを上書きしない。
既存venvへ`pip install`しても、同じversionとして導入が省略され、旧wheelのfileが残ることがある。
そこでArenaは、読み込まれた拡張moduleのfileをwheelの中身とbyte単位で照合する（`probe --wheel`、§3）。

取得・照合（2026-09-27）：

```bash
gh -R lisbun/lisjong run download 36296356964 \
  --name lisjong-native-wheel-8bdfd3f942ced49830bcee1894aefe3d2e0acc3a --dir wheel
(cd wheel && sha256sum --strict -c SHA256SUMS)   # OK
```

## 2. 過去の実行との関係

- #389・#393・#400・#406の測定記録、固定bootstrap、wheel hash、seed allocation、結果artifactは変更しない。
- `scripts/aws/bootstrap-rust-shanten-400.sh`と`scripts/aws/bootstrap-pure-offense-kobalab-406.sh`は、旧revision（`2553c1b`）と旧wheel hashを保持する。
  どちらも「checkoutしたArenaの`pyproject.toml`が`2553c1b`をpinしていること」を検査する。
  そのため、#409以降のArena revisionを渡すと起動時に拒否する。旧pinの拒否は解除していない。
- 過去の実行は、記録された旧Arena commit（#406は`106b50e`）と、それがpinするlisjong・旧wheelの組で再現する。
- #400のevidence判定（`report` / `evidence.py`）は、`plan`が固定する旧revision・旧wheel identityで行う。
  そのため、現行のArenaでも#400の保存済みevidenceを判定し直せる。
- 共有の検証code（`require_shanten_backend`、`verify-wheel`、`probe`、`games`）の既定値は現行の組である。
  旧組のArena commitでは、そのcommitの値がそのまま使われる。

`2553c1b..8bdfd3f`のlisjong変更は、0004参照Policy・belief（`conditional_uniform_hand_belief`、`tile_type_set`）・shanten backendだけである。
`lisjong.learning`と`outcome_source.py`は変わらないため、`focal_outcome_source.PINNED_LISJONG_REVISION`もpinへ追随した。
代表Policyへの影響がないことは§4で確認した。

## 3. 現行の実行入口

Rustは、`LISJONG_SHANTEN_BACKEND=rust`を明示したときだけ使う。
未導入・不一致のときは、対局開始前に停止する。Pythonへ黙ってfallbackしない。

```bash
# 新規venvを基本とする（Arenaのcheckoutがlisjong 8bdfd3fをpinする）
python3.14 -m venv .venv && .venv/bin/python -m pip install -e .

# wheel：install前にfile名・SHA-256を照合し、binary限定で入れる
.venv/bin/python -m lisjong_arena.shanten_backend_verification verify-wheel <wheel>
.venv/bin/python -m pip install --only-binary=:all: --no-index --no-deps <wheel>
#   既存venv（旧wheel導入済み）では同versionのため省略され得る → --force-reinstall を付ける

# process検査：pin・SOURCE_REVISION・API_VERSION・native呼び出し・導入fileとwheelの一致
LISJONG_SHANTEN_BACKEND=rust .venv/bin/python -m lisjong_arena.shanten_backend_verification \
    probe --backend rust --wheel <wheel>

# 例：pure-offense benchmarkの1 arm（全game processで同じ検査、記録はarmの外）
LISJONG_SHANTEN_BACKEND=rust .venv/bin/python -m lisjong_arena.pure_offense_benchmark run \
    ... --shanten-backend rust --shanten-backend-record <backend.json>
```

`probe`（rust）は、`SOURCE_REVISION`が現行pinであること、`API_VERSION`が2であること、`calculate_shanten()`でnative counterが進むことを検査する。
`--wheel`を付けると、wheelの`_lisjong_native/`以下の全fileについて、読み込まれたfileとbyte単位で一致することも検査する。
どれかが外れるとexit 2で停止する。

per-gameの記録（`pure_offense_benchmark run --shanten-backend-record`、`shanten_backend_verification games`）には、次を残す。

- workerのpid、backend、lisjong revision
- native `SOURCE_REVISION` / `API_VERSION`
- gameの前後差で数えたnative call数と`discard_evaluation_call_count()`の差
  - counterはprocess単位なので、必ずgame前後の差で数える。
  - 一括構造評価（lisjong#224）を使うのは0004参照Policyだけである。
    そのため、他のPolicyでは差が0でも失敗にしない。
  - 0004で一括Rust経路を使ったことは、この差（> 0）で確認する。
    `standard_shanten_call_count()`の増加だけでは、一括経路の使用の証明にならない。

**Pythonへ戻す**：`LISJONG_SHANTEN_BACKEND=python`にする。wheelのuninstallは不要である。
python選択時は拡張moduleをimportしないため、旧wheelが残っていても失敗しない（§4で確認）。
RiichiLabで稼働中のbotやAWS instanceは、本Issueでは更新しない。

## 4. 検証（2026-09-27、ローカルAL2023 container）

compilerのない`amazonlinux:2023` container（x86_64、8 CPU）で行った。
現行組（Arena `e06b248`）と旧組（Arena `438bf5a`）をそれぞれ新規venvに入れて比べた。
使ったscriptは`C:\Dev\lisjong-artifacts\issue-409-rust-wheel-8bdfd3f\verification\verify-409.sh`と`verify-409-rollback.sh`で、evidenceは同じ場所の`run-20260927\`・`rollback-20260927\`にある（`sha256sums.txt`付き）。
証跡manifestについて、初回のcontainer実行は`find`がないため生成段階で失敗し、host側で生成した（検証scriptは修正済み）。
2026-09-27 15:44:10 +09:00、ユーザー管理環境で既存の`sha256sums.txt`に対する読み戻し照合が成功した（ユーザー提示ログに基づく記録）。
PowerShellの`Get-FileHash -Algorithm SHA256`で各実ファイルのhashを再計算し、本検証`run-20260927`の67ファイル、rollback `rollback-20260927`の8ファイル、計75ファイルがmanifestと一致した。
最終出力は`ALL PASS: 2026-09-27T15:44:10.4344701+09:00`で、結果は同じ`verification` directoryの`checksum-readback.txt`に保存された。
manifestは再生成していない。wheel自体の`SHA256SUMS`照合（§1）とは別の、保存済み検証証跡の照合である。
記録：[Issue #409のreadback結果](https://github.com/lisbun/lisjong-arena/issues/409#issuecomment-5853513694)。

AWSは使っていない。seedの新規予約もしていない。
使った固定入力は、#400のsmoke seed（0..31、seed 0のdigest）と、#406が再利用したCOMMITTEDの#389 DEVELOPMENT allocation（`df846086…`）である。

**導入（compilerなし）**：`gcc` / `cc` / `c++` / `clang` / `rustc` / `cargo`はない。
保存したwheelを`--only-binary=:all: --no-index --no-deps`で導入した。
`probe --backend rust --wheel`の結果は次のとおりで、導入fileとwheelの2 fileは一致した。

- installed lisjong：`8bdfd3f`
- `SOURCE_REVISION`：`8bdfd3f`
- `API_VERSION`：2
- probe native call：1

**fail closed / Python経路**（現行組のvenv）：

| 状況 | 結果 |
| --- | --- |
| wheelなし、python | exit 0、`native` = null |
| wheelなし、rust | exit 1（lisjong `ShantenBackendError`、fallbackなし） |
| 旧wheel・1 byte追加したwheelの`verify-wheel` | exit 2（SHA-256不一致） |
| 旧API wheel（#400）を導入、rust | exit 1（`API_VERSION 2, got 1`） |
| 旧API wheel を導入、python | exit 0（旧wheelがあってもPython経路は失敗しない） |
| 旧wheelの上へ新wheelを`--force-reinstall`なしで導入、rust | exit 1。導入が省略され、旧fileが残っていた |
| `--force-reinstall`後、rust `--wheel` | exit 0 |
| `LISJONG_SHANTEN_BACKEND`未設定 | exit 2 |
| 旧Arena（`2553c1b` pin）に新wheel、rust | exit 2（`SOURCE_REVISION`不一致） |
| 旧Arena + 旧wheel（rollback後の組）、rust | exit 0（`SOURCE_REVISION` = `2553c1b`） |

`SOURCE_REVISION`不一致・native未呼び出し・導入file不一致の拒否は、unit testでも固定した。

**0004参照Policyの同値性**（最終行動は、全席のdecision入力と選択Actionのsemantic digestで比べた。あわせて得点・順位・step数も比べた）：

| 比較 | 入力 | 結果 |
| --- | --- | --- |
| 旧組Python vs 新組Python | `games`、4p-red-half、seed 0..31（#400のsmoke seed）、8 worker | 32/32一致 |
| 新組Python vs 新組Rust | 同上 | 32/32一致 |
| 旧組Rust（#406の0004 arm） vs 新組Rust | pure-offense、#389 allocation 1,000 seed × 4 rotation | 4,000/4,000 game、record・game result一致 |
| 新組Python vs 新組Rust | 同上 | 4,000/4,000一致 |

中間値（一括構造評価の向聴数・有効牌）のcore側の差分試験は、lisjong#225とその対象commitのCI（`native-backend` job）を正本とする。
Arenaでは、導入した組を通した最終行動の一致と、次の一括経路の実行回数で確認した。

**代表Policy**（0004以外）：Champion（`placement-aware-speed-call`）とtwo-stepを選んだ。
各1局（seed 0）を旧組Python・新組Python・新組Rustで実行した。
どれも#400が`2553c1b`で記録したseed 0のdigestと一致した（`--expected-semantic`）。
この2つは一括構造評価を使わないため、`discard_evaluation_call_count()`の差は0だった（想定どおり）。

**worker smoke**：どちらも要求8 worker、観測8 workerだった。
全workerについて、backend・`SOURCE_REVISION`・`API_VERSION`（2）を確認した。

| 経路 | games | worker別のgame数 | `discard_evaluation_call_count()`の差（worker別） |
| --- | --- | --- | --- |
| `games`（0004、rust） | 32 | 各4 | 1,455〜1,870（計13,570） |
| pure-offense `run`（0004、rust） | 4,000 | 461〜512 | 7,363〜8,060（計62,778） |

どのworkerでも一括Rust経路が実行された。Python armでは`native_discard_evaluations` = null だった。
所要時間は記録に残しているが、速度の主張には使わない。

## 5. rollback

rollbackは、Arena revision・lisjong revision・wheel/manifestの **組で** 戻す。

1. Arenaを旧組のrevision（`438bf5a`、またはその組でmergeされた後続の旧pin commit）でcheckoutする。`pyproject.toml`はlisjong `2553c1b`をpinしている。
2. **新規venv** を作って`pip install -e .`する。lisjong `2553c1b`が入る。
3. 旧wheel（`issue-216-rust-wheel\wheel\`、SHA-256 `ff8aaa40…`）を、そのcommitの`verify-wheel`で照合し、`--only-binary=:all: --no-index --no-deps`で入れる。
   既存venvを使い回す場合は`--force-reinstall`を付ける。
4. そのcommitの`probe --backend rust`で、`SOURCE_REVISION` = `2553c1b`を確認する。

wheelだけを入れ替えると、fail closedで停止する。

- 現行Arena（8bdfd3f）に旧wheelを入れた場合：lisjongが`API_VERSION`不一致で停止する。
- 旧Arena（2553c1b）に新wheelを入れた場合：Arenaが`SOURCE_REVISION`不一致で停止する。

戻した組で実行した結果は、その組のArena commitとwheel hashを付けて記録する。
