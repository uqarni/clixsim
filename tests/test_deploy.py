"""Figure deployment (P3-R5): arrange your army within your 3" starting area
before the battle begins — free, any number of times."""

import math

from clixengine.demo import demo_armies
from clixengine.setup import build_game


def _game(**kw):
    h, l = demo_armies(200, seed=1)
    return build_game(h, l, 200, seed=1, **kw)


def test_deploy_phase_opens_without_terrain():
    e = _game(with_deploy=True)
    assert e.state.phase == "deploy"


def test_deploy_figure_within_band_and_rejections(db):
    e = _game(with_deploy=True)
    # Single-base semantics: pick a non-mounted figure (mounted band fit has
    # its own test in test_mounted_rules.py).
    uid = next(f.uid for f in e.state.living("human") if not f.mounted)
    other = next(f.uid for f in e.state.living("human") if f.uid != uid)
    r = e.state.figure(uid).base_radius

    ok = e.deploy_figure("human", uid, (6.0, 1.0), math.pi / 2)
    assert ok.ok and abs(e.state.figure(uid).position.x - 6.0) < 1e-9

    assert e.deploy_figure("human", uid, (6.0, 8.0), 0.0).reason == "out_of_area"  # outside 3" band
    assert e.deploy_figure("human", uid, (0.1, 1.0), 0.0).reason == "off_board"     # base off edge
    onto = e.state.figure(other).position
    assert e.deploy_figure("human", uid, (onto.x, onto.y), 0.0).reason == "overlap"
    # can't deploy an opponent's figure
    llm_uid = next(f.uid for f in e.state.living("llm"))
    assert e.deploy_figure("human", llm_uid, (6.0, 1.0), 0.0).reason == "bad_figure"


def test_finish_deploy_starts_battle():
    e = _game(with_deploy=True)
    r = e.finish_deploy("human")
    assert r.ok and e.state.phase == "battle"
    assert e.state.active_player == e.state.first_player
    # deployment is closed now
    uid = next(f.uid for f in e.state.living("human"))
    assert e.deploy_figure("human", uid, (6.0, 1.0), 0.0).reason == "not_deploying"


def test_terrain_then_deploy_chain():
    e = _game(with_terrain=True, terrain_per_player=1, with_deploy=True)
    assert e.state.phase == "terrain"
    first = e.state.terrain_turn
    second = e.state.other_player(first)
    e.place_terrain(first, "boulder", (12, 18))
    e.place_terrain(second, "forest", (24, 18))
    # both terrain placed -> deployment opens (NOT battle yet)
    assert e.state.phase == "deploy"
    e.finish_deploy("human")
    assert e.state.phase == "battle"
