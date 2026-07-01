import math

import pytest

from clixengine.geometry import Vec, angle_to
from clixengine.intents import CloseIntent, RangedIntent
from clixengine.state import Figure

from .conftest import build_engine


def _facing(a, b):
    return angle_to(Vec(*a), Vec(*b))


def test_close_combat_property(db):
    for seed in range(50):
        e = build_engine(
            db,
            [
                ("human", "Werebear", (18, 18), _facing((18, 18), (19.1, 18)), 3),
                ("llm", "Werebear", (19.1, 18), math.pi, 0),
            ],
            seed=seed,
        )
        atk, dfn = e.state.figure(0), e.state.figure(1)
        before = dfn.current_click
        atk_dmg = atk.damage
        res = e.apply(CloseIntent(0, 1))
        assert res.ok
        ev = next(x for x in res.events if x["type"] == "close_attack")
        if ev["result"] in ("hit", "crit_hit"):
            raw = atk_dmg + (1 if ev["result"] == "crit_hit" else 0)
            assert ev["clicks"] == raw  # defender (click 0) has no Toughness
            assert dfn.current_click == before + raw
        elif ev["result"] == "miss":
            assert dfn.current_click == before
        elif ev["result"] == "crit_miss":
            assert dfn.current_click == before
            assert atk.current_click == 4  # attacker took 1 self-click (3 -> 4)


def test_close_requires_contact(db):
    e = build_engine(
        db,
        [
            ("human", "Werebear", (18, 18), 0.0, 0),
            ("llm", "Werebear", (25, 18), math.pi, 0),  # far away
        ],
    )
    res = e.apply(CloseIntent(0, 1))
    assert not res.ok and res.reason == "not_adjacent"


def test_close_requires_front_arc(db):
    # Target is directly behind the attacker (out of its front hemisphere).
    e = build_engine(
        db,
        [
            ("human", "Werebear", (18, 18), 0.0, 0),  # facing +x
            ("llm", "Werebear", (16.9, 18), 0.0, 0),  # behind, but touching
        ],
    )
    res = e.apply(CloseIntent(0, 1))
    assert not res.ok and res.reason == "out_of_arc"


def test_close_rear_bonus_detected(db):
    # Attacker in the defender's rear arc => +1 to the attack roll.
    e = build_engine(
        db,
        [
            ("human", "Werebear", (18, 18), _facing((18, 18), (19.1, 18)), 0),
            ("llm", "Werebear", (19.1, 18), 0.0, 0),  # facing +x, away from attacker
        ],
    )
    targets = e.legal_close_targets(e.state.figure(0))
    assert len(targets) == 1
    _, rear = targets[0]
    assert rear is True
    base = e.hit_odds(0, 1, rear_bonus=False)
    boosted = e.hit_odds(0, 1, rear_bonus=True)
    assert boosted >= base


def test_ranged_in_contact_rejected(db):
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (18, 18), 0.0, 0),
            ("llm", "Werebear", (19.0, 18), math.pi, 0),  # touching the shooter
        ],
    )
    res = e.apply(RangedIntent(0, (1,)))
    assert not res.ok and res.reason == "in_contact"


def test_ranged_out_of_range_rejected(db):
    shooter = db.find("Chaos Mage")[0]
    rng = shooter.range
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (18, 4), math.pi / 2, 0),
            ("llm", "Werebear", (18, 4 + rng + 5), -math.pi / 2, 0),
        ],
    )
    res = e.apply(RangedIntent(0, (1,)))
    assert not res.ok and res.reason == "no_lof"  # LoF fails on range


def test_ranged_lof_blocked_by_base(db):
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (18, 5), math.pi / 2, 0),
            ("llm", "Werebear", (18, 10), -math.pi / 2, 0),  # blocker in the line
            ("llm", "Werebear", (18, 14), -math.pi / 2, 0),  # intended target
        ],
    )
    clear, reason = e.line_of_fire(0, 2)
    assert not clear and "blocked" in reason


def _find_multitarget(db):
    for f in db.all_figures():
        if f.targets >= 2 and f.range >= 10:
            return f
    return None


def test_ranged_multi_target_damage_capped(db):
    firer = _find_multitarget(db)
    assert firer is not None
    e = build_engine(
        db,
        [
            ("human", firer.id, (18, 4), math.pi / 2, 0),
            ("llm", "Order Of Vladd", (15, 13), -math.pi / 2, 0),
            ("llm", "Order Of Vladd", (21, 13), -math.pi / 2, 0),
        ],
    )
    f = e.state.figure(0)
    assert f.targets >= 2 and f.damage >= 1
    res = e.apply(RangedIntent(0, (1, 2)))
    assert res.ok
    for ev in res.events:
        if ev["type"] == "ranged_attack":
            assert ev["clicks"] <= 2  # multi-target damage reduced to 1 (crit -> 2)


def test_toughness_reduces_damage_hook(db):
    # Direct hook test: a target with Toughness active takes 1 fewer click.
    e = build_engine(
        db,
        [
            ("human", "Werebear", (10, 18), 0.0, 3),
            ("llm", "Werebear", (20, 18), 0.0, 3),  # click 3 has Toughness
        ],
    )
    target = e.state.figure(1)
    assert 123 in target.active_ability_ids()  # Toughness
    before = target.current_click
    applied = e._deal_combat_damage(target, 3, source_type="close")
    assert applied == 2  # 3 - 1 (Toughness)
    assert target.current_click == before + 2

    # A target without Toughness takes full damage.
    e2 = build_engine(db, [("human", "Werebear", (10, 18), 0.0, 0),
                           ("llm", "Werebear", (20, 18), 0.0, 0)])
    t2 = e2.state.figure(1)
    assert 123 not in t2.active_ability_ids()
    assert e2._deal_combat_damage(t2, 3, source_type="close") == 3


def test_victory_by_elimination(db):
    e = build_engine(
        db,
        [
            ("human", "Werebear", (10, 18), 0.0, 0),
            ("llm", "Werebear", (20, 18), 0.0, 0),
        ],
    )
    e.state.figure(1).take_clicks(50)  # wipe out the llm figure
    e.end_turn()
    assert e.state.ended
    assert e.state.winner == "human"


def test_victory_points_scoring(db):
    e = build_engine(
        db,
        [
            ("human", "Werebear", (10, 18), 0.0, 0),
            ("llm", "Chaos Mage", (20, 18), 0.0, 0),
        ],
    )
    llm_fig = e.state.figure(1)
    llm_pts = llm_fig.points
    human_pts = e.state.figure(0).points
    llm_fig.take_clicks(50)
    vp = e.victory_points()
    # Eliminated llm figure => its points to human; surviving human => survival VP.
    assert vp["human"] == human_pts + llm_pts
    assert vp["llm"] == 0
