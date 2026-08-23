# AGENTS.md

## 適用範囲

このファイルはrepository全体へ適用する恒常的な作業規則である。
Issueまたはユーザーの明示的な指示が本書と異なる場合は、その指示を優先する。

以下の作業分担は現時点のdefault responsibilityであり、恒久的なtool制約や禁止ではない。利用可能なtool、credit、作業内容、学習目的に応じて担当や作業場所を変更できる。一方、本書でmandatoryとする安全境界と承認境界は維持する。

## Repositoryの責務

`lisjong-arena` は、lisjongのPolicyをconcrete environmentで実行・観測し、その実行をcontrolled / reproducibleな条件で評価する基盤を担当する。

repository内部では、次の2 layerを分離する。

```text
lisjong Policy contract
        ^
        |
execution / observation
        ^
        |
    evaluation
```

### Arena execution / observationが所有するもの

- environment-specific integration
- external / local runner / client
- session lifecycle / matchmaking
- retry / reconnect / continuous participation
- execution profile / credential source resolution
- protocol trace / raw game record acquisition
- objective execution event
- environmentへ実際に送信・適用したActionの記録
- external representationからlisjong-owned Policy contractへのprojection
- `InternalAction`からexternal legal Actionへのmapping / revalidation

### Arena evaluationが所有するもの

- Policy同士のmatchup定義
- fixed seed set
- deterministicなseat rotation
- 複数gameの実行計画
- Policy assignmentの記録
- raw evaluation / comparison result
- 平均順位・平均得点・順位回数等の基本metrics
- reproducible Policy comparison protocol
- comparison条件・raw result・metrics・provenanceのversion付きartifact contract
- A/B対等comparisonに限らないcandidate vs baseline群のevaluation protocol
  （例: ABBB single-round evaluation）
- external benchmark / external competitor orchestration

### Arenaが所有しないもの

- Policy / AI戦略
- `DecisionContext` / `InternalAction` のAI-side semantic contract
- Action / state evaluation、向聴数、受け入れ、HandBelief、risk / value / utility等のAIロジック
- Policy-internal candidate evaluation / selection reason semantics
- AI-side `InternalAction` semantic validation
- 麻雀ルール / game state transition
- 学習model実装
- generic external-player runtime / generic process host

責務判断では、**Arenaは「何が起きたか」を所有し、lisjongは「なぜそのActionを選んだか」を所有する**ことを基本線とする。

Arenaがlisjong-generated analysisを将来transport / persistenceしてもよいが、HandBelief等のpayload semanticsを再計算・再定義・correctness判定しない。

詳細なownership matrixとmigration boundaryは `docs/architecture.md` を正本とする。

## Current implementation と target ownership

現在のAABB / ABBB execution pathは次である。

```text
lisjong-arena evaluation
        |
        v
lisjong.LocalGameRunner
        |
        v
RiichiEnv
```

- `Seat` 等のAI-side contractは `lisjong.policy_contract` を使用する
- AABB / ABBB evaluation execution pathではArenaからRiichiEnvへdirect dependency / direct importを持たない。一方RiichiLab protocol-facing decision bridge(`lisjong_arena.riichilab.request_action` / `mjai_response`)は、Issue #27で`riichienv==0.4.8`をArena direct dependencyとして明示的に使用する(RiichiEnv game lifecycle自体はlisjong側RiichiEnv Adapterに残る)
- RiichiLab ranked first-party CLIと`RankedGameResult` / `run_ranked_game()`のcanonical one-game orchestrationはArenaにある
- RiichiLab ranked / validation orchestration、profile / credential / CLI composition、client errors / Session / Transport / protocol traceはArena-local canonical + physical implementationである
- lisjong側legacy lower-level runtime copyは`lisbun/lisjong#91` / PR #92で削除済みであり、Issue #25でArenaのdependency pinもcleanup merge SHAへ同期済みである
- `RiichiLabSeatAdapter` / request_action parse / MJAI response / possible-action validationは、Issue #27でArena-local canonical implementationへ移行済みである(`lisjong_arena.riichilab.adapter` / `request_action` / `mjai_response` / `possible_action_validation` / `adapter_errors`)。lisjong側legacy physical copyは`lisbun/lisjong#94` / PR #95で削除済みであり、Issue #29でArenaのdependency pinもこのactual cleanup merge SHAへ同期済みである
- RiichiEnv Adapter、`LocalGameRunner`、`GameTrace`はまだ`lisjong`に存在する
- `lisjong` dependencyは再現可能性のためrelease tagが出るまでfull commit SHAへpinする

これは**current physical placement**であり、恒久ownershipではない。

Target architectureでは次をArena execution / observationへ段階移管する。

- RiichiLab client / Adapter / protocol trace / session lifecycle
- RiichiEnv Adapter / external action mapping
- `LocalGameRunner` / local execution orchestration
- `GameTrace` / objective observation contract

一方、`DecisionContext` / `InternalAction` / Policy analysis等のsemantic contractはlisjongへ残す。

ArenaからRiichiEnv等のexternal environmentへdirect dependencyを追加することはtarget architecture上許容する。actual dependency追加・version・package layoutはconcrete migration Issueで決定する。

## Internal dependency direction

Arena内部では次を維持する。

```text
evaluation
    -> execution / observation
    -> lisjong Policy contract
```

禁止する方向:

```text
execution / observation
    -X-> AABB / ABBB semantics
    -X-> evaluation seed / rotation semantics
    -X-> strength metrics / statistical comparison
    -X-> evaluation artifact schema

lisjong
    -X-> lisjong-arena
```

Execution / observationはevaluation-specific semanticsを知らなくても成立する構造にする。

## Policy contract / Adapter boundary

`DecisionContext`のfield、visibility、meaning、seat-visible information semanticsはlisjongが所有する。

External environmentからのacquisition / materialization / projection codeはArena target ownershipとしてよいが、projection先contractをArena都合で変更しない。

Action validationは次の2責務へ分ける。

```text
lisjong:
    InternalActionがAI-side contractとしてsemanticに妥当か

Arena execution / observation:
    InternalActionを現在のexternal legal Actionへ
    正しくmapping / revalidationできるか
```

## Trace / analysis boundary

`GameTrace`はobjective execution observationとしてArena target ownershipとする。ただし既存GameTraceをRiichiEnv / RiichiLab / lisjong-engine共通のgeneric canonical traceへ先行一般化しない。

次をGameTraceへ暗黙に混在させない。

- shanten
- ukeire
- HandBelief
- danger / value estimate
- candidate evaluation
- selection reason

Policy-internal analysisはlisjong-owned semanticsとして別channelで扱う。DecisionTrace / AnalysisEnvelope / correlation ID等はconcrete consumerなしに先行設計しない。

Observation / analysisの有無そのものによってPolicyのAction selectionを変えない。ただしtrace / sink / persistence failureをsilentに無視することまでは要求せず、fail-closed execution failureを許容する。

## Information-flow / secret boundary

次を維持する。

```text
credential / Authorization information
    -X-> trace / game record / evaluation artifact

privileged offline / ground-truth data
    -X-> online Policy input

observer-only execution data
    -X-> Policy decision path
```

`.env`、token、API key、credentialをrepositoryへcommitしない。

## Generic abstractionを先行しない

RiichiLab / RiichiEnv等のconcrete execution pathとmigration実績を確認する前に、次を先行導入しない。

- `GameBackend`
- `EvaluationBackend`
- backend registry
- universal Agent API
- generic external process host
- generic match runtime
- environment abstraction hierarchy
- project-wide canonical GameTrace

共通化は複数の実経路とconcrete consumerの差異を確認してから判断する。

独立した`lisjong-runtime` repositoryも現時点では作らない。24/7 production hosting、independent deployment、Arena外の複数consumer等が成立した場合に再検討する。

## Evaluation protocolの設計方針

Arenaは同等Policy同士のA/B対等comparisonだけでなく、candidate 1体を固定baseline群へ投入して評価するような、意味の異なるevaluation protocolも所有できる（例: 既存AABB comparisonとABBB single-round evaluation）。

- matchupの意味が異なるprotocolを既存Plan / Resultへoption追加で無理に統合しない。必要に応じて独立したPlan / Result contractを持つ
- `4p-red-single` のようにprotocol identityそのものを構成する条件はcaller-configurable defaultにせず、そのprotocol自身のinvariantとして固定する
- public Result valueはconstruction時点で件数、順序、seat / candidate assignment、protocol条件、metricsの母数がplanと整合することをfail closedで検証する
- deterministic reproducibilityとstatistical strength claimを混同しない

## デフォルトの作業分担

- Git変更を伴わない方針・設計相談、Issue整理、実装方針・PR・実測結果のレビュー、GitHubへ記録する内容の作成は、通常のChatGPT conversationをdefaultとする
- source code、test、refactor、実装と不可分な小規模文書、品質確認、Git作業は、現時点ではClaude Codeをdefaultの変更担当とする
- `AGENTS.md`、README、設計・調査文書、文書間整合やstale documentationの整理は、現時点ではChatGPT WORKをdefaultの変更担当とする
- 上記は専属担当を定めない。必要に応じてAI間で担当を入れ替えられる

## 開発フロー

### Issue、branch、Pull Request

- GitHub Issueを作業の目的、scope、完了条件の正本とする
- `main`へ直接pushせず、実際にGit上の変更を担当する作業主体が対応Issueの主作業branchを作成する
- 原則として1 Issueにつき1つの主作業branchを使い、概ね1つのPull Requestで完結させる
- 1つのPull Requestでは1つの主目的を扱い、無関係な変更を混ぜない
- Git変更担当AIは、必要なIssue作成、branch作成、ファイル変更、品質確認、commit、push、Pull Request作成、Issueとの関連付け、Ready for review化までを追加承認なしで進めてよい
- PRのmergeでIssue全体が完了する場合は`Closes #123`等を使用し、途中PRや一部変更だけを扱う場合は`Refs #123`等を使用する

### mergeと完了後cleanup

- Pull Requestのmergeにはユーザーの明示的な承認を必要とする
- ユーザーがmergeを承認した時点で、そのmergeに伴う定型cleanup（`Closes #...`によるIssue close、完了Issueの手動close、不要になったremote branchの削除）も承認済みとみなし、追加承認を必要としない
- merge後は完了条件と不要branchのcleanupを確認する
- PRをmergeせずcloseした場合は、Issueを機械的にcompletedとしてcloseしない
- `main`等の長期branchはcleanup対象にしない

### repository settingsとその他の承認境界

- repository settings変更には個別のユーザー承認を必要とする。ただし、merged PRの不要なhead branchを自動削除する設定など、visibility、branch protection、Actions・security・permission、secret、外部公開、課金へ影響しない安全なcleanup設定に限り追加承認を不要とする
- 上記の承認済みmergeに伴う定型cleanupを除き、破壊的操作、外部公開、課金、認証情報の使用は、対象と影響を示して承認を得る

## 実装規則

- 通常版CPython 3.14を初期基準とし、free-threaded build（3.14t）は互換性を個別に検証するまで対象外とする
- 比較条件と比較結果は不変valueとして表現し、結果の意味が曖昧になる入力はfail closedする
- 比較対象はPolicy instanceではなく、明示的identityとfactoryの組で保持する。identityをclass名から暗黙導出しない
- Policy instanceは各game・各seatごとにfactoryから新規生成し、seat間・game間で共有しない
- comparisonは全体としてfail closedにする。1 gameでも失敗した場合、成功したgameだけの結果を返さず、失敗gameをskipするfallbackも導入しない
- 実行順序は各protocol contractに従ってdeterministicにし、raw resultの順序も監査可能に保つ
- artifactは実行用modelと分離したimmutable snapshotとし、factory・callable・任意codeを保存・復元しない。既存artifactを上書きせず、内部矛盾をload時にfail closedする
- migrationではexisting AABB / ABBBを壊さず、main branchをbroken stateにしない
- temporary compatibility / re-exportを使う場合はimplementationを複製せず、removal conditionをfollow-up Issueへ明記する
- cross-repository physical migrationで短期間のlegacy implementation copyが不可避な場合は、concrete Issueでcanonical side・legacy side・removal Issueを明示し、長期並行発展させない
- 調査前に将来の構造を過剰設計しない。module分割も最初から細かくしすぎない
- 信頼区間、統計検定、Elo / rating、visualization、database、distributed execution、job schedulerは必要性を実測してから別Issueで扱う
- 外部libraryを追加する場合は、必要性、license、version、保守状況を確認する

## テストと品質確認

変更内容に応じて、Pull Request前に次を実行する。

```text
python -m ruff format --check .
python -m ruff check .
python -m unittest discover -s tests -v
```

- 文書だけの変更では最低限`git diff --check`を実行する
- unit testでは実RiichiEnvを毎回起動せず、単一game実行境界を差し替えて高速に検証する。ただしtestのためだけにproduction側へgeneric backend abstractionを導入しない
- testは正常系だけでなく、rotationとPolicy assignment、実行順序、metricsの母数、異常入力、失敗時のfail closed、再現性を優先して固定する
- 実RiichiEnvを使うintegration testは重くしすぎず、環境差でflakyになる厳密なwall-clock thresholdを入れない
- 実行できなかった確認は、理由と影響をPull RequestまたはIssueへ記録する
- code変更により利用方法、設計、制約が変わる場合は関連文書も更新する

## 秘密情報と外部成果物

次をrepositoryへcommitしない。

- `.env`、token、API key、credential
- 外部model weightおよび生成model
- 利用条件を確認していない牌譜・raw data
- 実験artifact、対局結果のrun出力、coverageやcache等の生成物

秘密情報らしき値や大容量binaryを発見した場合は変更を止め、内容を出力せずにユーザーへ報告する。
