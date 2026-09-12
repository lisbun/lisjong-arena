# lisjong-arena

Reproducible execution, observation, research experimentation, and Policy evaluation arena for the lisjong ecosystem.

> [!IMPORTANT]
> `lisjong-arena` is part of an independent personal Japanese mahjong AI project developed by [lisbun](https://github.com/lisbun). It is not affiliated with other projects using the LisJong or lisjong name.

## Purpose

Arena separates three concerns:

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

The key boundary is:

```text
experiment-local model / feature / checkpoint
!= stable lisjong Policy semantics
!= production Policy
```

Project-wide repository responsibility and long-term capability direction are owned by [`lisjong-project`](https://github.com/lisbun/lisjong-project).

For Arena details, use:

- [Documentation map](docs/README.md) — current contracts vs historical experiment records
- [Architecture](docs/architecture.md) — responsibility and ownership
- [Roadmap](docs/roadmap.md) — long-term Arena capability direction
- [Policy strength evaluation policy](docs/policy-strength-evaluation.md) — reusable evaluation discipline

Current work and experiment status live in GitHub Issues / PRs rather than being duplicated in this README.

## Responsibility summary

### Execution / observation

Arena owns concrete environment integration such as:

- local / external runners and clients
- session lifecycle, retry, reconnect, continuous participation
- execution profile / credential-source resolution
- protocol traces and raw game-record acquisition
- objective applied-event observation
- projection between external representations and `lisjong` Policy contracts
- external legal-action mapping / revalidation

Execution / observation does not own research hypotheses or Policy-comparison conclusions.

### Experiment-local research / ML

Arena may own bounded, purpose-specific research implementation:

- dataset / tensor / split / manifest
- training harness
- fixed experiment model / loss / optimizer
- checkpoint / result / diagnostic artifact
- offline failure diagnosis
- experiment-local learned-Policy adapter

Promotion is explicit. A useful experiment implementation is not automatically a canonical model, feature schema, public API, or production Policy.

### Evaluation

Arena owns reproducible comparison mechanics:

- matchup definitions
- fixed seeds and deterministic seat rotation
- game / round execution plans
- immutable result artifacts and provenance
- strict readback / reaggregation
- diagnostic and strength metrics
- external competitor orchestration

Evaluation consumes candidates; it does not silently retune candidate generation after seeing results.

### What remains outside Arena

Stable AI-side semantics stay with `lisjong`, including Policy behavior, `DecisionContext`, `PolicyInput`, `InternalAction`, shanten / ukeire / HandBelief / value / risk semantics, and production Learned Policy contracts.

Mahjong game rules and state transitions are not reimplemented in Arena.

## Execution paths

### RiichiEnv local execution

Policy-vs-Policy development evaluation uses Arena's `LocalGameRunner` and RiichiEnv adapter / `GameTrace`:

```text
Arena evaluation / experiment
        v
LocalGameRunner
        v
RiichiEnv
        v
lisjong Policy contract
```

Successful standard local games can optionally be persisted as an Arena-owned [durable local game record](docs/durable-local-game-record.md). The record is strict, versioned, and cross-process readable; schema v2 also stores authoritative per-round result facts that RiichiEnv exposes at capture time.

It is **not** a training dataset or project-wide canonical `GameRecord`.

```bash
python -m lisjong_arena.durable_local_game_record_cli record \
  --seed 12345 \
  --game-mode 4p-red-single \
  --policy 0=two-step --policy 1=two-step \
  --policy 2=two-step --policy 3=two-step \
  --output /durable/path/game-12345
```

### RiichiLab

Arena owns ranked / validation / continuous-participation runtime integration.

```powershell
python -m lisjong_arena.riichilab.ranked --profile lisjong-dev
python -m lisjong_arena.riichilab.validation --profile lisjong-dev
python -m lisjong_arena.riichilab.continuous_ranked --profile lisjong-dev
```

See:

- [RiichiLab client runtime contract](docs/riichilab-client.md)
- [RiichiLab protocol bridge](docs/riichilab-protocol-bridge.md)
- [RiichiLab bounded server-log corpus](docs/riichilab-corpus.md)
- [RiichiLab downstream reconstruction qualification](docs/riichilab-downstream-qualification.md)

Corpus acquisition, runtime participation, and downstream ML qualification are separate concerns. Technical accessibility does not by itself establish ML-use or redistribution permission.

### First-party `lisjong-engine`

Arena can execute `lisjong` Policies through an Arena-owned bridge over first-party `lisjong-engine` without copying engine rule semantics into Arena.

```text
lisjong-arena
   |---> lisjong Policy
   `---> lisjong-engine execution
```

## Policy evaluation

Arena supports controlled AABB / ABBB comparisons with explicit seeds, seat rotation, Policy lifecycle, artifact identity, and provenance.

Example single-round comparison:

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate hand-value-aware `
  --baseline two-step `
  --seeds 0:99 `
  --workers 4 `
  --progress
```

A first-party research Policy may also be specified by explicit import reference when installed:

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate lisjong.policies.some_new_policy:SomeNewPolicy `
  --candidate-id some-new-policy-experiment `
  --baseline yakuhai-call `
  --seeds 0:99
```

Importability does not imply curated-catalog promotion.

Use [Policy strength evaluation policy](docs/policy-strength-evaluation.md) for durable comparison rules. [Automated Strength Evaluation](docs/automated-strength-evaluation.md) composes an existing comparison into one machine-readable locked run; it is not an autonomous candidate-generation or Champion-promotion loop.

External competitors such as Mortal may be orchestrated by Arena evaluation, but their model / protocol semantics are not absorbed into the stable `lisjong` Policy contract.

## Research documentation lifecycle

Arena has many experiment-specific documents because negative, inconclusive, and superseded results are useful evidence. Those files are intentionally preserved, but they are **not all current-status documents**.

Use [docs/README.md](docs/README.md) to distinguish:

```text
current reusable contract
historical bounded experiment record
operator documentation
```

Do not infer today's priority from an old experiment document's original `Status` or `Next step`. Current research choice belongs to active GitHub Issues.

## Artifact discipline

Arena artifacts are purpose-specific, versioned, immutable where required, and fail closed.

Core rules:

- raw measurement / corpus is the source of truth
- derived summaries should be reproducible
- unknown schema / protocol is not guessed into compatibility
- unresolved provenance is not presented as resolved
- exposed results do not justify hidden seed / threshold / rescue-run changes
- large generated artifacts are not committed to the repository by default
- secrets, credentials, and machine-local identity are excluded from research artifacts

Artifact persistence does not imply a generic artifact registry or cloud platform.

## Development environment

The initial supported baseline is normal CPython 3.14. Free-threaded 3.14t is out of scope until dependency compatibility is separately established.

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

Repository-specific development and review guidance is documented in `AGENTS.md` and [Claude Code workflow](docs/claude-code-workflow.md).

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

This keeps the README useful as an entry point without turning it into a chronological research ledger.
