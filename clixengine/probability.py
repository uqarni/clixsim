"""2d6 combat probability (DP2 / P4-R17).

The engine precomputes hit probability and expected clicks so the LLM never does
math it is bad at — it only chooses among annotated options.

Attack rule (§Combat Overview): a hit lands when ``2d6 + attack >= defense``.
Special rolls (§Rolling 2 and 12): a natural 2 always misses; a natural 12 always
hits and adds +1 click of damage.
"""

from __future__ import annotations

from functools import lru_cache

# Probability of each 2d6 total (out of 36).
_WAYS = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 5, 9: 4, 10: 3, 11: 2, 12: 1}


@lru_cache(maxsize=None)
def hit_probability(attack: int, defense: int) -> float:
    """P(hit) accounting for auto-miss on 2 and auto-hit on 12."""
    need = defense - attack  # required natural 2d6 total
    ways = 0
    for total, w in _WAYS.items():
        if total == 2:
            continue  # natural 2 always misses
        if total == 12:
            ways += w  # natural 12 always hits
            continue
        if total >= need:
            ways += w
    return ways / 36.0


@lru_cache(maxsize=None)
def crit_hit_probability() -> float:
    return _WAYS[12] / 36.0


@lru_cache(maxsize=None)
def expected_clicks(attack: int, defense: int, damage: int) -> float:
    """Expected clicks of damage delivered by one attack.

    A normal hit delivers ``damage`` clicks; a natural 12 delivers ``damage + 1``
    (§Rolling 2 and 12). A natural 2 delivers 0 (and, separately, costs the
    attacker 1 click — not modelled in this expectation, which is target-facing).
    """
    p_crit = crit_hit_probability()
    p_normal_hit = hit_probability(attack, defense) - p_crit
    return p_normal_hit * damage + p_crit * (damage + 1)


def outcome(die1: int, die2: int, attack: int, defense: int) -> str:
    """Classify a concrete roll: 'crit_miss' | 'miss' | 'hit' | 'crit_hit'."""
    total = die1 + die2
    if total == 2:
        return "crit_miss"
    if total == 12:
        return "crit_hit"
    return "hit" if total + attack >= defense else "miss"
