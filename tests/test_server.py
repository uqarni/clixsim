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
