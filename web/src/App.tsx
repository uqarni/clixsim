import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getState,
  opponentTurn,
  type GameEvent,
  type GameView,
} from "./api";
import BoardCanvas from "./components/BoardCanvas";
import DialInspector from "./components/DialInspector";
import ForceRail from "./components/ForceRail";
import LogLedger from "./components/LogLedger";
import OpponentPanel from "./components/OpponentPanel";
import TurnHud from "./components/TurnHud";

export default function App() {
  const [view, setView] = useState<GameView | null>(null);
  const [selectedUid, setSelectedUid] = useState<number | null>(null);
  const [events, setEvents] = useState<GameEvent[]>([]);

  // Load initial state (mock while USE_MOCK is true).
  useEffect(() => {
    let cancelled = false;
    getState()
      .then((v) => {
        if (cancelled) return;
        setView(v);
        setEvents([{ type: "info", summary: "Game state loaded." }]);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setEvents([
          {
            type: "error",
            summary: `Failed to load state: ${String(err)}`,
          },
        ]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedFig = useMemo(
    () => view?.figures.find((f) => f.uid === selectedUid) ?? null,
    [view, selectedUid],
  );

  const handleEndTurn = useCallback(async () => {
    setEvents((prev) => [
      ...prev,
      { type: "turn", summary: "Turn ended. Opponent is thinking…" },
    ]);
    const { view: v } = await opponentTurn();
    setView(v);
    setEvents((prev) => [
      ...prev,
      { type: "turn", summary: "Opponent turn resolved." },
    ]);
  }, []);

  if (!view) {
    return (
      <div className="app">
        <div className="hud">
          <span className="hud-title">Clix Engine</span>
          <div className="hud-group">Loading…</div>
        </div>
        <div className="zones" />
      </div>
    );
  }

  return (
    <div className="app">
      <TurnHud view={view} onEndTurn={handleEndTurn} />
      <div className="zones">
        <ForceRail
          figures={view.figures}
          selectedUid={selectedUid}
          onSelect={setSelectedUid}
        />
        <DialInspector fig={selectedFig} />
        <div className="zone board-zone">
          <div className="zone-head">
            <span>Board</span>
            <span className="fig-sub">
              {view.meta.board.width} × {view.meta.board.height} in
            </span>
          </div>
          <div className="zone-body no-pad">
            <BoardCanvas
              view={view}
              selectedUid={selectedUid}
              onSelect={setSelectedUid}
            />
          </div>
        </div>
        <OpponentPanel
          figures={view.figures}
          selectedUid={selectedUid}
          onSelect={setSelectedUid}
        />
        <LogLedger view={view} events={events} />
      </div>
    </div>
  );
}
