"""Ability effect tests — each of the implemented Rebellion abilities."""

import math

import pytest

from clixengine import abilities as ab
from clixengine.geometry import angle_to
from clixengine.geometry import Vec
from clixengine.intents import (
    CloseIntent,
    LevitateIntent,
    MoveIntent,
    NecromancyIntent,
    RangedIntent,
    RegenerateIntent,
)

from .conftest import build_engine


def _face(a, b):
    return angle_to(Vec(*a), Vec(*b))


# --- passive combat modifiers ---------------------------------------------
def test_battle_armor_boosts_defense_vs_ranged(db):
    # Altem Guardsman click 0 has Battle Armor (+2 defense vs ranged only).
    e = build_engine(db, [("human", "Altem Guardsman", (18, 18), 0.0, 0)])
    t = e.state.figure(0)
    assert 87 in t.active_ability_ids()
    assert ab.effective_defense(e.state, t, "ranged") == t.defense + 2
    assert ab.effective_defense(e.state, t, "close") == t.defense


def test_defend_shares_higher_defense(db):
    # Amazon Queen (Defend) shares its defense with a base-contact friendly.
    e = build_engine(
        db,
        [
            ("human", "Amazon Queen", (18, 18), 0.0, 0),
            ("human", "Woodland Scout", (19.0, 18), 0.0, 0),  # weaker defense, touching
        ],
    )
    queen, weak = e.state.figure(0), e.state.figure(1)
    assert 94 in queen.active_ability_ids()
    eff = ab.effective_defense(e.state, weak, "close")
    assert eff == max(weak.defense, queen.defense) >= weak.defense


def test_magic_immunity_negates_magic_damage(db):
    e = build_engine(db, [("human", "Chaos Mage", (18, 18), 0.0, 5)])  # click 5 = Magic Immunity
    t = e.state.figure(0)
    assert 108 in t.active_ability_ids()
    assert ab.damage_after_defenses(t, 5, "ranged", is_magic=True) == 0  # magic negated
    assert ab.damage_after_defenses(t, 5, "ranged", is_magic=False) == 5  # non-magic unaffected


# --- close-combat abilities ------------------------------------------------
def test_weapon_master_uses_d6_damage(db):
    # Altem Guardsman click 0 has Weapon Master: close damage is a d6, not printed.
    printed = db.find("Altem Guardsman")[0].dial[0].damage
    seen = set()
    for seed in range(60):
        e = build_engine(
            db,
            [
                ("human", "Altem Guardsman", (18, 18), _face((18, 18), (19.0, 18)), 0),
                ("llm", "Storm Golem", (19.0, 18), math.pi, 0),  # tanky, survives many hits
            ],
            seed=seed,
        )
        r = e.apply(CloseIntent(0, 1, variant="weapon_master"))
        ev = next(x for x in r.events if x["type"] == "close_attack")
        if ev["result"] in ("hit", "crit_hit"):
            seen.add(ev["clicks"])
    assert seen  # some hits happened
    assert all(c <= 7 for c in seen)  # 1d6 (+1 crit) capped
    assert seen != {printed}  # damage is the die, not the printed value


def test_vampirism_heals_attacker_on_close_damage(db):
    # Feral Bloodsucker click 2 has Vampirism; a successful close hit heals it 1.
    for seed in range(60):
        e = build_engine(
            db,
            [
                ("human", "Feral Bloodsucker", (18, 18), _face((18, 18), (19.0, 18)), 2),
                ("llm", "Woodland Scout", (19.0, 18), 0.0, 0),  # low defense => easy hit
            ],
            seed=seed,
        )
        atk = e.state.figure(0)
        before = atk.current_click  # 2
        r = e.apply(CloseIntent(0, 1))
        ev = next(x for x in r.events if x["type"] == "close_attack")
        if ev["result"] in ("hit", "crit_hit") and ev["clicks"] > 0:
            assert any(x["type"] == "vampirism" for x in r.events)
            assert atk.current_click == before - 1  # healed one click
            return
    pytest.skip("no hit landed across seeds")


# --- ranged / magic abilities ---------------------------------------------
def test_berserk_blocks_ranged_action(db):
    # Altem Guardsman click 6 has Berserk (range 6 but may not fire).
    e = build_engine(
        db,
        [
            ("human", "Altem Guardsman", (18, 6), math.pi / 2, 6),
            ("llm", "Werebear", (18, 14), -math.pi / 2, 0),
        ],
    )
    assert 89 in e.state.figure(0).active_ability_ids()
    r = e.apply(RangedIntent(0, (1,)))
    assert not r.ok and r.reason == "berserk"


def test_magic_blast_ignores_line_of_fire_blocking(db):
    # Amazon Queen click 0 has Magic Blast; a blocker stops a normal shot but not Magic Blast.
    e = build_engine(
        db,
        [
            ("human", "Amazon Queen", (18, 5), math.pi / 2, 0),
            ("llm", "Werebear", (18, 9), -math.pi / 2, 0),   # blocker on the line
            ("llm", "Werebear", (18, 13), -math.pi / 2, 0),  # intended target
        ],
    )
    assert 103 in e.state.figure(0).active_ability_ids()
    # Normal ranged at the far target is blocked...
    assert not e.line_of_fire(0, 2)[0]
    # ...but Magic Blast resolves (LoF blocking ignored).
    r = e.apply(RangedIntent(0, (2,), variant="magic_blast"))
    assert r.ok and any(x["type"] == "magic_blast" for x in r.events)


# --- reactive / movement abilities ----------------------------------------
def test_pole_arm_damages_figure_that_moves_into_contact(db):
    # Royal Pikeman click 0 has Pole Arm; an enemy ending in its front arc takes 1 click.
    e = build_engine(
        db,
        [
            ("llm", "Royal Pikeman", (18, 20), -math.pi / 2, 0),  # facing down (-y)
            ("human", "Werebear", (18, 14), math.pi / 2, 0),      # moves up into contact
        ],
        active="human",
    )
    pikeman, mover = e.state.figure(0), e.state.figure(1)
    assert 114 in pikeman.active_ability_ids()
    before = mover.current_click
    # Move the Werebear to just below the Pikeman (in its front arc & base contact).
    r = e.apply(MoveIntent(1, (18, 18.9), math.pi / 2))
    assert r.ok
    assert any(x["type"] == "pole_arm" for x in r.events)
    assert mover.current_click == before + 1  # took the Pole Arm click


def test_flight_moves_through_bases_and_breaks_away_easily(db):
    # Chaos Mage click 1 has Flight: pass through bases; break away on 2+.
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (18, 10), math.pi / 2, 1),
            ("llm", "Werebear", (18, 14), -math.pi / 2, 0),  # directly on the path
        ],
    )
    flier = e.state.figure(0)
    assert 98 in flier.active_ability_ids()
    assert ab.break_away_min(flier) == 2
    # A flier may pass through the intervening base and end clear beyond it.
    r = e.apply(MoveIntent(0, (18, 16), math.pi / 2))  # blocker at (18,14); ends 2" past it
    assert r.ok


def test_quickness_free_move_does_not_spend_budget(db):
    # Amazon Queen click 0 has Quickness: a free move keeps the turn's action budget.
    e = build_engine(
        db,
        [
            ("human", "Amazon Queen", (18, 18), 0.0, 0),
            ("human", "Werebear", (14, 18), 0.0, 0),
            ("llm", "Werebear", (26, 18), 0.0, 0),
        ],
        build_total=100,  # only 1 action
    )
    q = e.state.figure(0)
    assert 115 in q.active_ability_ids()
    assert e._actions_remaining() == 1
    r = e.apply(MoveIntent(0, (18, 22), 0.0, free=True))
    assert r.ok
    assert e._actions_remaining() == 1  # budget untouched
    assert 0 in e._acted_uids  # but the queen has acted


# --- healing / support abilities ------------------------------------------
def test_regeneration_heals_self(db):
    # Bone Golem click 6 has Regeneration: heal max(0, d6-2) clicks on self.
    for seed in range(40):
        e = build_engine(db, [
            ("human", "Bone Golem", (18, 18), 0.0, 6),
            ("llm", "Werebear", (30, 30), 0.0, 0),
        ], seed=seed)
        f = e.state.figure(0)
        before = f.current_click
        r = e.apply(RegenerateIntent(0))
        ev = next(x for x in r.events if x["type"] == "regenerate")
        assert ev["healed"] == max(0, ev["roll"] - 2)
        if ev["healed"] > 0:
            assert f.current_click == before - ev["healed"]
            return
    pytest.skip("all rolls healed 0")


def test_command_heals_demoralized_friendly_at_turn_start(db):
    # Amazon Queen (Command) heals a base-contact demoralized friendly each turn start.
    wb = db.find("Werebear")[0]
    demo_click = wb.num_live_clicks - 1  # the Demoralized click
    e = build_engine(
        db,
        [
            ("human", "Amazon Queen", (18, 18), 0.0, 0),
            ("human", "Werebear", (19.0, 18), 0.0, demo_click),
        ],
    )
    friend = e.state.figure(1)
    assert 92 in e.state.figure(0).active_ability_ids()
    assert friend.is_demoralized
    e._begin_player_turn("human")
    assert not friend.is_demoralized  # healed off the demoralized click


def test_command_sometimes_grants_bonus_action(db):
    e = build_engine(db, [("human", "Amazon Queen", (18, 18), 0.0, 0),
                          ("llm", "Werebear", (30, 30), 0.0, 0)], build_total=100)
    got_bonus = 0
    for seed in range(40):
        e.rng.seed = seed
        e.rng.__post_init__()
        e._bonus_actions = 0
        e._begin_player_turn("human")
        if e._actions_remaining() > e.state.actions_per_turn():
            got_bonus += 1
    assert got_bonus > 0  # a Command 6 adds an action on some seeds


# --- necromancy ------------------------------------------------------------
def test_necromancy_revives_eliminated_friendly(db):
    for seed in range(40):
        e = build_engine(db, [
            ("human", "Chaos Mage", (18, 18), 0.0, 4),      # click 4 = Necromancy
            ("human", "Werebear", (10, 10), 0.0, 0),
            ("llm", "Werebear", (30, 30), 0.0, 0),
        ], seed=seed)
        dead = e.state.figure(1)
        dead.take_clicks(50)  # eliminate the friendly Werebear
        assert dead.eliminated
        assert 111 in e.state.figure(0).active_ability_ids()
        r = e.apply(NecromancyIntent(0, 1))
        assert r.ok
        if any(x["type"] == "necromancy" for x in r.events):  # succeeded
            assert not dead.eliminated
            # placed in base contact with the necromancer
            from clixengine.geometry import in_base_contact
            nm = e.state.figure(0)
            assert in_base_contact(nm.position, nm.base_radius, dead.position, dead.base_radius)
            # the returned figure is a normal figure — not barred from acting (§Necromancy)
            assert dead.uid not in e._acted_uids
            assert dead.action_tokens == 0
            return
    pytest.skip("necromancy failed every seed")


def test_ability_coverage_reports_implemented(db):
    e = build_engine(db, [("human", "Altem Guardsman", (18, 18), 0.0, 0),
                          ("llm", "Chaos Mage", (20, 18), 0.0, 0)])
    cov = e.ability_coverage()
    impl_ids = {a["id"] for a in cov["implemented"]}
    # Battle Armor, Weapon Master, Battle Fury (Altem) are implemented.
    assert 87 in impl_ids and 126 in impl_ids
    # Stealth is flagged terrain-pending, not silently implemented.
    assert all(a["id"] != 121 for a in cov["implemented"])


# --- area / special-action abilities (engine resolution) ------------------
def test_flame_lightning_splashes_contacting_figures(db):
    # Amotep Gunner click 0 has Flame/Lightning (range 8): target + touching figures.
    e = build_engine(
        db,
        [
            ("human", "Amotep Gunner", (18, 6), math.pi / 2, 0),
            ("llm", "Werebear", (18, 10), -math.pi / 2, 0),      # primary target (dist 4)
            ("llm", "Werebear", (18, 11.1), -math.pi / 2, 0),    # touching the target
        ],
    )
    assert 97 in e.state.figure(0).active_ability_ids()
    r = e.apply(RangedIntent(0, (1,), variant="flame_lightning"))
    assert r.ok
    hit = [x for x in r.events if x["type"] == "flame_lightning"]
    assert len(hit) == 2  # target and its base-contact neighbour both rolled against


def test_shockwave_hits_all_within_half_range(db):
    # Chaos Mage click 3 has Shockwave (range 12 => half 6), ignoring arc.
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (18, 18), 0.0, 3),
            ("llm", "Werebear", (18, 22), 0.0, 0),   # dist 4 (behind is fine, arc ignored)
            ("llm", "Werebear", (14, 18), 0.0, 0),   # dist 4, to the side
            ("llm", "Werebear", (18, 30), 0.0, 0),   # dist 12, out of half range
        ],
    )
    assert 118 in e.state.figure(0).active_ability_ids()
    r = e.apply(RangedIntent(0, (), variant="shockwave"))
    assert r.ok
    targets = {x["target"] for x in r.events if x["type"] == "shockwave"}
    assert targets == {1, 2}  # both in half range; the far one is not hit


def test_magic_levitation_moves_a_friendly(db):
    from clixengine.intents import LevitateIntent
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (18, 18), 0.0, 4),        # click 4 = Magic Levitation
            ("human", "Werebear", (19.1, 18), 0.0, 0),        # friendly in base contact
        ],
    )
    assert 109 in e.state.figure(0).active_ability_ids()
    r = e.apply(LevitateIntent(0, 1, (25, 18), 0.0))
    assert r.ok
    moved = e.state.figure(1)
    assert (round(moved.position.x, 1), round(moved.position.y, 1)) == (25.0, 18.0)
    assert 1 in e._acted_uids  # the levitated figure may not be given an action


def test_magic_healing_heals_wounded_friendly(db):
    e = build_engine(
        db,
        [
            ("human", "Elemental Priest", (18, 6), math.pi / 2, 0),  # Magic Healing, range 10
            ("human", "Werebear", (18, 12), -math.pi / 2, 3),        # wounded friendly in range/arc
        ],
    )
    assert 107 in e.state.figure(0).active_ability_ids()
    friend = e.state.figure(1)
    for seed in range(40):
        e2 = build_engine(
            db,
            [
                ("human", "Elemental Priest", (18, 6), math.pi / 2, 0),
                ("human", "Werebear", (18, 12), -math.pi / 2, 3),
            ],
            seed=seed,
        )
        before = e2.state.figure(1).current_click
        r = e2.apply(RangedIntent(0, (1,), variant="magic_healing"))
        ev = next(x for x in r.events if x["type"] == "magic_healing")
        if ev["result"] in ("hit", "crit_hit") and ev["healed"] > 0:
            assert e2.state.figure(1).current_click == before - ev["healed"]
            return
    pytest.skip("no successful heal across seeds")


def test_healing_close_action_heals_friendly(db):
    for seed in range(40):
        e = build_engine(
            db,
            [
                ("human", "Leech Medic", (18, 18), 0.0, 0),          # Healing, close
                ("human", "Werebear", (19.1, 18), 0.0, 4),           # wounded friendly, touching
                ("llm", "Werebear", (30, 30), 0.0, 0),
            ],
            seed=seed,
        )
        assert 100 in e.state.figure(0).active_ability_ids()
        before = e.state.figure(1).current_click
        r = e.apply(CloseIntent(0, 1, variant="healing"))
        ev = next(x for x in r.events if x["type"] == "healing")
        if ev["result"] in ("hit", "crit_hit") and ev["healed"] > 0:
            assert e.state.figure(1).current_click == before - ev["healed"]
            return
    pytest.skip("no successful heal across seeds")


def test_healing_crit_miss_backfires_on_healer(db):
    # A roll of "2" on a Healing action is a critical miss: the healer turns his
    # dial 1 click (rulebook §Rolling 2 and 12 covers all close/ranged actions).
    e = build_engine(
        db,
        [
            ("human", "Leech Medic", (18, 18), 0.0, 0),
            ("human", "Werebear", (19.1, 18), 0.0, 4),
            ("llm", "Werebear", (30, 30), 0.0, 0),
        ],
    )
    healer, target = e.state.figure(0), e.state.figure(1)
    h0, t0 = healer.current_click, target.current_click
    e.rng.roll_2d6 = lambda kind="", note="": (1, 1, 2)  # force critical miss
    r = e.apply(CloseIntent(0, 1, variant="healing"))
    ev = next(x for x in r.events if x["type"] == "healing")
    assert ev["result"] == "crit_miss" and ev["healed"] == 0
    assert any(x["type"] == "crit_miss_self" and x["figure"] == 0 for x in r.events)
    assert healer.current_click == h0 + 1     # healer took a click
    assert target.current_click == t0         # target unchanged


def test_magic_healing_crit_miss_backfires_on_healer(db):
    e = build_engine(
        db,
        [
            ("human", "Elemental Priest", (18, 6), math.pi / 2, 0),
            ("human", "Werebear", (18, 12), -math.pi / 2, 3),
        ],
    )
    healer, target = e.state.figure(0), e.state.figure(1)
    h0, t0 = healer.current_click, target.current_click
    e.rng.roll_2d6 = lambda kind="", note="": (1, 1, 2)  # force critical miss
    r = e.apply(RangedIntent(0, (1,), variant="magic_healing"))
    ev = next(x for x in r.events if x["type"] == "magic_healing")
    assert ev["result"] == "crit_miss" and ev["healed"] == 0
    assert any(x["type"] == "crit_miss_self" and x["figure"] == 0 for x in r.events)
    assert healer.current_click == h0 + 1
    assert target.current_click == t0


def test_healing_d6_alternative(db, monkeypatch):
    # §Healing: the healer MAY heal by 1d6 instead of its damage value — matters
    # for a low-damage healer (Leech Medic damage 1). intent.heal_d6 selects it.
    e = build_engine(
        db,
        [
            ("human", "Leech Medic", (18, 18), 0.0, 0),
            ("human", "Werebear", (19.1, 18), 0.0, 6),  # deeply wounded (headroom >= 5)
            ("llm", "Werebear", (30, 30), 0.0, 0),
        ],
    )
    assert e.state.figure(0).damage == 1  # damage-value method would heal only 1
    monkeypatch.setattr("clixengine.engine.outcome", lambda *a, **k: "hit")
    e.rng.d6 = lambda kind="", note="": 5   # force the d6 roll
    target = e.state.figure(1)
    before = target.current_click
    r = e.apply(CloseIntent(0, 1, variant="healing", heal_d6=True))
    ev = next(x for x in r.events if x["type"] == "healing")
    assert ev["healed"] == 5                 # healed by the 1d6 roll, not the damage value
    assert target.current_click == before - 5


def test_levitation_rejects_already_acted_target(db):
    # §Magic Levitation targets a figure that has not yet acted; levitating an
    # already-acted figure would grant it a second action (an illegal chain).
    e = build_engine(
        db,
        [
            ("human", "Magus", (10, 10), 0.0, 0),
            ("human", "Werewolf", (11.0, 10), 0.0, 0),  # in base contact
            ("llm", "Werebear", (20, 10), math.pi, 0),
        ],
        active="human",
    )
    assert ab.MAGIC_LEVITATION in e.state.figure(0).active_ability_ids()
    e._acted_uids.add(1)  # the Werewolf already took an action this turn
    r = e.apply(LevitateIntent(0, 1, (12, 12), 0.0))
    assert not r.ok and r.reason == "already_acted"


# --- regression: input validation & rules fidelity (audit/fuzz sweep) --------
def test_ability_variant_requires_the_ability(db):
    # A figure without Magic Blast may not use the magic_blast variant.
    e = build_engine(db, [
        ("human", "Steam Golem", (18, 5), math.pi / 2, 0),   # ranged, no Magic Blast
        ("llm", "Werebear", (18, 11), -math.pi / 2, 0),
    ])
    assert 103 not in e.state.figure(0).active_ability_ids()
    r = e.apply(RangedIntent(0, (1,), variant="magic_blast"))
    assert not r.ok and r.reason == "no_ability"
    # And a non-Weapon-Master figure may not use the weapon_master variant.
    e2 = build_engine(db, [
        ("human", "Werebear", (18, 18), 0.0, 0),
        ("llm", "Werebear", (19.0, 18), math.pi, 0),
    ])
    assert 126 not in e2.state.figure(0).active_ability_ids()
    r2 = e2.apply(CloseIntent(0, 1, variant="weapon_master"))
    assert not r2.ok and r2.reason == "no_ability"


def test_magic_blast_cannot_target_enemy_adjacent_to_friendly(db):
    # P4-R25 applies to Magic Blast too (only LoF *blocking* is ignored).
    e = build_engine(db, [
        ("human", "Amazon Queen", (18, 5), math.pi / 2, 0),   # Magic Blast
        ("llm", "Werebear", (18, 12), -math.pi / 2, 0),        # target
        ("human", "Werebear", (18, 13.1), -math.pi / 2, 0),    # friendly touching the target
    ])
    r = e.apply(RangedIntent(0, (1,), variant="magic_blast"))
    assert not r.ok and r.reason == "adjacent_friendly"


def test_shockwave_excludes_blocked_figures(db):
    # A base between the caster and a far figure blocks the shockwave line of fire.
    e = build_engine(db, [
        ("human", "Chaos Mage", (18, 18), 0.0, 3),            # Shockwave, half range 6
        ("llm", "Werebear", (18, 20), 0.0, 0),                # dist 2, clear (also a blocker)
        ("llm", "Werebear", (18, 24), 0.0, 0),                # dist 6, blocked by the first
    ])
    r = e.apply(RangedIntent(0, (), variant="shockwave"))
    assert r.ok
    hit = {x["target"] for x in r.events if x["type"] == "shockwave"}
    assert 1 in hit and 2 not in hit  # near one hit, far one blocked


def test_battle_fury_reported_as_capture_pending(db):
    e = build_engine(db, [("human", "Altem Guardsman", (18, 18), 0.0, 0),
                          ("llm", "Werebear", (20, 18), 0.0, 0)])
    cov = e.ability_coverage()
    assert any(a["id"] == 88 for a in cov["capture_pending"])   # Battle Fury
    assert all(a["id"] != 88 for a in cov["implemented"])       # not overstated
