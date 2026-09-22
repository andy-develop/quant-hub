"""跨域隔离回归（2026-09-22）。

三域模板各自是**独立整页**写的（各带 `domains/*/scripts/build_html.py` 之类的
单独构建），合并进单页壳后它们共享**同一个 document**。于是域脚本里那些
从 document 全局查类名的写法会把手伸进别的域 —— 实测缺陷：

    点「短线策略」侧栏的「动量黑盒」→ 回到「个性化选股」是一片空白。

原因不是黑盒页，而是 stock 的脚本用 `document.querySelectorAll(".nav-item")`
绑点击 —— 短线域的侧栏条目**恰好也叫 `.nav-item`**，被一起绑上了 stock 的
handler。点一下就跑 `switchView(null)`，把 stock 四个 `.view` 的 `.active`
全摘掉（`switchView` 里的 `.view` 也是全局查），导航高亮同时消失。

修法是让域脚本从**自己的根**往下查（`ROOT`，见 stock 模板里 `currentScript`
+ `.qh-app` 那段）。这里锁死两件事：

1. 域脚本不许再出现「从 document 全局查类名」；
2. 如果出现，它整体必须**命不中**任何别的域（静态碰撞检测兜底）。
"""
from __future__ import annotations

import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 三域各自的脚本所在文件（etf/stock 是整页模板，quant-lab 是 build_report.py 里的 HTML_TEMPLATE）
_SCRIPTS = {
    "quant-lab": "domains/shortterm/scripts/build_report.py",
    "etf": "domains/etf/index_template.html",
    "stock": "domains/selected/templates/index_template.html",
}

_GLOBAL_QUERY_RE = re.compile(r'document\.querySelector(?:All)?\(\s*["\']([^"\']+)["\']')
_CLASS_RE = re.compile(r"\.([a-zA-Z][\w-]*)")


def _source(domain: str) -> str:
    path = os.path.join(_REPO_ROOT, _SCRIPTS[domain])
    if not os.path.exists(path):
        pytest.fail(f"模板源不可用：{_SCRIPTS[domain]}")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _declared_classes(src: str) -> set[str]:
    """源码里"用到过"的类名：`.foo{}` / `.foo` 选择器 + `class="a b"`。"""
    toks = set(_CLASS_RE.findall(src))
    for m in re.findall(r'class="([^"]*)"', src):
        toks.update(m.split())
    return toks


_ALL = {d: _source(d) for d in _SCRIPTS}
_DECLARED = {d: _declared_classes(s) for d, s in _ALL.items()}


@pytest.mark.parametrize("domain", sorted(_SCRIPTS))
def test_global_class_queries_cannot_match_another_domain(domain):
    """★ 核心不变量：域脚本从 document 全局查的类名选择器，**整体**不得命中外域。

    `document.getElementById` 是安全的（合并层会把跨域重名的 id 前缀化，
    见 `scope._namespace_ids`），但类名**不会**被前缀化 —— `.nav-item` 到哪儿都是
    `.nav-item`，于是 stock 的全局点击绑定把短线域的侧栏条目也绑上了。

    判据 = 选择器里**每个**类名 token 都在对方源码里出现过：
      · `.nav-item` 在 stock 脚本里 → 命中 quant-lab（它也有 `.nav-item`）★ 就是它
      · `.side .nav-item`（quant-lab 里的）→ 打不中 stock（stock 只有 `.sidebar`，没有 `.side`）
    """
    for sel in _GLOBAL_QUERY_RE.findall(_ALL[domain]):
        toks = _CLASS_RE.findall(sel)
        if not toks:
            continue
        for other in _SCRIPTS:
            if other == domain:
                continue
            if all(t in _DECLARED[other] for t in toks):
                pytest.fail(
                    f"{domain} 的全局查询 {sel!r} 整体能命中 {other} 的元素 —— "
                    "合并后会跨域误伤（stock 的 `.nav-item` 就是这么翻的车："
                    "点短线域侧栏 → stock 四个 .view 的 .active 全被摘掉 → 空白页）。"
                    "请改成从自己的根往下查（见 stock 模板顶部的 ROOT）。"
                )


def test_stock_root_falls_back_for_the_standalone_build():
    """★ stock 模板**也**要能单独构建（`domains/selected/scripts/build_html.py`），
    那里外面没有 `.qh-app` 包层 —— 所以 ROOT 必须有 `|| document` 的退路。"""
    src = _all_stock()
    assert "currentScript" in src and ".qh-app" in src, "ROOT 没有按 .qh-app 定位"
    assert re.search(r'closest\("\.qh-app"\)\)\s*\|\|\s*document', src), "ROOT 缺单独构建的退路"
    assert 'var ROOT' in src


def _all_stock() -> str:
    return _ALL["stock"]


@pytest.mark.parametrize("sel", [".view", ".nav-item", ".modal-overlay", ".rec-item"])
def test_stock_queries_are_rooted(sel):
    """★ stock 的五个全局查已全部收到 ROOT 下（`.view`/`.nav-item` 是翻过车的两个）。"""
    src = _all_stock()
    assert f'ROOT.querySelectorAll("{sel}")' in src, f"{sel} 没从 ROOT 查"
    assert f'document.querySelectorAll("{sel}")' not in src, f"{sel} 还在全局查"