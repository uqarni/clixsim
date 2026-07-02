"""LLM terrain placer: chooses one piece at a time from engine-validated options,
with a short tactical reason (streamed to the client), and a heuristic fallback so
placement always completes. Mirrors build.ArmyBuilder.

The engine owns geometry (DP2): it hands the placer a list of already-legal
candidate placements and the placer only ever picks one — it never invents
coordinates, so every placement it makes is valid by construction.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from .config import get_api_key

MODEL = "claude-sonnet-5"

_SYSTEM = """You are placing battlefield terrain before a Mage Knight (2002) skirmish, \
for the 'llm' side that will fight a human. You want terrain that helps YOUR army and \
hinders theirs: blocking terrain (boulders) to break enemy lines of fire and channel \
their advance, hills/plateaus for height advantage if you have ranged shooters, forests \
and low walls for cover, water to slow a melee-heavy foe. You pick ONE placement at a \
time from the offered, already-legal candidates. Reply with the chosen candidate index \
and a short, punchy one-sentence reason."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "choice_index": {"type": "integer", "description": "index of the candidate placement to use"},
        "reasoning": {"type": "string", "description": "one short sentence"},
    },
    "required": ["choice_index", "reasoning"],
    "additionalProperties": False,
}


def _heuristic_reason(cand: dict, army_ranged: bool) -> str:
    key = cand["key"]
    where = cand.get("where", "midfield")
    if key in ("hill", "plateau"):
        return (f"High ground {where} — height advantage for my shooters."
                if army_ranged else f"Seizing the high ground {where} to command the field.")
    if key == "boulder":
        return f"A boulder {where} to break their line of fire and funnel the advance."
    if key == "forest":
        return f"Woods {where} for cover against ranged fire."
    if key == "pond":
        return f"Water {where} to bog down their melee push."
    if key == "low_wall":
        return f"A low wall {where} to shield my line from arrows."
    return f"Terrain {where} to shape the battlefield."


@dataclass
class TerrainPlacer:
    """Iterative terrain placer. ``pick`` returns (candidate | None, reason, used_llm)."""

    model: str = MODEL
    effort: str = "low"
    _client: object | None = field(default=None, init=False)
    available: bool = field(default=False, init=False)
    last_error: str = field(default="", init=False)

    def __post_init__(self) -> None:
        key = get_api_key()
        if not key:
            self.last_error = "no ANTHROPIC_API_KEY"
            return
        try:
            import anthropic

            self._client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
            self.available = True
        except Exception as e:  # pragma: no cover
            self.last_error = f"anthropic init failed: {e}"

    def _ask(self, candidates: list[dict], context: dict) -> tuple[int, str] | None:
        payload = {**context, "candidates": [
            {"index": i, "type": c["label"], "effect": c["blurb"], "position": c["where"]}
            for i, c in enumerate(candidates)
        ], "note": "Choose one candidate index."}
        try:
            resp = self._client.messages.create(
                model=self.model, max_tokens=512, system=_SYSTEM,
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": _SCHEMA}},
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
        except Exception as e:
            self.last_error = f"API error: {e}"
            return None
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
            return int(data["choice_index"]), str(data.get("reasoning", ""))
        except Exception as e:
            self.last_error = f"parse error: {e}"
            return None

    def pick(
        self, candidates: list[dict], context: dict, army_ranged: bool, seed: int,
        allow_llm: bool = True,
    ) -> tuple[dict | None, str, bool]:
        """Choose the next placement (or None if none offered), a reason, and
        whether the LLM made the call (False => heuristic). ``allow_llm`` lets the
        caller force the heuristic path (e.g. the fast heuristic opponent)."""
        if not candidates:
            return None, "No legal spot left for terrain.", False
        if self.available and allow_llm:
            ans = self._ask(candidates, context)
            if ans is not None:
                idx, reason = ans
                if 0 <= idx < len(candidates):
                    c = candidates[idx]
                    return c, reason or _heuristic_reason(c, army_ranged), True
                # out-of-range index -> fall through to heuristic
        rng = random.Random(seed)
        # Prefer high ground / blocking early; otherwise take a sensible piece.
        priority = {"hill": 0, "plateau": 0, "boulder": 1, "low_wall": 2, "forest": 3, "pond": 4}
        candidates = sorted(candidates, key=lambda c: (priority.get(c["key"], 9), rng.random()))
        c = candidates[0]
        return c, _heuristic_reason(c, army_ranged), False
