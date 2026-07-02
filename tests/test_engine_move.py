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
