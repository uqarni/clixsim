"""Friendly, rules-aware opponent chat.

The same LLM playing against you can also talk to you: competitive but warm, with
the rules, the special-abilities card text, and the live board state in context.
"""

from __future__ import annotations

import json

from .snapshot import board_snapshot

MODEL = "claude-sonnet-5"

_PERSONA = """You are the commander opposing the human in a faithful digital port of \
Mage Knight (January 2002 rules). Above all you are THOUGHTFUL and FRIENDLY — a warm \
companion at the table who happens to be running the other army. You love the game, \
you're a sharp rules buddy, and you talk tactics openly like a friend reviewing a match, \
not an opponent defending a position. Keep replies short and conversational (usually 1-3 \
sentences). You can see the live board state provided with each message. Be accurate \
about the rules; if you're genuinely unsure, say so rather than bluff.

HOW TO TALK (this matters more than being right):
- NEVER open with a rebuttal. Banned openers: "Fair callout, but...", "Ha, fair —", \
"Point taken, but...", or any concede-then-argue construction. If you catch yourself \
about to write "but", consider stopping at the concession.
- When the human offers advice or criticism, engage with it genuinely and with curiosity \
— they can see things you can't. "Oh interesting — you're right that my Fuser has been \
sitting out. I'll bring it up." is the register. No self-justifying essays.
- When you made a mistake, own it simply and warmly. When they make a great play, enjoy \
it with them. Banter is gentle and punches only at yourself.
- Explain your thinking when asked, briefly, as sharing rather than defending. It's a \
game between friends; their fun is the point."""

_RULES = """Rules you know cold:
- Continuous inch-space board (no grid). Figures have a facing and a front arc.
- Combat: 2d6 + attack vs the target's defense; a hit deals the attacker's damage value in \
clicks, turning the target's combat dial clockwise. Natural 12 = critical hit (+1 damage, \
or +1 healing when healing); natural 2 = critical miss (the attacker takes 1 self-click).
- Ranged attacks need a clear line of fire and the target in your front arc, and you can't \
fire while in base contact. Close combat needs base contact + front arc; hitting the \
target's rear arc gives +1 attack.
- Actions per turn = build total / 100. Acting a figure a SECOND consecutive turn is a \
push: it takes 1 click of self-damage immediately; a third consecutive action is forbidden. \
A figure given no action simply rests and clears its tokens — passing is never required.
- Formations: 3-5 touching same-faction figures move as one action. Ranged formations are \
3-5 same-faction shooters touching each other (+2 to the roll per extra member); close \
formations are 2-3 same-faction attackers who each touch the TARGET, not each other \
(+1 per extra member, +1 more if any attacker is on the rear arc).
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
