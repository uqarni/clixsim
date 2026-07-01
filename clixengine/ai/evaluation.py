"""Evaluation function + candidate scoring (AI4).

Leaf boards are scored by material (points x health-fraction) plus positional
terms. Candidate scoring gives the heuristic AI a fast, deterministic ordering
and gives the LLM overseer an EV-annotated ranking to choose from. Ability
actions score through the same EV lens (passive abilities already flow through
the engine's hit-odds / expected-damage), and every non-pass action pays a
push-cost when the figure is already fatigued.
"""

from __future__ import annotations

from ..candidates import Candidate
from ..engine import Engine
from ..geometry import Vec, distance
from ..state import Figure, GameState


def _material(state: GameState, player: str) -> float:
    total = 0.0
    for f in state.figures.values():
        if not f.is_alive:
            continue
        val = f.points * f.health_fraction()
        total += val if f.owner == player else -val
    return total


def evaluate_state(engine: Engine, player: str) -> float:
    """Score the board from ``player``'s perspective (higher = better)."""
    state = engine.state
    if state.ended:
        if state.winner == player:
            return 1e6
        if state.winner is not None:
            return -1e6
        return 0.0
    score = _material(state, player)
    threat = 0.0
    for f in state.living(player):
        for e in state.opponents_of(f):
            reach = f.range if f.is_ranged else (f.speed + f.base_radius + e.base_radius)
            if distance(f.position, e.position) <= reach:
                threat += 0.5
    return score + threat


def _vpc(f: Figure) -> float:
    """A figure's point value per dial click (value of one click of damage)."""
    span = max(1, f.definition.num_live_clicks - f.definition.starting_click)
    return f.points / span


def _remaining_clicks(f: Figure) -> int:
    return f.definition.num_live_clicks - 1 - f.current_click


def _push_cost(figure: Figure) -> float:
    """Cost of a non-pass action when the figure is already tokened (it pushes)."""
    if figure.action_tokens < 1:
        return 0.0
    if _remaining_clicks(figure) <= 0:
        return float(figure.points)  # pushing would eliminate it — essentially never
    return _vpc(figure)  # one click of self-damage


def _attack_value(engine: Engine, figure: Figure, cand: Candidate) -> float:
    ann = cand.annotation
    state = engine.state
    if cand.kind == "ranged" and ann.get("multi_target"):
        total = 0.0
        for uid in ann["targets"]:
            t = state.figure(uid)
            total += engine.hit_odds(figure.uid, uid, attack_type="ranged") * _vpc(t)
        return total * 1.05
    uid = ann.get("target") if "target" in ann else ann["targets"][0]
    t = state.figure(uid)
    close_like = cand.kind in ("close", "weapon_master")
    exp = ann.get("expected_clicks")
    if exp is None:
        exp = engine.expected_damage(
            figure.uid, uid, ann.get("rear", False),
            attack_type="close" if close_like else "ranged")
    value = exp * _vpc(t)
    # Elimination bonus: reward likely finishing a wounded, valuable target.
    dmg_est = 3.5 if cand.kind in ("weapon_master", "magic_blast") else figure.damage
    if dmg_est >= _remaining_clicks(t) + 1:
        value += ann.get("hit_odds", 0.5) * t.points * 0.5
    if cand.kind in ("ranged", "magic_blast"):
        value *= 1.05  # ranged invites no immediate counterattack
    if cand.kind == "magic_blast":
        value *= 1.05  # unblockable premium
    return value


def _heal_value(engine: Engine, figure: Figure, cand: Candidate) -> float:
    ann = cand.annotation
    t = engine.state.figure(ann["target"])
    healable = t.current_click - t.definition.starting_click
    if healable <= 0:
        return 0.0
    amt = ann.get("heal_amount", 1)
    if amt == "1d6":
        amt = 3.5
    return min(float(amt), healable) * ann.get("hit_odds", 1.0) * _vpc(t)


def _flame_value(engine: Engine, figure: Figure, cand: Candidate) -> float:
    ann = cand.annotation
    hit = ann.get("hit_odds", 0.5)
    v = hit * _vpc(engine.state.figure(ann["target"]))
    for uid in ann.get("splash", []):
        o = engine.state.figure(uid)
        v += (1.0 if o.owner != figure.owner else -1.5) * hit * _vpc(o)
    return v


def _shockwave_value(engine: Engine, figure: Figure, cand: Candidate) -> float:
    ann = cand.annotation
    v = 0.0
    for uid in ann.get("foes", []):
        v += 0.5 * _vpc(engine.state.figure(uid))
    for uid in ann.get("friends", []):
        v -= 0.75 * _vpc(engine.state.figure(uid))  # friendly fire is bad
    return max(0.0, v)


def _move_value(engine: Engine, figure: Figure, cand: Candidate) -> float:
    ann = cand.annotation
    enemies = engine.state.opponents_of(figure)
    dest = ann.get("dest")
    if not enemies or dest is None:
        return 0.05
    if ann.get("intent_hint") == "rally":
        # Form up while still far from the enemy; once engaged, don't bother.
        cur = min(distance(figure.position, e.position) for e in enemies)
        return 1.6 if cur > 12 else 0.3
    dpt = Vec(dest[0], dest[1])
    cur = min(distance(figure.position, e.position) for e in enemies)
    new = min(distance(dpt, e.position) for e in enemies)
    progress = cur - new
    if figure.is_ranged:
        contact = figure.base_radius + 0.55
        in_band_after = contact < new <= figure.range
        in_band_now = contact < cur <= figure.range
        if in_band_after and not in_band_now:
            val = 1.4
        elif in_band_after:
            val = 0.3
        elif new <= contact:
            val = 0.1
        else:
            val = 0.4 + 0.1 * max(0.0, progress)
    else:
        val = 2.0 if ann.get("intent_hint") == "charge" else 0.5 + 0.15 * max(0.0, progress)
    return max(0.03, val) * 0.5


def _formation_push_cost(engine: Engine, cand: Candidate) -> float:
    return sum(_push_cost(engine.state.figure(u)) for u in cand.annotation.get("members", []))


def _formation_attack_value(engine: Engine, cand: Candidate) -> float:
    ann = cand.annotation
    t = engine.state.figure(ann["target"])
    val = ann.get("expected_clicks", 0.0) * _vpc(t)
    primary = engine.state.figure(ann["primary"])
    if primary.damage >= _remaining_clicks(t) + 1:
        val += ann.get("hit_odds", 0.5) * t.points * 0.5
    return val


def score_candidate(engine: Engine, figure: Figure, cand: Candidate) -> float:
    """Heuristic value of a candidate to the acting player (higher = better)."""
    k = cand.kind
    if k == "pass":
        return -0.01
    # Formations act on several figures at once; score them with their own push cost.
    if k in ("close_formation", "ranged_formation"):
        return _formation_attack_value(engine, cand) - _formation_push_cost(engine, cand)
    if k == "formation_move":
        # Advancing N figures for a single action is efficient (weak figures that
        # can't afford solo tempo benefit most — the whole point of formations).
        return 0.6 * cand.annotation.get("size", len(cand.annotation.get("members", []))) \
            - _formation_push_cost(engine, cand)
    if k in ("close", "ranged", "weapon_master", "magic_blast"):
        val = _attack_value(engine, figure, cand)
    elif k == "flame_lightning":
        val = _flame_value(engine, figure, cand)
    elif k == "shockwave":
        val = _shockwave_value(engine, figure, cand)
    elif k == "heal":
        val = _heal_value(engine, figure, cand)
    elif k == "regenerate":
        val = 1.0 * _vpc(figure)  # ~1 click healed on average
    elif k == "necromancy":
        val = 0.35 * cand.annotation.get("revive_points", 0)  # returns wounded
    elif k == "levitate":
        val = 0.4
    elif k == "move":
        val = _move_value(engine, figure, cand)
    else:
        val = 0.0
    # Every non-pass action pays a push-cost when the figure is already fatigued.
    return val - _push_cost(figure)
