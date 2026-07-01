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
