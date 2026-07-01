"""Client view-API: the rich read model + the read-only move dry-run."""

import math

from clixengine.view import figure_view, game_view

from .conftest import build_engine


def _engine(db):
    return build_engine(db, [
        ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
        ("human", "Werebear", (12, 10), math.pi / 2, 0),
        ("llm", "Storm Golem", (10, 20), -math.pi / 2, 0),
    ], active="human")


def test_game_view_shape(db):
    v = game_view(_engine(db))
    m = v["meta"]
    assert m["active_player"] == "human"
    assert m["board"] == {"width": 36.0, "height": 36.0}
    assert set(m["victory_points"]) == {"human", "llm"}
    assert "actions_remaining" in m and "actions_per_turn" in m
    assert len(v["figures"]) == 3
    assert [f["uid"] for f in v["figures"]] == [0, 1, 2]  # sorted, stable


def test_figure_view_exposes_full_dial_and_geometry(db):
    e = _engine(db)
    fv = figure_view(e, e.state.figure(0))
    # The full dial is present (the biggest gap the snapshot had).
    assert len(fv["dial"]) == e.state.figure(0).definition.num_live_clicks
    row = fv["dial"][fv["current_click"]]
    assert {"index", "speed", "attack", "defense", "damage", "abilities"} <= set(row)
    # Geometry needed to draw the base + arc wedge to scale.
    assert fv["base_radius"] == 0.55
    assert fv["arc_deg"] > 0
    # Turn flags + current-click convenience stats.
    assert fv["can_act"] is True and fv["acted"] is False
    assert fv["speed"] == e.state.figure(0).speed
    assert isinstance(fv["active_abilities"], list)


def test_game_view_includes_eliminated_figures(db):
    e = _engine(db)
    dead = e.state.figure(2)
    dead.eliminated = True
    v = game_view(e)
    dv = next(f for f in v["figures"] if f["uid"] == 2)
    assert dv["eliminated"] is True
    assert dv["health_fraction"] == 0.0
    assert dv["can_act"] is False


# --- validate_move dry-run -------------------------------------------------
def test_validate_move_ok_no_breakaway(db):
    e = _engine(db)
    r = e.validate_move(0, (10, 12), math.pi / 2)  # short legal step, no enemy contact
    assert r["ok"] is True
    assert r["break_away"]["needed"] is False


def test_validate_move_too_far_is_rejected(db):
    e = _engine(db)
    r = e.validate_move(0, (10, 34), math.pi / 2)  # well beyond speed
    assert r["ok"] is False and r["reason"] == "too_far"


def test_validate_move_reports_breakaway_when_in_contact(db):
    e = build_engine(db, [
        ("human", "Werebear", (10, 10), 0.0, 0),
        ("llm", "Werebear", (11.0, 10), math.pi, 0),  # in base contact
    ], active="human")
    r = e.validate_move(0, (10, 13), math.pi / 2)  # moving away from contact
    assert r["ok"] is True
    assert r["break_away"]["needed"] is True
    assert 0.0 < r["break_away"]["odds"] <= 1.0


def test_validate_move_does_not_mutate(db):
    e = _engine(db)
    before = e.state.figure(0).position.as_tuple()
    e.validate_move(0, (10, 12), math.pi / 2)
    assert e.state.figure(0).position.as_tuple() == before  # pure dry-run
