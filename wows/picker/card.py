"""Ship picker card (HTML for the renderer) and its text fallback. All external strings are escaped."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from pathlib import Path

from .models import CATEGORIES, NATIONS, POOL_LABELS, ROMAN, TYPE_CODES, TYPES, Ship

ASSETS = Path(__file__).resolve().parent / "assets"
_MIME = {".png": "image/png", ".svg": "image/svg+xml"}
_ASSET_NATIONS = {"pan_asia": "panasia", "pan_america": "panamerica"}


@lru_cache(maxsize=1)
def _assets() -> dict[str, str]:
    """Card decorations (flags, ship-type and category icons), base64 in one JSON file."""
    try:
        data = json.loads((ASSETS / "card_assets.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def asset(name: str) -> str | None:
    raw = _assets().get(name)
    mime = _MIME.get(Path(name).suffix)
    return f"data:{mime};base64,{raw}" if isinstance(raw, str) and mime else None


@dataclass
class Picked:
    ship: Ship
    name: str  # display name (client name when known)
    english: str | None = None
    image: str | None = None  # data URI
    contour: bool = False


def tier_label(ship: Ship) -> str:
    roman = ROMAN[ship.tier] if 0 < ship.tier < len(ROMAN) else str(ship.tier)
    return f"★ {roman}" if ship.category == "super" else roman


def ship_line(p: Picked) -> str:
    s = p.ship
    return (f"{p.name}（{tier_label(s)} 级 {NATIONS.get(s.nation, s.nation)} {TYPES.get(s.type, s.type)}"
            f"，{CATEGORIES[s.category]}）")


def pick_text(picks: list[Picked], requester: str, pool: str, notes: list[str]) -> str:
    head = f"🎲 为 {requester} 抽到："
    body = ship_line(picks[0]) if len(picks) == 1 else "\n" + "\n".join(
        f"{i}. {ship_line(p)}" for i, p in enumerate(picks, 1))
    lines = [head + body, f"抽取范围：{POOL_LABELS.get(pool, pool)}"] + notes
    return "\n".join(lines)


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:960px;background:#0e0e10}
body{font-family:"Noto Sans SC","Noto Sans CJK SC","PingFang SC","Microsoft YaHei","Source Han Sans SC","WenQuanYi Micro Hei",sans-serif}
.card{--ac:255,255,255;--aa:0;position:relative;width:960px;overflow:hidden;border-radius:36px;color:#fff;
background:radial-gradient(90% 60% at 50% 62%,#1c3049 0%,#142840 45%,#0d1b2d 100%);
box-shadow:inset 0 0 0 1.5px rgba(var(--ac),calc(.08 + var(--aa)*.4))}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:220px;pointer-events:none;
background:radial-gradient(70% 100% at 85% 0%,rgba(var(--ac),calc(var(--aa)*.22)),transparent 70%)}
.card.premium{--ac:234,194,107;--aa:1}.card.special{--ac:79,185,201;--aa:1}.card.super{--ac:233,115,102;--aa:1}
.head{position:relative;padding:36px 44px 0}
.top{display:flex;justify-content:space-between;align-items:center;height:46px}
.for{display:flex;align-items:center;gap:10px;font-size:22px;color:rgba(255,255,255,.65);min-width:0}
.for b{color:#8fc2f5;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:520px}
.dot{width:8px;height:8px;flex:none;border-radius:50%;background:#5E9FE8}
h1{margin-top:14px;font-size:58px;font-weight:700;letter-spacing:1px;line-height:1.15;overflow-wrap:anywhere}
.en{margin-top:6px;font-size:22px;color:rgba(255,255,255,.5);letter-spacing:1.5px}
.tags{display:flex;gap:12px;margin-top:24px;flex-wrap:wrap}
.tag{display:inline-flex;align-items:center;gap:10px;height:46px;padding:0 18px;border-radius:23px;white-space:nowrap;
background:rgba(255,255,255,.08);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14);font-size:23px;color:#e8eef6}
.tier{min-width:62px;justify-content:center;font-family:Georgia,serif;font-weight:700;letter-spacing:1px;font-size:22px}
.tier.sup{color:#F09A8F;background:rgba(233,115,102,.15);box-shadow:inset 0 0 0 1px rgba(233,115,102,.55);font-family:inherit}
.tag img.ico{width:32px;height:32px;margin:0 -3px}.tag img.flg{width:37px;height:24px;border-radius:3px}
.cat{height:44px;font-weight:500;font-size:21px}.cat img{width:22px;height:22px}
.ship{position:relative;display:flex;align-items:center;justify-content:center;height:400px;margin-top:4px}
.ship img{width:880px;max-height:370px;object-fit:contain;
-webkit-mask-image:linear-gradient(90deg,transparent 0,#000 9%,#000 91%,transparent 100%),linear-gradient(180deg,transparent 0,#000 12%,#000 88%,transparent 100%);
-webkit-mask-composite:source-in;mask-composite:intersect}
img.contour{filter:brightness(0) invert(1);opacity:.85}
.ship.empty{height:60px}
.foot{position:relative;display:flex;justify-content:space-between;align-items:center;gap:20px;padding:22px 44px 28px;
border-top:1px solid rgba(255,255,255,.1);font-size:21px;color:rgba(255,255,255,.55)}
.foot .cta{color:rgba(255,255,255,.8);white-space:nowrap}.foot .cta b{color:#8fc2f5;font-weight:500}
.notes{padding:0 44px 24px;font-size:19px;color:rgba(255,255,255,.5);line-height:1.5}
.list{padding:18px 32px 8px;display:grid;gap:14px}
.row{display:flex;align-items:center;gap:20px;padding:14px 20px;border-radius:22px;background:rgba(255,255,255,.05);
box-shadow:inset 0 0 0 1px rgba(255,255,255,.08)}
.row.premium{box-shadow:inset 0 0 0 1.5px rgba(234,194,107,.55)}.row.special{box-shadow:inset 0 0 0 1.5px rgba(79,185,201,.55)}
.row.super{box-shadow:inset 0 0 0 1.5px rgba(233,115,102,.55)}
.row .pic{width:300px;height:120px;flex:none;display:flex;align-items:center;justify-content:center}
.row .pic img{max-width:300px;max-height:120px;object-fit:contain}
.row .info{min-width:0;flex:1}.row .nm{font-size:34px;font-weight:700;overflow-wrap:anywhere}
.row .tags{margin-top:10px;gap:10px}.row .tag{height:38px;font-size:19px;padding:0 14px}
.row .tag img.ico{width:26px;height:26px}.row .tag img.flg{width:30px;height:20px}
"""


def _icon(uri: str | None, cls: str) -> str:
    return f'<img class="{cls}" src="{uri}" alt="">' if uri else ""


def _tags(s: Ship, with_category: bool = False) -> str:
    nation = _ASSET_NATIONS.get(s.nation, s.nation)
    cat = s.category
    type_icon = f"shiptype_{TYPE_CODES.get(s.type, 'BB')}_{cat}.png"
    tags = [f'<span class="tag tier{" sup" if cat == "super" else ""}">{escape(tier_label(s))}</span>',
            f'<span class="tag">{_icon(asset(type_icon), "ico")}'
            f'{escape(TYPES.get(s.type, s.type))}</span>',
            f'<span class="tag">{_icon(asset(f"flag_{nation}.png"), "flg")}{escape(NATIONS.get(s.nation, s.nation))}</span>']
    if with_category:
        tags.append(f'<span class="tag cat">{_icon(asset(f"category_{cat}.svg"), "")}{CATEGORIES[cat]}</span>')
    return '<div class="tags">' + "".join(tags) + "</div>"


def _contour(p: Picked) -> str:
    return ' class="contour"' if p.contour else ""


def _english(p: Picked) -> str:
    en = (p.english or "").strip()
    if not en or en.casefold() in (p.name.casefold(), p.ship.name.casefold()):
        return ""
    return en.upper()


def pick_html(picks: list[Picked], requester: str, pool: str, notes: list[str], again: str = "选船") -> str:
    first = picks[0]
    foot = (f'<div class="foot"><span>抽取范围 · {escape(POOL_LABELS.get(pool, pool))}</span>'
            f'<span class="cta">发送 <b>「{escape(again)}」</b> 再抽一次</span></div>')
    note_html = f'<div class="notes">{"<br>".join(escape(n) for n in notes)}</div>' if notes else ""
    who = f'<div class="for"><span class="dot"></span>为 <b>@{escape(requester)}</b> 抽取</div>'
    if len(picks) == 1:
        s, cat = first.ship, first.ship.category
        en = _english(first)
        pic = (f'<div class="ship"><img{_contour(first)} src="{first.image}" alt=""></div>'
               if first.image else '<div class="ship empty"></div>')
        body = (f'<div class="head"><div class="top">{who}<span class="tag cat">'
                f'{_icon(asset(f"category_{cat}.svg"), "")}{CATEGORIES[cat]}</span></div>'
                f"<h1>{escape(first.name)}</h1>" + (f'<div class="en">{escape(en)}</div>' if en else "")
                + _tags(s) + "</div>" + pic)
        cls = cat
    else:
        rows = []
        for p in picks:
            img = f'<img{_contour(p)} src="{p.image}" alt="">' if p.image else ""
            rows.append(f'<div class="row {p.ship.category}"><div class="pic">{img}</div><div class="info">'
                        f'<div class="nm">{escape(p.name)}</div>{_tags(p.ship, with_category=True)}</div></div>')
        body = (f'<div class="head"><div class="top">{who}<span class="tag cat">共 {len(picks)} 艘</span></div></div>'
                f'<div class="list">{"".join(rows)}</div>')
        cls = ""
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{CSS}</style></head><body>'
            f'<div class="card {cls}">{body}{note_html}{foot}</div></body></html>')
