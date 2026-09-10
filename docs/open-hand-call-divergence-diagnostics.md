# Open-hand call decision divergence diagnostics

Issue #196のdiagnosticsは、`OpenHandYakuAwareCallPolicy`とcurrent strength
baseline `yakuhai-call`のdecision divergenceが、ABBB / `4p-red-single`の
zero-delta seed blocksへどう関係するかをboundedに観測するinstrumentationである。
Policy改善、strength metric、baseline promotion判定ではない。

## Observation semantics

各gameのcandidate seatだけについて、actual candidateとfreshなshadow
`yakuhai-call`を**同じimmutable `DecisionContext`**で実行する。RiichiEnvへmappingし
game progressionへ適用するのはactual candidate actionだけであり、shadow actionは
比較後に破棄する。legal actions、candidate action、RiichiEnv state、game result、
ABBB rotation、canonical strength aggregationは変更しない。

集計するprimary diagnosticsは次のとおり。

- candidate-seat decision総数、same / divergent decision数、divergence rate
- shared-prefix initial-call opportunity数
- baseline Pass -> candidate Chi / Pon数（candidate-only Chi / Pon）
- divergenceを1件以上含むgame数とseed-block数
- score deltaがnonzeroの全seed-block数
- divergent seed blocksに限ったpositive / zero / negative score-delta block数

secondaryとして、divergent gameあたりの平均divergence数とcandidate seat別の
divergent decision数も保存する。route別countは、lisjongのpublic / stable seamが
存在しないため取得しない。Arenaからprivate yaku-route helperをimportして再判定しない。

### Shared-prefix opportunityとaccepted divergence

shared-prefix initial-call opportunityは、candidateとshadow baselineがまだ一度も
divergeしていない共有trajectory上で、次を同時に満たすdecisionである。

```text
shadow baseline action == Pass
AND legal_actions contains ChiAction or PonAction
```

candidate-only accepted divergenceは、shared prefixかどうかにかかわらず、shadow
baselineがPassしactual candidate actionがChiまたはPonになったdecisionである。
shared prefix上でcandidateもPassしたdecisionはopportunityには数えるが、accepted
divergenceには数えない。これにより、legal opportunity自体の希少性とcandidate
eligibilityによるfilteringを混同しない。

first divergenceを生んだdecision自身はshared prefixに含めるが、その後の
candidate-only trajectoryにあるdecisionはopportunity denominatorへ含めない。一方、
後続のdivergenceおよびcandidate-only Chi / Ponは診断値として引き続き数える。

## Persistence and provenance

既存strength artifact schemaは変更しない。diagnosticsはpurpose-specificなversion 1
sidecar JSONとして保存し、次へstrictにbindする。

- strength artifactのSHA-256、schema version、evaluation protocol
- candidate / baseline identity、ordered seeds、rotation、game mode、max steps
- Arena、lisjong、lisjong-engineのexact revisionsとruntime provenance
- canonical `(seed, rotation)`順のper-game aggregate diagnostics

sidecar load時はreferenced strength artifactをstrict readbackし、digest、plan、provenance、
game order、candidate seat、score deltaを照合する。unknown fields、summary tampering、
record tampering、provenance不一致はfail closedする。

diagnostic CLIはevaluation開始前にprovenanceをpreflightする。lisjong / lisjong-engineは
full Git commit IDを持つVCS install metadataが必要であり、Arenaはclean Git worktreeの
exact HEADでなければならない。lisjongは次のcandidate merge revisionへexactに固定する。

```text
fd9d87efd7c0563b990320f3cc12495aed418be4
```

## Locked evidence boundary

`22700..22799`の400-game runは完走したが、lisjong VCS metadata不足によりartifact
保存に失敗した。これはexploratory observed resultであり、provenance-complete immutable
artifactでもformal strength evidenceでもない。このrangeは再実行しない。

fresh rangeは`22800..22899` inclusive、4 rotations、400 games、8 workersへlock済みで
ある。instrumentationの実装・review中は、全体・部分・smokeを問わず実行しない。
merge後、clean `main`とVCS metadata付きexact dependenciesをユーザー側環境で確認して
から一度だけ実行する。

```powershell
python -m lisjong_arena.single_round_compare `
  --candidate lisjong.policies:OpenHandYakuAwareCallPolicy `
  --candidate-id open-hand-yaku-aware-call `
  --baseline yakuhai-call `
  --seeds 22800:22899 `
  --workers 8 `
  --progress `
  --artifact-out <new-strength-artifact.json> `
  --open-hand-call-diagnostics-out <new-diagnostic-sidecar.json>
```

`84% zero seed blocks`だけからeligibilityが保守的すぎるとは結論しない。fresh resultは
shared-prefix opportunity、candidate-only call、divergence coverage、divergent block
outcomeの順で
読み、その後にeligibility bottleneckの有無を判断する。同一rangeのrerun、seed追加による
救済、Policy変更、strength promotionはこのdiagnosticから自動的に行わない。
