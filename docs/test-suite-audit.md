# Test-suite audit — September 2026 snapshot

Audited revision: `d9e163a8f7c1bc0a59f9cd63af8f003d96f1fbd7` (`origin/main` and clean local `main` after `git fetch origin`, 2026-09-13). Scope: the 192 tracked Python modules directly under `tests/`, the current `.github/workflows/ci.yml`, Issue #236, and completed Issue #226. This is the first, audit-only step of #236. No test, production code, protocol, artifact, or CI behavior was changed.

The inventory below was generated from every `tests/*.py` file and checked against the module contents (test method names and imported production targets). It assigns exactly one **primary** responsibility to each of 162 discovered `test*.py` modules. `tests/` also has 30 underscore-prefixed fixture/helper modules. A primary label is for maintenance; many modules also exercise a second boundary. `P` means protocol/research regression, `U` core/unit, `I` integration, `M` Torch-backed ML, and `S` compatibility/documentation stub. The `Representative protection` column names assertions rather than asserting that the examples exhaust the file. For a historical module, the relevant production target is in the `Target` column; the historical-origin and durable-invariant notes following the inventory explain why it remains.

## What is current protection?

Core comparison, single-round, parallel, adapter, RiichiLab, Mortal, trace, artifact, and durable-record modules validate current construction, execution, information-flow, strict-loading, and failure contracts. Integration modules cross real RiichiEnv, lisjong-engine, or spawned-process boundaries. The Phase/Stage/Issue and Learned Policy families are largely **current protocol protection**, not an obsolete test stratum: they lock source populations and seed separation, test-only/holdout constraints, model and artifact identity, deterministic selection, checkpoint compatibility, and fail-closed research outcomes. The inventory exposes the `test_phase*` (23), `test_stage*` (11), `test_issue_*` (5), `test_learned_policy*` (37), and `*_ml.py` (32) families; these sets overlap. All 32 `_ml.py` files have primary label `M`.

## Historical provenance and durable invariants

The detailed per-module inventory below supplies the current target and representative assertions. This map records the origin and the *shared* durable boundary for each historical family. Every individual Phase/Stage/Issue/Learned Policy module is `KEEP` in this snapshot; no equivalent-or-stronger replacement of its entire coverage was demonstrated. A future candidate must be assessed at test-case level, not inferred from a family name.

| Origin / modules | Durable protection still exercised | Disposition and successor coverage |
| --- | --- | --- |
| Phase 05: `test_phase05_*` (8) | Player-visible features versus privileged labels; locked split, leakage prevention, estimator/metric, decision-linked samples. | `KEEP`; no complete successor. |
| Phase 2: `test_phase2_training_anchor` | Immutable player-safe turn anchor, ordered evidence, extraction and provenance. | `KEEP`; Phase 3/4 consume the anchor but do not replace its unit boundary. |
| Phase 3: `test_phase3_bootstrap_corpus*` (2) | Fixed bootstrap recipe, canonical artifact identity, readback and explicit malformed/tampered rejection. | `KEEP`; the negative-path module adds distinct failure cases. |
| Phase 4: `test_phase4_raw_corpus` | Raw corpus identity, provenance, turn semantics and strict persistence. | `KEEP`; downstream datasets consume this contract. |
| Phase 5: `test_phase5_belief_dataset` | Whole-game partitioning, turn-only anchor references, exclusion of labels/metrics from split input. | `KEEP`; no complete successor. |
| Phase 6: `test_phase6_snapshot_feature`, `test_phase6_snapshot_ml` | Frozen feature axis/identity, Torch-free base import, constrained model and locked shape. | `KEEP`; feature and model halves are complementary. |
| Phase 7: `test_phase7_snapshot_test`, `test_phase7_snapshot_ml` | One-shot test-only seal and frozen artifact spec, CPU inference on the sealed partition. | `KEEP`; later Phase 8/9 tests do not replace this holdout gate. |
| Phase 8: `test_phase8_sequential`, `test_phase8_sequential_ml` | Sequence grouping/reset, seal before materialization, model shape and label-free self rollout. | `KEEP`; later stage tests inherit but do not substitute these boundaries. |
| Phase 9: `test_phase9_confirmatory`, `test_phase9_confirmatory_ml` | Locked confirmatory population, whole-game bootstrap, public-input self rollout. | `KEEP`; confirmatory outcome remains distinct from pilot evidence. |
| Phase 11: `test_phase11_public_riichi_wait_readout`, `_ml` | Public riichi/wait readout, numerical tolerance and execution-revision provenance. | `KEEP`; no complete successor. |
| Stage 3 entry/kan/mix: `test_stage3_entry_gate*`, `test_stage3_kan_coverage`, `test_stage3_mix_pilot*` | Physical/coverage gates, distinct arm and population identity, paired measurements, no test partition, strict artifacts. | `KEEP`; distinct research decisions and gates. |
| Stage 3 scale/epoch/saturation: `test_stage3_scale_learning_curve*`, `test_stage3_epoch_budget*`, `test_stage3_optimization_saturation*` | Nested seed-only populations, fixed validation, deterministic training prefix, locked budgets, selection exposure, artifact/result binding. | `KEEP`; same model lineage but different locked experimental axes. |
| Issue #177: `test_issue_177_artifact_destination_preflight` | One-shot destination validity before evaluator invocation; no partial output. | `KEEP`; no named equivalent preflight coverage across every output. |
| Issue #181: `test_issue_181_p6_gate_a*` | Exact conservative-Q formulation and lock; legal/train-supported action set, deterministic training and checkpoint. | `KEEP`; P1 gate tests do not cover this P6 candidate. |
| Issue #190: `test_issue_190_data_sufficiency*` | Exact source/seed-only scale, train/validation-only data access, fixed 8204 model, deterministic training and strict artifact. | `KEEP`; no complete successor. |
| Learned Policy input/stages: `test_learned_policy_input*`, `test_learned_policy_stage2*`, `test_learned_policy_stage3*`, `test_learned_policy_stage4a*` | Schema fingerprint and public projection; fixed model/checkpoint/fixture identities, seed isolation and strict retention. | `KEEP`; each stage protects a different public or artifact boundary. |
| Learned Policy Offline-Q base: `test_learned_policy_offline_q`, `_artifact`, `_support`, `_smoke`, and `*_ml` base modules | Eligible decision and whole-game split semantics; train-only support, tensor masks, BC/Q training, serving fallbacks, replacement-test isolation, strict retention. | `KEEP`; ML and non-ML halves are complementary. |
| Offline-Q P1/P6 and curriculum families: all remaining `test_learned_policy_offline_q_*` | Locked experiment populations, arm identities, seed noncollision, shared evaluation rows, guarded serving, candidate/control comparison, no hidden test-data dependence. | `KEEP`; similarly named gates/diagnostics target different decisions and provenance. |

In particular, `test_stage3_epoch_budget*` and `test_stage3_optimization_saturation*` both check a shared training-prefix concept, but one locks the original 80-epoch adequacy experiment and the other locks the 80-versus-160 saturation experiment. The differing lock, result, and artifact rules are material. The `*_ml.py` companions exercise actual Torch model code; non-ML discovery alone cannot replace them.

## Consolidation / removal evidence

| Candidate | Old coverage | Exact current successor | Equivalence, provenance, and removal risk | Disposition |
| --- | --- | --- | --- | --- |
| Represent the note in `test_comparison_parallel_integration.py` in documentation instead | **Zero executable cases**; a docstring explains why the earlier real-boundary test was consolidated. | `test_comparison_integration.py::test_real_serial_and_spawned_parallel_comparison_agree`; `test_parallel_execution.py` covers process spawn, instance creation and failure; `test_comparison_parallel.py` covers ordering, worker forwarding and fail-closed aggregation. | These executable cases cover the behaviors named by the stub more strongly than a zero-test file. Removing the file would lose the discoverable historical pointer and require updating CI's explicit zero-test allowlist. Keep it until a reviewed documentation/CI change preserves that pointer. | `CONSOLIDATION_CANDIDATE` for **representation only**; no deletion in this PR. |

No test-body removal candidate meets #236's successor-equivalence gate. Apparent overlap in Phase 3 negative paths, Stage 3 budget lineage, and Offline-Q gate families has distinct failure/protocol protection; retain them. Where an exact successor has not been established, disposition is `KEEP`, including the intentionally documented stub for this step. `UNCERTAIN_KEEP` is the default for a historical test whose origin cannot be resolved later, rather than a deletion proposal.

## Intentional zero-test compatibility stub

`tests/test_comparison_parallel_integration.py` documents that the integrated real-RiichiEnv serial/spawned comparison moved to `test_comparison_integration.py`, while `test_parallel_execution.py` and `test_comparison_parallel.py` protect focused spawn, ordering, worker-count and failure semantics. The file has no `unittest` cases. The #226 `ml-tests` runner explicitly permits this single zero-test filename while failing for any other zero-test module. The note remains valuable as an immediately discoverable migration explanation. A later cleanup could move it to this document and remove the CI exception together; there is no urgent benefit to doing so now.

## Runtime observations

The runner environment is Windows, CPython 3.14.6, `.venv`, CPU `torch==2.13.0+cpu`, CUDA unavailable, 8 logical CPUs and default 8 Torch intra/inter-op threads. CI is Ubuntu with Python 3.14 and a 15-minute timeout per `quality` and `ml-tests` job; its available CPU/worker hardware was not measured. Wall times are not portable between those machines. `quality` installs `.[dev]` without Torch and runs full discovery; `ml-tests` verifies locked CPU Torch, enumerates every `tests/**/test*.py`, and executes each module in a fresh interpreter with `--durations 30`. This enumeration launches module processes serially; individual process-spawn tests can use their own worker counts. The job explicitly rejects duplicate basenames and unexpected zero-case modules. The local full-suite measurement uses one interpreter, so it is **not** an estimate of the isolated CI job.

| Measured path at audited revision | Actual wall / reported duration | Outcome and interpretation |
| --- | --- | --- |
| [Ubuntu `quality` CI run 34706992603](https://github.com/lisbun/lisjong-arena/actions/runs/34706992603) | `Run tests` step 17:02:06–17:07:17 UTC: **311 s wall** (step timestamps, second precision); `unittest`: **306.328 s**. | 3,539 cases, 456 skipped, success. The skipped cases include optional Torch tests. Format and Ruff also passed in this job. |
| Same run, Ubuntu `ml-tests` | Isolated test step 17:02:31–17:14:39 UTC: **728 s wall**; sum of the 161 individual `unittest` reports: **663.09 s**. | 3,539 cases across 161 nonzero modules, **0 skipped**, success. The 162nd module is the explicit zero-test stub. CPU Torch verification passed. Wall minus reported duration (about 65 s) includes process startup/import and runner overhead; it is not a per-test cost. |
| Local Windows `.venv\Scripts\python.exe -m unittest discover -s tests -v --durations 30` with Torch installed | **608.855 s wall** by PowerShell stopwatch; `unittest`: **604.244 s**. | 3,539 cases, 1 platform skip, **2 failures and 9 errors**. Nine `test_riichilab_corpus_acquisition.py` cases hard-code `tempfile.TemporaryDirectory(dir="/tmp")`, a nonexistent Windows directory. Two Torch-free import assertions in `test_phase8_sequential.py` and `test_stage3_entry_gate.py` fail after earlier cases imported Torch in the shared interpreter. Those two modules pass in fresh local interpreters (11/11 and 53/53). This is a measured local limitation, not a green full-suite claim. |
| Local isolated #226 regression modules | `test_issue_181_p6_gate_a_ml.py`: **6/6**, 3.422 s reported; `test_issue_190_data_sufficiency_ml.py`: **5/5**, 4.253 s reported. | Both execute Torch-backed cases rather than skip. Fresh-process module runs also confirm the normal-import checks above. The Windows system Python has no Torch but an incompatible older `lisjong` installation (`DecisionTrace` import failure), so a separate local Torch-free full suite was **not executed**; the Ubuntu `quality` CI run supplies that measured path. |

The `ml-tests` CI log provides a **module-isolated** cost profile, not just a filename-based guess. Highest individual `unittest` reports were `test_riichilab_source_pilot.py` **129.128 s** (live replay against generated games), `test_stage3_scale_learning_curve_ml.py` **62.286 s**, `test_phase2_training_anchor.py` **41.803 s**, `test_riichilab_source_pilot_ml.py` **34.871 s**, `test_stage3_mix_pilot_ml.py` **30.000 s**, `test_learned_policy_offline_q_fh_curriculum_ml.py` **28.634 s**, and `test_learned_policy_offline_q_p1_shanten_guard_ml.py` **28.183 s**. Parsing matched all 161 `Ran ... in ...` reports in filename order; no report was assigned to the explicitly allowed zero-test stub. These are reported test durations; the CI step wall time is above.

For the Torch-free `quality` run, `--durations 30` singled out `test_generated_games_match_live_semantics_exactly` (**101.598 s**) and `test_materialized_games_account_for_every_opportunity` (**15.195 s**) in the RiichiLab source pilot; `test_extraction_never_silently_drops_an_anchor` (**13.881 s**) in Phase 2; and real spawned execution in `test_single_round_evaluation_parallel_integration.py` (**13.840 s**) and `test_comparison_integration.py` (**12.326 s**). The local Torch-enabled full run likewise reported live replay (**89.349 s**) and added synthetic readout training (**29.526 s**) and source-pilot strict checkpoint readback (**22.796 s**). Real RiichiEnv, actual hanchan, multiprocessing, and artifact-heavy/ML tests are therefore the measured expensive boundaries. No test is a deletion candidate merely for cost.

## Unresolved questions and follow-up sequence

1. Track the Ubuntu per-module CI timings above across later revisions before changing isolation or timeouts; determine whether the long live-replay and ML modules stay dominant. Preserve #226 exhaustive coverage.
2. Decide whether Windows local full-suite support is desired. If so, handle the nine `/tmp` fixture errors and the two shared-interpreter Torch-free import checks in a separate scoped change with CI-equivalent isolation; do not change those tests under this audit.
3. In a small follow-up, decide whether to move the zero-test explanation into durable docs and remove its allowlist entry together. Preserve its historical pointer.
4. Only then evaluate any test-body consolidation case by case: list exact old cases, exact successor cases and their assertions, run both relevant CI paths, and preserve research provenance. Do not start with bulk renames or a directory move.
5. Revisit `tests/unit`, `tests/integration`, `tests/ml`, and `tests/protocol` only if new-module growth makes the current flat layout materially costly; first prove discovery and namespace imports work with the proposed layout.

## Per-module inventory

The next table is exhaustive for `test*.py` at the audited revision. Representative assertions are extracted from actual `test_` methods; a row with a single example may contain many additional cases. The target is the imported Arena contract or an explicitly named external/repository boundary. Historical origin follows the filename; no historical row is treated as obsolete by name.

| Module | Primary | Target | Representative protection | Disposition |
| --- | --- | --- | --- | --- |
| `test_artifact.py` | U | artifact, comparison | round trip returns factory free immutable snapshot; unverifiable revision fails closed | KEEP |
| `test_claude_git_global_option_guard.py` | U | repository Git guard | normalizes global options before push; unknown global option fails closed | KEEP |
| `test_claude_workflow_guard.py` | U | repository workflow guard | allows feature branch push; malformed input denies protected operation | KEEP |
| `test_comparison.py` | U | comparison, model + related contracts | rotations match the comparison protocol; inconsistent ranks are rejected | KEEP |
| `test_comparison_integration.py` | I | comparison | real serial and spawned parallel comparison agree | KEEP |
| `test_comparison_parallel.py` | U | _parallel_execution, comparison + related contracts | result order does not depend on outcome dict iteration order; parallel metrics use the shared aggregation function | KEEP |
| `test_comparison_parallel_integration.py` | S | serial/spawn migration note | migration rationale; zero executable cases | CONSOLIDATION_CANDIDATE (retain) |
| `test_durable_local_game_record.py` | U | _artifact_io, durable_local_game_record + related contracts | round trip preserves trace result decisions and supported analysis; rejects unknown analysis subtype before creating target | KEEP |
| `test_game_trace.py` | U | game_trace | accepts detached json object text; does not complete an empty trace | KEEP |
| `test_handbelief_throughput.py` | P | handbelief_throughput, phase4_raw_corpus + related contracts | live runtime provenance uses plain json strings; artifact readback and corruption rejection | KEEP |
| `test_issue_177_artifact_destination_preflight.py` | P | learned_policy_offline_q, single_round_artifact | each missing output parent fails before evaluator invocation; valid existing parents reach evaluator without preflight side effects | KEEP |
| `test_issue_181_p6_gate_a.py` | P | learned_policy_offline_q | locked change is single fixed formulation; recorded conditions cannot override metrics | KEEP |
| `test_issue_181_p6_gate_a_ml.py` | M | learned_policy_offline_q, learned_policy_stage2 | cql gap uses only current legal and train supported actions; gate a evaluates candidate and control on exact common rows | KEEP |
| `test_issue_190_data_sufficiency.py` | P | learned_policy_data_sufficiency, learned_policy_offline_q | exact source and nested seed only scales are locked; blocked and invalid failures are distinct | KEEP |
| `test_issue_190_data_sufficiency_ml.py` | M | learned_policy_data_sufficiency, learned_policy_offline_q + related contracts | model is exact flat 8204 128 relu 802; tampered checkpoint weights fail closed | KEEP |
| `test_learned_policy_input_feature.py` | P | learned_policy_input, lisjong_engine + related contracts | version dimension groups and fingerprint are locked; runtime dimension assertion is fail closed | KEEP |
| `test_learned_policy_input_ml.py` | M | learned_policy_input | to tensor is one finite float32 vector | KEEP |
| `test_learned_policy_offline_q.py` | P | learned_policy_offline_q, learned_policy_stage2 | all discard choice decision is eligible; a single legal action row fails closed | KEEP |
| `test_learned_policy_offline_q_artifact.py` | P | learned_policy_offline_q | round trip binds every required identity; writer rejects provenance from another lisjong revision | KEEP |
| `test_learned_policy_offline_q_bc_training_ml.py` | M | learned_policy_offline_q | training selects a checkpoint and round trips it; tampered manifest config fails closed | KEEP |
| `test_learned_policy_offline_q_diagnosis.py` | P | learned_policy_offline_q | locked identities are the issue values; ukeire states why it is unavailable | KEEP |
| `test_learned_policy_offline_q_diagnosis_ml.py` | M | learned_policy_offline_q, _artifact_io | binding records the observed identities; the diagnose cli fails closed on an unbound artifact | KEEP |
| `test_learned_policy_offline_q_exposure_evaluation_ml.py` | M | learned_policy_offline_q | bc test diagnostics are finite and bounded; q test diagnostics report finite q rate and residual | KEEP |
| `test_learned_policy_offline_q_fh_curriculum.py` | P | learned_policy_offline_q, learned_policy_stage2 + related contracts | arms use the curated catalog factories; policy catalog retains predecessor entries | KEEP |
| `test_learned_policy_offline_q_fh_curriculum_ml.py` | M | _artifact_io, learned_policy_offline_q + related contracts | the model is the locked 8241 input p1 model; the eligible rollout context is supported by both arms | KEEP |
| `test_learned_policy_offline_q_p1_gate_a.py` | P | learned_policy_input, learned_policy_offline_q | the mask has exactly thirty seven entries; every role is measured on the same eligible rows | KEEP |
| `test_learned_policy_offline_q_p1_gate_a_ml.py` | M | learned_policy_offline_q, learned_policy_stage2 | the parameter count is the locked value; a synthetic result cannot record an outcome | KEEP |
| `test_learned_policy_offline_q_p1_gate_b.py` | P | learned_policy_offline_q, policy_catalog + related contracts | ron is selected when it is legal; a non callable deriver is rejected | KEEP |
| `test_learned_policy_offline_q_p1_gate_b_ml.py` | M | learned_policy_input, learned_policy_offline_q + related contracts | the learned path receives exactly the 8241 feature; a missing bundle is rejected | KEEP |
| `test_learned_policy_offline_q_p1_guarded_higher_fidelity.py` | P | learned_policy_offline_q, model + related contracts | exact issue 162 and 173 identities are bound; result builder rejects a noncanonical summary | KEEP |
| `test_learned_policy_offline_q_p1_guarded_higher_fidelity_successor.py` | P | learned_policy_offline_q, single_round_artifact | successor identity and population are distinct from issue 175; pre result states remain distinct | KEEP |
| `test_learned_policy_offline_q_p1_shanten_guard.py` | P | learned_policy_input, learned_policy_offline_q + related contracts | the identity is a prefixed binding digest; keep shanten tile mask semantics are unchanged for a locked hand | KEEP |
| `test_learned_policy_offline_q_p1_shanten_guard_ml.py` | M | learned_policy_input, learned_policy_offline_q + related contracts | when a keep shanten subset exists the argmax is restricted to it; collect guard diagnostics reuses the activation diagnostics shape | KEEP |
| `test_learned_policy_offline_q_p6_gate_b.py` | P | learned_policy_offline_q, model + related contracts | default population is fresh contiguous 25x4; gate a signal identity is fixed | KEEP |
| `test_learned_policy_offline_q_p6_gate_b_ml.py` | M | learned_policy_offline_q | plan reuses p1 hybrid serving and passive comparator; checkpoint model is reused without retraining | KEEP |
| `test_learned_policy_offline_q_p6_higher_fidelity.py` | P | learned_policy_offline_q, model + related contracts | issue 185 has purpose specific identity and metadata; wrong baseline or partial population is rejected | KEEP |
| `test_learned_policy_offline_q_p6_higher_fidelity_ml.py` | M | learned_policy_offline_q | exact unguarded runtime and yakuhai baseline are fresh | KEEP |
| `test_learned_policy_offline_q_q_training_ml.py` | M | learned_policy_offline_q | train support mask matches the train behavior actions; target network is frozen within an epoch and resynced between epochs | KEEP |
| `test_learned_policy_offline_q_replacement_test.py` | P | _artifact_io, learned_policy_offline_q + related contracts | locked population is 354 to 359; identity excludes only itself | KEEP |
| `test_learned_policy_offline_q_replacement_test_ml.py` | M | learned_policy_offline_q | tensors match the artifact totals; support mask rejects an empty or invalid support set | KEEP |
| `test_learned_policy_offline_q_retention_ml.py` | M | learned_policy_offline_q | blocked without a declared non ephemeral root; a second freeze at the same key is write once | KEEP |
| `test_learned_policy_offline_q_serving_ml.py` | M | learned_policy_offline_q | eligible and support complete decision uses the learned model; mismatched expected support set fails closed | KEEP |
| `test_learned_policy_offline_q_smoke.py` | P | learned_policy_offline_q | summary aggregates rates across games; matching aggregates and matching actions pass | KEEP |
| `test_learned_policy_offline_q_split_tensors_ml.py` | M | learned_policy_offline_q | split tensors partition the dataset by whole hanchan; terminal rows are marked and present | KEEP |
| `test_learned_policy_offline_q_strength_ml.py` | M | learned_policy_offline_q, single_round_evaluation + related contracts | positive interval is signal; document is canonical json serializable | KEEP |
| `test_learned_policy_offline_q_support.py` | P | learned_policy_offline_q | supported indices come only from train behavior actions; runtime support gate matches the report | KEEP |
| `test_learned_policy_stage2.py` | P | _artifact_io, learned_policy_stage2 + related contracts | installed contracts match the locked stage2 identity; contract identity error is a stage2 error | KEEP |
| `test_learned_policy_stage2_ml.py` | M | learned_policy_stage2, _artifact_io | locked model has the locked parameter count; repeated test exposure classifies as stop invalid | KEEP |
| `test_learned_policy_stage3.py` | P | learned_policy_stage2, learned_policy_stage3 | serving population is the locked one; document reports every counter | KEEP |
| `test_learned_policy_stage3_ml.py` | M | learned_policy_stage2, learned_policy_stage3 | valid fixture checkpoint loads as a stage3 fixture; runtime records cpu only deterministic conditions | KEEP |
| `test_learned_policy_stage4a.py` | P | learned_policy_stage2, learned_policy_stage3 + related contracts | screening population is the locked one; candidate only metrics are not reported as baseline deltas | KEEP |
| `test_learned_policy_stage4a_ml.py` | M | learned_policy_stage2, learned_policy_stage3 + related contracts | retained bundle passes strict readback; the stage3 serving adapter boundary is not bypassed | KEEP |
| `test_lisjong_engine_action_mapping.py` | U | lisjong_engine | every descriptor variant has a translator; constructor rejects non contract keys and values | KEEP |
| `test_lisjong_engine_decision.py` | U | lisjong_engine | pairs a decision context with its mapping; rejects non value arguments | KEEP |
| `test_lisjong_engine_domain_conversion.py` | U | lisjong_engine | maps each engine seat to its lisjong seat; unhashable value fails closed | KEEP |
| `test_lisjong_engine_hanchan_integration.py` | I | lisjong_engine | reaches a completed match; missing seat policy fails before starting a match | KEEP |
| `test_lisjong_engine_policy_input.py` | U | lisjong_engine | self seat comes from the viewer seat; returns a policy input | KEEP |
| `test_lisjong_engine_policy_selector.py` | U | lisjong_engine | returns the original descriptor object; keeps each seats own policy instance | KEEP |
| `test_model.py` | U | model | identity and factory are kept as given; rejects round count mismatch with game count | KEEP |
| `test_mortal_decision_analysis_artifact.py` | U | mortal_decision_analysis_artifact, mortal_decision_comparison | exports every paired decision in canonical order; rejects a missing artifact directory | KEEP |
| `test_mortal_decision_compare.py` | U | mortal_decision_analysis_artifact, mortal_decision_compare + related contracts | cli resolves policy and dispatches dedicated diagnostic; export failure reports and leaves no complete artifact | KEEP |
| `test_mortal_decision_comparison.py` | U | mortal_decision_comparison | representation only physical copy difference is agreement; aggregation and disagreement extraction are deterministic | KEEP |
| `test_mortal_decision_evaluation.py` | U | model, mortal_decision_comparison + related contracts | runs canonical rotations with independent shadow instances; failure returns no partial result and reports rotation | KEEP |
| `test_mortal_mixed_game_runner.py` | U | mortal_runtime, riichienv | routes mortal and three policy seats through distinct paths; cleanup failure after success is reported | KEEP |
| `test_mortal_mixed_game_runner_shadow.py` | U | mortal_decision_comparison, mortal_runtime + related contracts | same observation shadow once and only mortal action is applied; comparison opt out preserves objective result | KEEP |
| `test_mortal_riichienv_integration.py` | I | Mortal/RiichiEnv real boundary | new events batch and mjai action resolution match riichienv 048; illegal mjai action returns none instead of a fallback | KEEP |
| `test_mortal_runtime.py` | U | mortal_runtime | config records resolved model path and sha256; cleanup failure is reported | KEEP |
| `test_mortal_single_round_compare.py` | U | model, mortal_runtime + related contracts | mortal is not registered as a policy; summary records mortal provenance and existing metrics | KEEP |
| `test_mortal_single_round_evaluation.py` | U | model, mortal_runtime + related contracts | runs four rotations per seed with fresh three policy instances; mismatched runner result fails closed | KEEP |
| `test_open_hand_call_diagnostic_artifact.py` | U | model, open_hand_call_diagnostic_artifact + related contracts | strict round trip and deterministic serialization; unknown sidecar field is rejected | KEEP |
| `test_open_hand_call_diagnostic_cli.py` | U | open_hand_call_diagnostics, single_round_compare | serial diagnostic dispatch saves strength then bound sidecar; purpose specific option rejects non exact candidate reference | KEEP |
| `test_open_hand_call_diagnostics.py` | U | model, open_hand_call_diagnostics + related contracts | shadow baseline never replaces candidate action; module does not import private lisjong yaku route helpers | KEEP |
| `test_oracle_sensitivity_main.py` | P | oracle-sensitivity main protocol | preregistered five percent classification boundaries; proxy counts only current live wall copies of effective type | KEEP |
| `test_oracle_sensitivity_pilot.py` | P | oracle-sensitivity pilot | oracle replaces only stable opponents from omniscient state; rejects empty seed set | KEEP |
| `test_package.py` | U | package/import boundaries | version is exposed; ranked module does not import lisjong legacy orchestration | KEEP |
| `test_parallel_execution.py` | U | _parallel_execution, model + related contracts | accepts positive int; workers run in separate spawned processes not forked | KEEP |
| `test_phase05_decision_linked.py` | P | lisjong_engine, oracle_sensitivity_pilot + related contracts | rows are summed into the canonical viewer table; estimator is picklable for reuse | KEEP |
| `test_phase05_estimator.py` | P | phase05_belief_slice | locked backoff hierarchy is strictly coarsening; training cell counts are reported per level | KEEP |
| `test_phase05_experiment.py` | P | phase05_belief_slice | only turn decisions become anchors; experiment rejects a non ruleset argument | KEEP |
| `test_phase05_feature.py` | P | phase05_belief_slice | feature rejects viewer wind as opponent; discard bucket is per opponent not global | KEEP |
| `test_phase05_label.py` | P | lisjong_engine, phase05_belief_slice | result requires exactly one of labels or reason; builder requires engine match state | KEEP |
| `test_phase05_leakage.py` | P | lisjong_engine, phase05_belief_slice | encoder signature takes only a policy input; remaining count never exceeds the viewer safe inventory | KEEP |
| `test_phase05_metrics.py` | P | phase05_belief_slice | exact prediction scores zero error; empty input fails closed | KEEP |
| `test_phase05_sample_split.py` | P | phase05_belief_slice | locked seed ranges match the preregistered experiment; serialization rejects non samples | KEEP |
| `test_phase11_public_riichi_wait_readout.py` | P | phase11_public_riichi_wait_readout, _execution_safety + related contracts | the old fixed absolute tolerance rejects equivalent evidence; cli has no resume hpo rescue or budget override | KEEP |
| `test_phase11_public_riichi_wait_readout_ml.py` | M | phase11_public_riichi_wait_readout, _execution_safety + related contracts | dirty arena worktree rejects lock before artifact readback; result is rederived and tampering is rejected | KEEP |
| `test_phase2_training_anchor.py` | P | lisjong_engine, phase2_training_anchor | turn anchor freezes observation and ordered evidence; deferral rationale is documented in the label module | KEEP |
| `test_phase3_bootstrap_corpus.py` | P | phase2_training_anchor, phase3_bootstrap_corpus | fixed protocol constants are locked; run local fields are absent from canonical content | KEEP |
| `test_phase3_bootstrap_corpus_negative_paths.py` | P | phase3_bootstrap_corpus | unknown schema version is rejected; tampered evidence coverage count is rejected | KEEP |
| `test_phase4_raw_corpus.py` | P | phase2_training_anchor, phase4_raw_corpus | fixed protocol identity is locked; viewer private draw leakage and public stream mismatch reject | KEEP |
| `test_phase5_belief_dataset.py` | P | phase2_training_anchor, phase4_raw_corpus + related contracts | turn only compact references preserve one anchor three rows; metrics preserve source game partition and wait is coverage only | KEEP |
| `test_phase6_snapshot_feature.py` | P | phase2_training_anchor, phase6_snapshot | formal identity axis and opponent wind order are fixed; broken response epoch fails closed | KEEP |
| `test_phase6_snapshot_ml.py` | M | phase4_raw_corpus, phase5_belief_dataset + related contracts | constraint marginals zero axes gradients and rejections; phase5 and common expected count metric values are equal | KEEP |
| `test_phase7_snapshot_ml.py` | M | phase4_raw_corpus, phase5_belief_dataset + related contracts | exact frozen artifact spec passes and any mismatch fails closed; test only inference is cpu eval no grad on synthetic partition | KEEP |
| `test_phase7_snapshot_test.py` | P | phase2_training_anchor, phase4_raw_corpus + related contracts | phase6 seal and phase7 test only guard preserve values; result artifact is atomic immutable and does not touch phase6 | KEEP |
| `test_phase8_sequential.py` | P | phase5_belief_dataset, phase8_sequential | exact grouping checkpoint order and reset boundaries; normal phase8 import and cli contract are torch free and test free | KEEP |
| `test_phase8_sequential_ml.py` | M | phase4_raw_corpus, phase5_belief_dataset + related contracts | models have exact fixed shapes and reuse phase6 constraint; artifact identity strict load test false and overwrite refusal | KEEP |
| `test_phase9_confirmatory.py` | P | phase4_raw_corpus, phase5_belief_dataset + related contracts | locked population and bootstrap configuration; generation report binds runtime and preflight | KEEP |
| `test_phase9_confirmatory_ml.py` | M | phase4_raw_corpus, phase5_belief_dataset + related contracts | self rollout uses public initialization wind remap and no labels; s2 latent resets for each phase9 sequence and is deterministic | KEEP |
| `test_policy_catalog.py` | U | _parallel_execution, model + related contracts | catalog has exactly seven registered policies; yakuhai call vs extended combined resolves on parallel path | KEEP |
| `test_policy_performance.py` | U | model, policy_performance + related contracts | records exactly one duration per call using the injected clock; module does not reference the parallel evaluation entry point | KEEP |
| `test_policy_performance_integration.py` | I | model, policy_performance + related contracts | timing and profile modes preserve the objective execution result; profile mode observes first party lisjong hotspots | KEEP |
| `test_policy_performance_profile_cli.py` | U | model, policy_catalog + related contracts | candidate and baseline choices are first party only; report shows all rows when top n covers every stat | KEEP |
| `test_policy_reference.py` | U | policy_catalog, policy_reference + related contracts | existing catalog alias resolves to existing spec; explicit reference cannot reuse mechanism identity on either side | KEEP |
| `test_progress_reporting.py` | U | _parallel_execution, model + related contracts | formats completed elapsed and eta on one rewritten line; failure with progress does not print partial success summary | KEEP |
| `test_riichienv_0410_compatibility.py` | I | RiichiEnv 0.4.10 runtime | runtime identity and observation serialization; call kan and ron candidates use physical tiles | KEEP |
| `test_riichienv_adapter_action_mapping.py` | U | riichienv | matches riichienv 0 4 8 full scan; many real decisions never raise adapter error | KEEP |
| `test_riichienv_adapter_integration.py` | I | riichienv | builds policy input across multiple kyoku without failure; mapping rejects cross seat and unmapped policy results | KEEP |
| `test_riichienv_adapter_materialized_state.py` | U | riichienv | rejects non seat; trackers for different seats do not share mutable state | KEEP |
| `test_riichienv_adapter_policy_input.py` | U | riichienv | accepts captured new events without reading observation again; cross seat trackers stay independent | KEEP |
| `test_riichienv_adapter_seat_conversion.py` | U | riichienv | valid indices map to corresponding seat; rejects out of range int | KEEP |
| `test_riichienv_adapter_tile_conversion.py` | U | riichienv | manzu ids map to expected ranks; rejects non str | KEEP |
| `test_riichienv_local_game_inspection.py` | U | game_trace, riichienv | opt out keeps execute policy path and mjai log reads; processing and completion failures leave no completed snapshot | KEEP |
| `test_riichienv_local_game_runner.py` | U | game_trace, riichienv | normalizes scores and ranks to tuples; completes only after result construction succeeds | KEEP |
| `test_riichienv_local_game_runner_integration.py` | I | durable_local_game_record, riichienv + related contracts | fixed seed single game completes and is reproducible | KEEP |
| `test_riichienv_round_result.py` | U | riichienv | single round win captures one complete round result; discontinuous round scores fail closed | KEEP |
| `test_riichienv_round_result_integration.py` | I | durable_local_game_record, riichienv + related contracts | single round game captures exactly one round; half game round results survive the durable boundary | KEEP |
| `test_riichienv_round_stats.py` | U | riichienv | accepts neutral stats and derives score delta; abortive draw does not mark exhaustive draw | KEEP |
| `test_riichienv_round_stats_integration.py` | I | riichienv | ron win points and deal in match pure hand value; first tenpai turn values are sane across several seeds | KEEP |
| `test_riichilab_adapter.py` | U | riichienv, riichilab | rejects non seat self seat; possible actions mismatch produces no payload | KEEP |
| `test_riichilab_adapter_errors.py` | U | riichilab | riichilab adapter error is a direct exception subclass; all adapter errors are arena local | KEEP |
| `test_riichilab_cli.py` | U | riichilab | profile is required; canonical symbols are arena local | KEEP |
| `test_riichilab_continuous_ranked.py` | U | riichilab | backoff sequence matches baseline; keyboard interrupt from asyncio run exits 0 and is secret safe | KEEP |
| `test_riichilab_corpus_acquisition.py` | U | riichilab_corpus | three bot documents produce deduplicated current snapshot; cache change after plan requires replan | KEEP |
| `test_riichilab_corpus_api_validation.py` | U | riichilab_corpus | extracts only minimized target participation; critical lifecycle and metadata join failures | KEEP |
| `test_riichilab_corpus_http.py` | U | riichilab_corpus | success and empty success; timeout connection failure and retry exhaustion | KEEP |
| `test_riichilab_dev_profile_policy.py` | U | riichilab | lisjong dev maps to exact mechanism policy; validation cli passes exact mechanism policy to validator | KEEP |
| `test_riichilab_downstream_qualification.py` | P | phase2_training_anchor, riichilab_corpus + related contracts | opponent initial hands are not projected; qualification package imports no network module | KEEP |
| `test_riichilab_durable_ranked_game_record.py` | U | _artifact_io, durable_local_game_record + related contracts | completed record round trips through the strict loader; failed record acquisition exits non zero | KEEP |
| `test_riichilab_errors.py` | U | riichilab | protocol error is a riichilab client error; all client errors are arena local | KEEP |
| `test_riichilab_mjai_response.py` | U | riichilab | discard response includes tsumogiri from selected action; tsumo response targets the actor itself | KEEP |
| `test_riichilab_possible_action_validation.py` | U | riichilab | accepts the official minimal dahai candidate shape; dahai without pai field is treated as malformed | KEEP |
| `test_riichilab_profile.py` | U | riichilab | exactly three known profiles; runtime profile instances are independent frozen values | KEEP |
| `test_riichilab_ranked.py` | U | riichilab | exact six fields are preserved; ranked module uses arena local lower level runtime | KEEP |
| `test_riichilab_request_action.py` | U | riichilab | accepts a well formed request action; rejects undecodable observation | KEEP |
| `test_riichilab_session.py` | U | riichilab | binds seat 0; invalid score element reports shape without values | KEEP |
| `test_riichilab_session_adapter_integration.py` | I | riichienv, riichilab | validation session reaches real adapter and policy; adapter error raised through session is not a riichilab client error | KEEP |
| `test_riichilab_source_pilot.py` | P | _artifact_io, learned_policy_input + related contracts | row budget is the locked 190 s20 population; a non directory bundle fails closed | KEEP |
| `test_riichilab_source_pilot_ml.py` | M | riichilab_source_pilot, single_round_artifact + related contracts | materialized tensors have the locked shapes; evaluation failure is durable and not rerunnable | KEEP |
| `test_riichilab_trace.py` | U | riichilab | record writes a single valid json line; constructor accepts only a path | KEEP |
| `test_riichilab_transport.py` | U | riichilab | receives and dispatches json text frames; no token or authorization key ever reaches the trace writer | KEEP |
| `test_riichilab_validation.py` | U | riichilab | exact seven fields are preserved; validation module uses arena local lower level runtime | KEEP |
| `test_single_round_artifact.py` | U | artifact, model + related contracts | round trip returns a factory free immutable snapshot; game result rejects scores inconsistent with round stats | KEEP |
| `test_single_round_compare.py` | U | model, policy_catalog + related contracts | single seed; failed evaluation does not write an artifact | KEEP |
| `test_single_round_evaluation.py` | U | model, riichienv + related contracts | plan does not accept a game mode argument; game mode mismatch is rejected | KEEP |
| `test_single_round_evaluation_integration.py` | I | model, single_round_evaluation | fixed seed single round evaluation is reproducible | KEEP |
| `test_single_round_evaluation_mahjong_metrics.py` | U | model, riichienv + related contracts | round count; an empty population is rejected | KEEP |
| `test_single_round_evaluation_parallel.py` | U | _parallel_execution, model + related contracts | result order does not depend on outcome dict iteration order; parallel metrics use the shared aggregation function | KEEP |
| `test_single_round_evaluation_parallel_integration.py` | I | model, policy_catalog + related contracts | serial and parallel workers agree with real riichienv; explicit reference runs through real spawn workers | KEEP |
| `test_stage3_entry_gate.py` | P | phase2_training_anchor, phase4_raw_corpus + related contracts | stage3 package never imports resource at module scope; distinct base games produce distinct population datasets | KEEP |
| `test_stage3_entry_gate_ml.py` | M | phase8_sequential, stage3_entry_gate | training uses the locked phase8 s2 family and budget; manifest rejects a test evaluated artifact | KEEP |
| `test_stage3_epoch_budget.py` | P | phase4_raw_corpus, stage3_epoch_budget + related contracts | the locked identities are the issue values; the phase10 split is unchanged | KEEP |
| `test_stage3_epoch_budget_ml.py` | M | phase8_sequential, stage3_epoch_budget + related contracts | the longer budget reproduces the shorter history exactly; the locked budget config is eighty epochs at patience six | KEEP |
| `test_stage3_kan_coverage.py` | P | phase4_raw_corpus, phase5_belief_dataset + related contracts | explicit import reference resolves to the coverage policy; the result keeps the next step boundary | KEEP |
| `test_stage3_mix_pilot.py` | P | phase2_training_anchor, phase4_raw_corpus + related contracts | ordered seeds are the locked fresh contiguous range; matrix requires every arm exactly once | KEEP |
| `test_stage3_mix_pilot_ml.py` | M | phase8_sequential, stage3_entry_gate + related contracts | training uses the locked phase8 s2 family and budget; manifest records execution source revisions | KEEP |
| `test_stage3_optimization_saturation.py` | P | phase4_raw_corpus, stage3_epoch_budget + related contracts | the locked phase10 identities are the issue values; the phase10 split is unchanged | KEEP |
| `test_stage3_optimization_saturation_ml.py` | M | phase4_raw_corpus, phase8_sequential + related contracts | the doubled budget reproduces the shorter history exactly; missing or extra files are rejected | KEEP |
| `test_stage3_scale_learning_curve.py` | P | learned_policy_offline_q, phase2_training_anchor + related contracts | the preferred 354 range collides with the 140 replacement test; the cli offers no test partition or rescue option | KEEP |
| `test_stage3_scale_learning_curve_ml.py` | M | phase4_raw_corpus, phase5_belief_dataset + related contracts | an execution revision mismatch is never allowed silently; the end to end curve produces an exhaustive outcome | KEEP |
| `test_strength_evaluation.py` | U | _artifact_io, _execution_safety + related contracts | minimum supported spec parses and normalizes threshold; failure is nonzero machine readable and has no stdout | KEEP |
| `test_summarize_single_round_artifacts.py` | U | single_round_compare, summarize_single_round_artifacts | summarizes one artifact; no artifact path is rejected by argparse | KEEP |

## Fixture and helper inventory

These 30 underscore-prefixed modules are not test modules and contain no discovered `test_` cases. They construct concrete fixtures for the named family; their target column is derived from their imports.

| Module | Shared fixture responsibility / production targets |
| --- | --- |
| `_learned_policy_input_fixtures.py` | learned policy input fixtures |
| `_learned_policy_offline_q_artifact_fixtures.py` | learned_policy_offline_q |
| `_learned_policy_offline_q_diagnosis_fixtures.py` | learned_policy_input, learned_policy_offline_q |
| `_learned_policy_offline_q_fh_curriculum_fixtures.py` | learned_policy_offline_q, model + related contracts |
| `_learned_policy_offline_q_fixtures.py` | game_trace, learned_policy_offline_q + related contracts |
| `_learned_policy_offline_q_p1_gate_a_fixtures.py` | learned_policy_offline_q |
| `_learned_policy_offline_q_p1_gate_b_fixtures.py` | learned_policy_offline_q, model + related contracts |
| `_learned_policy_offline_q_p1_shanten_guard_fixtures.py` | learned_policy_offline_q, model + related contracts |
| `_learned_policy_stage2_fixtures.py` | learned_policy_stage2 |
| `_learned_policy_stage3_fixtures.py` | _artifact_io, learned_policy_stage2 + related contracts |
| `_learned_policy_stage4a_fixtures.py` | learned_policy_stage3, learned_policy_stage4a + related contracts |
| `_lisjong_engine_fixtures.py` | lisjong engine fixtures |
| `_mortal_decision_analysis_fixtures.py` | model, mortal_decision_comparison + related contracts |
| `_phase05_fixtures.py` | phase05_belief_slice |
| `_phase2_anchor_fixtures.py` | lisjong_engine, phase2_training_anchor |
| `_phase3_bootstrap_fixtures.py` | phase2_training_anchor, phase3_bootstrap_corpus |
| `_phase4_raw_corpus_fixtures.py` | phase2_training_anchor, phase4_raw_corpus |
| `_riichilab_corpus_fixtures.py` | riichilab corpus fixtures |
| `_riichilab_downstream_qualification_fixtures.py` | riichilab downstream qualification fixtures |
| `_riichilab_source_pilot_fixtures.py` | riichienv |
| `_round_result_fixtures.py` | riichienv |
| `_round_stats_fixtures.py` | model, riichienv |
| `_single_round_artifact_fixtures.py` | model, policy_catalog + related contracts |
| `_source_pilot_strength_fixtures.py` | model, riichilab_source_pilot + related contracts |
| `_stage3_entry_gate_fixtures.py` | phase4_raw_corpus, phase5_belief_dataset |
| `_stage3_epoch_budget_fixtures.py` | stage3_epoch_budget, stage3_scale_learning_curve |
| `_stage3_kan_coverage_fixtures.py` | lisjong_engine, phase4_raw_corpus + related contracts |
| `_stage3_mix_pilot_fixtures.py` | phase4_raw_corpus, phase5_belief_dataset + related contracts |
| `_stage3_optimization_saturation_fixtures.py` | stage3_optimization_saturation, stage3_scale_learning_curve |
| `_stage3_scale_learning_curve_fixtures.py` | phase4_raw_corpus, phase5_belief_dataset + related contracts |

Inventory totals: U=67, I=12, M=32, P=50, S=1; 162 test modules and 30 fixture/helper modules.
