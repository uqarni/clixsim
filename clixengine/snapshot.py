"""Structured board snapshot for the LLM (X3 — the LLM interface contract).

A stable "board snapshot + annotated legal moves" payload is the *only* thing the
opponent reasons over. Everything numeric here is engine-computed.
"""

from __future__ import annotations

import math

from .engine import Engine
from .state import Figure


def _figure_view(engine: Engine, f: Figure) -> dict:
    abils = []
    for aid in sorted(f.active_ability_ids()):
        a = engine.db.ability(aid)
        abils.append(a.name if a else str(aid))
    contacts = [
        c.uid for c in engine.state.in_base_contact_with(f, engine.state.living())
    ]
    return {
        "uid": f.uid,
        "name": f.short_name,
        "owner": f.owner,
        "faction": f.definition.faction,
        "points": f.points,
        "pos": [round(f.position.x, 2), round(f.position.y, 2)],
        "facing_deg": round(math.degrees(f.facing) % 360, 1),
        "click": f.current_click,
        "health_fraction": round(f.health_fraction(), 2),
        "speed": f.speed,
        "attack": f.attack,
        "defense": f.defense,
        "damage": f.damage,
        "range": f.range,
        "targets": f.targets,
        "active_abilities": abils,
        "push_tokens": f.action_tokens,
        "in_base_contact_with": contacts,
    }


def _terrain_brief(t) -> dict:
    """Compact terrain fact for the LLM: type + where + rough size (the engine
    already folds exact terrain geometry into every candidate's odds)."""
    cx = sum(v.x for v in t.polygon) / len(t.polygon)
    cy = sum(v.y for v in t.polygon) / len(t.polygon)
    radius = max(math.hypot(v.x - cx, v.y - cy) for v in t.polygon)
    kind = ("deep water" if t.water == "deep" else "shallow water" if t.water == "shallow"
            else "low wall" if t.low_wall else "elevated" if t.elevated else t.kind)
    return {"type": kind, "center": [round(cx, 1), round(cy, 1)], "radius": round(radius, 1)}


def board_snapshot(engine: Engine) -> dict:
    state = engine.state
    return {
        "turn": state.turn_number,
        "active_player": state.active_player,
        "actions_per_turn": state.actions_per_turn(),
        "actions_remaining": state.actions_per_turn() - len(engine._acted_uids),
        "board": {"width": state.board.width, "height": state.board.height},
        "figures": [_figure_view(engine, f) for f in state.living()],
        "terrain": [_terrain_brief(t) for t in state.terrain],
        "ability_coverage": engine.ability_coverage(),
    }
