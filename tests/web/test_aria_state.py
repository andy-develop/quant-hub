"""选中态语义（aria）回归（2026-09-22）。

背景：三域都用「加一个类名」表示「我被选中 / 当前就在这里」—— `on`/`active`/`cur`。
类名只对眼睛可见：键盘切了页、切了区间，屏幕阅读器里毫无提示。实测（补之前）
Tab 到域内导航按回车真的切了页，`aria-*` 一个都没有。

修法在壳层（`build.SHELL_JS` + `scope.ARIA_SELECTED`）：把类名翻译成
`aria-current`/`aria-pressed`/`aria-expanded`，幂等重算。

★ 这里点名锁一个踩过的坑：**载体不能用 id**。`#sidebar`（etf 与 stock 都有）
会被 `scope._namespace_ids` 重命名成 `#etf__sidebar`/`#stock__sidebar`，
写 `#sidebar` 时 `root.querySelector` 返回 null，规则**静默全空** ——
按钮永远 `aria-expanded="false"`，页面上看不出任何报错。
"""
from __future__ import annotations

import json
import re

import pytest

from web.build import SHELL_JS, _assemble
from web.shell.scope import ARIA_SELECTED

_DOMAINS = ("quant-lab", "etf", "stock")
_ARIA = ("aria-current", "aria-pressed", "aria-expanded")


def _shell(default: str = "quant-lab") -> str:
    return _assemble(body=[], head_assets="", health={}, default_domain=default)


def test_every_domain_declares_selected_state():
    assert set(ARIA_SELECTED) == set(_DOMAINS)
    for dom, rules in ARIA_SELECTED.items():
        assert rules, f"{dom} 没有选中态规则"


def test_rules_use_only_the_three_aria_attributes():
    """`aria-current`（导航当前位置）/ `aria-pressed`（开关）/ `aria-expanded`（展开）。

    别的一律不加 —— 半套 ARIA 模式比不做更糟（见 scope.py 里关于 tablist 的注释）。
    """
    for dom, rules in ARIA_SELECTED.items():
        for sel, cls, attr, val, carrier in rules:
            assert attr in _ARIA, f"{dom} 用了没约定的属性 {attr}"
            assert sel and val, f"{dom} 的规则不完整: {sel}"
            if cls is None:
                assert carrier, f"{dom} 的 {sel} 既没有类名也没有载体"
            else:
                assert carrier is None, f"{dom} 的 {sel} 既有类名又有载体（语义重复）"


def test_carriers_are_class_based_not_id():
    """★ 载体写 id 会在合并层被重命名掉，规则静默全空（这个坑真踩过）。

    `#sidebar` → `#stock__sidebar`：`root.querySelector("#sidebar")` 返回 null，
    按钮就永远 `aria-expanded="false"` 且没有任何报错。
    """
    for dom, rules in ARIA_SELECTED.items():
        for sel, cls, attr, val, carrier in rules:
            for part in ([sel] if cls else []) + ([carrier[0]] if carrier else []):
                assert not part.startswith("#"), (
                    f"{dom} 的 {part!r} 用了 id —— 跨域重名的 id 会被前缀化，"
                    "规则会静默失效；请改用类名"
                )


def test_disclosure_rules_point_at_the_real_carrier():
    """展开态的载体得是**状态长在它身上**的那个元素（抽屉开关长在 `.sidebar` 上）。"""
    exp = {r[0]: r[4] for r in ARIA_SELECTED["etf"] if r[2] == "aria-expanded"}
    assert exp.get(".menu-btn") == (".sidebar", "open")
    exp = {r[0]: r[4] for r in ARIA_SELECTED["stock"] if r[2] == "aria-expanded"}
    assert exp.get(".hamburger") == (".sidebar", "show")


def test_shell_ships_the_selected_state_machine():
    js = SHELL_JS
    assert "var SELECTED = __SELECTED__" in js, "占位符没写进壳层"
    assert 'setAttribute(attr, ' in js, "没有落 aria 属性"
    assert '"false"' in js, "未选中没写 false（摘掉属性会让开关语义消失）"
    assert "attributeFilter" in js and "class" in js, "没监听类名变化（选中态就是换类名）"
    # 顶部 tag 也得跟着走
    assert 'tb.setAttribute("aria-current"' in js


def _norm(x):
    """JSON 往返会把嵌套元组变列表，比较前两边都归一。"""
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_norm(i) for i in x]
    return x


def test_shell_selected_placeholder_is_replaced_with_the_live_rules():
    html = _shell()
    assert "__SELECTED__" not in html
    m = re.search(r"var SELECTED = (\{.*?\});", html, re.S)
    assert m, "壳层里没有 SELECTED 变量"
    got = json.loads(m.group(1))
    assert got == _norm(ARIA_SELECTED)


def test_top_tabs_are_wired_to_aria_current():
    """★ 顶部三个 tag 是页面级导航，必须有 `aria-current="page"`。"""
    html = _shell()
    assert 'aria-current", d === dom ? "page" : "false"' in html