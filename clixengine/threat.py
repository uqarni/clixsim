"""Engine-computed incoming-threat facts (feeds AI scoring, candidate
annotations, and the board snapshot).

Answers "how hard can the enemy hit a figure standing HERE?" with the same
rules the engine resolves by: attack-type-aware effective defenses, terrain
modifiers evaluated at the hypothetical position, line of fire, and action
economy (a shooter already in range fires on its next activation — full
weight; a figure that must first spend its action closing threatens only the
turn after — discounted).

PURE with respect to engine state: the hypothetical position is threaded
through every computation explicitly. An earlier version temporarily mutated
figure.position and restored it — concurrent HTTP requests (candidates fetch,
chat snapshot, opponent stream share one engine with no lock) could observe
phantom positions and even strand them permanently via restore-clobber races.

The enemy's front arc is deliberately ignored: a figure re-faces freely when
it acts, so facing never protects you from a threat one activation away.

The forensic audit found the old AI donated ~15-20 clicks per game walking
into 92-97% kill zones because nothing in its scoring or its prompt knew
incoming fire existed (docs/ai-improvement-plan.md, items 1.2/1.8).
"""

from __future__ import annotations

from . import abilities as ab
from . import terrain as terr
from .geometry import Vec, distance, in_base_contact, segment_circle_intersects
from .probability import hit_probability
from .state import DEMORALIZED_ABILITY_ID, Figure

# A figure that must close before it can strike threatens the FOLLOWING turn;
# discount that future damage relative to fire that lands next activation.
SOON_WEIGHT = 0.5


def _lof_to_point(engine, shooter: Figure, mover: Figure, at: Vec) -> tuple[bool, int]:
    """(clear, defense_mod) for shooter firing at ``mover`` standing at ``at``,
    computed WITHOUT touching engine state. Mirrors engine.line_of_fire's
    terrain/base/Stealth blocking (front arc intentionally excluded — the
    shooter re-faces when it acts) plus the terrain defense modifiers."""
    state = engine.state
    elev_a = engine._elev(shooter.position)
    elev_t = engine._elev(at)
    mod = 0
    if state.terrain:
        blocked, hindering = terr.lof_terrain(
            state.terrain, shooter.position, at, elev_a, elev_t,
            engine._stand_on(shooter.position), engine._stand_on(at))
        if blocked:
            return False, 0
        if hindering:
            if ab.STEALTH in mover.active_ability_ids():
                return False, 0  # Stealth: hindering blocks the line entirely
            mod += 1
    if elev_t == 1 and elev_a == 0:
        mod += 1  # height advantage for the target
    both_elev = elev_a == 1 and elev_t == 1
    for other in state.living():
        if other.uid in (shooter.uid, mover.uid):
            continue
        if both_elev and engine._elev(other.position) == 0:
            continue
        if segment_circle_intersects(shooter.position, at, other.position, other.base_radius):
            return False, 0
    return True, mod


def _shooter_engaged(engine, shooter: Figure, mover: Figure, at: Vec) -> bool:
    """Is the shooter based by any of the mover's side (mover evaluated at
    ``at``)? A based shooter cannot make a ranged attack (P4-R23)."""
    if in_base_contact(shooter.position, shooter.base_radius, at, mover.base_radius):
        return True
    for o in engine.state.living(mover.owner):
        if o.uid == mover.uid:
            continue
        if in_base_contact(shooter.position, shooter.base_radius, o.position, o.base_radius):
            return True
    return False


def expected_incoming_clicks(engine, mover: Figure, at: Vec) -> tuple[float, float]:
    """(immediate, soon): expected clicks enemies can deal to ``mover`` standing
    at ``at``. Immediate = attackable on the enemy's next activation from where
    they stand (in contact, or in range with clear line of fire). Soon = they
    must first spend an action closing (within speed+reach envelope)."""
    imm = 0.0
    soon = 0.0
    for e in engine.state.opponents_of(mover):
        if not e.is_alive or e.is_demoralized or e.damage <= 0:
            continue
        d = distance(e.position, at)
        contact = in_base_contact(e.position, e.base_radius, at, mover.base_radius)
        if contact:
            eff_def = ab.effective_defense(engine.state, mover, "close", 0)
            odds = hit_probability(e.attack, eff_def)
            per = ab.damage_after_defenses(mover, e.damage, "close", False)
            imm += odds * per
            continue
        can_shoot_now = (
            e.range > 0
            and d <= e.range
            and ab.can_make_ranged_attack(e)
            and not _shooter_engaged(engine, e, mover, at)
        )
        if can_shoot_now:
            clear, mod = _lof_to_point(engine, e, mover, at)
            if clear:
                eff_def = ab.effective_defense(engine.state, mover, "ranged", mod)
                odds = hit_probability(e.attack, eff_def)
                per = ab.damage_after_defenses(mover, e.damage, "ranged", False)
                imm += odds * per
                continue
        reach = e.speed + (e.range if e.range > 0
                           else e.base_radius + mover.base_radius)
        if d <= reach:
            eff_def = ab.effective_defense(engine.state, mover, "close", 0)
            odds = hit_probability(e.attack, eff_def)
            per = ab.damage_after_defenses(mover, e.damage, "close", False)
            soon += odds * per
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
