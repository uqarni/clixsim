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

from dataclasses import dataclass, field

from .geometry import Vec, point_in_polygon


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
