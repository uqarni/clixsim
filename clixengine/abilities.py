"""Special-ability effect hooks (P4-R34/R35, X6).

Abilities are data (the colored squares on each dial click); this module maps the
24 Rebellion abilities to engine effects. The engine calls these hooks at the
relevant stages (defense, damage, break-away, movement legality, start-of-turn,
candidate generation). Effects are keyed by ability id so content stays data-driven.

An ability is "in effect" only while shown on the figure's current click and not
cancelled (optional abilities are on by default but cancelable — handled by
``Figure.active_ability_ids``). Terrain-dependent clauses are noted; the parts
that don't need terrain are implemented, the rest are flagged (never silently
mis-applied).
"""

from __future__ import annotations

# --- ability ids -----------------------------------------------------------
AQUATIC = 86
BATTLE_ARMOR = 87
BATTLE_FURY = 88
BERSERK = 89
BOUND = 90
CHARGE = 91
COMMAND = 92
DEFEND = 94
DEMORALIZED = 95
FLAME_LIGHTNING = 97
FLIGHT = 98
HEALING = 100
INVULNERABILITY = 101
MAGIC_BLAST = 103
MAGIC_ENHANCEMENT = 105
MAGIC_HEALING = 107
MAGIC_IMMUNITY = 108
MAGIC_LEVITATION = 109
NECROMANCY = 111
POLE_ARM = 114
QUICKNESS = 115
REGENERATION = 117
SHOCKWAVE = 118
STEALTH = 121
TOUGHNESS = 123
VAMPIRISM = 124
WEAPON_MASTER = 126

# Abilities with a real engine effect in this build. Everything referenced by the
# roster but absent here is flagged by ability-coverage telemetry.
IMPLEMENTED_ABILITY_IDS: set[int] = {
    AQUATIC, BATTLE_ARMOR, BERSERK, COMMAND, DEFEND, DEMORALIZED,
    FLAME_LIGHTNING, FLIGHT, HEALING, MAGIC_BLAST, MAGIC_ENHANCEMENT, MAGIC_HEALING,
    MAGIC_IMMUNITY, MAGIC_LEVITATION, NECROMANCY, POLE_ARM, QUICKNESS, REGENERATION,
    SHOCKWAVE, STEALTH, TOUGHNESS, VAMPIRISM, WEAPON_MASTER,
}
# Stealth is LIVE since terrain shipped: a line of fire to a Stealth figure
# that crosses hindering terrain is treated as blocked (engine.line_of_fire).
TERRAIN_DEPENDENT_IDS: set[int] = set()
# Whole effect depends on the capture subsystem (FUT-CAP), which is out of scope:
# reported separately so coverage isn't overstated.
CAPTURE_PENDING_IDS: set[int] = {BATTLE_FURY}
# Lancers trio, implementation staged in docs/lancers-plan.md P4 — flagged (not
# silently ignored) until then; move to IMPLEMENTED_ABILITY_IDS when they land.
FLAGGED_ABILITY_IDS: set[int] = {BOUND, CHARGE, INVULNERABILITY}

# Abilities that grant free (non-formation) movement: pass through figure bases,
# only fail break-away on a natural 1 (§Flight / §Aquatic).
FREE_MOVEMENT_IDS: set[int] = {FLIGHT, AQUATIC}

# Optional abilities that are activated by spending the figure's action as a
# *special* action rather than a normal move/ranged/close.
SPECIAL_ACTION_IDS: set[int] = {
    REGENERATION, HEALING, MAGIC_HEALING, MAGIC_BLAST, FLAME_LIGHTNING, SHOCKWAVE,
    NECROMANCY, MAGIC_LEVITATION, WEAPON_MASTER,
}


def has(figure, ability_id: int) -> bool:
    return ability_id in figure.active_ability_ids()


def is_mounted(figure) -> bool:
    """Mounted (cavalry) figures use a double 'peanut' base (P5-R1): two equal
    circles joined along the facing axis, position = the FRONT circle's center
    dot. They never receive a free spin (P5-R6), break away only failing on a 1
    (P5-R3), and deal Shake Off damage on success (P5-R5). 54 Lancers figures."""
    return bool(getattr(figure.definition, "mounted", False))


def _magic_immune(figure) -> bool:
    return MAGIC_IMMUNITY in figure.active_ability_ids()


# --- break-away ------------------------------------------------------------
def break_away_min(figure) -> int:
    """Minimum d6 needed to break away: normally 4; Flight/Aquatic fail only on 1."""
    if figure.active_ability_ids() & FREE_MOVEMENT_IDS:
        return 2
    return 4


def ignores_figure_bases(figure) -> bool:
    """Flight/Aquatic move through figure bases (may not END on one)."""
    return bool(figure.active_ability_ids() & FREE_MOVEMENT_IDS)


# --- action restrictions ---------------------------------------------------
def can_make_ranged_attack(figure) -> bool:
    """Berserk warriors may not be given a ranged combat action (§Berserk)."""
    return not has(figure, BERSERK)


# --- defense ---------------------------------------------------------------
def effective_defense(state, target, attack_type: str, terrain_mod: int = 0) -> int:
    """Target's defense against an incoming attack, applying Battle Armor (+2 vs
    ranged), Defend (may use a base-contact friendly's higher defense value), and a
    terrain modifier (hindering / height advantage, computed by the engine)."""
    base = target.defense
    ba = attack_type == "ranged" and has(target, BATTLE_ARMOR)
    # Defend: swap in the best friendly provider's printed defense if higher.
    from .state import figures_in_base_contact

    best_share = 0
    for friend in state.friends_of(target):
        if has(friend, DEFEND) and figures_in_base_contact(target, friend):
            best_share = max(best_share, friend.defense)
    d = max(base, best_share)
    if ba:
        d += 2
    return d + terrain_mod


# --- damage ----------------------------------------------------------------
def damage_after_defenses(target, raw: int, source_type: str, is_magic: bool) -> int:
    """Apply damage-reducing abilities. ``source_type`` in
    {ranged, close, ability}. Toughness reduces combat/ability damage by 1 (not
    pushing/crit-miss, which bypass this path). Magic Immunity negates magic damage."""
    if raw <= 0:
        return 0
    if is_magic and _magic_immune(target):
        return 0
    dmg = raw
    if has(target, TOUGHNESS) and source_type in ("ranged", "close", "ability"):
        dmg = max(0, dmg - 1)
    return dmg


def ranged_damage_bonus(state, attacker, target) -> int:
    """Magic Enhancement: +1 damage per enhancer in base contact with the
    attacker when it makes a ranged combat attack. STACKS — each enhancer is
    its own warrior granting "1 extra click" (the classic Demi-Magus battery:
    four in contact = +4). A Magic Immune figure neither *receives* nor
    *inflicts* the extra clicks (ability 108)."""
    if _magic_immune(target) or _magic_immune(attacker):
        return 0
    from .state import figures_in_base_contact

    bonus = 0
    for friend in state.friends_of(attacker, include_self=False):
        if has(friend, MAGIC_ENHANCEMENT) and figures_in_base_contact(attacker, friend):
            bonus += 1
    return bonus


def vampirism_heal(attacker) -> int:
    """Vampirism: heal 1 click when this warrior inflicts close-combat damage."""
    return 1 if has(attacker, VAMPIRISM) else 0
