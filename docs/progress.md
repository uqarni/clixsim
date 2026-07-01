# Progress — Clix Engine (Mage Knight 1.0 vs. LLM)

Lightweight kanban for the build. Columns: **Todo → In Progress → In Review → Done**.
"In Review" items are handed to a testing subagent before they move to Done.

**Status:** v1 slice + **formations & ability mechanics** complete and tested.
Headless engine + CLI + Sonnet 5 opponent, **111 unit tests green**. Formations
(movement/ranged/close) and 22 of 24 Rebellion abilities implemented in both the
rules engine and the heuristic AI, plus a push-cost term. Subagent sweep passed
(audit + 1,080-game fuzz + live Sonnet 5); **7 fixes landed**.
**Last updated:** 2026-07-01

Legend: `ENG` engine · `AI` opponent · `CLI` interface · `TEST` tests · `DATA` data · `FUT` future milestone

---

## In Progress / In Review

_(none)_

---

## Done

- **DATA-1** — Figure + ability data pipeline: `stats/rebellion.json` (160 figs),
  `stats/special_abilities.json` (41 abilities). Normalized loader (`clixengine/data.py`).
- **SCAF-1** — Project scaffolding: `clixengine/` package, venv, `requirements.txt`,
  `.gitignore` (excludes `.env`), `games/` log dir.
- **ENG-1** — Engine core: continuous geometry (`geometry.py`), seeded RNG
  (`rng.py`), dial/click tracking + healing + KO (`state.py`), turn/action
  structure with pushing & action tokens (`engine.py`). *(P1-R2, P4-R2/R4/R5)*
- **ENG-2** — Combat: ranged (range, base-contact ban, LoF, front arc, multi-target
  damage cap), close (front-arc contact, rear +1), 2d6 resolution, crit hit/miss,
  Toughness damage hook + ability coverage telemetry. *(P4-R17…27, P4-R34/35, X6)*
- **ENG-3** — Movement: speed-limited endpoint, board bounds, path-crosses-base
  block, break-away roll, free re-face. *(P4-R5…8)*
- **ENG-4** — Ending & scoring: elimination victory, demoralized handling (attack
  block, no-voluntary-contact, victory + survival-VP exclusion), move-push victory
  check, victory-point scoring (elimination + survival). *(P4-R36/R37)*
- **AI-1** — Heuristic opponent: candidate generation (`candidates.py`), evaluation
  + candidate scoring (`ai/evaluation.py`), greedy turn selection (`ai/heuristic.py`).
  Deterministic; doubles as LLM fallback. *(AI1/AI4)*
- **AI-2** — Sonnet 5 opponent (`ai/llm.py`): board snapshot + annotated legal
  candidates (`snapshot.py`), structured-output choice, engine-validated with
  heuristic repair/fallback. Verified making real `claude-sonnet-5` calls. *(AI5, X3)*
- **CLI-1** — CLI (`cli.py`): ASCII top-down renderer, `selfplay` (heuristic or llm),
  interactive `play` (human vs Sonnet 5), draw labelling, JSON game-log export.
- **ENG-6** — **Formations** (P4-R11…R16, R29): movement (rigid-translation, slowest
  speed, start/end cohesion + per-member path legality, no Flight/Aquatic/Quickness
  members), ranged (+2/extra member, single target, per-member LoF, crit-miss only
  primary), close (2–3 gang, +1/extra member, +1 rear). Engine resolution + AI
  generation (rally behaviour + touching deployment so the AI assembles clusters).
- **ENG-7** — **Ability hooks** (`abilities.py`, P4-R34/R35, X6): 22 of 24 abilities as
  engine effects (Toughness, Battle Armor, Defend, Weapon Master, Vampirism, Magic
  Enhancement, Magic Immunity, Berserk, Command, Pole Arm, Quickness, Flight, Aquatic,
  Regeneration, Healing, Magic Healing, Magic Blast, Flame/Lightning, Shockwave,
  Necromancy, Magic Levitation, Demoralized), each wired into the heuristic (passives
  via EV; actives as new candidate types + ability-gated). Stealth = terrain-pending,
  Battle Fury = capture-pending — reported separately by coverage telemetry.
- **AI-3** — **Push-cost term**: every non-pass action pays the self-damage cost when
  the acting figure is already fatigued, and is essentially forbidden if it would push
  the figure to death — pushing is now a deliberate decision, not an accident.
- **TEST-1** — Unit suite (`tests/`, **111 tests**): geometry, probability, dial, data,
  movement, combat, turns/pushing, army validation, deterministic self-play, all 22
  abilities, all 3 formation types, and sweep regressions.

---

## Test sweep results (subagents)

- **Gameplay robustness:** 603 full self-play games (100/200/300 pts × seeds 0–200)
  + ~73k malformed/illegal intents. No crashes, hangs, non-termination, or illegal
  states; deterministic; VP conserved. Only cosmetic NITs (fixed: draw label, dead
  code).
- **Rules-correctness audit:** brute-forced the 2d6 hit/crit distribution, damage/
  heal boundaries, LoF, rear-arc (uses target facing), pushing lifecycle, break-away
  — all correct. **3 fixes landed:** (1) `_apply_move` now runs the victory check
  after pushing damage; (2) demoralized figures can no longer move into base contact;
  (3) survival VP is zeroed when a player's whole surviving army is demoralized/captured.
- **Live Sonnet 5 opponent:** real `claude-sonnet-5` calls, valid structured output
  (0 parse fallbacks), 100% legal choices, graceful heuristic fallback on forced API
  failure, ~2.5s/action, solid tactical play (concentrates fire, keeps ranged out of
  contact, charges melee into contact). No bugs.

### Sweep 2 — formations & abilities

- **Rules audit** (all 22 abilities + 3 formations verified vs canonical text): **2
  formation-move validation bugs** (per-member path base-crossing + destination overlap
  not checked) and rules-fidelity gaps fixed — Shockwave now respects LoF blocking,
  Magic Blast honours the P4-R25 targeting rule, Healing/Magic Healing apply the +1
  crit-heal, Battle Fury moved to `capture_pending` coverage.
- **Gameplay fuzz** (1,080 heuristic games, both faction modes; ~invariant/determinism
  checks): AI never proposed an illegal action; deterministic; state invariants held.
  Direct-intent fuzz found **duplicate formation members** and **ungated ability
  variants** accepted — both now rejected. Final sweep: 720 games, 0 illegal, 0
  non-terminating, with formation moves used ~740×, Magic Blast ~366×.
- **Live Sonnet 5** formed up a 5-figure army and advanced it as a formation before
  engaging (0 fallbacks).

---

## Todo

_Future milestones (post-v1), roughly in PRD milestone order._

- **FUT-M2** — Terrain & elevation: clear/hindering/blocking, water, low wall,
  elevated, LoF + movement modifiers (P3, P4-R30…33). Unblocks Stealth and the
  terrain clauses of Aquatic/Flight/Magic Blast.
- **FUT-M4** — Full army lifecycle: manual builder, CRUD/persistence, LLM builder,
  sealed blind draft (P2).
- **FUT-M6** — Debrief & replay: step-through replay, summary, LLM analysis (P5).
- **FUT-AI** — Expectiminimax lookahead + multi-action turn search (AI2/AI3);
  currently one-ply greedy.
- **FUT-CAP** — Capture mechanic + free spin (P4-R9/R28) — deferred. (Battle Fury's
  no-capture clause is inert until capture exists.)
- **FUT-OQ5** — Verify arc convention against base art (OQ-5); currently
  `arc_raw` half-angle (90 → front hemisphere).
- **FUT-REND** — pygame renderer (A4) — CLI/ASCII only for now.

---

## Known limitations (intentional, flagged)

- **Stealth** has no effect until terrain exists (M2) — reported as `terrain_pending`
  by ability-coverage telemetry, never silently ignored. Aquatic/Flight's terrain
  clauses are likewise moot pre-terrain (their figure-pass-through and break-away
  effects are live). **Battle Fury** is inert until capture (FUT-CAP).
- **No terrain, capture, or free-spin** yet.
- Movement validates the submitted **endpoint** (straight-line distance); curved
  paths around obstacles are a UX concern deferred with the renderer (P4-R10).
- **Free-spin** (P4-R9) is not modelled, so a moved figure does not grant defenders a
  free re-facing before Pole Arm / close-combat arc is judged.
- AI is **one-ply greedy**, not expectiminimax (see FUT-AI). Movement/ranged formations
  are used mainly by single-faction armies that start clustered (touching deployment);
  reliably assembling formations from a scattered mixed-faction start is a tuning item.
