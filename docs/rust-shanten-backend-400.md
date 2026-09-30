# Rust向聴数backend：AWS workerへのopt-in導入と検証（#400）

親Issue：lisbun/lisjong#216（判断基準・対象外の正本）。lisjong側のwheel生成・compilerなしAL2023 container検証は
lisbun/lisjong#217（merge `2553c1b`）で完了している。配布手順の正本は
[lisjong `docs/rust-backend-distribution.md`](https://github.com/lisbun/lisjong/blob/main/docs/rust-backend-distribution.md)。

本書は、Arena側の導入・worker設定・実行記録と、AWS実行計画（§5、**実行前の承認用の案**）を扱う。

> **#409以降**：現行のlisjong pinとwheelは[現行の組み合わせ](lisjong-native-wheel-current.md)（#436：lisjong `e6346ed`、R5対応`API_VERSION` 3）である。
> 本書の値（`2553c1b`、wheel `ff8aaa40…`）は#400 / #406の実行時の組で、`plan.py`とbootstrapに固定されている。
> 過去の実行は記録された旧Arena commitで再現する。
default backendはPythonのままで、向聴数定義・Rust化範囲は変更しない。強さ評価ではない。

## 1. 配布artifact

| 項目 | 値 |
| --- | --- |
| wheel | `lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl`（229,050 bytes） |
| SHA-256 | `ff8aaa400de5b58e4bb61d040dce596b7875047cda2bf2daa15b1a0e3e90166c` |
| build元（`_lisjong_native.SOURCE_REVISION`） | lisjong `2553c1b9f22545bb2fcb914adce1879d15cdc58d` |
| CI run | https://github.com/lisbun/lisjong/actions/runs/36257082986（`push` / `refs/heads/main`、全job success） |
| 保持（正本） | `C:\Dev\lisjong-artifacts\issue-216-rust-wheel\wheel\`（`SHA256SUMS`・`BUILD-INFO.txt`付き、取得時に照合済み） |
| 対象 | Amazon Linux 2023 / x86_64 / distribution CPython 3.14（通常版） |

PR runの一時merge revisionからbuildしたwheelは使わない。

## 2. lisjong pinの更新（`2a9debe` → `2553c1b`）

wheelは、build元と同じlisjong revisionの組み合わせでだけ検証済みとして扱う。そのため、Arenaのproject pinをwheelのbuild元へ合わせた。

`2a9debe..2553c1b`のlisjong変更（#205 / #208 文書、#209、#211、#213、#217）と、その影響：

- Policy：`PlacementAwareSpeedCallPolicy` / `TwoStepUkeirePolicy`のmoduleは変わらない。
  shanten経路の変更は、backend選択（defaultはPython）と、同じtableをnativeへ渡すための読み出しだけである。
- `lisjong.learning.outcome_source`：`read_outcome_source(path, *, workers=1)`が追加された（#209）。
  default `workers=1`の結果・error・wire semanticsは変わらないため、producer contract `PINNED_LISJONG_REVISION`も追随した。
- lockされたworkload（#375 / #389 / #393のbootstrap）は、自身のfrozen lisjong revisionをpinするArena checkoutでしか実行しない。
  pin更新後のArena revisionでは起動時に拒否するため、暗黙に移行しない。
  これらのcontract testは、current pinではなくlockされた値を検査するよう変更した（#332と同じ方式）。

## 3. opt-in導入（`lisjong_arena.shanten_backend_verification`）

選択は、lisjongの`LISJONG_SHANTEN_BACKEND`（`python` / `rust`）で行う。Arenaは次の検査を加える。
汎用runtimeの新設や、全runnerへの一括展開はしない。

| 段階 | 検査 | 失敗時 |
| --- | --- | --- |
| install前 | `verify-wheel`：file名とSHA-256がfrozen値と一致（runnerの`manifest.sha256`照合に加えて） | exit 2、installしない |
| install | `pip install --only-binary=:all: --no-index --no-deps <wheel>` | 非対応wheelはinstallが失敗し、source buildへ回らない |
| process起動 | `LISJONG_SHANTEN_BACKEND`が`--backend`と明示的に一致（未設定は拒否） | exit 2 |
| process起動 | installed lisjong commit（`direct_url.json`）がpin `2553c1b`と一致 | exit 2 |
| process起動（rust） | `_lisjong_native.SOURCE_REVISION`がpinと一致。公開`calculate_shanten()`の1 callでnative call counterが進む | exit 2 |
| process起動（python） | `_lisjong_native`がimportされていない | exit 2 |
| rustでwheelがない / 読めない | lisjongがimport時に`ShantenBackendError`（Arena package importの時点） | exit ≠ 0、Pythonへfallbackしない |
| 各worker（spawn） | initializerで同じ検査を行い、結果（pid、backend、revision、native identity、初期化後peak RSS）を各game recordへ残す | game開始前にrun全体が失敗 |
| 各game（rust） | game中のnative call数 > 0。pythonではgame後も`_lisjong_native`が未import | run全体が失敗 |

実行記録：`games.jsonl`の各行と`summary.json`に、backend、lisjong revision、native module path・`SOURCE_REVISION`、worker pidを記録する。
wheel identity（file名・SHA-256・bytes）は`wheel-identity.json`と`install.json`に記録する。大規模な監視基盤は作らない。

```text
python -m lisjong_arena.shanten_backend_verification verify-wheel <wheel>
LISJONG_SHANTEN_BACKEND=rust python -m lisjong_arena.shanten_backend_verification probe --backend rust
LISJONG_SHANTEN_BACKEND=rust python -m lisjong_arena.shanten_backend_verification games \
    --policy placement-aware-speed-call --seeds 0 1 --backend rust --workers 16 --out <new dir>
python -m lisjong_arena.shanten_backend_verification compare <python dir> <rust dir> [--expected-semantic 0:<sha256>]
python -m lisjong_arena.shanten_backend_verification startup --repeat 10 --out startup.json
python -m lisjong_arena.shanten_backend_verification report <run output dir> [--hourly-usd <USD/h>]
```

**Pythonへ戻す**：`LISJONG_SHANTEN_BACKEND=python`にする（または未設定にし、この検証CLIを使わない）。wheelのuninstallは不要である。
Python-onlyの実行・install・CIは、wheelもRust toolchainも要求しない（`pip install -e .`のままでよい）。

decision列のsemantic digest（`lisjong_arena.decision_capture`）は、`scripts/capture_policy_decisions.py`（#398）と共通の定義である。

## 4. 事前確認（ローカル、AWS実機の代替ではない）

compilerのない`amazonlinux:2023` container（CPython 3.14.7、glibc 2.34）で、bootstrapと同じ手順をworkload縮小版で実行した
（Championの代わりにtwo-step、multi-workerは4 worker × 4 seed）。

- `verify-wheel`：正しいwheelは受理し、1 byte追加したwheelはexit 2。wheelなしでrustを指定するとexit 1（lisjongの`ShantenBackendError`）。
  環境変数と`--backend`の不一致、未設定、revision不一致、native未呼び出しの拒否はunit testで固定した。
- binary限定installのあと、rust probeでは`SOURCE_REVISION` = `2553c1b`、native call 1。lisjong native同値性test：OK。
- two-step seed 0：Python / Rustで席ごとのdecision・action列、得点、順位が一致した。
  semantic digestは、lisjong#213の元artifactの値`7057aee2…`とも一致した（Arena pin更新後もinputが再現する）。
- 4 workerすべてがbackendを検証し、Python / Rustの4 gameが一致した。`report`はすべての項目を評価できた。

container上の確認は、AWS実機での確認とは扱わない（#216 §2）。

## 5. AWS実行計画（案：承認前。承認までAWSリソースを起動しない）

### 5.1 構成

| 項目 | 案 | 理由 |
| --- | --- | --- |
| instance | `c7i.4xlarge` On-Demand、`ap-northeast-1`、1台（16 vCPU / 32 GiB、Intel Sapphire Rapids） | #389 / #393のChampion実運用runと同じ構成。同じinstanceで単一・複数workerを順番に測る |
| 単一worker | 1 process（他の測定と同時に実行しない） | 主性能指標（#216 §6） |
| 複数worker | 16 worker（spawn）、1種類だけ | #389 / #393の実運用値。Championのpeak RSSは#213で約0.67 GB/worker、16 workerで約11 GB（32 GiBに収まる） |
| launcher | `-Workers 16 -MinMemoryMiBPerWorker 1024 -RootVolumeGiB 30` | bootstrapは`LISJONG_WORKERS=16`以外を拒否する |

### 5.2 固定するもの

| 項目 | 値 |
| --- | --- |
| Arena | 本PRのmerge revision（`--arena-revision`、mainへmerge済みであることをbootstrapが検査） |
| lisjong | `2553c1b9f22545bb2fcb914adce1879d15cdc58d`（pin、wheel build元、replay tool・differential testのcheckout） |
| wheel | §1のSHA-256 |
| RiichiEnv / lisjong-engine | `0.4.10` / Arena pinのまま（`8735e89`） |
| Python | AL2023 distributionの`python3.14`（実際のversionを`environment/system.txt`に記録。containerでは3.14.7） |
| 主対象Policy | Champion：catalog `placement-aware-speed-call`（対局）、`lisjong.policies:PlacementAwareSpeedCallPolicy`（固定decision再生）。途中で差し替えない |
| 補助対象Policy | two-step：catalog `two-step`、`lisjong.policies:TwoStepUkeirePolicy` |
| ルール | `4p-red-half`、4席同一Policy（席ごとに別instance） |
| 固定decision入力（bytes identity） | #213元artifact：Champion `275ca281…`（708 decisions）、two-step `31d0448e…`（684 decisions）。run inputとして渡し、bootstrapがSHA-256を照合する |
| 再生成入力（semantic identity） | seed 0の対局をPython / Rustで再実行し、semantic digestを#213の値（Champion `1d80405e…`、two-step `7057aee2…`）と照合する |

### 5.3 seed

seed 0..31を使う。これらは`arena-legacy-declared-v1`のquarantine範囲（0..646）にあり、新しい評価には割り当てられない整数である。
#213 / #398と同じく、強さ評価ではない同値性・性能の測定にだけ使う。
新規allocationは作らず、ledgerも変更しない。結果を強さの証拠としては扱わない。

同じseedを両backendで使う。行動が一致すれば、両backendは同じ対局を処理することになり、時間は同じ仕事量どうしで比べられる（paired）。

> **確認事項**：legacy quarantineの整数を非科学的な測定に再利用することの可否。
> 不可の場合は、Seed Registry workflowで小さなDEVELOPMENT allocation（32 seed）をreserveし、bootstrapの`MULTI_SEED_LAST`と
> seed指定をそのallocationへ置き換える。その場合、#213 digestとのseed 0照合は、別途seed 0の単一対局で行う。

### 5.4 実行順序と実行量（1台で順番に実行し、同時には走らせない）

| # | phase | 内容 | 見積もり |
| --- | --- | --- | ---: |
| 0 | install | dnf（git / python3.14のみ）、compilerがないことの確認、Arena / lisjong checkout、venv、environment verify | 5分 |
| 1 | fail-closed | wheelなしでpython probe成功・rust probe失敗、改変wheelの拒否 | 1分未満 |
| 2 | wheel | `verify-wheel` → binary限定install（所要秒を記録）→ rust / python probe | 1分未満 |
| 3 | differential | lisjong native同値性test、Rust選択でのlisjong full suite | 3分 |
| 4 | games-single | 1 worker、seed 0：Champion P / R、two-step P / R → 比較（#213 digest照合込み） | 13分 |
| 5 | startup | fresh process、import + 初回計算、P / R交互 × 10 | 1分 |
| 6 | replay | 固定decision再生、1 process、`--repeat 1`、順序P R R P P R（各backend 3回）：Champion、次にtwo-step | 38分 |
| 7 | games-multi | 16 worker、Champion、seed 0..31（32半荘）：Python → Rust → 比較 | 35–55分 |
| 8 | report | 事前登録した基準で`report.json`を作成する | 1分未満 |

見積もりは#213（Windows、単一core）の値を基にしている：Champion半荘 約545秒（Python）/ 約159秒（Rust）、固定decision再生1 pass 約550秒 / 約160秒。
multi-workerは、16 worker（8物理core・HT）のため1半荘あたり1.3〜1.7倍に遅くなると仮定した。

- **合計の見積もり**：1.5〜2.5時間。**fail-safe**：4時間（launch-clock、runnerのpoweroffとは独立）。
- **概算compute費用**：`c7i.4xlarge`は約$0.90/h（概算。Preflightのpricing結果を正とする）。2.5時間で約$2.3、4時間で約$3.6。
  public IPv4、EBS 30 GiB、S3は数セント以下。
- **`CostBudgetUsd = 5`は請求の上限ではない**。Preflightの受け入れ条件で、
  「fail-safe時間 × Preflightの時間単価 + S3上限」が$5を超える計画をLaunch前に拒否するだけである。
  実際の停止は、instance内のpoweroff timer（boot fail-safeと、Launch時に設定するcost fail-safe）と
  terminate-on-shutdownによる。timerが働かない場合（instanceの異常など）、外部から自動停止する仕組みはない。
  失敗runのbucket / SGは`Collect` / `Cleanup`まで残る。
  したがって実費の判断は、Preflightが出す時間単価・最大稼働時間（fail-safe 4時間）・停止方式を確認して行う。
  実行中は`Status`で経過と費用を確認する。
- 同値性のgate（phase 3、4、6の行動不一致、7の比較）で1件でも不一致があれば、bootstrapはその時点で停止する（非0終了）。以降の性能測定は行わない。
  証跡はrunnerがuploadする。不一致は原因を調査してから扱い、性能判断へ進まない。

### 5.5 測定点・集計方法・判定（`report`に実装済みで、結果を見る前に固定する）

| 項目 | 測定点・集計 | 判定 |
| --- | --- | --- |
| 同値性 | differential 2 suiteのexit、phase 4 / 7の席ごとdigest・得点・順位・step・decision数、phase 6の行動不一致数、seed 0の#213 digest | すべて一致・不一致0 |
| 主性能（単一worker） | phase 6 Championの`choose_action()`合計時間。各backend 3回の中央値を比べ、短縮率 = 1 − R/P | **30%以上** |
| workerあたり追加メモリ | (a) phase 6 Championのprocess peak RSS（lisjong toolの`ru_maxrss`。親はbashなので、exec前から引き継ぐ分は数MBで両backend共通）の中央値の差（R − P）、(b) phase 7のworker初期化後peak RSS（`VmHWM`。import・Policy解決・backend検査の後）の16 worker中央値の差。**大きい方**で判定 | **20 MB（20,000,000 bytes）以下** |
| 起動 | phase 5のimport + 初回計算の中央値の差（R − P）。wheel取得（runnerのS3 input download、`runner.log`）とinstall秒（`install.json`）は別に記録する | **100 ms以下** |
| 配布 | compilerがないこと、binary限定install、fail-closed 3項目 | すべて満たす |
| 複数worker | phase 7：16 workerすべてで検査が通ること。同じ32半荘のwall time短縮率。system使用メモリの増分（MemTotal − MemAvailable、2秒間隔のpeak − 開始時）がR ≤ P + 16 × 20 MB | 全worker観測、**30%以上短縮**、メモリ条件 |
| 参考（判定外） | two-stepの短縮率、単一対局の半荘時間、半荘/時、games中のworker peak RSS（taskのworker割当がrunごとに異なるため）、1,000半荘あたりの推定compute費用（`--hourly-usd`） | — |

**証跡の完全性（判定より先に検証する）**：`report`は、指標を計算する前に、run directoryが§5.2〜§5.4の計画と一致するかを検査する
（`shanten_backend_verification/plan.py`・`evidence.py`）。

- replay：各Policy・各backendで、計画した3回（`REPLAY_ORDER`の位置）のfileがそろい、各1 passであること。
  計画にないfileがないこと。Policy class・入力のbytes SHA-256・decision数・backend・native call数（rustは1以上、pythonはなし）が一致すること。
- multi：両backendとも16 worker要求・16 worker観測・distinct worker pid 16、32半荘、seed 0..31、Champion、`4p-red-half`。
  各gameのworker backend・lisjong revision・`SOURCE_REVISION`が一致すること。
- 単一対局：両Policy・両backendで1 worker・seed 0。
- 比較：比較対象のdirectory、seed集合、#213 seed-0 digestの照合が実際に行われたこと。
- differential：必須2項目（native tests、Rust選択full suite）が両方あり、exit codeであること。
- startup：各backend 10 sample。wheel identity（file名・SHA-256）とrust / python probeのrevisionが一致すること。

1つでも欠損・不足・条件違いがあれば`decision = incomplete-evidence`とし、criteria・findingsは出さない（CLIはexit 3）。
bootstrapを正常に完走すれば完全な証跡になる。この検査は、不完全な証跡や、別runが混ざった証跡を再集計するときに誤った判定を防ぐためのものである。

判定の対応（`report.decision`）：

- `incomplete-evidence`：上記の完全性・条件を満たさない。採用判断をしない。
- `investigate-mismatch`：同値性に1件でも不一致がある。性能判断をしない。
- `decline`（見送り）：Championの単一worker短縮率が15%未満（#213の基準）。
- `recommend-opt-in`（対象AWS環境でのopt-in利用を推奨）：上表の判定をすべて満たす。
- `further-study`（追加検討）：上記以外。

短時間の計測であり、長時間運用のメモリ推移や安定性を保証するものではない。速度比較は強さ向上の証拠ではない。

### 5.6 起動・停止・cleanup（承認後）

実行前に、使うAWS profileを`aws configure list-profiles`で確認する（このマシンでは`lisbun-admin`）。
merge SHAは`git fetch origin`の後に確認したcommitで固定し、それがorigin/mainに含まれる#401のmerge commitであることを確かめる。

```powershell
git fetch origin
$sha = '<確認済みの#401 merge commit（40桁）>'
git merge-base --is-ancestor $sha origin/main; if ($LASTEXITCODE -ne 0) { throw 'not on origin/main' }
git checkout --detach $sha   # 実行するbootstrapと同じrevisionから起動する
$awsProfile = 'lisbun-admin'
$wheel = 'C:\Dev\lisjong-artifacts\issue-216-rust-wheel\wheel\lisjong_native-0.1.0-cp314-cp314-manylinux_2_28_x86_64.whl'
$inputs = 'C:\Dev\lisjong-artifacts\issue-213-rust-backend-prototype\inputs'
$run = @{ AwsProfile = $awsProfile; Label = 'lisjong-400-rust'; InstanceType = 'c7i.4xlarge'; Workers = 16
          MinMemoryMiBPerWorker = 1024
          Bootstrap = 'scripts\aws\bootstrap-rust-shanten-400.sh'
          BootstrapArgs = @('--arena-revision', $sha)
          InputFile = @($wheel, "$inputs\decisions-placement-aware-speed-call.pickle", "$inputs\decisions-two-step.pickle")
          EstimatedRuntimeHours = @(1.5, 2.5); FailSafeHours = 4; CostBudgetUsd = 5 }
.\scripts\aws\lisjong-ec2.ps1 -Action Preflight @run
.\scripts\aws\lisjong-ec2.ps1 -Action Launch @run -Plan <run dir>\plan.json
.\scripts\aws\lisjong-ec2.ps1 -Action Status  -RunId <run-id> -AwsProfile $awsProfile
.\scripts\aws\lisjong-ec2.ps1 -Action Collect -RunId <run-id> -AwsProfile $awsProfile
```

- Launch判断：Preflightの`plan.json`で、時間単価・見積もり・fail-safe worst caseを確認してから行う。
- 停止条件：同値性gateの失敗（bootstrapが停止）、fail-safe 4時間、または手動（`Collect -ForceTerminate`）。
  失敗runではinstanceは自動終了しないため、`Collect`（必要なら`-ForceTerminate`）で終了させる。
  成功時は、証跡upload後にrunnerがinstanceを終了する（#396）。
- cleanup：`Collect`で終了・download・SHA-256照合・SG / bucket削除・residual sweepを行う。
  bucketが残った場合は`Cleanup`を使う。EC2、SG、bucketが残っていないことを親Issueに記録する。
- 証跡：`C:\Dev\lisjong-artifacts\issue-400-rust-shanten-aws\<run-id>\`（`output/`一式、`plan.json`、`report.json`）。
  結果と判断を#400と親lisjong#216へ記録する。
