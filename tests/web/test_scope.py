"""前端隔离回归测试（方案 §5.2）。

覆盖三件事：
  1. CSS 选择器作用域化（含踩过的三个坑：注释吞块 / 全局块泄漏 / 双重前缀）
  2. 涨跌色逐域保留（个性化选股本来就是反的，不许"统一"）
  3. id 命名空间化（etf 与 stock 都有 id="sidebar"）
"""

from __future__ import annotations

import re

import pytest

from web.shell.scope import (
    THEMES,
    scope_css,
    scope_html_fragment,
    theme_block,
)

Q = '"'


# ---------------------------------------------------------------------------
# 1. 选择器作用域化
# ---------------------------------------------------------------------------
def test_basic_selector_scoped():
    out = scope_css(".card{color:red}", "stock")
    assert out == "#app-stock .card{color:red}"


def test_descendant_and_comma_list():
    out = scope_css(".side .brand,.nav-item{h:1}", "etf")
    assert "#app-etf .side .brand" in out
    assert "#app-etf .nav-item" in out


def test_comment_does_not_swallow_next_block():
    """★ 回归：`.a{...}/* 注释 */.b{...}` —— 注释曾把 .b 的块头吞掉。"""
    out = scope_css(".a{x:1}/* ===== 布局 ===== */\n.b{y:2}", "etf")
    assert "#app-etf .a{x:1}" in out
    assert "#app-etf .b{y:2}" in out, f"注释吞掉了 .b 的选择器: {out}"


def test_global_blocks_promoted_not_scoped_twice():
    """★ 回归：body/html/*/:root 改写为作用域根，且不得二次叠前缀。"""
    out = scope_css("body{background:#fff}", "stock")
    assert "#app-stock{background:#fff}" in out
    assert "#app-stock #app-stock" not in out, f"双重前缀产生死规则: {out}"


@pytest.mark.parametrize("glob", ["body", "html", "*", ":root", "html,body"])
def test_all_global_forms_promoted(glob):
    out = scope_css(f"{glob}{{margin:0}}", "quant-lab")
    assert "#app-quant-lab{margin:0}" in out
    assert "#app-quant-lab #app-quant-lab" not in out


def test_no_empty_rule_left():
    out = scope_css("body{}\n.card{a:1}", "etf")
    assert "#app-etf{}" not in out
    assert "#app-etf .card{a:1}" in out


def test_media_query_recursed():
    out = scope_css("@media(max-width:900px){.side{width:100%}}", "etf")
    assert "@media(max-width:900px)" in out
    assert "#app-etf .side{width:100%}" in out


def test_keyframes_frames_untouched():
    css = "@keyframes spin{from{transform:rotate(0)}to{transform:rotate(1turn)}}"
    out = scope_css(css, "stock")
    assert "from{transform:rotate(0)}" in out
    assert "to{transform:rotate(1turn)}" in out
    assert "#app-stock from" not in out


def test_pseudo_and_attribute_selectors():
    out = scope_css('a::before{x:1}', "stock")
    assert "#app-stock a::before" in out
    out2 = scope_css('input[type="text"]{x:1}', "stock")
    assert "#app-stock input" in out2


def test_is_where_with_comma_not_split():
    """`:is(a,b)` 里的逗号不是选择器分隔符 —— 不能切错。"""
    out = scope_css(":is(.a,.b){x:1}", "stock")
    # :is 开头的伪类选择器保持原样（不前缀），但也不得被逗号切碎
    assert ".a,.b" in out or "#app-stock :is(.a,.b)" in out


def test_declaration_body_untouched():
    css = ".x{background:url(a.png);content:'{'}')"
    out = scope_css(css, "etf")
    assert "url(a.png)" in out


# ---------------------------------------------------------------------------
# 2. ★ 涨跌色逐域保留（最易被"优化"掉的点）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("domain,up,down", [
    ("quant-lab", "#D5423E", "#1D9E75"),   # 中国惯例：红涨绿跌
    ("etf", "#E0443C", "#16A34A"),         # 中国惯例
    ("stock", "#16A34A", "#DC2626"),       # ★ 美股惯例：绿涨红跌
])
def test_up_down_frozen_per_domain(domain, up, down):
    blk = theme_block(domain)
    assert f"--up:{up}" in blk, f"{domain} 涨色被改动"
    assert f"--down:{down}" in blk, f"{domain} 跌色被改动"


def test_stock_domain_is_actually_inverted():
    """★ 断言"颠倒"这件事本身 —— 防止有人好心把它们统一成红涨绿跌。"""
    stock = THEMES["stock"]
    assert stock.up == "#16A34A" and stock.down == "#DC2626"
    for other in ("quant-lab", "etf"):
        o = THEMES[other]
        assert o.up.startswith("#") and o.up != stock.up, (
            f"{other} 的涨色被改成了和 stock 一样 —— 涨跌色颠倒的坑又踩了")
        assert o.down != stock.down


def test_three_domains_have_distinct_root_ids():
    ids = {k: THEMES[k].root_id for k in THEMES}
    assert len(set(ids.values())) == len(ids)
    assert ids == {"quant-lab": "app-quant-lab", "etf": "app-etf",
                   "stock": "app-stock"}


# ---------------------------------------------------------------------------
# 3. id 命名空间化（etf 与 stock 都有 id="sidebar"）
# ---------------------------------------------------------------------------
def test_ids_namespaced_and_js_refs_updated():
    frag = '<div id="sidebar"></div><script>var x=document.getElementById("sidebar");</script>'
    out = scope_html_fragment(frag, "etf", wrap=False)
    assert f'id={Q}etf__sidebar{Q}' in out
    assert f'getElementById({Q}etf__sidebar{Q})' in out
    assert f'id={Q}sidebar{Q}' not in out, "裸 id 残留会与另两域相撞"


def test_two_domains_sidebar_do_not_collide():
    a = scope_html_fragment('<div id="sidebar">A</div>', "etf", wrap=False)
    b = scope_html_fragment('<div id="sidebar">B</div>', "stock", wrap=False)
    ia = re.search(r'id="([\w-]+)"', a).group(1)
    ib = re.search(r'id="([\w-]+)"', b).group(1)
    assert ia != ib, "两域的 sidebar 未隔离"


def test_fragment_wrapped_in_scope_root():
    out = scope_html_fragment("<div>x</div>", "quant-lab")
    assert f'id={Q}app-quant-lab{Q}' in out
    assert f'<style data-domain={Q}quant-lab{Q}>' in out


def test_style_block_carries_theme():
    out = scope_html_fragment("<style>.a{}</style><div></div>", "stock")
    assert "--up:#16A34A" in out
    assert "--down:#DC2626" in out


# ---------------------------------------------------------------------------
# 4. 真实模板冒烟（三域模板都能作用于域且不丢规则）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("domain,needle", [
    ("quant-lab", ".kpis"),
    ("etf", ".layout"),
    ("stock", ".hamburger"),
])
def test_real_template_smoke(tmp_path, domain, needle):
    tmpl = _load_template(domain)
    if tmpl is None:
        pytest.skip("模板源不可用（需 /tmp/qh/src 检出）")
    out = scope_html_fragment(tmpl, domain)
    assert needle in out, f"{domain} 的 {needle} 规则块丢失"
    # 所有 CSS 选择器都要带作用域前缀（@keyframes 帧与全局头除外）
    css = re.findall(r"<style[^>]*>(.*?)</style>", out, re.S)[0]
    # 先摘掉 @keyframes 整块（其 from/to 不是选择器）
    css = re.sub(r"@(?:-webkit-)?keyframes[^{]*\{.*?\}\s*\}", "", css, flags=re.S)
    css = re.sub(r"@[^{]+\{", "", css)
    bad = []
    for sel in re.findall(r"(?:^|\})\s*([^{}@]+?)\{", css):
        sel = sel.strip()
        if not sel:
            continue
        for part in sel.split(","):
            p = part.strip()
            if not p:
                continue
            if not p.startswith(f"#{THEMES[domain].root_id}"):
                bad.append(p)
    assert not bad, f"{domain} 有未作用域化的选择器: {bad[:8]}"


def _load_template(domain: str) -> str | None:
    import os

    root = "/tmp/qh/src"
    if not os.path.isdir(root):
        return None
    try:
        if domain == "quant-lab":
            from web.build import _extract_from_report_py
            return _extract_from_report_py(f"{root}/quant-lab/scripts/build_report.py")
        if domain == "etf":
            with open(f"{root}/red-dividend-strategy/index_template.html", encoding="utf-8") as f:
                return f.read()
        with open(f"{root}/stock-factor-engine/templates/index_template.html", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None
