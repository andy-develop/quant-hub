"""键盘可达性回归（2026-09-22）。

背景：三域模板里点得动、却**聚焦不到**的元素不少 —— quant-lab 的域内导航是
**没有 `href` 的 `<a>`**（Chrome 里 `tabIndex` 返回 0 但 `focus()` 不生效），
etf/stock 的导航条目、区间 chip、因子筛选是 `<div>`/`<span>`。实测改前
quant-lab 整域只有 **7 个 Tab 落点**：域内导航（动量策略 / 量化黑盒）和 6 个
区间 chip 全在 Tab 序列之外，键盘用户切不了页。

修法是**壳层补**（不动三份模板）：`scope.KEYBOARD_REACH` 给出「点击目标」选择器，
`build.SHELL_JS` 给它们补 `tabindex="0"` + `role="button"`，并把 Enter/Space 映射成
`click()`（复用模板原有处理器），MutationObserver 兜住动态渲染的节点。

这里锁死：清单覆盖三域、选择器都带域作用域、产物里占位符已替换、shim 三件事俱在。
"""
from __future__ import annotations

import json
import re

import pytest

from web.build import SHELL_JS, _assemble
from web.shell.scope import KEYBOARD_REACH, THEMES

_DOMAINS = ("quant-lab", "etf", "stock")


def _shell(default: str = "quant-lab") -> str:
    """壳层 HTML（含已替换的 REACH JSON）。"""
    return _assemble(body=[], head_assets="", health={}, default_domain=default)


def test_reach_list_covers_every_domain():
    """★ 三域都要有清单，否则该域的键盘可达性静默消失。"""
    assert set(KEYBOARD_REACH) == set(_DOMAINS)
    for dom, sels in KEYBOARD_REACH.items():
        assert sels, f"{dom} 的键盘可达清单是空的"
        assert all(isinstance(s, str) and s.strip() for s in sels), f"{dom} 有空选择器"


def test_reach_list_targets_are_scoped_and_concrete():
    """★ 选择器写的是**域内相对选择器**（壳层按 `#app-<domain>` 再查），
    所以不能自带 `#app-` 前缀（带了就查不到），也不能是裸 `*` 这种大范围。"""
    for dom, sels in KEYBOARD_REACH.items():
        for s in sels:
            assert not s.startswith("#app-"), f"{dom} 的选择器自带作用域前缀: {s}"
            assert s not in ("*", "div", "span"), f"{dom} 的选择器太宽: {s}"


@pytest.mark.parametrize("dom", _DOMAINS)
def test_reach_selectors_still_match_the_templates(dom):
    """★ 模板改了类名/属性，清单会**静默落空**（不报错，只是又不可聚焦了）。

    这里按域去模板源码里找出该清单的证据：每个选择器的关键 token
    （类名或 `[data-x]`）必须在模板里出现过。
    """
    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    files = {
        "quant-lab": "domains/shortterm/scripts/build_report.py",
        "etf": "domains/etf/index_template.html",
        "stock": "domains/selected/templates/index_template.html",
    }
    path = os.path.join(root, files[dom])
    if not os.path.exists(path):
        pytest.fail(f"模板源不可用：{files[dom]}")
    with open(path, encoding="utf-8") as f:
        src = f.read()

    for s in KEYBOARD_REACH[dom]:
        token = re.sub(r"^.*?([.\[][\w-]+).*$", r"\1", s)
        token = token.lstrip(".[").rstrip("]")
        assert token in src, f"{dom} 清单里的 {s!r} 在模板里找不到（token={token!r}）"


def test_shell_js_ships_the_three_pieces():
    """★ shim 缺任何一件都会退化成「有 tabindex 但按 Enter 没反应」。"""
    js = SHELL_JS
    assert "tabindex" in js and "data-qh-key" in js, "没补 tabindex"
    assert "role" in js, "没补 role=button"
    assert "keydown" in js and "Enter" in js and '" "' in js, "没有 Enter/Space 映射"
    assert "preventDefault" in js, "Space 没 preventDefault（会滚页）"
    assert "MutationObserver" in js, "没兜住动态渲染出来的节点"


def test_shell_reach_placeholder_is_replaced_with_the_live_list():
    """★ `__REACH__` 漏替换 = 产物里一个语法错误 + 整段壳层 JS 不执行。"""
    html = _shell()
    assert "__REACH__" not in html
    m = re.search(r"var REACH = (\{.*?\});", html, re.S)
    assert m, "壳层里没有 REACH 变量"
    got = json.loads(m.group(1))
    assert got == {k: list(v) for k, v in KEYBOARD_REACH.items()}


@pytest.mark.parametrize("dom", _DOMAINS)
def test_reach_lookup_is_scoped_by_app_root(dom):
    """★ 只在**本域**根里查，别把选择器当全局用（etf 的 `.lv2` 和 stock 的
    `.nav-item` 会互相误伤，虽然类名不撞，但语义上必须按域隔离）。"""
    html = _shell()
    assert 'document.getElementById("app-" + dom)' in html
    assert THEMES[dom].root_id in ("app-quant-lab", "app-etf", "app-stock")