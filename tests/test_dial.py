import math

import pytest

from clixengine.geometry import Vec
from clixengine.state import Figure


def make_fig(db, short_name, click=0, owner="human"):
    fdef = db.find(short_name)[0]
    return Figure(uid=0, definition=fdef, owner=owner, position=Vec(0, 0), facing=0.0,
                  current_click=click)


def test_take_clicks_advances_and_eliminates(db):
    f = make_fig(db, "Werebear")  # 9 live clicks (0..8)
    assert f.current_click == 0
    assert f.take_clicks(2) == 2
    assert f.current_click == 2
    assert f.is_alive
    # Push to the last click, then one more click eliminates.
    f.take_clicks(6)
    assert f.current_click == 8
    assert f.is_alive
    applied = f.take_clicks(1)
    assert applied == 1
    assert f.eliminated
    assert not f.is_alive
    # Further damage is a no-op.
    assert f.take_clicks(3) == 0


def test_heal_never_past_start(db):
    f = make_fig(db, "Werebear", click=4)
    assert f.heal_clicks(2) == 2
    assert f.current_click == 2
    assert f.heal_clicks(10) == 2  # only 2 clicks back to Starting Position
    assert f.current_click == 0
    assert f.heal_clicks(1) == 0


def test_cannot_heal_eliminated(db):
    f = make_fig(db, "Werebear")
    f.take_clicks(20)
    assert f.eliminated
    assert f.heal_clicks(3) == 0


def test_health_fraction(db):
    f = make_fig(db, "Werebear")
    assert f.health_fraction() == pytest.approx(1.0)
    f.take_clicks(9)  # off the end -> dead
    assert f.health_fraction() == 0.0


def test_demoralized_click_detected(db):
    f = make_fig(db, "Werebear")
    assert not f.is_demoralized
    f.take_clicks(8)  # click 8 carries the Demoralized ability
    assert f.current_click == 8
    assert f.is_demoralized


def test_optional_ability_can_be_cancelled(db):
    f = make_fig(db, "Werebear", click=3)  # has Toughness (optional? mandatory)
    ids = f.active_ability_ids()
    if ids:
        an = next(iter(ids))
        f.disabled_ability_ids.add(an)
        assert an not in f.active_ability_ids()
