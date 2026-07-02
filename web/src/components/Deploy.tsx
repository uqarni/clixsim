import { useCallback, useEffect, useMemo, useState } from "react";
import { deployFigure, finishDeploy, type FigureView, type GameView } from "../api";
import { snapToContactRing } from "../terrainGeom";
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
  // contact tolerance is far tighter than the eye), then report overlap.
  const constrain = useCallback(
    (fig: FigureView, dest: [number, number]): Ghost => {
      const r = fig.base_radius;
      let x = Math.max(r, Math.min(W - r, dest[0]));
      let y = Math.max(r, Math.min(BAND - r, dest[1]));
      const targets = view.figures
        .filter((o) => !o.eliminated && o.uid !== fig.uid)
        .map((o) => ({ pos: o.pos, radius: o.base_radius, uid: o.uid }));
      const snapped = snapToContactRing(r, [x, y], targets);
      // Keep the snap only if it stays inside the deploy band.
      if (
        snapped &&
        snapped.point[0] >= r && snapped.point[0] <= W - r &&
        snapped.point[1] >= r && snapped.point[1] <= BAND - r
      ) {
        [x, y] = snapped.point;
      }
      const overlaps = targets.some(
        (o) => Math.hypot(x - o.pos[0], y - o.pos[1]) < r + o.radius - 0.02,
      );
      const facing = (fig.facing_deg * Math.PI) / 180;
      return { dest: [x, y], facing, ok: !overlaps, breakAway: false };
    },
    [view, W],
  );

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
        setMsg("That spot overlaps another figure.");
        return;
      }
      setMsg("");
      setPending({ dest: g.dest, facing: g.facing });
    },
    [view, activeUid, constrain],
  );

  const confirm = useCallback(async () => {
    if (!pending || activeUid == null || busy) return;
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
  }, [pending, activeUid, busy]);

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
                <div className="armed-btns">
                  <button className="btn primary" onClick={confirm} disabled={busy}>
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
