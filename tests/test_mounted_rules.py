"""Mounted-warrior rules P5-R3..R7 + Shake Off (docs/lancers-plan.md §2)."""

import math

from clixengine import abilities as ab
from clixengine.geometry import Vec
from clixengine.intents import MoveIntent

from .conftest import build_engine

MOUNTED = "Light Lancer On Light Warhorse"  # arc 180, speed 7 @ click 0 (Charge)


def _seed_roll(engine, wanted: int, tag: str = "break_away") -> None:
    """Advance the engine RNG until the NEXT d6 for ``tag`` equals wanted."""
    # The DiceRoller is deterministic per seed; probe by copy.
    import copy
    for _ in range(500):
        probe = copy.deepcopy(engine.rng)
        if probe.d6(tag) == wanted:
            return
        engine.rng.d6("_burn")
    raise AssertionError(f"could not steer RNG to a {wanted}")


def test_break_away_fails_only_on_one(db):
    # P5-R3: threshold 2 (fail only on a natural 1) for mounted figures.
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),
        ("llm", "Utem Guardsman", (11.1, 10), math.pi, 0),
    ])
    m = e.state.figure(0)
    assert ab.break_away_min(m) == 2
    assert ab.break_away_min(e.state.figure(1)) == 4


def test_failed_break_away_mounted_may_not_rotate(db):
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),
        ("llm", "Utem Guardsman", (11.1, 10), math.pi, 0),
    ])
    m = e.state.figure(0)
    _seed_roll(e, 1)  # force the break-away roll to fail
    r = e.apply(MoveIntent(0, (14, 14), facing=math.pi / 2))
    assert r.ok
    ba = next(ev for ev in r.events if ev["type"] == "break_away")
    assert not ba["success"]
    assert m.position == Vec(10, 10)
    assert m.facing == 0.0  # P5-R4: no re-face on a failed mounted break-away


def test_failed_break_away_foot_still_rotates(db):
    e = build_engine(db, [
        ("human", "Utem Guardsman", (10, 10), 0.0, 0),
        ("llm", "Utem Guardsman", (11.1, 10), math.pi, 0),
    ])
    f = e.state.figure(0)
    _seed_roll(e, 1)
    r = e.apply(MoveIntent(0, (5, 5), facing=math.pi / 2))
    assert r.ok and f.position == Vec(10, 10) and f.facing == math.pi / 2


def test_shake_off_hits_rear_contacts_only(db):
    # Mounted at (10,10) facing +x. Enemy A on the rear circle (outside the
    # front arc) takes Shake Off; enemy B ahead (front arc) does not.
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),
        ("llm", "Utem Guardsman", (7.8, 10), 0.0, 0),      # rear-circle contact
        ("llm", "Utem Guardsman", (11.1, 10), math.pi, 0), # dead ahead
    ])
    a, b = e.state.figure(1), e.state.figure(2)
    a0, b0 = a.current_click, b.current_click
    _seed_roll(e, 6)  # guarantee break-away success
    # Ride out perpendicular so the path doesn't cross the blocker dead ahead.
    r = e.apply(MoveIntent(0, (10, 16), facing=math.pi / 2))
    assert r.ok
    shakes = [ev for ev in r.events if ev["type"] == "shake_off"]
    assert [s["target"] for s in shakes] == [1]
    assert a.current_click == a0 + 1 and b.current_click == b0


def test_shake_off_reduced_by_toughness(db):
    # Steam Golem has Toughness on click 0: Shake Off's 1 click reduces to 0.
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),
        ("llm", "Steam Golem", (7.8, 10), 0.0, 0),
    ])
    golem = e.state.figure(1)
    assert ab.has(golem, ab.TOUGHNESS)
    g0 = golem.current_click
    _seed_roll(e, 6)
    r = e.apply(MoveIntent(0, (16, 10), facing=0.0))
    assert r.ok
    shakes = [ev for ev in r.events if ev["type"] == "shake_off"]
    assert shakes and shakes[0]["clicks"] == 0
    assert golem.current_click == g0


def test_no_shake_off_on_failed_break_away(db):
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),
        ("llm", "Utem Guardsman", (7.8, 10), 0.0, 0),
    ])
    _seed_roll(e, 1)
    r = e.apply(MoveIntent(0, (16, 10), facing=0.0))
    assert r.ok
    assert not [ev for ev in r.events if ev["type"] == "shake_off"]


def test_mounted_never_spins_but_grants_spins(db):
    # P5-R6 both directions: a mounted mover contacting a standard defender
    # GRANTS the spin; a mounted defender contacted by anyone never gets one.
    e = build_engine(db, [
        ("human", MOUNTED, (10, 5), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 12), -math.pi / 2, 0),
        ("llm", MOUNTED, (20, 12), -math.pi / 2, 0),
    ])
    r = e.apply(MoveIntent(0, (10, 10.9), facing=math.pi / 2))  # into the Guardsman
    assert r.ok
    offers = [ev for ev in r.events if ev["type"] == "free_spin_offer"]
    assert offers and offers[0]["spinners"] == [1]

    e2 = build_engine(db, [
        ("human", "Utem Guardsman", (20, 5), math.pi / 2, 0),
        ("llm", MOUNTED, (20, 12), -math.pi / 2, 0),  # rear at (20, 13.1)
    ])
    r2 = e2.apply(MoveIntent(0, (20, 10.9), facing=math.pi / 2))  # touch its front circle
    assert r2.ok
    assert not [ev for ev in r2.events if ev["type"] == "free_spin_offer"]


def test_move_validation_is_facing_aware(db):
    # Same destination, two facings: one swings the rear circle into a
    # neighbour (illegal), the other is clear (P5-R7).
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),
        ("human", "Utem Guardsman", (14, 12.2), 0.0, 0),
        ("llm", "Werebear", (30, 30), 0.0, 0),
    ])
    dest = (14, 14)
    ok = e.validate_move(0, dest, facing=math.pi / 2)      # rear at (14,12.9): gap 0.7-1.1 <0 overlap? see below
    bad = e.validate_move(0, dest, facing=-math.pi / 2)    # rear at (14,15.1): clear
    # rear for facing +pi/2 = (14, 12.9); guardsman at (14,12.2): dist 0.7 < 1.1 => overlap
    assert not ok["ok"] and ok["reason"] == "end_on_base"
    assert bad["ok"]


def test_mounted_deploy_band_capsule_fit(db):
    e = build_engine(db, [
        ("human", MOUNTED, (10, 1.75), math.pi / 2, 0),
        ("llm", "Werebear", (30, 34), -math.pi / 2, 0),
    ])
    e.state.phase = "deploy"
    m = e.state.figure(0)
    # Facing the enemy (+y): front dot must sit in [1.65, 2.45].
    assert e.deploy_figure("human", 0, (10, 1.75), math.pi / 2).ok
    assert e.deploy_figure("human", 0, (10, 1.5), math.pi / 2).reason == "out_of_area"
    # Facing along the band (+x) needs x-room for the rear circle instead.
    assert e.deploy_figure("human", 0, (10, 1.5), 0.0).ok
    # Facing away (-y) can never fit the rear circle in a 3" band from y<=2.45.
    assert not e.deploy_figure("human", 0, (10, 2.4), -math.pi / 2).ok


def test_selfplay_smoke_with_mounted_armies(db):
    """A seeded battle where both sides field cavalry terminates without any
    illegal engine states (the P5 acceptance smoke)."""
    from clixengine.army import Army
    from clixengine.setup import build_game
    from clixengine.ai.heuristic import HeuristicAI

    lancers = [f for f in db.all_figures() if f.expansion == "Lancers"]
    mounted = sorted((f for f in lancers if f.mounted), key=lambda f: f.points)
    foot = sorted((f for f in lancers if not f.mounted), key=lambda f: f.points)
    h = Army(name="h", owner="human",
             figure_ids=[mounted[0].id, mounted[1].id, foot[0].id])
    l = Army(name="l", owner="llm",
             figure_ids=[mounted[2].id, mounted[3].id, foot[1].id])
    e = build_game(h, l, 200, seed=7)
    ai = HeuristicAI()
    turns = 0
    while not e.state.ended and turns < 120:
        acted = False
        while e.actionable_figures() and not e.state.ended:
            best = ai.best_decision(e)
            if best is None or best.score <= 0.0:
                break
            res = e.apply(best.candidate.intent)
            assert res.ok, f"illegal AI intent: {getattr(res, 'detail', '?')}"
            acted = True
        e.end_turn()
        turns += 1
    # Sanity: every living footprint fully on the board.
    for f in e.state.living():
        assert e.state.board.contains_circles(f.circles()), f.short_name
