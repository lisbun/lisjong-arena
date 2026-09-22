"""Canonical Arena-owned seed allocation ledger.

This is an owner-scoped Arena ledger, not an ecosystem-global integer registry.
Collisions are defined inside one explicit seed_domain; scientific protocols may
apply stricter exclusions by querying all Arena allocations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

LEDGER_SCHEMA_VERSION = "arena-seed-ledger-v1"
ALLOCATION_SCHEMA_VERSION = "arena-seed-allocation-v1"
OWNER_REPOSITORY = "lisbun/lisjong-arena"
DEFAULT_LEDGER_PATH = Path(__file__).with_name("seed-ledger.json")

RESERVED = "RESERVED"
COMMITTED = "COMMITTED"
RETIRED = "RETIRED"
STATES = frozenset({RESERVED, COMMITTED, RETIRED})
ACTIVE_STATES = frozenset({RESERVED, COMMITTED})

LEGACY_SEED_DOMAIN = "arena-legacy-declared-v1"
RIICHIENV_HALF_HANCHAN_SEED_DOMAIN = "riichienv-4p-red-half-hanchan-v1"
RIICHIENV_SINGLE_ROUND_SEED_DOMAIN = "riichienv-4p-red-single-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class SeedRegistryError(ValueError):
    """The seed ledger or requested allocation is invalid."""


def canonical_json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _digest(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def new_ledger() -> dict[str, object]:
    return {
        "allocations": [],
        "owner_repository": OWNER_REPOSITORY,
        "schema_version": LEDGER_SCHEMA_VERSION,
    }


def _text(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value.strip():
        raise SeedRegistryError(f"{name} must be a non-empty string")
    return value


def _seed(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2**32:
        raise SeedRegistryError("seed must be an unsigned 32-bit exact integer")
    return value


def _seeds(values: object) -> tuple[int, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise SeedRegistryError("seed membership must be an iterable of integers")
    try:
        result = tuple(_seed(value) for value in values)  # type: ignore[arg-type]
    except TypeError:
        raise SeedRegistryError("seed membership must be iterable") from None
    if not result:
        raise SeedRegistryError("seed membership must not be empty")
    if len(set(result)) != len(result):
        raise SeedRegistryError("duplicate seed in allocation")
    return tuple(sorted(result))


def seed_membership_document(values: object) -> dict[str, object]:
    values = _seeds(values)
    if values == tuple(range(values[0], values[-1] + 1)):
        return {"first": values[0], "kind": "range", "last": values[-1]}
    return {"kind": "explicit", "seeds": list(values)}


def seeds_from_membership(value: object) -> tuple[int, ...]:
    if type(value) is not dict:
        raise SeedRegistryError("seed_membership must be an object")
    if value.get("kind") == "range":
        if set(value) != {"first", "kind", "last"}:
            raise SeedRegistryError("range membership has unexpected fields")
        first, last = _seed(value["first"]), _seed(value["last"])
        if last < first:
            raise SeedRegistryError("seed range is reversed")
        return tuple(range(first, last + 1))
    if value.get("kind") == "explicit":
        if set(value) != {"kind", "seeds"}:
            raise SeedRegistryError("explicit membership has unexpected fields")
        result = _seeds(value["seeds"])
        if list(result) != value["seeds"]:
            raise SeedRegistryError("explicit membership must be sorted")
        return result
    raise SeedRegistryError("seed_membership.kind must be range or explicit")


def seed_membership_identity(values: object) -> str:
    return _digest(seed_membership_document(values))


def _identity_material(record: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"allocation_identity", "allocation_timestamp", "state"}
    }


_RECORD_FIELDS = {
    "allocation_identity",
    "allocation_timestamp",
    "arena_revision",
    "owner_issue",
    "owner_repository",
    "population",
    "protocol",
    "protocol_revision",
    "provenance_reference",
    "purpose",
    "schema_version",
    "seed_domain",
    "seed_membership",
    "seed_membership_identity",
    "split",
    "state",
}


def build_allocation_record(
    *,
    owner_issue: str | None,
    protocol: str | None,
    seed_domain: str,
    purpose: str,
    population: str,
    split: str | None,
    seeds: object,
    state: str = RESERVED,
    arena_revision: str | None,
    protocol_revision: str | None,
    provenance_reference: str,
    allocation_timestamp: str | None,
) -> dict[str, object]:
    membership = seed_membership_document(seeds)
    record: dict[str, object] = {
        "allocation_identity": "",
        "allocation_timestamp": allocation_timestamp,
        "arena_revision": arena_revision,
        "owner_issue": owner_issue,
        "owner_repository": OWNER_REPOSITORY,
        "population": population,
        "protocol": protocol,
        "protocol_revision": protocol_revision,
        "provenance_reference": provenance_reference,
        "purpose": purpose,
        "schema_version": ALLOCATION_SCHEMA_VERSION,
        "seed_domain": seed_domain,
        "seed_membership": membership,
        "seed_membership_identity": _digest(membership),
        "split": split,
        "state": state,
    }
    record["allocation_identity"] = _digest(_identity_material(record))
    return validate_allocation_record(record)


def validate_allocation_record(record: object) -> dict[str, object]:
    if type(record) is not dict or set(record) != _RECORD_FIELDS:
        raise SeedRegistryError("allocation fields do not match v1 schema")
    if record["schema_version"] != ALLOCATION_SCHEMA_VERSION:
        raise SeedRegistryError("unsupported allocation schema")
    if record["owner_repository"] != OWNER_REPOSITORY:
        raise SeedRegistryError("allocation is not Arena-owned")
    owner_issue = _text(record["owner_issue"], "owner_issue", nullable=True)
    protocol = _text(record["protocol"], "protocol", nullable=True)
    if owner_issue is None and protocol is None:
        raise SeedRegistryError("owner_issue or protocol is required")
    domain = _text(record["seed_domain"], "seed_domain")
    if not _DOMAIN.fullmatch(domain):
        raise SeedRegistryError("seed_domain is not a stable lowercase identifier")
    for field in ("purpose", "population", "provenance_reference"):
        _text(record[field], field)
    _text(record["split"], "split", nullable=True)
    state = record["state"]
    if state not in STATES:
        raise SeedRegistryError("invalid allocation state")
    arena_revision = _text(record["arena_revision"], "arena_revision", nullable=True)
    protocol_revision = _text(
        record["protocol_revision"], "protocol_revision", nullable=True
    )
    if state in ACTIVE_STATES:
        if arena_revision is None or not _SHA1.fullmatch(arena_revision):
            raise SeedRegistryError("active allocation needs exact Arena revision")
        if protocol_revision is None:
            raise SeedRegistryError("active allocation needs protocol_revision")
        if record["allocation_timestamp"] is None:
            raise SeedRegistryError("active allocation needs allocation_timestamp")
    timestamp = record["allocation_timestamp"]
    if timestamp is not None:
        timestamp = _text(timestamp, "allocation_timestamp")
        if not timestamp.endswith("Z"):
            raise SeedRegistryError("allocation_timestamp must be UTC RFC3339")
        try:
            datetime.fromisoformat(timestamp[:-1] + "+00:00")
        except ValueError:
            raise SeedRegistryError("invalid allocation_timestamp") from None
    membership = record["seed_membership"]
    seeds_from_membership(membership)
    if record["seed_membership_identity"] != _digest(membership):
        raise SeedRegistryError("seed membership identity mismatch")
    identity = record["allocation_identity"]
    if type(identity) is not str or not _SHA256.fullmatch(identity):
        raise SeedRegistryError("allocation_identity must be SHA-256 hex")
    if identity != _digest(_identity_material(record)):
        raise SeedRegistryError("allocation_identity mismatch")
    return record


def _sort_key(record: dict[str, object]) -> tuple[object, ...]:
    seeds = seeds_from_membership(record["seed_membership"])
    return (
        record["seed_domain"],
        seeds[0],
        seeds[-1],
        record["owner_issue"] or "",
        record["population"],
        record["split"] or "",
        record["allocation_identity"],
    )


def validate_ledger(document: object) -> dict[str, object]:
    fields = {"allocations", "owner_repository", "schema_version"}
    if type(document) is not dict or set(document) != fields:
        raise SeedRegistryError("ledger fields do not match v1 schema")
    if document["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise SeedRegistryError("unsupported ledger schema")
    if document["owner_repository"] != OWNER_REPOSITORY:
        raise SeedRegistryError("ledger is not Arena-owned")
    allocations = document["allocations"]
    if type(allocations) is not list:
        raise SeedRegistryError("allocations must be a list")
    for record in allocations:
        validate_allocation_record(record)
    if allocations != sorted(allocations, key=_sort_key):
        raise SeedRegistryError("allocations are not in canonical order")
    identities = [record["allocation_identity"] for record in allocations]
    if len(set(identities)) != len(identities):
        raise SeedRegistryError("duplicate allocation_identity")
    occupied: dict[str, set[int]] = {}
    for record in allocations:
        domain = record["seed_domain"]
        seeds = set(seeds_from_membership(record["seed_membership"]))
        overlap = occupied.setdefault(domain, set()).intersection(seeds)
        if overlap:
            raise SeedRegistryError(
                f"same-domain seed collision: {domain} {sorted(overlap)!r}"
            )
        occupied[domain].update(seeds)
    return document


def ledger_revision(document: object) -> str:
    text = canonical_json_text(validate_ledger(document))
    return hashlib.sha256(text.encode()).hexdigest()


def load_ledger(
    path: str | Path = DEFAULT_LEDGER_PATH, *, strict_serialization: bool = True
) -> dict[str, object]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SeedRegistryError(f"cannot read ledger {path}: {error}") from error
    validate_ledger(document)
    if strict_serialization and text != canonical_json_text(document):
        raise SeedRegistryError("ledger serialization is not canonical")
    return document


def write_ledger(path: str | Path, document: object) -> None:
    path = Path(path)
    text = canonical_json_text(validate_ledger(document))
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        load_ledger(temporary_path)
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    load_ledger(path)


def allocated_seeds(
    document: object | None = None,
    *,
    seed_domain: str | None = None,
    exclude_owner_issues: object = (),
) -> frozenset[int]:
    ledger = load_ledger() if document is None else validate_ledger(document)
    excluded = frozenset(exclude_owner_issues)  # type: ignore[arg-type]
    result: set[int] = set()
    for record in ledger["allocations"]:
        if record["owner_issue"] in excluded:
            continue
        if seed_domain is None or record["seed_domain"] == seed_domain:
            result.update(seeds_from_membership(record["seed_membership"]))
    return frozenset(result)


def collision_records(
    document: object, *, seed_domain: str, seeds: object
) -> tuple[dict[str, object], ...]:
    ledger = validate_ledger(document)
    if type(seed_domain) is not str or not _DOMAIN.fullmatch(seed_domain):
        raise SeedRegistryError("invalid seed_domain")
    requested = set(_seeds(seeds))
    result = []
    for record in ledger["allocations"]:
        record_domain = record["seed_domain"]
        same_domain = record_domain == seed_domain
        legacy_quarantine = (
            seed_domain != LEGACY_SEED_DOMAIN and record_domain == LEGACY_SEED_DOMAIN
        )
        if not (same_domain or legacy_quarantine):
            continue
        overlap = requested.intersection(
            seeds_from_membership(record["seed_membership"])
        )
        if overlap:
            result.append(
                {
                    "allocation_identity": record["allocation_identity"],
                    "owner_issue": record["owner_issue"],
                    "population": record["population"],
                    "seeds": sorted(overlap),
                    "state": record["state"],
                }
            )
    return tuple(result)


def find_allocation(document: object, allocation_identity: str) -> dict[str, object]:
    ledger = validate_ledger(document)
    matches = [
        record
        for record in ledger["allocations"]
        if record["allocation_identity"] == allocation_identity
    ]
    if len(matches) != 1:
        raise SeedRegistryError("allocation_identity is not uniquely present")
    return matches[0]


def allocation_binding(document: object, allocation_identity: str) -> dict[str, object]:
    ledger = validate_ledger(document)
    record = find_allocation(ledger, allocation_identity)
    return {
        "allocation_identity": allocation_identity,
        "ledger_revision": ledger_revision(ledger),
        "owner_repository": OWNER_REPOSITORY,
        "seed_domain": record["seed_domain"],
        "seed_membership_identity": record["seed_membership_identity"],
    }


_BINDING_FIELDS = {
    "allocation_identity",
    "ledger_revision",
    "owner_repository",
    "seed_domain",
    "seed_membership_identity",
}


def validate_binding_shape(binding: object, *, seeds: object) -> dict[str, object]:
    if type(binding) is not dict or set(binding) != _BINDING_FIELDS:
        raise SeedRegistryError("allocation binding fields do not match v1 contract")
    for field in (
        "allocation_identity",
        "ledger_revision",
        "seed_membership_identity",
    ):
        value = binding[field]
        if type(value) is not str or not _SHA256.fullmatch(value):
            raise SeedRegistryError(f"allocation binding {field} must be SHA-256 hex")
    if binding["owner_repository"] != OWNER_REPOSITORY:
        raise SeedRegistryError("allocation binding is not Arena-owned")
    domain = binding["seed_domain"]
    if type(domain) is not str or not _DOMAIN.fullmatch(domain):
        raise SeedRegistryError("allocation binding has invalid seed_domain")
    if binding["seed_membership_identity"] != seed_membership_identity(seeds):
        raise SeedRegistryError("allocation binding membership mismatch")
    return binding


def require_allocation_binding(
    document: object,
    binding: object,
    *,
    seeds: object,
    owner_issue: str | None = None,
    protocol: str | None = None,
    seed_domain: str | None = None,
    population: str | None = None,
    split: str | None = None,
) -> dict[str, object]:
    ledger = validate_ledger(document)
    binding = validate_binding_shape(binding, seeds=seeds)
    if binding["ledger_revision"] != ledger_revision(ledger):
        raise SeedRegistryError("allocation binding ledger_revision is stale")
    record = find_allocation(ledger, binding["allocation_identity"])
    if allocation_binding(ledger, record["allocation_identity"]) != binding:
        raise SeedRegistryError("allocation binding differs from canonical ledger")
    if record["state"] not in ACTIVE_STATES:
        raise SeedRegistryError("allocation is not active")
    expected = {
        "owner_issue": owner_issue,
        "protocol": protocol,
        "seed_domain": seed_domain,
        "population": population,
        "split": split,
    }
    for field, value in expected.items():
        if value is not None and record[field] != value:
            raise SeedRegistryError(
                f"allocation {field} differs from required ownership"
            )
    if seeds_from_membership(record["seed_membership"]) != _seeds(seeds):
        raise SeedRegistryError("allocation membership differs from request")
    return record


def reserve_allocation(
    document: object,
    *,
    owner_issue: str | None,
    protocol: str | None,
    seed_domain: str,
    purpose: str,
    population: str,
    split: str | None,
    seeds: object,
    arena_revision: str,
    protocol_revision: str,
    provenance_reference: str,
    allocation_timestamp: str,
) -> tuple[dict[str, object], dict[str, object]]:
    ledger = deepcopy(validate_ledger(document))
    if seed_domain == LEGACY_SEED_DOMAIN:
        raise SeedRegistryError("legacy seed domain is bootstrap-only")
    collisions = collision_records(ledger, seed_domain=seed_domain, seeds=seeds)
    if collisions:
        raise SeedRegistryError(f"seed collision: {collisions!r}")
    record = build_allocation_record(
        owner_issue=owner_issue,
        protocol=protocol,
        seed_domain=seed_domain,
        purpose=purpose,
        population=population,
        split=split,
        seeds=seeds,
        arena_revision=arena_revision,
        protocol_revision=protocol_revision,
        provenance_reference=provenance_reference,
        allocation_timestamp=allocation_timestamp,
    )
    ledger["allocations"].append(record)
    ledger["allocations"].sort(key=_sort_key)
    validate_ledger(ledger)
    return ledger, record


def transition_allocation(
    document: object, allocation_identity: str, *, state: str
) -> dict[str, object]:
    if state not in {COMMITTED, RETIRED}:
        raise SeedRegistryError("transition target must be COMMITTED or RETIRED")
    ledger = deepcopy(validate_ledger(document))
    record = find_allocation(ledger, allocation_identity)
    allowed = {
        RESERVED: {COMMITTED, RETIRED},
        COMMITTED: {COMMITTED, RETIRED},
        RETIRED: {RETIRED},
    }
    if state not in allowed[record["state"]]:
        raise SeedRegistryError("allocation state cannot move backward or become FREE")
    record["state"] = state
    validate_ledger(ledger)
    return ledger


def validate_branch_against_base(branch: object, base: object) -> None:
    branch, base = validate_ledger(branch), validate_ledger(base)
    branch_by_id = {
        record["allocation_identity"]: record for record in branch["allocations"]
    }
    base_by_id = {
        record["allocation_identity"]: record for record in base["allocations"]
    }
    if set(base_by_id) - set(branch_by_id):
        raise SeedRegistryError("branch is stale or deleted main allocations")
    rank = {RESERVED: 0, COMMITTED: 1, RETIRED: 2}
    for identity, old in base_by_id.items():
        new = branch_by_id[identity]
        old_immutable = {k: v for k, v in old.items() if k != "state"}
        new_immutable = {k: v for k, v in new.items() if k != "state"}
        if old_immutable != new_immutable:
            raise SeedRegistryError("existing allocation immutable fields changed")
        if rank[new["state"]] < rank[old["state"]]:
            raise SeedRegistryError(
                "allocation state regressed relative to current main"
            )
    for record in branch["allocations"]:
        if record["allocation_identity"] in base_by_id:
            continue
        overlap = collision_records(
            base,
            seed_domain=record["seed_domain"],
            seeds=seeds_from_membership(record["seed_membership"]),
        )
        if overlap:
            raise SeedRegistryError("new branch allocation collides with current main")


def parse_seed_spec(value: str) -> tuple[int, ...]:
    value = value.strip()
    if ".." in value:
        parts = value.split("..")
        if len(parts) != 2:
            raise SeedRegistryError("range must be FIRST..LAST")
        try:
            first, last = map(int, parts)
        except ValueError:
            raise SeedRegistryError("range contains non-integer") from None
        first, last = _seed(first), _seed(last)
        if last < first:
            raise SeedRegistryError("range is reversed")
        return tuple(range(first, last + 1))
    try:
        return _seeds(int(part.strip()) for part in value.split(","))
    except ValueError:
        raise SeedRegistryError("seed list contains non-integer") from None


def _write(path: Path, document: dict[str, object], **extra: object) -> None:
    write_ledger(path, document)
    print(
        json.dumps(
            {"ledger_revision": ledger_revision(document), **extra},
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH))
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list")
    listing.add_argument("--seed-domain")
    show = commands.add_parser("show")
    show.add_argument("allocation_identity")
    check = commands.add_parser("check")
    check.add_argument("--seed-domain", required=True)
    check.add_argument("--seeds", required=True)
    reserve = commands.add_parser("reserve")
    for flag in ("owner-issue", "protocol", "split"):
        reserve.add_argument(f"--{flag}")
    for flag in (
        "seed-domain",
        "purpose",
        "population",
        "seeds",
        "arena-revision",
        "protocol-revision",
        "provenance-reference",
    ):
        reserve.add_argument(f"--{flag}", required=True)
    reserve.add_argument("--allocation-timestamp")
    commit = commands.add_parser("commit")
    commit.add_argument("allocation_identity")
    commit.add_argument("--state", choices=(COMMITTED, RETIRED), default=COMMITTED)
    commands.add_parser("validate-ledger")
    branch = commands.add_parser("validate-branch")
    branch.add_argument("--base-ledger")

    args = parser.parse_args(argv)
    path = Path(args.ledger)
    try:
        ledger = load_ledger(path)
        if args.command == "list":
            values = [
                record
                for record in ledger["allocations"]
                if args.seed_domain is None or record["seed_domain"] == args.seed_domain
            ]
            print(
                canonical_json_text(
                    {
                        "allocations": values,
                        "ledger_revision": ledger_revision(ledger),
                    }
                ),
                end="",
            )
        elif args.command == "show":
            record = find_allocation(ledger, args.allocation_identity)
            print(
                canonical_json_text(
                    {
                        "allocation": record,
                        "binding": allocation_binding(ledger, args.allocation_identity),
                    }
                ),
                end="",
            )
        elif args.command == "check":
            seeds = parse_seed_spec(args.seeds)
            collisions = collision_records(
                ledger, seed_domain=args.seed_domain, seeds=seeds
            )
            print(
                json.dumps(
                    {
                        "collisions": collisions,
                        "fresh": not collisions,
                        "ledger_revision": ledger_revision(ledger),
                        "seed_domain": args.seed_domain,
                        "seed_membership": seed_membership_document(seeds),
                    },
                    sort_keys=True,
                )
            )
            return 0 if not collisions else 2
        elif args.command == "reserve":
            timestamp = args.allocation_timestamp or datetime.now(
                UTC
            ).isoformat().replace("+00:00", "Z")
            updated, record = reserve_allocation(
                ledger,
                owner_issue=args.owner_issue,
                protocol=args.protocol,
                seed_domain=args.seed_domain,
                purpose=args.purpose,
                population=args.population,
                split=args.split,
                seeds=parse_seed_spec(args.seeds),
                arena_revision=args.arena_revision,
                protocol_revision=args.protocol_revision,
                provenance_reference=args.provenance_reference,
                allocation_timestamp=timestamp,
            )
            _write(
                path,
                updated,
                allocation_identity=record["allocation_identity"],
            )
        elif args.command == "commit":
            updated = transition_allocation(
                ledger, args.allocation_identity, state=args.state
            )
            _write(
                path,
                updated,
                allocation_identity=args.allocation_identity,
                state=args.state,
            )
        elif args.command == "validate-ledger":
            print(
                json.dumps(
                    {
                        "allocation_count": len(ledger["allocations"]),
                        "ledger_revision": ledger_revision(ledger),
                        "status": "PASS",
                    },
                    sort_keys=True,
                )
            )
        else:
            base = load_ledger(args.base_ledger) if args.base_ledger else new_ledger()
            validate_branch_against_base(ledger, base)
            print(json.dumps({"status": "PASS"}, sort_keys=True))
        return 0
    except Exception as error:
        print(
            f"STOP / INVALID: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
