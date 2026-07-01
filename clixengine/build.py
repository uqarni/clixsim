"""Army construction: sealed-pool sampling + an LLM army builder that picks
figures one at a time with reasoning (streamed to the client), plus a heuristic
fallback so construction always completes.

Preconstructed: build from the whole roster up to a points cap.
Sealed: open boosters into a pool (rarity-weighted; non-canonical — OQ-3) and
build from what was pulled, capped at 200 pts.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from . import abilities as ab
from .army import Army
from .config import get_api_key
from .data import FigureDB, FigureDef

MODEL = "claude-sonnet-5"


def _role(f: FigureDef) -> str:
    return "ranged" if f.is_ranged else "melee"


def _top_abilities(db: FigureDB, f: FigureDef) -> list[str]:
    names = []
    for aid in sorted(f.all_ability_ids()):
        if aid in ab.IMPLEMENTED_ABILITY_IDS:
            a = db.ability(aid)
            if a:
                names.append(a.name)
    return names[:4]


def _fig_brief(db: FigureDB, f: FigureDef) -> dict:
    return {
        "id": f.id,
        "name": f.short_name,
        "faction": f.faction,
        "points": f.points,
        "role": _role(f),
        "rank": f.rank,
        "abilities": _top_abilities(db, f),
    }


# --------------------------------------------------------------------------- #
# Sealed pool sampling (non-canonical approximation, OQ-3)
# --------------------------------------------------------------------------- #
def sample_sealed_pool(db: FigureDB, seed: int, boosters: int = 4, per_booster: int = 5) -> list[int]:
    """Open ``boosters`` packs of ``per_booster`` figures, weighted by rarity
    (rarity 1 = common .. 6 = rare). Returns figure ids (with duplicates)."""
    rng = random.Random(seed)
    figs = db.all_figures()
    weights = [max(1, 7 - int(f.rarity or 3)) for f in figs]
    pool: list[int] = []
    for _ in range(boosters * per_booster):
        pool.append(rng.choices(figs, weights=weights, k=1)[0].id)
    return pool


# --------------------------------------------------------------------------- #
# Candidate filtering
# --------------------------------------------------------------------------- #
def _affordable(
    db: FigureDB,
    candidate_ids: list[int] | None,
    remaining: int,
    used_uniques: set[int],
    pool_counts: dict[int, int] | None,
) -> list[FigureDef]:
    """Figures that fit the remaining budget and aren't an already-used unique.
    ``candidate_ids`` None => whole roster (preconstructed). ``pool_counts`` limits
    sealed picks to remaining pulled copies."""
    if candidate_ids is None:
        ids = [f.id for f in db.all_figures()]
    else:
        ids = sorted(set(candidate_ids))
    out = []
    for fid in ids:
        f = db.get(fid)
        if f.points > remaining:
            continue
        if f.is_unique and f.id in used_uniques:
            continue
        if pool_counts is not None and pool_counts.get(fid, 0) <= 0:
            continue
        out.append(f)
    out.sort(key=lambda f: (-f.points, f.short_name))
    return out


# --------------------------------------------------------------------------- #
# Heuristic builder (fallback + human side)
# --------------------------------------------------------------------------- #
def heuristic_army(
    db: FigureDB, owner: str, budget: int, seed: int,
    candidate_ids: list[int] | None = None,
) -> Army:
    """Fill the budget alternating ranged/melee, priciest-that-fits; respects
    uniques and (for sealed) pulled-copy counts."""
    rng = random.Random(seed)
    pool_counts: dict[int, int] | None = None
    if candidate_ids is not None:
        pool_counts = {}
        for fid in candidate_ids:
            pool_counts[fid] = pool_counts.get(fid, 0) + 1
    ids: list[int] = []
    used_uniques: set[int] = set()
    remaining = budget
    want_ranged = True
    max_size = max(2, budget // 40)
    while len(ids) < max_size:
        cands = _affordable(db, candidate_ids, remaining, used_uniques, pool_counts)
        if not cands:
            break
        prefer = [f for f in cands if (_role(f) == "ranged") == want_ranged] or cands
        # a little variety among the top few
        top = prefer[: min(3, len(prefer))]
        pick = rng.choice(top)
        ids.append(pick.id)
        remaining -= pick.points
        if pick.is_unique:
            used_uniques.add(pick.id)
        if pool_counts is not None:
            pool_counts[pick.id] -= 1
        want_ranged = not want_ranged
    if not ids:  # guarantee non-empty
        cheapest = min(db.all_figures(), key=lambda f: f.points)
        ids = [cheapest.id]
    return Army(name=f"{owner}-army", owner=owner, figure_ids=ids)


# --------------------------------------------------------------------------- #
# LLM builder — picks one figure at a time, with reasoning
# --------------------------------------------------------------------------- #
_SYSTEM = """You are drafting a Mage Knight (2002) army for the 'llm' side to fight \
a human. Build a synergistic, competitive force within the points budget: mix \
ranged and melee, value strong abilities (Command for extra actions, Toughness \
and Battle Armor for durability, Flight for mobility, Magic Blast/Enhancement for \
punch), and don't leave large points unspent. You pick ONE figure at a time from \
the offered candidates. You may take the same non-unique more than once. Reply \
with the chosen candidate id and a short, punchy one-sentence reason, or -1 to \
stop when the army is strong and the budget is nearly spent."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "choice_id": {"type": "integer", "description": "candidate figure id to add, or -1 to stop"},
        "reasoning": {"type": "string", "description": "one short sentence"},
    },
    "required": ["choice_id", "reasoning"],
    "additionalProperties": False,
}


@dataclass
class ArmyBuilder:
    """Iterative army builder. ``pick`` returns (figure_def | None, reasoning, used_llm)."""

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

            self._client = anthropic.Anthropic(api_key=key)
            self.available = True
        except Exception as e:  # pragma: no cover
            self.last_error = f"anthropic init failed: {e}"

    def _ask(self, db: FigureDB, cands: list[FigureDef], army_brief: list[dict],
             remaining: int, budget: int) -> tuple[int | None, str] | None:
        payload = {
            "budget": budget,
            "remaining": remaining,
            "current_army": army_brief,
            "candidates": [_fig_brief(db, f) for f in cands],
            "note": "Choose one candidate id to add, or -1 to stop.",
        }
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
            return int(data["choice_id"]), str(data.get("reasoning", ""))
        except Exception as e:
            self.last_error = f"parse error: {e}"
            return None

    def pick(self, db: FigureDB, cands: list[FigureDef], army_brief: list[dict],
             remaining: int, budget: int, seed: int) -> tuple[FigureDef | None, str, bool]:
        """Return the next figure to add (or None to stop), a reasoning string, and
        whether the LLM made the call (False => heuristic fallback)."""
        if self.available:
            ans = self._ask(db, cands, army_brief, remaining, budget)
            if ans is not None:
                cid, reason = ans
                if cid == -1:
                    return None, reason or "Army is set.", True
                match = next((f for f in cands if f.id == cid), None)
                if match is not None:
                    return match, reason or f"Adds {match.short_name}.", True
                # invalid id -> fall through to heuristic
        # Heuristic fallback: priciest affordable, roughly alternating role.
        rng = random.Random(seed)
        if not cands:
            return None, "No affordable figures left.", False
        top = cands[: min(3, len(cands))]
        pick = rng.choice(top)
        return pick, f"Solid {_role(pick)} pick at {pick.points} pts.", False
