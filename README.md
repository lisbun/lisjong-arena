# lisjong-arena

Reproducible policy comparison arena for the lisjong ecosystem.

> [!IMPORTANT]
> lisjong-arena is part of an independent personal Japanese mahjong AI project
> developed by [lisbun](https://github.com/lisbun). It is not affiliated with any
> other project using the LisJong or lisjong name.

## 概要

`lisjong-arena` は、lisjongのPolicy / agentをcontrolled / reproducibleな条件で評価し、
Policy間のperformance differenceやgame performanceを比較・検証するためのrepositoryです。

現在の到達目標は高度な統計処理ではなく、**同じ条件で再実行すれば同じ結果が得られる
最小のPolicy comparison protocol**を成立させることです。将来的には、Mortal等の
external competitorを利用したexternal benchmarkもArenaのevaluation責務として扱います。

lisjong ecosystem全体のrepository責務、repository間依存方向、長期ロードマップは
[`lisjong-project`](https://github.com/lisbun/lisjong-project) を正本とします。
`lisjong-arena` 固有の長期evaluation strategyは[Policy evaluation roadmap](docs/roadmap.md)を
参照してください。

## 責務

Arenaが所有するもの:

- Policy / agentのmatchup定義
- fixed seed set
- deterministicなseat rotation
- 複数gameの実行計画
- Policy / agent assignmentの記録
- raw comparison result
- 平均順位・平均得点・順位回数等の基本metrics
- 再現可能なPolicy comparison protocol
- 1 comparisonを1 fileとして保存するversion付きJSON artifact契約
- evaluation目的のexternal benchmark / external competitor orchestration

Arenaが所有しないもの:

- Policy / AI戦略、Action / state evaluation、向聴数・受け入れ等のAIロジック
- Policy contract
- lisjong用RiichiEnv Adapter、Observation / Action変換、legal action validationの再実装
- 麻雀ルール、単一gameのgame state transition
- lisjong自身のRiichiLab client / standalone participation
- 学習model実装
- generic external-player runtime / generic process host

## 実行経路

Arenaの実行経路は、現在実装済みのPolicy-vs-Policy evaluationと、将来許容する
mixed-agent external benchmarkを区別します。

### Current implementation: Policy-vs-Policy evaluation

現在のAABB / ABBBは `lisjong-engine` の完成を待たず、すでに利用できる `lisjong` の
RiichiEnv integrationを使ってPolicy比較を成立させます。

```text
lisjong-arena
    |
    | matchup / seeds / seat rotation / aggregation / artifact
    v
lisjong
    |
    | existing RiichiEnv integration / LocalGameRunner
    v
RiichiEnv
```

単一gameの実行は既存の `lisjong.local_game_runner.LocalGameRunner` へ委譲します。
`Seat` もArenaで再定義せず `lisjong.policy_contract.Seat` を使用し、
**現行実装では** `lisjong-arena` からRiichiEnvへ直接依存しません。

### Planned / allowed path: mixed-agent external benchmark

Mortal等のexternal competitorを含むevaluationでは、将来のconcrete implementationで
ArenaがOSS execution environmentを直接orchestrateしてよいものとします。

```text
                  lisjong-arena
                       |
              execution environment
                   /         \
                  v           v
          lisjong seat   external competitor
```

これはplanned / allowed pathであり、現時点で実装済みのpathではありません。本Issueの
文書同期だけを理由にRiichiEnvへのdirect dependency、mixed-agent runner、Mortal wrapperを
追加しません。

また、この経路を実装する場合も、Arenaがlisjong用のObservation / legal Action /
selected Action mapping / seat-visible information semanticsを独自に複製することは避け、
`lisjong`のPolicy contract / execution semanticsと公開integration capabilities / contractsを
可能な範囲で再利用します。具体的なreuse APIはconcrete implementation Issueで決定します。

## 最小comparison protocol

### Matchup と PolicySpec

比較対象はPolicy instanceではなく、明示的なidentityとfactoryの組
（`PolicySpec`）として指定します。

identityはclass名から暗黙導出しません。将来 `ukeire-v1` / `ukeire-v2` /
`model-a` のように、同じclassでも設定違い・model違いを別の比較対象として区別
できる余地を残すためです。A/Bのidentityが同じ場合は集計先を区別できなくなるため
拒否します。

### Policy lifecycle

Policy instanceは**各game・各seatごとにfactoryから新規生成**し、seat間・game間で
共有しません。1 gameのassignmentが `[A, A, B, B]` なら、`A` のfactoryも `B` の
factoryもその1 gameのためにそれぞれ2回呼ばれます。

Policy contractは意思決定へ影響するhidden mutable stateを禁止する一方、cacheや
metricsのような状態保持自体は許容するため、lifecycleをArena側で明示的に分離します。

### Fixed seed と seat rotation

seedは明示的なordered collectionとして与えます。入力順序もcomparison protocolの
一部として決定的に扱います。

各seedについて、base assignment `[A, A, B, B]` を4回巡回させます。

```text
rotation 0: [A, A, B, B]
rotation 1: [B, A, A, B]
rotation 2: [B, B, A, A]
rotation 3: [A, B, B, A]
```

実行順序は `seed入力順 -> rotation 0..3 -> Seat 0..3` で決定的です。
seed数をNとすると次のようになります。

```text
total games                = 4N
各Policyの参加game数        = 4N
各Policyのseat-result数     = 8N
各Policyの各seat担当回数    = 2N
```

同じseedを使うのは再現性と条件管理のためであり、異なるPolicy assignment間で同一の
game trajectoryになることまでは仮定しません。

### Raw result

raw comparison resultはseat単位のflatな不変record（`SeatResult`）の列です。

```text
seed / rotation / game_mode / seat / policy_identity / score / rank
```

この列の順序自体も `seed -> rotation -> seat` で安定する決定的な契約です。
`LocalGameResult.steps` / `decisions` は最小comparisonに不要なのでschemaへ含めて
いません。必要になった時点で拡張します。

### Basic metrics

Policy identityごとに `PolicyMetrics` を集計します。母数は次のとおり固定です。

| field | 意味 | 母数 |
| --- | --- | --- |
| `game_count` | そのPolicyが参加したgame数（1 gameで2 seat担当しても1） | game |
| `seat_result_count` | そのPolicyが担当したseat結果数 | seat result |
| `average_rank` | 平均順位 | seat result |
| `average_score` | 平均得点 | seat result |
| `first_count` 〜 `fourth_count` | 1位〜4位の回数 | seat result |

### Fail closed

comparison中に1 gameでも失敗した場合、成功したgameだけの `ComparisonResult` は
返しません。Policy factory failure、Policy execution failure、lisjong Adapter
failure、`LocalGameRunner` failure、結果の不整合はいずれも
`ComparisonExecutionError` としてcomparison全体を失敗させます。例外は失敗した
`seed` と `rotation` を保持し、元例外を `raise ... from` で連結します。失敗gameを
skipするfallbackは導入しません。

## AABB comparison protocolの使い方

```python
from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena import ComparisonPlan, PolicySpec, run_comparison

plan = ComparisonPlan(
    policy_a=PolicySpec(identity="minimal", factory=MinimalPolicy),
    policy_b=PolicySpec(identity="shanten", factory=ShantenPolicy),
    seeds=(12345, 23456, 34567),
)

result = run_comparison(plan)

for metrics in (result.metrics_a, result.metrics_b):
    print(
        metrics.policy_identity,
        metrics.game_count,
        metrics.seat_result_count,
        metrics.average_rank,
        metrics.average_score,
        (
            metrics.first_count,
            metrics.second_count,
            metrics.third_count,
            metrics.fourth_count,
        ),
    )

for seat_result in result.seat_results:
    print(
        seat_result.seed,
        seat_result.rotation,
        seat_result.seat,
        seat_result.policy_identity,
        seat_result.score,
        seat_result.rank,
    )
```

`game_mode` は既定で `"4p-red-half"`、`max_steps` は既定で `10_000` です。

`lisjong` の `UkeirePolicy` も同じ形で比較対象にできます。

```python
from lisjong.policies import ShantenPolicy, UkeirePolicy

plan = ComparisonPlan(
    policy_a=PolicySpec(identity="shanten", factory=ShantenPolicy),
    policy_b=PolicySpec(identity="ukeire", factory=UkeirePolicy),
    seeds=(12345,),
)
```

`UkeirePolicy` はdiscard候補ごとに多数の向聴数計算を行うため1局あたりの実行時間が
大きく、CIのintegration testには含めていません。

## ABBB single-round evaluation protocol

`lisjong-arena` はA/B対等comparisonだけでなく、candidate Policy 1体を固定
baseline Policy 3体へ投入し、fixed seedの最初の1局だけを評価する **ABBB
single-round evaluation** protocolも持ちます。既存AABB comparisonの
`ComparisonPlan` / `ComparisonResult` はA/B対等比較を意味する契約なので、
ABBBはそこへoption追加せず、独立した `SingleRoundEvaluationPlan` /
`SingleRoundEvaluationResult` として実装しています。

### ABBB rotation

各fixed seedについて、candidate `A` と固定baseline `B` を次の4通りへ
rotationします。

```text
rotation 0: [A, B, B, B]
rotation 1: [B, A, B, B]
rotation 2: [B, B, A, B]
rotation 3: [B, B, B, A]
```

- 実行順序は `seed入力順 -> rotation 0..3` で決定的です
- seed数をNとすると、total gamesは `4N` です
- candidateは各seatをちょうどN回ずつ担当します
- Policy instanceは既存AABBと同様、各game・各seatごとにfactoryからfreshに
  生成し、baseline 3seat間でもinstanceを共有しません

### `4p-red-single` invariant

ABBB single-round evaluationのgame modeは常に `4p-red-single` です。これは
既存AABB `ComparisonPlan.game_mode` のようなcaller-configurableなoptionや
default値ではなく、このprotocol自身のinvariantとして固定されています。
`SingleRoundEvaluationPlan` は `game_mode` fieldを持たず、呼び出し側が別の
game modeへ切り替えることはできません。

### ABBB raw result

raw resultはgame単位の不変record（`SingleRoundGameResult`）の列です。

```text
seed / rotation / game_mode / candidate_seat / scores（4 seat分のfinal score）
```

candidate scoreだけへ縮約せず、4 seat分の `scores` を正本として保持します。
candidate scoreは `scores[candidate_seat]` から導出します
（`SingleRoundGameResult.candidate_score`）。rankはこのprotocolのprimary
contractではないため保持しません。

### ABBB metrics

`SingleRoundCandidateMetrics` として次を集計します。

- `mean_candidate_score`: 全 seed × 4 rotationのcandidate final score平均
- `seat_mean_scores`: candidateがSeat 0〜3それぞれを担当した時のfinal score平均

開始score `25000` をArena側でhard-codeしたpoint deltaは使いません。和了率・
放銃率・聴牌率・順位率・composite reward等はこのprotocolのscope外です。

### ABBB fail closed

既存comparisonと同様、1 gameでも失敗した場合は成功したgameだけの
`SingleRoundEvaluationResult` を返さず、`SingleRoundEvaluationError` として
評価全体を失敗させます。例外は失敗した `seed` と `rotation` を保持します。

`SingleRoundEvaluationResult` はconstruction時点でも、`game_results` の件数が
`4N` であること、`seed入力順 -> rotation 0..3` の順序、各recordの
`candidate_seat` がrotationと一致すること、`game_mode` が `4p-red-single` で
あること、`candidate_metrics.candidate_identity` / `game_count` が `plan` と
一致することをfail closedで検証します。

### 使用例

```python
from lisjong.policies import MinimalPolicy, ShantenPolicy

from lisjong_arena import (
    PolicySpec,
    SingleRoundEvaluationPlan,
    run_single_round_evaluation,
)

plan = SingleRoundEvaluationPlan(
    candidate=PolicySpec(identity="minimal", factory=MinimalPolicy),
    baseline=PolicySpec(identity="shanten", factory=ShantenPolicy),
    seeds=(12345,),
)

result = run_single_round_evaluation(plan)

print(
    result.candidate_metrics.candidate_identity,
    result.candidate_metrics.game_count,
    result.candidate_metrics.mean_candidate_score,
    result.candidate_metrics.seat_mean_scores,
)

for game_result in result.game_results:
    print(
        game_result.seed,
        game_result.rotation,
        game_result.candidate_seat,
        game_result.scores,
        game_result.candidate_score,
    )
```

`max_steps` は既定で `10_000` です。

## Comparison artifact

既存のversion付きJSON artifact契約は、AABB comparison protocol（
`ComparisonPlan` / `ComparisonResult`）のみを対象とします。ABBB single-round
evaluation result（`SingleRoundEvaluationResult`）のartifact保存は現時点では
未実装で、必要になった時点で後続Issueとして独立に設計します。

成功した `ComparisonResult` は、呼び出し側が明示したpathへversion付きJSON artifact
として保存できます。comparison実行自体が暗黙にfileを生成することはありません。

```python
from pathlib import Path

from lisjong_arena import load_comparison_artifact, save_comparison_artifact

path = Path("comparison.json")
save_comparison_artifact(result, path)
artifact = load_comparison_artifact(path)

print(artifact.plan.policy_a_identity, artifact.plan.policy_b_identity)
print(artifact.plan.seeds)
print(artifact.provenance.lisjong_revision)
print(artifact.metrics_a.average_rank)
```

`save_comparison_artifact()` は `1 comparison = 1 immutable artifact` の方針で
既存fileを上書きせず、pathが存在する場合は `FileExistsError` で失敗します。
JSONはUTF-8、key順序固定、2-space indent、末尾newlineで保存し、非有限floatを
許可しません。

### Schemaと保持情報

初期schemaは `schema_version = 1`、comparison methodは独立したidentity
`fixed-seed-seat-rotation-v1` として記録します。readerは未知のschema versionや
comparison protocolを現在仕様として推測せずfail closedします。

artifactは次をlosslessに保持します。

- Policy A/B identity、ordered seeds、`game_mode`、`max_steps`
- `seed -> rotation -> seat` 順のraw `SeatResult`
- Policy A/Bの `PolicyMetrics`
- execution environment identity（現在は `riichienv`）
- `lisjong-arena` / `lisjong` / RiichiEnv / Pythonのversion
- VCS install metadataから確認した `lisjong` full commit ID

load時はfield型だけでなく、seed・rotation・seat順、A/B assignment、各gameの順位が
1〜4の順列であること、raw resultの件数、game mode、raw resultから再集計したmetrics
との一致まで検証します。truncated JSONや内部的に矛盾したrecordをdefault値で補完して
受理しません。top-level / nested objectのduplicate keyもlast-winsで解釈せず拒否します。

### Reproducibilityの意味と限界

artifactは「どの比較条件・Policy identity・execution provenanceで何が得られたか」を
監査し、対応するsourceとPolicy実装が利用可能なら同条件を再構成するためのrecordです。
過去のsourceやdependency自体を埋め込むものではなく、artifactだけからPolicyを自動実行
するものでもありません。

実行用 `ComparisonResult` と読込用 `ComparisonArtifact` は分離されています。
artifactへ `PolicySpec.factory`、Python callable、import path、dynamic codeを保存・復元
しません。secret、credential、environment variable、username、hostname、home directory、
absolute local path等の再現性に不要なmachine-local情報も保存しません。

現在のPolicy-vs-Policy execution pathは従来どおり `lisjong-arena -> lisjong -> RiichiEnv`
です。provenance取得にはpackage metadataを使い、ArenaからRiichiEnvへのdirect dependencyや
direct importを追加していません。planned mixed-agent external benchmark pathは別のconcrete
implementation Issueで扱い、本README更新だけでは実装しません。未完成の
`lisjong-engine` integrationやbackend abstractionも先取りしません。

artifactを保存できることとrepositoryで管理することは別です。test fixture以外の実測
artifactをrepositoryへ大量commitする運用、既定保存先、retention policy、artifact
repositoryは本機能の対象外です。

## 現時点で持たないもの

- 信頼区間、統計検定、bootstrap statistics
- Elo / rating system
- graph / visualization / dashboard
- database / artifact repository / retention policy
- distributed execution / multiprocessing / job scheduler
- RiichiLab rankedを使った強さ比較
- Mortalとのmixed-agent benchmark実装 / external competitor wrapper
- `lisjong-engine` integration
- CLI

高度な統計処理を持たないのは意図的です。同一game内の複数seat resultは相関して
いるため、それらを独立標本とみなす信頼区間や検定は誤った精度を主張します。まず
この最小protocolで実測データを得てから、別Issueで比較方法を設計します。

### backend abstractionをまだ持たない理由

`GameBackend` / `EvaluationBackend` / backend registry / 汎用runner protocol
のような抽象化は導入していません。現時点で実装済みのPolicy-vs-Policy実行経路は
RiichiEnv経由の1本だけで、planned mixed-agent external benchmarkも
`lisjong-engine` 経由の経路もまだ動いていません。実経路の差異を確認する前に共通
interfaceを推測すると、後続実装で作り直しになる可能性が高いためです。

AABB comparisonとABBB single-round evaluationは、それぞれ
`lisjong_arena.comparison._run_single_game()` /
`lisjong_arena.single_round_evaluation._run_single_game()` という小さな
private single-game execution boundaryを個別に持ち、いずれも単一gameの進行は
`lisjong.LocalGameRunner` へ委譲します。両者が同じ
`lisjong-arena -> lisjong -> RiichiEnv` の実経路を使うという類似だけを理由に、
共通のgeneric backend abstractionは導入しません。mixed-agent benchmark等のconcrete
execution pathが実際に増え、差異を実測できた段階で必要な抽象化を判断します。

## 開発環境

初期基準は通常版CPython 3.14です。free-threaded build（3.14t）は、依存libraryを
含む互換性を個別に検証するまで対象外とします。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Windows (PowerShell) では、activateコマンドを次のように読み替えます。

```powershell
.venv\Scripts\Activate.ps1
```

### lisjong dependency

`lisjong` にはまだrelease tagがないため、再現可能性を優先して `main` 追従では
なくfull commit SHAへpinしています（`pyproject.toml`）。

```text
lisjong @ git+https://github.com/lisbun/lisjong.git@b11841e287e8f11d55fe0fdaa5127ad16e00aa01
```

RiichiEnvは `lisjong` の依存として入ります。**現行実装では** `lisjong-arena` 自身は
RiichiEnvへ直接依存しません。将来のmixed-agent external benchmarkではevaluation-specific
external dependencyとしてArenaからRiichiEnv等へ直接依存することを許容しますが、
そのdependency追加は本Issueの対象外です。

### 品質確認

ローカルとCIで同じコマンドを使用します。

```text
python -m ruff format --check .
python -m ruff check .
python -m unittest discover -s tests -v
```

unit testでは実RiichiEnvを起動せず、単一game実行境界を差し替えてrotation、実行
順序、Policy lifecycle、raw result、metrics、fail closedを検証します。実RiichiEnvを
使うintegration testは、AABB comparison / ABBB single-round evaluationのそれぞれで
`MinimalPolicy` と `ShantenPolicy` の固定seedを2回実行し、raw resultとmetrics
（ABBBではgame_resultsとcandidate_metrics）が再現することを確認します。

## License

[MIT License](LICENSE)
