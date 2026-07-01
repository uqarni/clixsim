import type { FigureView } from "../api";
import FigCard from "./FigCard";

interface Props {
  figures: FigureView[];
  selectedUid: number | null;
  onSelect: (uid: number) => void;
  reasoning?: string;
}

// Zone D: the LLM's living figures plus a placeholder reasoning stream.
export default function OpponentPanel({
  figures,
  selectedUid,
  onSelect,
  reasoning,
}: Props) {
  const theirs = figures.filter((f) => f.owner === "llm" && !f.eliminated);
  return (
    <div className="zone">
      <div className="zone-head">
        <span>Opponent</span>
        <span>{theirs.length}</span>
      </div>
      <div className="zone-body">
        {theirs.length === 0 && <div className="empty">No living figures.</div>}
        {theirs.map((f) => (
          <FigCard
            key={f.uid}
            fig={f}
            selected={f.uid === selectedUid}
            onSelect={onSelect}
          />
        ))}
        <div className="section-label">Reasoning stream</div>
        <div className="reasoning">
          {reasoning ?? "Awaiting the opponent's turn. Reasoning will stream here."}
        </div>
      </div>
    </div>
  );
}
