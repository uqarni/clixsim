"""Rich client-facing view payload (the renderer's read model).

``board_snapshot`` (snapshot.py) is the lean, living-only payload the LLM reasons
over. A graphical client needs more: the FULL combat dial (all clicks — the game
is named for it), base radius and arc angle to draw bases/wedges to scale, dead
figures for the graveyard/Necromancy pool, and the per-figure turn flags. This
module assembles that read-only view. The engine stays the single source of truth
(DP1); nothing here mutates state.
"""

from __future__ import annotations

import math

from .engine import Engine
from .state import Figure


def _abilities_named(engine: Engine, ids) -> list[dict]:
    out = []
    for aid in sorted(ids):
        a = engine.db.ability(aid)
        out.append({"id": aid, "name": a.name if a else str(aid),
                    "optional": bool(a.optional) if a else True})
    return out


def _dial_view(engine: Engine, f: Figure) -> list[dict]:
    """Every click on the figure's dial, with per-slot ability chips."""
    clicks = []
    for cs in f.definition.dial:
        clicks.append({
            "index": cs.index,
            "speed": cs.speed,
            "attack": cs.attack,
            "defense": cs.defense,
            "damage": cs.damage,
            "abilities": [
                {"id": a.id, "name": a.name, "slot": a.slot, "optional": a.optional}
                for a in cs.abilities
            ],
        })
    return clicks


def _optional_abilities(f: Figure) -> list[dict]:
    """Optional abilities on the current click, with their cancel state (P4-R34)."""
    if not f.is_alive:
        return []
    out, seen = [], set()
    for a in f.definition.dial[f.current_click].abilities:
        if a.optional and a.id not in seen:
            seen.add(a.id)
            out.append({"id": a.id, "name": a.name, "disabled": a.id in f.disabled_ability_ids})
    return out


def _terrain_view(t) -> dict:
    """A placed terrain piece as world-space polygon + rule flags (the client
    picks colours/patterns from kind/elevated/water/low_wall)."""
    return {
        "id": t.id,
        "kind": t.kind,  # "clear" | "hindering" | "blocking"
        "owner": t.owner,
        "elevated": t.elevated,
        "water": t.water,  # "shallow" | "deep" | None
        "low_wall": t.low_wall,
        "abrupt": t.abrupt,
        "polygon": [[round(v.x, 3), round(v.y, 3)] for v in t.polygon],
        "access_points": [[round(v.x, 3), round(v.y, 3)] for v in t.access_points],
    }


def terrain_template_view(tmpl) -> dict:
    """A library shape for the placement palette (origin-centred polygon)."""
    return {
        "key": tmpl.key,
        "label": tmpl.label,
        "kind": tmpl.kind,
        "elevated": tmpl.elevated,
        "water": tmpl.water,
        "low_wall": tmpl.low_wall,
        "abrupt": tmpl.abrupt,
        "blurb": tmpl.blurb,
        "polygon": [[round(v.x, 3), round(v.y, 3)] for v in tmpl.polygon],
        "access_points": [[round(v.x, 3), round(v.y, 3)] for v in tmpl.access_points],
    }


def figure_view(engine: Engine, f: Figure) -> dict:
    contacts = [c.uid for c in engine.state.in_base_contact_with(f, engine.state.living())]
    return {
        "uid": f.uid,
        "name": f.name,
        "short_name": f.short_name,
        "owner": f.owner,
        "faction": f.definition.faction,
        "points": f.points,
        "pos": [round(f.position.x, 3), round(f.position.y, 3)],
        "elevation": engine._elev(f.position) if f.is_alive else 0,
        "facing_deg": round(math.degrees(f.facing) % 360, 1),
        "base_radius": f.base_radius,
        # Double-base (P5-R1): rear circle centre is server-computed so the
        # client never re-derives it through the rounded facing above.
        "mounted": f.mounted,
        **({"rear_pos": [round(f.rear_position.x, 3), round(f.rear_position.y, 3)]}
           if f.mounted else {}),
        "arc_deg": round(math.degrees(f.arc_half_angle), 1),  # front-arc HALF-angle
        "range": f.range,
        "targets": f.targets,
        "is_ranged": f.is_ranged,
        "current_click": f.current_click,
        "starting_click": f.definition.starting_click,
        "num_live_clicks": f.definition.num_live_clicks,
        "health_fraction": round(f.health_fraction(), 3),
        "eliminated": f.eliminated,
        "demoralized": f.is_demoralized,
        "captured": f.captured,
        "action_tokens": f.action_tokens,
        "acted": f.acted_nonpass_this_turn or (f.uid in engine._acted_uids),
        "can_act": engine.can_act(f),
        # current-click convenience stats (0 when eliminated — the dial has the rest)
        "speed": f.speed if f.is_alive else 0,
        "attack": f.attack if f.is_alive else 0,
        "defense": f.defense if f.is_alive else 0,
        "damage": f.damage if f.is_alive else 0,
        "active_abilities": _abilities_named(engine, f.active_ability_ids()) if f.is_alive else [],
        "optional_abilities": _optional_abilities(f),  # toggleable (P4-R34)
        "in_base_contact_with": contacts,
        "dial": _dial_view(engine, f),
    }


def game_view(engine: Engine) -> dict:
    """The complete read model for a graphical client — ALL figures (incl. dead)."""
    state = engine.state
    spent = len(engine._acted_uids)
    remaining = engine._actions_remaining()
    return {
        "meta": {
            "game_id": getattr(engine, "game_id", ""),
            "turn": state.turn_number,
            "active_player": state.active_player,
            "first_player": state.first_player,
            "phase": state.phase,  # "terrain" (setup placement) | "battle"
            "terrain_turn": state.terrain_turn,
            "terrain_budget": dict(state.terrain_budget),
            "actions_per_turn": state.actions_per_turn(),
            "actions_spent": spent,
            "actions_remaining": remaining,
            "ended": state.ended,
            "winner": state.winner,
            "victory_points": engine.victory_points(),
            "board": {"width": state.board.width, "height": state.board.height},
            "ability_coverage": engine.ability_coverage(),
        },
        # sorted by uid for stable client diffing; includes eliminated figures
        "figures": [figure_view(engine, f) for f in sorted(state.figures.values(), key=lambda x: x.uid)],
        "terrain": [_terrain_view(t) for t in state.terrain],
    }
