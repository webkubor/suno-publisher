"""浏览器驱动 —— 抓音乐人后台的唯一入口。

## 为什么不用 browser-harness / ego-browser

两个都不可用了：`browser-harness` 早已卸载，`ego-browser` 在本机也找不到
（`core/exe.py` 的 `find_exe("ego-browser")` 返回空）。留着调用它们的话，
脚本的失败方式是「找不到可执行文件」，而不是任何有信息量的报错。

现在走 `lite-browser`（本机在 `~/.local/bin`，`find_exe` 能找到）。

## 复用登录态这件事

抓后台必须有登录态 —— 匿名访问会被重定向到登录页，而那个页面**是正常渲染的**
（只是没有表格）。所以「抓不到表格」既可能是没登录、也可能是平台改版，
必须靠 URL 判据把两者分开，否则会去改选择器追一个不存在的问题。

登录态靠持久化 profile（lite-browser 默认 `default`）：在那个窗口里登录一次，
之后一直有效。

## 为什么不用 `--reuse`

lite-browser 的 `--reuse` 只采信 `session mark-login` 登记过的「已确认登录域」。
那是一层多余的人工绑定：没登记过就退化成正常打开、日志里写「未找到已确认的
登录态」——**看起来像没登录，其实只是没登记**。判断登录与否直接看页面内容就行，
不需要任何前置登记。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from core.exe import find_exe

_TIMEOUT = 60


class BrowserUnavailable(RuntimeError):
    """lite-browser 找不到 —— 不是「页面没数据」，装不上工具。"""


class NotLoggedIn(RuntimeError):
    """浏览器开着，但那个后台不让看 —— 多半是没登录。

    和「页面结构变了」严格分开：后者会抓到表格但列数对不上。
    混成一句话会让人以为是平台改版，然后去改选择器 —— 而真实原因是没登录。
    """


def _lite() -> str:
    exe = find_exe("lite-browser")
    if not exe:
        raise BrowserUnavailable(
            "找不到 lite-browser。抓后台必须靠它（复用登录态 + 跑页面 JS）。")
    return exe


def _run(args: list[str], timeout: int = _TIMEOUT) -> str:
    p = subprocess.run([_lite(), *args], capture_output=True, text=True,
                       timeout=timeout)
    if p.returncode != 0:
        raise BrowserUnavailable(f"lite-browser {' '.join(args[:2])} 失败："
                                 f"{(p.stderr or p.stdout)[-200:]}")
    return p.stdout or ""


def open_page(url: str) -> None:
    """打开页面。

    **不用 `--reuse`。** 它只采信 `session mark-login` 登记过的「已确认登录域」，
    而那个登记是额外一层人工绑定：没登记过就退化成正常打开，日志里写「未找到已确认
    的登录态」—— 看起来像「没登录」，其实只是没登记。

    登录与否**直接看页面内容是登录页还是后台**（见 `require_logged_in`），
    这个判据不需要任何前置登记，也不会把「没登记」误报成「没登录」。

    登录态本身靠持久化 profile（默认 `default`）：在那个窗口里登录一次，
    之后一直有效。
    """
    _run(["open", url], timeout=120)


def _parse_eval_output(out: str) -> Any:
    """从 lite-browser 的 eval 输出里取出真正的返回值。

    **对所有行都先试 `json.loads`**，不能只对 `{` / `[` / `"` 开头的试：
    `JSON.stringify(false)` 回显的是 `false`，而 Python 里字符串 `'false'`
    是**真值**。踩过 —— 登录探针拿它判断「页面上有没有登录入口」，永远得真，
    于是未登录也报「已登录」，然后去追一个不存在的数据问题。

    独立成函数是为了能单测：这个 bug 靠肉眼看输出发现不了（`'false'` 和
    `False` 打印出来很像）。
    """
    _NOISE = ("🌐", "✅", "📸", "ℹ️", "▶", "💥", "⚠️")
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if not line or line.startswith(_NOISE):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return line
    raise BrowserUnavailable(f"页面没返回任何值：{out[-200:]!r}")


def evaluate(expr: str, timeout: int = _TIMEOUT) -> Any:
    """在当前页面执行 JS 并取回值。

    页面上跑的东西**一律**通过它 —— 不要为了「少启一个进程」去引 playwright：
    那要自己管浏览器实例，而登录态就在这个浏览器的 profile 里，另起一个进程
    等于什么都抓不到。

    解析规则见 `_parse_eval_output`（尤其别把它改回「只认 JSON 开头的行」）。
    """
    return _parse_eval_output(_run(["eval", expr], timeout=timeout))


def probe_login(login_marker: str = "login") -> bool:
    """当前页面看着是登录页吗。

    两个判据，**任一命中就算没登录**：

    1. URL 里有登录标记（`login?targetUrl=...`）—— 网易云这类 SPA 的典型跳法
    2. 页面上找得到「登录」入口 —— 有的后台不跳 URL，直接在原地换成登录视图

    两个都不用额外登记，也不依赖平台特定的选择器。用「是否有登录入口」而不是
    「是否有表格」：**表格没有也可能只是平台改版了**，把改版当成没登录，
    会让人反复去登录一个本来就登录着的账号。
    """
    loc = str(evaluate("location.href") or "")
    if login_marker.lower() in loc.lower():
        return False
    # 页面上有没有明显的登录入口（导航栏那个「登录」链接/按钮）
    has_login_ui = evaluate("""JSON.stringify(
        [...document.querySelectorAll('a,button')]
          .some(e => /^\\s*登\\s*录\\s*$/.test(e.innerText || '')))""")
    return not has_login_ui


def require_logged_in(url_desc: str, login_marker: str = "login") -> None:
    """没登录就抛 —— 附带怎么做的说明，不给堆栈。"""
    if not probe_login(login_marker):
        raise NotLoggedIn(
            f"当前不是 {url_desc} 的登录态（页面被重定向到登录页）。\n"
            f"  在 lite-browser 打开的那个浏览器窗口里手动登录一次，之后脚本会复用它。\n"
            f"  脚本不代持登录态 —— 那是账号安全边界，不是脚本该操心的。")


def run_js_program(js: str, timeout: int = 600) -> Any:
    """喂一整段 JS 给页面（用于多步骤的页面操作）。

    `lite-browser eval` 接不了多行大程序时用这个：先存临时文件再让页面 import。
    这里保留入口是为了移植旧脚本时有地方落 ——
    注意 lite-browser 的 eval 不接受文件路径，所以真要用得改成分步 evaluate。
    """
    raise NotImplementedError(
        "lite-browser 的 eval 不接受文件路径；"
        "多步骤页面操作请拆成多次 evaluate() 调用。")


def close() -> None:
    """关掉会话释放 CDP 句柄。抓完记得调，否则浏览器一直挂着。"""
    try:
        _run(["close"], timeout=30)
    except Exception:
        pass          # 收尾失败不该盖掉真正的结果
