"""figure_action_hints: faithful "why can't I act here" explanations.

These lock in that the engine EXPLAINS correct rules restrictions (P4-R23/R24/R25,
P4-R27, Magic Healing arc, formation minimum) rather than silently offering nothing.
"""

import math

from .conftest import build_engine


def _hints(e, uid):
    return e.figure_action_hints(e.state.figure(uid))


def test_hint_ranged_target_screened_by_friendly(db):
    # Firer -> enemy is clear range/arc, but a friendly touches the enemy (P4-R25).
    e = build_engine(db, [
        ("human", "Utem Crossbowman", (10, 10), 0.0, 0),   # faces +x
        ("llm", "Werebear", (14, 10), math.pi, 0),          # in range, in front arc
        ("human", "Werebear", (14, 11.0), -math.pi / 2, 0), # our own figure touching the enemy
    ], active="human")
    hints = _hints(e, 0)
    assert any("own figure" in h or "P4-R25" in h for h in hints), hints


def test_hint_ranged_blocked_by_own_base_contact(db):
    # Firer is in base contact with enemy A, so it can't shoot enemy B (P4-R23).
    e = build_engine(db, [
        ("human", "Utem Crossbowman", (10, 10), 0.0, 0),
        ("llm", "Werebear", (11.0, 10), math.pi, 0),   # adjacent -> firer is "in contact"
        ("llm", "Werebear", (15, 10), math.pi, 0),     # in range, in arc, not adjacent
    ], active="human")
    hints = _hints(e, 0)
    assert any("base contact" in h and "P4-R23" in h for h in hints), hints


def test_hint_close_needs_front_arc(db):
    # An adjacent enemy sitting behind the attacker can't be close-attacked (P4-R27).
    e = build_engine(db, [
        ("human", "Werebear", (10, 10), 0.0, 0),   # faces +x
        ("llm", "Werebear", (9.0, 10), 0.0, 0),     # directly behind (rear arc), adjacent
    ], active="human")
    hints = _hints(e, 0)
    assert any("front arc" in h and "P4-R27" in h for h in hints), hints


def test_hint_magic_healing_rear_arc(db):
    # A wounded ally in the healer's rear arc: Magic Healing is a ranged action.
    e = build_engine(db, [
        ("human", "Mending Priestess", (10, 10), 0.0, 0),  # faces +x
        ("human", "Werebear", (7, 10), 0.0, 3),            # behind (rear arc), wounded (click 3)
    ], active="human")
    hints = _hints(e, 0)
    assert any("front arc" in h.lower() and "heal" in h.lower() for h in hints), hints


def test_no_hints_when_out_of_actions(db):
    # With the budget spent, no attack/heal is offered, so don't hint one.
    e = build_engine(db, [
        ("human", "Utem Crossbowman", (10, 10), 0.0, 0),
        ("llm", "Werebear", (14, 10), math.pi, 0),
        ("human", "Werebear", (14, 11.0), -math.pi / 2, 0),
    ], active="human")
    assert _hints(e, 0)  # sanity: there IS a hint with budget
    e._actions_spent = e.state.actions_per_turn()  # exhaust the budget
    assert _hints(e, 0) == []


def test_demoralized_figure_only_gets_the_demoralized_hint(db):
    e = build_engine(db, [
        ("human", "Werebear", (10, 10), 0.0, 0),
        ("llm", "Werebear", (9.0, 10), 0.0, 0),  # adjacent in the rear arc
    ], active="human")
    f = e.state.figure(0)
    f.demoralized = True
    hints = _hints(e, 0)
    assert len(hints) == 1 and "Demoralized" in hints[0]


def test_hint_formation_needs_three(db):
    # Two same-faction figures in base contact: short of the 3-figure minimum.
    e = build_engine(db, [
        ("human", "Demi-Magus", (10, 10), math.pi / 2, 0),
        ("human", "Demi-Magus", (11.0, 10), math.pi / 2, 0),  # touching same-faction ally
    ], active="human")
    hints = _hints(e, 0)
    assert any("3" in h and "same-faction" in h for h in hints), hints


def test_hint_flight_blocks_movement_formation(db):
    # Three touching Necropolis figures, but one flies (Order of Vladd): the hint
    # explains the exclusion AND that cancelling the optional ability fixes it.
    e = build_engine(db, [
        ("human", "Seething Knight", (10, 10), math.pi / 2, 0),
        ("human", "Seething Knight", (11.1, 10), math.pi / 2, 0),
        ("human", "Order Of Vladd", (12.2, 10), math.pi / 2, 0),
        ("llm", "Werebear", (20, 20), -math.pi / 2, 0),
    ], active="human")
    hints = _hints(e, 0)
    assert any("Flight" in h and "Vladd" in h and "cancel" in h.lower() for h in hints), hints
