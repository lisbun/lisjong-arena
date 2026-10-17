"""Offline position audit against the locked #489 Champion inputs; aggregate only."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from lisjong.policy_contract import Seat

from lisjong_arena.riichilab_corpus.models import sha256_bytes
from lisjong_arena.riichilab_corpus.validation import (
    parse_jsonl_gzip,
    validate_mjai_log,
)
from lisjong_arena.riichilab_deal_in_risk_disagreement.analysis import (
    _RiichiDeclarationTracker,
)
from lisjong_arena.riichilab_r1_choice_loss_diagnostic.champion_play import (
    CHAMPION_GROUPS,
    SHARED_GAMES,
    _select_games,
)
from lisjong_arena.riichilab_self_history.mjai import log_path
from lisjong_arena.riichilab_source_pilot.dataset import SOURCE_GAME_MODE
from lisjong_arena.riichilab_source_pilot.materialization import materialize_game


def compare_positions(view, tracker):
    """Compare only independently inferred singleton rows; no Policy semantics."""
    tracker.update(view)
    counts = Counter()
    for seat, bounds in tracker.declaration_range.items():
        player = view.players[seat]
        if bounds[0] != bounds[1]:
            counts["ambiguous_player_rows"] += 1
            continue
        expected = player.discards[bounds[0]].order
        if player.riichi_declaration_discard_order != expected:
            raise ValueError(
                "Adapter position differs from the singleton diagnostic position"
            )
        counts["singleton_player_rows_matched"] += 1
    return counts


def audit(roots, reference):
    if reference["schema_version"] != "arena-riichilab-r1-choice-loss-champion-play-v3":
        raise ValueError("expected the existing #489 v3 aggregate")
    selections = {
        g.name: _select_games(Path(roots[g.name]), g) for g in CHAMPION_GROUPS
    }
    shared = set(g.game_id for g in selections["dev"]) & set(
        g.game_id for g in selections["baseline"]
    )
    if len(shared) != SHARED_GAMES:
        raise ValueError("shared game count differs from locked input")
    result = {}
    for group in CHAMPION_GROUPS:
        root = Path(roots[group.name])
        digest = hashlib.sha256()
        counts = Counter()
        for game in selections[group.name]:
            payload = log_path(root, game.game_id).read_bytes()
            validate_mjai_log(payload, seats=(game.seat,))
            digest.update(f"{game.game_id}:{sha256_bytes(payload)}\n".encode())
            tracker = _RiichiDeclarationTracker()
            game_counts = Counter()

            def observe(observation):
                game_counts.update(
                    compare_positions(observation.decision.input, tracker)
                )
                game_counts["decision_rows"] += 1

            replay = materialize_game(
                list(parse_jsonl_gzip(payload)),
                game_id=game.game_id,
                target_seats={Seat(game.seat): group.bot_id},
                game_mode=SOURCE_GAME_MODE,
                decision_observer=observe,
            )
            if not replay.supported:
                raise ValueError("a locked game could not be replayed")
            counts.update(game_counts)
            counts["games"] += 1
        source = reference["source"]["groups"][group.name]
        if (
            source["history_identity"] != group.history_identity
            or source["game_ids_sha256"] != group.game_ids_sha256
            or source["games"] != group.games
            or source["game_logs_sha256"] != digest.hexdigest()
        ):
            raise ValueError("inputs differ from the existing #489 aggregate")
        if not counts["singleton_player_rows_matched"]:
            raise ValueError("no singleton positions were compared")
        result[group.name] = dict(sorted(counts.items()))
    return {
        "schema": "arena-declaration-position-audit-v1",
        "groups": result,
        "shared_games": SHARED_GAMES,
        "mismatches": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-history", required=True)
    parser.add_argument("--baseline-history", required=True)
    parser.add_argument(
        "--reference", required=True, help="existing #489 aggregate JSON"
    )
    args = parser.parse_args()
    result = audit(
        {"dev": args.dev_history, "baseline": args.baseline_history},
        json.loads(Path(args.reference).read_bytes()),
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
