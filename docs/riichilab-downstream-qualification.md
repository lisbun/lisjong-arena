# RiichiLab corpus downstream reconstruction qualification

Issue #203のtoolは、Issue #170で取得済みのimmutableなRiichiLab corpusを、
**player-safeなdecision-time reconstruction**へ変換できるかどうかをofflineで技術判定します。
training、新規acquisition、network accessは一切行いません。

```text
exact immutable #170 corpus
        |
        v
player-safe deterministic replay
        |
        v
behavior supervision qualification
+
hidden-state supervision qualification
        |
        v
coverage / leakage / provenance report
        |
        v
parent handoff only
```

## Source boundary

処理開始前に、local corpusの`corpus_identity`と`manifest_sha256`の両方が、Issue #203で
固定された値と完全一致することを確認します。値は
`lisjong_arena.riichilab_downstream_qualification.qualification`のmodule constantとして
固定しており、caller-configurableにしません。

```text
corpus_identity
064b949733b13026bdbe951c9f69b90653c5977e894449d23f99ce830718b865

manifest_sha256
378edce0a3a117abd47e1f400e92c59de52be2fb990947865c08d815b2f9b0ca
```

不一致、またはlocal cacheがsnapshotを網羅していない場合は`STOP / INVALID`です。newer snapshotへの
置換、raw recordのsilent repair、partial fallbackは行いません。identity gateを通過するまで、
cached gameのbytesを読みません。

Issue #170から継承するcompliance boundary（personal non-commercial利用はGO、
raw-log redistributionとpublic raw datasetはNO-GO）は本toolでも変わりません。

## Reconstruction contract

server-side MJAI recordは`start_kyoku.tehais`や他家の`tsumo.pai`といったomniscient fieldを
持ちます。そのため、player-safe stateはraw eventを直接読まず、必ずviewerごとのprojectionを
経由します。

```text
raw MJAI event
    -> project_visible_event(event, viewer)
    -> VisibleEvent
    -> PlayerSafeRoundState.apply()      # 前向きに1回だけ
    -> decision到達時にsnapshotをfreeze
```

player-safe channelへ流さないもの:

```text
他家のconcealed hand
future wall / future draw / future action
terminal result / future score change
server-only hidden truth（裏dora、終局時公開手牌等）
```

completed gameやfinal stateから過去decisionを逆算する経路はprimary implementationにしません。
decision pointのsnapshotは、そのactionが観測される直前にfreezeします。

### decision pointの存在証明

server logへ現れないdecisionは、current Arenaのrules semanticsだけではexactに存在証明できません。
推測で補完せず、実際に観測されたactionだけをdecision pointとして計上します。

| decision kind | 観測条件 |
| --- | --- |
| `turn` | 自身の`tsumo`直後 |
| `post_call_discard` | 自身の`chi` / `pon`直後 |
| `riichi_discard` | 自身の`reach`宣言直後 |
| `call_response` | 他家の`dahai` / `kakan`に対するcall / ron / 明示`none`が観測された場合 |

## Behavior supervision（Surface A）

観測されたMJAI actionを、current lisjong canonical `InternalAction` semanticsへ対応付けられるかを
判定します。計測するaction familyは次のとおりです。

```text
discard_tedashi / discard_tsumogiri
reach
chi / pon / daiminkan / ankan / kakan
pass
ron / tsumo
kyuushu_kyuuhai
```

- 赤5は`5mr`等のidentityを保持したまま扱い、通常5へ潰しません。
- chi / pon / daiminkanは、直前のdiscard triggerと`target` / 打牌が一致することまで確認します。
- kakanはadded tileだけで元Ponを推測せず、current meld snapshotとserver recordの`consumed`の
  両方で一意性を確認します。
- ron / tsumoは`hora`の`actor` / `target` fieldだけに依存せず、直前のtrigger contextと和了牌の
  一致まで確認します。
- exact対応付けができないdecisionは、silent dropせずreason code付きでunsupportedとして計数します。

## Hidden-state supervision（Surface B）

player-safe snapshotが完成したあと、同じdecision identityへtraining-only truthをjoinします。
truthの正本は全4 seat分のplayer-safe stateが保持する「そのseat自身の手牌」であり、
player-safe channelへは書き戻しません。

derivationは既存contractを再利用します。

```text
phase2_training_anchor.training_labels.expected_counts_for_concealed_hand()
    opponent concealed tile multiset / counts + 赤5 identity

phase2_training_anchor.training_labels.structural_wait_for_hand()
    stable 13-equivalentのときだけexact structural wait mask
```

structural tenpaiは、そのmaskからのみ導出します。maskがunavailableな場合の値は`None`であり、
「非聴牌」とは区別します。次のsemantic boundaryを維持します。

```text
realized hidden hand   != posterior
structural wait        != ron-legal wait != deal-in probability != hand EV
```

tile conservationは、concealed hand / meld / 未鳴きdiscard / 公開dora indicatorに現れる
同一base kindが物理上限の4枚を超えないことを確認します。concealed sizeは、meld調整後の
hand sizeが13 / 14であり、14が高々1 seatであることを確認します。

## Coverage limitation

次はserver logとcurrent Arenaのrules semanticsからexactに再構成できません。推測補完を禁止する
代わりに、report artifactの`coverage_limitations`として明示します。

- exact legal action set。current repositoryにexact legal-action reconstruction seamが存在せず、
  本Issueで新しい麻雀rules engineをArenaへ実装しません。`legal_action_sets_exactly_reconstructed`は
  常に`0`であり、reasonは`no_exact_legal_action_reconstruction_seam`です。
- callしなかった局面のpass機会。明示`none` actionが記録されている場合だけdecisionとして観測できます。
- decision pointはtarget botが座っているseatについてのみ再構成します。

これらはobserved decisionのfailureではないため、surface分類の入力にしません。

## Classification rule

分類は`lisjong_arena.riichilab_downstream_qualification.classification`が唯一の正本です。

Surface A（behavior supervision）:

```text
NOT QUALIFIED
    exact mappingが0件、またはleakage / replay consistency failureが1件以上
QUALIFIED
    上記に該当せず、unsupported gameが0、unsupported actionが0、
    exact mapping件数 == player-safe decision point件数
PARTIAL
    それ以外
```

Surface B（hidden-state supervision）:

```text
NOT QUALIFIED
    exact opponent truthを持つdecisionが0件、またはleakage / replay consistency failureが1件以上
QUALIFIED
    上記に該当せず、unsupported gameが0、tile conservation failureが0、
    concealed-size consistency failureが0、
    exact opponent truth件数 == player-safe decision point件数
PARTIAL
    それ以外
```

`structural_wait`のunsupported（stable 13-equivalentでないstate）はsemantic availability条件で
あり、failureではありません。これを理由にsurfaceを降格しません。

overall outcomeは次の対応で必ずちょうど1つに決まります。

| behavior | hidden-state | overall outcome |
| --- | --- | --- |
| QUALIFIED | QUALIFIED | `BOTH SURFACES TECHNICALLY QUALIFIED` |
| QUALIFIED | NOT QUALIFIED | `BEHAVIOR SUPERVISION ONLY QUALIFIED` |
| NOT QUALIFIED | QUALIFIED | `HIDDEN-STATE SUPERVISION ONLY QUALIFIED` |
| NOT QUALIFIED | NOT QUALIFIED | `NO DOWNSTREAM SURFACE QUALIFIED` |
| 上記以外（いずれかがPARTIAL） | | `DOWNSTREAM RECONSTRUCTION PARTIAL` |

`STOP / INVALID`はsource identity gateだけが返すoutcomeであり、surface分類とは組み合わせません。
このとき報告されるのはstop reasonだけで、measurementsは生成しません。

## Fail closedの粒度

```text
corpus / manifest identity不一致    -> STOP / INVALID（run全体）
game単位のstate machine違反         -> そのgameをunsupportedとして計上し、
                                      そのgameのdecisionは採用しない
decision単位のmapping不能           -> unsupported reasonとして計上する
```

unsupported gameが1件でもあれば、両surfaceともQUALIFIEDにはなりません。成功分だけを採って
QUALIFIEDへ寄せる経路はありません。

## Operator flow (post-merge only)

以下はPR merge後にoperatorがlocalで実行する想定です。本PR / CIではlive commandを実行せず、
実corpusにも触れません。すべてofflineであり、`snapshot` / `acquire`とHTTP transportは
呼ばれません。

```powershell
$root = "C:\private-research\riichilab-corpus"

# 1. exact #170 corpus identityの確認から開始する。
python -m lisjong_arena.riichilab_corpus validate `
  --snapshot "$root\snapshot.json" `
  --output-dir "$root\cache"
```

`validate`の出力で、`corpus_identity`と`manifest_sha256`がIssue #203の固定値と一致することを
先に目視確認します。一致しない場合はここで停止し、qualificationを実行しません。

```powershell
# 2. offline qualificationを実行する。
python -m lisjong_arena.riichilab_corpus qualify-downstream `
  --snapshot "$root\snapshot.json" `
  --output-dir "$root\cache" `
  --report-output "$root\reports\downstream-qualification.json"
```

- `--output-dir`はlocal cache（read only）です。
- `--report-output`はGit worktree外である必要があります。既存fileは上書きしません。
- exit codeは`STOP / INVALID`のとき`1`、それ以外は`0`です。

### Issueへ転記するcompact summary

report JSONから次の項目をIssue #203へ転記します。raw MJAI event、concealed hand、bot表示名等の
raw third-party contentはreportにもIssueにも載せません。

```text
corpus_identity / manifest_sha256（expected値との一致）
games_processed / games_replayable / games_unsupported (+ reason別件数)
rounds_processed
player_safe_decision_points
target_bot_supervised_decision_points
decision_points_per_target_bot
decision_points_per_decision_kind
action_family_counts
unsupported_actions (+ reason別件数)
open_hand / closed_hand decision points
riichi / non_riichi decision points
shared_games / shared_game_participations
legal_action_sets_exactly_reconstructed (+ unsupported reason)
leakage_check_failures / replay_consistency_failures

decision_points_with_exact_opponent_truth
opponent_rows
concealed_size_consistency_failures
tile_conservation_failures
structural_wait_exact_rows (+ unsupported reason別件数)
structural_tenpai_exact_rows / structural_tenpai_true_rows
red_five_holding_rows

behavior_classification
hidden_state_classification
overall_outcome
coverage_limitations
```

## Parent handoff

技術的qualificationはtrainingのactivationではありません。結果は次へ引き渡すだけです。

```text
#45   Behavior-supervision evidenceを、将来のP8 source実験の判断材料にする
#36   Hidden-state-supervision evidenceを、optionalなfuture source qualificationとして記録する
      （active Stage 3 populationは変更しない）
#37 / #50   それぞれのactivation prerequisiteを満たすまでinformational
```

本Issueでは、Learned PolicyとHandBeliefの優先順位を決めず、downstream training childも作りません。
