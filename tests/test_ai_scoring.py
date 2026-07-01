"""AI scoring correctness (regressions from the ability audit).

The heuristic's EV estimators must route through the ability-aware helpers
(effective_defense, damage_after_defenses, ranged_damage_bonus) rather than raw
printed stats — otherwise the AI mis-values moves the engine resolves correctly.
"""

import math

import clixengine.abilities as ab
from clixengine.ai.evaluation import score_candidate
from clixengine.candidates import generate_candidates, generate_formation_candidates

from .conftest import build_engine


def _kind(cands, kind):
    return [c for c in cands if c.kind == kind]


def test_expected_damage_accounts_for_toughness(db):
    # Grave Robber (dmg 1) close-attacking a Toughness defender: every normal hit
    # delivers max(0, 1-1)=0, so only the 1/36 natural-12 crit scores 1 click.
    e = build_engine(db, [
        ("human", "Grave Robber", (10, 10), 0.0, 0),
        ("llm", "Troll Brawler", (11.1, 10), math.pi, 0),
    ], active="human")
    gr, tb = e.state.figure(0), e.state.figure(1)
    assert ab.TOUGHNESS in tb.active_ability_ids()
    ed = e.expected_damage(gr.uid, tb.uid, attack_type="close")
    assert ed < 0.1  # was ~0.44 before the fix (16x overvaluation)


def test_expected_damage_ranged_includes_magic_enhancement(db):
    base = build_engine(db, [
        ("human", "Utem Crossbowman", (0, 0), 0.0, 0),
        ("llm", "Utem Crossbowman", (5, 0), math.pi, 0),
    ], active="human")
    ed_base = base.expected_damage(0, 1, attack_type="ranged")
    enh = build_engine(db, [
        ("human", "Utem Crossbowman", (0, 0), 0.0, 0),
        ("human", "Shaman", (0, 1.1), 0.0, 0),          # Magic Enhancement, in contact
        ("llm", "Utem Crossbowman", (5, 0), math.pi, 0),
    ], active="human")
    assert ab.ranged_damage_bonus(enh.state, enh.state.figure(0), enh.state.figure(2)) == 1
    ed_enh = enh.expected_damage(0, 2, attack_type="ranged")
    assert ed_enh > ed_base  # the +1 now flows into the AI's estimate


def test_magic_immune_attacker_gets_no_enhancement(db):
    # A Magic Immune figure neither receives nor *inflicts* Magic Enhancement's +1.
    e = build_engine(db, [
        ("human", "Wraith", (0, 0), 0.0, 0),
        ("human", "Shaman", (0, 1.1), 0.0, 0),
        ("llm", "Amotep Gunner", (5, 0), math.pi, 0),
    ], active="human")
    mi = e.state.figure(0)
    assert ab.MAGIC_IMMUNITY in mi.active_ability_ids()
    assert ab.ranged_damage_bonus(e.state, mi, e.state.figure(2)) == 0


def test_defend_lowers_formation_hit_odds(db):
    # A close formation's annotated hit odds must drop when the target is shielded
    # by a base-contact Defend friendly (formation scoring used raw defense before).
    def hit(shield):
        specs = [
            ("human", "Crystal Bladesman", (18.9, 20.0), 0.0, 0),
            ("human", "Crystal Bladesman", (20.0, 18.9), math.pi / 2, 0),
            ("llm", "Werewolf", (20.0, 20.0), 0.0, 0),
        ]
        if shield:
            specs.append(("llm", "Elemental Priest", (20.0, 21.1), 0.0, 0))  # Defend, def 18
        e = build_engine(db, specs, active="human")
        c = _kind(generate_formation_candidates(e, "human"), "close_formation")
        assert c, "expected a close-formation candidate"
        return c[0].annotation["hit_odds"]

    assert hit(True) < hit(False)


def test_magic_healing_candidate_annotates_hit_odds(db):
    # Magic-Heal candidates omitted hit_odds -> _heal_value scored them as always-hit.
    e = build_engine(db, [
        ("human", "Elemental Priest", (18, 6), math.pi / 2, 0),
        ("human", "Werebear", (18, 12), -math.pi / 2, 3),
    ], active="human")
    heals = _kind(generate_candidates(e, e.state.figure(0)), "heal")
    assert heals and all("hit_odds" in c.annotation for c in heals)


def test_pole_arm_charge_scored_below_safe_charge(db):
    # Charging into an enemy Pole Arm's reach is deterred (self-click) but not so
    # harshly that the AI would rather pass on a lone Pole Arm defender.
    def best_charge(enemy):
        e = build_engine(db, [
            ("human", "Werebear", (10, 10), 0.0, 0),
            ("llm", enemy, (14, 10), math.pi, 0),
        ], active="human")
        chs = [c for c in generate_candidates(e, e.state.figure(0))
               if c.kind == "move" and c.annotation.get("intent_hint") == "charge"]
        assert chs, "expected a charge candidate"
        return max(score_candidate(e, e.state.figure(0), c) for c in chs)

    assert best_charge("Royal Pikeman") < best_charge("Werewolf")  # Pole Arm deters
    assert best_charge("Royal Pikeman") > -0.01  # but still preferable to passing


def test_regeneration_offered_while_demoralized(db):
    # Regeneration is a move-class action the engine permits while demoralized;
    # the AI must still offer it (it was gated behind `not demoralized`).
    e = build_engine(db, [
        ("human", "Troll Chieftain", (10, 10), 0.0, 7),  # wounded
        ("llm", "Werebear", (12, 10), math.pi, 0),
    ], active="human")
    tc = e.state.figure(0)
    tc.demoralized = True
    assert tc.is_demoralized
    assert len(_kind(generate_candidates(e, tc), "regenerate")) == 1
