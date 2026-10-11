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
from pathlib import Path
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


def wait_for(expr: str, timeout: float = 30.0, interval: float = 1.0,
             desc: str = "") -> bool:
    """轮询到 `expr` 为真，或超时。

    **别用固定 sleep 等页面渲染完。** 2026-10-11 踩过：汽水控制台用
    `time.sleep(6)` 等，实测 5 秒时 `document.body` 已就绪、10 秒时那次 eval
    却回了空 —— 固定等待要么白等、要么赶不上，而两种失败长得一样
    （「读不到数据」），分不清是没登录、改版、还是没等够。

    返回是否等到；调用方据此给出**有信息量的**错误。
    """
    import time as _t
    deadline = _t.monotonic() + timeout
    last: Any = None
    while _t.monotonic() < deadline:
        try:
            last = evaluate(expr)
        except BrowserUnavailable:
            last = None          # 页面正在导航时 eval 会失败，不是致命错
        if last:
            return True
        _t.sleep(interval)
    if desc:
        print(f"  ⚠ 等「{desc}」超时（{timeout:.0f}s，最后一次得 {last!r}）")
    return False


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

    三个判据，**任一命中就算没登录**：

    1. URL 里有登录标记（`login?targetUrl=...`）—— 网易云这类 SPA 的典型跳法
    2. 页面上有**恰好是**「登录」的叶子节点 —— 汽水控制台就是这种：
       URL 不变、`<a>`/`<button>` 一个都没有，整个页面只有两个字「登录」
    3. 页面主体文本短得像一堵墙（≤ 20 字）且含「登录/扫码/授权」

    第 2 条原来只查 `a,button`，**漏掉了汽水**（实测 `链接数: 0`）——
    于是它被判成「已登录」，然后一路跑到「一条作品都没读到」，
    报错还写着「多半不是没登录」。这就是最该避免的误判：
    把「没登录」伪装成「平台改版」。

    用「有没有登录入口」而不是「有没有数据表」：**没有表格也可能只是改版了**，
    把改版当成没登录，会让人反复登录一个本来就登录着的账号。
    """
    loc = str(evaluate("location.href") or "")
    if login_marker.lower() in loc.lower():
        return False

    # 叶子节点里有没有恰好是「登录」的（不限于 a/button —— 汽水根本没有）
    hit = evaluate("""JSON.stringify(
        [...document.querySelectorAll('*')].some(e =>
          !e.children.length && /^\\s*登\\s*录\\s*$/.test(e.textContent || '')))""")
    if hit:
        return False

    # 兜底：整页文本短得像登录墙
    try:
        text = str(evaluate("document.body ? document.body.innerText : ''") or "")
    except BrowserUnavailable:
        text = ""
    short = len(text.strip()) <= 20
    if short and any(k in text for k in ("登录", "扫码", "授权", "Sign in", "Log in")):
        return False
    return True


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


# ── 页面操作原语 ─────────────────────────────────────────
# 移植发布脚本（原来跑在 ego-browser / browser-harness 上）需要的动作。
# 每条都对应一条踩过的坑，见 publish_qishui.py 的文件头。

def click(selector: str) -> bool:
    """点一个元素。

    **React 受控组件不认 `el.click()`** —— 在 `evaluate` 里调原生 click，
    状态不会变（AI 声明就是这么静默失败的）。所以这里走 lite-browser 的
    click，它发的是真实鼠标事件。

    选择器可以是 CSS，也可以用 `text=xxx` 按文本找（内部先解析成 CSS）。
    """
    if selector.startswith("text="):
        want = selector[len("text="):]
        ok = evaluate("""JSON.stringify((() => {
            const el = [...document.querySelectorAll('*')].find(e =>
              !e.children.length && (e.textContent || '').trim() === %s
              && e.getBoundingClientRect().width > 0);
            if (!el) return false;
            el.setAttribute('data-vf-click', '1');
            return true;
        })())""" % json.dumps(want))
        if not ok:
            return False
        try:
            _run(["click", "[data-vf-click='1']"], timeout=30)
            return True
        finally:
            evaluate("document.querySelector(\"[data-vf-click='1']\")"
                     "?.removeAttribute('data-vf-click')")
    try:
        _run(["click", selector], timeout=30)
        return True
    except BrowserUnavailable:
        return False


def insert_text(text: str) -> bool:
    """往当前焦点输入框整段插入文本。

    **中文要整段 insertText，不要逐字符键入**（规则 4）—— 逐字输入经常只落下
    第一个字（实测歌名变成「竹」），页面红字报错还看不出原因。
    """
    try:
        _run(["type", ":focus", text], timeout=30)
        return True
    except BrowserUnavailable:
        return False


def upload(selector: str, path: "str | Path") -> bool:
    """给文件输入框投一个本地文件。

    注意 `input.files` 在 React 表单里**不可信**（规则 1）：组件接收后会把
    files 清空、转存到自己 state。所以「这个槽传了没」不能靠它判断 ——
    要按页面上有没有显示文件名。
    """
    try:
        _run(["upload", selector, str(path)], timeout=120)
        return True
    except BrowserUnavailable:
        return False


def press(key: str) -> bool:
    try:
        _run(["press", key], timeout=20)
        return True
    except BrowserUnavailable:
        return False
