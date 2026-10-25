"""Ship picker data types (pure: no network, storage or framework)."""
from __future__ import annotations

from dataclasses import dataclass, field

TYPES = {"Destroyer": "驱逐舰", "Cruiser": "巡洋舰", "Battleship": "战列舰", "AirCarrier": "航空母舰",
         "Submarine": "潜艇"}
TYPE_CODES = {"Destroyer": "DD", "Cruiser": "CA", "Battleship": "BB", "AirCarrier": "CV", "Submarine": "SS"}
NATIONS = {"japan": "日本", "usa": "美国", "germany": "德国", "ussr": "苏联", "uk": "英国", "france": "法国",
           "italy": "意大利", "pan_asia": "泛亚", "europe": "欧洲", "pan_america": "泛美", "netherlands": "荷兰",
           "spain": "西班牙", "commonwealth": "英联邦"}
CATEGORIES = {"tech": "研发船", "premium": "金币船", "special": "特种船", "super": "超级战舰"}
POOLS = {"auto": "自动", "mine": "我的", "owned": "持有清单", "played": "玩过的船", "catalog": "全库"}
POOL_LABELS = {"owned": "持有清单", "played": "玩过的船", "catalog": "亚服全库"}
ROMAN = ("", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI")
# Official data has no is_premium ship from tier X up (they are all is_special): there 金币 includes 特种
SPECIAL_AS_PREMIUM_FROM_TIER = 10


@dataclass(frozen=True)
class Ship:
    ship_id: int
    name: str  # API name (display names come from ShipNameMap)
    tier: int
    nation: str
    type: str
    premium: bool = False
    special: bool = False
    code: str | None = None
    image_small: str | None = None
    image_medium: str | None = None
    image_large: str | None = None
    image_contour: str | None = None

    @property
    def category(self) -> str:
        if self.tier >= 11:
            return "super"
        if self.special:
            return "special"
        return "premium" if self.premium else "tech"


@dataclass(frozen=True)
class ShipFilter:
    nations: frozenset[str] = frozenset()
    types: frozenset[str] = frozenset()
    categories: frozenset[str] = frozenset()
    tier_min: int | None = None
    tier_max: int | None = None
    exclude_nations: frozenset[str] = frozenset()
    exclude_types: frozenset[str] = frozenset()
    exclude_categories: frozenset[str] = frozenset()
    exclude_ids: frozenset[int] = frozenset()

    def describe(self) -> str:
        """Short Chinese summary, e.g. “8–10级 日本/美国 战列舰，不要金币船”."""
        parts = []
        if self.tier_min is not None or self.tier_max is not None:
            lo, hi = self.tier_min or 1, self.tier_max or 11
            parts.append(f"{lo}级" if lo == hi else f"{lo}–{hi}级")
        for values, labels in ((self.nations, NATIONS), (self.types, TYPES), (self.categories, CATEGORIES)):
            if values:
                parts.append("/".join(labels.get(v, v) for v in sorted(values)))
        text = " ".join(parts)
        excluded = [labels.get(v, v) for values, labels in ((self.exclude_nations, NATIONS),
                    (self.exclude_types, TYPES), (self.exclude_categories, CATEGORIES)) for v in sorted(values)]
        if excluded:
            text += ("，" if text else "") + "不要" + "/".join(excluded)
        return text or "不限"


@dataclass(frozen=True)
class PickRequest:
    pool: str = "auto"  # auto | mine | owned | played | catalog
    filter: ShipFilter = field(default_factory=ShipFilter)
    count: int = 1


class PickError(Exception):
    """User-facing picker error with a machine-readable code (also used by the LLM tool)."""

    def __init__(self, code: str, message: str, options: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.options = options or []
