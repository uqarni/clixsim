"""Centralised, seeded RNG (DP4 / X1).

Every die roll in the game goes through a single ``DiceRoller`` so that a seed
plus an action sequence fully reproduces a game. Each roll is logged so the
debrief (Phase 5) and replay can reconstruct exactly what happened.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class RollRecord:
    kind: str  # e.g. "attack", "break_away", "initiative"
    dice: tuple[int, ...]
    total: int
    note: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "dice": list(self.dice), "total": self.total, "note": self.note}


@dataclass
class DiceRoller:
    seed: int
    _rng: random.Random = field(init=False)
    history: list[RollRecord] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def d6(self, kind: str = "d6", note: str = "") -> int:
        v = self._rng.randint(1, 6)
        self.history.append(RollRecord(kind, (v,), v, note))
        return v

    def roll_2d6(self, kind: str = "2d6", note: str = "") -> tuple[int, int, int]:
        """Return (die1, die2, total). Both dice recorded for crit detection."""
        a = self._rng.randint(1, 6)
        b = self._rng.randint(1, 6)
        total = a + b
        self.history.append(RollRecord(kind, (a, b), total, note))
        return a, b, total

    def randint(self, lo: int, hi: int, kind: str = "randint", note: str = "") -> int:
        v = self._rng.randint(lo, hi)
        self.history.append(RollRecord(kind, (v,), v, note))
        return v
