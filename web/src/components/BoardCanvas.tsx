import { useEffect, useRef, useState, type PointerEvent as RPE } from "react";
import type { FigureView, GameView } from "../api";

interface MoveGhost {
  dest: [number, number];
  facing: number; // radians
  ok: boolean;
  breakAway: boolean;
}

interface Props {
  view: GameView;
  selectedUid: number | null;
  onSelect: (uid: number | null) => void;
  // The selected friendly figure that may be dragged to move (null if none).
  activeUid: number | null;
  // Figures currently targeted by an armed action — highlighted as reticles.
  armedTargets: number[];
  moveGhost: MoveGhost | null;
  onMoveDrag: (dest: [number, number]) => void;
  onMoveDrop: (dest: [number, number]) => void;
  onMoveCancel: () => void;
}

interface Transform {
  scale: number; // css px per inch
  offX: number;
  offY: number;
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
  return { scale, offX: (cssW - drawnW) / 2, offY: (cssH - drawnH) / 2 };
}

function worldToScreen(t: Transform, x: number, y: number): [number, number] {
  return [t.offX + x * t.scale, t.offY + y * t.scale];
}
function screenToWorld(t: Transform, sx: number, sy: number): [number, number] {
  return [(sx - t.offX) / t.scale, (sy - t.offY) / t.scale];
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

  // Front-arc wedge.
  const wedgeR = r * 2.4;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, wedgeR, toRad(f.facing_deg - f.arc_deg), toRad(f.facing_deg + f.arc_deg), false);
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
  ctx.lineTo(cx + Math.cos(toRad(f.facing_deg)) * r, cy + Math.sin(toRad(f.facing_deg)) * r);
  ctx.strokeStyle = "rgba(255,255,255,0.85)";
  ctx.lineWidth = 1.5;
  ctx.stroke();

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

export default function BoardCanvas({
  view,
  selectedUid,
  onSelect,
  activeUid,
  armedTargets,
  moveGhost,
  onMoveDrag,
  onMoveDrop,
  onMoveCancel,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [hoverUid, setHoverUid] = useState<number | null>(null);
  const transformRef = useRef<Transform | null>(null);
  const dragRef = useRef<{ moved: boolean } | null>(null);

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
    const [px0, py0] = worldToScreen(t, 0, 0);
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
      dashedRing(ctx, cx, cy, active.speed * t.scale, "rgba(91,214,138,0.5)");
    }

    // Line of fire on hover (friendly selected -> hovered enemy).
    if (selected && hoverUid != null && hoverUid !== selected.uid) {
      const hv = live.find((f) => f.uid === hoverUid);
      if (hv && hv.owner !== selected.owner) {
        const [ax, ay] = worldToScreen(t, selected.pos[0], selected.pos[1]);
        const [bx, by] = worldToScreen(t, hv.pos[0], hv.pos[1]);
        ctx.save();
        ctx.setLineDash([2, 4]);
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
        ctx.strokeStyle = "rgba(224,90,90,0.9)";
        ctx.lineWidth = 1.5;
        ctx.stroke();
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
      ctx.lineTo(gx + Math.cos(moveGhost.facing) * gr, gy + Math.sin(moveGhost.facing) * gr);
      ctx.stroke();
      if (moveGhost.breakAway) {
        ctx.fillStyle = COLORS.warn;
        ctx.font = "500 11px system-ui, sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        ctx.fillText("break-away", gx, gy - gr - 3);
      }
      ctx.restore();
    }

    // Armed-action target reticles.
    const armedSet = new Set(armedTargets);
    for (const f of live) {
      drawFigure(ctx, t, f, f.uid === selectedUid, f.uid === hoverUid, false);
      if (armedSet.has(f.uid)) {
        const [cx, cy] = worldToScreen(t, f.pos[0], f.pos[1]);
        const rr = Math.max(6, f.base_radius * t.scale) + 7;
        ctx.save();
        ctx.beginPath();
        ctx.arc(cx, cy, rr, 0, Math.PI * 2);
        ctx.strokeStyle = COLORS.bad;
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.restore();
      }
    }
  }, [view, size, selectedUid, hoverUid, activeUid, armedTargets, moveGhost]);

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

  function clampedWorld(clientX: number, clientY: number): [number, number] {
    const canvas = canvasRef.current!;
    const t = transformRef.current!;
    const rect = canvas.getBoundingClientRect();
    const [wx, wy] = screenToWorld(t, clientX - rect.left, clientY - rect.top);
    const r = view.figures.find((f) => f.uid === activeUid)?.base_radius ?? 0.55;
    const { width, height } = view.meta.board;
    return [
      Math.max(r, Math.min(width - r, wx)),
      Math.max(r, Math.min(height - r, wy)),
    ];
  }

  function onPointerDown(e: RPE) {
    const hit = hitTest(e.clientX, e.clientY);
    if (hit != null && hit === activeUid) {
      dragRef.current = { moved: false };
      (e.target as Element).setPointerCapture?.(e.pointerId);
    }
  }

  function onPointerMove(e: RPE) {
    if (dragRef.current) {
      dragRef.current.moved = true;
      onMoveDrag(clampedWorld(e.clientX, e.clientY));
    } else {
      setHoverUid(hitTest(e.clientX, e.clientY));
    }
  }

  function onPointerUp(e: RPE) {
    if (dragRef.current) {
      const moved = dragRef.current.moved;
      dragRef.current = null;
      if (moved) onMoveDrop(clampedWorld(e.clientX, e.clientY));
      else onMoveCancel();
      return;
    }
    onSelect(hitTest(e.clientX, e.clientY));
  }

  return (
    <div className="board-wrap" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => {
          if (dragRef.current) {
            dragRef.current = null;
            onMoveCancel();
          } else {
            setHoverUid(null);
          }
        }}
      />
    </div>
  );
}
