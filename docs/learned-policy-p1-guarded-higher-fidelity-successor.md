# Issue #179 guarded candidate higher-fidelity successor

Issue #179 is a new development higher-fidelity screen for the exact Issue #173
shanten-guarded P1 Q candidate against exact `yakuhai-call` x3.

It is **not** a rerun of Issue #175.

Issue #175 consumed seeds `547..571` under a posted lock. Its evaluator completed
that population in memory, but durable artifact persistence failed because the
output parent directory did not exist. No valid strength artifact was created,
so Issue #175 is historical `STOP / INVALID` evidence and its transient scores
must not be recovered or interpreted.

PR #178 / Issue #177 added fail-closed destination checks. This successor applies
those checks both when the pre-execution lock is generated and again immediately
before the evaluator is entered.

## Locked scientific question

```text
G = exact #173 guarded P1 Q candidate
C = exact Stage 6 yakuhai-call baseline

G vs C x3
4p-red-single
25 fresh seed blocks
4 seat rotations per seed
100 games total
```

Candidate identity, model weights, P1 representation, TRAIN support, hybrid
activation, fallback, legal-action resolution, Q values, and guard semantics are
unchanged from #173/#175.

The first-choice successor population is:

```text
572..596
```

`547..571` is permanently treated as consumed by #175 and is not fresh for this
successor. Immediately before lock generation, review current open/closed Issues
and any known local/private allocations. A collision found before result exposure
requires `SEED PLAN REFORMULATE`; after execution begins there is no seed
replacement, extension, or rescue rerun.

## Historical isolation

The historical Issue #175 module remains unchanged. Issue #179 owns distinct:

- lock schema identity;
- result schema identity;
- experiment/source identity;
- GitHub lock-comment namespace;
- seed population;
- strength/result/classified retention keys.

The exact retained #162 candidate checkpoint is intentionally shared because the
scientific candidate itself is unchanged.

## Output destination boundary

The operator must explicitly create the durable Issue #179 output parent before
building the lock. The runner does not create it automatically.

For each locked output:

```text
strength_artifact
result
classified_result
```

lock generation requires:

```text
target absent
parent exists
parent is a directory
parent observably writable
```

The same readiness checks run again after the Issue #179 lock has been posted and
before game 1. The preflight does not create or truncate final output files.
Exclusive-create persistence remains the final write-once authority, so a
filesystem race after preflight still fails closed.

## Review and execution order

Do not run the real population from an implementation branch.

```text
1. implement successor machinery
2. focused tests
3. documentation
4. self-review
5. PR with `Refs #179`
6. GitHub Actions green
7. user-approved merge

then on merged main:

8. explicitly create the durable Issue #179 parent directory
9. fetch origin and verify clean HEAD == refs/remotes/origin/main
10. strict-read exact #162 checkpoint
11. recheck baseline/runtime and fresh seed allocation
12. build the Issue #179 lock (destination preflight runs here)
13. post the exact rendered lock to Issue #179
14. preserve that Issue #179 comment URL
15. execute the one-shot 100-game successor (destination preflight runs again)
16. strict-read the immutable strength artifact
17. derive and persist the exhaustive classification
18. hand the result back to lisjong-project #45
```

The implementation PR must not use `Closes #179`; the Issue remains open until
the real result or a terminal pre-result state is recorded.

## Local artifact layout

A recommended operator-local layout is:

```text
C:\Dev\lisjong-artifacts\offlineq-179-guarded-higher-fidelity\
    strength.json
    result.json
    classified.json
```

The parent directory must exist **before** lock generation. The files themselves
must not exist.

The retained candidate remains at the exact #162 checkpoint location, for
example:

```text
C:\Dev\lisjong-artifacts\offlineq-162-p1-gate-b\candidate
```

Local paths are retention locations, not semantic candidate identity.

## Primary inference

Primary unit: one four-rotation seed block.

```text
metric   seed-block G-vs-C score delta
CI       normal-approx 95%
```

Classification:

```text
lower > 0   GUARDED CANDIDATE HIGHER-FIDELITY SIGNAL
upper < 0   GUARDED CANDIDATE HIGHER-FIDELITY NEGATIVE
otherwise   GUARDED CANDIDATE HIGHER-FIDELITY INCONCLUSIVE
```

Pre-result states remain distinct:

```text
GUARDED CANDIDATE EVIDENCE BLOCKED
STOP / INVALID
```

Secondary Mahjong and serving/guard diagnostics never rewrite the primary
classification.

## Interpretation boundary

A positive result means only that the exact guarded candidate showed a clear
fresh-development single-round score signal against the exact locked
`yakuhai-call` comparator under this protocol.

It does not establish:

```text
hanchan strength
formal generalization
production promotion
baseline replacement
Q-objective sufficiency
hard shanten guard as final architecture
```

Any valid SIGNAL / NEGATIVE / INCONCLUSIVE result returns to project Issue #45.
No hanchan follow-up is filed automatically.

## CI boundary

Unit tests use synthetic fixtures and mocks. They must not consume `572..596` in
RiichiEnv. The real 100-game run is operator-controlled post-merge execution, not
a CI requirement.
