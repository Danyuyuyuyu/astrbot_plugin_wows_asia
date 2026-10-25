"""Free-text picker arguments -> PickRequest. Repeated words of one kind mean “any of them”."""
from __future__ import annotations

import re
import unicodedata

from .models import PickError, PickRequest, ShipFilter

NATION_ALIASES = {alias: nation for nation, aliases in {
    "japan": ("日本", "日", "日系", "日船"), "usa": ("美国", "美", "美系", "美船"), "germany": ("德国", "德", "德系"),
    "ussr": ("苏联", "苏", "苏系", "毛子"), "uk": ("英国", "英", "英系"), "france": ("法国", "法", "法系"),
    "italy": ("意大利", "意", "意系"), "pan_asia": ("泛亚", "泛亚系"), "europe": ("欧洲", "泛欧", "欧系"),
    "pan_america": ("泛美", "泛美系"), "netherlands": ("荷兰", "荷系"), "spain": ("西班牙", "西系"),
    "commonwealth": ("英联邦",),
}.items() for alias in aliases}
TYPE_ALIASES = {alias: kind for kind, aliases in {
    "Battleship": ("战列舰", "战列", "bb"), "Cruiser": ("巡洋舰", "巡洋", "巡", "ca"),
    "Destroyer": ("驱逐舰", "驱逐", "dd"), "AirCarrier": ("航空母舰", "航母", "cv"),
    "Submarine": ("潜艇", "潜水艇", "ss"),
}.items() for alias in aliases}
CATEGORY_ALIASES = {alias: cat for cat, aliases in {
    "tech": ("研发船", "研发", "银币船", "银币", "tech"), "premium": ("金币船", "金币", "金船", "premium"),
    "special": ("特种船", "特种", "special"), "super": ("超级战舰", "超级", "超战", "super"),
}.items() for alias in aliases}
POOL_WORDS = {"我的": "mine", "自己的": "mine", "持有": "owned", "持有清单": "owned", "玩过": "played",
              "玩过的": "played", "历史": "played", "全库": "catalog", "全部": "catalog", "所有": "catalog"}
NEGATIONS = ("不要", "除了", "排除", "非", "不", "-", "!")
ROMAN = {r: i for i, r in enumerate(("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi"), 1)}
DEFAULT_TIER_MIN, DEFAULT_TIER_MAX = 5, 10
MAX_COUNT = 5
HINT = ("可用条件：我的 / 全库、国家（日本 美国 德国…）、舰种（驱逐 巡洋 战列 航母 潜艇）、"
        "分类（研发 金币 特种 超级战舰）、等级（8、VIII、8-10、8级以上）、不要xx、3艘")

_TIER = r"(\d{1,2}|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i)"
_RANGE = re.compile(rf"{_TIER}(?:级|級)?[-~～到至]{_TIER}(?:级|級)?")
_ABOVE = re.compile(rf"{_TIER}(?:级|級)?(?:以上|及以上|\+)")
_BELOW = re.compile(rf"{_TIER}(?:级|級)?(?:以下|及以下)")
_SINGLE = re.compile(rf"{_TIER}(?:级|級)?")
_COUNT = re.compile(r"(?:[x×*抽来]\s*)?(\d{1,2})(?:艘|条|只|个)|[x×*](\d{1,2})")
_SPLIT = re.compile(r"[\s,，、;；]+")


def _tier(raw: str) -> int:
    value = ROMAN.get(raw) if not raw.isdecimal() else int(raw)
    if value is None or not 1 <= value <= 11:
        raise PickError("invalid", "等级必须是 1–11 或 I–XI")
    return value


def _kind(token: str) -> tuple[str, str] | None:
    for table, kind in ((NATION_ALIASES, "nation"), (TYPE_ALIASES, "type"), (CATEGORY_ALIASES, "category")):
        if token in table:
            return kind, table[token]
    return None


def parse_pick(text: str, *, default_tiers: tuple[int, int] = (DEFAULT_TIER_MIN, DEFAULT_TIER_MAX),
               max_count: int = MAX_COUNT) -> PickRequest:
    tokens = [t for t in _SPLIT.split(unicodedata.normalize("NFKC", text or "").casefold().strip()) if t]
    pool = None
    include: dict[str, set] = {"nation": set(), "type": set(), "category": set()}
    exclude: dict[str, set] = {"nation": set(), "type": set(), "category": set()}
    tier_min = tier_max = None
    count = 1
    negate_next = False
    for token in tokens:
        if token in ("不要", "除了", "排除"):
            negate_next = True
            continue
        if token in POOL_WORDS:
            new = POOL_WORDS[token]
            if pool not in (None, new):
                raise PickError("invalid", "“我的 / 持有 / 玩过 / 全库”只能选一个")
            pool = new
            continue
        if m := _COUNT.fullmatch(token):
            count = int(m.group(1) or m.group(2))
            if not 1 <= count <= max_count:
                raise PickError("invalid", f"一次最多抽 {max_count} 艘")
            continue
        negated, word = negate_next, token
        negate_next = False
        if not _kind(word):
            for prefix in NEGATIONS:
                if word.startswith(prefix) and _kind(word[len(prefix):]):
                    negated, word = True, word[len(prefix):]
                    break
        if kind := _kind(word):
            (exclude if negated else include)[kind[0]].add(kind[1])
            continue
        if negated:
            raise PickError("invalid", f"“不要”后面只能接国家、舰种或分类：{token}")
        lo = hi = None
        if m := _RANGE.fullmatch(token):
            lo, hi = sorted((_tier(m.group(1)), _tier(m.group(2))))
        elif m := _ABOVE.fullmatch(token):
            lo, hi = _tier(m.group(1)), 11
        elif m := _BELOW.fullmatch(token):
            lo, hi = 1, _tier(m.group(1))
        elif m := _SINGLE.fullmatch(token):
            lo = hi = _tier(m.group(1))
        if lo is None:
            raise PickError("invalid", f"看不懂“{token}”。{HINT}")
        if tier_min is not None and (tier_min, tier_max) != (lo, hi):
            raise PickError("invalid", "等级只能写一个（范围用 8-10）")
        tier_min, tier_max = lo, hi
    if negate_next:
        raise PickError("invalid", "“不要”后面要接国家、舰种或分类")
    for kind in include:
        if include[kind] & exclude[kind]:
            raise PickError("invalid", "同一个条件不能既要又不要")
    if tier_min is None and "super" not in include["category"]:
        # default V–X: a 超级战舰 request means tier XI, so it is not limited by the default range
        tier_min, tier_max = default_tiers
    flt = ShipFilter(frozenset(include["nation"]), frozenset(include["type"]), frozenset(include["category"]),
                     tier_min, tier_max, frozenset(exclude["nation"]), frozenset(exclude["type"]),
                     frozenset(exclude["category"]))
    return PickRequest(pool or "auto", flt, count)
