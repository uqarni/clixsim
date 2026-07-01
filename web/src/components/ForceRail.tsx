import type { FigureView } from "../api";
import FigCard from "./FigCard";

interface Props {
  figures: FigureView[];
  selectedUid: number | null;
  onSelect: (uid: number) => void;
}

// Zone A: the human player's living figures as compact, selectable cards.
export default function ForceRail({ figures, selectedUid, onSelect }: Props) {
  const mine = figures.filter((f) => f.owner === "human" && !f.eliminated);
  return (
    <div className="zone">
      <div className="zone-head">
        <span>Force rail</span>
        <span>{mine.length}</span>
      </div>
      <div className="zone-body">
        {mine.length === 0 && <div className="empty">No living figures.</div>}
        {mine.map((f) => (
          <FigCard
            key={f.uid}
            fig={f}
            selected={f.uid === selectedUid}
            onSelect={onSelect}
          />
        ))}
      </div>
    </div>
  );
}
