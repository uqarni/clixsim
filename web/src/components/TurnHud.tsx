import type { GameView } from "../api";

interface Props {
  view: GameView;
  onEndTurn: () => void;
  onNewGame: () => void;
}

// Top HUD bar: turn, active player, action pips, VP, end-turn.
export default function TurnHud({ view, onEndTurn, onNewGame }: Props) {
  const m = view.meta;
  const spent = Math.max(0, m.actions_per_turn - m.actions_remaining);

  return (
    <div className="hud">
      <span className="hud-title">Clix Engine</span>

      <div className="hud-group">
        Turn <b>{m.turn}</b>
      </div>

      <div className="hud-group">
        Active
        <span className="chip-owner">
          <span className={`dot ${m.active_player}`} />
          <b>{m.active_player === "human" ? "Human" : "LLM"}</b>
        </span>
      </div>

      <div className="hud-group">
        Actions
        <span className="pip-row" title={`${spent} spent / ${m.actions_remaining} remaining`}>
          {Array.from({ length: m.actions_per_turn }).map((_, i) => (
            <span className={`pip${i < spent ? " filled" : ""}`} key={i} />
          ))}
        </span>
        <span className="fig-sub">
          {m.actions_remaining} / {m.actions_per_turn}
        </span>
      </div>

      <div className="hud-spacer" />

      <div className="hud-group">
        VP
        <span className="chip-owner">
          <span className="dot human" /> <b>{m.victory_points.human}</b>
        </span>
        <span className="chip-owner">
          <span className="dot llm" /> <b>{m.victory_points.llm}</b>
        </span>
      </div>

      {m.ended && (
        <div className="hud-group">
          <b>Winner: {m.winner ?? "—"}</b>
        </div>
      )}

      <button className="btn" onClick={onNewGame} type="button">
        New game
      </button>
      <button
        className="btn primary"
        onClick={onEndTurn}
        disabled={m.ended || m.active_player !== "human"}
        type="button"
      >
        End turn
      </button>
    </div>
  );
}
