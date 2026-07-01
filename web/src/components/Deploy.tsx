import { useCallback, useMemo, useState } from "react";
import { deployFigure, finishDeploy, type FigureView, type GameView } from "../api";
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

  const W = view.meta.board.width;

  const activeUid = useMemo(
    () =>
      selectedUid != null &&
      view.figures.some((f) => f.uid === selectedUid && f.owner === "human" && !f.eliminated)
        ? selectedUid
        : null,
    [selectedUid, view],
  );

  // Clamp a drop point into the human's starting band; report overlap for red/green.
  const constrain = useCallback(
    (fig: FigureView, dest: [number, number]): Ghost => {
      const r = fig.base_radius;
      const x = Math.max(r, Math.min(W - r, dest[0]));
      const y = Math.max(r, Math.min(BAND - r, dest[1]));
      const overlaps = view.figures.some(
        (o) =>
          !o.eliminated &&
          o.uid !== fig.uid &&
          Math.hypot(x - o.pos[0], y - o.pos[1]) < r + o.base_radius - 1e-3,
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
      }
    } catch (err) {
      setMsg(String(err));
    } finally {
      setBusy(false);
    }
  }, [pending, activeUid, busy]);

  const start = useCallback(async () => {
    if (busy) return;
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
  }, [busy, onDone]);

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
          <button className="btn primary" type="button" onClick={start} disabled={busy}>
            Start battle →
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
