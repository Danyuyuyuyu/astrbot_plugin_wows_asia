"""Filtering and uniform random choice (pure)."""
from __future__ import annotations

import random
from typing import Iterable

from .models import SPECIAL_AS_PREMIUM_FROM_TIER, PickError, Ship, ShipFilter


def in_category(ship: Ship, category: str) -> bool:
    if category == "super":
        return ship.tier >= 11
    if ship.tier >= 11:
        return False
    if category == "special":
        return ship.special
    if category == "premium":
        if ship.tier >= SPECIAL_AS_PREMIUM_FROM_TIER:
            return ship.premium or ship.special  # no is_premium ships from X up: 金币 includes 特种 there
        return ship.premium and not ship.special
    return not ship.premium and not ship.special  # tech


def matches(ship: Ship, flt: ShipFilter) -> bool:
    if flt.tier_min is not None and ship.tier < flt.tier_min:
        return False
    if flt.tier_max is not None and ship.tier > flt.tier_max:
        return False
    if flt.nations and ship.nation not in flt.nations:
        return False
    if ship.nation in flt.exclude_nations or ship.type in flt.exclude_types or ship.ship_id in flt.exclude_ids:
        return False
    if flt.types and ship.type not in flt.types:
        return False
    if flt.categories and not any(in_category(ship, c) for c in flt.categories):
        return False
    if any(in_category(ship, c) for c in flt.exclude_categories):
        return False
    return True


def candidates(ships: Iterable[Ship], flt: ShipFilter, allowed: set[int] | frozenset[int] | None = None) -> list[Ship]:
    return [s for s in ships if (allowed is None or s.ship_id in allowed) and matches(s, flt)]


def pick(ships: Iterable[Ship], flt: ShipFilter, *, allowed=None, count: int = 1,
         rng: random.Random | None = None) -> list[Ship]:
    """Pick up to ``count`` distinct ships; raises PickError("empty") when nothing matches."""
    pool = candidates(ships, flt, allowed)
    if not pool:
        raise PickError("empty", "没有符合条件的船")
    chooser = rng or random
    return chooser.sample(pool, min(count, len(pool)))
