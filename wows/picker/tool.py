"""LLM tool for the ship picker: structured arguments -> PickRequest -> JSON result.

The model fills typed fields (pool / nations / types / categories / tiers / count / exclude);
the text command parser stays in parser.py. Both front-ends end up in Picker.pick_request().
"""
from __future__ import annotations

from ..tool_common import InvalidArgs, invalid as _invalid, result as _result
from .models import PickRequest, ShipFilter
from .parser import CATEGORY_ALIASES, NATION_ALIASES, TYPE_ALIASES

NAME = "wows_pick_ship"

DESCRIPTION = (
    "从战舰世界（窝窝/WoWS）亚服随机抽 1～5 艘船：可从持有清单、玩过的船或全库抽，卡片直接发到聊天。"
    "只给提问者本人抽，不查战绩（查战绩用 wows_query）。不填参数=默认范围随机一艘。"
)

NATIONS = ["日本", "美国", "德国", "苏联", "英国", "法国", "意大利", "泛亚",
           "欧洲", "泛美", "荷兰", "西班牙", "英联邦"]
TYPES = ["驱逐", "巡洋", "战列", "航母", "潜艇"]
CATEGORIES = ["研发", "金币", "特种", "超级战舰"]
POOLS = {"自动": "auto", "我的": "mine", "持有": "owned", "玩过": "played", "全库": "catalog"}

SCHEMA = {
    "type": "object",
    "properties": {
        "pool": {"type": "string", "enum": list(POOLS),
                 "description": "抽取范围，默认自动：有绑定优先持有清单，其次玩过的船，最后全库"},
        "nations": {"type": "array", "items": {"type": "string", "enum": NATIONS},
                    "description": "国家，可多选，多个是任一；空=不限"},
        "types": {"type": "array", "items": {"type": "string", "enum": TYPES},
                  "description": "舰种，可多选，多个是任一；空=不限"},
        "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES},
                       "description": "研发/金币/特种/超级战舰，可多选"},
        "tier_min": {"type": "integer", "minimum": 1, "maximum": 11,
                     "description": "最低等级；不填用配置默认"},
        "tier_max": {"type": "integer", "minimum": 1, "maximum": 11,
                     "description": "最高等级；不填用配置默认"},
        "count": {"type": "integer", "minimum": 1, "maximum": 5,
                  "description": "抽几艘，默认 1"},
        "exclude": {"type": "array", "items": {"type": "string"},
                    "description": "排除项：国家/舰种/分类，如 金币、航母、日本"},
    },
    "required": [],
}

PICK_NEXT = {
    "ok": "只说明抽到哪艘/哪几艘和抽取范围，不要自行再抽；用户明确要换一批时再调用一次。",
    "invalid": "选船参数写错了，按 message 修正后再调用一次。",
    "disabled": "随机选船被管理员关闭了，照实告诉用户。",
    "cooldown": "用户选船太频繁：告诉用户稍等几秒，不要立即重试。",
    "not_bound": "用户还没绑定账号：可以请用户发 /wows 绑定 昵称，或改用“全库”。",
    "no_owned": "用户还没有持有清单：建议 /wows 持有 导入 或 添加，或改用“全库”。",
    "no_history": "还没有玩过的船记录：建议改用“全库”。",
    "hidden": "账号隐藏了战绩：建议设置持有清单，或改用“全库”。",
    "stats_unavailable": "暂时拿不到战绩：建议改用“全库”或稍后重试。",
    "empty": "没有符合条件的船：建议放宽条件或改用“全库”，不要换成别的条件反复试。",
    "catalog_busy": "船库正在初始化：告诉用户稍后再试，不要连续调用。",
    "error": "选船失败：把 message 照实告诉用户，不要重试。",
}


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    return "" if value is None else str(value).strip()


def _strings(args: dict, key: str) -> list[str]:
    """Array of labels; a single string is accepted as a one-element list for model quirk tolerance."""
    value = args.get(key)
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        raise InvalidArgs(f"{key} 要填字符串数组，例如 [\"日本\"]")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidArgs(f"{key} 里的每一项都必须是字符串")
        if item.strip():
            out.append(item.strip())
    return out


def _enum_set(args: dict, key: str, aliases: dict[str, str], labels: list[str],
              field: str) -> frozenset[str]:
    values = set()
    for raw in _strings(args, key):
        if raw not in aliases:
            raise InvalidArgs(f"{field} 只能是：{'/'.join(labels)}")
        values.add(aliases[raw])
    return frozenset(values)


def _int_field(args: dict, key: str, lo: int, hi: int) -> int | None:
    value = args.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise InvalidArgs(f"{key} 要填 {lo}～{hi} 的整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidArgs(f"{key} 要填 {lo}～{hi} 的整数") from None
    if isinstance(value, float) and value != number:
        raise InvalidArgs(f"{key} 要填 {lo}～{hi} 的整数")
    if not lo <= number <= hi:
        raise InvalidArgs(f"{key} 要填 {lo}～{hi} 的整数")
    return number


def _exclude(args: dict) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    nations: set[str] = set()
    types: set[str] = set()
    categories: set[str] = set()
    for raw in _strings(args, "exclude"):
        token = raw
        for prefix in ("不要", "除了", "排除", "非"):
            if token.startswith(prefix) and token[len(prefix):].strip():
                token = token[len(prefix):].strip()
                break
        token = token.lstrip("-").strip()
        if token in NATION_ALIASES:
            nations.add(NATION_ALIASES[token])
        elif token in TYPE_ALIASES:
            types.add(TYPE_ALIASES[token])
        elif token in CATEGORY_ALIASES:
            categories.add(CATEGORY_ALIASES[token])
        else:
            raise InvalidArgs("exclude 只能是国家/舰种/分类，例如：金币、航母、日本")
    return frozenset(nations), frozenset(types), frozenset(categories)


def parse_args(args: dict, *, default_tiers: tuple[int, int] = (5, 10),
               max_count: int = 5) -> PickRequest:
    """Validate the tool arguments and build the same PickRequest the text parser produces."""
    pool_word = _text(args, "pool") or "自动"
    if pool_word not in POOLS:
        raise InvalidArgs(f"pool 只能是：{'/'.join(POOLS)}")
    nations = _enum_set(args, "nations", NATION_ALIASES, NATIONS, "nations")
    types = _enum_set(args, "types", TYPE_ALIASES, TYPES, "types")
    categories = _enum_set(args, "categories", CATEGORY_ALIASES, CATEGORIES, "categories")
    exclude_nations, exclude_types, exclude_categories = _exclude(args)
    for label, include, exclude in (("国家", nations, exclude_nations), ("舰种", types, exclude_types),
                                    ("分类", categories, exclude_categories)):
        if include & exclude:
            raise InvalidArgs(f"同一个{label}条件不能既要又不要")

    lo = _int_field(args, "tier_min", 1, 11)
    hi = _int_field(args, "tier_max", 1, 11)
    has_super = "super" in categories
    if lo is None and hi is None:
        if not has_super:
            lo, hi = default_tiers
    elif lo is None:
        lo = 1 if has_super else default_tiers[0]
    elif hi is None:
        hi = 11 if has_super else default_tiers[1]
    if lo is not None and hi is not None and lo > hi:
        raise InvalidArgs("tier_min 不能大于 tier_max")

    count = _int_field(args, "count", 1, max_count) or 1
    flt = ShipFilter(nations=nations, types=types, categories=categories, tier_min=lo, tier_max=hi,
                     exclude_nations=exclude_nations, exclude_types=exclude_types,
                     exclude_categories=exclude_categories)
    return PickRequest(pool=POOLS[pool_word], filter=flt, count=count)


def result(outcome, *, sent: bool) -> str:
    return _result(outcome, sent=sent, next_map=PICK_NEXT)


def invalid(message: str) -> str:
    return _invalid(message, next_map=PICK_NEXT)
