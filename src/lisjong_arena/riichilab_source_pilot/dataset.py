"""Arm R source materialization、Gate 0 report、matched row budget。

```text
exact #170 corpus identity gate      (Issue #203 module constants)
    -> 全対象gameのcached bytes / seat join preflight
    -> per-game Gate 0 materialization
    -> Gate 0 report (exhaustive counters + reason codes)
    -> raw-game単位のdeterministic TRAIN / VALIDATION partition
    -> deterministic row-budget truncation (9,116 / 2,555)
```

partitionとrow selectionはlabel、model loss、downstream score、bot strength、
action rarity、validation resultを一切読まない。canonical game orderは
source identityへbindしたhashのみから決まるため、結果を見てから並べ替える
入口が存在しない。
"""

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from lisjong.policy_contract.seat import Seat

from lisjong_arena.riichilab_corpus.models import (
    CorpusError,
    Participation,
    RecentGamesSnapshot,
    sha256_bytes,
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

from .errors import BudgetNotMatchableError, MaterializationError, SourceIdentityError
from .materialization import (
    REPLAY_SEAM,
    GameMaterialization,
    MaterializedRow,
    materialize_game,
)
from .protocol import (
    ARM_R_CORPUS_IDENTITY,
    ARM_R_MANIFEST_SHA256,
    MINIMUM_LEGAL_ACTION_COUNT,
    TRAIN_ROW_BUDGET,
    VALIDATION_ROW_BUDGET,
    feature_block,
    vocabulary_block,
)

#: RiichiLab ranked gameのexternal execution mode。#170のcorpusはranked
#: 4-player red-dora gameであり、replay engineへ渡すgame modeを
#: caller-configurableにしない。
SOURCE_GAME_MODE = "4p-red-half"

#: canonical game orderのdomain separator。source identityへbindするため、
#: 同じgame集合でもcorpus identityが違えば順序が変わる。
_GAME_ORDER_DOMAIN = "arena-riichilab-source-pilot-game-order-v1"


@dataclass(frozen=True, slots=True)
class Gate0Report:
    """Gate 0のexhaustive measurement。"""

    replay_seam: str
    games_processed: int
    games_replayable: int
    games_unsupported: int
    game_unsupported_reasons: tuple[tuple[str, int], ...]
    rounds_processed: int
    decision_opportunities: int
    explicit_action_rows: int
    implicit_pass_rows: int
    forced_rows: int
    eligible_rows: int
    unresolved_rows: int
    unresolved_reasons: tuple[tuple[str, int], ...]
    leakage_failures: int
    shared_games: int
    shared_game_participations: int

    @property
    def gate_passed(self) -> bool:
        """Gate 0のhard outcome。

        Issue #211はexact materializationを要求する。以下のいずれかが
        0でなければtrainingへ進まない。

        - unsupported game
        - unresolved decision（selected actionの未解決・illegal、feature /
          legal-mask materialization失敗、exactに復元できないlegal action
          semanticsを含む）
        - leakage / future-information failure
        """
        return (
            self.games_unsupported == 0
            and self.unresolved_rows == 0
            and self.leakage_failures == 0
            and self.eligible_rows > 0
        )

    def to_document(self) -> dict[str, object]:
        return {
            "replay_seam": self.replay_seam,
            "games_processed": self.games_processed,
            "games_replayable": self.games_replayable,
            "games_unsupported": self.games_unsupported,
            "game_unsupported_reasons": dict(self.game_unsupported_reasons),
            "rounds_processed": self.rounds_processed,
            "decision_opportunities": self.decision_opportunities,
            "explicit_action_rows": self.explicit_action_rows,
            "implicit_pass_rows": self.implicit_pass_rows,
            "forced_rows": self.forced_rows,
            "eligible_rows": self.eligible_rows,
            "unresolved_rows": self.unresolved_rows,
            "unresolved_reasons": dict(self.unresolved_reasons),
            "leakage_check_failures": self.leakage_failures,
            "shared_games": self.shared_games,
            "shared_game_participations": self.shared_game_participations,
            "minimum_legal_action_count": MINIMUM_LEGAL_ACTION_COUNT,
            "gate_passed": self.gate_passed,
        }


@dataclass(frozen=True, slots=True)
class MaterializedSource:
    """Gate 0を通したArm Rのin-memory source。

    rowは実行processの中だけに存在し、repositoryへcommitしない。
    """

    corpus_identity: str
    manifest_sha256: str
    snapshot_identity: str
    games: tuple[GameMaterialization, ...]
    report: Gate0Report

    @property
    def eligible_rows(self) -> tuple[MaterializedRow, ...]:
        return tuple(row for game in self.games for row in game.rows)


def _target_seats(participations: tuple[Participation, ...]) -> dict[Seat, int]:
    seats: dict[Seat, int] = {}
    for item in participations:
        seat = Seat(item.seat)
        if seat in seats:
            raise CorpusError("a game joins multiple target bots to one seat")
        seats[seat] = item.bot_id
    if not seats:
        raise CorpusError("a game must have at least one target participation")
    return seats


def _verify_every_cached_game(
    output: Path,
    snapshot: RecentGamesSnapshot,
    entries: dict[str, dict[str, object]],
    grouped: dict[str, tuple[Participation, ...]],
) -> dict[str, tuple[dict[Seat, int], bytes]]:
    """1件目のreplay前に、全対象gameのbytesとseat joinを検証しきる。"""
    verified: dict[str, tuple[dict[Seat, int], bytes]] = {}
    for game_id in snapshot.game_ids:
        participations = grouped[game_id]
        seats = _target_seats(participations)
        validate_cache_entry(output, entries[game_id], participations)
        payload = (output / GAMES_DIRECTORY / f"{game_id}.jsonl.gz").read_bytes()
        if sha256_bytes(payload) != entries[game_id]["compressed_sha256"]:
            raise CorpusError("cached game bytes changed during revalidation")
        verified[game_id] = (seats, payload)
    return verified


def materialize_local_corpus(
    snapshot: RecentGamesSnapshot, output_dir: str | Path
) -> MaterializedSource:
    """exact #170 corpusをGate 0 contractでmaterializeする。

    identity gateを通過するまでcached gameのbytesを読まない。network
    accessも、cacheへの書き込みも行わない。
    """
    if not isinstance(snapshot, RecentGamesSnapshot):
        raise TypeError("snapshot must be a RecentGamesSnapshot")
    output = ensure_outside_git_worktree(output_dir)

    try:
        entries = load_cache_index(output)
    except CorpusError as error:
        raise SourceIdentityError("local cache index is invalid") from error
    missing = [game_id for game_id in snapshot.game_ids if game_id not in entries]
    if missing:
        raise SourceIdentityError("local cache does not cover every snapshot game")
    try:
        manifest = validate_manifest_file(output, snapshot, entries)
    except CorpusError as error:
        raise SourceIdentityError(
            "local corpus manifest is invalid or missing"
        ) from error

    corpus_identity = manifest["corpus_identity"]
    manifest_sha256 = manifest["manifest_sha256"]
    if (
        corpus_identity != ARM_R_CORPUS_IDENTITY
        or manifest_sha256 != ARM_R_MANIFEST_SHA256
    ):
        raise SourceIdentityError(
            "local corpus identity does not match the exact Issue #211 source"
        )

    grouped = group_participations(snapshot)
    try:
        verified = _verify_every_cached_game(output, snapshot, entries, grouped)
    except CorpusError as error:
        raise SourceIdentityError(
            "local cached game bytes or participation seat join failed revalidation"
        ) from error

    games = []
    for game_id in snapshot.game_ids:
        seats, payload = verified[game_id]
        games.append(
            materialize_game(
                parse_jsonl_gzip(payload),
                game_id=game_id,
                target_seats=seats,
                game_mode=SOURCE_GAME_MODE,
            )
        )
    return build_source(
        tuple(games),
        corpus_identity=corpus_identity,
        manifest_sha256=manifest_sha256,
        snapshot_identity=snapshot.snapshot_identity,
        target_seat_counts={
            game_id: len(verified[game_id][0]) for game_id in snapshot.game_ids
        },
    )


def build_source(
    games: tuple[GameMaterialization, ...],
    *,
    corpus_identity: str,
    manifest_sha256: str,
    snapshot_identity: str,
    target_seat_counts: dict[str, int],
) -> MaterializedSource:
    """materialized gameからGate 0 reportを組み立てる。"""
    game_reasons: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    replayable = 0
    unsupported = 0
    rounds = 0
    opportunities = 0
    explicit_rows = 0
    implicit_rows = 0
    forced_rows = 0
    eligible = 0
    unresolved_total = 0
    shared_games = 0
    shared_participations = 0

    for game in games:
        seat_count = target_seat_counts.get(game.game_id, 1)
        if seat_count > 1:
            shared_games += 1
            shared_participations += seat_count
        if not game.supported:
            unsupported += 1
            reason = game.unsupported_reason
            if reason is None:
                raise MaterializationError("an unsupported game must carry a reason")
            game_reasons[reason.value] += 1
            continue
        replayable += 1
        rounds += game.rounds
        opportunities += game.decision_opportunities
        explicit_rows += game.explicit_rows
        implicit_rows += game.implicit_pass_rows
        forced_rows += game.forced_rows
        eligible += len(game.rows)
        for reason_code, count in game.unresolved_reasons:
            unresolved[reason_code] += count
            unresolved_total += count

    report = Gate0Report(
        replay_seam=REPLAY_SEAM,
        games_processed=len(games),
        games_replayable=replayable,
        games_unsupported=unsupported,
        game_unsupported_reasons=tuple(sorted(game_reasons.items())),
        rounds_processed=rounds,
        decision_opportunities=opportunities,
        explicit_action_rows=explicit_rows,
        implicit_pass_rows=implicit_rows,
        forced_rows=forced_rows,
        eligible_rows=eligible,
        unresolved_rows=unresolved_total,
        unresolved_reasons=tuple(sorted(unresolved.items())),
        leakage_failures=0,
        shared_games=shared_games,
        shared_game_participations=shared_participations,
    )
    return MaterializedSource(
        corpus_identity=corpus_identity,
        manifest_sha256=manifest_sha256,
        snapshot_identity=snapshot_identity,
        games=tuple(games),
        report=report,
    )


def canonical_game_order(
    source: MaterializedSource,
) -> tuple[GameMaterialization, ...]:
    """source identityへbindしたdeterministic canonical game order。

    並べ替えのkeyは`sha256(domain | corpus_identity | game_id)`だけであり、
    row数、label、metric、bot identityを読まない。
    """
    supported = [game for game in source.games if game.supported]

    def order_key(game: GameMaterialization) -> tuple[str, str]:
        digest = hashlib.sha256(
            f"{_GAME_ORDER_DOMAIN}|{source.corpus_identity}|{game.game_id}".encode()
        ).hexdigest()
        return (digest, game.game_id)

    return tuple(sorted(supported, key=order_key))


@dataclass(frozen=True, slots=True)
class RowBudget:
    """raw-game単位でisolateされたmatched row budget。"""

    train_game_ids: tuple[str, ...]
    validation_game_ids: tuple[str, ...]
    train_rows: tuple[MaterializedRow, ...]
    validation_rows: tuple[MaterializedRow, ...]

    def __post_init__(self) -> None:
        overlap = set(self.train_game_ids) & set(self.validation_game_ids)
        if overlap:
            raise BudgetNotMatchableError(
                "a raw game must not contribute to both TRAIN and VALIDATION"
            )
        if len(self.train_rows) != TRAIN_ROW_BUDGET:
            raise BudgetNotMatchableError("TRAIN row budget is not exact")
        if len(self.validation_rows) != VALIDATION_ROW_BUDGET:
            raise BudgetNotMatchableError("VALIDATION row budget is not exact")
        train_games = {row.game_id for row in self.train_rows}
        validation_games = {row.game_id for row in self.validation_rows}
        if train_games & validation_games:
            raise BudgetNotMatchableError("selected rows cross the raw-game partition")
        if not train_games <= set(self.train_game_ids):
            raise BudgetNotMatchableError("TRAIN rows come from outside the partition")
        if not validation_games <= set(self.validation_game_ids):
            raise BudgetNotMatchableError(
                "VALIDATION rows come from outside the partition"
            )

    def distribution_document(self) -> dict[str, object]:
        """sourceの差をdescriptiveに記録する。reweightはしない。"""
        return {
            "train": _partition_distribution(self.train_rows, self.train_game_ids),
            "validation": _partition_distribution(
                self.validation_rows, self.validation_game_ids
            ),
        }


def _partition_distribution(
    rows: tuple[MaterializedRow, ...], game_ids: tuple[str, ...]
) -> dict[str, object]:
    return {
        "rows": len(rows),
        "partition_games": len(game_ids),
        "represented_games": len({row.game_id for row in rows}),
        "target_participations": len({(row.game_id, row.actor_seat) for row in rows}),
        "rows_per_target_bot": dict(
            sorted(Counter(str(row.bot_id) for row in rows).items())
        ),
        "action_family_counts": dict(
            sorted(Counter(row.teacher_action_family for row in rows).items())
        ),
        "decision_kind_counts": dict(
            sorted(Counter(row.decision_kind.value for row in rows).items())
        ),
        "implicit_pass_rows": sum(1 for row in rows if row.implicit_pass),
        "explicit_action_rows": sum(1 for row in rows if not row.implicit_pass),
        "legal_action_count_distribution": dict(
            sorted(
                (str(count), total)
                for count, total in Counter(
                    row.legal_action_count for row in rows
                ).items()
            )
        ),
        "open_hand_rows": sum(1 for row in rows if row.is_open_hand),
        "closed_hand_rows": sum(1 for row in rows if not row.is_open_hand),
        "riichi_rows": sum(1 for row in rows if row.is_riichi_declared),
        "non_riichi_rows": sum(1 for row in rows if not row.is_riichi_declared),
    }


def build_row_budget(source: MaterializedSource) -> RowBudget:
    """raw-game partitionとdeterministic truncationでmatched budgetを作る。

    canonical game orderのprefixをVALIDATIONへ割り当て、VALIDATION budget
    へ到達したgameまでをVALIDATION partitionとする。残り全部がTRAIN
    partitionである。同じraw gameが両partitionへ跨がることはない。
    """
    if not isinstance(source, MaterializedSource):
        raise TypeError("source must be a MaterializedSource")
    if not source.report.gate_passed:
        raise MaterializationError(
            "row budgets must not be formed before Gate 0 passes"
        )

    ordered = canonical_game_order(source)
    validation_games: list[GameMaterialization] = []
    cumulative = 0
    for index, game in enumerate(ordered):
        validation_games.append(game)
        cumulative += len(game.rows)
        if cumulative >= VALIDATION_ROW_BUDGET:
            train_games = list(ordered[index + 1 :])
            break
    else:
        raise BudgetNotMatchableError(
            "the materialized source cannot fill the VALIDATION row budget"
        )

    validation_rows = [row for game in validation_games for row in game.rows]
    train_rows = [row for game in train_games for row in game.rows]
    if len(train_rows) < TRAIN_ROW_BUDGET:
        raise BudgetNotMatchableError(
            "the materialized source cannot fill the TRAIN row budget"
        )

    return RowBudget(
        train_game_ids=tuple(game.game_id for game in train_games),
        validation_game_ids=tuple(game.game_id for game in validation_games),
        train_rows=tuple(train_rows[:TRAIN_ROW_BUDGET]),
        validation_rows=tuple(validation_rows[:VALIDATION_ROW_BUDGET]),
    )


def rows_identity(rows: tuple[MaterializedRow, ...]) -> str:
    """row集合のidentity digest。順序とrow内容の両方にbindする。"""
    digest = hashlib.sha256()
    digest.update(f"{_GAME_ORDER_DOMAIN}|rows|{len(rows)}\n".encode())
    for row in rows:
        metadata = "|".join(
            (
                row.game_id,
                str(row.decision_ordinal),
                str(row.actor_seat),
                str(row.bot_id),
                row.decision_kind.value,
                str(row.legal_action_count),
                str(row.teacher_action_index),
                row.teacher_action_family,
                "1" if row.implicit_pass else "0",
            )
        )
        digest.update(metadata.encode())
        digest.update(b"\n")
        # featureとlegal maskはrow自身がfixed-size payloadとして保持する。
        # identityはそのbytesへ直接bindし、text formatへ依存させない。
        digest.update(row.feature_payload)
        digest.update(row.legal_mask_payload)
        digest.update(b"\n")
    return digest.hexdigest()


def dataset_identity_document(
    source: MaterializedSource, budget: RowBudget
) -> dict[str, object]:
    """materialized datasetのidentity block。"""
    return {
        "source_identity": {
            "corpus_identity": source.corpus_identity,
            "manifest_sha256": source.manifest_sha256,
            "snapshot_identity": source.snapshot_identity,
            "source_game_mode": SOURCE_GAME_MODE,
        },
        "gate0": source.report.to_document(),
        "feature": feature_block(),
        "vocabulary": vocabulary_block(),
        "train_game_ids": list(budget.train_game_ids),
        "validation_game_ids": list(budget.validation_game_ids),
        "train_rows_identity": rows_identity(budget.train_rows),
        "validation_rows_identity": rows_identity(budget.validation_rows),
        "train_row_count": len(budget.train_rows),
        "validation_row_count": len(budget.validation_rows),
        "distribution": budget.distribution_document(),
    }


__all__ = [
    "SOURCE_GAME_MODE",
    "Gate0Report",
    "MaterializedSource",
    "RowBudget",
    "build_row_budget",
    "build_source",
    "canonical_game_order",
    "dataset_identity_document",
    "materialize_local_corpus",
    "rows_identity",
]
