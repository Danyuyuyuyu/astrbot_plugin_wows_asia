"""Shared LLM-tool plumbing: argument errors, status hints and the JSON result contract.

Both `wows_query` (stats) and `wows_pick_ship` (random picker) return the same
{"status", "card_sent_to_user", "data"|"message", "options"?, "next"} shape; each tool owns
its own `next` wording through `next_map`.
"""
from __future__ import annotations

import json

from .commands import WowsError
from .outcome import Outcome


class InvalidArgs(WowsError):
    """The model filled the tool in wrong; tell it what to fix."""


# Default next-step wording for the stats tool. Status values are documented in outcome.Outcome.
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

_NOT_SENT_OK_STATUSES = ("ok", "newbie", "tracking", "idle", "nomatch", "noship", "hidden", "quota")


def result(outcome: Outcome, *, sent: bool, notes: list[str] | None = None,
           next_map: dict[str, str] | None = None) -> str:
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
    hints = next_map or NEXT
    data["next"] = hints.get(outcome.status, NEXT.get(outcome.status, ""))
    if not sent and outcome.status in _NOT_SENT_OK_STATUSES:
        data["next"] = ("用户还没看到结果，请据 message 用文字回答。"
                        + data["next"].replace("卡片已发给用户，", "").replace("卡片已发给用户：", ""))
    return json.dumps(data, ensure_ascii=False)


def invalid(message: str, *, next_map: dict[str, str] | None = None) -> str:
    return result(Outcome("invalid", message), sent=False, next_map=next_map)
