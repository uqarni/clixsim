"""Terrain features (Phase 3).

A terrain piece is a simple polygon in world space (rotation already baked in) with
a type and a set of flags that select its movement / line-of-fire semantics per the
Jan-2002 rulebook. The engine owns geometry (DP2); this module just carries the data
and the rule-derived predicates.

Types: clear (only ever placed when elevated), hindering, blocking.
Water: shallow (moves like hindering) / deep (moves like blocking); no ranged effect.
Low wall: special hindering (stop at far side; speed never halved leaving it).
Abrupt elevated: elevated, but on/off only via access points (Flight bypasses); no
close combat on/off; formations can't span it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import (
    Vec,
    circle_intersects_polygon,
    point_in_polygon,
    polygon_polygon_distance,
    rotate_point,
    rotate_polygon,
    segment_crosses_polygon,
    swept_base_crosses_polygon,
)


@dataclass(frozen=True)
class TerrainPiece:
    id: int
    kind: str  # "clear" | "hindering" | "blocking"
    polygon: tuple[Vec, ...]  # world-space vertices, CCW, rotation baked in
    elevated: bool = False
    water: str | None = None  # "shallow" | "deep" | None
    low_wall: bool = False
    abrupt: bool = False
    access_points: tuple[Vec, ...] = ()  # required (>=1) for abrupt elevated
    owner: str = "human"  # who placed it (pool tracking)

    # --- movement semantics ------------------------------------------------
    def blocks_move(self) -> bool:
        """Figures can't enter or cross this (blocking terrain / deep water)."""
        return self.kind == "blocking" or self.water == "deep"

    def is_hindering_move(self) -> bool:
        """Passable but with the hindering movement rules (brush / shallow water /
        low wall). Deep water is blocking, not hindering."""
        return not self.blocks_move() and (
            self.kind == "hindering" or self.water == "shallow" or self.low_wall
        )

    def halves_speed(self) -> bool:
        """Starting a move inside this halves speed for the turn — EXCEPT low walls
        (a figure leaving a low wall is never slowed)."""
        return self.is_hindering_move() and not self.low_wall

    # --- line-of-fire semantics -------------------------------------------
    def blocks_lof_ground(self) -> bool:
        """Blocks a ground-level line of fire (blocking terrain; deep water does
        NOT affect ranged, so only true blocking blocks)."""
        return self.kind == "blocking" and self.water is None

    def hinders_lof(self) -> bool:
        """A line of fire crossing this adds +1 to the target's defense (hindering
        terrain and low walls; water has no ranged effect)."""
        return (self.kind == "hindering" or self.low_wall) and self.water is None

    # --- geometry ----------------------------------------------------------
    def contains(self, p: Vec) -> bool:
        return point_in_polygon(p, self.polygon)


@dataclass
class TerrainPool:
    """A player's undeployed terrain, chosen before placement (0-4 pieces)."""

    owner: str
    pieces: list[TerrainPiece] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Terrain library (curated shapes) + placement
# --------------------------------------------------------------------------- #
def _poly(pts: list[tuple[float, float]]) -> tuple[Vec, ...]:
    return tuple(Vec(x, y) for x, y in pts)


def _ngon(sides: int, r: float, squash: float = 1.0) -> tuple[Vec, ...]:
    """A convex n-gon centred at the origin (used for organic blobs/rocks)."""
    step = 2 * math.pi / sides
    # start a little off-axis so shapes read as irregular, not clock-perfect
    return tuple(
        Vec(math.cos(step * i + 0.35) * r, math.sin(step * i + 0.35) * r * squash)
        for i in range(sides)
    )


@dataclass(frozen=True)
class TerrainTemplate:
    """A placeable terrain shape: an origin-centred polygon plus its rule flags.
    Placement bakes a translation + rotation into a concrete ``TerrainPiece``."""

    key: str
    label: str
    kind: str  # "clear" | "hindering" | "blocking"
    polygon: tuple[Vec, ...]
    elevated: bool = False
    water: str | None = None
    low_wall: bool = False
    abrupt: bool = False
    access_points: tuple[Vec, ...] = ()  # origin-relative (abrupt elevated)
    blurb: str = ""  # one-line palette/AI hint

    def rule_summary(self) -> str:
        if self.blurb:
            return self.blurb
        if self.blocks_move_kind():
            return "impassable — blocks movement and line of fire"
        return "passable"

    def blocks_move_kind(self) -> bool:
        return self.kind == "blocking" or self.water == "deep"


# Six mechanically-distinct pieces (the palette the player chose):
#   Boulder  — blocking (blocks move + LoF)
#   Forest   — hindering (halves speed, +1 defense vs ranged crossing it)
#   Pond     — shallow water (halves speed; no line-of-fire effect)
#   Hill     — elevated clear (height advantage; blocks a ground shot crossing it)
#   Low wall — thin hindering wall (+1 vs ranged; never halves speed)
#   Plateau  — abrupt elevated (on/off only via access points)
TERRAIN_LIBRARY: tuple[TerrainTemplate, ...] = (
    TerrainTemplate(
        "boulder", "Boulder", "blocking", _ngon(6, 1.7, 0.85),
        blurb="Impassable rock — blocks movement and line of fire.",
    ),
    TerrainTemplate(
        "forest", "Forest", "hindering", _ngon(8, 2.4, 0.9),
        blurb="Woods — halve speed to move through; +1 defense to targets shot through it.",
    ),
    TerrainTemplate(
        "pond", "Pond", "clear", _ngon(7, 2.3, 0.8), water="shallow",
        blurb="Shallow water — halve speed to wade through; no effect on shooting.",
    ),
    TerrainTemplate(
        "hill", "Hill", "clear", _ngon(7, 3.0, 0.9), elevated=True,
        blurb="Elevated ground — height advantage (+1 defense) and a longer view.",
    ),
    TerrainTemplate(
        "low_wall", "Low wall", "hindering", _poly(
            [(-3.0, -0.3), (3.0, -0.3), (3.0, 0.3), (-3.0, 0.3)]
        ), low_wall=True,
        blurb="Low wall — +1 defense to targets behind it; never slows a figure leaving it.",
    ),
    TerrainTemplate(
        "plateau", "Plateau", "clear", _poly(
            [(-2.4, -2.4), (2.4, -2.4), (2.4, 2.4), (-2.4, 2.4)]
        ), elevated=True, abrupt=True,
        access_points=(Vec(0.0, -2.4), Vec(0.0, 2.4)),
        blurb="Steep plateau — elevated, but climbed on/off only at its access points.",
    ),
)

_LIBRARY_BY_KEY = {t.key: t for t in TERRAIN_LIBRARY}


# Size limits for hand-drawn terrain, in line with the curated shapes (the biggest
# preset — the plateau — is ~23 in² and ~6.8" across; the low wall is 6" long).
# Area keeps a piece from swallowing the midfield; extent keeps a legal-area sliver
# from stretching into a board-spanning wall.
MAX_POLYGON_AREA = 24.0  # in²
MIN_POLYGON_AREA = 0.5  # in² — reject invisible slivers
MAX_POLYGON_EXTENT = 8.0  # longest vertex-to-vertex span, inches

# Terrain TYPES for the draw-your-own-polygon tool: a type key -> the rule flags a
# hand-drawn piece of that type carries (kind/elevated/water/low_wall) + display.
POLYGON_TYPES: dict[str, dict] = {
    "blocking": {"kind": "blocking", "elevated": False, "water": None, "low_wall": False,
                 "label": "Blocking", "blurb": "Impassable — blocks movement and line of fire."},
    "hindering": {"kind": "hindering", "elevated": False, "water": None, "low_wall": False,
                  "label": "Hindering (woods)", "blurb": "Halve speed to cross; +1 defense to targets shot through it."},
    "shallow_water": {"kind": "clear", "elevated": False, "water": "shallow", "low_wall": False,
                      "label": "Shallow water", "blurb": "Halve speed to wade; no effect on shooting."},
    "deep_water": {"kind": "clear", "elevated": False, "water": "deep", "low_wall": False,
                   "label": "Deep water", "blurb": "Impassable to non-fliers; no effect on shooting."},
    "elevated": {"kind": "clear", "elevated": True, "water": None, "low_wall": False,
                 "label": "Elevated (hill)", "blurb": "Height advantage (+1 defense) and longer sightlines."},
    "low_wall": {"kind": "hindering", "elevated": False, "water": None, "low_wall": True,
                 "label": "Low wall", "blurb": "+1 defense to targets behind it; never slows a figure leaving it."},
}


def piece_from_polygon(type_key: str, polygon: tuple[Vec, ...], piece_id: int, owner: str) -> TerrainPiece | None:
    """Build a TerrainPiece of ``type_key`` from a hand-drawn world-space polygon."""
    spec = POLYGON_TYPES.get(type_key)
    if spec is None:
        return None
    return TerrainPiece(
        id=piece_id, kind=spec["kind"], polygon=tuple(polygon), elevated=spec["elevated"],
        water=spec["water"], low_wall=spec["low_wall"], abrupt=False, owner=owner,
    )


def template(key: str) -> TerrainTemplate | None:
    return _LIBRARY_BY_KEY.get(key)


def instantiate(
    tmpl: TerrainTemplate, center: Vec, rotation: float, piece_id: int, owner: str
) -> TerrainPiece:
    """Bake ``tmpl`` at ``center`` rotated by ``rotation`` into a concrete piece."""
    origin = Vec(0.0, 0.0)
    poly = tuple(v + center for v in rotate_polygon(tmpl.polygon, origin, rotation))
    aps = tuple(center + rotate_point(a, origin, rotation) for a in tmpl.access_points)
    return TerrainPiece(
        id=piece_id, kind=tmpl.kind, polygon=poly, elevated=tmpl.elevated,
        water=tmpl.water, low_wall=tmpl.low_wall, abrupt=tmpl.abrupt,
        access_points=aps, owner=owner,
    )


def placement_reason(
    poly: tuple[Vec, ...], existing: list[TerrainPiece],
    board_w: float, board_h: float,
    edge_margin: float = 1.0, start_band: float = 3.0, min_gap: float = 2.0,
) -> str | None:
    """Why a candidate polygon may NOT be placed, or None if it's legal.

    Rules (§Terrain setup, adapted): a piece must sit wholly on the board (with a
    small edge margin), clear of BOTH players' starting bands (the 3"-deep deploy
    zones), and at least ``min_gap`` inches from every already-placed piece. All
    library shapes are convex, so vertex containment is exact."""
    for v in poly:
        if not (edge_margin <= v.x <= board_w - edge_margin):
            return "off_board"
        if not (start_band <= v.y <= board_h - start_band):
            return "in_starting_area"
    for t in existing:
        if polygon_polygon_distance(poly, t.polygon) < min_gap - 1e-9:
            return "too_close"
    return None


def elevation_at(pieces: list[TerrainPiece], p: Vec) -> int:
    """0 = ground level, 1 = elevated (all elevated terrain is one height level)."""
    return 1 if any(t.elevated and t.contains(p) for t in pieces) else 0


def terrain_at(pieces: list[TerrainPiece], p: Vec) -> list[TerrainPiece]:
    return [t for t in pieces if t.contains(p)]


# --------------------------------------------------------------------------- #
# Movement queries (used by the engine's move validation)
# --------------------------------------------------------------------------- #
def blocking_between(pieces: list[TerrainPiece], p0: Vec, p1: Vec, radius: float) -> TerrainPiece | None:
    """A blocking / deep-water piece the moving base would enter en route; None if clear."""
    for t in pieces:
        if t.blocks_move() and swept_base_crosses_polygon(p0, p1, radius, t.polygon):
            return t
    return None


def base_in_blocking(pieces: list[TerrainPiece], p: Vec, radius: float) -> bool:
    """Would a base centred at p overlap blocking terrain / deep water (illegal end)?"""
    return any(t.blocks_move() and circle_intersects_polygon(p, radius, t.polygon) for t in pieces)


def _starts_in_speed_hindering(pieces: list[TerrainPiece], p: Vec, radius: float) -> bool:
    return any(t.halves_speed() and circle_intersects_polygon(p, radius, t.polygon) for t in pieces)


def hindering_entry_violation(
    pieces: list[TerrainPiece], p0: Vec, p1: Vec, radius: float
) -> TerrainPiece | None:
    """A hindering piece the move ENTERS without stopping in — §Hindering / P4-R30
    ("a figure starting on clear must stop when its base crosses into hindering").
    A move that starts clear of a hindering piece and whose swept base touches it
    must END touching that piece (for a low wall this is the "stop at the far
    side" rule — the wall is thin, so ending in contact with it is the far side).
    Returns the violated piece, or None if the move is legal."""
    for t in pieces:
        if not t.is_hindering_move():
            continue
        if circle_intersects_polygon(p0, radius, t.polygon):
            continue  # started touching it — the halved speed already applied
        if swept_base_crosses_polygon(p0, p1, radius, t.polygon) and not (
            circle_intersects_polygon(p1, radius, t.polygon)
        ):
            return t
    return None


def effective_speed(pieces: list[TerrainPiece], speed: int, start: Vec, radius: float) -> int:
    """Speed for the turn, halved (round up) if the figure begins its move touching
    speed-halving hindering (§Hindering; low walls are exempt)."""
    if _starts_in_speed_hindering(pieces, start, radius):
        return max(1, math.ceil(speed / 2))
    return speed


# --------------------------------------------------------------------------- #
# Line-of-fire terrain verdict (used by the engine's LoF + combat modifiers)
# --------------------------------------------------------------------------- #
def lof_terrain(
    pieces: list[TerrainPiece], p0: Vec, p1: Vec, elev_a: int, elev_t: int,
    stand_a: list[TerrainPiece], stand_t: list[TerrainPiece],
) -> tuple[bool, bool]:
    """(blocked, hindering) for a line of fire p0->p1 given firer/target elevations
    and the elevated pieces each stands on. Blocking terrain always blocks; an
    elevated feature blocks unless a shooter is elevated (the feature they stand on
    never blocks their own shot); hindering crossed adds +1 (capped, boolean)."""
    both_elev = elev_a == 1 and elev_t == 1
    stand_ids = {id(s) for s in stand_a} | {id(s) for s in stand_t}
    blocked = False
    hindering = False
    for t in pieces:
        if not segment_crosses_polygon(p0, p1, t.polygon):
            continue
        if t.blocks_lof_ground():  # blocking terrain (ground or elevated) always blocks
            blocked = True
            continue
        if t.elevated and t.kind in ("clear", "hindering"):
            if id(t) in stand_ids:  # you can always see out of the feature you stand on
                if t.kind == "hindering":
                    hindering = True
                continue
            if both_elev:  # both up high: elevated clear is seen over; elevated hindering still +1
                if t.kind == "hindering":
                    hindering = True
            else:  # a shot involving the ground crosses an elevated feature it isn't standing on
                blocked = True
            continue
        if t.hinders_lof():  # ground hindering / low wall
            hindering = True
    return blocked, hindering
