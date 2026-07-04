# Lancers Expansion Plan — Mounted (Double-Base) Warriors

Status: reviewed draft (4-agent research + 4-agent adversarial critique, 38 findings
folded in). Sources: Mage Knight Lancers Rules (2001, 1 page), Mage Knight Unlimited
Rules (January 2002, 16 pp — supersedes Rebellion), Unlimited Special Abilities Card
(January 2002), mageknight.net `mkstats.json` (raw feed behind /dial-stats/).
Research + critique reports: scratchpad `lancers/report-*.md`, `lancers/critique-*.md`.

## 0. Executive summary

Lancers adds 142 core units (Weak 44 / Standard 44 / Tough 44 / Unique 10; 32 LE promos
excluded), of which **54 are mounted warriors** on "peanut" double bases — two equal
circles joined along the facing axis. The work splits into: (A) data ingestion,
(B) double-base geometry in the engine, (C) the mounted ruleset + three newly-used
abilities (Bound, Charge, Invulnerability), (D) AI awareness, (E) client rendering/UX,
(F) tests + rollout. The deepest change is (B): today *facing is cosmetic* — for a
capsule, every facing write changes the occupied footprint, so placement legality must
be checked wherever facing or an initial position is set (7 call sites). The second
deepest is (C): Charge/Bound's rider attack must respect the defender's free-spin
window, which forces a two-step resolution.

## 1. Data: identification and ingestion

### 1.1 Source recipe (verified live, re-verified by critique agent)

```
https://www.mageknight.net/wp-content/uploads/mkstats.json          (4.9 MB, 1003 models)
https://www.mageknight.net/wp-content/uploads/specialabilities.json (42 ability types)
```

Select: `Model.ExpansionName == "Lancers"` AND `Model.Rank in {Weak, Standard, Tough,
Unique}` → **142 units** (fig# 1–142, ids 8969–9110 contiguous; promos occupy
9111–9142). The only excluded category is 32 "Promo L3..L6" LE figures — Lancers has no
Dungeons-style heroes/artifacts/chariots. This mirrors the Rebellion ingest convention
(177 raw → 160 after dropping 17 promos).

Schema matches Rebellion's raw form (dot-prefixed keys, stats as strings, 12-click
dials). Verified value-level quirks the ingest must handle:

- **Corrupt dead-click encoding (would create zombie figures)**: two units — Arcane
  Draconum-Unique (id 9110, clicks 9–10) and Cave Butcher On Cave Runner-*** (id 9091,
  clicks 7–10) — encode dead clicks as numeric all-zero stats with ability id 93 (Dead)
  instead of the `"Dead"` string. A Rebellion-style `stat == "Dead"` test emits phantom
  live clicks (11 instead of 9 and 7): the figure would fight on as a 0-stat zombie.
  Rule: a click is dead when stats == "Dead" OR any slot AbilityId == 93; assert the
  dead tail is contiguous and no emitted live click has all-zero stats.
- Multi-target ranges: `"12 (2 Targets)"` and `"6 (2 Targets )"` (trailing space) —
  regex `^(\d+)\s*(?:\(\s*(\d+)\s*Targets?\s*\))?\s*$` parses all 142 with exactly 4
  two-target units. Rebellion's raw feed also has a third format (`"6 - 1 Target"`), and
  later sets have worse (`"8 (2 Targets"` unclosed, one empty string) — so the ingest
  **fails loud** on any unparsed range rather than defaulting.
- One 270° arc in Lancers (High Battle Mage On Scorpion Mount; the full feed has 15
  across later sets). Engine stores arbitrary `arc_deg`; see P5-R9 for the capsule arc
  ruling this unit forces.
- `starting_click` = 0 for all (ability 120 appears on zero clicks feed-wide).

### 1.2 Identifying mounted units — the load-bearing finding

**The raw data has NO base-type / speed-symbol field.** The horseshoe symbol was never
digitized. The one reliable marker is the name pattern **`" On "`** (capital O): "Light
Lancer On Light Warhorse", "Ankhar Archer On Ankhar", "King Of The Dead On Skeletal Fell
Beast", … Verified exhaustively: 54 mounted / 88 on-foot in the 142 core (16
Weak/Std/Tough trios + 6 Uniques = the 22 known mounted sculpt groups, every name reads
as rider-on-mount); zero lowercase variants; the only `" On "` matches elsewhere in the
1003-model feed are 54 Whirlwind figures that are themselves genuine cavalry — the rule
generalizes to future sets rather than misfiring. Corroborating signals both
misclassify (7 foot units have 180° arcs — Lich, Technomancer ×3, Whirling Golem ×3;
Bound/Charge appear on 9/6 foot units), so the ingest derives an explicit
**`"mounted": true`** field from the name and we never infer at runtime. The 54-name
list is pinned in the ingest test.

### 1.3 Ingest deliverables

- `scripts/ingest_lancers.py` — committed, reproducible (reads the cached scratchpad
  copy or re-downloads; emits `stats/lancers.json` in the rebellion.json shape + the
  `mounted` field, header `{"expansion": "Lancers", …, "count": 142}`); emits
  `"seed_v1": false` explicitly (the flag is vestigial but should stay truthful).
- `stats/lancers.json` — committed output.
- `data.py`: `load_db` reads every `stats/*.json` whose top level has an
  `expansion` + `figures` header (excludes special_abilities.json structurally);
  `FigureDef` gains `expansion: str = "Rebellion"` and `mounted: bool = False` — both
  **plain defaults** so pre-change pickled sessions unpickle cleanly (class-attribute
  fallback; `abilities.is_mounted` already getattr-guards; any new `expansion` read on
  a pickled def must getattr-guard likewise). Runtime id-collision assert = empty set
  intersection across loaded files (actual ranges: Lancers core 8969–9110, Rebellion
  9263–9422 — disjoint, but assert on data, not prose). `find()` by name is safe: zero
  short_name/name collisions across the two sets (verified).
- `stats/special_abilities.json`: add `used_in_lancers` flags (28 ability ids appear on
  Lancers live clicks; all 28 already exist in the file).
- New-game config: per-set checkboxes (Rebellion ✓ / Lancers ✓ default) filtering
  draft/sealed pools on `expansion`.

## 2. The mounted ruleset

Unlimited p.3: *"Mounted warriors follow all rules for normal figures, except where
noted."* Proposed PRD numbering **P5-R1…P5-R11**. Provenance matters: Unlimited (2002)
restates the break-away/free-spin exceptions and adds Shake Off, but is **silent** on
measurement geometry and post-move facing legality — those two rows come solely from
the Lancers 2001 sheet (rules 4–5), retained because Unlimited doesn't contradict them
and the physical bases embody them (the center dot is printed on the front circle).

| # | Rule (near-verbatim, source) | Engine change |
|---|---|---|
| P5-R1 | Double "peanut" base; horseshoe speed symbol = mounted (Unl. p.3) | `FigureDef.mounted`; footprint = two circles of `base_radius`, centers `2r` apart along facing; **`position` = FRONT-circle center dot** |
| P5-R2 | All distance and LoF measurements from the front-half center dot (Lancers 2001 r.4 — sole source; Unl. p.6 says only "center of base"/"center dot") | `position` IS the dot → existing distance/range/speed/LoF anchor math already correct; only *shape* queries change |
| P5-R3 | Break away fails only on a roll of 1 (Unl. p.6) | per-figure threshold in `_apply_move` |
| P5-R4 | On failed break away, may not rotate to a new facing (Unl. p.6) | skip the facing write on failure (today unconditional, engine.py:969) |
| P5-R5 | **Shake Off** (Unl. p.6, NEW vs the 2001 sheet): on successful break away, 1 click to each opposing figure that was in base contact **outside his front arc**; reducible by Toughness and Invulnerability | new step in `_apply_move`: snapshot contacts + arc class **before** the roll, apply damage after a successful roll, **before** the position/facing writes; `_on_eliminated` for any KO'd defender; trailing `_check_victory` already covers eliminations/demoralizations. Dormant gate, encoded + documented: a mounted figure with **Ram** active deals no Shake Off (card text; no Rebellion/Lancers unit carries Ram — it debuts in later sets) |
| P5-R6 | Mounted warriors never get a free spin (Unl. p.7) | already encoded (spin rejection + offer filter). **Fix the companion bug**: `_newly_contacted_opponents` returns `[]` when the *mover* is mounted (engine.py:716-717) — the printed rule denies the spin only to mounted *defenders*; a mounted mover contacting foot troops still grants them spins |
| P5-R7 | After moving, may face any direction **provided the double base does not rest on any figure base or blocking terrain** (Lancers 2001 r.5 — sole source; Unl. generalized the free-facing rule and dropped the proviso) | facing writes validated: final capsule placement on-board, non-overlapping, out of blocking terrain — endpoint check, no rotation sweep (per the printed 2001 proviso) |
| P5-R8 | Dial turns from under the base (Unl. p.2, physical only) | no-op |

### 2.1 Newly-used special abilities

Lancers live clicks use exactly three ability ids the engine doesn't implement:

- **Bound (90, optional, 26 units — 17 mounted, 9 foot)** — move action: up to 2×
  speed; OR — **if he did not start the TURN in base contact with an opposing figure**
  (turn-start snapshot, not action-start: a figure levitated out of contact mid-turn
  still forfeits the branch; one levitated INTO contact keeps it, subject to break-away)
  — normal speed then a **ranged combat attack** "as if given a ranged combat action",
  same action. Break away fails only on 1. May not be part of ANY formation while
  showing (optional → cancelable).
- **Charge (91, optional, 30 units — 24 mounted, 6 foot)** — same shape, rider is a
  **close combat attack**. Note: Martyr On Light Warhorse carries Charge on clicks 0–1
  and Bound on clicks 4–5 — the rider design must tolerate the kind changing mid-dial.
- **Invulnerability (101, NON-optional, 3 units / 4 clicks)** — defense +2 versus
  ranged attacks **that target or can affect him** (mirrors Battle Armor's identical
  wording — Flame/Lightning splash and Shockwave resolution get the +2 too, exactly as
  Battle Armor is handled today); damage from attacks/ability effects reduced by 2 (not
  pushing/crit-miss); cannot be healed; cannot capture or be captured.

Also present and worth stating: **Battle Fury (88) appears on 42 Lancers units** — it
remains in the capture-pending flagged set (capture isn't implemented), not silently
ignored; **Demoralized (95)** is printed on 123 Lancers units and is already implemented
(incl. the non-cancelable special-case). The rider attack obeys every normal attack
rule: Charge needs front-arc contact; Bound imports P4-R23 (bounding INTO contact
forfeits the shot), screening, range, hindering mods. The 2×-speed and attack branches
are mutually exclusive ("may instead"). **Speed order ruling**: hindering halving is
applied "after all other adjustments" (Unl. p.12), so the 2× doubling happens first,
then the halve — a speed-7 charger starting in brush moves ceil(14/2) = 7.

### 2.2 Printed rules applied to the capsule (not rulings — cited text)

- Hindering start-halving: "any part of his base touching" (Unl. p.12) → EITHER circle
  touching at move start halves speed.
- Hindering entry-stop: "must end immediately when his base crosses completely into"
  (p.12) → stop only when the ENTIRE capsule is inside; a long base can straddle a
  small feature and keep going (emergent but printed).
- Movement path: "the movement path … may not cross any figure bases and may not pass
  between two figures in base contact" (p.6) — the path is the RULER LINE from the
  front dot; mounted *blockers* contribute both circles to the crossing test.
- Base contact: "bases are touching" (p.5) — any circle of either figure.
- Free spin trigger: mover-agnostic (p.6) — spins are denied only to mounted spinners.

### 2.3 Genuine silences → explicit rulings (each gets a PRD note + test)

1. **Arc geometry on the peanut** (P5-R9): classification of a contact point uses the
   angle at the front dot against the unit's ACTUAL `arc_deg` — not a hardcoded 180°.
   For `arc_deg ≤ 180` (53 of 54 mounted): rear-circle contact = rear arc, always;
   front-circle contact judged by angle. For the one 270° unit (High Battle Mage On
   Scorpion Mount) a printed 270° arc physically wraps onto the rear circle: the pure
   angular test at the front dot governs ALL contact points, both circles. Named test.
2. **Blocking terrain en route** (P5-R10): both circles sweep along parallel segments
   (extends today's swept-circle `blocking_between`); figure-base path crossing stays
   the ruler-line rule per §2.2. One consistent choice, cited in `figure_blocking_between`.
3. **Terrain point queries**: elevation and the "center dot in hindering" firer
   exemption use the front dot (the measurement dot, Unl. p.12). Low wall: stop when
   the FRONT circle reaches the far side.
4. **Deployment** (P5-R11): whole capsule inside the 3" starting band. Facing the
   enemy, the legal front-dot strip is y ∈ [3r, 3−r] (≈1.65–2.45 for r=0.55).
5. **Formations**: mounted MAY join (rulebook silent = allowed; only Charge/Bound-active
   figures are barred, by card text). Cohesion "touching" = any-circle-pair contact.
   The rulebook's contacted-member formation break-away path stays UNIMPLEMENTED (the
   engine's existing documented deviation — members based by enemies move individually
   — stands), so no formation Shake Off / no-rotate interaction exists; documented.
6. **Pole Arm trigger vs a capsule mover**: any-circle contact + the mover's front dot
   inside the pole-arm's front arc (documented ruling).
7. **AI approximations** (not rules): threat model may evaluate hypothetical mounted
   movers front-dot-only where facing is unknown; documented heuristic.

## 3. Engine implementation

### 3.1 Representation (minimal diff)

No new stored geometry. `Figure.position` stays the front dot; `facing` exists;
`rear_center = position − 2·base_radius·(cos f, sin f)` derived. `FigureDef.mounted`
feeds the existing `abilities.is_mounted()` (already written, always False today).
`STANDARD_BASE_RADIUS` unchanged (equal circles).

### 3.2 New shared helpers

1. `iter_circles(fig)` / `iter_circles_at(pos, facing, r, mounted)`.
2. `figures_in_base_contact(a, b)` — any-pair contact. Route
   `GameState.in_base_contact_with` (state.py:223) through it — this covers the
   subsystems that flow through `opposing_contacts`: break-away, free-spin offers,
   P4-R23 gates, magic-heal/necromancy/healer opposing-contact gates.
   **It does NOT cover the direct `geometry.in_base_contact` callers** — each needs its
   own swap (full list, from the critique): Defend (abilities.py:117-119), Magic
   Enhancement (abilities.py:153-156), Command heal (engine.py:1884-1886), Healing
   adjacency gate (engine.py:1352), LoF screening (engine.py:159-161), Magic Blast
   screening (engine.py:1206), explain_attack's Defend loop (engine.py:787),
   same-faction clusters (engine.py:539-541), action hints incl. gap text
   (engine.py:577, 594, 609-611, 656), close-combat gates (legal_close_targets
   engine.py:498-500, _apply_close engine.py:1115, _check_close_formation
   engine.py:1729), Pole Arm (engine.py:1006-1008), threat engagement (threat.py:73,
   78, 94), candidates contact checks (candidates.py:152-155, 322-324, 515-516,
   628-631, 729-731, 862). Exit criterion: `grep -n "in_base_contact("` returns zero
   figure-vs-figure call sites outside geometry.py/state.py.
3. `segment_blocked_by_figure(p0, p1, fig)` — LoF + path blockers (both circles):
   engine.py:154, 1280; threat.py:65; candidates.py:297, 419, 767; formation paths.
4. `figure_overlap(pos, facing, fig, other)` — capsule-vs-capsule end placement.
5. `figure_in_bounds(board, fig, pos, facing)` — both circles.
6. Terrain wrappers `figure_in_blocking / figure_blocking_between /
   figure_hindering_entry / figure_effective_speed` (both-circle OR / parallel sweeps
   per P5-R10). Single-circle versions stay for terrain-vs-terrain use.
7. `Engine._placement_illegal(fig, pos, facing, ignore_uids)` — composite; called at
   **all 7 position/facing-write sites**: `_apply_move` (969, incl. skipping the write
   on mounted failed break-away per P5-R4), `deploy_figure` (320), `_apply_levitate`
   (1463), formation member facings (1621), `_apply_necromancy` (1424 +
   `_free_contact_position` capsule-aware), `_apply_free_spin` (743 — already airtight
   via the mounted rejection at 734, kept as a guard), and **setup.py:73
   `_deploy_positions`** — the deploy-less default game constructs figures directly and
   would strand a mounted rear circle off-board; make it capsule-aware AND assert
   placement legality for every constructed figure in build_game.
8. `contact_arc(fig, contact_point)` — front/rear classification per P5-R9.

### 3.3 Rule + ability wiring

- Break away: threshold 1 for mounted and Charge/Bound-active; no facing write on
  mounted failure; Shake Off per P5-R5 (snapshot before roll; Ram gate dormant).
- Free spin: delete the mover-mounted early-return (engine.py:716-717); keep
  defender-mounted filter + spinner rejection.
- **Charge/Bound resolution — two-step, rules-forced.** The defender's free spin
  happens "immediately" on contact (Unl. p.6), i.e. BEFORE the rider attack — resolving
  move+attack atomically would let a charger bank a rear-arc +1 the defender is
  entitled to spin away. Design: a Charge/Bound move (≤1× speed, turn-start-uncontacted
  snapshot) sets `engine._pending_rider = {uid, kind}`; free-spin offers resolve
  through the existing interactive P4-R9 window (human defenders get the prompt, AI
  defenders auto-spin); the follow-up `CloseIntent`/`RangedIntent` carrying
  `rider=True` resolves **without consuming an action or re-tokening** — via extracted
  `_resolve_close_core` / `_resolve_ranged_core` (the existing appliers CANNOT be
  reused as-is: their `_precheck` would reject `already_acted` and `_consume_nonpass`
  would double-token and double-push). `_consume_nonpass` + the pushing/victory tail
  run exactly once, on the move half. Pending rider is cleared by any other intent,
  end-turn, or the mover's death. No pre-declaration: dragging ≤1× speed leaves the
  rider available; dragging beyond 1× (up to 2×) is the no-attack branch.
  `intent_from_dict` (server.py:253) parses the rider fields — without this the human
  path would silently drop the attack (round-trip test required). Candidates annotate
  `charge`/`bound` kinds; speed budget = 2× applied before hindering halving (§2.1).
- LoF/screening/splash/shockwave/Magic Blast/adjacency/Pole Arm/Defend/Enhancement/
  Command-heal/hints/clusters: the §3.2(2)+(3) swap lists.
- Deploy: both circles in band+board; facing validated.
- Necromancy/Levitate: capsule-aware placement + validated facing writes.
- Formations: `_positions_cohesive` gets a capsule-aware signature (or keeps a
  positions/radii back-compat form — tests/test_formations.py:49 calls the old
  signature and must be updated either way); Charge/Bound-active members rejected with
  a clear reason; mounted allowed.
- Invulnerability in `effective_defense` (+2 vs ranged, target-or-affected — mirror
  Battle Armor) + `damage_after_defenses` (−2, Toughness exclusions) + heal gates.

### 3.4 AI

- threat.py: blocker swap; hypothetical movers front-dot-only (documented).
- candidates.py: `approach_contact_dest(mover, target)` replaces `_contact_point` and
  every `need = d − (r+r)` (flank point: behind the REAR circle, `2r_t` deeper);
  charge/bound candidates (move+attack in one action = big action-economy win);
  `_move_illegal` stays lock-step with engine via shared helpers.
- evaluation.py: fix hardcoded `0.55` (line 315) regardless; value charge/bound as
  attack+reposition; reach envelopes note the rear-circle extension.
- llm.py prompt + snapshot: `mounted: true`, rules digest (no free spin, Shake Off
  threat, break-away-on-1 = cavalry disengages at will, charge/bound tactics).
- build.py digest + drafter guidance: mounted counts per faction, cavalry doctrine.
- server.py `_auto_deploy_llm` + setup.py `_deploy_positions`: capsule-aware rows
  (both current row layouts strand mounted rear circles off-board, silently).

### 3.5 Contracts & persistence

- view.py `figure_view`: add `mounted` + server-computed `rear_pos` (client re-deriving
  through the 0.1° facing rounding can drift ~0.002" — inside tolerance but pointless
  risk). snapshot.py `_figure_view`: add `mounted`.
- **ConstructFigure surfaces too**: the roster/sealed/construction serializers behind
  /api/roster, sealed packs, and the new_game stream gain `mounted` — the Draft-screen
  badge has no data otherwise.
- `intent_from_dict` rider parsing (see §3.3) — wire-format change with round-trip test.
- Pickle back-compat: plain defaults only (verified: class-attribute fallback works on
  old pickles; `expansion` MUST have a default or restored sessions AttributeError).
- History archive: no structural change (front dot + facing suffice); `_army_brief`
  gains `mounted`; Lancers figures are new ids — no retroactive flags (replay
  determinism preserved). Existing view/server contract tests (`base_radius` asserts)
  stay green as-is.

## 4. Client implementation

Rendering (BoardCanvas): capsule silhouette as one two-arc path (front cap + rear cap);
selection/reticle/member halos stroke the offset capsule; facing wedge, health ring,
token pips stay on the front circle; name label below the capsule's screen bbox;
KO/damage fx at capsule midpoint. Applies in every phase (figures render during terrain
placement too).

Truth surfaces (from the critique — each mirrors an engine rule and must not lie):
- **LoF hover** (BoardCanvas:670-720 + terrainGeom `lofBlocker`): blockers and
  screeners contribute both circles; melee gap = min over circle pairs; sight line
  anchors on front dots (engine rule); blocker highlight rings the capsule.
- **Adjacency arcs** (BoardCanvas:284-313, feeds move ghost AND aim step): touching =
  min over mover-circles × enemy-circles, mover rear derived from the previewed facing;
  arc truth via the engine's `contact_arc` rule.
- **Contact-link dots** (BoardCanvas:898-935): dot at the closest circle-pair's
  tangency point; rear-bonus coloring via `contact_arc`, not bearing-to-front-center.
- **terrainGeom mirrors** (`effectiveSpeed`, `moveBlockReason` + their four call
  sites): accept multiple centers; rear segment endpoints derived from start/end
  facings. Rename/comment-fence the existing `capsuleCrossesPoly` (it means "circle
  swept along a path", not the base shape — naming trap).

Interaction:
- Hit-test: `pointToSegment(p, front, rear) ≤ r`; same capsule test for rigid-ghost
  re-grabs. Marquee: either circle center inside the box.
- Drag: cursor carries the front dot with grab-offset preserved (no 2r jump when
  grabbing the rump); the board clamp applies to the derived front dot; **ghostFor
  gains a both-circles board-bounds check** (none exists today — `clampedWorld`'s
  front-center clamp was the only guard and its invariant breaks for capsules).
- **Aim step becomes legality-bearing in all THREE flows**: battle pendingMove, Deploy
  pending, and **formation-member staging** (today `confirmMove`'s staging branch
  validates nothing at confirm and a later `bad_formation` submit rejection destroys
  the whole arrangement). Re-run ghostFor with the aimed facing; disable Confirm when
  red. On a server rejection of a battle move, KEEP pendingMove so the user re-aims
  instead of re-dragging.
- Snapping: mounted figures contribute two target circles **with distinct circle
  identity** — `snapToContactRing`'s `clear()` filter skips obstacles by uid, so naive
  same-uid entries would exempt the sibling circle and offer snaps overlapping the
  figure's own waist (critique HIGH). `clear()` must skip only the exact contact
  circle(s); figure-level uid kept for pocket same-figure skip + `faceUid`. No
  self-waist pockets in v1. Deploy's snap filter additionally runs the full capsule
  band+overlap check (today it band-checks the front center only → guaranteed
  confusing rejections).
- Free spin: restructure the stream handler so the opponent stream RESUMES on every
  no-spin-armed path (filtered-empty, uid-not-found, fig-missing) — the current code
  soft-locks on any fall-through; filter mounted uids defensively.
- Rigid formation move: staged entries carry `mounted`; staged targets' rear circles
  derive from the STAGED facing; endpoint checks 2×2; path-cross iterates obstacle
  circles × both mover sweep segments (engine re-validates on submit).
- Deploy: `constrain` clamps/validates the full capsule for the current facing and
  re-validates on facing changes; default mounted deploy facing = toward the enemy.
- **Charge/Bound UX**: ghostFor/reach-ring take the ability's speed budget (2× when no
  rider; the ring shows both radii when the fork exists); after a ≤1×-speed move by an
  eligible figure resolves (and free spins settle), the ActionPanel offers the rider
  attack targets ("Charge — strike now") with skip; `CLOSE_KINDS`/`RANGED_KINDS`/
  `ATTACK_KINDS`/`variantName` wiring for the new kinds; the rider offer recomputes as
  facings change. groupInfo bars Charge/Bound-showing members (same cancel-hint pattern
  as Flight) and computes its gap readout min-over-circle-pairs.
- Contract: `mounted?: boolean` / `rear_pos?: [number, number]` OPTIONAL in FigureView
  (mock.ts's `Omit`-typed factory breaks tsc on a required field); ConstructFigure gains
  `mounted?`.
- Badges: 🐴 on FigCard, ActionPanel header, Draft roster cards, terrain-placement army
  panel.

## 5. Tests

- `tests/test_ingest_lancers.py`: 142 units; 54 mounted (pinned name list); rank counts
  44/44/44/10; the two corrupt-dial units emit 9 and 7 live clicks; multi-target parse
  (4 units) + fail-loud on unparsed; 270° unit present; id-collision set intersection
  empty; every live ability id ∈ IMPLEMENTED ∪ flagged (Battle Fury 88 counts as
  flagged/capture-pending — explicitly).
- `tests/test_mounted_geometry.py`: rear-circle contact (break-away trigger, screening,
  Defend/Enhancement, Command heal, heal gate, close-attack gate); LoF blocked by a
  rear circle; capsule overlap rejection; board/band bounds; facing-write legality
  incl. mounted failed-break-away no-rotate; hindering straddle vs full-entry;
  front-dot measurements unchanged for singles; P5-R9 arc classification incl. the
  270° Scorpion Mount named case; Pole Arm capsule ruling.
- `tests/test_shake_off.py`: outside-front-arc only; Toughness/Invulnerability reduce;
  none on failed roll; defender KO'd by shake-off → `_on_eliminated` + victory check;
  Ram gate (constructed fixture, dormant in real data).
- `tests/test_charge_bound.py`: 2×-speed branch incl. hindering order (double first,
  halve last); turn-start contact snapshot (levitated-out case); rider through the
  free-spin window — defender spins away the rear +1 before the strike; bound-into-
  contact forfeits the shot; screening; formation bar while showing + cancel restores;
  break-away-on-1 for foot carriers; exactly one token/one push; Martyr mid-dial
  Charge→Bound switch; `intent_from_dict` round-trip.
- `tests/test_invulnerability.py`: −2 damage; +2 vs ranged incl. Flame/Lightning splash
  ("can affect"); no heal; pushing/crit-miss unaffected.
- Updates: tests/test_formations.py:49 (`_positions_cohesive` signature). The two
  view/server contract asserts stay green (fields are additive).
- evalharness: seeded self-play with mixed Lancers rosters terminates; A/B sanity.

## 6. Rollout

All on clix-dev (:8001/:5174); live-play smoke (Lancers draft → terrain → deploy
mounted → charge/bound/shake-off in anger) before prod merge; then the usual
game-preserving deploy. PRD gains §5 (P5-R1…R11) + P4-R35 coverage update. Order:
data → geometry helpers + contact-swap sweep → engine rules → Charge/Bound/Invuln →
AI → client → polish; each phase lands with its test batch.

## 7. Open questions for Uzair

1. **Draft pool default**: combined Rebellion+Lancers pool with per-set checkboxes
   (both on)? Or also a Lancers-only mode?
2. **Formations scope**: plan is faithful (mounted may join; capsule cohesion + rigid
   client work included). Cheap alternative: bar mounted from formations in v1 (like
   Mage Spawn) and cut that work — engine + client both simplify meaningfully.
