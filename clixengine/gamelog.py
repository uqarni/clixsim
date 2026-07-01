"""Structured game log (A3 / Phase 5).

Every state transition emits an event. The log is sufficient to reconstruct the
game (seed + ordered events) for replay and LLM-authored debrief.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GameLog:
    seed: int
    events: list[dict] = field(default_factory=list)

    def emit(self, type: str, **data) -> dict:
        event = {"seq": len(self.events), "type": type, **data}
        self.events.append(event)
        return event

    def to_dict(self) -> dict:
        return {"seed": self.seed, "events": self.events}

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2))
