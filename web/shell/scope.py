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
    "ARIA_SELECTED",
    "NAV_MEDIA",
    "KEYBOARD_REACH",
    "CHIP",
    "CHIP_ON",
    "decl",
    "unify_colors",
    "unify_type",
    "unify_fonts",
    "unify_ink",
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
#
# 设计方向：「行情纸」（2026-09 二次统一）。不是 SaaS 仪表盘，是一张**当日行情纸**：
#   · 数字是主角 —— 等宽 + tabular-nums + 右对齐，列能对齐才扫得快
#   · 暖色是信息 —— 全站只有「涨/跌」和真警示用暖色，结构性颜色一律冷墨
#   · 一纸一天 —— 报头写明数据日与覆盖率，这是一份带日期的快照，不是活应用
#   · 硬朗不软 —— 圆角三档封顶 12px，靠发丝线分隔而不是阴影
# 具体到色值的理由：
#   --bg 冷纸白而非暖米（暖米 + 陶土色 = 生成式页面的头号默认脸）
#   --muted/--dim 提到 AA（旧值 #88867E / #A9A79E 在浅底上只有 3.3 / 2.3，全站 100+ 处不达标）
#   --up-ink/--down-ink 是涨跌的**文字档**：填充/线条用饱和的 --up/--down，
#     文字用更深的一档（#D5423E 在白底只有 4.49、在纸底 4.04，红绿数字全都读不清）
PALETTE: dict[str, str] = {
    # 底 / 卡片
    "--bg": "#FAFAFB",
    "--card": "#FFFFFF",
    "--card-2": "#F5F6F8",
    # 文字四档（全部 ≥4.5:1 于纸底与卡片）
    "--ink": "#14171C",
    "--ink2": "#2E333B",
    "--text": "#14171C",          # stock 的正文变量名
    "--muted": "#5C636E",         # 5.5:1 on --bg
    "--sub": "#5C636E",
    "--dim": "#646B76",           # 4.68:1 on --primary-light（最浅的那个底）
    "--muted-2": "#646B76",
    # 描边
    "--line": "#E1E4E9",
    # 强调色（结构性 = 冷墨蓝；暖色留给涨跌）
    "--primary": "#185FA5",
    "--blue": "#185FA5",
    "--primary-light": "#E8F0F9",
    "--accent": "#185FA5",
    "--warn": "#8A5300",          # 真警示（琥珀）—— 几何平均留暖，语义保留
    "--amber": "#8A5300",
    "--accent-warm": "#8A5300",
    "--warn-bg": "#FDF6E9",
    "--warn-line": "#EFD9AE",
    "--warn-ink": "#5A3600",       # 4.9:1 on 琥珀徽章 #EF9F27
    "--purple": "#534AB7",
    # ★ 涨跌：全站统一中国惯例（红涨绿跌）
    "--up": "#D5423E",            # 涨 · 填充/线条（锁：tests/web/test_scope.py）
    "--down": "#1D9E75",          # 跌 · 填充/线条（锁）
    "--up-ink": "#B3342F",        # 涨 · 文字（5.5:1 on --bg）
    "--down-ink": "#0F6E56",      # 跌 · 文字（5.6:1 on --bg）
    # 形状
    "--radius": "8px",
    "--r-sm": "4px",
    "--r-md": "8px",
    "--r-lg": "12px",
    "--shadow": "none",
    # 字号阶（6 档 + 2 个行高）—— 取代模板里 20 个随手写的大小
    "--fs-xs": "11px",
    "--fs-sm": "12px",
    "--fs-md": "13px",
    "--fs-base": "14px",
    "--fs-lg": "16px",
    "--fs-xl": "20px",
    "--fs-2xl": "26px",
    "--lh-tight": "1.3",
    "--lh-base": "1.6",
    "--measure": "68ch",          # 正文行长上限
    # 字体：正文=中文屏幕黑体（三域共用同一支），数字=等宽（行情纸的本行）
    "--num": 'ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,"Liberation Mono",monospace',
    "--font-text": '"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC","Noto Sans CJK SC",system-ui,sans-serif',
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
#  注意：**目标值一律引用 PALETTE**，不再写第二份字面量 —— 旧版把目标值写成
#  #FAFAF7/#F6F6F4 之类的字面量，改 PALETTE 时这里会静默不同步（"换了个调色板、
#  模板写死的色还留在旧调色板"）。
UNIFY_MAP: tuple[tuple[str, str], ...] = (
    # 浅底 → 基准中性浅底
    ("#F6FAFE", PALETTE["--card-2"]), ("#F0F6FD", PALETTE["--card-2"]),
    ("#F0F7FE", PALETTE["--card-2"]), ("#EAF2FB", PALETTE["--card-2"]),
    ("#F8FBFE", PALETTE["--card-2"]), ("#EEF4FA", PALETTE["--card-2"]),
    ("#EFF4FF", PALETTE["--card-2"]), ("#F4F6FB", PALETTE["--bg"]),
    ("#EEF1F8", PALETTE["--card-2"]),
    # 描边（冷蓝灰 → 中性灰）
    ("#DDE8F4", PALETTE["--line"]), ("#E2E6F0", PALETTE["--line"]),
    ("#D1D5DB", PALETTE["--line"]), ("#EAF1F9", PALETTE["--line"]),
    # 次级文字 / 坐标轴（多写在图表 JS 里，CSS 够不着）
    ("#8AA3C0", PALETTE["--sub"]), ("#5B7BA3", PALETTE["--sub"]),
    ("#5A6480", PALETTE["--sub"]), ("#8B93A8", PALETTE["--sub"]),
    ("#6B7280", PALETTE["--sub"]),
    ("#374151", PALETTE["--ink2"]), ("#2B4A6F", PALETTE["--ink2"]),
    # 正文（藏青 → 墨黑）
    ("#16324F", PALETTE["--ink"]), ("#1A2040", PALETTE["--ink"]),
    # ★ 旧调色板兜底：这些是**上一版 PALETTE 的原值**（不是随手写的某个蓝/灰），
    #   而且写在选择器 / 内联 style / 图表 JS 里 —— 不走 `:root` 令牌块，
    #   改 PALETTE 时不会跟着变，页面就成了“新调色板 + 旧色”的混搭。
    #   实测漏网：quant-lab 的 `.sub-h`/`.doc h3` 标题（旧墨）、`.nav-item:hover`
    #   与 `tr:hover td` 底色（旧卡底）、图表坐标轴/图例文字（旧灰 3.3:1）。
    #   一律指回当前 token；换代时只需改 PALETTE。
    ("#F6F6F4", PALETTE["--bg"]), ("#FAFAF7", PALETTE["--card-2"]),
    ("#E4E3DC", PALETTE["--line"]), ("#F0EFE9", PALETTE["--line"]),
    ("#26251F", PALETTE["--ink"]), ("#3D3C34", PALETTE["--ink2"]),
    ("#44423B", PALETTE["--ink2"]),
    ("#88867E", PALETTE["--sub"]), ("#A9A79E", PALETTE["--dim"]),
    ("#854F0B", PALETTE["--warn"]), ("#633806", PALETTE["--warn-ink"]),
    # etf 空态卡片的虚线描边还是旧蓝 —— 结构色一律中性
    ("#B9CFE6", PALETTE["--line"]),
    # 强调蓝
    ("#2B6CB0", PALETTE["--primary"]), ("#4A8FD4", PALETTE["--primary"]),
    ("#2563EB", PALETTE["--primary"]), ("#1D4ED8", PALETTE["--primary"]),
    ("#DBEAFE", PALETTE["--primary-light"]),
    ("rgba(43,108,176,", "rgba(24,95,165,"),
    ("rgba(37,99,235,", "rgba(24,95,165,"),
    # 基准线（金色虚线 → 调色板里的金）
    ("#8A6D3B", "#B07A2A"),
    # 琥珀警示 → 调色板的 warn（已经过 AA 校对）
    ("#D97706", PALETTE["--warn"]), ("#B45309", PALETTE["--warn"]),
    ("#78350F", PALETTE["--warn-ink"]), ("#8a6420", PALETTE["--warn-ink"]),
    ("#FBBF24", "#EF9F27"), ("#FCD9B6", "#FAEEDA"),
    ("#FDF8EC", PALETTE["--warn-bg"]), ("#F3E2B5", PALETTE["--warn-line"]),
    # 涨跌"填充档"色相 → PALETTE 的 --up/--down
    ("#E0443C", PALETTE["--up"]), ("#C0392B", PALETTE["--up"]),
    ("#16A34A", PALETTE["--down"]), ("#177245", "#0F6E56"),
    ("#F0F9F2", "#E1F5EE"),
    # 弹窗遮罩 / 阴影（藏青 → 墨黑）
    ("rgba(30,41,80,", "rgba(20,23,28,"),
    ("rgba(22,50,79,", "rgba(20,23,28,"),
    # ★ 投影：整站只留一个中性档。旧的「蓝色发光按钮」是生成式页面的典型 tell，
    #   与 PALETTE 自带的 --shadow:none（发丝线分隔）也矛盾；
    #   只有真的浮在内容之上的层（抽屉 / 吐司）才留一层中性投影。
    ("box-shadow:0 4px 14px rgba(24,95,165,.35)", "box-shadow:none"),
    ("box-shadow:0 4px 12px rgba(24,95,165,.35)", "box-shadow:none"),
    ("box-shadow:0 4px 16px rgba(0,0,0,.2)", "box-shadow:0 4px 16px rgba(20,23,28,.16)"),
    ("rgba(0,0,0,.12)", "rgba(20,23,28,.08)"),
)

# 逐域单独替换（stock 的 #DC2626 是它的**跌**色：PALETTE 里跌色是绿的，
# 所以只在 stock 段把它映射成红——即「跌」→ 红，完成红涨绿跌的翻向）。
UNIFY_MAP_BY_DOMAIN: dict[str, tuple[tuple[str, str], ...]] = {
    "stock": (("#DC2626", PALETTE["--up"]),
              # ★ rgba 形式的旧涨跌底色：UNIFY_MAP 只认 #hex，这两个漏在下面，
              #   结果是"涨"行拿着绿的底（22,163,74）+ 红的字（var(--up)）——
              #   翻向只翻了一半。只在本域翻：绿色 rgba 在别的域可能是合法的跌底色。
              ("rgba(22,163,74,", "rgba(213,66,62,"),
              ("rgba(220,38,38,", "rgba(29,158,117,"),),
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
# ★ 字号 / 圆角 / 字体同一化：模板里 20 个随手写的 font-size、9 个随手写的圆角、
#   4 支各不相同的 font-family，在这里收敛到 PALETTE 的字号阶 / 圆角档 / 两支字体。
# ---------------------------------------------------------------------------
# 和 UNIFY_MAP 同一个套路、同一处生效范围（整段片段：CSS 与 JS 拼串一起改）。
# 理由也一样：模板是「三域原样并入、口径零改动」的，不该为了排版去逐个改模板。
#
# ★ 这是按**字面量**替换：上游改了写法（大小写、压缩空格）就静默落空 —— 表现是
#   "字号又散了"，不报错。改模板排版后请顺手核对这里（tools/ 无自动体检，靠审计）。
#
# 字号映射原则：就近归到阶上，**不改变量级**（12.5→13 而不是→14）。
# 10/10.5/10.8333 -> --fs-xs(11)   （旧值 10~11.5 共 5 种挤在一起，本质是同一个「小字」）
# 11 -> xs · 11.5/12 -> sm · 12.5/13 -> md · 13.5/14/14.5 -> base
# 15/16 -> lg · 17/18/19/20 -> xl · 22/24/26/32 -> 2xl
_TYPE_MAP: tuple[tuple[str, str], ...] = (
    ("font-size:10px", "font-size:var(--fs-xs)"),
    ("font-size:10.5px", "font-size:var(--fs-xs)"),
    ("font-size:10.8333px", "font-size:var(--fs-xs)"),
    ("font-size:11px", "font-size:var(--fs-xs)"),
    ("font-size:11.5px", "font-size:var(--fs-sm)"),
    ("font-size:12px", "font-size:var(--fs-sm)"),
    ("font-size:12.5px", "font-size:var(--fs-md)"),
    ("font-size:13px", "font-size:var(--fs-md)"),
    ("font-size:13.5px", "font-size:var(--fs-base)"),
    ("font-size:14px", "font-size:var(--fs-base)"),
    ("font-size:14.5px", "font-size:var(--fs-base)"),
    ("font-size:15px", "font-size:var(--fs-lg)"),
    ("font-size:16px", "font-size:var(--fs-lg)"),
    ("font-size:17px", "font-size:var(--fs-xl)"),
    ("font-size:18px", "font-size:var(--fs-xl)"),
    ("font-size:19px", "font-size:var(--fs-xl)"),
    ("font-size:20px", "font-size:var(--fs-xl)"),
    ("font-size:22px", "font-size:var(--fs-2xl)"),
    ("font-size:24px", "font-size:var(--fs-2xl)"),
    ("font-size:26px", "font-size:var(--fs-2xl)"),
    ("font-size:32px", "font-size:var(--fs-2xl)"),
    ("font-size:9px", "font-size:var(--fs-xs)"),
)

# 圆角：三档封顶 12px。50%（正圆）与 999px（药丸）语义明确，保持不动。
_RADIUS_MAP: tuple[tuple[str, str], ...] = (
    ("border-radius:2px", "border-radius:var(--r-sm)"),
    ("border-radius:3px", "border-radius:var(--r-sm)"),
    ("border-radius:4px", "border-radius:var(--r-sm)"),
    ("border-radius:5px", "border-radius:var(--r-sm)"),
    ("border-radius:6px", "border-radius:var(--r-sm)"),
    ("border-radius:7px", "border-radius:var(--r-sm)"),
    ("border-radius:8px", "border-radius:var(--r-md)"),
    ("border-radius:9px", "border-radius:var(--r-md)"),
    ("border-radius:10px", "border-radius:var(--r-md)"),
    ("border-radius:12px", "border-radius:var(--r-md)"),
    ("border-radius:14px", "border-radius:var(--r-md)"),
    ("border-radius:16px", "border-radius:var(--r-lg)"),
    ("border-radius:18px", "border-radius:var(--r-lg)"),
    ("border-radius:20px", "border-radius:var(--r-lg)"),
    ("border-radius:24px", "border-radius:var(--r-lg)"),
)

def unify_type(text: str) -> str:
    """字号 / 圆角字面量 -> 字号阶与圆角令牌。"""
    for old, new in _TYPE_MAP:
        text = text.replace(old, new)
    for old, new in _RADIUS_MAP:
        text = text.replace(old, new)
    # ★ 全大写 + 大字距的分组标签是生成式页面的常驻装饰，而且中文没有大小写，
    #   它只对夹在中间的英文生效（stock 的「核心功能 / 关于」实际没变）——去掉。
    text = text.replace("text-transform:uppercase", "text-transform:none")
    return text


# 字体：三域各长了一支，同一域内还混用两支 —— 统一成「正文一支、数字一支」。
# ★ 用正则而不是字面量表：同一支栈在模板里有单引号/双引号两种写法，字体还可能
#   藏在 `font:` 简写里（`font:14px/1.6 -apple-system,...`）。字面量替换只认字符，
#   引号或写法一变就静默漏掉 —— quant-lab 的 302 个节点就是这么漏的（整个域
#   仍在用旧字体，而记分卡看起来“只剩 1 支”）。
_LEGACY_FAMS = ("-apple-system", "BlinkMacSystemFont", "PingFang", "YaHei",
                "Hiragino", "Helvetica", "Segoe UI", "Roboto", "Arial",
                "sans-serif", "monospace", "system-ui")
_MONO_FAMS = ("monospace", "Menlo", "Consolas", "Courier", "ui-monospace")
# 只认「纯字体声明」的字面形状：出现 + ( ) ? : 的都是在拼 JS 字符串
# （`font-family:"+d+"` 是 Canvas 的 ctx.font），改了会把图表字体拼坏。
# ★ 值里必须允许连字符 `-`：字体名本身常带（`-apple-system`、`JetBrains Mono` 的
#   前缀、`ui-monospace`）。旧字符类漏了 `-`，于是 `font:14px/1.6 -apple-system,…`
#   匹配在 `-` 处断掉、整条声明静默漏掉 —— quant-lab 全域 302 个节点卡在旧字体栈。
_FONT_DECL_RE = re.compile(r"\b(font(?:-family)?)\s*:\s*([A-Za-z0-9,\s'\"_./%-]+)")
_FONT_VALUE_OK = re.compile(r"^[A-Za-z0-9,\s'\"_./%-]+$")


def _unify_one_font(m: re.Match[str]) -> str:
    prop, value = m.group(1), m.group(2)
    if not _FONT_VALUE_OK.match(value):
        return m.group(0)
    # 取**最靠前**的旧字体名做切口：tail 里含 monospace 才判等宽。
    idx = min((value.find(f) for f in _LEGACY_FAMS if f in value), default=-1)
    if idx < 0:
        return m.group(0)
    head = value[:idx]
    if head.endswith(("'", '"')):        # font-family:"PingFang SC",... 的开引号
        head = head[:-1]
    tail = value[idx:]
    tok = "var(--num)" if any(f in tail for f in _MONO_FAMS) else "var(--font-text)"
    return f"{prop}:{head}{tok}"


def unify_fonts(text: str) -> str:
    """把模板里写死的字体栈换成 var(--font-text) / var(--num)。"""
    return _FONT_DECL_RE.sub(_unify_one_font, text)


# ---------------------------------------------------------------------------
# ★ 涨跌的「文字档」：模板里凡是以 --up/--down 当**文字色**的地方，一律换成
#   更深的那一档。在源头改（改模板自己的规则）而不是另发一条覆盖规则，
#   是因为模板里存在 `#app-stock .gene-tags .g.up{color:var(--up)}` 这种
#   三层特异性的写法 —— 系统层发 `#app-stock .up` 根本盖不住（实测白写）。
#   `--up`（填充档）继续给色块、线条、图表用。
#   注意 background 用的是 rgba 浅底（不是 var(--up)），不能“有 background 就跳过”，
#   否则 `.g.up{background:rgba(213,66,62,.08);color:var(--up)}` 也会被跳过 ——
#   所以判断的是 background 的**值**是不是涨跌色。
_DECL_BLOCK_RE = re.compile(r"\{([^{}]*)\}")
_FILLED_RE = re.compile(
    r"background(?:-color)?\s*:\s*(?:var\(--(?:up|down)\)|#[Dd]5423[eE]|#1[dD]9[eE]75)")
_INK_RE = re.compile(r"\bcolor\s*:\s*(var\(--up\)|#D5423E|var\(--down\)|#1D9E75)")
_INK_TO = {"var(--up)": "var(--up-ink)", "#D5423E": "var(--up-ink)",
           "var(--down)": "var(--down-ink)", "#1D9E75": "var(--down-ink)"}


def _ink_one_block(m: re.Match[str]) -> str:
    body = m.group(1)
    if "color" not in body or _FILLED_RE.search(body):
        return m.group(0)
    return "{" + _INK_RE.sub(
        lambda mm: "color:" + _INK_TO[mm.group(1)], body) + "}"


# 模板里还有写死的内联 `<b style="color:#D5423E">`（etf 的「已触发」就是）——
# 内联样式没有 { }，块正则抡不到，得单独再过一遍 style="..."。
_STYLE_ATTR_RE = re.compile(r"style\s*=\s*(\"[^\"]*\"|'[^']*')")


def unify_ink(text: str) -> str:
    """涨跌色当文字时降到「文字档」（填充档不动）。

    两道：CSS 声明块 + 内联 style 属性。
    """
    text = _DECL_BLOCK_RE.sub(_ink_one_block, text)
    return _STYLE_ATTR_RE.sub(
        lambda m: "style=" + _INK_RE.sub(
            lambda mm: "color:" + _INK_TO[mm.group(1)], m.group(1)), text)




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
    "border-radius": "var(--r-sm)",
    "color": "var(--ink)",
    "cursor": "pointer",
    "font-family": "inherit",
    "font-size": "var(--fs-md)",
    "padding": "6px 14px",
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
    "border-radius": "var(--r-lg)",
    "flex": f"0 0 {NAV_W}",
    "max-height": "calc(100vh - var(--nav-pt) - var(--nav-pb))",
    "overflow-y": "auto",
    "padding": "12px 8px",
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
    "border-radius": "var(--r-sm)",
    "color": "var(--muted)",
    "cursor": "pointer",
    "display": "block",
    "font-size": "var(--fs-base)",
    "font-weight": "400",
    "padding": "7px 10px",
}
NAV_ITEM_ON: dict[str, str] = {
    "background": "var(--ink)",
    "color": "#fff",
    "font-weight": "600",
}
NAV_ITEM_HOVER: dict[str, str] = {"background": "var(--card-2)", "color": "var(--ink)"}

# ★ 短线域的域内导航（动量策略 / 量化黑盒）在用户口径里就是**标签** ——
#   和顶部 tag（build.py 的 `.qh-tab`）吃**同一份 CHIP 规范**：白底 + 发丝边 +
#   4px 圆角 → 选中黑底白字。差别只在排布：这里是侧栏里的一列（display:block），
#   顶部是一行 inline 的按钮。
#   etf/stock 的侧栏是层级目录树（etf 有 8 条、带缩进），套边框会变成一列盒子，
#   反而更乱，所以保持「无框条目」的 NAV_ITEM。
NAV_ITEM_CHIP: dict[str, str] = {**CHIP, "display": "block", "font-weight": "400"}

# 品牌区 / 分组标签 / 静态小药丸：只统一**形状**，语义色保留
#   ⚠️ 药丸的底色承载语义（候选/观察、风险中/高、共同基因/增强点…），
#      按用户口径"全部统一"把底色也抹平成白底黑字会**丢信息**，故不动底色。
BRAND_BLOCK: dict[str, str] = {
    "border-bottom": "1px solid var(--line)",
    "font-size": "var(--fs-lg)",
    "font-weight": "600",
    "letter-spacing": ".01em",
    "line-height": "1.5",
    "margin-bottom": "10px",
    "padding": "2px 6px 10px",
}
# ★ 不再吃 uppercase + 大字距：跟踪全大写的分组标签是生成式页面的常驻装饰，
#   而且中文根本没有大小写，它只对夹在中间的英文生效 —— 统一成正常的次级标签。
GROUP_LABEL: dict[str, str] = {
    "color": "var(--muted)",
    "font-size": "var(--fs-xs)",
    "font-weight": "600",
    "letter-spacing": ".02em",
    "padding": "6px 10px",
}
PILL: dict[str, str] = {
    "border-radius": "999px",
    "display": "inline-block",
    "font-size": "var(--fs-xs)",
    "line-height": "1.5",
    "padding": "2px 8px",
    "vertical-align": "1px",
}
# 桌面隐藏、仅 ≤900px 显示的导航触发按钮（汉堡）
NAV_TRIGGER: dict[str, str] = {"display": "none"}

# 
# ★ 键盘可达性：下面这些是**点击目标**，但浏览器原生聚焦不到 ——
#   quant-lab 的 `.nav-item` 是**没有 href 的 `<a>`**（Chrome 里 `tabIndex` 返回 0
#   但 `focus()` 不生效，实测 `document.activeElement` 不跟着走），
#   etf/stock 的导航条目、区间 chip、因子筛选干脆是 `<div>`/`<span>`。
#   实测改前 quant-lab 整域只有 **7 个 Tab 落点**（3 个顶部 tag + 2 个按钮 + 2 张卡），
#   域内导航和 12 个区间 chip 全在 Tab 序列之外 —— 键盘用户根本切不了页。
#   → 壳层（build.py 的 SHELL_JS）据此补 `tabindex="0"` + `role="button"`，
#     并把 Enter/Space 映射成 `click()`（复用模板原有处理器，不改模板）。
KEYBOARD_REACH: dict[str, tuple[str, ...]] = {
    "quant-lab": (".side .nav-item", ".tab"),
    "etf": (".lv1", ".lv2", ".lv3"),
    "stock": (".nav-item", "[data-add]", "[data-f]", "[data-rec]", ".search-item"),
}

# ★ 选中态的语义：三域都用「加一个类名」表示「我被选中 / 当前就在这里」，
#   但类名只对眼睛可见 —— 屏幕阅读器读不出「你现在在哪一页」。
#   实测（改前）：Tab 到导航条目按回车真的切了页，但 AT 里毫无提示。
#
#   每条 = (要标记的元素, 选中类名, aria 属性, 值, 承载类名的元素或 None)：
#     · 第 5 项为 None   → 看元素**自己**有没有那个类名（多数情况）
#     · 第 5 项为 (选,类) → 看**另一个**元素有没有那个类名。
#          抽屉（`#menu-btn`/`#hamburger`）的开关状态长在 `#sidebar` 上，
#          但要读的是按钮 —— 屏幕阅读器要知道这个按钮展开的是什么、现在展没展开。
#     · `#sidebar` 这类会重名的 id 在构建时被 `_namespace_ids` 改成 `#<域>__sidebar`，
#          所以载体一律用**类名**（`.sidebar`）—— 类名不重命名，且天然按域隔离。
#          踩过的坑：写 `"#sidebar"` 时 `root.querySelector` 返回 null，规则**静默全空**
#          （按钮永远 `aria-expanded="false"`，看不出报错）。
#
#   为什么选 `aria-current="page"` 而不是 `role="tablist"`：
#     `role="tab"` 有一整套硬性要求（方向键在组内移动、`aria-controls` 指向面板、
#     面板 `role="tabpanel"` + `tabindex`），半套反而比不做更糟。顶部三个 tag
#     先用「按钮 + `aria-current`」，真要上 tablist 得连方向键一起做（待专项）。
ARIA_SELECTED: dict[str, tuple[tuple[str, str | None, str, str,
                                    tuple[str, str] | None], ...]] = {
    "quant-lab": (
        (".side .nav-item", "on", "aria-current", "page", None),
        (".tab", "on", "aria-pressed", "true", None),
        (".mode-btn", "on", "aria-pressed", "true", None),
    ),
    "etf": (
        ("[data-key]", "cur", "aria-current", "page", None),
        (".lv1", "open", "aria-expanded", "true", None),
        (".menu-btn", None, "aria-expanded", "true", (".sidebar", "open")),
    ),
    "stock": (
        (".nav-item", "active", "aria-current", "page", None),
        ("[data-f]", "active", "aria-pressed", "true", None),
        ("[data-rec]", "sel", "aria-pressed", "true", None),
        (".hamburger", None, "aria-expanded", "true", (".sidebar", "show")),
    ),
}

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
    # `<small>` 是 UA 默认字号 10.83px（不在 6 档字号阶里）—— etf 的检查项注释就是它。
    # 模板里 `.pos-cell .val small` / `.bt-cell .val small` 写死 12px，
    # 那两条带两档特异性会自然压住本条，不必动。
    ("单位小字（<small>）", {"font-size": "var(--fs-xs)"}, {
        "quant-lab": ("small",),
        "etf": ("small",),
        "stock": ("small",),
    }),
    ("导航条目", NAV_ITEM, {
        "etf": (".lv1", ".lv2", ".lv3"),
        "stock": (".nav-item",),
    }),
    ("导航条目 · 悬停", NAV_ITEM_HOVER, {
        "etf": (".lv1:hover", ".lv2:hover", ".lv3:hover"),
        "stock": (".nav-item:hover",),
    }),
    ("导航条目 · 选中（黑底白字）",
     {**NAV_ITEM_ON, "transition": "none"}, {
         "etf": (".lv1.cur", ".lv2.cur", ".lv3.cur"),
         "stock": (".nav-item.active",),
     }),
    # ★ 短线域导航 = 标签：与顶部 tag 同规范（见 NAV_ITEM_CHIP 的注释）。
    #   选择器写成 `.side .nav-item`（多一档特异性），这样 ≤900px 把导航转成
    #   横排的 `display:inline-block` 仍然压得住（NAV_MEDIA 里同步加了 .side）。
    ("短线域导航标签", NAV_ITEM_CHIP, {
        "quant-lab": (".side .nav-item",),
    }),
    ("短线域导航标签 · 悬停", CHIP_HOVER, {
        "quant-lab": (".side .nav-item:hover",),
    }),
    ("短线域导航标签 · 选中（黑底白字）", CHIP_ON, {
        "quant-lab": (".side .nav-item.on",),
    }),
    # 副标题（"趋势质量轮动 · 日频"）也跟顶部 tag 的 .qh-note 对齐：
    # 模板写的是 opacity:.65 / 行高继承 1.6，顶部是 .78 / 1.25，
    # 且选中态会从 .nav-item.on 继承到 600 粗体（顶部是 400）
    ("短线域导航标签 · 副标题",
     {"font-weight": "400", "line-height": "1.25", "opacity": ".78"}, {
         "quant-lab": (".side .nav-item .nav-note",),
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
    # ★ 触控底线：`.quick-tags .t` 是**可点的**（〈快速加自选），只有 23px 高。
    #   键盘可达补齐后，它从「装饰」变成真正的「触点」——以后归 32px 那一档管。
    #   `.gene-tags .g` 是纯展示（共同基因 / 增强点），不跟：小药丸的紧凑是它的作用。
    #   注：stock 模板是 content-box（没有 `*{box-sizing:border-box}`），min-height 量的是
    #   内容盒 —— 不显式声明 border-box，32px 会变成 38px（实测）。
    ("可点小药丸 · 触控底线",
     {"align-items": "center", "box-sizing": "border-box", "display": "inline-flex",
      "min-height": "32px", "padding": "0 10px"}, {
         "stock": (".quick-tags .t",),
     }),
    ("移动端导航按钮（桌面隐藏）", NAV_TRIGGER, {
        "etf": (".menu-btn",),
        "stock": (".hamburger",),
    }),

    # ================= 以下为「行情纸」二次统一新增 =================
    # ★ 数字是主角：表格里的数位必须等宽，否则每行小数点对不齐、扫读时要重新找位。
    #   只上 tabular-nums、不动字体 —— 中文列（股票名、说明）仍走正文那一支，
    #   把整张表都换成等宽会让中英混排的字体跟个不同。
    ("数据列 · 定宽数位",
     {"font-variant-numeric": "tabular-nums",
      "font-feature-settings": '"tnum" 1'}, {
          "quant-lab": ("table th", "table td", ".num", ".val"),
          "etf": ("table th", "table td", ".num", ".val", ".v"),
          "stock": ("table th", "table td", ".num", ".score", ".value"),
      }),
    ("纯数字字面 · 等宽", {"font-family": "var(--num)"}, {
        "quant-lab": (".num", ".val", ".kpi b"),
        "etf": (".num", ".pos-cell .val", ".metric .v"),
        "stock": (".num", ".score", ".factor-val"),
    }),
    # ★ 涨跌分两档（填充档 / 文字档）：--up/--down 是给色块与线条的饱和色，
    #   当文字用在白底只有 4.49（纸底 4.04）/3.39，红绿数字全都不达标。
    #   实际换档在 unify_ink() 里做 —— 模板里存在三层特异性的写法
    #   （`#app-stock .gene-tags .g.up{color:var(--up)}`），在这层发覆盖规则盖不住。
    # ★ 行长上限：模板里有 161~185 字符的正文行（三倍于易读上限），
    #   只限正文类容器，不动表格（表格要的就是宽）。
    ("正文行长上限", {"max-width": "var(--measure)"}, {
        "quant-lab": (".sub", ".mode-note", ".note"),
        "etf": (".desc", ".tip", ".note", ".bt-note", ".gauge-nums", "summary", "p"),
        "stock": (".desc", ".sub", ".note", "p"),
    }),
    # ★ 表格桌面密排：发丝线分隔 + 不贴边；移动端另在 <900px 里放开换行
    ("表格密度", {"border-color": "var(--line)"}, {
        "quant-lab": ("table th", "table td"),
        "etf": ("table th", "table td"),
        "stock": ("table th", "table td"),
    }),
    # ★ 点击目标：stock 的删除按钮原本 26x20（低于任何可用性下限）
    ("次要点击目标下限",
     {"min-height": "32px", "min-width": "32px",
      "display": "inline-flex", "align-items": "center",
      "justify-content": "center"}, {
          "quant-lab": (".fold-btn",),
          "stock": (".rm", ".mini-refresh", ".refresh-btn"),
      }),
    # ★ 表单控件不吃继承：没声明 font-family 时，input 会用浏览器默认的
    #   Arial 13.3px —— 于是「三域字体已统一」的页面里会单独冒出一个 Arial。
    ("表单控件继承排版", {"font-family": "inherit", "font-size": "inherit"}, {
        "quant-lab": ("input", "select", "textarea", "button"),
        "etf": ("input", "select", "textarea", "button"),
        "stock": ("input", "select", "textarea", "button"),
    }),
    # ★ etf 的编号方块挂了蓝色发光（旧调色板遗留），行情纸不要发光
    ("编号方块 · 去发光", {"box-shadow": "none"}, {"etf": (".sec-n",)}),
    # ★ stock 的侧栏是 fixed（已脱离文档流），.main/.content 不该再当 flex 行容器：
    #   一旦变成 flex row，里面的 .view/.content 就变成紧挨的 flex item、
    #   宽度取 max-content —— 移动端实测 545px > 390px 视口，页面横向滚。
    #   放在「壳层布局」之后同优先级覆盖。★ 千万不能顺手加 width:100%：
    #   .main 左边已经让给了 fixed 侧栏，再加 100% 就是 100% + 侧栏宽 ——
    #   实测 1440 视口反而溢出 26px（修好一个域又壊了两个视口）。
    ("主内容列 · 块流",
     {"display": "block", "min-width": "0"}, {
         "stock": (".main", ".content"),
     }),
    # ★ 宽表：quant-lab 最大的表 1260px 宽，撑得整页横向滚。
    #   flex 子项默认 min-width:auto（= min-content = 表宽），不会缩 ——
    #   必须显式 min-width:0，滚条才会落在卡片里而不是整页。
    ("主内容列可压缩", {"min-width": "0"}, {
        "quant-lab": (".wrap",),
        "etf": (".main",),
        "stock": (".main",),
    }),
    ("宽表容器 · 卡内横滚", {"overflow-x": "auto", "max-width": "100%"}, {
        "quant-lab": (".card",),
        "etf": (".card",),
        "stock": (".card",),
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
        # ★ align-items 必须从 flex-start 改成 stretch：窄屏下 flex-direction 转成
        #   column 后，flex-start 会让 .wrap 取 max-content 宽度（= 最宽那张表），
        #   实测 390px 视口下页面宽 1345px —— 整个页面横向滚，手机根本没法看。
        ".shell": {"flex-direction": "column", "align-items": "stretch"},
        ".wrap": {"width": "100%", "min-width": "0"},
        ".side": {"width": "100%", "flex": "none", "position": "static",
                  "max-height": "none", "display": "flex",
                  "align-items": "center", "gap": "8px", "padding": "10px 12px"},
        ".side .brand": {"border": "0", "margin": "0", "font-size": "var(--fs-lg)",
                         "padding": "0 8px 0 2px"},
        ".side .nav-item": {"display": "inline-block"},
        # 移动端放开表格换行：桌面要密排扫读，手机要能读完一行
        "table th": {"white-space": "normal"},
        "table td": {"white-space": "normal", "overflow-wrap": "anywhere"},
    },
    "etf": {
        ".layout": {"flex-direction": "column", "gap": "16px"},
        ".sidebar": dict(_DRAWER),
        ".sidebar.open": {"transform": "translateX(0)"},
        ".menu-btn": {"display": "flex"},
        "table th": {"white-space": "normal"},
        "table td": {"white-space": "normal"},
    },
    "stock": {
        ".sidebar": dict(_DRAWER),
        ".sidebar.show": {"transform": "translateX(0)"},
        ".main": {"margin-left": "0"},
        ".hamburger": {"display": "flex"},
        "table th": {"white-space": "normal"},
        "table td": {"white-space": "normal"},
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
        unify_ink(unify_fonts(unify_type(unify_colors(scope_css(_promote_globals(c, domain), domain), domain))))
        for c in css_blocks
    )
    body = unify_ink(unify_fonts(unify_type(unify_colors(_namespace_ids(body, domain, rename), domain))))

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
