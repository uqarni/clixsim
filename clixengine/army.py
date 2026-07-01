"""Army construction & validation (Phase 2, P2-R2/R3).

An army is a list of figure definition ids for one owner. Validation enforces the
build total and unique-figure rules; the same unique may appear in *both* armies
but at most once within one army.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .data import FigureDB


@dataclass
class Army:
    name: str
    owner: str  # "human" | "llm"
    figure_ids: list[int] = field(default_factory=list)

    def total_points(self, db: FigureDB) -> int:
        return sum(db.get(fid).points for fid in self.figure_ids)


@dataclass
class Validation:
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_army(army: Army, db: FigureDB, build_total: int) -> Validation:
    errors: list[str] = []
    total = 0
    seen_uniques: set[int] = set()
    for fid in army.figure_ids:
        try:
            fdef = db.get(fid)
        except KeyError:
            errors.append(f"unknown figure id {fid}")
            continue
        total += fdef.points
        if fdef.is_unique:
            if fdef.id in seen_uniques:
                errors.append(f"unique {fdef.short_name} appears more than once")
            seen_uniques.add(fdef.id)
    if total > build_total:
        errors.append(f"army is {total} pts, over the {build_total}-pt build total")
    if not army.figure_ids:
        errors.append("army is empty")
    return Validation(ok=not errors, errors=errors)
