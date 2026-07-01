"""Local HTTP boundary around the headless engine (the renderer's server).

The engine stays the single source of truth (DP1). This exposes a thin JSON API a
browser client drives: read the game view, list an actionable figure's legal
candidates, dry-run a move, apply an intent, and run the opponent's turn. One
game session lives in-process (single-player local app).

    uvicorn clixengine.server:app --port 8000
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai.heuristic import HeuristicAI
from .ai.llm import LLMOpponent
from .army import Army
from .build import (
    ArmyBuilder,
    _affordable,
    _fig_brief,
    _role,
    heuristic_army,
    sample_sealed_pool,
)
from .candidates import generate_candidates, generate_formation_candidates
from .data import load_db
from .demo import demo_armies
from .engine import Engine
from .setup import build_game
from .intents import (
    CloseIntent,
    LevitateIntent,
    MoveIntent,
    NecromancyIntent,
    PassIntent,
    RangedIntent,
    RegenerateIntent,
    ToggleAbilityIntent,
)
from .view import game_view

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Start with a default game so the client's initial GET /api/state works
    # without an explicit new-game round-trip (single-player local app).
    if SESSION.engine is None:
        SESSION.new_game(points=200, seed=1)
    yield


app = FastAPI(title="Clix Engine", lifespan=_lifespan)
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

    def start_game(self, human_army: Army, llm_army: Army, build_total: int,
                   seed: int, opponent: str = "llm", board: float = 36.0):
        """Finalize a game from two prebuilt armies + wire the opponent controller."""
        self.engine = build_game(human_army, llm_army, build_total=build_total, seed=seed,
                                 board_size=board, db=self.db)
        if opponent == "heuristic":
            self.opponent = HeuristicAI()
        else:
            llm = LLMOpponent()
            self.opponent = llm if llm.available else HeuristicAI()
        return self.engine

    def new_game(self, points: int, seed: int, board: float = 36.0, opponent: str = "llm",
                 single_faction: bool = False):
        human_army, llm_army = demo_armies(points, seed=seed, single_faction=single_faction)
        return self.start_game(human_army, llm_army, points, seed, opponent, board)

    def require(self) -> Engine:
        if self.engine is None:
            raise HTTPException(409, "no active game — POST /api/new_game first")
        return self.engine


SESSION = Session()


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
    if kind == "toggle_ability":
        return ToggleAbilityIntent(
            figure_uid=d["figure_uid"], ability_id=int(d["ability_id"]), off=bool(d["off"]),
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
    single_faction: bool = False  # same-faction armies (enables formations)


class ValidateMoveReq(BaseModel):
    figure_uid: int
    dest: tuple[float, float]
    facing: float = 0.0
    free: bool = False


class ExplainReq(BaseModel):
    attacker_uid: int
    target_uid: int
    attack_type: str = "close"
    rear: bool = False


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #
@app.post("/api/new_game")
def new_game(req: NewGameReq):
    eng = SESSION.new_game(req.points, req.seed, req.board, req.opponent, req.single_faction)
    return game_view(eng)


def _brief(fid: int) -> dict:
    return _fig_brief(SESSION.db, SESSION.db.get(fid))


def _construct_stream(mode: str, points: int, opponent: str, seed: int):
    """Server-sent events: build the human army, stream the LLM drafting its army
    pick-by-pick with reasoning, then finalize the game."""
    db = SESSION.db
    budget = 200 if mode == "sealed" else max(100, points)

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    try:
        yield sse({"type": "start", "mode": mode, "budget": budget})

        human_pool = llm_pool = None
        if mode == "sealed":
            human_pool = sample_sealed_pool(db, seed * 7 + 1)
            llm_pool = sample_sealed_pool(db, seed * 7 + 2)
            yield sse({"type": "pool", "side": "human", "pool": [_brief(i) for i in human_pool]})
            yield sse({"type": "pool", "side": "llm", "pool": [_brief(i) for i in llm_pool]})

        # Human army — auto-built (from the sealed pool when sealed).
        human_army = heuristic_army(db, "human", budget, seed * 3 + 1, candidate_ids=human_pool)
        yield sse({"type": "human_army", "army": [_brief(i) for i in human_army.figure_ids],
                   "points": human_army.total_points(db)})

        # LLM army — drafted one figure at a time, streamed with reasoning.
        builder = ArmyBuilder()
        yield sse({"type": "llm_start", "available": builder.available})
        llm_ids: list[int] = []
        used_uniques: set[int] = set()
        remaining = budget
        pool_counts = None
        if llm_pool is not None:
            pool_counts = {}
            for fid in llm_pool:
                pool_counts[fid] = pool_counts.get(fid, 0) + 1
        for step in range(12):
            cands = _affordable(db, llm_pool, remaining, used_uniques, pool_counts)
            if not cands:
                break
            army_brief = [{"name": db.get(i).short_name, "points": db.get(i).points,
                           "role": _role(db.get(i))} for i in llm_ids]
            pick, reason, used_llm = builder.pick(db, cands, army_brief, remaining, budget, seed * 100 + step)
            if pick is None:
                yield sse({"type": "llm_stop", "reasoning": reason, "used_llm": used_llm})
                break
            llm_ids.append(pick.id)
            remaining -= pick.points
            if pick.is_unique:
                used_uniques.add(pick.id)
            if pool_counts is not None:
                pool_counts[pick.id] -= 1
            yield sse({"type": "llm_pick", "figure": _brief(pick.id), "reasoning": reason,
                       "used_llm": used_llm, "remaining": remaining,
                       "army": [_brief(i) for i in llm_ids], "points": budget - remaining})
        if not llm_ids:  # safety net
            llm_ids = heuristic_army(db, "llm", budget, seed * 3 + 2, candidate_ids=llm_pool).figure_ids
        llm_army = Army(name="llm-army", owner="llm", figure_ids=llm_ids)
        yield sse({"type": "llm_army", "army": [_brief(i) for i in llm_ids],
                   "points": llm_army.total_points(db)})

        SESSION.start_game(human_army, llm_army, budget, seed, opponent)
        yield sse({"type": "ready", "view": game_view(SESSION.engine)})
    except Exception as e:  # never leave the client hanging
        yield sse({"type": "error", "message": str(e)})


@app.get("/api/new_game_stream")
def new_game_stream(mode: str = "preconstructed", points: int = 200,
                    opponent: str = "llm", seed: int = 1):
    return StreamingResponse(
        _construct_stream(mode, points, opponent, seed),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


@app.post("/api/explain")
def explain(req: ExplainReq):
    eng = SESSION.require()
    if req.attacker_uid not in eng.state.figures or req.target_uid not in eng.state.figures:
        raise HTTPException(404, "unknown figure")
    return eng.explain_attack(req.attacker_uid, req.target_uid, req.attack_type, req.rear)


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
