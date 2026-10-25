import pytest

from scripts.migrate_from_nonebot import migrate
from wows.storage import Storage


def test_migrate_rewrites_qq_bindings(tmp_path):
    src = tmp_path / "old.sqlite3"
    old = Storage(src)
    old.bind("OneBot V11:10001", 2001, "张三三", 1)
    old.bind("Telegram:42", 2002, "李四四", 1)
    old.close()
    dst = tmp_path / "plugin_data" / "new.sqlite3"
    assert migrate(src, dst) == 1
    new = Storage(dst)
    assert new.binding("aiocqhttp:10001") == (2001, "张三三")
    assert new.binding("OneBot V11:10001") is None
    assert new.binding("Telegram:42") == (2002, "李四四")
    new.close()
    with pytest.raises(SystemExit):
        migrate(src, dst)
    assert migrate(src, dst, force=True) == 1
