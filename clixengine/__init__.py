"""Clix Engine — a headless, deterministic Mage Knight (2002) rules engine.

The engine is the single source of truth (DP1); the CLI renderer and the LLM
opponent are clients of it.
"""

from .army import Army, validate_army
from .data import FigureDB, load_db
from .engine import Engine
from .geometry import Vec
from .setup import build_game
from .state import Board, Figure, GameState

__all__ = [
    "Army",
    "validate_army",
    "FigureDB",
    "load_db",
    "Engine",
    "Vec",
    "build_game",
    "Board",
    "Figure",
    "GameState",
]
