"""All-time game history archive.

Every game is checkpointed to its own JSON file — one per game_id — so the
full record (armies, doctrine, every engine event, the complete chat, and the
AI's per-turn rationales) survives new games, restarts, and deploys. This is
the dataset for improving the LLM opponent later: the engine is deterministic,
so (seed + events) replays a game exactly, and the rationales/chat pair each
decision with the model's stated reasoning.

Files live outside the repo (default ``~/.clixengine/history/``) so worktree
churn can't eat them; ``CLIX_HISTORY_DIR`` overrides the location. Writes are
best-effort and atomic (tmp + rename): archiving must never break a game.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

# Serializes writers within this process (the opponent-stream thread and a
# request handler can checkpoint the same game concurrently); the unique tmp
# name guards the rename against any writer the lock doesn't cover.
_WRITE_LOCK = threading.Lock()


def history_dir() -> Path:
    override = os.environ.get("CLIX_HISTORY_DIR", "").strip()
    return Path(override) if override else Path.home() / ".clixengine" / "history"


# Which checkout produced a record: the server's repo/worktree dirname (the
# prod worktree vs clix-dev vs a review agent's scratch checkout). The training
# set filters on this — no launch-command discipline required.
_INSTANCE = Path(__file__).resolve().parents[1].name


def _army_brief(engine, owner: str) -> list[dict]:
    return [
        {
            "uid": f.uid,
            "figure_id": f.definition.id,
            "name": f.definition.short_name,
            "faction": f.definition.faction,
            "points": f.definition.points,
            "rank": f.definition.rank,
            "eliminated": f.eliminated,
        }
        for f in sorted(engine.state.figures.values(), key=lambda f: f.uid)
        if f.owner == owner
    ]


def archive_game(
    engine,
    *,
    opponent_kind: str = "",
    chat: list[dict] | None = None,
    ai_notes: list[dict] | None = None,
    reason: str = "checkpoint",
) -> Path | None:
    """Write/refresh the archive file for this game. Returns the path, or None
    if archiving failed (never raises — a game must not depend on it)."""
    try:
        game_id = getattr(engine, "game_id", "") or "unknown"
        record = {
            "game_id": game_id,
            "instance": _INSTANCE,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "saved_reason": reason,
            "build_total": engine.state.build_total,
            "board": [engine.state.board.width, engine.state.board.height],
            "seed": engine.log.seed,
            "opponent_kind": opponent_kind,
            "doctrine": getattr(engine, "doctrine", ""),
            "draft_notes": getattr(engine, "draft_notes", []),
            "phase": engine.state.phase,
            "turn": engine.state.turn_number,
            "winner": engine.state.winner,
            "armies": {
                "human": _army_brief(engine, "human"),
                "llm": _army_brief(engine, "llm"),
            },
            "events": engine.log.events,
            "chat": list(chat or []),
            "ai_notes": list(ai_notes or []),
        }
        d = history_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{game_id}.json"
        tmp = d / f".{game_id}.{uuid.uuid4().hex}.tmp"
        with _WRITE_LOCK:
            try:
                with open(tmp, "w") as fh:
                    json.dump(record, fh, default=str)
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)  # clean up if the dump/rename failed
        return path
    except Exception:
        return None


def list_games() -> list[dict]:
    """Lightweight summaries of every archived game, newest first."""
    out: list[dict] = []
    d = history_dir()
    if not d.is_dir():
        return out
    for p in d.glob("*.json"):
        try:
            with open(p) as fh:
                r = json.load(fh)
            out.append({
                "game_id": r.get("game_id"),
                "instance": r.get("instance"),
                "saved_at": r.get("saved_at"),
                "saved_reason": r.get("saved_reason"),
                "build_total": r.get("build_total"),
                "opponent_kind": r.get("opponent_kind"),
                "phase": r.get("phase"),
                "turn": r.get("turn"),
                "winner": r.get("winner"),
                "events": len(r.get("events") or []),
                "chat_messages": len(r.get("chat") or []),
            })
        except Exception:
            continue
    out.sort(key=lambda r: r.get("saved_at") or "", reverse=True)
    return out
