"""Player *intents* — the only way to mutate state (DP1).

The renderer and the LLM never touch state directly; they submit an intent the
engine validates and resolves into a Result or a Rejection. Ability-driven
special actions are carried as ``variant`` tags / dedicated intents.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MoveIntent:
    figure_uid: int
    dest: tuple[float, float]
    facing: float  # radians, set freely after moving (P4-R7)
    free: bool = False  # Quickness: move without spending a turn action
    formation_uids: tuple[int, ...] = ()  # movement formation members (incl. leader)
    member_dests: tuple[tuple[float, float], ...] = ()  # per-member destinations
    member_facings: tuple[float, ...] = ()
    kind: str = "move"


@dataclass(frozen=True)
class RangedIntent:
    attacker_uid: int
    target_uids: tuple[int, ...]
    # normal | magic_blast | flame_lightning | shockwave | magic_healing
    variant: str = "normal"
    formation_uids: tuple[int, ...] = ()  # ranged formation members
    kind: str = "ranged"


@dataclass(frozen=True)
class CloseIntent:
    attacker_uid: int
    target_uid: int
    variant: str = "normal"  # normal | weapon_master | healing
    formation_uids: tuple[int, ...] = ()  # close formation members
    heal_d6: bool = False  # Healing: use the 1d6 alternative instead of the damage value
    kind: str = "close"


@dataclass(frozen=True)
class PassIntent:
    figure_uid: int
    kind: str = "pass"


@dataclass(frozen=True)
class RegenerateIntent:
    figure_uid: int
    kind: str = "regenerate"


@dataclass(frozen=True)
class NecromancyIntent:
    figure_uid: int
    revive_uid: int
    kind: str = "necromancy"


@dataclass(frozen=True)
class LevitateIntent:
    figure_uid: int
    target_uid: int
    dest: tuple[float, float]
    facing: float
    kind: str = "levitate"


Intent = (
    MoveIntent
    | RangedIntent
    | CloseIntent
    | PassIntent
    | RegenerateIntent
    | NecromancyIntent
    | LevitateIntent
)


@dataclass
class Rejection:
    reason: str
    detail: str = ""

    ok: bool = field(default=False, init=False)


@dataclass
class Result:
    kind: str
    events: list[dict] = field(default_factory=list)
    summary: str = ""

    ok: bool = field(default=True, init=False)
