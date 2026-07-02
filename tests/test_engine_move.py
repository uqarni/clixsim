import math

import pytest

from clixengine.geometry import Vec, distance
from clixengine.intents import MoveIntent
from clixengine.state import STANDARD_BASE_RADIUS

from .conftest import build_engine


def a_melee(db):
    return db.find("Werebear")[0].id


def test_valid_move_updates_position_and_facing(db):
    e = build_engine(db, [("human", "Werebear", (18, 18), 0.0, 0)])
    f = e.state.figure(0)
    spd = f.speed
    res = e.apply(MoveIntent(0, (18 + spd, 18), math.pi))
    assert res.ok
    assert f.position.x == pytest.approx(18 + spd)
    assert f.facing == pytest.approx(math.pi)
    assert 0 in e._acted_uids


def test_move_too_far_rejected(db):
    e = build_engine(db, [("human", "Werebear", (18, 18), 0.0, 0)])
    spd = e.state.figure(0).speed
    res = e.apply(MoveIntent(0, (18 + spd + 2, 18), 0.0))
    assert not res.ok and res.reason == "too_far"


def test_move_off_board_rejected(db):
    e = build_engine(db, [("human", "Werebear", (1.0, 1.0), 0.0, 0)])
    res = e.apply(MoveIntent(0, (-0.5, 1.0), 0.0))
    assert not res.ok and res.reason == "off_board"


def test_move_path_blocked_by_base(db):
    # Blocker sits directly between mover and destination.
    e = build_engine(
        db,
        [
            ("human", "Werebear", (10, 18), 0.0, 0),
            ("llm", "Werebear", (12, 18), 0.0, 0),
        ],
    )
    res = e.apply(MoveIntent(0, (14, 18), 0.0))
    assert not res.ok and res.reason == "path_blocked"


def test_break_away_property(db):
    # Mover in base contact with an opponent must roll to break away.
    for seed in range(40):
        e = build_engine(
            db,
            [
                ("human", "Werebear", (18, 18), 0.0, 0),
                ("llm", "Werebear", (19.1, 18), math.pi, 0),  # touching
            ],
            seed=seed,
        )
        f = e.state.figure(0)
        start = Vec(f.position.x, f.position.y)
        dest = (18, 22)  # move straight up, away from the contact
        res = e.apply(MoveIntent(0, dest, math.pi))
        assert res.ok
        ba = next(ev for ev in res.events if ev["type"] == "break_away")
        if ba["success"]:
            assert f.position.y == pytest.approx(22)
        else:
            # Failed break-away: may not move, but may re-face.
            assert f.position.x == pytest.approx(start.x)
            assert f.position.y == pytest.approx(start.y)
            assert f.facing == pytest.approx(math.pi)


def test_reface_in_place_allowed_even_when_adjacent(db):
    # Zero-distance move (just re-face) is legal even if a base is adjacent.
    e = build_engine(
        db,
        [
            ("human", "Werebear", (18, 18), 0.0, 0),
            ("llm", "Werebear", (19.1, 18), math.pi, 0),
        ],
    )
    res = e.apply(MoveIntent(0, (18, 18), 1.0))
    assert res.ok
    assert e.state.figure(0).facing == pytest.approx(1.0)


def test_move_may_not_end_overlapping_a_base(db):
    """Regression: the path check treats the mover as a point, so a walker could
    END half-on-top of a neighbour (live bug: 'medic on top of my Magus')."""
    from clixengine.intents import MoveIntent

    def fresh():
        return build_engine(db, [
            ("human", "Leech Medic", (10, 10), 0.0, 0),
            ("human", "Magus", (13, 10), 0.0, 0),
            ("llm", "Werebear", (30, 30), 0.0, 0),
        ], active="human")

    for off in (0.6, 0.9, 1.05):  # inside the overlap band, path-line clear
        r = fresh().apply(MoveIntent(0, (13 - off, 10), 0.0))
        assert not r.ok and r.reason in ("end_on_base", "path_blocked"), (off, r)
    # Exact base contact remains the closest legal stop.
    ok = fresh().apply(MoveIntent(0, (13 - 1.1, 10), 0.0))
    assert ok.ok


def test_etiquette_tolerance_contact_survives_view_rounding(db):
    """Live bug: the client snaps using view positions rounded to 3 decimals, so a
    'snapped' landing could be ~1e-3 short of true contact — and the old 1e-6
    contact epsilon then denied the close attack ("Advance into contact 0.0\"").
    Near-touching counts as touching (P4-R39 / §Etiquette)."""
    from clixengine.candidates import generate_candidates

    e = build_engine(db, [
        ("human", "Crystal Bladesman", (10, 10.0004), 0.0, 0),
        ("llm", "Nightstalker", (14, 9.9996), math.pi, 0),
    ], active="human")
    mover, target = e.state.figure(0), e.state.figure(1)
    # Contact point computed from the ROUNDED enemy position, like the client:
    rx, ry = round(target.position.x, 3), round(target.position.y, 3)
    gap = mover.base_radius + target.base_radius
    r = e.apply(MoveIntent(0, (rx - gap, ry), 0.0))
    assert r.ok, r
    # Sub-millimetre error vs the TRUE position — still legally in base contact:
    assert [t.uid for t, _ in e.legal_close_targets(mover)] == [target.uid]
    # And the generator offers the close attack, not another "advance into contact".
    e._acted_uids.clear()  # pretend it's a fresh turn for this figure
    kinds = {(c.kind, c.annotation.get("intent_hint")) for c in generate_candidates(e, mover)}
    assert ("close", None) in kinds
    assert not any(h == "charge" for _, h in kinds), kinds


def test_overlap_tolerance_bounds(db):
    """Overlap within the etiquette tolerance counts as touching; deeper is
    still an illegal end_on_base."""
    def try_dest(offset):
        e = build_engine(db, [
            ("human", "Crystal Bladesman", (10, 10), 0.0, 0),
            ("llm", "Nightstalker", (14, 10), math.pi, 0),
        ], active="human")
        return e.apply(MoveIntent(0, (14 - offset, 10), 0.0))

    assert try_dest(1.09).ok            # 0.01" overlap — etiquette-touching
    r = try_dest(1.05)                  # 0.05" overlap — genuinely on top
    assert not r.ok and r.reason == "end_on_base"
