# Documentation map

`lisjong-arena/docs` には、**現在のcontract / operator向け文書**と、**過去のbounded experiment記録**が共存する。両者は意図的にライフサイクルが異なる。

repository READMEは入口に留め、本ページで「いま正本として読む文書」と「historical evidenceとして読む文書」を分ける。

## 1. Current repository contracts

以下は現在のArena責務・再利用可能なcontractを表し、実装変更に合わせて同期する。

| Document | Role |
| --- | --- |
| [Architecture](architecture.md) | Arena responsibility / ownership / promotion boundary |
| [Roadmap](roadmap.md) | 長期的なArena capability。current Issue trackerではない |
| [Policy strength evaluation policy](policy-strength-evaluation.md) | Policy比較の恒久的な評価規律 / measurement source-of-truth |
| [Learned Policy input schema](learned-policy-input-schema.md) | current experiment-local player-safe input / tensor contract |
| [Offense Foundation O0 prerequisites](offense-foundation.md) | #331 P0/P1 qualification and canonical corpus generator; #332 execution handoff |
| [AWS Offense Foundation execution](aws-offense-foundation-332.md) | #332 two-phase single-instance parallel AWS launcher, retention, gate, and reattachment contract |
| [AWS operational calibration / launch admission](aws-operational-calibration.md) | #340 bounded calibration execution, calibration evidence, matching/freshness policy, runtime and cost prediction, and the phase 0 / Phase 1 / Phase 2 Go/No-Go gates |
| [Durable local game record](durable-local-game-record.md) | RiichiEnv local record schema / writer / loader / integrity / limitation |
| [Durable ranked game record](durable-ranked-game-record.md) | RiichiLab ranked completed-hanchan raw record schema / loader / integrity / limitation |
| [Automated Strength Evaluation](automated-strength-evaluation.md) | machine-readable locked evaluation orchestration contract |
| [Claude Code workflow](claude-code-workflow.md) | repository development workflow |
| [Testing policy](testing.md) | current test placement, CI coverage, and consolidation gate |

ここへ載ることは、Arenaがstable AI semanticsのownerになることを意味しない。`PolicyInput`、Policy behavior、HandBelief、value / risk、production Policy等のstable AI-side semanticsは、該当する場合 `lisjong` がownerである。

## 2. Current execution / acquisition operator docs

### RiichiLab runtime

- [RiichiLab client runtime contract](riichilab-client.md)
- [RiichiLab development profile](riichilab-dev-profile.md)
- [RiichiLab protocol bridge](riichilab-protocol-bridge.md)
- [RiichiLab durable ranked game record](durable-ranked-game-record.md)
- [AWS execution observability](aws-execution-observability.md)
- [AWS operational calibration / launch admission](aws-operational-calibration.md)

### RiichiLab research corpus

- [RiichiLab self-history acquisition](riichilab-self-history.md)
- [RiichiLab longitudinal diagnostic](riichilab-longitudinal.md)
- [RiichiLab bounded server-log corpus](riichilab-corpus.md)
- [RiichiLab downstream reconstruction qualification](riichilab-downstream-qualification.md)
- [RiichiLab source pilot](riichilab-source-pilot.md)

### Bounded evaluation contracts pending execution

- [Progression development evaluation](progression-development-evaluation.md) — Issue #252のfeasibility gate + paired passive-x3 development screen（real executionはmerge後のoperator作業）
- [Overall Champion AABB half-game formal protocol v1](overall-champion-aabb-half.md) — Issue #250のcross-family Overall determination protocol / lock / bundle verification（actual 400-hanchan formal runはmerge後のoperator作業）
- [Heuristic candidate AABB half-game protocol v1](heuristic-candidate-aabb-half.md) — Issue #375のfamily-internal candidate評価（uma/oka final score primary）/ AWS executor（actual 400-hanchan runはmerge後のoperator作業）
- [Stage A0 non-riichi Tenpai label path feasibility](stage-a0-tenpai-label-feasibility.md) — Issue #258のlabel path / corpus qualification（retained corpusに対する実測はmerge後のoperator作業）

これらはconcreteなexecution / acquisition surfaceのcontractを記録する。**現在どの研究を優先するか**はactive GitHub Issueを正本とし、reusable contract自体が変わらない限りoperator文書へcurrent priorityを転記しない。

## 3. Historical bounded research evidence

`docs/` 内にはexperiment-specificな記録が多数ある。negative / inconclusive / supersededな結果も再利用価値があるため削除しないが、元文書の `Status`、candidate choice、threshold、`Next step` を**現在のproject priorityとして読まない**。

代表的なLearned Policy record:

- `learned-policy-stage2.md`
- `learned-policy-stage3.md`
- `learned-policy-stage4a.md`
- `learned-policy-offline-q.md`
- `learned-policy-offline-q-diagnosis.md`
- `learned-policy-data-sufficiency-preflight.md`
- `learned-policy-p1-gate-a.md`
- `learned-policy-p1-gate-b.md`
- `learned-policy-p1-shanten-guard.md`
- `learned-policy-p1-guarded-higher-fidelity.md`
- `learned-policy-p1-guarded-higher-fidelity-successor.md`
- `learned-policy-p6-higher-fidelity.md`
- `learned-policy-finite-horizon-curriculum.md`

代表的なHandBelief record:

- `phase10-scale-learning-curve.md`
- `epoch-budget-adequacy.md`
- `optimization-budget-saturation.md`
- `phase11-public-riichi-wait-readout.md`

[September 2026 test-suite audit](test-suite-audit.md) は Issue #236 第1段階の revision 固定 inventory、historical invariant、runtime 記録である。今後の testing policy は上記の `testing.md` を正本とする。

`extended-combined-evaluation.md`、`yakuhai-call-evaluation.md` 等のPolicy-specific comparison reportも、active Issueがcurrent statusへ昇格させない限りhistorical evidenceとして扱う。

### Reading rule

```text
historical experiment document
    = その実験で何をlockし、何を測り、何を結論したか

active GitHub Issue
    = いま何を進めているか

current reusable contract document
    = 現在どのcode / operator behaviorがsupportedか
```

roadmapが進むたびにhistorical experiment documentを書き換えない。元runについて事実として正しいならそのまま保持し、active parent Issueまたはcurrent contract documentを更新する。

## 4. Current project statusは重複管理しない

Learned Policy / HandBeliefのcurrent workはGitHub Issues / PRsを正本とする。これらはdurable documentationより速く変わるため、docsを第二のproject trackerにしない。

project-wide architectureとcurrent research axisは [`lisjong-project`](https://github.com/lisbun/lisjong-project) で調整する。

## 5. 新しい文書を追加する基準

次のいずれかを満たす場合にdurable documentを追加する。

- reusableなoperator / artifact contractを定義する
- bounded experimentのexact protocol / resultをIssue close後も参照可能にする必要がある
- 複数変更で使うArena-local architecture boundaryを定義する

次だけならIssue / PR commentを優先する。

- current priority
- 一時的なnext step
- one-off implementation checklist
- active Issueですでに表現されているstatus

この分離により、historical evidenceを失わずにREADME / architectureがchronological research diary化するのを防ぐ。
