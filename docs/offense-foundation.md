# Offense Foundation O0 prerequisites (#331)

[Issue #331](https://github.com/lisbun/lisjong-arena/issues/331) is the scientific
authority. [Issue #332](https://github.com/lisbun/lisjong-arena/issues/332) runs
this same generator on AWS; #329/#330 own AWS planning, billing, retention,
reattachment and teardown. This package does not create AWS resources.

Implemented here: P0 exact teacher qualification, P1 canonical semantic audit,
and the first-party generator needed for the later P2/scientific executions.
No training, checkpoint selection, learner inference or rollout is implemented
or invoked. Implementing P2 accounting does **not** qualify population support.

## P0/P1 qualification (no games or scientific seeds)

From a clean committed checkout, with the exact `pyproject.toml` environment:

```powershell
.venv\Scripts\python.exe -m lisjong_arena.offense_foundation qualify --output C:\path\to\retained\o0-qualification.json
```

On Linux/AWS, use the environment's `python` for the identical module/arguments.
The parent output directory must exist. Outputs are write-once canonical JSON.
The report binds Arena HEAD, exact installed lisjong/engine revisions, imported
lisjong source digest, Python/RiichiEnv versions, feature/vocabulary fingerprints,
named checks, and actual canonical candidate values. Retain the report outside
Git. After merge, regenerate qualification against clean merged main before
allocating scientific seeds. A PR-head report is not merged-main qualification.

The current implementation inspected for this prerequisite is
`lisjong@15799e5f0fe47f2e2b2c39060de804d99c51492d`:
`policies/two_step_ukeire.py` dispatches winning actions, legal riichi, ordinary
discard, then pass. `_evaluate_and_choose_prepared()` owns staged evaluation;
the public typed analysis exposes its actual candidate values. Qualification
executes that installed implementation; the class name is not a PASS condition.

P0 probes cover ron/tsumo > riichi > discard, shanten/ukeire/second-step selection,
stable red/ordinary-five and tenpai ties, pass over chi/pon/daiminkan, discard
over optional ankan/kakan, all legal-set permutations, repeated calls and
traced/untraced parity. These are declared contract fixtures, including
legal-action boundary probes; they are not a support population or an independent
Mahjong legality implementation. The kakan probe explicitly has a public pon
and exists to test refusal even though O0 never intentionally opens a hand.

P0 outcomes are `OFFENSE TEACHER QUALIFIED` / `OFFENSE TEACHER NOT QUALIFIED`.
P1 outcomes are `SEMANTIC AUDIT PATH QUALIFIED` /
`SEMANTIC AUDIT PATH NOT QUALIFIED`. Invalid environment, input, identity or I/O
prints `STOP / INVALID` and exits nonzero. Behavior/seam failure is retained in
the qualification JSON and exits nonzero. Do not wrap, replace or repair a
failing teacher in Arena; any required stable semantic change belongs in a
separate lisjong prerequisite.

## P1 semantics

`semantics.stage_sets()` consumes
`lisjong.policies.two_step_ukeire.TwoStepUkeireCandidateEvaluation` values from
the actual `TwoStepUkeireAnalysis`. It reduces values to minimum-shanten,
maximum-current-ukeire and (when evaluated) maximum-second-step sets. It does
not calculate shanten, effective tiles, unseen counts or second-step scores.

`None` remains unevaluated; an evaluated `0` remains a score. Current ukeire is
present only on minimum-shanten candidates. Second-step scores are present only
when multiple maximum-ukeire candidates remain and the hand is not tenpai,
matching the locked lisjong path. Red-five and tsumogiri action identities are
preserved through the existing action vocabulary.

`audit_discard()` exposes stage eligibility, conditional agreement and regret
for future semantic consumers. A shanten miss has no conditional ukeire result;
a ukeire miss has no conditional second-step result. These `None` results must
not be counted as successes or included in conditional denominators. Equivalent
discards have zero semantic regret despite an exact stable-tie disagreement.
This API runs no learner and does not produce OFFLINE-EVAL qualification.

## Later execution: population request and lock

**Do not run these generation commands as part of the prerequisite PR.**
No actual population or scientific seed range is supplied by this documentation.

Before #332 execution, use the canonical Arena seed authority described in
`docs/seed-registry.md`. Live allocation state is stored on the dedicated
`seed-registry` branch, not on Arena `main`. Therefore reserving, committing,
or retiring a population does not change the exact scientific code revision.

For P2:

```text
merge #346
    -> freeze exact reviewed Arena code revision X
    -> reserve one fresh 20-seed QUALIFICATION population on seed-registry
       with arena_revision = X
    -> regenerate / strict-read final P0/P1 qualification on X
    -> build the P2 request from the canonical allocation binding
    -> Phase A executes X
```

If P2 qualifies, reserve TRAIN / SELECT / OFFLINE-EVAL exactly once using the
same execution revision X. This is the predeclared Phase-B gate, not a
result-driven replacement. If P2 fails, no Phase-B allocation is needed.

The request has exactly these fields:

| Field | P2 | SCIENTIFIC |
| --- | --- | --- |
| `phase` | `"P2"` | `"SCIENTIFIC"` |
| `populations` | `{"QUALIFICATION": [20 explicit seeds]}` | `{"TRAIN": [100 explicit seeds], "SELECT": [20 explicit seeds], "OFFLINE-EVAL": [20 explicit seeds]}` |
| `allocation_bindings` | canonical QUALIFICATION allocation binding | canonical TRAIN / SELECT / OFFLINE-EVAL allocation bindings |

Each binding carries the Arena owner, allocation identity, seed domain,
membership identity and the authorizing `ledger_revision`. That ledger revision
is retained for audit; later unrelated allocations or a
`RESERVED -> COMMITTED` transition do not invalidate the lock. Current live
validation resolves the immutable allocation identity against the authority
ledger and requires the record to remain `RESERVED` or `COMMITTED`.

A locally free range is not authority. The #332 launcher fetches
`refs/heads/seed-registry` read-only, validates the request against that live
ledger, and passes the exact same ledger snapshot into the AWS bootstrap. It
does not switch or mutate the Arena scientific worktree.

The bracketed seed descriptions above are explanatory, not valid JSON or
recommended seeds. Each split must be an ascending contiguous unsigned 32-bit
range. Scientific splits must not overlap each other or P2 seeds. There is no
default seed allocation, extension, result-driven replacement or row
reshuffling. Historical/private scans were used only to bootstrap the authority;
normal fresh allocation no longer repeats a full artifact scan or requires a
PR/merge.

For a direct local invocation, first materialize the live ledger to a temporary
file as documented in `seed-registry.md`, then pass that file explicitly:

```text
python -m lisjong_arena.offense_foundation validate-request --request p2-request.json --phase P2 --seed-ledger <live-ledger.json>
python -m lisjong_arena.offense_foundation lock --request p2-request.json --qualification o0-qualification.json --seed-ledger <live-ledger.json> --output p2-lock.json
python -m lisjong_arena.offense_foundation generate --lock p2-lock.json --output retained/p2-corpus --source-record-output retained/p2-source-record
python -m lisjong_arena.offense_foundation readback --lock p2-lock.json --corpus retained/p2-corpus
python -m lisjong_arena.offense_foundation source-readback --lock p2-lock.json --corpus retained/p2-corpus --source-record retained/p2-source-record
```

The generator reexecutes P0/P1 fixtures and requires the report/runtime to match
exactly before starting any game. It constructs four fresh exact teacher
instances per hanchan, runs `LocalGameRunner` in `4p-red-half`, and consumes
the `execute_policy_with_trace` validated actual decisions. The original legal
set is retained, including voluntary calls/kan. No runtime rescue mask is added.
Features, action encode/resolve and legal masks reuse existing first-party
contracts. No teacher decision is rerun for semantic labels.

Only a complete, strict-read P2 corpus receives a final support classification.
The six #331 thresholds are 3,000 choice rows, 50 winning opportunities, 100
legal-riichi decisions, 200 voluntary meld/kan opportunities, 2,000 ordinary
discard choice rows, and 500 second-step-applicable choice rows. Winning/riichi
opportunities count legal availability, including forced decisions; the two
discard support counts use choice rows. The six failure counters must be zero;
any execution/encoding/materialization/audit failure aborts the entire artifact.
`OFFENSE SUPPORT NOT QUALIFIED` is a complete negative result, not permission
to add more games. No partial support values are printed during generation.

Only after final `OFFENSE SUPPORT QUALIFIED`, allocate and lock the scientific
request, then execute as a separate AWS run-id:

```text
python -m lisjong_arena.offense_foundation lock --request scientific-request.json --qualification o0-qualification.json --seed-ledger <live-ledger.json> --p2-corpus retained/p2-corpus --output scientific-lock.json
python -m lisjong_arena.offense_foundation generate --lock scientific-lock.json --p2-corpus retained/p2-corpus --output retained/scientific-corpus --source-record-output retained/scientific-source-record
python -m lisjong_arena.offense_foundation readback --lock scientific-lock.json --corpus retained/scientific-corpus
python -m lisjong_arena.offense_foundation source-readback --lock scientific-lock.json --corpus retained/scientific-corpus --source-record retained/scientific-source-record
```

Both locking and generation strict-read the original P2 corpus. Scientific
generation requires the same qualification binding and exact P2 artifact
identity. Full reruns must retain the same lock and use a new destination; no
resume-from-partial or successful-game adoption is provided. A new lock does
not authorize a result-driven population change.

## Corpus artifact and handoff

Issue #342 adds a separate reusable player-safe source record beside this locked
corpus. Its schema and strict-read contract are documented in
[`offense-source-record.md`](offense-source-record.md). The sidecar is projected
from the same actual decisions but is not referenced by this corpus manifest, so
#331 corpus bytes, feature/vocabulary identities, support rules and scientific
identity remain unchanged.

`manifest.json` binds the protocol/qualification, ordered hanchan identities,
per-file SHA-256/lengths, support, zero failures, and final P2 outcome (null for
the scientific phase). Each ordered `game-NNN` directory contains:

- `rows.jsonl`: all-decision metadata, legal vocabulary indices, actual teacher
  index and the canonical staged values. Forced rows are diagnostics only.
- `features.f32`: little-endian float32 rows of dimension 8204, **only** for
  metadata rows with at least two legal actions, in that order.
- `legal-mask.u8`: the matching choice-row masks of length 802, bytes 0/1.

Future supervised consumers must iterate only those choice metadata rows; no
forced tensor rows exist. Whole-hanchan split membership comes from the game
manifest and lock, never a row-level random split. File paths, AWS run-id,
instance type, worker count, timing and cost are not corpus identity fields.
Operational progress prints only completed/total, elapsed and ETA. #332 must
provide its existing AWS PLAN/SSM/reattachment/fail-safe and durable retention
surfaces around this CLI; this PR does not implement that operational wiring.

Generation is serial by default and accepts bounded hanchan-level process
parallelism through `generate --workers N` (maximum 32). Each game writes only
its protocol-indexed staging directory; completion order is operational, while
the manifest and final directories remain in exact protocol order. Worker count
does not enter scientific identity. Any worker failure discards the paired staging root. Corpus and source record
are both strict-read before publication; source record is published first so a
completed corpus is never exposed without it. Strict corpus readback checks exact files, ordered seeds/splits, checksums, feature dimensions/finiteness,
legal label/mask consistency, candidate coverage/missingness, teacher hierarchy,
decision order, per-game counts and final support. It never reruns tile-efficiency
calculations. Keep the original P2 corpus with the scientific corpus for audit;
the embedded P2 manifest is a provenance receipt, not a substitute for its data.

Successful local readback does not establish durable AWS retention. #332 must
recover to non-ephemeral storage, strict-read there, and confirm teardown and
remaining billing. Generated corpora and qualification artifacts stay out of Git.
