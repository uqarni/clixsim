"""Figure & ability data loading (§9 Data Pipeline, DP5).

Content is data, not code. This module normalises every roster file in
``stats/`` (rebellion.json, lancers.json — any JSON with an ``expansion`` +
``figures`` header) plus ``stats/special_abilities.json`` into immutable
definition objects the engine instantiates in-play figures from.

Arc convention (OQ-5, RESOLVED): ``arc_raw`` is the TOTAL front-arc angle in
degrees — 90 is the standard quarter-circle clix front arc (facing +/- 45), and
the four 180 figures (Amazon Queen, Hierophant, Magus, Storm Golem — exactly
the multi-target-arrow commanders) get the wide half-circle arc (facing +/-
90). The old half-angle reading gave those four a 360-degree front arc with NO
rear at all, which is what pinned the convention.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "stats"


@dataclass(frozen=True)
class AbilityRef:
    """An ability instance referenced by a single dial click's stat slot."""

    id: int
    name: str
    optional: bool
    slot: str  # "speed" | "attack" | "defense" | "damage"


@dataclass(frozen=True)
class AbilityDef:
    id: int
    short_name: str
    name: str
    optional: bool
    color: str
    symbol: str
    description: str
    used_in_rebellion: bool
    used_in_lancers: bool = False


@dataclass(frozen=True)
class ClickStats:
    """One click on a combat dial (§Dial). A click is alive iff its stats are
    numeric; padded 'Dead' clicks are dropped upstream."""

    index: int
    speed: int
    attack: int
    defense: int
    damage: int
    abilities: tuple[AbilityRef, ...] = ()

    def ability_ids(self) -> tuple[int, ...]:
        return tuple(a.id for a in self.abilities)


@dataclass(frozen=True)
class FigureDef:
    id: int
    short_name: str
    name: str
    faction: str
    rank: str
    rarity: str
    points: int
    figure_number: str
    range: int
    targets: int
    arc_deg: float
    starting_click: int
    dial: tuple[ClickStats, ...]
    seed_v1: bool = True
    # Plain defaults below are load-bearing: pre-Lancers pickled sessions carry
    # FigureDefs without these attributes and fall back to the class attribute.
    expansion: str = "Rebellion"
    mounted: bool = False  # double "peanut" base (P5-R1); horseshoe speed symbol

    @property
    def arc_half_angle(self) -> float:
        """Front-arc half-angle in radians. ``arc_deg`` is the TOTAL arc angle
        (see module docstring / OQ-5): 90 => facing +/- 45."""
        return math.radians(self.arc_deg / 2.0)

    @property
    def num_live_clicks(self) -> int:
        return len(self.dial)

    @property
    def is_unique(self) -> bool:
        return self.rank == "Unique"

    @property
    def is_ranged(self) -> bool:
        return self.range > 0

    def all_ability_ids(self) -> set[int]:
        ids: set[int] = set()
        for click in self.dial:
            ids.update(click.ability_ids())
        return ids


class FigureDB:
    """In-memory database of figure and ability definitions."""

    def __init__(self, figures: dict[int, FigureDef], abilities: dict[int, AbilityDef]):
        self._figures = figures
        self._abilities = abilities

    # -- figures -----------------------------------------------------------
    def get(self, figure_id: int) -> FigureDef:
        return self._figures[figure_id]

    def all_figures(self) -> list[FigureDef]:
        return list(self._figures.values())

    def find(self, short_name: str, faction: str | None = None) -> list[FigureDef]:
        out = []
        for f in self._figures.values():
            if f.short_name.lower() == short_name.lower() and (
                faction is None or f.faction == faction
            ):
                out.append(f)
        return out

    def filter(
        self,
        faction: str | None = None,
        rank: str | None = None,
        max_points: int | None = None,
        min_points: int | None = None,
        ranged: bool | None = None,
    ) -> list[FigureDef]:
        out = []
        for f in self._figures.values():
            if faction is not None and f.faction != faction:
                continue
            if rank is not None and f.rank != rank:
                continue
            if max_points is not None and f.points > max_points:
                continue
            if min_points is not None and f.points < min_points:
                continue
            if ranged is not None and f.is_ranged != ranged:
                continue
            out.append(f)
        return out

    def factions(self) -> list[str]:
        return sorted({f.faction for f in self._figures.values()})

    # -- abilities ---------------------------------------------------------
    def ability(self, ability_id: int) -> AbilityDef | None:
        return self._abilities.get(ability_id)

    def all_abilities(self) -> list[AbilityDef]:
        return list(self._abilities.values())


def _parse_ability_refs(raw_abilities: dict) -> tuple[AbilityRef, ...]:
    refs = []
    for slot in ("speed", "attack", "defense", "damage"):
        val = raw_abilities.get(slot)
        if val:
            refs.append(
                AbilityRef(
                    id=int(val["id"]),
                    name=val["name"],
                    optional=bool(val.get("optional", True)),
                    slot=slot,
                )
            )
    return tuple(refs)


def _parse_figure(raw: dict, expansion: str = "Rebellion") -> FigureDef:
    dial = tuple(
        ClickStats(
            index=int(c["click"]),
            speed=int(c["speed"]),
            attack=int(c["attack"]),
            defense=int(c["defense"]),
            damage=int(c["damage"]),
            abilities=_parse_ability_refs(c.get("abilities", {})),
        )
        for c in raw["dial"]
    )
    return FigureDef(
        id=int(raw["id"]),
        short_name=raw["short_name"],
        name=raw["name"],
        faction=raw["faction"],
        rank=raw["rank"],
        rarity=str(raw["rarity"]),
        points=int(raw["points"]),
        figure_number=str(raw["figure_number"]),
        range=int(raw["range"]),
        targets=int(raw["targets"]),
        arc_deg=float(raw["arc_raw"]),
        starting_click=int(raw.get("starting_click", 0)),
        dial=dial,
        seed_v1=bool(raw.get("seed_v1", True)),
        expansion=expansion,
        mounted=bool(raw.get("mounted", False)),
    )


@lru_cache(maxsize=1)
def load_db(data_dir: str | None = None) -> FigureDB:
    base = Path(data_dir) if data_dir else _DATA_DIR
    abil_raw = json.loads((base / "special_abilities.json").read_text())

    figures: dict[int, FigureDef] = {}
    for path in sorted(base.glob("*.json")):
        raw = json.loads(path.read_text())
        if "expansion" not in raw or "figures" not in raw:
            continue  # not a roster file (e.g. special_abilities.json)
        expansion = raw["expansion"]
        for fig_raw in raw["figures"]:
            fig = _parse_figure(fig_raw, expansion=expansion)
            if fig.id in figures:  # cross-set id collision would corrupt replays
                raise ValueError(
                    f"figure id {fig.id} in {expansion} collides with "
                    f"{figures[fig.id].expansion}/{figures[fig.id].name}"
                )
            figures[fig.id] = fig

    abilities = {}
    for a in abil_raw["abilities"]:
        ability = AbilityDef(
            id=int(a["id"]),
            short_name=a["short_name"],
            name=a["name"],
            optional=bool(a["optional"]),
            color=a.get("color", ""),
            symbol=a.get("symbol", ""),
            description=a.get("description", ""),
            used_in_rebellion=bool(a.get("used_in_rebellion", False)),
            used_in_lancers=bool(a.get("used_in_lancers", False)),
        )
        abilities[ability.id] = ability

    return FigureDB(figures, abilities)
