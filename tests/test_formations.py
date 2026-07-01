"""Formation tests (P4-R11..R16, R29) — engine rules and AI generation."""

import math

import pytest

from clixengine.candidates import generate_formation_candidates
from clixengine.geometry import Vec, distance, in_base_contact
from clixengine.intents import CloseIntent, MoveIntent, RangedIntent

from .conftest import build_engine


def _find(cands, kind):
    return next((c for c in cands if c.kind == kind), None)


# --- movement formation ----------------------------------------------------
def _three_mages_line(db, seed=0, faction_ok=True):
    third = "Chaos Mage" if faction_ok else "Werebear"  # Werebear is a different faction
    return build_engine(
        db,
        [
            ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
            ("human", "Chaos Mage", (11.1, 10), math.pi / 2, 0),  # touching #0
            ("human", third, (12.2, 10), math.pi / 2, 0),         # touching #1
            ("llm", "Werebear", (11, 30), -math.pi / 2, 0),       # far enemy
        ],
        seed=seed,
    )


def test_movement_formation_generated_and_moves_cohesively(db):
    e = _three_mages_line(db)
    cand = _find(generate_formation_candidates(e, "human"), "formation_move")
    assert cand is not None
    assert cand.annotation["size"] == 3
    starts = {u: Vec(*e.state.figure(u).position.as_tuple()) for u in cand.annotation["members"]}
    r = e.apply(cand.intent)
    assert r.ok
    # Every member moved the same offset (rigid translation) and the group is one action.
    assert e._actions_spent == 1
    for u in cand.annotation["members"]:
        f = e.state.figure(u)
        assert f.uid in e._acted_uids
        assert f.position.y > starts[u].y  # advanced toward the enemy
    # Still cohesive: each member touches another.
    figs = [e.state.figure(u) for u in cand.annotation["members"]]
    assert e._positions_cohesive([f.position for f in figs], [f.base_radius for f in figs])


def test_movement_formation_requires_single_faction(db):
    e = _three_mages_line(db, faction_ok=False)
    # The mixed-faction trio yields no movement formation candidate.
    assert _find(generate_formation_candidates(e, "human"), "formation_move") is None
    # And a hand-crafted mixed-faction formation intent is rejected.
    r = e.apply(MoveIntent(0, (10, 12), math.pi / 2, formation_uids=(0, 1, 2),
                           member_dests=((10, 12), (11.1, 12), (12.2, 12)),
                           member_facings=(math.pi / 2,) * 3))
    assert not r.ok and r.reason == "bad_formation"


def test_movement_formation_rejects_noncohesive(db):
    e = build_engine(db, [
        ("human", "Chaos Mage", (5, 10), math.pi / 2, 0),
        ("human", "Chaos Mage", (15, 10), math.pi / 2, 0),   # far apart
        ("human", "Chaos Mage", (25, 10), math.pi / 2, 0),
        ("llm", "Werebear", (15, 30), -math.pi / 2, 0),
    ])
    r = e.apply(MoveIntent(0, (5, 12), math.pi / 2, formation_uids=(0, 1, 2),
                           member_dests=((5, 12), (15, 12), (25, 12)),
                           member_facings=(math.pi / 2,) * 3))
    assert not r.ok and r.reason == "bad_formation"


def test_movement_formation_rejects_wrong_size(db):
    e = _three_mages_line(db)
    r = e.apply(MoveIntent(0, (10, 12), math.pi / 2, formation_uids=(0, 1),
                           member_dests=((10, 12), (11.1, 12)),
                           member_facings=(math.pi / 2,) * 2))
    assert not r.ok and r.reason == "bad_formation"


# --- ranged formation ------------------------------------------------------
def test_ranged_formation_boosts_attack_and_resolves(db):
    e = build_engine(db, [
        ("human", "Chaos Mage", (10, 5), math.pi / 2, 0),
        ("human", "Chaos Mage", (11.1, 5), math.pi / 2, 0),
        ("human", "Chaos Mage", (12.2, 5), math.pi / 2, 0),
        ("llm", "Werebear", (11, 13), -math.pi / 2, 0),  # within range, clear LoF
    ])
    cand = _find(generate_formation_candidates(e, "human"), "ranged_formation")
    assert cand is not None
    primary = e.state.figure(cand.annotation["primary"])
    assert cand.annotation["attack"] == primary.attack + 2 * 2  # +2 per extra member
    r = e.apply(cand.intent)
    assert r.ok
    assert any(x["type"] == "ranged_formation" for x in r.events)
    for u in cand.annotation["members"]:
        assert e.state.figure(u).uid in e._acted_uids


# --- close formation -------------------------------------------------------
def test_close_formation_generated_and_resolves(db):
    e = build_engine(db, [
        ("llm", "Werebear", (18, 18), 0.0, 0),                       # target, facing +x
        ("human", "Werebear", (16.9, 18), 0.0, 0),                   # touching, faces +x (target)
        ("human", "Werebear", (18, 16.9), math.pi / 2, 0),           # touching, faces +y (target)
    ], active="human")
    cand = _find(generate_formation_candidates(e, "human"), "close_formation")
    assert cand is not None
    assert len(cand.annotation["members"]) == 2
    primary = e.state.figure(cand.annotation["primary"])
    # +1 per extra member (2 members => +1); a rear contributor adds another +1.
    assert cand.annotation["attack"] >= primary.attack + 1
    r = e.apply(cand.intent)
    assert r.ok
    assert any(x["type"] == "close_formation" for x in r.events)
    for u in cand.annotation["members"]:
        assert e.state.figure(u).uid in e._acted_uids


def test_close_formation_rejects_member_out_of_contact(db):
    e = build_engine(db, [
        ("llm", "Werebear", (18, 18), 0.0, 0),
        ("human", "Werebear", (16.9, 18), 0.0, 0),   # in contact
        ("human", "Werebear", (10, 18), 0.0, 0),     # NOT in contact
    ], active="human")
    r = e.apply(CloseIntent(1, 0, formation_uids=(1, 2)))
    assert not r.ok and r.reason == "not_adjacent"


# --- regression: formation-move validation (found in the audit/fuzz sweep) ----
def test_formation_rejects_duplicate_members(db):
    e = _three_mages_line(db)
    r = e.apply(MoveIntent(0, (10, 12), math.pi / 2, formation_uids=(0, 0, 1),
                           member_dests=((10, 12), (10, 12), (11.1, 12)),
                           member_facings=(math.pi / 2,) * 3))
    assert not r.ok and r.reason == "bad_formation"


def test_formation_move_rejects_path_crossing_enemy_base(db):
    e = build_engine(db, [
        ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
        ("human", "Chaos Mage", (11.1, 10), math.pi / 2, 0),
        ("human", "Chaos Mage", (12.2, 10), math.pi / 2, 0),
        ("llm", "Werebear", (11.1, 12.5), -math.pi / 2, 0),  # sits on member #1's path
    ], active="human")
    r = e.apply(MoveIntent(0, (10, 14), math.pi / 2, formation_uids=(0, 1, 2),
                           member_dests=((10, 14), (11.1, 14), (12.2, 14)),
                           member_facings=(math.pi / 2,) * 3))
    assert not r.ok and r.reason == "path_blocked"


def test_formation_move_rejects_dest_overlapping_enemy(db):
    e = build_engine(db, [
        ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
        ("human", "Chaos Mage", (11.1, 10), math.pi / 2, 0),
        ("human", "Chaos Mage", (12.2, 10), math.pi / 2, 0),
        ("llm", "Werebear", (11.9, 14), -math.pi / 2, 0),  # overlaps a dest, off the path
    ], active="human")
    r = e.apply(MoveIntent(0, (10, 14), math.pi / 2, formation_uids=(0, 1, 2),
                           member_dests=((10, 14), (11.1, 14), (12.2, 14)),
                           member_facings=(math.pi / 2,) * 3))
    assert not r.ok and r.reason == "end_on_base"
