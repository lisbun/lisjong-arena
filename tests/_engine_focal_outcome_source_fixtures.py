"""Test-only builders for the #370 lisjong-engine focal outcome source tests.

engineの実value type（``RoundPosition`` / ``calculate_round_settlement`` /
``calculate_final_riichi_stick_awards`` / ``CompletedMatch``）でhistoryを組み、
実engine対局を起動せずにkyoku境界を検証する。
"""

import json
import shutil
from pathlib import Path

from lisjong_engine.final_score import calculate_final_scores
from lisjong_engine.match_state import (
    CompletedMatch,
    CompletedRound,
    MatchEndReason,
    RoundPosition,
)
from lisjong_engine.points import SeatPoints
from lisjong_engine.riichi_event import RiichiContribution
from lisjong_engine.round_allocation import create_round_random_provenance
from lisjong_engine.round_result import ExhaustiveDrawResult
from lisjong_engine.rules import RuleSet
from lisjong_engine.seat import Seat as ES
from lisjong_engine.settlement import (
    calculate_final_riichi_stick_awards,
    calculate_round_settlement,
)
from lisjong_engine.wind import Wind as EW

from lisjong_arena import seed_registry
from lisjong_arena.focal_outcome_source import engine_source
from lisjong_arena.focal_outcome_source.source import (
    PINNED_LISJONG_REVISION,
    _canonical_line,
    _file_info,
)
from lisjong_arena.offense_foundation.qualification import seal

MATCH_SEED = 900300
_DEALERS = {1: ES.EAST, 2: ES.SOUTH, 3: ES.WEST, 4: ES.NORTH}


def riichi(*seats):
    return tuple(RiichiContribution(seat, 1000) for seat in seats)


def build_match(rounds, *, starting=25_000, seed=MATCH_SEED):
    """``rounds``: ``(hand_number, honba, result, riichi_contributions)``の列。

    各局の供託本数は直前局の``riichi_sticks_after``から引き継ぐ。最終局は
    engineと同じく残存供託をfinal awardsで配分する。
    """
    rules = RuleSet.default()
    scores = SeatPoints(starting, starting, starting, starting)
    sticks = 0
    history = []
    for ordinal, (hand, honba, result, contributions) in enumerate(rounds):
        position = RoundPosition(EW.EAST, hand, _DEALERS[hand], honba, sticks)
        if history:
            history[-1] = _with_next(history[-1], position)
        settlement = calculate_round_settlement(
            result,
            dealer_seat=position.dealer_seat,
            honba=honba,
            riichi_sticks_before=sticks,
            riichi_contributions=contributions,
            rules=rules,
        )
        scores = scores.add(settlement.point_deltas)
        sticks = settlement.riichi_sticks_after
        history.append(
            CompletedRound(
                random_provenance=create_round_random_provenance(seed, ordinal + 1),
                position_before=position,
                result=result,
                settlement=settlement,
                scores_after_settlement=scores,
                dealer_continues=False,
                next_position=None,
            )
        )
    awards = calculate_final_riichi_stick_awards(scores, sticks, rules=rules)
    final_raw = scores
    for award in awards:
        final_raw = final_raw.add(
            SeatPoints.from_mapping(
                {seat: award.amount if seat is award.recipient else 0 for seat in ES}
            )
        )
    return CompletedMatch(
        end_reason=MatchEndReason.FINAL_ROUND,
        final_riichi_stick_awards=awards,
        final_raw_scores=final_raw,
        final_score=calculate_final_scores(final_raw.as_dict(), rules=rules),
        history=tuple(history),
    )


def _with_next(completed, position):
    return CompletedRound(
        random_provenance=completed.random_provenance,
        position_before=completed.position_before,
        result=completed.result,
        settlement=completed.settlement,
        scores_after_settlement=completed.scores_after_settlement,
        dealer_continues=completed.dealer_continues,
        next_position=position,
    )


def leftover_riichi_match():
    """全員聴牌流局（#364 pattern）を含み、最終局に供託3本が残るhistory。

    ```text
    E1-0  全員聴牌の流局、EAST / SOUTH立直         sticks 0 -> 2
    E1-1  EASTだけ聴牌の流局、WEST立直（ノーテン罰符） sticks 2 -> 3
    E1-2  全員聴牌の流局（最終局）                  sticks 3 -> 3 -> final award
    ```
    """
    all_tenpai = ExhaustiveDrawResult(tenpai_seats=tuple(ES))
    return build_match(
        [
            (1, 0, all_tenpai, riichi(ES.EAST, ES.SOUTH)),
            (1, 1, ExhaustiveDrawResult(tenpai_seats=(ES.EAST,)), riichi(ES.WEST)),
            (1, 2, all_tenpai, ()),
        ]
    )


def binding(seeds, *, domain=seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN):
    return {
        "allocation_identity": "1" * 64,
        "ledger_revision": "2" * 64,
        "owner_repository": "lisbun/lisjong-arena",
        "seed_domain": domain,
        "seed_membership_identity": seed_registry.seed_membership_identity(list(seeds)),
    }


def source_contract():
    return {
        "arena_revision": "0" * 40,
        "backend": engine_source.BACKEND_NAME,
        "dependencies": {
            "lisjong": PINNED_LISJONG_REVISION,
            "lisjong-engine": engine_source.PINNED_LISJONG_ENGINE_REVISION,
        },
        "python": "3.14.0",
        "rules": dict(engine_source.RULES),
    }


def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text("utf-8").splitlines()]


class Tampered:
    """source copyを改ざんし、payload digest / game summary / manifestをresealする。"""

    def __init__(self, original, root):
        self.path = Path(root) / "tampered"
        shutil.copytree(original, self.path)

    def manifest(self):
        return json.loads((self.path / "manifest.json").read_text("utf-8"))

    def write_manifest(self, body, *, reseal=True):
        body = dict(body)
        if reseal:
            body.pop("identity", None)
            body = seal(body)
        (self.path / "manifest.json").write_text(
            json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def rewrite_game(self, ordinal, *, kyokus=None, decisions=None, summary=None):
        game_path = self.path / f"game-{ordinal:03d}"
        for name, rows in (
            (engine_source.KYOKU_PAYLOAD_FILENAME, kyokus),
            (engine_source.DECISION_PAYLOAD_FILENAME, decisions),
        ):
            if rows is not None:
                (game_path / name).write_text(
                    "".join(_canonical_line(row) for row in rows),
                    encoding="utf-8",
                    newline="\n",
                )
        manifest = self.manifest()
        game = {k: v for k, v in manifest["games"][ordinal].items() if k != "identity"}
        game["files"] = {name: _file_info(game_path / name) for name in game["files"]}
        if summary is not None:
            game.update(summary)
        manifest["games"][ordinal] = seal(game)
        self.write_manifest(manifest)

    def kyokus(self, ordinal=0):
        return read_lines(
            self.path / f"game-{ordinal:03d}" / engine_source.KYOKU_PAYLOAD_FILENAME
        )

    def decisions(self, ordinal=0):
        return read_lines(
            self.path / f"game-{ordinal:03d}" / engine_source.DECISION_PAYLOAD_FILENAME
        )
