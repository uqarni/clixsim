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
- READ THE FACTS: every candidate carries engine-computed numbers. \
"incoming_clicks_at_dest" is how hard the enemy hits that spot next turn — \
walking a figure somewhere hotter than where it stands needs a REASON \
(a kill, a pin, massed support). "heuristic_rank" 0 is the engine's greedy \
best; deviate only for a stated strategic reason.
- MASS, THEN COMMIT. Never feed figures into a gunline one at a time — a solo \
charger dies in 1-2 turns for nothing. Advance as a formation or wait at a \
rally point until 2+ figures can engage the SAME target the same turn \
("pins_shooter" and flank candidates are what a committed turn looks like).
- ACTIVATE THE WHOLE ARMY. Your losses show back-line figures getting 3 \
actions in 46 turns while the frontline hogged every slot — idle figures are \
wasted points. A move fact "idle_turns": N means that figure has sat out N of \
your turns; give long-idle figures their advance unless something is truly \
more urgent. Formation moves are the efficient way to bring a group forward.
- Concentrate fire to eliminate enemy figures; finish wounded, high-value \
targets. KILL ENABLERS FIRST: a healer out-repairs your chip damage and a \
necromancer refunds your kills — candidates marked priority_target exist for \
exactly this.
- Use ranged attackers to hit without being hit: kite (step back out of a \
chaser's reach and keep shooting), take cover (+1 def in woods/on hills), and \
NEVER stand inside a longer-ranged enemy's band without a purpose. Basing an \
enemy shooter silences it (P4-R23); your own based shooters should break away.
- PUSHING: any candidate whose facts say "pushes": true deals 1 click of \
SELF-damage the moment it resolves (P4-R4). "push_would_eliminate" kills your \
figure; "push_would_demoralize" is nearly as bad — a Demoralized figure cannot \
attack and no longer counts toward victory. Push only deep, healthy dials for \
a decisive payoff.
- NEVER PASS. Tokens clear by themselves on any figure you leave alone — an \
explicit pass burns one of your precious actions for nothing. If nothing is \
worth doing, end the turn.
- Demoralized OWN figures are strategic corpses: never spend actions moving or \
"preserving" them.
- Doctrine is flavor, not license: it NEVER justifies a negative-EV action.
- SPORTSMANSHIP / ENDGAME: never stall a decided game. If you have no \
realistic path to victory (your last figures are battered and outgunned), do \
NOT loop retreat/rest turn after turn to delay the end — advance toward the \
enemy and fight to a swift, honorable finish. Dragging out a lost game is the \
one unforgivable move. (Retreating to heal or regroup mid-game is fine; this \
is about hopeless positions only.)
- FORMATIONS are your best action economy: a "formation_move" candidate moves \
3-5 same-faction figures for ONE action (vs one figure per action normally) — \
strongly prefer it over single moves when advancing a group. A \
"ranged_formation" pools shooters for +2 to the roll per extra member; a \
"close_formation" pools melee for +1 per extra member (members need only \
touch the TARGET, not each other — flank freely). Movement and ranged \
formations DO need members touching each other, so keep same-faction figures \
cohesive when advancing; scattering them throws the advantage away.
- CAVALRY (figures marked "mounted": double peanut bases). They break away on \
anything but a 1, deal 1 click of Shake Off to enemies on their rear arc when \
they do, and never get a free spin — so hit-and-run is their game, and basing \
one barely pins it. Charge/Bound figures move up to DOUBLE speed, or move \
normal speed and get a FREE attack: a candidate kind "charge_strike" or \
"bound_shot" is that free attack, already paid for — take the best one \
essentially always; skipping it wastes it (any other action forfeits it). \
Enemy Charge/Bound threat ranges are 2x their printed speed. Charge/Bound \
figures cannot join formations while the ability is on (cancel it to form up).

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


def _annotate_reply_deltas(engine: Engine, rows, k: int = 4) -> None:
    """Bounded one-ply lookahead (plan 3.1): for the top-k MOVE-like candidates,
    apply the move on a scratch copy of the deterministic engine and report the
    opponent's best single reply score afterward. Moves only — attack outcomes
    are dice, and their odds are already annotated."""
    import copy as _copy

    from .heuristic import HeuristicAI

    sim = HeuristicAI()
    done = 0
    for _score, _fig, cand in rows:
        if done >= k:
            break
        if cand.kind not in ("move", "formation_move"):
            continue
        done += 1
        try:
            ghost = _copy.deepcopy(engine)
            res = ghost.apply(_copy.deepcopy(cand.intent))
            if not getattr(res, "ok", False):
                continue
            ghost.end_turn()
            if ghost.state.ended:
                continue
            reply = sim.best_decision(ghost)
            if reply is not None:
                cand.annotation["enemy_best_reply_after"] = {
                    "action": reply.candidate.label,
                    "value": round(reply.score, 1),
                }
        except Exception:
            continue  # advisory only — never let lookahead break the turn


def position_hopeless(engine: Engine, ratio: float = 0.35) -> bool:
    """The sportsmanship trigger: True when the llm side's effective strength is
    a small fraction of the human's, so the picker is told to fight forward and
    end the game rather than stall with retreat/rest loops (a real game dragged
    a decided position from turn 53 to turn 60 this way). Strength = points x
    remaining-dial fraction; demoralized figures count at 30%."""
    def strength(owner: str) -> float:
        total = 0.0
        for f in engine.state.living(owner):
            s = f.definition.points * max(0.1, f.health_fraction())
            if f.is_demoralized:
                s *= 0.3
            total += s
        return total
    mine = strength("llm")
    theirs = strength("human")
    return theirs > 0 and mine < ratio * theirs


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

            self._client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
            self.available = True
        except Exception as e:  # pragma: no cover - import/setup guard
            self.last_error = f"anthropic client init failed: {e}"

    # ------------------------------------------------------------------ #
    def _ranked_candidates(self, engine: Engine) -> list[tuple[int, object, object]]:
        """Flatten all candidates across actionable figures, ranked by the
        heuristic score (best first), tagged with a stable id. The heuristic
        rank and score are STAMPED on each candidate (plan 1.9d) — the model
        used to see options with no idea which the engine thought best, and
        one-ply reply deltas are attached to the top few (plan 3.1, bounded)."""
        rows = []
        for fig in engine.actionable_figures():
            for cand in generate_candidates(engine, fig):
                rows.append((score_candidate(engine, fig, cand), fig, cand))
        for cand in generate_formation_candidates(engine, engine.state.active_player):
            primary = engine.state.figure(cand.annotation["primary"])
            rows.append((score_candidate(engine, primary, cand), primary, cand))
        rows.sort(key=lambda r: r[0], reverse=True)
        for rank, (score, _fig, cand) in enumerate(rows):
            cand.annotation["heuristic_rank"] = rank
            cand.annotation["heuristic_score"] = round(score, 2)
        _annotate_reply_deltas(engine, rows)
        return [(i, fig, cand) for i, (_, fig, cand) in enumerate(rows)]

    def _prompt(self, engine: Engine, ranked, table_talk: list[dict] | None = None,
                memory: list[str] | None = None,
                turn_log: list[str] | None = None) -> str:
        snap = board_snapshot(engine)
        endgame = position_hopeless(engine)
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
        if table_talk:
            payload["table_talk"] = table_talk
            payload["table_talk_note"] = (
                "Recent banter between you ('opponent') and the human. Honor plans "
                "you stated when they are tactically sound — your play should feel "
                "consistent with your words — but never sacrifice a winning line "
                "to keep a banter promise."
            )
        if memory:
            # Turn memory (plan 2.6): each ask used to be stateless — a figure
            # oscillated between two exact points for 60 turns. Stay consistent
            # with the plan you narrated unless the board actually changed.
            payload["your_recent_turns"] = memory
        if turn_log:
            payload["this_turn_so_far"] = turn_log
        if endgame:
            payload["endgame_note"] = (
                "Your position is almost certainly LOST — your remaining strength "
                "is a small fraction of the enemy's. Per the sportsmanship rule: "
                "stop retreating and resting to delay the end. Advance toward the "
                "enemy and fight — attack when you can, close distance when you "
                "can't. End the game with dignity."
            )
        return json.dumps(payload, indent=2)

    def _battle_system(self, engine: Engine) -> str:
        """Strategy principles + the rules digest + the OFFICIAL card text for
        every ability present in this battle — the opponent plays with the same
        references a human has (dials and geometry stay engine-computed, DP2)."""
        from ..chat import rules_digest

        ids = sorted({aid for f in engine.state.living()
                      for aid in f.definition.all_ability_ids()})
        lines = []
        for aid in ids:
            a = engine.db.ability(aid)
            if a and a.description:
                lines.append(f"- {a.name}: {a.description.strip()}")
        card = ("Official ability card text for the abilities in this battle:\n"
                + "\n".join(lines))
        # Continuity with the drafting phase: this is the SAME agent that built
        # the army — play to the doctrine it drafted under.
        identity = ""
        doctrine = getattr(engine, "doctrine", "")
        notes = getattr(engine, "draft_notes", [])
        if doctrine:
            identity = (f"\n\nYou drafted this army yourself, under the doctrine: "
                        f"{doctrine}\nYour draft picks and reasons:\n- "
                        + "\n- ".join(notes) + "\nPlay to that plan.")
        return f"{_SYSTEM}\n\n{rules_digest()}\n\n{card}{identity}"

    def _ask(self, engine: Engine, ranked, table_talk: list[dict] | None = None,
             memory: list[str] | None = None,
             turn_log: list[str] | None = None) -> tuple[int | None, str]:
        prompt = self._prompt(engine, ranked, table_talk, memory, turn_log)
        try:
            self.calls += 1
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self._battle_system(engine),
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

    def stream_turn(self, engine: Engine, table_talk: list[dict] | None = None,
                    memory: list[str] | None = None):
        """Yield one dict per action (summary, LLM reasoning, engine events) as it
        resolves, then end the turn. Falls back to the heuristic per action, and a
        candidate the engine rejects is excluded and re-picked (never ends the
        turn — that reads as the opponent freezing). ``memory`` carries the last
        few turns' rationales so plans persist across the stateless asks."""
        rejected: set[str] = set()
        turn_log: list[str] = []  # what this turn has already done
        retry_heuristic = False  # after a rejection, re-pick without another API call
        while engine.actionable_figures() and not engine.state.ended:
            ranked = [r for r in self._ranked_candidates(engine)
                      if repr(r[2].intent) not in rejected]
            if not ranked:
                break
            ask_llm = self.available and not retry_heuristic
            chosen_id, rationale = (self._ask(engine, ranked, table_talk, memory, turn_log)
                                    if ask_llm else (None, ""))
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
            if cand.kind == "pass":
                # NEVER pass: tokens clear on their own for idle figures — an
                # explicit pass burns an action slot (the audit counted 28 of
                # them, all wasted). Choosing pass means "nothing worth doing":
                # end the turn instead.
                break
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
            turn_log.append(f"{cand.label} — {reasoning[:120]}" if reasoning else cand.label)
            yield {
                "figure_uid": fig_uid, "candidate": cand, "score": score,
                "summary": cand.label, "reasoning": reasoning,
                "events": result.events, "fallback": fallback,
            }
        if not engine.state.ended:
            engine.end_turn()
