"""三域前端作用域化：把三个独立单文件 HTML 合成一个自包含单页壳。

产物 `web/dist/index.html` 要求：
  - 单文件，可离线打开（echarts 已本地化，不依赖 CDN）
  - 三域各自 CSS 作用域隔离，互不污染
  - 左侧一级导航切换三域（顶部锚点切换域内页面）
  - ★ 视觉风格全站统一：配色令牌与涨跌色都来自 `web/shell/scope.py` 的 PALETTE
    （含红涨绿跌），本文件不再自己写一份色值

用法：
    python -m web.build --src <三仓根目录> --out web/dist/index.html
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from web.shell.scope import (  # noqa: E402
    CHIP, CHIP_HOVER, CHIP_ON, PALETTE, THEMES, decl, fragment_ids,
    scope_html_fragment,
)

__all__ = ["build", "SHELL_CSS", "NAV", "DOMAINS", "inject_payloads",
           "duplicate_ids"]

# 一级导航（域级）
NAV = [
    ("quant-lab", "短线策略", "动量 + 量化黑盒"),
    ("etf", "ETF 策略", "红利低波跟投"),
    ("stock", "个性化选股", "因子智能选股"),
]

DOMAINS = [k for k, _, _ in NAV]

# ---------------------------------------------------------------------------
# 源码根解析：优先读合并后的 domains/<域>（自包含，无需再检出三个老仓），
# 回退到旧仓目录名（外部检出布局 src/quant-lab 等）。两种布局都能构建。
# ---------------------------------------------------------------------------
SRC_ALIASES = {
    "quant-lab": ("shortterm", "quant-lab"),
    "red-dividend-strategy": ("etf", "red-dividend-strategy"),
    "stock-factor-engine": ("selected", "stock-factor-engine"),
}


def _base(src_root: str, legacy: str) -> str:
    """返回某个域在 src_root 下真实存在的源码目录。"""
    for cand in SRC_ALIASES[legacy]:
        p = os.path.join(src_root, cand)
        if os.path.isdir(p):
            return p
    # 都不存在 → 返回首选名，让上层的 open() 抛出可读的错误路径
    return os.path.join(src_root, SRC_ALIASES[legacy][0])


# ---------------------------------------------------------------------------
# payload 注入：把 `common.payload.adapters` 的统一信封塞进各域模板
# ---------------------------------------------------------------------------
# 各域模板的原始占位符（不等同，逐域适配）
#
#   quant-lab : const MODES = __DATA__;  const MODES_BB = __DATA_BB__;
#   etf       : <script id="PAYLOAD" type="application/json">__PAYLOAD__</script>
#   stock     : /*__STOCK_UNIVERSE__*/[]   /*__REAL_FACTORS__*/{}
#               /*__GEN_TIME__*/  /*__DATA_DATE__*/
def inject_payloads(fragment: str, domain: str,
                    envelopes: dict[str, dict] | None = None) -> str:
    """把统一信封的数据写回某个域的 HTML 片段。

    ★ 无对应数据时也**把占位符替换成合法的空值**（{} / []），而不是原样留着 ——
    留着 `__DATA__` 会让前端 `const MODES = __DATA__;` 抛 ReferenceError、整域脚本崩、页面空白。
    替换成空值后该域走模板自带的空态，不崩、也不塞假数据。

    envelopes : {(domain, variant): envelope}
    """
    envelopes = envelopes or {}
    if domain == "quant-lab":
        m = envelopes.get(("quant-lab", "momentum"))
        bb = envelopes.get(("quant-lab", "blackbox"))
        # ★ 即便无数据也必须把占位符替换成合法 JS（{}）：否则 `const MODES = __DATA__;`
        #   抛 ReferenceError 让短线域脚本整体崩溃、页面空白（合并页默认落该域时即"完全没数据"）
        mval = json.dumps(m.get("payload", {}) if m else {}, ensure_ascii=False)
        bbval = json.dumps(bb.get("payload", {}) if bb else {}, ensure_ascii=False)
        fragment = fragment.replace("const MODES = __DATA__;", "const MODES = " + mval + ";")
        fragment = fragment.replace("const MODES_BB = __DATA_BB__;", "const MODES_BB = " + bbval + ";")
        fragment = fragment.replace("__DATA_BB__", bbval)   # 兜底裸占位（先 BB 再 DATA，避免前缀误伤）
        fragment = fragment.replace("__DATA__", mval)
        return fragment

    if domain == "etf":
        div = envelopes.get(("etf", "dividend"))
        sec = envelopes.get(("etf", "sector"))
        hs = envelopes.get(("etf", "hs300"))
        merged: dict = {}
        if div is not None:
            merged.update(div.get("payload") or {})
        if sec is not None:
            merged["sector"] = sec.get("payload")
        if hs is not None:
            merged["hs300"] = hs.get("payload")
        blob = json.dumps(merged, ensure_ascii=False)       # 无数据时为 "{}"，仍是合法 JSON
        # 整块替换 PAYLOAD（绝不留 __PAYLOAD__ 让前端 JSON.parse 崩）
        fragment = re.sub(
            r'(<script id="PAYLOAD" type="application/json">).*?(</script>)',
            lambda mm: mm.group(1) + blob + mm.group(2),
            fragment, count=1, flags=re.S)
        fragment = fragment.replace("__PAYLOAD__", blob)
        return fragment

    if domain == "stock":
        # ★ 注意：下面几个占位符位于页面**可见文本**里，必须无条件替换。
        #   曾经是 `if sc.get("data_date")` 条件替换 —— 信封里少一个字段，
        #   `/*__DATA_DATE__*/` 就原样显示在"数据日期"旁边（线上确实如此）。
        sc = envelopes.get(("stock", "screen")) or {}
        pl = sc.get("payload") or {}
        stocks = pl.get("stocks", [])
        factors = pl.get("factors", {})
        fragment = fragment.replace("/*__STOCK_UNIVERSE__*/[]", _js(stocks))
        fragment = fragment.replace("/*__REAL_FACTORS__*/{}", _js(factors))
        gen = (sc.get("generated_at") or "")[11:16]      # HH:MM
        fragment = fragment.replace("/*__GEN_TIME__*/", gen or "—")
        fragment = fragment.replace("/*__DATA_DATE__*/", sc.get("data_date") or "—")
        return fragment

    return fragment


def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def load_envelopes(payload_dir: str) -> dict[tuple[str, str], dict]:
    """从 `state/payload/*.json` 读统一信封（构建期数据入口）。"""
    out: dict[tuple[str, str], dict] = {}
    if not payload_dir or not os.path.isdir(payload_dir):
        return out
    for name in os.listdir(payload_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(payload_dir, name), encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            continue
        d, v = obj.get("domain"), obj.get("variant")
        if d and v:
            out[(d, v)] = obj
    return out


# ---------------------------------------------------------------------------
# 壳层样式（自身也用作用域，避免污染三域）
# ---------------------------------------------------------------------------
# ★ 色值不写死在这里：用 $TOKEN 占位，构建时从 PALETTE 取值（见 _shell_css）。
#   否则"统一"只到三域、外壳又成了第二套色。
SHELL_CSS = """
.qh-shell{background:$BG;min-height:100vh;margin:0;padding:0;font:14px/1.6 $NUM;}
.qh-topbar{position:sticky;top:0;z-index:9999;display:flex;align-items:center;gap:8px;
  background:$BG;color:$INK;min-height:60px;padding:10px 20px;
  border-bottom:1px solid $LINE;}
.qh-topbar .qh-logo{font-size:15px;font-weight:700;letter-spacing:.5px;color:$INK;
  padding-right:18px;margin-right:6px;border-right:1px solid $LINE;}
.qh-topbar .qh-tab{$CHIP}
.qh-topbar .qh-tab:hover{$CHIP_HOVER}
.qh-topbar .qh-tab.on{$CHIP_ON}
.qh-topbar .qh-tab .qh-note{display:block;font-size:10px;color:$MUTED;font-weight:400;
  margin-top:-1px;line-height:1.2;}
.qh-topbar .qh-tab.on .qh-note{color:#fff;opacity:.75;}
.qh-banner{margin:0;padding:10px 20px;font-size:12.5px;display:flex;gap:16px;
  align-items:center;background:#FFF8E6;border-bottom:1px solid #F0DDA8;color:$ACCENT;}
.qh-banner .qh-lamp{width:9px;height:9px;border-radius:50%;display:inline-block;}
.qh-banner .lamp-green{background:$GREEN}.qh-banner .lamp-yellow{background:$ACCENT}
.qh-banner .lamp-red{background:$RED}
.qh-banner b{font-weight:600;}
.qh-banner .qh-meta{margin-left:auto;color:#8A7A4E;font-size:11.5px;}
.qh-domain{display:none;}
.qh-domain.on{display:block;}
.qh-footer{padding:22px 20px 36px;color:$MUTED;font-size:11.5px;line-height:1.9;
  border-top:1px solid $LINE;margin-top:14px;background:$CARD;}
.qh-footer code{background:$BG;padding:1px 5px;border-radius:4px;font-size:11px;}
@media(max-width:760px){
  .qh-topbar{min-height:auto;flex-wrap:wrap;padding:8px 12px;gap:4px}
  .qh-topbar .qh-logo{border:0;margin:0;padding-right:8px;font-size:14px}
  .qh-topbar .qh-tab .qh-note{display:none}
  .qh-banner{flex-wrap:wrap;gap:8px}
  .qh-banner .qh-meta{margin-left:0}
}
"""

# 外壳里可用到的令牌。$GREEN/$RED 是 PALETTE 涨跌色的别名 —— 调色板里绿红只有
# 涨跌这两档（红涨绿跌），直接引用 --down/--up 读起来会歧义反了，故在此改名。
#
# ★ 顶部导航标签吃的是域内那套「可选中标签」规范（scope.py 的 CHIP）——
#   壳层在 `#app-*` 之外，拿不到 var(--card)/var(--ink)，所以这里把令牌值
#   落成实参。**只有色值需要落，形状仍在 CHIP 里**，两边不会各自漂。
def _shell_chip() -> dict[str, dict[str, str]]:
    return {
        "$CHIP": {**CHIP, "background": PALETTE["--card"],
                  "color": PALETTE["--ink"],
                  "border": f"1px solid {PALETTE['--line']}"},
        "$CHIP_HOVER": {**CHIP_HOVER, "color": PALETTE["--ink"],
                        "border-color": PALETTE["--muted"]},
        "$CHIP_ON": {**CHIP_ON, "background": PALETTE["--ink"],
                     "border-color": PALETTE["--ink"], "color": "#fff"},
    }


_SHELL_TOKENS = {
    "$BG": PALETTE["--bg"],
    "$CARD": PALETTE["--card"],
    "$CARD2": PALETTE["--card-2"],
    "$INK": PALETTE["--ink"],
    "$LINE": PALETTE["--line"],
    "$MUTED": PALETTE["--muted"],
    "$ACCENT": PALETTE["--accent"],
    "$NUM": PALETTE["--num"],
    "$GREEN": PALETTE["--down"],
    "$RED": PALETTE["--up"],
}


def _shell_css(css: str = SHELL_CSS) -> str:
    """把 SHELL_CSS 里的 $TOKEN 换成 PALETTE 的值（唯一真相源在 scope.py）。"""
    subs = dict(_SHELL_TOKENS)
    for k, props in _shell_chip().items():
        subs[k] = decl(props)
    # ★ 长键先换：`$CARD` 是 `$CARD2` 的前缀，先换短的会把 `$CARD2` 换残
    for k in sorted(subs, key=len, reverse=True):
        css = css.replace(k, subs[k])
    return css

SHELL_JS = """
(function(){
  "use strict";
  var DOMAINS = __DOMAINS__;
  var DEFAULT = "__DEFAULT__";
  function show(dom, push){
    DOMAINS.forEach(function(d){
      var el = document.getElementById("qh-domain-" + d);
      if (el) el.classList.toggle("on", d === dom);
      var tb = document.getElementById("qh-tab-" + d);
      if (tb) tb.classList.toggle("on", d === dom);
    });
    try { if (push !== false) history.replaceState(null, "", "#" + dom); } catch(e){}
    // 切换后让各域的 echarts resize（隐藏时初始化会算出 0 宽）
    window.dispatchEvent(new Event("resize"));
  }
  window.qhShow = show;
  document.addEventListener("DOMContentLoaded", function(){
    var h = (location.hash || "").replace("#", "");
    var init = DOMAINS.indexOf(h) >= 0 ? h
             : (DOMAINS.indexOf(DEFAULT) >= 0 ? DEFAULT : DOMAINS[0]);
    show(init, false);
    Array.prototype.forEach.call(document.querySelectorAll(".qh-tab"), function(b){
      b.addEventListener("click", function(){ show(b.getAttribute("data-domain")); });
    });
  });
})();
"""


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _extract_from_report_py(path: str) -> str:
    """quant-lab 的 HTML_TEMPLATE 内嵌在 build_report.py 的 r-string 里。"""
    src = _read(path)
    m = re.search(r'HTML_TEMPLATE\s*=\s*r"""(.*?)"""', src, re.S)
    if not m:
        raise ValueError(f"未在 {path} 找到 HTML_TEMPLATE")
    return m.group(1)


def _inline_echarts(root: str) -> str:
    """把 CDN 引用换成内联的本地 echarts（发布时无外网也能用）。"""
    p = os.path.join(_base(root, "stock-factor-engine"), "assets/echarts.min.js")
    if not os.path.exists(p):
        return ""
    return f"<script>{_read(p)}</script>"


CDN_ECHARTS_RE = re.compile(
    r'<script\s+src="(?:https?:)?//[^"]*echarts[^"]*"\s*>\s*</script>')
# 相对路径引用（stock 模板用 assets/echarts.min.js）——同样要清掉，统一走内联
LOCAL_ECHARTS_RE = re.compile(
    r'<script\s+src="[^"]*echarts[^"]*"\s*>\s*</script>')
# ★ 兜底 loader：`if(typeof echarts==='undefined'){document.write('<script src=...>')}`
#   这种写法不匹配上面的正则，但同样会去拉 CDN，必须一起清掉
CDN_ECHARTS_LOADER_RE = re.compile(
    r'<script>\s*if\s*\(\s*typeof\s+echarts\s*===?\s*[\'"]undefined[\'"]\s*\)'
    r'[\s\S]*?<\/script>')


def duplicate_ids(fragments: dict[str, str]) -> set[str]:
    """跨片段重名的 id 名集合（本仓实测 = `{"sidebar"}`）。

    只有这些 id 需要加域前缀：两个同名节点同时进一个文档时，
    `getElementById` 只返回第一个，后一个域会拿到别人的节点。

    只在本域出现的 id **保持原名** —— 少改一处就少一处可能漏。
    全量前缀化的教训见 `_namespace_ids`。
    """
    seen: dict[str, int] = {}
    for html in fragments.values():
        for name in fragment_ids(html):
            seen[name] = seen.get(name, 0) + 1
    return {name for name, n in seen.items() if n > 1}


def build(src_root: str, out_path: str, *, health: dict | None = None,
          echarts_inline: bool = True, payload_dir: str | None = None) -> str:
    """合成单页壳。

    src_root : 域源码根目录。两种布局皆可：
               ① 合并后的 `domains/`（含 shortterm/ etf/ selected/）——自包含，推荐；
               ② 旧的外部检出根（含 quant-lab/ red-dividend-strategy/
                  stock-factor-engine/ 三个子目录）。
    health   : 可选的健康状态 {"level": "green|yellow|red", "day": "2026-09-11", ...}
    payload_dir : 可选，`state/payload/` 目录（统一信封入口）。
                  给了就注入真实数据；不给则保留占位符（页面显示空态）。
    """
    frags: dict[str, str] = {}
    ech = _inline_echarts(src_root) if echarts_inline else ""
    envelopes = load_envelopes(payload_dir) if payload_dir else {}

    # ---- 域 1：短线策略（模板内嵌在 build_report.py）----
    ql = _extract_from_report_py(os.path.join(_base(src_root, "quant-lab"), "scripts/build_report.py"))
    ql = inject_payloads(ql, "quant-lab", envelopes)

    # ---- 域 2：ETF 策略 ----
    etf = _read(os.path.join(_base(src_root, "red-dividend-strategy"), "index_template.html"))
    etf = inject_payloads(etf, "etf", envelopes)

    # ---- 域 3：个性化选股 ----
    stk = _read(os.path.join(_base(src_root, "stock-factor-engine"), "templates/index_template.html"))
    stk = inject_payloads(stk, "stock", envelopes)

    # ★ 只给**跨域重名**的 id 加域前缀（本仓 = `sidebar`，etf 与 stock 各有一个）。
    #   所以必须先收齐三域原始片段、算出重名集合，再逐个作用域化。
    raw = {"quant-lab": ql, "etf": etf, "stock": stk}
    dup = duplicate_ids(raw)
    for k, frag in raw.items():
        frags[k] = scope_html_fragment(frag, k, rename=dup)

    # ★ ECharts 三个域都依赖，作为全局资源提到 head，只放一份
    #   （模板里的 CDN / document.write 兜底 loader 都要清掉，否则发布出去会去拉外网）
    head_assets = ""
    if ech:
        head_assets = ech
    for k in frags:
        frags[k] = CDN_ECHARTS_RE.sub(lambda _m: "", frags[k])
        frags[k] = CDN_ECHARTS_LOADER_RE.sub(lambda _m: "", frags[k])
        frags[k] = LOCAL_ECHARTS_RE.sub(lambda _m: "", frags[k])

    body: list[str] = []
    for key, title, note in NAV:
        body.append(
            f'<section class="qh-domain" data-domain="{key}" id="qh-domain-{key}">\n'
            + frags[key] + "\n</section>"
        )

    # 默认落在"第一个有真实数据"的域，避免一开页就是空的短线域（用户会以为"完全没数据"）
    def _has_data(dom: str) -> bool:
        for (d, _v), e in envelopes.items():
            if d != dom:
                continue
            pl = e.get("payload")
            if isinstance(pl, dict) and any(pl.values()):
                return True
            if isinstance(pl, list) and pl:
                return True
        return False

    default_domain = next((d for d in DOMAINS if _has_data(d)), DOMAINS[0])
    html = _assemble(body=body, head_assets=head_assets, health=health or {},
                     default_domain=default_domain)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html


def _assemble(*, body: list[str], head_assets: str, health: dict,
              default_domain: str = "quant-lab") -> str:
    tabs = "\n".join(
        f'  <button class="qh-tab" id="qh-tab-{k}" data-domain="{k}">{t}'
        f'<span class="qh-note">{n}</span></button>'
        for k, t, n in NAV)

    lvl = health.get("level", "green")
    day = health.get("day", "—")
    cov = health.get("coverage")
    cov_txt = f"覆盖率 {cov:.1%}" if isinstance(cov, (int, float)) else "覆盖率 —"
    extra = health.get("note", "")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>Quant Hub · 量化中枢</title>
{head_assets}
<style>
{_shell_css()}
</style>
</head>
<body class="qh-shell">
<header class="qh-topbar">
  <span class="qh-logo">Quant Hub</span>
{tabs}
</header>
<div class="qh-banner">
  <span class="qh-lamp lamp-{lvl}"></span>
  <span><b>数据状态：{lvl.upper()}</b> · 数据日 {day} · {cov_txt}</span>
  {f'<span class="qh-meta">{extra}</span>' if extra else ''}
</div>
{chr(10).join(body)}
<footer class="qh-footer">
  <div><b>Quant Hub</b> —— 短线策略 / ETF 策略 / 个性化选股 三域合并单页。</div>
  <div>数据与代码分离：代码公开于 <code>quant-hub</code>，行情数据私有于 <code>quant-hub-data</code>。</div>
  <div>本页为静态快照，不构成投资建议。全站视觉统一（配色令牌见 <code>web/shell/scope.py</code>），涨跌色为中国惯例：<b>红涨绿跌</b>。</div>
</footer>
<script>
{SHELL_JS.replace("__DOMAINS__", repr(DOMAINS)).replace("__DEFAULT__", default_domain)}
</script>
</body>
</html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="domains",
                    help="域源码根目录：合并后的 domains/（默认，自包含）或旧的外部检出根")
    ap.add_argument("--out", default="web/dist/index.html")
    ap.add_argument("--payload-dir", default=None,
                    help="统一信封目录（state/payload），给了就注入真实数据")
    ap.add_argument("--no-echarts", action="store_true")
    args = ap.parse_args(argv)
    html = build(args.src, args.out, echarts_inline=not args.no_echarts,
                 payload_dir=args.payload_dir)
    print(f"已生成 {args.out} ({len(html)/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
