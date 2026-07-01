"""Regression tests for issues found in the subagent test sweep."""

import math

import pytest

from clixengine.intents import MoveIntent

from .conftest import build_engine


def test_move_push_selfkill_ends_game(db):
    # A move whose pushing damage KO's the last figure on a side must end the
    # game (previously _apply_move never called _check_victory).
    e = build_engine(
        db,
        [
            ("human", "Werebear", (18, 18), 0.0, 8),  # last live click
            ("llm", "Werebear", (18, 30), math.pi, 0),
        ],
        build_total=100,
    )
    h = e.state.figure(0)
    h.action_tokens = 1  # next non-pass action pushes (deals 1 click)
    assert not e.state.ended
    res = e.apply(MoveIntent(0, (18, 16), 0.0))  # move away (not into contact)
    assert res.ok
    assert any(ev["type"] == "push_damage" for ev in res.events)
    assert h.eliminated
    assert e.state.ended and e.state.winner == "llm"


def test_demoralized_may_not_move_into_contact(db):
    e = build_engine(
        db,
        [
            ("human", "Werebear", (18, 18), 0.0, 8),  # demoralized click
            ("llm", "Werebear", (18, 22), math.pi, 0),
        ],
    )
    h = e.state.figure(0)
    assert h.is_demoralized
    # Destination puts it in base contact with the opponent -> rejected.
    res = e.apply(MoveIntent(0, (18, 20.9), 0.0))
    assert not res.ok and res.reason == "demoralized_contact"
    # A move that does NOT enter contact is still allowed (flee).
    res2 = e.apply(MoveIntent(0, (18, 16), 0.0))
    assert res2.ok


def test_all_demoralized_army_scores_no_survival_vp(db):
    # P4-R37: if all your figures are captured/demoralized, zero survival points.
    e = build_engine(
        db,
        [
            ("human", "Werebear", (10, 18), 0.0, 8),  # demoralized, sole human figure
            ("llm", "Chaos Mage", (20, 18), 0.0, 0),
        ],
    )
    assert e.state.figure(0).is_demoralized and e.state.figure(0).is_alive
    vp = e.victory_points()
    assert vp["human"] == 0
    assert vp["llm"] == e.state.figure(1).points


def test_partial_demoralized_army_still_scores_survivors(db):
    # With at least one fighting figure, survivors (including the demoralized one)
    # still score survival VP.
    e = build_engine(
        db,
        [
            ("human", "Werebear", (10, 18), 0.0, 8),  # demoralized
            ("human", "Chaos Mage", (12, 18), 0.0, 0),  # fighting
            ("llm", "Werebear", (26, 18), 0.0, 0),
        ],
    )
    vp = e.victory_points()
    assert vp["human"] == e.state.figure(0).points + e.state.figure(1).points
