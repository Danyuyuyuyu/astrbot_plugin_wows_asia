"""把 NoneBot 版 nonebot-plugin-wows-asia 的数据（绑定、快照、逐场记录）迁移到 AstrBot 版。

用法（先停止 AstrBot）：
    python migrate_from_nonebot.py 旧的wows_asia.sqlite3 [新的wows_asia.sqlite3] [--force]

新路径默认是 AstrBot 的 data/plugin_data/astrbot_plugin_wows_asia/wows_asia.sqlite3
（在 AstrBot 根目录运行，或设置 ASTRBOT_ROOT）。
绑定的用户标识会从 NoneBot 的 “OneBot V11:QQ号” 改成 AstrBot 的 “aiocqhttp:QQ号”。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

RENAMES = {"OneBot V11:": "aiocqhttp:"}


def default_target() -> Path:
    root = Path(os.environ.get("ASTRBOT_ROOT") or Path.cwd())
    return root / "data" / "plugin_data" / "astrbot_plugin_wows_asia" / "wows_asia.sqlite3"


def migrate(src: Path, dst: Path, force: bool = False) -> int:
    if not src.is_file():
        raise SystemExit(f"找不到旧数据库：{src}")
    if dst.exists() and not force:
        raise SystemExit(f"目标已存在：{dst}\n确认要覆盖（AstrBot 版已有的数据会丢失）请加 --force")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as old, sqlite3.connect(dst) as new:
        old.backup(new)  # consistent copy even if the old bot is still running
        moved = 0
        for old_prefix, new_prefix in RENAMES.items():
            moved += new.execute(
                "UPDATE OR REPLACE binding SET user_key = ? || substr(user_key, ?) WHERE user_key LIKE ? || '%'",
                (new_prefix, len(old_prefix) + 1, old_prefix),
            ).rowcount
    return moved


def main(argv: list[str]) -> None:
    force = "--force" in argv
    paths = [a for a in argv if a != "--force"]
    if not paths or len(paths) > 2:
        raise SystemExit(__doc__)
    src = Path(paths[0])
    dst = Path(paths[1]) if len(paths) > 1 else default_target()
    moved = migrate(src, dst, force)
    print(f"已迁移到 {dst}，改写了 {moved} 个 QQ 绑定。重启 AstrBot 后生效。")


if __name__ == "__main__":
    main(sys.argv[1:])
