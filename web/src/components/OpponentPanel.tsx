import type { FigureView } from "../api";
import Chat from "./Chat";
import FigCard from "./FigCard";

interface Thought {
  summary: string;
  reasoning: string;
  fallback: boolean;
}

interface Props {
  figures: FigureView[];
  selectedUid: number | null;
  onSelect: (uid: number) => void;
  thoughts: Thought[];
}

// Zone D: the LLM's forces, its live per-action reasoning, and a chat with it.
export default function OpponentPanel({ figures, selectedUid, onSelect, thoughts }: Props) {
  const theirs = figures.filter((f) => f.owner === "llm" && !f.eliminated);
  return (
    <div className="zone">
      <div className="zone-head">
        <span>Opponent</span>
        <span>{theirs.length}</span>
      </div>
      <div className="zone-body opp-body">
        <div className="opp-forces">
          {theirs.length === 0 && <div className="empty">No living figures.</div>}
          {theirs.map((f) => (
            <FigCard key={f.uid} fig={f} selected={f.uid === selectedUid} onSelect={onSelect} />
          ))}
        </div>

        <div className="section-label">Thinking</div>
        <div className="opp-thoughts">
          {thoughts.length === 0 ? (
            <div className="empty">Its reasoning streams here on its turn.</div>
          ) : (
            thoughts.slice(-8).map((t, i) => (
              <div className="thought" key={i}>
                <div className="thought-move">
                  {t.fallback && <span className="badge">auto</span>} {t.summary}
                </div>
                {t.reasoning && <div className="thought-reason">{t.reasoning}</div>}
              </div>
            ))
          )}
        </div>

        <Chat />
      </div>
    </div>
  );
}
