"""Evaluation function + candidate scoring (AI4).

Leaf boards are scored by material (points x health-fraction) plus positional
terms. Candidate scoring gives the heuristic AI a fast, deterministic ordering
and gives the LLM overseer an EV-annotated ranking to choose from. Ability
actions score through the same EV lens (passive abilities already flow through
the engine's hit-odds / expected-damage), and every non-pass action pays a
push-cost when the figure is already fatigued.
"""

from __future__ import annotations

from .. import abilities as ab
from ..candidates import Candidate
from ..engine import Engine
from ..geometry import Vec, distance, in_base_contact, in_front_arc
from ..probability import hit_probability
from ..state import Figure, GameState
from ..threat import clicks_to_demoralized

# Support abilities that make a target a force multiplier: a click of damage
# here is worth more than its vpc (Necromancy is click-gated — one chip click
# on a Grave Robber disables the revives that nullified 55% of a whole game's
# damage; an unopposed healer out-healed the AI's entire net output).
SUPPORT_ABILITY_NAMES = ("Necromancy", "Healing", "Magic Healing", "Command")


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
    """Cost of a non-pass action when the figure is already tokened (it pushes).

    Dial-aware (plan 1.3): a push onto a Demoralized click removes the figure
    from combat AND from the victory count — strategically an elimination
    priced at nearly the full figure. A game literally ended on a self-push
    (Wings, seq 315). Conversely, a deep healthy dial makes a push cheap when
    the payoff is real — don't be push-phobic either."""
    if figure.action_tokens < 1:
        return 0.0
    if _remaining_clicks(figure) <= 0:
        return float(figure.points)  # pushing would eliminate it — essentially never
    to_demo = clicks_to_demoralized(figure)
    if to_demo is not None and to_demo <= 1:
        return 0.7 * figure.points  # the next click IS the cliff
    cost = _vpc(figure)
    if to_demo is not None and to_demo == 2:
        cost *= 1.5  # one click of margin left
    return cost


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
    value += _support_bounty(t, exp)
    # Necromancer discount (plan 1.4): a kill next to a living necromancer is
    # largely refunded — Wings removed 23 net points while dealing 53 clicks.
    revive_discount = _revivable_discount(engine, t)
    # Elimination bonus: reward likely finishing a wounded, valuable target.
    dmg_est = 3.5 if cand.kind in ("weapon_master", "magic_blast") else figure.damage
    if dmg_est >= _remaining_clicks(t) + 1:
        value += ann.get("hit_odds", 0.5) * t.points * 0.5 * revive_discount
    else:
        # Chip damage into a deep dial is near-worthless against Toughness
        # monsters (Phalanx fed 5 of 9 attacks into a 9-click DV17 Draconum at
        # 17-42% odds and died to the counterattacks) — grade by completability.
        depth = _remaining_clicks(t)
        if depth > 4:
            value *= max(0.5, 4.0 / depth)
    odds = ann.get("hit_odds")
    if odds is not None and odds < 0.35:
        value *= 0.5  # soft floor: low-odds swings waste the action slot
    if close_like:
        # Expected retaliation: standing in contact invites the target's swing.
        if t.is_alive and not t.is_demoralized and t.damage > 0:
            t_odds = engine.hit_odds(t.uid, figure.uid, attack_type="close")
            per = ab.damage_after_defenses(figure, t.damage, "close", False)
            value -= 0.6 * t_odds * per * _vpc(figure)
    if cand.kind in ("ranged", "magic_blast"):
        value *= 1.05  # ranged invites no immediate counterattack
    if cand.kind == "magic_blast":
        value *= 1.05  # unblockable premium
    return value


def _support_bounty(t: Figure, exp_clicks: float) -> float:
    """Extra value for damaging enablers (healers/necromancers/commanders)."""
    names = {a.name for a in t.definition.dial[t.current_click].abilities}
    if any(n in names for n in SUPPORT_ABILITY_NAMES):
        return exp_clicks * _vpc(t) * 0.8
    return 0.0


def _revivable_discount(engine: Engine, t: Figure) -> float:
    """0.25 when the target's side has a living, non-demoralized necromancer
    (kills get refunded); 1.0 otherwise."""
    for f in engine.state.living(t.owner):
        if f.uid == t.uid or f.is_demoralized:
            continue
        if any(a.name == "Necromancy"
               for a in f.definition.dial[f.current_click].abilities):
            return 0.25
    return 1.0


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
    """One shared 2d6 vs each target's RAW defense; full printed damage with a
    single target, 1 click each with 2+ (engine._resolve_shockwave). The old
    flat 0.5/foe blasted revivable zombie clusters through its own Jarl while
    never valuing the fact shockwave ignores Stealth and hindering (plan 2.5)."""
    ann = cand.annotation
    state = engine.state
    foes = ann.get("foes", [])
    friends = ann.get("friends", [])
    single = len(foes) + len(friends) == 1 and len(foes) == 1
    v = 0.0
    for uid in foes:
        t = state.figure(uid)
        odds = hit_probability(figure.attack, t.defense)  # raw defense — no abilities
        clicks = figure.damage if single else 1
        v += odds * clicks * _vpc(t) * _revivable_discount(engine, t)
        v += _support_bounty(t, odds * clicks)  # chip a necromancer off its click
    for uid in friends:
        o = state.figure(uid)
        odds = hit_probability(figure.attack, o.defense)
        clicks = figure.damage if single else 1
        v -= 1.2 * odds * clicks * _vpc(o)  # friendly fire on a shared roll
    return max(0.0, v)


_DAMAGE_EVENTS = ("close_attack", "ranged_attack", "magic_blast", "shockwave",
                  "flame_lightning", "close_formation", "ranged_formation",
                  "push_damage", "pole_arm", "crit_miss")


def _staleness(engine: Engine) -> float:
    """Aggression ramp: if NOBODY has dealt combat damage for a while, mutual
    caution has produced a standoff — decay the danger penalty (and the value
    of turtling actions like Regenerate) so somebody commits. Self-play showed
    two regen-tanks healing at each other forever; a standoff is a non-game."""
    turns_since = 0
    current = engine.state.turn_number
    for e in reversed(engine.log.events[-240:]):
        if e.get("type") == "begin_turn":
            turns_since = current - e.get("turn", current)
        if e.get("type") in _DAMAGE_EVENTS and e.get("clicks", 0) > 0:
            break
        if turns_since >= 12:
            break
    return max(0.25, 1.0 - 0.12 * max(0, turns_since - 6))


def side_hopeless(engine: Engine, owner: str, ratio: float = 0.35) -> bool:
    """Sportsmanship at the scoring level: ``owner``'s effective strength is a
    small fraction of the enemy's. A hopeless side must stop valuing evasion —
    kiting/retreat-looping a decided game is the one unforgivable behavior
    (a real game dragged turn 53 to 60 this way; self-play draws did the same)."""
    def strength(side: str) -> float:
        total = 0.0
        for f in engine.state.living(side):
            s = f.definition.points * max(0.1, f.health_fraction())
            if f.is_demoralized:
                s *= 0.3
            total += s
        return total
    mine = strength(owner)
    theirs = strength("human" if owner == "llm" else "llm")
    return theirs > 0 and mine < ratio * theirs


def _danger_penalty(engine: Engine, figure: Figure, ann: dict) -> float:
    """Exposure cost of ending a move where the candidate says (plan 1.2):
    penalize INCREASING exposure hard, standing exposed a little. Units are
    move-value points (clicks scaled), so a hot destination zeroes a casual
    advance but a real payoff (charge + support, formation mass) survives."""
    after = ann.get("incoming_clicks_at_dest")
    now = ann.get("incoming_clicks_here")
    if after is None or now is None:
        return 0.0
    frac_vpc = min(1.6, _vpc(figure) / 6.0)  # expensive figures fear fire more
    return (0.30 * max(0.0, after - now) + 0.06 * after) * (0.6 + frac_vpc) \
        * _staleness(engine)


def _support_count(engine: Engine, figure: Figure, target_uid) -> int:
    """Friendlies already in base contact with the same target — massed attacks
    (the human's playbook) beat the serial suicide charges the audit found."""
    t = engine.state.figures.get(target_uid)
    if t is None:
        return 0
    n = 0
    for fr in engine.state.living(figure.owner):
        if fr.uid == figure.uid:
            continue
        if in_base_contact(fr.position, fr.base_radius, t.position, t.base_radius):
            n += 1
    return n


def _move_value(engine: Engine, figure: Figure, cand: Candidate) -> float:
    ann = cand.annotation
    enemies = engine.state.opponents_of(figure)
    dest = ann.get("dest")
    if ann.get("intent_hint") == "heal_approach":
        # Walking a healer toward a wounded ally is worth a meaningful slice of
        # the clicks it can restore — more when this move gets the ally in reach.
        tgt = engine.state.figures.get(ann.get("target"))
        if tgt is None or not tgt.is_alive:
            return 0.05
        missing = tgt.current_click - tgt.definition.starting_click
        base = _vpc(tgt) * min(missing, 3) * 0.15
        return (base if ann.get("in_reach_after") else 0.6 * base) \
            - _danger_penalty(engine, figure, ann)
    if not enemies or dest is None:
        return 0.05
    hint = ann.get("intent_hint")
    if hint == "rally":
        # Form up while still far from the enemy; once engaged, don't bother.
        cur = min(distance(figure.position, e.position) for e in enemies)
        val = 1.6 if cur > 12 else 0.3
        return val - _danger_penalty(engine, figure, ann)
    dpt = Vec(dest[0], dest[1])
    cur = min(distance(figure.position, e.position) for e in enemies)
    new = min(distance(dpt, e.position) for e in enemies)
    progress = cur - new
    pole_pen = 0.0
    support = 0.0
    if hint in ("charge", "flank"):
        support = 0.35 * min(2, _support_count(engine, figure, ann.get("target")))
        if ann.get("pins_shooter"):
            support += 0.5  # basing a shooter silences it (P4-R23)
        if hint == "flank":
            support += 0.25  # rear arc: +1 attack, no front-arc reply
    if figure.is_ranged:
        contact = figure.base_radius + 0.55
        in_band_after = contact < new <= figure.range
        in_band_now = contact < cur <= figure.range
        if hint == "kite":
            # Deny contact while keeping the target shootable — the audited
            # Wings game was a guaranteed kite win the AI never took.
            val = 1.5 if ann.get("escapes_reach") else 0.6
        elif in_band_after and not in_band_now:
            val = 1.4
        elif in_band_after:
            val = 0.3
        elif new <= contact:
            val = 0.1
        else:
            val = 0.4 + 0.1 * max(0.0, progress)
    else:
        if hint in ("charge", "flank"):
            val = 2.0
            pole_pen = _charge_pole_arm_penalty(engine, figure, dpt)  # self-click on arrival
        else:
            val = 0.5 + 0.15 * max(0.0, progress)
    if hint == "cover":
        val = max(val, 0.9)  # +1 def under fire is a real turn's work
    if hint == "reface" and ann.get("enables_attack"):
        # Turning to face an in-range enemy unlocks next turn's attack — worth
        # a real slice of that attack (self-play locked up because refacing
        # never competed with regen-tanking).
        val = max(val, 1.6)
    if hint in ("kite", "retreat") and side_hopeless(engine, figure.owner):
        val *= 0.15  # a lost army fights to the finish; it doesn't run laps
    # Anti-thrash (plan 2.6): a figure oscillated between two exact points for
    # 60 turns — walking back to where it JUST stood is almost never a plan.
    prev = getattr(engine, "_prev_positions", {}).get(figure.uid)
    thrash = 0.5 if prev and distance(dpt, Vec(prev[0], prev[1])) < 0.4 else 0.0
    return max(0.03, val + support) * 0.5 - pole_pen - thrash \
        - _danger_penalty(engine, figure, ann)


def _charge_pole_arm_penalty(engine: Engine, figure: Figure, dest: Vec) -> float:
    """Deterrent for charging into an enemy Pole Arm's front-arc contact (mirrors
    engine._apply_pole_arm; Toughness-adjusted -> 0 if the mover would take 0 clicks).
    Capped below the charge's base value so it only breaks ties toward a safe target:
    a full material penalty would wrongly make the AI pass on a lone Pole Arm defender
    (one-ply scoring can't yet see the follow-up attack payoff — see FUT-AI lookahead)."""
    for p in engine.state.opponents_of(figure):
        if ab.has(p, ab.POLE_ARM) and in_base_contact(
            dest, figure.base_radius, p.position, p.base_radius
        ) and in_front_arc(p.position, p.facing, dest, p.arc_half_angle):
            clicks = ab.damage_after_defenses(figure, 1, "ability", False)
            return min(clicks * _vpc(figure), 0.5)
    return 0.0


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
        # Engine heals max(0, d6-2): EV 1.67 clicks. Decays with staleness —
        # regen-tanking a standoff heals nothing that matters and stalls games.
        val = (10.0 / 6.0) * _vpc(figure) * _staleness(engine)
    elif k == "necromancy":
        val = 0.35 * cand.annotation.get("revive_points", 0)  # returns wounded
    elif k == "levitate":
        val = 0.4
    elif k == "toggle_ability":
        # Cancel Flight/Quickness to unlock a 3+ movement formation: worth a bit
        # (the follow-up formation move is where the value lands); free action.
        val = 0.5 if cand.annotation.get("enables_formation_size", 0) >= 3 else 0.05
    elif k == "move":
        val = _move_value(engine, figure, cand)
    else:
        val = 0.0
    # Every non-pass action pays a push-cost when the figure is already fatigued.
    return val - _push_cost(figure)
