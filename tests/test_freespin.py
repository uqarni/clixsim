"""Free spin (P4-R9): a figure an opponent moves into contact with may re-face for
free — no action, no token, no pushing — and only the contacted defender may do it."""

import math

from clixengine.intents import FreeSpinIntent, MoveIntent

from .conftest import build_engine


def _offer(events):
    return next((e for e in events if e["type"] == "free_spin_offer"), None)


def test_move_into_contact_offers_free_spin_to_defender(db):
    e = build_engine(db, [
        ("llm", "Werebear", (10, 10), 0.0, 0),     # active mover
        ("human", "Werebear", (13, 10), math.pi, 0),  # stationary defender (uid 1)
    ], active="llm")
    res = e.apply(MoveIntent(0, (11.9, 10), 0.0))  # slide into base contact
    assert res.ok
    offer = _offer(res.events)
    assert offer is not None and 1 in offer["spinners"] and offer["by"] == 0

    human = e.state.figure(1)
    spent_before = e._actions_spent
    tokens_before = human.action_tokens
    r2 = e.apply(FreeSpinIntent(1, math.pi / 2))
    assert r2.ok
    assert abs(human.facing - math.pi / 2) < 1e-9      # re-faced
    assert human.action_tokens == tokens_before        # no token
    assert 1 not in e._acted_uids                       # not marked acted
    assert e._actions_spent == spent_before             # no action spent


def test_free_spin_rejected_for_active_mover(db):
    e = build_engine(db, [
        ("llm", "Werebear", (10, 10), 0.0, 0),
        ("human", "Werebear", (13, 10), math.pi, 0),
    ], active="llm")
    e.apply(MoveIntent(0, (11.9, 10), 0.0))
    # The mover (active player) is now in contact but may NOT free-spin.
    r = e.apply(FreeSpinIntent(0, 1.0))
    assert not r.ok and r.reason == "not_defender"


def test_free_spin_rejected_when_not_contacted(db):
    e = build_engine(db, [
        ("llm", "Werebear", (10, 10), 0.0, 0),
        ("human", "Werebear", (30, 30), 0.0, 0),  # far away, not in contact
    ], active="llm")
    r = e.apply(FreeSpinIntent(1, 1.0))
    assert not r.ok and r.reason == "not_contacted"


def test_move_without_new_contact_offers_nothing(db):
    e = build_engine(db, [
        ("llm", "Werebear", (10, 10), 0.0, 0),
        ("human", "Werebear", (30, 30), 0.0, 0),
    ], active="llm")
    res = e.apply(MoveIntent(0, (12, 10), 0.0))  # moves, contacts nobody
    assert res.ok and _offer(res.events) is None
