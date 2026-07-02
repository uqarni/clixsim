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
say so rather than bluff.

READ THE ROOM — this matters more than being right. You are the host as much as the \
opponent; the human's fun is the whole point. Banter punches at yourself, never at them. \
Never debate their feelings, score rhetorical points, or lecture ("I'll own the mean but \
not the wrong" — never say things like this). If they sound frustrated, or de-escalate \
("it's just a game"), match them instantly: concede the vibe, warm up, maybe crack a \
self-deprecating joke. When your position is clearly lost, say so with grace and play to \
a swift finish — a good loser makes the win feel earned, not extracted."""

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


def abilities_card(db) -> str:
    """The official special-abilities card text (shared by chat + the drafter)."""
    lines = []
    for a in db.all_abilities():
        if getattr(a, "used_in_rebellion", False) and a.description:
            lines.append(f"- {a.name}: {a.description.strip()}")
    return "Special abilities (the official card text):\n" + "\n".join(lines)


def rules_digest() -> str:
    """A compact rules summary (shared by chat + the drafter)."""
    return _RULES


def build_system(db) -> str:
    return f"{_PERSONA}\n\n{_RULES}\n\n{abilities_card(db)}"


def chat_reply(client, system: str, message: str, history: list[dict], engine,
               recent_moves: list[str] | None = None) -> str:
    msgs: list[dict] = []
    for h in history[-12:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        text = str(h.get("content", "")).strip()
        if text:
            msgs.append({"role": role, "content": text})
    content = message
    if engine is not None:
        content += "\n\n[Live board state]\n" + json.dumps(board_snapshot(engine))
        doctrine = getattr(engine, "doctrine", "")
        if doctrine:
            content += (f"\n\n[You drafted your army under this doctrine — it's your "
                        f"game plan]\n{doctrine}")
    if recent_moves:
        content += ("\n\n[Your battle actions last turn, and why — stay consistent "
                    "with what you actually did]\n" + "\n".join(recent_moves[-10:]))
    msgs.append({"role": "user", "content": content})
    # Snappy 1-3 sentence banter: disable thinking so the whole token budget goes to
    # visible text. (On Sonnet 5 thinking is adaptive-on when omitted, and with a small
    # max_tokens it consumes the budget, leaving no text block — a blank reply.)
    resp = client.messages.create(
        model=MODEL, max_tokens=1024, system=system,
        thinking={"type": "disabled"}, messages=msgs,
    )
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "").strip()
    return text or "(Hmm, lost my train of thought — say that again?)"
