"""HandBelief hand-truth source producer for lisbun/lisjong#255 (lisjong-arena#453).

Plays Champion self-play hanchan on lisjong-engine and records, for every
observer decision whose legal actions include a discard, the **observed facts**
lisjong needs to label the opponents' canonical ``HandBelief``.  Arena does not
compute any label: lisjong reads these files through
``lisjong.learning.hand_belief_source`` (lisbun/lisjong#256, the v1 contract in
lisjong ``docs/hand-belief-accuracy-source.md``).  Arena only writes that wire
shape.

Output directory (all new; existing paths are refused)::

    manifest.json      lisjong-hand-belief-source-manifest-v1
    decisions.jsonl    lisjong-hand-belief-decision-record-v1    player-safe
    hand_facts.jsonl   lisjong-hand-belief-hand-fact-record-v1   training-only
    coverage.json      Arena producer evidence (not read by lisjong)

- ``decisions.jsonl`` holds what the observer sees (``PolicyInput``, legal
  actions, the selected action).
- ``hand_facts.jsonl`` holds the three opponents' concealed tiles (red fives
  distinguished) and melds.  Each hand is the engine state at the **same
  sequence** as the decision, taken inside the selector call, i.e. before the
  decision's action is applied (v1 contract).  The snapshot is read by the
  recording wrapper only; it never reaches the Policy, ``PolicyInput`` or any
  trace.
- Coverage: the wrapper also collects, at call time, the key of every in-scope
  decision the runner produced.  The written rows must cover exactly that key
  set (count and keys), and every fact row must carry the three opponents
  exactly once.  The strict lisjong reader cannot detect a decision dropped
  from both files together with a matching manifest; this check can.

Seeds: a run uses either the producer pilot (inside the RETIRED
lisjong-arena#385 engine-domain population 931000..931999 and outside the
ranges already used by lisjong#236 / #237 / #245 development, 931100..931399)
or the lisjong#257 measurement population 933000..933399, which is reserved
through the seed registry before generation.  A run never mixes the two.
Generated data is not committed.

Usage::

    LISJONG_SHANTEN_BACKEND=rust python scripts/generate_hand_belief_source_255.py \
        --train 931400..931407 --valid 931408..931408 --test 931409..931409 \
        --workers 4 --output <new directory>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

RETIRED_SEED_FIRST = 931000
RETIRED_SEED_LAST = 931999
USED_DEVELOPMENT_FIRST = 931100
USED_DEVELOPMENT_LAST = 931399
MEASUREMENT_257_FIRST = 933000
MEASUREMENT_257_LAST = 933399
SPLITS = ("train", "valid", "test")
SEAT_COUNT = 4

# lisjong.learning.hand_belief_source (lisjong#256) owns these identifiers.
MANIFEST_SCHEMA = "lisjong-hand-belief-source-manifest-v1"
DECISION_SCHEMA = "lisjong-hand-belief-decision-record-v1"
HAND_FACT_SCHEMA = "lisjong-hand-belief-hand-fact-record-v1"
SCOPE = "all-observer-discard-decisions.v1"
POLICY = "PlacementAwareSpeedCallPolicy"
COVERAGE_SCHEMA = "lisjong-arena-hand-belief-source-coverage-v1"


class HandBeliefSourceProducerError(RuntimeError):
    """The recorded facts are inconsistent or do not cover the runner's decisions."""


_E = HandBeliefSourceProducerError


@dataclass(frozen=True, slots=True)
class SeatHand:
    """One seat's engine-side hand at a decision point (lisjong value types)."""

    concealed_tiles: tuple
    melds: tuple


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    """One selector call: the decision context, the chosen action and the
    same-state hands of all four seats (indexed by lisjong seat)."""

    decision: object
    action: object
    hands: tuple[SeatHand, ...]


def _is_in_scope(legal_actions: Iterable[object]) -> bool:
    from lisjong.policy_contract.action import DiscardAction

    return any(isinstance(action, DiscardAction) for action in legal_actions)


def capture_hands(round_state) -> tuple[SeatHand, ...]:
    """Read every seat's concealed tiles and melds from the engine round.

    Called inside the selector, before the engine applies the choice, so the
    values are the decision-point state.
    """
    from lisjong_engine.public_state import public_meld, public_tile
    from lisjong_engine.seat import Seat as EngineSeat

    from lisjong_arena.lisjong_engine.domain_conversion import (
        public_meld_from_engine_meld,
        seat_from_engine_seat,
        tile_from_public_tile,
    )

    by_seat: dict[int, SeatHand] = {}
    for engine_seat in EngineSeat:
        by_seat[int(seat_from_engine_seat(engine_seat))] = SeatHand(
            concealed_tiles=tuple(
                tile_from_public_tile(public_tile(tile))
                for tile in round_state.hand_tiles(engine_seat)
            ),
            melds=tuple(
                public_meld_from_engine_meld(public_meld(meld))
                for meld in round_state.melds(engine_seat)
            ),
        )
    return tuple(by_seat[index] for index in range(SEAT_COUNT))


class _Recorder:
    """Wraps one seat's Policy; records each call with the engine hands.

    ``scope_keys`` is the runner-side coverage reference: it is filled at call
    time from the legal actions, independently of record extraction.
    """

    def __init__(self, inner, match_state, events: list, scope_keys: list) -> None:
        self._inner = inner
        self._match_state = match_state
        self._events = events
        self._scope_keys = scope_keys

    def choose_action(self, decision):
        round_state = self._match_state.active_round
        if round_state is None:
            raise _E("a decision was requested without an active round")
        hands = capture_hands(round_state)
        sequence = len(self._events)
        if _is_in_scope(decision.legal_actions):
            self._scope_keys.append((int(decision.input.self_seat), sequence))
        action = self._inner.choose_action(decision)
        self._events.append(RecordedDecision(decision, action, hands))
        return action


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


def _tile_counter(tiles) -> dict:
    from collections import Counter

    return Counter((tile.tile_type, tile.is_red) for tile in tiles)


def _check_same_state(seed: int, sequence: int, recorded: RecordedDecision) -> None:
    """Fail closed unless the hands and the observer's PolicyInput agree."""
    policy_input = recorded.decision.input
    seat = int(policy_input.self_seat)
    context = f"seed {seed} sequence {sequence}"
    own = recorded.hands[seat]
    if _tile_counter(own.concealed_tiles) != _tile_counter(
        policy_input.own_hand.concealed_tiles
    ):
        raise _E(f"{context}: the engine hand of the observer differs from PolicyInput")
    for index in range(SEAT_COUNT):
        if tuple(recorded.hands[index].melds) != tuple(
            policy_input.players[index].melds
        ):
            raise _E(f"{context}: seat {index} melds differ from the public state")


def extract_records(
    seed: int, events: Sequence[RecordedDecision]
) -> tuple[list[dict], list[dict]]:
    """Return (decision rows, hand fact rows) for one hanchan.

    ``events`` are every seat's decisions in the order the engine asked for
    them; the index is the decision ``sequence``.
    """
    from lisjong_arena.durable_local_game_record import (
        _action_to_value,
        _meld_to_value,
        _policy_input_to_value,
        _tiles_to_value,
    )

    decisions: list[dict] = []
    facts: list[dict] = []
    for sequence, recorded in enumerate(events):
        decision = recorded.decision
        if not _is_in_scope(decision.legal_actions):
            continue
        _check_same_state(seed, sequence, recorded)
        seat = int(decision.input.self_seat)
        key = {"seat": seat, "seed": seed, "sequence": sequence}
        decisions.append(
            {
                "key": key,
                "legal_actions": [
                    _action_to_value(a, f"legal_actions[{i}]")
                    for i, a in enumerate(decision.legal_actions)
                ],
                "policy_input": _policy_input_to_value(decision.input),
                "schema": DECISION_SCHEMA,
                "selected_action": _action_to_value(recorded.action, "selected"),
            }
        )
        facts.append(
            {
                "key": key,
                "opponents": [
                    {
                        "concealed_tiles": _tiles_to_value(
                            recorded.hands[other].concealed_tiles,
                            f"opponents[{other}].concealed_tiles",
                        ),
                        "melds": [
                            _meld_to_value(meld, f"opponents[{other}].melds[{i}]")
                            for i, meld in enumerate(recorded.hands[other].melds)
                        ],
                        "seat": other,
                        "sequence": sequence,
                    }
                    for other in range(SEAT_COUNT)
                    if other != seat
                ],
                "schema": HAND_FACT_SCHEMA,
            }
        )
    return decisions, facts


def _row_key(row: dict) -> tuple[int, int, int]:
    key = row["key"]
    return (key["seed"], key["seat"], key["sequence"])


def verify_coverage(
    seed: int,
    scope_keys: Sequence[tuple[int, int]],
    decisions: Sequence[dict],
    facts: Sequence[dict],
) -> None:
    """Check that the rows cover exactly the runner's in-scope decisions.

    ``scope_keys`` holds ``(seat, sequence)`` collected by the runner at call
    time.  A decision missing from both files, an extra row, a duplicate, or a
    fact row whose opponents are not the other three seats exactly once each
    fails closed.
    """
    expected = [(seed, seat, sequence) for seat, sequence in scope_keys]
    if len(set(expected)) != len(expected):
        raise _E(f"seed {seed}: the runner reported a decision twice")
    for name, rows in (("decision", decisions), ("hand fact", facts)):
        keys = [_row_key(row) for row in rows]
        if len(keys) != len(expected):
            raise _E(
                f"seed {seed}: {len(keys)} {name} rows for "
                f"{len(expected)} in-scope decisions"
            )
        if len(set(keys)) != len(keys):
            raise _E(f"seed {seed}: duplicate {name} key")
        if set(keys) != set(expected):
            raise _E(f"seed {seed}: {name} keys do not match the runner's decisions")
    for row in facts:
        _, seat, sequence = _row_key(row)
        opponents = [hand["seat"] for hand in row["opponents"]]
        if sorted(opponents) != [s for s in range(SEAT_COUNT) if s != seat] or len(
            opponents
        ) != len(set(opponents)):
            raise _E(
                f"seed {seed} sequence {sequence}: opponents must be the other "
                "three seats exactly once"
            )
        if any(hand["sequence"] != sequence for hand in row["opponents"]):
            raise _E(
                f"seed {seed} sequence {sequence}: an opponent hand is not the "
                "decision-point snapshot"
            )


def _keys_digest(keys: Iterable[tuple[int, int, int]]) -> str:
    text = "".join(f"{seed},{seat},{sequence}\n" for seed, seat, sequence in keys)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _play(seed: int) -> tuple[int, list[dict], list[dict], list[tuple[int, int]]]:
    from lisjong.policies.placement_aware_speed_call import (
        PlacementAwareSpeedCallPolicy,
    )
    from lisjong_engine.driver import run_hanchan
    from lisjong_engine.match_state import MatchState
    from lisjong_engine.seat import Seat as EngineSeat

    from lisjong_arena.lisjong_engine.policy_selector import build_seat_selectors

    # Same composition as lisjong_engine.hanchan.run_policy_hanchan (default
    # RuleSet), holding the MatchState so the wrapper can read the hands.
    match_state = MatchState(seed=seed, rules=None)
    events: list[RecordedDecision] = []
    scope_keys: list[tuple[int, int]] = []
    policies = {
        seat: _Recorder(
            PlacementAwareSpeedCallPolicy(), match_state, events, scope_keys
        )
        for seat in EngineSeat
    }
    run_hanchan(match_state, build_seat_selectors(policies))
    decisions, facts = extract_records(seed, events)
    verify_coverage(seed, scope_keys, decisions, facts)
    return seed, decisions, facts, scope_keys


def _seed_range(text: str) -> list[int]:
    first, _, last = text.partition("..")
    seeds = list(range(int(first), int(last or first) + 1))
    if not seeds:
        raise argparse.ArgumentTypeError("empty seed range")
    if MEASUREMENT_257_FIRST <= seeds[0] and seeds[-1] <= MEASUREMENT_257_LAST:
        return seeds
    if seeds[0] < RETIRED_SEED_FIRST or seeds[-1] > RETIRED_SEED_LAST:
        raise argparse.ArgumentTypeError(
            "seeds must stay inside the RETIRED #385 range 931000..931999 (pilot)"
            " or the lisjong#257 measurement range 933000..933399"
        )
    if seeds[0] <= USED_DEVELOPMENT_LAST and seeds[-1] >= USED_DEVELOPMENT_FIRST:
        raise argparse.ArgumentTypeError(
            "931100..931399 were already used by lisjong#236 / #237 / #245"
        )
    return seeds


def _is_measurement(seed: int) -> bool:
    return MEASUREMENT_257_FIRST <= seed <= MEASUREMENT_257_LAST


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
    games: Sequence[tuple[int, list[dict], list[dict], list[tuple[int, int]]]],
    producer: dict[str, str],
) -> dict[str, object]:
    """Write the v1 files plus Arena's coverage evidence.

    Coverage is re-checked over the whole run immediately before writing.
    """
    output.mkdir(parents=False, exist_ok=False)
    by_seed = {}
    for seed, decisions, facts, scope_keys in games:
        verify_coverage(seed, scope_keys, decisions, facts)
        by_seed[seed] = (decisions, facts, scope_keys)
    if sorted(by_seed) != sorted(seed for name in SPLITS for seed in splits[name]):
        raise _E("the games do not match the split seeds")
    files = {}
    for name, filename, index in (
        ("decisions", "decisions.jsonl", 0),
        ("hand_facts", "hand_facts.jsonl", 1),
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
    coverage = {
        "schema": COVERAGE_SCHEMA,
        "seeds": {
            str(seed): {
                "in_scope_decisions": len(by_seed[seed][2]),
                "keys_sha256": _keys_digest(
                    sorted(
                        (seed, seat, sequence) for seat, sequence in by_seed[seed][2]
                    )
                ),
            }
            for seed in sorted(by_seed)
        },
        "total_in_scope_decisions": sum(len(v[2]) for v in by_seed.values()),
    }
    (output / "coverage.json").write_text(
        json.dumps(coverage, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def verify_source_coverage(directory: Path) -> int:
    """Re-check written files against ``coverage.json`` (keys, counts, opponents)."""
    coverage = json.loads((directory / "coverage.json").read_text(encoding="utf-8"))
    if coverage.get("schema") != COVERAGE_SCHEMA:
        raise _E("coverage.json has an unsupported schema")
    rows = {}
    for name in ("decisions", "hand_facts"):
        with (directory / f"{name}.jsonl").open(encoding="utf-8") as stream:
            rows[name] = [json.loads(line) for line in stream]
    seeds = coverage["seeds"]
    total = 0
    for seed_text, entry in seeds.items():
        seed = int(seed_text)
        for name in ("decisions", "hand_facts"):
            keys = sorted(_row_key(r) for r in rows[name] if r["key"]["seed"] == seed)
            if len(keys) != entry["in_scope_decisions"] or (
                _keys_digest(keys) != entry["keys_sha256"]
            ):
                raise _E(f"seed {seed}: {name} rows do not match the runner coverage")
        total += entry["in_scope_decisions"]
    for name in ("decisions", "hand_facts"):
        if len(rows[name]) != total or any(
            str(r["key"]["seed"]) not in seeds for r in rows[name]
        ):
            raise _E(f"{name} has rows outside the recorded coverage")
    for row in rows["hand_facts"]:
        _, seat, sequence = _row_key(row)
        opponents = [hand["seat"] for hand in row["opponents"]]
        if opponents != [s for s in range(SEAT_COUNT) if s != seat]:
            raise _E(f"sequence {sequence}: opponents must be the other three seats")
    return total


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "verify-coverage":
        parser = argparse.ArgumentParser(prog="verify-coverage")
        parser.add_argument("source", type=Path)
        arguments = parser.parse_args(argv[1:])
        total = verify_source_coverage(arguments.source)
        print(json.dumps({"in_scope_decisions": total, "status": "ok"}))
        return 0
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
    if len({_is_measurement(seed) for seed in seeds}) != 1:
        parser.error("a run must not mix pilot and measurement seeds")
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
