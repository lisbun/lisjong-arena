# #573: L2a M1+R1 / Arm Y AWS実行計画

Status: 計画・実装。課金・正式seed予約・formal lock・対局は未実施。
本書は [#573](https://github.com/lisbun/lisjong-arena/issues/573) の実行案である。
ユーザーの「AWSでの実行まで含めて進行」に基づく準備であり、未確定だった費用上限を
**新規累計10 USD、2回のone-shot起動**として提案する。計画の承認後に下の順序で進める。
Champion変更は含めない。STOP後の再開・再起動は別承認である。

## 固定する比較とruntime

- 設計 lisbun/lisjong#368: K=100、600半荘、全6配置、主指標はseed blockごとの
  `mean_rank_ArmY - mean_rank_L2a`。95%正規近似区間の下限>0でCANDIDATE SUPERIOR、
  上限<0でCHAMPION SUPERIOR、それ以外INCONCLUSIVE。局・半荘・seatを独立標本にしない。
- M1 identity `c976ed4a5cb667c406eb57f5cc79c128b1a221fb9b478f0dd017fcf48bde6e03`、
  R1 `d565713f51a2b9f9cbdd2146bf01cb4bfb13ff859b639de962c74562eaf01efc`。
  Arm Y checkpoint `ecb31fc6…bb45e`、weights SHA-256
  `746ef5ada2cc83d2aefbfa94cac464f03d27edb8082d22e0e455ea916dde385f`。
  3 artifactは#564のbundleと同一hashを要求する。再学習・guard変更なし。
- lisjong `a341257f199e284a3519e17b458e06ced9f04c67`、native wheel SHA-256
  `01d2476cf433f1a3be3d336679366364929d84299f439b19a83f07a087ebd1d5`、
  engine `91af75e3aa11520c3b0543719dc74bb4c517ee06`、Rust backend。
  CPython 3.14通常build、torch 2.13.0+cpu、CPU、各worker threads=1、deterministic。
  実際のPython patch versionもlockし、不一致はSTOP。
- Arenaは本変更をmergeしたclean mainのexact SHA。準備成功後の正式seed台帳PRによって
  SHAが変わる場合は、差分が台帳だけであることを確認し、そのSHAをformal lockに固定する。
  pin/runtime/record経路の変更があれば自動継続せず計画へ戻る。
- #564 PREPARATION-PASSは旧新runtimeの固定1,151件MATCHと局記録経路の証拠であり、
  新しい並列実行の証拠には流用しない。#572の11固定局面ではserving不具合なし、
  合法な和了等を選ばない例はモデルの性質。Arm Yを差し替えない。

## AWS・予算

| 項目 | 固定値 |
| --- | --- |
| account / region / profile | 507861384062 / ap-northeast-1 / lisbun-admin |
| instance | c7i.4xlarge On-Demand、16 vCPU・8物理core・32 GiB |
| worker | **8**。6等への切替なし |
| process tree | MemoryMax=4 GiB（4,194,304 KiB）、MemorySwapMax=0、OOMPolicy=kill、TimeoutStopSec=3 |
| storage | encrypted gp3 root 40 GiB、workspace＋collectionは10 GiB検知上限、開始時空き20 GiB以上 |
| validation instance | fail-safe 3時間、1回のみ、累積stage上限10,800秒 |
| formal instance | fail-safe 6時間、1回のみ、累積stage上限18,000秒 |
| 費用上限 | **新規累計10 USD**（#564の支出と承認枠は別）。各launch前に残予算とfail-safe最悪額を照合 |

Pricing API（2026-11-07）でTokyo/Linux/shared/NA/UsedのEC2単価は
**0.8988 USD/時**（SKU `5JPUS4UJNNTMFMDG`、effective 2026-11-01）。
これにEBS・public IPv4・S3/transfer・marginを足し、既存
`scripts/aws/lisjong-ec2.ps1 -Action Preflight` の計算を正本として起動前に再取得する。
EC2部分の9時間上限は8.0892 USD。追加費用込みのPreflight最悪額の和が10 USDを超えれば起動しない。
既存のno-inbound SG、SSM、IMDSv2、暗号化、terminate-on-shutdown、boot fail-safe、
回収・残存resource確認を使用する。未承認のinstanceへの変更なし。

c7i.2xlargeは8 vCPU/4物理coreで8 workerがSMTを共有するため、4xlargeを選ぶ。
これは線形8倍速の保証ではない。600×18.13秒÷8=約23分は理想比較値だけである。
正式runには**12,600秒（210分）**を割り当てる。過去最大値を逐次で600倍した
約3時間を上回るが、未使用seedのworst-caseを保証しない。期限で止め、延長しない。
各半荘timeoutは300秒。起点はsubmit直前で起動待ちを含むWAIVED-P1を継承し、
false STOPでも自動救済しない。

## 段階1: 8 worker validation（正式seedなし）

#564の5回目bundle `4ec0f71e…e645` の入力だけを再利用する。既使用TRAIN/SELECT subset、
M1/R1/Arm Y、旧新wheelは不変。外部取得は既存setupと同じGitHub、PyPI、PyTorch CPU packageのみ。
新しい牌譜・model・未使用OFFLINE-EVALは取得・開封しない。

| 段階 | wall上限 | PASS条件 |
| --- | ---: | --- |
| setup | 1,800秒 | exact SHA clone/install、入力hash、clean main、環境記録 |
| OS/scope | 300秒 | #564の10停止case＋本書の親子scope確認、全PASS |
| manifest | 300秒 | #564の固定subset manifestとbyte一致 |
| 旧capture | 1,200秒 | 固定1,151件、旧runtime strict load |
| 新capture/compare | 1,200秒 | MATCH、coverage達成、非有限値・action差0 |
| binding/mapping | 1,200秒 | exact strict load、mapping test成功、dev-plan workers=8 |
| dev-run | 1,800秒 | 472005/472006×6配置＋472005再現6半荘、18完走、再現6/6 MATCH |
| verify/collect | 1,800秒 | 別processと回収copyのdev-verify一致、checksum error 0 |

全stage上限の和9,600秒は3時間fail-safe内。stage間も累積時計をresetしない。
`dev-plan --workers 8 --timeout-seconds 300 --wall-seconds 1800 --peak-rss-kib 4194304`
を指定し、`dev-run` scopeのRuntimeMaxSecも1800にする。
workers以外のschedule・report semanticsは従来の固定18半荘と同じで、正式評価ではない。
保存planのowner/implementationは既存の#564/#567、今回の運用ownerは#573としてrun記録へ明記する。

新watchdogは `systemctl --user show <unit>.scope --property=ControlGroup --value` で
対象scopeを特定する。PIDの移動前のSSM serviceを読まない。実scopeのmemory.peakを
処理終了後にも読むため、wrapperは最終取得・releaseまでscopeを保持する。
scopeが不明、path不一致、peak読取失敗、成功なのに最終peak未記録ならfail closed。
起動時の未作成scopeだけは10秒を上限に待つ。

```text
# Python 3.14 / ec2-user / systemd user managerで、対局・modelなし
python3.14 scripts/aws/check-learning-573-watchdog.py <未作成のevidence directory>
```

親32 MiB・子96 MiBをallocateし、親子の/proc cgroupと記録pathの一致、scope peakが
allocation合計以上、正常終了時のfinal_peak_recorded=true、wall停止時の親子終了を確認する。
ローカルWSLでは2026-11-07にPASS: 正常scope peak 261,165,056 bytes、
wall case 261,423,104 bytes / exit124、停止理由wall、親子撤収済み。
証跡 `.cache/issue-573/watchdog-check-3/`（commit対象外）。
最初のPython3.12構文不対応とPATH上Python3.14不在はdummy起動前の失敗であり、
モデル・対局・評価seedを実行していない。AWSではexact Python3.14で再確認する。

8 workerの実RSS単純和3,325 MiBは安全の証拠にしない。全workerのload、18半荘、
保持・書込み・verifyに対する実scope peakとOOMを確認する。OOM・STOP・上限超過・
再現不一致があれば後続を止める。MemoryMax・worker・timeoutを自動で変更しない。

## 段階2: 正式seed予約・one-shot評価

段階1の全PASS・回収verifyと、当該実装headのCI/semantic reviewをIssueへ記録してから進む。
事前候補seedは **573000..573099の昇順**（未予約）。最新台帳の全domainとregistry外既知seedを
照合し、衝突ならSTOP。別blockへ自動変更しない。予約はprotocol
`arena-learning-candidate-engine-aabb-v1`、engine domain、FORMAL-EVAL、population
`learning-candidate-engine-aabb-573` とし、identityとmerged台帳SHAを記録する。
正式lockはこの100 seeds・8 worker・exact artifact/environment・保存先を実loadから固定する。
未使用seedはvalidationに使わず、途中結果を見て計画を変えない。

| 段階 | wall上限 |
| --- | ---: |
| setup/hash/binding/lock・scope確認 | 2,400秒 |
| formal run（600半荘、8 worker） | 12,600秒 |
| 別process verify・回収・hash再確認 | 1,800秒 |
| 最終記録・cleanup余裕 | 1,200秒 |

和は18,000秒（5時間）。6時間fail-safeとの差1時間をOS起動・SSM・回収余裕とする。
600件fixtureの保存・strict verifyは#575にあり、今回の変更後もCIを通す。
実際の600件保持・集計・保存メモリの安全性は未証明で、正式runでも4 GiBを強制し超過ならSTOP。
出力の算術見積りは約150 MB、raw/observations/result/diagnostics用に1 GiBを見込み、
workspace＋collectionの10 GiB検知上限に含める（30秒poll、hard quotaとは区別する）。

局単位のobjective outcomeを同じrunで保存し、和了率・放銃率・平均和了打点・平均放銃失点を
`secondary_metrics.py` の既存定義で再導出する。率の母数は参加seat局数、seed内合算後にseed等重み。
条件付き打点は全seedの分子/分母、分母0はnull。選択理由やPolicy内部推定を混ぜない。
第2層event factsのINVALIDは既存protocolに従い主指標を上書きしないが、局4指標が
算出不能なら副指標未達として報告し、Issue全体を完了扱いにしない。

```text
python -m lisjong_arena.learning_candidate_engine_aabb lock --event 573 --workers 8 \
  --seeds <573000..573099をカンマ区切り> --allocation-identity <照合済みidentity> \
  --timeout-seconds 300 --wall-seconds 12600 --peak-rss-kib 4194304 \
  --wheel <exact wheel> --discard-artifact <M1> --reaction-artifact <R1> \
  --champion-checkpoint <ArmY> --raw <raw> --result <result.json> \
  --observations <observations.json> --diagnostics <diagnostics> --output <lock.json>
# 外側watchdog: MemoryMax=4G、RuntimeMaxSec=12600、wall=12600で次を実行
python -m lisjong_arena.learning_candidate_engine_aabb run --lock <lock.json>
python -m lisjong_arena.learning_candidate_engine_aabb verify --lock <lock.json>
```

lock/result/raw/observations/diagnostics、watchdog、runtime/limits、SHA256SUMSをrunごとに回収する。
保存先は各run固有の未作成directory。local運用成果物は
`C:\Dev\lisjong-arena\.cache\issue-573\`、AWS lifecycle出力はその下の`aws-ec2/<run-id>/`を使う。
回収後に別process verify・全SHA-256照合・600件順序・100 blocks・各role12 seat-resultを確認する。
成功分のみの正式公開、seed追加、救済再実行なし。STOP時も診断・課金時間・資源を回収し、
instance・volume・ENI・SG・bucketの残存確認まで行う。

## 起動前に固定する運用証跡

各launchの前に、merged Arena SHA、bootstrap/bundle/runnerのSHA-256、
Preflight plan.json、AMI、価格、fail-safe最悪額、累計残予算、保存先を#573へ記録する。
これらが揃うまでLaunchしない。初回validationと正式runを同一の古いbundleで誤起動しない。
preflight不一致・費用超過・未承認の条件変更もSTOPとする。
