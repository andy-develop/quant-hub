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
    CHIP, CHIP_HOVER, CHIP_ON, KEYBOARD_REACH, PALETTE, THEMES, decl, fragment_ids,
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
/* ★ 报头 = 一张「行情纸」的报头（不是 SaaS 导航栏）：
   左边是这张纸叫什么、在看哪个域；右边是这张纸是哪一天的、覆盖多少、状态灯。
   数字走等宽 + tabular-nums —— 这一行就是整个页面的“版本号”，扫一眼就知道
   手上这份是不是今天的。 */
.qh-shell{background:$BG;min-height:100vh;margin:0;padding:0;color:$INK;
  font:14px/1.6 $FONT_TEXT;-webkit-font-smoothing:antialiased;}
.qh-topbar{position:sticky;top:0;z-index:9999;display:flex;align-items:center;gap:14px;
  background:$BG;color:$INK;min-height:56px;padding:9px 20px;
  border-bottom:1px solid $LINE;}
.qh-brand{display:flex;align-items:baseline;gap:9px;padding-right:16px;
  border-right:1px solid $LINE;}
.qh-brand .qh-logo{font-size:$FS_LG;font-weight:700;letter-spacing:.02em;color:$INK;}
.qh-brand .qh-logo-en{font-family:$NUM;font-size:$FS_XS;letter-spacing:.12em;
  color:$MUTED;text-transform:uppercase;}
.qh-tabs{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.qh-topbar .qh-tab{$CHIP}
.qh-topbar .qh-tab:hover{$CHIP_HOVER}
.qh-topbar .qh-tab.on{$CHIP_ON}
.qh-topbar .qh-tab .qh-note{display:block;font-size:$FS_XS;color:$MUTED;font-weight:400;
  margin-top:1px;line-height:1.25;}
.qh-topbar .qh-tab.on .qh-note{color:#fff;opacity:.78;}
.qh-stamp{margin-left:auto;display:flex;align-items:center;gap:16px;
  font-size:$FS_XS;color:$MUTED;white-space:nowrap;}
.qh-stamp .qh-lamp{width:8px;height:8px;border-radius:50%;display:inline-block;
  flex:0 0 auto;}
.qh-stamp .lamp-green{background:$GREEN}
.qh-stamp .lamp-yellow{background:$WARN}
.qh-stamp .lamp-red{background:$RED}
.qh-stamp b{font-family:$NUM;font-variant-numeric:tabular-nums;font-weight:600;
  color:$INK;margin-left:5px;}
/* 警示条：**只在非 green 时出现**。旧版不管状态都挂一条黄条，
   日常（green）看到的是一个常驻的假警告；现在真有告警才占版面。 */
.qh-banner{margin:0;padding:9px 20px;font-size:$FS_MD;display:flex;gap:10px;
  align-items:center;background:$WARNBG;border-bottom:1px solid $WARNLINE;
  color:$WARNINK;}
.qh-banner b{font-weight:600;}
.qh-banner .qh-meta{margin-left:auto;color:$WARNINK;font-size:$FS_SM;}
.qh-domain{display:none;}
.qh-domain.on{display:block;}
.qh-footer{padding:22px 20px 36px;color:$MUTED;font-size:$FS_SM;line-height:1.9;
  border-top:1px solid $LINE;margin-top:14px;background:$CARD;}
.qh-footer b{color:$INK2;}
.qh-footer code{background:$BG;border:1px solid $LINE;padding:1px 5px;
  border-radius:$R_SM;font-family:$NUM;font-size:$FS_XS;}
@media(max-width:860px){
  .qh-topbar{flex-wrap:wrap;padding:8px 12px;gap:8px}
  .qh-brand{border:0;padding-right:4px}
  .qh-topbar .qh-tab .qh-note{display:none}
  .qh-stamp{margin-left:0;width:100%;gap:14px;border-top:1px solid $LINE;padding-top:7px}
  .qh-banner{flex-wrap:wrap;gap:8px}
  .qh-banner .qh-meta{margin-left:0}
}
/* ★ 可达性底线（全站、不按域）：键盘焦点环 + 减少动效。
   壳层 CSS 在 <head>、先于三域样式，而模板里散着 `outline:none`
   （stock 的 .search-box input 就是）—— 同优先级靠文档顺序必输，故用
   `!important` 压过：否则键盘用户在一大片可点元素上完全看不到焦点在哪。 */
:focus-visible{outline:2px solid $PRIMARY !important;outline-offset:2px;}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms !important;animation-iteration-count:1 !important;
    transition-duration:.001ms !important;scroll-behavior:auto !important;}
}
"""

# 外壳里可用到的令牌。$GREEN/$RED 是 PALETTE 涨跌色的别名 —— 调色板里绿红只有
# 涨跌这两档（红涨绿跌），直接引用 --down/--up 读起来会歧义反了，故在此改名。
#
# ★ 顶部导航标签吃的是域内那套「可选中标签」规范（scope.py 的 CHIP）——
#   壳层在 `#app-*` 之外，拿不到 var(--card)/var(--ink)，所以这里把令牌值
#   落成实参。**形状（圆角/字号）也一并落**，否则 CHIP 里的 var(--r-sm)/
#   var(--fs-md) 在壳层是无效值，顶部标签会悄悄退回方角 14px（见 _resolve_vars）。
_VAR_RE = re.compile(r"var\((--[a-z0-9-]+)\)")


def _resolve_vars(props: dict[str, str]) -> dict[str, str]:
    """把 `var(--x)` 落成 PALETTE 里的实参。

    ★ 壳层在 `#app-*` **之外** —— 域内的主题块（`--r-sm`/`--fs-md`…）写在域根上，
      壳层读不到。不落值的下场实测过：`.qh-tab` 的 `border-radius:var(--r-sm)`
      整条声明作废（计算值 0px），顶部标签成了**方角**、字号也退回继承的 14px ——
      跟域内那套 4px / 13px 的标签根本对不上。
    """
    return {k: _VAR_RE.sub(lambda m: PALETTE.get(m.group(1), m.group(0)), v)
            for k, v in props.items()}


def _shell_chip() -> dict[str, dict[str, str]]:
    return {
        "$CHIP": _resolve_vars({**CHIP, "background": PALETTE["--card"],
                                "color": PALETTE["--ink"],
                                "border": f"1px solid {PALETTE['--line']}"}),
        "$CHIP_HOVER": _resolve_vars({**CHIP_HOVER, "color": PALETTE["--ink"],
                                      "border-color": PALETTE["--muted"]}),
        "$CHIP_ON": _resolve_vars({**CHIP_ON, "background": PALETTE["--ink"],
                                   "border-color": PALETTE["--ink"],
                                   "color": "#fff"}),
    }


_SHELL_TOKENS = {
    "$BG": PALETTE["--bg"],
    "$CARD": PALETTE["--card"],
    "$CARD2": PALETTE["--card-2"],
    "$INK": PALETTE["--ink"],
    "$INK2": PALETTE["--ink2"],
    "$LINE": PALETTE["--line"],
    "$MUTED": PALETTE["--muted"],
    "$PRIMARY": PALETTE["--primary"],
    # ★ 壳层在 `#app-*` 之外，拿不到 var(--font-text)/var(--r-sm) 这些域内令牌
    #   （主题块是写在域根上的），所以字体/字号/圆角也得落成实参。
    "$FONT_TEXT": PALETTE["--font-text"],
    "$NUM": PALETTE["--num"],
    "$FS_XS": PALETTE["--fs-xs"],
    "$FS_SM": PALETTE["--fs-sm"],
    "$FS_MD": PALETTE["--fs-md"],
    "$FS_LG": PALETTE["--fs-lg"],
    "$R_SM": PALETTE["--r-sm"],
    "$WARNBG": PALETTE["--warn-bg"],
    "$WARNLINE": PALETTE["--warn-line"],
    "$WARNINK": PALETTE["--warn-ink"],
    "$WARN": PALETTE["--warn"],
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
    bootKeyboard();
  });

  /* ★ 键盘可达性底线（见 scope.py 的 KEYBOARD_REACH）
     三域模板里有大量「用 JS 绑了点击的 div / 没有 href 的 a」—— 浏览器原生聚焦不到，
     键盘用户 Tab 不到它们。改模板要动三份、风险大，所以在这里统一补齐：
       tabindex=0 + role=button，Enter/Space → click()（复用模板原有处理器）。
     MutationObserver 兜住动态渲染出来的节点（因子筛选、自选列表都是 innerHTML 重绘的）。 */
  var REACH = __REACH__;
  var NATIVE = /^(A|BUTTON|INPUT|SELECT|TEXTAREA|SUMMARY|DETAILS)$/;
  function markReachable(){
    Object.keys(REACH).forEach(function(dom){
      var root = document.getElementById("app-" + dom);
      if (!root) return;
      REACH[dom].forEach(function(sel){
        Array.prototype.forEach.call(root.querySelectorAll(sel), function(el){
          if (el.hasAttribute("data-qh-key")) return;
          // 原生就能聚焦的不动（有 href 的 a、button、input…）
          if (el.tagName === "A" ? el.hasAttribute("href") : NATIVE.test(el.tagName)) return;
          el.setAttribute("data-qh-key", "1");
          el.setAttribute("tabindex", "0");
          if (!el.hasAttribute("role")) el.setAttribute("role", "button");
        });
      });
    });
  }
  document.addEventListener("keydown", function(e){
    if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
    var el = e.target;
    if (!el || !el.hasAttribute || !el.hasAttribute("data-qh-key")) return;
    e.preventDefault();   // Space 默认会滚页
    el.click();
  });
  function bootKeyboard(){
    markReachable();
    if (!window.MutationObserver) return;
    var pending = null;
    new MutationObserver(function(){
      if (pending) return;
      pending = setTimeout(function(){ pending = null; markReachable(); }, 60);
    }).observe(document.body, {childList: true, subtree: true});
  }
})();
"""


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


_HTML_TEMPLATE_RE = re.compile(r'HTML_TEMPLATE\s*=\s*r"""(.*?)"""', re.S)


def _html_template(src: str) -> str:
    """build_report.py 里的内层 HTML_TEMPLATE（**未渲染**，占位符还在）。"""
    m = _HTML_TEMPLATE_RE.search(src)
    if not m:
        raise ValueError("未找到 HTML_TEMPLATE")
    return m.group(1)


def _extract_from_report_py(path: str) -> str:
    """quant-lab 的 HTML_TEMPLATE 内嵌在 build_report.py 的 r-string 里。"""
    return _html_template(_read(path))


# ---------------------------------------------------------------------------
# 短线域「双页」装配：render_html() 的纯标准库复刻
# ---------------------------------------------------------------------------
# 域内 build_report.render_html() 会做两件事，合并层必须一起做，否则线上就是
# 「页面显示 __STRAT_DOC__ 字面量 + 侧栏『量化黑盒』点进去空白 + 加载即 JS 报错」：
#   ① 按 PAGE_BLOCK 标记把动量页克隆成黑盒页（id 加 bb_ 前缀）—— 两页各自一份
#      DOM、共用 tail 里的一套 initPage；缺了克隆，initPage('bb_') 找不到
#      bb_rangeTabs 直接抛 TypeError，后续初始化整段中断。
#   ② 替换 __STRAT_DOC__ / __ST_BANNER__ / __MAX_HOLD__ 三个占位符。
# 但 render_html 依赖 pandas/numpy（要读 parquet 算 KPI、生成 ST 横幅），而合并层
# 被要求仅用标准库（见 ci.yml），且旧仓布局的 domains/ 未必有 data/。所以：
#   - 文案 / 持有上限：从域源码**抽取**，不复制一份（单一真相源）；
#   - KPI 表：直接用统一信封 payload 里的 equity/trades 现算（纯 Python）；
#   - ST 横幅：留空 —— 它是「ST 数据新鲜度」告警，需要读 parquet；合并页顶部
#     的状态灯/数据日由壳层统一给出，域内不重复再挂一条。
_QL_DOC_RE = re.compile(r'^STRAT_DOC\s*=\s*"""(.*?)"""', re.S | re.M)
_QL_DOC_BB_RE = re.compile(r'STRAT_DOC_BB\s*=\s*\((.*?)\)\s*\n\s*\n', re.S)
_QL_P_MOM_RE = re.compile(r'^_P_MOM\s*=\s*"""(.*?)"""', re.S | re.M)
_QL_P_BB_RE = re.compile(r'^_P_BB\s*=\s*"""(.*?)"""', re.S | re.M)
_QL_LIT_REPLACE_RE = re.compile(r'\.replace\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)')
_QL_MAX_HOLD_RE = re.compile(r'^MAX_HOLD\s*=\s*(\d+)', re.M)
_PAGE_START = "<!--PAGE_BLOCK_START-->"
_PAGE_END = "<!--PAGE_BLOCK_END-->"


def _quant_lab_docs(src: str) -> tuple[str, str]:
    """从 build_report.py 源码抽出（动量版, 黑盒版）策略说明。

    黑盒版在原文件里是 `STRAT_DOC_BB = (STRAT_DOC.replace(_P_MOM, _P_BB).replace(...))`
    —— 这里照着算一遍，省得文案改一处、合并页看的是另一处。
    """
    m = _QL_DOC_RE.search(src)
    if not m:
        raise ValueError("未在 build_report.py 找到 STRAT_DOC")
    doc, bb = m.group(1), m.group(1)
    pm, pb = _QL_P_MOM_RE.search(src), _QL_P_BB_RE.search(src)
    if pm and pb:
        bb = bb.replace(pm.group(1), pb.group(1))
    region = _QL_DOC_BB_RE.search(src)
    if region:      # 只扫 STRAT_DOC_BB 表达式内，别把 render_html 里的 .replace 也吃进来
        for a, b in _QL_LIT_REPLACE_RE.findall(region.group(1)):
            bb = bb.replace(a, b)
    return doc, bb


def _max_hold(src_root: str) -> str:
    """持有上限的权威来源：域内 engine.MAX_HOLD。"""
    p = os.path.join(_base(src_root, "quant-lab"), "scripts/engine.py")
    m = _QL_MAX_HOLD_RE.search(_read(p))
    if not m:
        raise ValueError(f"未在 {p} 找到 MAX_HOLD")
    return m.group(1)


def _kpi_stats(p: dict) -> dict | None:
    """从单一全期 payload 现算 收益/回撤/夏普/交易/胜率（纯 Python，口径同域内）。"""
    eq = [float(x) for x in (p.get("equity") or [])]
    if len(eq) < 2:
        return None
    d = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    mean = sum(d) / len(d)
    sd = (sum((x - mean) ** 2 for x in d) / (len(d) - 1)) ** 0.5 if len(d) > 1 else 0.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    tr = p.get("trades") or []
    win = sum(1 for t in tr if (t.get("pnl_pct") or 0) > 0) / len(tr) if tr else 0.0
    return {"ret": eq[-1] / eq[0] - 1, "maxdd": mdd,
            "sharpe": mean / sd * (244 ** 0.5) if sd > 0 else 0.0,
            "trades": len(tr), "winrate": win}


def _kpi_block_html(modes: dict, model: str) -> str:
    """策略说明里的 KPI 对比表（对齐域内 _kpi_block_* 的列与口径）。"""
    w = (modes or {}).get("y3") or {}
    on, off = _kpi_stats(w.get("on") or {}), _kpi_stats(w.get("off") or {})
    if not on or not off:
        return '<p class="note">暂无回测数据（构建时未注入 payload）。</p>'

    def row(label, m):
        return (f'<tr><td>{label}</td><td class="{"up" if m["ret"] >= 0 else "down"}">'
                f'<b>{m["ret"]:+.1%}</b></td><td>{m["maxdd"]:.1%}</td>'
                f'<td>{m["sharpe"]:.2f}</td><td>{m["trades"]}</td>'
                f'<td>{m["winrate"]:.1%}</td></tr>')

    p_on = w["on"]
    return ('<table><tr><th>口径</th><th>收益</th><th>最大回撤</th><th>夏普</th>'
            '<th>交易</th><th>胜率</th></tr>'
            + row("开 (默认)", on) + row("关", off) + '</table>'
            f'<p class="note">数据区间 {p_on.get("start_day", "—")} ~ {p_on.get("last_day", "—")}'
            f' · 评分模型 {model} · 夏普口径 √244</p>')


def _assemble_quant_lab(src: str, src_root: str, envelopes: dict) -> str:
    """内层 HTML_TEMPLATE → 合并页可用的短线域片段（动量页 + 黑盒页）。"""
    head, rest = _html_template(src).split(_PAGE_START, 1)
    block, tail = rest.split(_PAGE_END, 1)
    bb = block.replace('id="page-momentum"', 'id="page-blackbox" style="display:none"')
    bb = re.sub(r'id="(?!page-)', 'id="bb_', bb)

    doc_m, doc_bb = _quant_lab_docs(src)

    def page(html: str, doc: str, variant: str, model: str) -> str:
        payload = (envelopes.get(("quant-lab", variant)) or {}).get("payload") or {}
        return html.replace("__STRAT_DOC__",
                            doc.replace("__KPI_BLOCK__", _kpi_block_html(payload, model)))

    html = (head
            + page(block, doc_m, "momentum", "六项指标加权排队")
            + page(bb, doc_bb, "blackbox", "LightGBM lambdarank (walk-forward 滚动训练)")
            + tail)
    return (html.replace("__ST_BANNER__", "")
                .replace("__MAX_HOLD__", _max_hold(src_root)))


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
                  给了就注入真实数据；不给则注入合法空值（页面走自带空态）。
    """
    frags: dict[str, str] = {}
    ech = _inline_echarts(src_root) if echarts_inline else ""
    envelopes = load_envelopes(payload_dir) if payload_dir else {}

    # ---- 域 1：短线策略（模板内嵌在 build_report.py）----
    # 先装配（克隆黑盒页 + 替换文案占位符），再注入 payload
    ql_path = os.path.join(_base(src_root, "quant-lab"), "scripts/build_report.py")
    ql = inject_payloads(_assemble_quant_lab(_read(ql_path), src_root, envelopes),
                         "quant-lab", envelopes)

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
        return any(_envelope_filled(e) for (d, _v), e in envelopes.items() if d == dom)

    default_domain = next((d for d in DOMAINS if _has_data(d)), DOMAINS[0])
    html = _assemble(body=body, head_assets=head_assets,
                     health=health or _derive_health(envelopes),
                     default_domain=default_domain)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html


def _envelope_filled(e: dict) -> bool:
    """信封里的 payload 是不是真有东西（空字典 / 空列表都算无数据）。"""
    pl = e.get("payload")
    if isinstance(pl, dict):
        return any(pl.values())
    return bool(pl)


# 状态词用中文：这是一张中文行情纸，报头上挂一个 GREEN 是给机器看的
_LEVEL_CN = {"green": "正常", "yellow": "注意", "red": "异常"}


def _derive_health(envelopes: dict) -> dict:
    """没有外部健康信息时，从信封自己读。

    ★ 旧版 `main()` 和发布工作流都不传 health，于是报头恒显示
      「数据状态：GREEN · 数据日 — · 覆盖率 —」—— 一张没有日期的行情纸，
      而“哪天数据”恰恰是这个页面最该说的一句话。数据日直接取信封的 `data_date`，
      覆盖率 = 有数据的域 / 总域数。
    """
    days = sorted({str(e.get("data_date")) for e in envelopes.values()
                   if e.get("data_date")})
    filled = sum(1 for d in DOMAINS
                 if any(_envelope_filled(e) for (dd, _v), e in envelopes.items() if dd == d))
    return {"level": "green", "day": days[-1] if days else "—",
            "coverage": filled / len(DOMAINS)}


def _assemble(*, body: list[str], head_assets: str, health: dict,
              default_domain: str = "quant-lab") -> str:
    tabs = "\n".join(
        f'  <button class="qh-tab" id="qh-tab-{k}" data-domain="{k}">{t}'
        f'<span class="qh-note">{n}</span></button>'
        for k, t, n in NAV)

    lvl = health.get("level", "green")
    day = health.get("day", "—")
    cov = health.get("coverage")
    cov_txt = f"{cov:.0%}" if isinstance(cov, (int, float)) else "—"
    extra = health.get("note", "")

    # 警示条只在真有告警时占版面（旧版无论什么状态都挂着一条黄条，
    # 于是 green 的日常天也在报警 —— 告警一富有含义，天天报就不算报警了）。
    banner = ""
    if lvl != "green":
        banner = ('\n<div class="qh-banner">\n'
                  f'  <span><b>数据状态：{_LEVEL_CN.get(lvl, lvl)}</b></span>\n'
                  + (f'  <span class="qh-meta">{extra}</span>\n' if extra else "")
                  + '</div>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>量化中枢 · 短线策略 / ETF 策略 / 个性化选股</title>
{head_assets}
<style>
{_shell_css()}
</style>
</head>
<body class="qh-shell">
<header class="qh-topbar">
  <div class="qh-brand">
    <span class="qh-logo">量化中枢</span>
    <span class="qh-logo-en">Quant Hub</span>
  </div>
  <nav class="qh-tabs">
{tabs}
  </nav>
  <div class="qh-stamp">
    <span class="qh-lamp lamp-{lvl}"></span>
    <span>数据日<b>{day}</b></span>
    <span>覆盖<b>{cov_txt}</b></span>
    <span>状态<b>{_LEVEL_CN.get(lvl, lvl)}</b></span>
  </div>
</header>{banner}
{chr(10).join(body)}
<footer class="qh-footer">
  <div><b>量化中枢</b> —— 短线策略 / ETF 策略 / 个性化选股 三域合并单页。</div>
  <div>数据与代码分离：代码公开于 <code>quant-hub</code>，行情数据私有于 <code>quant-hub-data</code>。</div>
  <div>本页为静态快照，不构成投资建议。全站视觉统一（配色令牌见 <code>web/shell/scope.py</code>），涨跌色为中国惯例：<b>红涨绿跌</b>。</div>
</footer>
<script>
{SHELL_JS.replace("__DOMAINS__", repr(DOMAINS)).replace("__DEFAULT__", default_domain)
         .replace("__REACH__", json.dumps({k: list(v) for k, v in KEYBOARD_REACH.items()},
                                          ensure_ascii=False))}
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
