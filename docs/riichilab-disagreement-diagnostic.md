# RiichiLab strong-bot disagreement diagnostic

Issue #251 measures descriptive action disagreement between the exact retained
RiichiLab #170 strong-bot corpus and the protocol-scoped
mechanism-riichi-defense heuristic baseline.

The diagnostic is offline and aggregate-only. It reuses the Issue #211 replay
materialization path and receives the exact replay-time DecisionContext through
an optional observer seam. It never reconstructs DecisionContext from
MaterializedRow and does not persist raw game logs, transformed decision rows,
or individual third-party decisions.

## Locked inputs

- corpus identity: 064b949733b13026bdbe951c9f69b90653c5977e894449d23f99ce830718b865
- manifest SHA-256: 378edce0a3a117abd47e1f400e92c59de52be2fb990947865c08d815b2f9b0ca
- target bots: FuuroMaster 126, Mortal-v4b 120, zero-test2 294
- comparator alias: mechanism-riichi-defense
- comparator class: MechanismRiichiDefenseYakuhaiCallPolicy
- comparator binding Arena revision: 05bf7c24613e4a94de9db889605390df318e4557
- lisjong revision: f29d129c67e5232d06563c6e457754377734ed14
- RiichiEnv: 0.4.10
- expected coverage: 112 games / 19,874 target-seat decision opportunities /
  18,992 choice rows / 882 forced rows

Forced rows are counted for coverage but excluded from agreement rates.

## Run

Run only after the implementation PR is merged, from a clean checkout of the
exact revision to be recorded. Reuse the same local #170 snapshot and corpus
directory that passed the existing source identity gate.

PowerShell example:

    $arenaRevision = git rev-parse HEAD
    python -m lisjong_arena.riichilab_disagreement_diagnostic --snapshot <path-to-170-snapshot.json> --output-dir <path-to-170-corpus-directory> --arena-revision $arenaRevision | Tee-Object -FilePath <outside-git-artifact-dir>\issue-251-aggregate.json

The JSON output is the local aggregate report. Keep it outside the repository.
Only its aggregate values and compact interpretation should be copied to
GitHub.

## Interpretation boundary

This is not a strength evaluation. A teacher action is an observed choice, not
a correctness label. The primary output is agreement/disagreement frequency by
bot, decision kind, action-family pair, hand openness, riichi state, and legal
action count. For discard-vs-discard disagreements only, the diagnostic also
compares public calculate_shanten results after removing exactly one selected
tile from the player-visible concealed hand.

No opponent concealed hand, wall truth, future state, outcome, or teacher
internal analysis is consumed.
