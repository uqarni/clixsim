"""Engine-computed incoming-threat facts (feeds AI scoring, candidate
annotations, and the board snapshot).

Answers "how hard can the enemy hit a figure standing HERE?" with the same
rules the engine resolves by: attack-type-aware effective defenses, terrain
modifiers evaluated at the hypothetical position, line of fire, and action
economy (a shooter already in range fires on its next activation — full
weight; a figure that must first spend its action closing threatens only the
turn after — discounted).

The forensic audit found the old AI donated ~15-20 clicks per game walking
into 92-97% kill zones because nothing in its scoring or its prompt knew
incoming fire existed (docs/ai-improvement-plan.md, items 1.2/1.8).
"""

from __future__ import annotations

from . import abilities as ab
from .geometry import Vec, distance, in_base_contact
from .probability import hit_probability
from .state import DEMORALIZED_ABILITY_ID, Figure

# A figure that must close before it can strike threatens the FOLLOWING turn;
# discount that future damage relative to fire that lands next activation.
SOON_WEIGHT = 0.5


def expected_incoming_clicks(engine, mover: Figure, at: Vec) -> tuple[float, float]:
    """(immediate, soon): expected clicks enemies can deal to ``mover`` standing
    at ``at``. Immediate = attackable on the enemy's next activation from where
    they stand (in contact, or in range with clear line of fire). Soon = they
    must first spend an action closing (within speed+reach envelope)."""
    imm = 0.0
    soon = 0.0
    old = mover.position
    mover.position = at  # evaluate defenses/LoF at the hypothetical position
    try:
        for e in engine.state.opponents_of(mover):
            if not e.is_alive or e.is_demoralized or e.damage <= 0:
                continue
            d = distance(e.position, at)
            contact = in_base_contact(e.position, e.base_radius, at, mover.base_radius)
            can_melee_now = contact
            can_shoot_now = (
                not contact
                and e.range > 0
                and d <= e.range
                and ab.can_make_ranged_attack(e)
                and not engine.state.opposing_contacts(e)
                and engine.line_of_fire(e.uid, mover.uid)[0]
            )
            if can_melee_now or can_shoot_now:
                atype = "close" if can_melee_now else "ranged"
                eff_def = ab.effective_defense(
                    engine.state, mover, atype, engine.terrain_defense_mod(e, mover, atype))
                odds = hit_probability(e.attack, eff_def)
                per = ab.damage_after_defenses(mover, e.damage, atype, False)
                imm += odds * per
            else:
                reach = e.speed + (e.range if e.range > 0
                                   else e.base_radius + mover.base_radius)
                if d <= reach:
                    eff_def = ab.effective_defense(engine.state, mover, "close", 0)
                    odds = hit_probability(e.attack, eff_def)
                    per = ab.damage_after_defenses(mover, e.damage, "close", False)
                    soon += odds * per
    finally:
        mover.position = old
    return imm, soon


def threat_score(engine, mover: Figure, at: Vec) -> float:
    """Single-number exposure at ``at`` in expected clicks."""
    imm, soon = expected_incoming_clicks(engine, mover, at)
    return imm + SOON_WEIGHT * soon


def remaining_clicks(f: Figure) -> int:
    return f.definition.num_live_clicks - 1 - f.current_click


def clicks_to_demoralized(f: Figure) -> int | None:
    """Clicks of damage until this figure's dial shows Demoralized (strategically
    dead: cannot attack, does not count for victory). None if the dial never
    demoralizes before elimination."""
    dial = f.definition.dial
    for i in range(f.current_click + 1, f.definition.num_live_clicks):
        if DEMORALIZED_ABILITY_ID in dial[i].ability_ids():
            return i - f.current_click
    return None


def figure_threat_brief(engine, f: Figure) -> dict:
    """Snapshot block: how much danger this figure is in where it stands."""
    imm, soon = expected_incoming_clicks(engine, f, f.position)
    rem = remaining_clicks(f)
    return {
        "incoming_clicks_if_i_stand_here": round(imm, 2),
        "incoming_clicks_if_enemies_close": round(soon, 2),
        "at_risk_of_elimination": imm >= rem,
    }
