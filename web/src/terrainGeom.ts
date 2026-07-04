// Client-side terrain geometry — mirrors clixengine/terrain.placement_reason so
// the placement ghost can show live green/red before the server confirms. The
// engine remains the source of truth; this is only for responsive feedback.

import type { TerrainPiece, TerrainTemplate } from "./api";

export type Pt = [number, number];

// --- double-base (mounted) capsule helpers ----------------------------------
// A mounted figure's base is two equal circles of `base_radius` whose centres
// sit 2r apart along the facing axis; `pos` is the FRONT-circle centre dot
// (P5-R1). These helpers are the one place the rear centre is derived.

export interface CapsuleFig {
  pos: [number, number];
  facing_deg: number;
  base_radius: number;
  mounted?: boolean;
  rear_pos?: [number, number];
}

// Rear-circle centre for a front dot + world facing (radians).
export function rearCenter(pos: Pt, facingRad: number, r: number): Pt {
  return [pos[0] - 2 * r * Math.cos(facingRad), pos[1] - 2 * r * Math.sin(facingRad)];
}

// Circle centres for a HYPOTHETICAL placement (drag ghosts, staged members)
// where the previewed facing differs from the live figure's.
export function centersAt(pos: Pt, facingRad: number, r: number, mounted: boolean): Pt[] {
  return mounted ? [pos, rearCenter(pos, facingRad, r)] : [pos];
}

// All base-circle centres of a live figure: [front] or [front, rear]. Prefers
// the server-computed rear_pos (the view rounds facing to 0.1°, so a derived
// rear can drift ~0.002" from the engine's) and derives only when absent.
export function figureCenters(f: CapsuleFig): Pt[] {
  if (!f.mounted) return [f.pos as Pt];
  const rear: Pt =
    (f.rear_pos as Pt | undefined) ??
    rearCenter(f.pos as Pt, (f.facing_deg * Math.PI) / 180, f.base_radius);
  return [f.pos as Pt, rear];
}

function rot(p: Pt, a: number): Pt {
  const c = Math.cos(a);
  const s = Math.sin(a);
  return [p[0] * c - p[1] * s, p[0] * s + p[1] * c];
}

// A template's polygon baked at a center with a rotation (world inches).
export function placedPolygon(tmpl: TerrainTemplate, center: Pt, rotation: number): Pt[] {
  return tmpl.polygon.map((v) => {
    const r = rot(v as Pt, rotation);
    return [r[0] + center[0], r[1] + center[1]] as Pt;
  });
}

export function placedAccessPoints(tmpl: TerrainTemplate, center: Pt, rotation: number): Pt[] {
  return tmpl.access_points.map((v) => {
    const r = rot(v as Pt, rotation);
    return [r[0] + center[0], r[1] + center[1]] as Pt;
  });
}

function pointInPoly(p: Pt, poly: Pt[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > p[1] !== yj > p[1]) {
      const x = ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi;
      if (p[0] < x) inside = !inside;
    }
  }
  return inside;
}

function segDist(a0: Pt, a1: Pt, b0: Pt, b1: Pt): number {
  // segment-to-segment distance (0 if they intersect)
  const d = (u: Pt, v: Pt) => Math.hypot(u[0] - v[0], u[1] - v[1]);
  const ptSeg = (p: Pt, s0: Pt, s1: Pt) => {
    const vx = s1[0] - s0[0];
    const vy = s1[1] - s0[1];
    const l2 = vx * vx + vy * vy;
    if (l2 === 0) return d(p, s0);
    let t = ((p[0] - s0[0]) * vx + (p[1] - s0[1]) * vy) / l2;
    t = Math.max(0, Math.min(1, t));
    return d(p, [s0[0] + t * vx, s0[1] + t * vy]);
  };
  const inter = (p1: Pt, p2: Pt, p3: Pt, p4: Pt) => {
    const o = (a: Pt, b: Pt, c: Pt) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    const d1 = o(p3, p4, p1);
    const d2 = o(p3, p4, p2);
    const d3 = o(p1, p2, p3);
    const d4 = o(p1, p2, p4);
    return d1 > 0 !== d2 > 0 && d3 > 0 !== d4 > 0;
  };
  if (inter(a0, a1, b0, b1)) return 0;
  return Math.min(ptSeg(a0, b0, b1), ptSeg(a1, b0, b1), ptSeg(b0, a0, a1), ptSeg(b1, a0, a1));
}

function polyDist(a: Pt[], b: Pt[]): number {
  if (a.some((v) => pointInPoly(v, b)) || b.some((v) => pointInPoly(v, a))) return 0;
  let best = Infinity;
  for (let i = 0; i < a.length; i++) {
    const a0 = a[i];
    const a1 = a[(i + 1) % a.length];
    for (let j = 0; j < b.length; j++) {
      best = Math.min(best, segDist(a0, a1, b[j], b[(j + 1) % b.length]));
    }
  }
  return best;
}

function segsIntersect(p1: Pt, p2: Pt, p3: Pt, p4: Pt): boolean {
  const o = (a: Pt, b: Pt, c: Pt) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const d1 = o(p3, p4, p1);
  const d2 = o(p3, p4, p2);
  const d3 = o(p1, p2, p3);
  const d4 = o(p1, p2, p4);
  return d1 > 0 !== d2 > 0 && d3 > 0 !== d4 > 0;
}

// A simple (non-self-intersecting) ring of >=3 vertices — mirrors the engine check.
export function polygonSimple(poly: Pt[]): boolean {
  const n = poly.length;
  if (n < 3) return false;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (j === i || (i + 1) % n === j || (j + 1) % n === i) continue;
      if (segsIntersect(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n])) return false;
    }
  }
  return true;
}

// --- movement-rule mirrors (clixengine/terrain.py predicates) ---------------
// The engine stays authoritative; these give the drag ghost live truth.
export function blocksMove(t: TerrainPiece): boolean {
  return t.kind === "blocking" || t.water === "deep";
}
function hindersMove(t: TerrainPiece): boolean {
  return !blocksMove(t) && (t.kind === "hindering" || t.water === "shallow" || t.low_wall);
}
function halvesSpeed(t: TerrainPiece): boolean {
  return hindersMove(t) && !t.low_wall;
}

function circleTouchesPoly(c: Pt, r: number, poly: Pt[]): boolean {
  if (pointInPoly(c, poly)) return true;
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i];
    const b = poly[(i + 1) % poly.length];
    if (segDist(c, c, a, b) <= r + 1e-6) return true;
  }
  return false;
}

// A circle of radius r SWEPT along the segment p0->p1 (the path it brushes).
// NOTE: despite tracing a stadium shape, this is NOT the mounted double-base
// "capsule" (two discrete circles — see rearCenter/centersAt/figureCenters).
// It was named capsuleCrossesPoly before real capsule bases existed; renamed
// to fence off that trap.
function sweptCircleCrossesPoly(p0: Pt, p1: Pt, r: number, poly: Pt[]): boolean {
  if (circleTouchesPoly(p0, r, poly) || circleTouchesPoly(p1, r, poly)) return true;
  if (pointInPoly(p0, poly) || pointInPoly(p1, poly)) return true;
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i];
    const b = poly[(i + 1) % poly.length];
    if (segDist(p0, p1, a, b) <= r + 1e-6) return true;
  }
  return false;
}

// Speed for the turn: halved (round up) when starting in speed-halving
// hindering. `centers` is every base-circle centre of the mover — EITHER
// circle touching at move start halves a mounted figure's speed ("any part of
// his base touching", Unl. p.12).
export function effectiveSpeed(speed: number, centers: Pt[], radius: number, terrain: TerrainPiece[]): number {
  const slowed = terrain.some(
    (t) => halvesSpeed(t) && centers.some((c) => circleTouchesPoly(c, radius, t.polygon as Pt[])),
  );
  return slowed ? Math.max(1, Math.ceil(speed / 2)) : speed;
}

// Why the engine would reject this move's geometry, or null — mirrors
// _validate_move's terrain rules (endpoint blocking for everyone; path blocking
// and the hindering entry-stop for non-fliers; a stuck start may walk out).
export function moveBlockReason(
  from: Pt,
  dest: Pt,
  radius: number,
  terrain: TerrainPiece[],
  flies: boolean,
  // Mounted movers: the rear circle's own segment. Endpoints are derived from
  // the START and END facings (P5-R10) — the rotation sweep in between is
  // approximated by the straight segment; the engine re-validates on submit.
  rear?: { from: Pt; dest: Pt } | null,
): string | null {
  const startCs: Pt[] = rear ? [from, rear.from] : [from];
  const destCs: Pt[] = rear ? [dest, rear.dest] : [dest];
  const sweeps: [Pt, Pt][] = rear ? [[from, dest], [rear.from, rear.dest]] : [[from, dest]];
  for (const t of terrain) {
    if (blocksMove(t) && destCs.some((c) => circleTouchesPoly(c, radius, t.polygon as Pt[]))) {
      return t.water === "deep" ? "can't end in deep water" : "can't end in blocking terrain";
    }
  }
  if (flies) return null;
  const stuck = terrain.some(
    (t) => blocksMove(t) && startCs.some((c) => circleTouchesPoly(c, radius, t.polygon as Pt[])),
  );
  if (stuck) return null; // escaping an illegal overlap — endpoint check only
  const moving = sweeps.some(([a, b]) => Math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-9);
  if (!moving) return null;
  for (const t of terrain) {
    if (blocksMove(t) && sweeps.some(([a, b]) => sweptCircleCrossesPoly(a, b, radius, t.polygon as Pt[]))) {
      return t.water === "deep" ? "path crosses deep water" : "path crosses blocking terrain";
    }
  }
  for (const t of terrain) {
    if (!hindersMove(t)) continue;
    if (startCs.some((c) => circleTouchesPoly(c, radius, t.polygon as Pt[]))) continue; // started in it
    // Entry-stop mirrors "ends when the base crosses COMPLETELY into" (Unl.
    // p.12): flag a pass-through only when every circle's sweep crosses and no
    // circle ends touching — a long base straddling a small feature keeps going.
    if (
      sweeps.every(([a, b]) => sweptCircleCrossesPoly(a, b, radius, t.polygon as Pt[])) &&
      !destCs.some((c) => circleTouchesPoly(c, radius, t.polygon as Pt[]))
    ) {
      return t.low_wall ? "stop at the low wall" : "entering hindering ends the move — stop inside";
    }
  }
  return null;
}

// Distance from a point to a segment — mirrors the engine's
// segment_circle_intersects check (a mover's path crossing another base).
export function pointToSegment(p: Pt, a: Pt, b: Pt): number {
  return segDist(p, p, a, b);
}

// --- base-contact snapping ---------------------------------------------------
// Shared by the battle drag and the deployment drag: pull a point onto the EXACT
// contact ring of the nearest base when within `window` inches of it. Exactness
// matters — engine contact tolerance is 0.02", so eyeballed gaps don't count.
//
// Two-tangent "pockets": when the cursor is near the notch where the mover would
// touch TWO bases at once (the intersection of their contact rings), snap there —
// `uid2` reports the second contact.
//
// Returns candidates RANKED by preference, not a single winner: a pocket can be
// illegal for reasons only the caller knows (outside the deploy band, beyond the
// mover's speed, fails formation cohesion), and the old best single-ring snap
// must survive as the fallback. Callers take the first candidate that passes
// their own legality checks.
//
// Pocket-vs-single ranking is aim-aware: a sloppy drop near the notch means
// "touch both", but a cursor sitting right ON a lone contact ring is a precise
// placement that must not be stolen into contact with a second (possibly enemy)
// base. The pocket outranks the nearest single only while its miss distance is
// within 3x the single's (capped at POCKET_EDGE beyond it) — dead-on ring aim
// makes the single unbeatable except at the notch itself, where they coincide.
const POCKET_EDGE = 0.35;

export interface SnapCandidate {
  point: Pt;
  uid: number;
  uid2?: number;
}

export function snapToContactRing(
  moverRadius: number,
  dest: Pt,
  // `key` is the DISTINCT circle identity — a mounted figure contributes two
  // entries with the same uid but different keys. The overlap filter skips by
  // key, so a figure's SIBLING circle is never exempted (naive same-uid
  // entries would offer snaps overlapping the figure's own waist). `uid`
  // stays figure-level for the pocket same-figure skip and the returned
  // contact report (faceUid).
  targets: { pos: Pt; radius: number; uid: number; key?: string }[],
  window = 0.9,
): SnapCandidate[] {
  const keyOf = (t: { uid: number; key?: string }) => t.key ?? String(t.uid);
  const clear = (cp: Pt, skipA: string, skipB: string) =>
    !targets.some(
      (o) =>
        keyOf(o) !== skipA &&
        keyOf(o) !== skipB &&
        Math.hypot(cp[0] - o.pos[0], cp[1] - o.pos[1]) < moverRadius + o.radius - 0.02,
    );

  type Cand = SnapCandidate & { err: number; pocket: boolean };
  const cands: Cand[] = [];

  for (const t of targets) {
    const dx = dest[0] - t.pos[0];
    const dy = dest[1] - t.pos[1];
    const dlen = Math.hypot(dx, dy) || 1e-9;
    const gap = moverRadius + t.radius;
    const err = Math.abs(dlen - gap);
    if (err > window) continue;
    const cp: Pt = [t.pos[0] + (dx / dlen) * gap, t.pos[1] + (dy / dlen) * gap];
    if (clear(cp, keyOf(t), keyOf(t))) cands.push({ point: cp, uid: t.uid, err, pocket: false });
  }

  for (let i = 0; i < targets.length; i++) {
    for (let j = i + 1; j < targets.length; j++) {
      const a = targets[i];
      const b = targets[j];
      // No self-waist pockets: a mounted figure's own front+rear pair is not a
      // two-contact notch in v1.
      if (a.uid === b.uid) continue;
      const ra = moverRadius + a.radius;
      const rb = moverRadius + b.radius;
      const dx = b.pos[0] - a.pos[0];
      const dy = b.pos[1] - a.pos[1];
      const d = Math.hypot(dx, dy);
      // Contact rings must intersect for a both-touching spot to exist.
      if (d < 1e-9 || d > ra + rb || d < Math.abs(ra - rb)) continue;
      const along = (ra * ra - rb * rb + d * d) / (2 * d);
      const h = Math.sqrt(Math.max(0, ra * ra - along * along));
      const mx = a.pos[0] + (dx / d) * along;
      const my = a.pos[1] + (dy / d) * along;
      for (const s of h > 1e-9 ? [1, -1] : [1]) {
        const cp: Pt = [mx + s * (-dy / d) * h, my + s * (dx / d) * h];
        const err = Math.hypot(dest[0] - cp[0], dest[1] - cp[1]);
        if (err > window) continue;
        if (clear(cp, keyOf(a), keyOf(b))) cands.push({ point: cp, uid: a.uid, uid2: b.uid, err, pocket: true });
      }
    }
  }

  cands.sort((x, y) => x.err - y.err);
  const bestSingle = cands.find((c) => !c.pocket);
  const bestPocket = cands.find((c) => c.pocket);
  if (
    bestPocket &&
    (!bestSingle ||
      bestPocket.err <= Math.min(bestSingle.err + POCKET_EDGE, 3 * bestSingle.err + 1e-9))
  ) {
    const rest = cands.filter((c) => c !== bestPocket);
    return [bestPocket, ...rest].map(({ point, uid, uid2 }) => ({ point, uid, uid2 }));
  }
  return cands.map(({ point, uid, uid2 }) => ({ point, uid, uid2 }));
}

// --- line-of-fire mirror (clixengine/engine.line_of_fire) --------------------
// Display-only: lets the board show WHERE and WHY a shot is blocked while
// hovering. The engine stays authoritative for what's actually legal.
function segmentCrossesPoly(p0: Pt, p1: Pt, poly: Pt[]): boolean {
  if (pointInPoly(p0, poly) || pointInPoly(p1, poly)) return true;
  for (let i = 0; i < poly.length; i++) {
    if (segsIntersect(p0, p1, poly[i], poly[(i + 1) % poly.length])) return true;
  }
  return false;
}

export interface LofFigure {
  uid: number;
  pos: Pt | [number, number];
  base_radius: number;
  owner: string;
  elevation: number;
  eliminated: boolean;
  short_name: string;
  active_abilities?: { name: string }[];
  // Mounted (double-base) figures: both circles participate in blocking,
  // screening, and melee-gap tests. facing_deg lets the rear be derived when
  // the server rear_pos is absent (fail-open: front-only if neither exists).
  mounted?: boolean;
  rear_pos?: [number, number];
  facing_deg?: number;
}

export interface LofVerdict {
  clear: boolean;
  reason?: string;
  terrainId?: number; // blocking terrain piece to highlight
  figUid?: number; // blocking/screening figure to highlight
}

export function lofBlocker(
  a: LofFigure & { facing_deg: number; arc_deg: number; range: number },
  t: LofFigure,
  figures: LofFigure[],
  terrain: TerrainPiece[],
): LofVerdict {
  const ap = a.pos as Pt;
  const tp = t.pos as Pt;
  // Sight line + range anchor on the FRONT dots (all measurements from the
  // front-half centre dot, P5-R2); only SHAPE tests below go capsule-aware.
  const centersOf = (f: LofFigure): Pt[] => {
    if (!f.mounted) return [f.pos as Pt];
    const rear =
      (f.rear_pos as Pt | undefined) ??
      (f.facing_deg != null
        ? rearCenter(f.pos as Pt, (f.facing_deg * Math.PI) / 180, f.base_radius)
        : null);
    return rear ? [f.pos as Pt, rear] : [f.pos as Pt];
  };
  const dist = Math.hypot(tp[0] - ap[0], tp[1] - ap[1]);
  // Front arc (facing_deg is a world +y-up angle; arc_deg is the HALF-angle).
  const facing = (a.facing_deg * Math.PI) / 180;
  const inArcTo = (target: Pt): boolean => {
    const bearing = Math.atan2(target[1] - ap[1], target[0] - ap[0]);
    let delta = Math.abs(bearing - facing) % (2 * Math.PI);
    if (delta > Math.PI) delta = 2 * Math.PI - delta;
    return delta <= (a.arc_deg * Math.PI) / 180 + 1e-9;
  };
  const inArc = inArcTo(tp);
  // MELEE figures get melee verdicts — ranged-LoF reasons (screening, range)
  // are meaningless for a range-0 attacker.
  if (a.range <= 0) {
    // Melee gap = min over circle pairs (either capsule may make the contact).
    const aCs = centersOf(a);
    const tCs = centersOf(t);
    let gap = Infinity;
    let nearT: Pt = tp;
    let viaRear = false;
    aCs.forEach((ac, ai) => {
      for (const tc of tCs) {
        const g = Math.hypot(tc[0] - ac[0], tc[1] - ac[1]) - (a.base_radius + t.base_radius);
        if (g < gap) {
          gap = g;
          nearT = tc;
          viaRear = ai === 1;
        }
      }
    });
    if (gap > 0.02) {
      return { clear: false, reason: `melee — not in base contact (${gap.toFixed(1)}″ away)` };
    }
    // Arc truth mirrors the engine's contact_arc (P5-R9): contact through the
    // attacker's own REAR circle is behind him for a <=180° total arc;
    // otherwise the angular test at the front dot toward the nearest target
    // circle centre.
    const inArcContact = viaRear && a.arc_deg <= 90 + 1e-9 ? false : inArcTo(nearT);
    return inArcContact
      ? { clear: true, reason: "in contact — close attack ⚔" }
      : { clear: false, reason: "in contact but BEHIND you — re-face to attack" };
  }
  if (!inArc) {
    return { clear: false, reason: "not in your front arc — re-face" };
  }
  if (dist > a.range + 1e-9) {
    return { clear: false, reason: `beyond range (${dist.toFixed(1)}″ of ${a.range}″)` };
  }
  // Magic Blast ignores blocked lines of fire (§Magic Blast) — when the plain
  // shot is blocked, the verdict says whether the blast is the answer, and if
  // not, exactly why (a silently-missing pointer reads as a bug).
  const hasBlast = a.active_abilities?.some((x) => x.name === "Magic Blast") ?? false;
  const blastImmune = t.active_abilities?.some((x) => x.name === "Magic Immunity") ?? false;
  const blastScreened = figures.some(
    (o) =>
      !o.eliminated &&
      o.owner === a.owner &&
      o.uid !== a.uid &&
      centersOf(t).some((tc) =>
        centersOf(o).some(
          (oc) => Math.hypot(tc[0] - oc[0], tc[1] - oc[1]) <= t.base_radius + o.base_radius + 0.02,
        ),
      ),
  );
  const blastNote = !hasBlast
    ? ""
    : blastImmune
      ? " — no Magic Blast: target is Magic Immune"
      : blastScreened
        ? " — no Magic Blast: your own figure screens it (P4-R25)"
        : " — Magic Blast still hits (unblockable)";
  const bothElev = a.elevation === 1 && t.elevation === 1;
  for (const piece of terrain) {
    const poly = piece.polygon as Pt[];
    if (!segmentCrossesPoly(ap, tp, poly)) continue;
    if (piece.kind === "blocking" && piece.water === null) {
      return { clear: false, reason: `blocked by blocking terrain${blastNote}`, terrainId: piece.id };
    }
    if (piece.elevated) {
      if (bothElev) continue; // both up high — see over the feature
      const standsOn =
        (a.elevation === 1 && pointInPoly(ap, poly)) || (t.elevation === 1 && pointInPoly(tp, poly));
      if (standsOn) continue; // your own hill never blocks your shot
      return { clear: false, reason: `blocked by elevated terrain${blastNote}`, terrainId: piece.id };
    }
  }
  for (const o of figures) {
    if (o.eliminated || o.uid === a.uid || o.uid === t.uid) continue;
    if (bothElev && o.elevation === 0) continue; // shot passes over ground bases
    // BOTH circles of a mounted blocker can cut the line of fire.
    if (centersOf(o).some((oc) => segDist(ap, tp, oc, oc) <= o.base_radius + 1e-6)) {
      return { clear: false, reason: `blocked by ${o.short_name}'s base${blastNote}`, figUid: o.uid };
    }
  }
  for (const o of figures) {
    if (o.eliminated || o.owner !== a.owner || o.uid === a.uid) continue;
    // Screening gap = min over target-circle x screener-circle pairs.
    let gap = Infinity;
    for (const tc of centersOf(t)) {
      for (const oc of centersOf(o)) {
        gap = Math.min(gap, Math.hypot(tc[0] - oc[0], tc[1] - oc[1]) - (t.base_radius + o.base_radius));
      }
    }
    if (gap <= 1e-6) {
      return { clear: false, reason: `${o.short_name} screens the target (P4-R25)`, figUid: o.uid };
    }
  }
  return { clear: true, reason: `clear · ${dist.toFixed(1)}″` };
}

// Drawn-terrain size caps — mirrors clixengine/terrain.py MAX_POLYGON_* so the
// drawing UI can show live legality before the server confirms.
export const MAX_POLYGON_AREA = 24; // in²
export const MIN_POLYGON_AREA = 0.5; // in²
export const MAX_POLYGON_EXTENT = 8; // longest vertex-to-vertex span, inches

// Unsigned shoelace area, in square inches.
export function polygonArea(poly: Pt[]): number {
  const n = poly.length;
  if (n < 3) return 0;
  let s = 0;
  for (let i = 0; i < n; i++) {
    const [ax, ay] = poly[i];
    const [bx, by] = poly[(i + 1) % n];
    s += ax * by - bx * ay;
  }
  return Math.abs(s) / 2;
}

// Longest vertex-to-vertex span (the shape's "diameter"), in inches.
export function polygonExtent(poly: Pt[]): number {
  let best = 0;
  for (let i = 0; i < poly.length; i++) {
    for (let j = i + 1; j < poly.length; j++) {
      best = Math.max(best, Math.hypot(poly[i][0] - poly[j][0], poly[i][1] - poly[j][1]));
    }
  }
  return best;
}

// Size verdict for a drawn shape, or null if within the caps.
export function sizeReason(poly: Pt[]): string | null {
  if (poly.length < 3) return null;
  const area = polygonArea(poly);
  const span = polygonExtent(poly);
  if (area > MAX_POLYGON_AREA) return `too big — ${area.toFixed(0)} in² (max ${MAX_POLYGON_AREA} in²)`;
  if (span > MAX_POLYGON_EXTENT) return `too long — ${span.toFixed(1)}" across (max ${MAX_POLYGON_EXTENT}")`;
  if (area < MIN_POLYGON_AREA) return "too thin to be a real terrain piece";
  return null;
}

// Why a candidate polygon may NOT be placed, or null if legal. Mirrors the engine
// (edge margin 1", starting bands 3" deep at both ends, >=2" from other pieces).
export function placementReason(
  poly: Pt[],
  existing: TerrainPiece[],
  boardW: number,
  boardH: number,
  edgeMargin = 1.0,
  startBand = 3.0,
  minGap = 2.0,
): string | null {
  for (const [x, y] of poly) {
    if (x < edgeMargin || x > boardW - edgeMargin) return "off the board";
    if (y < startBand || y > boardH - startBand) return "in a starting area";
  }
  for (const t of existing) {
    if (polyDist(poly, t.polygon as Pt[]) < minGap - 1e-9) return "too close to another piece";
  }
  return null;
}
