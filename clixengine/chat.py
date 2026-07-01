"""Friendly, rules-aware opponent chat.

The same LLM playing against you can also talk to you: competitive but warm, with
the rules, the special-abilities card text, and the live board state in context.
"""

from __future__ import annotations

import json

from .snapshot import board_snapshot

MODEL = "claude-sonnet-5"

_PERSONA = """You are the commander opposing the human in a faithful digital port of \
Mage Knight (January 2002 rules). You are genuinely competitive and playing to win — but \
you're also a warm, encouraging companion and a sharp rules buddy. Happily explain rules, \
talk tactics, react to the board, throw friendly banter, and congratulate good plays. \
Keep replies short and conversational (usually 1-3 sentences). You can see the live board \
state provided with each message. Be accurate about the rules; if you're genuinely unsure, \
say so rather than bluff."""

_RULES = """Rules you know cold:
- Continuous inch-space board (no grid). Figures have a facing and a front arc.
- Combat: 2d6 + attack vs the target's defense; a hit deals the attacker's damage value in \
clicks, turning the target's combat dial clockwise. Natural 12 = critical hit (+1 damage, \
or +1 healing when healing); natural 2 = critical miss (the attacker takes 1 self-click).
- Ranged attacks need a clear line of fire and the target in your front arc, and you can't \
fire while in base contact. Close combat needs base contact + front arc; hitting the \
target's rear arc gives +1 attack.
- Actions per turn = build total / 100. Acting a figure on consecutive turns adds push \
tokens; pushing a third turn costs self-damage. Passing rests a figure and clears tokens.
- Formations: 3-5 touching same-faction figures move as one action; combat formations pool \
an attack (+2 per extra ranged member; +1 per extra close member; +1 for a rear attacker).
- A figure is eliminated on 3 skulls. You win by eliminating or demoralizing every enemy \
figure. Optional abilities may be switched off until end of turn."""


def build_system(db) -> str:
    lines = []
    for a in db.all_abilities():
        if getattr(a, "used_in_rebellion", False) and a.description:
            lines.append(f"- {a.name}: {a.description.strip()}")
    abilities = "Special abilities (the official card text):\n" + "\n".join(lines)
    return f"{_PERSONA}\n\n{_RULES}\n\n{abilities}"


def chat_reply(client, system: str, message: str, history: list[dict], engine) -> str:
    msgs: list[dict] = []
    for h in history[-12:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        text = str(h.get("content", "")).strip()
        if text:
            msgs.append({"role": role, "content": text})
    content = message
    if engine is not None:
        content += "\n\n[Live board state]\n" + json.dumps(board_snapshot(engine))
    msgs.append({"role": "user", "content": content})
    resp = client.messages.create(model=MODEL, max_tokens=400, system=system, messages=msgs)
    return next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
