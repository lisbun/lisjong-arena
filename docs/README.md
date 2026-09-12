# Documentation map

`lisjong-arena/docs` contains both **current contracts/operator documentation** and **historical bounded experiment records**. They intentionally have different lifecycle semantics.

The repository README should stay compact. Use this page to decide which document is authoritative for a question.

## 1. Current repository contracts

These documents describe current Arena responsibility or reusable behavior and should be kept synchronized with implementation changes.

| Document | Role |
| --- | --- |
| [Architecture](architecture.md) | Arena responsibility, ownership, and promotion boundaries |
| [Roadmap](roadmap.md) | long-term Arena capability direction; not current Issue tracking |
| [Policy strength evaluation policy](policy-strength-evaluation.md) | durable evaluation discipline / measurement source-of-truth rules |
| [Learned Policy input schema](learned-policy-input-schema.md) | current experiment-local player-safe input/tensor contract where still referenced |
| [Durable local game record](durable-local-game-record.md) | RiichiEnv local-record schema, writer/loader, integrity and limitations |
| [Automated Strength Evaluation](automated-strength-evaluation.md) | machine-readable locked evaluation orchestration contract |
| [Claude Code workflow](claude-code-workflow.md) | repository development workflow guidance |

A document being listed here does **not** make Arena the owner of stable AI semantics. Stable `PolicyInput`, Policy behavior, HandBelief semantics, value/risk semantics, and production Policy contracts remain owned by `lisjong` where applicable.

## 2. Current execution / acquisition operator docs

### RiichiLab runtime

- [RiichiLab client runtime contract](riichilab-client.md)
- [RiichiLab protocol bridge](riichilab-protocol-bridge.md)

### RiichiLab research corpus

- [RiichiLab bounded server-log corpus](riichilab-corpus.md)
- [RiichiLab downstream reconstruction qualification](riichilab-downstream-qualification.md)

These documents describe concrete Arena execution/acquisition surfaces. Current research choice and experiment state belong in the active GitHub Issue, not in these documents unless the reusable contract itself changes.

## 3. Historical bounded research evidence

Many files under `docs/` are **experiment records**, not current project state. Preserve them because negative, inconclusive, and superseded results remain useful evidence, but do not read their original `Status`, candidate choice, threshold, or next step as today's project priority.

Representative Learned Policy records include:

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

Representative HandBelief records include:

- `phase10-scale-learning-curve.md`
- `epoch-budget-adequacy.md`
- `optimization-budget-saturation.md`
- `phase11-public-riichi-wait-readout.md`

Policy-specific comparison reports such as `extended-combined-evaluation.md` and `yakuhai-call-evaluation.md` are likewise historical evidence unless an active Issue explicitly promotes their conclusion into current status.

### Reading rule

```text
historical experiment document
    = what was locked / measured / concluded in that experiment

active GitHub Issue
    = what is currently being worked on

current reusable contract document
    = what code / operator behavior is supported now
```

Do not rewrite historical experiment documents every time the roadmap advances. If an old experiment document is factually correct for its original run, leave it intact and update the active parent Issue or current contract document instead.

## 4. Current project status is not duplicated here

For current work, use GitHub Issues / PRs. In particular, long-running Learned Policy and HandBelief research evolves faster than durable documentation.

Project-wide architecture and current research axes are coordinated in [`lisjong-project`](https://github.com/lisbun/lisjong-project). Arena documentation should not become a second project tracker.

## 5. When to add a new document

Add a durable document when at least one is true:

- it defines a reusable operator or artifact contract
- it records a bounded experiment whose exact protocol/result must remain inspectable after the Issue closes
- it defines an Arena-local architecture boundary used by multiple changes

Prefer an Issue/PR comment instead when the content is only:

- current priority
- temporary next step
- one-off implementation checklist
- status already represented by an active Issue

This separation keeps historical evidence available without making the repository README or architecture document a chronological research diary.
