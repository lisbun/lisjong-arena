"""Offline engine-owned ron source projection (Arena #457; lisjong #262).

Only the recording wrapper sees privileged checkpoints. Policies receive the
unchanged DecisionContext. This producer neither derives legal actions nor labels.
The CLI requires a registry allocation bound to its clean, merged revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_hand_belief_source_255 as base  # noqa: E402
from lisjong.policy_contract import (  # noqa: E402
    AnkanAction,
    Discard,
    DiscardAction,
    OwnHandState,
    PlayerPublicState,
    PolicyInput,
    RiichiAction,
    RiichiState,
    RoundState,
)
from lisjong.policy_contract.tile import tile_sort_key  # noqa: E402
from lisjong_engine.meld import Kakan  # noqa: E402
from lisjong_engine.public_state import public_meld, public_tile  # noqa: E402
from lisjong_engine.round_event import (  # noqa: E402
    DrawSource,
    KanConfirmedEvent,
    KanDeclaredEvent,
    ReactionsResolvedEvent,
    RiichiFinalizedEvent,
    TileDiscardedEvent,
    TileDrawnEvent,
)
from lisjong_engine.transaction_observation import (
    TransactionStepKind as Kind,  # noqa: E402
)

from lisjong_arena import seed_registry  # noqa: E402
from lisjong_arena.durable_local_game_record import (  # noqa: E402
    _action_to_value,
    _policy_input_to_value,
    _tile_to_value,
)
from lisjong_arena.lisjong_engine.action_mapping import (  # noqa: E402
    internal_action_from_descriptor,
)
from lisjong_arena.lisjong_engine.domain_conversion import (  # noqa: E402
    public_meld_from_engine_meld,
    seat_from_engine_seat,
    tile_from_public_tile,
    wind_from_engine_wind,
)

OWNER = "lisbun/lisjong-arena#457"
PROTOCOL = "ron-legal-source-pilot-v1"
POPULATION = "ron-legal-source-pilot-2-hanchan"
SPLIT = "TRAIN1-VALID1-TEST0"
CONTEXT = "project-standard-normal-discard-ron-v1"


class RonProducerError(ValueError):
    """Incomplete, unsupported or inconsistent execution evidence."""


def seat(value):
    return int(seat_from_engine_seat(value))


def tile(value):
    return tile_from_public_tile(public_tile(value))


def context(value):
    reason = value.missed_ron_furiten
    return {
        "missed_ron_state": "none" if reason is None else reason.value,
        "riichi_status": value.riichi_status.value,
        "is_ippatsu": value.is_ippatsu,
    }


def draw_kind(value):
    if value is None:
        return None
    if value is DrawSource.LIVE_WALL:
        return "normal"
    if value is DrawSource.RINSHAN:
        return "rinshan"
    raise RonProducerError("unsupported draw source")


def rules_projection(rules):
    names = (
        "rounded_mangan_enabled",
        "counted_yakuman_enabled",
        "multiple_yakuman_enabled",
        "double_wind_pair_fu",
        "kokushi_ankan_chankan_enabled",
        "triple_ron_abortive_draw",
    )
    from lisjong_engine.rules import RuleSet

    if rules != RuleSet.default():
        raise RonProducerError("pilot requires the exact project-standard RuleSet")
    result = {name: getattr(rules, name) for name in names}
    # Open tanyao and red tiles are fixed engine semantics, not RuleSet switches.
    result.update(kuitan_enabled=True, red_dora_enabled=True)
    result.update(
        identity="project-standard-v1",
        double_yakuman_variants=sorted(v.value for v in rules.double_yakuman_variants),
        ron_resolution_policy=rules.ron_resolution_policy.value,
    )
    from lisjong.learning.ron_legal_source import RULES

    if result != RULES:
        raise RonProducerError("RuleSet is not the fixed ron source projection")
    return result


class Recorder:
    """One hanchan's offline recorder, independent of the Policy input path."""

    def __init__(self, seed, match):
        self.seed = seed
        self.match = match
        self.events = []
        self.scope_keys = []
        self.facts = []
        self.history = []
        self.coverage = []
        self.checkpoint = None
        self.raw_checkpoint = None
        self.round_id = None
        self.index = 0
        self.orders = {}
        self.pending_riichi = None
        self.reaction_id = None
        self.next_reaction_id = 0
        self.expected_revision = 1
        self.consumed = 0
        self.terminal = False

    def wrap(self, policy):
        recorder = self

        class RecordingPolicy:
            def choose_action(self, decision):
                if recorder.checkpoint is None or recorder.terminal:
                    raise RonProducerError("selector outside a recorded round")
                sequence = len(recorder.events)
                wrapped = base._Recorder(
                    policy, recorder.match, recorder.events, recorder.scope_keys
                )
                action = wrapped.choose_action(decision)
                counter = recorder.coverage[-1]
                counter["selectors"] += 1
                if base._is_in_scope(decision.legal_actions):
                    counter["decisions"] += 1
                    own = int(decision.input.self_seat)
                    recorder.facts.append(
                        {
                            "schema": "lisjong-ron-legal-fact-record-v1",
                            "key": {
                                "seed": recorder.seed,
                                "seat": own,
                                "sequence": sequence,
                            },
                            "round_id": recorder.round_id,
                            "history_boundary": recorder.index,
                            "opponents": [
                                {
                                    "seat": other,
                                    "sequence": sequence,
                                    "context": recorder.checkpoint["contexts"][other],
                                }
                                for other in range(4)
                                if other != own
                            ],
                        }
                    )
                return action

        return RecordingPolicy()

    def project_checkpoint(self, raw):
        position = self.match.position
        contributions = [
            f.contribution
            for f in raw.riichi_finalizations
            if f.contribution is not None
        ]
        players = []
        for value in raw.seats:
            own = seat(value.seat)
            status = RiichiState.NONE
            if value.riichi_status.value != "none":
                status = RiichiState.ACCEPTED
            elif self.pending_riichi == own:
                status = RiichiState.DECLARED
            players.append(
                PlayerPublicState(
                    score=self.match.scores[value.seat]
                    - sum(c.points for c in contributions if c.seat is value.seat),
                    discards=tuple(
                        Discard(
                            tile=tile(d.tile),
                            tsumogiri=d.is_tsumogiri,
                            order=self.orders[d.tile.id],
                            called_by=None
                            if d.called_by is None
                            else seat_from_engine_seat(d.called_by),
                        )
                        for d in value.discards
                    ),
                    melds=tuple(
                        public_meld_from_engine_meld(public_meld(m))
                        for m in value.melds
                    ),
                    riichi=status,
                )
            )
        round_value = RoundState(
            round_wind=wind_from_engine_wind(raw.prevailing_wind),
            hand_number=position.hand_number,
            dealer_seat=seat_from_engine_seat(raw.dealer_seat),
            honba=position.honba,
            riichi_sticks=position.riichi_sticks + len(contributions),
            dora_indicators=tuple(tile(t) for t in raw.revealed_dora_indicators),
            live_wall_tiles_remaining=raw.live_tiles_remaining,
        )
        views = []
        for value in raw.seats:
            # Same sorting as the existing engine observation projection. The
            # checkpoint hand already contains its drawn tile exactly once.
            hand = sorted(
                (tile(t) for t in value.hand_tiles),
                key=tile_sort_key,
            )
            views.append(
                _policy_input_to_value(
                    PolicyInput(
                        self_seat=seat_from_engine_seat(value.seat),
                        round=round_value,
                        players=tuple(players),
                        own_hand=OwnHandState(
                            concealed_tiles=tuple(hand),
                            drawn_tile=None
                            if value.drawn_tile is None
                            else tile(value.drawn_tile),
                        ),
                    )
                )
            )
        return {"views": views, "contexts": [context(s) for s in raw.seats]}

    def reaction(self, raw, decisions, skipped=False):
        if self.raw_checkpoint is None:
            raise RonProducerError("reaction requires a preceding checkpoint")
        previous = self.raw_checkpoint
        if skipped:
            # The engine explicitly supplied REACTION_WINDOW_SKIPPED. Ankan
            # has no ron opportunity under the fixed rule; don't invent one.
            if isinstance(self.events[-1].action, AnkanAction):
                return None
            source = int(self.events[-1].action.actor)
            winning = previous.seats[source].discards[-1].tile
            origin = "discard"
            resolution = None
        else:
            resolutions = [
                e.resolution
                for e in raw.events
                if isinstance(e, ReactionsResolvedEvent)
            ]
            if len(resolutions) != 1:
                raise RonProducerError(
                    "reaction must have exactly one engine resolution"
                )
            resolution = resolutions[0]
            source = seat(resolution.source_seat)
            origin = resolution.origin.value
            if origin == "discard":
                winning = previous.seats[source].discards[-1].tile
            elif origin == "kakan" and previous.pending_kakan is not None:
                winning = previous.pending_kakan.kakan.added_tile
            else:
                raise RonProducerError(
                    "unexpected kan reaction under project-standard rules"
                )
            if winning.id != resolution.target_tile_id:
                raise RonProducerError(
                    "reaction target does not match engine checkpoint"
                )
        self.reaction_id = f"{self.round_id}:{self.next_reaction_id}"
        self.next_reaction_id += 1
        candidates = []
        for other in range(4):
            if other == source:
                continue
            entry = decisions.get(other)
            if entry is None:
                if not skipped:
                    raise RonProducerError("explicit reaction is missing a selector")
                candidates.append(
                    {
                        "seat": other,
                        "sequence": None,
                        "legal_actions": [],
                        "selected_action": None,
                    }
                )
            else:
                sequence, choice = entry
                candidates.append(
                    {
                        "seat": other,
                        "sequence": sequence,
                        "legal_actions": [
                            _action_to_value(a, "candidate")
                            for a in choice.decision.legal_actions
                        ],
                        "selected_action": _action_to_value(choice.action, "selected"),
                    }
                )
        turn_order = [(source + offset) % 4 for offset in range(1, 4)]

        def ordered(values):
            return [s for s in turn_order if s in {seat(v) for v in values}]

        is_call = resolution is not None and resolution.is_call
        evidence = {
            "reaction_id": self.reaction_id,
            "origin": origin,
            "source_seat": source,
            "winning_tile": _tile_to_value(tile(winning), "winning"),
            "discard_draw_kind": self.last_discard_source
            if origin == "discard"
            else None,
            "candidates": candidates,
            "ron_capable": [] if skipped else ordered(resolution.ron_capable_seats),
            "ron_selected": [] if skipped else ordered(resolution.ron_selected_seats),
            "ron_awarded": [] if skipped else ordered(resolution.ron_awarded_seats),
            "ron_passed": [] if skipped else ordered(resolution.ron_passed_seats),
            "resolution": "pass"
            if skipped
            else "ron"
            if resolution.is_ron
            else "call"
            if is_call
            else "pass",
            "resolved_action": None
            if not is_call
            else _action_to_value(
                decisions[seat(resolution.resolved_seat)][1].action, "resolved"
            ),
        }
        return {"kind": "reaction", "evidence": evidence}

    def observe(self, transaction):
        kinds = [s.kind for s in transaction.steps]
        if kinds == [Kind.ROUND_STARTED]:
            if self.coverage and not self.terminal:
                raise RonProducerError("new round before the previous round ended")
            if transaction.round_ordinal != len(self.coverage) + 1:
                raise RonProducerError("round ordinal is missing or repeated")
            self.round_id = str(transaction.round_ordinal)
            self.index = 0
            self.orders = {}
            self.pending_riichi = None
            self.reaction_id = None
            self.next_reaction_id = 0
            self.expected_revision = transaction.revision
            self.terminal = False
            self.coverage.append(
                {
                    "seed": self.seed,
                    "round_id": self.round_id,
                    "transitions": 1,
                    "reactions": 0,
                    "decisions": 0,
                    "selectors": 0,
                }
            )
        else:
            if self.terminal or transaction.round_ordinal != len(self.coverage):
                raise RonProducerError("transaction outside the active round")
            if transaction.revision != self.expected_revision + 1:
                raise RonProducerError("committed transaction is missing or repeated")
            self.expected_revision = transaction.revision
            self.coverage[-1]["transitions"] += 1
        decisions = {}
        commit = transaction.selector_decision
        if commit is not None:
            for engine_decision in commit.decisions:
                sequence = self.consumed
                if sequence >= len(self.events):
                    raise RonProducerError("commit has an unrecorded selector")
                record = self.events[sequence]
                action = internal_action_from_descriptor(
                    engine_decision.selected_action, engine_decision.observation
                )
                from lisjong_arena.lisjong_engine.decision import build_decision

                projected = build_decision(
                    engine_decision.observation, engine_decision.legal_actions
                ).context
                if record.decision != projected or record.action != action:
                    raise RonProducerError(
                        "selector snapshot/choice differs from committed decision"
                    )
                decisions[seat(engine_decision.seat)] = (sequence, record)
                self.consumed += 1
        # Count engine events independently of reaction projection/emission.
        # Every discard and kakan declaration has a reaction under fixed rules;
        # ankan has none, and daiminkan uses its originating discard reaction.
        self.coverage[-1]["reactions"] += sum(
            isinstance(event, TileDiscardedEvent)
            or isinstance(event, KanDeclaredEvent)
            and isinstance(event.kan, Kakan)
            for raw in transaction.steps
            for event in raw.events
        )
        steps = []
        for raw in transaction.steps:
            for e in raw.events:
                if isinstance(e, TileDiscardedEvent):
                    if e.tile.id in self.orders:
                        raise RonProducerError("physical discard recorded twice")
                    self.orders[e.tile.id] = len(self.orders)
            sequences = []
            if raw.kind is Kind.ROUND_STARTED:
                event = {"kind": "round_start"}
            elif raw.kind is Kind.DRAW:
                draws = [e for e in raw.events if isinstance(e, TileDrawnEvent)]
                if len(draws) != 1:
                    raise RonProducerError("draw must have one draw event")
                event = {
                    "kind": "draw",
                    "seat": seat(draws[0].seat),
                    "draw_kind": draw_kind(draws[0].source),
                }
            elif raw.kind is Kind.TURN_CHOICE:
                if len(decisions) != 1:
                    raise RonProducerError(
                        "turn choice requires one committed selector"
                    )
                sequence, record = next(iter(decisions.values()))
                sequences = [sequence]
                event = {
                    "kind": "progress",
                    "sequence": sequence,
                    "action": _action_to_value(record.action, "progress"),
                }
                if isinstance(record.action, RiichiAction):
                    self.pending_riichi = int(record.action.actor)
                if isinstance(record.action, DiscardAction):
                    self.last_discard_source = draw_kind(
                        self.raw_checkpoint.seats[
                            int(record.action.actor)
                        ].drawn_tile_source
                    )
            elif raw.kind in (Kind.REACTION_RESOLVED, Kind.REACTION_WINDOW_SKIPPED):
                skipped = raw.kind is Kind.REACTION_WINDOW_SKIPPED
                event = self.reaction(raw, decisions, skipped)
                if event is None:
                    continue
                if not skipped:
                    sequences = sorted(s for s, _ in decisions.values())
            elif raw.kind is Kind.RIICHI_FINALIZED:
                finals = [
                    e.finalization
                    for e in raw.events
                    if isinstance(e, RiichiFinalizedEvent)
                ]
                if len(finals) != 1 or self.reaction_id is None:
                    raise RonProducerError("riichi finalization requires its reaction")
                f = finals[0]
                event = {
                    "kind": "riichi_established"
                    if f.is_established
                    else "riichi_cancelled",
                    "seat": seat(f.seat),
                    "reaction_id": self.reaction_id,
                }
                if f.is_established:
                    event.update(
                        riichi_status=f.riichi_status.value, is_ippatsu=f.grants_ippatsu
                    )
                self.pending_riichi = None
            elif raw.kind is Kind.KAN_CONFIRMED:
                kans = [e for e in raw.events if isinstance(e, KanConfirmedEvent)]
                if len(kans) != 1:
                    raise RonProducerError("kan confirmation must have one kan event")
                event = {"kind": "kan_confirmed", "seat": seat(kans[0].seat)}
            elif raw.kind is Kind.ROUND_ENDED:
                event = {"kind": "round_result"}
                self.terminal = True
            else:
                raise RonProducerError("unknown engine transaction step")
            self.checkpoint = self.project_checkpoint(raw.checkpoint)
            self.raw_checkpoint = raw.checkpoint
            steps.append(
                {
                    "selector_sequences": sequences,
                    "event": event,
                    "checkpoint": self.checkpoint,
                }
            )
        self.append_row(steps)
        if self.terminal:
            self.coverage[-1]["transitions"] += 1
            self.append_row(
                [
                    {
                        "selector_sequences": [],
                        "event": {"kind": "round_end"},
                        "checkpoint": self.checkpoint,
                    }
                ]
            )

    def append_row(self, steps):
        if not steps:
            raise RonProducerError("transaction has no representable semantic step")
        self.history.append(
            {
                "schema": "lisjong-ron-legal-history-record-v1",
                "seed": self.seed,
                "round_id": self.round_id,
                "index": self.index,
                "steps": steps,
            }
        )
        self.index += 1

    def finish(self):
        if not self.terminal or self.consumed != len(self.events):
            raise RonProducerError("incomplete hanchan or uncommitted selector")
        decisions, hands = base.extract_records(self.seed, self.events)
        base.verify_coverage(self.seed, self.scope_keys, decisions, hands)
        if [r["key"] for r in self.facts] != [r["key"] for r in decisions]:
            raise RonProducerError("ron facts do not match base decisions")
        return (
            (self.seed, decisions, hands, self.scope_keys),
            self.facts,
            self.history,
            self.coverage,
        )


def play(seed):
    from lisjong.policies.placement_aware_speed_call import (
        PlacementAwareSpeedCallPolicy,
    )
    from lisjong_engine.driver import run_hanchan
    from lisjong_engine.match_state import MatchState
    from lisjong_engine.rules import RuleSet
    from lisjong_engine.seat import Seat as EngineSeat

    from lisjong_arena.lisjong_engine.policy_selector import build_seat_selectors

    match = MatchState(seed=seed, rules=RuleSet.default())
    rules_projection(match.rules)
    recorder = Recorder(seed, match)
    policies = {s: recorder.wrap(PlacementAwareSpeedCallPolicy()) for s in EngineSeat}
    run_hanchan(
        match,
        build_seat_selectors(policies),
        on_transaction_observation=recorder.observe,
    )
    return recorder.finish()


def authorize(ledger, identity, seeds, revision):
    if len(seeds) != 2 or seeds[0] >= seeds[1]:
        raise RonProducerError("pilot requires exactly two distinct ascending seeds")
    binding = seed_registry.allocation_binding(ledger, identity)
    record = seed_registry.require_allocation_binding(
        ledger,
        binding,
        seeds=seeds,
        owner_issue=OWNER,
        protocol=PROTOCOL,
        seed_domain=seed_registry.LISJONG_ENGINE_HANCHAN_SEED_DOMAIN,
        population=POPULATION,
        split=SPLIT,
    )
    if record["state"] != seed_registry.RESERVED:
        raise RonProducerError("pilot requires a fresh RESERVED allocation")
    if record["arena_revision"] != revision or "-dirty" in revision:
        raise RonProducerError("allocation must match the clean executing revision")
    return binding


def write_population(output, games, splits, producer, rules):
    output.mkdir(parents=False, exist_ok=False)
    base.write_source(
        output / "base", splits=splits, games=[g[0] for g in games], producer=producer
    )
    extension = output / "ron"
    extension.mkdir()
    files = {}
    for field, filename, index in (
        ("ron_facts", "ron_facts.jsonl", 1),
        ("ron_history", "ron_history.jsonl", 2),
    ):
        rows = [row for game in games for row in game[index]]
        data = "".join(base.canonical_line(row) for row in rows).encode("utf-8")
        (extension / filename).write_bytes(data)
        files[field] = {
            "bytes": len(data),
            "rows": len(rows),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    manifest = {
        "schema": "lisjong-ron-legal-source-manifest-v1",
        "base_manifest_sha256": hashlib.sha256(
            (output / "base" / "manifest.json").read_bytes()
        ).hexdigest(),
        "context_protocol": CONTEXT,
        "rules": rules,
        "producer": producer,
        "splits": splits,
        "files": files,
        "coverage": [row for g in games for row in g[3]],
    }
    (extension / "manifest.json").write_bytes(
        (
            json.dumps(
                manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2
            )
            + "\n"
        ).encode("utf-8")
    )
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-ledger", type=Path, required=True)
    parser.add_argument("--allocation-identity", required=True)
    parser.add_argument("--seeds", type=int, nargs=2, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    from lisjong.belief.ron_legal_ground_truth import require_scoring_backend
    from lisjong.learning.ron_legal_source import (
        read_labelled_ron_source,
        read_ron_source,
    )
    from lisjong_engine.rules import RuleSet

    from lisjong_arena.environment_verify import verify_environment

    plan_path = args.output.with_name(args.output.name + ".plan.json")
    if args.output.exists() or plan_path.exists():
        raise RonProducerError("refusing to overwrite output or execution plan")
    # Verify the exact installed Git dependencies against the pinned project.
    checked = verify_environment(Path(__file__).resolve().parents[1] / "pyproject.toml")
    if not checked.ok:
        raise RonProducerError(f"environment mismatch: {checked.errors}")
    revision = base._arena_revision()
    ledger = seed_registry.load_ledger(args.seed_ledger, strict_serialization=False)
    binding = authorize(ledger, args.allocation_identity, args.seeds, revision)
    require_scoring_backend()
    import _lisjong_native

    if _lisjong_native.SOURCE_REVISION != base._revision("lisjong"):
        raise RonProducerError(
            "native scorer source revision differs from installed lisjong"
        )
    if os.environ.get("LISJONG_SHANTEN_BACKEND") != "rust":
        raise RonProducerError("pilot requires LISJONG_SHANTEN_BACKEND=rust")
    rules = rules_projection(RuleSet.default())
    producer = {
        "arena_revision": revision,
        "lisjong_revision": base._revision("lisjong"),
        "lisjong_engine_revision": base._revision("lisjong-engine"),
        "policy": base.POLICY,
    }
    splits = {"train": [args.seeds[0]], "valid": [args.seeds[1]], "test": []}
    plan = {
        "schema": "lisjong-arena-ron-source-pilot-plan-v1",
        "allocation": binding,
        "producer": producer,
        "seeds": args.seeds,
        "splits": splits,
        "rules": rules,
        "runtime": sys.version,
        "shanten_backend": "rust",
        "native_source_revision": _lisjong_native.SOURCE_REVISION,
        "output": str(args.output.resolve()),
    }
    plan_bytes = (json.dumps(plan, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with plan_path.open("xb") as stream:
        stream.write(plan_bytes)
    print(json.dumps(plan, sort_keys=True), flush=True)
    games = [play(seed) for seed in args.seeds]
    manifest = write_population(args.output, games, splits, producer, rules)
    base.verify_source_coverage(args.output / "base")
    source = read_ron_source(args.output / "ron", base_directory=args.output / "base")
    _, labelled = read_labelled_ron_source(
        args.output / "ron", base_directory=args.output / "base"
    )
    strata = {
        name: {
            "opponent_snapshots": 0,
            "ron_positive_snapshots": 0,
            "ron_positive_tiles": 0,
        }
        for name in ("riichi", "closed_nonriichi", "open")
    }
    for labelled_decision in labelled:
        for opponent in labelled_decision.opponents:
            player = labelled_decision.decision.policy_input.players[opponent.seat]
            from lisjong.policy_contract import MeldKind

            name = (
                "riichi"
                if player.riichi is RiichiState.ACCEPTED
                else "open"
                if any(m.kind is not MeldKind.ANKAN for m in player.melds)
                else "closed_nonriichi"
            )
            positives = sum(
                value > 0 for value in opponent.truth.ron_legal_probability_raw
            )
            strata[name]["opponent_snapshots"] += 1
            strata[name]["ron_positive_snapshots"] += int(positives > 0)
            strata[name]["ron_positive_tiles"] += positives
    observed = {}
    for game in games:
        for row in game[2]:
            for step in row["steps"]:
                kind = step["event"]["kind"]
                observed[kind] = observed.get(kind, 0) + 1
    report = {
        "schema": "lisjong-arena-ron-source-pilot-completion-v1",
        "allocation": binding,
        "execution_plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "producer": producer,
        "runtime": sys.version,
        "native_source_revision": _lisjong_native.SOURCE_REVISION,
        "strata": strata,
        "observed_event_counts": observed,
        "shanten_backend": "rust",
        "seeds": args.seeds,
        "decisions": len(source.decisions),
        "labelled_decisions": len(labelled),
        "coverage": manifest["coverage"],
        "base_manifest_sha256": manifest["base_manifest_sha256"],
        "files": manifest["files"],
    }
    (args.output / "complete.json").write_bytes(
        (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
