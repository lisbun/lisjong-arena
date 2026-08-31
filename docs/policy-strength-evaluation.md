# Policy strength evaluation policy

## Purpose

本書は、lisjong Policyのstrengthを`lisjong-arena`でどのような規律により
比較・解釈するかを定めるArena-ownedの恒久方針である。

本書は次を管理しない。

- current baselineやcurrent candidateの具体名
- Policy inventoryやcandidateごとのroadmap
- 過去runのscore、promotion / reject履歴
- 永久的なseed range
- 全評価へ共通するexact checkpoint、promotion / rejection threshold

これらの変化しやすいstateや個別判断を、本書と別の場所で重複管理しない。

## Ownership

Policy strengthに関する情報は、役割ごとに次のownerへ分離する。

| Owner | Responsibility |
| --- | --- |
| `lisjong` | Policy implementationとcurrent Policy status |
| `lisjong-arena` | evaluation protocol、fixed-seed execution、measurement、statistics、evaluation artifact、本書の共通評価方針 |
| bounded GitHub Issue / PR | 個別evaluation plan、実行条件、artifact参照、解釈、decision、work / decision history |
| `lisjong-project` | cross-repository direction、ownership、governance |

Arena artifactはmeasurementを、lisjongのcurrent statusは現在の解釈を、Issue / PRは
過去を含む個別判断を表す。同じmeasurement、current interpretation、historical
decision、cross-repository governanceを複数の場所で独立に更新しない。

current Policy statusの参照先は、lisjong側に実在するcanonical documentがある場合に
限って案内する。将来の想定pathを先取りしてcanonical sourceとして扱わない。

## Current ABBB strength-comparison protocol

Policy-vs-Policy strength comparisonのcurrent protocolは、candidate `A`を1体、
baseline `B`を3体配置するABBB single-round evaluationである。各gameは
`4p-red-single`で実行される。このgame modeはcallerが変更できるdefaultではなく、
protocol invariantである。

入力は順序を持つfixed / common seed列である。1 seedにつき同じseedを用いた4 gameを、
次の順序で実行する。

```text
rotation 0: [A, B, B, B]
rotation 1: [B, A, B, B]
rotation 2: [B, B, A, B]
rotation 3: [B, B, B, A]
```

したがって、seed数を`N`とするとgame数は`4N`であり、candidateは各seatをちょうど
`N`回担当する。candidate / baselineの役割はevaluation中に入れ替えず、Policy instanceは
各game・各seatについてfactoryからfreshに生成する。raw resultのcanonical orderは
`ordered seed -> rotation 0..3`である。

fixed seedとrotationは条件管理と再現性を与えるが、異なるrotationのgame trajectoryが
同一になることや、それだけでstrength differenceが証明されることを意味しない。

ABBBは、A/Bを各gameへ2席ずつ配置するAABB comparisonとは目的、assignment、Plan / Result、
artifact contractが異なる。個別Issueは使用するprotocolを明示し、両者のmetricやartifactを
混同しない。本書はcurrent implementationを説明するものであり、新しいprotocolを定義しない。

## Comparator selection

baselineは、評価目的に対して適切なcomparatorを選ぶ。

- overall strengthのpromotionを評価する場合は、lisjong-owned current strength statusで
  確認したcurrent strength baselineをprimary comparatorとする
- 特定機能の因果効果やcomponent差を確認する場合は、direct parentやcomponent Policyを
  secondary comparatorとして使用できる
- secondary comparisonの結果を、primary strength comparisonの代替として自動的に扱わない

個別Issueは選択したcomparatorと理由を記録する。本書にはcurrent baselineの具体名を固定しない。

## Evidence classes and seed hygiene

次の区別は既存evaluation evidenceの扱いを説明するためのものであり、新しいGate taxonomyや
統計的判定方式を定義するものではない。

### Exploratory evidence

仮説の探索、方向性確認、causal / component comparison等に使う。過去に使用したseedを
再利用した結果も利用できるが、reused seedであることを明示し、formal promotion evidenceと
同じ重みで扱わない。supporting evidenceとして使う場合も、その制約を解釈に残す。

### Formal screening / Gate evidence

bounded Issueでcandidate、comparator、protocol、fresh seed population、確認するmetrics、
decision scopeを結果を見る前に定める。fixed / common seedsにより再現可能に実行するが、
再現可能性とstrength claimは別に判断する。screeningはpositive、negative、inconclusiveの
いずれにもなり得る。

### Evidence supporting promotion

promotion判断を支えるevidenceは、そevidenceを取得する時点でcandidateの調整や
先行の結果確認に使っていないfresh seed populationから得る。追加evidenceは
それまでのformal evidenceとnon-overlappingなfresh seedsを使う。必要なevidence量や
判断基準は、evaluation目的に応じて個別のbounded Issueで事前に明示する。
本書では全candidate共通のthresholdを定めない。

すべてのclassで、次のseed hygieneを守る。

- candidate結果を見た後で都合のよいseedだけを追加しない
- reused seedの結果はexploratory / supporting evidenceであることを明示する
- overlapping seedをcumulative evidenceとして二重計上しない
- permanentな共通seed rangeを本書へ固定しない
- ordered seedと、その選定がfreshかreusedかを個別Issueに記録する

## Sample-size strategy

sample sizeは一律の大規模runではなく、次の段階的な原則で決める。

```text
small screening
    |
    v
positive / unresolved
    |
    v
additional fresh evidence
```

- 全candidateに一律10,000 gamesを要求しない
- 小さいsampleから開始できる
- 有望または未解決なcandidateだけ、追加のfresh evidenceを検討する
- 明確なregressionをsample追加で救済することを目的にしない
- inconclusiveを正当な結果として許容する

過去の個別評価で使われた400 gamesや10,000 gamesは、そのevaluationのhistorical checkpointで
あり、恒久的なmandatory checkpointではない。本書ではexact checkpoint sizes、alpha spending、
sequential-test formula、promotion / futility threshold、adaptive stopping ruleを定義しない。
必要なら個別のbounded Issueでevaluation開始前に定める。

current artifact capabilityでは、compatibleかつseedがnon-overlappingな複数のPolicy-vs-Policy
ABBB artifactを合成し、連結したraw game resultsからcumulative summaryを再集計できる。
このためstaged / incrementalにevidenceを蓄積できるが、合成可能であること自体は追加実行や
promotionを要求しない。

## Metrics and their meanings

current ABBB summaryは、raw game resultsから次のstrength metricsを導出する。

| Metric | Meaning |
| --- | --- |
| candidate mean score | 全`4N` gamesにおけるcandidate final scoreの平均 |
| baseline mean score | 全gameにおけるcandidate以外のbaseline 3 seats、合計`12N` seat scoresの平均 |
| mean candidate game delta | 各gameの`candidate final score - baseline 3 seatsのfinal score平均`を、全gameで平均した値 |
| candidate seat means | candidateがSeat 0〜3を担当した各`N` gamesのfinal score平均 |

`mean candidate game delta`は、各game内でcandidateを同じgameのbaseline 3 seatsと比較する
relative final-score metricである。これはcandidateの`SeatRoundStats.score_delta`をgame間で
平均する`mean round score delta`とは別metricである。

同一seedの4 rotationsは1 seed blockとして集約する。各blockのdeltaは、そのseedの4 gameの
candidate game delta平均である。current seed-block statisticsは次を含む。

- seed block count
- mean delta
- block deltaのsample standard deviation（`n - 1` denominator）
- standard error（sample standard deviation / `sqrt(seed block count)`）
- normal-approx 95% interval（mean ± `1.96 * standard error`）
- positive / zero / negative block count

seedが1つだけの場合、sample standard deviation、standard error、intervalは`N/A`となる。
normal-approx intervalはcurrent implementationが提供するdescriptive uncertainty summaryであり、
自動的なsignificance判定やpromotion ruleではない。

candidate Mahjong metricsは、candidateのraw `SeatRoundStats`だけから次を集計する。

- mean round score delta
- win count / rateとmean win points
- deal-in count / rateとmean deal-in loss
- exhaustive-draw count、tenpai count / rate
- tenpai reached countとmean first-tenpai turn

これらはcandidate-onlyのdescriptive metricsである。baseline側の同一Mahjong metricsとの差を
表していないため、candidate-only値からwin、deal-in、tenpai等の改善差を推測しない。

## Interpretation and decisions

individual evaluationの解釈とdecisionでは、次を守る。

- descriptive meanだけでpromotionを決めない
- small positive deltaを過大評価しない
- complexity itself is not successとし、深い探索や多いfeature自体を成功とみなさない
- 明確なregressionを大規模sampleで救済することを目的にしない
- inconclusiveを正当な結果として許容する
- strengthとruntime / computational performanceを別軸で評価する
- formal decisionではevaluation conditionsとprovenanceを明示する

promotion、reject、continue等に具体的な判断基準が必要な場合は、evaluation目的とcostに応じて
個別のbounded Issueで事前に明示する。本書へ全candidate共通の数値thresholdを固定しない。

## Measurement source of truth

current Policy-vs-Policy ABBB pathでは、1 runをversioned immutable artifactとして保存できる。

```text
Arena immutable evaluation artifact
    = measurement source of truth

derived summary
    = raw artifactから再生成可能

bounded GitHub Issue
    = artifact reference + compact summary + interpretation + decision
```

`single_round_compare --artifact-out`は、成功したPolicy-vs-Policy evaluationをopt-inで保存する。
`summarize_single_round_artifacts`は1 artifactを再集計でき、compatibleかつnon-overlappingな
複数artifactからcumulative summaryを生成できる。合成時はcandidate / baseline identity、
protocol parameters、execution provenanceの不一致やseed overlapをfail closedで拒否し、
silent deduplicationを行わない。

derived summaryはraw game resultsからcanonical aggregationで再生成できるcacheであり、唯一の
measurement正本ではない。GitHub Issueコメントへraw measurementを手作業で転記して永続化する
ことを標準運用にしない。artifact schema、validation、storage / retention policyは本書で
並行定義しない。

このartifact persistenceは、current `SingleRoundEvaluationResult`を使うfirst-party
Policy-vs-Policy ABBB pathが対象である。Mortal candidateのmixed evaluationでは
`--artifact-out`が未対応であり、RiichiLab、first-party `lisjong-engine`、その他すべての
strength evaluation pathにartifact persistenceが実装済みであるとは扱わない。

## Provenance and reproducibility

comparison evidenceでは、利用可能なcurrent artifact contractまたは個別Issueにより、可能な範囲で
次を追跡する。

- candidate / baseline identity
- ordered seeds
- protocol / game mode
- lisjong revision
- lisjong-arena revision
- lisjong-engine revision where applicable
- RiichiEnv version
- Python version

値を取得できない場合に推測・捏造しない。具体的なfield、schema、validation ruleはcurrent artifact
implementationを正本とし、本書で並行schemaを作らない。artifactが未対応のevaluation pathでは、
個別Issueに利用したimplementation / model / environment provenanceを明示する。

## Individual evaluations and historical documents

今後の標準的な情報配置は次のとおりとする。

```text
common durable evaluation policy
    -> this document

individual evaluation plan / execution / decision
    -> bounded GitHub Issue

raw measurement
    -> Arena artifact where supported

current Policy state
    -> lisjong-owned current status
```

`extended-combined-evaluation.md`、`yakuhai-call-evaluation.md`等の既存candidate-specific
文書は、その時点のconsumer wiringとevaluation planを示すhistorical documentationとして残す。
そこに記録された当時のbaseline、seed range、checkpoint、Issueへの記録方法を、本書の恒久ルールへ
自動的に昇格させない。新しいcandidateごとに恒久文書を増やすことも標準運用としない。

## Relationship to lisjong-project #34 and lisjong #121

本書の追加は[`lisbun/lisjong-project #34`](https://github.com/lisbun/lisjong-project/issues/34)
のchild workであり、[`lisbun/lisjong #121`](https://github.com/lisbun/lisjong/issues/121)
のlong-lived tracking role終了に向けたownership migrationの一部である。

#121に蓄積された内容のうち、Arena-ownedで長期的に再利用するevaluation principlesだけを
本書へ整理する。current baseline、historical run result、individual promotion decision、current
roadmap、future Policy workstream候補は移行しない。historical evidenceは#121と関連するclosed
Issue / PRに残し、current stateと今後のbounded workをそれぞれのownerへ分離する。
