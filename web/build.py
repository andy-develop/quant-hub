"""三域前端作用域化：把三个独立单文件 HTML 合成一个自包含单页壳。

产物 `web/dist/index.html` 要求：
  - 单文件，可离线打开（echarts 已本地化，不依赖 CDN）
  - 三域各自 CSS 作用域隔离，互不污染
  - 左侧一级导航切换三域（顶部锚点切换域内页面）
  - 涨跌色逐域保留（个性化选股本来就是反的，见 scope.py 注释）

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

from web.shell.scope import THEMES, scope_html_fragment  # noqa: E402

__all__ = ["build", "SHELL_CSS", "NAV", "DOMAINS", "inject_payloads"]

# 一级导航（域级）
NAV = [
    ("quant-lab", "短线策略", "动量 + 量化黑盒"),
    ("etf", "ETF 策略", "红利低波跟投"),
    ("stock", "个性化选股", "因子智能选股"),
]

DOMAINS = [k for k, _, _ in NAV]


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

    找不到对应数据时**保持原占位符不动**（页面会显示"数据加载失败"级别的
    空态，但不会崩）—— 比塞一份假数据安全。

    envelopes : {(domain, variant): envelope}
    """
    if not envelopes:
        return fragment
    if domain == "quant-lab":
        m = envelopes.get(("quant-lab", "momentum"))
        bb = envelopes.get(("quant-lab", "blackbox"))
        if m is not None:
            fragment = fragment.replace(
                "const MODES = __DATA__;",
                "const MODES = " + json.dumps(m.get("payload", {}), ensure_ascii=False) + ";")
        if bb is not None:
            fragment = fragment.replace(
                "const MODES_BB = __DATA_BB__;",
                "const MODES_BB = " + json.dumps(bb.get("payload", {}), ensure_ascii=False) + ";")
        # 兼容 render_html 在服务端就替换掉的写法
        if m is not None:
            fragment = fragment.replace("__DATA__",
                                        json.dumps(m.get("payload", {}), ensure_ascii=False))
        if bb is not None:
            fragment = fragment.replace("__DATA_BB__",
                                        json.dumps(bb.get("payload", {}), ensure_ascii=False))
        return fragment

    if domain == "etf":
        div = envelopes.get(("etf", "dividend"))
        sec = envelopes.get(("etf", "sector"))
        hs = envelopes.get(("etf", "hs300"))
        if div is None and sec is None and hs is None:
            return fragment
        merged: dict = {}
        if div is not None:
            merged.update(div.get("payload") or {})
        if sec is not None:
            merged["sector"] = sec.get("payload")
        if hs is not None:
            merged["hs300"] = hs.get("payload")
        blob = json.dumps(merged, ensure_ascii=False)
        # 模板里 PAYLOAD 块初始是 __PAYLOAD__
        fragment = re.sub(
            r'(<script id="PAYLOAD" type="application/json">).*?(</script>)',
            lambda mm: mm.group(1) + blob + mm.group(2),
            fragment, count=1, flags=re.S)
        return fragment

    if domain == "stock":
        sc = envelopes.get(("stock", "screen"))
        if sc is None:
            return fragment
        pl = sc.get("payload") or {}
        stocks = pl.get("stocks", [])
        factors = pl.get("factors", {})
        fragment = fragment.replace("/*__STOCK_UNIVERSE__*/[]", _js(stocks))
        fragment = fragment.replace("/*__REAL_FACTORS__*/{}", _js(factors))
        gen = (sc.get("generated_at") or "")[11:16]      # HH:MM
        if gen:
            fragment = fragment.replace("/*__GEN_TIME__*/", gen)
        if sc.get("data_date"):
            fragment = fragment.replace("/*__DATA_DATE__*/", sc["data_date"])
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
SHELL_CSS = """
.qh-shell{background:#F1F2F4;min-height:100vh;margin:0;padding:0;
  font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif;}
.qh-topbar{position:sticky;top:0;z-index:9999;display:flex;align-items:center;gap:6px;
  background:#1F2430;color:#fff;padding:0 20px;height:52px;
  box-shadow:0 1px 4px rgba(0,0,0,.18);}
.qh-topbar .qh-logo{font-size:15px;font-weight:700;letter-spacing:.5px;
  padding-right:18px;margin-right:6px;border-right:1px solid rgba(255,255,255,.18);}
.qh-topbar .qh-tab{padding:6px 16px;border-radius:8px;font-size:13.5px;color:#B9BEC9;
  cursor:pointer;white-space:nowrap;border:0;background:transparent;font-family:inherit;}
.qh-topbar .qh-tab:hover{color:#fff;background:rgba(255,255,255,.08);}
.qh-topbar .qh-tab.on{background:#fff;color:#1F2430;font-weight:600;}
.qh-topbar .qh-tab .qh-note{display:block;font-size:10px;opacity:.62;font-weight:400;
  margin-top:-1px;line-height:1.2;}
.qh-banner{margin:0;padding:10px 20px;font-size:12.5px;display:flex;gap:16px;
  align-items:center;background:#FFF8E6;border-bottom:1px solid #F0DDA8;color:#6B4E0B;}
.qh-banner .qh-lamp{width:9px;height:9px;border-radius:50%;display:inline-block;}
.qh-banner .lamp-green{background:#16A34A}.qh-banner .lamp-yellow{background:#D97706}
.qh-banner .lamp-red{background:#DC2626}
.qh-banner b{font-weight:600;}
.qh-banner .qh-meta{margin-left:auto;color:#8A7A4E;font-size:11.5px;}
.qh-domain{display:none;}
.qh-domain.on{display:block;}
.qh-footer{padding:22px 20px 36px;color:#8A8F99;font-size:11.5px;line-height:1.9;
  border-top:1px solid #E2E4E9;margin-top:14px;background:#fff;}
.qh-footer code{background:#F4F5F7;padding:1px 5px;border-radius:4px;font-size:11px;}
@media(max-width:760px){
  .qh-topbar{height:auto;flex-wrap:wrap;padding:8px 12px;gap:4px}
  .qh-topbar .qh-logo{border:0;margin:0;padding-right:8px;font-size:14px}
  .qh-topbar .qh-tab .qh-note{display:none}
  .qh-banner{flex-wrap:wrap;gap:8px}
  .qh-banner .qh-meta{margin-left:0}
}
"""

SHELL_JS = """
(function(){
  "use strict";
  var DOMAINS = __DOMAINS__;
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
    show(DOMAINS.indexOf(h) >= 0 ? h : DOMAINS[0], false);
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
    p = os.path.join(root, "stock-factor-engine/assets/echarts.min.js")
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


def build(src_root: str, out_path: str, *, health: dict | None = None,
          echarts_inline: bool = True, payload_dir: str | None = None) -> str:
    """合成单页壳。

    src_root : 三仓检出根目录（含 quant-lab/ 、red-dividend-strategy/ 、
               stock-factor-engine/ 三个子目录）
    health   : 可选的健康状态 {"level": "green|yellow|red", "day": "2026-09-11", ...}
    payload_dir : 可选，`state/payload/` 目录（统一信封入口）。
                  给了就注入真实数据；不给则保留占位符（页面显示空态）。
    """
    frags: dict[str, str] = {}
    ech = _inline_echarts(src_root) if echarts_inline else ""
    envelopes = load_envelopes(payload_dir) if payload_dir else {}

    # ---- 域 1：短线策略（模板内嵌在 build_report.py）----
    ql = _extract_from_report_py(os.path.join(src_root, "quant-lab/scripts/build_report.py"))
    ql = inject_payloads(ql, "quant-lab", envelopes)
    frags["quant-lab"] = scope_html_fragment(ql, "quant-lab")

    # ---- 域 2：ETF 策略 ----
    etf = _read(os.path.join(src_root, "red-dividend-strategy/index_template.html"))
    etf = inject_payloads(etf, "etf", envelopes)
    frags["etf"] = scope_html_fragment(etf, "etf")

    # ---- 域 3：个性化选股 ----
    stk = _read(os.path.join(src_root, "stock-factor-engine/templates/index_template.html"))
    stk = inject_payloads(stk, "stock", envelopes)
    frags["stock"] = scope_html_fragment(stk, "stock")

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

    html = _assemble(body=body, head_assets=head_assets, health=health or {})
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html


def _assemble(*, body: list[str], head_assets: str, health: dict) -> str:
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
{SHELL_CSS}
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
  <div>本页为静态快照，不构成投资建议。涨跌颜色沿用各域原有约定（个性化选股为绿涨红跌）。</div>
</footer>
<script>
{SHELL_JS.replace("__DOMAINS__", repr(DOMAINS))}
</script>
</body>
</html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="三仓检出根目录")
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
