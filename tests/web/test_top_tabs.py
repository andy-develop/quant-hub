"""顶部域切换 = 真 tablist（2026-09-22 三补）。

背景：顶部三个 tag 只是「长得像标签的按钮」—— 屏幕阅读器读到三个孤立按钮，
听不出这是一组、也不知道哪个是当前。补法是标准 tablist 模式，但**手动激活**：

    role=tablist  →  direction: ArrowLeft/Right/Up/Down, Home/End 移动焦点
    role=tab     →  Enter/Space 才真切域（标签本身是原生 `<button>`，不用额外映射）
    role=tabpanel →  aria-labelledby 指回标签

为什么手动激活而不是「焦点一动就切」：切域要重建 ECharts（隐藏时初始化算 0 宽，
切回来必须 resize）。自动激活时按住右箭头会连切两域、每个都重画一遍图 ——
用户只是想看看旁边那个域叫什么名字，代价不该是两轮重绘。
"""
from __future__ import annotations

import re

import pytest

from web.build import DOMAINS, SHELL_JS, _assemble, _domain_section


def _shell(default: str = "quant-lab") -> str:
    """带真面板的壳层（面板 id 必须存在，否则 aria-controls 指向空气）。"""
    body = [_domain_section(k, f'<div class="qh-app" data-domain="{k}"></div>')
            for k in DOMAINS]
    return _assemble(body=body, head_assets="", health={}, default_domain=default)


def _tabs(html: str) -> list[str]:
    return re.findall(r"<button[^>]*class=\"qh-tab\"[^>]*>", html)


def test_top_tabs_are_a_tablist():
    html = _shell()
    assert re.search(r'<nav class="qh-tabs" role="tablist" aria-label="[^"]+"', html), \
        "顶部 tag 容器没标 role=tablist / 没给组名"
    tabs = _tabs(html)
    assert len(tabs) == len(DOMAINS)
    for t in tabs:
        assert 'role="tab"' in t, f"标签没标 role=tab: {t}"
        assert 'aria-selected="' in t, f"标签没 aria-selected: {t}"


@pytest.mark.parametrize("default", list(DOMAINS))
def test_exactly_one_tab_is_selected_and_tabbable(default):
    """★ roving tabindex：只有当前这一枚在 Tab 序列里，其余靠方向键进。

    三枚都可 Tab（或都不可）会破坏「整组算一个落点」的规矩。
    """
    tabs = _tabs(_shell(default))
    sel = [t for t in tabs if 'aria-selected="true"' in t]
    zero = [t for t in tabs if 'tabindex="0"' in t]
    assert len(sel) == 1 and len(zero) == 1, f"选中 {len(sel)} 个 / 可 Tab {len(zero)} 个"
    assert default in sel[0] and default in zero[0], "选中的不是 default_domain 那一枚"
    for t in tabs:
        if t not in sel:
            assert 'aria-selected="false"' in t and 'tabindex="-1"' in t, \
                f"未选中的标签要显式写 false/-1: {t}"


def test_tabs_and_panels_reference_each_other():
    """★ aria-controls / aria-labelledby 必须**指向真的 id**：指错了 AT 直接哑掉
    （指向不存在的 id 不报错，只是念不出面板）。"""
    html = _shell()
    ids = set(re.findall(r'\bid="([\w-]+)"', html))
    for k in DOMAINS:
        assert f'aria-controls="qh-domain-{k}"' in html, f"{k} 的标签没指向面板"
        assert f"qh-domain-{k}" in ids, f"{k} 的面板 id 不存在"
        panel = re.search(rf'<section[^>]*id="qh-domain-{k}"[^>]*>', html)
        assert panel, f"{k} 没有面板"
        assert 'role="tabpanel"' in panel.group(0), f"{k} 面板没标 role=tabpanel"
        assert f'aria-labelledby="qh-tab-{k}"' in panel.group(0), \
            f"{k} 面板没指回它的标签"


def test_domain_section_carries_the_hash_owner_marker_before_its_content():
    """★ `__QH_DOMAIN` 标记必须在域内容**之前**：三域的 hashchange 监听器都是在

    自己那 `<script>` 里同步注册的，晚一步就归档到别人名下（见 test_hash_isolation）。
    """
    sec = _domain_section("etf", '<style></style><div class="qh-app"></div><script>1</script>')
    assert sec.index('window.__QH_DOMAIN="etf"') < sec.index("<style>")
    assert sec.index('window.__QH_DOMAIN="etf"') < sec.index("<script>1</script>")


def test_shell_js_wires_the_arrow_keys():
    """★ 补了 role 却没有方向键 = 只做了半套 ARIA（比不做更糟）。"""
    js = SHELL_JS
    for k in ("ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown", "Home", "End"):
        assert k in js, f"没处理 {k}"
    assert '[role="tablist"]' in js and '[role="tab"]' in js, "没按角色取标签"
    assert "preventDefault" in js, "方向键没 preventDefault（会滚页）"
    assert 'setAttribute("tabindex"' in js, "没做 roving tabindex"


def test_arrow_keys_move_focus_without_switching_domain():
    """★ 手动激活：方向键那段里**不许**出现 `show(`。

    （自动激活的实现就是在这里调 show —— 这条用例是那件事的守卫。）
    """
    js = SHELL_JS
    block = js[js.index("function bootTabs"):js.index("function bootHash")]
    assert "show(" not in block, "方向键直接切域了 —— 改成手动激活（回车/空格才切）"
    assert ".focus()" in block


def test_tabs_are_native_buttons():
    """★ 原生 `<button>` 才有「回车/空格即激活」和正确角色，不用再补 shim。"""
    html = _shell()
    for t in _tabs(html):
        assert t.startswith("<button"), f"顶部标签不是原生按钮: {t}"

    js = SHELL_JS
    assert 'NATIVE = /^(A|BUTTON|INPUT' in js, "键盘 shim 不该再给 button 补 tabindex"