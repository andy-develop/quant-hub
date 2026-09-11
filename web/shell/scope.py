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
2. **涨跌色显式重绑**：三域的 `--up/--down` 语义不一致，必须逐域写死。

### ★ 涨跌色颠倒（方案 §5.2 已证实）

| 域 | `--up` | `--down` |
|----|--------|----------|
| 短线策略 | `#D5423E` 红 | `#1D9E75` 绿 |
| ETF 策略 | `#E0443C` 红 | `#16A34A` 绿 |
| **个性化选股** | **`#16A34A` 绿** | **`#DC2626` 红** |

前两域是中国惯例（红涨绿跌），第三域沿用了美股惯例。硬把三域统一会在
个性化选股里把**所有涨跌颜色翻译反**——用户看到的"涨"变成绿色。
本模块的做法：**保留各域自身颜色**，只在 shell 层显式声明，绝不隐式继承。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "DomainTheme",
    "THEMES",
    "scope_css",
    "scope_html_fragment",
    "scope_id",
    "theme_block",
]

# ---------------------------------------------------------------------------
# 逐域主题（★ 颜色为各域模板实测原值，不得"统一"）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DomainTheme:
    """一个域的隔离信息。"""

    key: str                 # 域标识：quant-lab / etf / stock
    root_id: str             # 作用域根元素 id
    up: str                  # 涨色（各域原值）
    down: str                # 跌色（各域原值）
    bg: str
    card: str
    ink: str
    line: str
    # 该域把涨跌色用在哪些变量名上（不同域命名不同，需要全部重绑）
    extra_vars: dict = field(default_factory=dict)


THEMES: dict[str, DomainTheme] = {
    # 短线策略（quant-lab）—— 中国惯例
    "quant-lab": DomainTheme(
        key="quant-lab", root_id="app-quant-lab",
        up="#D5423E", down="#1D9E75",
        bg="#F6F6F4", card="#FFFFFF", ink="#26251F", line="#E4E3DC",
    ),
    # ETF 策略（red-dividend-strategy）—— 中国惯例
    "etf": DomainTheme(
        key="etf", root_id="app-etf",
        up="#E0443C", down="#16A34A",
        bg="#F6FAFE", card="#FFFFFF", ink="#16324F", line="#DDE8F4",
        extra_vars={"--ink2": "#2B4A6F", "--sub": "#5B7BA3",
                    "--dim": "#8AA3C0", "--blue": "#2B6CB0"},
    ),
    # ★ 个性化选股（stock-factor-engine）—— 涨跌色颠倒，保留原样
    "stock": DomainTheme(
        key="stock", root_id="app-stock",
        up="#16A34A", down="#DC2626",   # ★ 绿涨红跌，与另两域相反
        bg="#F4F6FB", card="#FFFFFF", ink="#1A2040", line="#E2E6F0",
        extra_vars={"--card-2": "#EEF1F8", "--primary": "#2563EB",
                    "--primary-light": "#EFF4FF", "--accent": "#D97706",
                    "--text": "#1A2040", "--muted": "#5A6480",
                    "--muted-2": "#8B93A8"},
    ),
}


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
# 主题变量块：显式冻结各域的颜色语义
# ---------------------------------------------------------------------------
def theme_block(domain: str, *, extra_scope: bool = True) -> str:
    """生成该域的作用域变量块。

    ★ 关键是 `--up/--down` 显式写死：不写的话会继承到 shell 或上一个域的值，
      个性化选股的涨跌色就会被"统一"掉，所有颜色翻译反。
    """
    t = THEMES[domain]
    sel = f"#{t.root_id}" if extra_scope else ":root"
    lines = [
        f"{sel}{{",
        f"  --bg:{t.bg}; --card:{t.card}; --ink:{t.ink}; --line:{t.line};",
        f"  --up:{t.up}; --down:{t.down};   /* ★ 本域原值，勿统一 */",
    ]
    for k, v in sorted(t.extra_vars.items()):
        lines.append(f"  {k}:{v};")
    lines.append("}")
    return "\n".join(lines)


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


def scope_html_fragment(html: str, domain: str, *, wrap: bool = True) -> str:
    """把一个域的整段 HTML（样式+结构）包进作用域容器。

    - 抽出 `<style>` 内容做选择器前缀化 + 全局块改写
    - 把裸 `id="x"` 重命名为 `id="{domain}__x"` 并同步更新 JS 里的引用
    - 结构包进 `<div id="app-{domain}">`
    """
    t = THEMES[domain]
    css_blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    body = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S)

    scoped_css = "\n".join(
        scope_css(_promote_globals(c, domain), domain) for c in css_blocks
    )
    body = _namespace_ids(body, domain)

    out = [f'<style data-domain="{domain}">', theme_block(domain), scoped_css, "</style>"]
    if wrap:
        out.append(scope_html_body(body, domain))
    else:
        out.append(body)
    return "\n".join(out)


def scope_html_body(body: str, domain: str) -> str:
    t = THEMES[domain]
    return f'<div id="{t.root_id}" class="qh-app" data-domain="{domain}">\n{body}\n</div>'


_ID_RE = re.compile(r'\bid="([\w-]+)"')


def _namespace_ids(html: str, domain: str) -> str:
    """`id="sidebar"` -> `id="stock__sidebar"`，并同步 getElementById/querySelector。

    etf 与 stock 都有 `id="sidebar"`，不重命名的话後者会抢到前者的节点。

    ★ 必须**先改 JS 引用再改 id 属性**：反过来的话 id 属性已带前缀，
      JS 引用那一轮又会把 `etf__sidebar` 再套一层 -> `etf__etf__sidebar`。
    """
    pfx = f"{domain}__"

    def _bump(name: str) -> str:
        return name if name.startswith(pfx) else pfx + name

    # 第一轮：JS 引用
    html = re.sub(r'document\.getElementById\(\s*(["\'])([\w-]+)\1\s*\)',
                  lambda m: f'document.getElementById("{_bump(m.group(2))}")', html)
    html = re.sub(r'(getElementById\(\s*)(["\'])([\w-]+)\2',
                  lambda m: f'{m.group(1)}"{_bump(m.group(3))}"', html)
    # 第二轮：id 属性
    html = re.sub(r'\bid="([\w-]+)"',
                  lambda m: f'id="{_bump(m.group(1))}"', html)
    return html
