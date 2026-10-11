"""浏览器驱动的纯逻辑自检（不需要真浏览器）。

跑法：`.venv/bin/python tests/test_browser.py`

只测**解析和判定**这类纯函数。真正驱动页面的部分（open/eval/click）必须在
真实浏览器上验 —— mock 一个 DOM 出来只会测到 mock 自己。

这里卡的是「不会报错、只会给出错误结论」的那类 bug：
解析把 `false` 变成真值、登录探针把「没登记」当成「没登录」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser  # noqa: E402
from core.browser import _parse_eval_output as parse  # noqa: E402


def test_bool_false_is_not_truthy():
    """`JSON.stringify(false)` 回显 `false` —— 必须解析成 False，不是字符串。

    这是踩过的 bug：解析只认 `{` / `[` / `"` 开头的行，于是 `'false'` 作为
    Python 字符串（**真值**）返回。登录探针拿它判断「页面上有没有登录入口」，
    永远得真 → 未登录也报「已登录」，然后去追一个不存在的数据问题。

    肉眼发现不了：`print('false')` 和 `print(False)` 长得很像。
    """
    assert parse("false") is False, "false 必须解析成 False，不能是字符串"
    assert parse("true") is True
    assert not parse("false"), "真值判断也必须是假 —— 探针就是靠这个"
    print("  ✓ JSON 布尔正确解析（false 不再被当成真值）")


def test_other_return_shapes():
    """对象 / 数组 / 数字 / 裸字符串 —— 四种形态都得对。"""
    assert parse('{"a": 1}') == {"a": 1}
    assert parse("[1, 2]") == [1, 2]
    assert parse("42") == 42
    assert parse("null") is None
    # 裸字符串（location.href / innerText 这类）：json.loads 失败，原样返回
    assert parse("https://music.163.com/#/login?targetUrl=x") == \
        "https://music.163.com/#/login?targetUrl=x"
    print("  ✓ 对象 / 数组 / 数字 / null / 裸字符串都解析正确")


def test_noise_lines_skipped():
    """lite-browser 会在结果前后打状态行，不能把它们当成返回值。"""
    out = "🌐 正在打开 x\n✅ 页面已就绪\n{\"ok\": true}\n  ▶ 执行中 phase=acting"
    assert parse(out) == {"ok": True}
    print("  ✓ 状态行被跳过，取到真正的返回值")


def test_parse_raises_when_nothing():
    """整段都是状态行时明确报错，而不是返回空串让调用方猜。"""
    try:
        parse("🌐 打开中\n✅ 就绪\n")
    except browser.BrowserUnavailable as e:
        assert "没返回任何值" in str(e)
        print("  ✓ 没有返回值时明确报错")
    else:
        raise AssertionError("全是状态行却没报错")


def test_backfill_unlinked_rows_are_not_written():
    """`link_state='unlinked'` 的行不该被写入播放量。

    这条防的是静默失真：给一条「还不知道是哪首歌」的记录填上播放量，
    看着有数据、实际答不了「哪首歌在赚钱」—— 而那是这个同步存在的唯一理由。

    这里只测判据本身（不连库、不起浏览器）：源脚本里那行过滤条件必须在。
    """
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "sync_netease_stats.py").read_text(encoding="utf-8")
    # 选行时排除 unlinked
    assert 'r["link_state"] != "unlinked"' in src, \
        "usable 的过滤条件里必须有 link_state 判断，否则会给认不出的记录写播放量"
    # 折算收益时只认 linked
    assert "AND link_state = 'linked'" in src, \
        "收益折算的 WHERE 也必须限定 linked —— 给 unlinked 算收益就是编数据"
    print("  ✓ unlinked 的行既不写播放量、也不折算收益")


if __name__ == "__main__":
    for fn in (test_bool_false_is_not_truthy, test_other_return_shapes,
               test_noise_lines_skipped, test_parse_raises_when_nothing,
               test_backfill_unlinked_rows_are_not_written):
        print(f"\n{fn.__name__}:")
        fn()
    print("\n全部通过")
