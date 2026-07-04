"""Structured board snapshot for the LLM (X3 — the LLM interface contract).

A stable "board snapshot + annotated legal moves" payload is the *only* thing the
opponent reasons over. Everything numeric here is engine-computed.
"""

from __future__ import annotations

import math

from .engine import Engine
from .threat import clicks_to_demoralized, figure_threat_brief, remaining_clicks
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
        # Mounted (P5): double base, no free spin, break-away fails only on 1,
        # Shake Off on successful break away. The LLM prompt explains the rules.
        **({"mounted": True} if f.mounted else {}),
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
        # Dial futures (plan 1.8): the human client renders whole dials; the AI
        # used to see only the current click — it called a 2-click figure
        # "healthy" right before the game-ending self-push.
        "remaining_clicks": remaining_clicks(f),
        "clicks_to_demoralized": clicks_to_demoralized(f),
        "next_clicks": _dial_future(f),
    }


def _dial_future(f, n: int = 2) -> list[dict]:
    """Stats of the next couple of clicks — kill thresholds and stat cliffs."""
    out = []
    dial = f.definition.dial
    for i in range(f.current_click + 1, min(f.current_click + 1 + n, f.definition.num_live_clicks)):
        cs = dial[i]
        out.append({"speed": cs.speed, "attack": cs.attack, "defense": cs.defense,
                    "damage": cs.damage,
                    "abilities": [a.name for a in cs.abilities]})
    return out


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
    figures = []
    for f in state.living():
        fv = _figure_view(engine, f)
        if f.owner == state.active_player:
            # Engine-computed danger facts for the side about to act (plan 1.8):
            # the prompt forbids the model from doing geometry, so the engine
            # must SAY who is in how much trouble where it stands.
            fv["threats"] = figure_threat_brief(engine, f)
        figures.append(fv)
    return {
        "turn": state.turn_number,
        "active_player": state.active_player,
        "actions_per_turn": state.actions_per_turn(),
        # Engine truth — counting _acted_uids undercounts after a formation
        # action (5 members token, 1 action spent; the old math reported -3).
        "actions_remaining": engine._actions_remaining(),
        "board": {"width": state.board.width, "height": state.board.height},
        "figures": figures,
        "terrain": [_terrain_brief(t) for t in state.terrain],
        "ability_coverage": engine.ability_coverage(),
    }
