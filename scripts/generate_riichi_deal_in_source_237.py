"""Development data generator for lisbun/lisjong#237 (R1800 S1).

Plays Champion self-play hanchan on lisjong-engine and records, for every
in-scope discard decision, the **observed facts** that lisjong needs to compute
the riichi deal-in label.  Arena does not decide furiten or the label: lisjong
reads these files through ``lisjong.learning.riichi_deal_in_source`` (the input
contract is owned by lisjong; Arena only writes that wire shape).

Output directory (all new; existing paths are refused)::

    manifest.json      lisjong-riichi-deal-in-source-manifest-v1
    decisions.jsonl    lisjong-riichi-deal-in-decision-record-v1    player-safe
    label_facts.jsonl  lisjong-riichi-deal-in-label-fact-record-v1  training-only

- In scope: exactly one opponent in riichi (declared or accepted), the decider
  not in riichi, at least two candidate tile types.
- ``decisions.jsonl`` holds what the decider sees (``PolicyInput``, legal
  actions, the selected discard).
- ``label_facts.jsonl`` holds hidden facts, kept out of the decision record:
  the riichi player's 13-equivalent hand (concealed tiles and melds, taken from
  that player's own last discard decision), the riichi declaration discard
  sequence, every later decision at which the riichi player was offered a win
  (sequence, winning tiles, chosen action), and, for the selected discard only,
  whether the engine offered the riichi player a ron on it and whether that
  player won on it.
- Only facts from before the decision go into the hand and win options.
  Engine facts are recorded for the selected discard only; unselected
  candidates get no engine value.

Development-only: seeds must be inside the RETIRED lisjong-arena#385
engine-domain population (931000..931999).  Generated data is not committed.

Usage::

    LISJONG_SHANTEN_BACKEND=rust python scripts/generate_riichi_deal_in_source_237.py \
        --train 931200..931359 --valid 931360..931379 --test 931380..931399 \
        --workers 4 --output <new directory>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

RETIRED_SEED_FIRST = 931000
RETIRED_SEED_LAST = 931999
SPLITS = ("train", "valid", "test")

# lisjong.learning.riichi_deal_in_source (lisjong#237) owns these identifiers.
MANIFEST_SCHEMA = "lisjong-riichi-deal-in-source-manifest-v1"
DECISION_SCHEMA = "lisjong-riichi-deal-in-decision-record-v1"
LABEL_FACT_SCHEMA = "lisjong-riichi-deal-in-label-fact-record-v1"
SCOPE = "single-opponent-riichi.self-not-riichi.at-least-two-candidate-tile-types.v1"
POLICY = "PlacementAwareSpeedCallPolicy"


class _Recorder:
    def __init__(self, inner, events: list) -> None:
        self._inner = inner
        self._events = events

    def choose_action(self, decision):
        action = self._inner.choose_action(decision)
        self._events.append((decision, action))
        return action


def _round_key(policy_input):
    round_state = policy_input.round
    return (round_state.round_wind.name, round_state.hand_number, round_state.honba)


def canonical_line(value: object) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def extract_records(seed: int, events, ron_results) -> tuple[list[dict], list[dict]]:
    """Return (decision rows, label fact rows) for one hanchan.

    Args:
        events: ``(decision, chosen action)`` for every seat, in the order the
            engine asked for them; the index is the decision ``sequence``.
        ron_results: round key -> (dealing-in seat index, winner seat indexes),
            for rounds that ended on a ron from a discard.
    """
    from lisjong.policy_contract import RiichiState
    from lisjong.policy_contract.action import DiscardAction, RonAction, TsumoAction

    from lisjong_arena.durable_local_game_record import (
        _action_to_value,
        _meld_to_value,
        _policy_input_to_value,
        _tiles_to_value,
    )

    last_discard: dict[tuple, int] = {}
    for sequence, (decision, action) in enumerate(events):
        if isinstance(action, DiscardAction):
            pi = decision.input
            last_discard[(_round_key(pi), int(pi.self_seat))] = sequence

    declared: dict[tuple, int] = {}  # (round, seat) -> riichi declaration discard
    hands: dict[tuple, dict] = {}  # (round, seat) -> latest post-discard hand
    win_options: dict[tuple, list] = {}  # (round, seat) -> offered wins after declared
    decisions: list[dict] = []
    facts: list[dict] = []
    for sequence, (decision, action) in enumerate(events):
        pi = decision.input
        key = _round_key(pi)
        seat = int(pi.self_seat)
        in_riichi = pi.players[seat].riichi is not RiichiState.NONE

        if (key, seat) in declared:
            wins = [
                a
                for a in decision.legal_actions
                if isinstance(a, (RonAction, TsumoAction))
            ]
            if wins:
                win_options.setdefault((key, seat), []).append(
                    {
                        "selected_action": _action_to_value(action, "selected"),
                        "sequence": sequence,
                        "winning_tiles": _tiles_to_value(
                            [a.winning_tile for a in wins], "winning_tiles"
                        ),
                    }
                )

        if isinstance(action, DiscardAction) and in_riichi:
            declared.setdefault((key, seat), sequence)
            concealed = list(pi.own_hand.concealed_tiles)
            concealed.remove(action.tile)
            hands[(key, seat)] = {
                "concealed_tiles": _tiles_to_value(concealed, "concealed"),
                "melds": [
                    _meld_to_value(meld, f"melds[{index}]")
                    for index, meld in enumerate(pi.players[seat].melds)
                ],
                "sequence": sequence,
            }
            continue

        if not isinstance(action, DiscardAction) or in_riichi:
            continue
        riichi = [
            index
            for index, player in enumerate(pi.players)
            if index != seat and player.riichi is not RiichiState.NONE
        ]
        discards = [a for a in decision.legal_actions if isinstance(a, DiscardAction)]
        if len(riichi) != 1 or len({a.tile.tile_type for a in discards}) < 2:
            continue
        r = riichi[0]
        if (key, r) not in hands:
            raise RuntimeError(f"seed {seed} {key}: riichi seat {r} has no hand yet")

        chosen = action.tile.tile_type
        ron_offered = False
        for later_decision, _ in events[sequence + 1 :]:
            later = later_decision.input
            if _round_key(later) != key:
                break
            if int(later.self_seat) == r:
                ron_offered = any(
                    isinstance(a, RonAction)
                    and int(a.target) == seat
                    and a.winning_tile.tile_type == chosen
                    for a in later_decision.legal_actions
                )
                break
        ron = ron_results.get(key)
        dealt_in = (
            last_discard.get((key, seat)) == sequence
            and ron is not None
            and ron[0] == seat
            and r in ron[1]
        )
        record_key = {"seat": seat, "seed": seed, "sequence": sequence}
        decisions.append(
            {
                "key": record_key,
                "legal_actions": [
                    _action_to_value(a, f"legal_actions[{i}]")
                    for i, a in enumerate(decision.legal_actions)
                ],
                "policy_input": _policy_input_to_value(pi),
                "schema": DECISION_SCHEMA,
                "selected_action": _action_to_value(action, "selected"),
            }
        )
        facts.append(
            {
                "key": record_key,
                "riichi_declared_sequence": declared[(key, r)],
                "riichi_hand": hands[(key, r)],
                "riichi_seat": r,
                "schema": LABEL_FACT_SCHEMA,
                "selected_outcome": {"dealt_in": dealt_in, "ron_offered": ron_offered},
                "win_options": list(win_options.get((key, r), ())),
            }
        )
    return decisions, facts


def _play(seed: int) -> tuple[int, list[dict], list[dict]]:
    from lisjong.policies.placement_aware_speed_call import (
        PlacementAwareSpeedCallPolicy,
    )
    from lisjong_engine.seat import Seat as EngineSeat
    from lisjong_engine.win_context import WinMethod, WinOrigin

    from lisjong_arena.lisjong_engine.domain_conversion import seat_from_engine_seat
    from lisjong_arena.lisjong_engine.hanchan import run_policy_hanchan

    events: list = []
    match = run_policy_hanchan(
        {s: _Recorder(PlacementAwareSpeedCallPolicy(), events) for s in EngineSeat},
        seed=seed,
    )
    ron_results = {}
    for completed in match.history:
        result = completed.result
        if (
            getattr(result, "method", None) is WinMethod.RON
            and result.origin is WinOrigin.DISCARD
        ):
            position = completed.position_before
            ron_results[
                (position.prevailing_wind.name, position.hand_number, position.honba)
            ] = (
                int(seat_from_engine_seat(result.source_seat)),
                {int(seat_from_engine_seat(w.seat)) for w in result.winners},
            )
    decisions, facts = extract_records(seed, events, ron_results)
    return seed, decisions, facts


def _seed_range(text: str) -> list[int]:
    first, _, last = text.partition("..")
    seeds = list(range(int(first), int(last or first) + 1))
    if not seeds or seeds[0] < RETIRED_SEED_FIRST or seeds[-1] > RETIRED_SEED_LAST:
        raise argparse.ArgumentTypeError(
            "seeds must stay inside the RETIRED #385 range 931000..931999"
        )
    return seeds


def _revision(name: str) -> str:
    from lisjong_arena.environment_identity import _installed_identity

    return _installed_identity(name).revision


def _arena_revision() -> str:
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return revision + ("-dirty" if dirty else "")


def write_source(
    output: Path,
    *,
    splits: dict[str, list[int]],
    games: list[tuple[int, list[dict], list[dict]]],
    producer: dict[str, str],
) -> dict[str, object]:
    output.mkdir(parents=False, exist_ok=False)
    by_seed = {seed: (decisions, facts) for seed, decisions, facts in games}
    files = {}
    for name, filename, index in (
        ("decisions", "decisions.jsonl", 0),
        ("label_facts", "label_facts.jsonl", 1),
    ):
        rows = [row for seed in sorted(by_seed) for row in by_seed[seed][index]]
        data = "".join(canonical_line(row) for row in rows).encode("utf-8")
        (output / filename).write_bytes(data)
        files[name] = {
            "bytes": len(data),
            "rows": len(rows),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    manifest = {
        "files": files,
        "producer": producer,
        "schema": MANIFEST_SCHEMA,
        "scope": SCOPE,
        "splits": {name: list(splits[name]) for name in SPLITS},
    }
    (output / "manifest.json").write_text(
        json.dumps(
            manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    for name in SPLITS:
        parser.add_argument(f"--{name}", type=_seed_range, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    splits = {name: getattr(arguments, name) for name in SPLITS}
    seeds = [seed for name in SPLITS for seed in splits[name]]
    if len(set(seeds)) != len(seeds):
        parser.error("splits must not share a seed")
    if arguments.output.exists():
        parser.error(f"refusing to overwrite {arguments.output}")
    producer = {
        "arena_revision": _arena_revision(),
        "lisjong_engine_revision": _revision("lisjong-engine"),
        "lisjong_revision": _revision("lisjong"),
        "policy": POLICY,
    }
    if arguments.workers == 1:
        games = [_play(seed) for seed in seeds]
    else:
        with ProcessPoolExecutor(arguments.workers) as executor:
            games = list(executor.map(_play, seeds))
    manifest = write_source(
        arguments.output, splits=splits, games=games, producer=producer
    )
    json.dump(manifest, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
