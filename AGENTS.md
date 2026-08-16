# AGENTS.md

## 適用範囲

このファイルはrepository全体へ適用する恒常的な作業規則である。
Issueまたはユーザーの明示的な指示が本書と異なる場合は、その指示を優先する。

以下の作業分担は現時点のdefault responsibilityであり、恒久的なtool制約や禁止ではない。
利用可能なtool、credit、作業内容、学習目的に応じて担当や作業場所を変更できる。
一方、本書でmandatoryとする安全境界と承認境界は維持する。

## Repositoryの責務

`lisjong-arena` は、複数のlisjong Policyを再現可能な条件で対局させ、Policy全体の
強さを比較するarena / comparison基盤を担当する。

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

- Policy / AI戦略
- Action / state evaluation、向聴数・受け入れ等のAIロジック
- Policy contract
- RiichiEnv Adapter、RiichiEnv protocol、Observation / Action変換
- legal action validation
- 麻雀ルール
- 単一gameのgame state transition
- 学習model実装

責務判断では「その機能がなくても複数対局の比較条件を計画・集計できるか」を基準と
する。単一gameを正しく進行させるための機能はArenaへ持ち込まない。

## 実行経路と依存方向

初期Arenaの実行経路はproject-wide architectureどおり次に固定する。

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

- 単一gameの実行には既存の `lisjong.local_game_runner.LocalGameRunner` を使用する
- `Seat` 等の共通契約型はArenaで再定義せず `lisjong.policy_contract` を使用する
- `lisjong-arena` からRiichiEnvへ直接依存・直接importしない
- `lisjong` への依存は再現可能性のため、release tagが出るまでfull commit SHAへpinする

RiichiEnvと `lisjong-engine` という2つの実経路が実際に揃うまで、`GameBackend` /
`EvaluationBackend` / backend registry / 汎用runner protocol / environment
abstraction hierarchy等の将来を推測したabstractionを先行導入しない。共通化は実経路の
差異を確認してから判断する。

## デフォルトの作業分担

- Git変更を伴わない方針・設計相談、Issue整理、実装方針・PR・実測結果のレビュー、
  GitHubへ記録する内容の作成は、通常のChatGPT conversationをdefaultとする
- source code、test、refactor、実装と不可分な小規模文書、品質確認、Git作業は、
  現時点ではClaude Codeをdefaultの変更担当とする
- `AGENTS.md`、README、設計・調査文書、文書間整合やstale documentationの整理は、
  現時点ではChatGPT WORKをdefaultの変更担当とする
- 上記は専属担当を定めない。必要に応じてAI間で担当を入れ替えられる

## 開発フロー

### Issue、branch、Pull Request

- GitHub Issueを作業の目的、scope、完了条件の正本とする
- `main`へ直接pushせず、実際にGit上の変更を担当する作業主体が対応Issueの主作業
  branchを作成する
- 原則として1 Issueにつき1つの主作業branchを使い、概ね1つのPull Requestで完結させる
- 1つのPull Requestでは1つの主目的を扱い、無関係な変更を混ぜない
- Git変更担当AIは、必要なIssue作成、branch作成、ファイル変更、品質確認、commit、push、
  Pull Request作成、Issueとの関連付け、Ready for review化までを追加承認なしで進めてよい
- PRのmergeでIssue全体が完了する場合は`Closes #123`等を使用し、途中PRや一部変更だけを
  扱う場合は`Refs #123`等、Issueを早期closeしない関連付けを使用する

### mergeと完了後cleanup

- Pull Requestのmergeにはユーザーの明示的な承認を必要とする
- ユーザーがmergeを承認した時点で、そのmergeに伴う定型cleanup（`Closes #...`による
  Issue close、完了Issueの手動close、不要になったremote branchの削除）も承認済みと
  みなし、追加承認を必要としない
- merge後は完了条件と不要branchのcleanupを確認する
- PRをmergeせずcloseした場合は、Issueを機械的にcompletedとしてcloseしない
- `main`等の長期branchはcleanup対象にしない

### repository settingsとその他の承認境界

- repository settings変更には個別のユーザー承認を必要とする。ただし、merged PRの不要な
  head branchを自動削除する設定など、visibility、branch protection、Actions・security・
  permission、secret、外部公開、課金へ影響しない安全なcleanup設定に限り追加承認を不要とする
- 上記の承認済みmergeに伴う定型cleanupを除き、破壊的操作、外部公開、課金、認証情報の
  使用は、対象と影響を示して承認を得る

## 実装規則

- 通常版CPython 3.14を初期基準とし、free-threaded build（3.14t）は互換性を個別に
  検証するまで対象外とする
- 比較条件と比較結果は不変valueとして表現し、結果の意味が曖昧になる入力はfail closedする
- 比較対象はPolicy instanceではなく、明示的identityとfactoryの組で保持する。identityを
  class名から暗黙導出しない
- Policy instanceは各game・各seatごとにfactoryから新規生成し、seat間・game間で共有しない
- comparisonは全体としてfail closedにする。1 gameでも失敗した場合、成功したgameだけの
  結果を返さず、失敗gameをskipするfallbackも導入しない
- 実行順序（seed入力順 -> rotation -> seat）とraw resultの順序をdeterministicな契約として扱う
- 調査前に将来の構造を過剰設計しない。module分割も最初から細かくしすぎない
- 信頼区間、統計検定、Elo / rating、visualization、database、persistence、distributed
  execution、job schedulerは、最小comparisonの実測データを得てから別Issueで扱う
- 外部libraryを追加する場合は、必要性、license、version、保守状況を確認する

## テストと品質確認

変更内容に応じて、Pull Request前に次を実行する。

```text
python -m ruff format --check .
python -m ruff check .
python -m unittest discover -s tests -v
```

- 文書だけの変更では最低限`git diff --check`を実行する
- unit testでは実RiichiEnvを毎回起動せず、単一game実行境界を差し替えて高速に検証する。
  ただしtestのためだけにproduction側へ汎用backend abstractionを導入しない
- testは正常系だけでなく、rotationとPolicy assignment、実行順序、metricsの母数、
  異常入力、失敗時のfail closed、再現性を優先して固定する
- 実RiichiEnvを使うintegration testは重くしすぎず、環境差でflakyになる厳密な
  wall-clock thresholdを入れない
- 実行できなかった確認は、理由と影響をPull RequestまたはIssueへ記録する
- code変更により利用方法、設計、制約が変わる場合は関連文書も更新する

## 秘密情報と外部成果物

次をrepositoryへcommitしない。

- `.env`、token、API key、credential
- 外部model weightおよび生成model
- 利用条件を確認していない牌譜・raw data
- 実験artifact、対局結果のrun出力、coverageやcache等の生成物

秘密情報らしき値や大容量binaryを発見した場合は変更を止め、内容を出力せずにユーザーへ
報告する。
