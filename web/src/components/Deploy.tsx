import { useCallback, useEffect, useMemo, useState } from "react";
import { deployFigure, finishDeploy, type FigureView, type GameView } from "../api";
import { centersAt, figureCenters, snapToContactRing } from "../terrainGeom";
import BoardCanvas from "./BoardCanvas";

interface Props {
  initialView: GameView;
  onDone: (v: GameView) => void;
  onCancel: () => void;
}

interface Ghost {
  dest: [number, number];
  facing: number;
  ok: boolean;
  breakAway: boolean;
  reason?: string;
}

const BAND = 3.0; // human starting band depth (P3-R5)

export default function Deploy({ initialView, onDone, onCancel }: Props) {
  const [view, setView] = useState<GameView>(initialView);
  const [selectedUid, setSelectedUid] = useState<number | null>(null);
  const [moveGhost, setMoveGhost] = useState<Ghost | null>(null);
  const [pending, setPending] = useState<{ dest: [number, number]; facing: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  // Guard against skipping deployment by accident: the screen can appear the
  // instant the last terrain piece lands, so the start button stays disabled
  // briefly (click-through protection), and starting without having moved any
  // figure requires a second, explicit click.
  const [armedStart, setArmedStart] = useState(false);
  const [movedAny, setMovedAny] = useState(false);
  const [confirmSkip, setConfirmSkip] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setArmedStart(true), 1200);
    return () => clearTimeout(t);
  }, []);
  useEffect(() => {
    if (!confirmSkip) return;
    const t = setTimeout(() => setConfirmSkip(false), 4000);
    return () => clearTimeout(t);
  }, [confirmSkip]);

  const W = view.meta.board.width;

  const activeUid = useMemo(
    () =>
      selectedUid != null &&
      view.figures.some((f) => f.uid === selectedUid && f.owner === "human" && !f.eliminated)
        ? selectedUid
        : null,
    [selectedUid, view],
  );

  // Clamp a drop point into the human's starting band, SNAP it onto exact base
  // contact with a nearby figure (formations need legal touching, and the engine's
  // contact tolerance is far tighter than the eye), then report overlap. The
  // WHOLE capsule of a mounted figure must sit in the band (P5-R11) — every
  // check below runs at the CURRENT facing, and the rear swings when the
  // facing handle moves (the pending re-validation catches that).
  const constrain = useCallback(
    (fig: FigureView, dest: [number, number], facingIn?: number): Ghost => {
      const r = fig.base_radius;
      const mounted = !!fig.mounted;
      // Mounted figures default to facing the enemy (+y): the 3" band only
      // fits the capsule for roughly forward facings.
      const facing = facingIn ?? (mounted ? Math.PI / 2 : (fig.facing_deg * Math.PI) / 180);
      // Both circles inside board x band re-expressed as a clamp on the front
      // dot: rear = front − 2r·(cos f, sin f).
      const dx = mounted ? 2 * r * Math.cos(facing) : 0;
      const dy = mounted ? 2 * r * Math.sin(facing) : 0;
      const xLo = r + Math.max(0, dx);
      const xHi = W - r + Math.min(0, dx);
      const yLo = r + Math.max(0, dy);
      const yHi = BAND - r + Math.min(0, dy);
      let reason: string | undefined;
      let x = dest[0];
      let y = dest[1];
      if (xLo > xHi + 1e-9 || yLo > yHi + 1e-9) {
        // No legal front-dot strip at this facing (the rear can't fit in the
        // band) — clamp loosely and go red rather than teleporting the ghost.
        reason = "the rear circle can't fit in the band at this facing";
        x = Math.max(r, Math.min(W - r, x));
        y = Math.max(r, Math.min(BAND - r, y));
      } else {
        x = Math.max(xLo, Math.min(xHi, x));
        y = Math.max(yLo, Math.min(yHi, y));
      }
      // Mounted neighbours contribute BOTH circles with distinct identity
      // (same waist-overlap trap as the battle snap).
      const targets: { pos: [number, number]; radius: number; uid: number; key: string }[] = [];
      for (const o of view.figures) {
        if (o.eliminated || o.uid === fig.uid) continue;
        figureCenters(o).forEach((c, i) =>
          targets.push({ pos: c as [number, number], radius: o.base_radius, uid: o.uid, key: `${o.uid}:${i}` }),
        );
      }
      const overlapAt = (p: [number, number]): boolean => {
        const cs = centersAt(p, facing, r, mounted);
        return targets.some((o) =>
          cs.some((c) => Math.hypot(c[0] - o.pos[0], c[1] - o.pos[1]) < r + o.radius - 0.02),
        );
      };
      const capsuleFits = (p: [number, number]): boolean =>
        centersAt(p, facing, r, mounted).every(
          ([px, py]) =>
            px >= r - 1e-9 && px <= W - r + 1e-9 && py >= r - 1e-9 && py <= BAND - r + 1e-9,
        );
      // Candidates are ranked (two-contact pocket first when the cursor is near
      // the notch); take the first whose FULL capsule fits the band and
      // overlaps nothing — a band-check on the front centre alone would offer
      // spots the engine then rejects.
      const snapped = reason
        ? undefined
        : snapToContactRing(r, [x, y], targets).find(
            (c) => capsuleFits(c.point) && !overlapAt(c.point),
          );
      if (snapped) [x, y] = snapped.point;
      const overlaps = overlapAt([x, y]);
      if (!reason && overlaps) reason = "overlaps another figure";
      return { dest: [x, y], facing, ok: !reason, breakAway: false, reason };
    },
    [view, W],
  );

  // The facing handle swings a mounted rear OUT of the band/board or into a
  // neighbour AFTER the drop was green — re-validate the pending placement
  // whenever it changes so Confirm greys out with the reason.
  const pendingBad = useMemo(() => {
    if (!pending) return null;
    const fig = view.figures.find((f) => f.uid === activeUid);
    if (!fig) return null;
    const r = fig.base_radius;
    const cs = centersAt(pending.dest, pending.facing, r, !!fig.mounted);
    for (const [px, py] of cs) {
      if (px < r - 1e-9 || px > W - r + 1e-9) return "off the board";
      if (py < r - 1e-9 || py > BAND - r + 1e-9)
        return "the whole base must sit inside your starting band";
    }
    for (const o of view.figures) {
      if (o.eliminated || o.uid === fig.uid) continue;
      const oCs = figureCenters(o);
      const lim = r + o.base_radius - 0.02;
      if (cs.some((c) => oCs.some((oc) => Math.hypot(c[0] - oc[0], c[1] - oc[1]) < lim))) {
        return `overlaps ${o.short_name}'s base`;
      }
    }
    return null;
  }, [pending, view, activeUid, W]);

  const onMoveDrag = useCallback(
    (dest: [number, number]) => {
      const fig = view.figures.find((f) => f.uid === activeUid);
      if (fig) setMoveGhost(constrain(fig, dest));
    },
    [view, activeUid, constrain],
  );

  const onMoveDrop = useCallback(
    (dest: [number, number]) => {
      const fig = view.figures.find((f) => f.uid === activeUid);
      setMoveGhost(null);
      if (!fig) return;
      const g = constrain(fig, dest);
      if (!g.ok) {
        setMsg(g.reason ?? "That spot overlaps another figure.");
        return;
      }
      setMsg("");
      setPending({ dest: g.dest, facing: g.facing });
    },
    [view, activeUid, constrain],
  );

  const confirm = useCallback(async () => {
    if (!pending || activeUid == null || busy || pendingBad) return;
    setBusy(true);
    try {
      const res = await deployFigure(activeUid, pending.dest, pending.facing);
      if (!res.ok) {
        setMsg(res.detail || res.reason || "Illegal placement.");
      } else {
        setView(res.view);
        setPending(null);
        setMovedAny(true);
      }
    } catch (err) {
      setMsg(String(err));
    } finally {
      setBusy(false);
    }
  }, [pending, activeUid, busy, pendingBad]);

  const start = useCallback(async () => {
    if (busy || !armedStart) return;
    if (!movedAny && !confirmSkip) {
      // First click without any repositioning: ask, don't start.
      setConfirmSkip(true);
      setMsg("You haven't repositioned anyone — click again to start as-is.");
      return;
    }
    setBusy(true);
    try {
      const res = await finishDeploy();
      if (res.ok) onDone(res.view);
      else setMsg(res.reason || "Could not start the battle.");
    } catch (err) {
      setMsg(String(err));
    } finally {
      setBusy(false);
    }
  }, [busy, armedStart, movedAny, confirmSkip, onDone]);

  const selFig = activeUid != null ? view.figures.find((f) => f.uid === activeUid) ?? null : null;

  return (
    <div className="app">
      <header className="hud">
        <div className="hud-left">
          <strong>Deploy your army</strong>
          <span className="fig-sub">
            Arrange your figures anywhere in your (green) starting area — drag to move, then aim their facing.
          </span>
        </div>
        <div className="hud-right">
          <button className="btn primary" type="button" onClick={start} disabled={busy || !armedStart}>
            {confirmSkip ? "Start without repositioning?" : "Done deploying — start battle →"}
          </button>
          <button className="btn" type="button" onClick={onCancel}>
            Quit
          </button>
        </div>
      </header>

      <div className="zones placement-zones">
        <div className="zone" style={{ minWidth: 240, maxWidth: 280 }}>
          <div className="zone-head">
            <span>Deployment</span>
          </div>
          <div className="zone-body">
            <p className="fig-sub">
              Click one of your figures to select it, then drag it within the green band. Drop it, drag the
              handle to set its facing, and confirm. Figures may touch to pre-form a formation.
            </p>
            {selFig && (
              <div className="deploy-sel">
                Selected: <strong>{selFig.short_name}</strong>
              </div>
            )}
            {pending && (
              <div className="armed">
                <div className="armed-title">
                  Place at ({pending.dest[0].toFixed(1)}, {pending.dest[1].toFixed(1)})
                </div>
                <div className="armed-stats">Drag the handle on the board to aim, then confirm.</div>
                {pendingBad && <div className="group-reason">✕ {pendingBad} — re-aim the handle</div>}
                <div className="armed-btns">
                  <button
                    className="btn primary"
                    onClick={confirm}
                    disabled={busy || !!pendingBad}
                    title={pendingBad ?? ""}
                  >
                    Confirm
                  </button>
                  <button className="btn" onClick={() => setPending(null)} disabled={busy}>
                    Cancel
                  </button>
                </div>
              </div>
            )}
            {msg && <p className="terrain-msg bad">{msg}</p>}
          </div>
        </div>

        <div className="zone board-zone">
          <div className="zone-head">
            <span>Deploy</span>
            <span className="fig-sub">
              {view.meta.board.width} × {view.meta.board.height} in
            </span>
          </div>
          <div className="zone-body no-pad board-host">
            <BoardCanvas
              view={view}
              selectedUid={selectedUid}
              onSelect={setSelectedUid}
              activeUid={activeUid}
              armedTargets={[]}
              armedMembers={[]}
              moveGhost={moveGhost}
              onMoveDrag={onMoveDrag}
              onMoveDrop={onMoveDrop}
              onMoveCancel={() => setMoveGhost(null)}
              pendingMove={pending}
              onFaceDrag={(facing) => setPending((pm) => (pm ? { ...pm, facing } : pm))}
              fx={[]}
              fxSeq={0}
              deployBand
            />
          </div>
        </div>
      </div>
    </div>
  );
}
