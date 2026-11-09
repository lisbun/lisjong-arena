# Overall #583 — L2a M1+R1 exact model-pair binding / execution preparation

Status: **implementation draft (PR #585), not authorized to start a paid or formal
evaluation**. The project-level current Champions are Heuristic
`placement-aware-speed-call` and Learning exact `lisjong-l2a:M1:R1` (#127).
The Overall Champion for this pair is **not established** (#69).

## Scientific contract: unchanged

The frozen scientific protocol remains
`arena-overall-champion-aabb-half-v1` from #250 / `overall-champion-aabb-half.md`.

- A = current Heuristic Champion; B = current Learning Champion
- RiichiEnv `4p-red-half`, AABB, four seat rotations per seed, 100 fresh
  independent seed blocks = **400 hanchans**
- Per seed block `D(s) = L_rank(s) - H_rank(s)`; one statistical unit per seed
- Normal-approximation two-sided 95% CI. Strict lower >0: Heuristic superior;
  strict upper <0: Learning superior; else Overall inconclusive.
- Strict verification of all 400, no rescue, no score-based override.
- No result from #582 (six-rotation lisjong-engine evaluation) is a valid
  substitute for this different four-rotation RiichiEnv comparison.

## Lock v2: only the participant attestation changes

Historical lock **v1** remains readable and semantically unchanged for past
events (#281 etc.). New v2 is selected only for the frozen #582-promoted L2a
pair and adds a separate `learning_artifacts` document included in the
canonical lock digest. It **does not** present two independently learned
heads as one surrogate Arm Y checkpoint.

The attestation is:
- supplier: `lisjong_arena.current_learning_champion:served_current_learning_champion_artifacts`
- factory: `lisjong_arena.current_learning_champion:create_current_learning_champion_policy`
- policy identity = exact `lisjong-l2a:c976ed4a...:d565713f...`
- `discard`: M1 artifact identity `c976ed4a...`, manifest SHA-256
  `37ddf3b89f9a814341cb3ef067c5faf0322df3fe28df3b321a5e335c21d8bc5c`,
  weights SHA-256 `74bd966ecdfeebee21eb09a53fdbf495d29eb22fcba382115aad53ff2e88ba8c`
- `reaction`: R1 artifact identity `d565713f...`, manifest SHA-256
  `70962c2e620326b804eda78402bada06b1405684cb5aa381a438ba232b2367a9`,
  weights SHA-256 `b996ea98c35aec7693e07d23e0fb886c6797f7490b3a2f2833bde16c74f94a13`

Supplier always invokes the **actual strict-loaded serving runtime** and
re-hashes both on-disk manifest and weights files. Lock construction and
live pre/post-execution require an exact match; offline strict verification
uses the sealed digest alone and remains portable. Operator must set the
two existing absolute-path env vars to the retained #582 artifact directories.
Missing, swapped, mutated, or inconsistent files fail closed. The full
frozen evaluation source path and retained SHA256SUMS are in #127.

V2 lock creation additionally requires `--ml-runtime-package torch` and
`--learning-artifact-binding` set to the exact supplier above. Check
the `lisjong-arena` pin against the final merged PR commit and the underlying
`lisjong` / `lisjong-engine` pins before lock.

## Why an operational preflight is essential

The existing Overall generic executor is based on **RiichiEnv** and creates
policies via the top-level factory per seat. #582 used the **lisjong-engine**
runner with separate per-worker model load. Therefore:
- #127's 1,151-action equivalence demonstrates the new factory did not change
  the evaluated behavior *on that machine*.
- Neither #127 nor #582 proves this factory supports **all legal action and
  game transitions in RiichiEnv**, nor does it establish Overall throughput or
  400-game memory usage.
- Cross-CPU float32 scorer differences (max 7.63e-6 on existing fixed inputs)
  are a predeclared runtime limitation, not permission to tune weights or
  evaluate with mixed CPU/torch identities.

Preliminary no-charge preflight: reuse the already-exposed **development-only
seed set**, and run at most **4 seed blocks × 4 rotations = 16 hanchans** in
the *generic RiichiEnv comparison path*, not in the locked 100-seed formal
runner. Freeze the exact seed values before this preflight, have the operator
record the source/usage ledger and label all 16 DEVELOPMENT; no previously
unused formal seeds, no testing on held-out #582 / #281 TEST. External
system timeout 40 minutes; fail on missing action, incompatible Policy,
assertion, mismatch, reproduction failure, or time cap. Tests on synthetic
comparison fixtures do not launch these games.

## Proposed paid stages (NOT YET AUTHORIZED)

- Region: `ap-northeast-1`; candidate EC2: On-Demand `c7i.4xlarge`
  (16 vCPU, 32 GiB), eight workers; encrypted gp3 40 GiB; IMDSv2,
  SSM-only no inbound access; fail-safe termination and no residual resource.
- Stage A, **paid operational smoke only after WSL preflight PASS**:
  2 previously used dev seed blocks × four rotations = **8 games**;
  1.5h instance TTL and cgroup/torch/lock/wheel/operator wiring check.
  No formal seed allocation. Same 6GiB cgroup `memory.peak` soft STOP and
  8GiB hard cap / swap0 as #582 are **proposed operating limits**, not the
  immutable scientific protocol. Monitor cgroup OOM events, disk, clock and
  results; explicitly stage/capture/cleanup and record AWS spend.
- Stage B, **one-shot formal after A PASS**, 100 *new*, previously unused
  seed blocks × four rotations = 400 games. 6h instance TTL / 5h formal
  runner wall proposal, eight workers. On any runtime/provenance/guard/
  verify/timeout/drive/fail-safe event: **STOP / INVALID**, no partial
  strength claim, no automatic retry, no threshold relaxation and no
  rescue; newly allocated formal population becomes COMMITTED/RETIRED
  according to canonical registry policy, never reused.
- Verify result in a separate clean process, using full raw/comparison/result
  bundle and canonical lock. Check `result_identity`, all 400 rows, paired
  AABB seat assignments and A1-like *but not identical* Overall metric,
  95% CI, SHA256SUMS, full runtime/model hashes, final watchdog receipt,
  zero OOM, and cleanup.
- **Operator guard gap**: historical Overall v1 writes `comparison.json`
  before final `overall-result.json` + strict verification. For this event
  the outside watchdog/staging orchestrator MUST quarantine the former
  if later verification or guard fails; neither path alone makes the
  partial output valid. Refuse a successful event if `quarantine_error`
  is non-null or guard outputs remain after a failed stage.

## Proposed budget and explicit approval gate

The previously quoted Tokyo Linux `c7i.4xlarge` On-Demand reference is
**$0.8988/h** (third-party public price, not a live AWS account quote).
At roughly $0.91/h inclusive of 40 GiB gp3 and a public IPv4, a
1.5h smoke + 6h formal TTL gives **~$6.83** of instance-associated
exposure; reserve the balance for snapshots, transfer/S3, launch delays
and contingencies. **Proposed new all-stage hard budget: $10.00 USD,
separate from #582**. This is *not* approved. Before any launch reprice
from AWS Pricing API and verify projected max spend < $10 including
preflight, retries forbidden, instance/storage/network left running until
failsafe; if any uncertainty or budget shortfall, STOP before launch.

**No new formal seeds, AWS launch, charge, or real formal games without an
explicit user approval of a final signed-off plan**. Re-plan rather than
silently proceed if the 16-game local smoke cannot establish compatible
behavior, or the 8-game AWS smoke predicts >5h formal execution.

## CLI sketch (run only after reviewed PR merge / separate authorization)

A v2 lock is created only on clean merged main and only with the exact
evaluated frozen artifact pair. The full Heuristic and Learning identity,
factory and source-revision flags and new untouched population must also
be supplied:

```text
python -m lisjong_arena.overall_champion_aabb lock \
  --seeds <FRESH_100_SEEDS> --workers 8 \
  --out <NEW_LOCK> \
  --comparison-artifact <NEW_COMPARISON_PATH> \
  --overall-result <NEW_RESULT_PATH> \
  --heuristic-identity placement-aware-speed-call \
  --heuristic-factory lisjong_arena.policy_catalog:create_placement_aware_speed_call \
  --heuristic-source lisjong --heuristic-revision <PINNED_LISJONG> \
  --learning-identity <FULL_LISJONG_L2A_POLICY_IDENTITY> \
  --learning-factory lisjong_arena.current_learning_champion:create_current_learning_champion_policy \
  --learning-source lisjong-arena --learning-revision <PINNED_ARENA> \
  --learning-artifact-binding lisjong_arena.current_learning_champion:served_current_learning_champion_artifacts \
  --ml-runtime-package torch

python -m lisjong_arena.overall_champion_aabb run \
  --lock <NEW_LOCK> --progress

python -m lisjong_arena.overall_champion_aabb verify \
  --lock <NEW_LOCK> --comparison <NEW_COMPARISON_PATH> \
  --result <NEW_RESULT_PATH>
```

**Do not run this sketch as-is**: placeholders must be replaced only from
the final merged pins and canonically allocated fresh seed block, and all
external watchdog/timeout/quarantine conditions must already be active.

## Completion checklist

- [ ] PR #585 CI/Ruff/unit tests PASS + independent review
- [ ] v1 historical lock and #281 bundle strict-read regression PASS
- [ ] v2 lock two-model hashes/pre+post attestation and tamper regression PASS
- [ ] WSL actual RiichiEnv compat development smoke (16 games max) PASS
- [ ] Stage A / Stage B exact watchdog/instance/cleanup wiring and budget
- [ ] Explicit operator approval for paid Stage A/B and formal seeds
- [ ] Formal one-shot 400/400 + strict verify / cleanup (not this PR)
