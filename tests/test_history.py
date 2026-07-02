"""All-time game history archive: every game gets a durable JSON record."""

import json
import math

from clixengine.history import archive_game, history_dir, list_games

from .conftest import build_engine


def _engine(db):
    e = build_engine(
        db,
        [
            ("human", "Chaos Mage", (10, 10), math.pi / 2, 0),
            ("llm", "Werebear", (11, 30), -math.pi / 2, 0),
        ],
    )
    e.game_id = "hist-test-1"
    e.doctrine = "Horde: bodies win."
    e.draft_notes = ["picked a mage"]
    return e


def test_history_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIX_HISTORY_DIR", str(tmp_path / "h"))
    assert history_dir() == tmp_path / "h"


def test_archive_round_trip_and_summaries(monkeypatch, tmp_path, db):
    monkeypatch.setenv("CLIX_HISTORY_DIR", str(tmp_path))
    e = _engine(db)
    p = archive_game(
        e, opponent_kind="llm",
        chat=[{"turn": 1, "role": "human", "content": "gl hf"}],
        ai_notes=[{"turn": 2, "notes": ["advanced the line"]}],
        reason="checkpoint",
    )
    assert p is not None and p.name == "hist-test-1.json"
    rec = json.loads(p.read_text())
    assert rec["game_id"] == "hist-test-1"
    assert rec["doctrine"].startswith("Horde")
    assert rec["draft_notes"] == ["picked a mage"]
    assert [f["name"] for f in rec["armies"]["human"]] == ["Chaos Mage"]
    assert rec["chat"][0]["content"] == "gl hf"
    assert rec["ai_notes"][0]["notes"] == ["advanced the line"]
    assert isinstance(rec["events"], list)

    # Re-archiving the same game overwrites (latest full record), not duplicates.
    e.state.turn_number = 7
    archive_game(e, opponent_kind="llm", reason="end_turn")
    games = list_games()
    assert len(games) == 1
    assert games[0]["turn"] == 7 and games[0]["saved_reason"] == "end_turn"

    # A different game gets its own file — the all-time archive accumulates.
    e2 = _engine(db)
    e2.game_id = "hist-test-2"
    archive_game(e2, opponent_kind="heuristic", reason="created")
    assert {g["game_id"] for g in list_games()} == {"hist-test-1", "hist-test-2"}


def test_archive_never_raises_on_bad_dir(monkeypatch, db):
    # Point the dir at a path that cannot be created (under a file).
    monkeypatch.setenv("CLIX_HISTORY_DIR", "/dev/null/nope")
    assert archive_game(_engine(db), reason="checkpoint") is None


def test_session_persist_carries_archive_feeds(monkeypatch, tmp_path, db):
    """A deploy restart must not truncate the game's chat/rationale record."""
    import clixengine.server as srv

    monkeypatch.setenv("CLIX_HISTORY_DIR", str(tmp_path / "hist"))
    monkeypatch.setattr(srv, "_SESSION_FILE", tmp_path / "session.pkl")
    s = srv.Session()
    s.engine = _engine(db)
    s.opponent = srv.HeuristicAI()
    s.chat_archive = [{"turn": 1, "role": "human", "content": "hello"}]
    s.ai_notes_log = [{"turn": 2, "notes": ["pushed forward"]}]
    s.persist()

    s2 = srv.Session()
    assert s2.restore()
    assert s2.chat_archive == s.chat_archive
    assert s2.ai_notes_log == s.ai_notes_log
    # persist() also checkpointed the archive file.
    assert (tmp_path / "hist" / "hist-test-1.json").exists()
