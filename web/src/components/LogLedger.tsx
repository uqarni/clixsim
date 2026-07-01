import type { FigureView, GameEvent, GameView } from "../api";

interface Props {
  view: GameView;
  events: GameEvent[];
}

function eventLine(e: GameEvent): string {
  if (typeof e.summary === "string") return e.summary;
  if (typeof e.message === "string") return e.message;
  const rest = Object.entries(e)
    .filter(([k]) => k !== "type")
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(" ");
  return rest ? `${e.type} ${rest}` : e.type;
}

// Zone E: scrolling event log (upper) + VP / casualty ledger (lower).
export default function LogLedger({ view, events }: Props) {
  const eliminated: FigureView[] = view.figures.filter((f) => f.eliminated);
  const vp = view.meta.victory_points;

  return (
    <div className="zone">
      <div className="zone-head">
        <span>Log + ledger</span>
      </div>
      <div className="zone-body no-pad">
        <div className="split">
          <div className="upper" style={{ padding: 8 }}>
            <div className="log-list">
              {events.length === 0 && (
                <div className="empty">No events yet.</div>
              )}
              {events.map((e, i) => (
                <div className="log-line" key={i}>
                  <span className="lt">{String(i + 1).padStart(2, "0")}</span>
                  {eventLine(e)}
                </div>
              ))}
            </div>
          </div>

          <div className="lower" style={{ padding: 8 }}>
            <div className="ledger">
              <div className="ledger-vp">
                <div className="side">
                  <div className="fig-sub">
                    <span className="dot human" /> Human VP
                  </div>
                  <div className="n">{vp.human}</div>
                </div>
                <div className="side">
                  <div className="fig-sub">
                    <span className="dot llm" /> LLM VP
                  </div>
                  <div className="n">{vp.llm}</div>
                </div>
              </div>

              <div className="section-label">Eliminated</div>
              {eliminated.length === 0 ? (
                <div className="empty">No casualties.</div>
              ) : (
                eliminated.map((f) => (
                  <div className="casualty" key={f.uid}>
                    <span className={`dot ${f.owner}`} />
                    <span>{f.name}</span>
                    <span className="fig-sub">· {f.points} pts</span>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
