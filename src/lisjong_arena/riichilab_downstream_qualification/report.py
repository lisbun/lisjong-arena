"""Issue #203専用のsummary artifact schema。

このartifactはoffline downstream reconstruction qualification専用であり、
training dataset、canonical GameRecord、generic replay schemaではない。

保持するのはaggregate count、reason code別件数、classification、source
provenance identityだけである。次はartifactへ書かない。

```text
raw MJAI event stream
raw / transformed third-party record
concealed hand tiles
per-decision row
bot表示名等のraw third-party content
credential / Authorization情報
```

artifactはimmutable snapshotとして扱い、既存fileを上書きしない
（`riichilab_corpus.persistence.write_new_json`と同じcontract）。書き込み先は
Git worktree外だけである。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lisjong_arena.riichilab_corpus.persistence import (
    ensure_outside_git_worktree,
    write_new_json,
)
from lisjong_arena.riichilab_downstream_qualification.classification import (
    OverallOutcome,
    SurfaceClassification,
)

REPORT_SCHEMA_ID = "lisjong-arena-riichilab-downstream-qualification"
REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class BehaviorMeasurements:
    """Surface Aのrequired measurements。"""

    games_processed: int
    games_replayable: int
    games_unsupported: int
    game_unsupported_reasons: Mapping[str, int]
    rounds_processed: int
    player_safe_decision_points: int
    target_bot_supervised_decision_points: int
    decision_points_per_target_bot: Mapping[str, int]
    decision_points_per_decision_kind: Mapping[str, int]
    action_family_counts: Mapping[str, int]
    unsupported_actions: int
    unsupported_action_reasons: Mapping[str, int]
    open_hand_decision_points: int
    closed_hand_decision_points: int
    riichi_decision_points: int
    non_riichi_decision_points: int
    shared_games: int
    shared_game_participations: int
    legal_action_sets_exactly_reconstructed: int
    legal_action_set_unsupported_reasons: Mapping[str, int]
    leakage_check_failures: int
    replay_consistency_failures: int

    def to_value(self) -> dict[str, object]:
        return {
            "action_family_counts": dict(self.action_family_counts),
            "closed_hand_decision_points": self.closed_hand_decision_points,
            "decision_points_per_decision_kind": dict(
                self.decision_points_per_decision_kind
            ),
            "decision_points_per_target_bot": dict(self.decision_points_per_target_bot),
            "game_unsupported_reasons": dict(self.game_unsupported_reasons),
            "games_processed": self.games_processed,
            "games_replayable": self.games_replayable,
            "games_unsupported": self.games_unsupported,
            "leakage_check_failures": self.leakage_check_failures,
            "legal_action_set_unsupported_reasons": dict(
                self.legal_action_set_unsupported_reasons
            ),
            "legal_action_sets_exactly_reconstructed": (
                self.legal_action_sets_exactly_reconstructed
            ),
            "non_riichi_decision_points": self.non_riichi_decision_points,
            "open_hand_decision_points": self.open_hand_decision_points,
            "player_safe_decision_points": self.player_safe_decision_points,
            "replay_consistency_failures": self.replay_consistency_failures,
            "riichi_decision_points": self.riichi_decision_points,
            "rounds_processed": self.rounds_processed,
            "shared_game_participations": self.shared_game_participations,
            "shared_games": self.shared_games,
            "target_bot_supervised_decision_points": (
                self.target_bot_supervised_decision_points
            ),
            "unsupported_action_reasons": dict(self.unsupported_action_reasons),
            "unsupported_actions": self.unsupported_actions,
        }


@dataclass(frozen=True, slots=True)
class HiddenStateMeasurements:
    """Surface Bのrequired measurements。"""

    decision_points: int
    decision_points_with_exact_opponent_truth: int
    opponent_rows: int
    concealed_size_consistency_failures: int
    tile_conservation_failures: int
    structural_wait_exact_rows: int
    structural_wait_unsupported_rows: Mapping[str, int]
    structural_tenpai_exact_rows: int
    structural_tenpai_true_rows: int
    red_five_holding_rows: int

    def to_value(self) -> dict[str, object]:
        return {
            "concealed_size_consistency_failures": (
                self.concealed_size_consistency_failures
            ),
            "decision_points": self.decision_points,
            "decision_points_with_exact_opponent_truth": (
                self.decision_points_with_exact_opponent_truth
            ),
            "opponent_rows": self.opponent_rows,
            "red_five_holding_rows": self.red_five_holding_rows,
            "structural_tenpai_exact_rows": self.structural_tenpai_exact_rows,
            "structural_tenpai_true_rows": self.structural_tenpai_true_rows,
            "structural_wait_exact_rows": self.structural_wait_exact_rows,
            "structural_wait_unsupported_rows": dict(
                self.structural_wait_unsupported_rows
            ),
            "tile_conservation_failures": self.tile_conservation_failures,
        }


@dataclass(frozen=True, slots=True)
class DownstreamQualificationReport:
    """1回のqualification runのimmutable summary。"""

    generated_at: str
    snapshot_identity: str
    corpus_identity: str | None
    manifest_sha256: str | None
    expected_corpus_identity: str
    expected_manifest_sha256: str
    overall_outcome: OverallOutcome
    stop_reason: str | None
    behavior_classification: SurfaceClassification | None
    hidden_state_classification: SurfaceClassification | None
    behavior: BehaviorMeasurements | None
    hidden_state: HiddenStateMeasurements | None
    coverage_limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        stopped = self.overall_outcome is OverallOutcome.STOP_INVALID
        if stopped != (self.stop_reason is not None):
            raise ValueError("stop_reason must be set exactly for STOP / INVALID")
        if stopped and (
            self.behavior is not None
            or self.hidden_state is not None
            or self.behavior_classification is not None
            or self.hidden_state_classification is not None
        ):
            raise ValueError("a stopped run must not report measurements")
        if not stopped and (
            self.behavior is None
            or self.hidden_state is None
            or self.behavior_classification is None
            or self.hidden_state_classification is None
        ):
            raise ValueError("a completed run must report both surfaces")

    def to_value(self) -> dict[str, object]:
        return {
            "behavior": None if self.behavior is None else self.behavior.to_value(),
            "behavior_classification": (
                None
                if self.behavior_classification is None
                else self.behavior_classification.value
            ),
            "corpus_identity": self.corpus_identity,
            "coverage_limitations": list(self.coverage_limitations),
            "expected_corpus_identity": self.expected_corpus_identity,
            "expected_manifest_sha256": self.expected_manifest_sha256,
            "generated_at": self.generated_at,
            "hidden_state": (
                None if self.hidden_state is None else self.hidden_state.to_value()
            ),
            "hidden_state_classification": (
                None
                if self.hidden_state_classification is None
                else self.hidden_state_classification.value
            ),
            "manifest_sha256": self.manifest_sha256,
            "overall_outcome": self.overall_outcome.value,
            "schema": REPORT_SCHEMA_ID,
            "schema_version": REPORT_SCHEMA_VERSION,
            "snapshot_identity": self.snapshot_identity,
            "stop_reason": self.stop_reason,
        }


def report_filename(snapshot_identity: str) -> str:
    return f"downstream-qualification-{snapshot_identity}.json"


def write_report(destination: Path, report: DownstreamQualificationReport) -> Path:
    """report artifactをGit worktree外へ新規fileとして書き出す。

    既存fileは上書きしない。同じdestinationで再実行したい場合は、operatorが
    明示的に別のdestinationを選ぶ。
    """
    if not isinstance(report, DownstreamQualificationReport):
        raise TypeError("report must be a DownstreamQualificationReport")
    directory = ensure_outside_git_worktree(destination.parent)
    path = directory / destination.name
    write_new_json(path, report.to_value())
    return path


__all__ = [
    "REPORT_SCHEMA_ID",
    "REPORT_SCHEMA_VERSION",
    "BehaviorMeasurements",
    "DownstreamQualificationReport",
    "HiddenStateMeasurements",
    "report_filename",
    "write_report",
]
