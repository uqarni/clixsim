"""Local HTTP boundary around the headless engine (the renderer's server).

The engine stays the single source of truth (DP1). This exposes a thin JSON API a
browser client drives: read the game view, list an actionable figure's legal
candidates, dry-run a move, apply an intent, and run the opponent's turn. One
game session lives in-process (single-player local app).

    uvicorn clixengine.server:app --port 8000
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai.heuristic import HeuristicAI
from .ai.llm import LLMOpponent
from .candidates import generate_candidates, generate_formation_candidates
from .data import load_db
from .demo import demo_armies
from .engine import Engine
from .intents import (
    CloseIntent,
    LevitateIntent,
    MoveIntent,
    NecromancyIntent,
    PassIntent,
    RangedIntent,
    RegenerateIntent,
)
from .setup import build_game
from .view import game_view

app = FastAPI(title="Clix Engine")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ------------------------------------------------------------------ #
# In-process game session
# ------------------------------------------------------------------ #
class Session:
    def __init__(self) -> None:
        self.db = load_db()
        self.engine: Engine | None = None
        self.opponent = None  # controller for the "llm" side
        self.human_side = "human"

    def new_game(self, points: int, seed: int, board: float = 36.0, opponent: str = "llm"):
        human_army, llm_army = demo_armies(points, seed=seed)
        self.engine = build_game(human_army, llm_army, build_total=points, seed=seed,
                                 board_size=board)
        if opponent == "heuristic":
            self.opponent = HeuristicAI()
        else:
            llm = LLMOpponent()
            self.opponent = llm if llm.available else HeuristicAI()
        return self.engine

    def require(self) -> Engine:
        if self.engine is None:
            raise HTTPException(409, "no active game — POST /api/new_game first")
        return self.engine


SESSION = Session()


@app.on_event("startup")
def _bootstrap() -> None:
    # Start with a default game so the client's initial GET /api/state works
    # without an explicit new-game round-trip (single-player local app).
    if SESSION.engine is None:
        SESSION.new_game(points=200, seed=1)


# ------------------------------------------------------------------ #
# Intent (de)serialization — the client round-trips the intent dict
# ------------------------------------------------------------------ #
def _tup(seq):
    return tuple(seq) if seq is not None else ()


def intent_from_dict(d: dict):
    kind = d.get("kind")
    if kind == "move":
        return MoveIntent(
            figure_uid=d["figure_uid"], dest=_tup(d["dest"]), facing=float(d["facing"]),
            free=bool(d.get("free", False)),
            formation_uids=_tup(d.get("formation_uids")),
            member_dests=tuple(_tup(m) for m in d.get("member_dests", ())),
            member_facings=_tup(d.get("member_facings")),
        )
    if kind == "ranged":
        return RangedIntent(
            attacker_uid=d["attacker_uid"], target_uids=_tup(d["target_uids"]),
            variant=d.get("variant", "normal"), formation_uids=_tup(d.get("formation_uids")),
        )
    if kind == "close":
        return CloseIntent(
            attacker_uid=d["attacker_uid"], target_uid=d["target_uid"],
            variant=d.get("variant", "normal"), formation_uids=_tup(d.get("formation_uids")),
            heal_d6=bool(d.get("heal_d6", False)),
        )
    if kind == "pass":
        return PassIntent(figure_uid=d["figure_uid"])
    if kind == "regenerate":
        return RegenerateIntent(figure_uid=d["figure_uid"])
    if kind == "necromancy":
        return NecromancyIntent(figure_uid=d["figure_uid"], revive_uid=d["revive_uid"])
    if kind == "levitate":
        return LevitateIntent(
            figure_uid=d["figure_uid"], target_uid=d["target_uid"],
            dest=_tup(d["dest"]), facing=float(d["facing"]),
        )
    raise HTTPException(400, f"unknown intent kind: {kind!r}")


def _candidate_view(c) -> dict:
    return {"kind": c.kind, "label": c.label, "annotation": c.annotation,
            "intent": asdict(c.intent)}


# ------------------------------------------------------------------ #
# Request models
# ------------------------------------------------------------------ #
class NewGameReq(BaseModel):
    points: int = 200
    seed: int = 1
    board: float = 36.0
    opponent: str = "llm"  # "llm" | "heuristic"


class ValidateMoveReq(BaseModel):
    figure_uid: int
    dest: tuple[float, float]
    facing: float = 0.0
    free: bool = False


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #
@app.post("/api/new_game")
def new_game(req: NewGameReq):
    eng = SESSION.new_game(req.points, req.seed, req.board, req.opponent)
    return game_view(eng)


@app.get("/api/state")
def state():
    return game_view(SESSION.require())


@app.get("/api/candidates/{uid}")
def candidates(uid: int):
    eng = SESSION.require()
    f = eng.state.figures.get(uid)
    if f is None:
        raise HTTPException(404, f"no figure {uid}")
    return [_candidate_view(c) for c in generate_candidates(eng, f)]


@app.get("/api/formation_candidates")
def formation_candidates():
    eng = SESSION.require()
    return [_candidate_view(c)
            for c in generate_formation_candidates(eng, eng.state.active_player)]


@app.post("/api/validate_move")
def validate_move(req: ValidateMoveReq):
    eng = SESSION.require()
    return eng.validate_move(req.figure_uid, req.dest, req.facing, req.free)


@app.post("/api/intent")
def apply_intent(intent: dict):
    eng = SESSION.require()
    result = eng.apply(intent_from_dict(intent))
    return {
        "ok": result.ok,
        "reason": getattr(result, "reason", None),
        "detail": getattr(result, "detail", None),
        "events": getattr(result, "events", []),
        "summary": getattr(result, "summary", ""),
        "view": game_view(eng),
    }


@app.post("/api/end_turn")
def end_turn():
    eng = SESSION.require()
    eng.end_turn()
    return game_view(eng)


@app.post("/api/opponent_turn")
def opponent_turn():
    eng = SESSION.require()
    if eng.state.active_player == SESSION.human_side:
        raise HTTPException(409, "it is the human's turn")
    decisions = SESSION.opponent.take_turn(eng)
    return {
        "decisions": [{"summary": getattr(d, "summary", str(d)),
                       "kind": getattr(getattr(d, "intent", None), "kind", None)}
                      for d in decisions],
        "view": game_view(eng),
    }


# Serve the built client (web/dist) at / when present, so one process serves all.
_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
