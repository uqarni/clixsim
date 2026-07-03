"""HTTP boundary contract (the renderer's server). Uses the heuristic opponent
so no live LLM calls happen in tests."""

import pytest

from clixengine.server import app

TestClient = pytest.importorskip("fastapi.testclient").TestClient


@pytest.fixture()
def client():
    c = TestClient(app)
    c.post("/api/new_game", json={"points": 200, "seed": 1, "opponent": "heuristic"})
    return c


def test_new_game_returns_full_view(client):
    v = client.get("/api/state").json()
    assert v["meta"]["active_player"] == "human"
    assert v["figures"] and all("dial" in f and "base_radius" in f for f in v["figures"])


def test_candidate_intent_round_trip(client):
    v = client.get("/api/state").json()
    uid = next(f["uid"] for f in v["figures"] if f["owner"] == "human" and f["can_act"])
    body = client.get(f"/api/candidates/{uid}").json()
    assert {"candidates", "hints"} <= set(body) and isinstance(body["hints"], list)
    cands = body["candidates"]
    assert cands and all({"kind", "label", "annotation", "intent"} <= set(c) for c in cands)
    # The intent a candidate carries must apply cleanly when sent straight back.
    res = client.post("/api/intent", json=cands[0]["intent"]).json()
    assert res["ok"] is True and "view" in res


def test_validate_move_endpoint(client):
    v = client.get("/api/state").json()
    fig = next(f for f in v["figures"] if f["owner"] == "human" and f["can_act"])
    x, y = fig["pos"]
    r = client.post("/api/validate_move",
                    json={"figure_uid": fig["uid"], "dest": [x, y + 1.0], "facing": 1.57}).json()
    assert r["ok"] is True and "break_away" in r


def test_unknown_intent_kind_is_rejected(client):
    r = client.post("/api/intent", json={"kind": "teleport", "figure_uid": 0})
    assert r.status_code == 400


def test_draft_fills_a_big_budget_with_faction_concentration():
    """A flat 12-pick cap stranded ~140 pts of a 400-pt draft, and the server's
    army_brief lacked the faction field the pick() fallback filters on — the
    live heuristic path drafted faction salads while the tests (which used
    _fig_brief) passed."""
    import json
    from collections import Counter

    from clixengine.data import load_db
    from clixengine.server import _construct_stream

    db = load_db()
    any_fig = db.all_figures()[0].id
    events = []
    for chunk in _construct_stream("pre", 400, "heuristic", seed=9,
                                   human_ids=[any_fig], terrain=False, deploy=False):
        events.append(json.loads(chunk.removeprefix("data: ")))
    army = next(e for e in reversed(events) if e["type"] == "llm_army")
    assert army["points"] >= 360, f"stranded budget: {army['points']}/400"
    facs = Counter(f["faction"] for f in army["army"])
    assert max(facs.values()) >= 3, f"no formation block on the live path: {dict(facs)}"


def test_sealed_draft_topup_uses_remaining_pool():
    """The 171/200 sealed under-draft: the old top-up consulted the FULL pool
    (including consumed pulls), matched nothing available, and added zero
    figures. Force the LLM's early stop and require the guard to fill from
    what's actually left."""
    import json

    from clixengine.build import ArmyBuilder
    from clixengine.data import load_db
    from clixengine.server import _construct_stream

    db = load_db()
    # Force -1 on the very first pick, heuristic-opponent path (no API).
    orig = ArmyBuilder.pick
    def early_stop(self, db_, cands, brief, remaining, budget, seed):
        return None, "I like a lean army.", False
    ArmyBuilder.pick = early_stop
    try:
        events = []
        for chunk in _construct_stream("sealed", 200, "heuristic", seed=17,
                                       human_ids=None, terrain=False, deploy=False):
            events.append(json.loads(chunk.removeprefix("data: ")))
    finally:
        ArmyBuilder.pick = orig
    army = next(e for e in reversed(events) if e["type"] == "llm_army")
    assert army["points"] >= 160, f"sealed top-up failed again: {army['points']}/200"
