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


_FORMATION_BARRED = {ab.FLIGHT, ab.AQUATIC, ab.QUICKNESS}


def _formation_capable(f: FigureDef) -> bool:
    """Can this figure join a movement formation? (P4-R12: Mage Spawn never
    join formations; Flight/Aquatic/Quickness bar movement formations.)"""
    if f.faction == "Mage Spawn":
        return False
    return not (_FORMATION_BARRED & f.all_ability_ids())


def _top_abilities(db: FigureDB, f: FigureDef) -> list[str]:
    names = []
    for aid in sorted(f.all_ability_ids()):
        if aid in ab.IMPLEMENTED_ABILITY_IDS:
            a = db.ability(aid)
            if a:
                names.append(a.name)
    return names[:4]


def _fig_brief(db: FigureDB, f: FigureDef) -> dict:
    cs = f.dial[f.starting_click]
    return {
        "id": f.id,
        "name": f.short_name,
        "faction": f.faction,
        "points": f.points,
        "role": _role(f),
        "rank": f.rank,          # Weak | Standard | Tough | Unique
        "rarity": f.rarity,      # "1".."6"
        "unique": f.is_unique,
        # Movement formations need grounded, non-Mage-Spawn figures — the
        # drafter used to justify picks with formation plans the engine forbids.
        "formation_capable": _formation_capable(f),
        "abilities": _top_abilities(db, f),
        # starting-click stats + printed range so the drafter can compare figures
        "stats": {
            "speed": cs.speed, "attack": cs.attack, "defense": cs.defense,
            "damage": cs.damage, "range": f.range, "targets": f.targets,
        },
        "clicks": f.num_live_clicks,
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
        # Formation-aware: once a faction is chosen, stick to it while options
        # exist — 3-5 same-faction figures unlock movement/ranged formations.
        if ids:
            used_factions = {db.get(i).faction for i in ids}
            same = [f for f in cands if f.faction in used_factions and f.faction != "Mage Spawn"]
            if same:
                cands = same
        # ...and prefer figures that can actually join one (Flight/Aquatic/
        # Quickness bar movement formations; Mage Spawn bar all formations).
        grounded = [f for f in cands if _formation_capable(f)]
        if grounded:
            cands = grounded
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
    # Top-up: the faction lock can strand budget when faction-mates run out
    # (common in sealed pools, where the pulled copies of a faction are few).
    # Relax it and spend the remainder on the priciest affordable figures.
    while len(ids) < max(3, budget // 25):
        cands = _affordable(db, candidate_ids, remaining, used_uniques, pool_counts)
        if not cands:
            break
        pick = rng.choice(cands[: min(3, len(cands))])
        ids.append(pick.id)
        remaining -= pick.points
        if pick.is_unique:
            used_uniques.add(pick.id)
        if pool_counts is not None:
            pool_counts[pick.id] -= 1
    if not ids:  # guarantee non-empty
        cheapest = min(db.all_figures(), key=lambda f: f.points)
        ids = [cheapest.id]
    return Army(name=f"{owner}-army", owner=owner, figure_ids=ids)


# --------------------------------------------------------------------------- #
# LLM builder — picks one figure at a time, with reasoning
# --------------------------------------------------------------------------- #
_SYSTEM = """You are drafting a Mage Knight (2002) army for the 'llm' side to fight \
a human. Build a synergistic, competitive force within the points budget and don't \
leave large points unspent. You pick ONE figure at a time from the offered \
candidates (each comes with its starting stats, rank, and abilities — the official \
ability card text is below). You may take the same non-unique more than once. Reply \
with the chosen candidate id and a short, punchy one-sentence reason, or -1 to \
stop when the army is strong and the budget is nearly spent.

FORMATIONS ARE A CORE LEVER — draft for them: a movement formation is 3-5 \
SAME-FACTION figures moving as ONE action (huge action economy), and a ranged \
formation of same-faction shooters adds +2 to the roll per extra member. A \
faction-salad army can never form one. Concentrate most of your points in ONE \
faction (two at most), aiming for at least 3-4 figures of it. Caveats: figures \
with Flight/Aquatic/Quickness cannot join MOVEMENT formations (fine as loners), \
and Mage Spawn can never join any formation.

BUT a formation is 3-5 figures, NOT the whole army — buying the same cheap \
figure six or more times wastes the bonus (only 5 can act together) and makes \
you predictable and fragile. Army shape that wins: ONE cohesive 3-5 figure \
same-faction block + one or two expensive heavy hitters + support (a healer, \
melee to screen shooters). Prefer at most ~3 copies of any single figure \
unless you are deliberately drafting a horde.

SPEND THE BUDGET. An army that leaves points on the table is strictly weaker — \
keep picking until fewer than ~10 points remain. In big-points games, that \
means BIG pieces, not more copies of the cheapest figure.

DRAFT A HEALER when the pool offers one (Healing/Magic Healing/Necromancy): \
every archived attrition loss traces to the enemy repairing damage the army \
could not. Candidates carry "formation_capable" — only plan formations around \
figures where it is true."""

# A per-game drafting doctrine keeps armies varied across games (the model
# otherwise converges on the same "best" picks every time).
DOCTRINES = (
    "Elite few: a handful of expensive, hard-hitting figures. Quality over numbers.",
    "Horde: as many cheap figures as the budget allows — win on action economy and bodies.",
    "Gunline: maximize ranged attackers and keep them in a mutually-supporting block.",
    "Wings: prioritize Flight and high speed — mobility, flanking, and rear-arc strikes.",
    "Anvil: durability first (Toughness, Battle Armor, deep dials) — grind the enemy down.",
    "Synergy: build around ability combos — Command for actions, Magic Enhancement behind "
    "shooters, Defend to share a high defense, healers to sustain.",
    "Combined arms: a balanced core of melee bruisers screening ranged support.",
    "Glass cannons: maximum damage output per point, defense be damned.",
    "Phalanx: a single-faction block of 3-5 figures that marches as one movement "
    "formation and pools its attacks — cohesion above all.",
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "choice_id": {"type": "integer", "description": "candidate figure id to add, or -1 to stop"},
        "reasoning": {"type": "string", "description": "one short sentence"},
    },
    "required": ["choice_id", "reasoning"],
    "additionalProperties": False,
}

# Up-front planning pass (user request 2026-07-03): take stock of the WHOLE pool,
# reason, commit to a formation + strategy, THEN pick one figure at a time to
# execute it — instead of greedy myopic picks that spammed the same cheap unit.
_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "strategy": {"type": "string",
                     "description": "2-4 sentences: the whole game plan for this army"},
        "primary_faction": {"type": "string",
                            "description": "the ONE faction to concentrate for a formation"},
        "formation_plan": {"type": "string",
                           "description": "which formation (movement/ranged/close) and from which figures"},
        "must_grab": {"type": "array", "items": {"type": "string"},
                      "description": "figure names to prioritize"},
    },
    "required": ["strategy", "primary_faction", "formation_plan", "must_grab"],
    "additionalProperties": False,
}

_HEAL_IDS = {ab.HEALING, ab.MAGIC_HEALING, ab.NECROMANCY}


def _planning_digest(db: FigureDB, figs: list[FigureDef]) -> list[dict]:
    """Compact by-faction stock-take of the available pool for the planning
    pass: how many formation-capable figures each faction has, whether it holds
    a healer, and its strongest few pieces."""
    by_fac: dict[str, list[FigureDef]] = {}
    for f in figs:
        by_fac.setdefault(f.faction, []).append(f)
    out = []
    for fac, members in sorted(by_fac.items(), key=lambda kv: -len(kv[1])):
        cap = [m for m in members if _formation_capable(m)]
        ranged_cap = [m for m in cap if m.is_ranged]
        top = sorted(members, key=lambda m: -m.points)[:4]
        out.append({
            "faction": fac,
            "distinct_figures": len(members),
            "formation_capable": len(cap),
            "ranged_formation_capable": len(ranged_cap),
            "has_healer": any(m.all_ability_ids() & _HEAL_IDS for m in members),
            "top_pieces": [{"name": m.short_name, "points": m.points, "role": _role(m)}
                           for m in top],
        })
    return out


def _heuristic_plan(db: FigureDB, figs: list[FigureDef], doctrine: str) -> dict:
    """Deterministic plan for the key-less / heuristic path: concentrate on the
    faction with the most formation-capable figures."""
    digest = _planning_digest(db, figs)
    formable = [d for d in digest if d["formation_capable"] >= 3] or digest
    if not formable:
        return {}
    best = max(formable, key=lambda d: (d["formation_capable"], d["distinct_figures"]))
    kind = "ranged" if best["ranged_formation_capable"] >= 3 else "movement"
    return {
        "strategy": f"Concentrate on {best['faction']} for a {kind} formation, add a "
                    f"healer if available, and round out with the strongest affordable pieces.",
        "primary_faction": best["faction"],
        "formation_plan": f"{max(3, min(5, best['formation_capable']))} {best['faction']} "
                          f"figures for a {kind} formation",
        "must_grab": [p["name"] for p in best["top_pieces"][:2]],
    }


@dataclass
class ArmyBuilder:
    """Iterative army builder. ``pick`` returns (figure_def | None, reasoning, used_llm).
    ``seed`` selects a per-game drafting doctrine so armies vary between games."""

    model: str = MODEL
    effort: str = "low"
    seed: int = 0
    _client: object | None = field(default=None, init=False)
    available: bool = field(default=False, init=False)
    last_error: str = field(default="", init=False)
    doctrine: str = field(default="", init=False)
    plan: dict = field(default_factory=dict, init=False)  # the up-front strategy

    def __post_init__(self) -> None:
        self.doctrine = random.Random(self.seed).choice(DOCTRINES)
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

    def system_prompt(self, db: FigureDB) -> str:
        """Base directive + this game's doctrine + rules digest + the ability card."""
        from .chat import abilities_card, rules_digest

        return (
            f"{_SYSTEM}\n\nYour drafting doctrine this game (lean into it, even at "
            f"some cost): {self.doctrine}\n\n{rules_digest()}\n\n{abilities_card(db)}"
        )

    def make_plan(self, db: FigureDB, available: list[FigureDef], budget: int) -> dict:
        """Take stock of the ENTIRE available pool and commit to a formation +
        strategy BEFORE drafting figure-by-figure (user request). Sets and
        returns self.plan; falls back to a deterministic plan without an API."""
        if not self.available:
            self.plan = _heuristic_plan(db, available, self.doctrine)
            return self.plan
        payload = {
            "budget": budget,
            "doctrine": self.doctrine,
            "pool_by_faction": _planning_digest(db, available),
            "task": "Take stock of the ENTIRE pool above BEFORE drafting. Decide: "
                    "which ONE faction to concentrate for a formation (need 3-5 "
                    "same-faction figures), what formation type and from which "
                    "pieces, which key figures you must grab, and the overall plan. "
                    "You will then pick figures one at a time to execute THIS plan.",
        }
        try:
            resp = self._client.messages.create(
                model=self.model, max_tokens=700, system=self.system_prompt(db),
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": _PLAN_SCHEMA}},
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
            self.plan = json.loads(text)
        except Exception as e:
            self.last_error = f"plan error: {e}"
            self.plan = _heuristic_plan(db, available, self.doctrine)
        return self.plan

    def _ask(self, db: FigureDB, cands: list[FigureDef], army_brief: list[dict],
             remaining: int, budget: int, seed: int = 0) -> tuple[int | None, str] | None:
        # Shuffle the presentation so the priciest-first ordering doesn't anchor
        # the model to the same opening pick every game.
        briefs = [_fig_brief(db, f) for f in cands]
        random.Random(seed).shuffle(briefs)
        payload = {
            "budget": budget,
            "remaining": remaining,
            "current_army": army_brief,
            "candidates": briefs,
            "note": "Choose one candidate id to add, or -1 to stop.",
        }
        if getattr(self, "plan", None):
            # Every pick executes the up-front plan — this is what keeps the
            # draft coherent instead of greedy-and-myopic.
            payload["your_plan"] = self.plan
        try:
            resp = self._client.messages.create(
                model=self.model, max_tokens=512, system=self.system_prompt(db),
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
            ans = self._ask(db, cands, army_brief, remaining, budget, seed)
            if ans is not None:
                cid, reason = ans
                if cid == -1:
                    return None, reason or "Army is set.", True
                match = next((f for f in cands if f.id == cid), None)
                if match is not None:
                    return match, reason or f"Adds {match.short_name}.", True
                # invalid id -> fall through to heuristic
        # Heuristic fallback: formation-aware, like heuristic_army — stick to
        # factions already drafted and prefer formation-capable figures, so
        # key-less/heuristic games also field armies that can form up.
        rng = random.Random(seed)
        if not cands:
            return None, "No affordable figures left.", False
        pool = cands
        used_factions = {b.get("faction") for b in army_brief} - {"Mage Spawn", None}
        # Steer the first pick toward the plan's primary faction so the fallback
        # executes the same strategy the plan committed to.
        _plan = getattr(self, "plan", None) or {}
        if not used_factions and _plan.get("primary_faction"):
            used_factions = {_plan["primary_faction"]}
        if used_factions:
            same = [f for f in pool if f.faction in used_factions]
            if same:
                pool = same
        grounded = [f for f in pool if _formation_capable(f)]
        if grounded:
            pool = grounded
        top = pool[: min(3, len(pool))]
        pick = rng.choice(top)
        return pick, f"Solid {_role(pick)} pick at {pick.points} pts.", False
