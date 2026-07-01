"""In-play game state (§6 Core Domain Model).

The engine is the single source of truth (DP1). ``Figure`` is a mutable in-play
instance that references an immutable ``FigureDef`` for its dial. Dial/click
tracking (damage, healing, elimination) lives here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .data import ClickStats, FigureDef
from .geometry import Vec, in_base_contact

# Standard single base radius in inches. Mage Knight bugs use a ~1.1" diameter
# standard base; no mounted "peanut" bases appear in the Rebellion seed roster
# (D5(d) / OQ-6), so a single radius suffices for v1.
STANDARD_BASE_RADIUS = 0.55

# Demoralized is encoded as a dial ability on a figure's final click (§Demoralized).
DEMORALIZED_ABILITY_ID = 95


@dataclass
class Figure:
    uid: int
    definition: FigureDef
    owner: str  # "human" | "llm"
    position: Vec
    facing: float  # radians
    base_radius: float = STANDARD_BASE_RADIUS
    current_click: int = 0  # index into definition.dial (starts at starting_click)

    # Pushing / action-token tracking (P4-R4).
    action_tokens: int = 0
    acted_nonpass_this_turn: bool = False

    # Status flags.
    eliminated: bool = False
    captured: bool = False
    demoralized: bool = False

    # Optional abilities the controller has cancelled until end of turn (P4-R34).
    disabled_ability_ids: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.current_click == 0:
            self.current_click = self.definition.starting_click

    # -- identity / display ------------------------------------------------
    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def short_name(self) -> str:
        return self.definition.short_name

    @property
    def points(self) -> int:
        return self.definition.points

    # -- dial / clicks -----------------------------------------------------
    @property
    def is_alive(self) -> bool:
        return not self.eliminated

    def _click_stats(self) -> ClickStats:
        return self.definition.dial[self.current_click]

    @property
    def speed(self) -> int:
        return self._click_stats().speed

    @property
    def attack(self) -> int:
        return self._click_stats().attack

    @property
    def defense(self) -> int:
        return self._click_stats().defense

    @property
    def damage(self) -> int:
        return self._click_stats().damage

    @property
    def range(self) -> int:
        return self.definition.range

    @property
    def targets(self) -> int:
        return self.definition.targets

    @property
    def arc_half_angle(self) -> float:
        return self.definition.arc_half_angle

    @property
    def is_ranged(self) -> bool:
        return self.definition.range > 0

    @property
    def is_demoralized(self) -> bool:
        """A figure on its Demoralized click may only move or pass, may not
        voluntarily enter base contact, and doesn't count as a fighting figure
        for the victory condition (§Demoralized / P4-R36)."""
        return self.demoralized or (DEMORALIZED_ABILITY_ID in self.active_ability_ids())

    def active_ability_ids(self) -> set[int]:
        """Ability ids in effect at the current click (optional-cancelled removed)."""
        ids = set(self._click_stats().ability_ids())
        return ids - self.disabled_ability_ids

    def health_fraction(self) -> float:
        """Fraction of the dial still remaining (1.0 at start, 0.0 when KO'd)."""
        if self.eliminated:
            return 0.0
        total = self.definition.num_live_clicks
        start = self.definition.starting_click
        remaining = total - self.current_click
        span = total - start
        return remaining / span if span > 0 else 1.0

    def take_clicks(self, n: int) -> int:
        """Apply ``n`` clicks of damage (clockwise). Returns clicks actually
        applied. Eliminates the figure at the end of the dial (3 skulls)."""
        if n <= 0 or self.eliminated:
            return 0
        last = self.definition.num_live_clicks - 1
        applied = 0
        for _ in range(n):
            if self.current_click >= last:
                self.current_click = last
                self.eliminated = True
                applied += 1  # the killing click
                break
            self.current_click += 1
            applied += 1
        return applied

    def heal_clicks(self, n: int) -> int:
        """Heal ``n`` clicks (counter-clockwise), never past Starting Position
        (§Healing). Cannot revive an eliminated figure via normal healing."""
        if n <= 0 or self.eliminated:
            return 0
        start = self.definition.starting_click
        before = self.current_click
        self.current_click = max(start, self.current_click - n)
        return before - self.current_click

    # -- turn bookkeeping --------------------------------------------------
    def begin_owner_turn(self) -> None:
        self.acted_nonpass_this_turn = False
        self.disabled_ability_ids.clear()

    def end_owner_turn(self) -> None:
        # A figure not given a non-pass action this turn rests and clears tokens.
        if not self.acted_nonpass_this_turn:
            self.action_tokens = 0
        self.acted_nonpass_this_turn = False


@dataclass
class Board:
    width: float = 36.0
    height: float = 36.0

    def contains(self, p: Vec, radius: float = 0.0) -> bool:
        return (
            radius <= p.x <= self.width - radius
            and radius <= p.y <= self.height - radius
        )


@dataclass
class GameState:
    board: Board
    figures: dict[int, Figure] = field(default_factory=dict)
    build_total: int = 200
    turn_number: int = 1  # increments each half-turn (one player's turn)
    active_player: str = "human"
    first_player: str = "human"
    winner: str | None = None
    ended: bool = False
    terrain: list = field(default_factory=list)  # list[TerrainPiece] (Phase 3)
    # Setup: "terrain" while players alternate placing terrain, then "battle".
    # Defaults to "battle" so games/tests without a placement step are unchanged.
    phase: str = "battle"
    terrain_budget: dict = field(default_factory=dict)  # pieces left to place per owner
    terrain_turn: str = "human"  # whose turn it is to place a piece

    def actions_per_turn(self) -> int:
        """Actions per turn = build_total / 100, fixed for the game (P1-R2)."""
        return max(1, self.build_total // 100)

    # -- queries -----------------------------------------------------------
    def figure(self, uid: int) -> Figure:
        return self.figures[uid]

    def living(self, owner: str | None = None) -> list[Figure]:
        out = []
        for f in self.figures.values():
            if not f.is_alive:
                continue
            if owner is not None and f.owner != owner:
                continue
            out.append(f)
        return out

    def opponents_of(self, figure: Figure) -> list[Figure]:
        return [f for f in self.living() if f.owner != figure.owner]

    def friends_of(self, figure: Figure, include_self: bool = False) -> list[Figure]:
        return [
            f
            for f in self.living()
            if f.owner == figure.owner and (include_self or f.uid != figure.uid)
        ]

    def in_base_contact_with(self, figure: Figure, others: list[Figure]) -> list[Figure]:
        out = []
        for o in others:
            if o.uid == figure.uid:
                continue
            if in_base_contact(
                figure.position, figure.base_radius, o.position, o.base_radius
            ):
                out.append(o)
        return out

    def opposing_contacts(self, figure: Figure) -> list[Figure]:
        return self.in_base_contact_with(figure, self.opponents_of(figure))

    def other_player(self, player: str | None = None) -> str:
        p = player or self.active_player
        # Two-sided game: the two owners present are "human" and "llm".
        owners = {f.owner for f in self.figures.values()}
        owners.add("human")
        owners.add("llm")
        for o in owners:
            if o != p:
                return o
        return "llm" if p == "human" else "human"
