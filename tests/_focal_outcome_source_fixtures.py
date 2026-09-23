"""Shared test-only provenance fixtures for the #359 focal outcome source tests."""

from lisjong_arena import seed_registry
from lisjong_arena.focal_outcome_source import source


def binding(seeds):
    return {
        "allocation_identity": "1" * 64,
        "ledger_revision": "2" * 64,
        "owner_repository": "lisbun/lisjong-arena",
        "seed_domain": seed_registry.RIICHIENV_HALF_HANCHAN_SEED_DOMAIN,
        "seed_membership_identity": seed_registry.seed_membership_identity(list(seeds)),
    }


def source_contract():
    return {
        "arena_revision": "0" * 40,
        "backend": {"name": "riichienv", "version": "0.4.10"},
        "dependencies": {"lisjong": source.PINNED_LISJONG_REVISION},
        "game_mode": source.GAME_MODE,
        "python": "3.14.0",
    }
