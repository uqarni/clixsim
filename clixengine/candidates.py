"""Candidate-action generation (AI1 — the highest-leverage AI component).

The engine collapses the continuous action space into a small set of tactically
meaningful candidate actions per figure, each annotated with pre-computed facts
(distance, arc, line-of-fire, hit odds, expected clicks). Both the heuristic AI
and the LLM choose among these; neither computes geometry or probability itself
(DP2). Ability-driven actions (Weapon Master, Magic Blast, Healing, Regeneration,
Quickness, Necromancy, ...) are generated here so the AI can actually use them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import abilities as ab
from . import terrain as terr
from .engine import MAGE_SPAWN_FACTION, Engine
from .geometry import (
    CONTACT_TOLERANCE,
    Vec,
    angle_to,
    distance,
    in_base_contact,
    in_front_arc,
    in_rear_arc,
    segment_circle_intersects,
)
from .probability import hit_probability
from .intents import (
    CloseIntent,
    Intent,
    LevitateIntent,
    MoveIntent,
    NecromancyIntent,
    PassIntent,
    RangedIntent,
    RegenerateIntent,
    ToggleAbilityIntent,
)
from .state import Figure
from .threat import clicks_to_demoralized, threat_score

# Enemy figures whose CURRENT click carries one of these are force multipliers:
# the audit found an unopposed healer out-repaired the AI's entire net damage
# and a necromancer refunded 5 of 7 kills. Approach candidates must exist for
# them even when they hide behind the line (plan 1.4).
SUPPORT_ABILITY_NAMES = ("Necromancy", "Healing", "Magic Healing", "Command")


def _is_support(fig: Figure) -> bool:
    names = {a.name for a in fig.definition.dial[fig.current_click].abilities}
    return any(n in names for n in SUPPORT_ABILITY_NAMES)


def _poly_centroid(piece) -> "Vec":
    xs = [v.x for v in piece.polygon]
    ys = [v.y for v in piece.polygon]
    return Vec(sum(xs) / len(xs), sum(ys) / len(ys))

D6_AVG = 3.5  # expected value of a six-sided die (Weapon Master / Magic Blast)


@dataclass
class Candidate:
    intent: Intent
    kind: str
    label: str
    annotation: dict = field(default_factory=dict)


def _facing_toward(a: Vec, b: Vec) -> float:
    return 0.0 if distance(a, b) < 1e-9 else angle_to(a, b)


def _point_toward(frm: Vec, to: Vec, dist: float) -> Vec:
    d = to - frm
    length = d.length()
    if length < 1e-9:
        return frm
    s = dist / length
    return Vec(frm.x + d.x * s, frm.y + d.y * s)


def _clamp_to_board(engine: Engine, p: Vec, radius: float) -> Vec:
    b = engine.state.board
    return Vec(min(max(p.x, radius), b.width - radius),
               min(max(p.y, radius), b.height - radius))


def _contact_point(engine: Engine, mover: Figure, target: Figure) -> Vec:
    gap = mover.base_radius + target.base_radius
    return _point_toward(target.position, mover.position, gap)


def _wounded(f: Figure) -> bool:
    return f.current_click > f.definition.starting_click and f.is_alive


def generate_candidates(engine: Engine, figure: Figure) -> list[Candidate]:
    """All candidate actions for ``figure`` this turn, annotated with facts."""
    cands: list[Candidate] = []
    enemies = engine.state.opponents_of(figure)
    demoralized = figure.is_demoralized
    aids = figure.active_ability_ids()
    has_budget = engine._actions_remaining() > 0
    quick = ab.QUICKNESS in aids  # moves are free; other actions still cost budget

    # ---- attack actions (need a budget action) --------------------------
    if has_budget and not demoralized:
        _close_candidates(engine, figure, cands, aids)
        _ranged_candidates(engine, figure, enemies, cands, aids)
        _support_candidates(engine, figure, cands, aids)

    # ---- Regeneration: a move-class self-heal the engine permits even while
    #      demoralized (unlike attacks/support), so it lives outside that gate. --
    if has_budget and figure.action_tokens < 2 and ab.REGENERATION in aids and _wounded(figure):
        cands.append(Candidate(
            RegenerateIntent(figure.uid), "regenerate", f"Regenerate {figure.short_name}",
            {"expected_heal": 1.0},
        ))

    # ---- movement (free for Quickness) ----------------------------------
    if (has_budget or quick) and figure.speed > 0 and figure.action_tokens < 2:
        _move_candidates(engine, figure, enemies, cands, demoralized, free=quick)

    # ---- cancel optional Flight/Aquatic/Quickness to unlock a formation ----
    # (plan 1.1d). Free and un-tokened; auto-restores at the figure's next turn.
    # Costs base pass-through / easy break-away for the rest of THIS turn, so
    # it's only offered when it actually enables a 3+ movement formation.
    if has_budget and not demoralized:
        barred_ref = next(
            (a for a in figure.definition.dial[figure.current_click].abilities
             if a.id in (ab.FLIGHT, ab.AQUATIC, ab.QUICKNESS) and a.optional
             and a.id in figure.active_ability_ids()),
            None,
        )
        if barred_ref is not None and figure.definition.faction != MAGE_SPAWN_FACTION:
            groundmates = [
                fr for fr in engine.state.friends_of(figure)
                if fr.definition.faction == figure.definition.faction
                and not (fr.active_ability_ids() & (ab.FREE_MOVEMENT_IDS | {ab.QUICKNESS}))
                and not engine.state.opposing_contacts(fr)
                and in_base_contact(figure.position, figure.base_radius,
                                    fr.position, fr.base_radius)
            ]
            if len(groundmates) >= 2:
                cands.append(Candidate(
                    ToggleAbilityIntent(figure.uid, barred_ref.id, off=True),
                    "toggle_ability",
                    f"Switch off {barred_ref.name} this turn — unlocks a movement "
                    f"formation with {len(groundmates)} touching faction-mates "
                    f"(loses its movement perks until end of turn)",
                    {"ability": barred_ref.name,
                     "enables_formation_size": len(groundmates) + 1,
                     "free": True}))

    # ---- pass (costs the action; only when there is budget) -------------
    # NOTE: a figure given NO action clears its tokens anyway at turn end
    # (state.end_owner_turn), so an explicit pass buys nothing an idle figure
    # doesn't get for free — it exists for the human UI; the AI should end its
    # turn instead of passing.
    if has_budget:
        cands.append(Candidate(
            PassIntent(figure.uid), "pass",
            "Stand down (tokens clear anyway if it simply doesn't act)",
            {"clears_tokens": figure.action_tokens > 0, "wastes_action": True},
        ))

    # A tokened figure PUSHES on any non-pass action: 1 click of self-damage
    # (P4-R4). Stamp the fact on every candidate so the AI picker sees the cost
    # instead of having to cross-reference tokens in the board snapshot.
    if figure.action_tokens >= 1:
        dying = (figure.definition.num_live_clicks - 1 - figure.current_click) <= 0
        to_demo = clicks_to_demoralized(figure)
        for c in cands:
            if c.kind == "pass":
                continue
            c.annotation["pushes"] = True
            c.annotation["push_self_damage"] = 1
            if dying:
                c.annotation["push_would_eliminate"] = True
            elif to_demo is not None and to_demo <= 1:
                # The next click is the Demoralized cliff: can't attack, doesn't
                # count for victory — strategically this push loses the figure.
                c.annotation["push_would_demoralize"] = True
    return cands


def _close_candidates(engine, figure, cands, aids):
    wm = ab.WEAPON_MASTER in aids
    for target, rear in engine.legal_close_targets(figure):
        atk = figure.attack + (1 if rear else 0)
        cands.append(Candidate(
            CloseIntent(figure.uid, target.uid), "close",
            f"Close-attack {target.short_name}{' (rear)' if rear else ''}",
            {
                "target": target.uid, "target_name": target.short_name, "rear": rear,
                "hit_odds": round(engine.hit_odds(figure.uid, target.uid, rear, "close"), 3),
                "expected_clicks": round(
                    engine.expected_damage(figure.uid, target.uid, rear, attack_type="close"), 2),
                "damage": figure.damage,
            },
        ))
        if wm:  # Weapon Master: 1d6 damage instead of the printed value
            hit = engine.hit_odds(figure.uid, target.uid, rear, "close")
            cands.append(Candidate(
                CloseIntent(figure.uid, target.uid, variant="weapon_master"), "weapon_master",
                f"Weapon-Master {target.short_name}{' (rear)' if rear else ''}",
                {"target": target.uid, "target_name": target.short_name, "rear": rear,
                 "hit_odds": round(hit, 3), "expected_clicks": round(hit * D6_AVG, 2),
                 "damage": "1d6"},
            ))


def _ranged_candidates(engine, figure, enemies, cands, aids):
    if figure.range <= 0 or not ab.can_make_ranged_attack(figure):
        return
    if engine.state.opposing_contacts(figure):
        return
    ranged_targets = engine.legal_ranged_targets(figure)
    for t in ranged_targets:
        cands.append(Candidate(
            RangedIntent(figure.uid, (t.uid,)), "ranged", f"Shoot {t.short_name}",
            {"targets": [t.uid], "target_names": [t.short_name],
             "hit_odds": round(engine.hit_odds(figure.uid, t.uid, attack_type="ranged"), 3),
             "expected_clicks": round(
                 engine.expected_damage(figure.uid, t.uid, attack_type="ranged"), 2),
             "distance": round(engine.distance_between(figure.uid, t.uid), 2)},
        ))
    if figure.targets > 1 and len(ranged_targets) > 1:
        chosen = tuple(t.uid for t in ranged_targets[: figure.targets])
        names = [engine.state.figure(u).short_name for u in chosen]
        cands.append(Candidate(
            RangedIntent(figure.uid, chosen), "ranged", f"Multi-shot {', '.join(names)}",
            {"targets": list(chosen), "target_names": names, "multi_target": True},
        ))
    # Magic Blast: unblockable single-target 1d6, ignores figure/terrain blocking.
    if ab.MAGIC_BLAST in aids:
        for t in enemies:
            if ab.MAGIC_IMMUNITY in t.active_ability_ids():
                continue
            if distance(figure.position, t.position) > figure.range + 1e-9:
                continue
            if not in_front_arc(figure.position, figure.facing, t.position, figure.arc_half_angle):
                continue
            if any(in_base_contact(t.position, t.base_radius, fr.position, fr.base_radius)
                   for fr in engine.state.friends_of(figure)):
                continue  # P4-R25: can't target an enemy adjacent to your own figure
            hit = engine.hit_odds(figure.uid, t.uid, attack_type="ranged")
            cands.append(Candidate(
                RangedIntent(figure.uid, (t.uid,), variant="magic_blast"), "magic_blast",
                f"Magic Blast {t.short_name}",
                {"target": t.uid, "target_name": t.short_name, "hit_odds": round(hit, 3),
                 "expected_clicks": round(hit * D6_AVG, 2), "unblockable": True},
            ))
    # Flame/Lightning: splash to figures touching the target (damage reduced to 1).
    if ab.FLAME_LIGHTNING in aids:
        for t in ranged_targets:
            splash = [
                o.uid for o in engine.state.living()
                if o.uid not in (figure.uid, t.uid)
                and distance(t.position, o.position) <= t.base_radius + o.base_radius + 1e-6
            ]
            cands.append(Candidate(
                RangedIntent(figure.uid, (t.uid,), variant="flame_lightning"), "flame_lightning",
                f"Flame/Lightning {t.short_name} (+{len(splash)} splash)",
                {"target": t.uid, "target_name": t.short_name, "splash": splash,
                 "hit_odds": round(engine.hit_odds(figure.uid, t.uid, attack_type="ranged"), 3),
                 "expected_clicks": round(engine.hit_odds(figure.uid, t.uid, attack_type="ranged"), 2)},
            ))
    # Shockwave: hit every figure (friend & foe) within half range in all directions.
    if ab.SHOCKWAVE in aids:
        half = max(1, figure.range // 2)

        def _sw_clear(o):
            for b in engine.state.living():
                if b.uid in (figure.uid, o.uid):
                    continue
                if segment_circle_intersects(figure.position, o.position, b.position, b.base_radius):
                    return False
            return True

        foes = [o for o in enemies
                if distance(figure.position, o.position) <= half + 1e-9 and _sw_clear(o)]
        friends_hit = [o for o in engine.state.friends_of(figure)
                       if distance(figure.position, o.position) <= half + 1e-9 and _sw_clear(o)]
        if foes:
            cands.append(Candidate(
                RangedIntent(figure.uid, (), variant="shockwave"), "shockwave",
                f"Shockwave ({len(foes)} foes, {len(friends_hit)} friends in blast)",
                {"foes": [o.uid for o in foes], "friends": [o.uid for o in friends_hit],
                 "half_range": half},
            ))


def _support_candidates(engine, figure, cands, aids):
    state = engine.state
    # Healing (close): heal a wounded friendly in base contact.
    if ab.HEALING in aids and not state.opposing_contacts(figure):
        for fr in state.friends_of(figure):
            if _wounded(fr) and not state.opposing_contacts(fr) and distance(
                figure.position, fr.position
            ) <= figure.base_radius + fr.base_radius + 1e-6:
                # Healing ignores all modifiers, so the hit chance is raw attack
                # vs raw defense (NOT effective_defense — the engine ignores Defend
                # / Battle Armor here, engine.py _resolve_healing).
                base = {"target": fr.uid, "target_name": fr.short_name,
                        "hit_odds": round(hit_probability(figure.attack, fr.defense), 3)}
                cands.append(Candidate(
                    CloseIntent(figure.uid, fr.uid, variant="healing"), "heal",
                    f"Heal {fr.short_name}",
                    {**base, "heal_amount": figure.damage},
                ))
                # The 1d6 alternative can heal more than a low damage value.
                if figure.damage < D6_AVG:
                    cands.append(Candidate(
                        CloseIntent(figure.uid, fr.uid, variant="healing", heal_d6=True), "heal",
                        f"Heal {fr.short_name} (roll d6)",
                        {**base, "heal_amount": "1d6"},
                    ))
    # Magic Healing (ranged): heal a wounded friendly within range/arc.
    if ab.MAGIC_HEALING in aids and not state.opposing_contacts(figure):
        for fr in state.friends_of(figure):
            if not _wounded(fr) or state.opposing_contacts(fr):
                continue
            if ab.MAGIC_IMMUNITY in fr.active_ability_ids():
                continue
            if distance(figure.position, fr.position) > figure.range + 1e-9:
                continue
            if not in_front_arc(figure.position, figure.facing, fr.position, figure.arc_half_angle):
                continue
            cands.append(Candidate(
                RangedIntent(figure.uid, (fr.uid,), variant="magic_healing"), "heal",
                f"Magic-Heal {fr.short_name}",
                # Magic Healing also ignores modifiers -> raw attack vs raw defense.
                {"target": fr.uid, "target_name": fr.short_name, "heal_amount": D6_AVG,
                 "hit_odds": round(hit_probability(figure.attack, fr.defense), 3)},
            ))
    # Necromancy: bring back your most valuable eliminated figure.
    if ab.NECROMANCY in aids and not state.opposing_contacts(figure):
        dead = [d for d in state.figures.values() if d.owner == figure.owner and d.eliminated]
        if dead:
            best = max(dead, key=lambda d: d.points)
            cands.append(Candidate(
                NecromancyIntent(figure.uid, best.uid), "necromancy",
                f"Necromancy: revive {best.short_name}",
                {"revive": best.uid, "revive_name": best.short_name, "revive_points": best.points},
            ))
    # Magic Levitation: fling a friendly (in contact) toward the nearest enemy.
    if ab.MAGIC_LEVITATION in aids:
        enemies = state.opponents_of(figure)
        for fr in state.friends_of(figure):
            if fr.uid in engine._acted_uids:  # a figure that already acted can't be levitated
                continue
            if ab.MAGIC_IMMUNITY in fr.active_ability_ids():
                continue
            if distance(figure.position, fr.position) > figure.base_radius + fr.base_radius + 1e-6:
                continue
            if not enemies:
                continue
            tgt = min(enemies, key=lambda e: distance(fr.position, e.position))
            reach = min(10.0, distance(fr.position, tgt.position) - fr.base_radius - tgt.base_radius)
            if reach <= 0.1:
                continue
            dest = _clamp_to_board(engine, _point_toward(fr.position, tgt.position, reach), fr.base_radius)
            # Skip if the drop point would overlap any other figure (engine rejects).
            if any(o.uid != fr.uid
                   and distance(dest, o.position) < fr.base_radius + o.base_radius - CONTACT_TOLERANCE
                   for o in state.living()):
                continue
            cands.append(Candidate(
                LevitateIntent(figure.uid, fr.uid, (dest.x, dest.y), _facing_toward(dest, tgt.position)),
                "levitate", f"Levitate {fr.short_name} toward {tgt.short_name}",
                {"target": fr.uid, "toward": tgt.uid},
            ))


def _move_candidates(engine, figure, enemies, cands, demoralized, free: bool):
    move_seen: set[tuple] = set()
    pieces = engine.state.terrain
    flies = ab.ignores_figure_bases(figure)
    # The engine validates against the hindering-halved speed (§Hindering), so
    # candidates must budget with it too or every proposal comes back "too_far".
    eff_speed = figure.speed if flies else terr.effective_speed(
        pieces, figure.speed, figure.position, figure.base_radius)

    stuck = bool(pieces) and not flies and terr.base_in_blocking(
        pieces, figure.position, figure.base_radius)

    def _move_illegal(dest: Vec) -> bool:
        """Mirror of the engine's _validate_move geometry (figure bases + terrain:
        blocking, flier landings, hindering entry-stop) so we never propose a move
        the engine would reject."""
        moving = distance(figure.position, dest) > 1e-9
        for other in engine.state.living():
            if other.uid == figure.uid:
                continue
            if moving and not flies and segment_circle_intersects(
                figure.position, dest, other.position, other.base_radius
            ):
                return True
            # Nobody may END overlapping another base (engine end_on_base rule).
            if distance(dest, other.position) < figure.base_radius + other.base_radius - CONTACT_TOLERANCE:
                return True
        if pieces:
            if terr.base_in_blocking(pieces, dest, figure.base_radius):
                return True  # nobody (flier included) may END in blocking / deep water
            if not flies and not stuck and moving:
                if terr.blocking_between(pieces, figure.position, dest, figure.base_radius):
                    return True
                if terr.hindering_entry_violation(
                    pieces, figure.position, dest, figure.base_radius
                ) is not None:
                    return True
        return False

    # Exposure where the figure STANDS, computed once — every move candidate
    # reports the danger delta so neither the heuristic nor the LLM walks into
    # a kill zone blind (plan 1.2; the audit's most-corroborated finding).
    danger_now = threat_score(engine, figure, figure.position)

    def add_move(dest: Vec, facing: float, label: str, extra: dict) -> bool:
        dest = _clamp_to_board(engine, dest, figure.base_radius)
        key = (round(dest.x, 2), round(dest.y, 2), round(facing, 2))
        if key in move_seen or _move_illegal(dest):
            return False
        move_seen.add(key)
        danger_after = threat_score(engine, figure, dest)
        cands.append(Candidate(
            MoveIntent(figure.uid, (dest.x, dest.y), facing, free=free), "move", label,
            {"dest": [round(dest.x, 2), round(dest.y, 2)],
             "move_distance": round(distance(figure.position, dest), 2),
             "free": free,
             "incoming_clicks_here": round(danger_now, 2),
             "incoming_clicks_at_dest": round(danger_after, 2),
             **extra},
        ))
        return True

    def _detour_toward(target) -> Vec | None:
        """When the straight advance is blocked (usually by terrain), probe rotated
        bearings — a greedy way around a wall without pathfinding. Prefer a step
        that closes distance; against a wide wall no single step does, so fall back
        to the legal sidestep that ends nearest the target (the flanking move that
        opens a closing step next turn)."""
        bearing = angle_to(figure.position, target.position)
        now = distance(figure.position, target.position)
        best_fallback: Vec | None = None
        best_d = math.inf
        for off in (0.45, -0.45, 0.9, -0.9, 1.35, -1.35, 1.75, -1.75):
            for frac in (1.0, 0.6):
                step_len = eff_speed * frac
                if step_len <= 0.2:
                    continue
                d = _clamp_to_board(engine, Vec(
                    figure.position.x + math.cos(bearing + off) * step_len,
                    figure.position.y + math.sin(bearing + off) * step_len,
                ), figure.base_radius)
                if _move_illegal(d):
                    continue
                nd = distance(d, target.position)
                if nd < now - 0.3:
                    return d  # closes distance — take it immediately
                # Sideways is fine; walking mostly AWAY from the target is not.
                if nd < now + step_len * 0.5 and nd < best_d:
                    best_fallback, best_d = d, nd
        return best_fallback

    enemies_by_dist = sorted(enemies, key=lambda e: distance(figure.position, e.position))
    # Approach targets: the 3 nearest PLUS priority pieces regardless of distance
    # rank — enemy support (healers/necromancers/commanders) and the biggest
    # shooter. The audit found the enemy healer was unreachable BY CONSTRUCTION
    # for 60 turns because only the nearest 3 ever got approach candidates.
    targets: list[tuple[Figure, str]] = [(t, "near") for t in enemies_by_dist[:3]]
    if not demoralized:
        for e in enemies_by_dist:
            if _is_support(e):
                targets.append((e, "support"))
        shooters = [e for e in enemies if e.range > 0 and not e.is_demoralized]
        if shooters:
            targets.append((max(shooters, key=lambda e: e.points), "shooter"))
    seen_targets: set[int] = set()
    for target, why in ([] if demoralized else targets):
        if target.uid in seen_targets:
            continue
        seen_targets.add(target.uid)
        if in_base_contact(figure.position, figure.base_radius,
                           target.position, target.base_radius):
            continue  # already engaged — close combat is the action, not another approach
        dvec_len = distance(figure.position, target.position)
        facing = _facing_toward(figure.position, target.position)
        contact = _contact_point(engine, figure, target)
        prio: dict = {}
        if why == "support":
            prio["priority_target"] = "support (healer/necromancer/commander — kills near it get repaired or refunded)"
        elif why == "shooter":
            prio["priority_target"] = "the enemy's biggest shooter"
        pins = ({"pins_shooter": True}  # basing a shooter silences it (P4-R23)
                if target.range > 0 and not engine.state.opposing_contacts(target) else {})
        charge_label = (f"Hunt down {target.short_name} (enemy support piece)"
                        if why == "support" else f"Advance into contact with {target.short_name}")
        if distance(figure.position, contact) <= eff_speed + 1e-9:
            added = add_move(contact, facing, charge_label,
                             {"target": target.uid, "intent_hint": "charge", **prio, **pins})
            # Flank: a second contact point on the target's REAR arc (+1 attack,
            # no retaliation facing). Only reachable on oblique approaches — the
            # straight-segment move rule forbids crossing the target's base.
            behind = Vec(
                target.position.x - math.cos(target.facing) * (figure.base_radius + target.base_radius),
                target.position.y - math.sin(target.facing) * (figure.base_radius + target.base_radius),
            )
            if distance(figure.position, behind) <= eff_speed + 1e-9:
                add_move(behind, _facing_toward(behind, target.position),
                         f"Flank behind {target.short_name} (rear attack: +1, no front-arc reply)",
                         {"target": target.uid, "intent_hint": "flank", "rear": True,
                          **prio, **pins})
        else:
            added = add_move(_point_toward(figure.position, target.position, eff_speed),
                             facing, f"Advance toward {target.short_name}",
                             {"target": target.uid, "intent_hint": "approach", **prio})
        if not added:
            det = _detour_toward(target)
            if det is not None:
                add_move(det, _facing_toward(det, target.position),
                         f"Advance toward {target.short_name} (around terrain)",
                         {"target": target.uid, "intent_hint": "detour", **prio})
        if figure.range > 0 and dvec_len > figure.range:
            stop = _point_toward(figure.position, target.position, dvec_len - figure.range + 0.1)
            stop = _point_toward(figure.position, stop,
                                 min(eff_speed, distance(figure.position, stop)))
            add_move(stop, facing, f"Move into range of {target.short_name}",
                     {"target": target.uid, "intent_hint": "range_band", **prio})

    # Kite (plan 1.2c): a shooter about to be based steps back out of the
    # chaser's reach while staying inside its own range — the audit's Wings
    # fliers had a guaranteed kiting win and instead stood still for 24 turns.
    if (figure.range > 0 and not demoralized and enemies_by_dist
            and not engine.state.opposing_contacts(figure)):
        chasers = [
            e for e in enemies_by_dist
            if not e.is_demoralized and e.damage > 0
            and distance(figure.position, e.position)
            - (figure.base_radius + e.base_radius) <= e.speed + 1.0
        ]
        if chasers:
            chief = chasers[0]
            gap = distance(figure.position, chief.position) - (figure.base_radius + chief.base_radius)
            need = chief.speed - gap + 1.0  # step that puts contact out of reach
            step = min(eff_speed, max(1.0, need))
            away = Vec(figure.position.x + (figure.position.x - chief.position.x),
                       figure.position.y + (figure.position.y - chief.position.y))
            dest = _point_toward(figure.position, away, step)
            add_move(dest, _facing_toward(dest, chief.position),
                     f"Kite back from {chief.short_name} — deny contact, keep shooting",
                     {"target": chief.uid, "intent_hint": "kite",
                      "escapes_reach": step >= need - 1e-6})

    # Take cover (plan 2.4): under ranged fire, hindering terrain and hills are
    # +1 defense — 0 of 74 audited AI moves ever ended on either.
    if not demoralized and pieces and danger_now > 0.4:
        shooters_exist = any(e.range > 0 and not e.is_demoralized for e in enemies)
        if shooters_exist:
            covers = [t for t in pieces
                      if (t.kind == "hindering" and not t.low_wall) or t.elevated]
            covers.sort(key=lambda t: distance(figure.position, _poly_centroid(t)))
            for t in covers[:2]:
                c = _poly_centroid(t)
                dest = _point_toward(figure.position, c,
                                     min(eff_speed, distance(figure.position, c)))
                kind = "the hill (+1 def, height advantage)" if t.elevated \
                    else "the woods (+1 def vs shooting)"
                add_move(dest, _facing_toward(dest, enemies_by_dist[0].position),
                         f"Take cover on {kind}",
                         {"intent_hint": "cover", "cover_defense_bonus": 1})

    if enemies_by_dist:
        nearest = enemies_by_dist[0]
        away = Vec(figure.position.x - (nearest.position.x - figure.position.x),
                   figure.position.y - (nearest.position.y - figure.position.y))
        retreat_extra: dict = {"intent_hint": "retreat"}
        if engine.state.opposing_contacts(figure):
            # Leaving contact needs a break-away roll (P4-R8) — for a based
            # shooter this is THE move: escape, then shoot next turn (P4-R23).
            retreat_extra["requires_break_away"] = True
            if figure.range > 0:
                retreat_extra["frees_my_ranged_attack"] = True
        add_move(_point_toward(figure.position, away, eff_speed),
                 _facing_toward(figure.position, nearest.position),
                 f"Retreat from {nearest.short_name}", retreat_extra)
        # Say WHY a re-face matters when the enemy is already touching but out of
        # the front arc — it's the prerequisite for the close attack (P4-R27).
        engaged_behind = in_base_contact(
            figure.position, figure.base_radius, nearest.position, nearest.base_radius
        ) and not in_front_arc(
            figure.position, figure.facing, nearest.position, figure.arc_half_angle
        )
        reface_label = (
            f"Re-face toward {nearest.short_name} — needed before a close attack"
            if engaged_behind else f"Turn to face {nearest.short_name}"
        )
        add_move(figure.position, _facing_toward(figure.position, nearest.position),
                 reface_label, {"intent_hint": "reface"})

    # Rally: a *singleton* joins the nearest same-faction ally to build a
    # movement/ranged formation. Only singletons rally (figures already touching
    # an ally stay put), so clusters grow monotonically to 3+ instead of churning
    # between pairs. Prefer an ally that is itself already in a cluster.
    if not demoralized and enemies_by_dist:
        same = [fr for fr in engine.state.friends_of(figure)
                if fr.definition.faction == figure.definition.faction]
        touching = any(
            in_base_contact(figure.position, figure.base_radius, fr.position, fr.base_radius)
            for fr in same
        )
        if len(same) >= 2 and not touching:
            reachable = [
                fr for fr in same
                if 0 < distance(figure.position, fr.position)
                - (figure.base_radius + fr.base_radius) <= eff_speed + 1e-9
            ]
            if reachable:
                def _clustered(fr):
                    return any(
                        o.uid != fr.uid
                        and in_base_contact(fr.position, fr.base_radius, o.position, o.base_radius)
                        for o in same
                    )
                friend = min(reachable, key=lambda fr: (not _clustered(fr),
                                                        distance(figure.position, fr.position)))
                need = distance(figure.position, friend.position) - (figure.base_radius + friend.base_radius)
                dest = _point_toward(figure.position, friend.position, need)
                enemy = enemies_by_dist[0]
                if distance(dest, enemy.position) <= distance(figure.position, enemy.position) + 3.0:
                    add_move(dest, _facing_toward(dest, enemy.position),
                             f"Form up with {friend.short_name}", {"intent_hint": "rally"})

    # Healer positioning: close the gap to a wounded friendly so a heal becomes
    # possible — without this, healers only ever orbit ENEMIES and a hurt ally
    # across the board is invisible to them.
    heal_aids = figure.active_ability_ids()
    if not demoralized and (ab.HEALING in heal_aids or ab.MAGIC_HEALING in heal_aids):
        heal_ranged = ab.MAGIC_HEALING in heal_aids
        wounded = [fr for fr in engine.state.friends_of(figure)
                   if fr.is_alive and fr.current_click > fr.definition.starting_click
                   and not engine.state.opposing_contacts(fr)]
        wounded.sort(key=lambda fr: fr.current_click - fr.definition.starting_click,
                     reverse=True)
        for ally in wounded[:2]:
            d = distance(figure.position, ally.position)
            if heal_ranged:
                need = d - max(0.5, figure.range - 0.5)  # inside range, small margin
            else:
                need = d - (figure.base_radius + ally.base_radius)  # to base contact
            if need <= 1e-6:
                continue  # already in reach — the heal candidate itself covers it
            step = min(eff_speed, need)
            dest = _point_toward(figure.position, ally.position, step)
            added = add_move(dest, _facing_toward(dest, ally.position),
                             f"Move to heal {ally.short_name}",
                             {"target": ally.uid, "intent_hint": "heal_approach",
                              "in_reach_after": step >= need - 1e-6})
            if not added:
                det = _detour_toward(ally)
                if det is not None:
                    add_move(det, _facing_toward(det, ally.position),
                             f"Move to heal {ally.short_name} (around terrain)",
                             {"target": ally.uid, "intent_hint": "heal_approach",
                              "in_reach_after": False})


# ====================================================================== #
# Formation candidates (P4-R11..R16, R29) — turn-level, not per-figure.
# ====================================================================== #
def _cohesive_clusters(figs: list[Figure]) -> list[list[Figure]]:
    """Connected components of figures by base contact (each is cohesive)."""
    by_uid = {f.uid: f for f in figs}
    adj: dict[int, set] = {f.uid: set() for f in figs}
    for i, a in enumerate(figs):
        for b in figs[i + 1:]:
            if in_base_contact(a.position, a.base_radius, b.position, b.base_radius):
                adj[a.uid].add(b.uid)
                adj[b.uid].add(a.uid)
    seen: set[int] = set()
    comps: list[list[Figure]] = []
    for f in figs:
        if f.uid in seen:
            continue
        comp, stack = [], [f.uid]
        seen.add(f.uid)
        while stack:
            u = stack.pop()
            comp.append(by_uid[u])
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        comps.append(comp)
    return comps


def _formation_translation(engine: Engine, cluster: list[Figure], off: Vec,
                           face_at: Vec) -> tuple[list, list] | None:
    """Validate ONE rigid translation for every member; (dests, facings) or None."""
    board = engine.state.board
    pieces = engine.state.terrain
    member_uids = {f.uid for f in cluster}
    dests, facings = [], []
    for f in cluster:
        nd = Vec(f.position.x + off.x, f.position.y + off.y)
        if not board.contains(nd, f.base_radius):
            return None
        # Don't propose a formation move whose members cross / land on a
        # non-member base or blocking terrain (the engine rejects it).
        for other in engine.state.living():
            if other.uid in member_uids:
                continue
            if segment_circle_intersects(f.position, nd, other.position, other.base_radius):
                return None
            if distance(nd, other.position) < f.base_radius + other.base_radius - CONTACT_TOLERANCE:
                return None
        if pieces:
            if terr.base_in_blocking(pieces, nd, f.base_radius):
                return None
            if terr.blocking_between(pieces, f.position, nd, f.base_radius):
                return None
            if terr.hindering_entry_violation(pieces, f.position, nd, f.base_radius) is not None:
                return None
        dests.append((nd.x, nd.y))
        facings.append(_facing_toward(nd, face_at))
    return dests, facings


def _make_formation_move(engine: Engine, cluster: list[Figure]) -> Candidate | None:
    """Rigid translation of a cohesive cluster toward an enemy — relative
    positions (hence cohesion) are preserved by construction.

    Probes multiple targets, bearings, and step sizes (plan 1.1): the old
    single-attempt version (full step at the nearest enemy only) meant one
    human wall deleted the AI's action-economy tool for an entire game —
    verified in the Anvil archive, where the translation toward the Magus
    failed all game while the one toward the Artillerists was always legal."""
    enemies = engine.state.opponents_of(cluster[0])
    if not enemies:
        return None
    cx = sum(f.position.x for f in cluster) / len(cluster)
    cy = sum(f.position.y for f in cluster) / len(cluster)
    centroid = Vec(cx, cy)
    pieces = engine.state.terrain
    # Slowest member sets the pace (P4-R13), with hindering halving applied.
    speed = min(terr.effective_speed(pieces, f.speed, f.position, f.base_radius)
                for f in cluster)
    by_dist = sorted(enemies, key=lambda e: distance(centroid, e.position))
    for tgt in by_dist[:3]:
        full = min(speed, max(0.0, distance(centroid, tgt.position) - 3.0))
        if full <= 0.1:
            continue
        bearing = math.atan2(tgt.position.y - centroid.y, tgt.position.x - centroid.x)
        for rot in (0.0, math.pi / 6, -math.pi / 6, math.pi / 3, -math.pi / 3):
            for frac in (1.0, 0.66, 0.5):
                step = full * frac
                off = Vec(math.cos(bearing + rot) * step, math.sin(bearing + rot) * step)
                ok = _formation_translation(engine, cluster, off, tgt.position)
                if ok is None:
                    continue
                dests, facings = ok
                uids = tuple(f.uid for f in cluster)
                intent = MoveIntent(cluster[0].uid, dests[0], facings[0],
                                    formation_uids=uids, member_dests=tuple(dests),
                                    member_facings=tuple(facings))
                suffix = "" if rot == 0.0 and frac == 1.0 else " (around the blockage)"
                return Candidate(
                    intent, "formation_move",
                    f"Formation move ({len(cluster)} {cluster[0].definition.faction})"
                    f" toward {tgt.short_name}{suffix}",
                    {"primary": cluster[0].uid, "members": list(uids),
                     "size": len(cluster), "toward": tgt.uid})
    return None


def _make_ranged_formations(engine: Engine, cluster: list[Figure], cands: list[Candidate]) -> None:
    """One candidate per enemy the WHOLE cluster can see — the picker chooses
    the target (first-visible-only starved the AI of its best volley)."""
    primary = max(cluster, key=lambda f: (f.damage, f.attack))
    n = len(cluster)
    atk = primary.attack + 2 * (n - 1)
    uids = tuple(f.uid for f in cluster)
    for target in engine.state.opponents_of(cluster[0]):
        if not all(engine.line_of_fire(f.uid, target.uid)[0] for f in cluster):
            continue
        # Score against the defense the engine actually resolves against (Battle
        # Armor / Defend via effective_defense; hindering terrain via the mod;
        # Toughness via damage_after_defenses).
        eff_def = ab.effective_defense(engine.state, target, "ranged",
                                       engine.terrain_defense_mod(primary, target, "ranged"))
        hit = hit_probability(atk, eff_def)
        per_hit = ab.damage_after_defenses(target, primary.damage, "ranged", False)
        cands.append(Candidate(
            RangedIntent(primary.uid, (target.uid,), formation_uids=uids), "ranged_formation",
            f"Ranged formation ({n}) fires at {target.short_name}",
            {"primary": primary.uid, "members": list(uids), "target": target.uid,
             "hit_odds": round(hit, 3), "expected_clicks": round(hit * per_hit, 2),
             "attack": atk},
        ))


def _make_close_formations(engine: Engine, members: list[Figure], cands: list[Candidate]) -> None:
    if not members:
        return
    for t in engine.state.opponents_of(members[0]):
        attackers = [
            f for f in members
            if in_base_contact(f.position, f.base_radius, t.position, t.base_radius)
            and in_front_arc(f.position, f.facing, t.position, f.arc_half_angle)
        ]
        if len(attackers) < 2:
            continue
        chosen = attackers[:3]
        primary = max(chosen, key=lambda f: (f.damage, f.attack))
        n = len(chosen)
        rear = 1 if any(
            in_rear_arc(t.position, t.facing, f.position, t.arc_half_angle) for f in chosen
        ) else 0
        atk = primary.attack + (n - 1) + rear
        eff_def = ab.effective_defense(engine.state, t, "close",
                                       engine.terrain_defense_mod(primary, t, "close"))
        hit = hit_probability(atk, eff_def)
        per_hit = ab.damage_after_defenses(t, primary.damage, "close", False)
        uids = tuple(f.uid for f in chosen)
        cands.append(Candidate(
            CloseIntent(primary.uid, t.uid, formation_uids=uids), "close_formation",
            f"Close formation ({n}) attacks {t.short_name}",
            {"primary": primary.uid, "members": list(uids), "target": t.uid,
             "hit_odds": round(hit, 3), "expected_clicks": round(hit * per_hit, 2),
             "attack": atk, "rear": bool(rear)},
        ))


def _manual_formation_candidate(cluster: list[Figure]) -> Candidate:
    """A formation-move candidate carrying NO precomputed destinations — offered
    to the human client (``include_manual``) so the interactive place-each-member
    flow exists even when the rigid auto-translation is infeasible (wall ahead,
    enemy too close). Never offered to the AI: its intent is a stay-put no-op."""
    uids = tuple(f.uid for f in cluster)
    dests = tuple((f.position.x, f.position.y) for f in cluster)
    facings = tuple(f.facing for f in cluster)
    intent = MoveIntent(cluster[0].uid, dests[0], facings[0], formation_uids=uids,
                        member_dests=dests, member_facings=facings)
    return Candidate(intent, "formation_move",
                     f"Formation move ({len(cluster)} {cluster[0].definition.faction})",
                     {"primary": cluster[0].uid, "members": list(uids),
                      "size": len(cluster), "manual_only": True})


def generate_formation_candidates(
    engine: Engine, player: str, include_manual: bool = False
) -> list[Candidate]:
    """Movement / ranged / close formation candidates for the active player.
    ``include_manual`` adds destination-less movement-formation entries for
    clusters whose auto-move is infeasible (client interactive staging only)."""
    if engine._actions_remaining() <= 0:
        return []
    state = engine.state
    eligible = [f for f in state.living(player) if engine.can_act(f) and f.action_tokens < 2]
    by_faction: dict[str, list[Figure]] = {}
    for f in eligible:
        by_faction.setdefault(f.definition.faction, []).append(f)

    cands: list[Candidate] = []
    for fac, members in by_faction.items():
        if fac == "Mage Spawn":
            continue  # Mage Spawn cannot form formations (no Shyft in the roster)
        # Movement formation: cohesive clusters of 3-5, free of enemy contact.
        move_elig = [
            f for f in members
            if not state.opposing_contacts(f) and not f.is_demoralized
            and not (f.active_ability_ids() & (ab.FREE_MOVEMENT_IDS | {ab.QUICKNESS}))
        ]
        for cluster in _cohesive_clusters(move_elig):
            # A DFS component's prefix is itself connected, so [:5] is a valid
            # cohesive 3-5 subgroup carved from a larger cluster.
            if len(cluster) >= 3:
                c = _make_formation_move(engine, cluster[:5])
                if c:
                    cands.append(c)
                elif include_manual:
                    # The auto translation is infeasible, but the formation is
                    # legal — the human can still place members one at a time.
                    cands.append(_manual_formation_candidate(cluster[:5]))
        # Ranged formation: cohesive clusters of 3-5, all ranged with a common LoF.
        ranged_elig = [
            f for f in members
            if f.range > 0 and ab.can_make_ranged_attack(f)
            and not state.opposing_contacts(f) and not f.is_demoralized
        ]
        for cluster in _cohesive_clusters(ranged_elig):
            if len(cluster) >= 3:
                _make_ranged_formations(engine, cluster[:5], cands)
        # Close formation: 2-3 members ganging one enemy (need not touch each other).
        _make_close_formations(engine, [f for f in members if not f.is_demoralized], cands)

    # Formations token EVERY member (P4-R12) — name the members that would push.
    for c in cands:
        pushers = [state.figure(u).short_name
                   for u in (c.annotation.get("members") or [])
                   if state.figure(u).action_tokens >= 1]
        if pushers:
            c.annotation["pushes"] = True
            c.annotation["pushing_members"] = pushers
    return cands
