"""Local HTTP boundary around the headless engine (the renderer's server).

The engine stays the single source of truth (DP1). This exposes a thin JSON API a
browser client drives: read the game view, list an actionable figure's legal
candidates, dry-run a move, apply an intent, and run the opponent's turn. One
game session lives in-process (single-player local app).

    uvicorn clixengine.server:app --port 8000
"""

from __future__ import annotations

import json
import math
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
    pool_figures,
    sample_sealed_pool,
    set_pool_expansions,
)
from .candidates import generate_candidates, generate_formation_candidates
from .chat import build_system, chat_reply
from .config import get_api_key
from .data import load_db
from .demo import demo_armies
from .history import archive_game, history_dir, list_games
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
        # All-time archive feeds (untrimmed, per game): the full chat transcript
        # and the AI's per-turn rationales, checkpointed to the history dir.
        self.chat_archive: list[dict] = []
        self.ai_notes_log: list[dict] = []
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

            # Fail FAST and always answer inside the client's 45s abort window:
            # with timeout=30 + a retry the server could take ~60s, finish after
            # the browser gave up, and strand the reply as a ghost in chat_log
            # (the AI then "remembers" saying something the human never saw).
            self._chat_client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=0)
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
        turn = self.engine.state.turn_number if self.engine else 0
        self.chat_archive.append({"turn": turn, "role": "human", "content": message})
        self.chat_archive.append({"turn": turn, "role": "opponent", "content": reply})
        self.checkpoint(reason="chat")  # conversations archive immediately, not on next action
        return reply

    def checkpoint(self, reason: str = "checkpoint") -> None:
        """Refresh this game's all-time archive file (best-effort, never raises)."""
        if self.engine is None:
            return
        kind = "heuristic" if isinstance(self.opponent, HeuristicAI) else "llm"
        archive_game(self.engine, opponent_kind=kind, chat=self.chat_archive,
                     ai_notes=self.ai_notes_log, reason=reason)

    def record_opponent_notes(self, turn: int) -> None:
        """Bank this opponent turn's action rationales into the archive feed.
        ``turn`` is snapshotted when the stream STARTS — stream_turn calls
        end_turn() before finishing, so reading turn_number here would label
        every completed turn's notes with the NEXT turn."""
        if self.engine is None or not self.last_turn_notes:
            return
        self.ai_notes_log.append({"turn": turn, "notes": list(self.last_turn_notes)})
        self.checkpoint(reason="opponent_turn")

    def start_game(self, human_army: Army, llm_army: Army, build_total: int,
                   seed: int, opponent: str = "llm", board: float = 36.0,
                   with_terrain: bool = False, terrain_per_player: int = 3,
                   with_deploy: bool = False):
        """Finalize a game from two prebuilt armies + wire the opponent controller."""
        # Flush the outgoing game's final record before replacing it — the
        # all-time archive must not lose abandoned games.
        self.checkpoint(reason="replaced")
        self.chat_log, self.last_turn_notes = [], []
        self.chat_archive, self.ai_notes_log = [], []
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
        self.checkpoint(reason="created")
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
        self.checkpoint(reason="shutdown")
        try:
            _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            kind = "heuristic" if isinstance(self.opponent, HeuristicAI) else "llm"
            with open(_SESSION_FILE, "wb") as fh:
                pickle.dump({"engine": self.engine, "opponent": kind,
                             "chat_log": self.chat_log,
                             "chat_archive": self.chat_archive,
                             "ai_notes_log": self.ai_notes_log}, fh)
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
            # Archive feeds ride along so a deploy doesn't truncate the record.
            self.chat_log = list(data.get("chat_log") or [])
            self.chat_archive = list(data.get("chat_archive") or [])
            self.ai_notes_log = list(data.get("ai_notes_log") or [])
            if data.get("opponent") == "heuristic":
                self.opponent = HeuristicAI()
            else:
                llm = LLMOpponent()
                self.opponent = llm if llm.available else HeuristicAI()
            # Games created before the archive existed (or restored across a
            # deploy) enter the all-time record immediately, not on next action.
            self.checkpoint(reason="restored")
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
            rider=bool(d.get("rider", False)),  # Bound follow-up (P5 §2.1)
        )
    if kind == "close":
        return CloseIntent(
            attacker_uid=d["attacker_uid"], target_uid=d["target_uid"],
            variant=d.get("variant", "normal"), formation_uids=_tup(d.get("formation_uids")),
            heal_d6=bool(d.get("heal_d6", False)),
            rider=bool(d.get("rider", False)),  # Charge follow-up (P5 §2.1)
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


def _auto_deploy_llm(eng: Engine) -> None:
    """Doctrine-agnostic combined-arms deployment for the AI (plan 3.3): melee
    screen up front, shooters and support behind, faction-mates adjacent so
    formations exist on turn one. All 19 deploy events across three archived
    games were the human's — the AI had been starting in its raw draft row,
    shooters exposed, regardless of the terrain it just placed."""
    if eng.state.phase != "deploy" or getattr(eng, "llm_deployed", False):
        return
    figs = [f for f in eng.state.figures.values() if f.owner == "llm" and f.is_alive]
    if not figs:
        return
    h = eng.state.board.height
    w = eng.state.board.width

    def role(f) -> int:
        names = {a.name for a in f.definition.dial[f.current_click].abilities}
        if any(n in names for n in ("Healing", "Magic Healing", "Necromancy")):
            return 2  # support: safest row
        return 1 if f.range > 0 else 0  # shooters middle, melee front

    figs.sort(key=lambda f: (role(f), f.definition.faction, -f.points))
    # llm deploys at the TOP edge; "front" (toward the human) = smaller y.
    rows_y = [h - 2.45, h - 1.35, h - 0.6]
    by_role: dict[int, list] = {0: [], 1: [], 2: []}
    for f in figs:
        by_role[role(f)].append(f)
    if not by_role[0]:  # pure gunline: shooters take the front two rows
        by_role[0], by_role[1] = by_role[1], []
    for r_idx, members in by_role.items():
        if not members:
            continue
        spacing = 1.11  # touching -> formations are live immediately
        x0 = w / 2 - spacing * (len(members) - 1) / 2
        for i, f in enumerate(members):
            # A mounted figure facing the human (-y from the top edge) trails
            # its rear circle UP toward the edge: the front dot must sit at
            # y <= h - 3r or the capsule leaves the board (P5-R11) and every
            # nudge would be rejected, silently stranding it in the draft row.
            y = rows_y[r_idx]
            if f.mounted:
                y = min(y, h - 3 * f.base_radius)
            placed = False
            for nudge in (0.0, 0.6, -0.6, 1.2, -1.2, 2.4, -2.4):
                res = eng.deploy_figure(
                    "llm", f.uid, (x0 + spacing * i + nudge, y),
                    -math.pi / 2)
                if getattr(res, "ok", False):
                    placed = True
                    break
            if not placed:
                pass  # keep its draft-row spot — legal by construction
    eng.llm_deployed = True


def _best_spin_facing(eng: Engine, fig) -> float:
    """Threat-weighted free-spin facing (plan 1.6). The old face-the-mover rule
    was deterministic and got farmed: the human pinned with a cheap figure to
    force the spin, then rear-killed with the expensive one already in contact
    (3 of 5 deaths in one archived game). Face the most dangerous adjacent
    enemy instead; when the two worst both fit in the front arc, bisect."""
    threats = []
    for o in eng.state.opposing_contacts(fig):
        if not o.is_alive:
            continue
        odds = eng.hit_odds(o.uid, fig.uid, attack_type="close")
        threats.append((odds * max(1, o.damage), o))
    if not threats:
        return fig.facing
    threats.sort(key=lambda t: -t[0])
    best = threats[0][1]
    ang = angle_to(fig.position, best.position)
    if len(threats) >= 2:
        a2 = angle_to(fig.position, threats[1][1].position)
        diff = (a2 - ang + math.pi) % (2 * math.pi) - math.pi
        # Bisect only when both threats then sit inside the front arc.
        if abs(diff) < 2 * fig.arc_half_angle * 0.95:
            mid = ang + diff / 2
            if abs((ang - mid + math.pi) % (2 * math.pi) - math.pi) < fig.arc_half_angle:
                ang = mid
    return ang


def _auto_free_spin_opponents(eng: Engine, result) -> None:
    """When the human moves into base contact with the AI's figures, the AI takes
    its free spin (P4-R9): each contacted opponent re-faces toward its most
    DANGEROUS adjacent enemy (not blindly toward the mover). Keeps the rule
    symmetric without asking the AI to reason about facing."""
    if not getattr(result, "ok", False):
        return
    for e in getattr(result, "events", []):
        if e.get("type") != "free_spin_offer":
            continue
        for u in e.get("spinners", []):
            fig = eng.state.figures.get(u)
            if fig and fig.is_alive and fig.owner != eng.state.active_player:
                eng.apply(FreeSpinIntent(u, _best_spin_facing(eng, fig)))


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
    expansions: list[str] | None = None  # draft-pool sets; None = default (all)


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
    set_pool_expansions(req.expansions)  # scope demo_armies' pool to the request
    eng = SESSION.new_game(req.points, req.seed, req.board, req.opponent, req.single_faction)
    return game_view(eng)


def _brief(fid: int) -> dict:
    return _fig_brief(SESSION.db, SESSION.db.get(fid))


def _construct_stream(mode: str, points: int, opponent: str, seed: int,
                      human_ids: list[int] | None = None, terrain: bool = True,
                      deploy: bool = True, expansions: list[str] | None = None):
    """Server-sent events: settle the human army (drafted by the client, or auto-
    built), stream the LLM drafting its own army with reasoning, then finalize."""
    db = SESSION.db
    set_pool_expansions(expansions)  # New Game set checkboxes scope the pools
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

        # Planning pass (user request): take stock of the WHOLE pool, reason,
        # and commit to a formation + strategy BEFORE picking figure-by-figure.
        if llm_pool is not None:
            planning_pool = [db.get(i) for i in sorted(set(llm_pool))]
        else:
            planning_pool = _affordable(db, None, budget, set(), None)
        plan = builder.make_plan(db, planning_pool, budget)
        if plan:
            draft_notes.append("PLAN: " + plan.get("strategy", ""))
            yield sse({"type": "plan", "plan": plan})
        # Pick cap scales with the budget — a flat 12 stranded ~140 pts of a
        # 400-pt draft built from cheap figures (it's a runaway guard, not a
        # design constraint).
        for step in range(max(12, budget // 10)):
            cands = _affordable(db, llm_pool, remaining, used_uniques, pool_counts)
            if not cands:
                break
            army_brief = [{"name": db.get(i).short_name, "points": db.get(i).points,
                           "faction": db.get(i).faction,  # the fallback's faction filter reads this
                           "role": _role(db.get(i))} for i in llm_ids]
            pick, reason, used_llm = builder.pick(db, cands, army_brief, remaining, budget, seed * 100 + step)
            if pick is None:
                # Draft-stop guard (plan 1.5): an early -1 used to be honored
                # unconditionally (111/200-pt army). Refuse to stop while real
                # budget remains: greedy-fill straight from _affordable, which
                # respects the SEALED pool's remaining pulls and used uniques —
                # the old top-up consulted the full pool and could pick only
                # already-consumed figures, adding nothing (171/200 sealed).
                majority = None
                if llm_ids:
                    from collections import Counter as _Counter
                    majority = _Counter(db.get(i).faction for i in llm_ids).most_common(1)[0][0]
                while remaining > max(10, budget * 0.05):
                    fill_cands = _affordable(db, llm_pool, remaining, used_uniques, pool_counts)
                    if not fill_cands:
                        break
                    f = next((c for c in fill_cands if c.faction == majority), fill_cands[0])
                    llm_ids.append(f.id)
                    remaining -= f.points
                    if f.is_unique:
                        used_uniques.add(f.id)
                    if pool_counts is not None:
                        pool_counts[f.id] -= 1
                    draft_notes.append(f"{f.short_name} ({f.points}pts): budget top-up "
                                       "(an under-strength army loses at the draft table)")
                    yield sse({"type": "llm_pick", "figure": _brief(f.id),
                               "reasoning": "Topping up the remaining budget.",
                               "used_llm": False, "remaining": remaining,
                               "army": [_brief(i) for i in llm_ids],
                               "points": budget - remaining})
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
        SESSION.engine.draft_notes = draft_notes[:20]
        # The battle picker + chat inherit the draft PLAN (its game plan), not
        # just the doctrine flavor — so play executes the strategy it drafted for.
        if builder.plan.get("strategy"):
            SESSION.engine.doctrine = (
                f"{builder.doctrine} — this game's plan: {builder.plan['strategy']}")
        _auto_deploy_llm(SESSION.engine)  # deploy-only games (no terrain phase)
        yield sse({"type": "ready", "view": game_view(SESSION.engine)})
    except Exception as e:  # never leave the client hanging
        yield sse({"type": "error", "message": str(e)})


@app.get("/api/new_game_stream")
def new_game_stream(mode: str = "preconstructed", points: int = 200,
                    opponent: str = "llm", seed: int = 1, human_ids: str = "",
                    terrain: bool = True, deploy: bool = True, expansions: str = ""):
    ids = [int(x) for x in human_ids.split(",") if x.strip()] or None
    exps = [e.strip() for e in expansions.split(",") if e.strip()] or None
    return StreamingResponse(
        _construct_stream(mode, points, opponent, seed, ids, terrain, deploy, exps),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat")
def chat(req: ChatReq):
    return {"reply": SESSION.chat(req.message, req.history)}


@app.get("/api/roster")
def roster(expansions: str = ""):
    """Full drafting roster for the preconstructed builder, scoped to the
    requested sets (comma-separated; empty = the current default). Filters
    LOCALLY — a read-only browse must not mutate the shared pool selection
    under an in-flight construction stream."""
    from .build import DEFAULT_POOL_EXPANSIONS, KNOWN_EXPANSIONS
    db = SESSION.db
    exps = {e.strip() for e in expansions.split(",")
            if e.strip() and e.strip() in KNOWN_EXPANSIONS} or set(DEFAULT_POOL_EXPANSIONS)
    figs = sorted(
        (f for f in db.all_figures()
         if getattr(f, "expansion", "Rebellion") in exps),
        key=lambda f: (f.faction, -f.points, f.short_name))
    return {"figures": [_fig_brief(db, f) for f in figs]}


@app.get("/api/sealed_packs")
def sealed_packs(seed: int = 1, expansions: str = ""):
    """The human's 4 booster packs (5 figures each) to open one at a time. Uses
    the same seed derivation AND the same expansion scope the construction
    endpoint validates the draft against — a mismatch dead-ends the flow with
    'army uses figures not in your pool'."""
    db = SESSION.db
    exps = [e.strip() for e in expansions.split(",") if e.strip()] or None
    set_pool_expansions(exps)
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
    if result.ok:
        _auto_deploy_llm(eng)  # terrain may have just completed -> AI arranges its line
        SESSION.checkpoint(reason="terrain")
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
    _auto_deploy_llm(eng)
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
    _auto_deploy_llm(eng)
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
                SESSION.checkpoint(reason="terrain")
                yield sse({"type": "place", "summary": r.summary, "reasoning": reason,
                           "used_llm": used, "view": game_view(eng)})
            _auto_deploy_llm(eng)  # alternation may have ended -> AI arranges its line
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


@app.get("/api/formation_attack_options")
def formation_attack_options(uids: str):
    """Assist-attack options for a player-chosen group (engine-computed: a legal
    option is exactly an intent the engine will accept)."""
    eng = SESSION.require()
    try:
        ids = [int(x) for x in uids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "uids must be a comma-separated list of ints")
    return {"options": eng.formation_attack_options(ids)}


@app.get("/api/history")
def history():
    """All-time archived game summaries (full records live one-per-game on disk)."""
    return {"dir": str(history_dir()), "games": list_games()}


@app.post("/api/finish_deploy")
def finish_deploy():
    """The human is done arranging — begin the first battle turn."""
    eng = SESSION.require()
    result = eng.finish_deploy(SESSION.human_side)
    SESSION.checkpoint(reason="deploy_done")
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
    if result.ok:
        SESSION.checkpoint(reason="intent")
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
    SESSION.checkpoint(reason="end_turn")
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
            notes_turn = eng.state.turn_number  # before stream_turn's end_turn()
            recent = [f"turn {n['turn']}: " + " | ".join(n["notes"][:3])
                      for n in SESSION.ai_notes_log[-3:]]
            for step in SESSION.opponent.stream_turn(
                    eng, table_talk=SESSION.chat_log[-8:], memory=recent):
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
                        SESSION.record_opponent_notes(notes_turn)
                        yield sse({"type": "free_spin", "spinners": spinners,
                                   "by": offer.get("by"), "view": game_view(eng)})
                        return
            SESSION.record_opponent_notes(notes_turn)
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
