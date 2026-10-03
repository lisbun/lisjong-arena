# Teacher-selectable policy source record (#442)

## Purpose

The Offense Foundation source record (`docs/offense-source-record.md`) is
bound to the #331 protocol lock and scientific corpus, and its teacher is fixed
to `TwoStepUkeirePolicy`. lisbun/lisjong-arena#442 needs source rows whose
teacher is a different first-party Policy, starting with the Heuristic Champion
`PlacementAwareSpeedCallPolicy` (C0).

Adding a teacher option to the existing generator would silently change the
meaning of its v2 contract, its protocol lock and its scientific corpus.
This is therefore a separate contract. The Offense Foundation v1/v2 source
record, the #331 corpus and their readers are unchanged.

Ownership is unchanged: Arena owns the producer, this file format and the seed
population; lisjong owns the teacher's decisions, any analysis semantics, and
the Learning code that consumes these rows.

## Schema and layout

```text
arena-policy-source-record-v1
```

```text
<source-record>/
  manifest.json
  game-000/source-record.jsonl
  game-001/source-record.jsonl
  ...
```

Each JSONL row has **exactly the same fields and encoding** as the Offense
Foundation source row (`game_ordinal`, `seed`, `split`, `step_ordinal`,
`decision_ordinal`, `actor_seat`, `policy_input`, `legal_actions`,
`teacher_selected_action`). The encoder and row validator are reused, not
copied. `teacher_selected_action` is the `DecisionTrace.selected_action` of the
same traced execution: the canonical legal action that passed lisjong's
legal-action validation and that the runner resolved and applied. Rows hold no
analysis, reward, oracle label, or encoded feature tensor.

`manifest.json` is sealed canonical JSON and binds:

| Field | Content |
| --- | --- |
| `purpose` | `DEVELOPMENT` or `SCIENTIFIC` (see below) |
| `game_mode` | `4p-red-half` |
| `populations` | `{split: [seeds]}`, splits among `TRAIN` / `SELECT` / `OFFLINE-EVAL` |
| `allocation_bindings` | `null` for `DEVELOPMENT`; one Arena ledger binding per split for `SCIENTIFIC` |
| `source_contract.teacher` | `policy_catalog` identity, fully qualified class, factory and its arguments, configuration digest |
| `source_contract.seat_policies` | the Policy of each seat (currently teacher self-play on all four) |
| `source_contract.runtime` | Arena revision, installed lisjong / lisjong-engine VCS revisions, digest of the imported lisjong source, shanten backend actually loaded (and, for `rust`, the native extension API version and file SHA-256), Python and RiichiEnv versions |
| `source_contract.producer` | producer module, runner, recorded seats (`ALL`) |
| `games` | one sealed summary per hanchan in protocol order, with payload bytes and SHA-256 |

Game order is the `TRAIN`, `SELECT`, `OFFLINE-EVAL` split order, then the seed
order given in the population.

## Population purpose

- `DEVELOPMENT`: connection and cost measurement only. It claims no allocation
  authority (`allocation_bindings` must be `null`) and must not be used as a
  scientific or formal-evaluation population.
- `SCIENTIFIC`: every split carries an Arena seed-allocation binding in the
  `riichienv-4p-red-half-hanchan-v1` domain. Generation requires the live
  ledger and resolves each binding with `seed_registry.require_allocation_binding`
  before any hanchan runs.

Seeds must be unique within and across splits in both cases.

## Generation

Each hanchan runs once on the RiichiEnv `LocalGameRunner` with a fresh teacher
instance per seat and the inspection recorder (traced execution). Rows are
projected from that one inspection; the teacher is never rerun for recording.
The output is built in a same-parent staging directory, strict-read, and only
then renamed into place. Existing destinations are refused.

## Replay verification

Reading the stored action back only proves the file round-trips. `replay-verify`
re-executes the bound teacher on every stored `policy_input` and canonical
legal action set and requires the result to equal `teacher_selected_action`
for every decision; one mismatch fails the check.

Before replaying, the installed teacher identity must equal the manifest's, and
the runtime `dependencies`, `lisjong_source_digest`, `shanten_backend`,
`python` and `riichienv` must equal the manifest's. The Arena revision may
differ. Legal actions are passed in their stored canonical order, so a teacher
whose tie-break depends on legal-action order is reported as a mismatch.

## Commands

```text
python -m lisjong_arena.policy_source_record population \
  --train 944500000..944500007 --select 944500008..944500011 \
  --offline-eval 944500012..944500015 --output population.json

LISJONG_SHANTEN_BACKEND=rust python -m lisjong_arena.policy_source_record generate \
  --population population.json --teacher placement-aware-speed-call \
  --workers 4 --output <new directory>

python -m lisjong_arena.policy_source_record readback --source <directory>

LISJONG_SHANTEN_BACKEND=rust python -m lisjong_arena.policy_source_record replay-verify \
  --source <directory> --workers 4
```

The `population` command writes `DEVELOPMENT` populations only. A
`SCIENTIFIC` population document is written by hand with the bindings from the
ledger and generated with `--ledger <seed-ledger.json>`.

## Not covered

- A decision trace of the teacher's branches and candidate evaluations
  (deferred to its own Issue; the Champion module would need a new analysis
  type, and the speed-call branch currently returns no analysis).
- Non-self-play seat assignments, the lisjong-engine runner, and AWS hosting.
