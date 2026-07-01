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
from .engine import Engine
from .geometry import (
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
)
from .state import Figure

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

    # ---- pass (costs the action; only when there is budget) -------------
    if has_budget:
        cands.append(Candidate(
            PassIntent(figure.uid), "pass", "Pass (rest, clear push tokens)",
            {"clears_tokens": figure.action_tokens > 0},
        ))
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
                   and distance(dest, o.position) < fr.base_radius + o.base_radius - 1e-6
                   for o in state.living()):
                continue
            cands.append(Candidate(
                LevitateIntent(figure.uid, fr.uid, (dest.x, dest.y), _facing_toward(dest, tgt.position)),
                "levitate", f"Levitate {fr.short_name} toward {tgt.short_name}",
                {"target": fr.uid, "toward": tgt.uid},
            ))


def _move_candidates(engine, figure, enemies, cands, demoralized, free: bool):
    move_seen: set[tuple] = set()

    def add_move(dest: Vec, facing: float, label: str, extra: dict) -> None:
        dest = _clamp_to_board(engine, dest, figure.base_radius)
        key = (round(dest.x, 2), round(dest.y, 2), round(facing, 2))
        if key in move_seen:
            return
        flies = ab.ignores_figure_bases(figure)
        moving = distance(figure.position, dest) > 1e-9
        for other in engine.state.living():
            if other.uid == figure.uid:
                continue
            if moving and not flies and segment_circle_intersects(
                figure.position, dest, other.position, other.base_radius
            ):
                return
            if flies and distance(dest, other.position) < figure.base_radius + other.base_radius - 1e-6:
                return
        move_seen.add(key)
        cands.append(Candidate(
            MoveIntent(figure.uid, (dest.x, dest.y), facing, free=free), "move", label,
            {"dest": [round(dest.x, 2), round(dest.y, 2)],
             "move_distance": round(distance(figure.position, dest), 2),
             "free": free, **extra},
        ))

    enemies_by_dist = sorted(enemies, key=lambda e: distance(figure.position, e.position))
    for target in ([] if demoralized else enemies_by_dist[:3]):
        dvec_len = distance(figure.position, target.position)
        facing = _facing_toward(figure.position, target.position)
        contact = _contact_point(engine, figure, target)
        if distance(figure.position, contact) <= figure.speed + 1e-9:
            add_move(contact, facing, f"Advance into contact with {target.short_name}",
                     {"target": target.uid, "intent_hint": "charge"})
        else:
            step = _point_toward(figure.position, target.position, figure.speed)
            add_move(step, facing, f"Advance toward {target.short_name}",
                     {"target": target.uid, "intent_hint": "approach"})
        if figure.range > 0 and dvec_len > figure.range:
            stop = _point_toward(figure.position, target.position, dvec_len - figure.range + 0.1)
            stop = _point_toward(figure.position, stop,
                                 min(figure.speed, distance(figure.position, stop)))
            add_move(stop, facing, f"Move into range of {target.short_name}",
                     {"target": target.uid, "intent_hint": "range_band"})

    if enemies_by_dist:
        nearest = enemies_by_dist[0]
        away = Vec(figure.position.x - (nearest.position.x - figure.position.x),
                   figure.position.y - (nearest.position.y - figure.position.y))
        add_move(_point_toward(figure.position, away, figure.speed),
                 _facing_toward(figure.position, nearest.position),
                 f"Retreat from {nearest.short_name}", {"intent_hint": "retreat"})
        add_move(figure.position, _facing_toward(figure.position, nearest.position),
                 f"Turn to face {nearest.short_name}", {"intent_hint": "reface"})

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
                - (figure.base_radius + fr.base_radius) <= figure.speed + 1e-9
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


def _make_formation_move(engine: Engine, cluster: list[Figure]) -> Candidate | None:
    """Rigid translation of a cohesive cluster toward the nearest enemy — relative
    positions (hence cohesion) are preserved by construction."""
    enemies = engine.state.opponents_of(cluster[0])
    if not enemies:
        return None
    cx = sum(f.position.x for f in cluster) / len(cluster)
    cy = sum(f.position.y for f in cluster) / len(cluster)
    centroid = Vec(cx, cy)
    tgt = min(enemies, key=lambda e: distance(centroid, e.position))
    speed = min(f.speed for f in cluster)
    step = min(speed, max(0.0, distance(centroid, tgt.position) - 3.0))
    if step <= 0.1:
        return None
    d = tgt.position - centroid
    L = d.length()
    if L < 1e-9:
        return None
    off = Vec(d.x / L * step, d.y / L * step)
    board = engine.state.board
    member_uids = {f.uid for f in cluster}
    dests, facings = [], []
    for f in cluster:
        nd = Vec(f.position.x + off.x, f.position.y + off.y)
        if not board.contains(nd, f.base_radius):
            return None
        # Don't propose a formation move whose members cross / land on a
        # non-member base (the engine rejects it).
        for other in engine.state.living():
            if other.uid in member_uids:
                continue
            if segment_circle_intersects(f.position, nd, other.position, other.base_radius):
                return None
            if distance(nd, other.position) < f.base_radius + other.base_radius - 1e-6:
                return None
        dests.append((nd.x, nd.y))
        facings.append(_facing_toward(nd, tgt.position))
    uids = tuple(f.uid for f in cluster)
    intent = MoveIntent(cluster[0].uid, dests[0], facings[0], formation_uids=uids,
                        member_dests=tuple(dests), member_facings=tuple(facings))
    return Candidate(intent, "formation_move",
                     f"Formation move ({len(cluster)} {cluster[0].definition.faction})",
                     {"primary": cluster[0].uid, "members": list(uids),
                      "size": len(cluster), "toward": tgt.uid})


def _make_ranged_formation(engine: Engine, cluster: list[Figure]) -> Candidate | None:
    enemies = engine.state.opponents_of(cluster[0])
    target = next(
        (t for t in enemies if all(engine.line_of_fire(f.uid, t.uid)[0] for f in cluster)),
        None,
    )
    if target is None:
        return None
    primary = max(cluster, key=lambda f: (f.damage, f.attack))
    n = len(cluster)
    atk = primary.attack + 2 * (n - 1)
    # Score against the defense the engine actually resolves against (Battle Armor /
    # Defend via effective_defense; Toughness via damage_after_defenses).
    eff_def = ab.effective_defense(engine.state, target, "ranged")
    hit = hit_probability(atk, eff_def)
    per_hit = ab.damage_after_defenses(target, primary.damage, "ranged", False)
    uids = tuple(f.uid for f in cluster)
    return Candidate(
        RangedIntent(primary.uid, (target.uid,), formation_uids=uids), "ranged_formation",
        f"Ranged formation ({n}) fires at {target.short_name}",
        {"primary": primary.uid, "members": list(uids), "target": target.uid,
         "hit_odds": round(hit, 3), "expected_clicks": round(hit * per_hit, 2),
         "attack": atk},
    )


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
        eff_def = ab.effective_defense(engine.state, t, "close")
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


def generate_formation_candidates(engine: Engine, player: str) -> list[Candidate]:
    """Movement / ranged / close formation candidates for the active player."""
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
        # Ranged formation: cohesive clusters of 3-5, all ranged with a common LoF.
        ranged_elig = [
            f for f in members
            if f.range > 0 and ab.can_make_ranged_attack(f)
            and not state.opposing_contacts(f) and not f.is_demoralized
        ]
        for cluster in _cohesive_clusters(ranged_elig):
            if len(cluster) >= 3:
                c = _make_ranged_formation(engine, cluster[:5])
                if c:
                    cands.append(c)
        # Close formation: 2-3 members ganging one enemy (need not touch each other).
        _make_close_formations(engine, [f for f in members if not f.is_demoralized], cands)
    return cands
