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
        ("human", "Order Of Vladd", (16.9, 18), 0.0, 0),             # touching, faces +x (target)
        ("human", "Order Of Vladd", (18, 16.9), math.pi / 2, 0),     # touching, faces +y (target)
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
        ("human", "Order Of Vladd", (16.9, 18), 0.0, 0),   # in contact
        ("human", "Order Of Vladd", (10, 18), 0.0, 0),     # NOT in contact
    ], active="human")
    r = e.apply(CloseIntent(1, 0, formation_uids=(1, 2)))
    assert not r.ok and r.reason == "not_adjacent"


def test_mage_spawn_cannot_form_formations(db):
    # Mage Spawn are faction-less monsters — no formations (no Shyft in the roster).
    e = build_engine(db, [
        ("human", "Werebear", (10, 10), math.pi / 2, 0),
        ("human", "Werebear", (11.1, 10), math.pi / 2, 0),
        ("human", "Werebear", (12.2, 10), math.pi / 2, 0),
        ("llm", "Chaos Mage", (11, 30), -math.pi / 2, 0),
    ], active="human")
    assert all(e.state.figure(u).definition.faction == "Mage Spawn" for u in (0, 1, 2))
    # The AI offers no formation for a Mage Spawn cluster...
    assert generate_formation_candidates(e, "human") == []
    # ...and a hand-crafted Mage Spawn formation is rejected by the engine.
    r = e.apply(MoveIntent(0, (10, 12), math.pi / 2, formation_uids=(0, 1, 2),
                           member_dests=((10, 12), (11.1, 12), (12.2, 12)),
                           member_facings=(math.pi / 2,) * 3))
    assert not r.ok and r.reason == "bad_formation"


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


# --- combat-formation assist bonuses (the "units assist each other" mechanic) --
def test_ranged_formation_assist_scales_by_two_per_member(db):
    # Each extra member adds +2 to the shared attack roll (P4-R29).
    def formation_attack(n):
        specs = [("human", "Chaos Mage", (10 + 1.1 * i, 5), math.pi / 2, 0) for i in range(n)]
        specs.append(("llm", "Storm Golem", (10 + 1.1 * (n - 1) / 2, 13), -math.pi / 2, 0))
        e = build_engine(db, specs)
        c = _find(generate_formation_candidates(e, "human"), "ranged_formation")
        return c.annotation["attack"], e.state.figure(c.annotation["primary"]).attack
    a3, base = formation_attack(3)
    a4, _ = formation_attack(4)
    assert a3 == base + 2 * 2   # 3 figures => +4
    assert a4 == base + 2 * 3   # 4 figures => +6


def test_close_formation_rear_bonus(db):
    # +1 per extra member, plus +1 because one attacker sits in the target's rear.
    e = build_engine(db, [
        ("llm", "Werebear", (18, 18), 0.0, 0),                 # 90-arc target, faces +x
        ("human", "Order Of Vladd", (16.9, 18), 0.0, 0),       # behind => rear arc
        ("human", "Order Of Vladd", (18, 16.9), math.pi / 2, 0),  # side => front arc
    ], active="human")
    c = _find(generate_formation_candidates(e, "human"), "close_formation")
    primary = e.state.figure(c.annotation["primary"])
    assert c.annotation["rear"] is True
    assert c.annotation["attack"] == primary.attack + 1 + 1  # +1 extra member, +1 rear


def test_wide_arc_figure_still_has_a_rear(db):
    # arc_deg is the TOTAL arc (OQ-5 resolved): a 180-arc figure (Storm Golem)
    # has a half-circle front (facing +/- 90) — an attacker directly behind IS
    # in its rear arc, while one dead ahead is not.
    assert db.find("Storm Golem")[0].arc_deg == 180.0
    e = build_engine(db, [
        ("llm", "Storm Golem", (18, 18), 0.0, 0),               # facing +x
        ("human", "Order Of Vladd", (16.9, 18), 0.0, 0),        # directly BEHIND (-x)
        ("human", "Order Of Vladd", (18, 16.9), math.pi / 2, 0),  # at its side (front edge)
    ], active="human")
    c = _find(generate_formation_candidates(e, "human"), "close_formation")
    primary = e.state.figure(c.annotation["primary"])
    assert c.annotation["rear"] is True  # the behind attacker grants the +1
    assert c.annotation["attack"] == primary.attack + 1 + 1  # extra member + rear
    # And a standard 90-arc figure: a target 90 degrees off its facing is OUTSIDE
    # its quarter-circle front arc now.
    e2 = build_engine(db, [
        ("human", "Werebear", (10, 10), 0.0, 0),   # facing +x, arc +/- 45
        ("llm", "Werebear", (10, 11.1), 0.0, 0),   # touching, straight UP (90 deg off)
    ], active="human")
    assert e2.legal_close_targets(e2.state.figure(0)) == []


# --- assist-attack options (the human's group-select combat formations) -----
def test_formation_attack_options_volley_matches_applier(db):
    """A legal option is exactly an intent the engine accepts; an illegal one
    carries the applier's own reason (per target, per kind)."""
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
            ("human", "Chaos Mage", (11.1, 10), math.pi / 2, 0),
            ("human", "Chaos Mage", (12.2, 10), math.pi / 2, 0),
            ("llm", "Werebear", (11.1, 16), -math.pi / 2, 0),   # in range/arc of all
            ("llm", "Werebear", (11.1, 33), -math.pi / 2, 0),   # far beyond range 12
        ],
    )
    uids = [0, 1, 2]
    opts = e.formation_attack_options(uids)
    volley = next(o for o in opts if o["kind"] == "ranged_formation" and o["target"] == 3)
    assert volley["ok"] and volley["attack"] == e.state.figure(volley["primary"]).attack + 4
    far = next(o for o in opts if o["kind"] == "ranged_formation" and o["target"] == 4)
    assert not far["ok"] and far["reason"]
    # Close formation of 3 vs an untouched target: illegal with the contact reason.
    gang = next(o for o in opts if o["kind"] == "close_formation" and o["target"] == 3)
    assert not gang["ok"] and "base contact" in gang["reason"]
    # The legal volley round-trips through the applier unchanged.
    r = e.apply(RangedIntent(volley["primary"], (volley["target"],),
                             formation_uids=tuple(volley["members"])))
    assert r.ok


def test_formation_attack_options_close_gang_with_rear(db):
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (15, 13.9), math.pi / 2, 0),    # in front of target
            ("human", "Chaos Mage", (15, 16.1), -math.pi / 2, 0),   # behind it (rear)
            ("llm", "Werebear", (15, 15), math.pi / 2, 0),          # faces +y
        ],
    )
    opts = e.formation_attack_options([0, 1])
    gang = next(o for o in opts if o["kind"] == "close_formation")
    assert gang["ok"] and gang["rear"]
    primary = e.state.figure(gang["primary"])
    assert gang["attack"] == primary.attack + 1 + 1  # +1 assist, +1 rear
    r = e.apply(CloseIntent(gang["primary"], gang["target"],
                            formation_uids=tuple(gang["members"])))
    assert r.ok
    # 2 members can't volley: no ranged_formation entries for a pair.
    assert not [o for o in opts if o["kind"] == "ranged_formation"]


def test_ranged_formation_candidates_cover_every_visible_target(db):
    """The AI's candidate list has one volley per enemy the whole cluster sees
    (first-visible-only starved it of the better target)."""
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
            ("human", "Chaos Mage", (11.1, 10), math.pi / 2, 0),
            ("human", "Chaos Mage", (12.2, 10), math.pi / 2, 0),
            ("llm", "Werebear", (9, 16), -math.pi / 2, 0),
            ("llm", "Werebear", (14, 16), -math.pi / 2, 0),
        ],
    )
    cands = [c for c in generate_formation_candidates(e, "human") if c.kind == "ranged_formation"]
    assert {c.annotation["target"] for c in cands} == {3, 4}


def test_formation_attack_options_gated_on_phase_and_ended(db):
    """The options mirror apply()'s ended gate and only exist in battle —
    an ok:true option must ALWAYS be an intent the applier accepts."""
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
            ("human", "Chaos Mage", (11.1, 10), math.pi / 2, 0),
            ("human", "Chaos Mage", (12.2, 10), math.pi / 2, 0),
            ("llm", "Werebear", (11.1, 16), -math.pi / 2, 0),
        ],
    )
    assert any(o["ok"] for o in e.formation_attack_options([0, 1, 2]))
    e.state.phase = "deploy"
    assert e.formation_attack_options([0, 1, 2]) == []
    e.state.phase = "battle"
    e.state.ended = True
    assert e.formation_attack_options([0, 1, 2]) == []
