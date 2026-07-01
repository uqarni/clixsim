"""Sonnet 5 opponent (AI5 — policy prior + strategic overseer).

The engine does all geometry and probability; the LLM only *chooses* among the
engine's annotated, EV-ranked candidate actions, injecting strategy the eval
misses. Every choice is validated by the engine; an invalid or failed choice
falls back to the heuristic pick (the repair loop of X3), so a game never stalls
or executes an illegal move.

Model: claude-sonnet-5 (adaptive thinking; effort tuned low for snappy per-action
decisions). API key is read from the environment / project .env.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..candidates import generate_candidates, generate_formation_candidates
from ..config import get_api_key
from ..engine import Engine
from ..snapshot import board_snapshot
from .evaluation import score_candidate
from .heuristic import Decision, HeuristicAI

MODEL = "claude-sonnet-5"

_SYSTEM = """You are the opponent 'brain' in a faithful digital port of Mage Knight \
(2002 rules), playing as the 'llm' side against a human. You are a sharp, \
genuinely competitive tabletop tactician trying to win.

The rules engine has ALREADY computed every fact you need: distances, arcs, \
line-of-fire, hit odds, and expected damage. You do NOT compute geometry or \
probability. Your job is pure strategy: choose the single best action from the \
numbered list of engine-validated, legal candidate actions provided each step.

Principles of strong play:
- Concentrate fire to eliminate enemy figures (removing a figure removes its \
attacks and scores its point value).
- Prefer high expected-damage actions; finish wounded, high-value targets.
- Use ranged attackers to hit without being hit; keep them out of base contact.
- Advance melee figures into contact; attack the target's rear arc for +1 when \
you can.
- Don't push a figure to exhaustion for a marginal gain.

Respond with ONLY the chosen candidate id and a one-line rationale."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "choice_id": {"type": "integer", "description": "id of the chosen candidate action"},
        "rationale": {"type": "string", "description": "one short sentence"},
    },
    "required": ["choice_id", "rationale"],
    "additionalProperties": False,
}


@dataclass
class LLMOpponent:
    model: str = MODEL
    effort: str = "low"
    max_tokens: int = 1024
    verbose: bool = False
    _client: object | None = field(default=None, init=False)
    _fallback: HeuristicAI = field(default_factory=HeuristicAI, init=False)
    available: bool = field(default=False, init=False)
    last_error: str = field(default="", init=False)
    name: str = field(default="sonnet-5", init=False)
    calls: int = field(default=0, init=False)
    fallbacks: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        key = get_api_key()
        if not key:
            self.last_error = "no ANTHROPIC_API_KEY found"
            return
        try:
            import anthropic

            self._client = anthropic.Anthropic(api_key=key)
            self.available = True
        except Exception as e:  # pragma: no cover - import/setup guard
            self.last_error = f"anthropic client init failed: {e}"

    # ------------------------------------------------------------------ #
    def _ranked_candidates(self, engine: Engine) -> list[tuple[int, object, object]]:
        """Flatten all candidates across actionable figures, ranked by the
        heuristic score (best first), tagged with a stable id."""
        rows = []
        for fig in engine.actionable_figures():
            for cand in generate_candidates(engine, fig):
                rows.append((score_candidate(engine, fig, cand), fig, cand))
        for cand in generate_formation_candidates(engine, engine.state.active_player):
            primary = engine.state.figure(cand.annotation["primary"])
            rows.append((score_candidate(engine, primary, cand), primary, cand))
        rows.sort(key=lambda r: r[0], reverse=True)
        return [(i, fig, cand) for i, (_, fig, cand) in enumerate(rows)]

    def _prompt(self, engine: Engine, ranked) -> str:
        snap = board_snapshot(engine)
        options = []
        for cid, fig, cand in ranked:
            options.append(
                {
                    "id": cid,
                    "figure": fig.short_name,
                    "figure_uid": fig.uid,
                    "action": cand.label,
                    "facts": cand.annotation,
                }
            )
        payload = {
            "board": snap,
            "you_are": "llm",
            "candidate_actions": options,
            "note": "Choose exactly one candidate id. Pick the 'Pass' option only "
            "if no action improves your position.",
        }
        return json.dumps(payload, indent=2)

    def _ask(self, engine: Engine, ranked) -> tuple[int | None, str]:
        prompt = self._prompt(engine, ranked)
        try:
            self.calls += 1
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_SYSTEM,
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": _SCHEMA},
                },
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            self.last_error = f"API error: {e}"
            return None, ""
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
            cid = int(data["choice_id"])
            rationale = str(data.get("rationale", ""))
        except Exception as e:
            self.last_error = f"parse error: {e} :: {text[:200]}"
            return None, ""
        valid_ids = {cid_ for cid_, _, _ in ranked}
        if cid not in valid_ids:
            self.last_error = f"choice {cid} out of range"
            return None, rationale
        return cid, rationale

    # ------------------------------------------------------------------ #
    def take_turn(self, engine: Engine) -> list[Decision]:
        return [
            Decision(s["figure_uid"], s["candidate"], s["score"],
                     ("[fallback] " if s["fallback"] else "") + s["summary"])
            for s in self.stream_turn(engine)
        ]

    def stream_turn(self, engine: Engine):
        """Yield one dict per action (summary, LLM reasoning, engine events) as it
        resolves, then end the turn. Falls back to the heuristic per action, and a
        candidate the engine rejects is excluded and re-picked (never ends the
        turn — that reads as the opponent freezing)."""
        rejected: set[str] = set()
        retry_heuristic = False  # after a rejection, re-pick without another API call
        while engine.actionable_figures() and not engine.state.ended:
            ranked = [r for r in self._ranked_candidates(engine)
                      if repr(r[2].intent) not in rejected]
            if not ranked:
                break
            ask_llm = self.available and not retry_heuristic
            chosen_id, rationale = self._ask(engine, ranked) if ask_llm else (None, "")
            fallback = chosen_id is None
            if fallback:
                if ask_llm:
                    self.fallbacks += 1
                best = self._fallback.best_decision(engine, frozenset(rejected))
                if best is None or best.score <= 0.0:
                    break
                fig_uid, cand, score = best.figure_uid, best.candidate, best.score
                reasoning = rationale or "Falling back to the strongest available move."
            else:
                _, fig_obj, cand = next(r for r in ranked if r[0] == chosen_id)
                fig_uid = fig_obj.uid
                score = 0.0 if cand.kind == "pass" else score_candidate(engine, fig_obj, cand)
                reasoning = rationale
            result = engine.apply(cand.intent)
            if not result.ok:
                self.last_error = f"engine rejected: {result.reason}"
                self.fallbacks += 1
                rejected.add(repr(cand.intent))
                retry_heuristic = True
                if len(rejected) >= 12:
                    break
                continue
            rejected.clear()
            retry_heuristic = False
            yield {
                "figure_uid": fig_uid, "candidate": cand, "score": score,
                "summary": cand.label, "reasoning": reasoning,
                "events": result.events, "fallback": fallback,
            }
        if not engine.state.ended:
            engine.end_turn()
