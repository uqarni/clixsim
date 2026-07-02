"""The headless rules engine (A1/A2) — the single source of truth.

Exposes a pure-ish API: ``apply(intent) -> Result | Rejection`` plus read-only
query functions the renderer and AI consume. Runs with no renderer for tests and
LLM-vs-LLM self-play. All randomness flows through one seeded roller (X1).

Scope for v1 (M1 vertical slice): single-figure Move / Ranged / Close / Pass,
no terrain, win by elimination. Formations, terrain, capture and free-spin are
out of scope and flagged, not silently mis-handled.
"""

from __future__ import annotations

import math

from . import abilities as ab
from . import terrain as terr
from .data import FigureDB, load_db
from .gamelog import GameLog
from .geometry import (
    CONTACT_TOLERANCE,
    Vec,
    angle_to,
    distance,
    in_base_contact,
    in_front_arc,
    in_rear_arc,
    polygon_area,
    polygon_extent,
    polygon_is_simple,
    segment_circle_intersects,
)
from .intents import (
    CloseIntent,
    FreeSpinIntent,
    Intent,
    LevitateIntent,
    MoveIntent,
    NecromancyIntent,
    PassIntent,
    RangedIntent,
    RegenerateIntent,
    Rejection,
    Result,
    ToggleAbilityIntent,
)
from .probability import crit_hit_probability, hit_probability, outcome
from .rng import DiceRoller
from .state import Board, Figure, GameState

# Ability effect coverage lives in clixengine.abilities (X6 telemetry).
IMPLEMENTED_ABILITY_IDS = ab.IMPLEMENTED_ABILITY_IDS

# Mage Spawn are faction-less monsters: they may not be part of any formation
# except with a Shyft figure (§Restrictions / P4-R16). No Shyft exists in the
# Rebellion roster, so in v1 Mage Spawn simply cannot form formations.
MAGE_SPAWN_FACTION = "Mage Spawn"


class Engine:
    def __init__(self, state: GameState, db: FigureDB | None = None, seed: int = 0):
        self.state = state
        self.db = db or load_db()
        self.rng = DiceRoller(seed)
        self.log = GameLog(seed)
        # Figures that have acted this turn (each warrior gets <=1 action).
        self._acted_uids: set[int] = set()
        # Action budget bookkeeping, separate from "who acted": Quickness moves
        # mark a figure acted without spending a budget action; Command can add
        # bonus actions to the budget.
        self._actions_spent: int = 0
        self._bonus_actions: int = 0
        self._command_rolled: bool = False
        # Figures currently entitled to a free spin (P4-R9): populated when a mover
        # enters base contact, consumed by the spin, and expired when the mover acts
        # again or a new turn begins — so a spin can't be redeemed out of context.
        self._pending_free_spins: set[int] = set()

    # ------------------------------------------------------------------ #
    # Ability telemetry (X6)
    # ------------------------------------------------------------------ #
    def ability_coverage(self) -> dict:
        used: set[int] = set()
        for f in self.state.figures.values():
            used |= f.definition.all_ability_ids()
        implemented = sorted(used & IMPLEMENTED_ABILITY_IDS)
        terrain_pending = sorted(used & ab.TERRAIN_DEPENDENT_IDS)
        capture_pending = sorted(used & ab.CAPTURE_PENDING_IDS)
        unimplemented = sorted(
            used - IMPLEMENTED_ABILITY_IDS - ab.TERRAIN_DEPENDENT_IDS - ab.CAPTURE_PENDING_IDS
        )

        def names(ids):
            return [
                {"id": i, "name": (self.db.ability(i).name if self.db.ability(i) else str(i))}
                for i in ids
            ]

        return {
            "implemented": names(implemented),
            "terrain_pending": names(terrain_pending),
            "capture_pending": names(capture_pending),
            "unimplemented": names(unimplemented),
        }

    # ------------------------------------------------------------------ #
    # Queries (DP2) — read-only, no state mutation
    # ------------------------------------------------------------------ #
    def distance_between(self, a_uid: int, b_uid: int) -> float:
        a, b = self.state.figure(a_uid), self.state.figure(b_uid)
        return distance(a.position, b.position)

    def in_front_arc_of(self, viewer_uid: int, target_uid: int) -> bool:
        v, t = self.state.figure(viewer_uid), self.state.figure(target_uid)
        return in_front_arc(v.position, v.facing, t.position, v.arc_half_angle)

    def _elev(self, p: Vec) -> int:
        return terr.elevation_at(self.state.terrain, p) if self.state.terrain else 0

    def _stand_on(self, p: Vec) -> list:
        return [t for t in self.state.terrain if t.elevated and t.contains(p)]

    def line_of_fire(self, attacker_uid: int, target_uid: int) -> tuple[bool, str]:
        """Straight centre->centre LoF (P4-R24) incl. terrain/elevation blocking.
        Returns (clear, reason)."""
        a = self.state.figure(attacker_uid)
        t = self.state.figure(target_uid)
        if not in_front_arc(a.position, a.facing, t.position, a.arc_half_angle):
            return False, "target not in firer's front arc"
        if distance(a.position, t.position) > a.range + 1e-9:
            return False, "target beyond range"
        elev_a, elev_t = self._elev(a.position), self._elev(t.position)
        # Blocking terrain / elevation blocks the line of fire.
        if self.state.terrain:
            blocked, _ = terr.lof_terrain(
                self.state.terrain, a.position, t.position, elev_a, elev_t,
                self._stand_on(a.position), self._stand_on(t.position))
            if blocked:
                return False, "line of fire blocked by terrain"
        # Blocked if it crosses an intervening base — but a fully-elevated shot
        # ignores non-elevated bases (§Elevated Terrain).
        both_elev = elev_a == 1 and elev_t == 1
        for other in self.state.living():
            if other.uid in (attacker_uid, target_uid):
                continue
            if both_elev and self._elev(other.position) == 0:
                continue
            if segment_circle_intersects(
                a.position, t.position, other.position, other.base_radius
            ):
                return False, f"line of fire blocked by {other.short_name}"
        for friend in self.state.friends_of(a):
            if in_base_contact(
                t.position, t.base_radius, friend.position, friend.base_radius
            ):
                return False, "target is in base contact with a friendly figure"
        return True, "clear"

    def terrain_defense_mod(self, a: Figure, t: Figure, attack_type: str) -> int:
        """Terrain contribution to the target's defense: +1 for a line of fire
        crossing hindering (ranged only), +1 height advantage when the attacker is
        on the ground and the target is elevated (ranged and close)."""
        if not self.state.terrain:
            return 0
        elev_a, elev_t = self._elev(a.position), self._elev(t.position)
        mod = 0
        if attack_type == "ranged":
            _, hindering = terr.lof_terrain(
                self.state.terrain, a.position, t.position, elev_a, elev_t,
                self._stand_on(a.position), self._stand_on(t.position))
            if hindering:
                mod += 1
        if elev_t == 1 and elev_a == 0:  # height advantage
            mod += 1
        return mod

    # ------------------------------------------------------------------ #
    # Terrain placement (setup phase) — players alternate placing pieces
    # ------------------------------------------------------------------ #
    def _where_label(self, c: Vec) -> str:
        """Human/AI-readable rough position for a candidate placement."""
        w, h = self.state.board.width, self.state.board.height
        col = "left" if c.x < w / 3 else "right" if c.x > 2 * w / 3 else "center"
        if c.y < h / 3:
            row = "near your side"
        elif c.y > 2 * h / 3:
            row = "near the enemy side"
        else:
            row = "midfield"
        return f"{row}, {col}" if col != "center" else row

    def terrain_placement_candidates(self, owner: str, max_n: int = 6) -> list[dict]:
        """Propose a spread of LEGAL placements (one per library shape where it
        fits) for an AI placer to choose among — the engine owns geometry, so the
        placer only ever selects a pre-validated option. Scans a fine grid so a
        spot is found whenever one exists (avoids a placer stalling on a full-ish
        board)."""
        w, h = self.state.board.width, self.state.board.height
        band = 3.0
        step = 2.0
        xs = [3.0 + step * i for i in range(int((w - 6.0) / step) + 1)]
        ys = [band + 1.0 + step * i for i in range(int((h - 2 * band - 2.0) / step) + 1)]
        rots = [0.0, math.pi / 4, math.pi / 2]
        placed = len(self.state.terrain)
        out: list[dict] = []
        for i, tmpl in enumerate(terr.TERRAIN_LIBRARY):
            combos = [(x, y, r) for y in ys for x in xs for r in rots]
            off = (i * 7 + placed * 13) % len(combos)
            combos = combos[off:] + combos[:off]  # rotate the scan start for variety
            for x, y, r in combos:
                center = Vec(x, y)
                poly = tuple(
                    v + center for v in terr.rotate_polygon(tmpl.polygon, Vec(0.0, 0.0), r)
                )
                if terr.placement_reason(poly, self.state.terrain, w, h) is None:
                    out.append({
                        "key": tmpl.key, "label": tmpl.label, "kind": tmpl.kind,
                        "blurb": tmpl.blurb, "center": [round(x, 2), round(y, 2)],
                        "rotation": round(r, 4), "where": self._where_label(center),
                    })
                    break
            if len(out) >= max_n:
                break
        return out

    def skip_terrain_placement(self, owner: str) -> Result | Rejection:
        """Forfeit ``owner``'s remaining terrain (they're done, or no legal spot
        is left) and hand off — starting the battle if both sides are finished."""
        if self.state.phase != "terrain":
            return Rejection("not_placing", "terrain is not being placed right now")
        if owner != self.state.terrain_turn:
            return Rejection("not_your_turn", f"it is {self.state.terrain_turn}'s turn to place")
        left = self.state.terrain_budget.get(owner, 0)
        self.state.terrain_budget[owner] = 0
        ev = self.log.emit("terrain_forfeit", owner=owner, forfeited=left)
        self._advance_terrain_turn(owner)
        return Result("skip_terrain", [ev], f"{owner} is done placing terrain")

    def _advance_terrain_turn(self, just_placed: str) -> None:
        """After ``just_placed`` places a piece, hand off (alternate; a player with
        no budget left is skipped). When both are done, start the battle."""
        budget = self.state.terrain_budget
        other = self.state.other_player(just_placed)
        if budget.get(other, 0) > 0:
            self.state.terrain_turn = other
        elif budget.get(just_placed, 0) > 0:
            self.state.terrain_turn = just_placed
        else:
            self._begin_battle_after_terrain()

    def _begin_battle_after_terrain(self) -> None:
        """Terrain placement complete: enter figure deployment if requested, else
        start the first battle turn."""
        self.log.emit("terrain_done", pieces=len(self.state.terrain))
        if getattr(self.state, "pending_deploy", False):
            self._begin_deploy()
            return
        self._begin_first_turn()

    def _begin_deploy(self) -> None:
        """Setup: let the human arrange their figures within the starting area (P3-R5)
        before the battle begins."""
        self.state.phase = "deploy"
        self.state.pending_deploy = False
        self.log.emit("deploy_setup", first=self.state.first_player)

    def _begin_first_turn(self) -> None:
        self.state.phase = "battle"
        self._begin_player_turn(self.state.first_player)
        self.log.emit("begin_turn", player=self.state.first_player, turn=self.state.turn_number)

    # ------------------------------------------------------------------ #
    # Figure deployment (setup phase) — arrange your army in your starting area
    # ------------------------------------------------------------------ #
    def _deploy_band(self, owner: str) -> tuple[float, float]:
        """The owner's 3"-deep starting band (low_y, high_y) — P3-R5."""
        h = self.state.board.height
        return (0.0, 3.0) if owner == "human" else (h - 3.0, h)

    def deploy_figure(
        self, owner: str, uid: int, pos: tuple[float, float], facing: float,
    ) -> Result | Rejection:
        """Reposition one of ``owner``'s figures anywhere within its starting area
        during setup — free, any number of times (P3-R5)."""
        if self.state.phase != "deploy":
            return Rejection("not_deploying", "deployment is not open right now")
        f = self.state.figures.get(uid)
        if f is None or not f.is_alive or f.owner != owner:
            return Rejection("bad_figure", "not your figure")
        p = Vec(*pos)
        r = f.base_radius
        b = self.state.board
        lo, hi = self._deploy_band(owner)
        if not (r <= p.x <= b.width - r):
            return Rejection("off_board", "figure would leave the board")
        if not (lo + r <= p.y <= hi - r):
            return Rejection("out_of_area", 'figures deploy within your 3" starting area')
        for o in self.state.living():
            if o.uid == uid:
                continue
            if distance(p, o.position) < r + o.base_radius - CONTACT_TOLERANCE:
                return Rejection("overlap", f"would overlap {o.short_name}")
        if self.state.terrain and terr.base_in_blocking(self.state.terrain, p, r):
            return Rejection("in_blocking", "cannot deploy in blocking terrain")
        f.position = p
        f.facing = float(facing)
        ev = self.log.emit("deploy", figure=uid, to=[round(p.x, 2), round(p.y, 2)], facing=f.facing)
        return Result("deploy", [ev], f"{f.short_name} deploys")

    def finish_deploy(self, owner: str) -> Result | Rejection:
        """The human is done arranging — start the first battle turn."""
        if self.state.phase != "deploy":
            return Rejection("not_deploying", "deployment is not open right now")
        self.log.emit("deploy_done", by=owner)
        self._begin_first_turn()
        return Result("finish_deploy", [], "battle begins")

    def place_terrain(
        self, owner: str, key: str, center: tuple[float, float], rotation: float = 0.0,
    ) -> Result | Rejection:
        """Place one terrain piece for ``owner`` during the setup phase."""
        if self.state.phase != "terrain":
            return Rejection("not_placing", "terrain is not being placed right now")
        if owner != self.state.terrain_turn:
            return Rejection("not_your_turn", f"it is {self.state.terrain_turn}'s turn to place")
        if self.state.terrain_budget.get(owner, 0) <= 0:
            return Rejection("no_budget", f"{owner} has no terrain left to place")
        tmpl = terr.template(key)
        if tmpl is None:
            return Rejection("no_such_terrain", str(key))
        c = Vec(*center)
        piece = terr.instantiate(tmpl, c, float(rotation), len(self.state.terrain), owner)
        reason = terr.placement_reason(
            piece.polygon, self.state.terrain, self.state.board.width, self.state.board.height
        )
        if reason is not None:
            return Rejection(reason, f"cannot place {tmpl.label} there")
        self.state.terrain.append(piece)
        self.state.terrain_budget[owner] = self.state.terrain_budget.get(owner, 0) - 1
        ev = self.log.emit("place_terrain", owner=owner, kind=key, id=piece.id,
                           center=[round(c.x, 2), round(c.y, 2)], rotation=round(float(rotation), 4))
        self._advance_terrain_turn(owner)
        return Result("place_terrain", [ev], f"{owner} places {tmpl.label} ({self._where_label(c)})")

    def place_terrain_polygon(
        self, owner: str, type_key: str, polygon: list[tuple[float, float]],
    ) -> Result | Rejection:
        """Place one hand-drawn terrain polygon of ``type_key`` for ``owner``."""
        if self.state.phase != "terrain":
            return Rejection("not_placing", "terrain is not being placed right now")
        if owner != self.state.terrain_turn:
            return Rejection("not_your_turn", f"it is {self.state.terrain_turn}'s turn to place")
        if self.state.terrain_budget.get(owner, 0) <= 0:
            return Rejection("no_budget", f"{owner} has no terrain left to place")
        if type_key not in terr.POLYGON_TYPES:
            return Rejection("no_such_terrain", str(type_key))
        if len(polygon) < 3:
            return Rejection("bad_polygon", "a terrain polygon needs at least 3 points")
        poly = tuple(Vec(float(x), float(y)) for x, y in polygon)
        if not polygon_is_simple(poly):
            return Rejection("self_intersecting", "the shape crosses itself — draw a simple outline")
        area = polygon_area(poly)
        span = polygon_extent(poly)
        if area > terr.MAX_POLYGON_AREA + 1e-9:
            return Rejection("too_big", f"that's {area:.0f} in² — terrain pieces max out at "
                                        f"{terr.MAX_POLYGON_AREA:.0f} in²")
        if span > terr.MAX_POLYGON_EXTENT + 1e-9:
            return Rejection("too_big", f"that spans {span:.1f}\" — terrain pieces max out at "
                                        f"{terr.MAX_POLYGON_EXTENT:.0f}\" across")
        if area < terr.MIN_POLYGON_AREA:
            return Rejection("too_small", "that shape is too thin to be a real terrain piece")
        piece = terr.piece_from_polygon(type_key, poly, len(self.state.terrain), owner)
        reason = terr.placement_reason(
            piece.polygon, self.state.terrain, self.state.board.width, self.state.board.height
        )
        if reason is not None:
            return Rejection(reason, f"cannot place that shape there ({reason})")
        self.state.terrain.append(piece)
        self.state.terrain_budget[owner] = self.state.terrain_budget.get(owner, 0) - 1
        label = terr.POLYGON_TYPES[type_key]["label"]
        ev = self.log.emit("place_terrain", owner=owner, kind=type_key, id=piece.id, custom=True)
        self._advance_terrain_turn(owner)
        return Result("place_terrain", [ev], f"{owner} places {label}")

    def hit_odds(
        self,
        attacker_uid: int,
        target_uid: int,
        rear_bonus: bool = False,
        attack_type: str = "close",
    ) -> float:
        a = self.state.figure(attacker_uid)
        t = self.state.figure(target_uid)
        atk = a.attack + (1 if rear_bonus else 0)
        tmod = self.terrain_defense_mod(a, t, attack_type)
        return hit_probability(atk, ab.effective_defense(self.state, t, attack_type, tmod))

    def expected_damage(
        self,
        attacker_uid: int,
        target_uid: int,
        rear_bonus: bool = False,
        dmg: int | None = None,
        attack_type: str = "close",
    ) -> float:
        a = self.state.figure(attacker_uid)
        t = self.state.figure(target_uid)
        atk = a.attack + (1 if rear_bonus else 0)
        base_dmg = a.damage if dmg is None else dmg
        if attack_type == "ranged":
            base_dmg += ab.ranged_damage_bonus(self.state, a, t)  # Magic Enhancement +1
        eff_def = ab.effective_defense(
            self.state, t, attack_type, self.terrain_defense_mod(a, t, attack_type))
        # Fold damage-reducing abilities (Toughness, Magic Immunity) into each term:
        # a normal hit delivers ``base_dmg``, a natural 12 delivers ``base_dmg + 1``.
        p_crit = crit_hit_probability()
        p_normal = hit_probability(atk, eff_def) - p_crit
        normal_dmg = ab.damage_after_defenses(t, base_dmg, attack_type, is_magic=False)
        crit_dmg = ab.damage_after_defenses(t, base_dmg + 1, attack_type, is_magic=False)
        return p_normal * normal_dmg + p_crit * crit_dmg

    def validate_move(
        self, figure_uid: int, dest: tuple[float, float], facing: float | None = None,
        free: bool = False,
    ) -> dict:
        """Read-only dry-run for an arbitrary move destination (renderer support).

        Mirrors ``_apply_move``'s legality WITHOUT mutating state or rolling dice,
        so a client can show live green/red for any dragged endpoint. Returns
        ``{ok, reason?, detail?, break_away?}``; break_away carries the odds only
        (the actual roll happens inside apply())."""
        f = self._precheck(figure_uid, free=free)
        if isinstance(f, Rejection):
            return {"ok": False, "reason": f.reason, "detail": f.detail}
        if free and not ab.has(f, ab.QUICKNESS):
            return {"ok": False, "reason": "no_quickness",
                    "detail": f"{f.short_name} lacks Quickness for a free move"}
        if f.action_tokens >= 2:
            return {"ok": False, "reason": "pushed_out",
                    "detail": f"{f.short_name} cannot act a third consecutive turn"}
        d = Vec(*dest)
        rej = self._validate_move(f, d)
        if rej is not None:
            return {"ok": False, "reason": rej.reason, "detail": rej.detail}
        # Legal. Report whether the move triggers a break-away roll, and its odds.
        moving = distance(f.position, d) > 1e-9
        contacts = self.state.opposing_contacts(f)
        result: dict = {"ok": True}
        if moving and contacts:
            need = ab.break_away_min(f)
            result["break_away"] = {"needed": True, "odds": round((7 - need) / 6.0, 3)}
        else:
            result["break_away"] = {"needed": False, "odds": 1.0}
        return result

    # ------------------------------------------------------------------ #
    # Legal-action generation
    # ------------------------------------------------------------------ #
    def can_act(self, figure: Figure) -> bool:
        return (
            figure.is_alive
            and figure.owner == self.state.active_player
            and figure.uid not in self._acted_uids
            and figure.action_tokens < 2  # cannot act 3 consecutive turns
        )

    def actionable_figures(self) -> list[Figure]:
        can = [f for f in self.state.living(self.state.active_player) if self.can_act(f)]
        if self._actions_remaining() > 0:
            return can
        # No budget left, but Quickness figures may still take a free move.
        return [f for f in can if ab.has(f, ab.QUICKNESS)]

    def _actions_remaining(self) -> int:
        return self.state.actions_per_turn() + self._bonus_actions - self._actions_spent

    def legal_close_targets(self, figure: Figure) -> list[tuple[Figure, bool]]:
        """Targets in the attacker's front arc & base contact. bool = rear hit."""
        out = []
        for t in self.state.opponents_of(figure):
            if not in_base_contact(
                figure.position, figure.base_radius, t.position, t.base_radius
            ):
                continue
            if not in_front_arc(
                figure.position, figure.facing, t.position, figure.arc_half_angle
            ):
                continue
            rear = in_rear_arc(t.position, t.facing, figure.position, t.arc_half_angle)
            out.append((t, rear))
        return out

    def legal_ranged_targets(self, figure: Figure) -> list[Figure]:
        """Single-target ranged options with a clear LoF (multi-target handled at
        apply time)."""
        if figure.range <= 0 or not ab.can_make_ranged_attack(figure):
            return []  # no range, or Berserk (§Berserk)
        if self.state.opposing_contacts(figure):
            return []  # in base contact => cannot fire (P4-R23)
        out = []
        for t in self.state.opponents_of(figure):
            clear, _ = self.line_of_fire(figure.uid, t.uid)
            if clear:
                out.append(t)
        return out

    def _same_faction_cluster(self, figure: Figure) -> list[Figure]:
        """The base-contact-connected group of same-faction friendlies (incl.
        ``figure``) — used to explain why a group isn't a legal formation."""
        same = {f.uid: f for f in self.state.living(figure.owner)
                if f.definition.faction == figure.definition.faction}
        if figure.uid not in same:
            return []
        seen, stack = set(), [figure.uid]
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            fu = same[u]
            for v, fv in same.items():
                if v not in seen and in_base_contact(
                    fu.position, fu.base_radius, fv.position, fv.base_radius
                ):
                    stack.append(v)
        return [same[u] for u in seen]

    def _same_faction_cluster_size(self, figure: Figure) -> int:
        return len(self._same_faction_cluster(figure))

    def figure_action_hints(self, figure: Figure) -> list[str]:
        """Human-readable reasons an *expected* action isn't offered, so the UI can
        explain "why can't I attack/heal/form up here" instead of silently showing
        nothing. Faithful to the rules — these describe correct restrictions, they
        do not relax them. Returns at most a few of the most relevant hints."""
        if figure.owner != self.state.active_player or not figure.is_alive:
            return []
        # No budget left => generate_candidates offers nothing, so don't advise an
        # attack/heal/re-face the figure can't actually make this turn.
        if self._actions_remaining() <= 0:
            return []
        aids = figure.active_ability_ids()
        hints: list[str] = []
        is_ranged = figure.range > 0 and ab.can_make_ranged_attack(figure)
        in_contact = bool(self.state.opposing_contacts(figure))
        close_ok = {t.uid for t, _ in self.legal_close_targets(figure)}
        ranged_ok = {t.uid for t in self.legal_ranged_targets(figure)}
        friends = self.state.friends_of(figure)

        # A demoralized figure may only move or pass (P4-R36) — attack/heal hints
        # would steer the player toward actions it fundamentally cannot take.
        if figure.is_demoralized:
            return ["Demoralized: this figure may only move or pass this turn (P4-R36)."]

        # --- attacks: explain a near-but-unattackable enemy -------------------
        for e in sorted(self.state.opponents_of(figure),
                        key=lambda o: distance(figure.position, o.position)):
            if e.uid in close_ok or e.uid in ranged_ok:
                continue
            adjacent = in_base_contact(figure.position, figure.base_radius,
                                       e.position, e.base_radius)
            in_arc = in_front_arc(figure.position, figure.facing, e.position,
                                  figure.arc_half_angle)
            if adjacent:
                if not in_arc:
                    hints.append(f"Can't close-attack {e.short_name}: it's beside/behind "
                                 f"you — a close attack needs it in your front arc (P4-R27). "
                                 f"Re-face with a move (uses the action), or spin free if it "
                                 f"just moved into you.")
            elif is_ranged and distance(figure.position, e.position) <= figure.range + 1e-9:
                if in_contact:
                    hints.append(f"Can't shoot {e.short_name}: you're in base contact, so no "
                                 f"ranged attack (P4-R23) — close-attack instead or break away.")
                elif not in_arc:
                    hints.append(f"Can't shoot {e.short_name}: it's not in your front arc "
                                 f"(P4-R24). Re-face toward it with a move.")
                elif any(in_base_contact(e.position, e.base_radius, fr.position, fr.base_radius)
                         for fr in friends):
                    hints.append(f"Can't shoot {e.short_name}: it's in base contact with one "
                                 f"of your own figures (P4-R25) — you'd risk your ally.")
                else:
                    clear, reason = self.line_of_fire(figure.uid, e.uid)
                    if not clear:
                        hints.append(f"Can't shoot {e.short_name}: {reason} (P4-R24).")
            if len(hints) >= 3:
                break

        # --- healing (Healing / Magic Healing) -------------------------------
        heal_touch = ab.HEALING in aids
        heal_ranged = ab.MAGIC_HEALING in aids
        if (heal_touch or heal_ranged) and in_contact:
            # §Healing / §Magic Healing: the healer may not be in base contact
            # with an opposing figure — the most common invisible blocker.
            hints.append("Can't heal while in base contact with an enemy — "
                         "break away first (§Healing).")
        elif heal_touch or heal_ranged:
            for fr in friends:
                wounded = fr.current_click > fr.definition.starting_click and fr.is_alive
                if not wounded:
                    continue
                if self.state.opposing_contacts(fr):
                    hints.append(f"Can't heal {fr.short_name}: it's in base contact with an "
                                 f"enemy (§Healing) — it must break away first.")
                    break
                near = distance(figure.position, fr.position)
                if heal_ranged:
                    if near > figure.range + 1e-9:
                        continue
                    if not in_front_arc(figure.position, figure.facing, fr.position,
                                        figure.arc_half_angle):
                        hints.append(f"Can't heal {fr.short_name}: your Magic Healing is a "
                                     f"ranged action — the ally must be in your front arc. "
                                     f"Re-face toward it.")
                        break
                elif heal_touch:
                    gap = near - (figure.base_radius + fr.base_radius)
                    if gap > 1e-6:
                        hints.append(f"Can't heal {fr.short_name}: Healing needs base contact "
                                     f"— you're {gap:.1f}″ short of touching. Drag close and "
                                     f"let the snap grab it.")
                        break

        # --- formation short of the minimum / blocked by an ability ----------
        if not figure.is_demoralized:
            cluster = self._same_faction_cluster(figure)
            barred = [g for g in cluster
                      if g.active_ability_ids() & (ab.FREE_MOVEMENT_IDS | {ab.QUICKNESS})]
            eligible = len(cluster) - len(barred)
            if barred and len(cluster) >= 2:
                names = ", ".join(sorted(g.short_name for g in barred))
                hints.append(
                    f"Movement formations exclude Flight/Aquatic/Quickness (card text): "
                    f"{names}. Those are optional abilities — cancel them (dial panel) "
                    f"and the group can form up."
                )
            elif eligible == 2:
                hints.append("Form up: a movement formation needs 3–5 same-faction figures "
                             "in base contact — you have 2, so bring one more alongside.")
        return hints[:4]

    # ------------------------------------------------------------------ #
    # apply(intent)
    # ------------------------------------------------------------------ #
    def apply(self, intent: Intent) -> Result | Rejection:
        if self.state.ended:
            return Rejection("game_over", "the game has already ended")
        # A new resolving action expires any un-redeemed free-spin offers (P4-R9 is
        # a reaction to a *just-made* contact); free-spin and (non-action) ability
        # toggles don't expire it.
        if not isinstance(intent, (FreeSpinIntent, ToggleAbilityIntent)):
            self._pending_free_spins.clear()
        if isinstance(intent, PassIntent):
            return self._apply_pass(intent)
        if isinstance(intent, MoveIntent):
            return self._apply_move(intent)
        if isinstance(intent, RangedIntent):
            return self._apply_ranged(intent)
        if isinstance(intent, CloseIntent):
            return self._apply_close(intent)
        if isinstance(intent, RegenerateIntent):
            return self._apply_regenerate(intent)
        if isinstance(intent, NecromancyIntent):
            return self._apply_necromancy(intent)
        if isinstance(intent, LevitateIntent):
            return self._apply_levitate(intent)
        if isinstance(intent, FreeSpinIntent):
            return self._apply_free_spin(intent)
        if isinstance(intent, ToggleAbilityIntent):
            return self._apply_toggle_ability(intent)
        return Rejection("unknown_intent", type(intent).__name__)

    def _newly_contacted_opponents(self, mover: Figure, before_uids: set[int]) -> list[Figure]:
        """Opposing figures the ``mover`` has just entered base contact with (were
        not in contact before the move). These defenders are entitled to a free
        spin (P4-R9) — unless the mover is mounted, which grants none."""
        if ab.is_mounted(mover):
            return []
        out = []
        for o in self.state.opposing_contacts(mover):
            if o.uid not in before_uids and not ab.is_mounted(o):
                out.append(o)
        return out

    def _apply_free_spin(self, intent: FreeSpinIntent) -> Result | Rejection:
        """Free spin (P4-R9): re-face a contacted defender at no cost. Never spends
        an action, places a token, marks the figure acted, or triggers pushing —
        and only the NON-active player's figure (the one just contacted) may do it."""
        f = self.state.figures.get(intent.figure_uid)
        if f is None or not f.is_alive:
            return Rejection("no_such_figure", str(intent.figure_uid))
        if f.owner == self.state.active_player:
            return Rejection("not_defender", "only a contacted defender may free-spin")
        if ab.is_mounted(f):
            return Rejection("mounted", "mounted figures do not free-spin")
        # Must have been just contacted by a mover this action (P4-R9), not merely
        # standing in a contact that has persisted — otherwise it's an out-of-turn re-face.
        if intent.figure_uid not in self._pending_free_spins:
            return Rejection("no_free_spin_offer",
                             "no opponent just moved into base contact with this figure")
        if not self.state.opposing_contacts(f):
            return Rejection("not_contacted",
                             "free spin is only for a figure in base contact with an opponent")
        f.facing = float(intent.facing)
        self._pending_free_spins.discard(intent.figure_uid)
        ev = self.log.emit("free_spin", figure=f.uid, facing=f.facing)
        return Result("free_spin", [ev], f"{f.short_name} spins free to face the threat")

    def _apply_toggle_ability(self, intent: ToggleAbilityIntent) -> Result | Rejection:
        """Cancel/restore an optional ability shown on the figure's current click
        (P4-R34). Not an action: no budget, no token, no acted-mark; the engine
        clears all cancellations at the start of each owner turn."""
        f = self.state.figures.get(intent.figure_uid)
        if f is None or not f.is_alive:
            return Rejection("no_such_figure", str(intent.figure_uid))
        ref = next(
            (a for a in f.definition.dial[f.current_click].abilities if a.id == intent.ability_id),
            None,
        )
        if ref is None:
            return Rejection("no_ability", "that ability is not on the current click")
        if not ref.optional:
            return Rejection("not_optional", f"{ref.name} is not an optional ability")
        if intent.off:
            f.disabled_ability_ids.add(intent.ability_id)
        else:
            f.disabled_ability_ids.discard(intent.ability_id)
        ev = self.log.emit("toggle_ability", figure=f.uid, ability=intent.ability_id,
                           name=ref.name, off=intent.off)
        verb = "cancels" if intent.off else "restores"
        return Result("toggle_ability", [ev], f"{f.short_name} {verb} {ref.name}")

    def explain_attack(
        self, attacker_uid: int, target_uid: int, attack_type: str = "close", rear: bool = False,
    ) -> dict:
        """Decompose an attack's numbers so the client can show WHY they changed:
        Battle Armor / Defend on defense, Magic Enhancement / Toughness on damage."""
        a = self.state.figure(attacker_uid)
        t = self.state.figure(target_uid)
        base_def = t.defense
        ba = 2 if (attack_type == "ranged" and ab.has(t, ab.BATTLE_ARMOR)) else 0
        best_share = 0
        for fr in self.state.friends_of(t):
            if ab.has(fr, ab.DEFEND) and in_base_contact(
                t.position, t.base_radius, fr.position, fr.base_radius
            ):
                best_share = max(best_share, fr.defense)
        defend = max(0, best_share - base_def)
        tmod = self.terrain_defense_mod(a, t, attack_type)
        eff_def = ab.effective_defense(self.state, t, attack_type, tmod)
        base_dmg = a.damage
        enh = ab.ranged_damage_bonus(self.state, a, t) if attack_type == "ranged" else 0
        per_hit = ab.damage_after_defenses(t, base_dmg + enh, attack_type, is_magic=False)
        toughness = per_hit - (base_dmg + enh)  # negative when Toughness reduces
        return {
            "attack": a.attack + (1 if rear else 0),
            "rear": rear,
            "defense": {"base": base_def, "battle_armor": ba, "defend": defend, "terrain": tmod, "effective": eff_def},
            "damage": {"base": base_dmg, "enhancement": enh, "toughness": toughness, "per_hit": per_hit},
            "hit_odds": round(self.hit_odds(attacker_uid, target_uid, rear, attack_type), 3),
            "expected_clicks": round(
                self.expected_damage(attacker_uid, target_uid, rear, attack_type=attack_type), 3
            ),
        }

    def _precheck(self, figure_uid: int, free: bool = False) -> Figure | Rejection:
        if not free and self._actions_remaining() <= 0:
            return Rejection("no_actions", "no actions remaining this turn")
        f = self.state.figures.get(figure_uid)
        if f is None:
            return Rejection("no_such_figure", str(figure_uid))
        if not f.is_alive:
            return Rejection("eliminated", f"{f.short_name} is eliminated")
        if f.owner != self.state.active_player:
            return Rejection("not_your_figure", f"{f.short_name} is not active player's")
        if f.uid in self._acted_uids:
            return Rejection("already_acted", f"{f.short_name} already acted this turn")
        return f

    def _consume_nonpass(self, figure: Figure, free: bool = False) -> bool:
        """Mark a non-pass action; return True if this is a *pushing* action.
        ``free`` (Quickness) marks the figure as acted & tokened without spending
        one of the turn's budget actions (§Quickness)."""
        pushing = figure.action_tokens >= 1
        figure.acted_nonpass_this_turn = True
        figure.action_tokens += 1
        self._acted_uids.add(figure.uid)
        if not free:
            self._actions_spent += 1
        return pushing

    def _apply_pushing_damage(self, figure: Figure, events: list[dict]) -> None:
        # Pushing deals 1 click after the action resolves; Toughness does not
        # reduce it (§Pushing / Toughness text).
        applied = figure.take_clicks(1)
        ev = self.log.emit(
            "push_damage", figure=figure.uid, clicks=applied, eliminated=figure.eliminated
        )
        events.append(ev)
        if figure.eliminated:
            self._on_eliminated(figure, events)

    # -- Pass --------------------------------------------------------------
    def _apply_pass(self, intent: PassIntent) -> Result | Rejection:
        f = self._precheck(intent.figure_uid)
        if isinstance(f, Rejection):
            return f
        # Pass does not token (P4-R4); the figure rests and clears tokens at turn end.
        self._acted_uids.add(f.uid)
        self._actions_spent += 1
        ev = self.log.emit("pass", figure=f.uid)
        return Result("pass", [ev], f"{f.short_name} passes")

    # -- Move --------------------------------------------------------------
    def _validate_move(self, f: Figure, dest: Vec) -> Rejection | None:
        """Shared move legality (bounds, speed, demoralized, path). Returns a
        Rejection or None if legal."""
        if not self.state.board.contains(dest, f.base_radius):
            return Rejection("off_board", "destination is off the board")
        flies = ab.ignores_figure_bases(f)  # Flight/Aquatic pass through bases & terrain
        pieces = self.state.terrain
        # Speed may be halved by starting in hindering terrain (§Hindering; fliers exempt).
        eff_speed = f.speed if flies else terr.effective_speed(pieces, f.speed, f.position, f.base_radius)
        dist = distance(f.position, dest)
        if dist > eff_speed + 1e-9:
            extra = " (halved by hindering)" if eff_speed < f.speed else ""
            return Rejection("too_far", f"distance {dist:.2f}\" exceeds speed {eff_speed}\"{extra}")
        # Blocking terrain / deep water: non-fliers can't cross it or end in it;
        # a flier soars over it but may not END its move there (§Flight card text).
        if pieces:
            if terr.base_in_blocking(pieces, dest, f.base_radius):
                return Rejection("in_blocking", "destination is in impassable terrain")
            if not flies:
                # Escape hatch: a figure that somehow overlaps blocking terrain
                # (a legacy/bug state — never reachable by legal play) may still
                # walk OUT; only the destination legality is enforced for it.
                stuck = terr.base_in_blocking(pieces, f.position, f.base_radius)
                if not stuck and dist > 1e-9:
                    if terr.blocking_between(pieces, f.position, dest, f.base_radius):
                        return Rejection("path_blocked", "path crosses impassable terrain")
                    hv = terr.hindering_entry_violation(pieces, f.position, dest, f.base_radius)
                    if hv is not None:
                        what = "the low wall" if hv.low_wall else "hindering terrain"
                        return Rejection(
                            "must_stop_in_hindering",
                            f"entering {what} ends the move — stop there (§Hindering)",
                        )
        if f.is_demoralized:
            already = {c.uid for c in self.state.opposing_contacts(f)}
            for opp in self.state.opponents_of(f):
                if opp.uid in already:
                    continue
                if in_base_contact(dest, f.base_radius, opp.position, opp.base_radius):
                    return Rejection(
                        "demoralized_contact",
                        f"{f.short_name} is demoralized and may not move into contact",
                    )
        for other in self.state.living():
            if other.uid == f.uid:
                continue
            if dist > 1e-9 and not flies and segment_circle_intersects(
                f.position, dest, other.position, other.base_radius
            ):
                return Rejection("path_blocked", f"path crosses {other.short_name}'s base")
            # NOBODY may END overlapping another base — touching is the closest
            # legal stop. (The path check treats the mover as a point, so without
            # this a walker could legally land half-on-top of a neighbour.)
            if distance(dest, other.position) < f.base_radius + other.base_radius - CONTACT_TOLERANCE:
                return Rejection("end_on_base",
                                 f"would end overlapping {other.short_name}'s base")
        return None

    def _apply_move(self, intent: MoveIntent) -> Result | Rejection:
        if intent.formation_uids:
            return self._apply_formation_move(intent)
        f = self._precheck(intent.figure_uid, free=intent.free)
        if isinstance(f, Rejection):
            return f
        if intent.free and not ab.has(f, ab.QUICKNESS):
            return Rejection("no_quickness", f"{f.short_name} lacks Quickness for a free move")
        if f.action_tokens >= 2:
            return Rejection("pushed_out", f"{f.short_name} cannot act a third consecutive turn")

        dest = Vec(*intent.dest)
        rej = self._validate_move(f, dest)
        if rej is not None:
            return rej

        events: list[dict] = []
        pushing = self._consume_nonpass(f, free=intent.free)

        # Break-away if in base contact with an opposing figure (P4-R8); Flight /
        # Aquatic only fail on a natural 1 (§Flight / §Aquatic).
        dist = distance(f.position, dest)
        contacts = self.state.opposing_contacts(f)
        before_contacts = {c.uid for c in contacts}
        broke_away = True
        if contacts and dist > 1e-9:
            d1 = self.rng.d6("break_away", f"{f.short_name} breaking away")
            broke_away = d1 >= ab.break_away_min(f)
            events.append(self.log.emit(
                "break_away", figure=f.uid, roll=d1, success=broke_away,
                opponents=[c.uid for c in contacts],
            ))

        start = f.position
        if broke_away:
            f.position = dest
        f.facing = intent.facing  # re-face allowed even on a failed break-away
        events.append(self.log.emit(
            "move", figure=f.uid, frm=[start.x, start.y],
            to=[f.position.x, f.position.y], facing=f.facing, moved=broke_away, free=intent.free,
        ))

        # Pole Arm: an enemy whose front arc the mover now sits in deals 1 click.
        if broke_away and dist > 1e-9:
            self._apply_pole_arm(f, events)

        # Free spin (P4-R9): opposing figures the mover just contacted may re-face.
        if broke_away and dist > 1e-9 and f.is_alive:
            newly = self._newly_contacted_opponents(f, before_contacts)
            if newly:
                self._pending_free_spins.update(o.uid for o in newly)
                events.append(self.log.emit(
                    "free_spin_offer", by=f.uid, spinners=[o.uid for o in newly]))

        if pushing and f.is_alive:
            self._apply_pushing_damage(f, events)
        self._check_victory(events)

        summary = (
            f"{f.short_name} moves to ({f.position.x:.1f},{f.position.y:.1f})"
            if broke_away
            else f"{f.short_name} fails to break away, re-faces"
        )
        return Result("move", events, summary)

    def _apply_pole_arm(self, mover: Figure, events: list[dict]) -> None:
        """Opposing Pole Arm figures damage a mover that ends in their front arc
        and base contact (§Pole Arm)."""
        if not mover.is_alive:
            return
        for p in self.state.opponents_of(mover):
            if not ab.has(p, ab.POLE_ARM):
                continue
            if in_base_contact(
                mover.position, mover.base_radius, p.position, p.base_radius
            ) and in_front_arc(p.position, p.facing, mover.position, p.arc_half_angle):
                dmg = self._deal_combat_damage(mover, 1, source_type="ability")
                events.append(self.log.emit(
                    "pole_arm", attacker=p.uid, target=mover.uid, clicks=dmg,
                    eliminated=mover.eliminated,
                ))
                if mover.eliminated:
                    self._on_eliminated(mover, events)
                    return

    # -- Ranged ------------------------------------------------------------
    def _apply_ranged(self, intent: RangedIntent) -> Result | Rejection:
        if intent.formation_uids:
            return self._apply_ranged_formation(intent)
        f = self._precheck(intent.attacker_uid)
        if isinstance(f, Rejection):
            return f
        if f.range <= 0:
            return Rejection("no_range", f"{f.short_name} has no ranged attack")
        if f.is_demoralized:
            return Rejection("demoralized", f"{f.short_name} is demoralized (move/pass only)")
        if not ab.can_make_ranged_attack(f):
            return Rejection("berserk", f"{f.short_name} (Berserk) cannot make ranged attacks")
        if f.action_tokens >= 2:
            return Rejection("pushed_out", f"{f.short_name} cannot act a third consecutive turn")
        if self.state.opposing_contacts(f):
            return Rejection("in_contact", "cannot fire while in base contact (P4-R23)")

        v = intent.variant
        _variant_ability = {
            "magic_healing": ab.MAGIC_HEALING, "shockwave": ab.SHOCKWAVE,
            "magic_blast": ab.MAGIC_BLAST, "flame_lightning": ab.FLAME_LIGHTNING,
        }
        if v != "normal" and not ab.has(f, _variant_ability.get(v, -1)):
            return Rejection("no_ability", f"{f.short_name} lacks the '{v}' ability")
        if v == "magic_healing":
            return self._resolve_magic_healing(f, intent)
        if v == "shockwave":
            return self._resolve_shockwave(f, intent)
        if v == "magic_blast":
            return self._resolve_magic_blast(f, intent)
        if v == "flame_lightning":
            return self._resolve_flame_lightning(f, intent)

        # -- normal ranged --
        targets = [self.state.figures.get(u) for u in intent.target_uids]
        if not targets or any(t is None for t in targets):
            return Rejection("bad_target", "unknown target")
        if len(set(intent.target_uids)) != len(intent.target_uids):
            return Rejection("dup_target", "duplicate targets")
        if len(targets) > f.targets:
            return Rejection(
                "too_many_targets", f"{f.short_name} may hit at most {f.targets} targets"
            )
        for t in targets:
            if not t.is_alive or t.owner == f.owner:
                return Rejection("bad_target", "target must be a living opponent")
            clear, reason = self.line_of_fire(f.uid, t.uid)
            if not clear:
                return Rejection("no_lof", reason)

        events: list[dict] = []
        pushing = self._consume_nonpass(f)
        d1, d2, total = self.rng.roll_2d6("attack", f"{f.short_name} ranged")
        multi = len(targets) > 1
        for t in targets:
            eff_def = ab.effective_defense(self.state, t, "ranged", self.terrain_defense_mod(f, t, "ranged"))
            res = outcome(d1, d2, f.attack, eff_def)
            if res not in ("hit", "crit_hit"):
                events.append(self.log.emit("ranged_attack", attacker=f.uid, target=t.uid,
                              dice=[d1, d2], result=res, clicks=0))
                continue
            if multi:
                raw = 2 if res == "crit_hit" else 1
            else:
                raw = f.damage + (1 if res == "crit_hit" else 0)
            raw += ab.ranged_damage_bonus(self.state, f, t)  # Magic Enhancement
            dmg = self._deal_combat_damage(t, raw, source_type="ranged")
            events.append(self.log.emit("ranged_attack", attacker=f.uid, target=t.uid,
                          dice=[d1, d2], result=res, clicks=dmg, eliminated=t.eliminated))
            if t.eliminated:
                self._on_eliminated(t, events)
        if total == 2:
            self._crit_miss_self(f, events)
        tnames = ", ".join(t.short_name for t in targets)
        return self._finish_action(f, events, pushing, "ranged", f"{f.short_name} fires at {tnames}")

    # -- Close -------------------------------------------------------------
    def _apply_close(self, intent: CloseIntent) -> Result | Rejection:
        if intent.formation_uids:
            return self._apply_close_formation(intent)
        f = self._precheck(intent.attacker_uid)
        if isinstance(f, Rejection):
            return f
        if f.is_demoralized:
            return Rejection("demoralized", f"{f.short_name} is demoralized (move/pass only)")
        if f.action_tokens >= 2:
            return Rejection("pushed_out", f"{f.short_name} cannot act a third consecutive turn")
        if intent.variant == "weapon_master" and not ab.has(f, ab.WEAPON_MASTER):
            return Rejection("no_ability", f"{f.short_name} lacks Weapon Master")
        if intent.variant == "healing":
            if not ab.has(f, ab.HEALING):
                return Rejection("no_ability", f"{f.short_name} lacks Healing")
            return self._resolve_healing(f, intent)
        t = self.state.figures.get(intent.target_uid)
        if t is None or not t.is_alive or t.owner == f.owner:
            return Rejection("bad_target", "target must be a living opponent")
        if not in_base_contact(f.position, f.base_radius, t.position, t.base_radius):
            return Rejection("not_adjacent", "attacker not in base contact with target")
        if not in_front_arc(f.position, f.facing, t.position, f.arc_half_angle):
            return Rejection("out_of_arc", "target not in attacker's front arc (P4-R27)")

        events: list[dict] = []
        pushing = self._consume_nonpass(f)

        rear = in_rear_arc(t.position, t.facing, f.position, t.arc_half_angle)
        atk = f.attack + (1 if rear else 0)
        d1, d2, total = self.rng.roll_2d6("attack", f"{f.short_name} close")
        eff_def = ab.effective_defense(self.state, t, "close", self.terrain_defense_mod(f, t, "close"))
        res = outcome(d1, d2, atk, eff_def)

        if res in ("hit", "crit_hit"):
            # Weapon Master: deliver 1d6 clicks instead of the printed damage value.
            base = (
                self.rng.d6("weapon_master_damage", f"{f.short_name} Weapon Master")
                if intent.variant == "weapon_master"
                else f.damage
            )
            raw = base + (1 if res == "crit_hit" else 0)
            dmg = self._deal_combat_damage(t, raw, source_type="close")
            events.append(self.log.emit(
                "close_attack", attacker=f.uid, target=t.uid, dice=[d1, d2],
                rear=rear, result=res, variant=intent.variant, clicks=dmg,
                eliminated=t.eliminated,
            ))
            if dmg > 0 and ab.vampirism_heal(f):  # Vampirism: heal 1 on inflicting damage
                healed = f.heal_clicks(1)
                if healed:
                    events.append(self.log.emit("vampirism", figure=f.uid, healed=healed))
            if t.eliminated:
                self._on_eliminated(t, events)
        else:
            events.append(self.log.emit(
                "close_attack", attacker=f.uid, target=t.uid, dice=[d1, d2],
                rear=rear, result=res, clicks=0,
            ))
            if res == "crit_miss":
                self._crit_miss_self(f, events)

        rtag = " (rear)" if rear else ""
        return self._finish_action(f, events, pushing, "close",
                                   f"{f.short_name} attacks {t.short_name}{rtag}")

    # ------------------------------------------------------------------ #
    # Damage & shared action tails (ability hooks)
    # ------------------------------------------------------------------ #
    def _deal_combat_damage(
        self, target: Figure, raw: int, source_type: str = "close", is_magic: bool = False
    ) -> int:
        """Apply damage through defensive ability hooks (Toughness, Magic Immunity)."""
        clicks = ab.damage_after_defenses(target, raw, source_type, is_magic)
        return target.take_clicks(clicks)

    def _crit_miss_self(self, f: Figure, events: list[dict]) -> None:
        applied = f.take_clicks(1)
        events.append(self.log.emit("crit_miss_self", figure=f.uid, clicks=applied,
                      eliminated=f.eliminated))
        if f.eliminated:
            self._on_eliminated(f, events)

    def _finish_action(
        self, f: Figure, events: list[dict], pushing: bool, kind: str, summary: str
    ) -> Result:
        if pushing and f.is_alive:
            self._apply_pushing_damage(f, events)
        self._check_victory(events)
        return Result(kind, events, summary)

    def _on_eliminated(self, figure: Figure, events: list[dict]) -> None:
        ev = self.log.emit("eliminated", figure=figure.uid, owner=figure.owner,
                           points=figure.points)
        events.append(ev)

    # ------------------------------------------------------------------ #
    # Ability special-attack resolvers (all consume one ranged action)
    # ------------------------------------------------------------------ #
    def _resolve_magic_blast(self, f: Figure, intent) -> Result | Rejection:
        if len(intent.target_uids) != 1:
            return Rejection("bad_target", "Magic Blast targets exactly one figure")
        t = self.state.figures.get(intent.target_uids[0])
        if t is None or not t.is_alive or t.owner == f.owner:
            return Rejection("bad_target", "target must be a living opponent")
        if not in_front_arc(f.position, f.facing, t.position, f.arc_half_angle):
            return Rejection("out_of_arc", "target not in firer's front arc")
        if distance(f.position, t.position) > f.range + 1e-9:
            return Rejection("out_of_range", "target beyond range")
        # Magic Blast ignores LoF blocking, but not the P4-R25 targeting rule.
        for friend in self.state.friends_of(f):
            if in_base_contact(t.position, t.base_radius, friend.position, friend.base_radius):
                return Rejection("adjacent_friendly",
                                 "target is in base contact with a friendly figure")
        events: list[dict] = []
        pushing = self._consume_nonpass(f)
        d1, d2, total = self.rng.roll_2d6("attack", f"{f.short_name} Magic Blast")
        eff_def = ab.effective_defense(self.state, t, "ranged", self.terrain_defense_mod(f, t, "ranged"))
        res = outcome(d1, d2, f.attack, eff_def)
        if res in ("hit", "crit_hit"):
            roll = self.rng.d6("magic_blast_damage")
            raw = roll + (1 if res == "crit_hit" else 0)
            dmg = self._deal_combat_damage(t, raw, source_type="ranged", is_magic=True)
            events.append(self.log.emit("magic_blast", attacker=f.uid, target=t.uid,
                          dice=[d1, d2], result=res, roll=roll, clicks=dmg,
                          eliminated=t.eliminated))
            if t.eliminated:
                self._on_eliminated(t, events)
        else:
            events.append(self.log.emit("magic_blast", attacker=f.uid, target=t.uid,
                          dice=[d1, d2], result=res, clicks=0))
            if res == "crit_miss":
                self._crit_miss_self(f, events)
        return self._finish_action(f, events, pushing, "ranged",
                                   f"{f.short_name} Magic Blasts {t.short_name}")

    def _resolve_flame_lightning(self, f: Figure, intent) -> Result | Rejection:
        if len(intent.target_uids) != 1:
            return Rejection("bad_target", "Flame/Lightning targets one figure")
        prim = self.state.figures.get(intent.target_uids[0])
        if prim is None or not prim.is_alive or prim.owner == f.owner:
            return Rejection("bad_target", "target must be a living opponent")
        clear, reason = self.line_of_fire(f.uid, prim.uid)
        if not clear:
            return Rejection("no_lof", reason)
        # Splash to every figure (friend or foe) in base contact with the target.
        affected = [prim]
        for o in self.state.living():
            if o.uid in (f.uid, prim.uid):
                continue
            if in_base_contact(prim.position, prim.base_radius, o.position, o.base_radius):
                affected.append(o)
        events: list[dict] = []
        pushing = self._consume_nonpass(f)
        d1, d2, total = self.rng.roll_2d6("attack", f"{f.short_name} Flame/Lightning")
        for o in affected:
            eff_def = ab.effective_defense(self.state, o, "ranged", self.terrain_defense_mod(f, o, "ranged"))
            res = outcome(d1, d2, f.attack, eff_def)
            if res in ("hit", "crit_hit"):
                raw = 1 + (1 if res == "crit_hit" else 0)
                dmg = self._deal_combat_damage(o, raw, source_type="ranged")
                events.append(self.log.emit("flame_lightning", attacker=f.uid, target=o.uid,
                              dice=[d1, d2], result=res, clicks=dmg, eliminated=o.eliminated))
                if o.eliminated:
                    self._on_eliminated(o, events)
            else:
                events.append(self.log.emit("flame_lightning", attacker=f.uid, target=o.uid,
                              dice=[d1, d2], result=res, clicks=0))
        if total == 2:
            self._crit_miss_self(f, events)
        return self._finish_action(f, events, pushing, "ranged",
                                   f"{f.short_name} Flame/Lightning on {prim.short_name}")

    def _resolve_shockwave(self, f: Figure, intent) -> Result | Rejection:
        half = max(1, f.range // 2)

        def _clear_lof(o: Figure) -> bool:
            # A line of fire is drawn to each figure (arc ignored); it is blocked
            # by any intervening base (§Shockwave / P4-R24).
            for b in self.state.living():
                if b.uid in (f.uid, o.uid):
                    continue
                if segment_circle_intersects(f.position, o.position, b.position, b.base_radius):
                    return False
            return True

        targets = [
            o for o in self.state.living()
            if o.uid != f.uid and not o.captured
            and distance(f.position, o.position) <= half + 1e-9
            and _clear_lof(o)
        ]
        if not targets:
            return Rejection("no_targets", "no figures within half range with a clear line of fire")
        events: list[dict] = []
        pushing = self._consume_nonpass(f)
        d1, d2, total = self.rng.roll_2d6("attack", f"{f.short_name} Shockwave")
        normal = len(targets) == 1
        for o in targets:
            # Resolved as if targets have no special abilities: raw defense, no hooks.
            res = outcome(d1, d2, f.attack, o.defense)
            if res in ("hit", "crit_hit"):
                raw = (f.damage if normal else 1) + (1 if res == "crit_hit" else 0)
                applied = o.take_clicks(raw)
                events.append(self.log.emit("shockwave", attacker=f.uid, target=o.uid,
                              dice=[d1, d2], result=res, clicks=applied, eliminated=o.eliminated))
                if o.eliminated:
                    self._on_eliminated(o, events)
            else:
                events.append(self.log.emit("shockwave", attacker=f.uid, target=o.uid,
                              dice=[d1, d2], result=res, clicks=0))
        if total == 2:
            self._crit_miss_self(f, events)
        return self._finish_action(f, events, pushing, "ranged",
                                   f"{f.short_name} Shockwave hits {len(targets)} figures")

    def _resolve_magic_healing(self, f: Figure, intent) -> Result | Rejection:
        if len(intent.target_uids) != 1:
            return Rejection("bad_target", "Magic Healing targets one friendly figure")
        t = self.state.figures.get(intent.target_uids[0])
        if t is None or not t.is_alive or t.owner != f.owner:
            return Rejection("bad_target", "Magic Healing targets a friendly figure")
        if self.state.opposing_contacts(t):
            return Rejection("bad_target", "target is in base contact with an opponent")
        if ab.has(t, ab.MAGIC_IMMUNITY):
            return Rejection("magic_immune", f"{t.short_name} is immune to Magic effects")
        if not in_front_arc(f.position, f.facing, t.position, f.arc_half_angle):
            return Rejection("out_of_arc", "target not in firer's front arc")
        if distance(f.position, t.position) > f.range + 1e-9:
            return Rejection("out_of_range", "target beyond range")
        events: list[dict] = []
        pushing = self._consume_nonpass(f)
        d1, d2, total = self.rng.roll_2d6("attack", f"{f.short_name} Magic Healing")
        res = outcome(d1, d2, f.attack, t.defense)  # ignore modifiers
        healed = 0
        if res in ("hit", "crit_hit"):
            healed = t.heal_clicks(self.rng.d6("magic_heal_amount") + (1 if res == "crit_hit" else 0))
        elif res == "crit_miss":
            self._crit_miss_self(f, events)  # roll of "2": weapon backfire on the healer (rulebook §Rolling 2 and 12)
        events.append(self.log.emit("magic_healing", healer=f.uid, target=t.uid,
                      dice=[d1, d2], result=res, healed=healed))
        if res == "crit_miss":
            summary = (f"{f.short_name}'s Magic Healing backfires (natural 2) — "
                       f"{f.short_name} takes 1 click; {t.short_name} is unhurt")
        elif healed > 0:
            summary = f"{f.short_name} magically heals {t.short_name} ({healed} clicks)"
        else:
            summary = f"{f.short_name} fails to heal {t.short_name} (missed — no effect)"
        return self._finish_action(f, events, pushing, "ranged", summary)

    def _resolve_healing(self, f: Figure, intent) -> Result | Rejection:
        t = self.state.figures.get(intent.target_uid)
        if t is None or not t.is_alive or t.owner != f.owner:
            return Rejection("bad_target", "Healing targets a friendly figure")
        if not in_base_contact(f.position, f.base_radius, t.position, t.base_radius):
            return Rejection("not_adjacent", "healer not in base contact with target")
        if self.state.opposing_contacts(f) or self.state.opposing_contacts(t):
            return Rejection("in_contact", "neither figure may be in contact with an opponent")
        events: list[dict] = []
        pushing = self._consume_nonpass(f)
        d1, d2, total = self.rng.roll_2d6("attack", f"{f.short_name} Healing")
        res = outcome(d1, d2, f.attack, t.defense)  # ignore modifiers
        # The healer may heal by its damage value OR, alternatively, by a 1d6 roll
        # (§Healing). The client picks the method via intent.heal_d6.
        base_heal = self.rng.d6("heal_amount") if getattr(intent, "heal_d6", False) else f.damage
        healed = t.heal_clicks(base_heal + (1 if res == "crit_hit" else 0)) if res in ("hit", "crit_hit") else 0
        if res == "crit_miss":
            self._crit_miss_self(f, events)  # roll of "2": weapon backfire on the healer (rulebook §Rolling 2 and 12)
        events.append(self.log.emit("healing", healer=f.uid, target=t.uid,
                      dice=[d1, d2], result=res, healed=healed))
        # Summaries must say what actually happened — "heals (0 clicks)" on a
        # backfire reads like the TARGET was hurt.
        if res == "crit_miss":
            summary = (f"{f.short_name}'s healing backfires (natural 2) — "
                       f"{f.short_name} takes 1 click; {t.short_name} is unhurt")
        elif healed > 0:
            summary = f"{f.short_name} heals {t.short_name} ({healed} clicks)"
        else:
            summary = f"{f.short_name} fails to heal {t.short_name} (missed — no effect)"
        return self._finish_action(f, events, pushing, "close", summary)

    # ------------------------------------------------------------------ #
    # Ability special *move* actions (move-but-don't-move)
    # ------------------------------------------------------------------ #
    def _apply_regenerate(self, intent) -> Result | Rejection:
        f = self._precheck(intent.figure_uid)
        if isinstance(f, Rejection):
            return f
        if not ab.has(f, ab.REGENERATION):
            return Rejection("no_ability", f"{f.short_name} lacks Regeneration")
        if f.action_tokens >= 2:
            return Rejection("pushed_out", f"{f.short_name} cannot act a third consecutive turn")
        events: list[dict] = []
        pushing = self._consume_nonpass(f)
        roll = self.rng.d6("regeneration", f"{f.short_name} Regeneration")
        healed = f.heal_clicks(max(0, roll - 2))
        events.append(self.log.emit("regenerate", figure=f.uid, roll=roll, healed=healed))
        return self._finish_action(f, events, pushing, "regenerate",
                                   f"{f.short_name} regenerates {healed} clicks")

    def _apply_necromancy(self, intent) -> Result | Rejection:
        f = self._precheck(intent.figure_uid)
        if isinstance(f, Rejection):
            return f
        if not ab.has(f, ab.NECROMANCY):
            return Rejection("no_ability", f"{f.short_name} lacks Necromancy")
        if f.action_tokens >= 2:
            return Rejection("pushed_out", f"{f.short_name} cannot act a third consecutive turn")
        if self.state.opposing_contacts(f):
            return Rejection("in_contact", "Necromancer may not be in base contact with an opponent")
        dead = self.state.figures.get(intent.revive_uid)
        if dead is None or dead.owner != f.owner or not dead.eliminated:
            return Rejection("bad_target", "choose one of your eliminated figures")
        events: list[dict] = []
        pushing = self._consume_nonpass(f)
        name = dead.short_name.lower()
        auto = "zombie" in name or "skeleton" in name
        clicks = 0 if auto else self.rng.d6("necromancy", f"{f.short_name} Necromancy")
        dead.eliminated = False
        dead.current_click = dead.definition.starting_click
        dead.take_clicks(clicks)
        if dead.eliminated:
            events.append(self.log.emit("necromancy_fail", necromancer=f.uid,
                          target=dead.uid, clicks=clicks))
        else:
            dead.position = self._free_contact_position(f, dead.base_radius)
            dead.facing = f.facing
            dead.action_tokens = 0
            dead.begin_owner_turn()  # fresh turn state; §Necromancy places no bar on the
            # returned figure being given an action later this turn.
            events.append(self.log.emit("necromancy", necromancer=f.uid, target=dead.uid,
                          clicks=clicks, pos=[dead.position.x, dead.position.y]))
        return self._finish_action(f, events, pushing, "necromancy",
                                   f"{f.short_name} attempts Necromancy on {dead.short_name}")

    def _apply_levitate(self, intent) -> Result | Rejection:
        f = self._precheck(intent.figure_uid)
        if isinstance(f, Rejection):
            return f
        if not ab.has(f, ab.MAGIC_LEVITATION):
            return Rejection("no_ability", f"{f.short_name} lacks Magic Levitation")
        if f.action_tokens >= 2:
            return Rejection("pushed_out", f"{f.short_name} cannot act a third consecutive turn")
        t = self.state.figures.get(intent.target_uid)
        if t is None or not t.is_alive or t.owner != f.owner:
            return Rejection("bad_target", "levitation target must be a friendly figure")
        if t.uid in self._acted_uids:
            return Rejection("already_acted", "levitation target has already acted this turn")
        if ab.has(t, ab.MAGIC_IMMUNITY):
            return Rejection("magic_immune", "target is immune to Magic effects")
        if not in_base_contact(f.position, f.base_radius, t.position, t.base_radius):
            return Rejection("not_adjacent", "target must be in base contact with the caster")
        dest = Vec(*intent.dest)
        if distance(t.position, dest) > 10 + 1e-9:
            return Rejection("too_far", "Magic Levitation moves a figure up to 10 units")
        if not self.state.board.contains(dest, t.base_radius):
            return Rejection("off_board", "destination is off the board")
        for o in self.state.living():
            if o.uid == t.uid:
                continue
            if distance(dest, o.position) < t.base_radius + o.base_radius - CONTACT_TOLERANCE:
                return Rejection("end_on_base", "levitated figure may not end on a base")
        events: list[dict] = []
        pushing = self._consume_nonpass(f)
        t.position = dest
        t.facing = intent.facing
        self._acted_uids.add(t.uid)  # levitated figure may not be given an action this turn
        events.append(self.log.emit("levitate", caster=f.uid, target=t.uid,
                      to=[dest.x, dest.y]))
        return self._finish_action(f, events, pushing, "levitate",
                                   f"{f.short_name} levitates {t.short_name}")

    def _free_contact_position(self, anchor: Figure, radius: float) -> Vec:
        # Exact touch with the anchor (edge gap 0 => base contact); reject only
        # spots that would *overlap* another figure (touching others is fine).
        gap = anchor.base_radius + radius
        for k in range(16):
            ang = 2 * math.pi * k / 16
            p = Vec(anchor.position.x + gap * math.cos(ang),
                    anchor.position.y + gap * math.sin(ang))
            if not self.state.board.contains(p, radius):
                continue
            if not any(
                o.uid != anchor.uid
                and distance(p, o.position) < radius + o.base_radius - CONTACT_TOLERANCE
                for o in self.state.living()
            ):
                return p
        return Vec(anchor.position.x, anchor.position.y + gap)

    # ------------------------------------------------------------------ #
    # Formations (P4-R11..R16, R29)
    # ------------------------------------------------------------------ #
    def _positions_cohesive(self, positions, radii) -> bool:
        n = len(positions)
        adj = {i: set() for i in range(n)}
        for i in range(n):
            for j in range(i + 1, n):
                if in_base_contact(positions[i], radii[i], positions[j], radii[j]):
                    adj[i].add(j)
                    adj[j].add(i)
        if any(not adj[i] for i in range(n)):
            return False  # every member must touch at least one other
        seen, stack = {0}, [0]
        while stack:
            for k in adj[stack.pop()]:
                if k not in seen:
                    seen.add(k)
                    stack.append(k)
        return len(seen) == n  # single connected group

    def _validate_formation(self, uids, kind: str) -> tuple | Rejection:
        if len(set(uids)) != len(uids):
            return Rejection("bad_formation", "a figure may not appear twice in a formation")
        figs = [self.state.figures.get(u) for u in uids]
        if any(g is None or not g.is_alive for g in figs):
            return Rejection("bad_formation", "all members must be living figures")
        if any(g.owner != self.state.active_player for g in figs):
            return Rejection("bad_formation", "all members must be yours")
        if any(g.uid in self._acted_uids for g in figs):
            return Rejection("already_acted", "a member has already acted this turn")
        if any(g.action_tokens >= 2 for g in figs):
            return Rejection("pushed_out", "a member cannot act a third consecutive turn")
        if len({g.definition.faction for g in figs}) != 1:
            return Rejection("bad_formation", "formation members must share a faction")
        if figs[0].definition.faction == MAGE_SPAWN_FACTION:
            return Rejection("bad_formation",
                             "Mage Spawn cannot form formations (no Shyft present)")
        return tuple(figs)

    def _token_formation(self, figs) -> list:
        pushers = [g for g in figs if g.action_tokens >= 1]
        for g in figs:
            g.acted_nonpass_this_turn = True
            g.action_tokens += 1
            self._acted_uids.add(g.uid)
        self._actions_spent += 1  # the whole formation is one action
        return pushers

    def _apply_pushing_to(self, pushers, events) -> None:
        for g in pushers:
            if g.is_alive:
                self._apply_pushing_damage(g, events)

    def _apply_formation_move(self, intent) -> Result | Rejection:
        if self._actions_remaining() <= 0:
            return Rejection("no_actions", "no actions remaining this turn")
        uids = list(intent.formation_uids)
        if not (3 <= len(uids) <= 5):
            return Rejection("bad_formation", "a movement formation is 3-5 figures")
        if intent.figure_uid not in uids:
            return Rejection("bad_formation", "the acting figure must be a member")
        figs = self._validate_formation(uids, "move")
        if isinstance(figs, Rejection):
            return figs
        if any(g.active_ability_ids() & (ab.FREE_MOVEMENT_IDS | {ab.QUICKNESS}) for g in figs):
            return Rejection("bad_formation", "Flight/Aquatic/Quickness may not join a movement formation")
        if any(g.is_demoralized for g in figs):
            return Rejection("bad_formation", "a demoralized figure may not join a formation")
        # Deliberate, conservative deviation (P4-R12): the rulebook lets a contacted
        # member join and roll to break away (stay put on a fail, may still rotate). We
        # instead reject — the player then moves those figures individually (always
        # legal) and the AI only ever forms movement formations before contact, so this
        # path is never taken. Documented in docs/progress.md → Known limitations.
        if any(self.state.opposing_contacts(g) for g in figs):
            return Rejection("bad_formation", "members in base contact with enemies must move individually")
        if not self._positions_cohesive([g.position for g in figs], [g.base_radius for g in figs]):
            return Rejection("bad_formation", "members must each touch another member at the start")
        dests = [Vec(*d) for d in intent.member_dests]
        facings = list(intent.member_facings)
        if len(dests) != len(figs) or len(facings) != len(figs):
            return Rejection("bad_formation", "must give a destination and facing for each member")
        pieces = self.state.terrain
        # Slowest member's speed (P4-R13), with hindering halving applied per
        # member before taking the minimum (§Hindering). Members never fly
        # (Flight/Aquatic are rejected above), so no flier exemptions here.
        speed = min(
            terr.effective_speed(pieces, g.speed, g.position, g.base_radius) for g in figs
        )
        member_uids = {g.uid for g in figs}
        for g, d in zip(figs, dests):
            if distance(g.position, d) > speed + 1e-9:
                return Rejection("too_far", f"{g.short_name} exceeds formation speed {speed}\"")
            if not self.state.board.contains(d, g.base_radius):
                return Rejection("off_board", f"{g.short_name} would leave the board")
            # Each member's straight path may not cross a non-member base, and it
            # may not end overlapping one (P4-R6/R14).
            for other in self.state.living():
                if other.uid in member_uids:
                    continue
                if distance(g.position, d) > 1e-9 and segment_circle_intersects(
                    g.position, d, other.position, other.base_radius
                ):
                    return Rejection("path_blocked",
                                     f"{g.short_name}'s path crosses {other.short_name}'s base")
                if distance(d, other.position) < g.base_radius + other.base_radius - CONTACT_TOLERANCE:
                    return Rejection("end_on_base",
                                     f"{g.short_name} would end on {other.short_name}'s base")
            # Terrain is validated per member exactly like a single move — a
            # formation may not carry its members into blocking terrain / deep
            # water, and entering hindering ends the move there (P4-R30).
            if pieces:
                if terr.base_in_blocking(pieces, d, g.base_radius):
                    return Rejection("in_blocking",
                                     f"{g.short_name} would end in impassable terrain")
                if distance(g.position, d) > 1e-9:
                    if terr.blocking_between(pieces, g.position, d, g.base_radius):
                        return Rejection("path_blocked",
                                         f"{g.short_name}'s path crosses impassable terrain")
                    hv = terr.hindering_entry_violation(pieces, g.position, d, g.base_radius)
                    if hv is not None:
                        return Rejection(
                            "must_stop_in_hindering",
                            f"{g.short_name} must stop on entering hindering terrain",
                        )
        if not self._positions_cohesive(dests, [g.base_radius for g in figs]):
            return Rejection("bad_formation", "formation must stay cohesive (one touching group) at the end")
        events: list[dict] = []
        pushers = self._token_formation(figs)
        events.append(self.log.emit("formation_move", members=list(uids), size=len(figs)))
        for g, d, fac in zip(figs, dests, facings):
            g.position = d
            g.facing = fac
            events.append(self.log.emit("move", figure=g.uid, to=[d.x, d.y], facing=fac, formation=True))
        for g in figs:
            if g.is_alive:
                self._apply_pole_arm(g, events)
        # Free spin (P4-R9): members can't start in enemy contact (rejected above),
        # so every opponent now touching a member was just contacted.
        spun: set[int] = set()
        for g in figs:
            if not g.is_alive or ab.is_mounted(g):
                continue
            for o in self.state.opposing_contacts(g):
                if o.uid not in spun and not ab.is_mounted(o):
                    spun.add(o.uid)
        if spun:
            self._pending_free_spins.update(spun)
            events.append(self.log.emit("free_spin_offer", by=intent.figure_uid,
                                        spinners=sorted(spun)))
        self._apply_pushing_to(pushers, events)
        self._check_victory(events)
        return Result("formation_move", events, f"formation of {len(figs)} advances")

    def _apply_ranged_formation(self, intent) -> Result | Rejection:
        if self._actions_remaining() <= 0:
            return Rejection("no_actions", "no actions remaining this turn")
        uids = list(intent.formation_uids)
        if not (3 <= len(uids) <= 5):
            return Rejection("bad_formation", "a ranged formation is 3-5 figures")
        figs = self._validate_formation(uids, "ranged")
        if isinstance(figs, Rejection):
            return figs
        primary = self.state.figures.get(intent.attacker_uid)
        if primary is None or primary.uid not in uids:
            return Rejection("bad_formation", "primary attacker must be a member")
        if not self._positions_cohesive([g.position for g in figs], [g.base_radius for g in figs]):
            return Rejection("bad_formation", "members must each touch another member")
        if len(intent.target_uids) != 1:
            return Rejection("bad_target", "a ranged formation attacks a single target")
        target = self.state.figures.get(intent.target_uids[0])
        if target is None or not target.is_alive or target.owner == primary.owner:
            return Rejection("bad_target", "target must be a living opponent")
        for g in figs:
            if g.range <= 0 or not ab.can_make_ranged_attack(g):
                return Rejection("bad_formation", f"{g.short_name} cannot make a ranged attack")
            if g.is_demoralized:
                return Rejection("bad_formation", "a demoralized figure may not join a formation")
            if self.state.opposing_contacts(g):
                return Rejection("in_contact", f"{g.short_name} is in base contact with an enemy")
            clear, reason = self.line_of_fire(g.uid, target.uid)
            if not clear:
                return Rejection("no_lof", f"{g.short_name}: {reason}")
        events: list[dict] = []
        pushers = self._token_formation(figs)
        atk = primary.attack + 2 * (len(figs) - 1)  # +2 per extra member
        d1, d2, total = self.rng.roll_2d6("attack", "ranged formation")
        eff_def = ab.effective_defense(self.state, target, "ranged", self.terrain_defense_mod(primary, target, "ranged"))
        res = outcome(d1, d2, atk, eff_def)
        if res in ("hit", "crit_hit"):
            raw = primary.damage + (1 if res == "crit_hit" else 0)  # no damage bonus
            dmg = self._deal_combat_damage(target, raw, source_type="ranged")
            events.append(self.log.emit("ranged_formation", primary=primary.uid, members=uids,
                          target=target.uid, dice=[d1, d2], result=res, clicks=dmg,
                          eliminated=target.eliminated))
            if target.eliminated:
                self._on_eliminated(target, events)
        else:
            events.append(self.log.emit("ranged_formation", primary=primary.uid, members=uids,
                          target=target.uid, dice=[d1, d2], result=res, clicks=0))
            if res == "crit_miss":  # only the primary takes the click (P4-R29)
                self._crit_miss_self(primary, events)
        self._apply_pushing_to(pushers, events)
        self._check_victory(events)
        return Result("ranged_formation", events,
                      f"{len(figs)}-figure ranged formation fires at {target.short_name}")

    def _apply_close_formation(self, intent) -> Result | Rejection:
        if self._actions_remaining() <= 0:
            return Rejection("no_actions", "no actions remaining this turn")
        uids = list(intent.formation_uids)
        if not (2 <= len(uids) <= 3):
            return Rejection("bad_formation", "a close formation is 2-3 figures")
        figs = self._validate_formation(uids, "close")
        if isinstance(figs, Rejection):
            return figs
        primary = self.state.figures.get(intent.attacker_uid)
        if primary is None or primary.uid not in uids:
            return Rejection("bad_formation", "primary attacker must be a member")
        target = self.state.figures.get(intent.target_uid)
        if target is None or not target.is_alive or target.owner == primary.owner:
            return Rejection("bad_target", "target must be a living opponent")
        for g in figs:
            if g.is_demoralized:
                return Rejection("bad_formation", "a demoralized figure may not join a formation")
            if not in_base_contact(g.position, g.base_radius, target.position, target.base_radius):
                return Rejection("not_adjacent", f"{g.short_name} is not in base contact with the target")
            if not in_front_arc(g.position, g.facing, target.position, g.arc_half_angle):
                return Rejection("out_of_arc", f"{g.short_name}'s front arc is not on the target")
        events: list[dict] = []
        pushers = self._token_formation(figs)
        rear = 1 if any(
            in_rear_arc(target.position, target.facing, g.position, target.arc_half_angle)
            for g in figs
        ) else 0
        atk = primary.attack + (len(figs) - 1) + rear  # +1/extra member, +1 if any rear
        d1, d2, total = self.rng.roll_2d6("attack", "close formation")
        eff_def = ab.effective_defense(self.state, target, "close", self.terrain_defense_mod(primary, target, "close"))
        res = outcome(d1, d2, atk, eff_def)
        if res in ("hit", "crit_hit"):
            raw = primary.damage + (1 if res == "crit_hit" else 0)
            dmg = self._deal_combat_damage(target, raw, source_type="close")
            events.append(self.log.emit("close_formation", primary=primary.uid, members=uids,
                          target=target.uid, dice=[d1, d2], result=res, rear=bool(rear),
                          clicks=dmg, eliminated=target.eliminated))
            if dmg > 0 and ab.vampirism_heal(primary):
                healed = primary.heal_clicks(1)
                if healed:
                    events.append(self.log.emit("vampirism", figure=primary.uid, healed=healed))
            if target.eliminated:
                self._on_eliminated(target, events)
        else:
            events.append(self.log.emit("close_formation", primary=primary.uid, members=uids,
                          target=target.uid, dice=[d1, d2], result=res, clicks=0))
            if res == "crit_miss":  # only the primary takes the click (P4-R29)
                self._crit_miss_self(primary, events)
        self._apply_pushing_to(pushers, events)
        self._check_victory(events)
        return Result("close_formation", events,
                      f"{len(figs)}-figure close formation attacks {target.short_name}")

    # ------------------------------------------------------------------ #
    # Start-of-turn ability effects (Command)
    # ------------------------------------------------------------------ #
    def _begin_player_turn(self, player: str) -> list[dict]:
        self._acted_uids.clear()
        self._actions_spent = 0
        self._bonus_actions = 0
        self._pending_free_spins.clear()  # free-spin offers don't survive a turn boundary
        for f in self.state.figures.values():
            if f.owner == player and f.is_alive:
                f.begin_owner_turn()
        events: list[dict] = []
        for f in self.state.living(player):
            if ab.has(f, ab.COMMAND):
                roll = self.rng.d6("command", f"{f.short_name} Command")
                if roll == 6:
                    self._bonus_actions += 1
                    events.append(self.log.emit("command_bonus", figure=f.uid, roll=roll))
                for friend in self.state.friends_of(f):
                    if friend.is_demoralized and in_base_contact(
                        f.position, f.base_radius, friend.position, friend.base_radius
                    ):
                        healed = friend.heal_clicks(1)
                        if healed:
                            events.append(self.log.emit("command_heal", figure=f.uid, target=friend.uid))
        return events

    # ------------------------------------------------------------------ #
    # Turn management
    # ------------------------------------------------------------------ #
    def end_turn(self) -> Result:
        events = []
        active = self.state.active_player
        for f in self.state.figures.values():
            if f.owner == active:
                f.end_owner_turn()
        ev = self.log.emit("end_turn", player=active, turn=self.state.turn_number)
        events.append(ev)
        self._check_victory(events)
        if self.state.ended:
            return Result("end_turn", events, "game over")
        # Advance to the other player and run start-of-turn ability effects.
        self.state.active_player = self.state.other_player(active)
        self.state.turn_number += 1
        events.extend(self._begin_player_turn(self.state.active_player))
        self.log.emit("begin_turn", player=self.state.active_player, turn=self.state.turn_number)
        return Result("end_turn", events, f"{self.state.active_player} to act")

    def _check_victory(self, events: list[dict]) -> None:
        """Game ends when only one side has non-captive, non-demoralized figures
        (P4-R36)."""
        if self.state.ended:
            return
        sides = {}
        for f in self.state.figures.values():
            if f.is_alive and not f.captured and not f.is_demoralized:
                sides.setdefault(f.owner, 0)
                sides[f.owner] += 1
        alive_sides = [o for o, n in sides.items() if n > 0]
        if len(alive_sides) <= 1:
            self.state.ended = True
            self.state.winner = alive_sides[0] if alive_sides else None
            events.append(
                self.log.emit("game_over", winner=self.state.winner)
            )

    # ------------------------------------------------------------------ #
    # Victory-point scoring (P4-R37, simplified for v1: elimination + survival)
    # ------------------------------------------------------------------ #
    def victory_points(self) -> dict[str, int]:
        players = {"human": 0, "llm": 0}
        # A player earns survival points only if they still have at least one
        # fighting figure; if all their figures are captured/demoralized, they
        # score zero survival points (P4-R37).
        fighting = {
            f.owner
            for f in self.state.figures.values()
            if f.is_alive and not f.is_demoralized and not f.captured
        }
        for f in self.state.figures.values():
            if f.eliminated:
                # Eliminated opposing figure => its points to the other side.
                other = self.state.other_player(f.owner)
                players[other] = players.get(other, 0) + f.points
            elif f.is_alive and f.owner in fighting:
                # Surviving figure => its points to its own side (survival VP).
                players[f.owner] = players.get(f.owner, 0) + f.points
        return players
