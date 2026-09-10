"""local #170 corpusに対するoffline downstream reconstruction qualification。

Issue #203のsource boundaryをここで固定する。処理開始前にlocal corpusの
`corpus_identity`と`manifest_sha256`の両方が、Issue #203で固定された値と完全に
一致することを確認する。一致しない場合は`STOP / INVALID`であり、newer snapshot
への置換、raw recordのsilent repair、partial fallbackはいずれも行わない。

```text
local cache index + snapshot-specific manifest
    -> corpus_identity / manifest_sha256の厳密一致確認
    -> （ここで初めて）cached bytesのreplay開始
    -> per-game forward replay
    -> aggregate measurements
    -> surface classification + exactly one overall outcome
```

このmoduleはoffline専用である。`StdlibHttpTransport`、`snapshot_recent_games()`、
`acquire_from_plan()`を含むnetwork acquisition pathを一切importしない。読むのは
既にlocalへ保存済みのcache index / manifest / compressed bytesだけである。
"""

from collections import Counter
from pathlib import Path

from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichilab_corpus.models import (
    CorpusError,
    Participation,
    RecentGamesSnapshot,
    utc_now_text,
)
from lisjong_arena.riichilab_corpus.persistence import (
    GAMES_DIRECTORY,
    ensure_outside_git_worktree,
    group_participations,
    load_cache_index,
    validate_cache_entry,
    validate_manifest_file,
)
from lisjong_arena.riichilab_corpus.validation import parse_jsonl_gzip
from lisjong_arena.riichilab_downstream_qualification.behavior import ActionFamily
from lisjong_arena.riichilab_downstream_qualification.classification import (
    OverallOutcome,
    classify_behavior_surface,
    classify_hidden_state_surface,
    combine_overall_outcome,
)
from lisjong_arena.riichilab_downstream_qualification.mjai_events import (
    UnsupportedReason,
)
from lisjong_arena.riichilab_downstream_qualification.replay import (
    GameReplayResult,
    ObservedDecision,
    replay_game,
)
from lisjong_arena.riichilab_downstream_qualification.report import (
    BehaviorMeasurements,
    DownstreamQualificationReport,
    HiddenStateMeasurements,
)

# Issue #170で完了し、Issue #170 / #203が固定したsource identity。
# caller-configurableにしない。ここを可変にすると、qualificationがどのcorpusに
# 対する証拠なのかがreportから決まらなくなる。
EXPECTED_CORPUS_IDENTITY = (
    "064b949733b13026bdbe951c9f69b90653c5977e894449d23f99ce830718b865"
)
EXPECTED_MANIFEST_SHA256 = (
    "378edce0a3a117abd47e1f400e92c59de52be2fb990947865c08d815b2f9b0ca"
)

# server logとcurrent Arenaのrules semanticsからはexactに再構成できず、
# 推測補完も禁止されている範囲。observed decisionのfailureではなく、
# qualification結果の適用範囲を限定するcoverage limitationとして報告する。
COVERAGE_LIMITATIONS = (
    "exact legal action set is not reconstructable from the server log alone; "
    "no mahjong rules engine is introduced in this Issue",
    "pass decision existence is not provable from the server log unless an "
    "explicit none action is recorded",
    "decision points are reconstructed only for seats occupied by a target bot",
)

LEGAL_ACTION_SET_UNSUPPORTED_REASON = "no_exact_legal_action_reconstruction_seam"


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def _stop_report(
    snapshot: RecentGamesSnapshot,
    *,
    stop_reason: str,
    corpus_identity: str | None = None,
    manifest_sha256: str | None = None,
) -> DownstreamQualificationReport:
    return DownstreamQualificationReport(
        generated_at=utc_now_text(),
        snapshot_identity=snapshot.snapshot_identity,
        corpus_identity=corpus_identity,
        manifest_sha256=manifest_sha256,
        expected_corpus_identity=EXPECTED_CORPUS_IDENTITY,
        expected_manifest_sha256=EXPECTED_MANIFEST_SHA256,
        overall_outcome=OverallOutcome.STOP_INVALID,
        stop_reason=stop_reason,
        behavior_classification=None,
        hidden_state_classification=None,
        behavior=None,
        hidden_state=None,
        coverage_limitations=COVERAGE_LIMITATIONS,
    )


def _target_seats(
    participations: tuple[Participation, ...],
) -> dict[Seat, int]:
    """1 gameのtarget participationをseat -> bot_idへjoinする。

    同じseatへ複数のtarget botがjoinする入力はfail closedする。
    """
    seats: dict[Seat, int] = {}
    for item in participations:
        seat = Seat(item.seat)
        if seat in seats:
            raise CorpusError(
                f"game {item.game_id} joins multiple target bots to one seat"
            )
        seats[seat] = item.bot_id
    if not seats:
        raise CorpusError("a game must have at least one target participation")
    return seats


class _Aggregate:
    """replay結果をstreamingで畳み込むmutable accumulator。

    per-decision rowを保持しない。1 gameを処理したらそのgameのdecisionは
    捨てる（report artifactもaggregateだけを持つ）。
    """

    __slots__ = (
        "games_processed",
        "games_replayable",
        "games_unsupported",
        "game_unsupported_reasons",
        "rounds_processed",
        "decision_points",
        "supervised_decision_points",
        "per_bot",
        "per_kind",
        "families",
        "unsupported_actions",
        "unsupported_action_reasons",
        "open_hand",
        "closed_hand",
        "riichi",
        "non_riichi",
        "shared_games",
        "shared_game_participations",
        "leakage_failures",
        "consistency_failures",
        "hidden_decision_points",
        "hidden_exact_decision_points",
        "opponent_rows",
        "concealed_size_failures",
        "tile_conservation_failures",
        "structural_wait_exact_rows",
        "structural_wait_unsupported_rows",
        "structural_tenpai_true_rows",
        "red_five_rows",
    )

    def __init__(self) -> None:
        self.games_processed = 0
        self.games_replayable = 0
        self.games_unsupported = 0
        self.game_unsupported_reasons: Counter[str] = Counter()
        self.rounds_processed = 0
        self.decision_points = 0
        self.supervised_decision_points = 0
        self.per_bot: Counter[str] = Counter()
        self.per_kind: Counter[str] = Counter()
        self.families: Counter[str] = Counter()
        self.unsupported_actions = 0
        self.unsupported_action_reasons: Counter[str] = Counter()
        self.open_hand = 0
        self.closed_hand = 0
        self.riichi = 0
        self.non_riichi = 0
        self.shared_games = 0
        self.shared_game_participations = 0
        self.leakage_failures = 0
        self.consistency_failures = 0
        self.hidden_decision_points = 0
        self.hidden_exact_decision_points = 0
        self.opponent_rows = 0
        self.concealed_size_failures = 0
        self.tile_conservation_failures = 0
        self.structural_wait_exact_rows = 0
        self.structural_wait_unsupported_rows: Counter[str] = Counter()
        self.structural_tenpai_true_rows = 0
        self.red_five_rows = 0

    def add_unjoinable_game(self, target_seat_count: int) -> None:
        """participation seat joinが成立しないgameをunsupportedとして計上する。"""
        self.games_processed += 1
        self.games_unsupported += 1
        self.game_unsupported_reasons[
            UnsupportedReason.PARTICIPATION_SEAT_JOIN_FAILED.value
        ] += 1
        if target_seat_count > 1:
            self.shared_games += 1
            self.shared_game_participations += target_seat_count

    def add_game(self, result: GameReplayResult, target_seat_count: int) -> None:
        self.games_processed += 1
        self.rounds_processed += result.rounds
        self.leakage_failures += result.leakage_check_failures
        self.consistency_failures += result.replay_consistency_failures
        if target_seat_count > 1:
            self.shared_games += 1
            self.shared_game_participations += target_seat_count
        if not result.replayable:
            self.games_unsupported += 1
            reason = result.unsupported_reason
            if reason is None:
                raise ValueError("an unsupported game must carry a reason code")
            self.game_unsupported_reasons[reason.value] += 1
            return
        self.games_replayable += 1
        for decision in result.decisions:
            self._add_decision(decision)

    def _add_decision(self, decision: ObservedDecision) -> None:
        snapshot = decision.snapshot
        self.decision_points += 1
        self.per_bot[str(decision.bot_id)] += 1
        self.per_kind[snapshot.decision_kind.value] += 1
        self.families[decision.mapped.family.value] += 1
        if decision.mapped.family is ActionFamily.UNSUPPORTED:
            self.unsupported_actions += 1
            reason = decision.mapped.unsupported_reason
            if reason is None:
                raise ValueError("an unsupported action must carry a reason code")
            self.unsupported_action_reasons[reason.value] += 1
        else:
            self.supervised_decision_points += 1
        if snapshot.is_open_hand:
            self.open_hand += 1
        else:
            self.closed_hand += 1
        if snapshot.is_riichi_declared:
            self.riichi += 1
        else:
            self.non_riichi += 1

        hidden = decision.hidden
        self.hidden_decision_points += 1
        self.opponent_rows += len(hidden.rows)
        if not hidden.concealed_size_consistent:
            self.concealed_size_failures += 1
        if not hidden.tile_conservation_consistent:
            self.tile_conservation_failures += 1
        exact_rows = 0
        for row in hidden.rows:
            if row.concealed_size_is_consistent:
                exact_rows += 1
            if row.structural_wait.is_available:
                self.structural_wait_exact_rows += 1
                if row.structural_tenpai:
                    self.structural_tenpai_true_rows += 1
            else:
                unavailable = row.structural_wait.unavailable_reason
                if unavailable is None:
                    raise ValueError(
                        "an unavailable structural wait must carry a reason code"
                    )
                self.structural_wait_unsupported_rows[unavailable.value] += 1
            if row.holds_red_five:
                self.red_five_rows += 1
        if exact_rows == len(hidden.rows) and hidden.concealed_size_consistent:
            self.hidden_exact_decision_points += 1

    def behavior_measurements(self) -> BehaviorMeasurements:
        return BehaviorMeasurements(
            games_processed=self.games_processed,
            games_replayable=self.games_replayable,
            games_unsupported=self.games_unsupported,
            game_unsupported_reasons=_sorted_counts(self.game_unsupported_reasons),
            rounds_processed=self.rounds_processed,
            player_safe_decision_points=self.decision_points,
            target_bot_supervised_decision_points=self.supervised_decision_points,
            decision_points_per_target_bot=_sorted_counts(self.per_bot),
            decision_points_per_decision_kind=_sorted_counts(self.per_kind),
            action_family_counts=_sorted_counts(self.families),
            unsupported_actions=self.unsupported_actions,
            unsupported_action_reasons=_sorted_counts(self.unsupported_action_reasons),
            open_hand_decision_points=self.open_hand,
            closed_hand_decision_points=self.closed_hand,
            riichi_decision_points=self.riichi,
            non_riichi_decision_points=self.non_riichi,
            shared_games=self.shared_games,
            shared_game_participations=self.shared_game_participations,
            legal_action_sets_exactly_reconstructed=0,
            legal_action_set_unsupported_reasons=(
                {LEGAL_ACTION_SET_UNSUPPORTED_REASON: self.decision_points}
                if self.decision_points
                else {}
            ),
            leakage_check_failures=self.leakage_failures,
            replay_consistency_failures=self.consistency_failures,
        )

    def hidden_state_measurements(self) -> HiddenStateMeasurements:
        return HiddenStateMeasurements(
            decision_points=self.hidden_decision_points,
            decision_points_with_exact_opponent_truth=(
                self.hidden_exact_decision_points
            ),
            opponent_rows=self.opponent_rows,
            concealed_size_consistency_failures=self.concealed_size_failures,
            tile_conservation_failures=self.tile_conservation_failures,
            structural_wait_exact_rows=self.structural_wait_exact_rows,
            structural_wait_unsupported_rows=_sorted_counts(
                self.structural_wait_unsupported_rows
            ),
            structural_tenpai_exact_rows=self.structural_wait_exact_rows,
            structural_tenpai_true_rows=self.structural_tenpai_true_rows,
            red_five_holding_rows=self.red_five_rows,
        )


def qualify_local_corpus(
    snapshot: RecentGamesSnapshot, output_dir: Path
) -> DownstreamQualificationReport:
    """local cacheだけを読み、Issue #203のqualification reportを作る。

    network accessを行わず、cacheへの書き込みも行わない。identity gateを通る
    前にcached gameのbytesを読まない。
    """
    if not isinstance(snapshot, RecentGamesSnapshot):
        raise TypeError("snapshot must be a RecentGamesSnapshot")
    output = ensure_outside_git_worktree(output_dir)

    # stop reasonは固定文言だけにする。CorpusErrorのmessageは`game_id`等の
    # source-derived値を含み得るため、report artifactへ転記しない。
    try:
        entries = load_cache_index(output)
    except CorpusError:
        return _stop_report(snapshot, stop_reason="local cache index is invalid")
    missing = [game_id for game_id in snapshot.game_ids if game_id not in entries]
    if missing:
        return _stop_report(
            snapshot,
            stop_reason="local cache does not cover every snapshot game",
        )
    try:
        manifest = validate_manifest_file(output, snapshot, entries)
    except CorpusError:
        return _stop_report(
            snapshot, stop_reason="local corpus manifest is invalid or missing"
        )

    corpus_identity = manifest["corpus_identity"]
    manifest_sha256 = manifest["manifest_sha256"]
    if (
        corpus_identity != EXPECTED_CORPUS_IDENTITY
        or manifest_sha256 != EXPECTED_MANIFEST_SHA256
    ):
        return _stop_report(
            snapshot,
            stop_reason=(
                "local corpus identity does not match the Issue #203 source boundary"
            ),
            corpus_identity=corpus_identity,
            manifest_sha256=manifest_sha256,
        )

    grouped = group_participations(snapshot)
    aggregate = _Aggregate()
    for game_id in snapshot.game_ids:
        participations = grouped[game_id]
        try:
            target_seats = _target_seats(participations)
        except CorpusError:
            aggregate.add_unjoinable_game(len(participations))
            continue
        validate_cache_entry(output, entries[game_id], participations)
        payload = (output / GAMES_DIRECTORY / f"{game_id}.jsonl.gz").read_bytes()
        aggregate.add_game(
            replay_game(
                parse_jsonl_gzip(payload),
                game_id=game_id,
                target_seats=target_seats,
            ),
            len(target_seats),
        )

    behavior = aggregate.behavior_measurements()
    hidden_state = aggregate.hidden_state_measurements()
    behavior_classification = classify_behavior_surface(
        player_safe_decision_points=behavior.player_safe_decision_points,
        exactly_mapped_actions=behavior.target_bot_supervised_decision_points,
        unsupported_actions=behavior.unsupported_actions,
        games_unsupported=behavior.games_unsupported,
        leakage_check_failures=behavior.leakage_check_failures,
        replay_consistency_failures=behavior.replay_consistency_failures,
    )
    hidden_classification = classify_hidden_state_surface(
        player_safe_decision_points=behavior.player_safe_decision_points,
        decision_points_with_exact_opponent_truth=(
            hidden_state.decision_points_with_exact_opponent_truth
        ),
        concealed_size_consistency_failures=(
            hidden_state.concealed_size_consistency_failures
        ),
        tile_conservation_failures=hidden_state.tile_conservation_failures,
        games_unsupported=behavior.games_unsupported,
        leakage_check_failures=behavior.leakage_check_failures,
        replay_consistency_failures=behavior.replay_consistency_failures,
    )
    return DownstreamQualificationReport(
        generated_at=utc_now_text(),
        snapshot_identity=snapshot.snapshot_identity,
        corpus_identity=corpus_identity,
        manifest_sha256=manifest_sha256,
        expected_corpus_identity=EXPECTED_CORPUS_IDENTITY,
        expected_manifest_sha256=EXPECTED_MANIFEST_SHA256,
        overall_outcome=combine_overall_outcome(
            behavior_classification, hidden_classification
        ),
        stop_reason=None,
        behavior_classification=behavior_classification,
        hidden_state_classification=hidden_classification,
        behavior=behavior,
        hidden_state=hidden_state,
        coverage_limitations=COVERAGE_LIMITATIONS,
    )


__all__ = [
    "COVERAGE_LIMITATIONS",
    "EXPECTED_CORPUS_IDENTITY",
    "EXPECTED_MANIFEST_SHA256",
    "LEGAL_ACTION_SET_UNSUPPORTED_REASON",
    "qualify_local_corpus",
]
