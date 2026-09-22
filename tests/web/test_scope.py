"""前端隔离回归测试（方案 §5.2）。

覆盖三件事：
  1. CSS 选择器作用域化（含踩过的三个坑：注释吞块 / 全局块泄漏 / 双重前缀）
  2. ★ 视觉统一：三域令牌块逐字相同、涨跌全站红涨绿跌（2026-09 用户要求统一，
     早期版本是"逐域保留、个性化选股绿涨红跌"，已废弃）
  3. id 命名空间化（etf 与 stock 都有 id="sidebar"）
"""

from __future__ import annotations

import os
import re

import pytest

from web.shell.scope import (
    CHIP,
    CHIP_ON,
    COMPONENTS,
    NAV_MEDIA,
    PALETTE,
    THEMES,
    component_css,
    fragment_ids,
    nav_media_css,
    scope_css,
    scope_html_fragment,
    theme_block,
)

Q = '"'
_DOMAINS = ("quant-lab", "etf", "stock")


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
# 2. ★ 视觉统一（最易被"改回去"的点）
# ---------------------------------------------------------------------------
def test_palette_is_single_source_of_truth():
    """★ 统一调色板必须有涨跌两色，且是中国惯例。"""
    assert PALETTE["--up"] == "#D5423E"      # 红 = 涨
    assert PALETTE["--down"] == "#1D9E75"    # 绿 = 跌


@pytest.mark.parametrize("domain", ["quant-lab", "etf", "stock"])
def test_all_domains_share_identical_tokens(domain):
    """★ 三域的令牌块必须逐字相同 —— 外观统一就靠这个。"""
    blk = theme_block(domain)
    for k, v in PALETTE.items():
        assert f"{k}:{v};" in blk, f"{domain} 缺令牌 {k}"


def test_domain_tokens_are_byte_identical():
    """★ 断言"统一"这件事本身：三域令牌体一字不差（只差选择器头）。"""
    bodies = [theme_block(d).split("{", 1)[1] for d in ("quant-lab", "etf", "stock")]
    assert bodies[0] == bodies[1] == bodies[2], "三域令牌不一致：外观又分家了"


def test_up_down_unified_to_china_convention():
    """★ 个性化选股已从绿涨红跌翻向为红涨绿跌。

    早期这里断言的是"stock 必须与另两域相反"（防好心统一）。决策已反转，
    现在反过来锁死"必须统一"。
    """
    for d in THEMES:
        blk = theme_block(d)
        assert "--up:#D5423E" in blk, f"{d} 涨色不是红"
        assert "--down:#1D9E75" in blk, f"{d} 跌色不是绿"


def test_theme_block_is_emitted_after_domain_styles():
    """★ 回归：统一令牌块必须排在域样式**之后**。

    模板自己的 `:root{}` 被作用域化成 `#app-x{}`，与令牌块**同优先级**，
    谁在后面谁赢 —— 令牌块放前面会被模板原值静默盖掉（颜色看起来"根本没统一"）。
    """
    out = scope_html_fragment(
        "<style>:root{--up:#16A34A;--down:#DC2626}</style><div></div>", "stock")
    css = re.findall(r"<style[^>]*>(.*?)</style>", out, re.S)[0]
    marker = "/* ★ 统一调色板"
    assert marker in css, "没找到统一令牌块"
    # 模板原值（#16A34A 经值映射后为 #1D9E75）在前，统一令牌块必须在其后
    assert css.index(marker) > css.index("--up:#1D9E75"), (
        "统一令牌块排到了域样式前面，会被同优先级的模板 :root 静默覆盖")
    assert "--up:#D5423E;" in css[css.index(marker):], "令牌块里的涨色不是统一的红"


def test_hardcoded_colors_unified():
    """★ 模板里写死在选择器 / JS 里的色值（不走变量）也要统一。"""
    out = scope_html_fragment(
        '<style>.a{color:#16324F;border-color:#DDE8F4}</style>'
        '<script>var c="#2B6CB0";</script>', "etf")
    assert "#16324F" not in out and "#DDE8F4" not in out and "#2B6CB0" not in out
    assert PALETTE["--ink"] in out and PALETTE["--line"] in out
    assert PALETTE["--blue"] in out


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


# ---- ★ 2026-09 线上故障回归：全量前缀化把包装函数引用改漏了 ----
#   根因：`_namespace_ids` 原本给**所有** id 加域前缀，但只重写
#   `getElementById("字面量")`；`el('x')` / `$('x')` 这种间接引用改不到
#   -> 脚本块整块中断，两个域的图表全空、区间切换与多张表失效。
#   修法：收窄到跨域重名集合 + 补一轮包装函数实参改名。


def test_fragment_ids_collects_ids():
    assert fragment_ids('<i id="a"></i><b id="b"></b>') == {"a", "b"}
    assert fragment_ids("<div>x</div>") == set()


def test_rename_only_touches_duplicates():
    """★ 只在本域出现的 id 必须保持原名（这就是故障根因）。"""
    frag = ('<div id="sidebar"></div><div id="unique"></div>'
            '<script>var a=document.getElementById("sidebar");'
            'var b=document.getElementById("unique");</script>')
    out = scope_html_fragment(frag, "stock", wrap=False, rename={"sidebar"})
    assert f'id={Q}stock__sidebar{Q}' in out
    assert f'id={Q}unique{Q}' in out, "非重名 id 被改名了 -> 引用必然断裂"
    assert "stock__unique" not in out
    assert f'getElementById({Q}stock__sidebar{Q})' in out
    assert f'getElementById({Q}unique{Q})' in out


def test_wrapper_call_args_renamed():
    """`$("sidebar")` 不是 `getElementById("sidebar")` 字面量，要单独一轮才追得到。"""
    frag = ('<nav id="sidebar"></nav>'
            '<script>function $(id){return document.getElementById(id);}'
            '$("sidebar").classList.toggle("show");</script>')
    out = scope_html_fragment(frag, "stock", wrap=False, rename={"sidebar"})
    assert f'$({Q}stock__sidebar{Q})' in out, "包装函数实参没跟着改名"
    assert f'$({Q}sidebar{Q})' not in out


def test_arrow_wrapper_detected_but_not_enclosing_function():
    """★ 只认「体内直接 return getElementById」的转发函数。

    quant-lab 的 `initPage` 体内也出现 getElementById，但它是普通函数，
    名字不能被当成包装函数（否则会把 `initPage('bb_' …)` 的字符串也改名）。
    """
    frag = ("<script>function initPage(P){ const el = id => document.getElementById(P + id);"
            " el('modeSw'); } initPage('bb_');</script>")
    out = scope_html_fragment(frag, "quant-lab", wrap=False, rename={"modeSw"})
    assert f"el({Q}quant-lab__modeSw{Q})" in out, "箭头包装函数未被识别"
    # 未被触碰的文本保留原单引号 —— 这正是「没被误判」的证据
    assert "initPage('bb_')" in out, "普通函数被误判成包装函数"
    assert "initPage__" not in out and f"initPage({Q}bb_{Q})" not in out


def test_default_rename_is_backward_compatible():
    """rename 不给 = 老行为（单域片段：全部 id 加前缀），既有调用方不受影响。"""
    out = scope_html_fragment('<div id="anything"></div>', "etf", wrap=False)
    assert f'id={Q}etf__anything{Q}' in out


@pytest.mark.parametrize("domain", _DOMAINS)
def test_real_templates_only_sidebar_collides(domain):
    """真实模板实测：跨三域重名的 id 只有 `sidebar`（etf 与 stock 各一个）。"""
    from web.build import duplicate_ids

    frags = {}
    for d in _DOMAINS:
        tmpl = _load_template(d)
        if tmpl is None:
            pytest.fail("模板源不可用 —— 这条测试实际上没跑")
        frags[d] = tmpl
    assert duplicate_ids(frags) == {"sidebar"}, "跨域重名集合变了，需重审改名范围"


def test_real_templates_produce_no_duplicate_id():
    """★ 端到端：三域合并后 id 必须全局唯一，且每个引用都指向存在的 id。

    这条同时守住两个方向：改名不足（重名 -> getElementById 抢到别人的节点）
    与改名过度（引用断裂 -> 脚本块中断）。
    """
    from web.build import duplicate_ids

    frags = {}
    for d in _DOMAINS:
        tmpl = _load_template(d)
        if tmpl is None:
            pytest.fail("模板源不可用 —— 这条测试实际上没跑")
        frags[d] = tmpl
    dup = duplicate_ids(frags)
    doc = "\n".join(scope_html_fragment(frags[d], d, wrap=False, rename=dup)
                    for d in _DOMAINS)

    ids = re.findall(r'\bid="([\w-]+)"', doc)
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    assert not repeated, f"合并后仍有重名 id（会在浏览器里互相抢）: {repeated}"

    have = set(ids)
    ref = re.compile(r'(?:getElementById|(?<![\w$.])\$|(?<![\w$.])el)'
                     r'\(\s*["\']([\w-]+)["\']\s*\)')
    missing = sorted({m for m in ref.findall(doc) if m not in have})
    assert not missing, f"引用了不存在的 id（脚本会中断）: {missing}"


def test_fragment_wrapped_in_scope_root():
    out = scope_html_fragment("<div>x</div>", "quant-lab")
    assert f'id={Q}app-quant-lab{Q}' in out
    assert f'<style data-domain={Q}quant-lab{Q}>' in out


def test_style_block_carries_theme():
    out = scope_html_fragment("<style>.a{}</style><div></div>", "stock")
    assert "--up:#D5423E" in out
    assert "--down:#1D9E75" in out


# ---------------------------------------------------------------------------
# 4. 真实模板冒烟（三域模板都能作用于域且不丢规则）
# ---------------------------------------------------------------------------
# 未统一的旧色值：出现即说明硬编码色漏替换（CSS 或 JS 里）
_LEGACY_COLORS = ("#16A34A", "#DC2626", "#E0443C", "#16324F", "#1A2040",
                  "#F4F6FB", "#F6FAFE", "#2B6CB0", "#2563EB", "#5B7BA3")


@pytest.mark.parametrize("domain,needle", [
    ("quant-lab", ".kpis"),
    ("etf", ".layout"),
    ("stock", ".hamburger"),
])
def test_real_template_smoke(tmp_path, domain, needle):
    tmpl = _load_template(domain)
    if tmpl is None:
        pytest.fail("模板源不可用：既没有仓库自带 domains/，也没有 /tmp/qh/src 检出 —— 这条测试实际上没跑")
    out = scope_html_fragment(tmpl, domain)
    assert needle in out, f"{domain} 的 {needle} 规则块丢失"
    # 所有 CSS 选择器都要带作用域前缀（@keyframes 帧与全局头除外）
    css = re.findall(r"<style[^>]*>(.*?)</style>", out, re.S)[0]
    # 先摘注释：`/* 语义名 */` 夹在规则之间时会被当成选择器一部分
    # （scope_css 自己第一步也是剥注释 —— 扫描器必须同样处理，否则误报）
    css = _strip_comments(css)
    # 再摘掉 @keyframes 整块（其 from/to 不是选择器）
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


@pytest.mark.parametrize("domain", ["quant-lab", "etf", "stock"])
def test_real_template_fully_unified(domain):
    """★ 端到端：真实模板作用域化后，旧的不统一色值一个都不许残留（CSS+JS）。"""
    tmpl = _load_template(domain)
    if tmpl is None:
        pytest.fail("模板源不可用：既没有仓库自带 domains/，也没有 /tmp/qh/src 检出 —— 这条测试实际上没跑")
    out = scope_html_fragment(tmpl, domain)
    left = [c for c in _LEGACY_COLORS if c in out]
    assert not left, f"{domain} 仍有未统一的硬编码色: {left}"
    # 统一令牌必须在（且涨跌是中国惯例）
    assert "--up:#D5423E;" in out and "--down:#1D9E75;" in out


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 模板查找次序：① 仓库自带 domains/（合并后布局，默认）② 旧的外部检出根 /tmp/qh/src
#   ⚠️ 这里以前只认 /tmp/qh/src —— 在自带 domains/ 的仓库里必然返回 None，上层
#      `pytest.skip()` 于是把这条测试**静默跳过**，CI 全绿但一条都没跑（假绿）。
#      现在两边都找，且找不到时上层改为**失败**而非跳过。
_TEMPLATE_PATHS = {
    "quant-lab": ("domains/shortterm/scripts/build_report.py",
                  "quant-lab/scripts/build_report.py"),
    "etf": ("domains/etf/index_template.html",
            "red-dividend-strategy/index_template.html"),
    "stock": ("domains/selected/templates/index_template.html",
              "stock-factor-engine/templates/index_template.html"),
}


def _load_template(domain: str) -> str | None:
    from web.build import _extract_from_report_py

    for rel in _TEMPLATE_PATHS[domain]:
        for root in (_REPO_ROOT, "/tmp/qh/src"):
            p = os.path.join(root, rel)
            if not os.path.exists(p):
                continue
            if p.endswith(".py"):
                return _extract_from_report_py(p)
            with open(p, encoding="utf-8") as f:
                return f.read()
    return None


# ---------------------------------------------------------------------------
# 5. ★ 语义组件层：导航 / 标签的三域统一
# ---------------------------------------------------------------------------
# 三域导航是三套独立实现、类名几乎不重叠，只能靠"语义映射"统一（见
# scope.py 的 COMPONENTS）。这里锁死的是**决策**，不是实现细节。
def _rules(css: str) -> dict[str, str]:
    """把一段生成的 CSS 拆成 {选择器: 声明体}（组件层不含嵌套，够用）。

    同名选择器可能同时出现在基础样式和 @media 里 —— **保留第一次**出现的
    （基础样式在前），否则查 `.qh-topbar` 会拿到媒体查询里那条残规则。
    """
    out: dict[str, str] = {}
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _strip_comments(css)):
        out.setdefault(sel.strip(), body.strip())
    return out


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _rule_for(domain: str, keyword: str) -> str:
    """取某域组件层里「语义名含 keyword」那条规则的声明体。"""
    root = THEMES[domain].root_id
    rules = _rules(component_css(domain))
    for name, _props, targets in COMPONENTS:
        if keyword not in name or not targets.get(domain):
            continue
        sels = ",".join(f"#{root} {s}" for s in targets[domain])
        body = rules.get(sels)
        assert body is not None, f"{domain} 缺组件规则 {name}"
        return body
    raise AssertionError(f"{domain} 组件层里没有含「{keyword}」的规则")


@pytest.mark.parametrize("domain", _DOMAINS)
def test_component_selectors_all_scoped(domain):
    """★ 组件层选择器必须全部带本域作用域前缀 —— 漏一个就污染另两域。"""
    root = THEMES[domain].root_id
    css = _strip_comments(component_css(domain))
    bad = [s.strip() for s in re.findall(r"(?:^|\})\s*([^{}]+)\{", css)
           if not s.strip().startswith(f"#{root}")]
    assert not bad, f"{domain} 组件层有未作用域化的选择器: {bad[:5]}"


@pytest.mark.parametrize("domain", _DOMAINS)
def test_nav_container_width_is_192_everywhere(domain):
    """★ 用户口径：侧栏统一到 ETF 的宽度 192px。"""
    body = _rule_for(domain, "侧栏容器")
    assert "width:192px" in body, f"{domain} 侧栏宽度不是 192px: {body}"


@pytest.mark.parametrize("domain", _DOMAINS)
def test_nav_item_selected_is_black_on_white(domain):
    """★ 导航条目选中态：黑底白字（统一到 quant-lab 原样式）。

    quant-lab 的域内导航已升级为「标签」规范（见 NAV_ITEM_CHIP），名字不同、
    口径一致。
    """
    key = "短线域导航标签 · 选中" if domain == "quant-lab" else "导航条目 · 选中"
    body = _rule_for(domain, key)
    assert "background:var(--ink)" in body, f"{domain} 选中态不是黑底: {body}"
    assert "color:#fff" in body, f"{domain} 选中态不是白字: {body}"


def test_shortterm_nav_items_are_tags_not_plain_links():
    """★ 用户口径（2026-09）：短线域的「动量策略 / 量化黑盒」就是标签 ——

    必须和顶部 tag 吃同一份 CHIP 规范（白底 / 发丝边 / 4px 圆角），
    而不是 NAV_ITEM 那套无框条目。
    """
    body = _rule_for("quant-lab", "短线域导航标签")
    assert f"background:{CHIP['background']}" in body, f"短线域导航不是白底: {body}"
    assert f"border:{CHIP['border']}" in body, f"短线域导航没有边框: {body}"
    assert f"border-radius:{CHIP['border-radius']}" in body, f"圆角没跟标签: {body}"
    assert f"font-size:{CHIP['font-size']}" in body, f"字号没跟标签: {body}"
    assert "display:block" in body, f"侧栏里必须竖排: {body}"
    # 目视一眼：nav-item 不再出现在无框条目组里
    assert "quant-lab" not in dict(
        (n, t) for n, _p, t in COMPONENTS)["导航条目"]


@pytest.mark.parametrize("domain", _DOMAINS)
def test_ua_default_small_is_pulled_onto_the_scale(domain):
    """★ `<small>` 没人写字号时吃 UA 默认 10.8333px —— 不在 6 档字号阶里。

    实测（etf 的检查项注释，10 处）就卡在这个值上；组件层把它收到 fs-xs。
    `@media` 里的媒体查询不参与本用例（只看组件层）。
    """
    body = _rule_for(domain, "单位小字")
    assert "font-size:var(--fs-xs)" in body, f"{domain} 的 small 不在字号阶上: {body}"


def test_chip_is_white_then_black_on_white():
    """★ 可选中标签规范：白底黑字 → 选中黑底白字。"""
    assert CHIP["background"] == "var(--card)"
    assert CHIP["color"] == "var(--ink)"
    assert CHIP["border"].startswith("1px solid")
    assert CHIP_ON["background"] == "var(--ink)"
    assert CHIP_ON["color"] == "#fff"


def test_component_layer_brings_no_second_palette():
    """★ 组件层不得自带色值 —— 否则又成了"域外第二套配色"。

    （`#fff` 是选中态的纯白，不属于调色板取值，允许。）
    """
    forbidden = ("#16324F", "#2B4CB0", "#2B6CB0", "#16A34A", "#DC2626",
                 "#F0F6FD", "#EAF2FB", "#1F2430", "#B9BEC9")
    for d in _DOMAINS:
        css = component_css(d)
        left = [c for c in forbidden if c in css]
        assert not left, f"{d} 组件层混入硬编码色: {left}"


@pytest.mark.parametrize("domain", _DOMAINS)
def test_component_layer_lands_after_domain_styles(domain):
    """★ 回归：组件层必须排在域样式**之后**（同优先级靠文档顺序取胜）。"""
    tmpl = _load_template(domain)
    if tmpl is None:
        pytest.fail("模板源不可用，这条顺序回归没跑")
    out = scope_html_fragment(tmpl, domain)
    css = re.findall(r"<style[^>]*>(.*?)</style>", out, re.S)[0]
    # ★ 标记只用组件层独有的注释：`#app-etf .layout{` 这类选择器域内也有，
    #   拿它当标记会 index 到域样式那份 → 测试对 etf 静默失明（反向验证抓到的）。
    marker = "/* 壳层布局 */"
    assert marker in css, f"{domain} 找不到组件层标记"
    pal = "/* ★ 统一调色板"
    assert pal in css, f"{domain} 找不到令牌块"
    assert css.index(pal) > css.index(marker), (
        f"{domain} 组件层排到了令牌块之后，顺序假设被破坏")


@pytest.mark.parametrize("domain", _DOMAINS)
def test_nav_media_block_is_emitted_last(domain):
    """★ 响应式块必须**排最后**，且只含本域规则。

    组件层的基础规则与域内 @media 同优先级、又写在它们之后 —— 不把
    移动端抽屉样式重申在最后，就会被组件层静默顶掉（桌面样式泄漏到手机）。
    """
    tmpl = _load_template(domain)
    if tmpl is None:
        pytest.fail("模板源不可用，这条顺序回归没跑")
    block = nav_media_css(domain)
    # ★ 只发本域：早期版本把三域规则一起发，同一份规则在产物里重复 3 遍。
    #   注意「别的域 root_id 是否出现」抓不到这个 bug —— 串域时选择器会被打上
    #   **本域**前缀（`#app-quant-lab .layout{}`），前缀检查全绿而样式全错。
    #   所以要比对**展开后的选择器集合**是否恰好等于本域那套。
    sels = {s.strip() for s in re.findall(r"#\S+\s+([^{]+?)\{", block)}
    assert sels == set(NAV_MEDIA[domain]), (
        f"{domain} 响应式块的选择器不是本域那套：{sorted(sels)}")
    for other in _DOMAINS:
        if other != domain:
            assert f"#{THEMES[other].root_id} " not in block, (
                f"{domain} 的响应式块里混进了 {other} 的规则")
    out = scope_html_fragment(tmpl, domain)
    css = re.findall(r"<style[^>]*>(.*?)</style>", out, re.S)[0]
    assert block in css, f"{domain} 响应式块没进产物"
    marker = "/* ★ 统一调色板"
    assert css.index(block) > css.index(marker), (
        f"{domain} 响应式块排到了令牌块前面")
    # 产物里最后一段 @media(max-width:900px) 必须就是它（域内媒体查询都已过去）
    assert css.rindex("@media(max-width:900px){") >= css.index(block), (
        f"{domain} 后面还有别的 900px 媒体查询，会盖回来")


@pytest.mark.parametrize("domain", _DOMAINS)
def test_every_domain_has_nav_media_rules(domain):
    assert domain in NAV_MEDIA and NAV_MEDIA[domain], f"{domain} 没有移动端导航规则"
    css = nav_media_css(domain)
    root = THEMES[domain].root_id
    assert f"#{root} " in css, f"{domain} 的响应式规则没展开"


def test_stock_sidebar_is_converted_to_card():
    """★ stock 原本是 240px 占满整屏高的 fixed 侧栏，要换成浮动卡片。

    关键是**同时**把它主列的 `margin-left:240px` 抵消掉，否则左边留一条白带。
    """
    body = _rule_for("stock", "侧栏容器")
    assert "position:sticky" in body, "stock 侧栏没换成 sticky 卡片"
    assert "border-radius" in body
    layout = _rule_for("stock", "壳层布局")
    assert "margin:0 auto" in layout, "stock 主列没抵消 240px 的左边距"


@pytest.mark.parametrize("domain", _DOMAINS)
def test_real_templates_get_component_layer(domain):
    """★ 端到端：三域产物里必须真的带上组件层与响应式块。"""
    tmpl = _load_template(domain)
    if tmpl is None:
        pytest.fail("模板源不可用，这条端到端没跑")
    out = scope_html_fragment(tmpl, domain)
    root = THEMES[domain].root_id
    assert f"/* 侧栏容器 */" in out, f"{domain} 产物里没有组件层"
    assert f"#{root} .nav-item{{" in out or f"#{root} .sidebar{{" in out
    assert "@media(max-width:900px){" in out, f"{domain} 产物里没有响应式块"


# ---------------------------------------------------------------------------
# 6. 壳层：顶部导航标签与域内标签共用同一套规范
# ---------------------------------------------------------------------------
def test_shell_tab_uses_same_chip_spec():
    """★ 顶栏标签必须和域内标签同规范：白底黑字 → 选中黑底白字。"""
    from web.build import _shell_css

    css = _shell_css()
    rules = _rules(css)
    base = rules[".qh-topbar .qh-tab"]
    on = rules[".qh-topbar .qh-tab.on"]
    assert f"background:{PALETTE['--card']}" in base, f"标签默认不是白底: {base}"
    assert f"color:{PALETTE['--ink']}" in base, f"标签默认不是黑字: {base}"
    assert f"background:{PALETTE['--ink']}" in on, f"选中不是黑底: {on}"
    assert "color:#fff" in on, f"选中不是白字: {on}"


def test_shell_chip_shape_is_resolved_to_literals():
    """★ 回归：顶部标签的**形状**也必须落成实参。

    CHIP 里的 `border-radius:var(--r-sm)` / `font-size:var(--fs-md)` 在壳层
    （`#app-*` 之外）是无效值 —— 浏览器整条丢弃，标签变成方角 + 继承 14px。
    实测过：`getComputedStyle(.qh-tab).borderRadius === "0px"`。
    """
    from web.build import _shell_css

    css = _shell_css()
    assert "var(--" not in css, "壳层里还有解析不了的自定义属性"
    base = _rules(css)[".qh-topbar .qh-tab"]
    assert f"border-radius:{PALETTE['--r-sm']}" in base, f"顶部标签还是方角: {base}"
    assert f"font-size:{PALETTE['--fs-md']}" in base, f"顶部标签字号没落值: {base}"


def test_shell_token_prefix_collision_fixed():
    """★ 回归：`$CARD` 是 `$CARD2` 的前缀 —— 先换短的会把 `$CARD2` 换残。

    症状是产物里留下 `#FFFFFF2` 这种非法色值（浏览器静默丢弃该声明）。
    """
    from web.build import _SHELL_TOKENS, _shell_css

    assert {"$CARD", "$CARD2"} <= set(_SHELL_TOKENS), "用例前提失效：前缀对已不在"
    css = _shell_css(".a{background:$CARD2}.b{background:$CARD}")
    assert "$" not in css, "有令牌没被替换"
    assert "#FFFFFF2" not in css, "前缀冲突：$CARD2 被 $CARD 替换残了"
    assert css.index(PALETTE["--card-2"]) < css.index("background:#FFFFFF}")


def test_shell_css_has_no_leftover_token():
    """★ 任何 `$TOKEN` 漏替换都会留在产物里（CSS 静默丢弃，极难发现）。"""
    from web.build import SHELL_CSS, _shell_css

    assert "$" in SHELL_CSS, "用例前提失效：SHELL_CSS 已经没有任何令牌"
    css = _shell_css()
    # ⚠️ 不能把 re.findall(r"\$\w+", css) 直接写进 f-string —— 表达式内不允许反斜杠
    # （PEP 701 到 3.12 才放开；仓里 CI 跑 3.11，写了就是 SyntaxError 收集期炸）
    leftover = re.findall(r"\$\w+", css)
    assert not leftover, f"有令牌没被替换: {leftover}"


def test_shell_topbar_is_light_not_dark():
    """★ 顶栏由深色 `#1F2430` 改为配色板浅底。

    这是为了让「标签默认白底黑字」成立 —— 深色栏上白底标签才需要理由。
    若将来要回深色栏，这条测试要一起改（所以它在这儿挡着）。
    """
    from web.build import _shell_css

    css = _shell_css()
    bar = _rules(css)[".qh-topbar"]
    assert "#1F2430" not in css, "顶栏又是深色了，白底标签的前提不成立"
    assert f"background:{PALETTE['--bg']}" in bar
    assert "border-bottom" in bar
