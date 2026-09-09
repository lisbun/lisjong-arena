# lisjong-arena

Reproducible execution, observation, research experimentation, and policy evaluation arena for the lisjong ecosystem.

> [!IMPORTANT]
> lisjong-arena is part of an independent personal Japanese mahjong AI project
> developed by [lisbun](https://github.com/lisbun). It is not affiliated with any
> other project using the LisJong or lisjong name.

## 概要

`lisjong-arena` は、lisjongのPolicy / agentをconcrete environmentで実行・観測し、
controlled / reproducibleな条件で研究用candidateを生成・診断し、Policy間の
performance differenceやgame performanceを比較・検証するrepositoryです。

Arena内部では、少なくとも次の3責務を分離します。

```text
Execution / Observation
    what happened
        |
        v
objective execution data
        |
        +------------------------------+
        |                              |
        v                              v
Experiment-local Research          Evaluation
bounded dataset / training        matchup / seeds / rotation
analysis / model artifact         metrics / artifact / provenance
        |                              ^
        v                              |
research candidate -------------------+
```

重要なのは、**Arenaがexperiment-local MLを実装できることと、stableなAI semanticsを
Arenaが所有することは別**だという点です。

lisjong ecosystem全体のrepository責務、repository間依存方向、長期ロードマップは
[`lisjong-project`](https://github.com/lisbun/lisjong-project) を正本とします。

Arena固有の詳細な責務・ownership decisionは
[Architecture](docs/architecture.md)、長期的な発展方針は
[Roadmap](docs/roadmap.md)、Policy strength comparisonの恒久的な評価規律は
[Policy strength evaluation policy](docs/policy-strength-evaluation.md)を参照してください。

## 責務

### Execution / observation

Arenaがtarget responsibilityとして所有するもの:

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

Execution / observationは研究仮説やcomparison semanticsを知りません。

### Experiment-local research / ML

Arenaは、**bounded research questionを検証するために必要なexperiment-localな
ML / analysis implementation**を所有できます。

例:

- purpose-specific player-safe feature / tensor representation
- experiment-local dataset schema / split / manifest
- data generation / materialization harness
- bounded training harness
- fixed experiment model architecture / loss / optimizer configuration
- checkpoint / result / diagnostic artifact
- offline analysis / failure diagnosis
- experiment-local learned Policy adapter
- current experimentだけで使うclassification / decision rule

このownershipは、次の条件を満たす場合に限定します。

```text
bounded research question
    + explicit provenance / reproducibility
    + purpose-specific schema
    + clear promotion boundary
    + no silent production adoption
```

Arena内にmodel classやtraining codeが存在しても、それだけで次を意味しません。

```text
experiment-local model
!= canonical lisjong model architecture

experiment-local feature schema
!= canonical PolicyInput / production feature contract

experiment-local checkpoint
!= production Policy

experiment result
!= stable public API
```

研究結果がstableなAI-side semanticsやproduction contractへ昇格する場合は、
その時点でowner repositoryを明示的に再評価します。`lisjong`が所有すべきstable
Policy / inference semanticsを、experiment codeがArenaにあるという理由だけで
Arena canonical contractへ固定しません。

逆に、research harnessをすべて`lisjong`へ置くことも要求しません。dataset生成、
training、artifact、diagnostic、evaluationがArenaのcontrolled execution / evidence
pipelineと強く結び付くbounded experimentでは、Arena-local implementationの方が
責務を明確に保てます。

### Evaluation

Arenaが所有するもの:

- Policy / agentのmatchup定義
- fixed seed set
- deterministicなseat rotation
- multiple-game / round execution plan
- Policy / agent assignmentの記録
- raw evaluation / comparison result
- strength / diagnostic metrics
- 再現可能なcomparison protocol
- versioned immutable evaluation artifact
- compatible artifactのstrict readback / reaggregation
- external benchmark / external competitor orchestration

Evaluationはcandidateの生成方法を所有しません。research harnessが生成したcandidateを
consumerとして評価できますが、評価結果を見てtraining conditionを暗黙に変更しません。

### `lisjong` に残すもの

Arenaがcanonical ownerにならないもの:

- Policy / AI strategy
- `DecisionContext` / `PolicyInput` / `InternalAction` 等のstable AI-side semantic contract
- AI-side Action identity / legality semantics
- shanten / ukeire / HandBelief / risk / value / utility等のstable domain semantics
- candidate evaluation / selection reason等のstable Policy-internal analysis semantics
- production / public Learned Policy semantics
- canonical production model / feature contract
- AI-side public APIのpromotion decision
- 麻雀ルール / game state transition
- generic external-player runtime / generic process host

境界を短く言うと、次のようになります。

```text
Arena Execution / Observation
    = what happened

Arena Experiment-local Research
    = how a bounded experiment materializes / trains / diagnoses evidence

Arena Evaluation
    = how candidates are compared reproducibly

lisjong
    = what stable AI decisions / features / beliefs / values mean
```

## Experiment-local MLのpromotion boundary

研究用implementationは、最初からproduction-quality generic frameworkへ昇格させません。

```text
bounded experiment
    |
    v
experiment-local implementation
    |
    v
result / evidence
    |
    +--> negative / inconclusive
    |       keep as historical experiment record
    |
    `--> repeatedly useful / promoted principle
            |
            v
       owner repository review
            |
            +--> remain Arena experiment infrastructure
            `--> move / formalize as lisjong stable AI contract
```

promotion時には少なくとも次を確認します。

- semanticsが特定experimentを超えてstableか
- production / multiple consumerで必要か
- Arena evaluation concernとAI decision concernのどちらがownerとして自然か
- current artifact / feature identityをそのままstable contractへ流用してよいか
- breaking-change policy / versioning / compatibilityを新たに定義すべきか

「研究で使えた」だけではpromotion理由にしません。

## Current research examples

Arenaには現在、experiment-local ML / analysisの具体例があります。

### Learned Policy input / training

- [Learned Policy input schema](docs/learned-policy-input-schema.md)
- [Learned Policy Stage 2](docs/learned-policy-stage2.md)
- [Learned Policy Stage 3](docs/learned-policy-stage3.md)
- [Learned Policy Stage 4A](docs/learned-policy-stage4a.md)
- [Offline Q experiment](docs/learned-policy-offline-q.md)
- [Offline Q failure diagnosis](docs/learned-policy-offline-q-diagnosis.md)
- [Learned Policy data-sufficiency preflight](docs/learned-policy-data-sufficiency-preflight.md)
- [P1 Gate A](docs/learned-policy-p1-gate-a.md)
- [P1 Gate B](docs/learned-policy-p1-gate-b.md)
- [FiniteHorizon-teacher curriculum](docs/learned-policy-finite-horizon-curriculum.md)
- [Shanten-constrained Q serving diagnostic](docs/learned-policy-p1-shanten-guard.md)
- [Guarded P1 higher-fidelity screen](docs/learned-policy-p1-guarded-higher-fidelity.md)
- [P6 higher-fidelity screen](docs/learned-policy-p6-higher-fidelity.md)

これらは、PolicyInputをplayer-safe inputとして利用しつつ、dataset / tensor / model /
training / checkpoint / analysisをpurpose-specificなexperiment contractとして扱います。

### HandBelief research

HandBelief関連では、Arenaがtraining corpus、bounded learner、artifact、holdout protocol、
scale study等のexperiment harnessを所有できます。一方、HandBeliefというAI-side概念の
stable semanticsや将来production consumer semanticsは`lisjong`側の責務です。

代表的なexperiment record:

- [Phase 10 scale learning curve](docs/phase10-scale-learning-curve.md)
- [Epoch-budget adequacy study](docs/epoch-budget-adequacy.md)
- [Optimization-budget saturation study](docs/optimization-budget-saturation.md)
- [Phase 11 public-riichi wait readout](docs/phase11-public-riichi-wait-readout.md)

component prediction qualityとPolicy decision value / game strengthは別claimとして扱います。

## Current execution paths

Arenaは複数のconcrete execution pathを持ちますが、早期にgeneric backend abstractionへ
統合しません。

### RiichiEnv

Policy-vs-Policy development evaluationでは、Arena-localの
`lisjong_arena.riichienv.local_game_runner.LocalGameRunner`とArena-local RiichiEnv Adapter /
GameTraceを利用します。

```text
Arena evaluation / experiment
        |
        v
LocalGameRunner
        |
        v
RiichiEnv
        |
        v
lisjong Policy contract
```

### RiichiLab

ranked / validation / continuous participationのclient、session、transport、protocol bridge、
profile / credential compositionはArena execution / observationが所有します。

主なentry point:

```powershell
python -m lisjong_arena.riichilab.ranked --profile lisjong-dev
python -m lisjong_arena.riichilab.validation --profile lisjong-dev
python -m lisjong_arena.riichilab.continuous_ranked --profile lisjong-dev
```

詳細は[RiichiLab client runtime contract](docs/riichilab-client.md)と
[RiichiLab protocol bridge](docs/riichilab-protocol-bridge.md)を参照してください。

### First-party `lisjong-engine`

Arena-owned bridgeを介してfirst-party `lisjong-engine`上でもlisjong Policyを実行できます。
engineのgame progressionやrule semanticsをArenaへ複製しません。

```text
lisjong-arena
   |---> lisjong Policy
   `---> lisjong-engine execution
```

## Policy evaluation

### AABB / ABBB

Arenaは、controlled / reproducibleなPolicy comparisonのためにAABBとABBBのprotocolを
提供します。seed、seat rotation、Policy lifecycle、artifact / provenanceをexplicitに扱い、
partial successをsuccessful evaluationとして返しません。

開発用single-round比較:

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate hand-value-aware `
  --baseline two-step `
  --seeds 0:99 `
  --workers 4 `
  --progress
```

first-party research Policyは、installed environmentからimport可能なら
`package.module:attribute`形式のexplicit import referenceでも指定できます。

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate lisjong.policies.some_new_policy:SomeNewPolicy `
  --candidate-id some-new-policy-experiment `
  --baseline yakuhai-call `
  --seeds 0:99
```

explicit importできることはcurated catalogへのpromotionを意味しません。

Policy strength comparisonの恒久的な規律は
[Policy strength evaluation policy](docs/policy-strength-evaluation.md)を正本とします。

既存ABBB primitiveを1つのmachine-readable / non-interactive locked runとして
compositionする場合は、[Automated Strength Evaluation v0](docs/automated-strength-evaluation.md)
を利用できます。これは1回のevaluation orchestrationであり、candidate生成、seed allocation、
Champion promotion、retry / resumeを行うStrength Loopではありません。

### External competitor

Mortal等のexternal competitorはArena evaluationがorchestrateできます。
external model / image / protocol semanticsをlisjong Policy contractへ取り込みません。

## Artifact discipline

Arenaのexperiment / evaluation artifactは、目的ごとにversioned / immutable / fail-closedな
contractを持たせます。

原則:

- raw measurement / corpusをsource of truthにする
- derived summaryは再計算可能にする
- unknown schema / protocolを推測して受理しない
- exact provenanceを確認できない場合は捏造しない
- resultを見てseed / threshold / rescue runを暗黙追加しない
- test fixture以外のlarge artifactをrepositoryへ常設commitしない
- secret / credential / machine-local identityをartifactへ入れない

artifactを保存できることと、repository-managed artifact platformを持つことは別です。

## 開発環境

初期基準は通常版CPython 3.14です。free-threaded build（3.14t）は、依存libraryを含む
互換性を個別に検証するまで対象外とします。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Windows (PowerShell):

```powershell
.venv\Scripts\Activate.ps1
```

ML experimentを実行する場合は、そのexperiment documentationが指定するextra / exact
provenance requirementを優先してください。formal / locked experimentではeditable local
installを拒否する場合があります。

### 品質確認

```text
python -m ruff format --check .
python -m ruff check .
python -m unittest discover -s tests -v
```

文書のみの変更では最低限 `git diff --check` を確認します。

## Detailed documentation

READMEはcurrent ownershipと主要entry pointのoverviewに留めます。詳細なhistorical protocol、
runbook、schema、result interpretationは各purpose-specific documentを正本とします。

- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- [Policy strength evaluation policy](docs/policy-strength-evaluation.md)
- [Automated Strength Evaluation v0](docs/automated-strength-evaluation.md)
- [RiichiEnv compatibility](docs/riichienv-compatibility.md)
- [RiichiLab client](docs/riichilab-client.md)
- [RiichiLab protocol bridge](docs/riichilab-protocol-bridge.md)
- [Learned Policy input schema](docs/learned-policy-input-schema.md)
- [Learned Policy Stage 2](docs/learned-policy-stage2.md)
- [Learned Policy Stage 3](docs/learned-policy-stage3.md)
- [Learned Policy Stage 4A](docs/learned-policy-stage4a.md)
- [Offline Q experiment](docs/learned-policy-offline-q.md)
- [Offline Q failure diagnosis](docs/learned-policy-offline-q-diagnosis.md)
- [Learned Policy data-sufficiency preflight](docs/learned-policy-data-sufficiency-preflight.md)
- [P1 Gate A](docs/learned-policy-p1-gate-a.md)
- [P1 Gate B](docs/learned-policy-p1-gate-b.md)
- [FiniteHorizon-teacher curriculum](docs/learned-policy-finite-horizon-curriculum.md)
- [Shanten-constrained Q serving diagnostic](docs/learned-policy-p1-shanten-guard.md)
- [Guarded P1 higher-fidelity screen](docs/learned-policy-p1-guarded-higher-fidelity.md)
- [P6 higher-fidelity screen](docs/learned-policy-p6-higher-fidelity.md)
- [Phase 10 scale learning curve](docs/phase10-scale-learning-curve.md)
- [Epoch-budget adequacy study](docs/epoch-budget-adequacy.md)
- [Optimization-budget saturation study](docs/optimization-budget-saturation.md)

## 現時点で持たないもの

- generic ML platform / model registry / HPO service
- canonical production Learned Policy architecture owned by Arena
- project-wide canonical GameRecord / DecisionTrace schema
- database / dashboard / artifact repository
- distributed multi-machine job scheduler
- automatic production promotion
- generic external-player runtime / process host
- speculative generic backend abstraction

必要性はconcrete consumerとmeasured bottleneckから判断します。

## License

[MIT License](LICENSE)
