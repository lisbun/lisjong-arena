# lisjong-arena

lisjong ecosystem向けの、reproducibleなexecution / observation / research experimentation / Policy evaluation arenaです。

> [!IMPORTANT]
> `lisjong-arena` は [lisbun](https://github.com/lisbun) が開発する独立した個人の日本式麻雀AI projectの一部です。他のLisJong / lisjong名称のprojectとは関係ありません。

## 概要

Arenaは次の3責務を分離します。

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

重要な境界は次です。

```text
experiment-local model / feature / checkpoint
!= stable lisjong Policy semantics
!= production Policy
```

project-wideなrepository responsibility / long-term directionは [`lisjong-project`](https://github.com/lisbun/lisjong-project) を正本とします。

Arena内の文書は次から辿ってください。

- [Documentation map](docs/README.md) — current contractとhistorical experiment recordの区別
- [Architecture](docs/architecture.md) — responsibility / ownership
- [Roadmap](docs/roadmap.md) — 長期的なArena capability
- [Policy strength evaluation policy](docs/policy-strength-evaluation.md) — Policy比較の恒久的な評価規律

**current work / next actionはGitHub Issues / PRsを正本**とし、READMEへ重複転記しません。

## Responsibility summary

### Execution / observation

Arenaはconcrete environment integrationを所有します。

- local / external runner / client
- session lifecycle、retry / reconnect、continuous participation
- execution profile / credential-source resolution
- protocol trace / raw game record acquisition
- objective applied-event observation
- external representationと`lisjong` Policy contract間のprojection
- external legal Action mapping / revalidation

Execution / observationはresearch hypothesisやPolicy comparison conclusionを所有しません。

### Experiment-local research / ML

bounded research questionに必要なpurpose-specific implementationはArenaで所有できます。

- dataset / tensor / split / manifest
- training harness
- fixed experiment model / loss / optimizer
- checkpoint / result / diagnostic artifact
- offline failure diagnosis
- experiment-local Learned Policy adapter

研究で使えたことだけを理由にcanonical model / feature schema / public API / production Policyへ昇格させません。promotionはownerを含めて明示的に判断します。

### Evaluation

Arenaはreproducibleなcomparison mechanicsを所有します。

- matchup definition
- fixed seeds / deterministic seat rotation
- game / round execution plan
- immutable result artifact / provenance
- strict readback / reaggregation
- diagnostic / strength metrics
- external competitor orchestration

Evaluationはcandidateをconsumerとして扱い、結果を見てcandidate生成条件を暗黙に変更しません。

### Arenaがownerにならないもの

stable AI-side semanticsは該当する場合`lisjong`側に残します。例:

- Policy behavior
- `DecisionContext` / `PolicyInput` / `InternalAction`
- shanten / ukeire / HandBelief / value / risk semantics
- production Learned Policy contract

麻雀ルールやgame state transitionもArenaへ複製しません。

## Execution paths

### RiichiEnv local execution

Policy-vs-Policy development evaluationではArenaの`LocalGameRunner`とRiichiEnv adapter / `GameTrace`を利用します。

```text
Arena evaluation / experiment
        v
LocalGameRunner
        v
RiichiEnv
        v
lisjong Policy contract
```

正常終了したstandard local gameは、opt-inでArena-ownedの [durable local game record](docs/durable-local-game-record.md) として保存できます。recordはstrict / versioned / cross-process readableで、schema v2ではRiichiEnvからcapture時点で取得できるauthoritative per-round result factも保持します。

これはtraining datasetでもproject-wide canonical `GameRecord`でもありません。

```bash
python -m lisjong_arena.durable_local_game_record_cli record \
  --seed 12345 \
  --game-mode 4p-red-single \
  --policy 0=two-step --policy 1=two-step \
  --policy 2=two-step --policy 3=two-step \
  --output /durable/path/game-12345
```

### RiichiLab

ranked / validation / continuous participation runtime integrationはArenaが所有します。

```powershell
python -m lisjong_arena.riichilab.ranked --profile lisjong-dev
python -m lisjong_arena.riichilab.validation --profile lisjong-dev
python -m lisjong_arena.riichilab.continuous_ranked --profile lisjong-dev
```

詳細:

- [RiichiLab client runtime contract](docs/riichilab-client.md)
- [RiichiLab protocol bridge](docs/riichilab-protocol-bridge.md)
- [RiichiLab bounded server-log corpus](docs/riichilab-corpus.md)
- [RiichiLab downstream reconstruction qualification](docs/riichilab-downstream-qualification.md)

runtime participation、corpus acquisition、downstream ML qualificationは別責務です。technical accessibilityだけからML利用・redistribution permissionを推定しません。

### First-party `lisjong-engine`

Arena-owned bridgeを介してfirst-party `lisjong-engine`上でも`lisjong` Policyを実行できます。engine rule semanticsをArenaへ複製しません。

```text
lisjong-arena
   |---> lisjong Policy
   `---> lisjong-engine execution
```

## Policy evaluation

ArenaはAABB / ABBB comparisonを、explicitなseed / seat rotation / Policy lifecycle / artifact identity / provenanceで実行します。

single-round comparison例:

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate hand-value-aware `
  --baseline two-step `
  --seeds 0:99 `
  --workers 4 `
  --progress
```

installed environmentからimport可能なfirst-party research Policyはexplicit import referenceでも指定できます。

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate lisjong.policies.some_new_policy:SomeNewPolicy `
  --candidate-id some-new-policy-experiment `
  --baseline yakuhai-call `
  --seeds 0:99
```

import可能であることはcurated catalog promotionを意味しません。

恒久的なcomparison ruleは [Policy strength evaluation policy](docs/policy-strength-evaluation.md) を正本とします。[Automated Strength Evaluation](docs/automated-strength-evaluation.md) は既存comparisonを1つのmachine-readable locked runとしてcompositionする仕組みであり、自律的なcandidate生成・Champion promotion loopではありません。

Mortal等のexternal competitorもArena evaluationがorchestrateできますが、そのmodel / protocol semanticsをstable `lisjong` Policy contractへ取り込みません。

## Research documentation lifecycle

Arenaにはnegative / inconclusive / superseded resultも含むexperiment-specific文書が多数あります。これらはevidenceとして保持しますが、**すべてがcurrent-status文書ではありません**。

[docs/README.md](docs/README.md) で次を区別します。

```text
current reusable contract
historical bounded experiment record
operator documentation
```

古いexperiment文書の元の`Status`や`Next step`から現在のpriorityを推測しないでください。current research choiceはactive GitHub Issueを正本とします。

## Artifact discipline

Arena artifactはpurpose-specific / versionedで、必要なprotocolではimmutable / fail-closedに扱います。

原則:

- raw measurement / corpusをsource of truthとする
- derived summaryは再計算可能にする
- unknown schema / protocolを推測してcompatible扱いしない
- unresolved provenanceをresolvedとして報告しない
- result exposure後にseed / threshold / rescue runを暗黙追加しない
- large generated artifactをrepositoryへ常設commitしない
- secret / credential / machine-local identityをresearch artifactへ入れない

artifactを保存できることとgeneric artifact registry / cloud platformを持つことは別です。

## 開発環境

通常版CPython 3.14を初期基準とします。free-threaded 3.14tは依存libraryを含む互換性を個別検証するまで対象外です。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

ML experimentでは各experiment documentationが指定するextra / exact provenance requirementを優先します。formal / locked experimentではeditable local installを拒否する場合があります。

### 品質確認

```text
python -m ruff format --check .
python -m ruff check .
python -m unittest discover -s tests -v
```

文書のみの変更では最低限 `git diff --check` を確認します。

repository-specificなdevelopment / review guidanceは`AGENTS.md`と [Claude Code workflow](docs/claude-code-workflow.md) を参照してください。

## Documentation rule of thumb

```text
current implementation / reusable contract
    -> README / architecture / purpose-specific current doc

current work / next action
    -> GitHub Issue / PR

bounded experiment protocol + result
    -> experiment record + Issue / artifact

project-wide ownership / roadmap
    -> lisjong-project
```

READMEを入口に留め、chronological research ledger化させないことを基本方針とします。

## License

[MIT License](LICENSE)
