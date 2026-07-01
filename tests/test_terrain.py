"""Terrain foundation: polygon geometry + TerrainPiece rule semantics."""

import math

from clixengine.geometry import (
    Vec,
    point_in_polygon,
    rotate_polygon,
    segment_crosses_polygon,
)
from clixengine.terrain import (
    TerrainPiece,
    elevation_at,
    instantiate,
    placement_reason,
    template,
)

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


# --- terrain library + placement ------------------------------------------
def test_library_instantiate_and_placement_reason():
    boulder = template("boulder")
    assert boulder is not None and boulder.blocks_move_kind()
    p = instantiate(boulder, Vec(18, 18), 0.0, 0, "human")
    assert p.kind == "blocking" and p.contains(Vec(18, 18))
    # legal in the open midfield of a 36x36 board
    assert placement_reason(p.polygon, [], 36, 36) is None
    # off the board / in a starting band / too close to an existing piece
    edge = instantiate(boulder, Vec(0.5, 18), 0.0, 1, "human")
    assert placement_reason(edge.polygon, [], 36, 36) == "off_board"
    start = instantiate(boulder, Vec(18, 2), 0.0, 2, "human")
    assert placement_reason(start.polygon, [], 36, 36) == "in_starting_area"
    near = instantiate(boulder, Vec(19.5, 18), 0.0, 3, "human")
    assert placement_reason(near.polygon, [p], 36, 36) == "too_close"


def _terrain_engine(db):
    e = build_engine(db, [
        ("human", "Werebear", (18, 2), math.pi / 2, 0),
        ("llm", "Werebear", (18, 34), -math.pi / 2, 0),
    ], active="human")
    e.state.phase = "terrain"
    e.state.first_player = "human"
    e.state.terrain_budget = {"human": 1, "llm": 1}
    e.state.terrain_turn = "human"
    return e


def test_place_terrain_alternates_then_starts_battle(db):
    e = _terrain_engine(db)
    # Not the llm's turn yet.
    assert e.place_terrain("llm", "boulder", (10, 18)).reason == "not_your_turn"
    # Human places -> turn passes to the llm, human's budget spent.
    r = e.place_terrain("human", "boulder", (10, 18))
    assert r.ok and len(e.state.terrain) == 1
    assert e.state.terrain_turn == "llm" and e.state.terrain_budget["human"] == 0
    assert e.state.phase == "terrain"  # still placing
    # LLM places the last piece -> battle begins, first player is to act.
    r2 = e.place_terrain("llm", "forest", (26, 18))
    assert r2.ok and len(e.state.terrain) == 2
    assert e.state.phase == "battle" and e.state.active_player == "human"
    # No more placing once the battle is on.
    assert e.place_terrain("human", "pond", (18, 18)).reason == "not_placing"


def test_terrain_placement_candidates_are_legal(db):
    e = _terrain_engine(db)
    cands = e.terrain_placement_candidates("human")
    assert cands, "expected at least one candidate placement"
    for c in cands:
        tmpl = template(c["key"])
        piece = instantiate(tmpl, Vec(*c["center"]), c["rotation"], 99, "human")
        assert placement_reason(piece.polygon, e.state.terrain, 36, 36) is None


def test_polygon_is_simple():
    from clixengine.geometry import polygon_is_simple
    square = (Vec(0, 0), Vec(4, 0), Vec(4, 4), Vec(0, 4))
    bowtie = (Vec(0, 0), Vec(4, 4), Vec(4, 0), Vec(0, 4))  # self-intersecting
    assert polygon_is_simple(square)
    assert not polygon_is_simple(bowtie)
    assert not polygon_is_simple((Vec(0, 0), Vec(1, 1)))  # too few


def test_place_terrain_polygon(db):
    e = _terrain_engine(db)  # human first, 1 piece each
    # 5 x 4.5 = 22.5 in², ~6.7" diagonal — inside the drawn-terrain size caps.
    good = [(16, 16), (21, 16), (21, 20.5), (16, 20.5)]
    # rejections (none consume budget)
    assert e.place_terrain_polygon("human", "blocking", good[:2]).reason == "bad_polygon"
    assert e.place_terrain_polygon("human", "nope", good).reason == "no_such_terrain"
    assert e.place_terrain_polygon("human", "blocking",
                                   [(16, 16), (21, 20.5), (21, 16), (16, 20.5)]).reason == "self_intersecting"
    assert e.place_terrain_polygon("human", "blocking",
                                   [(14, 0.5), (18, 0.5), (16, 2)]).reason == "in_starting_area"
    # success: an elevated hill in midfield
    r = e.place_terrain_polygon("human", "elevated", good)
    assert r.ok and len(e.state.terrain) == 1
    p = e.state.terrain[0]
    assert p.elevated and p.kind == "clear" and p.owner == "human"
    assert e.state.terrain_turn == "llm" and e.state.terrain_budget["human"] == 0


def test_skip_terrain_forfeits_and_hands_off(db):
    e = _terrain_engine(db)  # human first, both have 1 piece
    r = e.skip_terrain_placement("human")
    assert r.ok and e.state.terrain_budget["human"] == 0
    assert e.state.terrain_turn == "llm" and e.state.phase == "terrain"
    # llm skipping too ends setup and starts the battle even with 0 pieces placed.
    r2 = e.skip_terrain_placement("llm")
    assert r2.ok and e.state.phase == "battle" and len(e.state.terrain) == 0
