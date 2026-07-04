"""Charge (91), Bound (90), and Invulnerability (101) — docs/lancers-plan.md §2.1."""

import math

from clixengine import abilities as ab
from clixengine.geometry import Vec
from clixengine.intents import CloseIntent, MoveIntent, RangedIntent
from clixengine.server import intent_from_dict

from .conftest import build_engine

CHARGER = "Light Lancer On Light Warhorse"     # mounted, Charge @ clicks 0-1, speed 7
MARTYR = "Martyr On Light Warhorse"            # Charge @ 0-1, Bound @ 4-5 (mid-dial switch)


def _bound_shooter(db):
    """A Lancers figure with Bound showing at click 0 and range > 0."""
    for f in db.all_figures():
        if f.expansion != "Lancers" or f.range <= 0:
            continue
        for a in f.dial[0].abilities:
            if a.id == ab.BOUND:
                return f.short_name
    raise AssertionError("no bound shooter at click 0")


def test_charge_grants_double_speed(db):
    e = build_engine(db, [
        ("human", CHARGER, (10, 10), math.pi / 2, 0),
        ("llm", "Werebear", (30, 30), 0.0, 0),
    ])
    m = e.state.figure(0)
    assert ab.charge_bound_kind(m) == "close" and m.speed == 7
    # 2x speed branch: a 12" ride is legal, 14.5" is not.
    assert e.validate_move(0, (10, 22), facing=math.pi / 2)["ok"]
    assert e.validate_move(0, (10, 24.5), facing=math.pi / 2)["reason"] == "too_far"


def test_charge_rider_through_move_then_strike(db):
    e = build_engine(db, [
        ("human", CHARGER, (10, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 16), -math.pi / 2, 0),
    ])
    target = e.state.figure(1)
    t0 = target.current_click
    # Move at most NORMAL speed into contact -> rider armed.
    r = e.apply(MoveIntent(0, (10, 14.9), facing=math.pi / 2))
    assert r.ok
    assert any(ev["type"] == "rider_armed" and ev["kind"] == "close" for ev in r.events)
    assert e.state.figures[0].uid in [f.uid for f in e.actionable_figures()]
    # The free strike: no second token, no extra action spent.
    spent_before = e._actions_spent
    tokens_before = e.state.figure(0).action_tokens
    r2 = e.apply(CloseIntent(0, 1, rider=True))
    assert r2.ok and any(ev["type"] == "close_attack" for ev in r2.events)
    assert e._actions_spent == spent_before
    assert e.state.figure(0).action_tokens == tokens_before
    assert e._pending_rider is None
    # A second rider attempt is rejected.
    assert e.apply(CloseIntent(0, 1, rider=True)).reason == "no_rider"


def test_rider_denied_beyond_normal_speed(db):
    # Moving past 1x speed is the double-speed branch: no rider.
    e = build_engine(db, [
        ("human", CHARGER, (10, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 20.4), -math.pi / 2, 0),
    ])
    r = e.apply(MoveIntent(0, (10, 19.3), facing=math.pi / 2))  # 9.3" > speed 7
    assert r.ok
    assert not any(ev["type"] == "rider_armed" for ev in r.events)
    assert e.apply(CloseIntent(0, 1, rider=True)).reason == "no_rider"


def test_rider_denied_if_turn_started_in_contact(db):
    e = build_engine(db, [
        ("human", CHARGER, (10, 10), 0.0, 0),
        ("llm", "Utem Guardsman", (11.1, 10), math.pi, 0),   # based at turn start
        ("llm", "Utem Guardsman", (20, 10), math.pi, 0),
    ])
    e._begin_player_turn("human")  # rebuild the turn-start snapshot with contact
    m = e.state.figure(0)
    assert m.uid in e._turn_start_contacted
    # Break away (mounted fails only on 1) and ride to the second enemy.
    for _ in range(50):
        import copy
        if copy.deepcopy(e.rng).d6("break_away") != 1:
            break
        e.rng.d6("_burn")
    r = e.apply(MoveIntent(0, (10, 16.5), facing=math.pi / 2))
    assert r.ok
    assert not any(ev["type"] == "rider_armed" for ev in r.events)


def test_rider_expires_on_other_intent(db):
    e = build_engine(db, [
        ("human", CHARGER, (10, 10), math.pi / 2, 0),
        ("human", "Utem Guardsman", (20, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 16), -math.pi / 2, 0),
    ])
    r = e.apply(MoveIntent(0, (10, 14.9), facing=math.pi / 2))
    assert r.ok and e._pending_rider is not None
    # Any unrelated action forfeits the strike (it's part of the move action).
    assert e.apply(MoveIntent(1, (20, 14), facing=math.pi / 2)).ok
    assert e._pending_rider is None
    assert e.apply(CloseIntent(0, 2, rider=True)).reason == "no_rider"


def test_bound_into_contact_forfeits_the_shot(db):
    name = _bound_shooter(db)
    e = build_engine(db, [
        ("human", name, (10, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 14), -math.pi / 2, 0),
    ])
    m = e.state.figure(0)
    assert ab.charge_bound_kind(m) == "ranged"
    r = e.apply(MoveIntent(0, (10, 12.9), facing=math.pi / 2))  # into contact
    assert r.ok
    # Rider may be armed, but the shot itself is illegal while based (P4-R23).
    r2 = e.apply(RangedIntent(0, (1,), rider=True))
    assert not r2.ok and r2.reason in ("in_contact", "no_rider")


def test_bound_shot_after_move(db):
    name = _bound_shooter(db)
    e = build_engine(db, [
        ("human", name, (10, 10), math.pi / 2, 0),
        ("llm", "Utem Guardsman", (10, 18), -math.pi / 2, 0),
    ])
    m = e.state.figure(0)
    step = min(3, m.speed)
    r = e.apply(MoveIntent(0, (10, 10 + step), facing=math.pi / 2))
    assert r.ok and any(ev["type"] == "rider_armed" and ev["kind"] == "ranged"
                        for ev in r.events)
    if distance := (18 - 10 - step) <= m.range:
        r2 = e.apply(RangedIntent(0, (1,), rider=True))
        assert r2.ok
        assert any(ev["type"] == "ranged_attack" for ev in r2.events)


def test_charge_bound_bar_all_formations(db):
    # Three touching same-faction Knights Immortal, one with Charge showing:
    # both movement and combat formations reject with the cancel hint.
    e = build_engine(db, [
        ("human", CHARGER, (10, 10), math.pi / 2, 0),               # KI, Charge
        ("human", "Liege Knight", (11.1, 10), math.pi / 2, 0),       # KI
        ("human", "Liege Knight", (12.2, 10), math.pi / 2, 0),       # KI
        ("llm", "Werebear", (11, 13), -math.pi / 2, 0),
    ])
    chk = e._validate_formation([0, 1, 2], "move")
    from clixengine.intents import Rejection
    assert isinstance(chk, Rejection) and "Charge/Bound" in chk.detail
    # Cancel Charge -> formation legal again.
    e.state.figure(0).disabled_ability_ids.add(ab.CHARGE)
    assert not isinstance(e._validate_formation([0, 1, 2], "move"), Rejection)


def test_martyr_switches_rider_kind_mid_dial(db):
    e = build_engine(db, [
        ("human", MARTYR, (10, 10), math.pi / 2, 0),
        ("llm", "Werebear", (30, 30), 0.0, 0),
    ])
    m = e.state.figure(0)
    assert ab.charge_bound_kind(m) == "close"    # clicks 0-1: Charge
    m.current_click = 4
    assert ab.charge_bound_kind(m) == "ranged"   # clicks 4-5: Bound


def test_foot_charge_break_away_on_one(db):
    # Charge's own break-away-on-1 for a non-mounted carrier.
    for f in db.all_figures():
        if f.expansion == "Lancers" and not f.mounted and any(
            a.id == ab.CHARGE for a in f.dial[0].abilities
        ):
            name = f.short_name
            break
    e = build_engine(db, [
        ("human", name, (10, 10), 0.0, 0),
        ("llm", "Utem Guardsman", (11.1, 10), math.pi, 0),
    ])
    assert ab.break_away_min(e.state.figure(0)) == 2


def test_invulnerability_effects(db):
    # Lancers carries non-optional Invulnerability on 3 units; find one and put
    # it on its invulnerable click.
    inv = None
    for f in db.all_figures():
        if f.expansion != "Lancers":
            continue
        for c in f.dial:
            if ab.INVULNERABILITY in c.ability_ids():
                inv = (f.short_name, c.index)
                break
        if inv:
            break
    assert inv is not None
    name, click = inv
    e = build_engine(db, [
        ("human", "Utem Guardsman", (10, 10), 0.0, 0),
        ("llm", name, (11.1, 10), math.pi, click),
    ])
    t = e.state.figure(1)
    assert ab.has(t, ab.INVULNERABILITY)
    # -2 damage from combat/ability sources; +2 defense vs ranged only.
    assert ab.damage_after_defenses(t, 3, "close", False) == 1
    assert ab.damage_after_defenses(t, 2, "ranged", False) == 0
    assert ab.effective_defense(e.state, t, "ranged") == t.defense + 2
    assert ab.effective_defense(e.state, t, "close") == t.defense
    # Cannot be healed, from any source.
    t.current_click = min(t.current_click + 1, t.definition.num_live_clicks - 1)
    if ab.has(t, ab.INVULNERABILITY):  # still invulnerable on the damaged click?
        assert t.heal_clicks(1) == 0


def test_intent_wire_roundtrip_with_rider(db):
    c = intent_from_dict({"kind": "close", "attacker_uid": 3, "target_uid": 7, "rider": True})
    assert isinstance(c, CloseIntent) and c.rider
    r = intent_from_dict({"kind": "ranged", "attacker_uid": 3, "target_uids": [7], "rider": True})
    assert isinstance(r, RangedIntent) and r.rider
    # Default stays off — a plain attack never resolves as a rider.
    assert not intent_from_dict({"kind": "close", "attacker_uid": 3, "target_uid": 7}).rider
