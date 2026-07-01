import math

import pytest

from clixengine.intents import MoveIntent, PassIntent

from .conftest import build_engine


def three_fig(db, build_total=200):
    return build_engine(
        db,
        [
            ("human", "Werebear", (10, 18), 0.0, 0),
            ("human", "Werebear", (14, 18), 0.0, 0),
            ("llm", "Werebear", (26, 18), 0.0, 0),
        ],
        build_total=build_total,
    )


def test_actions_per_turn(db):
    assert three_fig(db, 200).state.actions_per_turn() == 2
    assert three_fig(db, 100).state.actions_per_turn() == 1
    assert three_fig(db, 300).state.actions_per_turn() == 3


def test_no_warrior_two_actions_per_turn(db):
    e = three_fig(db, 200)
    assert e.apply(MoveIntent(0, (10, 20), 0.0)).ok
    r2 = e.apply(MoveIntent(0, (10, 22), 0.0))
    assert not r2.ok and r2.reason == "already_acted"


def test_action_count_limit(db):
    e = three_fig(db, 100)  # only 1 action per turn
    assert e.apply(MoveIntent(0, (10, 20), 0.0)).ok
    r2 = e.apply(MoveIntent(1, (14, 20), 0.0))
    assert not r2.ok and r2.reason == "no_actions"


def test_turn_alternation(db):
    e = three_fig(db, 100)
    assert e.state.active_player == "human"
    n = e.state.turn_number
    e.end_turn()
    assert e.state.active_player == "llm"
    assert e.state.turn_number == n + 1


def _next_human_turn(e):
    """Advance from a human turn through the llm turn back to human."""
    e.end_turn()  # human -> llm
    e.end_turn()  # llm -> human


def test_pushing_damage_on_second_consecutive_action(db):
    e = three_fig(db, 100)
    f = e.state.figure(0)
    # Turn 1: move (tokens -> 1, no pushing).
    res = e.apply(MoveIntent(0, (10, 20), 0.0))
    assert res.ok
    assert f.action_tokens == 1
    assert not any(ev["type"] == "push_damage" for ev in res.events)
    click_after_t1 = f.current_click
    _next_human_turn(e)
    # Turn 3 (human again): move again -> pushing (1 click of damage).
    res = e.apply(MoveIntent(0, (10, 22), 0.0))
    assert res.ok
    assert f.action_tokens == 2
    assert any(ev["type"] == "push_damage" for ev in res.events)
    assert f.current_click == click_after_t1 + 1


def test_cannot_act_three_consecutive_turns(db):
    e = three_fig(db, 100)
    e.apply(MoveIntent(0, (10, 20), 0.0))  # token 1
    _next_human_turn(e)
    e.apply(MoveIntent(0, (10, 22), 0.0))  # token 2 (pushing)
    _next_human_turn(e)
    f = e.state.figure(0)
    assert f.action_tokens == 2
    assert f not in e.actionable_figures()  # excluded from acting
    r = e.apply(MoveIntent(0, (10, 24), 0.0))
    assert not r.ok and r.reason == "pushed_out"


def test_pass_clears_push_tokens(db):
    e = three_fig(db, 100)
    e.apply(MoveIntent(0, (10, 20), 0.0))  # token 1
    _next_human_turn(e)
    e.apply(PassIntent(0))  # rest -> clears at turn end
    _next_human_turn(e)
    assert e.state.figure(0).action_tokens == 0
    # A fresh move now is not a pushing action.
    res = e.apply(MoveIntent(0, (10, 22), 0.0))
    assert not any(ev["type"] == "push_damage" for ev in res.events)
