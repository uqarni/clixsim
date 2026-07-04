"""Double-base (capsule) geometry semantics — docs/lancers-plan.md P5-R1/R2/R9.

Fixtures use real Lancers figures: Light Lancer On Light Warhorse (mounted,
180-degree arc, Charge on click 0) and Rebellion singles. The rear circle of a
mounted figure at position p facing theta is centred at p - 2r*(cos, sin).
"""

import math

from clixengine import abilities as ab
from clixengine.geometry import Vec, capsule_circles, circles_gap
from clixengine.intents import CloseIntent, RangedIntent
from clixengine.state import contact_is_rear, figures_in_base_contact

from .conftest import build_engine

MOUNTED = "Light Lancer On Light Warhorse"   # mounted, arc 180, range 0
SCORPION = "High Battle Mage On Scorpion Mount"  # mounted, arc 270, range 12
R = 0.55


def test_capsule_circles_derivation():
    circles = capsule_circles(Vec(10, 10), 0.0, R, True)
    assert len(circles) == 2
    (front, rf), (rear, rr) = circles
    assert front == Vec(10, 10) and rf == R == rr
    assert abs(rear.x - 8.9) < 1e-9 and abs(rear.y - 10.0) < 1e-9
    assert capsule_circles(Vec(10, 10), 0.0, R, False) == ((Vec(10, 10), R),)


def test_rear_circle_contact_counts_everywhere(db):
    # Mounted figure at (10,10) facing +x: rear circle at (8.9,10). An enemy at
    # (7.8,10) touches ONLY the rear circle (front dot is 2.2" away).
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),
        ("llm", "Utem Guardsman", (7.8, 10), 0.0, 0),
    ])
    m, g = e.state.figure(0), e.state.figure(1)
    assert figures_in_base_contact(m, g)
    assert g in e.state.opposing_contacts(m)      # break-away/P4-R23 gating
    assert m in e.state.opposing_contacts(g)
    # The enemy's shot at someone else is irrelevant here; what matters: the
    # mounted figure is ENGAGED, so it cannot be given a ranged action even if
    # it had range — and close combat legality sees the contact (arc permitting).


def test_rear_contact_blocks_ranged_and_screens(db):
    # Screening (P4-R25): a target touching a FIRER-friendly figure ANYWHERE
    # (incl. via that friendly Lancer's rear circle) cannot be shot.
    e = build_engine(db, [
        ("human", "Black Powder Boomer", (18, 5), math.pi / 2, 0),   # shooter, range 10
        ("llm", "Utem Guardsman", (18, 12), -math.pi / 2, 0),     # target
        ("human", MOUNTED, (18, 14.2), math.pi / 2, 0),           # rear circle at (18,13.1) touches target
    ])
    clear, reason = e.line_of_fire(0, 1)
    assert not clear and "base contact with a friendly" in reason


def test_mounted_blocker_blocks_lof_with_rear_circle(db):
    # Shot along y=10; the mounted blocker's FRONT dot is 2" off the line but
    # its rear circle sits on it.
    e = build_engine(db, [
        ("human", "Black Powder Boomer", (10, 10), 0.0, 0),          # facing +x
        ("llm", "Utem Guardsman", (18, 10), math.pi, 0),          # target
        ("llm", MOUNTED, (14, 8.9), math.pi / 2, 0),              # facing +y: rear at (14, 7.8)?
    ])
    # place the blocker so its REAR circle straddles the line: facing -y puts
    # rear at (14, 10.0) => re-deploy with facing -pi/2
    b = e.state.figure(2)
    b.facing = -math.pi / 2  # rear = (14, 8.9 + 1.1) = (14, 10.0) — on the line
    clear, reason = e.line_of_fire(0, 1)
    assert not clear and "blocked by" in reason
    # Facing +y instead, the rear circle swings to (14, 7.8) — line is clear.
    b.facing = math.pi / 2
    clear, _ = e.line_of_fire(0, 1)
    assert clear


def test_close_attack_via_rear_circle_and_rear_bonus(db):
    # Attacker touches ONLY the mounted target's rear circle: close combat is
    # legal (front-arc contact on the ATTACKER side) and earns the rear +1
    # (P5-R9: rear-circle contact = rear arc for arc<=180).
    e = build_engine(db, [
        ("human", "Utem Guardsman", (7.8, 10), 0.0, 0),   # facing +x toward the rump
        ("llm", MOUNTED, (10, 10), 0.0, 0),               # rear circle at (8.9, 10)
    ])
    att, m = e.state.figure(0), e.state.figure(1)
    targets = e.legal_close_targets(att)
    assert [(t.uid, rear) for t, rear in targets] == [(1, True)]
    assert contact_is_rear(m, att)
    r = e.apply(CloseIntent(0, 1))
    assert r.ok and any(ev["type"] == "close_attack" and ev["rear"] for ev in r.events)


def test_front_circle_contact_uses_bearing(db):
    # Contact on the mounted figure's FRONT circle, dead ahead: not rear.
    e = build_engine(db, [
        ("human", "Utem Guardsman", (11.1, 10), math.pi, 0),
        ("llm", MOUNTED, (10, 10), 0.0, 0),
    ])
    att, m = e.state.figure(0), e.state.figure(1)
    assert figures_in_base_contact(m, att) and not contact_is_rear(m, att)


def test_scorpion_mount_270_arc_wraps_rear_circle(db):
    # P5-R9: the 270-degree mounted unit classifies ALL contact by bearing at
    # the front dot. An attacker on its rear circle but within the wide arc
    # (bearing 135 deg < half-angle) is NOT rear.
    e = build_engine(db, [
        ("llm", SCORPION, (18, 18), 0.0, 0),                    # rear circle at (16.9, 18)
        ("human", "Utem Guardsman", (17.09, 19.083), -math.pi / 2, 0),  # on the rear circle, bearing ~130
        ("human", "Utem Guardsman", (15.8, 18), 0.0, 0),        # dead behind (bearing 180)
    ], active="human")
    s = e.state.figure(0)
    above, behind = e.state.figure(1), e.state.figure(2)
    assert figures_in_base_contact(s, above) and figures_in_base_contact(s, behind)
    # bearing to 'above' from front dot ~ 130 deg < 135 (half of 270) => front arc
    assert not contact_is_rear(s, above)
    # bearing to 'behind' = 180 deg > 135 => rear
    assert contact_is_rear(s, behind)


def test_defend_and_enhancement_flow_through_rear_circle(db):
    # Defend shared through the provider Lancer's rear circle; Magic
    # Enhancement likewise counts a rear-circle-touching enhancer.
    e = build_engine(db, [
        ("human", "Demi-Magus", (10, 10), math.pi / 2, 0),     # Magic Enhancement
        ("human", "Amazon Queen", (11.1, 10), math.pi / 2, 0), # shooter, touching the enhancer
        ("llm", "Werebear", (12.2, 20), -math.pi / 2, 0),
    ])
    # Wedge a mounted figure so its REAR circle touches the shooter: front at
    # (13.3, 10) facing +x -> rear at (12.2, 10), touching the shooter at (11.1,10)
    from clixengine.state import Figure
    lancer_def = db.find(MOUNTED)[0]
    e.state.figures[3] = Figure(uid=3, definition=lancer_def, owner="human",
                                position=Vec(13.3, 10), facing=0.0)
    shooter = e.state.figure(1)
    enhancer = e.state.figure(0)
    assert ab.ranged_damage_bonus(e.state, shooter, e.state.figure(2)) == 1
    assert figures_in_base_contact(shooter, e.state.figures[3])


def test_healer_reaches_mounted_ally_rear_circle(db):
    # Healing needs base contact — touching the wounded Lancer's rear circle
    # qualifies (P5-R1: contact anywhere on the peanut).
    e = build_engine(db, [
        ("human", "Leech Medic", (7.8, 10), 0.0, 0),
        ("human", MOUNTED, (10, 10), 0.0, 2),   # wounded (click 2), rear at (8.9,10)
        ("llm", "Werebear", (30, 30), 0.0, 0),
    ])
    healer, lancer = e.state.figure(0), e.state.figure(1)
    assert figures_in_base_contact(healer, lancer)


def test_formation_cohesion_through_rear_circle(db):
    # Three same-faction figures chained through a Lancer's rear circle form a
    # legal cohesive footprint set (P5 formations ruling).
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),                 # KI, rear at (8.9,10)
        ("human", "Utem Guardsman", (7.8, 10), 0.0, 0),       # KI, touches rear circle
        ("human", "Utem Guardsman", (6.7, 10), 0.0, 0),       # KI, touches the second
    ])
    figs = [e.state.figure(i) for i in range(3)]
    assert e._positions_cohesive([f.circles() for f in figs])
    # Break the chain: swing the Lancer's rear away and cohesion fails.
    assert not e._positions_cohesive([
        figs[0].circles(None, math.pi / 2), figs[1].circles(), figs[2].circles(),
    ])


def test_view_exposes_mounted_and_rear_pos(db):
    e = build_engine(db, [
        ("human", MOUNTED, (10, 10), 0.0, 0),
        ("llm", "Werebear", (30, 30), 0.0, 0),
    ])
    from clixengine.view import figure_view
    fv = figure_view(e, e.state.figure(0))
    assert fv["mounted"] is True and fv["rear_pos"] == [8.9, 10.0]
    wv = figure_view(e, e.state.figure(1))
    assert wv["mounted"] is False and "rear_pos" not in wv
