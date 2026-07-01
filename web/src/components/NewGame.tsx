import { useState } from "react";

export interface GameConfig {
  mode: "preconstructed" | "sealed";
  points: number;
  opponent: "llm" | "heuristic";
  seed: number;
}

const CAPS = [100, 200, 300, 400, 500];

export default function NewGame({ onStart, onResume }: { onStart: (c: GameConfig) => void; onResume: () => void }) {
  const [mode, setMode] = useState<"preconstructed" | "sealed">("preconstructed");
  const [points, setPoints] = useState(200);
  const [opponent, setOpponent] = useState<"llm" | "heuristic">("llm");

  const start = () =>
    onStart({
      mode,
      points: mode === "sealed" ? 200 : points,
      opponent,
      seed: Math.floor(Math.random() * 1_000_000),
    });

  return (
    <div className="menu">
      <div className="menu-card">
        <h1 className="menu-title">Clix Engine</h1>
        <p className="menu-sub">Mage Knight — you versus an LLM commander.</p>

        <div className="menu-section-label">Game type</div>
        <div className="mode-grid">
          <button
            className={`mode-card${mode === "preconstructed" ? " on" : ""}`}
            onClick={() => setMode("preconstructed")}
            type="button"
          >
            <div className="mode-name">Preconstructed</div>
            <div className="mode-desc">Both sides build from the whole roster up to a points cap.</div>
          </button>
          <button
            className={`mode-card${mode === "sealed" ? " on" : ""}`}
            onClick={() => setMode("sealed")}
            type="button"
          >
            <div className="mode-name">Sealed</div>
            <div className="mode-desc">Open 4 boosters each and build from what you pull. Always 200 pts.</div>
          </button>
        </div>

        {mode === "preconstructed" && (
          <>
            <div className="menu-section-label">Points cap</div>
            <div className="pill-row">
              {CAPS.map((c) => (
                <button
                  key={c}
                  className={`pill${points === c ? " on" : ""}`}
                  onClick={() => setPoints(c)}
                  type="button"
                >
                  {c}
                </button>
              ))}
            </div>
          </>
        )}

        <div className="menu-section-label">Opponent</div>
        <div className="pill-row">
          <button className={`pill${opponent === "llm" ? " on" : ""}`} onClick={() => setOpponent("llm")} type="button">
            Sonnet 5
          </button>
          <button className={`pill${opponent === "heuristic" ? " on" : ""}`} onClick={() => setOpponent("heuristic")} type="button">
            Heuristic (fast)
          </button>
        </div>

        <button className="btn primary menu-start" onClick={start} type="button">
          Build armies →
        </button>
        <button className="btn menu-resume" onClick={onResume} type="button">
          Resume current game
        </button>
      </div>
    </div>
  );
}
