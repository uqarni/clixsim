"""Local HTTP boundary around the headless engine (the renderer's server).

The engine stays the single source of truth (DP1). This exposes a thin JSON API a
browser client drives: read the game view, list an actionable figure's legal
candidates, dry-run a move, apply an intent, and run the opponent's turn. One
game session lives in-process (single-player local app).

    uvicorn clixengine.server:app --port 8000
"""

from __future__ import annotations

import json
import pickle
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai.heuristic import HeuristicAI
from .ai.llm import LLMOpponent
from .army import Army, validate_army
from .build import (
    ArmyBuilder,
    _affordable,
    _fig_brief,
    _role,
    heuristic_army,
    sample_sealed_pool,
)
from .candidates import generate_candidates, generate_formation_candidates
from .chat import build_system, chat_reply
from .config import get_api_key
from .data import load_db
from .demo import demo_armies
from .engine import Engine
from .setup import build_game
from .terrain import TERRAIN_LIBRARY
from .terrain_ai import TerrainPlacer
from .geometry import angle_to
from .intents import (
    CloseIntent,
    FreeSpinIntent,
    LevitateIntent,
    MoveIntent,
    NecromancyIntent,
    PassIntent,
    RangedIntent,
    RegenerateIntent,
    ToggleAbilityIntent,
)
from .view import game_view, terrain_template_view

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Restore the previous session if one was saved (a deploy/restart must not
    # eat an in-progress game); otherwise start a default game so the client's
    # initial GET /api/state works without an explicit new-game round-trip.
    if SESSION.engine is None and not SESSION.restore():
        SESSION.new_game(points=200, seed=1)
    yield
    SESSION.persist()  # graceful shutdown (SIGTERM) saves the live game


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
        self._chat_client = None
        self._chat_system: str | None = None
        self._placer: TerrainPlacer | None = None
        # Two-way context between the CHAT persona and the ACTION PICKER: the
        # picker reads the recent table talk; the chat reads what the picker
        # actually did last turn (and why), so promises and play stay coherent.
        self.chat_log: list[dict] = []
        self.last_turn_notes: list[str] = []
        # Serializes terrain-placement streams so a duplicate/overlapping connection
        # (e.g. React StrictMode's dev double-mount) can't double-place. NEVER wait
        # on this indefinitely: a stream holds it across LLM calls, and a blocking
        # acquire in a request handler deadlocks the whole placement flow (the
        # request also never appears in the access log — it logs on completion).
        self.terrain_lock = threading.Lock()

    def terrain_guard(self, timeout: float = 3.0):
        """Acquire the terrain lock with a bounded wait; None if unavailable."""
        return self.terrain_lock.acquire(timeout=timeout)

    def placer(self) -> TerrainPlacer:
        if self._placer is None:
            self._placer = TerrainPlacer()
        return self._placer

    def chat(self, message: str, history: list[dict]) -> str:
        if self._chat_client is None:
            key = get_api_key()
            if not key:
                return "(Chat is unavailable — no API key configured.)"
            import anthropic

            self._chat_client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
            self._chat_system = build_system(self.db)
        try:
            reply = chat_reply(self._chat_client, self._chat_system, message, history,
                               self.engine, recent_moves=self.last_turn_notes)
        except Exception as e:
            return f"(Sorry — I hit an error: {e})"
        # Shared context loop: the action picker reads the recent table talk, so
        # banter promises ("I'll heal the Jarl this turn") can actually be kept.
        self.chat_log.append({"role": "human", "content": message})
        self.chat_log.append({"role": "opponent", "content": reply})
        del self.chat_log[:-16]
        return reply

    def start_game(self, human_army: Army, llm_army: Army, build_total: int,
                   seed: int, opponent: str = "llm", board: float = 36.0,
                   with_terrain: bool = False, terrain_per_player: int = 3,
                   with_deploy: bool = False):
        """Finalize a game from two prebuilt armies + wire the opponent controller."""
        self.engine = build_game(human_army, llm_army, build_total=build_total, seed=seed,
                                 board_size=board, db=self.db, with_terrain=with_terrain,
                                 terrain_per_player=terrain_per_player, with_deploy=with_deploy)
        # Identity for client/server sync detection: a tab rendering a DIFFERENT
        # game than the server holds (e.g. after a restart) can notice and resync.
        self.engine.game_id = uuid4().hex
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

    # -- persistence: a restart (deploy) must not eat an in-progress game -----
    def persist(self) -> None:
        """Best-effort snapshot of the live game on shutdown."""
        if self.engine is None:
            return
        try:
            _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            kind = "heuristic" if isinstance(self.opponent, HeuristicAI) else "llm"
            with open(_SESSION_FILE, "wb") as fh:
                pickle.dump({"engine": self.engine, "opponent": kind}, fh)
        except Exception:
            pass  # persistence is a convenience — never block shutdown

    def restore(self) -> bool:
        """Reload the last session; False (fresh game) on any mismatch/error —
        e.g. schema drift across deploys."""
        try:
            with open(_SESSION_FILE, "rb") as fh:
                data = pickle.load(fh)
            eng = data["engine"]
            eng.game_id = getattr(eng, "game_id", "") or uuid4().hex
            _ = game_view(eng)  # smoke-test the restored object against current code
            self.engine = eng
            if data.get("opponent") == "heuristic":
                self.opponent = HeuristicAI()
            else:
                llm = LLMOpponent()
                self.opponent = llm if llm.available else HeuristicAI()
            return True
        except Exception:
            return False


_SESSION_FILE = Path(__file__).resolve().parent.parent / ".claude" / "session.pkl"

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
    if kind == "free_spin":
        return FreeSpinIntent(figure_uid=d["figure_uid"], facing=float(d["facing"]))
    if kind == "toggle_ability":
        return ToggleAbilityIntent(
            figure_uid=d["figure_uid"], ability_id=int(d["ability_id"]), off=bool(d["off"]),
        )
    raise HTTPException(400, f"unknown intent kind: {kind!r}")


def _auto_free_spin_opponents(eng: Engine, result) -> None:
    """When the human moves into base contact with the AI's figures, the AI takes
    its free spin (P4-R9): each contacted opponent re-faces toward the mover. Keeps
    the rule symmetric without asking the AI to reason about facing."""
    if not getattr(result, "ok", False):
        return
    for e in getattr(result, "events", []):
        if e.get("type") != "free_spin_offer":
            continue
        mover = eng.state.figures.get(e.get("by"))
        if mover is None:
            continue
        for u in e.get("spinners", []):
            fig = eng.state.figures.get(u)
            if fig and fig.is_alive and fig.owner != eng.state.active_player:
                eng.apply(FreeSpinIntent(u, angle_to(fig.position, mover.position)))


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


class ChatReq(BaseModel):
    message: str
    history: list[dict] = []


# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #
@app.post("/api/new_game")
def new_game(req: NewGameReq):
    eng = SESSION.new_game(req.points, req.seed, req.board, req.opponent, req.single_faction)
    return game_view(eng)


def _brief(fid: int) -> dict:
    return _fig_brief(SESSION.db, SESSION.db.get(fid))


def _construct_stream(mode: str, points: int, opponent: str, seed: int,
                      human_ids: list[int] | None = None, terrain: bool = True,
                      deploy: bool = True):
    """Server-sent events: settle the human army (drafted by the client, or auto-
    built), stream the LLM drafting its own army with reasoning, then finalize."""
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
            yield sse({"type": "pool", "side": "llm", "pool": [_brief(i) for i in llm_pool]})

        # Human army: use the client's drafted list (validated) or auto-build it.
        if human_ids:
            human_army = Army(name="human-army", owner="human", figure_ids=list(human_ids))
            if mode == "sealed" and human_pool is not None:
                from collections import Counter
                need, have = Counter(human_ids), Counter(human_pool)
                if any(need[k] > have.get(k, 0) for k in need):
                    yield sse({"type": "error", "message": "army uses figures not in your pool"})
                    return
            v = validate_army(human_army, db, budget)
            if not v.ok:
                yield sse({"type": "error", "message": "; ".join(v.errors)})
                return
        else:
            human_army = heuristic_army(db, "human", budget, seed * 3 + 1, candidate_ids=human_pool)
        yield sse({"type": "human_army", "army": [_brief(i) for i in human_army.figure_ids],
                   "points": human_army.total_points(db)})

        # LLM army — drafted one figure at a time, streamed with reasoning.
        # "Heuristic (fast)" opponent drafts heuristically too (no LLM latency).
        builder = ArmyBuilder(seed=seed)  # per-game doctrine => varied armies
        if opponent == "heuristic":
            builder.available = False
        yield sse({"type": "llm_start", "available": builder.available})
        llm_ids: list[int] = []
        draft_notes: list[str] = []
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
            draft_notes.append(f"{pick.short_name} ({pick.points}pts): {reason}")
            yield sse({"type": "llm_pick", "figure": _brief(pick.id), "reasoning": reason,
                       "used_llm": used_llm, "remaining": remaining,
                       "army": [_brief(i) for i in llm_ids], "points": budget - remaining})
        if not llm_ids:  # safety net
            llm_ids = heuristic_army(db, "llm", budget, seed * 3 + 2, candidate_ids=llm_pool).figure_ids
        llm_army = Army(name="llm-army", owner="llm", figure_ids=llm_ids)
        yield sse({"type": "llm_army", "army": [_brief(i) for i in llm_ids],
                   "points": llm_army.total_points(db)})

        SESSION.start_game(human_army, llm_army, budget, seed, opponent,
                           with_terrain=terrain, with_deploy=deploy)
        # ONE agent across phases: the battle picker, terrain placer, and chat
        # all inherit the drafter's doctrine + pick reasoning (stored on the
        # engine so it survives restarts with the game).
        SESSION.engine.doctrine = builder.doctrine
        SESSION.engine.draft_notes = draft_notes[:12]
        yield sse({"type": "ready", "view": game_view(SESSION.engine)})
    except Exception as e:  # never leave the client hanging
        yield sse({"type": "error", "message": str(e)})


@app.get("/api/new_game_stream")
def new_game_stream(mode: str = "preconstructed", points: int = 200,
                    opponent: str = "llm", seed: int = 1, human_ids: str = "",
                    terrain: bool = True, deploy: bool = True):
    ids = [int(x) for x in human_ids.split(",") if x.strip()] or None
    return StreamingResponse(
        _construct_stream(mode, points, opponent, seed, ids, terrain, deploy),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat")
def chat(req: ChatReq):
    return {"reply": SESSION.chat(req.message, req.history)}


@app.get("/api/roster")
def roster():
    """Full drafting roster (all set pieces) for the preconstructed builder."""
    db = SESSION.db
    figs = sorted(db.all_figures(), key=lambda f: (f.faction, -f.points, f.short_name))
    return {"figures": [_fig_brief(db, f) for f in figs]}


@app.get("/api/sealed_packs")
def sealed_packs(seed: int = 1):
    """The human's 4 booster packs (5 figures each) to open one at a time. Uses
    the same seed derivation the construction endpoint validates the draft against."""
    db = SESSION.db
    pool = sample_sealed_pool(db, seed * 7 + 1)
    packs = [[_brief(i) for i in pool[k * 5:(k + 1) * 5]] for k in range(4)]
    return {"packs": packs}


def _terrain_busy(eng) -> dict:
    """Friendly non-blocking answer when the opponent's placement stream holds the
    terrain lock — the client shows it and the user simply retries."""
    return {
        "ok": False,
        "reason": "opponent_busy",
        "detail": "the opponent is still placing terrain — try again in a moment",
        "summary": "",
        "view": game_view(eng),
    }


class PlaceTerrainReq(BaseModel):
    key: str
    center: tuple[float, float]
    rotation: float = 0.0


@app.get("/api/terrain_library")
def terrain_library():
    """The curated placement palette (origin-centred shapes + rule flags)."""
    return {"pieces": [terrain_template_view(t) for t in TERRAIN_LIBRARY]}


@app.get("/api/terrain_types")
def terrain_types():
    """Terrain TYPES for the draw-your-own-polygon tool (key + rule flags + display)."""
    from .terrain import POLYGON_TYPES
    return {"types": [{"key": k, **v} for k, v in POLYGON_TYPES.items()]}


class PlaceTerrainPolygonReq(BaseModel):
    type: str
    polygon: list[tuple[float, float]]


@app.post("/api/place_terrain_polygon")
def place_terrain_polygon(req: PlaceTerrainPolygonReq):
    """The human places one hand-drawn terrain polygon during the setup phase."""
    eng = SESSION.require()
    if not SESSION.terrain_guard():
        return _terrain_busy(eng)
    try:
        result = eng.place_terrain_polygon(SESSION.human_side, req.type, req.polygon)
    finally:
        SESSION.terrain_lock.release()
    return {
        "ok": result.ok,
        "reason": getattr(result, "reason", None),
        "detail": getattr(result, "detail", None),
        "summary": getattr(result, "summary", ""),
        "view": game_view(eng),
    }


@app.get("/api/terrain_candidates")
def terrain_candidates(owner: str = "human"):
    """Legal example placements for ``owner`` (renderer hint / AI options)."""
    eng = SESSION.require()
    return {"candidates": eng.terrain_placement_candidates(owner)}


@app.post("/api/place_terrain")
def place_terrain(req: PlaceTerrainReq):
    """The human places one terrain piece during the setup phase."""
    eng = SESSION.require()
    if not SESSION.terrain_guard():  # don't race the opponent stream — but never hang
        return _terrain_busy(eng)
    try:
        result = eng.place_terrain(SESSION.human_side, req.key, req.center, req.rotation)
    finally:
        SESSION.terrain_lock.release()
    return {
        "ok": result.ok,
        "reason": getattr(result, "reason", None),
        "detail": getattr(result, "detail", None),
        "summary": getattr(result, "summary", ""),
        "view": game_view(eng),
    }


@app.post("/api/skip_terrain")
def skip_terrain():
    """The human forfeits their remaining terrain and hands off (Done placing)."""
    eng = SESSION.require()
    if not SESSION.terrain_guard():
        return _terrain_busy(eng)
    try:
        result = eng.skip_terrain_placement(SESSION.human_side)
    finally:
        SESSION.terrain_lock.release()
    return {
        "ok": result.ok,
        "reason": getattr(result, "reason", None),
        "summary": getattr(result, "summary", ""),
        "view": game_view(eng),
    }


@app.get("/api/terrain_placement_stream")
def terrain_placement_stream():
    """SSE: the opponent places its terrain (its run of the alternation), one piece
    at a time with reasoning; ends when it's the human's turn or setup completes."""
    eng = SESSION.require()

    def gen():
        def sse(o: dict) -> str:
            return f"data: {json.dumps(o)}\n\n"
        if eng.state.phase != "terrain":
            yield sse({"type": "done", "view": game_view(eng)})
            return
        # One placement stream at a time — a duplicate connection (StrictMode's
        # dev double-mount, or a refresh racing a zombie stream) no-ops instead of
        # queueing on the lock. NEVER block here: this generator holds the lock
        # across LLM calls, and a second waiter deadlocks the whole placement UI.
        if not SESSION.terrain_lock.acquire(blocking=False):
            yield sse({"type": "done", "view": game_view(eng)})
            return
        placer = SESSION.placer()
        allow_llm = not isinstance(SESSION.opponent, HeuristicAI)
        llm_ranged = any(f.is_ranged for f in eng.state.living("llm"))
        step = 0
        try:
            while eng.state.phase == "terrain" and eng.state.terrain_turn == "llm":
                if eng is not SESSION.engine:
                    break  # a new game replaced this engine — stop the zombie stream
                cands = eng.terrain_placement_candidates("llm")
                if not cands:  # nowhere legal left: forfeit the rest and hand off
                    eng.skip_terrain_placement("llm")
                    break
                context = {
                    "my_army_has_ranged": llm_ranged,
                    "my_doctrine": getattr(eng, "doctrine", ""),
                    "pieces_left": eng.state.terrain_budget.get("llm", 0),
                    "already_placed": [{"type": t.kind, "elevated": t.elevated, "owner": t.owner}
                                       for t in eng.state.terrain],
                }
                seed = 4000 + len(eng.state.terrain) * 7 + step
                choice, reason, used = placer.pick(cands, context, llm_ranged, seed, allow_llm)
                step += 1
                if choice is None:
                    # The placer gave up (shouldn't happen — candidates exist). Never
                    # strand the turn on "llm": forfeit so the human can proceed.
                    eng.skip_terrain_placement("llm")
                    break
                r = eng.place_terrain("llm", choice["key"], choice["center"], choice["rotation"])
                if not r.ok:  # pre-validated, so this is a bug-state: forfeit, don't strand
                    eng.skip_terrain_placement("llm")
                    break
                yield sse({"type": "place", "summary": r.summary, "reasoning": reason,
                           "used_llm": used, "view": game_view(eng)})
            yield sse({"type": "done", "view": game_view(eng)})
        except Exception as e:
            yield sse({"type": "error", "message": str(e), "view": game_view(eng)})
        finally:
            SESSION.terrain_lock.release()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class DeployFigureReq(BaseModel):
    uid: int
    pos: tuple[float, float]
    facing: float = 0.0


@app.post("/api/deploy_figure")
def deploy_figure(req: DeployFigureReq):
    """Reposition one of the human's figures within its starting area during setup."""
    eng = SESSION.require()
    result = eng.deploy_figure(SESSION.human_side, req.uid, req.pos, req.facing)
    return {
        "ok": result.ok,
        "reason": getattr(result, "reason", None),
        "detail": getattr(result, "detail", None),
        "summary": getattr(result, "summary", ""),
        "view": game_view(eng),
    }


@app.post("/api/finish_deploy")
def finish_deploy():
    """The human is done arranging — begin the first battle turn."""
    eng = SESSION.require()
    result = eng.finish_deploy(SESSION.human_side)
    return {
        "ok": result.ok,
        "reason": getattr(result, "reason", None),
        "view": game_view(eng),
    }


@app.get("/api/state")
def state():
    return game_view(SESSION.require())


@app.get("/api/candidates/{uid}")
def candidates(uid: int):
    eng = SESSION.require()
    f = eng.state.figures.get(uid)
    if f is None:
        raise HTTPException(404, f"no figure {uid}")
    return {
        "candidates": [_candidate_view(c) for c in generate_candidates(eng, f)],
        "hints": eng.figure_action_hints(f),
    }


@app.get("/api/formation_candidates")
def formation_candidates():
    eng = SESSION.require()
    return [_candidate_view(c)
            for c in generate_formation_candidates(eng, eng.state.active_player,
                                                   include_manual=True)]


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
    _auto_free_spin_opponents(eng, result)  # AI defenders re-face when the human contacts them
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


@app.get("/api/opponent_turn_stream")
def opponent_turn_stream():
    """SSE: the opponent's turn, one action at a time, with reasoning + the engine
    events for each (so the client shows its thinking and animates its moves)."""
    eng = SESSION.require()

    def gen():
        def sse(o: dict) -> str:
            return f"data: {json.dumps(o)}\n\n"
        if eng.state.active_player == SESSION.human_side:
            yield sse({"type": "error", "message": "it is the human's turn"})
            return
        try:
            SESSION.last_turn_notes = []  # fresh notes for this opponent turn
            for step in SESSION.opponent.stream_turn(eng, table_talk=SESSION.chat_log[-8:]):
                SESSION.last_turn_notes.append(f"{step['summary']} — {step['reasoning']}")
                yield sse({"type": "action", "summary": step["summary"],
                           "reasoning": step["reasoning"], "events": step["events"],
                           "fallback": step["fallback"], "view": game_view(eng)})
                # Free spin (P4-R9): if that move contacted the human's figures, PAUSE
                # the opponent's turn so the human can re-face before it acts again.
                # Abandoning this generator suspends stream_turn WITHOUT ending the
                # turn; the client re-opens the stream to resume after spinning.
                offer = next((e for e in step["events"]
                              if e.get("type") == "free_spin_offer"), None)
                if offer:
                    spinners = [
                        u for u in offer.get("spinners", [])
                        if (g := eng.state.figures.get(u)) is not None
                        and g.owner == SESSION.human_side and g.is_alive
                        and eng.state.opposing_contacts(g)
                    ]
                    if spinners:
                        yield sse({"type": "free_spin", "spinners": spinners,
                                   "by": offer.get("by"), "view": game_view(eng)})
                        return
            yield sse({"type": "done", "view": game_view(eng)})
        except Exception as e:
            yield sse({"type": "error", "message": str(e), "view": game_view(eng)})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Serve the built client (web/dist) at / when present, so one process serves all.
_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
