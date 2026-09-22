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
# 同一类危险还有几种写法（都是「从 document 全局抓一把」）：
#   document.getElementsByClassName("a")        —— 直接就是类名，连选择器都不用写
#   document.body.querySelectorAll(".x")         —— body 也是全局
#   document.documentElement.querySelector(...)  —— 同上
#   document.getElementsByTagName("div")         —— 按标签抓，抓的是三家的 div
_GLOBAL_CLASS_QUERY_RE = re.compile(
    r'document\.(?:body|documentElement)?\.?getElementsByClassName\(\s*["\']([^"\']+)["\']')
_GLOBAL_SCOPED_QUERY_RE = re.compile(
    r'document\.(?:body|documentElement)\.querySelector(?:All)?\(\s*["\']([^"\']+)["\']')
_GLOBAL_TAG_QUERY_RE = re.compile(r'document\.getElementsByTagName\(\s*["\']([^"\']+)["\']')
# 事件委托：绑在 document 上的监听 + 在 handler 里用 `e.target.closest(".类名")`
# —— closest 是**向上**走的，跨过域边界轻而易举（三域都包在 `.qh-app` 里，
#   但 body/document 上的委托会先接到别域的点击）。
_DOC_DELEGATION_RE = re.compile(
    r'document\.addEventListener\(\s*["\'](click|mousedown|mouseup|keydown|keyup|input|change|focus)')
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


def _collisions(dom: str, selector: str) -> list[str]:
    """`selector` 整体能不能命中别的域的元素（判据：每个类名 token 都在对方源码里）。"""
    toks = _CLASS_RE.findall(selector)
    if not toks:
        return []
    return [o for o in _SCRIPTS
            if o != dom and all(t in _DECLARED[o] for t in toks)]


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
        for other in _collisions(domain, sel):
            pytest.fail(
                f"{domain} 的全局查询 {sel!r} 整体能命中 {other} 的元素 —— "
                "合并后会跨域误伤（stock 的 `.nav-item` 就是这么翻的车："
                "点短线域侧栏 → stock 四个 .view 的 .active 全被摘掉 → 空白页）。"
                "请改成从自己的根往下查（见 stock 模板顶部的 ROOT）。"
            )


@pytest.mark.parametrize("domain", sorted(_SCRIPTS))
def test_other_global_query_styles_are_guarded_too(domain):
    """★ 同一类地雷的另外三种写法（第一版用例只盖了 `document.querySelector*`）。

    `getElementsByClassName` / `document.body|documentElement.querySelector*`
    一样是「从全局抓一把」，用同一套碰撞判据。
    """
    src = _ALL[domain]
    found = []
    for m in _GLOBAL_CLASS_QUERY_RE.findall(src):
        found += [(t, sel) for t in m.split() for sel in ["." + t]]
    found += [(None, s) for s in _GLOBAL_SCOPED_QUERY_RE.findall(src) if "." in s]
    for _tok, sel in found:
        for other in _collisions(domain, sel):
            pytest.fail(f"{domain} 的全局查询（{sel!r}）能命中 {other} 的元素")


@pytest.mark.parametrize("domain", sorted(_SCRIPTS))
def test_domain_scripts_do_not_delegate_from_document(domain):
    """★ 绑在 `document` 上的点击/键盘委托同样跨域：三域的节点都在同一个 document 里，
    委托的 handler 会先接到别域的点击，再用 `e.target.closest(".类名")` **向上**走到
    别域的容器上。现在三域都是绑自己的容器（`$("watchList")` 这种），保持住。"""
    hit = _DOC_DELEGATION_RE.findall(_ALL[domain])
    assert not hit, (
        f"{domain} 把 {hit} 委托绑在了 document 上 —— 合并后三个域的节点都在同一个 "
        "document 里，会互相接住对方的点击。请绑到本域自己的容器上。"
    )


def test_no_global_element_queries_by_tag_name():
    """★ `document.getElementsByTagName("div")` 抓的是三家的 div，一律不许。"""
    for dom, src in _ALL.items():
        tags = _GLOBAL_TAG_QUERY_RE.findall(src)
        assert not tags, f"{dom} 按标签从 document 全局抓元素：{tags}"


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