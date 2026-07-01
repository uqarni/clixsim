"""Demo army construction for quick games and tests.

Builds two legal, roughly-balanced armies from the live Rebellion roster under a
given build total, mixing ranged and melee figures. Data-driven so it stays valid
as the roster changes.
"""

from __future__ import annotations

from .army import Army, validate_army
from .data import FigureDB, load_db


def _pick_army(
    db: FigureDB, owner: str, build_total: int, seed: int, faction: str | None = None
) -> Army:
    import random

    rng = random.Random(seed)
    ranged = sorted(db.filter(ranged=True, faction=faction), key=lambda f: f.points)
    melee = sorted(db.filter(ranged=False, faction=faction), key=lambda f: f.points)
    # Focus on affordable, non-trivial figures.
    lo, hi = max(5, build_total // 8), max(20, build_total // 2)
    ranged = [f for f in ranged if lo <= f.points <= hi]
    melee = [f for f in melee if lo <= f.points <= hi]
    rng.shuffle(ranged)
    rng.shuffle(melee)

    figure_ids: list[int] = []
    used_uniques: set[int] = set()
    remaining = build_total
    pools = [ranged, melee]
    turn = 0
    # Alternate ranged/melee picks, taking the priciest that still fits.
    while remaining >= lo and (ranged or melee):
        pool = pools[turn % 2]
        turn += 1
        pick = None
        for f in sorted(pool, key=lambda x: -x.points):
            if f.points > remaining:
                continue
            if f.is_unique and f.id in used_uniques:
                continue
            pick = f
            break
        if pick is None:
            # Try the other pool.
            other = pools[(turn) % 2]
            for f in sorted(other, key=lambda x: -x.points):
                if f.points <= remaining and not (f.is_unique and f.id in used_uniques):
                    pick = f
                    break
        if pick is None:
            break
        figure_ids.append(pick.id)
        remaining -= pick.points
        if pick.is_unique:
            used_uniques.add(pick.id)
        if len(figure_ids) >= max(2, build_total // 40):
            break

    return Army(name=f"{owner}-demo", owner=owner, figure_ids=figure_ids)


def demo_armies(
    build_total: int = 200,
    seed: int = 0,
    db: FigureDB | None = None,
    single_faction: bool = False,
) -> tuple[Army, Army]:
    db = db or load_db()
    hf = lf = None
    if single_faction:
        # Larger factions give more room to form 3+ same-faction clusters.
        facs = sorted(db.factions(), key=lambda f: -len(db.filter(faction=f)))[:5]
        import random

        r = random.Random(seed)
        hf, lf = r.choice(facs), r.choice(facs)
    human = _pick_army(db, "human", build_total, seed=seed * 2 + 1, faction=hf)
    llm = _pick_army(db, "llm", build_total, seed=seed * 2 + 2, faction=lf)
    # Guarantee both armies are legal & non-empty.
    for army in (human, llm):
        v = validate_army(army, db, build_total)
        if not v.ok or not army.figure_ids:
            # Fallback: two cheap figures.
            cheap = sorted(db.all_figures(), key=lambda f: f.points)[:2]
            army.figure_ids = [f.id for f in cheap]
    return human, llm
