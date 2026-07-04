"""Regressions for the final adversarial review findings (docs/lancers-plan.md P7)."""

import math
import pickle

from clixengine import abilities as ab
from clixengine.geometry import Vec, circles_overlap
from clixengine.intents import CloseIntent, MoveIntent, PassIntent, Rejection

from .conftest import build_engine

MOUNTED = "Light Lancer On Light Warhorse"   # mounted, Charge @ 0-1
SCORPION = "High Battle Mage On Scorpion Mount"


def _steer(engine, wanted: int, tag: str = "break_away") -> None:
    import copy
    for _ in range(500):
        if copy.deepcopy(engine.rng).d6(tag) == wanted:
            return
        engine.rng.d6("_burn")
    raise AssertionError(f"could not steer RNG to {wanted}")


def test_mounted_zero_distance_reface_rolls_break_away(db):
    """A pure facing change repositions a capsule — it must roll break-away,
    deal Shake Off on success, and stay put (no rotate) on failure."""
    e = build_engine(db, [
        ("human", SCORPION, (18, 18), 0.0, 0),
        ("llm", "Utem Guardsman", (15.8, 18), 0.0, 0),   # rear-circle contact, dead behind
    ])
    m = e.state.figure(0)
    _steer(e, 1)  # fail the roll
    r = e.apply(MoveIntent(0, (18, 18), facing=math.pi))
    assert r.ok
    ba = next(ev for ev in r.events if ev["type"] == "break_away")
    assert not ba["success"]
    assert m.facing == 0.0  # P5-R4: no rotate on failure

    _steer(e, 6)  # succeed
    e._acted_uids.clear()
    r2 = e.apply(MoveIntent(0, (18, 18), facing=math.pi))
    assert r2.ok
    assert any(ev["type"] == "shake_off" for ev in r2.events)
    assert m.facing == math.pi


def test_foot_zero_distance_reface_needs_no_roll(db):
    # Single-base rotation is footprint-neutral: unchanged behavior.
    e = build_engine(db, [
        ("human", "Utem Guardsman", (10, 10), 0.0, 0),
        ("llm", "Utem Guardsman", (11.1, 10), math.pi, 0),
    ])
    r = e.apply(MoveIntent(0, (10, 10), facing=math.pi / 2))
    assert r.ok and not any(ev["type"] == "break_away" for ev in r.events)


def test_pushing_deferred_until_rider_resolves(db):
    """P4-R4 lands the pushing click after the WHOLE action — a pushed charger
    strikes on its pre-push dial, and the click follows the strike."""
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 14), -math.pi / 2, 0),
    ])
    m = e.state.figure(0)
    m.action_tokens = 1  # this move pushes
    r = e.apply(MoveIntent(0, (10, 12.9), facing=math.pi / 2))
    assert r.ok and any(ev["type"] == "rider_armed" for ev in r.events)
    assert not any(ev["type"] == "push_damage" for ev in r.events)
    assert m.current_click == 0  # strike resolves on the healthy click
    r2 = e.apply(CloseIntent(0, 1, rider=True))
    assert r2.ok
    assert any(ev["type"] == "push_damage" for ev in r2.events)
    assert m.current_click == 1  # the deferred click landed after the strike


def test_deferred_push_lands_on_rider_expiry(db):
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), math.pi / 2, 0),
        ("human", "Utem Guardsman", (20, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 14), -math.pi / 2, 0),
    ])
    m = e.state.figure(0)
    m.action_tokens = 1
    assert e.apply(MoveIntent(0, (10, 12.9), facing=math.pi / 2)).ok
    assert m.current_click == 0
    # Skipping the rider via another action expires it — the push lands then.
    r = e.apply(PassIntent(1))
    assert r.ok
    assert m.current_click == 1
    assert any(ev["type"] == "push_damage" for ev in r.events)


def test_rejected_intent_does_not_burn_rider(db):
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 14), -math.pi / 2, 0),
        ("llm", "Utem Guardsman", (25, 25), -math.pi / 2, 0),
    ])
    assert e.apply(MoveIntent(0, (10, 12.9), facing=math.pi / 2)).ok
    assert e._pending_rider is not None
    # A rejected unrelated intent must not consume the free strike...
    assert not e.apply(MoveIntent(99, (5, 5), facing=0.0)).ok
    assert e._pending_rider is not None
    # ...nor a rider aimed at an illegal target.
    bad = e.apply(CloseIntent(0, 2, rider=True))
    assert not bad.ok and bad.reason == "not_adjacent"
    assert e._pending_rider is not None
    # The legal strike still fires.
    assert e.apply(CloseIntent(0, 1, rider=True)).ok


def test_arming_move_does_not_expire_its_own_rider(db):
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 14), -math.pi / 2, 0),
    ])
    r = e.apply(MoveIntent(0, (10, 12.9), facing=math.pi / 2))
    assert r.ok and e._pending_rider is not None


def test_ranged_arc_gate_uses_front_dot(db):
    """P5-R2: the LoF is drawn front dot to front dot — a mounted target whose
    front dot is in the firer's arc is shootable even when its rear circle
    (the nearest) sits outside the arc."""
    e = build_engine(db, [
        ("human", "Black Powder Boomer", (10, 10), 0.0, 0),  # range 10, arc 90
        ("llm", MOUNTED, (14, 12.9), math.pi / 2, 0),        # front dot bearing ~36 deg
    ])
    # rear circle at (14, 11.8): bearing ~24.2 deg — both in arc here; push the
    # rear OUT of arc instead: facing -x puts rear at (15.1, 12.9) bearing ~29.6.
    # Construct the review's exact shape: front dot inside, rear outside.
    t = e.state.figure(1)
    t.position, t.facing = Vec(13, 12.8), -math.pi / 2   # rear at (13, 13.9)
    # front dot bearing = atan2(2.8, 3) = 43.0 deg (inside 45); rear = 52.4 (outside)
    clear, reason = e.line_of_fire(0, 1)
    assert clear, reason


def test_levitate_cannot_drop_into_blocking_terrain(db):
    from clixengine.terrain import TerrainPiece
    # A caster with Magic Levitation at click 0 (search the roster).
    caster_name = next(
        f.short_name for f in db.all_figures()
        if ab.MAGIC_LEVITATION in f.dial[0].ability_ids()
    )
    e = build_engine(db, [
        ("human", caster_name, (10, 10), 0.0, 0),
        ("human", "Utem Guardsman", (11.1, 10), 0.0, 0),
        ("llm", "Werebear", (30, 30), 0.0, 0),
    ])
    e.state.terrain.append(
        TerrainPiece(0, "blocking", (Vec(16, 16), Vec(20, 16), Vec(20, 20), Vec(16, 20)))
    )
    from clixengine.intents import LevitateIntent
    r = e.apply(LevitateIntent(0, 1, (17, 17), facing=0.0))  # 8.4" away, in the block
    assert not r.ok and r.reason == "in_blocking"


def test_necromancy_places_mounted_revenant_legally(db):
    e = build_engine(db, [
        ("human", "Grave Robber", (18, 18), 0.0, 0),
        ("human", MOUNTED, (30, 5), 0.0, 0),
        ("llm", "Werebear", (33, 33), 0.0, 0),
    ])
    lancer = e.state.figure(1)
    lancer.eliminated = True
    necro = e.state.figure(0)
    pos, facing = e._free_contact_position(necro, lancer)
    circles = lancer.circles(pos, facing)
    assert e.state.board.contains_circles(circles)
    assert not circles_overlap(circles, necro.circles())


def test_formation_members_cannot_end_overlapping_each_other(db):
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),                # KI mounted
        ("human", "Liege Knight", (11.1, 10), 0.0, 0),
        ("human", "Liege Knight", (12.2, 10), 0.0, 0),
        ("llm", "Werebear", (20, 20), -math.pi / 2, 0),
    ])
    e.state.figure(0).disabled_ability_ids.add(ab.CHARGE)  # allow formation
    # Rig dests so the mounted member's rear circle (facing +y after the move)
    # swings onto member 1's destination.
    intent = MoveIntent(
        0, (10, 12), facing=-math.pi / 2,   # rear at (10, 13.1)
        formation_uids=(0, 1, 2),
        member_dests=((10, 12), (10, 13.1), (11.1, 13.1)),
        member_facings=(-math.pi / 2, 0.0, 0.0),
    )
    r = e.apply(intent)
    assert isinstance(r, Rejection) and r.reason == "end_on_base"


def test_apply_gated_outside_battle_phase(db):
    from clixengine.demo import demo_armies
    from clixengine.setup import build_game
    h, l = demo_armies(200, seed=1)
    e = build_game(h, l, 200, seed=1, with_deploy=True)
    assert e.state.phase == "deploy"
    uid = next(f.uid for f in e.state.living("human"))
    r = e.apply(MoveIntent(uid, (10, 10), facing=0.0))
    assert not r.ok and r.reason == "not_battle"


def test_pickled_rider_survives_and_resolves(db):
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 14), -math.pi / 2, 0),
    ])
    assert e.apply(MoveIntent(0, (10, 12.9), facing=math.pi / 2)).ok
    clone = pickle.loads(pickle.dumps(e))
    assert clone._pending_rider is not None
    assert clone.apply(CloseIntent(0, 1, rider=True)).ok
