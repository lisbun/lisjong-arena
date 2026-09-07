# One-shot artifact destination preflight

This document records the execution-safety hardening from Issue #177. It applies
to purpose-specific one-shot evaluation flows such as the guarded P1
higher-fidelity screen documented in
`docs/learned-policy-p1-guarded-higher-fidelity.md`.

## Motivation — Issue #175 incident

The locked Issue #175 real execution validated the exact candidate, baseline,
revisions, runtime, seeds, and posted pre-execution lock, then completed the
100-game evaluator in memory. The first durable artifact write failed because the
locked `strength_artifact` parent directory did not exist.

```text
run_single_round_evaluation(plan)
  -> 100 games complete in memory
  -> save_single_round_artifact(...)
  -> FileNotFoundError: output parent did not exist
```

The attempt is recorded as `STOP / INVALID`. It is not strength evidence and must
not be rerun as the same one-shot experiment.

The failure exposed a protocol gap: checking only that write-once output files do
not already exist is insufficient when the population must be consumed exactly
once. Deterministic output-destination defects must be rejected before game 1.

## Operator / runner boundary

Before a real one-shot run, the operator chooses the durable output locations and
creates the required parent directories explicitly. The runner does not create a
missing output parent or redirect a locked destination to another location.

For every locked write-once output used by the Issue #175-style flow:

```text
strength_artifact
result
classified_result
```

`run_higher_fidelity_evaluation()` validates before invoking the evaluator that:

1. the exact locked output does not already exist;
2. its parent exists;
3. the parent is a directory; and
4. the parent is observably writable by the current process.

The preflight does not create or truncate any final output file. The existing
exclusive-create persistence remains the final write-once authority.

`os.access(..., os.W_OK)` is an early check for obvious unusable destinations; it
cannot eliminate filesystem races. If the filesystem changes after preflight,
the final exclusive write still fails closed rather than redirecting, overwriting,
or retrying silently.

## One-shot ordering

The intended order for future one-shot evaluations is:

```text
operator prepares durable parent directories
        ↓
reviewed machine-readable execution lock
        ↓
runner validates VCS / dependency / runtime identity
        ↓
runner validates every locked output destination
        ↓
ONLY THEN invoke the game evaluator
        ↓
write-once strength artifact
        ↓
strict readback / canonical re-derivation
        ↓
write-once result / classification
```

A missing parent, parent that is not a directory, existing write-once output, or
obviously non-writable parent is a pre-execution failure. Tests must prove the
evaluator is not called in those cases.

## Scope boundary

This hardening does not introduce:

- automatic directory provisioning;
- retry or resume semantics;
- recovery of transient Issue #175 scores;
- replacement seeds or a rescue rerun;
- a generic artifact registry or remote object store; or
- changes to candidate, baseline, classification, or evaluation semantics.

After this machinery is reviewed and merged, the parent research roadmap decides
whether a new bounded higher-fidelity experiment with a fresh population and new
pre-execution lock is warranted.
