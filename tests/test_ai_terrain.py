"""The AI must route around terrain, not freeze on it.

Regression suite for the live-game stall: blocking terrain in front of the AI's
line made every straight-line advance illegal; the engine rejected them; both AI
loops treated any rejection as end-of-turn — so the opponent looked frozen.
"""

import math

from clixengine.ai.heuristic import HeuristicAI
from clixengine.candidates import generate_candidates
from clixengine.geometry import Vec
from clixengine.terrain import TerrainPiece

from .conftest import build_engine

# A wall spanning the mid-board, with open flanks (x<8 or x>28).
WALL = TerrainPiece(0, "blocking", (Vec(8, 17), Vec(28, 17), Vec(28, 19), Vec(8, 19)))


def _walled_engine(db, active="llm"):
    e = build_engine(db, [
        ("human", "Werebear", (18, 6), math.pi / 2, 0),
        ("llm", "Werebear", (18, 30), -math.pi / 2, 0),
        ("llm", "Utem Crossbowman", (14, 30), -math.pi / 2, 0),
    ], active=active)
    e.state.terrain.append(WALL)
    return e


def test_move_candidates_are_engine_legal_despite_wall(db):
    e = _walled_engine(db)
    for fig in e.state.living("llm"):
        for c in generate_candidates(e, fig):
            if c.kind != "move" or getattr(c.intent, "formation_uids", ()):
                continue
            v = e.validate_move(c.intent.figure_uid, c.intent.dest, c.intent.facing,
                                c.intent.free)
            assert v["ok"], f"candidate '{c.label}' is illegal: {v}"


def test_detour_candidate_offered_when_straight_line_blocked(db):
    # Mover right up against the wall: every straight advance crosses it.
    e = build_engine(db, [
        ("human", "Werebear", (18, 6), math.pi / 2, 0),
        ("llm", "Werebear", (18, 21), -math.pi / 2, 0),
    ], active="llm")
    e.state.terrain.append(WALL)
    mover, enemy = e.state.figure(1), e.state.figure(0)
    cands = generate_candidates(e, mover)
    detours = [c for c in cands if c.annotation.get("intent_hint") == "detour"]
    assert detours, "expected an 'around terrain' advance when the direct line is walled off"
    # Against a wide wall the step may be lateral (flanking), but it must be
    # engine-legal and must not walk mostly AWAY from the enemy.
    d0 = math.hypot(mover.position.x - enemy.position.x, mover.position.y - enemy.position.y)
    for det in detours:
        v = e.validate_move(det.intent.figure_uid, det.intent.dest, det.intent.facing)
        assert v["ok"], f"detour is illegal: {v}"
        d1 = math.hypot(det.intent.dest[0] - enemy.position.x,
                        det.intent.dest[1] - enemy.position.y)
        assert d1 < d0 + mover.speed * 0.5 + 1e-6


def test_heuristic_rounds_converge_past_the_wall(db):
    """Over a few rounds the two sides actually reach each other around the wall
    (the greedy detour must not oscillate in place forever)."""
    e = _walled_engine(db)
    ai = HeuristicAI()
    d_start = min(
        math.hypot(a.position.x - b.position.x, a.position.y - b.position.y)
        for a in e.state.living("llm") for b in e.state.living("human")
    )
    for _ in range(8):  # 8 half-turns, both sides heuristic
        if e.state.ended:
            break
        list(ai.stream_turn(e))
    d_end = min(
        math.hypot(a.position.x - b.position.x, a.position.y - b.position.y)
        for a in e.state.living("llm") for b in e.state.living("human")
        if a.is_alive and b.is_alive
    )
    assert d_end < d_start - 6, f"armies failed to close around the wall ({d_start:.1f} -> {d_end:.1f})"


def test_hindering_start_halves_candidate_budget(db):
    e = build_engine(db, [
        ("human", "Werebear", (10, 10), math.pi / 2, 0),
        ("llm", "Werebear", (10, 30), -math.pi / 2, 0),
    ], active="human")
    sp = e.state.figure(0).speed
    e.state.terrain.append(TerrainPiece(
        0, "hindering", (Vec(6, 6), Vec(14, 6), Vec(14, 14), Vec(6, 14))))
    half = max(1, math.ceil(sp / 2))
    for c in generate_candidates(e, e.state.figure(0)):
        if c.kind == "move":
            assert c.annotation["move_distance"] <= half + 1e-6


def test_heuristic_turn_advances_despite_wall(db):
    e = _walled_engine(db)
    before = {f.uid: (f.position.x, f.position.y) for f in e.state.living("llm")}
    steps = list(HeuristicAI().stream_turn(e))
    assert steps, "the opponent froze: no actions taken against a walled board"
    moved = any((f.position.x, f.position.y) != before[f.uid] for f in e.state.living("llm"))
    assert moved, "no llm figure moved at all"


def test_best_decision_exclusion_repicks(db):
    e = _walled_engine(db)
    ai = HeuristicAI()
    first = ai.best_decision(e)
    assert first is not None
    second = ai.best_decision(e, frozenset({repr(first.candidate.intent)}))
    assert second is not None
    assert repr(second.candidate.intent) != repr(first.candidate.intent)


# --- terrain vs formations, entry-stop, fliers, escape hatch ------------------
from clixengine.intents import MoveIntent  # noqa: E402


def _formation_engine(db, terrain_piece):
    """Three touching same-faction figures pointed at an enemy, with a piece of
    terrain directly in their path."""
    e = build_engine(db, [
        ("human", "Werebear", (18, 30), math.pi / 2, 0),
        ("llm", "Brass Golem", (16.9, 10), math.pi / 2, 0),
        ("llm", "Brass Golem", (18.0, 10), math.pi / 2, 0),
        ("llm", "Brass Golem", (19.1, 10), math.pi / 2, 0),
    ], active="llm")
    e.state.terrain.append(terrain_piece)
    return e


def _formation_intent(e, dy):
    uids = (1, 2, 3)
    dests = tuple((e.state.figure(u).position.x, e.state.figure(u).position.y + dy) for u in uids)
    facings = (math.pi / 2,) * 3
    return MoveIntent(1, dests[0], facings[0], formation_uids=uids,
                      member_dests=dests, member_facings=facings)


def test_formation_cannot_enter_blocking_terrain(db):
    wall = TerrainPiece(0, "blocking", (Vec(14, 13), Vec(22, 13), Vec(22, 15), Vec(14, 15)))
    e = _formation_engine(db, wall)
    r = e.apply(_formation_intent(e, 4.0))  # would march the line into the wall
    assert not r.ok and r.reason in ("in_blocking", "path_blocked")
    assert all(e.state.figure(u).position.y == 10 for u in (1, 2, 3))  # nobody moved


def test_formation_cannot_enter_deep_water(db):
    pond = TerrainPiece(0, "clear", (Vec(14, 13), Vec(22, 13), Vec(22, 17), Vec(14, 17)),
                        water="deep")
    e = _formation_engine(db, pond)
    r = e.apply(_formation_intent(e, 4.0))
    assert not r.ok and r.reason in ("in_blocking", "path_blocked")


def test_formation_speed_halved_when_starting_in_hindering(db):
    woods = TerrainPiece(0, "hindering", (Vec(14, 8), Vec(22, 8), Vec(22, 12), Vec(14, 12)))
    e = _formation_engine(db, woods)  # the line starts inside the woods
    sp = min(e.state.figure(u).speed for u in (1, 2, 3))
    half = max(1, math.ceil(sp / 2))
    r = e.apply(_formation_intent(e, half + 1.0))
    assert not r.ok and r.reason == "too_far"
    r2 = e.apply(_formation_intent(e, float(half)))
    assert r2.ok, f"{getattr(r2, 'reason', '')}: {getattr(r2, 'detail', '')}"


def test_single_move_must_stop_on_entering_hindering(db):
    e = build_engine(db, [
        ("human", "Amazon Blademistress", (18, 10), math.pi / 2, 0),  # speed 10
        ("llm", "Werebear", (18, 30), -math.pi / 2, 0),
    ], active="human")
    e.state.terrain.append(TerrainPiece(
        0, "hindering", (Vec(15, 13), Vec(21, 13), Vec(21, 16), Vec(15, 16))))
    sp = e.state.figure(0).speed
    assert sp >= 8  # needs to be able to overshoot the 3"-deep woods
    # Blowing straight through the woods is illegal...
    v = e.validate_move(0, (18, 10 + sp))
    assert not v["ok"] and v["reason"] == "must_stop_in_hindering"
    # ...but stopping inside them is fine.
    assert e.validate_move(0, (18, 14.5))["ok"] is True


def test_flier_may_cross_but_not_land_in_blocking(db):
    e = build_engine(db, [
        ("human", "Feral Bloodsucker", (18, 10), math.pi / 2, 0),  # Flight, speed 10
        ("llm", "Werebear", (18, 30), -math.pi / 2, 0),
    ], active="human")
    flier = e.state.figure(0)
    assert 98 in flier.active_ability_ids(), "test needs a figure with Flight"
    e.state.terrain.append(TerrainPiece(
        0, "blocking", (Vec(15, 12), Vec(21, 12), Vec(21, 15), Vec(15, 15))))
    sp = flier.speed
    # Soaring OVER the block is legal...
    assert e.validate_move(0, (18, min(10 + sp, 17.0)))["ok"] is True
    # ...but landing IN it is not (§Flight card text).
    v = e.validate_move(0, (18, 13.5))
    assert not v["ok"] and v["reason"] == "in_blocking"


def test_stuck_figure_can_escape_blocking(db):
    """Figures trapped inside blocking by the old formation hole must be able to
    walk out (the path check is waived for an illegally-overlapping start)."""
    e = build_engine(db, [
        ("human", "Werebear", (18, 14), math.pi / 2, 0),
        ("llm", "Werebear", (18, 30), -math.pi / 2, 0),
    ], active="human")
    e.state.terrain.append(TerrainPiece(
        0, "blocking", (Vec(15, 12), Vec(21, 12), Vec(21, 16), Vec(15, 16))))
    # (18,14) is inside the block — the bug state. Walking out is legal...
    v = e.validate_move(0, (18, 18))
    assert v["ok"], f"trapped figure cannot escape: {v}"
    # ...but ending still-inside is not.
    assert not e.validate_move(0, (17, 14))["ok"]


# --- drawn-terrain size caps -------------------------------------------------
def _placing_engine(db):
    e = build_engine(db, [
        ("human", "Werebear", (18, 2), math.pi / 2, 0),
        ("llm", "Werebear", (18, 34), -math.pi / 2, 0),
    ], active="human")
    e.state.phase = "terrain"
    e.state.first_player = "human"
    e.state.terrain_budget = {"human": 2, "llm": 2}
    e.state.terrain_turn = "human"
    return e


def test_polygon_terrain_rejects_oversized_area(db):
    e = _placing_engine(db)
    giant = [(8, 8), (30, 10), (16, 26)]  # far beyond 24 in²
    r = e.place_terrain_polygon("human", "blocking", giant)
    assert not r.ok and r.reason == "too_big"


def test_polygon_terrain_rejects_board_spanning_sliver(db):
    e = _placing_engine(db)
    sliver = [(5, 17.8), (30, 17.8), (30, 18.4), (5, 18.4)]  # ~15 in² but 25" long
    r = e.place_terrain_polygon("human", "blocking", sliver)
    assert not r.ok and r.reason == "too_big"


def test_polygon_terrain_accepts_reasonable_shape(db):
    e = _placing_engine(db)
    ok_shape = [(16, 16), (20, 16), (21, 19), (18, 21), (15, 19)]  # ~19 in², ~6" across
    r = e.place_terrain_polygon("human", "hindering", ok_shape)
    assert r.ok, f"{getattr(r, 'reason', '')}: {getattr(r, 'detail', '')}"
