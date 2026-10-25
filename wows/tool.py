"""Structured LLM tool: JSON-schema arguments -> Command, Outcome -> a JSON result with a next-step hint.

The model never writes command strings; every argument has its own field (enums where possible) and is
validated here. Bad arguments come back as status "invalid" with what to fix instead of a guessed query.
"""
from __future__ import annotations

import json
import re

from .app import Outcome
from .commands import (SELF_WORDS, TOTAL_WORDS, Command, Filter, Query, WowsError, _parse_filter, _ship_name,
                       _target)

NAME = "wows_query"

ACTIONS = {"总战绩": "total", "最近": "recent", "场次": "battles", "单场": "battle", "单船": "ship",
           "绑定": "bind", "解绑": "unbind"}
MODES = {"随机": ("pvp", None), "排位": ("rank_solo", None), "单野": ("pvp", "solo"), "组队": ("pvp", "div"),
         "双排": ("pvp", "div2"), "三排": ("pvp", "div3")}
RESULTS = {"胜": "win", "负": "loss"}
SORTS = {"时间": None, "伤害": "damage", "击沉": "frags", "经验": "xp", "侦察": "scouting", "潜在": "potential"}
ORDERS = {"从高到低": False, "从新到旧": False, "从低到高": True, "从旧到新": True}

DESCRIPTION = (
    "查战舰世界（窝窝/WoWS）亚服战绩。卡片图片直接发到聊天里，返回 JSON：status、卡片数据、next（下一步怎么做）。"
    "对比多人时分别调用。不要编造昵称或数字。最近/场次/单场只含开始记录后的场次；总战绩、单船是生涯数据。"
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS),
                   "description": "总战绩=生涯；最近=近7天概况；场次=列出多场（拿不准用它）；单场=只看一把；单船=某船生涯；绑定=绑定用户给的昵称"},
        "player": {"type": "string", "description": "其他玩家完整昵称；查提问者本人留空"},
        "account_id": {"type": "integer"},
        "mention_id": {"type": "string", "description": "被@者ID（“@昵称(ID)”括号里的数字），看不到填@"},
        "ship": {"type": "string", "description": "船名，可有错字；单船必填"},
        "date": {"type": "string", "description": "今天/昨天/前天/近N天/本周/10-15/10-12~10-15/今天下午"},
        "start_hour": {"type": "integer", "description": "时段起（点）"},
        "end_hour": {"type": "integer", "description": "时段止（点，不含）"},
        "mode": {"type": "string", "enum": list(MODES)},
        "result": {"type": "string", "enum": list(RESULTS)},
        "sort": {"type": "string", "enum": list(SORTS), "description": "默认时间；最好/最差用经验"},
        "order": {"type": "string", "enum": list(ORDERS), "description": "最低/最差/最早用从低到高或从旧到新"},
        "limit": {"type": "integer", "description": "场次：取排序后前几场"},
        "nth": {"type": "integer", "description": "单场：排序后第几把"},
    },
    "required": ["action"],
}

_MENTION_TEXT = re.compile(r"@?.*?[(（](\d{5,})[)）]")  # WG nicknames never contain brackets
_FILTER_KEYS = ("date", "start_hour", "end_hour", "mode", "result", "sort", "order", "limit", "nth")


class InvalidArgs(WowsError):
    """The model filled the tool in wrong; tell it what to fix."""


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    return "" if value is None else str(value).strip()


def _int(args: dict, key: str, lo: int, hi: int) -> int | None:
    value = args.get(key)
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidArgs(f"{key} 要填 {lo}～{hi} 的整数") from None
    if not lo <= number <= hi or (isinstance(value, float) and value != number):
        raise InvalidArgs(f"{key} 要填 {lo}～{hi} 的整数")
    return number


def _choice(args: dict, key: str, options: dict):
    value = _text(args, key)
    if not value:
        return None
    if value not in options:
        raise InvalidArgs(f"{key} 只能是：{' / '.join(options)}")
    return value


def parse_args(args: dict) -> tuple[Command, str | None, list[str]]:
    """-> (command, mention_id or None, notes about ignored arguments)."""
    notes: list[str] = []
    action = _choice(args, "action", ACTIONS)
    if action is None:
        raise InvalidArgs("action 必填：" + " / ".join(ACTIONS))
    kind = ACTIONS[action]

    player, mention = _text(args, "player"), _text(args, "mention_id")
    if m := _MENTION_TEXT.fullmatch(player):  # "@小明(20002)" pasted into player
        player, mention = "", mention or m.group(1)
    if player.lower() in SELF_WORDS or player in TOTAL_WORDS or player in ("提问者", "本人", "自己"):
        player = ""
    account_id = _int(args, "account_id", 1, 2**63 - 1)
    if mention and mention != "@" and not mention.isdecimal():
        raise InvalidArgs("mention_id 要填消息里“@昵称(ID)”括号中的数字，看不到时填 \"@\"")
    if sum(bool(x) for x in (player, account_id, mention)) > 1:
        raise InvalidArgs("player / account_id / mention_id 一次只填一个；对比多人请分别调用")
    target = None
    if account_id:
        target = Query("id", str(account_id))
    elif player:
        target = _target([player]) if " " not in player else None
        if target is None:
            raise InvalidArgs("player 只填一个完整昵称，不能有空格")

    if kind == "unbind":
        return Command("unbind"), None, notes
    if kind == "bind":
        if target is None:
            raise InvalidArgs("绑定要填 player（用户给出的游戏昵称）或 account_id")
        return Command("bind", target), None, notes

    ship = _text(args, "ship")
    ship = _ship_name(ship) if ship else None
    given = [k for k in _FILTER_KEYS if args.get(k) not in (None, "")]
    if kind == "total" and ship:
        kind = "ship"  # “张三的大和” asked as 总战绩
    if kind == "recent" and (ship or given):
        kind = "battles"  # 最近 has no filters; the same question with conditions is a 场次 list

    if kind in ("total", "recent"):
        if given:
            notes.append(f"{action}不支持筛选，已忽略：{'、'.join(given)}")
        return Command(kind, target), mention or None, notes

    mode_word = _choice(args, "mode", MODES)
    if kind == "ship":
        if not ship:
            raise InvalidArgs("单船要填 ship（船名）")
        mode = None
        if mode_word in ("随机", "排位"):
            mode = MODES[mode_word][0]
        elif mode_word:
            notes.append(f"单船只区分随机/排位，已忽略 mode={mode_word}")
        ignored = [k for k in given if k != "mode"]
        if ignored:
            notes.append(f"单船是生涯数据，已忽略：{'、'.join(ignored)}")
        return Command("ship", target, Filter(ship=ship, mode=mode)), mention or None, notes

    period = hours = None
    date = _text(args, "date")
    if date:
        try:
            flt, extra = _parse_filter(date.split(), allow_index=False)
        except WowsError:
            flt, extra = None, None
        if (flt is None or extra is not None or flt.period is None
                or any(getattr(flt, k) not in (None, False) for k in ("ship", "mode", "team", "result", "sort",
                                                                       "asc", "limit", "index"))):
            raise InvalidArgs("date 写法：今天 / 昨天 / 前天 / 近N天 / 本周 / 10-15 / 10-12~10-15 / 今天下午")
        period, hours = flt.period, flt.hours
    start, end = _int(args, "start_hour", 0, 23), _int(args, "end_hour", 1, 24)
    if start is not None or end is not None:
        if hours is not None:
            raise InvalidArgs("date 里已经写了时段，start_hour/end_hour 不要再填")
        hours = (start or 0, 24 if end is None else end)
        if hours[0] >= hours[1]:
            raise InvalidArgs("start_hour 要小于 end_hour")
    if hours and period is None:
        period = ("ago", 0, 0)  # 只写时段 = 今天
    mode, team = MODES[mode_word] if mode_word else (None, None)
    result = _choice(args, "result", RESULTS)
    sort_word, order_word = _choice(args, "sort", SORTS), _choice(args, "order", ORDERS)
    limit, nth = _int(args, "limit", 1, 15), _int(args, "nth", 1, 99)
    if kind == "battles" and nth:
        notes.append("nth 只用于单场，已忽略；看第几把请用 action=单场")
        nth = None
    if kind == "battle" and limit:
        notes.append("limit 只用于场次，已忽略；单场用 nth 指定第几把")
        limit = None
    flt = Filter(period=period, hours=hours, ship=ship, mode=mode, team=team,
                 result=RESULTS[result] if result else None, sort=SORTS[sort_word] if sort_word else None,
                 asc=ORDERS[order_word] if order_word else False, limit=limit, index=nth)
    return Command(kind, target, flt), mention or None, notes


NEXT = {
    "ok": "卡片已发给用户，结合数据简短点评即可，不要逐项复述；“按时间第2/共2场”指发生顺序。",
    "help": "提问者还没绑定账号。请用户发送“/wows 绑定 游戏昵称”；只有用户给出昵称并要求绑定时，才用 action=绑定 代为绑定。",
    "not_bound": "提问者还没绑定账号。请用户发送“/wows 绑定 游戏昵称”，或告诉你要查的昵称；只有用户给出昵称并要求绑定时，才用 action=绑定 代为绑定。",
    "mention_not_bound": "被@的人还没绑定账号。可以请 TA 发送“/wows 绑定 游戏昵称”，或问到 TA 的游戏昵称后用 player 查询。",
    "candidates": "没有完全同名的玩家，options 是候选。把候选告诉用户并确认是哪一个，不要自己挑；确认后用对应的 account_id 查询。",
    "not_found": "亚服没有这个昵称。请用户确认完整拼写（区分大小写）或提供账号 ID。",
    "noship": "TA 没开过这艘船。options 里的船名明显就是用户说的那艘（错字、同音字）时可以用它重查一次，否则照实告诉用户，不要换成别的船。",
    "nomatch": "没有符合条件的场次。options 里的船名明显就是用户说的那艘时可以用它重查一次；否则告诉用户并建议放宽条件。只能查到开始记录之后的场次。",
    "tracking": "刚开始记录这个账号，之后打的场次才会出现；卡片已附生涯概要，不要再自动查总战绩。",
    "idle": "记录期间近 7 天没有战斗；卡片已附生涯概要，不要再自动查总战绩。",
    "hidden": "该玩家隐藏了战绩，查不到数据，照实告诉用户。",
    "newbie": "随机场次太少，卡片只显示了基础数据。",
    "quota": "记录名额已满，暂时不能记录这个账号，照实告诉用户（可以请管理员调大上限）。",
    "bound": "绑定成功，告诉用户之后打的每一场都会被记录。",
    "unbound": "已解除绑定。",
    "error": "查询失败，把 message 照实告诉用户，不要重试。",
    "invalid": "参数填写有误，按 message 修正后再调用一次。",
}


def result(outcome: Outcome, *, sent: bool, notes: list[str] | None = None) -> str:
    data: dict = {"status": outcome.status}
    if sent:
        data["card_sent_to_user"] = True
        data["data"] = outcome.caption or outcome.text
    else:
        data["card_sent_to_user"] = False
        data["message"] = outcome.text
    if outcome.options:
        data["options"] = outcome.options
    if notes:
        data["notes"] = notes
    data["next"] = NEXT.get(outcome.status, "")
    if not sent and outcome.status in ("ok", "newbie", "tracking", "idle", "nomatch", "noship", "hidden", "quota"):
        data["next"] = "用户还没看到结果，请据 message 用文字回答。" + data["next"].replace("卡片已发给用户，", "")
    return json.dumps(data, ensure_ascii=False)


def invalid(message: str) -> str:
    return result(Outcome("invalid", message), sent=False)
