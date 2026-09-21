"""CSS 作用域隔离（方案 §5.2）。

## 为什么必须做

三域前端是三套独立长出来的单文件 HTML，把它们的 `<style>` 直接内联进一个页面
必然互相踩。按三域模板实测（`tools/scope_conflicts.py` 可复现）：

```
--- css ---
  quant-lab ∩ etf   : ['brand', 'card', 'chart', 'warn']
  quant-lab ∩ stock : ['brand', 'card', 'down', 'nav-item', 'sub', 'tag', 'up']
  etf       ∩ stock : ['badge', 'brand', 'card', 'g', 'main', 'meta',
                       'sidebar', 'v', 'view']
  出现在 >1 域      : 16 个
--- fn ---   etf ∩ stock : ['esc']
--- id ---   etf ∩ stock : ['sidebar']
--- var ---  etf ∩ stock : ['el','h','i','rows','s','score']
```

`brand`/`card` 出现在全部三域 —— 不隔离的话后加载的域会改掉前面的卡片样式。

## 隔离策略（两件事，缺一不可）

1. **CSS 选择器加前缀**：`.card{}` -> `#app-{domain} .card{}`
   纯 CSS 层解决，零运行时开销。
2. **主题变量显式声明**：三域各自的 `--up/--down/--bg/--ink…` 在这里统一写死，
   绝不依赖继承（不写就会继承到上一个域的值）。

### ★ 视觉风格：全站统一（2026-09 起）

早期版本**刻意保留**各域原值（个性化选股沿用美股惯例、绿涨红跌）。现在改为
**全站统一**——配色令牌、字号、容器宽度、卡片处理、涨跌色全部一致。

| 令牌 | 三域统一值 |
|------|-----------|
| `--up` / `--down` | `#D5423E` 红 / `#1D9E75` 绿（**红涨绿跌**） |
| `--bg` / `--card` / `--line` | `#F6F6F4` / `#FFFFFF` / `#E4E3DC` |
| `--ink` / `--ink2` / `--muted` | `#26251F` / `#3D3C34` / `#88867E` |
| `--primary`(`--blue`) / `--primary-light` | `#185FA5` / `#E6F1FB` |
| `--accent`(`--warn`) | `#854F0B` |
| `--radius` / `--shadow` | `14px` / `none` |

⚠️ **个性化选股的涨跌语义因此反转**（原来绿=涨，现在红=涨）。这一步成立的前提是：
该域所有涨跌着色都走 `var(--up)` / `var(--down)`（实测 CSS 里 7+7 处，
JS 拼 HTML 的字符串也写 `var(--up)`），**没有任何地方硬编码红绿**——所以改变量
即可整域翻向。**将来该域若新增硬编码的 `#16A34A`/`#DC2626` 着色，翻向就会只翻
一半**（CSS 走变量、JS 写死），务必一律走变量。

### 令牌从哪来

- 统一值集中在模块顶部的 [`PALETTE`](#)（唯一真相源），`theme_block()` 为**每个域**
  生成同一份变量块。
- 三域模板自己 `:root{}` 里写的原值**不去改模板**：作用域化后它与本模块的块
  同优先级，靠文档顺序覆盖 —— 所以 `scope_html_fragment()` 把 `theme_block()`
  生成在**域样式之后**（改这里前先读那条注释）。
- 模板里写死在选择器 / JS 里的十六进制色（不走变量）由 `unify_colors()` 按
  [`UNIFY_MAP`](#) 统一替换。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "DomainTheme",
    "THEMES",
    "PALETTE",
    "UNIFY_MAP",
    "COMPONENTS",
    "NAV_MEDIA",
    "CHIP",
    "CHIP_ON",
    "decl",
    "unify_colors",
    "scope_css",
    "scope_html_fragment",
    "scope_id",
    "theme_block",
    "component_css",
]

# ---------------------------------------------------------------------------
# ★ 统一调色板（唯一真相源）—— 三域共用同一份令牌，改这里即改全站观感
# ---------------------------------------------------------------------------
# 键是**三域模板出现过的全部变量名并集**（20 个）。不同域命名不同（etf 用
# --blue、stock 用 --primary、quant-lab 用 --muted…），全部一起发出去，
# 各域取自己认得的那些，多余的没人用、无害。
#
# ⚠️ 改色只改这里。模板里 `:root{}` 写的原值会被本模块的块覆盖（见
#    scope_html_fragment 的顺序注释）；写死在选择器/JS 里的十六进制色由
#    UNIFY_MAP 兜底。
PALETTE: dict[str, str] = {
    # 底 / 卡片
    "--bg": "#F6F6F4",
    "--card": "#FFFFFF",
    "--card-2": "#FAFAF7",
    # 文字三档
    "--ink": "#26251F",
    "--ink2": "#3D3C34",
    "--text": "#26251F",          # stock 的正文变量名
    "--muted": "#88867E",
    "--sub": "#88867E",
    "--dim": "#A9A79E",
    "--muted-2": "#A9A79E",
    # 描边
    "--line": "#E4E3DC",
    # 强调色
    "--primary": "#185FA5",
    "--blue": "#185FA5",
    "--primary-light": "#E6F1FB",
    "--accent": "#854F0B",
    "--warn": "#854F0B",
    "--amber": "#854F0B",
    "--purple": "#534AB7",
    # ★ 涨跌：全站统一中国惯例（红涨绿跌）
    "--up": "#D5423E",
    "--down": "#1D9E75",
    # 形状
    "--radius": "14px",
    "--shadow": "none",
    "--num": '-apple-system,"PingFang SC","Helvetica Neue",sans-serif',
}


# ---------------------------------------------------------------------------
# 逐域隔离信息
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DomainTheme:
    """一个域的隔离信息。

    配色不在这里 —— 三域已统一，颜色只认 PALETTE（见上）。这里只留
    「哪个域对应哪个作用域根」这件真正逐域不同的事。
    """

    key: str                 # 域标识：quant-lab / etf / stock
    root_id: str             # 作用域根元素 id


THEMES: dict[str, DomainTheme] = {
    "quant-lab": DomainTheme(key="quant-lab", root_id="app-quant-lab"),
    "etf": DomainTheme(key="etf", root_id="app-etf"),
    "stock": DomainTheme(key="stock", root_id="app-stock"),
}


# ---------------------------------------------------------------------------
# ★ 硬编码色统一：模板里写死在选择器 / JS 里的十六进制色不走变量，只能按值替换
# ---------------------------------------------------------------------------
# 覆盖范围（在 scope_html_fragment 里对「整段片段」生效，所以 CSS 与 JS 一起改）：
#   - 冷色中性调（etf/stock 的蓝白底、藏青正文、蓝灰描边）→ 基准暖中性调
#   - 强调蓝（#2B6CB0 / #2563EB）→ #185FA5
#   - 琥珀警示 → #854F0B
#   - 涨跌色相 → 统一到 PALETTE 的 --up/--down
#   - 弹窗遮罩 / 阴影的藏青 → 暖黑
#
# ⚠️ 这是**按字面量**替换，上游改了色值写法（大小写、压缩掉空格等）就会静默落空：
#    表现=样式回退、不报错。改模板配色后请顺手核对这里。
# ⚠️ 不含「数据系列色」（选股雷达图、回测曲线的推荐组合/沪深300 等）：那些是
#    数据语义、不是观感，统一会让不同曲线分不开。
UNIFY_MAP: tuple[tuple[str, str], ...] = (
    # 浅底 → 基准中性浅底
    ("#F6FAFE", "#FAFAF7"), ("#F0F6FD", "#FAFAF7"), ("#F0F7FE", "#FAFAF7"),
    ("#EAF2FB", "#FAFAF7"), ("#F8FBFE", "#FAFAF7"), ("#EEF4FA", "#FAFAF7"),
    ("#EFF4FF", "#FAFAF7"), ("#F4F6FB", "#F6F6F4"), ("#EEF1F8", "#FAFAF7"),
    # 描边（冷蓝灰 → 暖灰）
    ("#DDE8F4", "#E4E3DC"), ("#E2E6F0", "#E4E3DC"), ("#D1D5DB", "#E4E3DC"),
    # 次级文字 / 坐标轴（多写在图表 JS 里，CSS 够不着）
    ("#8AA3C0", "#88867E"), ("#5B7BA3", "#88867E"), ("#5A6480", "#88867E"),
    ("#8B93A8", "#88867E"), ("#6B7280", "#88867E"),
    ("#374151", "#3D3C34"), ("#2B4A6F", "#3D3C34"),
    # 正文（藏青 → 暖黑）
    ("#16324F", "#26251F"), ("#1A2040", "#26251F"),
    # 强调蓝
    ("#2B6CB0", "#185FA5"), ("#2563EB", "#185FA5"), ("#1D4ED8", "#185FA5"),
    ("#DBEAFE", "#E6F1FB"),
    ("rgba(43,108,176,", "rgba(24,95,165,"), ("rgba(37,99,235,", "rgba(24,95,165,"),
    # 基准线（金色虚线 → 调色板里的金）
    ("#8A6D3B", "#B07A2A"),
    # 琥珀警示
    ("#D97706", "#854F0B"), ("#B45309", "#854F0B"), ("#78350F", "#633806"),
    ("#FBBF24", "#EF9F27"), ("#FCD9B6", "#FAEEDA"),
    # 涨跌色相（同色系对齐，方向由 PALETTE 的 --up/--down 决定）
    ("#E0443C", "#D5423E"), ("#C0392B", "#D5423E"),
    ("#16A34A", "#1D9E75"), ("#177245", "#0F6E56"), ("#F0F9F2", "#E1F5EE"),
    # 弹窗遮罩 / 阴影（藏青 → 暖黑）
    ("rgba(30,41,80,", "rgba(38,37,31,"),
)

# 逐域单独替换（stock 的 #DC2626 是它的**跌**色：PALETTE 里跌色是绿的，
# 所以只在 stock 段把它映射成红——即「跌」→ 红，完成红涨绿跌的翻向）。
UNIFY_MAP_BY_DOMAIN: dict[str, tuple[tuple[str, str], ...]] = {
    "stock": (("#DC2626", "#D5423E"),),
}


def unify_colors(text: str, domain: str = "") -> str:
    """把模板里写死的十六进制色替换为统一调色板的等值色。

    domain : 给了就额外套用该域的专属映射（见 UNIFY_MAP_BY_DOMAIN）。
    """
    for old, new in UNIFY_MAP:
        text = text.replace(old, new)
    for old, new in UNIFY_MAP_BY_DOMAIN.get(domain, ()):
        text = text.replace(old, new)
    return text


# ---------------------------------------------------------------------------
# CSS 作用域化
# ---------------------------------------------------------------------------
# 不应加作用域前缀的选择器头（全局/根作用域）。注意：一旦跳过，该规则块
# 就是全局的 —— 所以调用方要先做"全局块改写"（见 _promote_globals）。
_GLOBAL_HEADS = ("html", "body", ":root", "*")

# 注释剥离（先剥再解析选择器，避免 `/* 说明 ===== */` 把后一个选择器吞掉）
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def scope_id(domain: str) -> str:
    return THEMES[domain].root_id


def _scope_one(sel: str, root: str) -> str:
    """给单个选择器加作用域前缀。"""
    sel = sel.strip()
    if not sel:
        return sel
    # 伪元素/伪类开头（如 @keyframes 的 0%/from/to，或 `::selection`）
    if sel.startswith("@"):
        return sel
    # :root -> 作用域根本身
    if sel == ":root":
        return f"#{root}"
    # ★ 已经是本域根（_promote_globals 改写产物）——不能再叠一层，
    #   否则会生成 `#app-stock #app-stock{}` 这种永远不匹配的死规则
    if sel == f"#{root}" or sel.startswith(f"#{root} "):
        return sel
    # 全局选择器直接跳过（保持全局；调用方负责把它们收进壳层）
    head = re.split(r"[\s>+~,:.\[]", sel, maxsplit=1)[0].strip()
    if head.lower() in _GLOBAL_HEADS or head in _GLOBAL_HEADS:
        return sel
    if sel.startswith("::") or sel.startswith(":where") or sel.startswith(":is"):
        return sel
    return f"#{root} {sel}"


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """按 sep 切分，但跳过括号内的分隔符（`:is(a,b)` / `:not(.x,.y)`）。"""
    out, buf, depth = [], [], 0
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _scope_selector_list(sel: str, root: str) -> str:
    """对一个逗号分隔的选择器列表逐个加前缀，保留原始空白风格。"""
    parts = _split_top_level(sel)
    scoped = []
    for p in parts:
        lead = p[: len(p) - len(p.lstrip())]
        trail = p[len(p.rstrip()):]
        scoped.append(lead + _scope_one(p.strip(), root) + trail)
    return ",".join(scoped)


def scope_css(css: str, domain: str) -> str:
    """把一段 CSS 里的选择器全部限定到 `#app-{domain}` 之下。

    实现要点（踩过的坑都在这里）：
      1. **先剥注释**：`/* ===== 布局 ===== */` 夹在两条规则之间时，
         不剥会被当成选择器一部分，导致**下一条规则的块体丢失**
         （曾经的 bug：`.card{...}/*注释*/.x{...}` -> `.x{...}` 的内容被吞）。
      2. **全局块改写**：`html`/`body`/`*`/`:root` 保持全局但重写为作用域根，
         否则 body 的背景色会被后加载的域覆盖。
      3. 递归处理 `@media`/`@supports` 内部规则。
      4. 清掉改写产生的空规则块（`#app-x{}`），减少无意义体积。
    """
    theme = THEMES[domain]
    root = theme.root_id
    css = _COMMENT_RE.sub("", css)
    css = _promote_globals(css, domain)      # body/html/*/:root -> 作用域根
    out = _rewrite_blocks(css, root)
    # 空规则块：只有全局块被改写/跳过后才会产生
    out = re.sub(r'#[\w-]+\{\s*\}', '', out)
    return out


def _rewrite_blocks(text: str, root: str) -> str:
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)

    while i < n:
        ch = text[i]
        if ch == "{":
            sel = "".join(buf).strip()
            buf = []
            if sel.startswith("@"):
                # @media / @supports / @keyframes：保留头，递归内部
                depth, j = 1, i + 1
                while j < n and depth:
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                    j += 1
                inner = text[i + 1: j - 1]
                if sel.startswith("@keyframes") or sel.startswith("@-webkit-keyframes"):
                    out.append(sel + "{" + inner + "}")     # 帧内不作用域化
                else:
                    out.append(sel + "{" + _rewrite_blocks(inner, root) + "}")
                i = j
                continue
            out.append(_scope_selector_list(sel, root))
            # ★ 声明体必须原样搬过去：找到配对的 `}`，整段复制
            #   （曾经的 bug：进到 `{` 后继续把 body 当选择器累积，
            #    结果 .card{color:red} 变成 #app-stock .card{} —— 声明全丢）
            depth, j = 1, i + 1
            while j < n and depth:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            out.append("{" + text[i + 1: j - 1] + "}")
            i = j
            continue
        if ch == "}":
            buf = []
            out.append("}")
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return "".join(out)


def _legacy_scope_css(css: str, domain: str) -> str:  # pragma: no cover - 保留参考
    """旧实现（有注释吞选择器的 bug），仅供对照，不再使用。"""
    theme = THEMES[domain]
    root = theme.root_id

    def _rewrite_block(text: str) -> str:
        out: list[str] = []
        i, n = 0, len(text)
        buf: list[str] = []

        while i < n:
            ch = text[i]
            if ch == "{":
                sel = "".join(buf)
                buf = []
                stripped = sel.strip()
                if stripped.startswith("@"):
                    out.append(sel)
                    depth, j = 1, i + 1
                    while j < n and depth:
                        if text[j] == "{":
                            depth += 1
                        elif text[j] == "}":
                            depth -= 1
                        j += 1
                    inner = text[i + 1:j - 1]
                    out.append("{" + _rewrite_block(inner) + "}")
                    i = j
                    continue
                out.append(_split_selectors_naive(sel, root, wrap=True))
                out.append("{")
                i += 1
                continue
            if ch == "}":
                buf = []
                out.append("}")
                i += 1
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
        return "".join(out)

    return _rewrite_block(css)


def _split_selectors_naive(sel: str, root: str, *, wrap: bool) -> str:  # pragma: no cover
    if not wrap:
        return sel
    parts = sel.split(",")
    return ",".join(_scope_one(p, root) for p in parts)


# ---------------------------------------------------------------------------
# 主题变量块：三域发同一份统一令牌
# ---------------------------------------------------------------------------
def theme_block(domain: str, *, extra_scope: bool = True) -> str:
    """生成该作用域的统一令牌块。

    ★ 三域内容完全相同（都来自 PALETTE）—— 外观已全站统一。仍逐域生成而不是只发
      一份到全局，是因为三域片段各自带 `<style>`、要能单独取出复用；代价几十字节。
    ★ `--up/--down` 必须显式写死：不写就会继承到上一个域或 shell 的值。
    """
    t = THEMES[domain]
    sel = f"#{t.root_id}" if extra_scope else ":root"
    lines = [f"{sel}{{",
             "  /* ★ 统一调色板 PALETTE（web/shell/scope.py）：三域同一份 */"]
    for k, v in PALETTE.items():
        lines.append(f"  {k}:{v};")
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ★ 语义组件层（唯一真相源 #2）—— 组件外观与配色无关，光靠 PALETTE 统一不了
# ---------------------------------------------------------------------------
# 三域的导航/标签是**三套独立长出来的实现**，类名几乎不重叠（全站只共有
# brand/card 两个 class），所以不能靠"同名类覆盖"，只能做**语义映射**：
# 这里定义「导航条目」这个语义的规范写法，再逐域映射到各自的类名
# （quant-lab `.nav-item` / etf `.lv1.lv2.lv3` / stock `.nav-item`）。
#
# 展开方式：每个选择器加 `#{root} ` 前缀后**放在域样式之后** → 同优先级靠
# 文档顺序取胜、伪类/状态类还天然多一层特异性。所以这里写的值一定赢。
def decl(props: dict[str, str]) -> str:
    """把声明字典拼成 CSS 声明体（组件层的公共拼装器）。

    build.py 的壳层（.qh-tab）也用它 —— 顶部导航标签和域内标签共用 CHIP 规范。
    """
    return "".join(f"{k}:{v};" for k, v in props.items())


assert decl({"a": "1px"}) == "a:1px;"          # 用法自检

# 可选中标签/药丸的规范：**白底黑字 → 选中黑底白字**（出处是 quant-lab 的
# `.tab`，全站统一到它）。壳层顶部导航也吃这一份，见 build.py。
CHIP: dict[str, str] = {
    "background": "var(--card)",
    "border": "1px solid var(--line)",
    "border-radius": "16px",
    "color": "var(--ink)",
    "cursor": "pointer",
    "font-family": "inherit",
    "font-size": "13px",
    "padding": "6px 16px",
}
CHIP_ON: dict[str, str] = {
    "background": "var(--ink)",
    "border-color": "var(--ink)",
    "color": "#fff",
    "font-weight": "600",
}
CHIP_HOVER: dict[str, str] = {"border-color": "var(--muted)", "color": "var(--ink)"}

# 侧栏容器：统一成「居中 flex + 192px 的 sticky 浮动白卡片」。
# stock 原本是占满整屏高的 fixed 侧栏，改这里要**连它的布局变量一起改** ——
# 变量与结构是解耦的（.main 的 margin-left、.sidebar 的 width 都是 var），
# 所以纯 CSS 就能换壳，不用动 DOM。
NAV_W = "192px"
NAV_CONTAINER: dict[str, str] = {
    "--nav-w": NAV_W,
    "--nav-pt": "16px",
    "--nav-pb": "28px",
    "--nav-gap": "22px",
    "background": "var(--card)",
    "border": "1px solid var(--line)",
    "border-radius": "var(--radius)",
    "flex": f"0 0 {NAV_W}",
    "max-height": "calc(100vh - var(--nav-pt) - var(--nav-pb))",
    "overflow-y": "auto",
    "padding": "14px 10px",
    "position": "sticky",
    "top": "var(--nav-pt)",
    "width": NAV_W,
}
NAV_LAYOUT: dict[str, str] = {
    "--nav-w": NAV_W,
    "--nav-pt": "16px",
    "--nav-pb": "28px",
    "--nav-gap": "22px",
    "align-items": "flex-start",
    "display": "flex",
    "gap": "var(--nav-gap)",
    "margin": "0 auto",
    "max-width": "1380px",
    "padding": "var(--nav-pt) var(--nav-pb) var(--nav-pb)",
}
# 导航条目：统一 padding/圆角/字号；**选中一律黑底白字**
NAV_ITEM: dict[str, str] = {
    "border-radius": "10px",
    "color": "var(--muted)",
    "cursor": "pointer",
    "display": "block",
    "font-size": "13.5px",
    "font-weight": "400",
    "padding": "8px 10px",
}
NAV_ITEM_ON: dict[str, str] = {
    "background": "var(--ink)",
    "color": "#fff",
    "font-weight": "600",
}
NAV_ITEM_HOVER: dict[str, str] = {"background": "var(--card-2)", "color": "var(--ink)"}

# 品牌区 / 分组标签 / 静态小药丸：只统一**形状**，语义色保留
#   ⚠️ 药丸的底色承载语义（候选/观察、风险中/高、共同基因/增强点…），
#      按用户口径"全部统一"把底色也抹平成白底黑字会**丢信息**，故不动底色。
BRAND_BLOCK: dict[str, str] = {
    "border-bottom": "1px solid var(--line)",
    "font-size": "14px",
    "font-weight": "600",
    "letter-spacing": ".5px",
    "line-height": "1.55",
    "margin-bottom": "10px",
    "padding": "2px 6px 10px",
}
GROUP_LABEL: dict[str, str] = {
    "color": "var(--muted)",
    "font-size": "11px",
    "letter-spacing": ".08em",
    "padding": "6px 10px",
    "text-transform": "uppercase",
}
PILL: dict[str, str] = {
    "border-radius": "999px",
    "display": "inline-block",
    "font-size": "11.5px",
    "line-height": "1.6",
    "padding": "2px 10px",
    "vertical-align": "1px",
}
# 桌面隐藏、仅 ≤900px 显示的导航触发按钮（汉堡）
NAV_TRIGGER: dict[str, str] = {"display": "none"}

# (语义名, 声明, {域: (选择器元组,)}) —— 选择器不含作用域前缀，展开时补
COMPONENTS: tuple[tuple[str, dict[str, str], dict[str, tuple[str, ...]]], ...] = (
    ("壳层布局", NAV_LAYOUT, {
        "quant-lab": (".shell",),
        "etf": (".layout",),
        "stock": (".main", ".content"),   # stock 的主列：外壳 + 内容容器
    }),
    ("侧栏容器", NAV_CONTAINER, {
        "quant-lab": (".side",),
        "etf": (".sidebar",),
        "stock": (".sidebar",),
    }),
    ("侧栏品牌区", BRAND_BLOCK, {
        "quant-lab": (".side .brand",),
        "etf": (".side-head",),
        "stock": (".sidebar-logo",),
    }),
    ("导航分组标签", GROUP_LABEL, {
        "stock": (".nav-group .group-label",),
    }),
    ("导航条目", NAV_ITEM, {
        "quant-lab": (".nav-item",),
        "etf": (".lv1", ".lv2", ".lv3"),
        "stock": (".nav-item",),
    }),
    ("导航条目 · 悬停", NAV_ITEM_HOVER, {
        "quant-lab": (".nav-item:hover",),
        "etf": (".lv1:hover", ".lv2:hover", ".lv3:hover"),
        "stock": (".nav-item:hover",),
    }),
    ("导航条目 · 选中（黑底白字）",
     {**NAV_ITEM_ON, "transition": "none"}, {
         "quant-lab": (".nav-item.on",),
         "etf": (".lv1.cur", ".lv2.cur", ".lv3.cur"),
         "stock": (".nav-item.active",),
     }),
    # etf 的 `›`/`▾` 箭头在选中态要跟着变白（否则深底上一枚深色箭头）
    ("导航条目 · 选中箭头", {"color": "inherit", "opacity": ".8"}, {
        "etf": (".lv1.cur .arr", ".lv2.cur .arr", ".lv3.cur .arr"),
    }),
    # ★ 可选中标签：域内三处（quant-lab .tab/.mode-btn、stock 因子筛选）
    ("标签药丸", CHIP, {
        "quant-lab": (".tab", ".mode-btn", ".fold-btn"),
        "stock": (".factor-filters .f",),
    }),
    ("标签药丸 · 悬停", CHIP_HOVER, {
        "quant-lab": (".tab:hover", ".mode-btn:hover"),
        "stock": (".factor-filters .f:hover",),
    }),
    ("标签药丸 · 选中（黑底白字）", CHIP_ON, {
        "quant-lab": (".tab.on", ".mode-btn.on"),
        "stock": (".factor-filters .f.active",),
    }),
    ("静态小药丸 · 形状", PILL, {
        "quant-lab": (".tag",),
        "etf": (".badge", ".risk"),
        "stock": (".badge", ".quick-tags .t", ".gene-tags .g",
                  ".refresh-btn", ".header h1 .tag"),
    }),
    ("移动端导航按钮（桌面隐藏）", NAV_TRIGGER, {
        "etf": (".menu-btn",),
        "stock": (".hamburger",),
    }),
)

# ≤900px 的导航行为：三域统一——quant-lab 收成横向一排，etf/stock 收成抽屉。
# ⚠️ 这段**必须**存在且排在最后：组件层的基础规则与域内媒体查询同优先级、
#    又写在它们之后，不在这里重申就会把域内的移动端抽屉样式顶掉。
_DRAWER: dict[str, str] = {
    "position": "fixed",
    "left": "0",
    "top": "0",
    "bottom": "0",
    "height": "100%",
    "width": "270px",
    "border-radius": "0",
    "border": "0",
    "border-right": "1px solid var(--line)",
    "max-height": "100%",
    "overflow-y": "auto",
    "z-index": "100",
    "transform": "translateX(-100%)",
    "transition": "transform .25s ease",
}

NAV_MEDIA: dict[str, dict[str, dict[str, str]]] = {
    "quant-lab": {
        ".shell": {"flex-direction": "column"},
        ".side": {"width": "100%", "flex": "none", "position": "static",
                  "max-height": "none", "display": "flex",
                  "align-items": "center", "gap": "8px", "padding": "10px 12px"},
        ".side .brand": {"border": "0", "margin": "0", "font-size": "14px",
                         "padding": "0 8px 0 2px"},
        ".nav-item": {"display": "inline-block"},
    },
    "etf": {
        ".layout": {"flex-direction": "column", "gap": "16px"},
        ".sidebar": dict(_DRAWER),
        ".sidebar.open": {"transform": "translateX(0)"},
        ".menu-btn": {"display": "flex"},
    },
    "stock": {
        ".sidebar": dict(_DRAWER),
        ".sidebar.show": {"transform": "translateX(0)"},
        ".main": {"margin-left": "0"},
        ".hamburger": {"display": "flex"},
    },
}


def component_css(domain: str, *, extra_scope: bool = True) -> str:
    """把语义组件层展开成该域的选择器 + 统一声明。

    extra_scope=False 时不加 `#app-{d}` 前缀（写进 `@media` 里用不到，
    但保留开关便于单测与复用）。
    """
    root = THEMES[domain].root_id
    pfx = f"#{root} " if extra_scope else ""
    lines: list[str] = []
    for name, props, targets in COMPONENTS:
        sels = targets.get(domain)
        if not sels:
            continue
        lines.append(f"/* {name} */")
        lines.append(",".join(pfx + s for s in sels) + "{" + decl(props) + "}")
    return "\n".join(lines)


def nav_media_css(domain: str) -> str:
    """该域的 ≤900px 导航响应式块。

    ★ 只发**本域**的规则：早期版本把三域的规则一起发，导致每个域的 `<style>`
      里都带着另两域的媒体查询（同一份规则在产物里重复 3 遍）。
    """
    root = THEMES[domain].root_id
    out = ["@media(max-width:900px){"]
    for sel, props in NAV_MEDIA[domain].items():
        out.append(f"  #{root} {sel}{{{decl(props)}}}")
    out.append("}")
    return "\n".join(out)


# 三域模板的全局块（html/body/*/:root）需要改写为作用域根，
# 否则后加载的域会覆盖前面的 body 背景/字体
_GLOBAL_HEADS_RE = r'(?:html\s*,\s*body|html|body|:root|\*)'
_GLOBAL_BLOCK_RE = re.compile(
    r'(?<![\w#.\-])(' + _GLOBAL_HEADS_RE + r')\s*(,[^{};/]*?)?\{', re.S)


def _promote_globals(css: str, domain: str) -> str:
    """把 `body{...}` / `*{...}` / `html,body{...}` 的选择器头改写为 `#app-{d}`。

    保留 `box-sizing` 之类的通用重置到作用域根，不泄漏到壳层与另两域。

    注意：只改**选择器头**，不动规则体。用 `(?<![\\w#.\\-])` 负向断言确保
    `#app-stock` 这类已带前缀的选择器不会被二次匹配（曾把 theme_block 整个吃掉）。
    """
    root = THEMES[domain].root_id

    def _sub(m: re.Match) -> str:
        # 只保留逗号后到 `{` 之间的部分？不 —— 全局头整体替换为作用域根，
        # 因为 html/body/*/:root 在语义上就是"本域根元素"。
        return f"#{root}{{"

    return _GLOBAL_BLOCK_RE.sub(_sub, css)


def scope_html_fragment(html: str, domain: str, *,
                        wrap: bool = True, rename: "set[str] | None" = None) -> str:
    """把一个域的整段 HTML（样式+结构）包进作用域容器。

    - 抽出 `<style>` 内容做选择器前缀化 + 全局块改写
    - 把**跨域重名**的 `id="x"` 重命名为 `id="{domain}__x"` 并同步更新 JS 里的引用
    - 结构包进 `<div id="app-{domain}">`

    rename : 需要加域前缀的 id 名集合（`build()` 跨三域算出的重名集合）。
             不给（None）则重命名该片段的全部 id —— 那是本模块最早的行为，
             只适合单域片段；三域合并必须传重名集合，理由见 `_namespace_ids`。
    """
    t = THEMES[domain]
    css_blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    body = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S)

    scoped_css = "\n".join(
        unify_colors(scope_css(_promote_globals(c, domain), domain), domain)
        for c in css_blocks
    )
    body = unify_colors(_namespace_ids(body, domain, rename), domain)

    # ★ 顺序关键：theme_block 必须放在域样式**之后**。
    #   模板自己的 `:root{}` 被 _promote_globals 改写成 `#app-{d}{}`，与本模块的
    #   令牌块**同优先级**，胜负只由文档顺序决定 —— 放前面会被模板原值覆盖，
    #   统一令牌静默失效（表现=颜色还是各域老样子，不报错）。
    #   component_css / nav_media_css 同理，且**必须**在域样式之后：
    #   它们是同优先级的覆盖层，还要顶掉域内已有的 @media 抽屉样式。
    out = [f'<style data-domain="{domain}">', scoped_css,
           component_css(domain), theme_block(domain), nav_media_css(domain),
           "</style>"]
    if wrap:
        out.append(scope_html_body(body, domain))
    else:
        out.append(body)
    return "\n".join(out)


def scope_html_body(body: str, domain: str) -> str:
    t = THEMES[domain]
    return f'<div id="{t.root_id}" class="qh-app" data-domain="{domain}">\n{body}\n</div>'


_ID_RE = re.compile(r'\bid="([\w-]+)"')

# 「只是转发到 getElementById」的包装函数：`function $(id){return document.getElementById(id);}`
# 或 `const el = id => document.getElementById(P + id);`。
#   ⚠️ 必须要求函数体**紧跟** return（或直接就是 getElementById）：否则像 quant-lab 的
#      `function initPage(P,...){ const el = id => document.getElementById(P+id); ... }`
#      会被误判成包装函数，把它的调用点也一起改名。
_WRAPPER_PATS = (
    r'function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{\s*(?:return\s+)?document\.getElementById',
    r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*[^;=]*=>\s*document\.getElementById',
)


def fragment_ids(html: str) -> set[str]:
    """片段里出现的全部 id 名 —— 供 `build()` 跨三域求重名集合。"""
    return set(_ID_RE.findall(html))


def _wrapper_funcs(html: str) -> set[str]:
    """找出包装着 getElementById 的函数名（`$`、`el` 之类）。

    调用点写的是裸字面量（`$("sidebar")`、`el('modeSw')`），并不是
    `getElementById("sidebar")`，所以只改 getElementById 字面量的正则追不到。
    """
    names: set[str] = set()
    for pat in _WRAPPER_PATS:
        names.update(re.findall(pat, html))
    return names


def _namespace_ids(html: str, domain: str, rename: "set[str] | None" = None) -> str:
    """`id="sidebar"` -> `id="stock__sidebar"`，并同步 JS 里的引用。

    etf 与 stock 都有 `id="sidebar"`，不重命名的话后者会抢到前者的节点
    （`getElementById` 只返回文档里第一个）。

    rename : 要加前缀的 id 名集合；None = 本片段全部 id（单域片段的老行为）。
             ★ 三域合并**必须**传跨域重名集合。全量前缀化会把只在本域出现的
             id 也改掉，而 `el('x')` / `$('x')` 这类间接引用改不到位 ——
             实测后果是那个脚本块整块中断，图表全空、区间切换与多张表失效
             （quant-lab 的 `initPage('', …)`，stock 的 37 处 `$()`）。

    ★ 必须**先改 JS 引用再改 id 属性**：反过来的话 id 属性已带前缀，
      JS 引用那一轮又会把 `etf__sidebar` 再套一层 -> `etf__etf__sidebar`。
    """
    pfx = f"{domain}__"

    def _bump(name: str) -> str:
        if rename is not None and name not in rename:
            return name
        return name if name.startswith(pfx) else pfx + name

    # 第一轮：JS 直连引用 document.getElementById("x") / getElementById("x")
    html = re.sub(r'document\.getElementById\(\s*(["\'])([\w-]+)\1\s*\)',
                  lambda m: f'document.getElementById("{_bump(m.group(2))}")', html)
    html = re.sub(r'(getElementById\(\s*)(["\'])([\w-]+)\2',
                  lambda m: f'{m.group(1)}"{_bump(m.group(3))}"', html)
    # 第二轮：包装函数实参 $("x") / el('x') —— 上面两条正则看不见这种写法
    wrappers = _wrapper_funcs(html)
    if wrappers:
        call_re = re.compile(
            r'(?<![\w$])(' + "|".join(
                map(re.escape, sorted(wrappers, key=len, reverse=True))) +
            r')\(\s*(["\'])([\w-]+)\2\s*\)')
        html = call_re.sub(lambda m: f'{m.group(1)}("{_bump(m.group(3))}")', html)
    # 第三轮：id 属性
    html = re.sub(r'\bid="([\w-]+)"',
                  lambda m: f'id="{_bump(m.group(1))}"', html)
    return html
