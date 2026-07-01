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
