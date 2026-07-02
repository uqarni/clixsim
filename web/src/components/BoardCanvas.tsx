import { useEffect, useRef, useState, type PointerEvent as RPE, type WheelEvent as RWE } from "react";
import type { FigureView, GameView } from "../api";
import { effectiveSpeed, lofBlocker } from "../terrainGeom";

// A terrain shape being dragged during the placement phase (world coords).
export interface PlacingGhost {
  polygon: [number, number][];
  accessPoints: [number, number][];
  kind: string;
  elevated: boolean;
  water: string | null;
  lowWall: boolean;
  abrupt: boolean;
  ok: boolean;
}

interface MoveGhost {
  dest: [number, number];
  facing: number; // radians
  ok: boolean;
  breakAway: boolean;
  reason?: string; // why the drop is illegal — drawn at the ghost
}

interface PendingMove {
  dest: [number, number];
  facing: number;
}

// Transient combat effects, anchored in WORLD coords (inches).
export type Fx =
  | { kind: "dice"; x: number; y: number; dice: number[]; result?: string; dur: number }
  | { kind: "float"; x: number; y: number; text: string; color: string; dur: number }
  | { kind: "flash"; x: number; y: number; color: string; dur: number }
  | { kind: "ko"; x: number; y: number; dur: number };

interface Props {
  view: GameView;
  selectedUid: number | null;
  onSelect: (uid: number | null, additive?: boolean) => void;
  // Marquee (drag a box on empty felt) selecting the player's figures inside it.
  onMarquee?: (a: [number, number], b: [number, number]) => void;
  // The selected friendly figure that may be dragged to move (null if none).
  activeUid: number | null;
  // Figures currently targeted by an armed action — highlighted as reticles.
  armedTargets: number[];
  // Friendly formation members of an armed formation — highlighted.
  armedMembers: number[];
  moveGhost: MoveGhost | null;
  // The optional uid reports WHICH figure was grabbed (rigid formation drags
  // accept any member; single-figure drags always pass the active figure).
  onMoveDrag: (dest: [number, number], dragUid?: number) => void;
  onMoveDrop: (dest: [number, number], dragUid?: number) => void;
  onMoveCancel: () => void;
  // A placed-but-uncommitted move whose facing is being aimed via the handle.
  pendingMove: PendingMove | null;
  onFaceDrag: (facing: number) => void;
  // Combat effects to play; fxSeq bumps to trigger a new batch.
  fx: Fx[];
  fxSeq: number;
  // Terrain placement (setup phase). When placementMode is on, pointer events
  // aim/commit the ghost instead of selecting/moving figures.
  placementMode?: boolean;
  placingGhost?: PlacingGhost | null;
  onPlacePointer?: (world: [number, number], commit: boolean) => void;
  onPlaceRotate?: (deltaRad: number) => void;
  // Free spin (P4-R9): a contacted figure being re-faced (drag the handle to aim).
  spin?: SpinGhost | null;
  onSpinFace?: (facing: number) => void;
  // Interactive formation move: members already placed this staging (ghosts at
  // their destinations), figures to dim (they've been staged away), and the
  // formation speed that overrides the active figure's reach ring (P4-R13).
  // `ok` colors a ghost green (legal) / red (can't land there); undefined keeps
  // the neutral staged blue.
  staged?: { dest: [number, number]; facing: number; radius: number; ok?: boolean; uid?: number }[] | null;
  dimUids?: number[];
  reachOverride?: number | null;
  // Rigid formation move: drag ANY member to translate the whole block; once
  // pending, a pivot handle at the block's centroid rotates it (like facing).
  rigid?: { uids: number[]; pivot: [number, number]; theta: number; pending: boolean } | null;
  onRigidPivot?: (theta: number) => void;
  // Deploy setup: shade the human's 3" starting band as the legal deploy zone.
  deployBand?: boolean;
  // Terrain draw tool: an in-progress hand-drawn polygon + click/move/undo hooks.
  draw?: DrawGhost | null;
  onDrawPoint?: (world: [number, number]) => void;
  onDrawMove?: (world: [number, number]) => void;
  onDrawUndo?: () => void;
  onDrawLeave?: () => void;
}

export interface DrawGhost {
  poly: [number, number][]; // committed vertices (world inches)
  cursor: [number, number] | null;
  ok: boolean;
  kind: string;
  elevated: boolean;
  water: string | null;
  lowWall: boolean;
}

export interface SpinGhost {
  uid: number;
  pos: [number, number];
  facing: number; // radians
}

interface Transform {
  scale: number; // css px per inch
  offX: number;
  offY: number;
  h: number; // board height (inches) — used to flip Y so +y renders upward
}

const FELT_MARGIN = 24;

const COLORS = {
  human: "#4a9de0",
  humanSoft: "rgba(74, 157, 224, 0.22)",
  llm: "#e07a4a",
  llmSoft: "rgba(224, 122, 74, 0.22)",
  felt: "#1b3b2f",
  feltEdge: "#122a22",
  feltLine: "rgba(120, 180, 150, 0.10)",
  select: "#7c9cff",
  text: "#e7ebf2",
  good: "#5bd68a",
  bad: "#e05a5a",
  warn: "#e0c04a",
};

function computeTransform(cssW: number, cssH: number, boardW: number, boardH: number): Transform {
  const availW = cssW - FELT_MARGIN * 2;
  const availH = cssH - FELT_MARGIN * 2;
  const scale = Math.max(1, Math.min(availW / boardW, availH / boardH));
  const drawnW = boardW * scale;
  const drawnH = boardH * scale;
  return { scale, offX: (cssW - drawnW) / 2, offY: (cssH - drawnH) / 2, h: boardH };
}

// The engine's convention is +y = up (the human deploys at low y, faces +y toward
// the centre). Canvas y grows downward, so flip: world +y renders toward the top,
// putting the human at the BOTTOM of the screen (matches the CLI renderer).
function worldToScreen(t: Transform, x: number, y: number): [number, number] {
  return [t.offX + x * t.scale, t.offY + (t.h - y) * t.scale];
}
function screenToWorld(t: Transform, sx: number, sy: number): [number, number] {
  return [(sx - t.offX) / t.scale, t.h - (sy - t.offY) / t.scale];
}
function toRad(deg: number): number {
  return (deg * Math.PI) / 180;
}

function drawFigure(
  ctx: CanvasRenderingContext2D,
  t: Transform,
  f: FigureView,
  selected: boolean,
  hovered: boolean,
  dimmed: boolean,
) {
  const [cx, cy] = worldToScreen(t, f.pos[0], f.pos[1]);
  const r = Math.max(6, f.base_radius * t.scale);
  const hue = f.owner === "human" ? COLORS.human : COLORS.llm;
  const soft = f.owner === "human" ? COLORS.humanSoft : COLORS.llmSoft;

  ctx.save();
  if (dimmed) ctx.globalAlpha = 0.5;

  // Front-arc wedge. facing_deg is a world angle (+y up); negate for the flipped
  // canvas so the wedge points the right way on screen.
  const wedgeR = r * 2.4;
  const sf = toRad(-f.facing_deg);
  const ha = toRad(f.arc_deg);
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, wedgeR, sf - ha, sf + ha, false);
  ctx.closePath();
  ctx.fillStyle = soft;
  ctx.fill();

  // Base.
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = hue;
  ctx.fill();
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = "rgba(0,0,0,0.4)";
  ctx.stroke();

  // Facing tick.
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.cos(sf) * r, cy + Math.sin(sf) * r);
  ctx.strokeStyle = "rgba(255,255,255,0.85)";
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // Elevation badge: a small chevron marking a figure on high ground.
  if (f.elevation === 1) {
    ctx.save();
    ctx.fillStyle = "#e7d9a0";
    ctx.strokeStyle = "rgba(0,0,0,0.5)";
    ctx.lineWidth = 1;
    const bx = cx + r * 0.9;
    const by = cy - r * 0.9;
    ctx.beginPath();
    ctx.moveTo(bx, by - 4);
    ctx.lineTo(bx + 4, by + 3);
    ctx.lineTo(bx - 4, by + 3);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  // Health ring.
  const ringR = r + 3;
  ctx.beginPath();
  ctx.arc(cx, cy, ringR, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(0,0,0,0.35)";
  ctx.lineWidth = 3;
  ctx.stroke();
  const frac = Math.max(0, Math.min(1, f.health_fraction));
  ctx.beginPath();
  ctx.arc(cx, cy, ringR, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * frac);
  ctx.strokeStyle = frac > 0.5 ? COLORS.good : frac > 0.25 ? COLORS.warn : COLORS.bad;
  ctx.lineWidth = 3;
  ctx.stroke();

  // Push-token pips.
  if (f.action_tokens > 0) {
    const n = f.action_tokens;
    const spread = Math.min(0.9, 0.28 * n);
    for (let i = 0; i < n; i++) {
      const a = -Math.PI / 2 + (i - (n - 1) / 2) * spread;
      ctx.beginPath();
      ctx.arc(cx + Math.cos(a) * (r + 8), cy + Math.sin(a) * (r + 8), 2.5, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.warn;
      ctx.fill();
    }
  }

  if (selected || hovered) {
    ctx.beginPath();
    ctx.arc(cx, cy, ringR + 4, 0, Math.PI * 2);
    ctx.strokeStyle = selected ? COLORS.select : "rgba(255,255,255,0.5)";
    ctx.lineWidth = selected ? 2 : 1.5;
    ctx.stroke();
  }

  if (selected || hovered) {
    const label = f.short_name;
    ctx.font = "500 12px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const tw = ctx.measureText(label).width;
    const ly = cy + ringR + 6;
    ctx.fillStyle = "rgba(10,16,14,0.72)";
    ctx.fillRect(cx - tw / 2 - 4, ly - 2, tw + 8, 16);
    ctx.fillStyle = COLORS.text;
    ctx.fillText(label, cx, ly);
  }

  ctx.restore();
}

// Is `targetPos` inside the front arc of a figure standing at `from` with the
// given world facing (radians) and arc HALF-angle (degrees)?
function inArcFrom(
  from: [number, number],
  facingRad: number,
  arcDeg: number,
  targetPos: [number, number],
): boolean {
  const bearing = Math.atan2(targetPos[1] - from[1], targetPos[0] - from[0]);
  let d = Math.abs(bearing - facingRad) % (2 * Math.PI);
  if (d > Math.PI) d = 2 * Math.PI - d;
  return d <= (arcDeg * Math.PI) / 180 + 1e-9;
}

// While placing/aiming a move next to enemies, ring each ADJACENT enemy green
// (in the front arc = attackable after this move) or red (behind you — not).
function drawAdjacencyArcs(
  ctx: CanvasRenderingContext2D,
  t: Transform,
  active: FigureView,
  live: FigureView[],
  dest: [number, number],
  facingRad: number,
) {
  for (const o of live) {
    if (o.owner === active.owner || o.uid === active.uid) continue;
    const touching =
      Math.hypot(dest[0] - o.pos[0], dest[1] - o.pos[1]) <= active.base_radius + o.base_radius + 0.05;
    if (!touching) continue;
    const ok = inArcFrom(dest, facingRad, active.arc_deg, o.pos);
    const [ox, oy] = worldToScreen(t, o.pos[0], o.pos[1]);
    const rr = Math.max(6, o.base_radius * t.scale) + 5;
    ctx.save();
    ctx.beginPath();
    ctx.arc(ox, oy, rr, 0, Math.PI * 2);
    ctx.strokeStyle = ok ? COLORS.good : COLORS.bad;
    ctx.lineWidth = 2.5;
    ctx.stroke();
    ctx.fillStyle = ok ? COLORS.good : COLORS.bad;
    ctx.font = "600 10px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.fillText(ok ? "⚔ attackable" : "behind you", ox, oy - rr - 2);
    ctx.restore();
  }
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function drawFx(ctx: CanvasRenderingContext2D, t: Transform, f: Fx, p: number) {
  const [sx, sy] = worldToScreen(t, f.x, f.y);
  ctx.save();
  if (f.kind === "float") {
    ctx.globalAlpha = Math.max(0, 1 - p);
    ctx.fillStyle = f.color;
    ctx.font = "600 15px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(f.text, sx, sy - 14 - p * 26);
  } else if (f.kind === "flash") {
    ctx.globalAlpha = Math.max(0, (1 - p) * 0.9);
    ctx.beginPath();
    ctx.arc(sx, sy, 12 + p * 22, 0, Math.PI * 2);
    ctx.strokeStyle = f.color;
    ctx.lineWidth = 3;
    ctx.stroke();
  } else if (f.kind === "ko") {
    ctx.globalAlpha = Math.max(0, 1 - p);
    const s = 10 + p * 16;
    ctx.strokeStyle = COLORS.bad;
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    ctx.moveTo(sx - s, sy - s);
    ctx.lineTo(sx + s, sy + s);
    ctx.moveTo(sx + s, sy - s);
    ctx.lineTo(sx - s, sy + s);
    ctx.stroke();
    ctx.fillStyle = COLORS.bad;
    ctx.font = "600 13px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("KO", sx, sy - s - 4);
  } else if (f.kind === "dice") {
    const appear = Math.min(1, p * 6);
    ctx.globalAlpha = Math.max(0, p > 0.75 ? 1 - (p - 0.75) / 0.25 : 1);
    const cy = sy - 34;
    const sz = 18 * appear;
    const gap = 5;
    const total = f.dice.length * sz + (f.dice.length - 1) * gap;
    let x = sx - total / 2;
    ctx.textBaseline = "middle";
    for (const d of f.dice) {
      ctx.fillStyle = "#e7ebf2";
      roundRect(ctx, x, cy - sz / 2, sz, sz, 3);
      ctx.fill();
      ctx.fillStyle = "#10131a";
      ctx.font = `600 ${Math.round(sz * 0.7)}px system-ui, sans-serif`;
      ctx.textAlign = "center";
      ctx.fillText(String(d), x + sz / 2, cy);
      x += sz + gap;
    }
    ctx.textBaseline = "alphabetic";
    if (f.result) {
      const col =
        f.result === "crit_hit" ? COLORS.good : f.result === "hit" ? COLORS.warn :
        f.result === "miss" || f.result === "crit_miss" ? COLORS.bad : "#c3ccd8";
      const label =
        f.result === "crit_hit" ? "CRIT" : f.result === "crit_miss" ? "CRIT MISS" :
        f.result === "hit" ? "HIT" : f.result === "miss" ? "MISS" : f.result;
      ctx.fillStyle = col;
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(label, sx, cy + sz);
    }
  }
  ctx.restore();
}

function dashedRing(ctx: CanvasRenderingContext2D, cx: number, cy: number, rr: number, color: string) {
  if (rr <= 0) return;
  ctx.save();
  ctx.setLineDash([6, 5]);
  ctx.beginPath();
  ctx.arc(cx, cy, rr, 0, Math.PI * 2);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.restore();
}

interface TerrainLike {
  polygon: [number, number][];
  accessPoints?: [number, number][];
  access_points?: [number, number][];
  kind: string;
  elevated: boolean;
  water: string | null;
  lowWall?: boolean;
  low_wall?: boolean;
  abrupt: boolean;
}

function terrainStyle(t: TerrainLike): { fill: string; stroke: string; contour?: string } {
  const lowWall = t.lowWall ?? t.low_wall ?? false;
  if (t.water === "deep") return { fill: "rgba(46,86,150,0.72)", stroke: "#2b5590" };
  if (t.water === "shallow") return { fill: "rgba(92,156,214,0.55)", stroke: "#3f7fb0" };
  if (t.kind === "blocking") return { fill: "#41464e", stroke: "#20232a" };
  if (t.elevated)
    return { fill: "rgba(156,140,92,0.55)", stroke: "#8a7a45", contour: "rgba(214,200,150,0.75)" };
  if (lowWall) return { fill: "#6d7178", stroke: "#3a3d44" };
  if (t.kind === "hindering") return { fill: "rgba(58,120,72,0.55)", stroke: "#2f6b3f" };
  return { fill: "rgba(150,150,160,0.35)", stroke: "#5a5f68" };
}

function polyPath(ctx: CanvasRenderingContext2D, pts: [number, number][]) {
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.closePath();
}

function drawTerrainPiece(
  ctx: CanvasRenderingContext2D,
  t: Transform,
  piece: TerrainLike,
  ghost?: { ok: boolean },
) {
  const pts = piece.polygon.map(([x, y]) => worldToScreen(t, x, y));
  if (pts.length < 3) return;
  const style = terrainStyle(piece);
  ctx.save();
  polyPath(ctx, pts);
  if (ghost) {
    ctx.globalAlpha = 0.55;
    ctx.fillStyle = ghost.ok ? "rgba(91,214,138,0.4)" : "rgba(224,90,90,0.4)";
    ctx.fill();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = ghost.ok ? COLORS.good : COLORS.bad;
    ctx.lineWidth = 2;
    ctx.stroke();
  } else {
    ctx.fillStyle = style.fill;
    ctx.fill();
    ctx.strokeStyle = style.stroke;
    ctx.lineWidth = piece.abrupt ? 3 : 1.5;
    ctx.stroke();
  }

  // Elevation: an inset contour line to read as raised ground.
  if (piece.elevated && (style.contour || ghost)) {
    const cx = pts.reduce((s, p) => s + p[0], 0) / pts.length;
    const cy = pts.reduce((s, p) => s + p[1], 0) / pts.length;
    const inset = pts.map(([x, y]) => [cx + (x - cx) * 0.6, cy + (y - cy) * 0.6] as [number, number]);
    ctx.setLineDash([]);
    ctx.globalAlpha = ghost ? 0.5 : 1;
    polyPath(ctx, inset);
    ctx.strokeStyle = ghost ? (ghost.ok ? COLORS.good : COLORS.bad) : style.contour!;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  // Forest: a few darker canopy dabs so woods read as woods.
  if (piece.kind === "hindering" && !(piece.lowWall ?? piece.low_wall) && piece.water === null) {
    const cx = pts.reduce((s, p) => s + p[0], 0) / pts.length;
    const cy = pts.reduce((s, p) => s + p[1], 0) / pts.length;
    ctx.setLineDash([]);
    ctx.fillStyle = ghost ? "rgba(47,107,63,0.4)" : "rgba(30,74,44,0.65)";
    for (let i = 0; i < pts.length; i++) {
      const [x, y] = pts[i];
      const dx = cx + (x - cx) * 0.5;
      const dy = cy + (y - cy) * 0.5;
      ctx.beginPath();
      ctx.arc(dx, dy, 3, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // Abrupt-elevated access points: light notches where a figure may climb on/off.
  const aps = piece.accessPoints ?? piece.access_points ?? [];
  if (aps.length) {
    ctx.setLineDash([]);
    for (const [ax, ay] of aps) {
      const [sx, sy] = worldToScreen(t, ax, ay);
      ctx.beginPath();
      ctx.arc(sx, sy, 4, 0, Math.PI * 2);
      ctx.fillStyle = ghost ? "rgba(255,255,255,0.6)" : "#e7d9a0";
      ctx.fill();
      ctx.strokeStyle = "rgba(0,0,0,0.5)";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  }
  ctx.restore();
}

export default function BoardCanvas({
  view,
  selectedUid,
  onSelect,
  onMarquee,
  activeUid,
  armedTargets,
  armedMembers,
  moveGhost,
  onMoveDrag,
  onMoveDrop,
  onMoveCancel,
  pendingMove,
  onFaceDrag,
  fx,
  fxSeq,
  placementMode = false,
  placingGhost = null,
  onPlacePointer,
  onPlaceRotate,
  spin = null,
  onSpinFace,
  staged = null,
  dimUids = [],
  reachOverride = null,
  rigid = null,
  onRigidPivot,
  deployBand = false,
  draw = null,
  onDrawPoint,
  onDrawMove,
  onDrawUndo,
  onDrawLeave,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const fxCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [hoverUid, setHoverUid] = useState<number | null>(null);
  // Marquee selection (drag on empty felt), in world coords.
  const [marquee, setMarquee] = useState<{ a: [number, number]; b: [number, number] } | null>(null);
  const marqueeRef = useRef(false);
  const transformRef = useRef<Transform | null>(null);
  const dragRef = useRef<{ moved: boolean; uid?: number } | null>(null);
  const faceRef = useRef<boolean>(false);

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0].contentRect;
      setSize({ w: Math.floor(cr.width), h: Math.floor(cr.height) });
    });
    ro.observe(wrap);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || size.w === 0 || size.h === 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(size.w * dpr);
    canvas.height = Math.round(size.h * dpr);
    canvas.style.width = `${size.w}px`;
    canvas.style.height = `${size.h}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const { width: bw, height: bh } = view.meta.board;
    const t = computeTransform(size.w, size.h, bw, bh);
    transformRef.current = t;

    ctx.fillStyle = COLORS.feltEdge;
    ctx.fillRect(0, 0, size.w, size.h);
    // Play-area top-left in screen space is (offX, offY) regardless of the Y-flip;
    // worldToScreen(0,0) is the BOTTOM-left after the flip, which drew the felt
    // downward off-canvas.
    const px0 = t.offX;
    const py0 = t.offY;
    const pw = bw * t.scale;
    const ph = bh * t.scale;
    ctx.fillStyle = COLORS.felt;
    ctx.fillRect(px0, py0, pw, ph);

    ctx.strokeStyle = COLORS.feltLine;
    ctx.lineWidth = 1;
    for (let gx = 6; gx < bw; gx += 6) {
      const [sx] = worldToScreen(t, gx, 0);
      ctx.beginPath();
      ctx.moveTo(sx, py0);
      ctx.lineTo(sx, py0 + ph);
      ctx.stroke();
    }
    for (let gy = 6; gy < bh; gy += 6) {
      const [, sy] = worldToScreen(t, 0, gy);
      ctx.beginPath();
      ctx.moveTo(px0, sy);
      ctx.lineTo(px0 + pw, sy);
      ctx.stroke();
    }
    ctx.strokeStyle = "rgba(0,0,0,0.5)";
    ctx.lineWidth = 2;
    ctx.strokeRect(px0, py0, pw, ph);

    // Terrain sits on the felt, beneath the figures.
    for (const piece of view.terrain) drawTerrainPiece(ctx, t, piece);

    // Placement phase: the 3"-deep starting bands are off-limits — shade them.
    if (placementMode) {
      ctx.save();
      ctx.fillStyle = "rgba(224,90,90,0.10)";
      const [, byTop] = worldToScreen(t, 0, bh - 3);
      const [, byBot] = worldToScreen(t, 0, 3);
      ctx.fillRect(px0, py0, pw, byTop - py0); // enemy band (top)
      ctx.fillRect(px0, byBot, pw, py0 + ph - byBot); // your band (bottom)
      ctx.restore();
    }

    // Deploy: shade the human's 3" band as the legal placement zone.
    if (deployBand) {
      ctx.save();
      ctx.fillStyle = "rgba(91,214,138,0.10)";
      const [, byBot] = worldToScreen(t, 0, 3);
      ctx.fillRect(px0, byBot, pw, py0 + ph - byBot);
      ctx.restore();
    }

    const live = view.figures.filter((f) => !f.eliminated);
    const selected = live.find((f) => f.uid === selectedUid) ?? null;
    const active = live.find((f) => f.uid === activeUid) ?? null;

    // Rings beneath figures: range ring for a ranged selection; speed reach ring
    // for the active (draggable) figure so you can see how far it may move.
    if (selected && selected.range > 0) {
      const [cx, cy] = worldToScreen(t, selected.pos[0], selected.pos[1]);
      dashedRing(ctx, cx, cy, selected.range * t.scale, "rgba(124,156,255,0.6)");
    }
    if (active && active.speed > 0) {
      const [cx, cy] = worldToScreen(t, active.pos[0], active.pos[1]);
      // The reach ring reflects the hindering-halved speed (fliers exempt).
      const activeFlies = active.active_abilities.some(
        (a) => a.name === "Flight" || a.name === "Aquatic",
      );
      const reach = reachOverride ?? (activeFlies
        ? active.speed
        : effectiveSpeed(active.speed, active.pos, active.base_radius, view.terrain));
      dashedRing(ctx, cx, cy, reach * t.scale, "rgba(91,214,138,0.5)");
      if (reach < active.speed) {
        ctx.save();
        ctx.fillStyle = "rgba(224,192,74,0.9)";
        ctx.font = "500 11px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(`speed halved: ${reach}″`, cx, cy - reach * t.scale - 5);
        ctx.restore();
      }
    }

    // Line of fire on hover (friendly selected -> hovered enemy): GREEN when the
    // shot is clear, RED with the reason where it's blocked, and the blocking
    // terrain piece / figure highlighted — so "why can't I shoot" answers itself.
    if (selected && hoverUid != null && hoverUid !== selected.uid) {
      const hv = live.find((f) => f.uid === hoverUid);
      if (hv && hv.owner !== selected.owner) {
        const verdict = lofBlocker(selected, hv, view.figures, view.terrain);
        const col = verdict.clear ? "rgba(91,214,138,0.9)" : "rgba(224,90,90,0.9)";
        const [ax, ay] = worldToScreen(t, selected.pos[0], selected.pos[1]);
        const [bx, by] = worldToScreen(t, hv.pos[0], hv.pos[1]);
        ctx.save();
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
        ctx.strokeStyle = col;
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.setLineDash([]);
        if (verdict.terrainId != null) {
          const piece = view.terrain.find((p) => p.id === verdict.terrainId);
          if (piece) {
            polyPath(ctx, piece.polygon.map(([x, y]) => worldToScreen(t, x, y)));
            ctx.strokeStyle = "rgba(224,90,90,0.95)";
            ctx.lineWidth = 2.5;
            ctx.stroke();
          }
        }
        if (verdict.figUid != null) {
          const bf = live.find((f) => f.uid === verdict.figUid);
          if (bf) {
            const [fx2, fy2] = worldToScreen(t, bf.pos[0], bf.pos[1]);
            ctx.beginPath();
            ctx.arc(fx2, fy2, Math.max(6, bf.base_radius * t.scale) + 5, 0, Math.PI * 2);
            ctx.strokeStyle = "rgba(224,90,90,0.95)";
            ctx.lineWidth = 2.5;
            ctx.stroke();
          }
        }
        if (verdict.reason) {
          const mx = (ax + bx) / 2;
          const my = (ay + by) / 2;
          ctx.font = "500 11px system-ui, sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "bottom";
          const tw = ctx.measureText(verdict.reason).width;
          ctx.fillStyle = "rgba(10,16,14,0.8)";
          ctx.fillRect(mx - tw / 2 - 4, my - 15, tw + 8, 16);
          ctx.fillStyle = col;
          ctx.fillText(verdict.reason, mx, my - 1);
        }
        ctx.restore();
      }
    }

    // Move ghost (drag preview).
    if (moveGhost && active) {
      const [ox, oy] = worldToScreen(t, active.pos[0], active.pos[1]);
      const [gx, gy] = worldToScreen(t, moveGhost.dest[0], moveGhost.dest[1]);
      const col = moveGhost.ok ? COLORS.good : COLORS.bad;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(ox, oy);
      ctx.lineTo(gx, gy);
      ctx.strokeStyle = col;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.setLineDash([]);
      const gr = Math.max(6, active.base_radius * t.scale);
      ctx.beginPath();
      ctx.arc(gx, gy, gr, 0, Math.PI * 2);
      ctx.fillStyle = moveGhost.ok ? "rgba(91,214,138,0.28)" : "rgba(224,90,90,0.28)";
      ctx.fill();
      ctx.strokeStyle = col;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(gx, gy);
      ctx.lineTo(gx + Math.cos(-moveGhost.facing) * gr, gy + Math.sin(-moveGhost.facing) * gr);
      ctx.stroke();
      const ghostLabel = !moveGhost.ok && moveGhost.reason
        ? moveGhost.reason
        : moveGhost.breakAway
          ? "break-away"
          : null;
      if (ghostLabel) {
        ctx.fillStyle = moveGhost.ok ? COLORS.warn : COLORS.bad;
        ctx.font = "500 11px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        ctx.fillText(ghostLabel, gx, gy - gr - 3);
      }
      ctx.restore();
      if (moveGhost.ok) {
        drawAdjacencyArcs(ctx, t, active, live, moveGhost.dest, moveGhost.facing);
      }
    }

    // Pending move: placed ghost with a draggable facing handle (aim, then confirm).
    if (pendingMove && active) {
      const [gx, gy] = worldToScreen(t, pendingMove.dest[0], pendingMove.dest[1]);
      const r = Math.max(6, active.base_radius * t.scale);
      const arc = toRad(active.arc_deg);
      const sf = -pendingMove.facing; // world +y-up angle -> flipped screen angle
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(gx, gy);
      ctx.arc(gx, gy, r * 2.4, sf - arc, sf + arc, false);
      ctx.closePath();
      ctx.fillStyle = "rgba(124,156,255,0.18)";
      ctx.fill();
      ctx.beginPath();
      ctx.arc(gx, gy, r, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(124,156,255,0.5)";
      ctx.fill();
      ctx.strokeStyle = COLORS.select;
      ctx.lineWidth = 2;
      ctx.stroke();
      const hr = r * 2.4;
      const hx = gx + Math.cos(sf) * hr;
      const hy = gy + Math.sin(sf) * hr;
      ctx.beginPath();
      ctx.moveTo(gx, gy);
      ctx.lineTo(hx, hy);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(hx, hy, 6, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.select;
      ctx.fill();
      ctx.restore();
      // Live attackability while AIMING: adjacent enemies ring green (in the
      // front arc) or red (behind you) as the handle rotates.
      drawAdjacencyArcs(ctx, t, active, live, pendingMove.dest, pendingMove.facing);
    }

    // Free spin: an amber facing handle on the contacted figure being re-faced.
    if (spin) {
      const sf0 = live.find((f) => f.uid === spin.uid);
      const [gx, gy] = worldToScreen(t, spin.pos[0], spin.pos[1]);
      const r = Math.max(6, (sf0?.base_radius ?? 0.55) * t.scale);
      const arc = toRad(sf0?.arc_deg ?? 45);
      const sf = -spin.facing;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(gx, gy);
      ctx.arc(gx, gy, r * 2.4, sf - arc, sf + arc, false);
      ctx.closePath();
      ctx.fillStyle = "rgba(224,192,74,0.22)";
      ctx.fill();
      ctx.beginPath();
      ctx.arc(gx, gy, r + 3, 0, Math.PI * 2);
      ctx.strokeStyle = COLORS.warn;
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.setLineDash([]);
      const hr = r * 2.4;
      const hx = gx + Math.cos(sf) * hr;
      const hy = gy + Math.sin(sf) * hr;
      ctx.beginPath();
      ctx.moveTo(gx, gy);
      ctx.lineTo(hx, hy);
      ctx.strokeStyle = COLORS.warn;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(hx, hy, 6, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.warn;
      ctx.fill();
      ctx.restore();
    }

    // Rigid formation pivot: a handle at the block's centroid — drag to rotate
    // the whole formation (member ghosts and facings follow via `staged`).
    if (rigid?.pending) {
      const [gx, gy] = worldToScreen(t, rigid.pivot[0], rigid.pivot[1]);
      const r = Math.max(6, 0.7 * t.scale);
      const sf = -rigid.theta;
      ctx.save();
      ctx.beginPath();
      ctx.arc(gx, gy, 5, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.select;
      ctx.fill();
      const hr = r * 2.4;
      const hx = gx + Math.cos(sf) * hr;
      const hy = gy + Math.sin(sf) * hr;
      ctx.beginPath();
      ctx.moveTo(gx, gy);
      ctx.lineTo(hx, hy);
      ctx.strokeStyle = COLORS.select;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(hx, hy, 6, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.select;
      ctx.fill();
      ctx.restore();
    }

    // Armed-action highlights: red reticle on targets, blue ring on formation members.
    const armedSet = new Set(armedTargets);
    const memberSet = new Set(armedMembers);
    const dimSet = new Set(dimUids);
    for (const f of live) {
      drawFigure(ctx, t, f, f.uid === selectedUid, f.uid === hoverUid, dimSet.has(f.uid));
      const [cx, cy] = worldToScreen(t, f.pos[0], f.pos[1]);
      const rr = Math.max(6, f.base_radius * t.scale) + 7;
      if (armedSet.has(f.uid)) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(cx, cy, rr, 0, Math.PI * 2);
        ctx.strokeStyle = COLORS.bad;
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.restore();
      }
      if (memberSet.has(f.uid)) {
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.arc(cx, cy, rr, 0, Math.PI * 2);
        ctx.strokeStyle = COLORS.select;
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.restore();
      }
    }

    // Contact links: a dot where two bases LEGALLY touch (engine truth via
    // in_base_contact_with) — so "touching" is visible fact, not a guess. Amber
    // for enemy contact (engagement), white for friendly contact (formations).
    {
      const drawnPairs = new Set<string>();
      for (const f of live) {
        for (const u of f.in_base_contact_with) {
          const key = f.uid < u ? `${f.uid}:${u}` : `${u}:${f.uid}`;
          if (drawnPairs.has(key)) continue;
          drawnPairs.add(key);
          const o = live.find((x) => x.uid === u);
          if (!o) continue;
          const dx = o.pos[0] - f.pos[0];
          const dy = o.pos[1] - f.pos[1];
          const L = Math.hypot(dx, dy) || 1e-9;
          const [sx, sy] = worldToScreen(
            t,
            f.pos[0] + (dx / L) * f.base_radius,
            f.pos[1] + (dy / L) * f.base_radius,
          );
          // For the SELECTED figure's enemy contacts, the dot tells arc truth:
          // green = in its front arc (close attack legal), red = behind it.
          let fill = o.owner !== f.owner ? "rgba(224,192,74,0.95)" : "rgba(255,255,255,0.85)";
          if (o.owner !== f.owner && (f.uid === selectedUid || o.uid === selectedUid)) {
            const me = f.uid === selectedUid ? f : o;
            const them = f.uid === selectedUid ? o : f;
            const ok = inArcFrom(me.pos, (me.facing_deg * Math.PI) / 180, me.arc_deg, them.pos);
            fill = ok ? "rgba(91,214,138,0.95)" : "rgba(224,90,90,0.95)";
          }
          ctx.save();
          ctx.beginPath();
          ctx.arc(sx, sy, 3.5, 0, Math.PI * 2);
          ctx.fillStyle = fill;
          ctx.fill();
          ctx.strokeStyle = "rgba(0,0,0,0.55)";
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.restore();
        }
      }
    }

    // Marquee selection box.
    if (marquee) {
      const [mx0, my0] = worldToScreen(t, marquee.a[0], marquee.a[1]);
      const [mx1, my1] = worldToScreen(t, marquee.b[0], marquee.b[1]);
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = COLORS.select;
      ctx.fillStyle = "rgba(124,156,255,0.10)";
      ctx.lineWidth = 1.5;
      const rx = Math.min(mx0, mx1);
      const ry = Math.min(my0, my1);
      ctx.fillRect(rx, ry, Math.abs(mx1 - mx0), Math.abs(my1 - my0));
      ctx.strokeRect(rx, ry, Math.abs(mx1 - mx0), Math.abs(my1 - my0));
      ctx.restore();
    }

    // Staged formation members: ghosts at their placed destinations, numbered
    // in placement order, with facing ticks.
    if (staged) {
      staged.forEach((s, i) => {
        const [sx, sy] = worldToScreen(t, s.dest[0], s.dest[1]);
        const sr = Math.max(6, s.radius * t.scale);
        const sf = -s.facing;
        ctx.save();
        ctx.beginPath();
        ctx.arc(sx, sy, sr, 0, Math.PI * 2);
        // ok=true/false → live green/red legality (rigid move); undefined keeps
        // the neutral staged blue (place-one-at-a-time ghosts).
        ctx.fillStyle =
          s.ok === false ? "rgba(224,90,90,0.4)" : s.ok === true ? "rgba(91,214,138,0.35)" : "rgba(124,156,255,0.45)";
        ctx.fill();
        ctx.setLineDash([4, 3]);
        ctx.strokeStyle = s.ok === false ? "#e05a5a" : s.ok === true ? "#5bd68a" : COLORS.select;
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(sx + Math.cos(sf) * sr, sy + Math.sin(sf) * sr);
        ctx.stroke();
        ctx.fillStyle = "#e7ebf2";
        ctx.font = "600 10px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(String(i + 1), sx, sy);
        ctx.restore();
      });
    }

    // Terrain draw tool: the in-progress hand-drawn polygon (committed vertices
    // form the shape; a dashed rubber-band trails the cursor to the next point).
    if (draw) {
      const pts = draw.poly.map(([x, y]) => worldToScreen(t, x, y));
      const style = terrainStyle({
        polygon: [], kind: draw.kind, elevated: draw.elevated, water: draw.water,
        low_wall: draw.lowWall, abrupt: false,
      });
      const col = draw.ok ? COLORS.good : COLORS.bad;
      ctx.save();
      if (pts.length >= 3) {
        polyPath(ctx, pts);
        ctx.globalAlpha = 0.45;
        ctx.fillStyle = draw.ok ? style.fill : "rgba(224,90,90,0.4)";
        ctx.fill();
        ctx.globalAlpha = 1;
        polyPath(ctx, pts);
        ctx.strokeStyle = col;
        ctx.lineWidth = 2;
        ctx.stroke();
      } else if (pts.length === 2) {
        ctx.beginPath();
        ctx.moveTo(pts[0][0], pts[0][1]);
        ctx.lineTo(pts[1][0], pts[1][1]);
        ctx.strokeStyle = col;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      if (pts.length >= 1 && draw.cursor) {
        const [cx, cy] = worldToScreen(t, draw.cursor[0], draw.cursor[1]);
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(pts[pts.length - 1][0], pts[pts.length - 1][1]);
        ctx.lineTo(cx, cy);
        if (pts.length >= 2) ctx.lineTo(pts[0][0], pts[0][1]); // hint the closing edge
        ctx.strokeStyle = "rgba(231,235,242,0.5)";
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.setLineDash([]);
      }
      pts.forEach(([x, y], i) => {
        ctx.beginPath();
        ctx.arc(x, y, i === 0 ? 6 : 4, 0, Math.PI * 2);
        ctx.fillStyle = i === 0 ? COLORS.warn : "#e7ebf2";
        ctx.fill();
        ctx.strokeStyle = "rgba(0,0,0,0.5)";
        ctx.lineWidth = 1;
        ctx.stroke();
      });
      ctx.restore();
    }

    // Terrain placement ghost, drawn last so it sits above everything.
    if (placingGhost) {
      drawTerrainPiece(
        ctx, t,
        {
          polygon: placingGhost.polygon,
          access_points: placingGhost.accessPoints,
          kind: placingGhost.kind,
          elevated: placingGhost.elevated,
          water: placingGhost.water,
          low_wall: placingGhost.lowWall,
          abrupt: placingGhost.abrupt,
        },
        { ok: placingGhost.ok },
      );
    }
  }, [view, size, selectedUid, hoverUid, activeUid, armedTargets, armedMembers, moveGhost, pendingMove, placementMode, placingGhost, spin, deployBand, draw, staged, dimUids, reachOverride, rigid, marquee]);

  // Combat-effect overlay: an independent rAF that draws fx on a top canvas,
  // reusing the transform the base render computed. Keyed on fxSeq.
  useEffect(() => {
    const canvas = fxCanvasRef.current;
    if (!canvas || size.w === 0 || size.h === 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(size.w * dpr);
    canvas.height = Math.round(size.h * dpr);
    canvas.style.width = `${size.w}px`;
    canvas.style.height = `${size.h}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.w, size.h);
    if (fx.length === 0) return;
    const start = performance.now();
    let raf = 0;
    const tick = () => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size.w, size.h);
      const t = transformRef.current;
      const now = performance.now();
      let active = false;
      if (t) {
        for (const f of fx) {
          const p = (now - start) / f.dur;
          if (p >= 1) continue;
          active = true;
          drawFx(ctx, t, f, p);
        }
      }
      if (active) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fxSeq, size.w, size.h]);

  function hitTest(clientX: number, clientY: number): number | null {
    const canvas = canvasRef.current;
    const t = transformRef.current;
    if (!canvas || !t) return null;
    const rect = canvas.getBoundingClientRect();
    const [wx, wy] = screenToWorld(t, clientX - rect.left, clientY - rect.top);
    let best: { uid: number; d: number } | null = null;
    for (const f of view.figures) {
      if (f.eliminated) continue;
      const d = Math.hypot(wx - f.pos[0], wy - f.pos[1]);
      if (d <= f.base_radius && (best === null || d < best.d)) best = { uid: f.uid, d };
    }
    return best?.uid ?? null;
  }

  function worldPoint(clientX: number, clientY: number): [number, number] {
    const canvas = canvasRef.current!;
    const t = transformRef.current!;
    const rect = canvas.getBoundingClientRect();
    const [wx, wy] = screenToWorld(t, clientX - rect.left, clientY - rect.top);
    const { width, height } = view.meta.board;
    return [Math.max(0, Math.min(width, wx)), Math.max(0, Math.min(height, wy))];
  }

  function clampedWorld(clientX: number, clientY: number): [number, number] {
    const canvas = canvasRef.current!;
    const t = transformRef.current!;
    const rect = canvas.getBoundingClientRect();
    const [wx, wy] = screenToWorld(t, clientX - rect.left, clientY - rect.top);
    const uidForR = dragRef.current?.uid ?? activeUid;
    const r = view.figures.find((f) => f.uid === uidForR)?.base_radius ?? 0.55;
    const { width, height } = view.meta.board;
    return [
      Math.max(r, Math.min(width - r, wx)),
      Math.max(r, Math.min(height - r, wy)),
    ];
  }

  // The figure whose facing is currently being edited (a placed move, or a free
  // spin) with its aim point + a setter — unifies the two draggable-handle flows.
  function facePoint(): { pos: [number, number]; radius: number; facing: number; set: (f: number) => void } | null {
    if (pendingMove) {
      const a = view.figures.find((f) => f.uid === activeUid);
      if (a) return { pos: pendingMove.dest, radius: a.base_radius, facing: pendingMove.facing, set: onFaceDrag };
    }
    if (spin) {
      const s = view.figures.find((f) => f.uid === spin.uid);
      return { pos: spin.pos, radius: s?.base_radius ?? 0.55, facing: spin.facing, set: onSpinFace ?? (() => {}) };
    }
    if (rigid?.pending) {
      // Pivot handle: rotates the whole formation about its centroid.
      return { pos: rigid.pivot, radius: 0.7, facing: rigid.theta, set: onRigidPivot ?? (() => {}) };
    }
    return null;
  }

  function nearHandle(clientX: number, clientY: number): boolean {
    const t = transformRef.current;
    const canvas = canvasRef.current;
    const fp = facePoint();
    if (!t || !canvas || !fp) return false;
    const [gx, gy] = worldToScreen(t, fp.pos[0], fp.pos[1]);
    const hr = Math.max(6, fp.radius * t.scale) * 2.4;
    const hx = gx + Math.cos(-fp.facing) * hr; // matches the drawn handle
    const hy = gy + Math.sin(-fp.facing) * hr;
    const rect = canvas.getBoundingClientRect();
    return Math.hypot(clientX - rect.left - hx, clientY - rect.top - hy) <= 13;
  }

  function facingFromCursor(clientX: number, clientY: number): number {
    const t = transformRef.current!;
    const canvas = canvasRef.current!;
    const fp = facePoint()!;
    const rect = canvas.getBoundingClientRect();
    const [wx, wy] = screenToWorld(t, clientX - rect.left, clientY - rect.top);
    return Math.atan2(wy - fp.pos[1], wx - fp.pos[0]);
  }

  function onPointerDown(e: RPE) {
    if (draw) {
      if (e.button === 0 && onDrawPoint) onDrawPoint(worldPoint(e.clientX, e.clientY));
      return;
    }
    if (placementMode) {
      if (e.button === 0 && onPlacePointer) onPlacePointer(worldPoint(e.clientX, e.clientY), true);
      return;
    }
    if (pendingMove || spin || rigid?.pending) {
      if (nearHandle(e.clientX, e.clientY)) {
        faceRef.current = true;
        (e.target as Element).setPointerCapture?.(e.pointerId);
      }
      return; // a facing/pivot edit is pending — only the handle is interactive
    }
    // Rigid mode: the displaced GHOSTS are grabbable too — after the block has
    // moved, re-dragging it from where it now appears is the natural gesture.
    if (rigid != null && staged) {
      const w = worldPoint(e.clientX, e.clientY);
      const g = staged.find(
        (s) => s.uid != null && Math.hypot(w[0] - s.dest[0], w[1] - s.dest[1]) <= s.radius + 0.1,
      );
      if (g) {
        dragRef.current = { moved: false, uid: g.uid };
        (e.target as Element).setPointerCapture?.(e.pointerId);
        return;
      }
    }
    const hit = hitTest(e.clientX, e.clientY);
    if (hit != null && (hit === activeUid || (rigid != null && rigid.uids.includes(hit)))) {
      dragRef.current = { moved: false, uid: hit };
      (e.target as Element).setPointerCapture?.(e.pointerId);
    } else if (hit == null && e.button === 0 && onMarquee) {
      // Drag on empty felt = marquee selection box (StarCraft-style).
      const w = worldPoint(e.clientX, e.clientY);
      marqueeRef.current = true;
      setMarquee({ a: w, b: w });
      (e.target as Element).setPointerCapture?.(e.pointerId);
    }
  }

  function onPointerMove(e: RPE) {
    if (draw) {
      if (onDrawMove) onDrawMove(worldPoint(e.clientX, e.clientY));
      return;
    }
    if (placementMode) {
      if (onPlacePointer) onPlacePointer(worldPoint(e.clientX, e.clientY), false);
      return;
    }
    if (faceRef.current) {
      facePoint()?.set(facingFromCursor(e.clientX, e.clientY));
      return;
    }
    if (marqueeRef.current) {
      const w = worldPoint(e.clientX, e.clientY);
      setMarquee((m) => (m ? { a: m.a, b: w } : m));
      return;
    }
    if (pendingMove || spin || rigid?.pending) return;
    if (dragRef.current) {
      dragRef.current.moved = true;
      onMoveDrag(clampedWorld(e.clientX, e.clientY), dragRef.current.uid);
    } else {
      setHoverUid(hitTest(e.clientX, e.clientY));
    }
  }

  function onWheel(e: RWE) {
    if (placementMode && onPlaceRotate) onPlaceRotate(e.deltaY > 0 ? Math.PI / 12 : -Math.PI / 12);
  }

  function onPointerUp(e: RPE) {
    if (draw) return;
    if (placementMode) return;
    if (faceRef.current) {
      faceRef.current = false;
      return;
    }
    if (marqueeRef.current) {
      marqueeRef.current = false;
      const m = marquee;
      setMarquee(null);
      if (m && Math.hypot(m.b[0] - m.a[0], m.b[1] - m.a[1]) > 0.4) {
        onMarquee?.(m.a, m.b);
      } else {
        onSelect(null); // a plain click on empty felt deselects
      }
      return;
    }
    if (pendingMove || spin || rigid?.pending) return;
    if (dragRef.current) {
      const moved = dragRef.current.moved;
      const uid = dragRef.current.uid;
      if (moved) {
        onMoveDrop(clampedWorld(e.clientX, e.clientY), uid);
      } else {
        onMoveCancel();
        // Rigid mode: the ghost-grab runs before hitTest, so a plain CLICK on a
        // figure under/near a ghost would otherwise die here — treat it as the
        // selection it was meant to be (inspection still works mid-rigid).
        if (rigid) onSelect(hitTest(e.clientX, e.clientY), e.shiftKey);
      }
      dragRef.current = null;
      return;
    }
    onSelect(hitTest(e.clientX, e.clientY), e.shiftKey);
  }

  return (
    <div className="board-wrap" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onWheel={onWheel}
        onContextMenu={(e) => {
          if (placementMode || draw) e.preventDefault();
          if (draw && onDrawUndo) onDrawUndo(); // right-click removes the last point
        }}
        onPointerLeave={() => {
          if (placementMode || draw) {
            onDrawLeave?.(); // drop the rubber-band cursor when the pointer exits
            return;
          }
          if (marqueeRef.current) {
            marqueeRef.current = false;
            setMarquee(null);
            return;
          }
          if (faceRef.current) {
            faceRef.current = false;
          } else if (dragRef.current) {
            dragRef.current = null;
            onMoveCancel();
          } else {
            setHoverUid(null);
          }
        }}
      />
      <canvas ref={fxCanvasRef} className="fx-canvas" />
    </div>
  );
}
