# PRD — Mage Knight 1.0 vs. LLM (Digital)

**Working title:** *Clix Engine*
**Author:** Uzair
**Status:** Draft v0.1 (for review)
**Last updated:** 2026-06-30

---

## 1. Summary

A single-player, top-down 2D digital implementation of **Mage Knight Unlimited (Jan 2002 rules)** in which a human plays against an LLM opponent. The system is faithful to the tabletop rules — continuous (inch-based) space, combat dials, facing/arcs, terrain, and formations — and uses the published dial database as its stat source. The build is organized so that a **deterministic, headless rules engine is the single source of truth**; the renderer and the LLM opponent are clients of that engine.

The product spans five phases end-to-end: **(1)** point selection, **(2)** army creation (manual, LLM-built, and blind-draft), **(3)** terrain selection & placement, **(4)** play, and **(5)** debrief & analysis.

---

## 2. Goals & Non-Goals

### Goals
- G1 — Play a rules-accurate game of Mage Knight 1.0 solo against a competent LLM opponent.
- G2 — Accurate handling of the three mechanically hard subsystems: **combat**, **movement (incl. formation movement)**, and **terrain/elevation**.
- G3 — Full army lifecycle: create / save / modify armies for both the human and the LLM, plus a **blind-draft** mode.
- G4 — Post-game **debrief** with a reconstructable log and LLM-authored analysis.
- G5 — Engine is deterministic and seed-reproducible (for testing, replay, and giving the LLM exact computed facts).

### Non-Goals (v1)
- N1 — Multiplayer / networked play (single human + single LLM side only).
- N2 — Implementing all 160 figures and every special ability on day one (data-driven, incremental).
- N3 — 3D graphics or physical-camera input (explicitly descoped — this replaces the camera concept entirely).
- N4 — Mobile/touch client, campaign/persistence beyond saved armies and game logs.
- N5 — Faithfully modeling booster **collation** if real data is unavailable (blind-draft may approximate; see OQ-3).

---

## 3. References

| Ref | Source | Use |
|-----|--------|-----|
| **[RULES]** | Mage Knight Unlimited — Complete Rules of Play, Jan 2002 Edition (PDF) — https://www.mageknight.net/wp-content/uploads/Mage-Knight-Unlimited-Rules-January-2002.pdf | Authoritative source for all mechanics. Section names below (e.g. *§Movement*, *§Elevated Terrain*) refer to this doc. |
| **[DIALS]** | Mage Knight Dial Stats DB — https://www.mageknight.net/dial-stats/ (backing data: static JSON at `mageknight.net/wp-content/uploads/{mkstats,expansions,factions,specialabilities}.json`) | Stat source for every figure (full click-by-click dials, point value, rank, faction, rarity, range, abilities). |
| **[ABILITIES]** | `specialabilities.json` (part of [DIALS]) | Canonical `Description`, `IsOptional`, `AbilityColor`, `AbilitySymbol` for all 42 abilities; referenced per-click via `*AbilityId`. **Sourced & normalized** to `stats/special_abilities.json` (see §9/D2–D3). Remaining work is effect-hook implementation, not sourcing. |

> **Note on [DIALS]:** the dial data is **not** embedded in the rendered page (the static HTML is only filter controls) — it's served as four static JSON files under `wp-content/uploads/` (`mkstats.json`, `expansions.json`, `factions.json`, `specialabilities.json`). Fetch these directly and cache; no scraping or page-rendering required. Already done — see §9.

---

## 4. Design Principles

- **DP1 — Engine is the source of truth.** All state and legality live in a headless engine. The renderer and LLM never mutate state directly; they submit *intents* the engine validates and resolves.
- **DP2 — Engine does geometry; the LLM does strategy.** All spatial/probabilistic computation (distance, arc membership, line of fire, legal-move enumeration, hit probability, expected clicks) is done by the engine. The LLM receives structured, pre-computed facts and chooses among annotated legal options.
- **DP3 — Continuous space.** Positions are floats in inches; facing is a float angle. There is **no grid**.
- **DP4 — Deterministic & seedable.** Given a seed and an action sequence, the game is fully reproducible. RNG is centralized.
- **DP5 — Data-driven content.** Figures, dials, and abilities are data, not code. New content = new rows, not new branches.
- **DP6 — Open information.** Mage Knight is a perfect-information game ("You may measure anything on the battlefield at any time" — *§Measurements*). No fog of war; the LLM legitimately sees the full board.

---

## 5. System Architecture

```
                 ┌───────────────────────────────────────────┐
                 │              Rules Engine (headless)        │
  Data ───────►  │  state · legal-move gen · geometry · dice   │  ◄─── Seeded RNG
 (dials,         │  combat resolution · dial/click tracking    │
  abilities,     │  terrain/elevation · victory scoring        │
  rules params)  └───────────────┬──────────────────┬──────────┘
                                  │ intents/queries  │ state + annotated legal moves
                     ┌────────────▼───────┐   ┌──────▼─────────────┐
                     │      Renderer      │   │   LLM Orchestrator  │
                     │ top-down 2D board  │   │ prompt build · RAG  │
                     │ arcs/range/LoF     │   │ intent parse/repair │
                     │ overlays, input    │   │ (opponent brain)    │
                     └────────────────────┘   └─────────────────────┘
                                  │
                          ┌───────▼────────┐
                          │  Game Log /    │  ← full action + state-delta stream
                          │  Debrief store │     (drives Phase 5 + replay)
                          └────────────────┘
```

- **A1** — The engine exposes a pure API: `apply(intent) -> Result | Rejection`, plus read-only query functions (`legal_moves(figure)`, `distance(a,b)`, `line_of_fire(a,b)`, `hit_odds(attacker, target, modifiers)`, `in_arc(a, b)`).
- **A2** — The engine is runnable with **no renderer** (for unit tests and LLM-vs-LLM self-play).
- **A3** — Every state transition emits a structured event to the game log (Phase 5 dependency).
- **A4** — Renderer choice is deferred; recommended path is a Python engine + lightweight top-down renderer (pygame) for local play, with a web/canvas UI as an optional later target. (See OQ-1.)

---

## 6. Core Domain Model

- **Board:** continuous plane, default 36" × 36" (*§The Standard Game*). Origin + inch scale defined once.
- **Figure (in-play instance):**
  - `figure_def_id` (→ dial DB row), `owner` (human | llm)
  - `position: (x, y)` float inches (the center dot)
  - `facing: angle` (radians/degrees)
  - `base_radius` (standard vs. mounted "peanut" double base — see OQ-6)
  - `current_click: int` (index into the dial), from which speed/attack/defense/damage + active abilities are derived
  - `range`, `targets` (arrow count), `rank`, `faction`, `point_value` — mostly static
  - `tokens`: action tokens (for pushing tracking), captive link, status flags (captured, demoralized, eliminated)
- **Dial:** ordered list of clicks; each click = {speed, attack, defense, damage, active_abilities[], colored-square positions}. Starting Position = the green-square click. Damage = +1 click (clockwise); healing = −1 click but never past Starting Position (*§Healing*). Three skulls in the damage slot = eliminated (*§Eliminating Warriors*).
- **Arc:** `front_arc_half_angle` parameter (front is the wide wedge; rear is the smaller wedge). `in_front_arc(self, point)` / `in_rear_arc(self, point)` tests. **Exact arc geometry must be parameterized from the base art — see OQ-5.**
- **Terrain feature:** `type` ∈ {clear, hindering, blocking}, `elevation` ∈ {ground, elevated}, optional special subtype {shallow_water, deep_water, low_wall, abrupt_elevated}, polygon boundary, and (for abrupt elevated) one or more access points.
- **Line of fire:** straight segment center→center; see combat & terrain requirements for blocking logic.

---

## 7. Phases

### Phase 1 — Point Selection

**Purpose:** choose the build total, which sets army size caps and actions-per-turn for both sides.

- **P1-R1** — Build total is a positive multiple of 100 (*§Building Your Army*). Default **200** (standard game); offer 100 (learning) and 300+.
- **P1-R2** — Actions per turn = `build_total / 100`, fixed for the entire game and unaffected by losses (*§Turns and Actions*).
- **P1-R3** — Both sides use the same build total (v1). Asymmetric totals are out of scope but the data model should not preclude them.
- **P1-R4** — Optional standard-game presets bundle: 3×3, 200 pts, 50-min limit, 4 terrain items/side, no elevated terrain (*§The Standard Game*). Selecting a preset pre-fills Phases 1–3.
- **Open:** default build total; whether to expose a time limit (OQ-4).

---

### Phase 2 — Army Creation

**Purpose:** build, persist, and edit armies for the human and the LLM, plus a blind-draft mode.

#### 2a. Manual builder (human)
- **P2-R1** — Browse/search/filter the figure DB by faction, rank, rarity, point value, abilities, expansion.
- **P2-R2** — Add figures until `sum(point_value) <= build_total` (may be under; may never exceed) (*§Building Your Army*).
- **P2-R3** — Enforce uniqueness: a **unique** figure (no rank stars) may appear at most once per army; non-uniques may repeat. The same unique may appear in **both** opposing armies (*§Building Your Army*).
- **P2-R4** — Show live totals, remaining points, action count, and a per-figure dial preview.
- **P2-R5** — **CRUD + persistence:** create, name, save, load, duplicate, modify, delete armies. Stored locally in a stable, versioned format.

#### 2b. LLM builder
- **P2-R6** — The LLM can construct a legal army to a given build total and (optional) strategic brief ("ranged-heavy", "capture-focused", "single-faction Orc Raiders"). Output is validated by the same legality checks as P2-R2/R3; illegal builds are rejected and regenerated.
- **P2-R7** — LLM builder receives the candidate figure pool as structured data (not free recall), so it selects from real, in-set figures only.

#### 2c. Blind draft ("sealed")  *(decided)*
- **P2-R8** — Both sides first agree on a single **expansion**. Then each side independently opens **4 boosters** from that expansion, generating a **separate per-side pool** (each opens their own product). *(Note: this supersedes the earlier "4 booster boxes" framing — 4 boosters each is a much smaller, tighter pool, which is the intended sealed-deck challenge.)*
- **P2-R9** — From its own pool, each player (human or LLM) builds an army up to the build total using only its pooled figures, respecting uniqueness within what was pulled.
- **P2-R10** — Both pools draw from the same chosen expansion but are rolled independently; pools are not shared.
- **P2-R11** — Collation parameters — figures per booster and per-rarity pull rates for the chosen expansion — are configurable. If real MK collation data is unavailable, approximate from the DB's rarity field and label it non-canonical (OQ-3). Pack size and rarity slots must be captured by the scrape (D1) so pools can be sampled.

---

### Phase 3 — Terrain Selection & Placement

**Purpose:** assemble the terrain pool and place it, honoring spacing and elevation rules.

- **P3-R1** — Each side contributes 0–4 terrain items to a shared **terrain pool** (*§Setting the Scene*). Non-elevated clear terrain may **not** be placed in the pool (the whole board is clear by default).
- **P3-R2** — Determine first player by opposed 2d6 roll, re-rolling ties (*§Setting the Scene*). First player also places first and takes the first turn.
- **P3-R3** — Alternate placement, first player first, until 4 items are placed or the pool empties.
- **P3-R4** — Placement constraints: each item ≥2" from any other terrain item and from any board edge, and not inside any starting area (*§Setting the Scene*).
- **P3-R5** — **Starting areas:** a 3"-deep band along each player's chosen edge, and must be ≥8" from any other edge (*§Setting the Scene*). Figures deploy only within their own starting area.
- **P3-R6** — Support terrain types & special subtypes with correct effects (see Phase 4 terrain requirements): clear, hindering, blocking; shallow water, deep water, low wall, abrupt elevated; and the **elevated** flag on clear/hindering/blocking.
- **P3-R7** — Abrupt elevated terrain must declare ≥1 **access point** at placement (*§Abrupt Elevated Terrain*).
- **P3-R8** — LLM participates in terrain contribution and placement as a strategic decision (receives board state; emits placements validated by P3-R4).
- **P3-R9** — Standard-game preset disables elevated terrain (P1-R4).

---

### Phase 4 — Playing the Game

**Purpose:** the turn loop with accurate movement, combat, formations, terrain/elevation, and end/scoring.

#### 4a. Turn & action structure
- **P4-R1** — Players alternate turns; first player starts (*§Turns and Actions*).
- **P4-R2** — Each turn grants `build_total/100` actions. Each action is assigned to one warrior; **no warrior gets >1 action/turn**; unused actions (more actions than warriors) are lost; actions do not carry over.
- **P4-R3** — Action types: **Move**, **Ranged combat**, **Close combat**, **Pass**. Results of one action are visible before choosing the next.
- **P4-R4** — **Action tokens & pushing:** any non-pass action tokens the warrior. Giving a non-pass action on two consecutive turns → **pushing**: 1 click of damage after the action resolves. A warrior may not receive a non-pass action on three consecutive turns (*§Pushing*).

#### 4b. Movement (single figure)
- **P4-R5** — Movement allowance = current speed value (inches). Path may curve; measured center→destination (*§Movement*).
- **P4-R6** — Path may **not** cross any figure base and may **not** pass between two figures that are in base contact (*§Movement*).
- **P4-R7** — After moving, the player may set facing freely. Facing gates all attacks (front arc only) (*§Movement*, *§A Moved Warrior*).
- **P4-R8** — **Breaking away:** giving a move to a figure in base contact with ≥1 opposing figure requires a break-away roll — 4/5/6 succeeds; 1/2/3 fails (may still re-face). Mounted: fails only on a 1, may not re-face on failure, and on a successful break from contact outside its front arc deals 1 "shake off" click to each such figure (reducible by Toughness/Invulnerability) (*§Breaking Away*).
- **P4-R9** — **Free spin:** when a moving figure enters base contact with opposing figures, those figures may spin in place (free, no action, no pushing) to bring their front arc to bear. Mounted figures do not grant a free spin when contacted (*§Free Spin*).
- **P4-R10** — Movement input model for the human (drag-to-place + set-facing vs. draw-path) is a UX decision (OQ-8); the engine validates whatever endpoint/path is submitted against P4-R5/6.

#### 4c. Movement formations (the emphasized case)
- **P4-R11** — A **movement formation** is 3–5 friendly figures, all same faction (Shyft may include Mage Spawn — P4-R12), each in base contact with ≥1 other member (*§Movement Formations*, *§Restrictions*).
- **P4-R12** — Giving **one** move action to a member moves the **whole** formation as that single action. All members are tokened and considered to have acted (pushing applies per-member based on their own prior-turn tokens).
- **P4-R13** — Every member's speed is reduced to the **slowest** member's speed for that move.
- **P4-R14** — Members move **one at a time**, each along a legal path (P4-R6). **End-state constraint:** when the action completes, each member must again be in base contact with ≥1 other member, and the formation may **not** split into ≥2 groups. *(This is the "begin in one place, end in another" behavior: the cluster relocates as long as final cohesion holds and each figure's individual path is legal — it is a cohesion constraint, not literally a shared path.)*
- **P4-R15** — A member that fails a break-away roll may not move but may re-face; other members still move, and final cohesion (P4-R14) must still hold.
- **P4-R16** — **Shyft interaction:** Shyft figures may form with Mage Spawn; each Mage Spawn must be in base contact with a Shyft member at declaration and, at completion, with a Shyft member that started the action in the formation (*§Shyfts*).

#### 4d. Combat — shared
- **P4-R17** — Attack resolution: `2d6 + attacker.attack >= target.defense` ⇒ hit (*§Combat Overview*). Engine centralizes the 2d6 roll via seeded RNG and precomputes hit probability + expected clicks for the LLM (DP2).
- **P4-R18** — **Crit miss (roll 2):** auto-miss; attacker takes 1 click. **Crit hit (roll 12):** auto-hit; +1 click of damage (or +1 healing on a heal) (*§Rolling 2 and 12*).
- **P4-R19** — Damage = attacker's current damage value = clicks applied to target (clockwise). Healing = counter-clockwise, never past Starting Position (*§Damage*, *§Healing*).
- **P4-R20** — Cannot damage friendlies; cannot target self (*§Targeting Friendly Warriors*).
- **P4-R21** — Elimination at 3 skulls (*§Eliminating Warriors*).
- **P4-R22** — Enforce the full attack sequence incl. optional-ability toggling order (attacker then defender) (*§Sequence of an Attack*).

#### 4e. Ranged combat
- **P4-R23** — Eligible only if range > 0 and the firer is **not** in base contact with an opposing figure (*§Ranged Combat*).
- **P4-R24** — **Line of fire:** straight center→center; must pass through the firer's front arc; length ≤ range; blocked if it crosses **any** figure base (friendly or opposing) other than firer/target (*§Ranged Combat*). Plus terrain blocking (P4-R30…33).
- **P4-R25** — May not target an opposing figure that is in base contact with a figure friendly to the firer.
- **P4-R26** — **Multi-target:** a figure may hit up to (arrow count) distinct targets, each needing its own clear LoF; one dice roll compared to each target's defense; damage reduced to 1 for multi-target (crit hit → 2) (*§Ranged Combat against Multiple Targets*).

#### 4f. Close combat & capturing
- **P4-R27** — Requires the attacker's **front arc** in base contact with the target (*§Close Combat*). +1 to the roll if attacking the target's **rear arc**.
- **P4-R28** — **Capture:** declared before rolling; target defense +2; on hit, target becomes a captive (no damage). Captive can't act, abilities ignored, not friendly/opposing, must stay in base contact with controller, can't be targeted; controller may only move/pass and can't be ranged/capture-targeted (*§Capturing*). One captive per controller.

#### 4g. Combat formations
- **P4-R29** — **Ranged formation:** 3–5 friendly same-faction figures each touching another; single target, damage-intent only; every member needs a clear LoF within its range; primary attacker's attack/damage used; **+2 to the roll per extra member**; no damage bonus. **Close formation:** 2–3 figures each with front arc in base contact with the same target (members need not touch each other); **+1 per extra member**, plus a single **+1 if any member contacts the target's rear arc**; primary attacker's values used (*§Combat Formations*). Formation crit-miss: only the primary attacker takes the click.

#### 4h. Terrain & elevation effects
- **P4-R30** — **Hindering:** entering — a figure starting on clear must stop when its base fully crosses into hindering; a figure starting in hindering has speed halved (round up) for the turn. LoF crossing any hindering → target defense **+1** (hindering modifier); firer-at-edge exception applies; close combat unaffected (*§Hindering Terrain*).
- **P4-R31** — **Blocking:** cannot move into/through; blocks any LoF crossing it (*§Blocking Terrain*).
- **P4-R32** — **Elevated:** all elevated terrain is a single common height. Figures must stop when moving up into / down out of elevated terrain; measure only horizontal distance. LoF is blocked by elevation unless firer and/or target is elevated, with the detailed mixed-elevation rules of *§Elevated Terrain Types*. **Height advantage:** a non-elevated firer vs. an elevated target → target defense **+1** (stacks with hindering for up to +2). Close combat is allowed across elevations (base contact judged from overhead); elevated target vs. non-elevated attacker also grants the height-advantage modifier.
- **P4-R33** — **Special terrain:** shallow water = hindering for movement only; deep water = blocking for movement only; low wall = special hindering (stop at far side, no subsequent halving when leaving, hindering modifier for LoF across unless firer in base contact, close combat across as if in base contact); abrupt elevated = elevated but no close combat on↔off, no split-elevation formations, movement on/off only via access points (or Flight) (*§Special Terrain*).

#### 4i. Special abilities
- **P4-R34** — Abilities are the colored squares on the dial; they appear/disappear per current click and are in effect while shown; optional abilities are on-by-default but cancelable until end of turn (*§Special Abilities*).
- **P4-R35** — Implement abilities as **data-driven effect hooks** into the relevant engine stages (movement, LoF, attack roll, defense, damage, healing, break-away, end-of-turn). Ship an initial subset (those present on the seed roster) and expand incrementally. Maintain an ability→status coverage list; abilities not yet implemented are flagged, never silently ignored.

#### 4j. Ending & scoring
- **P4-R36** — Game ends when only one side has non-captive, non-demoralized figures on the board; or a time limit is reached; or by agreement (*§Ending the Game*).
- **P4-R37** — **Victory points:** eliminated opposing figure = its point value (scored immediately, and re-scorable if it re-enters via Necromancy and is eliminated again); captive in your **starting area** at game end = 2× its point value; each friendly figure that started on the board, never left, and survives = its point value — **unless all your figures are captured/demoralized**, in which case zero survival points (*§Victory!*). Tie-break: fewest build points.
- **P4-R38** — **Withdrawing:** a player may leave before end; opponents keep elimination VP; the withdrawer's captives are forfeited as if eliminated; no survival points for the withdrawer (*§Withdrawing*).
- **P4-R39** — For a digital engine, ambiguous-geometry situations that tabletop resolves by die roll (*§Etiquette* rule 4) are instead resolved **exactly** by the engine; expose a tolerance parameter for base-contact "close enough" cases (*§Etiquette* rule 3).

---

### Phase 5 — Debrief & Analysis

**Purpose:** review and learn from the completed game.

- **P5-R1** — Persist a complete, ordered **game log**: every action, roll (with seed), state delta, and VP event — sufficient to fully reconstruct the game.
- **P5-R2** — **Replay:** step forward/back through the game; render the board at any point.
- **P5-R3** — **Summary:** final VP breakdown, turn count, per-figure outcome (survived / eliminated / captured), key swing moments (big damage, crits, captures, eliminations).
- **P5-R4** — **LLM analysis:** narrative debrief of the opponent's (and optionally the human's) key decisions, mistakes, and alternatives, grounded in the logged state — not hallucinated. Optionally surface engine-computed "what-if" deltas at flagged decision points (e.g., EV of the move taken vs. the top alternative).
- **P5-R5** — Export the log (JSON) for external analysis; optionally push a summary to the user's notes/issue tracker.

---

## 8. Cross-Cutting Requirements

- **X1 — Determinism:** single seeded RNG; identical seed + intents ⇒ identical game. All dice go through it.
- **X2 — Persistence:** versioned save formats for armies and games; schema migration tolerated.
- **X3 — LLM interface contract:** a stable structured "board snapshot + annotated legal moves" payload is the *only* thing the opponent reasons over. Includes computed distances, arc/LoF flags, hit odds, expected clicks, and terrain modifiers. Intents returned by the LLM are validated; invalid intents trigger a repair/re-prompt loop rather than illegal execution.
- **X4 — Opponent strength (decided: max competitive w/ lookahead).** The opponent is a hybrid **policy + search** system (see §9b): the engine generates a discrete set of tactically meaningful candidate actions, runs a shallow expectiminimax over them against an evaluation function, and the LLM acts as policy prior + strategic overseer. A strength slider still exists (search depth / candidate breadth), but the default target is "genuinely trying to win."
- **X5 — Testability:** engine unit-tested against worked examples from [RULES] (the Gunslinger multi-target example, the ranged/close formation examples, etc.); property tests for geometry (arc, LoF, distance) and dial math.
- **X6 — Ability coverage telemetry:** the set of implemented vs. unimplemented abilities is queryable; armies containing unimplemented abilities warn at build time. The seed-roster baseline (which abilities must be covered at all) is data-derivable from the `used_in_rebellion` flag in `stats/special_abilities.json` (24 abilities).

---

## 9. Figure Data Pipeline

- **D1** — **(RESOLVED — data acquired.)** The [DIALS] site loads from four static JSON files under `mageknight.net/wp-content/uploads/`: **`mkstats.json`** (1003 models, full dials), **`specialabilities.json`** (42 abilities), **`expansions.json`** (11 expansions; Rebellion = id 7), **`factions.json`** (12 factions). No scraping needed — fetch directly. Each model carries: `ModelId`, `ShortName`, `Name`, `ExpansionId/Name`, `Rank` (Weak/Standard/Tough/Unique/Promo), `Frequency` (rarity 1–6, or `PR`), `UnitCost` (points), `FigureNumber`, `Range` (bundles target-arrow count, e.g. `"12 (2 Targets)"`), `Arc` (degrees), `Factions`, and `Dials[].Clicks[]` with per-click Speed/Attack/Defense/Damage plus a per-stat `*AbilityId`.
- **D2** — Normalized into the engine schema (§6): `stats/rebellion.json` (160 non-promo Rebellion figures) and `stats/special_abilities.json` (42 entries = 41 real abilities + id 85 `"----"` blank slot). Each ability record carries `id`, `short_name`, `name`, `optional`, `color`, `symbol`, `description`, plus a derived `used_in_rebellion` flag (**24 true** — the abilities actually referenced by the seed roster's per-click `*AbilityId`s; this is the X6 coverage baseline).
- **D3** — **(RESOLVED — supersedes OQ-7.)** Ability effects come from `specialabilities.json`, now normalized locally to `stats/special_abilities.json`: each ability has `name`, `description`, `optional`, `color`, `symbol`. Per-click stat slots reference these via `*AbilityId` (85 = "----" = no ability). D3's remaining work is mapping each ability to an engine effect **hook**, not sourcing its text. **Interaction caveat:** several ability descriptions cross-reference *other* abilities — including some unused-in-Rebellion (e.g. Toughness's text names Pole Arm, Ram, Venom, Magic Retaliation) — so a few "unused" abilities must be understood as damage *interactions* even though they need no standalone hook in the seed roster.
- **D4** — **Seed roster (decided):** **all non-Promo Rebellion figures** = Weak + Standard + Tough (the "commons") + Unique = **160 figures**, 8 factions, 5–145 pts (all flagged `seed_v1: true`). The whole set draws on only **24 of 41 abilities** — the remaining real abilities were introduced in later expansions and never appear in Rebellion — so there's no ability-surface reason to trim; the full roster is barely more work than a subset. Lancers/Unlimited defer post-v1.0.
- **D5 — Data quirks to handle:** (a) dials are padded to 12 clicks; elimination is encoded as stat value `"Dead"` (a click is alive iff stats are numeric) — maps to the rules' 3-skulls KO. (b) `Range` must be parsed for both range value and target-arrow count. (c) `Arc` is present per figure (mostly 90, a few 180) — resolves the *sourcing* of arc data; the angle **convention** (front vs. rear, half vs. full angle) still needs pinning against known base art (OQ-5). (d) No base-type/mounted field exists; irrelevant for Rebellion (OQ-6). (e) `AbilityColor` is sometimes an RGBA-ish string (e.g. `"1.0,0.582,0.0,1.0"` = orange) rather than a color name — preserve raw. (f) Two `specialabilities.json` entries are dial **markers**, not combat abilities: id 93 `Dead` and id 120 `"Staring Position"` (sic — the green Starting-Position marker); both are excluded from the used-in-Rebellion set.

---

## 9b. AI Opponent Architecture  *(consequence of the "max competitive + lookahead" decision)*

Lookahead here is **not** naive minimax. Mage Knight is continuous (movement is any (x, y) + facing → infinite branching), stochastic (2d6), and multi-action per turn. Brute-force search is impossible; the design tames each of those:

- **AI1 — Candidate generation (the highest-leverage component).** The engine collapses the continuous action space into a small set of *tactically meaningful* candidate actions per figure: move-to-range-band on a chosen target, move-into-front-arc-and-charge, move-to-cover/height, break-away-and-reposition, retreat, plus every discrete combat action. Search quality is capped by this generator — if it never proposes the right destination, no search finds it. This is where most tuning effort goes.
- **AI2 — Multi-action turns.** A turn assigns `build_total/100` actions to distinct figures. Search operates over *combinations* of candidate actions (action sequences within a turn), pruned to the top-K per figure to keep branching bounded.
- **AI3 — Shallow expectiminimax.** Depth is measured in plies of (my turn → opponent's best reply). Dice are handled by expected value (or a few sampled rolls) using the engine's exact `hit_odds`/expected-clicks. One full ply already beats greedy play; depth is the strength slider (X4).
- **AI4 — Evaluation function.** Leaf boards are scored by a heuristic: Σ(figure point value × health-fraction) for each side, plus positional terms (threat, target coverage, cover, height advantage, VP, board control, capture opportunities). Much of the opponent's strength lives here.
- **AI5 — LLM as policy prior + overseer, not calculator.** The LLM does the things search is bad at and it is good at: proposing plausible *plans* to seed candidate generation (biasing AI1 toward sensible lines), and making the final selection among the engine's top-ranked, EV-annotated options — injecting strategy the eval misses (capture setups, tempo, objective focus, "sandbagging" a figure). The engine does all numeric search and geometry; the LLM never computes distances or odds. This is a policy-prior + value-search split (AlphaGo-shaped), which is why "genuinely competitive" is achievable without asking the LLM to do math it fails at.
- **AI6 — Time budget.** Turn-based + local means a per-turn compute budget of a few seconds is fine for shallow search plus one or two LLM calls. Budget is configurable and bounds depth/breadth.

**Risk to de-risk first:** whether this actually produces *interesting* play. Recommend prototyping AI1+AI3+AI4 on the M1 vertical slice before building breadth, since it may reshape the eval and candidate set.

---

## 10. Suggested Milestones

1. **M0 — Data & engine core:** scrape/cache dials (D1, first task); domain model; geometry (distance, arc, LoF); dice; dial/click tracking. Headless, tested.
2. **M1 — Vertical slice + AI de-risk:** 100-pt manual armies, no terrain, single-figure move + ranged + close combat, win by elimination. Minimal **pygame** renderer. **Prototype the full AI stack early here** — candidate generation + one-ply expectiminimax + eval + LLM overseer (AI1/AI3/AI4/AI5) — to validate that play is interesting before scaling.
3. **M2 — Terrain & elevation:** Phase 3 + P4-R30…33.
4. **M3 — Formations:** movement + ranged + close formations (P4-R11…16, R29).
5. **M4 — Full army lifecycle:** CRUD, LLM builder, sealed blind draft (4 boosters/side).
6. **M5 — Abilities depth:** expand the ability hook library across the seed roster.
7. **M6 — Debrief:** log, replay, LLM analysis.

---

## 11. Open Questions (for review)

**Resolved**
- ~~**OQ-1 — Platform/renderer**~~ → **Python + pygame**, local (§A4, M1).
- ~~**OQ-2 — Blind-draft pool ownership**~~ → **separate pools**; agree an expansion, each opens 4 boosters (P2-R8…R11).
- ~~**OQ-9 — Opponent strength**~~ → **max competitive with lookahead**; hybrid policy + search (§9b, X4).
- ~~**OQ-10 — Seed roster**~~ → **non-LE Commons + Uniques from Rebellion, Lancers, Unlimited** (D4).

- ~~**OQ-7 — Ability source**~~ → **resolved:** `specialabilities.json` provides full descriptions + optional flag + color for all 42 abilities, referenced per-click by `*AbilityId` (D3). Remaining work is effect-hook implementation, not sourcing.

**Still open**
- **OQ-3 — Collation fidelity:** `Frequency` (rarity 1–6) enables rarity-weighted booster sampling, but real per-expansion **pack composition** (figures per booster, rarity slots per pack) is still unknown for Rebellion — source it, or approximate (labeled non-canonical)?
- **OQ-4 — Time limit:** model the 50-minute standard-game limit as a real end condition, or ignore it digitally?
- **OQ-5 — Arc convention:** arc data is in the feed (`Model.Arc`, mostly 90, a few 180) — sourcing is solved; the remaining question is the **angle convention** (does 90 mean a 180°-total front arc? what are the 180-value figures?). Pin against a known figure's base art.
- **OQ-8 — Movement input UX:** drag-and-place-then-face, or draw-the-path?
- **OQ-11 — LLM army-building scope:** always drafts its own, or also mirror/counter a human-built army?