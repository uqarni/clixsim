import { useEffect, useRef, useState } from "react";
import {
  newGameStreamUrl,
  type ConstructEvent,
  type ConstructFigure,
  type GameView,
} from "../api";
import type { GameConfig } from "./NewGame";

interface Pick {
  figure: ConstructFigure;
  reasoning: string;
  used_llm: boolean;
}

export default function Construction({
  config,
  onReady,
  onCancel,
}: {
  config: GameConfig;
  onReady: (v: GameView) => void;
  onCancel: () => void;
}) {
  const [budget, setBudget] = useState(config.points);
  const [humanArmy, setHumanArmy] = useState<ConstructFigure[]>([]);
  const [humanPts, setHumanPts] = useState(0);
  const [picks, setPicks] = useState<Pick[]>([]);
  const [llmPts, setLlmPts] = useState(0);
  const [pools, setPools] = useState<{ human?: ConstructFigure[]; llm?: ConstructFigure[] }>({});
  const [status, setStatus] = useState<"streaming" | "ready" | "error">("streaming");
  const [error, setError] = useState<string>("");
  const readyView = useRef<GameView | null>(null);

  useEffect(() => {
    const url = newGameStreamUrl(config.mode, config.points, config.opponent, config.seed);
    const es = new EventSource(url);
    es.onmessage = (m) => {
      const e = JSON.parse(m.data) as ConstructEvent;
      switch (e.type) {
        case "start":
          setBudget(e.budget);
          break;
        case "pool":
          setPools((p) => ({ ...p, [e.side]: e.pool }));
          break;
        case "human_army":
          setHumanArmy(e.army);
          setHumanPts(e.points);
          break;
        case "llm_pick":
          setPicks((ps) => [...ps, { figure: e.figure, reasoning: e.reasoning, used_llm: e.used_llm }]);
          setLlmPts(e.points);
          break;
        case "ready":
          readyView.current = e.view;
          setStatus("ready");
          es.close();
          break;
        case "error":
          setError(e.message);
          setStatus("error");
          es.close();
          break;
      }
    };
    es.onerror = () => {
      if (readyView.current == null) {
        setError("Connection to the engine was lost.");
        setStatus("error");
      }
      es.close();
    };
    return () => es.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pct = Math.min(100, Math.round((llmPts / Math.max(1, budget)) * 100));

  return (
    <div className="menu">
      <div className="menu-card wide">
        <div className="construct-head">
          <h1 className="menu-title">Building armies</h1>
          <span className="menu-sub">
            {config.mode === "sealed" ? "Sealed · 200 pts" : `Preconstructed · ${budget} pts`} ·{" "}
            {config.opponent === "llm" ? "Sonnet 5" : "Heuristic"} opponent
          </span>
        </div>

        <div className="construct-grid">
          <div className="construct-col">
            <div className="menu-section-label">
              <span className="dot human" /> Your army — {humanPts} pts
            </div>
            {config.mode === "sealed" && pools.human && (
              <div className="pool-note">Pool: {pools.human.length} figures pulled</div>
            )}
            {humanArmy.length === 0 ? (
              <div className="empty">Assembling…</div>
            ) : (
              humanArmy.map((f, i) => (
                <div className="draft-row" key={i}>
                  <span className="draft-name">{f.name}</span>
                  <span className="draft-pts">{f.points}</span>
                </div>
              ))
            )}
          </div>

          <div className="construct-col">
            <div className="menu-section-label">
              <span className="dot llm" /> {config.opponent === "llm" ? "Sonnet" : "Opponent"} drafting — {llmPts}/{budget} pts
            </div>
            <div className="draft-bar">
              <div className="draft-fill" style={{ width: `${pct}%` }} />
            </div>
            {config.mode === "sealed" && pools.llm && (
              <div className="pool-note">Pool: {pools.llm.length} figures pulled</div>
            )}
            {picks.length === 0 && status === "streaming" && <div className="empty">Thinking…</div>}
            {picks.map((p, i) => (
              <div className="draft-pick" key={i}>
                <div className="draft-row">
                  <span className="draft-name">
                    {p.figure.name} <span className="fig-sub">· {p.figure.faction}</span>
                  </span>
                  <span className="draft-pts">{p.figure.points}</span>
                </div>
                <div className="draft-reason">
                  {!p.used_llm && <span className="badge">auto</span>} {p.reasoning}
                </div>
              </div>
            ))}
          </div>
        </div>

        {status === "error" && (
          <div className="construct-error">Construction failed: {error}</div>
        )}

        <div className="construct-actions">
          {status === "ready" ? (
            <button className="btn primary" onClick={() => readyView.current && onReady(readyView.current)} type="button">
              Start battle →
            </button>
          ) : status === "error" ? (
            <button className="btn" onClick={onCancel} type="button">
              Back
            </button>
          ) : (
            <button className="btn" onClick={onCancel} type="button">
              Cancel
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
