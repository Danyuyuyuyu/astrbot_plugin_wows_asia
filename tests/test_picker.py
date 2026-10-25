"""Ship picker (随机选船): parser, randomizer, catalog sync, pools, owned lists, cards, names, admin."""
import hashlib
import json
import random
import struct

import pytest

from wows.app import WowsApp
from wows.commands import parse_command
from wows.picker import names_source
from wows.picker.models import PickError, Ship, ShipFilter
from wows.picker.parser import parse_pick
from wows.picker.randomizer import candidates, in_category, pick
from wows.picker.service import Picker, PickerSettings
from wows.ship_names import ShipNameMap
from wows.storage import Storage
from wows.tracker import Tracker

from .fakes import AID, Clock, FakeWG, make_api

PNG = b"\x89PNG\r\n\x1a\nfake"


class Renderer:
    def __init__(self):
        self.html = []

    async def render(self, html):
        self.html.append(html)
        return PNG

    async def close(self):
        pass


class Logger:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda text: self.lines.append((level, text))


def make(tmp_path, renderer=None, settings=None, fetch=None):
    fake, clock = FakeWG(), Clock()
    api = make_api(fake)
    storage = Storage(":memory:")
    names = ShipNameMap()
    picker = Picker(api, storage, renderer, data_dir=tmp_path, names=names, settings=settings or PickerSettings(),
                    clock=clock, rng=random.Random(7), logger=Logger(), fetch=fetch)
    tracker = Tracker(api, storage, clock=clock)
    app = WowsApp(api, storage, renderer, tracker=tracker, cache_seconds=0, clock=clock, picker=picker)
    return picker, app, fake, storage


def ship(sid, tier=10, nation="japan", kind="Battleship", premium=False, special=False):
    return Ship(sid, f"S{sid}", tier, nation, kind, premium, special)


# ---- parser ----
def test_parser_words_ranges_and_exclusions():
    r = parse_pick("日本 美国 战列 8-10 不要金币 3艘 全库")
    f = r.filter
    assert r.pool == "catalog" and r.count == 3
    assert f.nations == {"japan", "usa"} and f.types == {"Battleship"} and (f.tier_min, f.tier_max) == (8, 10)
    assert f.exclude_categories == {"premium"}
    assert parse_pick("").filter.tier_min == 5 and parse_pick("").filter.tier_max == 10
    assert parse_pick("超级战舰").filter.tier_min is None  # XI is outside the default V–X
    assert (parse_pick("VIII级以上").filter.tier_min, parse_pick("VIII级以上").filter.tier_max) == (8, 11)
    assert parse_pick("非航母 x2").filter.exclude_types == {"AirCarrier"} and parse_pick("x2").count == 2
    assert parse_pick("不要 潜艇").filter.exclude_types == {"Submarine"}
    assert parse_pick("８級").filter.tier_min == 8  # full-width digits, traditional 級
    assert parse_pick("我的").pool == "mine" and parse_pick("持有").pool == "owned"
    assert parse_pick("", default_tiers=(6, 8)).filter.tier_max == 8


@pytest.mark.parametrize("text, message", [
    ("12", "1–11"), ("我的 全库", "只能选一个"), ("abc", "看不懂"), ("不要", "后面要接"),
    ("日本 不要日本", "既要又不要"), ("8 9", "等级只能写一个"), ("9艘", "最多抽 5"), ("不要大和", "不要"),
])
def test_parser_errors(text, message):
    with pytest.raises(PickError, match=message):
        parse_pick(text)


# ---- randomizer ----
def test_categories_and_matching():
    tech, prem9, prem10, spec10, sup = (ship(1), ship(2, 9, premium=True), ship(3, 10, premium=True),
                                        ship(4, 10, special=True), ship(5, 11))
    assert in_category(tech, "tech") and not in_category(prem9, "tech")
    assert in_category(prem10, "premium") and in_category(spec10, "premium")  # X: 金币 includes 特种
    assert not in_category(ship(6, 9, special=True), "premium")
    assert in_category(sup, "super") and not in_category(sup, "tech")
    flt = ShipFilter(categories=frozenset({"premium"}), tier_min=9, tier_max=10)
    assert {s.ship_id for s in candidates([tech, prem9, prem10, spec10, sup], flt)} == {2, 3, 4}
    flt = ShipFilter(exclude_nations=frozenset({"japan"}))
    assert candidates([tech, ship(7, nation="usa")], flt)[0].ship_id == 7
    assert {s.ship_id for s in pick([tech, prem9, prem10], ShipFilter(), count=5)} == {1, 2, 3}
    with pytest.raises(PickError):
        pick([tech], ShipFilter(tier_max=5))
    assert pick([tech, prem9], ShipFilter(), allowed={2})[0].ship_id == 2


# ---- catalog ----
async def test_catalog_sync_and_client_names(tmp_path):
    picker, app, fake, storage = make(tmp_path)
    report = await picker.catalog.sync()
    assert report.count == 10 and report.added == 10 and report.game_version == "15.9.0"
    assert picker.names.name_for(1, "Yamato") == "大和"  # bundled client names via ship_id_str
    assert picker.names.english_for(1) == "Yamato"
    again = await picker.catalog.sync()
    assert (again.added, again.updated, again.removed) == (0, 0, 0)
    del fake.catalog["10"]
    fake.catalog["1"]["is_premium"] = True
    report = await picker.catalog.sync()
    assert (report.updated, report.removed, report.count) == (1, 1, 9)
    fake.catalog = {"1": fake.catalog["1"]}  # far fewer ships than before: refuse, keep the catalog
    with pytest.raises(Exception, match="不完整"):
        await picker.catalog.sync()
    assert picker.store.count() == 9
    await app.api.close()


async def test_catalog_skips_bad_rows_and_ensure_fresh(tmp_path):
    picker, app, fake, _ = make(tmp_path)
    fake.catalog["99"] = {"ship_id": 99, "name": "", "tier": 5, "nation": "japan", "type": "Cruiser"}
    report = await picker.catalog.ensure_fresh()  # empty -> full sync
    assert report.count == 10 and report.skipped == 1
    assert await picker.catalog.ensure_fresh() is None  # checked recently
    picker.clock.t += 7 * 3600
    assert await picker.catalog.ensure_fresh() is None  # same game version
    fake.game_version = "15.10.0"
    picker.clock.t += 7 * 3600
    assert (await picker.catalog.ensure_fresh()).game_version == "15.10.0"
    await app.api.close()


# ---- picking ----
async def test_pick_unbound_uses_catalog_and_renders_card(tmp_path):
    renderer = Renderer()
    picker, app, fake, _ = make(tmp_path, renderer)
    out = await picker.pick("qq:1", "日本 战列 10", "小明<b>")
    assert out.status == "ok" and out.image == PNG
    assert "为 小明<b> 抽到：大和" in out.text and "亚服全库" in out.text
    html = renderer.html[-1]
    assert "小明&lt;b&gt;" in html and "data:image/png;base64," in html
    assert "YAMATO" not in html  # the English line is hidden when it equals the API name
    assert any(c[0] == "image" and "large/PJSB018" in c[1] for c in fake.calls)
    out = await picker.pick("qq:1", "")
    assert out.status == "cooldown"
    await app.api.close()


async def test_pick_pools_for_bound_user(tmp_path):
    picker, app, fake, storage = make(tmp_path, settings=PickerSettings(cooldown=0))
    storage.bind("qq:1", AID, "Example_Player", 0)
    out = await picker.pick("qq:1", "")
    assert out.status == "ok" and "玩过的船" in out.text
    for _ in range(10):
        out = await picker.pick("qq:1", "x3")
        assert out.text.count("\n") >= 3 and not any(n in out.text for n in ("武藏", "密苏里", "白龙"))
    out = await picker.pick("qq:1", "航母")
    assert out.status == "empty" and "全库" in out.text
    assert (await picker.pick("qq:1", "航母 全库")).status == "ok"
    assert (await picker.pick("qq:2", "我的")).status == "not_bound"
    assert (await picker.pick("qq:1", "持有")).status == "no_owned"
    await app.api.close()


async def test_hidden_profile_falls_back_to_catalog(tmp_path):
    picker, app, fake, storage = make(tmp_path, settings=PickerSettings(cooldown=0))
    storage.bind("qq:1", AID, "Example_Player", 0)
    fake.account["hidden_profile"] = True
    out = await picker.pick("qq:1", "")
    assert out.status == "ok" and "隐藏了战绩，这次从全库抽" in out.text
    assert (await picker.pick("qq:1", "我的")).status == "hidden"
    await app.api.close()


async def test_owned_list_commands(tmp_path):
    picker, app, fake, storage = make(tmp_path, settings=PickerSettings(cooldown=0))
    assert (await picker.owned("qq:1", "")).status == "not_bound"
    storage.bind("qq:1", AID, "Example_Player", 0)
    out = await picker.owned("qq:1", "")
    assert "还没有持有清单" in out.text
    out = await picker.owned("qq:1", "添加 武藏，密苏里 Shimakaze")
    assert out.status == "ok" and "共 3 艘" in out.text
    out = await picker.owned("qq:1", "添加 不存在的船")
    assert out.status == "invalid" and "没有改动" in out.text
    assert picker.store.owned(AID) == {3, 5, 6}
    for _ in range(5):
        out = await picker.pick("qq:1", "")
        assert "持有清单" in out.text and any(n in out.text for n in ("武藏", "密苏里", "岛风"))
    out = await picker.owned("qq:1", "删除 岛风")
    assert picker.store.owned(AID) == {5, 6}
    out = await picker.owned("qq:1", "查看")
    assert "9 级：" in out.text and "（2 艘）" in out.text
    out = await picker.owned("qq:1", "导入")
    assert picker.store.owned(AID) == {1, 2, 3, 4} and "4 艘" in out.text
    await picker.owned("qq:1", "清空")
    assert picker.store.owned(AID) == set()
    assert "玩过的船" in (await picker.pick("qq:1", "")).text  # empty list -> played ships for 自动
    await picker.owned("qq:1", "关闭")
    assert picker.store.owned(AID) is None
    assert (await picker.owned("qq:1", "乱写")).status == "invalid"
    await app.api.close()


async def test_text_mode_skips_images(tmp_path):
    picker, app, fake, _ = make(tmp_path, renderer=None)
    out = await picker.pick(None, "全库")
    assert out.status == "ok" and out.image is None
    assert not any(c[0] == "image" for c in fake.calls)
    await app.api.close()


async def test_image_failure_falls_back_and_style(tmp_path):
    renderer = Renderer()
    picker, app, fake, _ = make(tmp_path, renderer, settings=PickerSettings(cooldown=0, image_style="contour"))
    await picker.pick(None, "日本 战列 10")
    assert any("contour/PJSB018" in c[1] for c in fake.calls if c[0] == "image")
    assert 'class="contour"' in renderer.html[-1]
    fake.image_status = 404
    picker.images._failed.clear()
    for f in tmp_path.joinpath("ship_images").glob("*.img"):
        f.unlink()
    out = await picker.pick(None, "日本 战列 10")
    assert out.status == "ok" and "<div class=\"ship empty\">" in renderer.html[-1]
    await app.api.close()


async def test_disabled_and_render_failure(tmp_path):
    class Broken:
        async def render(self, html):
            raise RuntimeError("no chromium")

    picker, app, fake, _ = make(tmp_path, Broken())
    out = await picker.pick(None, "")
    assert out.status == "ok" and out.image is None and "抽到" in out.text
    picker.settings.enabled = False
    assert (await picker.pick(None, "")).status == "disabled"
    await app.api.close()


# ---- commands / admin through the app ----
async def test_wows_subcommands_and_admin(tmp_path):
    picker, app, fake, storage = make(tmp_path, settings=PickerSettings(cooldown=0))
    assert parse_command("选船 日本 8").action == "pick" and parse_command("选船 日本 8").text == "日本 8"
    assert parse_command("持有 添加 大和").action == "owned"
    assert parse_command("船库更新").text == "更新" and parse_command("船库 状态").text == "状态"
    assert "抽到" in await app.handle("qq:1", "选船 全库")
    assert "只有管理员" in await app.handle("qq:1", "船库更新")
    assert "船库：10 艘" in await app.handle("qq:1", "船库状态")
    assert "船库已同步" in await app.handle("qq:1", "船库更新", is_admin=True)
    out = await app.handle("qq:1", "船库校验", is_admin=True)
    assert "后台补全" in out
    await picker._prefetch
    assert picker.images.stats()[0] == 10
    await picker.close()
    await app.api.close()


async def test_maintenance_respects_settings(tmp_path):
    picker, app, fake, _ = make(tmp_path, settings=PickerSettings(auto_update=False))
    await picker.maintenance()  # empty catalog is synced even with auto update off
    assert picker.store.count() == 10
    fake.game_version = "16.0"
    picker.clock.t += 30 * 86400
    await picker.maintenance()
    assert picker.store.meta("game_version") == "15.9.0"
    await app.api.close()


# ---- client names download ----
def mo(entries: dict[str, str]) -> bytes:
    keys = sorted(entries)
    ids = b"".join(k.encode() + b"\0" for k in keys)
    strs = b"".join(entries[k].encode() + b"\0" for k in keys)
    n = len(keys)
    o_tab, t_tab = 28, 28 + n * 8
    data_start = 28 + n * 16
    out_o, out_t, pos = [], [], data_start
    for k in keys:
        out_o.append((len(k.encode()), pos))
        pos += len(k.encode()) + 1
    for k in keys:
        out_t.append((len(entries[k].encode()), pos))
        pos += len(entries[k].encode()) + 1
    head = struct.pack("<IIIIIII", 0x950412DE, 0, n, o_tab, t_tab, 0, 0)
    return head + b"".join(struct.pack("<II", *x) for x in out_o) + b"".join(
        struct.pack("<II", *x) for x in out_t) + ids + strs


def test_mo_reader():
    raw = mo({"IDS_PJSB018": "大和号", "IDS_PJSB018_DESCR": "长文本", "IDS_OTHER": "x"})
    assert names_source.read_mo_ship_names(raw) == {"PJSB018": "大和号"}
    with pytest.raises(names_source.NamesError):
        names_source.read_mo_ship_names(b"not a mo file at all, definitely")


def upstream(zh: bytes, en: bytes, build="15.10.0_14000000", tamper=False):
    def obj(raw):
        digest = hashlib.sha256(raw).hexdigest()[:20]
        return digest, f"common/{digest[:2]}/{digest[2:]}"

    files = {"builds.toml": f'[[builds]]\nversion = "15.9.0"\ndir = "15.9.0_13357625"\n\n'
                            f'[[builds]]\nversion = "x"\ndir = "{build}"\n'.encode()}
    for lang, raw in (("zh_sg", zh), ("en", en)):
        _, path = obj(raw)
        files[f"{build}/translations/{lang}/LC_MESSAGES/global.mo"] = f"../../../../{path}".encode()
        files[path] = raw + (b"x" if tamper else b"")
    calls = []

    async def fetch(url, limit):
        calls.append(url)
        return files[url.removeprefix(names_source.RAW_BASE)]
    return fetch, calls


async def test_names_download_and_override(tmp_path):
    fetch, calls = upstream(mo({"IDS_PJSB018": "新大和"}), mo({"IDS_PJSB018": "New Yamato"}))
    picker, app, fake, _ = make(tmp_path, settings=PickerSettings(names_download=True), fetch=fetch)
    await picker.catalog.sync()
    text = await picker.update_names()
    assert "15.10.0_14000000" in text and picker.names.name_for(1, "x") == "新大和"
    assert picker.names.name_for(3, "x") == "岛风"  # bundled names still fill the rest
    assert "已是最新" in await picker.update_names()
    data = json.loads((tmp_path / "ship_names.json").read_text(encoding="utf-8"))
    assert data["build"] == "15.10.0_14000000"
    assert "自动下载（15.10.0_14000000）" in picker.status_text()
    await app.api.close()


async def test_names_download_rejects_bad_hash(tmp_path):
    fetch, _ = upstream(mo({"IDS_PJSB018": "新大和"}), mo({"IDS_PJSB018": "N"}), tamper=True)
    picker, app, fake, _ = make(tmp_path, fetch=fetch)
    assert "校验失败" in await picker.update_names()
    assert not (tmp_path / "ship_names.json").exists()
    await app.api.close()


def test_name_map_custom_file_wins(tmp_path):
    custom = tmp_path / "zh_sg.json"
    custom.write_text(json.dumps({"names": {"PJSB018": "自定义大和"}, "ship_ids": {"123": "PJSB018"}}),
                      encoding="utf-8")
    names = ShipNameMap(custom)
    assert names.name_for(123, "x") == "自定义大和"
    names.set_codes({1: "PJSD012"})
    assert names.name_for(1, "x") == "岛风" and "岛风" in names.names()
    mo_file = tmp_path / "global.mo"
    mo_file.write_bytes(mo({"IDS_PJSD012": "岛风改"}))
    names.path = mo_file
    assert names.name_for(1, "x") == "岛风改"
