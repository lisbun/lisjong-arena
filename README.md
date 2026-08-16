# lisjong-arena

Reproducible policy comparison arena for the lisjong ecosystem.

> [!IMPORTANT]
> lisjong-arena is part of an independent personal Japanese mahjong AI project
> developed by [lisbun](https://github.com/lisbun). It is not affiliated with any
> other project using the LisJong or lisjong name.

## 概要

`lisjong-arena` は、複数のlisjong Policyを再現可能な条件で対局させ、Policy全体の
強さを比較するためのrepositoryです。

現在の到達目標は高度な統計処理ではなく、**同じ条件で再実行すれば同じ結果が得られる
最小のPolicy comparison protocol**を成立させることです。

lisjong ecosystem全体のrepository責務、repository間依存方向、長期ロードマップは
[`lisjong-project`](https://github.com/lisbun/lisjong-project) を正本とします。

## 責務

Arenaが所有するもの:

- Policy同士のmatchup定義
- fixed seed set
- deterministicなseat rotation
- 複数gameの実行計画
- Policy assignmentの記録
- raw comparison result
- 平均順位・平均得点・順位回数等の基本metrics
- 再現可能なPolicy comparison protocol

Arenaが所有しないもの:

- Policy / AI戦略、Action / state evaluation、向聴数・受け入れ等のAIロジック
- Policy contract
- RiichiEnv Adapter、Observation / Action変換、legal action validation
- 麻雀ルール、単一gameのgame state transition
- 学習model実装

## 実行経路

初期Arenaは `lisjong-engine` の完成を待たず、すでに利用できる `lisjong` の
RiichiEnv integrationを使ってPolicy比較を成立させます。

```text
lisjong-arena
    |
    | matchup / seeds / seat rotation / aggregation
    v
lisjong
    |
    | existing RiichiEnv integration / LocalGameRunner
    v
RiichiEnv
```

単一gameの実行は既存の `lisjong.local_game_runner.LocalGameRunner` へ委譲します。
`Seat` もArenaで再定義せず `lisjong.policy_contract.Seat` を使用し、
`lisjong-arena` からRiichiEnvへは直接依存しません。

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

## 使い方

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

## 現時点で持たないもの

- 信頼区間、統計検定、bootstrap statistics
- Elo / rating system
- graph / visualization / dashboard
- database / persistence layer / result file format
- distributed execution / multiprocessing / job scheduler
- RiichiLab rankedを使った強さ比較
- `lisjong-engine` integration
- CLI

高度な統計処理を持たないのは意図的です。同一game内の複数seat resultは相関して
いるため、それらを独立標本とみなす信頼区間や検定は誤った精度を主張します。まず
この最小protocolで実測データを得てから、別Issueで比較方法を設計します。

### backend abstractionをまだ持たない理由

`GameBackend` / `EvaluationBackend` / backend registry / 汎用runner protocol
のような抽象化は導入していません。現時点で実在する実行経路はRiichiEnv経由の1本
だけで、`lisjong-engine` 経由の経路はまだ動いていません。1本しかない経路から共通
interfaceを推測すると、2本目が現れた時点でほぼ確実に作り直しになります。

`lisjong-arena` にとっての単一game実行境界は
`lisjong_arena.comparison._run_single_game()` という1関数だけです。2つの実経路が
実際に揃い、差異を実測できた段階で、必要な抽象化を判断します。

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

RiichiEnvは `lisjong` の依存として入ります。`lisjong-arena` 自身はRiichiEnvへ
直接依存しません。

### 品質確認

ローカルとCIで同じコマンドを使用します。

```text
python -m ruff format --check .
python -m ruff check .
python -m unittest discover -s tests -v
```

unit testでは実RiichiEnvを起動せず、単一game実行境界を差し替えてrotation、実行
順序、Policy lifecycle、raw result、metrics、fail closedを検証します。実RiichiEnvを
使うintegration testは `MinimalPolicy` と `ShantenPolicy` の固定seed comparisonを
2回実行し、raw resultとmetricsが再現することを確認します。

## License

[MIT License](LICENSE)
