"""Terrain foundation: polygon geometry + TerrainPiece rule semantics."""

import math

from clixengine.geometry import (
    Vec,
    point_in_polygon,
    rotate_polygon,
    segment_crosses_polygon,
)
from clixengine.terrain import TerrainPiece, elevation_at

SQUARE = (Vec(2, 2), Vec(6, 2), Vec(6, 6), Vec(2, 6))


# --- geometry --------------------------------------------------------------
def test_point_in_polygon():
    assert point_in_polygon(Vec(4, 4), SQUARE)
    assert not point_in_polygon(Vec(0, 0), SQUARE)
    assert not point_in_polygon(Vec(8, 4), SQUARE)


def test_segment_crosses_polygon():
    assert segment_crosses_polygon(Vec(0, 4), Vec(8, 4), SQUARE)  # straight through
    assert segment_crosses_polygon(Vec(4, 4), Vec(10, 4), SQUARE)  # starts inside
    assert not segment_crosses_polygon(Vec(0, 0), Vec(1, 1), SQUARE)  # clear miss
    assert not segment_crosses_polygon(Vec(0, 10), Vec(10, 10), SQUARE)  # passes above


def test_rotate_polygon_90():
    r = rotate_polygon(SQUARE, Vec(4, 4), math.pi / 2)
    # (2,2) about (4,4) by +90deg -> (6,2)
    assert abs(r[0].x - 6) < 1e-9 and abs(r[0].y - 2) < 1e-9


# --- terrain semantics -----------------------------------------------------
def test_blocking_terrain():
    t = TerrainPiece(0, "blocking", SQUARE)
    assert t.blocks_move() and t.blocks_lof_ground()
    assert not t.is_hindering_move() and not t.halves_speed()


def test_hindering_terrain():
    t = TerrainPiece(1, "hindering", SQUARE)
    assert t.is_hindering_move() and t.halves_speed() and t.hinders_lof()
    assert not t.blocks_move() and not t.blocks_lof_ground()


def test_deep_water_moves_like_blocking_no_ranged_effect():
    t = TerrainPiece(2, "clear", SQUARE, water="deep")
    assert t.blocks_move()
    assert not t.blocks_lof_ground() and not t.hinders_lof()  # water: no ranged effect


def test_shallow_water_moves_like_hindering_no_ranged_effect():
    t = TerrainPiece(3, "clear", SQUARE, water="shallow")
    assert t.is_hindering_move() and t.halves_speed()
    assert not t.hinders_lof()  # water has no ranged effect


def test_low_wall_hindering_but_no_speed_halve():
    t = TerrainPiece(4, "hindering", SQUARE, low_wall=True)
    assert t.is_hindering_move() and t.hinders_lof()
    assert not t.halves_speed()  # low-wall exception


def test_elevation_lookup():
    hill = TerrainPiece(5, "clear", SQUARE, elevated=True)
    assert elevation_at([hill], Vec(4, 4)) == 1
    assert elevation_at([hill], Vec(0, 0)) == 0


# --- terrain effects on the engine's move validation -----------------------
from .conftest import build_engine  # noqa: E402


def test_blocking_terrain_blocks_and_bounds_a_move(db):
    e = build_engine(db, [
        ("human", "Werebear", (10, 10), 0.0, 0),
        ("llm", "Werebear", (30, 30), 0.0, 0),
    ], active="human")
    # A blocking wall just east of the mover.
    e.state.terrain.append(TerrainPiece(0, "blocking", (Vec(11.5, 5), Vec(12.5, 5), Vec(12.5, 20), Vec(11.5, 20))))
    assert e.validate_move(0, (14, 10))["reason"] == "path_blocked"   # straight through the wall
    assert e.validate_move(0, (12, 10))["reason"] == "in_blocking"    # ends inside the wall
    assert e.validate_move(0, (10, 13))["ok"] is True                 # stays on the near side


def test_hindering_halves_speed(db):
    e = build_engine(db, [
        ("human", "Werebear", (10, 10), 0.0, 0),
        ("llm", "Werebear", (30, 30), 0.0, 0),
    ], active="human")
    sp = e.state.figure(0).speed
    half = max(1, math.ceil(sp / 2))
    assert sp > half  # Werebear is fast enough for the test to be meaningful
    # A hindering patch covering the mover's start halves its speed for the turn.
    e.state.terrain.append(TerrainPiece(0, "hindering", (Vec(6, 6), Vec(14, 6), Vec(14, 14), Vec(6, 14))))
    assert e.validate_move(0, (10, 10 + sp))["reason"] == "too_far"   # full speed now too far
    assert e.validate_move(0, (10, 10 + half))["ok"] is True          # within halved speed


# --- terrain effects on line of fire + combat ------------------------------
def _shooter(db):
    return build_engine(db, [
        ("human", "Utem Crossbowman", (10, 10), math.pi / 2, 0),
        ("llm", "Werebear", (10, 14), -math.pi / 2, 0),
    ], active="human")


def test_blocking_terrain_blocks_line_of_fire(db):
    e = _shooter(db)
    assert e.line_of_fire(0, 1)[0] is True  # clear first
    e.state.terrain.append(TerrainPiece(0, "blocking", (Vec(8, 11.5), Vec(12, 11.5), Vec(12, 12.5), Vec(8, 12.5))))
    ok, reason = e.line_of_fire(0, 1)
    assert not ok and "terrain" in reason


def test_hindering_adds_defense_vs_ranged_only(db):
    e = _shooter(db)
    assert e.explain_attack(0, 1, "ranged")["defense"]["terrain"] == 0
    e.state.terrain.append(TerrainPiece(0, "hindering", (Vec(8, 11.5), Vec(12, 11.5), Vec(12, 12.5), Vec(8, 12.5))))
    assert e.explain_attack(0, 1, "ranged")["defense"]["terrain"] == 1  # +1 hindering
    # close combat is unaffected by hindering (no elevation here).
    assert e.terrain_defense_mod(e.state.figure(0), e.state.figure(1), "close") == 0


def test_height_advantage_vs_elevated_target(db):
    e = _shooter(db)
    base = e.explain_attack(0, 1, "ranged")["defense"]["effective"]
    # Put the target on an elevated hill (it stands on it, so its own hill won't block).
    e.state.terrain.append(TerrainPiece(0, "clear", (Vec(8, 12), Vec(12, 12), Vec(12, 16), Vec(8, 16)), elevated=True))
    x = e.explain_attack(0, 1, "ranged")
    assert x["defense"]["terrain"] >= 1  # height advantage +1
    assert x["defense"]["effective"] == base + x["defense"]["terrain"]
    # height advantage applies to close combat too (target elevated, attacker not).
    assert e.terrain_defense_mod(e.state.figure(0), e.state.figure(1), "close") == 1
