#!/usr/bin/env python3
"""Ingest the Lancers expansion from mageknight.net's raw dial-stats feed.

Reproducible: reads a cached mkstats.json if given, else downloads the live
feed. Emits stats/lancers.json in the rebellion.json shape, plus the derived
``mounted`` flag (docs/lancers-plan.md §1).

Selection: ExpansionName == "Lancers", Rank in {Weak, Standard, Tough, Unique}
(the 32 "Promo L3..L6" LE figures are the only exclusions) -> 142 core units.

Known feed quirks handled here (plan §1.1):
- Two units (ids 9091, 9110) encode dead clicks as numeric all-zero stats with
  ability id 93 instead of the "Dead" string -> a click is dead when its stats
  read "Dead" OR any slot carries ability 93.
- Multi-target ranges "12 (2 Targets)" / "6 (2 Targets )" -> whitespace-
  tolerant regex; the script FAILS LOUD on any range it cannot parse.
- Mounted identification: the horseshoe symbol was never digitized; the only
  reliable marker is the " On " name pattern (54 mounted / 88 foot, verified
  against the 22 known mounted sculpt groups).

Usage:
    python scripts/ingest_lancers.py [path/to/mkstats.json]
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FEED_URL = "https://www.mageknight.net/wp-content/uploads/mkstats.json"
RANGE_RE = re.compile(r"^(\d+)\s*(?:\(\s*(\d+)\s*Targets?\s*\)?\s*)?$")
KEEP_RANKS = {"Weak", "Standard", "Tough", "Unique"}
DEAD_ABILITY_ID = 93
NO_ABILITY_ID = 85
P = "Model.Dial.Clicks.Click."


def load_feed(argv: list[str]) -> list[dict]:
    if len(argv) > 1:
        raw = json.loads(Path(argv[1]).read_text())
    else:
        print(f"downloading {FEED_URL} ...")
        with urllib.request.urlopen(FEED_URL) as r:
            raw = json.loads(r.read().decode("utf-8"))
    return raw["Models"]


def click_is_dead(c: dict) -> bool:
    if c[P + "Attack"] == "Dead":
        return True
    return any(
        c[P + slot + "AbilityId"] == DEAD_ABILITY_ID
        for slot in ("Speed", "Attack", "Defense", "Damage")
    )


def conv_ability(aid: int, abilities: dict[int, dict]) -> dict | None:
    if aid == NO_ABILITY_ID:
        return None
    a = abilities[aid]
    return {"id": aid, "name": a["name"], "optional": bool(a["optional"])}


def main(argv: list[str]) -> None:
    models = load_feed(argv)
    abilities = {
        a["id"]: a
        for a in json.loads((REPO / "stats" / "special_abilities.json").read_text())[
            "abilities"
        ]
    }

    figures = []
    used_ability_ids: set[int] = set()
    for m in models:
        if m["Model.ExpansionName"] != "Lancers" or m["Model.Rank"] not in KEEP_RANKS:
            continue
        match = RANGE_RE.match(m["Model.Range"])
        if not match:  # fail loud: later sets have malformed range strings
            raise ValueError(f"unparseable range {m['Model.Range']!r} on {m['Model.Name']}")
        rng, tgt = match.groups()

        clicks = sorted(m["Dials"][0]["Clicks"], key=lambda c: c[P + "ClickNumber"])
        dial = []
        seen_dead = False
        for c in clicks:
            if click_is_dead(c):
                seen_dead = True
                continue
            if seen_dead:
                raise ValueError(f"non-contiguous dead tail on {m['Model.Name']}")
            stats = {s: int(c[P + s]) for s in ("Speed", "Attack", "Defense", "Damage")}
            if not any(stats.values()):
                raise ValueError(f"all-zero live click on {m['Model.Name']}")
            abil = {
                slot.lower(): conv_ability(c[P + slot + "AbilityId"], abilities)
                for slot in ("Speed", "Attack", "Defense", "Damage")
            }
            used_ability_ids.update(a["id"] for a in abil.values() if a)
            dial.append(
                {
                    "click": c[P + "ClickNumber"],
                    "speed": stats["Speed"],
                    "attack": stats["Attack"],
                    "defense": stats["Defense"],
                    "damage": stats["Damage"],
                    "abilities": abil,
                }
            )
        if not dial:
            raise ValueError(f"no live clicks on {m['Model.Name']}")

        figures.append(
            {
                "id": m["Model.ModelId"],
                "short_name": m["Model.ShortName"],
                "name": m["Model.Name"],
                "faction": m["Model.Factions"],
                "rank": m["Model.Rank"],
                "rarity": m["Model.Frequency"],
                "points": int(m["Model.UnitCost"]),
                "figure_number": str(m["Model.FigureNumber"]),
                "range": int(rng),
                "targets": int(tgt or 1),
                "arc_raw": m["Model.Arc"],
                "num_live_clicks": len(dial),
                "starting_click": 0,
                "seed_v1": False,
                "mounted": " On " in m["Model.Name"],
                "dial": dial,
            }
        )

    figures.sort(key=lambda f: f["id"])
    mounted = sum(1 for f in figures if f["mounted"])
    out = {
        "expansion": "Lancers",
        "source": "mageknight.net dial-stats (mkstats.json)",
        "count": len(figures),
        "figures": figures,
    }
    dest = REPO / "stats" / "lancers.json"
    dest.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {dest}: {len(figures)} figures ({mounted} mounted)")

    # Stamp used_in_lancers on the shared ability list (plan §1.3).
    abil_path = REPO / "stats" / "special_abilities.json"
    abil_raw = json.loads(abil_path.read_text())
    for a in abil_raw["abilities"]:
        a["used_in_lancers"] = a["id"] in used_ability_ids
    abil_path.write_text(json.dumps(abil_raw, indent=1) + "\n")
    print(f"stamped used_in_lancers on {len(used_ability_ids)} ability ids")


if __name__ == "__main__":
    main(sys.argv)
