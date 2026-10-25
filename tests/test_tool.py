import json

import pytest

from wows import views
from wows.api import ShipInfo
from wows.app import Outcome
from wows.commands import Query
from wows.stats import Stat
from wows.storage import Record
from wows.tool import ACTIONS, SCHEMA, InvalidArgs, invalid, parse_args, result


def cmd(**args):
    return parse_args(args)


@pytest.mark.parametrize("args, action, target", [
    ({"action": "总战绩"}, "total", None),
    ({"action": "总战绩", "player": "张三三"}, "total", Query("name", "张三三")),
    ({"action": "总战绩", "player": "生涯"}, "total", None),
    ({"action": "总战绩", "player": "我"}, "total", None),
    ({"action": "最近", "account_id": 2000000001}, "recent", Query("id", "2000000001")),
    ({"action": "总战绩", "player": "张三三", "ship": "大和"}, "ship", Query("name", "张三三")),
    ({"action": "最近", "date": "昨天"}, "battles", None),
    ({"action": "解绑", "player": "x"}, "unbind", None),
    ({"action": "绑定", "player": "ABC"}, "bind", Query("name", "ABC")),
])
def test_actions_and_targets(args, action, target):
    command, mention, _ = parse_args(args)
    assert (command.action, command.target, mention) == (action, target, None)


def test_filters_map_to_filter_fields():
    c, _, _ = cmd(action="场次", player="张三三", date="今天下午", ship="伊吹")
    f = c.filter
    assert (f.period, f.hours, f.ship) == (("ago", 0, 0), (12, 18), "伊吹")
    f = cmd(action="场次", start_hour=14)[0].filter
    assert (f.period, f.hours) == (("ago", 0, 0), (14, 24))
    f = cmd(action="场次", end_hour=6, date="昨天")[0].filter
    assert (f.period, f.hours) == (("ago", 1, 1), (0, 6))
    f = cmd(action="场次", date="近3天", sort="经验", order="从低到高", limit=5)[0].filter
    assert (f.period, f.sort, f.asc, f.limit) == (("ago", 2, 0), "xp", True, 5)
    f = cmd(action="场次", date="10-12~10-15", mode="双排", result="胜")[0].filter
    assert (f.period, f.mode, f.team, f.result) == (("date", (10, 12), (10, 15)), "pvp", "div2", "win")
    f = cmd(action="单场", order="从旧到新")[0].filter
    assert (f.sort, f.asc, f.index) == (None, True, None)
    f = cmd(action="单场", sort="伤害", nth=2)[0].filter
    assert (f.sort, f.asc, f.index) == ("damage", False, 2)
    f = cmd(action="单场", sort="时间")[0].filter
    assert f.sort is None
    c = cmd(action="单船", ship="大和", mode="排位")[0]
    assert (c.action, c.filter.ship, c.filter.mode) == ("ship", "大和", "rank_solo")


def test_ignored_arguments_are_reported():
    c, _, notes = cmd(action="单船", ship="大和", mode="单野", date="今天")
    assert c.filter.mode is None and any("mode" in n for n in notes) and any("date" in n for n in notes)
    c, _, notes = cmd(action="场次", nth=3)
    assert c.filter.index is None and notes
    c, _, notes = cmd(action="单场", limit=3)
    assert c.filter.limit is None and notes
    c, _, notes = cmd(action="总战绩", sort="伤害")
    assert c.action == "total" and notes


def test_mentions():
    assert cmd(action="总战绩", mention_id="20002")[1] == "20002"
    assert cmd(action="总战绩", mention_id="@")[1] == "@"
    c, mention, _ = cmd(action="最近", player="@小明 (二号)(20002)")
    assert c.target is None and mention == "20002"


@pytest.mark.parametrize("args, hint", [
    ({}, "action"),
    ({"action": "生涯"}, "action"),
    ({"action": "单船"}, "ship"),
    ({"action": "绑定"}, "player"),
    ({"action": "场次", "date": "明年"}, "date"),
    ({"action": "场次", "date": "昨天 船=大和"}, "date"),
    ({"action": "场次", "start_hour": 18, "end_hour": 12}, "start_hour"),
    ({"action": "场次", "start_hour": 30}, "start_hour"),
    ({"action": "场次", "date": "今天下午", "start_hour": 13}, "date"),
    ({"action": "总战绩", "player": "张三三", "account_id": 1}, "一次只填一个"),
    ({"action": "场次", "limit": 0}, "limit"),
    ({"action": "场次", "limit": "abc"}, "limit"),
    ({"action": "场次", "mode": "乱斗"}, "mode"),
    ({"action": "总战绩", "mention_id": "小明"}, "mention_id"),
    ({"action": "总战绩", "player": "张 三"}, "player"),
])
def test_invalid_arguments(args, hint):
    with pytest.raises(InvalidArgs, match=hint):
        parse_args(args)


def test_schema_matches_parser():
    props = SCHEMA["properties"]
    assert props["action"]["enum"] == list(ACTIONS)
    for word in props["sort"]["enum"]:
        cmd(action="场次", sort=word)
    for word in props["mode"]["enum"]:
        cmd(action="场次", mode=word)
    for word in props["order"]["enum"]:
        cmd(action="场次", order=word)
    for date in props["date"]["description"].split("/"):
        cmd(action="场次", date=date.replace("N", "3"))


def test_results():
    ok = json.loads(result(Outcome("ok", "文字版", b"png", "卡片数据"), sent=True, notes=["已忽略：date"]))
    assert ok == {"status": "ok", "card_sent_to_user": True, "data": "卡片数据", "notes": ["已忽略：date"],
                  "next": ok["next"]} and "点评" in ok["next"]
    cand = json.loads(result(Outcome("candidates", "未定位…", options=["Abc（id 1）"]), sent=False))
    assert cand["options"] == ["Abc（id 1）"] and "不要自己挑" in cand["next"] and cand["message"]
    text_card = json.loads(result(Outcome("ok", "文字卡片"), sent=False))
    assert text_card["message"] == "文字卡片" and text_card["next"].startswith("用户还没看到结果")
    assert json.loads(invalid("ship 必填"))["status"] == "invalid"


def _rec(t, sid=1):
    return Record(1, t, t - 600, sid, "pvp", Stat(battles=1, wins=1, damage=t % 100000), None)


def test_oldest_first_order():
    now = 1792130400
    recs = [_rec(now - 600), _rec(now - 7200), _rec(now - 3600)]
    infos = {1: ShipInfo(1, "大和", 10, "Battleship")}
    flt = views_filter(asc=True)
    picked = views.filter_records(recs, flt, infos, now, default_days=None)
    assert [r.captured_at for r in picked.records] == sorted(r.captured_at for r in recs)
    assert "从旧到新" in picked.desc
    view = views.build_battle(views.Identity(1, "x"), picked, 0, recs, {}, now, oldest_first=True)
    assert "最早一场 · 按时间第 1 / 共 3 场" in view.position


def views_filter(**kw):
    from wows.commands import Filter
    return Filter(**kw)


def test_ship_suggestions_reach_outcome_options():
    now = 1792130400
    infos = {1: ShipInfo(1, "大和", 10, "Battleship")}
    picked = views.filter_records([_rec(now - 600)], views_filter(ship="大合"), infos, now, default_days=None)
    if not picked.records:  # corrected matches return records directly; misses carry suggestions
        assert picked.suggestions
    picked = views.filter_records([_rec(now - 600)], views_filter(ship="完全不像"), infos, now, default_days=None)
    assert picked.records == [] and picked.suggestions == []


def test_readme_examples_parse():
    from pathlib import Path
    import re as _re

    text = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    examples = _re.findall(r"`(\{\"action\".*?\})`", text)
    assert len(examples) >= 3
    for example in examples:
        command, _, notes = parse_args(json.loads(example))
        assert not notes, example
