"""AI opponent package: candidate generation, evaluation, heuristic + LLM brains."""

from .evaluation import evaluate_state, score_candidate
from .heuristic import Decision, HeuristicAI
from .llm import LLMOpponent

__all__ = ["evaluate_state", "score_candidate", "Decision", "HeuristicAI", "LLMOpponent"]
